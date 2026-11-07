"""飞书对 `core/contract.py` 的答案：把内核的 `plat_*` 落到具体方法上。

**这张表就是「换平台要重写什么」的清单** —— 与企微那张逐条对应，
所以两边对着看就知道"哪些是平台差异、哪些是内核语义"。
"""

from platforms.feishu.outbound import FeishuBubble


class FeishuPlatformMixin:
    """12 个平台适配点的飞书实现。"""

    # ① 建连与重连 → 官方 SDK 的长连接（transport）
    def plat_run(self):
        return self.feishu_run_forever()

    def plat_stop(self):
        return self.feishu_stop()

    # ③ 发消息 + 更新同一条
    def plat_send_text(self, chatid, chat_type, text, wait=True):
        return self.send_text(chatid, chat_type, text, wait=wait)

    def plat_send_card(self, chatid, chat_type, card, wait=True):
        return self.send_card(chatid, chat_type, card, wait=wait)

    def plat_update_card(self, reply_token, card):
        return self.send_update_card(reply_token, card)

    # ④ 交互卡片
    def plat_status_card(self, chatid, chat_type, userid):
        return self._status_card(chatid, chat_type, userid)

    def plat_approval_card(self, req):
        return self._approval_card(req)

    def plat_approval_update_card(self, reply_token, req, allow, userid):
        return self._approval_update_card(reply_token, req, allow, userid)

    # ② 一个气泡装下 ack 与正文（飞书是 CardKit 流式卡片）
    #    ⚠️ 契约允许返回 None ⇒ 内核退回 `plat_send_chunks`。
    #    这里只有"没有 message_id"时才返回 None（连建卡都不知道回给谁）。
    def plat_open_bubble(self, reply_token, chatid, chat_type):
        if not reply_token:
            return None
        return FeishuBubble(self, reply_token, chatid, chat_type)

    def plat_deliver(self, bubble, chatid, chat_type, text):
        return self._deliver(bubble, chatid, chat_type, text)

    # ⑤ 被动回复（飞书没有 response_url，token 就是 message_id）
    def plat_reply(self, token, content):
        return self.reply(token, content)

    # ⑥ 分片与错误码
    def plat_send_chunks(self, chatid, chat_type, text):
        return self.send_chunks(chatid, chat_type, text)