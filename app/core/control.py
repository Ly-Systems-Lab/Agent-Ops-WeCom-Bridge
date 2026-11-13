"""控制目录：用**文件**驱动桥。

**为什么是文件，而不是再写一个 HTTP 端口**（2026-11-13）：
    · 这个工程已经有一个同范式的通道 —— 审批卡（`data/approvals*/`：
      hook 写请求文件、桥扫、桥写决定文件）。多一个本地端口就多一份
      "绑定地址 / 鉴权 / 防火墙"的坑，而收益只是省一次 `listdir`；
    · **任何 Agent 都能用**：会写文件就会用（Claude Code / Codex / OpenCode /
      一个 bat 脚本 / 人）。不需要它认识我们的 Python 包，也不需要它有 shell；
    · 可观测：投递什么、什么时候、结果如何，全在磁盘上，事后能查。

协议（都是 JSON，UTF-8，不带 BOM）：

    data/control/inbox/<id>.json   投递
        {"id": "<id>", "cmd": "switch_platform", "args": {"to": "feishu"},
         "by": "<谁发起的>", "ts": 1763000000}
    data/control/done/<id>.json    回执（桥写）
        {"id": "<id>", "cmd": "...", "ok": true, "text": "给人看的一段话",
         "ts": 1763000001}

命令是**固定白名单**（见 `CoreMixin._control_handlers`），不做任何 eval ——
"能往这个目录写文件"本身就等于"能改 `.env`"，所以不该再扩大成"能执行任意代码"。

⚠️ 认领用 `os.replace`（同一个卷上原子），不是"读走再删"：
    同一时刻可能有 CLI 和某个 Agent 同时投递，也可能有两个进程在看这个目录。
"""

import json
import os
import time

from common import log

# 单个命令文件的上限。正常一条几百字节；给到 64 KB 是防"有人把整个日志丢进来"。
MAX_CMD_BYTES = 64 * 1024


def control_dir(root):
    return os.path.join(root, "data", "control")


def _paths(base):
    return (os.path.join(base, "inbox"),
            os.path.join(base, "done"),
            os.path.join(base, ".work"))


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


# ---------------- 投递端（CLI / 别的 Agent 用） ----------------

def submit(base, cmd, args=None, by="", wait=0.0):
    """投一条命令，返回 `(id, done_dict_or_None)`。

    `wait=0` 只投不等 —— 给"我只想让它发生"的场景（MCP 工具、脚本）。
    `wait>0` 等回执，超时返回 None（**结果未知，不是失败** —— 和平台发送同一条语义）。
    """
    inbox, done, work = _paths(base)
    for d in (inbox, done, work):
        os.makedirs(d, exist_ok=True)
    cid = f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}-{int(time.time() * 1000) % 1000}"
    payload = {"id": cid, "cmd": cmd, "args": args or {},
               "by": by or "", "ts": time.time()}
    _atomic_write(os.path.join(inbox, cid + ".json"), payload)
    if wait <= 0:
        return cid, None
    return cid, wait_done(base, cid, wait)


def wait_done(base, cid, timeout):
    _, done, _ = _paths(base)
    p = os.path.join(done, cid + ".json")
    deadline = time.time() + float(timeout)
    while time.time() < deadline:
        try:
            with open(p, encoding="utf-8") as f:
                return json.load(f)
        except FileNotFoundError:
            pass
        except Exception as e:                      # noqa: BLE001（半截 JSON：再等一拍）
            log.debug("[control] 回执读不出来（%s），继续等", e)
        time.sleep(0.2)
    return None


# ---------------- 消费端（桥里跑） ----------------

