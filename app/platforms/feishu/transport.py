"""飞书长连接：建连 / 事件分发 / 重连 / 收尾。

**与企微那套最大的不同**：企微是我们自己跑 WebSocket、自己认 `aibot_*` 帧；
飞书用官方 SDK 的 `lark.ws.Client`，建连、心跳、重连都是它的事，我们只管事件。

⚠️ 三条由实测（3a / 3c）定下来的硬事实，写在这里是因为**它们决定代码形状**：

① **事件派发是"在 asyncio 事件循环里同步调用回调"**
   （`lark_oapi/ws/client.py::_handle_data_frame` → `_event_handler._do_without_validation(pl)`）。
   ⇒ 回调里做任何阻塞（等回执 / 跑命令 / 睡）都会**把整条长连接卡住**：
      下一条事件收不到，连 PING/PONG 也停。
   ⇒ 所以消息入口只做「解析 + 入队」，真正的处理在 `feishu-inbound` 线程上。

② **卡片回调是唯一必须同步返回的例外**：它的响应（新卡片 + toast）**只能**通过
   `P2CardActionTriggerResponse` 在这次调用的返回值里交出去，晚一步就没有通道了。
   ⇒ 审批的「就地改卡」由 `plat_approval_update_card` 记到本次回调上下文里，
      由 codec 在回调返回时一起交出去（见 `_card_ctx`）。

③ **同一应用的多条长连接是"分摊事件"，不是广播**（3c 实测：两个进程同时连，
   消息和回调会随机落到其中一条，表现为"点了好几次才有反应"）。
   ⇒ 单实例锁是硬要求，不是可选优化。见 `app/bridge.py` 的互斥量名。
"""

import threading
import time

from common import BACKOFF_MAX, BACKOFF_START, knob, log


class FeishuTransportMixin:
    """连接生命周期。核心层不知道这些存在。"""

    # -- HTTP 客户端 --

    def feishu_new_client(self):
        """统一的 HTTP 客户端：所有主动推送 / 建卡 / 改卡都走它。"""
        import lark_oapi as lark
        return (lark.Client.builder()
                .app_id(self.app_id)
                .app_secret(self.app_secret)
                .log_level(lark.LogLevel.INFO)
                .build())

    def _feishu_build_ws(self):
        """建一个带事件订阅的长连接客户端。

        `auto_reconnect=True` 是 SDK 自带的：同一条连接断了它自己重连。
        我们外层的重连循环兜的是**建连阶段**的异常（凭据错、网络起不来）。
        """
        import lark_oapi as lark
        handler = (lark.EventDispatcherHandler.builder("", "")
                   .register_p2_im_message_receive_v1(self._feishu_on_message)
                   .register_p2_card_action_trigger(self._feishu_on_card_action)
                   .build())
        return lark.ws.Client(self.app_id, self.app_secret,
                              event_handler=handler,
                              log_level=lark.LogLevel.INFO,
                              auto_reconnect=True)

    # -- 生命周期 --

    def _feishu_thread_specs(self):
        """要常驻的旁路线程清单 —— **唯一一份**，启动器和看门狗都读它。

        ⚠️ `cc-worker` 是内核的队列消费者（`CoreMixin._worker`），**必须在清单里**。
        漏了它不会报错：任务照样入队，但没有线程取 ⇒ `_q.qsize() > 0` 永远为真 ⇒
        之后每条消息都被判成「忙碌中」并丢掉 —— 表现为"机器人装死"。
        （2026-11-05 首次真机启动就踩到；离线单测抓不到，因为测试不走这个循环。
          所以清单收成一处：启动器和看门狗**不可能各自漏一项**。）
        """
        specs = [("cc-worker", self._worker),
                 ("feishu-inbound", self._inbound_worker),
                 ("reply-worker", self._reply_worker)]
        if self.approval_dir:
            specs.append(("approval-loop", self._approval_loop))
        return specs

    def feishu_run_forever(self):
        # 旁路线程先起来：入站分发 / 任务消费 / 被动回复 / 审批扫描
        for name, target in self._feishu_thread_specs():
            t = threading.Thread(target=target, daemon=True, name=name)
            self._threads[name] = t
            t.start()
        threading.Thread(target=self._feishu_watchdog_loop, daemon=True,
                         name="watchdog").start()

        log.info("[feishu] 队列上限 %d，切分粒度 %dB，超时 %ds，看门狗 %ds",
                 self._qmax, self.feishu_chunk_bytes, self.runner.timeout,
                 knob("WATCHDOG_SEC"))

        while not self.stop:
            try:
                log.info("[feishu] 建立长连接 …")
                self.feishu_ws = self._feishu_build_ws()
                # ⚠️ 阻塞调用：成功连上之后它就一直在等事件（SDK 内部自己重连）。
                # 退出靠异常或进程结束 —— SDK **没有公开的 stop()**。
                self.feishu_ws.start()
            except Exception as e:
                log.warning("[feishu] 长连接异常：%s: %s", type(e).__name__, e)
            if self.stop:
                break
            log.info("[feishu] %ss 后重连", self._backoff)
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def feishu_stop(self):
        """优雅停止的边界，说实话：

        · 我们的旁路线程（入站 / 回复 / 审批 / 看门狗）靠 `self.stop` 退出；
        · **长连接本身没有公开的关闭接口**，靠进程退出把 socket 带走。
          这不是"没做"，是 SDK 的接口就是这样 —— 写在这里免得下一个人去找。
        """
        self.stop = True
        try:
            # 所有会话的任务都要停（2026-11-05 起不同会话可并行）
            self._kill_all()
        except Exception as e:
            log.warning("[feishu] 停止时杀进程失败：%s", e)
        log.info("[feishu] 已请求停止（长连接随进程退出断开）")

    def _feishu_watchdog_loop(self):
        """旁路线程死了就重启 —— 与企微那条同一个理由：

        主线程看起来"连着、也在跑"，但消息再也不会被处理，这种**静默失效**最难发现。
        """
        targets = self._feishu_thread_specs()
        while not self.stop:
            time.sleep(knob("WATCHDOG_SEC"))
            if self.stop:
                break
            for name, target in targets:
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
                        self.send_text(cid, ctype,
                                       f"⚠️ 桥自愈：`{name}` 线程已重启。"
                                       f"上一轮任务若有回答缺失，请重发。")
                except Exception:
                    pass