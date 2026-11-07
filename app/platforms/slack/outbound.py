"""Slack 发送侧：发消息 / 改消息 / 分片 / 气泡。

⚠️ **和企微、飞书都不一样的一条：超长是"静默截断"，不是报错。**

  · `chat.postMessage`：官方建议 text ≤ **4,000 字符**；超过 **40,000 字符**
    会被截断，响应里只多一条 `message_truncated` 警告 —— **`ok` 仍然是 true**。
  · `chat.update`：text 上限 **4,000 字符**，超了返回 `msg_too_long`。

  ⇒ 企微/飞书那套"先整条发、被平台拒了才切片"在这里**不成立**：
    平台不会拒，它会悄悄砍掉后半截。所以 Slack 必须**预切**。
  ⇒ 切分口径是**字符**，不是字节（中文 1 字 = 1 字符，但 3 字节）。
    照抄 `CHUNK_BYTES=18000` 会把一条 18 KB 的中文回答切成 6000 字的碎片。

⚠️ 频率：官方文档 `chat.postMessage` 写 **1 条/秒/频道**，`chat.update` 是
  Tier 3（**50+/分钟**）。两者都做成节流，不做"撞了再退避"。
"""

import threading
import time

from common import log

# 4,000 是官方给的上限；留 200 字符余量，别卡着边界发。
TEXT_MAX_CHARS = 4000
SAFE_CHARS = 3800
# 超过它就是"平台会悄悄截断"的区域。这个数只用来**报警**，不用来切分。
TRUNCATE_CHARS = 40000

# `chat.postMessage` 1 条/秒/频道；`chat.update` 50+/分钟 ≈ 0.83/秒 ⇒ 留一点余量。
POST_MIN_SEC = 1.05
UPDATE_MIN_SEC = 1.25

# 我们自己定义的"这一帧太长"结果码（`errcode` 的语义由平台层自己解释）。
SLACK_TOO_LONG = 41301


def _split_chars(text, limit):
    """按**字符**切分，优先在段落/换行处断开。

    和 `core/text.chunk_md` 的区别只有一个：那个按**字节**。
    这里必须按字符 —— Slack 的上限是按字符算的。
    """
    text = text or ""
    if len(text) <= limit:
        return [text]
    parts, cur = [], ""
    # 先按空行分段，段内再按行，最后才硬切 —— 尽量不把一个句子劈开
    for para in text.split("\n\n"):
        piece = (para + "\n\n")
        if len(piece) <= limit:
            if len(cur) + len(piece) > limit:
                parts.append(cur)
                cur = ""
            cur += piece
            continue
        if cur:
            parts.append(cur)
            cur = ""
        # 这一段自己就超了
        lines = piece.split("\n")
        if len(lines) > 1:
            for line in lines:
                seg = line + "\n"
                if len(seg) > limit:
                    if cur:
                        parts.append(cur)
                        cur = ""
                    parts.extend(seg[i:i + limit]
                                 for i in range(0, len(seg), limit))
                elif len(cur) + len(seg) > limit:
                    parts.append(cur)
                    cur = seg
                else:
                    cur += seg
        else:
            parts.extend(piece[i:i + limit] for i in range(0, len(piece), limit))
    if cur:
        parts.append(cur)
    return [p for p in parts if p] or [""]


class SlackBubble:
    """一条可以反复 `chat.update` 的消息 —— 契约里 `plat_open_bubble` 的 Slack 答案。

    与企微 `Stream` / 飞书 CardKit 的语义对齐点：返回值同样带 `dead`。
    `dead=True` 的约定是「这一帧**什么都没发出去**，可以安全改道」；
    反过来"发出去了但没拿到结果"**不许**标 dead（会把同一条答案发两遍）。

    ⚠️ Slack **没有流式接口**，所以这里不是"逐帧推内容"，而是"同一条消息反复改"。
    这正好对上我们本来就有的策略（变化才推 + 心跳兜底），不需要退化成"一段一条"。
    """

    def __init__(self, bridge, channel, thread_ts):
        self.b = bridge
        self.channel = channel
        self.thread_ts = thread_ts or ""
        self.ts = ""
        self.created = False
        self.closed = False
        self._lock = threading.Lock()

    def send(self, content, finish):
        content = content or ""
        with self._lock:
            if self.closed:
                return {"sent": False, "errcode": None,
                        "errmsg": "bubble closed", "dead": True}
            if len(content) > TEXT_MAX_CHARS:
                # 注定放不进一条消息 ⇒ 这一帧**确定没发**，让调用方改走分片。
                return {"sent": False, "errcode": SLACK_TOO_LONG,
                        "errmsg": "local: %d chars > %d"
                                  % (len(content), TEXT_MAX_CHARS)}

            if not self.created:
                r = self.b.slack_post(self.channel, content,
                                      thread_ts=self.thread_ts)
                if r.get("ok"):
                    self.ts = r.get("ts") or ""
                    self.created = True
                    # 让后续主动推送也落进同一条线程
                    self.b._slack_thread_of[self.channel] = \
                        self.thread_ts or self.ts
                    if finish:
                        self.closed = True
                    return {"sent": True, "errcode": 0, "errmsg": ""}
                err = r.get("error") or ""
                if err in ("channel_not_found", "not_in_channel", "is_archived"):
                    # 这个频道根本发不了 ⇒ 气泡不可用，可以安全改道
                    self.closed = True
                    return {"sent": False, "errcode": 1, "errmsg": err, "dead": True}
                # 其它错误：帧可能到了也可能没到 ⇒ **结果未知**，不许重发
                return {"sent": False, "errcode": None, "errmsg": err or "unknown"}

            r = self.b.slack_update(self.channel, self.ts, content)
            if r.get("ok"):
                if finish:
                    self.closed = True
                # 更新是幂等的：同一条消息改成同样的内容没有副作用
                return {"sent": True, "errcode": 0, "errmsg": ""}
            err = r.get("error") or ""
            if err == "message_not_found":
                self.closed = True
                return {"sent": False, "errcode": 1, "errmsg": err, "dead": True}
            return {"sent": False, "errcode": None, "errmsg": err or "unknown"}


