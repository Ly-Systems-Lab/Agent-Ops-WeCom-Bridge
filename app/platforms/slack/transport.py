"""Slack 长连接：Socket Mode 的建连 / 收帧 / 回执 / 重连 / 收尾。

**为什么手写而不用 `slack_sdk`**（用户 2026-11-07 拍板）：
  ① `requirements.txt` 里"桥只有两个第三方依赖"是一句**设计承诺**，加 SDK 就破了；
  ② 飞书那一轮已经吃过 SDK 的亏 —— 重连参数由服务端下发、调用方改不了
     （最长 2.5 分钟收不到消息），SDK 的日志在 `pythonw` 下还会**静默蒸发**。
     断网自愈是我们要实测的项目，参数和日志必须自己握着。

⚠️ 四条来自官方文档的硬事实（2026-11-07 核对，`docs.slack.dev/apis/events-api/using-socket-mode`）：

  ① **URL 是现取的、一次性的**：`POST apps.connections.open` 用 **app-level token**
     换一个 `wss://…?ticket=…`。**每次重连都要重新申请**，不能缓存复用。
     那个 ticket 本身就是凭据 ⇒ **一行都不许打进日志**。

  ② **每条事件都要 ack**：`{"envelope_id": "…"}`。官方只说"平台据此决定是否重投"，
     **没有写期限**（"3 秒"是社区/HTTP 模式的说法，本项目不把它当事实）。
     实现上仍然**收到就先 ack、再做别的** —— 这样无论真实期限是多少都不会撞，
     顺带解释了为什么读线程上不能做任何阻塞（同飞书那条：一阻塞整条连接就废）。

  ③ **连接每几小时会自己刷新**（`hello.debug_info.approximate_connection_time`；
     2026-11-07 实测本工作区给的是 **18060 秒 ≈ 5 小时**，文档示例里写的 3600 只是示例），
     刷新前会推 `{"type":"disconnect","reason":"warning"}`（约早 10 秒）
     或 `"refresh_requested"`。⇒ 收到 disconnect 就**主动关掉、重新申请 URL**，
     不要等它自己断。

  ④ **最多 10 条并发连接，事件随机落到任一条**。⇒ 单实例锁是硬要求
     （与飞书同一条坑：两个进程同时连，"点好几次才有反应"）。

⚠️ **一个和企微不一样的地方，别照抄**：Slack 空闲时**什么帧都不发**。
  企微靠 30 秒心跳判断连接死活（`stale_rx_sec`），这里没有可收的东西 ——
  把"静默"判成"已死"会每隔几分钟无谓重连一次。

⚠️⚠️ **但"收不到帧"不等于"我们没辙"（2026-11-07 断网实测的教训）。**
  原来这里写着"官方文档没有 ping/pong，所以只能靠 7200 秒的空闲阈值兜底"——
  **前提对、结论错**：WebSocket 的 ping/pong 是**协议级**的（RFC 6455 §5.5.2，
  收到 ping **必须**回 pong），不属于"应用层约定"，服务端不需要在文档里写。
  实测代价：断网 10.5 分钟，桥**一条日志都没有**、连接在 `netstat` 里始终 Established，
  恢复后也不会自愈（僵尸到天亮）。根因是 Slack 的 TCP 连的是**本机代理**，
  断网时本地内核无可报错，而空闲时又收不到任何帧 ⇒ `recv()` 永久阻塞。
  ⇒ 现在**主动发 ping**（`CC_SLACK_PING_SEC`，默认 30 秒）并要求 pong
  （`CC_SLACK_PONG_SEC`，默认 10 秒）⇒ 僵尸最长约 40 秒被发现。
  `CC_SLACK_IDLE_SEC`（7200 秒）保留作最后一道兜底，正常情况轮不到它。
"""

import json
import threading
import time
from urllib.parse import urlparse

import requests
import websocket  # websocket-client

from common import BACKOFF_MAX, BACKOFF_START, cfg, cfg_int, knob, log

API = "https://slack.com/api/"


def _slack_unsent(exc):
    """这个异常能不能**确定请求没送到平台**？只有"连接都没建立起来"才算。

      · `ConnectTimeout` —— 连代理/连平台就超时了，一个字节都没发出去
      · `ProxyError`     —— 代理本身没起来（2026-11-07 17:41 实测 `WinError 10061`）
      · `SSLError`       —— TLS 握手阶段就断了（同日 17:43 实测 `SSLEOFError`）

    ⚠️ **`ReadTimeout` 和一般的 `ConnectionError` 都不算**：请求可能**已经**到了平台，
      只是响应没回来。这时候重试 `chat.postMessage` 就是**把同一条消息发两遍**。
      宁可报"结果未知"让用户多看到一条，也不许悄悄重复。
    """
    if isinstance(exc, requests.exceptions.ConnectTimeout):
        return True
    return isinstance(exc, (requests.exceptions.ProxyError,
                            requests.exceptions.SSLError))


