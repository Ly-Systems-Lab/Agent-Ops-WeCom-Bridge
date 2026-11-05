"""企微协议编解码：把平台 JSON 翻译成**规范化事件**，再交给内核。

本文件是「平台细节」与「业务判据」的分界线：
  · 在这里做的：字段名、单聊无 chatid 的兜底、@ 语法剥离、去重键位置、卡片 event_key。
  · 交给内核的：白名单、去重判据、忙碌丢弃、指令分派、审批动作。
⇒ 换成飞书时，只需要换掉这一层，内核一行不动。
"""

import json
import os
import re
import threading

from common import ID_FIELDS, LOGDIR, log

_AT_LEAD = re.compile(r"^\s*(?:@\S+[ \t]*)+")


def strip_at(text, bot_name=""):
    """去掉正文开头的 @机器人。

    企微下发的 text.content **带着 @ 前缀**。实测（2026-10-27）：机器人配名 "CC BOT"，
    但正文是 **"@CC 你好"** —— 平台只写"@ + 第一个词"。所以两条路都要走：
      ① 名字完整匹配；② 兜底剥 @token 后，再把名字剩下的部分也剥掉。
    """
    if not text:
        return text, ""
    s = text.lstrip()
    if bot_name and s.startswith("@" + bot_name):
        cut = len("@" + bot_name)
        return s[cut:].lstrip(), s[:cut]
    m = _AT_LEAD.match(s)
    if not m or not m.group(0).strip():
        return text, ""
    prefix, rest = m.group(0), s[m.end():]
    if bot_name:
        tail = " ".join(bot_name.split()[1:])
        if tail and rest.startswith(tail):
            prefix += rest[:len(tail)]
            rest = rest[len(tail):].lstrip()
    return rest, prefix


def find_msg_id(msg):
    body = msg.get("body") or {}
    for container, label in ((body, "body"), (msg.get("headers") or {}, "headers"),
                             (msg, "root")):
        for f in ID_FIELDS:
            v = container.get(f)
            if v:
                return f"{label}.{f}", str(v)
    return None, None


class WeComCodecMixin:
    """收消息 / 收卡片事件：解析成规范化 dict，然后交给内核。

    ⚠️ 这里**只做翻译**：字段名、单聊无 chatid 的兜底、@ 语法、去重键位置。
    白名单 / 去重判据 / 忙碌丢弃 / 指令分派都在 `CoreMixin` 里 —— 换平台时只换本文件。
    """

    def _handle_callback(self, msg):
        body = msg.get("body") or {}
        from_ = body.get("from") or {}
        userid = from_.get("userid") or ""
        raw_chatid = body.get("chatid") or ""
        chattype = body.get("chattype") or "unknown"
        is_single = chattype == "single"
        chat_type = 1 if is_single else 2
        # 🚨 **单聊回调里没有 `chatid`**（实测 2026-10-27 20:48：日志 `chat= type=single`）
        # ⇒ 用 `from.userid` 当推送目标。不兜底的话：模型跑完了、`send_markdown` 却
        # 「无 chatid，跳过推送」，用户在手机上永远停在"收到，处理中…"。
        # 单聊因此天然拥有**独立会话**（session 键是 (chatid, userid)，
        # 群聊是 (群 chatid, userid)，两者不会串）。
        chatid = raw_chatid or userid
        if not raw_chatid and not is_single:
            # chattype 缺失/改名的兜底：**宁可发到私聊，也别把结果丢掉**
            chat_type = 1
            log.warning("[msg] chattype=%r 且没有 chatid ⇒ 按单聊发给 userid", chattype)
        response_url = body.get("response_url") or ""
        raw_text = (body.get("text") or {}).get("content", "") or ""

        if body.get("aibotid") and self.bot_id and body["aibotid"] != self.bot_id:
            log.warning("[msg] aibotid 不匹配: %s != %s", body["aibotid"], self.bot_id)

        if not self._shape_logged:
            self._shape_logged = True
            log.info("[shape] body keys=%s  from keys=%s  text keys=%s",
                     sorted(body.keys()), sorted(from_.keys()),
                     sorted((body.get("text") or {}).keys()))
            f, v = find_msg_id(msg)
            log.info("[shape] 去重键 %s = %s", f, v)
        if self.dump_raw:
            self._dump_raw(msg)

        text, at_prefix = strip_at(raw_text, self.bot_name)
        f, key = find_msg_id(msg)
        self.core_handle_incoming({
            "userid": userid, "chatid": chatid, "chat_type": chat_type,
            "raw_chatid": raw_chatid, "chattype": chattype, "is_single": is_single,
            "response_url": response_url, "text": text, "at_prefix": at_prefix,
            "msgtype": (body.get("msgtype") or "text"),
            "dedupe_field": f, "dedupe_key": key or "",
            "reply_token": (msg.get("headers") or {}).get("req_id") or "",
        })

    def _handle_card_event(self, msg):
        """处理卡片按钮点击。跑在旁路线程里（见 `_on_message` 的分派）。"""
        body = msg.get("body") or {}
        tce = ((body.get("event") or {}).get("template_card_event") or {})
        userid = (body.get("from") or {}).get("userid") or ""
        raw_chatid = body.get("chatid") or ""
        chat_type = 1 if (body.get("chattype") or "") == "single" else 2
        self.core_handle_card_action({
            "key": tce.get("event_key") or "",
            "userid": userid,
            "chatid": raw_chatid or userid,      # 单聊没有 chatid，与消息回调同一套兜底
            "chat_type": chat_type,
            "raw_chatid": raw_chatid,
            "task_id": tce.get("task_id") or "",
            "reply_token": (msg.get("headers") or {}).get("req_id") or "",
        })

    def _dump_raw(self, msg):
        os.makedirs(LOGDIR, exist_ok=True)
        with open(os.path.join(LOGDIR, "raw-callback.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        log.info("[raw] 原始回调已追加")

    # -- worker：单线程串行 --

