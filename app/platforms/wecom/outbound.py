"""企微出站：WS 帧构造、回执等待、流式气泡、按 20480 字节分片。

`40058` / `42045` / 10 分钟窗口**全部只在这个文件里**出现 —— 这是平台层该有的样子。
"""

import json
import queue
import threading
import time
import uuid

import requests

from common import log, knob
from core.text import chunk_md, split_half

STREAM_LIMIT = 20480          # 平台硬上限，超了必然 40058


class Stream:
    """一次回调对应的一条流式消息：`aibot_respond_msg` + 自定义 `stream.id`。

    **为什么要有它**（用户 2026-10-28 提的调整）：原来 ack 走 `response_url` HTTP POST、
    正文走 `aibot_send_msg`，手机上就是**两条独立气泡** —— 看着像"还有个对话在跑"。
    平台本身支持 `stream`：首次 `finish=false` 建气泡，**同一个 `stream.id`** 再发就
    覆盖内容（`content` 是**全文**，不是增量），`finish=true` 定稿。
    ⇒ ack 和正文落在同一个气泡里，正文直接把「收到，处理中…」盖掉。

    ⚠️ 三条约束（少了任何一条都会**静默坏掉**）：
      · `headers.req_id` 必须是**回调里的那个** req_id（平台拿它关联回调与回复）；
        我们自己编的 req_id 平台不认。
      · 首帧起 10 分钟内必须定稿，超了平台自动结束 ⇒ 之后所有更新无效，
        只能改走 `aibot_send_msg` 另发一条（`send()` 会返回 `dead=True`）。
      · 已 `finish=true` 的气泡**不可再更新** ⇒ 内部记 `closed`，别指望"再补一句"。
    """

    def __init__(self, bridge, req_id, chatid, chat_type):
        self.b = bridge
        self.req_id = req_id
        self.chatid = chatid
        self.chat_type = chat_type
        self.id = "s" + uuid.uuid4().hex[:24]
        self.started = time.time()
        self.closed = False      # 已定稿，或已判定不可用
        self.created = False     # 首帧是否成功建出气泡（仅用于日志/诊断）
        self._lock = threading.Lock()

    def remaining(self):
        return knob("STREAM_MAX_SEC") - (time.time() - self.started)

    def send(self, content, finish):
        """发/更新气泡内容。返回值同 `_send_wait`，另加 `dead`：

        `dead=True` ⇒ **这一帧什么都没发出去**，气泡不可用（已关闭或超窗口），
        调用方可以安全地改走主动推送 —— 不会重复投递。
        （`errcode is None` 且没有 `dead` 才是"结果未知"，那种不许重发。）
        """
        nbytes = len(content.encode("utf-8"))
        with self._lock:
            # 「气泡还能不能用」优先于「内容多长」：已关闭就别再让调用方去切片，
            # 否则它会先切一轮、再发现气泡早就没了。
            if self.closed:
                return {"sent": False, "errcode": None, "errmsg": "stream closed", "dead": True}
            if self.remaining() <= 0:
                self.closed = True
                log.warning("[stream] 已超过 %ds 窗口（气泡 %s），本条改走主动推送",
                            knob("STREAM_MAX_SEC"), self.id[:8])
                return {"sent": False, "errcode": None, "errmsg": "stream expired", "dead": True}
            if nbytes > STREAM_LIMIT:
                # 注定被 40058 拒，别浪费一次往返；语义保持和平台一致，让调用方走切片分支
                return {"sent": False, "errcode": 40058,
                        "errmsg": f"local: content {nbytes}B > {STREAM_LIMIT}"}

            frame = {
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": self.req_id},
                "body": {"msgtype": "stream",
                         "stream": {"id": self.id, "finish": bool(finish),
                                    "content": content}},
            }
            r = self.b._send_wait(
                frame, f"stream {self.id[:8]} finish={finish} {nbytes}B")
            if r["sent"]:
                self.created = True
                if finish:
                    self.closed = True
            elif r.get("unsent"):
                # 连接不在，这一帧压根没出去 ⇒ 气泡没建出来，交给调用方改道推送
                self.closed = True
                r = dict(r, dead=True)
            elif r["errcode"] is None:
                # 帧发出去了但没有回执 ⇒ **结果未知**（可能已经落地）。
                # ⚠️ 两种处理都不能错：
                #   · 不能标 `dead` —— dead 的约定是"什么都没发出去、可以安全改道"，
                #     而这次可能恰恰发出去了，标了就会重复投递；
                #   · 也**不要**判死气泡 —— 回执丢一次（心跳回执被误当、平台慢）很常见，
                #     真正落地时后续同一 id 仍能覆盖。留下来继续用，代价只是"ack 可能卡着"；
                #     判死反而是**确定**退化成两个气泡。
                log.warning("[stream] 气泡 %s 这一帧没回执（结果未知），仍继续用这个 id",
                            self.id[:8])
            return r


