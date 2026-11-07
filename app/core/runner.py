"""Claude Code 运行器：起子进程、读 stream-json、解析结果、杀进程树。

**这是"大脑接口"层**，不是平台层 —— 它只认识 stdin/stdout 与 JSON，
飞书/钉钉/Slack 复用同一份。
"""

import json
import os
import subprocess
import tempfile
import threading
import time
from datetime import datetime

from common import LOGDIR, PROGRESS_RAW_CAP, ROOT, log
from core.text import usage_footer


def _clip(s, n):
    """压成一行、截到 n 字符。气泡里的一行进度，不许换行撑开。"""
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def _short_path(p, limit=48):
    """长路径压成「上一级/文件名」。绝对路径会把气泡那一行挤爆。"""
    s = str(p or "").replace("\\", "/").strip()
    parts = [x for x in s.split("/") if x]
    if not parts:
        return ""
    s = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return s if len(s) <= limit else "…" + s[-(limit - 1):]


# 工具名 → 进度短语。**只做"看得懂现在在干什么"**，不回显完整入参。
# 未列出的工具走最后一行的兜底（新工具出现时也不会哑掉）。
def tool_desc(name, inp):
    """把一次 `tool_use` 压成气泡里的一行进度。"""
    inp = inp or {}
    name = name or "?"
    if name == "Read":
        return "读 " + _short_path(inp.get("file_path"))
    if name in ("Edit", "Write", "MultiEdit"):
        return "改 " + _short_path(inp.get("file_path"))
    if name == "NotebookEdit":
        return "改 " + _short_path(inp.get("notebook_path"))
    if name == "Bash":
        # 命令常常很长且带换行；`description` 是模型自己写的短标签，优先用它
        d = str(inp.get("description") or "").strip()
        return "运行 " + _clip(d or str(inp.get("command") or ""), 40)
    if name == "Grep":
        return "搜 " + _clip(str(inp.get("pattern") or ""), 40)
    if name == "Glob":
        return "找 " + _clip(str(inp.get("pattern") or ""), 40)
    if name in ("Task", "Agent"):
        return "派子任务 " + _clip(str(inp.get("description")
                                       or inp.get("subagent_type") or ""), 34)
    if name == "WebFetch":
        return "取网页 " + _clip(str(inp.get("url") or ""), 40)
    if name == "WebSearch":
        return "搜网 " + _clip(str(inp.get("query") or ""), 40)
    if name == "TodoWrite":
        return "更新待办"
    if name == "Skill":
        return "用技能 " + _clip(str(inp.get("skill") or ""), 34)
    return "用 " + name


