"""Slack 事件编解码：把 `events_api` 的 payload 翻译成**规范化事件**交给内核。

本文件是「平台细节」与「业务判据」的分界线（与企微/飞书 `codec.py` 同一条原则）：
  · 在这里做的：`<@U…>` 的剥离、线程准入、去重键、单聊口径；
  · 交给内核的：白名单、去重判据、忙碌丢弃、指令分派、审批动作。

⚠️ **线程内续接的准入规则**（用户 2026-11-07 拍板）：
  Slack 的 `app_mention` 只在正文里**真的 @ 了**才推，所以"在频道里 @ 一次之后
  可以接着聊"这件事平台本身不提供。我们按下面三条判：

  | 事件 | 条件 | 处理 |
  |---|---|---|
  | `app_mention` | 任意 | **接**，并把 `thread_ts or ts` 记成"我参与的线程" |
  | `message.*` | 在**我参与的线程**里（`thread_ts` 命中） | **接** |
  | `message.*` | 其它（频道顶层的闲聊） | **不接** —— 这就是 @ 隔离的落地方式 |

  ⚠️ 这里做的是**应用层过滤**，不是协议层隔离。按契约 §四，这一档必须在
  `/status` 里如实说明"@ 隔离降级成应用层"，不能描述成协议级。

⚠️ **同一条消息可能到达两次**：同时订阅 `app_mention` 和 `message.channels` 时，
  一条 @ 了我的消息会触发两个事件（`event_id` 不同，`ts` 相同）。
  ⇒ 去重键**必须用 `channel:ts`**，不能用 `event_id` —— 用后者等于没有去重。
"""

import json
import os
import queue
import re
import threading

from common import DEFAULT_DB, cfg_int, log

# `<@U0TESTBOT01>` / `<@U0TESTBOT01|cc>`；Slack 的 user id 以 U 或 W 开头
_AT_RE = re.compile(r"<@([UW][A-Z0-9]+)(?:\|([^>]*))?>")

# 线程准入的持久化：桥重启之后，之前参与过的线程**仍然能续接**。
# 不落盘的话，每次重启都会要求用户重新 @ 一次 —— 那是"能用但别扭"，且很难复现。
THREADS_PATH = os.path.join(os.path.dirname(DEFAULT_DB), "slack-threads.json")
THREADS_MAX = 2000


def normalize_mentions(text, bot_user_id):
    """剥掉 @ 机器人的那段，把别人的 @ 降级成 `@U…` 纯文本。

    为什么不能像飞书那样整段替换：Slack 的 `<@U…>` 是**协议级标记**，
    原样喂给模型就是 `<@U0CFR…>` 这种噪音；而别人的 mention 也不该直接删掉
    （那会改变语义：`@张三 看一下` 变成 `看一下`）。
    """
    prefix = []

    def _repl(m):
        uid = m.group(1)
        if bot_user_id and uid == bot_user_id:
            prefix.append(m.group(0))
            return ""
        return "@" + uid

    out = _AT_RE.sub(_repl, text or "")
    return out.strip(), " ".join(prefix).strip()