class WeComOutboundMixin:
    def send_markdown(self, chatid, chat_type, content, wait=True):
        """主动推送。返回 {sent, errcode, errmsg}。

        ⚠️ 必须**等平台回执**才算数：`_send` 的 True 只代表 WS 帧发出去了。
        实测平台会异步回 errcode 拒收（40058 = body.markdown.content exceed max length 20480）。
        早先的版本把 True 当成功 ⇒ 分片被拒也会当成送达，属于静默失败。
        """
        if not chatid:
            log.warning("[send] 无 chatid，跳过推送")
            return {"sent": False, "errcode": None, "errmsg": "no chatid"}
        self._note_side()
        frame = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._new_req_id()},
            "body": {"chatid": chatid, "chat_type": int(chat_type),
                     "msgtype": "markdown", "markdown": {"content": content}},
        }
        nbytes = len(content.encode("utf-8"))
        if not wait:
            self._send(frame)
            log.info("[send] markdown -> %s (type=%s) %dB 不等回执", chatid, chat_type, nbytes)
            return {"sent": True, "errcode": None, "errmsg": ""}
        return self._send_wait(frame, f"markdown -> {chatid} (type={chat_type}) {nbytes}B")


    def send_card(self, chatid, chat_type, card, wait=True):
        """主动推送**模板卡片**（`msgtype=template_card`）。

        卡片是 markdown 之外唯一被 `aibot_send_msg` 支持的富消息类型。
        能发哪种卡由 `template_card.card_type` 决定：
          · `text_notice` —— 纯展示，无回调要求；
          · `button_interaction` —— 带按钮，点了会回调 `template_card_event`。
        ⚠️ 官方文档写着「按钮类卡片要求机器人**设置了回调URL**」(`101032:1307`)，
        而我们是**长连接**模式（没有回调 URL）⇒ 这类卡片能不能下发**必须实测**，
        所以这里不假设成功，照样走 `_send_wait` 等平台回执。
        """
        if not chatid:
            log.warning("[send] 无 chatid，跳过卡片")
            return {"sent": False, "errcode": None, "errmsg": "no chatid"}
        self._note_side()
        frame = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._new_req_id()},
            "body": {"chatid": chatid, "chat_type": int(chat_type),
                     "msgtype": "template_card", "template_card": card},
        }
        label = f"card[{(card or {}).get('card_type', '?')}] -> {chatid} (type={chat_type})"
        if not wait:
            self._send(frame)
            log.info("[send] %s 不等回执", label)
            return {"sent": True, "errcode": None, "errmsg": ""}
        return self._send_wait(frame, label)


    def send_update_card(self, req_id, card):
        """把**已经发出去的那张卡**就地改掉（`aibot_respond_update_msg`）。

        平台的三条硬约束（官方长连接文档原文，已核对）：
          · `headers.req_id` 必须是**那次点击事件**的 req_id（自己编的不认）；
          · **只能在收到点击事件后 5 秒内发**，超时平台就不让改了；
          · 只适用于模板卡片点击事件。
        ⇒ 因此**超时没人点的卡片是改不掉的**（没有点击就没有 req_id），
          只有"点了"的那张能就地改成「已允许 / 已拒绝」。
        """
        if not req_id:
            return {"sent": False, "errcode": None, "errmsg": "no req_id"}
        frame = {
            "cmd": "aibot_respond_update_msg",
            "headers": {"req_id": req_id},
            "body": {"response_type": "update_template_card", "template_card": card},
        }
        return self._send_wait(frame, f"update_card[{card.get('card_type')}]")


    def _post_reply(self, response_url, content):
        r = requests.post(response_url, json={
            "msgtype": "markdown", "markdown": {"content": content}}, timeout=6)
        log.info("[reply] -> %s %s", r.status_code, r.text[:120])
        return r.status_code == 200


    def reply(self, response_url, content):
        """被动回复。**一次性、有时效** —— 只用来回"收到/忙碌"。

        ⚠️ **必须异步**：这个方法是在 **websocket 读线程**上调用的（`_handle_callback` /
        `_handle_control`），而 HTTP POST 最长要等 6 秒。同步做的话，连发消息时读线程会被
        一条条卡住：期间收不到任何帧 ⇒ `_last_rx` 不前进 ⇒ 看门狗会把**健康的连接**判死。
        队列满就丢（这类回执本来就是"知道了"性质的，丢了不影响正确性）。
        """
        if not response_url:
            return False
        if self._reply_q.full():
            log.warning("[reply] 回复队列已满，丢弃一条：%r", content[:20])
            return False
        self._note_side()
        self._reply_q.put_nowait((response_url, content))
        return True


    def _reply_worker(self):
        while not self.stop:
            try:
                url, content = self._reply_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._post_reply(url, content)
            except Exception as e:
                log.warning("[reply] 失败: %s", e)
            finally:
                self._reply_q.task_done()


    def send_chunks(self, chatid, chat_type, text):
        """**先整条发**；只有被平台以 `40058` 拒收，才切开重发。

        为什么不预切：平台的上限按**字节**算（实测 20480），不是按条数。
        3000 字的中文回答 ≈ 9 KB，本来一条就读得完 —— 预先切片会把它硬拆成
        `(1/2)` `(2/2)`，既碎又多一次失败机会（用户 2026-10-27 认可这条策略）。
        ⇒ `CHUNK_BYTES` 的语义现在是**切分粒度**（真的超长时每片多大），不是"超过就切"。

        返回未送达列表 `[(label, {errcode, errmsg}), …]`，空列表 = 全部送达。
        """
        text = text or ""
        if not text:
            return []
        nbytes = len(text.encode("utf-8"))

        # ① 整条先发（绝大多数回答走到这里就结束了）
        r = self.send_markdown(chatid, chat_type, text)
        if r["sent"]:
            return []

        if r["errcode"] is None:
            # 没收到回执 = **结果未知**，不是"失败"。按本项目自己的约定（超时 ≠ 失败），
            # 不能让用户以为没发出去而重发 —— 重发会让 Claude 把刚才那件事再做一遍。
            log.error("[send] 没拿到平台回执（%s），推送结果未知", (r["errmsg"] or "")[:80])
            self.send_markdown(
                chatid, chat_type,
                "❔ **推送结果未知**：没收到平台回执。\n"
                "> 它可能已经发出去了，也可能没有。**先翻一下有没有**，别急着重发 —— "
                "重发会让它把刚才那件事再做一遍。")
            return [(0, r)]

        if r["errcode"] != 40058:
            # 不是"太长"，切片重发没有意义 —— 直接如实告诉用户
            log.error("[send] 整条发送失败 %dB errcode=%s %s",
                      nbytes, r["errcode"], (r["errmsg"] or "")[:160])
            self.send_markdown(
                chatid, chat_type,
                f"⚠️ **结果没能送达**（errcode=`{r['errcode']}`，{nbytes} 字节）。\n"
                f"> 平台明确拒收了，内容没发出去。看桥的日志。")
            return [(0, r)]

        # ② 被 40058 拒 ⇒ 按切分粒度切开，逐片重发（每片仍等回执）
        parts = chunk_md(text, self.chunk_bytes)
        log.warning("[send] 整条 %dB 被 40058 拒收，切成 %d 片重发", nbytes, len(parts))
        return self._send_parts(chatid, chat_type, parts)


    def _send_parts(self, chatid, chat_type, parts, first_idx=1):
        """逐片主动推送（带 `(i/n)` 前缀 + 失败汇总）。

        `first_idx` 让流式那条路可以从第 2 片开始发（第 1 片已经进了气泡）。
        """
        n = len(parts)
        failed = []
        for i in range(first_idx, n + 1):
            p = parts[i - 1]
            body = f"({i}/{n})\n{p}" if n > 1 else p
            failed.extend(self._send_piece(chatid, chat_type, body, i, n))
            if i < n:
                time.sleep(0.4)
        if failed:
            label, rf = failed[0]
            log.error("[send] 有 %d 段未送达，首段 %s/%d errcode=%s",
                      len(failed), label, n, rf["errcode"])
            self.send_markdown(
                chatid, chat_type,
                f"⚠️ **有 {len(failed)} 段内容没送达**（首段：第 {label}/{n} 段，"
                f"errcode=`{rf['errcode']}`）。\n> 上面的回答可能不完整，别把它当全文。")
        return failed


    def _send_piece(self, chatid, chat_type, body, idx, n, depth=0):
        """发一片；**仍**被 40058 拒就按字节对半再发（最多 3 层）。

        正常情况走不到这里 —— 只有"平台实际上限比我们以为的更小"才会触发
        （比如将来平台把 20480 改小、或加前缀后刚好越界）。
        这里按**实际字节**对半，不按 `CHUNK_BYTES` 再切：这一片本来就 ≤ 粒度，
        再按粒度切等于没切。
        """
        r = self.send_markdown(chatid, chat_type, body)
        if r["sent"]:
            return []
        if r["errcode"] == 40058 and depth < 3:
            halves = split_half(body)
            if len(halves) == 2:
                log.warning("[send] 第 %d/%d 片仍超长（%dB），对半重发",
                            idx, n, len(body.encode("utf-8")))
                out = []
                for k, h in enumerate(halves, 1):
                    out.extend(self._send_piece(
                        chatid, chat_type, f"({idx}/{n} 续{k})\n{h}", idx, n, depth + 1))
                    time.sleep(0.3)
                return out
            log.error("[send] 第 %d/%d 片仍超长但已切不动（%dB）",
                      idx, n, len(body.encode("utf-8")))
        return [(str(idx), r)]


    def _deliver(self, st, chatid, chat_type, text):
        """把最终文本落到**同一个气泡**里；太长 / 窗口用尽时退回主动推送。

        用户 2026-10-28 定的效果：ack 和正文是**一个气泡**，正文直接覆盖 ack。
        退化路径的硬要求是**不丢内容**：
          · 正文 > 20480 字节 ⇒ 切片，第 1 片顶替气泡，其余照旧推送（编号接续）；
          · 气泡已关闭 / 超过 10 分钟窗口 ⇒ 整条改走 `send_chunks`（另发一条）；
          · 平台明确拒收（非 40058）⇒ 同上；**结果未知 ⇒ 不重发**（沿用老约定）。
        """
        text = text or ""
        if not text:
            return []
        nbytes = len(text.encode("utf-8"))

        # ① 整条塞进气泡（绝大多数回答走到这里就结束了：一个气泡、零重复）
        r = st.send(text, finish=True)
        if r["sent"]:
            return []
        if r.get("dead"):
            log.warning("[deliver] 气泡不可用（%s）⇒ 正文改走主动推送（另发一条）",
                        r["errmsg"])
            return self.send_chunks(chatid, chat_type, text)
        if r["errcode"] is None:
            log.error("[deliver] 定稿帧没拿到回执，推送结果未知")
            self.send_markdown(
                chatid, chat_type,
                "❔ **推送结果未知**：没收到平台回执。\n"
                "> 它可能已经发出去了，也可能没有。**先翻一下有没有**，别急着重发 —— "
                "重发会让它把刚才那件事再做一遍。")
            return [(0, r)]
        if r["errcode"] != 40058:
            log.error("[deliver] 定稿被拒 errcode=%s ⇒ 整条改走主动推送", r["errcode"])
            return self.send_chunks(chatid, chat_type, text)

        # ② 超长：第 1 片顶替气泡，其余主动推送（顺序与编号和以前完全一致）
        parts = chunk_md(text, self.chunk_bytes)
        n = len(parts)
        log.warning("[deliver] 正文 %dB 超过上限，切成 %d 片（第 1 片进气泡）", nbytes, n)
        r1 = st.send(f"(1/{n})\n{parts[0]}", finish=True)
        if r1["sent"]:
            return self._send_parts(chatid, chat_type, parts, first_idx=2)
        if r1.get("dead") or r1["errcode"] is not None:
            # 第 1 片明确没进气泡 ⇒ 整条推送兜底（一条都没发出去，不会重复）
            log.warning("[deliver] 第 1 片没进气泡（%s）⇒ 整条改走主动推送",
                        r1.get("errmsg") or r1["errcode"])
            return self.send_chunks(chatid, chat_type, text)
        # 第 1 片结果未知 ⇒ 绝不重发它，只补后面的，并把话说清楚
        log.error("[deliver] 第 1 片结果未知，只补第 2 片之后")
        self.send_markdown(chatid, chat_type,
                           f"❔ 第 1/{n} 段推送结果未知（可能已发出，也可能没有）。")
        return self._send_parts(chatid, chat_type, parts, first_idx=2)

