"""内核：队列 / 会话 / 代次 / 忙碌语义 / 指令 / 审批状态机 / worker / 主循环。

**本模块不认识任何聊天平台**：所有对外动作都走 `core/contract.py` 里的 `plat_*`。
判据：用 `tools/check_layers.py` 复核，平台帧名 / 错误码 / WebSocket 出现 0 次。
"""

import json
import os
import queue
import threading
import time

from common import APPROVAL_POLL_SEC, PROGRESS_HEARTBEAT_SEC, knob, log
from core.model_store import valid_model_name
from core.runner import approval_configured
from core.text import _looks_like_resume_failure, safe_to_retry_resume


class ProgressPump:
    """收 `Runner` 报来的工具进度，节流后更新**流式气泡**（与 ack/正文同一个 id）。

    **为什么必须是单独一个线程**：平台回复帧要等平台回执（最多 `SEND_WAIT_SEC`
    15 秒）。如果直接在读 stdout 的线程上发帧，一次网络抖动就会让 stdout 管道没人读、
    写满 ⇒ **把 claude 子进程堵死**。所以：读线程只调 `update()`（写字符串 + 置事件，
    不碰网络），真正发帧在这个线程里。

    节流口径（用户 2026-10-29 定）：
      · **只在「工具/目标变了」时推** —— 同一条命令连跑 5 次只算一次；
      · 没变化时每 `heartbeat` 秒补一条兜底，让用户知道它还活着。
    一帧就是一条限流额度（30 条/分钟、1000 条/小时），所以宁少勿多。
    """

    def __init__(self, st, chatid, chat_type, heartbeat_sec=PROGRESS_HEARTBEAT_SEC):
        self.st = st
        self.chatid = chatid
        self.chat_type = chat_type
        self.hb = max(0.0, float(heartbeat_sec or 0))
        self.started = time.time()
        self._lock = threading.Lock()
        self._desc = ""
        self._tools = 0
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._last_sent = 0.0
        self.sent = 0            # 真发出去的帧数（测试/诊断用）
        self._t = threading.Thread(target=self._loop, daemon=True, name="progress-pump")
        self._t.start()

    def _content(self, desc, n):
        head = f"⏳ 正在跑（{time.time() - self.started:.0f}s · 第 {n} 步）"
        return head + (f"\n🛠 {desc}" if desc else "")

    def _push(self, desc, n):
        self._last_sent = time.time()
        r = self.st.send(self._content(desc, n), finish=False)
        if r.get("dead"):
            # 气泡已经不可用（超 10 分钟窗口 / 已定稿）⇒ 别再拿它发任何东西
            log.warning("[progress] 气泡不可用（%s）⇒ 停止真进度", r.get("errmsg"))
            self._stop.set()
            return
        if r["sent"]:
            self.sent += 1
        elif r["errcode"] is None:
            log.warning("[progress] 这一帧没回执（结果未知），继续用同一个 id")

    def _loop(self):
        while not self._stop.is_set():
            timeout = None
            if self.hb:
                timeout = max(0.05, self.hb - (time.time() - self._last_sent))
            fired = self._dirty.wait(timeout=timeout)
            if self._stop.is_set():
                return
            with self._lock:
                desc, n = self._desc, self._tools
            if fired:
                self._dirty.clear()
                self._push(desc, n)
            elif self.hb and n:
                self._push(desc, n)      # 兜底心跳：没新进展也要让用户知道它还活着

    def update(self, desc, n_tools=0):
        """读线程调用。**只在变化时才置位** —— 这就是"变化时推"。"""
        with self._lock:
            if desc == self._desc and n_tools == self._tools:
                return
            self._desc, self._tools = desc, int(n_tools or 0)
        self._dirty.set()

    def close(self):
        """收口。**必须在正文定稿之前调**，否则迟到的进度会盖掉答案。"""
        self._stop.set()
        self._dirty.set()        # 让 wait 立刻返回
        if self._t.is_alive():
            self._t.join(timeout=knob("SEND_WAIT_SEC") + 2)