class SlackTransportMixin:
    """连接生命周期。核心层不知道这些存在。"""

    # -- 代理 --

    def slack_proxy(self):
        """返回 `http://host:port` 或空串。配了才用。

        为什么必须专门配（用户 2026-11-07 实测）：
          · Slack 在国内**直连不通**（`slack.com:443` 连 32 秒超时，而同一时刻
            baidu / github / 飞书 / 企微全部正常）；
          · `requests` 认 `HTTPS_PROXY` 环境变量，但 **`websocket-client` 不认** ——
            长连接那条必须把代理参数显式传进 `run_forever`。
            只设环境变量的话，API 通了、事件一条也来不了，而且看起来像"连上了但不理人"。
        ⇒ 两处都从这**一个**配置项取，不各读各的。
        """
        raw = (cfg("SLACK_PROXY") or "").strip()
        if not raw:
            return ""
        if "://" not in raw:
            raw = "http://" + raw
        return raw

    def _slack_proxies(self):
        raw = self.slack_proxy()
        if not raw:
            return None          # None = 交给 requests 按环境变量决定（不配就是直连）
        return {"http": raw, "https": raw}

    # -- HTTP 底座（outbound 也走这里，一处管鉴权与限流）--

    def slack_http(self, method, token, params=None, timeout=20, retry_429=2,
                   retry_net=2):
        """调一个 Slack Web API 方法，返回平台原样的 dict。**从不抛异常。**

        **429 要退避重试，不能当失败** —— 和飞书 `230020` 同一个处理口径。
        Slack 用 HTTP 429 + `Retry-After` 头表达限流（不是 JSON 里的 error）。

        ⚠️ **网络异常也必须在这里吃掉**（2026-11-07 实测的教训）：
        这一层原来只处理 429，网络抖动时 `requests` 直接抛；而上层**每一处"报告错误"
        的代码用的还是同一条已经断掉的路** ⇒ 逐层抛穿 ⇒ 最后把 `job-*` 线程打死，
        用户既没收到答案、也没收到"没发出去"（现象是"发消息完全没反应"）。
        ⇒ 契约改成"**返回 dict，绝不抛**"：调用方全都按 dict 写，改这一处整条链自然降级。

        重试口径见 `_slack_unsent()`：**能确定没送出去的才重试**，
        结果未知的立刻返回，让上层按"宁可多发一条、绝不重复发"处理。
        """
        url = API + method
        headers = {"Authorization": "Bearer " + token}
        n429 = nnet = 0
        while True:
            try:
                r = requests.post(url, headers=headers, data=params or {},
                                  timeout=timeout,
                                  proxies=self._slack_proxies())
            except requests.RequestException as e:
                detail = "%s: %s" % (type(e).__name__, e)
                unsent = _slack_unsent(e)
                if unsent and nnet < retry_net:
                    nnet += 1
                    wait = 0.5 * (2 ** (nnet - 1))
                    log.warning("[slack] %s 没连上平台（%s），%.1fs 后重试 %d/%d",
                                method, detail, wait, nnet, retry_net)
                    time.sleep(wait)
                    continue
                log.error("[slack] %s 网络异常（%s）%s", method, detail,
                          "" if unsent else "；请求可能已送达，不重试")
                return {"ok": False, "error": "network", "detail": detail,
                        "sent_unknown": not unsent}

            if r.status_code == 429:
                try:
                    wait = float(r.headers.get("Retry-After") or 1)
                except (TypeError, ValueError):
                    wait = 1.0
                wait = min(max(wait, 0.5), 30.0)
                if n429 >= retry_429:
                    log.error("[slack] %s 被限流（429），重试已用完", method)
                    return {"ok": False, "error": "ratelimited",
                            "retry_after": wait}
                n429 += 1
                log.warning("[slack] %s 被限流，%.1fs 后重试", method, wait)
                time.sleep(wait)
                continue

            try:
                return r.json()
            except Exception:
                return {"ok": False, "error": "http_%s" % r.status_code,
                        "raw": r.text[:200]}

    # -- 建连 --

    def slack_open_url(self):
        """现取一个 Socket Mode 的 wss 地址。

        ⚠️ 返回值**含 ticket**，只有本函数和调用它的那一行能看到它 ——
        日志里一律只打长度，永远不打内容。
        """
        d = self.slack_http("apps.connections.open", self.slack_app_token)
        if not d.get("ok"):
            # `detail` 比 `error` 具体得多（网络异常时是 `SSLError: …` 原文），
            # 重连日志全靠这一句定位，别把它丢成干巴巴的 "network"。
            raise RuntimeError("apps.connections.open 失败：%s"
                               % (d.get("detail") or d.get("error") or d))
        url = d.get("url") or ""
        if not url.startswith("wss://"):
            raise RuntimeError("apps.connections.open 返回的不是 wss 地址")
        # 启动时若网络不通，`self.slack_bot_user_id` 会是空（见 main）。
        # 能连到这里说明网络已经通了 ⇒ 便宜地补一次身份解析。
        # 它决定 `<@U…>` 剥不剥得干净，**不能一直空着**。
        if not self.slack_bot_user_id:
            self.slack_resolve_identity()
        return url

    def slack_resolve_identity(self):
        d = self.slack_http("auth.test", self.slack_bot_token)
        if d.get("ok"):
            self.slack_bot_user_id = d.get("user_id") or ""
            log.info("[slack] 身份已补解析：workspace=%s bot=%s",
                     d.get("team"), self.slack_bot_user_id)
            return True
        log.warning("[slack] 补解析身份失败：%s", d.get("error") or d)
        return False

    # -- 帧处理（跑在 websocket 读线程上）--

    def _slack_ack(self, ws, envelope_id):
        with self._send_lock:
            try:
                ws.send(json.dumps({"envelope_id": envelope_id}))
            except Exception as e:
                log.warning("[slack] ack 发送失败：%s", e)

    def _slack_on_open(self, ws):
        log.info("[slack] TCP 已连上，等 hello …")

    def _slack_on_error(self, ws, err):
        log.warning("[slack] ws error: %s", err)

    def _slack_on_close(self, ws, code, msg):
        self.subscribed.clear()
        log.info("[slack] ws closed code=%s msg=%s", code, msg)

    def _slack_on_message(self, ws, raw):
        self._last_rx = time.time()          # 任何帧都算"连接还活着"
        try:
            msg = json.loads(raw)
        except Exception:
            log.warning("[slack] 非 JSON 帧：%r", raw[:200])
            return

        typ = msg.get("type") or ""

        if typ == "hello":
            # 连上即"可用"：Slack 不需要额外订阅帧，事件订阅是后台配置。
            dbg = msg.get("debug_info") or {}
            log.info("[slack] hello：连接数=%s 预计存活=%ss（服务端会自己刷新）",
                     msg.get("num_connections"), dbg.get("approximate_connection_time"))
            if self._backoff != BACKOFF_START:
                log.info("[slack] 退避重置 %ss -> %ss", self._backoff, BACKOFF_START)
            self._backoff = BACKOFF_START
            self.subscribed.set()
            return

        if typ == "disconnect":
            reason = msg.get("reason") or ""
            log.warning("[slack] 服务端要求断开 reason=%r ⇒ 重新申请 URL 再连", reason)
            # 三种 reason（link_disabled / warning / refresh_requested）都要重连：
            # · warning / refresh_requested ⇒ 正常刷新，**主动关**比等它断干净；
            # · link_disabled ⇒ 有人把 Socket Mode 关了，重连会失败并刷日志 ——
            #   那正是我们想要的（静默挂着才是最难查的）。
            self.subscribed.clear()
            try:
                ws.close()
            except Exception:
                pass
            return

        # ⚠️ **先 ack，再处理**。顺序反过来意味着处理慢就把 ack 也拖慢。
        envelope_id = msg.get("envelope_id") or ""
        if envelope_id:
            self._slack_ack(ws, envelope_id)

        payload = msg.get("payload") or {}

        if typ == "events_api":
            # 只有解析 + 入队，真正的处理在 `slack-inbound` 线程上。
            self._slack_on_event(payload)
            return

        if typ == "interactive":
            # 按钮被点。与消息不同，**这条必须在这一刻就处理完** ——
            # 它要拿「改的是哪条消息」的信息去 chat.update，没有别的通道。
            # 但 update 本身是 HTTP 调用（会阻塞读线程）⇒ 丢到独立线程。
            threading.Thread(target=self._slack_on_interactive,
                             args=(payload,), daemon=True).start()
            return

        if typ == "slash_commands":
            log.info("[slack] 收到斜杠命令（本适配器没注册，忽略）")
            return

        if typ:
            log.info("[slack] 未处理的帧 type=%r", typ)

    # -- 生命周期 --

    def _slack_thread_specs(self):
        """要常驻的旁路线程清单 —— **唯一一份**，启动器和看门狗都读它。

        同飞书那条：漏一个不会报错，表现是"机器人装死"。收成一处就不可能各自漏一项。
        """
        # ⚠️ **没有 `reply-worker`**，和飞书不一样：那边是因为卡片回调必须
        # 在这一次调用里同步返回响应，所以"被动回复"得挤进一条专用队列。
        # Slack 的按钮回调已经在 `_slack_on_message` 里丢到独立线程了，
        # `plat_reply` 只是一次 HTTP POST（几百毫秒），跑在 `slack-inbound` 线程上就行。
        specs = [("cc-worker", self._worker),
                 ("slack-inbound", self._inbound_worker)]
        if self.approval_dir:
            specs.append(("approval-loop", self._approval_loop))
        return specs

    def slack_run_forever(self):
        for name, target in self._slack_thread_specs():
            t = threading.Thread(target=target, daemon=True, name=name)
            self._threads[name] = t
            t.start()
        threading.Thread(target=self._slack_watchdog_loop, daemon=True,
                         name="watchdog").start()

        log.info("[slack] 队列上限 %d，切分粒度 %d 字符，超时 %ds，看门狗 %ds",
                 self._qmax, self.slack_chunk_chars, self.runner.timeout,
                 knob("WATCHDOG_SEC"))
        log.info("[slack] 心跳：每 %ds 发 ping，%ds 没 pong 就重连（僵尸最长约 %ds）",
                 cfg_int("CC_SLACK_PING_SEC", 30), cfg_int("CC_SLACK_PONG_SEC", 10),
                 cfg_int("CC_SLACK_PING_SEC", 30) + cfg_int("CC_SLACK_PONG_SEC", 10))

        while not self.stop:
            try:
                url = self.slack_open_url()
                log.info("[slack] 建立 Socket Mode 连接（地址已隐去，长度 %d）",
                         len(url))
                self.ws = websocket.WebSocketApp(
                    url, on_open=self._slack_on_open,
                    on_message=self._slack_on_message,
                    on_error=self._slack_on_error,
                    on_close=self._slack_on_close)
                # ⚠️ **必须自己发 ping**（2026-11-07 实测教训，见模块头）。
                # 不发的话：断网时本地代理那条 TCP 毫无异常、Slack 空闲又不发帧
                # ⇒ recv() 永久阻塞 ⇒ 连接变僵尸，而且永远不会自愈。
                # `ping_timeout` 一过没收到 pong 就由 websocket-client 关掉连接，
                # 我们的重连循环随即接管 —— 这是唯一能"主动问"的活性判据。
                # 两个值都做成旋钮：万一某个版本的服务端不认 pong，能立刻调回来。
                kw = {}
                px = self.slack_proxy()
                if px:
                    u = urlparse(px)
                    kw = {"http_proxy_host": u.hostname,
                          "http_proxy_port": u.port or 80,
                          "proxy_type": "http"}
                    log.info("[slack] 长连接经代理 %s:%s", u.hostname, u.port)
                ping_sec = cfg_int("CC_SLACK_PING_SEC", 30)
                pong_sec = cfg_int("CC_SLACK_PONG_SEC", 10)
                if ping_sec <= 0:            # 关掉 = 退回老行为（僵尸化），只在排障时用
                    log.warning("[slack] CC_SLACK_PING_SEC=0 ⇒ 不发 ping，"
                                "断网后连接会静默变僵尸、不会自愈")
                    self.ws.run_forever(ping_interval=0, **kw)
                else:
                    self.ws.run_forever(ping_interval=ping_sec,
                                        ping_timeout=pong_sec, **kw)
            except Exception as e:
                log.warning("[slack] 建连异常：%s: %s", type(e).__name__, e)
            self.subscribed.clear()
            if self.stop:
                break
            log.info("[slack] %ss 后重连", self._backoff)
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def slack_stop(self):
        self.stop = True
        try:
            self._kill_all()
        except Exception as e:
            log.warning("[slack] 停止时杀进程失败：%s", e)
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        log.info("[slack] 已请求停止")

    def _slack_watchdog_loop(self):
        """旁路线程死了就重启；连接静默太久才怀疑半开。

        ⚠️ **和企微的看门狗不是一回事**：企微 30 秒一次心跳，`stale_rx_sec=90`
        就有意义；Slack **空闲时一个帧都不发**，拿 90 秒去判会**每 90 秒重连一次**。
        所以这里用 `CC_SLACK_IDLE_SEC`（默认 7200 秒），只兜"TCP 已经半开"这一种。
        """
        targets = self._slack_thread_specs()
        # 默认 7200 秒：远大于服务端的连接刷新周期（约 3600 秒），
        # 只用来兜"TCP 已半开但没报错"这一种；正常空闲**不该**触发重连。
        idle_limit = cfg_int("CC_SLACK_IDLE_SEC", 7200)
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

            if idle_limit > 0 and self.subscribed.is_set() and self.ws is not None:
                idle = time.time() - self._last_rx
                if idle > idle_limit:
                    log.warning("[slack] 已 %.0fs 没收到任何帧（阈值 %ds）⇒ 主动重连",
                                idle, idle_limit)
                    self._last_rx = time.time()   # 别每拍重复触发
                    self.subscribed.clear()
                    try:
                        self.ws.close()
                    except Exception:
                        pass