class SlackOutboundMixin:
    """HTTP 侧：发消息 / 改消息 / 分片。长连接那边不碰这些。"""

    # -- 节流 --

    def _slack_wait(self, bucket, key, min_sec):
        """按 (桶, 键) 节流。桶之间互不影响：post 和 update 是两套额度。"""
        with self._rate_lock:
            table = self._rate_last.setdefault(bucket, {})
            wait = table.get(key, 0.0) + min_sec - time.time()
            if wait > 0:
                time.sleep(wait)
            table[key] = time.time()

    # -- 底层两个方法 --

    def slack_post(self, channel, text, blocks=None, thread_ts=None):
        self._slack_wait("post", channel, POST_MIN_SEC)
        params = {"channel": channel, "text": text}
        if blocks:
            import json
            params["blocks"] = json.dumps(blocks, ensure_ascii=False)
        if thread_ts and thread_ts != channel:
            params["thread_ts"] = thread_ts
        r = self.slack_http("chat.postMessage", self.slack_bot_token, params)
        if r.get("ok"):
            if r.get("warning"):
                log.warning("[slack][send] 平台警告：%s", r.get("warning"))
            return r
        log.error("[slack][send] postMessage 失败：%s",
                  r.get("detail") or r.get("error") or r)
        return r

    def slack_update(self, channel, ts, text, blocks=None):
        self._slack_wait("update", channel, UPDATE_MIN_SEC)
        params = {"channel": channel, "ts": ts, "text": text}
        if blocks:
            import json
            params["blocks"] = json.dumps(blocks, ensure_ascii=False)
        r = self.slack_http("chat.update", self.slack_bot_token, params)
        if not r.get("ok"):
            log.warning("[slack][send] update 失败：%s",
                        r.get("detail") or r.get("error") or r)
        return r

    # -- 契约里的发送方法 --

    def send_text(self, chatid, chat_type, text, wait=True):
        """主动推送一条文本。

        ⚠️ 超长**必须在这里就切**：`chat.postMessage` 对 40,000 字符以上是
        **静默截断**（`ok: true` + `message_truncated` 警告），不切就丢内容。
        """
        text = text or ""
        if len(text) > TEXT_MAX_CHARS:
            # 交给 send_chunks 走完整的分片 + 未送达清单
            failed = self.send_chunks(chatid, chat_type, text)
            return {"sent": not failed, "errcode": 0 if not failed else 1,
                    "errmsg": "" if not failed else "部分未送达"}
        thread_ts = self._slack_thread_of.get(chatid) or ""
        r = self.slack_post(chatid, text, thread_ts=thread_ts)
        if r.get("ok"):
            return {"sent": True, "errcode": 0, "errmsg": ""}
        err = r.get("error") or ""
        # 平台**明确回复了拒绝** ⇒ 不是"结果未知"
        return {"sent": False, "errcode": 1, "errmsg": err}

    def send_card(self, chatid, chat_type, blocks, wait=True):
        """发一张 Block Kit 卡片。`blocks` 是已经渲染好的列表。"""
        fallback = "（这是一张交互卡片，请用 Slack 客户端查看）"
        thread_ts = self._slack_thread_of.get(chatid) or ""
        r = self.slack_post(chatid, fallback, blocks=blocks, thread_ts=thread_ts)
        if r.get("ok"):
            ts = r.get("ts") or ""
            # `token` = 这张卡的位置（`channel|ts`），给内核在"任务被停"时收口用。
            return {"sent": True, "errcode": 0, "errmsg": "", "ts": ts,
                    "token": "%s|%s" % (chatid, ts) if ts else ""}
        return {"sent": False, "errcode": 1, "errmsg": r.get("error") or ""}

    def _slack_parse_token(self, token):
        """`channel|ts` → (channel, ts)。解析不出来返回 (token, "")。"""
        t = token or ""
        if "|" in t:
            ch, ts = t.split("|", 1)
            return ch, ts
        return t, ""

    def update_card(self, token, blocks):
        ch, ts = self._slack_parse_token(token)
        if not ch or not ts:
            log.warning("[slack][send] 改卡缺少 channel/ts：%r", (token or "")[:40])
            return {"sent": False, "errcode": 1, "errmsg": "bad token"}
        r = self.slack_update(ch, ts, "（卡片已更新）", blocks=blocks)
        return {"sent": bool(r.get("ok")), "errcode": 0 if r.get("ok") else 1,
                "errmsg": "" if r.get("ok") else (r.get("error") or "")}

    def reply(self, token, content):
        """被动回复（"收到 / 忙碌中"）。token = `channel|ts`，回在同一个线程里。"""
        ch, ts = self._slack_parse_token(token)
        if not ch:
            return False
        r = self.slack_post(ch, content, thread_ts=ts)
        return bool(r.get("ok"))

    def send_chunks(self, chatid, chat_type, text):
        """超长文本分片推送。

        ⚠️ **必须预切**（和企微/飞书相反）。那两家的策略是"先整条发，被拒才切"，
        因为平台会用错误码拒收超长；Slack 是**静默截断**，`ok` 照样是 true
        ⇒ 不预切就是**悄悄丢内容**，而且用户看不出来。

        返回未送达列表 `[(label, {errcode, errmsg}), …]`，空列表 = 全部送达。
        """
        text = text or ""
        if not text:
            return []
        parts = _split_chars(text, self.slack_chunk_chars)
        if len(parts) > 1:
            log.info("[slack][send] 正文 %d 字符，按 %d 字符切成 %d 片",
                     len(text), self.slack_chunk_chars, len(parts))
        return self._send_parts(chatid, chat_type, parts)

    def _send_parts(self, chatid, chat_type, parts):
        n = len(parts)
        failed = []
        thread_ts = self._slack_thread_of.get(chatid) or ""
        for i in range(1, n + 1):
            piece = parts[i - 1]
            # 编号只占几十个字符，切分时已经留了余量
            body = piece if n == 1 else "%s\n\n_(%d/%d)_" % (piece, i, n)
            if len(body) > TEXT_MAX_CHARS:      # 兜底：留的余量不该被编号吃掉
                body = piece[:TEXT_MAX_CHARS]
            r = self.slack_post(chatid, body, thread_ts=thread_ts)
            if not r.get("ok"):
                failed.append((i, {"errcode": 1,
                                   "errmsg": r.get("error") or "unknown"}))
                log.error("[slack][send] 第 %d/%d 片失败：%s",
                          i, n, r.get("error") or r)
        if not failed:
            log.info("[slack][send] %d 片全部送达", n)
        else:
            try:
                self.slack_post(
                    chatid,
                    "⚠️ **有 %d/%d 段没送达**（第 %s 段）。内容没发全，别当它不存在。"
                    % (len(failed), n, ",".join(str(x[0]) for x in failed)),
                    thread_ts=thread_ts)
            except Exception:
                pass
        return failed

    def _deliver(self, st, chatid, chat_type, text):
        """把最终文本收口到同一个气泡里；放不下 / 气泡不可用就整条改走分片。"""
        text = text or ""
        if not text:
            return []
        if st is None:
            return self.send_chunks(chatid, chat_type, text)

        r = st.send(text, finish=True)
        if r["sent"]:
            # 成功也要留一行：`slack_post` 只在失败时说话，否则"到底发没发出去"
            # 在日志里完全看不出来（2026-11-07 首跑就因为这一点误判成"没发送"）。
            log.info("[slack][deliver] 正文已落进气泡（%d 字符）", len(text))
            return []
        if r.get("dead"):
            log.warning("[slack][deliver] 气泡不可用（%s）⇒ 正文改走主动推送",
                        r.get("errmsg"))
            return self.send_chunks(chatid, chat_type, text)
        if r["errcode"] == SLACK_TOO_LONG:
            # 正文超过单条 4000 字符：先把气泡收口（别停在"处理中"），再分片
            st.send("✅ 已完成，回答较长，见下一条 ↓", finish=True)
            log.warning("[slack][deliver] 正文 %d 字符超过单条上限 ⇒ 改走分片",
                        len(text))
            return self.send_chunks(chatid, chat_type, text)
        # 结果未知：气泡那条可能到了也可能没到 ⇒ 正文另发，宁可多一条
        log.error("[slack][deliver] 定稿没拿到结果 ⇒ 正文另发一条")
        failed = self.send_chunks(chatid, chat_type, text)
        self.send_text(chatid, chat_type,
                       "☝️ 上面若有一条停在「处理中」的消息，请忽略它 —— 正文见下一条。")
        return failed