"""飞出站：发文本 / 发卡片 / 就地改卡 / CardKit 流式气泡 / 按 150 KiB 分片。

平台上限与限流**只在这个文件里**出现（对应企微那边的 20480 / 40058）。
几个数字都是 3d 实测出来的，不是抄别家的：

  · 单条消息 `content` ≤ **150 KiB（153,600 B）**，超了是 `230025`；
  · 均匀 **≤12 条/秒** 安全，但设计取 **5 条/秒** 的保守值；
  · `230020`（chat rate limit）是**限流**，不是失败 —— 要退避重试。
    这条同「结果未知 ≠ 失败」是同一族的语义，写错了会把一条正常消息判成没发出去。

⚠️ 与企微最大的差别：**飞书的 HTTP 返回值就是结果**。
企微是"WS 帧发出去 + 等一条异步回执"，所以那边 `sent=True` 只代表帧发出去了；
这边 `code == 0` 就是平台收下了，没有第二道回执。
"""

import json
import queue
import threading
import time

from common import log
from core.text import chunk_md, split_half

FEISHU_TOO_LONG = 230025      # content 超长（实测 153,600 B 触发）
FEISHU_RATE_LIMIT = 230020    # chat rate limit：退避重试，**不许当失败**
SEND_MAX_BYTES = 153600       # 实测上限，留 0 余量（153,000 过 / 153,600 拒）
RATE_PER_SEC = 5              # 设计值（实测 12 条/秒安全，取保守的一半不到）

# 卡片正文的**保守**上限。⚠️ 这个数**没有实测过**（卡片 JSON 的官方上限是 30 KB 量级），
# 所以取值刻意保守：宁可让长回答走"分片文本"，也不要先发一次注定被拒的卡片。
# 就算估错了也不会丢内容 —— 超限时 `_deliver` 会整条改走 `send_chunks`。
CARD_TEXT_MAX_BYTES = 28000


