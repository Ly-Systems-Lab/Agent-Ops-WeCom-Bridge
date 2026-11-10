"""飞书卡片渲染：Card 2.0 的形状、按钮、回调 value 的约定。

规格限制**只在这里**：飞书按钮文字实测 ≥12 个汉字都不截断（是企微 2 个字的 6 倍），
所以不要再按企微那套写两字短标签；反过来，**卡片的回调 value 是我们自己定的约定**
（`{key, task_id, chatid, chat_type}`），改这里就必须同步改 `codec.py` 的拆解。

为什么 value 里要塞 `chatid` / `chat_type`：
飞书的卡片回调**只给 `context.open_chat_id`、不给会话类型**（3c 实测），
而内核要用 `chat_type` 决定工作区路由（单聊/群聊）与推送地址。
卡片是我们自己发出去的，把这两个值随按钮带回去是最省事、也无歧义的做法。
"""

import json
import time

from common import log
from core.runner import _clip, _short_path, tool_desc


class FeishuRenderMixin:
    """把「要给人看的东西」渲染成飞书 Card 2.0。"""

    def _status_card(self, chatid, chat_type, userid):
        pairs = self._status_pairs(chatid, chat_type, userid)
        lines = "\n".join(f"- **{k}**：{v}" for k, v in pairs)
        base = {"chatid": chatid, "chat_type": int(chat_type)}
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "body": {"elements": [
                {"tag": "markdown",
                 "content": f"**{self.bot_name} · 状态**\n\n{lines}"},
                {"tag": "markdown",
                 "content": "_点按钮直接操作；「清空」= 开新会话，下一轮不记得之前的内容。_"},
                self._button("刷新", "default", {**base, "key": "status_refresh",
                                                "task_id": ""}),
                self._button("清空", "default", {**base, "key": "status_clear",
                                                "task_id": ""}),
                self._button("停止", "danger", {**base, "key": "status_stop",
                                               "task_id": ""}),
            ]},
        }

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
        # 跨平台命令的执行身份（dws → 钉钉 / lark-cli → 飞书）。没有就不加这一行。
        ident = (f"- **身份**：{req['exec_identity']}\n" if req.get("exec_identity") else "")
        # 为什么弹这张卡（写操作 / 表外按写 / 会读出密钥 …）。闸门算的，见 approval_hook。
        policy = (f"- **策略**：{req['policy']}\n" if req.get("policy") else "")
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "body": {"elements": [
                {"tag": "markdown",
                 "content": (f"**需要你确认**\n\n"
                             f"- **工具**：`{tool}`\n"
                             f"{ident}"
                             f"{policy}"
                             f"- **内容**：{self._approval_detail(tool, req.get('tool_input'))}\n"
                             f"- **会话**：{_short_path(req.get('cwd') or '', 60) or '—'}")},
                {"tag": "markdown",
                 "content": f"_点「允许」才会执行；{wait} 秒内不点按拒绝处理。_"},
                self._button("允许", "primary", {**base, "key": "approve_ok"}),
                self._button("拒绝", "danger", {**base, "key": "approve_no"}),
            ]},
        }

    def _approval_resolved_card(self, req, allow, userid):
        """点完之后那张卡的样子：**没有按钮** —— 留着按钮就等于留了个点了没用的入口。"""
        tool = req.get("tool_name") or "?"
        return {
            "schema": "2.0",
            "config": {"update_multi": True},
            "body": {"elements": [
                {"tag": "markdown",
                 "content": (f"**{'✅ 已允许' if allow else '🚫 已拒绝'}**\n\n"
                             f"- **工具**：`{tool}`\n"
                             f"- **内容**：{self._approval_detail(tool, req.get('tool_input'))}\n"
                             f"- **处理**：{self._user_label(userid)} · "
                             f"{time.strftime('%H:%M:%S')}")},
                {"tag": "markdown",
                 "content": ("_这次调用已经执行。_" if allow else "_这次调用没有执行。_")},
            ]},
        }

    def _approval_update_card(self, reply_token, req, allow, userid):
        """就地改卡。成功返回 True；失败返回 False，让内核退回一条文字确认。"""
        card = self._approval_resolved_card(req, allow, userid)

        # toast 是"点完立刻有反馈"的那一半：改卡本身要等平台渲染，
        # 而 toast 在点击处马上弹出来。两条一起给，手感才不悬空。
        self._feishu_set_toast("success" if allow else "info",
                               "已允许，继续执行" if allow else "已拒绝，这条不会执行")

        r = self.send_update_card(reply_token, card)
        if r.get("sent"):
            log.info("[feishu][approval] 卡片已就地更新为「%s」",
                     "已允许" if allow else "已拒绝")
            return True
        log.warning("[feishu][approval] 更新卡片失败：%s",
                    r.get("errmsg") or r.get("errcode"))
        return False

    # -- 小工具 --

    @staticmethod
    def _button(text, kind, value):
        return {
            "tag": "button",
            "text": {"tag": "plain_text", "content": text},
            "type": kind,
            "behaviors": [{"type": "callback", "value": value}],
        }

    def _user_label(self, userid):
        """卡片上给人看的名字。**没配映射一律脱敏**，绝不把 open_id 摆到群里。

        与企微同一个理由：open_id 是账号标识，不是昵称，而卡片在群里所有人都看得见。
        想显示 "CEO" 就在 `.env` 里配 `FEISHU_USER_LABELS=ou_xxx=CEO`。
        """
        uid = (userid or "").strip()
        if not uid:
            return "?"
        mapped = self.user_labels.get(uid)
        if mapped:
            return mapped
        return (uid[:6] + "***") if len(uid) > 6 else "*"