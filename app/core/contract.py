"""平台契约 —— 内核与适配器之间唯一被允许的接口。

对应公开文档 `platforms/CONTRACT.md` 的「8 个适配点」与「四条能力轴」。
本文件是**约定本身**，不含实现；实现见 `platforms/<平台>/adapter.py`。

## 内核 → 平台（内核只会调这些）

| 方法 | 语义 | 返回 | 能力轴 |
|---|---|---|---|
| `plat_run()` | 建立长连接并阻塞运行；断线自愈 | 无 | ① |
| `plat_stop()` | 优雅停止 | 无 | ① |
| `plat_send_text(chat, ctype, text, wait=True)` | 主动推送一条文本 | `{sent, errcode, errmsg}` | ③ |
| `plat_send_card(chat, ctype, card, wait=True)` | 主动推送一张交互卡片 | 同上 | ④ |
| `plat_update_card(token, card)` | 就地更新已发卡片 | 同上 | ③ |
| `plat_open_bubble(token, chat, ctype)` | 开一个可反复更新的气泡；不支持则 None | Bubble / None | ② |
| `plat_deliver(bubble, chat, ctype, text)` | 把最终文本落进气泡；放不下按平台规则兜底 | 未送达列表 | ②③⑥ |
| `plat_send_chunks(chat, ctype, text)` | 超长文本分片推送 | 未送达列表 | ⑥ |
| `plat_reply(token, text)` | 一次性被动回复（"收到 / 忙碌"） | bool | ⑤ |
| `plat_status_card(chat, ctype, user)` | 渲染状态卡 | 平台卡片对象 | ④ |
| `plat_approval_card(req)` | 渲染审批卡 | 平台卡片对象 | ④ |
| `plat_approval_update_card(token, req, allow, user)` | 审批卡改成已处理 | bool | ③④ |

## 平台 → 内核（适配器只会调这两个）

| 方法 | 入参 |
|---|---|
| `core_handle_incoming(inc)` | `{userid, chatid, chat_type, raw_chatid, chattype, is_single, response_url, text, at_prefix, msgtype, dedupe_field, dedupe_key, reply_token}` |
| `core_handle_card_action(act)` | `{key, userid, chatid, chat_type, raw_chatid, task_id, reply_token}` |

## 三条必须遵守的语义（不遵守就会静默坏掉）

1. **`sent=False` 且 `errcode is None` = 结果未知**，不是失败。调用方不许据此重发
   （重发会让 Agent 把同一件事再做一遍）。
2. **`plat_open_bubble` 返回 None 时**，内核必须退回 `plat_send_chunks`。
   缺这条能力的平台**不提供逐次询问档**，不能拿文本假装。
3. **`chat_type` 是平台内部语义**（企微 1=单聊 2=群聊），内核只拿它做目录路由与会话键，
   不做任何其它假设。

⚠️ `errcode` 是历史名字，语义是**平台结果码**；内核只判断「有没有值 / 是不是 0」，
不解读具体数值（平台专属错误码只在平台层出现）。
"""