class FeishuBubble:
    """一条 CardKit 流式卡片 —— 契约里 `plat_open_bubble` 的飞书答案。

    生命周期（3b 已实测）：
        建卡（`streaming_mode: true` + 元素带 `element_id`）
        → 以 `{"type":"card","data":{"card_id":…}}` 当消息回出去
        → 逐帧改元素内容（`sequence` 必须递增）
        → 收口（`card.settings` 把 `streaming_mode` 置 false）

    ⚠️ 与企微 `Stream` 的语义对齐点：返回值同样带 `dead`。
       `dead=True` 的约定是「这一帧**什么都没发出去**，可以安全改道」；
       反过来"发出去了但没拿到结果"**不许**标 dead（会把同一条答案发两遍）。
    """

    ELEMENT_ID = "md_main"

    def __init__(self, bridge, message_id, chatid, chat_type):
        self.b = bridge
        self.message_id = message_id
        self.chatid = chatid
        self.chat_type = chat_type
        self.card_id = ""
        self.seq = 0
        self.created = False
        self.closed = False
        self._lock = threading.Lock()

    def _next_seq(self):
        self.seq += 1
        return self.seq

    def send(self, content, finish):
        with self._lock:
            if self.closed:
                return {"sent": False, "errcode": None,
                        "errmsg": "bubble closed", "dead": True}

            nbytes = len(content.encode("utf-8"))
            if nbytes > CARD_TEXT_MAX_BYTES:
                # 注定放不进一张卡 ⇒ **这一帧确定没发**，让调用方整条改走文本分片。
                # 不带 dead（dead 的语义是"气泡不可用，可安全改道"，这一帧只是太长），
                # 但 `_deliver` 看到 `FEISHU_TOO_LONG` 会先把气泡收口再改道。
                return {"sent": False, "errcode": FEISHU_TOO_LONG,
                        "errmsg": f"local: card text {nbytes}B > {CARD_TEXT_MAX_BYTES}"}

            if not self.created:
                r = self._open(content)
                if not r["sent"]:
                    return r            # `_open` 已经把 dead / unknown 标清楚了
                self.created = True
                if finish:
                    self.closed = True
                    self._close_stream()
                return r

            r = self._push(content)
            if finish:
                self.closed = True
                if r["sent"]:
                    self._close_stream()
            return r

    # -- 内部三步 --

    def _open(self, content):
        """建卡 + 把卡片当消息回出去。返回契约三元组，并标好 dead / unknown。"""
        from lark_oapi.api.cardkit.v1 import (
            CreateCardRequest, CreateCardRequestBody,
        )
        card = {
            "schema": "2.0",
            "config": {"streaming_mode": True, "update_multi": True},
            "body": {"elements": [
                {"tag": "markdown", "element_id": self.ELEMENT_ID, "content": content},
            ]},
        }

        def _create():
            body = (CreateCardRequestBody.builder()
                    .type("card_json")
                    .data(json.dumps(card, ensure_ascii=False)).build())
            return self.b.cli.cardkit.v1.card.create(
                CreateCardRequest.builder().request_body(body).build())

        r = self.b._feishu_call("建卡", _create)
        if not r["sent"]:
            # 卡都没建出来 ⇒ 这一帧什么都没发，可以安全改道
            self.closed = True
            return dict(r, dead=True)
        # 卡号在**原始响应**里（`_feishu_call` 的 `raw` 字段），不在结果码三元组里。
        self.card_id = getattr(getattr(r.get("raw"), "data", None), "card_id", "") or ""
        if not self.card_id:
            log.error("[feishu] 建卡成功但没拿到 card_id，气泡不可用")
            self.closed = True
            return {"sent": False, "errcode": None,
                    "errmsg": "no card_id", "dead": True}

        r2 = self.b._reply_card(self.message_id, self.card_id)
        if not r2["sent"]:
            self.closed = True
            if r2.get("errcode") is None:
                # **结果未知**：卡片有可能已经发出去了。
                # ⇒ 绝不能标 dead（dead 的约定是"什么都没发出去、可以安全改道"），
                #   标了就可能在用户那儿多出一张空卡；但也**不能**当已送达 ——
                #   标 `unknown` 让 `_deliver` 把正文另发一条（宁可重复，不可丢内容）。
                log.error("[feishu] 气泡消息结果未知（%s），正文将另发一条",
                          (r2.get("errmsg") or "")[:80])
                return dict(r2, unknown=True)
            return dict(r2, dead=True)

        self.created = True
        return r2

    def _push(self, content):
        from lark_oapi.api.cardkit.v1 import (
            ContentCardElementRequest, ContentCardElementRequestBody,
        )
        seq = self._next_seq()

        def _do():
            body = (ContentCardElementRequestBody.builder()
                    .content(content).sequence(seq).build())
            return self.b.cli.cardkit.v1.card_element.content(
                ContentCardElementRequest.builder()
                .card_id(self.card_id).element_id(self.ELEMENT_ID)
                .request_body(body).build())

        return self.b._feishu_call(f"流式帧 seq={seq}", _do)

    def _close_stream(self):
        """收口。失败只记日志 —— 卡片停在"还在长"的状态，但内容已经是对的。"""
        from lark_oapi.api.cardkit.v1 import (
            SettingsCardRequest, SettingsCardRequestBody,
        )
        seq = self._next_seq()

        def _do():
            body = (SettingsCardRequestBody.builder()
                    .settings(json.dumps({"config": {"streaming_mode": False}}))
                    .sequence(seq).build())
            return self.b.cli.cardkit.v1.card.settings(
                SettingsCardRequest.builder().card_id(self.card_id)
                .request_body(body).build())

        r = self.b._feishu_call("收口", _do)
        if not r["sent"]:
            log.warning("[feishu] 收口失败 code=%s %s",
                        r["errcode"], (r["errmsg"] or "")[:120])


