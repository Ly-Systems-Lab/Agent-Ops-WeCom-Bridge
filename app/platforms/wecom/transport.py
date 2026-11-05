"""企微长连接：建立 / 订阅 / 心跳 / 看门狗 / 重连 / 回执派发。

只有这里认识 WebSocket 与 `aibot_*` 帧名。
"""

import json
import threading
import time
import uuid

import websocket  # websocket-client

from common import (BACKOFF_MAX, BACKOFF_START, HEARTBEAT_SEC, STALE_RX_SEC,
                    WS_URL, knob, log)


class WeComTransportMixin:
    """连接生命周期 + 收发帧。核心层不知道这些存在。"""

    def _new_req_id(self):
        return uuid.uuid4().hex


    def _deliver_receipt(self, rc, emsg="", req_id=None):
        """把平台回执交给**正在等待的那个**发送方（同一时刻只允许一个发送方等）。

        `rc is None` = 这个帧没有 errcode，不是回执，别去叫醒等待方。

        ⚠️ **能对上 req_id 就必须对上**（2026-10-29 复核 #4）：早先任何带 errcode 的
        帧都会叫醒等待方 —— 上一条消息的迟到回执、甚至心跳相关帧，都可能被当成
        "这一条发成功了"。现在：
          · 回执**带** `req_id` ⇒ 必须等于我正在等的那一个，否则忽略（记日志）；
          · 回执**不带** ⇒ 保持旧行为（平台当前的回执到底带不带，我们还没实测过，
            `DECISIONS` §16.3 仍是待办）—— 不能凭空收紧，否则会把真回执也丢掉。
        """
        if rc is None:
            return
        with self._receipt_cv:
            want = self._receipt_want
            if req_id and want and req_id != want:
                # ⚠️ 只在**真的在等**的时候才记：`_receipt_want` 是上一帧留下的陈旧值，
                # 空闲时每一拍心跳的回执都会撞上它 ⇒ 30 秒一条"正在等 xxx"的假象
                # （2026-10-31 日志实测：连续 6 分钟刷同一行，看着像某个发送卡死了）。
                if self._receipt_pending:
                    log.info("[send] 忽略无关回执 req_id=%s（正在等 %s）", req_id[:8], want[:8])
                return
            if self._receipt_pending and self._receipt is None:
                self._receipt = (rc, emsg)
                self._receipt_cv.notify_all()


    def _send(self, obj):
        with self._send_lock:
            if self.ws is None:
                return False
            try:
                self.ws.send(json.dumps(obj, ensure_ascii=False))
                return True
            except Exception as e:
                log.warning("[ws] send 失败: %s", e)
                return False

    # -- 两条回复通道 --


    def _send_wait(self, frame, label):
        """发一帧并**等平台回执**。返回 {sent, errcode, errmsg}。

        `errcode is None` 的语义是「**结果未知**」——帧可能到了也可能没到，
        调用方不许据此重发（重发会让模型把同一件事再干一遍）。
        ⚠️ 这个方法会阻塞，**绝不能**在 websocket 读线程上调（回执就在那条线程上收）。
        """
        with self._receipt_lock:
            with self._receipt_cv:
                self._receipt = None
                self._receipt_pending = True
                # 这次要等哪一帧的回执（供 `_deliver_receipt` 过滤无关回执）
                self._receipt_want = ((frame.get("headers") or {}).get("req_id") or "")
            try:
                if not self._send(frame):
                    # 帧**根本没发出去**（连接不在）⇒ 这不是"结果未知"，是"没发"。
                    # 必须和"发出去了但没回执"区分开：前者可以安全改道，后者不行。
                    log.error("[send] %s 没发出去：连接不在", label)
                    return {"sent": False, "errcode": None,
                            "errmsg": "ws 未连接", "unsent": True}
                with self._receipt_cv:
                    got = self._receipt_cv.wait_for(
                        lambda: self._receipt is not None, timeout=knob("SEND_WAIT_SEC"))
                    rc, emsg = self._receipt if got else (None, "未收到平台回执")
            finally:
                with self._receipt_cv:
                    self._receipt_pending = False

        sent = (rc == 0)
        level = log.info if sent else log.error
        level("[send] %s errcode=%s %s", label, rc, "" if sent else (emsg or "")[:160])
        return {"sent": sent, "errcode": rc, "errmsg": emsg or ""}


    def subscribe(self):
        self._send({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self._new_req_id()},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        })


    def _on_open(self, ws):
        log.info("[ws] connected, subscribe bot_id=%s", self.bot_id)
        if self._backoff != BACKOFF_START:
            log.info("[ws] 退避重置 %ss -> %ss", self._backoff, BACKOFF_START)
        self._backoff = BACKOFF_START
        self.subscribe()


    def _on_error(self, ws, err):
        log.warning("[ws] error: %s", err)


    def _on_close(self, ws, code, msg):
        self.subscribed.clear()
        log.info("[ws] closed code=%s msg=%s", code, msg)


    def _on_message(self, ws, raw):
        self._last_rx = time.time()      # 任何帧都算"连接还活着"（看门狗用）
        try:
            msg = json.loads(raw)
        except Exception:
            log.warning("[ws] 非 JSON: %r", raw[:200])
            return
        cmd = msg.get("cmd", "")
        log.debug("[ws] rx cmd=%r %dB", cmd, len(raw))

        if cmd == "aibot_subscribe" or (not cmd and "errcode" in msg
                                        and not self.subscribed.is_set()):
            # `.get(k, default)` 在 k 存在但值为 null 时**不会**退回默认值 ⇒ 显式兜底
            rc = msg.get("errcode")
            if rc is None:
                rc = (msg.get("result") or {}).get("errcode")
            if rc == 0:
                self.subscribed.set()
                log.info("[ws] subscribe ok")
            else:
                log.error("[ws] subscribe 失败: %s",
                          json.dumps(msg, ensure_ascii=False)[:400])
                # 🚨 订阅失败**不能挂着不动**：心跳和惰性检测都以 subscribed 为前提，
                # 连接又不会自己断 ⇒ 进程活着、TCP 连着，但再也收不到一条消息。
                # 主动断开，让主循环带退避重连（凭据错的话会一直重试并刷日志，那是想要的）。
                self.subscribed.clear()
                try:
                    ws.close()
                except Exception:
                    pass
            return

        if cmd == "aibot_msg_callback":
            self._handle_callback(msg)
            return

        if cmd == "aibot_event_callback":
            ev = ((msg.get("body") or {}).get("event") or {}).get("eventtype", "")
            if ev == "disconnected_event":
                log.warning("[ws] 服务端断开旧连接（同一 bot 不要起两个实例）")
                self.subscribed.clear()
                try:
                    ws.close()
                except Exception:
                    pass
            elif ev == "template_card_event":
                # 卡片按钮被点。**必须离开读线程**：下面的动作要么发卡片（等平台回执）、
                # 要么发 markdown，两条都会阻塞，而回执就在这条读线程上收。
                log.info("[card] template_card_event 到达")
                if self.dump_raw:
                    self._dump_raw(msg)
                threading.Thread(target=self._handle_card_event,
                                 args=(msg,), daemon=True).start()
            else:
                log.info("[ws] event: %s", ev or msg)
                # 其余事件（含以后平台新增的类型）只在 --dump-raw 下整个落盘，
                # 免得"来了个没见过的 event 却什么都没留下"。
                if self.dump_raw:
                    self._dump_raw(msg)
            return

        if cmd in ("aibot_respond_msg", "aibot_send_msg", "aibot_respond_welcome_msg",
                   "aibot_respond_update_msg", "ping"):
            rc = msg.get("errcode")
            if rc not in (0, None):
                log.warning("[ws] %s 回执错误: %s", cmd,
                            json.dumps(msg, ensure_ascii=False)[:300])
            elif cmd == "aibot_send_msg":
                log.info("[ws] aibot_send_msg 回执 errcode=0")
            # 实测回执**不带 cmd**，但万一哪天带上了：也要能交给等待方，
            # 否则发送方会白等到超时，然后错误地报"没能送达"（还不走切片分支）。
            self._deliver_receipt(rc, msg.get("errmsg") or "",
                                  (msg.get("headers") or {}).get("req_id"))
            return

        if not cmd:
            rc = msg.get("errcode")
            if rc not in (0, None):
                log.warning("[ws] 响应错误: %s", json.dumps(msg, ensure_ascii=False)[:300])
            else:
                log.debug("[ws] 回执 errcode=0")
            # 实测：`aibot_send_msg` 的回执**没有 cmd 字段**，落在这个分支。
            self._deliver_receipt(rc, msg.get("errmsg") or "",
                                  (msg.get("headers") or {}).get("req_id"))
            return

        log.info("[ws] 未处理 cmd=%r raw=%s", cmd, raw[:300])

    # -- 收消息 --


    def _heartbeat_loop(self):
        while not self.stop:
            if self.subscribed.is_set():
                self._send({"cmd": "ping", "headers": {"req_id": self._new_req_id()}})
            time.sleep(HEARTBEAT_SEC)


    def _watchdog_loop(self):
        """看门狗：旁路线程死了就重启；连接静默太久就主动断开重连。

        为什么需要：重连循环在**主线程**，而 worker / 心跳是旁路线程。
        它们意外退出时主线程照样"看起来正常"（连着、也在发心跳），
        但消息再也不会被处理 —— 这种**静默失效**最难发现。
        """
        while not self.stop:
            time.sleep(knob("WATCHDOG_SEC"))
            if self.stop:
                break
            for name, target in (("cc-worker", self._worker),
                                 ("wecom-heartbeat", self._heartbeat_loop),
                                 ("reply-worker", self._reply_worker),
                                 ("approval-loop", self._approval_loop)):
                t = self._threads.get(name)
                if t is None or t.is_alive():
                    continue
                log.error("[watchdog] 线程 %s 已退出，重启", name)
                nt = threading.Thread(target=target, daemon=True, name=name)
                self._threads[name] = nt
                nt.start()
                try:                     # 自愈要出声，别静默
                    cid, ctype = self.store.last_chat()
                    if cid:
                        self.send_markdown(cid, ctype,
                                           f"⚠️ 桥自愈：`{name}` 线程已重启。"
                                           f"上一轮任务若有回答缺失，请重发。")
                except Exception:
                    pass

            # 连接活性：订阅之后长期收不到任何帧 ⇒ 认为这条连接已死
            if self.stale_rx_sec > 0 and self.subscribed.is_set() and self.ws is not None:
                idle = time.time() - self._last_rx
                if idle > self.stale_rx_sec:
                    log.warning("[watchdog] 已 %.0fs 没收到任何帧（阈值 %ds），主动断开重连",
                                idle, self.stale_rx_sec)
                    self._last_rx = time.time()   # 别每 20s 重复触发
                    self.subscribed.clear()
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                else:
                    log.debug("[watchdog] 距上帧 %.0fs，线程正常", idle)


    def run_forever(self):
        self._threads["wecom-heartbeat"] = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="wecom-heartbeat")
        self._threads["cc-worker"] = threading.Thread(
            target=self._worker, daemon=True, name="cc-worker")
        self._threads["reply-worker"] = threading.Thread(
            target=self._reply_worker, daemon=True, name="reply-worker")
        if self.approval_dir:
            self._threads["approval-loop"] = threading.Thread(
                target=self._approval_loop, daemon=True, name="approval-loop")
        for t in self._threads.values():
            t.start()
        threading.Thread(target=self._watchdog_loop, daemon=True,
                         name="watchdog").start()
        log.info("[main] 队列上限 %d，切分粒度 %dB，超时 %ds，看门狗 %ds/惰性 %ds",
                 self._qmax, self.chunk_bytes, self.runner.timeout,
                 knob("WATCHDOG_SEC"), self.stale_rx_sec)
        while not self.stop:
            try:
                log.info("[ws] 连接 %s ...", WS_URL)
                self.ws = websocket.WebSocketApp(
                    WS_URL, on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close)
                self.ws.run_forever(ping_interval=0)
            except Exception as e:
                log.warning("[ws] run_forever 异常: %s", e)
            if self.stop:
                break
            log.info("[ws] %ss 后重连", self._backoff)
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)


    def stop_bot(self):
        self.stop = True
        self.runner.kill_current()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass

