"""飞书协议编解码：把飞书事件翻译成**规范化事件**，再交给内核。

本文件是「平台细节」与「业务判据」的分界线（与企微 `codec.py` 同一条原则）：
  · 在这里做的：字段名、`@_user_N` 占位符、去重键位置、单聊的 chatid 口径、
    卡片按钮 value 的拆解；
  · 交给内核的：白名单、去重判据、忙碌丢弃、指令分派、审批动作。

⚠️ 两条线程模型上的硬约束（源码在 `lark_oapi/ws/client.py::_handle_data_frame`）：

  ① **消息事件**跑在事件循环线程上，回调一阻塞整条连接就卡死
     ⇒ 这里只解析 + 入队，处理在 `feishu-inbound` 线程（`_inbound_worker`）。
  ② **卡片事件**必须**同步**返回响应对象（3c 实测：新卡片只能这么交出去）
     ⇒ 卡片这条**不走队列**，就在回调线程里跑完内核那一小段并返回。
"""

import json
import queue
import threading

from common import log

# 群聊 `@` 在正文里是这种占位符，映射在 `message.mentions[].key`
AT_PLACEHOLDER_HEAD = "@_user_"


def strip_mentions(text, mentions):
    """把正文里的 `@_user_N` 占位符剥掉，同时把被剥掉的原文当 `at_prefix` 返回。

    ⚠️ **不能照抄企微的 `<@userid>` 正则**：飞书下发的是占位符，
    企微那条正则在飞书正文里**一个字符都匹配不上**，而且**不会报错** ——
    正文会带着 `@_user_1` 一起喂给 Agent（3a 实测踩到）。
    """
    if not text:
        return text, ""
    out = text
    prefix = ""
    for m in mentions or []:
        key = getattr(m, "key", "") or ""
        if not key:
            continue
        name = getattr(m, "name", "") or ""
        if key in out:
            # 记录被剥掉的部分（给日志看"他到底 @ 了谁"），但正文里必须清干净
            prefix += f"{key}({name}) " if name else f"{key} "
            out = out.replace(key, "")
    # 占位符被替换后常见的残留：开头多出来的空格
    return out.lstrip(), prefix.strip()


class FeishuCodecMixin:
    """收消息 / 收卡片事件：解析成规范化 dict，然后交给内核。"""

    # -- 消息事件（事件循环线程：只入队）--

    def _feishu_on_message(self, data):
        """**只解析 + 入队**。这里绝不做任何会阻塞的事（见模块头 ①）。"""
        try:
            ev = data.event
            item = self._feishu_normalize(ev)
        except Exception as e:
            log.warning("[feishu] 消息解析异常：%s: %s", type(e).__name__, e)
            return
        if item is None:
            return
        try:
            self._in_q.put_nowait(item)
        except queue.Full:
            # 队列满 = 内核那头已经跟不上（正常情况下一分钟也不会有几条）。
            # 丢一条并**喊出来**，别静默 —— 静默丢消息是最难查的那类。
            log.error("[feishu] 入站队列已满（%d），丢弃一条消息 mid=%s",
                      self._in_qmax, item.get("dedupe_key", "")[:12])

    def _feishu_normalize(self, ev):
        m = getattr(ev, "message", None)
        s = getattr(ev, "sender", None)
        if m is None or s is None:
            return None

        sender_type = getattr(s, "sender_type", "") or ""
        if sender_type == "app":
            # 不回机器人自己发的消息，否则自激循环
            return None

        open_id = ""
        try:
            open_id = s.sender_id.open_id or ""
        except Exception:
            pass

        chat_id = getattr(m, "chat_id", "") or ""
        raw_type = getattr(m, "chat_type", "") or ""      # p2p / group
        is_single = (raw_type == "p2p")
        chat_type = 1 if is_single else 2

        msg_type = getattr(m, "message_type", "") or ""
        mid = getattr(m, "message_id", "") or ""
        mentions = getattr(m, "mentions", None) or []
        text = ""
        if msg_type == "text":
            try:
                text = json.loads(m.content or "{}").get("text", "")
            except Exception:
                text = ""

        # 群聊只认 @ 我的消息。本应用申请的是 `im:message.group_at_msg`（协议级隔离），
        # 正常收不到没 @ 的 —— 这条检查是**应用层的第二道**，防的是将来权限被放宽。
        if not is_single and not mentions:
            log.info("[feishu] 群聊里没 @ 我，按适配点 ④ 不回")
            return None

        text, at_prefix = strip_mentions(text, mentions)

        if not self._shape_logged:
            self._shape_logged = True
            log.info("[feishu][shape] chat_type=%r msg_type=%r sender_type=%r "
                     "mentions=%d 去重键=message_id",
                     raw_type, msg_type, sender_type, len(mentions))
            log.info("[feishu][shape] 单聊 chatid 口径 = open_id（与企微'单聊用 userid 兜底'同构）")

        # ⚠️ **单聊的 chatid 用 open_id**，理由与企微那条一模一样：
        #   ① 推送时 `receive_id_type=open_id` 在单聊里就是发给这个人；
        #   ② 会话键变成 (open_id, open_id) ⇒ `Store.last_chat()` 能认出单聊(1)，
        #      否则它会按"chat_id != user_id"一律判成群聊，自愈通知就发错类型了。
        # `raw_chatid` 故意留空：群白名单只该对**真实的群**生效，
        # 拿单聊的 open_id 去比群白名单只会误杀（企微那边同样的取舍）。
        chatid = open_id if is_single else chat_id
        raw_chatid = "" if is_single else chat_id

        return {
            "userid": open_id,
            "chatid": chatid,
            "chat_type": chat_type,
            "raw_chatid": raw_chatid,
            "chattype": "single" if is_single else "group",
            "is_single": is_single,
            # 飞书没有 `response_url` 这种一次性回复通道；
            # 这里放 `message_id` —— 内核会原样传给 `plat_reply`，按"回复这条消息"实现。
            "response_url": mid,
            "text": text,
            "at_prefix": at_prefix,
            "msgtype": msg_type or "text",
            "dedupe_field": "message_id",
            "dedupe_key": mid,
            # 气泡用 message_id 建（回在那条消息下面）；也为 None 时的退化路径服务
            "reply_token": mid,
        }

    def _inbound_worker(self):
        """把规范化消息逐条交给内核。

        **单线程串行**是刻意的：事件的到达顺序 = 用户的操作顺序，
        并发交给内核会让"忙碌即丢弃"的判据出现竞态。
        """
        while not self.stop:
            try:
                inc = self._in_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self.core_handle_incoming(inc)
            except Exception as e:
                log.exception("[feishu] 处理消息异常：%s", e)
            finally:
                self._in_q.task_done()

    # -- 卡片事件（事件循环线程：必须同步返回）--

    def _feishu_on_card_action(self, data):
        """卡片按钮被点。**必须在这个调用里返回响应**（3c 实测的唯一通道）。

        响应里能带两样东西：
          · `toast` —— 右上角的小提示，纯反馈，不改变消息；
          · `card`  —— **就地替换那张卡**（`type="raw"` + 新卡 JSON），按钮就此消失。
        审批靠的是后者：按钮不撤，同一条卡还能被再点一次。
        """
        from lark_oapi.event.callback.model.p2_card_action_trigger import (
            P2CardActionTriggerResponse,
        )

        resp = P2CardActionTriggerResponse()
        try:
            ev = getattr(data, "event", None)
            act = getattr(ev, "action", None)
            value = getattr(act, "value", None) or {}
            op = getattr(ev, "operator", None)
            ctx = getattr(ev, "context", None)
            open_id = getattr(op, "open_id", "") or ""
            open_chat = getattr(ctx, "open_chat_id", "") or ""
            open_msg = getattr(ctx, "open_message_id", "") or ""
            token = getattr(ev, "token", "") or ""

            # 按钮 value 是**我们自己**渲染卡片时塞进去的（见 render.py）：
            # `{key, task_id, chatid, chat_type}`。回调里原样回来（3c 已验）。
            chatid = value.get("chatid") or open_chat
            try:
                chat_type = int(value.get("chat_type") or 2)
            except (TypeError, ValueError):
                chat_type = 2

            log.info("[feishu][card] key=%r task=%r chat=%s type=%s 操作人=%s",
                     value.get("key"), value.get("task_id"), chatid, chat_type,
                     (open_id[:6] + "…") if open_id else "?")

            # 交给内核；它若决定改卡，会调 `plat_approval_update_card`，
            # 那条路把新卡写进**本次回调上下文**（见下面两行）。
            self._card_ctx.resp = resp
            self._card_ctx.msg_id = open_msg
            self._card_ctx.token = token
            try:
                self.core_handle_card_action({
                    "key": value.get("key") or "",
                    "userid": open_id,
                    "chatid": chatid,
                    "chat_type": chat_type,
                    "raw_chatid": "" if chat_type == 1 else chatid,
                    "task_id": value.get("task_id") or "",
                    # 飞书的"更新本卡"不需要令牌，真正的通道就是这次返回值；
                    # 这里放消息 id，只为日志里能对上"改的是哪张卡"。
                    "reply_token": open_msg,
                })
            finally:
                self._card_ctx.resp = None
                self._card_ctx.msg_id = ""
                self._card_ctx.token = ""
        except Exception as e:
            log.warning("[feishu][card] 回调处理异常：%s: %s", type(e).__name__, e)
        return resp

    def _feishu_set_toast(self, kind, content):
        """在**当前卡片回调**里挂一个 toast。不在回调上下文里就静默跳过。"""
        ctx = getattr(self, "_card_ctx", None)
        resp = getattr(ctx, "resp", None) if ctx is not None else None
        if resp is None:
            return False
        try:
            from lark_oapi.event.callback.model.p2_card_action_trigger import CallBackToast
            t = CallBackToast()
            t.type = kind
            t.content = content
            resp.toast = t
            return True
        except Exception as e:
            log.warning("[feishu][card] 设置 toast 失败：%s", e)
            return False