class FeishuOutboundMixin:
    # -- 基础：限流 + 统一结果码 --

    def _rid_type(self, chat_type):
        """单聊发给 `open_id`，群聊发给 `chat_id`（见 codec 里的 chatid 口径）。"""
        try:
            return "open_id" if int(chat_type) == 1 else "chat_id"
        except (TypeError, ValueError):
            return "chat_id"

    def _rate_wait(self):
        """令牌桶：任意 1 秒窗口内最多 `RATE_PER_SEC` 条。

        实测 12 条/秒安全，但那时是"均匀发"；真实场景里 ProgressPump 可能连推几帧。
        取 5 条/秒，代价只是慢一点，换来不会撞 `230020`。
        """
        with self._rate_lock:
            while True:
                now = time.time()
                self._rate_events = [t for t in self._rate_events if now - t < 1.0]
                if len(self._rate_events) < RATE_PER_SEC:
                    self._rate_events.append(now)
                    return
                time.sleep(max(0.02, 1.0 - (now - self._rate_events[0])))

    def _feishu_call(self, label, fn, tries=4):
        """调一次飞书 HTTP 接口，返回契约要求的三元组。

        · `code == 0` ⇒ `sent=True`
        · `230020`   ⇒ **退避重试**（限流不是失败）
        · 抛异常 / 网络断 ⇒ `errcode=None`，语义是**结果未知**（不许据此重发内容）
        """
        res = {"sent": False, "errcode": None, "errmsg": ""}
        for i in range(tries):
            self._rate_wait()
            try:
                r = fn()
            except Exception as e:
                log.warning("[feishu] %s 调用异常：%s: %s", label, type(e).__name__, e)
                return {"sent": False, "errcode": None,
                        "errmsg": f"{type(e).__name__}: {e}"}

            code = getattr(r, "code", None)
            msg = getattr(r, "msg", None) or ""
            # `raw` 只给"需要从响应里取东西"的调用方（建卡要拿 card_id）。
            # 内核只读 sent / errcode / errmsg 三个键，多一个键不影响契约。
            res = {"sent": code == 0, "errcode": code, "errmsg": msg, "raw": r}
            if res["sent"]:
                log.info("[feishu] %s ok", label)
                return res
            if code == FEISHU_RATE_LIMIT and i < tries - 1:
                wait = 0.5 * (2 ** i)
                log.warning("[feishu] %s 被限流 230020，%.1fs 后重试（第 %d 次）",
                            label, wait, i + 1)
                time.sleep(wait)
                continue
            log.error("[feishu] %s 失败 code=%s msg=%s", label, code, msg[:160])
            return res
        return res

    # -- 主动推送 --

    def send_text(self, chatid, chat_type, text, wait=True):
        """主动推送一条文本。`wait` 参数保留只为与契约签名一致 ——
        HTTP 本身就是同步的，没有"不等回执"这个选项。"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if not chatid:
            log.warning("[feishu][send] 无 chatid，跳过推送")
            return {"sent": False, "errcode": None, "errmsg": "no chatid"}
        self._note_side(chatid, chat_type)
        nbytes = len((text or "").encode("utf-8"))
        body = (CreateMessageRequestBody.builder()
                .receive_id(chatid).msg_type("text")
                .content(json.dumps({"text": text or ""}, ensure_ascii=False)).build())

        def _do():
            return self.cli.im.v1.message.create(
                CreateMessageRequest.builder()
                .receive_id_type(self._rid_type(chat_type))
                .request_body(body).build())

        return self._feishu_call(f"text -> {chatid[:12]}… ({chat_type}) {nbytes}B", _do)

    def send_card(self, chatid, chat_type, card, wait=True):
        """主动推送一张 Card 2.0 卡片。"""
        from lark_oapi.api.im.v1 import CreateMessageRequest, CreateMessageRequestBody
        if not chatid:
            log.warning("[feishu][send] 无 chatid，跳过卡片")
            return {"sent": False, "errcode": None, "errmsg": "no chatid"}
        self._note_side(chatid, chat_type)
        body = (CreateMessageRequestBody.builder()
                .receive_id(chatid).msg_type("interactive")
                .content(json.dumps(card, ensure_ascii=False)).build())

        def _do():
            return self.cli.im.v1.message.create(
                CreateMessageRequest.builder()
                .receive_id_type(self._rid_type(chat_type))
                .request_body(body).build())

        return self._feishu_call(f"card -> {chatid[:12]}… ({chat_type})", _do)

    def _reply_card(self, message_id, card_id):
        """把一张**卡片实体**当消息回出去（CardKit 流式气泡的第二步）。"""
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        content = json.dumps({"type": "card", "data": {"card_id": card_id}})
        body = (ReplyMessageRequestBody.builder()
                .content(content).msg_type("interactive").build())

        def _do():
            return self.cli.im.v1.message.reply(
                ReplyMessageRequest.builder().message_id(message_id)
                .request_body(body).build())

        return self._feishu_call(f"reply card {card_id[-8:]}", _do)

    def send_update_card(self, token, card):
        """就地更新已发卡片。

        ⚠️ **飞书只有一条通道能干这件事：卡片回调的返回值。**
        回调之外没有可用的"改这张卡"接口（消息级 PATCH 我们**没实测过**，
        所以不在这里假装能用）。不在回调上下文里就如实返回"没送达"，
        让内核走它自己的降级路径 —— 绝不假装成功。
        """
        ctx = getattr(self, "_card_ctx", None)
        resp = getattr(ctx, "resp", None) if ctx is not None else None
        if resp is None:
            log.warning("[feishu][card] 不在卡片回调上下文里，无法就地更新（token=%s）",
                        (token or "")[-8:])
            return {"sent": False, "errcode": None,
                    "errmsg": "card update only available inside a card callback"}
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackCard
            cb = CallBackCard()
            cb.type = "raw"
            cb.data = card
            resp.card = cb
            log.info("[feishu][card] 已把新卡挂到本次回调响应上（按钮就此撤销）")
            return {"sent": True, "errcode": 0, "errmsg": "in callback response"}
        except Exception as e:
            log.warning("[feishu][card] 挂新卡失败：%s", e)
            return {"sent": False, "errcode": None, "errmsg": str(e)}

    # -- 被动回复 --

    def reply(self, token, content):
        """被动回复：回在**触发这次动作的那条消息**下面。

        与企微同一个理由必须**异步**：这个方法可能跑在事件循环线程上
        （消息入口虽然只入队，但卡片回调是同步的），而一次 HTTP 往返要几百毫秒。
        队列满就丢 —— 这类回执本来就是"知道了"性质的。
        """
        if not token:
            return False
        if self._reply_q.full():
            log.warning("[feishu][reply] 回复队列已满，丢弃一条：%r", (content or "")[:20])
            return False
        self._note_side()
        self._reply_q.put_nowait((token, content))
        return True

    def _reply_worker(self):
        while not self.stop:
            try:
                token, content = self._reply_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._post_reply(token, content)
            except Exception as e:
                log.warning("[feishu][reply] 失败：%s", e)
            finally:
                self._reply_q.task_done()

    def _post_reply(self, message_id, content):
        from lark_oapi.api.im.v1 import ReplyMessageRequest, ReplyMessageRequestBody
        body = (ReplyMessageRequestBody.builder()
                .content(json.dumps({"text": content or ""}, ensure_ascii=False))
                .msg_type("text").build())

        def _do():
            return self.cli.im.v1.message.reply(
                ReplyMessageRequest.builder().message_id(message_id)
                .request_body(body).build())

        r = self._feishu_call(f"reply -> {message_id[-8:]}", _do)
        return r["sent"]

    # -- 分片 --

    def send_chunks(self, chatid, chat_type, text):
        """**先整条发**；只有被 `230025`（超长）拒了，才切开重发。

        与企微同一条策略（用户 2026-10-27 认可）：绝大多数回答一条就读完了，
        预切只会把它拆得又碎又多一次失败机会。
        上限是企微的 7.5 倍 ⇒ 切分粒度也按飞书自己的来（`FEISHU_CHUNK_BYTES`），
        **不照搬企微的 18000**。

        返回未送达列表 `[(label, {errcode, errmsg}), …]`，空列表 = 全部送达。
        """
        text = text or ""
        if not text:
            return []
        nbytes = len(text.encode("utf-8"))

        r = self.send_text(chatid, chat_type, text)
        if r["sent"]:
            return []

        if r["errcode"] is None:
            # 没拿到结果 = **结果未知**，不是"失败"。按项目约定，不能让用户以为
            # 没发出去而重发 —— 重发会让 Agent 把刚才那件事再做一遍。
            log.error("[feishu][send] 结果未知（%s），不重发", (r["errmsg"] or "")[:80])
            self.send_text(chatid, chat_type,
                           "❔ **推送结果未知**：没拿到平台的明确结果。\n"
                           "> 它可能已经发出去了，也可能没有。**先翻一下有没有**，"
                           "别急着重发 —— 重发会让它把刚才那件事再做一遍。")
            return [(0, r)]

        if r["errcode"] != FEISHU_TOO_LONG:
            log.error("[feishu][send] 整条发送失败 %dB code=%s %s",
                      nbytes, r["errcode"], (r["errmsg"] or "")[:160])
            self.send_text(chatid, chat_type,
                           f"⚠️ **结果没能送达**（code=`{r['errcode']}`，{nbytes} 字节）。\n"
                           f"> 平台明确拒收了，内容没发出去。看桥的日志。")
            return [(0, r)]

        parts = chunk_md(text, self.feishu_chunk_bytes)
        log.warning("[feishu][send] 整条 %dB 超限被拒，切成 %d 片重发", nbytes, len(parts))
        return self._send_parts(chatid, chat_type, parts)

    def _send_parts(self, chatid, chat_type, parts):
        n = len(parts)
        failed = []
        for i in range(1, n + 1):
            p = parts[i - 1]
            body = f"({i}/{n})\n{p}" if n > 1 else p
            failed.extend(self._send_piece(chatid, chat_type, body, i, n))
        if failed:
            label, rf = failed[0]
            log.error("[feishu][send] 有 %d 段未送达，首段 %s/%d code=%s",
                      len(failed), label, n, rf["errcode"])
            self.send_text(chatid, chat_type,
                           f"⚠️ **有 {len(failed)} 段内容没送达**（首段：第 {label}/{n} 段，"
                           f"code=`{rf['errcode']}`）。\n> 上面的回答可能不完整，别把它当全文。")
        return failed

    def _send_piece(self, chatid, chat_type, body, idx, n, depth=0):
        """发一片；仍超长就按字节对半再发（最多 3 层）。

        正常走不到这里 —— 只有"实际可用字节比我们以为的更小"时才触发
        （比如加了 `(i/n)` 前缀后刚好越界）。
        """
        r = self.send_text(chatid, chat_type, body)
        if r["sent"]:
            return []
        if r["errcode"] == FEISHU_TOO_LONG and depth < 3:
            halves = split_half(body)
            if len(halves) == 2:
                log.warning("[feishu][send] 第 %d/%d 片仍超长（%dB），对半重发",
                            idx, n, len(body.encode("utf-8")))
                out = []
                for k, h in enumerate(halves, 1):
                    out.extend(self._send_piece(
                        chatid, chat_type, f"({idx}/{n} 续{k})\n{h}", idx, n, depth + 1))
                return out
        return [(str(idx), r)]

    def _deliver(self, st, chatid, chat_type, text):
        """把最终文本**收口到同一个气泡**里；放不下 / 气泡不可用就整条改走分片。

        退化路径的硬要求是**不丢内容**，顺序是：
          ① 整条塞进气泡（绝大多数回答走到这里就结束了）；
          ② 气泡不可用（建卡失败 / 已关闭）⇒ `send_chunks`（另发一条）；
          ③ **结果未知 ⇒ 绝不重发**（沿用项目总约定）；
          ④ 明确被拒（含"太长"）⇒ `send_chunks`。
        """
        text = text or ""
        if not text:
            return []

        r = st.send(text, finish=True)
        if r["sent"]:
            return []
        if r.get("unknown"):
            # 气泡那条消息**可能发出去了也可能没有** ⇒ 正文另发一条。
            # 宁可用户多看到一张空卡，也不能让答案消失。
            log.error("[feishu][deliver] 气泡结果未知 ⇒ 正文另发一条")
            failed = self.send_chunks(chatid, chat_type, text)
            self.send_text(chatid, chat_type,
                           "☝️ 上面若有一张停在「处理中」的卡片，请忽略它 —— 正文见上一条。")
            return failed
        if r.get("dead"):
            log.warning("[feishu][deliver] 气泡不可用（%s）⇒ 正文改走主动推送",
                        r.get("errmsg"))
            return self.send_chunks(chatid, chat_type, text)
        if r["errcode"] == FEISHU_TOO_LONG:
            # 正文放不进一张卡：**先把气泡收口**，别让它永远停在「处理中」，
            # 然后整条改走文本分片（内容一点不少）。
            st.send("✅ 已完成，回答较长，见下一条 ↓", finish=True)
            log.warning("[feishu][deliver] 正文 %dB 超过卡片容量 ⇒ 改走文本分片",
                        len(text.encode("utf-8")))
            return self.send_chunks(chatid, chat_type, text)
        if r["errcode"] is None:
            log.error("[feishu][deliver] 定稿没拿到结果，按「结果未知」处理")
            self.send_text(chatid, chat_type,
                           "❔ **推送结果未知**：没拿到平台的明确结果。\n"
                           "> 它可能已经发出去了，也可能没有。**先翻一下有没有**，"
                           "别急着重发。")
            return [(0, r)]

        log.warning("[feishu][deliver] 气泡定稿失败 code=%s ⇒ 整条改走主动推送",
                    r["errcode"])
        return self.send_chunks(chatid, chat_type, text)