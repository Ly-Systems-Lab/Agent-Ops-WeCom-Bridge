"""企微卡片渲染：模板卡片的形状、按钮文字上限、card_action 的硬要求。

规格限制（按钮只有 2 个汉字余量、text_notice 无按钮）**只在这里**，
换平台时整份重写，而不是在业务代码里改 if。
"""

import json
import time

from common import log
from core.runner import _clip, _short_path, tool_desc

# 平台硬限制（官方文档「模板卡片」）：`horizontal_content_list` **列表长度不超过 6**。
# 超了不是截断，是**整张卡被 `42035 Template_Card horizontal_content_list Invalid Size` 拒**
# —— 2026-11-12 实测：`/status` 加到第 7 行后，卡片再没发出去过，只有一行 WARNING。
# 所以这里统一兜底：超了先**喊一声**（哪些行没显示），再截。
_CARD_ROWS_MAX = 6


def _cap_rows(rows, what):
    """按平台上限截断 `horizontal_content_list`，并把丢掉的行打出来。"""
    if len(rows) > _CARD_ROWS_MAX:
        log.error("[card] %s 有 %d 行，超过平台上限 %d；多出的行不会显示：%s",
                  what, len(rows), _CARD_ROWS_MAX,
                  [r.get("keyname") for r in rows[_CARD_ROWS_MAX:]])
        return rows[:_CARD_ROWS_MAX]
    return rows


