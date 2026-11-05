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
        session_id, cwd = self.store.get_session(chatid, userid, want)
        if cwd != want:
            log.warning("[job] cwd 路由与记录不一致（%s → %s），换目录并新建会话",
                        cwd, want)
            self.store.save_session(chatid, userid, None, want)
            return None, want
        return session_id, cwd


    def _is_busy(self):
        with self._job_lock:
            return self._current is not None


    def _epoch_of(self, chatid, userid):
        with self._epoch_lock:
            return self._epoch.get((chatid, userid), 0)


    def _bump_epoch(self, chatid, userid):
        with self._epoch_lock:
            k = (chatid, userid)
            self._epoch[k] = self._epoch.get(k, 0) + 1


    def _stop_text(self, killed):
        """按 runner 的**实际**结果给话。`failed` 不能说成"已清理"。

        `kill_status` 是 `kill_current()` 复核过 `p.poll()` 之后写的：
        `killed` = 确认退出；`failed` = 命令发了但进程没退；`idle` = 本来就没在跑。
        （老的单测 FakeRunner 只返回 bool、不写 kill_status，这里照样按 killed 处理。）
        """
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
            killed = self.runner.kill_current()
            log.info("[ctl] /stop 终止=%s 状态=%s", killed,
                     getattr(self.runner, "kill_status", "?"))
            self.plat_reply(response_url, self._stop_text(killed))
            return
        if cmd == "/status":
            # 改成**卡片**：一眼能看，还带三个按钮（刷新 / 清上下文 / 停止）。
            # 卡片发不出去就退回文本 —— 状态是"必须能看到"的东西，不能因为卡片失败就沉默。
            self._status_async(chatid, chat_type, userid)
            return

    # -- /status 卡片 --


    def _status_pairs(self, chatid, chat_type, userid):
        """状态键值对。文本版和卡片版都用它，保证两处**永远一致**。

        ⚠️ 默认 cwd 必须按 `chat_type` 取 —— 不然私聊在还没跑过第一轮时，
        `/status` 会显示群聊那个目录。
        """
        sid, cwd = self.store.get_session(chatid, userid,
                                          self._cwd_for(chat_type))
        with self._job_lock:
            cur = dict(self._current) if self._current else None
        usage = (self._last_usage.get((chatid, userid)) or "").replace("📊 ", "").strip()
        return [
            ("队列", f"{self._q.qsize()} 条"),
            ("当前任务", ("运行中 %.0f 秒" % (time.time() - cur["started"])) if cur else "空闲"),
            ("上下文", usage or "—（重启后首轮还没跑）"),
            ("会话", (sid or "")[:8] or "（无，下一轮新建）"),
            ("档位", f"{self.runner.perm_mode or '默认'} / 超时 {self.runner.timeout}s"),
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
                self._approval_decide(rid, False, "无目标")
                continue
            chatid, chat_type, userid = target[0], int(target[1] or 2), target[2]
            with self._approval_lock:
                self._approvals[rid] = {"chatid": chatid, "chat_type": chat_type,
                                        "userid": userid, "ts": time.time(),
                                        "tool": req.get("tool_name"),
                                        # 留着整份请求：点击后要把卡片就地改成
                                        # 「已允许/已拒绝」，得知道刚才要干什么
                                        "req": req}
            # ① 先告诉 hook「我看到了」（它只等 10 秒；不然桥没在跑时它会干等两分钟）
            try:
                open(os.path.join(self.approval_dir, rid + ".seen"), "w").close()
            except Exception:
                pass
            # ② 发卡片。发不出去 = 用户永远点不到 ⇒ 立刻拒绝，别让他白等 120 秒
            r = self.plat_send_card(chatid, chat_type, self.plat_approval_card(req),
                               wait=True)
            if not r.get("sent"):
                log.warning("[approval] 卡片没发出去（%s）⇒ 直接拒绝 rid=%s",
                            r.get("errmsg") or r.get("errcode"), rid)
                self._approval_decide(rid, False, "卡片未送达")
                with self._approval_lock:
                    self._approvals.pop(rid, None)


    def _approval_loop(self):
        while not self.stop:
            try:
                self._approval_scan_once()
            except Exception as e:
                log.exception("[approval] 扫描异常: %s", e)
            time.sleep(APPROVAL_POLL_SEC)


    def _note_side(self):
        """记一条"气泡之外的消息"（markdown / 卡片 / response_url 回执）。"""
        with self._side_lock:
            self._side_msgs += 1


    def _side_count(self):
        with self._side_lock:
            return self._side_msgs


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
        if since is not None and self._side_count() != since:
            n = self._side_count() - since
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
        while not self.stop:
            try:
                job = self._q.get(timeout=1)
            except queue.Empty:
                continue
            # 一出队就占住"忙碌"位：`_do_job` 要读库、查目录之后才设 `_current`，
            # 那个空档里新消息会被当成"空闲"收进队列，破坏"忙碌即丢弃"的约定。
            with self._job_lock:
                if self._current is None:
                    self._current = {"started": time.time(),
                                     "text": (job.get("text") or "")[:60],
                                     "userid": job.get("userid"),
                                     "chatid": job.get("chatid")}
            try:
                self._do_job(job)
            except Exception as e:
                log.exception("[worker] 任务异常: %s", e)
                self.plat_send_text(job["chatid"], job["chat_type"],
                                   f"❌ 桥内部错误：{type(e).__name__}: {e}")
            finally:
                self._q.task_done()
                # 占位必须在这里收：`_do_job` 有几条**提前 return**（空消息、目录不存在），
                # 那些路径不会走到它内部的清理，占位留着就会永久显示"忙碌中"。
                with self._job_lock:
                    self._current = None


    def _do_job(self, job):
        chatid, chat_type, userid, text = (job["chatid"], job["chat_type"],
                                           job["userid"], job["text"])
        # 本轮开始时的旁路消息计数：跑到一半若发过审批卡/状态卡，答案要另发一条
        # 才能落在最底部（见 `_finish`）。
        job["_side0"] = self._side_count()

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
            self._current = {"started": time.time(), "text": text[:60],
                             "userid": userid, "chatid": chatid}
        self.runner.clear_stop()

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
            kw = {"on_progress": cb}
            if aenv:
                kw["extra_env"] = aenv
            res = self.runner.run(text, session_id, cwd, **kw)
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
                    res = self.runner.run(text, None, cwd, **kw)
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
            self._finish(st, chatid, chat_type,
                         f"❌ 桥内部错误：{type(e).__name__}: {e}")
        finally:
            if pump:
                # 异常路径也必须收口，否则它会在 `_worker` 那条兜底推送之后继续发帧
                pump.close()
            if timer:
                timer.cancel()
            if latch:
                latch.cancel()
            with self._job_lock:
                self._current = None


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

        # —— 普通任务：忙碌时**直接丢弃，不排队** ——
        # 用户 2026-10-27 明确要求：处理中收到的新消息回"忙碌中…"并抛弃，
        # 目的是防短时间输入轰炸。
        # ⚠️ 这**推翻了交接文档 §附录C** 的"聊天桥应该排队"——按用户决定执行。
        if self._is_busy() or self._q.qsize() > 0:
            with self._job_lock:
                cur = dict(self._current) if self._current else None
            waited = (time.time() - cur["started"]) if cur else 0.0
            log.warning("[msg] 忙碌中，丢弃 user=%s 正文=%.40r（已跑 %.0fs）",
                        userid, text, waited)
            self.plat_reply(response_url,
                            f"⏳ **忙碌中**（当前任务已跑 {waited:.0f} 秒），**本条已忽略**。\n\n"
                            f"> 要打断请发 `/stop`；查看进度发 `/status`；"
                            f"换新话题请等当前任务结束。")
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
            killed = self.runner.kill_current()
            log.info("[card] status_stop 终止=%s 状态=%s", killed,
                     getattr(self.runner, "kill_status", "?"))
            self.plat_send_text(chatid, chat_type, self._stop_text(killed))
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
            with self._approval_lock:
                self._approvals.pop(rid, None)
            return
        log.info("[card] 未知 event_key=%r，忽略", key)

