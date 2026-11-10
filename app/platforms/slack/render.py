"""Slack 卡片渲染：Block Kit 的形状、按钮、回调 value 的约定。

规格限制**只在这里**：
  · 按钮 `text` 官方上限 **75 字符**，但文档同时写 "may truncate with ~30 characters"
    ⇒ 实际约 30 字符就开始截（中文约 30 字）。比企微的 2 个汉字宽，比飞书的 12 个窄。
  · `section` 的 mrkdwn 上限约 3000 字符；一张卡最多 50 个 block。
  · 按钮 `value` 上限 2000 字符 —— 我们塞的是 `{key, task_id, chatid, chat_type}`。

⚠️ **卡片的回调 value 是我们自己定的约定**（`{key, task_id, chatid, chat_type}`），
改这里就必须同步改 `codec.py` 的拆解，两边是一对。

⚠️ Slack 的 mrkdwn 是 `*粗体*`，**不是** Markdown 的 `**粗体**`。
写成 `**x**` 会原样显示成星号 —— 所以这里一律用单星号，别照抄飞书那份。
"""

import json
import time

from common import log
from core.runner import _clip, _short_path, tool_desc


class SlackRenderMixin:
    """把「要给人看的东西」渲染成 Block Kit。"""

    # -- 状态卡 --

    def _status_card(self, chatid, chat_type, userid):
        pairs = self._status_pairs(chatid, chat_type, userid)
        lines = "\n".join("*%s*：%s" % (k, v) for k, v in pairs)
        base = {"chatid": chatid, "chat_type": int(chat_type)}
        return [
            {"type": "header",
             "text": {"type": "plain_text",
                      "text": "%s · 状态" % (self.bot_name or "CC BOT")}},
            {"type": "section",
             "text": {"type": "mrkdwn", "text": lines or "（无）"}},
            {"type": "context",
             "elements": [{"type": "mrkdwn",
                           "text": "_点按钮直接操作；「清空」= 开新会话，"
                                   "下一轮不记得之前的内容。_"}]},
            {"type": "actions", "elements": [
                self._button("刷新", {**base, "key": "status_refresh", "task_id": ""}),
                self._button("清空", {**base, "key": "status_clear", "task_id": ""}),
                self._button("停止", {**base, "key": "status_stop", "task_id": ""},
                             style="danger"),
            ]},
        ]

    # -- 审批卡 --

    def _approval_detail(self, tool, inp):
        inp = inp or {}
        if tool == "Bash":
            return _clip(str(inp.get("command") or ""), 300)
        if tool in ("Edit", "Write", "MultiEdit"):
            return _clip(str(inp.get("file_path") or ""), 300)
        if tool == "WebFetch":
            return _clip(str(inp.get("url") or ""), 300)
        try:
            return _clip(json.dumps(inp, ensure_ascii=False), 300)
        except Exception:
            return _clip(str(inp), 300)

    def _approval_target(self, req):
        """从请求里取 `chatid|chat_type|userid`（hook 写进来的目标会话）。"""
        target = str(req.get("target") or "").split("|")
        chatid = target[0] if target else ""
        try:
            chat_type = int(target[1] or 2) if len(target) > 1 else 2
        except (TypeError, ValueError):
            chat_type = 2
        return chatid, chat_type

    def _approval_card(self, req):
        tool = req.get("tool_name") or "?"
        rid = req.get("id") or ""
        chatid, chat_type = self._approval_target(req)
        base = {"chatid": chatid, "chat_type": chat_type, "task_id": rid}
        wait = int(self.approval_wait_sec or 120)
        detail = self._approval_detail(tool, req.get("tool_input"))
        # 跨平台命令的执行身份（dws → 钉钉 / lark-cli → 飞书）。没有就不加这一行。
        ident = (f"*身份*：{req['exec_identity']}\n" if req.get("exec_identity") else "")
        # 为什么弹这张卡（写操作 / 表外按写 / 会读出密钥 …）。闸门算的，见 approval_hook。
        policy = (f"*策略*：{req['policy']}\n" if req.get("policy") else "")
        return [
            {"type": "header",
             "text": {"type": "plain_text", "text": "需要你确认"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": ("*工具*：`%s`\n%s%s*内容*：%s\n*会话*：%s"
                      % (tool, ident, policy, detail,
                         _short_path(req.get("cwd") or "", 60) or "—"))}},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": "_点「允许」才会执行；%d 秒内不点按拒绝处理。_" % wait}]},
            {"type": "actions", "elements": [
                self._button("允许", {**base, "key": "approve_ok"}, style="primary"),
                self._button("拒绝", {**base, "key": "approve_no"}, style="danger"),
            ]},
        ]

    def _approval_resolved_card(self, req, allow, userid):
        """点完之后那张卡的样子：**没有按钮** —— 留着按钮就是个点了没用的入口。"""
        tool = req.get("tool_name") or "?"
        return [
            {"type": "header",
             "text": {"type": "plain_text",
                      "text": "✅ 已允许" if allow else "🚫 已拒绝"}},
            {"type": "section", "text": {"type": "mrkdwn",
             "text": ("*工具*：`%s`\n*内容*：%s\n*处理*：%s · %s"
                      % (tool, self._approval_detail(tool, req.get("tool_input")),
                         self._user_label(userid), time.strftime("%H:%M:%S")))}},
            {"type": "context", "elements": [{"type": "mrkdwn",
             "text": "_这次调用已经执行。_" if allow else "_这次调用没有执行。_"}]},
        ]

    def _approval_update_card(self, reply_token, req, allow, userid):
        """就地改卡。成功返回 True；失败返回 False，让内核退回一条文字确认。"""
        blocks = self._approval_resolved_card(req, allow, userid)
        r = self.update_card(reply_token, blocks)
        if r.get("sent"):
            log.info("[slack][approval] 卡片已就地更新为「%s」",
                     "已允许" if allow else "已拒绝")
            return True
        log.warning("[slack][approval] 更新卡片失败：%s",
                    r.get("errmsg") or r.get("errcode"))
        return False

    # -- 小工具 --

    @staticmethod
    def _button(text, value, style=None):
        el = {"type": "button",
              "text": {"type": "plain_text", "text": text},
              "action_id": str(value.get("key") or "act"),
              "value": json.dumps(value, ensure_ascii=False)}
        if style:
            el["style"] = style
        return el

    def _user_label(self, userid):
        """卡片上给人看的名字。**没配映射一律脱敏**，别把 member ID 摆到频道里。"""
        uid = (userid or "").strip()
        if not uid:
            return "?"
        mapped = self.user_labels.get(uid)
        if mapped:
            return mapped
        return (uid[:6] + "***") if len(uid) > 6 else "*"