class ControlInbox:
    """轮询 `inbox/`，认领、执行、写回执。一个进程一份，跑在旁路线程上。"""

    def __init__(self, base, handlers, log_fn=None, poll_sec=0.5, keep_done_days=2):
        self.base = base
        self.inbox, self.done, self.work = _paths(base)
        self.handlers = handlers or {}
        self._log = log_fn or log
        self.poll_sec = float(poll_sec or 0.5)
        self.keep_done_days = float(keep_done_days or 2)
        self.stop = False
        self._last_prune = 0.0
        # 三个子目录先建出来：认领是 `os.replace` 到 `.work/`，目录不在就**静默跳过**
        # （异常被当成"被别人先拿走了"吞掉）—— 那会让投递方永远等不到回执。
        for d in (self.inbox, self.done, self.work):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError:
                pass

    def loop(self):
        """常驻。`stop` 由看门狗/退出路径置位。"""
        for d in (self.inbox, self.done, self.work):
            try:
                os.makedirs(d, exist_ok=True)
            except OSError as e:
                # 建不出目录 = 这条路用不了。**如实说**，别让 CLI 等到超时还以为桥死了。
                self._log.error("[control] 建不出 %s：%s —— 控制目录不可用", d, e)
                return
        self._log.info("[control] 控制目录 = %s（命令：%s）",
                       self.base, "、".join(sorted(self.handlers)))
        while not self.stop:
            try:
                self.poll_once()
            except Exception as e:                 # noqa: BLE001
                # 一次处理失败不能把整条控制通道带走（和 worker 同一条理由）。
                self._log.exception("[control] 处理失败：%s", e)
            time.sleep(self.poll_sec)

    def poll_once(self):
        """返回这一拍处理了几条。单测直接调它，不需要起线程。"""
        self._prune_done()
        n = 0
        try:
            names = sorted(os.listdir(self.inbox))
        except FileNotFoundError:
            return 0
        for name in names:
            if not name.endswith(".json"):
                continue
            src = os.path.join(self.inbox, name)
            dst = os.path.join(self.work, name)     # 认领：原子改名
            try:
                os.replace(src, dst)
            except OSError:
                continue                            # 被别人先拿走了
            try:
                self._handle(dst, name)
                n += 1
            finally:
                try:
                    os.remove(dst)
                except OSError:
                    pass
        return n

    def _handle(self, path, name):
        cid = name[:-5]
        payload, err = self._load(path)
        if err:
            self._reply(cid, "", False, f"❌ 命令文件读不出来：{err}")
            return
        cid = str(payload.get("id") or cid)
        cmd = str(payload.get("cmd") or "")
        args = payload.get("args") or {}
        by = str(payload.get("by") or "")
        fn = self.handlers.get(cmd)
        if fn is None:
            self._reply(cid, cmd, False,
                        f"❌ 不认识的命令 `{cmd}`。可用："
                        + "、".join(f"`{k}`" for k in sorted(self.handlers)))
            return
        if not isinstance(args, dict):
            self._reply(cid, cmd, False, "❌ `args` 必须是对象")
            return
        self._log.info("[control] 收到 %s by=%s args=%s", cmd, by or "(未署名)", args)
        post = None
        try:
            res = fn(args, by)
            if isinstance(res, tuple) and len(res) == 3:
                ok, text, post = res
            else:
                ok, text = res
        except Exception as e:                     # noqa: BLE001
            self._log.exception("[control] %s 执行失败", cmd)
            ok, text = False, f"❌ 执行失败：{type(e).__name__}: {e}"
        # ⚠️ **先写回执、再跑后续动作**：`switch_platform` 的后续动作会把进程换掉，
        #    反过来写的话，投递方永远等不到回执（看起来像"没生效"）。
        self._reply(cid, cmd, bool(ok), str(text or ""))
        if callable(post):
            try:
                post()
            except Exception as e:                 # noqa: BLE001
                self._log.exception("[control] %s 的后续动作失败：%s", cmd, e)

    def _reply(self, cid, cmd, ok, text):
        try:
            os.makedirs(self.done, exist_ok=True)
            _atomic_write(os.path.join(self.done, cid + ".json"),
                          {"id": cid, "cmd": cmd, "ok": bool(ok),
                           "text": text, "ts": time.time()})
        except Exception as e:                     # noqa: BLE001
            self._log.error("[control] 回执写不出去（%s）：%s", cid, e)

    def _load(self, path):
        try:
            if os.path.getsize(path) > MAX_CMD_BYTES:
                return None, f"文件超过 {MAX_CMD_BYTES} 字节"
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
        except Exception as e:                     # noqa: BLE001
            return None, f"{type(e).__name__}: {e}"
        if not isinstance(d, dict):
            return None, "顶层不是对象"
        return d, ""

    def _prune_done(self):
        """回执别无限堆 —— 一天清一次就够（按文件 mtime，这里不是台账）。"""
        now = time.time()
        if now - self._last_prune < 3600:
            return
        self._last_prune = now
        cut = now - self.keep_done_days * 86400
        try:
            for name in os.listdir(self.done):
                p = os.path.join(self.done, name)
                try:
                    if os.path.getmtime(p) < cut:
                        os.remove(p)
                except OSError:
                    pass
        except FileNotFoundError:
            pass