class WeComRenderMixin:
    """把「要给人看的东西」渲染成企微卡片。""" 

    def _status_card(self, chatid, chat_type, userid):
        """按钮卡。`card_action` 是**恒填**的：官方文档标"否"，实测不带它平台拒收 42045
        （2026-10-29，见 DECISIONS §21.9）。"""
        return {
            "card_type": "button_interaction",
            "source": {"desc": "Agent-Ops-WeCom-Bridge"},
            # 标题用**群里的机器人名**（`WECOM_BOT_NAME`），不硬编码项目名 ——
            # 卡片上写一个群里不存在的名字比不写更糟。
            "main_title": {"title": f"{self.bot_name} · 状态",
                           "desc": "企微 ⇄ 本机 Claude Code"},
            "horizontal_content_list": _cap_rows(
                [{"keyname": k, "value": v}
                 for k, v in self._status_pairs(chatid, chat_type, userid)], "/status"),
            # ⚠️ 按钮文字**只能是 2 个字**：实测 4 个字会被截断，
            # 手机上显示成「清...」、PC 上「清上...」（2026-10-29 用户实测）。
            # 语义放到 `sub_title_text` 里解释，别指望按钮放得下。
            "sub_title_text": "点按钮直接操作；「清空」= 开新会话，下一轮不记得之前的内容。",
            "button_list": [
                {"text": "刷新", "style": 1, "key": "status_refresh"},
                {"text": "清空", "style": 2, "key": "status_clear"},
                {"text": "停止", "style": 3, "key": "status_stop"},
            ],
            "task_id": "status-%d" % int(time.time() * 1000),
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        }


    def _approval_detail(self, tool, inp):
        """卡片正文里那一行"到底要干什么"。比 `tool_desc` 详细，但同样截断。"""
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


    def _approval_card(self, req):
        """一张"允许 / 拒绝"按钮卡。`card_action` 恒填（实测不带它 42045）。"""
        tool = req.get("tool_name") or "?"
        rid = req.get("id") or ""
        rows = [{"keyname": "工具", "value": tool}]
        # 跨平台命令的执行身份（dws → 钉钉 / lark-cli → 飞书）。没有就不加这一行。
        if req.get("exec_identity"):
            rows.append({"keyname": "身份", "value": str(req["exec_identity"])})
        # 为什么弹这张卡（写操作 / 表外按写 / 会读出密钥 …）。闸门算的，见 approval_hook。
        if req.get("policy"):
            rows.append({"keyname": "策略", "value": str(req["policy"])})
        rows += [
            {"keyname": "内容", "value": self._approval_detail(tool, req.get("tool_input"))},
            {"keyname": "会话", "value": _short_path(req.get("cwd") or "", 60) or "—"},
        ]
        return {
            "card_type": "button_interaction",
            "source": {"desc": "Agent-Ops-WeCom-Bridge"},
            "main_title": {"title": "需要你确认",
                           "desc": f"{tool_desc(tool, req.get('tool_input'))}"},
            "horizontal_content_list": _cap_rows(rows, "审批卡"),
            # 按钮文字**只能 2 个字**（实测），语义只能放这里
            "sub_title_text": f"点「允许」才会执行；{int(self.approval_wait_sec or 120)} 秒内不点按拒绝处理。",
            "button_list": [
                {"text": "允许", "style": 1, "key": "approve_ok"},
                {"text": "拒绝", "style": 3, "key": "approve_no"},
            ],
            "task_id": rid,
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        }


    def _approval_resolved_card(self, req, allow, userid="", kind="text_notice"):
        """点击之后把那张卡改成"已处理"。**没有按钮** —— 否则会留下一个点了没用的按钮。"""
        tool = req.get("tool_name") or "?"
        return {
            "card_type": kind,
            "source": {"desc": "Agent-Ops-WeCom-Bridge"},
            "main_title": {"title": "已允许" if allow else "已拒绝",
                           "desc": tool_desc(tool, req.get("tool_input"))},
            "horizontal_content_list": [
                {"keyname": "工具", "value": tool},
                {"keyname": "内容", "value": self._approval_detail(tool, req.get("tool_input"))},
                # ⚠️ 这里**不能**直接写 `userid`：那是账号 ID（形如 "zhangsan"），
                # 群里所有人都看得见这张卡，而改企微昵称不会改变它。
                {"keyname": "处理", "value": self._user_label(userid) + " · " +
                                             time.strftime("%H:%M:%S")},
            ],
            "sub_title_text": ("这次调用已经执行。" if allow else "这次调用没有执行。"),
            "task_id": req.get("id") or "",
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        } if kind == "text_notice" else {
            "card_type": "button_interaction",
            "source": {"desc": "Agent-Ops-WeCom-Bridge"},
            "main_title": {"title": "已允许" if allow else "已拒绝",
                           "desc": tool_desc(tool, req.get("tool_input"))},
            "horizontal_content_list": [
                {"keyname": "工具", "value": tool},
                {"keyname": "内容", "value": self._approval_detail(tool, req.get("tool_input"))},
            ],
            "sub_title_text": "这条已经处理过了。",
            # 平台若不允许把 button_interaction 换成无按钮的类型，就退成这一个
            # "已处理" 按钮；点它会被 `_handle_card_event` 静默忽略（不弹"已失效"）
            "button_list": [{"text": "已处理", "style": 2, "key": "approve_done"}],
            "task_id": req.get("id") or "",
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        }


    def _approval_update_card(self, req_id, req, allow, userid):
        """点击后 5 秒内把卡片改成已处理。失败返回 False，让调用方退回一条文字确认。"""
        # 先试"无按钮"的 text_notice（最干净的"已处理"形态），
        # 平台不认再退回仍带一个"已处理"按钮的 button_interaction。
        for kind in ("text_notice", "button_interaction"):
            card = self._approval_resolved_card(req, allow, userid, kind)
            r = self.send_update_card(req_id, card)
            if r.get("sent"):
                log.info("[approval] 卡片已就地更新为「%s」(kind=%s)",
                         card["main_title"]["title"], kind)
                return True
            log.warning("[approval] 更新卡片失败 kind=%s: errcode=%s %s",
                        kind, r.get("errcode"), r.get("errmsg"))
        return False


    def _user_label(self, userid):
        """卡片上给人看的名字。**没配映射一律脱敏**，绝不把账号 ID 摆到群里。

        ⚠️ 企微回调的 `from` 只有 `userid`，没有昵称/别名（2026-10-30 实测，
        改过群昵称也没用）⇒ 想显示 "CEO" 只能在这里配 `WECOM_USER_LABELS`。
        """
        uid = (userid or "").strip()
        if not uid:
            return "?"
        mapped = self.user_labels.get(uid)
        if mapped:
            return mapped
        return (uid[0] + "***") if len(uid) > 1 else "*"

