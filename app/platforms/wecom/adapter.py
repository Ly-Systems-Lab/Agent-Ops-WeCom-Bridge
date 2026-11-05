"""企微对 `core/contract.py` 的答案：把内核的 `plat_*` 落到具体方法上。

**这张表就是「换平台要重写什么」的清单。**
"""

from platforms.wecom.outbound import Stream


class WeComPlatformMixin:
    """8 个平台适配点的企业微信实现。"""

    # ① 建连与重连 → run_forever / stop_bot（transport）
    def plat_run(self):
        return self.run_forever()

    def plat_stop(self):
        return self.stop_bot()

    # ③ 发消息 + 更新同一条
    def plat_send_text(self, chatid, chat_type, text, wait=True):
        return self.send_markdown(chatid, chat_type, text, wait=wait)

    def plat_send_card(self, chatid, chat_type, card, wait=True):
        return self.send_card(chatid, chat_type, card, wait=wait)

    def plat_update_card(self, reply_token, card):
        return self.send_update_card(reply_token, card)

    # ④ 交互卡片（模板 + 规格限制）
    def plat_status_card(self, chatid, chat_type, userid):
        return self._status_card(chatid, chat_type, userid)

    def plat_approval_card(self, req):
        return self._approval_card(req)

    def plat_approval_update_card(self, reply_token, req, allow, userid):
        return self._approval_update_card(reply_token, req, allow, userid)

    # ③' 一个气泡装下 ack 与正文（没有 reply_token 就没有气泡）
    def plat_open_bubble(self, reply_token, chatid, chat_type):
        if not reply_token:
            return None
        return Stream(self, reply_token, chatid, chat_type)

    def plat_deliver(self, bubble, chatid, chat_type, text):
        return self._deliver(bubble, chatid, chat_type, text)

    # ⑤ 被动回复（一次性、有时效）
    def plat_reply(self, response_url, content):
        return self.reply(response_url, content)

    # ⑥ 分片与错误码
    def plat_send_chunks(self, chatid, chat_type, text):
        return self.send_chunks(chatid, chat_type, text)