class CoreMixin:
    """平台无关的那一半。与适配器共用同一个实例，但只通过 `plat_*` 与外界交互。"""

    def _cwd_for(self, chat_type):
        """这个会话该在哪个目录干活。

        `chat_type` 用的是平台回调里的语义：**1 = 单聊，2 = 群聊**（见
        `_handle_callback`）。单聊走 `CC_CWD_DM`，其余走 `CC_CWD`。
        ⚠️ 单聊的 `chatid` 其实是 `userid`（回调里不带 chatid），所以两者
        天然是两套 `(chat_id, user_id)`，不会互相串会话。
        """
        try:
            single = int(chat_type) == 1
        except (TypeError, ValueError):
            single = False
        return self.cc_cwd_dm if single else self.cc_cwd


    def _session_for(self, chatid, chat_type, userid):
        """取这一轮的 `(session_id, cwd)`；路由变了就清掉旧会话。

        Claude 的会话文件是**按 cwd 分目录**存的 ⇒ 换目录后旧 session_id 一定
        resume 不上。与其让它失败再兜底，不如当场换目录 + 新建会话，并留日志。
        """
        want = self._cwd_for(chat_type)
        # 模型专用的工作目录优先（`data/model_profiles.json` 的 `cwd`）。
        # 本地 9B 只有 32K 上下文，而默认目录的项目 CLAUDE.md 可能就有几万字符
        # ⇒ 不换目录的话，模型光读项目说明就撑爆了。
        # ⚠️ 切模型同时切目录时，下面那段"路由不一致 ⇒ 换目录并新建会话"会生效：
        #    这是**对的**，Claude 的会话文件按 cwd 分目录存，换目录本来 resume 不上。
        prof = getattr(self.runner, "profiles", None)
        if prof is not None:
            try:
                cur_model = self.runner.live_model() \
                    if hasattr(self.runner, "live_model") else ""
            except Exception:                      # noqa: BLE001
                cur_model = ""
            want = prof.cwd_for(cur_model) or want
        session_id, cwd = self.store.get_session(chatid, userid, want)
        if cwd != want:
            log.warning("[job] cwd 路由与记录不一致（%s → %s），换目录并新建会话",
                        cwd, want)
            self.store.save_session(chatid, userid, None, want)
            return None, want
        return session_id, cwd


    def _is_busy(self, chatid=None, chat_type=None):
        """不传参数 = 全局还有没有任务在跑（看门狗/老单测用）；
        传了会话 = **这个会话**有没有任务在跑（入站判断用）—— 两者语义不同，
        因为现在是"同一会话串行、不同会话并行"。"""
        with self._job_lock:
            if chatid is None:
                return bool(self._current)
            return (chatid, chat_type) in self._current


    def _job_key(self, chatid, chat_type):
        return (chatid, chat_type)


    def _attach_runner(self, chatid, chat_type):
        """给这一轮任务配一份独立的 Runner（停任务时只停这一个会话的）。"""
        # 老的单测 FakeRunner 没有 `clone()`（它是这次并发改造才需要的）；
        # 它们本来也跑不到真进程，共用一份即可。
        r = self.runner.clone() if hasattr(self.runner, "clone") else self.runner
        with self._job_lock:
            self._runners[(chatid, chat_type)] = r
        return r


    def _kill_job(self, chatid, chat_type):
        """只停**这个会话**的任务。返回 (killed, status)。"""
        with self._job_lock:
            r = self._runners.get((chatid, chat_type))
        if r is None:
            return False, "idle"
        killed = r.kill_current()
        return killed, getattr(r, "kill_status", "?")


    def _kill_all(self):
        """进程退出时用：把所有在跑的任务都停掉。"""
        with self._job_lock:
            rs = list(self._runners.values())
        killed = False
        for r in rs:
            try:
                killed = r.kill_current() or killed
            except Exception as e:
                log.warning("[kill] 终止失败：%s", e)
        return killed


    def _epoch_of(self, chatid, userid):
        with self._epoch_lock:
            return self._epoch.get((chatid, userid), 0)


    def _bump_epoch(self, chatid, userid):
        with self._epoch_lock:
            k = (chatid, userid)
            self._epoch[k] = self._epoch.get(k, 0) + 1


    def _stop_text(self, killed, status=None):
        """按 runner 的**实际**结果给话。`failed` 不能说成"已清理"。

        `kill_status` 是 `kill_current()` 复核过 `p.poll()` 之后写的：
        `killed` = 确认退出；`failed` = 命令发了但进程没退；`idle` = 本来就没在跑。
        （老的单测 FakeRunner 只返回 bool、不写 kill_status，这里照样按 killed 处理。）
        """
        if status is None:
            status = getattr(self.runner, "kill_status", "")
        if killed or status == "killed":
            return "⏹ 已停止当前任务（进程树已清理）。"
        if status == "failed":
            return ("⚠️ 停止命令已发出，但**进程 5 秒内没有退出** —— 它可能还在跑。\n\n"
                    "> 请到电脑上按 PID 确认（`tasklist /FI \"PID eq <pid>\"`），"
                    "别以为已经停了。")
        return "当前没有正在跑的任务。"


    def _handle_control(self, cmd, chatid, chat_type, userid, response_url):
        if cmd == "/new":
            self.store.clear_session(chatid, userid)
            self._last_usage.pop((chatid, userid), None)   # 旧上下文读数不能留着
            self._bump_epoch(chatid, userid)     # 让**正在跑**的旧任务别把旧会话写回来
            log.info("[ctl] /new user=%s", userid)
            self.plat_reply(response_url, "已开新会话：下一轮不会记得之前的内容。")
            return
        if cmd == "/stop":
            # ⚠️ **先收口待审批，再杀进程。** 反过来的话中间有一个窗口：进程正在死、
            # 而卡片仍然"有效"，用户点下去会**真的写进一条 allow**
            # （2026-11-07 实测：/stop 之后 9 秒点「允许」，日志里出现 `决定=allow`）。
            self._cancel_pending_approvals(chatid, chat_type, "用户 /stop")
            killed, status = self._kill_job(chatid, chat_type)
            log.info("[ctl] /stop 终止=%s 状态=%s chat=%s/%s", killed, status,
                     chatid, chat_type)
            self.plat_reply(response_url, self._stop_text(killed, status))
            return
        if cmd == "/status":
            # 改成**卡片**：一眼能看，还带三个按钮（刷新 / 清上下文 / 停止）。
            # 卡片发不出去就退回文本 —— 状态是"必须能看到"的东西，不能因为卡片失败就沉默。
            self._status_async(chatid, chat_type, userid)
            return

    # -- /model：模型的唯一真源是 data/model.json --


    def _model_async(self, arg, chatid, chat_type, userid, response_url):
        """在旁路线程里处理 `/model`。

        **为什么不能同步**：切换要先做一次真实探测（3~10 秒）。放在回调线程上
        会把后面所有消息一起堵住 —— 而 `/stop` 也在同一条线上。
        """
        def _go():
            try:
                msg = self._model_command(arg, userid)
            except Exception as e:                 # noqa: BLE001
                log.exception("[model] /model 处理失败")
                msg = f"⚠️ 处理 `/model` 时出错：{type(e).__name__}: {e}"
            self.plat_reply(response_url, msg)
        threading.Thread(target=_go, daemon=True, name="model-cmd").start()


    def _model_command(self, arg, userid):
        ms = getattr(self, "model_store", None)
        if ms is None:
            return ("本进程没有启用模型热切换（没有 model_store）。\n\n"
                    "> 模型仍由 `.env` 的 `CC_MODEL` 决定，改完需要重启桥。")
        snap = ms.snapshot()
        cur = snap["current"]
        arg = (arg or "").strip()

        if not arg or arg.lower() in ("list", "ls"):
            return self._model_list_text(snap)

        if arg.lower() in ("reset", "default"):
            ok, msg = ms.reset(actor=userid)
            if not ok:
                return f"❌ {msg}"
            log.info("[ctl] /model reset by %s", userid)
            return f"✅ {msg}\n\n> 下一条消息起生效。发 `/model` 看当前值。"

        name = arg
        if arg.isdigit():
            i = int(arg)
            cands = snap["candidates"]
            if not (1 <= i <= len(cands)):
                return (f"❌ 编号 {i} 不在 1~{len(cands)} 之间。\n\n"
                        f"> 发 `/model` 看列表。")
            name = cands[i - 1]

        ok, why = valid_model_name(name)
        if not ok:
            return f"❌ 模型名不合法：{why}"
        if name == cur:
            return f"当前已经是 `{name}`，没有改动。"

        # ⚠️ **先探测，通过才写** —— 错的模型永远进不了真源。
        probe = getattr(self.runner, "probe", None)
        if probe is None:
            return ("❌ 这个 Runner 不支持探测（老版本或测试桩）。\n\n"
                    "> 为了不把没验证过的值写进真源，这次切换已拒绝。")
        # 探测也要在**这个模型自己的目录**里跑：本地模型的项目说明和云端不是同一份，
        # 在别的目录探测通过，不代表真正跑的时候能起来。
        probe_cwd = self.cc_cwd
        prof = getattr(self.runner, "profiles", None)
        if prof is not None:
            probe_cwd = prof.cwd_for(name) or probe_cwd
        ok, detail = probe(name, cwd=probe_cwd)
        if not ok:
            return (f"❌ `{name}` **不可用**：{detail}\n\n"
                    f"> 当前模型没变，仍是 `{cur or '（未设置）'}`。\n"
                    f"> 发 `/model` 看列表。")

        ok, msg = ms.set(name, actor=userid)
        if not ok:
            return f"❌ {msg}"
        log.info("[ctl] /model 切换 %r → %r by %s（探测 %s）", cur, name, userid, detail)
        return (f"✅ {msg}（探测通过，{detail}）\n\n"
                f"> **下一条消息起生效**；正在跑的任务不受影响。\n"
                f"> 这是桥的全局设置，群聊和私聊一起变。")


    def _model_list_text(self, snap):
        lines = [f"🧠 **当前模型**：`{snap['current'] or '（未设置）'}`"]
        if snap["updated_at"]:
            by = f"，by {snap['updated_by']}" if snap["updated_by"] else ""
            lines.append(f"> 更新于 {snap['updated_at']}{by}")
        if not snap["file_ok"]:
            # 采信了兜底值，就得说出来 —— 否则用户以为"我设的那个还在跑"。
            lines.append(f"> ⚠️ 真源文件有问题：{snap['file_error']}（已用兜底值）")
        lines.append("")
        cands = snap["candidates"]
        prof = getattr(self.runner, "profiles", None)
        if cands:
            lines.append("**可选**（来自 `.env` 的 `CC_MODELS`）：")
            for i, c in enumerate(cands, 1):
                label = prof.label_for(c) if prof is not None else ""
                lines.append(f"{i}. `{c}`"
                             + (f"  —— {label}" if label else "")
                             + ("  ← 当前" if c == snap["current"] else ""))
            lines.append("")
            lines.append("发 `/model <编号>` 或 `/model <名字>` 切换；"
                         "也可以直接发任意模型名（会先探测一次）。")
        else:
            lines.append("没有配置 `CC_MODELS` 白名单。")
            lines.append("发 `/model <模型名>` 直接切换（会先探测一次）。")
        if snap["seed"]:
            lines.append("")
            lines.append(f"`/model reset` 回到 `.env` 的默认值 `{snap['seed']}`。")
        return "\n".join(lines)


    # -- /status 卡片 --


    def _status_pairs(self, chatid, chat_type, userid):
        """状态键值对。文本版和卡片版都用它，保证两处**永远一致**。

        ⚠️ 默认 cwd 必须按 `chat_type` 取 —— 不然私聊在还没跑过第一轮时，
        `/status` 会显示群聊那个目录。
        """
        sid, cwd = self.store.get_session(chatid, userid,
                                          self._cwd_for(chat_type))
        with self._job_lock:
            cur = dict(self._current.get((chatid, chat_type)) or {}) or None
            others = len(self._current)
        usage = (self._last_usage.get((chatid, userid)) or "").replace("📊 ", "").strip()
        # 模型走真源（可能已经被 `/model` 改过）。取不到就如实写"由 Runner 决定"，
        # 不编造 —— 这张卡上的每一项都必须是"现在真的这样"。
        try:
            model = self.runner.live_model() if hasattr(self.runner, "live_model") \
                else getattr(self.runner, "model", "")
        except Exception:                          # noqa: BLE001
            model = ""
        return [
            ("队列", f"{self._q.qsize()} 条"),
            ("当前任务",
             ("运行中 %.0f 秒" % (time.time() - cur["started"])) if cur
             else (f"空闲（另有 {others} 个会话在跑）" if others else "空闲")),
            ("上下文", usage or "—（重启后首轮还没跑）"),
            ("会话", (sid or "")[:8] or "（无，下一轮新建）"),
            ("档位", f"{self.runner.perm_mode or '默认'} / 超时 {self.runner.timeout}s"),
            ("模型", model or "（未设置，用 claude 默认）"),
            ("工作区", cwd),
        ]


    def _status_text(self, chatid, chat_type, userid):
        return ("**/status**\n"
                + "\n".join(f"> {k}：{v}"
                            for k, v in self._status_pairs(chatid, chat_type, userid)))


    def _status_async(self, chatid, chat_type, userid):
        """在旁路线程里发状态卡（会等平台回执，不能占读线程）。"""
        def _go():
            r = self.plat_send_card(chatid, chat_type,
                               self._status_card(chatid, chat_type, userid))
            if not r["sent"]:
                log.warning("[card] /status 卡片未送达（%s），退回文本", r["errmsg"])
                self.plat_send_chunks(chatid, chat_type,
                                 self._status_text(chatid, chat_type, userid))
        threading.Thread(target=_go, daemon=True).start()


    def _approval_env(self, chatid, chat_type, userid):
        """这一轮要注入给 claude 子进程的环境变量（hook 靠它找目录 + 找会话）。

        ⚠️ **私聊默认也注入**（`CC_APPROVAL_DM=1`）⇒ 群聊和私聊走同一套弹卡流程。
        设为 0 才是“私聊全自动”的显式选择：此时不注入 `CC_APPROVAL_DIR`，
        目标工作区的 hook 若按约定在无 env 时放行，就不会拦人
        （见 `tools/approval_hook.py` 开头：它是可随仓库发布的空壳）。
        ⚠️ 只对“无 env 就 fail-open”的 hook 成立；换用别的 hook 时，行为由它决定。
        """
        if not self.approval_dir:
            return None
        try:
            single = int(chat_type) == 1
        except (TypeError, ValueError):
            single = False
        if single and not self.approval_dm:
            return None
        e = {
            "CC_APPROVAL_DIR": self.approval_dir,
            "CC_APPROVAL_WAIT_SEC": str(int(self.approval_wait_sec or 120)),
            "CC_APPROVAL_TARGET": f"{chatid}|{chat_type}|{userid}",
        }
        if self.approval_allow:
            e["CC_APPROVAL_ALLOW"] = self.approval_allow
        return e


    def _check_gate(self, cwd):
        """这一轮会注入 `CC_APPROVAL_DIR` 时，确认**这个目录**真的注册了 hook。

        ⚠️ 为什么必须按目录查（2026-11-09 实测踩到）：模型可以带自己的 cwd
        （`data/model_profiles.json` 的 `cwd`），而 hook 是挂在
        `<cwd>/.claude/settings.json` 里的。启动时只检查了 `CC_CWD` ——
        切到别的目录后就可能"注入了 CC_APPROVAL_DIR、但没人读"，
        **审批静默失效，日志里一片安静**。
        那次的具体形态：某个工作目录的 hook 命令写成了反斜杠路径，被 bash 吃掉 ⇒
        命令起不来 ⇒ harness 按放行处理 ⇒ 工具直接执行了。
        """
        if not self.approval_dir:
            return
        checked = getattr(self, "_gate_checked", None)
        if checked is None:
            checked = self._gate_checked = set()
        key = os.path.normcase(os.path.abspath(cwd or "."))
        if key in checked:
            return
        checked.add(key)
        try:
            ok, why = approval_configured(cwd, getattr(self.runner, "sources", ""))
        except Exception as e:                     # noqa: BLE001
            log.error("[approval] 检查 %s 的 hook 失败：%s: %s", cwd, type(e).__name__, e)
            return
        if ok:
            log.info("[approval] %s 的审批 hook 检查通过（%s）", cwd, why)
        else:
            log.error("[approval] ⚠️ %s **没有可用的审批 hook**：%s"
                      " ⇒ 这个目录里的工具调用**不会弹卡**"
                      "（CC_APPROVAL_DIR 已注入，但没人读）。",
                      cwd, why)


    def _approval_decide(self, rid, allow, why=""):
        """把决定写成文件交给 hook —— **只有 hook 真读到了才算数**。"""
        path = os.path.join(self.approval_dir, rid + ".decision")
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("allow\n" if allow else "deny\n")
            log.info("[approval] rid=%s 决定=%s %s", rid, "allow" if allow else "deny", why)
            return True
        except Exception as e:
            log.error("[approval] 写决定失败 rid=%s: %s", rid, e)
            return False


    def _approval_resolve(self, rid):
        """标成「已处理」，**不是删除**。

        删掉的话，只要 `.req.json` 还在（hook 每 0.2 秒才轮询一次），
        下一轮扫描就会把同一个 rid 当成新请求、再发一张卡 —— 2026-11-05 实测：
        用户点掉第一张后 1 秒内又冒出一张同内容的卡，点它提示"已失效"。
        真正的清理交给"请求文件消失"那条分支。
        """
        with self._approval_lock:
            info = self._approvals.get(rid)
            if info is not None:
                info["resolved"] = True

    def _cancel_pending_approvals(self, chatid, chat_type, why="任务已停止"):
        """把某会话**所有还没处理**的审批请求按拒绝收口；能改卡就把卡也收掉。

        为什么 `/stop` 必须走这一步（2026-11-07 真机实测的漏洞）：
        停止只杀进程，请求文件与内存记录都还留着 ⇒ 那张卡在用户眼里仍然"有效"，
        再点「允许」会**真的写进一条 allow 决定**（日志：`rid=… 决定=allow`）。
        进程已经死了，这一次没有造成实际动作，但**闸门的语义破了**：
        停止之后不该还能授权。fail-closed 要求"停 = 全部关掉"。

        与 `_deny_and_stop` 的分工：那个是"点了拒绝"这条路径（还要额外处理
        刚被点掉的那张卡）；这个是"任务结束/被停"这条路径，只收口还没处理的。

        返回收口的条数（测试与日志用）。
        """
        if not self.approval_dir:
            return 0
        with self._approval_lock:
            mine = [(rid, dict(v)) for rid, v in self._approvals.items()
                    if (v.get("chatid"), v.get("chat_type")) == (chatid, chat_type)
                    and not v.get("resolved")]
        done = 0
        for rid, info in mine:
            # ① hook 那边：写 deny，让它立刻退出（它只认文件）
            self._approval_decide(rid, False, why)
            # ② 内存这边：标 resolved，之后任何点击都会走"已处理过"分支
            self._approval_resolve(rid)
            # ③ 文件：自己的请求文件留着，下一轮扫描会把它当**新请求**再弹一张幽灵卡
            for suffix in (".req.json", ".seen"):
                try:
                    os.remove(os.path.join(self.approval_dir, rid + suffix))
                except OSError:
                    pass
            # ④ 已经发出去的那张卡：有位置就改掉，别留一个点了没用的按钮
            token = info.get("card_token") or ""
            if token:
                try:
                    self.plat_approval_update_card(token, info.get("req") or {},
                                                   False, info.get("userid") or "")
                except Exception as e:
                    log.warning("[approval] 收口卡片失败 rid=%s: %s", rid, e)
            done += 1
        if done:
            log.info("[approval] %s ⇒ 同会话 %d 条待审批按拒绝收口", why, done)
        return done

    def _deny_and_stop(self, chatid, chat_type, why="用户拒绝"):
        """点「拒绝」= **终止本轮任务**（用户 2026-11-05 拍板）。

        为什么不是"只拒这一次"：拒绝的语义是"这件事到此为止"。不停的话，模型会换个
        工具再来一次，用户就得对同一件事反复点拒绝 —— 实测就是这样，卡片一张接一张。

        三件事，顺序不能反：
          ① 把同会话里**还没处理**的请求一起按拒绝落盘（它们的 hook 才肯退出）；
          ② 删掉**这个会话所有**请求文件（含刚被点掉的那一张）—— 进程树马上会被杀，
             hook 来不及自己删；留着的话下一轮扫描会把它们当新请求、又在"已拒绝"
             之后弹卡，而且会让这个会话永久卡在"已有一张卡在等点击"；
          ③ 停进程放到**旁路线程**：`kill_current` 最多等 5 秒，
             而卡片回调只有约 3 秒预算（飞书超时会判失败）。
        """
        with self._approval_lock:
            mine = [(r, v) for r, v in self._approvals.items()
                    if (v.get("chatid"), v.get("chat_type")) == (chatid, chat_type)]
        pending = [(r, v) for r, v in mine if not v.get("resolved")]
        for r, _v in pending:
            self._approval_decide(r, False, why)
            self._approval_resolve(r)
        # ⚠️ **文件清理要覆盖整个会话，不只是"还没处理"的那几张。**
        # 刚被点掉的那张已经是 resolved、不在 `pending` 里，但它的 hook 正是马上要被
        # 杀掉的那个 —— 进程树一死，hook 就来不及删自己的 `.req.json`。
        # 留着有两个后果：① 内存里的死记录让这个会话再也弹不出新卡（见 `_approval_show_next`）；
        # ② 桥重启后这个文件会被当成**新请求**，给一个早就结束的任务弹一张幽灵卡。
        # 2026-11-05 实测：`130bff3b4f99.req.json` 在拒绝之后一直躺在目录里。
        for r, _v in mine:
            for suffix in (".req.json", ".seen"):
                try:
                    os.remove(os.path.join(self.approval_dir, r + suffix))
                except OSError:
                    pass
        if pending:
            log.info("[approval] 拒绝 ⇒ 同会话另外 %d 条待审批一并按拒绝处理",
                     len(pending))
        self._stopped_by_deny[(chatid, chat_type)] = True
        threading.Thread(target=self._stop_for_deny, args=(chatid, chat_type),
                         daemon=True, name="deny-stop").start()

    def _stop_for_deny(self, chatid, chat_type):
        killed, status = self._kill_job(chatid, chat_type)
        log.info("[approval] 拒绝 ⇒ 终止本轮任务 killed=%s 状态=%s chat=%s/%s",
                 killed, status, chatid, chat_type)

    def _approval_scan_once(self):
        """扫一遍待审批请求。**跑在专用线程上**：发卡片会等平台回执（最多 15s）。"""
        try:
            names = os.listdir(self.approval_dir)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("[approval] 列目录失败: %s", e)
            return
        live = {n[:-len(".req.json")] for n in names if n.endswith(".req.json")}
        with self._approval_lock:
            # hook 退出时会删掉请求文件 ⇒ 过期的直接忘掉（免得点击写进一个没人读的文件）
            for rid in [r for r in self._approvals if r not in live]:
                # ⚠️ **这一行必须打日志**：它就是"卡片还在、点了一下却说已失效"的那个
                # 静默分支 —— 记录被删掉了，而用户手上的卡片还留着按钮。
                # 没有这行日志时，事后只能看到"未知或已过期"，不知道为什么。
                log.warning("[approval] 请求文件已消失 ⇒ 忘掉 rid=%s（之后点这张卡会判为失效）",
                            rid)
                self._approvals.pop(rid, None)
            known = set(self._approvals)
        for rid in live - known:
            path = os.path.join(self.approval_dir, rid + ".req.json")
            try:
                with open(path, encoding="utf-8") as f:
                    req = json.load(f)
            except Exception as e:
                log.warning("[approval] 读请求失败 rid=%s: %s", rid, e)
                continue
            target = str(req.get("target") or "").split("|")
            if len(target) < 3 or not target[0]:
                log.warning("[approval] 请求没带目标会话 rid=%s ⇒ 直接拒绝", rid)
                # 记一笔（resolved + 不显示）：否则每 0.3 秒都会重新读一遍这个文件、
                # 重新写一次拒绝，直到 hook 自己删掉 —— 纯噪音。
                with self._approval_lock:
                    self._approvals[rid] = {"chatid": "", "chat_type": 2,
                                            "userid": "", "ts": time.time(),
                                            "tool": req.get("tool_name"),
                                            "shown": True, "resolved": True,
                                            "req": req}
                self._approval_decide(rid, False, "无目标")
                continue
            chatid, chat_type, userid = target[0], int(target[1] or 2), target[2]
            with self._approval_lock:
                self._approvals[rid] = {"chatid": chatid, "chat_type": chat_type,
                                        "userid": userid, "ts": time.time(),
                                        "tool": req.get("tool_name"),
                                        # `shown=False` = 已登记、**还没上屏**（排队中）
                                        "shown": False,
                                        # `resolved=True` = 已经点过了。
                                        # ⚠️ **处理完不能从表里删**：hook 还没醒过来删
                                        # `.req.json`（0.2 秒轮询），下一轮扫描就会把
                                        # 同一个 rid 当**新请求**再发一张卡 —— 用户看到
                                        # 两张一模一样的卡，点第二张提示"已失效"
                                        # （2026-11-05 实测，日志里同一个 rid 出现两次）。
                                        "resolved": False,
                                        # 留着整份请求：点击后要把卡片就地改成
                                        # 「已允许/已拒绝」，得知道刚才要干什么
                                        "req": req}
            # ① 先告诉 hook「我看到了」（它只等 10 秒；不然桥没在跑时它会干等两分钟）
            #    ⚠️ 「我看到了」≠「我把它上屏了」：排队的请求也要立刻回执，
            #       否则后面的 hook 会在 10 秒后自己拒绝 —— 用户根本还没轮到看它。
            try:
                open(os.path.join(self.approval_dir, rid + ".seen"), "w").close()
                log.info("[approval] 已回执 .seen rid=%s（hook 可以继续等了）", rid)
            except Exception as e:
                # 写不出 .seen ⇒ hook 会在 10 秒后**自己拒绝**，用户还没看到卡片就没了。
                # 这是必须出声的一条：它是"卡片弹出来了但一点就失效"的上游原因之一。
                log.error("[approval] 写不出 .seen rid=%s：%s —— hook 会在超时后自行拒绝",
                          rid, e)

        # ② **每个会话同时只上一张卡**：一次并发好几个工具调用时，
        #    卡片一张张弹会把聊天窗刷满，用户也读不过来（2026-11-05 用户明确要求）。
        #    排队顺序按请求到达时间，前一张处理完（点击/过期/卡片发失败）才发下一张。
        self._approval_show_next()

    def _approval_show_next(self):
        """把队首那张还没上屏的审批卡发出去（每个会话最多一张在等点击）。

        ⚠️ 排队期间**每个请求自己的超时照旧在走**（hook 从它被创建那一刻算），
        所以队太长、人离开太久时，后面几张会在轮到之前就过期 —— 那是 fail-closed
        该有的行为，不是 bug。日志里会看到 `.req.json` 先消失、卡片根本没发。
        """
        with self._approval_lock:
            # ⚠️ **必须排除 `resolved`**。点掉的那张记录不会立刻删（见 `_approval_resolve`），
            # 如果它还算作"在等点击"，这个会话就会被**永久**判定为有一张卡没处理完 ——
            # 之后所有新卡只排队、永不上屏，hook 干等满 120 秒自己拒绝。
            # 2026-11-05 实测：群里点过一次「拒绝」之后，那个群再也不弹审批卡，
            # 日志里只有 `.seen` 回执、没有"审批卡已送达"，最后 `denials=2`。
            showing = {(v.get("chatid"), v.get("chat_type"))
                       for v in self._approvals.values()
                       if v.get("shown") and not v.get("resolved")}
            pending = sorted(((rid, v) for rid, v in self._approvals.items()
                              if not v.get("shown") and not v.get("resolved")),
                             key=lambda kv: kv[1].get("ts") or 0)
        pick = None
        for rid, info in pending:
            if (info.get("chatid"), info.get("chat_type")) in showing:
                continue        # 这个会话已经有一张在等点击了
            pick = (rid, info)
            break
        if pick is None:
            return
        rid, info = pick
        req = info.get("req") or {}
        with self._approval_lock:
            info["shown"] = True

        r = self.plat_send_card(info["chatid"], info["chat_type"],
                                self.plat_approval_card(req), wait=True)
        # 记下这张卡的位置（平台给了才记）。`/stop` 时要用它把卡收口，
        # 否则会留下一张"看起来还能点"的按钮 —— 见 `_cancel_pending_approvals`。
        if r.get("token"):
            with self._approval_lock:
                info["card_token"] = r["token"]
        mine = (info.get("chatid"), info.get("chat_type"))
        left = max(0, sum(1 for _, v in pending
                          if (v.get("chatid"), v.get("chat_type")) == mine) - 1)
        log.info("[approval] 审批卡 rid=%s tool=%s → %s/%s：%s%s",
                 rid, req.get("tool_name"), info["chatid"], info["chat_type"],
                 "已送达（等点击）" if r.get("sent")
                 else f"未送达 code={r.get('errcode')} {r.get('errmsg')}",
                 f"；同会话还有 {left} 张排队" if left > 0 else "")
        if not r.get("sent"):
            log.warning("[approval] 卡片没发出去（%s）⇒ 直接拒绝 rid=%s",
                        r.get("errmsg") or r.get("errcode"), rid)
            self._approval_decide(rid, False, "卡片未送达")
            self._approval_resolve(rid)


    def _approval_sweep(self):
        """启动时清空审批目录。

        为什么安全：hook 最长只等 `CC_APPROVAL_WAIT_SEC`（默认 120 秒），
        而**本进程**刚起来，不可能有任何 hook 在等"这个进程"的答复 ——
        目录里剩下的必然是上一次运行留下的死请求（典型来源：点了「拒绝」之后
        进程树被强杀，hook 没来得及删自己的 `.req.json`）。

        不清的话，重启后那一张会被当成**新请求**发进聊天 —— 用户会对着一张
        早就结束的任务的审批卡点下去。
        """
        if not self.approval_dir:
            return
        try:
            names = os.listdir(self.approval_dir)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("[approval] 启动清理：列目录失败 %s", e)
            return
        removed = 0
        for n in names:
            if n.endswith((".req.json", ".req.json.tmp", ".seen", ".decision")):
                try:
                    os.remove(os.path.join(self.approval_dir, n))
                    removed += 1
                except OSError:
                    pass
        if removed:
            log.info("[approval] 启动清理：删掉上次运行残留的 %d 个文件", removed)


    def _approval_loop(self):
        self._approval_sweep()
        while not self.stop:
            try:
                self._approval_scan_once()
            except Exception as e:
                log.exception("[approval] 扫描异常: %s", e)
            time.sleep(APPROVAL_POLL_SEC)


    def _note_side(self, chatid=None, chat_type=None):
        """记一条"气泡之外的消息"（markdown / 卡片 / response_url 回执）。

        `chatid` 能拿到就按会话记；`reply()` 那条路只有 token、不知道会话，
        记进 `None` 那一格（对所有会话生效）—— 控制类回执很少，
        最坏后果只是答案多分一条发，不会丢内容。
        """
        key = (chatid, chat_type) if chatid else None
        with self._side_lock:
            self._side_msgs[key] = self._side_msgs.get(key, 0) + 1


    def _side_count(self, chatid=None, chat_type=None):
        key = (chatid, chat_type) if chatid else None
        with self._side_lock:
            return self._side_msgs.get(key, 0) + self._side_msgs.get(None, 0)


    def _finish(self, st, chatid, chat_type, text, since=None):
        """收尾统一出口：有气泡就复用气泡，没有就走主动推送。

        `since` = 本轮开始时的旁路消息计数（见 `_do_job`）。**中途发过任何别的消息**
        （审批卡 / `/status` 卡 / `/stop` 回执 / 自愈提示）时，答案必须**另发一条**：
        流式气泡的位置由**创建时间**锁定，后面每次更新只是改内容、位置不动
        ⇒ 答案写回去就会停在那几张卡**上方**（2026-10-30 用户实测的体验问题）。

        `since=None` = 调用方不关心顺序（早退路径、异常路径），保持老行为。
        """
        if st is None:
            return self.plat_send_chunks(chatid, chat_type, text)
        if since is not None and self._side_count(chatid, chat_type) != since:
            n = self._side_count(chatid, chat_type) - since
            log.info("[deliver] 本轮中途发过 %d 条旁路消息 ⇒ 气泡收口，答案另发一条", n)
            r = st.send("✅ 已完成，回答见下 ↓", finish=True)
            if not r.get("sent"):
                # 收口失败也**照发**答案：气泡可能还停在"处理中"，但答案绝不能丢
                log.warning("[deliver] 收口气泡没成功（%s）⇒ 答案照发",
                            r.get("errmsg") or r.get("errcode"))
            return self.plat_send_chunks(chatid, chat_type, text)
        return self.plat_deliver(st, chatid, chat_type, text)

    # -- 连接生命周期（同 Phase 0）--


    def _worker(self):
        """**派发器**（不是执行者）：拿到一个任务就另起一个线程去跑。

        为什么这样拆（2026-11-05 用户拍板：同一会话串行、不同会话并行）：
        「同一会话同时只能有一个」这条约束在**入队那一刻**就已经保证了
        （见 `core_handle_incoming` 里的按会话占位），所以这里不需要再串行 ——
        串行会让一个会话把别的会话堵住，正是要修掉的那个现象。
        """
        while not self.stop:
            try:
                job = self._q.get(timeout=1)
            except queue.Empty:
                continue
            t = threading.Thread(
                target=self._run_job_slot, args=(job,), daemon=True,
                name="job-%s" % (str(job.get("chatid"))[-6:] or "?"))
            t.start()


    def _run_job_slot(self, job):
        """一个任务的完整生命周期（跑在**自己的线程**里，会话之间互不阻塞）。"""
        key = (job["chatid"], job["chat_type"])
        try:
            self._do_job(job)
        except Exception as e:
            log.exception("[worker] 任务异常: %s", e)
            # ⚠️ **这一句自己也必须兜住。** 它走的还是"刚刚出错的那条发送链路" ——
            # 2026-11-07 实测：断网时 chat.update 抛错 → 这里再抛 → 异常逃出 except
            # ⇒ 整个 `job-*` 线程当场死掉，用户既没收到答案、也没收到"没发出去"。
            # 「报错的动作」不许再把线程带走：发不出去就只记日志。
            try:
                self.plat_send_text(job["chatid"], job["chat_type"],
                                   f"❌ 桥内部错误：{type(e).__name__}: {e}")
            except Exception as e2:
                log.error("[worker] 连报错都发不出去：%s: %s",
                          type(e2).__name__, e2)
        finally:
            self._q.task_done()
            # 占位必须在这里收：`_do_job` 有几条**提前 return**（空消息、目录不存在），
            # 那些路径不会走到它内部的清理，占位留着就会永久显示"忙碌中"。
            with self._job_lock:
                self._current.pop(key, None)
                self._runners.pop(key, None)


    def _do_job(self, job):
        chatid, chat_type, userid, text = (job["chatid"], job["chat_type"],
                                           job["userid"], job["text"])
        # 每轮开头清掉**本会话**"被拒绝终止"的标记：它只在**本轮**有意义，
        # 留着会把下一轮的 `/stop` 也报成"已拒绝"。
        self._stopped_by_deny.pop((chatid, chat_type), None)
        # 本轮开始时的旁路消息计数：跑到一半若发过审批卡/状态卡，答案要另发一条
        # 才能落在最底部（见 `_finish`）。
        job["_side0"] = self._side_count(chatid, chat_type)

        # 这一条**流式气泡**装下 ack 与正文（同一个 stream.id，正文覆盖 ack）。
        # 回调没带 req_id 时退回老路径（response_url 回执 + 主动推送），别把消息吞掉。
        st = self.plat_open_bubble(job["req_id"], chatid, chat_type) if job.get("req_id") else None
        if st is None:
            log.warning("[stream] 回调没带 req_id ⇒ 退回 response_url + 主动推送")
            self.plat_reply(job.get("response_url") or "", "收到，处理中…")

        if not text.strip():
            self._finish(st, chatid, chat_type, "（空消息，未执行）")
            return

        session_id, cwd = self._session_for(chatid, chat_type, userid)
        if not os.path.isdir(cwd):
            # 配置的目录被删/搬走。**不静默回落到另一个项目** —— 那会让 Agent 在
            # 用户没预期的仓库里动手，比报错危险得多。
            key = "CC_CWD_DM" if cwd == self.cc_cwd_dm and self.cc_cwd_dm != self.cc_cwd \
                else "CC_CWD"
            self._finish(st, chatid, chat_type,
                         f"❌ 启动目录不存在：`{cwd}`\n> 请检查 `.env` 的 `{key}`")
            return

        with self._job_lock:
            self._current[(chatid, chat_type)] = {
                "started": time.time(), "text": text[:60],
                "userid": userid, "chatid": chatid}
        # 这一轮用**自己那份** Runner（入队时已经挂上）：`kill_current()` 只停本会话的进程。
        with self._job_lock:
            runner = self._runners.get((chatid, chat_type))
        if runner is None:      # 直接调 `_do_job` 的路径（老单测）兜底
            runner = self._attach_runner(chatid, chat_type)
        runner.clear_stop()

        # ack：流式首帧（`finish=false`），正文到点时用**同一个 id** 覆盖它。
        # ⚠️ 必须**在 worker 线程**发（要等回执），不能放在回调的读线程上。
        if st is not None:
            r = st.send("收到，处理中…", finish=False)
            if r.get("dead"):
                log.warning("[stream] ack 建不出气泡（%s）⇒ 正文将改走主动推送",
                            r["errmsg"])
                st = None

        # 真进度（runner 增量读 `stream-json`）：
        #   有气泡 ⇒ `ProgressPump` 按「变化时推 + 兜底心跳」更新**同一个**气泡；
        #   没气泡 ⇒ 退回那条只看时间的定时文案 —— 没有气泡就没地方显示进度，
        #            主动推送一条"还在跑"已经是能做到的全部。
        pump = None
        if st is not None:
            pump = ProgressPump(st, chatid, chat_type,
                                heartbeat_sec=self.progress_hb_sec)
        timer = None
        if st is None and self.progress_sec > 0:
            def _progress():
                self.plat_send_text(chatid, chat_type,
                                   f"⏳ 还在跑（已 {self.progress_sec} 秒）…")
            timer = threading.Timer(self.progress_sec, _progress)
            timer.daemon = True
            timer.start()

        # 长任务的收口闸：快到 10 分钟窗口时**主动结束气泡**，别让它永远停在
        # 「收到，处理中…」。用户的原始诉求就是"这个气泡不能误导人"。
        # 结束后气泡定型，正文由 `_deliver` 改走主动推送（另发一条，内容不丢）。
        latch = None
        if st is not None:
            def _latch():
                r2 = st.send("⏳ 任务仍在运行（已超过 9 分钟），结果稍后单独发一条。",
                             finish=True)
                if r2["sent"]:
                    log.warning("[stream] 到 9 分钟闸口，气泡已收口，正文将另发一条")
            latch = threading.Timer(knob("STREAM_LATCH_SEC"), _latch)
            latch.daemon = True
            latch.start()
        try:
            cb = pump.update if pump else None
            # 审批环境变量只在开了审批时才传（没开就别给 runner 加无关参数）
            aenv = self._approval_env(chatid, chat_type, userid)
            if aenv:
                # 注入了审批环境 ≠ 真的会弹卡：hook 是挂在**这个 cwd** 的
                # settings 里的。没有就当场报出来，别让它静默放行。
                self._check_gate(cwd)
            kw = {"on_progress": cb}
            if aenv:
                kw["extra_env"] = aenv
            res = runner.run(text, session_id, cwd, **kw)
            note = ""
            # resume 失效（会话文件被清理/损坏）⇒ 新建并在回复里说明，不静默失忆。
            #
            # ⚠️ 但**只在"确认这一轮什么都没干"时才敢重跑**（2026-10-29 复核 #3）。
            # 重跑 = 把整条指令再执行一遍；如果第一轮已经改过文件 / 提交过 / 发过消息，
            # 就会**重复副作用**（重复写入、重复提交、重复调外部服务）。三条否决：
            #   · tools > 0         —— 已经调用过工具，可能已经动手了
            #   · stream_failed     —— 流断了、结果未知
            #   · stopped/timed_out —— 被我们打断或卡住过，执行到哪一步不知道
            # 这些情况下即使错误文本像"会话失效"，也**不重跑**，如实告诉用户。
            if session_id and res.get("is_error") and _looks_like_resume_failure(res["text"]):
                retry_ok, why = safe_to_retry_resume(res)
                if retry_ok:
                    log.warning("[run] resume 失败，改新建会话重跑")
                    res = runner.run(text, None, cwd, **kw)
                    note = "> ⚠️ 上一轮的会话已失效，已为你新建会话（所以这轮不记得之前的内容）。\n\n"
                else:
                    # 不能重跑：宁可让用户核实，也不冒重复副作用的风险
                    log.warning("[run] resume 失败，但**不重跑**（%s）", why)
                    note = (f"> ⚠️ 上一轮的会话已失效，**但没有自动重跑**：{why}，"
                            "重跑可能造成重复副作用。\n"
                            "> 请先核实实际状态，再决定要不要重发。\n\n")
            # ⚠️ 必须**在定稿之前**收口：否则迟到的进度帧会把答案盖掉
            if pump:
                pump.close()
                pump = None
            self._report(job, res, cwd, session_id, note, st)
        except Exception as e:
            # 这里兜住：否则 _worker 那个兜底会**另发一条**，用户就又是两个气泡了
            log.exception("[job] 任务异常: %s", e)
            # 同 `_run_job_slot` 那条：**报错回执自己失败，只能记日志**。
            try:
                self._finish(st, chatid, chat_type,
                             f"❌ 桥内部错误：{type(e).__name__}: {e}")
            except Exception as e2:
                log.error("[job] 报错回执也没发出去：%s: %s",
                          type(e2).__name__, e2)
        finally:
            if pump:
                # 异常路径也必须收口，否则它会在 `_worker` 那条兜底推送之后继续发帧
                pump.close()
            if timer:
                timer.cancel()
            if latch:
                latch.cancel()
            with self._job_lock:
                self._current.pop((chatid, chat_type), None)
                self._runners.pop((chatid, chat_type), None)


    def _report(self, job, res, cwd, session_id, note="", st=None):
        chatid, chat_type = job["chatid"], job["chat_type"]
        if res.get("fatal"):
            self._finish(st, chatid, chat_type, "❌ " + res["text"],
                         since=job.get("_side0"))
            return

        if res["timed_out"]:
            msg = (f"⏱ **超时（{self.runner.timeout}s）**\n\n"
                   f"> 语义是**结果未知**：任务可能已经执行了一部分，也可能没有。\n"
                   f"> 重试前请先核实实际状态，别直接重发。\n"
                   f"> 想改超时：`.env` 里的 `CC_TIMEOUT_SEC`。")
        elif res["stopped"]:
            # ⚠️ 按**会话**读：别的窗口拒绝了一件不相干的事，不能把这个窗口的
            # `/stop` 也说成"已拒绝"（两个会话现在可以同时跑）。
            if self._stopped_by_deny.pop((chatid, chat_type), False):
                # 被「拒绝」终止：话要说得像"你拒绝了这件事"，不是"有人按了 /stop"
                msg = ("🚫 **已拒绝，本轮任务已停止。**\n"
                       "> 这次调用没有执行，后面的步骤也不会再问。"
                       "要它做别的事，请重新发一条指令。")
            else:
                msg = "⏹ 已按 `/stop` 停止（进程树已清理）。"
        elif res["is_error"]:
            msg = (f"❌ **执行出错**（subtype=`{res['subtype']}`，rc={res['rc']}）\n\n"
                   f"```\n{(res['text'] or '（无输出）')[:3000]}\n```")
        else:
            msg = res["text"] or "（无文本输出）"

        if res["denials"]:
            names = ", ".join(sorted({d.get("tool_name", "?") for d in res["denials"]}))
            msg += f"\n\n> ⚠️ 本次被权限拦截：{names}"
        if res.get("usage_footer"):
            self._last_usage[(chatid, job["userid"])] = res["usage_footer"]
            msg += res["usage_footer"]
        msg = note + msg

        self._finish(st, chatid, chat_type, msg, since=job.get("_side0"))

        # 会话落库：拿到就存（失败那次也会带 session_id，存下来无妨，但别据此认为成功）
        if res.get("session_id"):
            # ⚠️ 但**跑的过程中用户可能发过 /new**（或点了卡片「清空」）——
            # 那时清库已经发生，这里再把旧 session 写回去，就等于"清空了又接上旧上下文"。
            # 代次变了就丢弃；日志留痕，免得以后有人以为会话丢了。
            if job.get("epoch", 0) != self._epoch_of(chatid, job["userid"]):
                log.info("[session] 任务期间被 /new 清过（代次 %s→%s），本轮 session 不落库",
                         job.get("epoch"), self._epoch_of(chatid, job["userid"]))
            else:
                self.store.save_session(chatid, job["userid"], res["session_id"], cwd)

    # -- 主循环 --

    # ---- 平台 → 内核的两个入口（见 core/contract.py）----
    def core_handle_incoming(self, inc):
        """一条**已规范化**的普通消息。平台已完成字段解析、@ 剥离、去重键提取。"""
        userid = inc.get("userid") or ""
        chatid = inc.get("chatid") or ""
        chat_type = inc.get("chat_type")
        raw_chatid = inc.get("raw_chatid") or ""
        response_url = inc.get("response_url") or ""

        if not userid:
            log.warning("[msg] 无 from.userid，丢弃")
            return

        # ⚠️ 白名单是唯一真正的鉴权；群里 @ 只是消息路由
        if self.allow_users and userid not in self.allow_users:
            log.warning("[msg] 非白名单 userid=%s，静默丢弃", userid)
            return
        if self.allow_chats and raw_chatid and raw_chatid not in self.allow_chats:
            # 只对**真实的** chatid 生效：单聊那个"chatid"其实是 userid，
            # 拿它去比群白名单只会误杀。
            log.warning("[msg] 非白名单 chatid=%s，静默丢弃", raw_chatid)
            return

        key = inc.get("dedupe_key") or ""
        if key and self.store.seen(key):
            log.warning("[msg] 重复消息 %s=%s，跳过", inc.get("dedupe_field"), key)
            return

        if (inc.get("msgtype") or "text") != "text":
            log.info("[msg] 非文本 msgtype=%s，暂不支持", inc.get("msgtype"))
            self.plat_reply(response_url, "目前只支持文本消息。")
            return

        text = inc.get("text") or ""
        log.info("[msg] user=%s chat=%s type=%s%s 去@=%r 正文=%.80r",
                 userid, chatid, inc.get("chattype"),
                 "（单聊无 chatid，用 userid 兜底）"
                 if inc.get("is_single") and not raw_chatid else "",
                 inc.get("at_prefix"), text)

        # —— 控制指令：**在回调线程里立即处理**，否则 /stop 会排在正在跑的任务后面 ——
        lowered = text.strip().lower()
        if lowered in ("/new", "/stop", "/status"):
            self._handle_control(lowered, chatid, chat_type, userid, response_url)
            return
        # `/model` 带参数，只能前缀匹配（不能塞进上面的精确匹配集合）。
        if (lowered == "/model" or lowered.startswith("/model ")
                or lowered.startswith("/model\t")):
            self._model_async(text.strip()[len("/model"):].strip(),
                              chatid, chat_type, userid, response_url)
            return

        # —— 普通任务：**本会话**忙就丢弃，别的会话在跑不影响 ——
        # 用户 2026-10-27：同一会话处理中收到的新消息回"忙碌中…"并抛弃（防输入轰炸）；
        # 用户 2026-11-05：不同会话之间要能**并行**（群聊在跑时私聊照样能用）。
        # ⚠️ 占位必须在**入队这一刻**做，不能等 worker 拿到 —— 中间那段空档里
        # 同一会话再来一条，会被误判成"空闲"而收进去，破坏"忙碌即丢弃"。
        slot_key = (chatid, chat_type)
        with self._job_lock:
            cur = dict(self._current.get(slot_key) or {}) or None
            running = len(self._current)
            if cur is None and running < self.max_concurrent:
                self._current[slot_key] = {"started": time.time(),
                                           "text": text[:60],
                                           "userid": userid, "chatid": chatid}
                slot = True
            else:
                slot = False
        if slot:
            # 占位的同时就把这一轮的 Runner 挂上：从这一刻起 `/stop` 就能停到它。
            # 否则从"入队"到"`_do_job` 走到中途"之间有个空档，那时 `/stop`
            # 会答"当前没有正在跑的任务"，而任务随后照样跑起来。
            self._attach_runner(chatid, chat_type)
        if not slot:
            if cur is not None:
                waited = time.time() - cur["started"]
                log.warning("[msg] 本会话忙碌中，丢弃 user=%s 正文=%.40r（已跑 %.0fs）",
                            userid, text, waited)
                self.plat_reply(response_url,
                                f"⏳ **忙碌中**（本会话当前任务已跑 {waited:.0f} 秒），"
                                f"**本条已忽略**。\n\n"
                                f"> 要打断请发 `/stop`；查看进度发 `/status`；"
                                f"换新话题请等当前任务结束。")
            else:
                log.warning("[msg] 全局已满（%d/%d），丢弃 user=%s 正文=%.40r",
                            running, self.max_concurrent, userid, text)
                self.plat_reply(response_url,
                                f"⏳ **忙碌中**（已有 {running} 个会话在跑，"
                                f"上限 {self.max_concurrent} 个），**本条已忽略**。\n\n"
                                f"> 等另一个窗口的任务结束后再发；"
                                f"要打断**这个窗口**请发 `/stop`（只停本会话）。")
            return

        # ⚠️ `req_id` 是**平台**给这次回调发的关联键，回复时必须原样透传
        # （平台回复帧头里的 req_id）；自己编一个平台不认。
        job = {"chatid": chatid, "chat_type": chat_type, "userid": userid,
               "text": text, "msgid": key or "",
               "req_id": inc.get("reply_token") or "",
               "response_url": response_url,
               # 入队那一刻的会话代次；跑完落库前要再比一次（见 `_epoch`）
               "epoch": self._epoch_of(chatid, userid)}
        try:
            self._q.put_nowait(job)
        except queue.Full:      # 理论上到不了这里（上面已拦掉忙碌），留个兜底
            log.warning("[q] 入队失败（队列满 %d）", self._qmax)
            # ⚠️ 占位已经占上了，入队失败必须**还回去**，否则这个会话永久"忙碌中"。
            with self._job_lock:
                self._current.pop(slot_key, None)
                self._runners.pop(slot_key, None)
            self.plat_reply(response_url, "⏳ 忙碌中，本条已忽略。")
            return
        # 「收到，处理中…」**不在这里发**：它要在 worker 线程里以**流式首帧**发出，
        # 这样正文才能用同一个 stream.id 覆盖它（见 Stream / _deliver）。
        # 在这里发就等于两套通道，手机上就是两条独立气泡 —— 正是要修掉的那个问题。

    def core_handle_card_action(self, act):
        """一次**已规范化**的卡片动作。"""
        key = act.get("key") or ""
        userid = act.get("userid") or ""
        chatid = act.get("chatid") or ""
        chat_type = act.get("chat_type")
        raw_chatid = act.get("raw_chatid") or ""

        if not userid:
            log.warning("[card] 无 from.userid，丢弃（key=%r）", key)
            return
        # 按钮和文字一样是**可执行入口** ⇒ 同一道白名单，不能因为是点卡片就放行
        if self.allow_users and userid not in self.allow_users:
            log.warning("[card] 非白名单 userid=%s key=%s，忽略", userid, key)
            return
        # ⚠️ 群白名单也要查。早先这里只查了用户、没查群 ⇒ 白名单用户在
        # **已经不被允许的群**里点旧卡片，照样能查状态 / 清空 / 停止（2026-10-29 复核 #6）。
        # 条件与文字消息那条完全一致：只对**真实的** chatid 生效，单聊那个"chatid"是 userid。
        if self.allow_chats and raw_chatid and raw_chatid not in self.allow_chats:
            log.warning("[card] 非白名单 chatid=%s key=%s，忽略", raw_chatid, key)
            return
        log.info("[card] user=%s key=%s task=%s chat=%s type=%s",
                 userid, key, act.get("task_id"), chatid, chat_type)
        if key == "status_refresh":
            self._status_async(chatid, chat_type, userid)
            return
        if key == "status_clear":
            self.store.clear_session(chatid, userid)
            self._last_usage.pop((chatid, userid), None)   # 与 `/new` 同一条语义
            self._bump_epoch(chatid, userid)
            self.plat_send_text(chatid, chat_type,
                                "✅ 已开新会话：下一轮不会记得之前的内容。")
            return
        if key == "status_stop":
            self._cancel_pending_approvals(chatid, chat_type, "点了「停止」")
            killed, status = self._kill_job(chatid, chat_type)
            log.info("[card] status_stop 终止=%s 状态=%s chat=%s/%s", killed,
                     status, chatid, chat_type)
            self.plat_send_text(chatid, chat_type, self._stop_text(killed, status))
            return
        if key == "approve_done":
            # 更新后的卡片若带着这个"已处理"按钮（降级形态），点了什么都别做
            log.info("[card] 已处理卡片的按钮被点，忽略")
            return
        if key in ("approve_ok", "approve_no"):
            rid = act.get("task_id") or ""
            allow = key == "approve_ok"
            with self._approval_lock:
                info = self._approvals.get(rid)
            if not info:
                # 超时后 hook 已经自己拒绝了、请求文件也没了 ⇒ 这次点击无效。
                # 明确告诉用户，别让他以为点上了。
                log.info("[card] 审批 task_id=%r 未知或已过期，忽略", rid)
                self.plat_send_text(chatid, chat_type,
                                    "⌛ 这张审批已失效（超时或任务已结束），这次点击没有生效。")
                return
            if info.get("resolved"):
                # 已经点过（卡片也已就地改成"已允许/已拒绝"，按钮早没了）。
                # 连点两下时第二下会走到这里 —— 静默忽略，别弹第二条"已失效"。
                log.info("[card] 审批 task_id=%r 已处理过，忽略重复点击", rid)
                return
            if info.get("userid") and userid != info["userid"]:
                # 卡片是发给"发起这条任务的人"的，别人点不算数
                log.warning("[card] 审批被非发起人点击（%s != %s），忽略",
                            userid, info["userid"])
                return
            # ⚠️ 顺序有讲究：平台的更新窗口只有 **5 秒**（实测文档原文），
            #    而 `send_markdown` 要等回执（最多 15 秒）⇒ **先决定、再更新卡片**，
            #    卡片更新成功后**不再发文字**（卡片本身就是反馈，少一条噪音）。
            self._approval_decide(rid, allow)
            if not self.plat_approval_update_card(act.get("reply_token") or "",
                                                  info.get("req") or {}, allow, userid):
                self.plat_send_text(chatid, chat_type,
                                    "✅ 已允许，继续执行。" if allow
                                    else "🚫 已拒绝，这条不会执行。")
            self._approval_resolve(rid)
            if not allow:
                # 拒绝 = 终止本轮（见 `_deny_and_stop`）。放在**改卡之后**：
                # 卡片先变成「已拒绝」，用户立刻看到反馈，然后才去停进程。
                self._deny_and_stop(chatid, chat_type)
            return
        log.info("[card] 未知 event_key=%r，忽略", key)