class Runner:
    def __init__(self, exe, model, perm_mode, sources, timeout,
                 allowed, disallowed, inject_env, show_usage):
        self.exe = exe
        self.model = model
        self.perm_mode = perm_mode
        self.sources = sources
        self.timeout = timeout
        self.allowed = allowed
        self.disallowed = disallowed
        self.inject_env = inject_env
        self.show_usage = show_usage
        self._lock = threading.Lock()
        self._proc = None
        self._stopped = False
        # 上一次 kill_current 的结果：idle / killed / failed（见 kill_current）
        self.kill_status = "idle"

    def clone(self):
        """**每个任务一份**的副本：配置共用，`_proc` / `_stopped` / `kill_status` 各自独立。

        为什么需要（2026-11-05 改成按会话并行之后）：`kill_current()` 停的是
        `self._proc`，而一个 Runner 只有一个 —— 两个会话同时跑的时候，
        停私聊会把群聊那个一起杀掉（或者反过来）。
        """
        return Runner(self.exe, self.model, self.perm_mode, self.sources,
                      self.timeout, self.allowed, self.disallowed,
                      self.inject_env, self.show_usage)

    def build_env(self):
        """剥掉继承的 ANTHROPIC*/CLAUDE*，再用桥自己 .env 里的值注入。

        理由（实测）：排掉 user 设置后认证就没了；而继承来的环境变量会让
        "认证成功"变成假阳性。桥的真实运行环境本来也不该依赖父进程。
        """
        e = {k: v for k, v in os.environ.items()
             if not (k.upper().startswith("ANTHROPIC") or k.upper().startswith("CLAUDE"))}
        e.update(self.inject_env)
        return e

    def build_cmd(self, session_id):
        # `stream-json` + 增量读：`json` 模式要等进程结束才有输出，**没有拦截点**
        # ⇒ 真进度气泡和审批卡片都做不出来。`result` 事件字段与 `json` 模式完全一致（实测）。
        # ⚠️ `--verbose` 是**硬要求**：不带它 `claude` 直接
        #    `rc=1 / --output-format=stream-json requires --verbose`（实测 2.1.216）。
        cmd = [self.exe, "-p", "--output-format", "stream-json", "--verbose"]
        if self.sources:
            cmd += ["--setting-sources", self.sources]
        if self.perm_mode:
            cmd += ["--permission-mode", self.perm_mode]
        if self.model:
            cmd += ["--model", self.model]
        if self.allowed:
            cmd += ["--allowedTools", self.allowed]
        if self.disallowed:
            cmd += ["--disallowedTools", self.disallowed]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def kill_current(self):
        """终止当前任务。返回 True **仅当确认进程已退出**。

        ⚠️ 早先只要"调 taskkill 前进程还活着"就返回 True —— `taskkill` 失败、
        或者进程根本没死，用户也会收到「已停止（进程树已清理）」。
        现在按 `p.poll()` 复核，并把结果写进 `kill_status`：
        `idle`（本来就没在跑）/ `killed`（确认已退出）/ `failed`（发了但没退）。
        """
        with self._lock:
            p = self._proc
            self._stopped = True
        if p is None or p.poll() is not None:
            self.kill_status = "idle"
            return False
        _kill_tree(p.pid)
        deadline = time.time() + 5
        while time.time() < deadline:
            if p.poll() is not None:
                self.kill_status = "killed"
                return True
            time.sleep(0.1)
        self.kill_status = "failed"
        log.error("[kill] pid=%s 5 秒内没有退出 —— 可能还在跑，别告诉用户已清理", p.pid)
        return False

    def clear_stop(self):
        with self._lock:
            self._stopped = False

    def run(self, prompt, session_id, cwd, on_progress=None, extra_env=None):
        """跑一轮。返回 dict：ok / text / session_id / is_error / denials / usage / 超时·停止标志。

        `on_progress(desc, n_tools)` 每读到一次**工具调用**就回调一次（可能很密，
        节流交给调用方 —— 这里只保证"不阻塞读管道"）。

        **为什么不用 `subprocess.run` 一把拿完整 JSON**（2026-10-29 改）：
        那样只能等进程结束才知道发生了什么，没有拦截点 ⇒ 真进度气泡做不出来，
        将来的审批卡片也做不出来。现在的形状是"三条读线程 + 主线程等退出"：
          · stdout：逐行解析 `stream-json`，**只认** `system/init`、`assistant(tool_use)`、`result`；
          · stderr：必须有人读，否则管道写满会把子进程卡死（这是 `communicate` 替我们做的）；
          · stdin：单独一个线程喂 prompt。prompt 很大时阻塞写会卡住"等退出"那一步。
        """
        cmd = self.build_cmd(session_id)
        log.info("[run] cmd=%s cwd=%s", " ".join(cmd[1:]), cwd)
        t0 = time.time()
        # 审批卡片要靠环境变量把「决策目录 + 发给哪个会话」传给 hook 子进程
        # （实测：hook 能完整继承桥注入的环境变量，DECISIONS §25 E7）
        env = self.build_env()
        if extra_env:
            env.update(extra_env)
        try:
            # CREATE_NO_WINDOW: 桥常驻后别让子进程弹黑框
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            p = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", cwd=cwd, env=env,
                creationflags=flags)
        except FileNotFoundError as e:
            return {"ok": False, "fatal": True,
                    "text": f"找不到 claude 可执行文件：{self.exe}\n{e}"}
        with self._lock:
            self._proc = p
            stop_now = self._stopped      # `/stop` 恰好落在 clear_stop() 与 Popen 之间
        if stop_now:
            # 不补这一枪的话：用户看到"当前没有正在跑的任务"，任务却**真跑完了**
            # （副作用照做），而结果被报成"已停止"扔掉了。
            log.warning("[run] 启动瞬间已收到 /stop，立即终止 pid=%s", p.pid)
            _kill_tree(p.pid)

        acc = {"result": None, "init": None, "events": 0, "last": None,
               "tools": 0, "bad": 0, "raw": [], "raw_bytes": 0, "raw_dropped": 0,
               "stderr": "", "last_usage": None, "last_model": None}
        t_out = threading.Thread(target=self._read_stdout, args=(p, acc, on_progress),
                                 daemon=True, name="run-stdout")
        t_err = threading.Thread(target=self._read_stderr, args=(p, acc),
                                 daemon=True, name="run-stderr")
        t_in = threading.Thread(target=self._feed_stdin, args=(p, prompt),
                                daemon=True, name="run-stdin")
        t_out.start()
        t_err.start()
        t_in.start()

        timed_out = False
        kill_failed = False
        try:
            p.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(p.pid)
            try:
                p.wait(timeout=15)
            except Exception as e:
                kill_failed = True
                log.error("[kill] pid=%s 强杀后仍不退出（%s）⇒ 可能残留进程树，请按 PID 核查",
                          p.pid, e)
        finally:
            with self._lock:
                # 杀不掉的进程**保留引用**，让后续 /stop 还能再试一次；清掉就再也够不着它了
                self._proc = p if kill_failed else None
                stopped = self._stopped
        # 等读线程把尾巴收干净：正常退出会立刻 EOF；被强杀时也应当很快 EOF。
        # ⚠️ 杀不掉的进程不会给 EOF ⇒ 绝不能无限等（那样 /stop 就变成"卡住"）。
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        if t_out.is_alive():
            log.error("[run] pid=%s 退出后 stdout 读线程仍未收尾（残留进程？），按已读到的内容继续",
                      p.pid)
        dur = round(time.time() - t0, 1)

        data = acc["result"] or {}
        init = acc["init"] or {}
        err = acc["stderr"] or ""
        if init.get("session_id"):
            # 这行很有用：**进程中途死掉时**（超时/被杀）也能知道本轮是哪个 session
            log.info("[run] init session=%s model=%s perm=%s tools=%d",
                     init["session_id"], init.get("model"), init.get("permissionMode"),
                     len(init.get("tools") or []))
        if err.strip():
            log.info("[run] stderr: %s", err.strip()[:400])
        log.info("[run] 事件 %d 条（工具 %d，坏行 %d）", acc["events"], acc["tools"], acc["bad"])

        res = {
            "ok": False,
            "text": data.get("result") or "",
            "session_id": data.get("session_id"),
            "is_error": data.get("is_error"),
            "subtype": data.get("subtype"),
            "denials": data.get("permission_denials") or [],
            # ⚠️ 上下文占用取**逐条 assistant 的 usage**（最后一次调用的 prompt 大小），
            #    不能取 result.modelUsage 的累计值 —— 详见 usage_footer() 的说明。
            "usage_footer": (usage_footer(data, acc["last_usage"], acc["last_model"])
                             if self.show_usage else ""),
            "timed_out": timed_out,
            "stopped": stopped and not timed_out,
            "rc": p.returncode,
            "duration": dur,
            "tools": acc["tools"],
            "events": acc["events"],
            "init_session_id": init.get("session_id"),
            "raw_path": None,
            "stream_failed": False,
        }
        # 判成功**只看 is_error**（实测失败那次 subtype 仍报 'success'）；没拿到 result 也算失败
        res["ok"] = (not timed_out) and (not stopped) and bool(data) and not data.get("is_error")
        if not data and not timed_out and not stopped:
            res["stream_failed"] = True
            res["raw_path"] = self._dump_raw_stream(acc)
            where = (f"\n原始流已落盘：`{res['raw_path']}`" if res["raw_path"]
                     else "\n原始流落盘失败，只剩上面这些统计。")
            res["text"] = (
                f"没拿到 claude 的 result 事件（rc={p.returncode}，用时 {dur}s，"
                f"收到 {acc['events']} 条事件，最后一条 `{acc['last']}`，"
                f"已执行 {acc['tools']} 次工具）。\n"
                f"> 语义是**结果未知**：工具可能已经真的跑过了 —— 重试前先核实实际状态，别直接重发。"
                f"{where}"
                + (f"\nstderr: {err.strip()[:300]}" if err.strip() else ""))
            res["is_error"] = True
        log.info("[run] rc=%s is_error=%s 耗时=%ss 超时=%s 停止=%s denials=%d",
                 p.returncode, res["is_error"], dur, timed_out, res["stopped"],
                 len(res["denials"]))
        return res

    # -- 三条读线程（都不许抛出去：抛了没人接，主线程会误判成"正常结束"）--
    def _read_stdout(self, p, acc, on_progress):
        """逐行解析 `stream-json`。

        ⚠️ 其余事件必须**便宜地丢掉**：一个很小的任务里 `system/thinking_tokens`
        就有 44 条（实测），逐条深解析纯属浪费，还会拖慢读管道。
        """
        try:
            for raw in p.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    acc["bad"] += 1
                    continue
                acc["events"] += 1
                t = d.get("type")
                acc["last"] = t if t != "system" else f"system/{d.get('subtype')}"
                # 原始流只为"失败时落盘"留，正常跑完就随 acc 一起扔掉
                b = len(line) + 1
                if acc["raw_bytes"] + b <= PROGRESS_RAW_CAP:
                    acc["raw"].append(line)
                    acc["raw_bytes"] += b
                else:
                    acc["raw_dropped"] += 1
                if t == "result":
                    acc["result"] = d
                elif t == "system":
                    if d.get("subtype") == "init":
                        acc["init"] = d
                elif t == "assistant":
                    msg = d.get("message") or {}
                    u = msg.get("usage") or {}
                    # 只留"最后一次有 token 的 assistant 调用"：它是本轮结束时的
                    # 真实上下文大小。result.modelUsage 是整轮累计，会随工具轮次膨胀。
                    if u and any(u.get(x) for x in (
                            "input_tokens", "cache_read_input_tokens",
                            "cache_creation_input_tokens", "output_tokens")):
                        acc["last_usage"] = u
                        acc["last_model"] = msg.get("model")
                    for c in msg.get("content") or []:
                        if c.get("type") != "tool_use":
                            continue
                        acc["tools"] += 1
                        if on_progress:
                            try:
                                on_progress(tool_desc(c.get("name"), c.get("input")),
                                            acc["tools"])
                            except Exception as e:
                                # 进度是"锦上添花"，它出错绝不能拖垮这一轮
                                log.warning("[run] on_progress 抛错（已忽略）: %s", e)
        except Exception as e:
            log.warning("[run] 读 stdout 中断: %s", e)
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass

    def _read_stderr(self, p, acc):
        """stderr **必须有人读** —— 管道写满会把子进程卡死。只留尾部 4 KB。"""
        buf = []
        size = 0
        try:
            for raw in p.stderr:
                if size < 65536:
                    buf.append(raw)
                    size += len(raw)
        except Exception as e:
            log.info("[run] 读 stderr 中断: %s", e)
        finally:
            acc["stderr"] = "".join(buf)[-4000:]
            try:
                p.stderr.close()
            except Exception:
                pass

    def _feed_stdin(self, p, prompt):
        """喂 prompt 后**立刻关掉** stdin —— 不关的话子进程会一直等输入，永远不结束。"""
        try:
            p.stdin.write(prompt)
        except Exception as e:
            # 进程可能已经被 /stop 杀了；这不是错误，交给上层判定
            log.info("[run] 写 stdin 失败（进程可能已退出）: %s", e)
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass

    def _dump_raw_stream(self, acc):
        """失败时把原始流落盘（用户 2026-10-29 定：**不重跑**，留证据）。

        不重跑的理由：工具副作用可能已经发生了，重跑等于把同一批工具再执行一遍。
        """
        try:
            d = os.path.join(LOGDIR, "raw-stream")
            os.makedirs(d, exist_ok=True)
            fn = os.path.join(d, datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
            with open(fn, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(list(acc["raw"])))
                f.write("\n")
            log.warning("[run] 原始流已落盘 %s（%d 字节，丢弃 %d 行）",
                        fn, acc["raw_bytes"], acc["raw_dropped"])
            try:
                return os.path.relpath(fn, ROOT)
            except Exception:
                return fn
        except Exception as e:
            log.error("[run] 原始流落盘失败: %s", e)
            return None


def approval_configured(cc_cwd, sources):
    """静态检查：**Claude 到底会不会加载这个 hook**。

    为什么还要这一道（2026-10-29 复核 #2②）：`approval_selfcheck` 只是桥自己把
    `python tools/approval_hook.py` 跑一遍 —— 它证明"脚本能跑"，**不证明 Claude 会调它**。
    把 `.claude/settings.json` 删掉、或者 `CC_SETTING_SOURCES` 里没有 `project`，
    自检照样通过，而实际一个审批卡片都不会弹。那就又成了"假闸门"。

    查三件事：
      ① `.claude/settings*.json` 存在且是合法 JSON
         （⚠️ 校验失败时 CLI 是**静默忽略**整份文件，`-p` 模式不弹提示）；
      ② 里面有 `hooks.PreToolUse`，且命令串指向 `approval_hook.py`；
      ③ `--setting-sources` 含 `project`。（⚠️ `CC_SETTING_SOURCES` **留空不等于不传**：
         `cfg()` 把空值当未设置 ⇒ 落到 `main()` 的默认值 `project,local`，见那里。
         只有显式写成不含 `project` 的值才会真的把它排掉。）

    返回 (ok, 说明)。说明文字会被打进日志，所以写成"现在是什么状态"，别只写"失败"。
    """
    srcs = [s.strip() for s in (sources or "").split(",") if s.strip()]
    if srcs and "project" not in srcs:
        return False, (f"CC_SETTING_SOURCES={sources!r} 不含 project ⇒ "
                       f"{os.path.join(cc_cwd, '.claude')} 下的 settings 不会被加载，hook 不会跑")
    d = os.path.join(cc_cwd, ".claude")
    cands = [os.path.join(d, n) for n in ("settings.json", "settings.local.json")]
    found = []
    for p in cands:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                found.append((p, json.load(f)))
        except Exception as e:
            return False, (f"{p} 不是合法 JSON（{e}）—— settings 校验失败会被**静默忽略**，"
                           f"等于 hook 不存在")
    if not found:
        return False, (f"找不到 {cands[0]} —— 没有它就没有 PreToolUse hook，"
                       f"审批一张卡都不会弹")
    for p, data in found:
        for group in ((data.get("hooks") or {}).get("PreToolUse") or []):
            for item in (group.get("hooks") or []):
                cmd = str(item.get("command") or "")
                if "approval_hook.py" in cmd:
                    return True, f"{os.path.basename(p)} 已注册 PreToolUse（{cmd[:70]}）"
    return False, (f"{os.path.basename(found[0][0])} 里没有注册 tools/approval_hook.py 的 "
                   f"PreToolUse hook")


def approval_selfcheck(cc_cwd, approval_dir, wait_sec=3.0):
    """运行时自检：确认**那条 hook 脚本真的跑得起来**。

    为什么必须自检（`DECISIONS` §25）：harness 的所有异常路径都是**放行**
    （超时、hook 崩溃、字段写错）。如果 hook 根本没跑起来（比如 PATH 里没有
    `python`），审批会**静默失效**而桥自己完全不知道 —— 那就等于假装有闸门。
    所以：跑一次假的 Write 事件，要求它在临时目录里吐出 `.req.json`。

    用 `shell=True` + 和 `.claude/settings.json` 里**同一个命令串**，能抓到
    "这条命令本身跑不起来"（PATH 没有 python、路径不对、解释器坏了）。

    ⚠️ 但它**不证明 Claude 会调它** —— 那是 `approval_configured` 的活。两道都要过。
    """
    script = os.path.join(cc_cwd, "tools", "approval_hook.py")
    if not os.path.isfile(script):
        return False, f"找不到 {script}"
    tmp = tempfile.mkdtemp(prefix="ccbridge-selfcheck-")
    env = dict(os.environ)
    env.update({
        "CC_APPROVAL_DIR": tmp,
        "CC_APPROVAL_TARGET": "selfcheck|2|selfcheck",
        "CC_APPROVAL_WAIT_SEC": "5",
        "PYTHONIOENCODING": "utf-8",
    })
    ev = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write",
                     "tool_input": {"file_path": "selfcheck.txt"},
                     "session_id": "selfcheck", "cwd": cc_cwd})
    try:
        p = subprocess.Popen("python tools/approval_hook.py", shell=True,
                             cwd=cc_cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
        p.stdin.write(ev)
        p.stdin.close()
    except Exception as e:
        return False, f"hook 起不来：{e}"
    t0 = time.time()
    found = None
    while time.time() - t0 < wait_sec:
        try:
            names = [n for n in os.listdir(tmp) if n.endswith(".req.json")]
        except FileNotFoundError:
            names = []
        if names:
            found = names[0]
            break
        time.sleep(0.1)
    if not found:
        err = ""
        try:
            err = (p.stderr.read() or "")[:300]
        except Exception:
            pass
        try:
            _kill_tree(p.pid)
        except Exception:
            pass
        return False, f"{wait_sec}s 内没有写出审批请求（hook 没跑起来？）{err}"
    # 放它走，别留一个挂着等 5 秒的进程
    try:
        with open(os.path.join(tmp, found[:-len(".req.json")] + ".decision"),
                  "w", encoding="utf-8") as f:
            f.write("deny\n")
        p.wait(timeout=5)
    except Exception:
        try:
            _kill_tree(p.pid)
        except Exception:
            pass
    return True, "ok"


def _kill_tree(pid):
    """Windows 上必须杀进程树 —— Claude 会拉起子进程。

    ⚠️ **要看返回码**：`taskkill` 失败（进程已换主、权限不足、PID 被复用）时
    它只往 stderr 说话、退出码非 0，早先这里把结果整个吞掉了。
    返回 True = 命令自报成功（**不等于进程已退出**，那要 `p.poll()` 复核）。
    """
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        if r.returncode == 0:
            log.info("[kill] 已终止进程树 pid=%s", pid)
            return True
        log.warning("[kill] taskkill 退出码=%s pid=%s stderr=%s", r.returncode, pid,
                    (r.stderr or b"")[:200])
        return False
    except Exception as e:
        log.warning("[kill] 失败 pid=%s: %s", pid, e)
        return False