class SlackCodecMixin:
    """收消息 / 收按钮：解析成规范化 dict，然后交给内核。"""

    # -- 线程准入（内存 + 落盘）--

    def _slack_load_threads(self):
        self._threads_lock = threading.Lock()
        self._threads_seen = {}
        try:
            with open(THREADS_PATH, "r", encoding="utf-8") as f:
                d = json.load(f)
            if isinstance(d, dict):
                self._threads_seen = {str(k): float(v) for k, v in d.items()}
            log.info("[slack] 线程准入表已载入：%d 条（%s）",
                     len(self._threads_seen), THREADS_PATH)
        except FileNotFoundError:
            self._threads_seen = {}
        except Exception as e:
            # 表坏了不是致命错：最坏后果是"重启后要重新 @"。喊一声，别静默。
            log.warning("[slack] 线程准入表读取失败（按空表处理）：%s", e)
            self._threads_seen = {}

    def _slack_save_threads(self):
        """整份写回。条目最多 2000，一次几毫秒，且只在**新增线程**时写。"""
        try:
            os.makedirs(os.path.dirname(THREADS_PATH), exist_ok=True)
            tmp = THREADS_PATH + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self._threads_seen, f, ensure_ascii=False)
            os.replace(tmp, THREADS_PATH)   # 原子替换，别让半份文件把表读坏
        except Exception as e:
            log.warning("[slack] 线程准入表写入失败（不影响本次会话）：%s", e)

    def _slack_mark_thread(self, channel, thread_ts):
        if not channel or not thread_ts:
            return
        key = "%s:%s" % (channel, thread_ts)
        with self._threads_lock:
            if key in self._threads_seen:
                self._threads_seen[key] = max(self._threads_seen[key], 0.0)
                return
            self._threads_seen[key] = 0.0
            # 超上限就裁掉最早的一批（按插入顺序，dict 保序）
            if len(self._threads_seen) > THREADS_MAX:
                drop = len(self._threads_seen) - THREADS_MAX
                for k in list(self._threads_seen)[:drop]:
                    self._threads_seen.pop(k, None)
        self._slack_save_threads()

    def _slack_thread_ok(self, channel, thread_ts):
        if not channel or not thread_ts:
            return False
        with self._threads_lock:
            return ("%s:%s" % (channel, thread_ts)) in self._threads_seen

    # -- 事件入口（websocket 读线程：只解析 + 入队）--

    def _slack_on_event(self, payload, meta=None):
        """**只解析 + 入队**。这里绝不做任何会阻塞的事（会卡住整条连接）。"""
        try:
            item = self._slack_normalize(payload, meta)
        except Exception as e:
            log.warning("[slack] 事件解析异常：%s: %s", type(e).__name__, e)
            return
        if item is None:
            return
        try:
            self._in_q.put_nowait(item)
        except queue.Full:
            # 队列满 = 内核那头已经跟不上。丢一条并**喊出来**，别静默。
            log.error("[slack] 入站队列已满（%d），丢弃一条 channel:ts=%s",
                      self._in_qmax, item.get("dedupe_key", "")[:24])

    # -- 断线积压的过滤 --

    def _slack_drop_backlog(self, ts_raw, kind, key="", meta=None):
        """这条事件是**断线期间积压的**吗？是就记一行日志并返回 True。

        背景（2026-11-13 用户实测后拍板的取舍）：Socket Mode 会把桥不在线那段时间的
        事件攒着，连上后按原顺序补投。补投上来的 `ts` 是**原始发送时刻**，
        所以"比本次连接还早"就是积压 —— 用户原话：「丢掉断线前积压的」。
        另一条路（照答）被否掉的理由是：手机上会突然冒出一堆对旧问题的回答，
        用户会以为那是新消息。

        ⚠️ **判据的三个前提**，缺一个就会误伤正常消息：
          ① **时间基准用平台的时钟**（`_slack_skew`，从 HTTP `Date` 头量出来）。
             拿本机 `time.time()` 直接当基准的话，本机快 30 秒就会把重连后的
             正常消息全丢 ⇒ 机器人"突然不说话了"。
          ② **宽限窗口**（`CC_SLACK_BACKLOG_GRACE_SEC`，默认 10 秒）吃掉网络与
             时钟的残留误差 —— 窗口内一律放行。**宁可偶尔答一条旧消息，
             也别漏答新的**（这条判据的方向由代价决定，不是因为旧消息无害）。
          ③ **坏值一律放行**：`ts` 解不出来、"还没连上过"（基准为 0）时都返回 False，
             不敢用一个坏值去拦真消息。

        关掉：把 `CC_SLACK_BACKLOG_GRACE_SEC` 设成 0 或负数（排障用，见文档）。
        """
        grace = cfg_int("CC_SLACK_BACKLOG_GRACE_SEC", 10)
        base = getattr(self, "_slack_connected_at", 0.0) or 0.0
        if grace <= 0 or base <= 0:
            return False
        try:
            ts = float(ts_raw)
        except (TypeError, ValueError):
            return False
        base_server = base - (getattr(self, "_slack_skew", 0.0) or 0.0)
        if ts >= base_server - grace:
            return False

        age = base_server - ts
        self._slack_backlog_dropped = getattr(self, "_slack_backlog_dropped", 0) + 1
        n = self._slack_backlog_dropped
        # 积压可能是一整串，逐条打会把日志刷爆 ⇒ 只详记前 3 条，之后每 20 条一次。
        if n <= 3 or n % 20 == 0:
            meta = meta or {}
            log.info("[slack] 丢弃断线积压的%s（比本次连接早 %.0f 秒）%s "
                     "retry=%s/%s；本连接累计已丢 %d 条",
                     kind, age, (" " + key) if key else "",
                     meta.get("retry_attempt") or 0,
                     meta.get("retry_reason") or "-", n)
        return True

    def _slack_normalize(self, payload, meta=None):
        ev = payload.get("event") or {}
        etype = ev.get("type") or ""

        # ⚠️ **只认这两种事件**，别的一律丢掉。
        # 2026-11-07 实测：`app_home_opened` 也会推到长连接上（点开应用主页就发一次）。
        # 它没有正文，所以这次被后面的"正文为空"顺手挡住了 —— **那是侥幸**。
        # 将来任何一个**带正文**的非消息事件（`channel_join` 之类）进来，
        # 就会被当成"用户说的话"去执行。判据必须写在入口。
        if etype not in ("message", "app_mention"):
            log.debug("[slack] 忽略事件类型 %r", etype)
            return None

        # 机器人自己（以及别的 bot）发的消息：一律不回，否则自激循环。
        # `bot_id` 覆盖"应用以机器人身份发消息"；`user == 自己` 是第二道。
        if ev.get("bot_id"):
            return None
        user = ev.get("user") or ""
        if not user or user == self.slack_bot_user_id:
            return None

        channel = ev.get("channel") or ""
        if not channel:
            return None

        # 只处理真正的消息事件；`message_changed` / `channel_join` / `message_deleted`
        # 这些 subtype 不是"人对我说的话"，当成消息处理会去执行 edit 里的内容。
        subtype = ev.get("subtype") or ""
        if subtype not in ("", "thread_broadcast"):
            log.debug("[slack] 忽略 subtype=%s", subtype)
            return None

        ts = ev.get("ts") or ""
        thread_ts = ev.get("thread_ts") or ""
        text = ev.get("text") or ""

        # ⚠️ **放在准入判断之前**：积压的消息连"我参与过的线程"都不该登记，
        # 否则用户会在一个我其实从未答过的线程里继续说话（那才是更难解释的怪象）。
        if self._slack_drop_backlog(ts, "事件(%s)" % etype, "", meta):
            return None

        channel_type = ev.get("channel_type") or ""
        is_single = (channel_type == "im") or channel.startswith("D")

        if is_single:
            accept = True                      # 单聊不需要 @
        elif etype == "app_mention":
            accept = True                      # 频道里 @ 了一次 ⇒ 起会话
            self._slack_mark_thread(channel, thread_ts or ts)
        else:
            # 频道顶层的消息：没 @ 我 ⇒ 不接（这就是 @ 隔离）。
            # 线程里的消息：只有**我参与过的线程**才接（用户选的"线程内续接"）。
            accept = self._slack_thread_ok(channel, thread_ts)

        if not accept:
            if not thread_ts:
                log.debug("[slack] 频道顶层未 @ 我，按适配点 ④ 忽略")
            else:
                log.info("[slack] 线程 %s… 我没参与过，忽略", thread_ts[-8:])
            return None

        text, at_prefix = normalize_mentions(text, self.slack_bot_user_id)
        if not text:
            log.info("[slack] 剥掉 @ 之后正文为空，忽略")
            return None

        if not self._shape_logged:
            self._shape_logged = True
            log.info("[slack][shape] event=%r channel_type=%r subtype=%r "
                     "thread_ts=%s 去重键=channel:ts",
                     etype, channel_type, subtype, "有" if thread_ts else "无")
            log.info("[slack][shape] 单聊 chatid 口径 = 频道 ID（D…）；"
                     "频道 chat_type=2，单聊 chat_type=1")

        # 单聊用**频道 ID**（D…）当 chatid：它既是推送目标，也是会话键。
        # 不像飞书那样退回 open_id —— Slack 的 D… 本身就是一条频道，
        # 拿用户 ID 去 postMessage 反而不成立。
        chatid = channel
        raw_chatid = "" if is_single else channel

        return {
            "userid": user,
            "chatid": chatid,
            "chat_type": 1 if is_single else 2,
            "raw_chatid": raw_chatid,
            "chattype": "single" if is_single else "group",
            "is_single": is_single,
            # Slack 没有 `response_url` 这种东西（那是斜杠命令才有的），
            # 但内核的 `/stop`「/new」「忙碌中」回执全走 `plat_reply(response_url, …)`。
            # 留空 ⇒ 这些回执**一条都发不出去**（2026-11-07 实测：/stop 只杀了进程、
            # 用户没收到任何确认）。这里塞同一个 `channel|ts` 令牌，让
            # `plat_reply` 把回执发回**同一个线程**。
            "response_url": "%s|%s" % (channel, thread_ts or ts),
            "text": text,
            "at_prefix": at_prefix,
            "msgtype": "text",
            "dedupe_field": "channel:ts",
            # ⚠️ 必须用 ts 而不是 event_id：同一条消息会经 `app_mention` 和
            # `message.*` 各来一次，event_id 不同而 ts 相同。
            "dedupe_key": "%s:%s" % (channel, ts),
            # 回复落在**同一个线程**里；顶层消息则以自己为线程根。
            # ⚠️ 格式必须是 **`channel|thread_ts`**：Slack 改消息 / 发消息都要频道 ID，
            # 只给 ts 会让 `chat.postMessage` 把时间戳当频道名去查 ⇒ `channel_not_found`
            # （2026-11-07 首跑实测踩到：气泡一次都没建出来，用户只看到最终答案）。
            "reply_token": "%s|%s" % (channel, thread_ts or ts),
        }

    def _inbound_worker(self):
        """把规范化消息逐条交给内核。**单线程串行**是刻意的。
        """
        while not self.stop:
            try:
                inc = self._in_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.core_handle_incoming(inc)
            except Exception as e:
                log.exception("[slack] 处理消息异常：%s", e)
            finally:
                self._in_q.task_done()

    # -- 按钮事件（读线程里另起线程，HTTP 调用会阻塞）--

    def _slack_on_interactive(self, payload):
        """Block Kit 按钮被点。payload 里能拿到：点的人、哪条消息、按钮的 value。"""
        try:
            actions = payload.get("actions") or []
            if not actions:
                return
            act0 = actions[0] or {}

            # 按钮也一样会重投：断线期间点的"允许/拒绝"，重连后补上来时
            # **卡片早就作废了**（请求已超时、任务已结束）。照放会去改一张
            # 不存在的卡，或者更糟 —— 批准一件已经被忘掉的事。
            # 用 `action_ts`（点击时刻）走同一条判据。
            act_ts = act0.get("action_ts") or (payload.get("event_ts") or "")
            if self._slack_drop_backlog(act_ts, "按钮点击", "", None):
                return

            # `value` 是**我们自己**渲染卡片时塞进去的（见 render.py）
            raw = act0.get("value") or ""
            try:
                value = json.loads(raw) if raw.startswith("{") else {}
            except Exception:
                value = {}
            user = (payload.get("user") or {}).get("id") or ""
            chan = (payload.get("channel") or {}).get("id") or ""
            container = payload.get("container") or {}
            msg_ts = container.get("message_ts") or (payload.get("message") or {}).get("ts") or ""

            chatid = value.get("chatid") or chan
            try:
                chat_type = int(value.get("chat_type") or 2)
            except (TypeError, ValueError):
                chat_type = 2

            log.info("[slack][card] key=%r task=%r chat=%s type=%s 操作人=%s",
                     value.get("key"), value.get("task_id"), chatid, chat_type,
                     (user[:6] + "…") if user else "?")

            self.core_handle_card_action({
                "key": value.get("key") or act0.get("action_id") or "",
                "userid": user,
                "chatid": chatid,
                "chat_type": chat_type,
                "raw_chatid": "" if chat_type == 1 else chatid,
                "task_id": value.get("task_id") or "",
                # 就地改卡需要 (channel, ts)，两个都放在这里；
                # 平台层按 "频道|消息ts" 解析（见 outbound.slack_update_from_token）。
                "reply_token": "%s|%s" % (chan, msg_ts),
            })
        except Exception as e:
            log.warning("[slack][card] 回调处理异常：%s: %s", type(e).__name__, e)