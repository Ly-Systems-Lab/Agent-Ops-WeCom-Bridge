# 飞书（Lark）

**状态：能力已核实（对照官方文档逐条确认），适配器未实现。**

飞书是目前四个平台里**形态最接近企业微信**的一个：长连接是官方一等公民，
流式更新有专门接口，卡片回调也能走长连接。按契约的四条能力轴，它四条全过。

---

## 它的形态

| 能力 | 飞书的做法 |
|---|---|
| **① 长连接** | 官方提供「使用长连接接收事件」与「使用长连接接收回调」两种模式，SDK 主动外连，不需要公网地址或域名 |
| **② 可更新消息** | CardKit 提供流式更新卡片：创建卡片时开启 `streaming_mode`，或调用 `cardkit.v1.cardElement.content` 逐步更新元素内容 |
| **③ 交互卡片** | Card 2.0 + 卡片回调；按钮、表单都能带 |
| **④ @ 隔离** | `im.message.receive_v1` —— 群里只有 @ 机器人的消息才会推给应用，和企微同构 |

---

## 8 个适配点，待填的答案

| # | 适配点 | 目前判断 | 依据 |
|---|---|---|---|
| 1 | 建连与重连 | 官方长连接，SDK 负责重连 | 官方文档 |
| 2 | 收消息 + @ + 去重 | `im.message.receive_v1`；去重键待定 | 官方文档 / 待实测 |
| 3 | 发消息 + 更新同一条 | CardKit 流式；**普通文本消息不能更新，流式必须走卡片** | 官方文档 |
| 4 | 发卡片 + 规格限制 | Card 2.0；按钮文字长度上限**待实测** | 待实测 |
| 5 | 收卡片回调 | 可走长连接；**必须是 Card 2.0 的回调** | 官方文档 + 待实测 |
| 6 | 分片与错误码 | 单条大小上限、切片触发条件**待实测** | 待实测 |
| 7 | 身份与白名单 | `open_id` / `union_id` / `user_id` / 邮箱都存在，**白名单用哪一个要先定** | 官方文档 |
| 8 | 限流与节流 | QPS 与推送频率**待实测** | 待实测 |

---

## 与企微的差异（动手前必须知道）

1. **长连接只支持企业自建应用。** 想用"自建应用 + 长连接 + 免公网"这条路，应用类型不能选错。
2. **流式更新只对富文本组件生效。** 官方写明：搭建工具里的卡片，流式仅支持富文本组件，
   普通文本元素不支持流式 —— 所以卡片模板一开始就要按"富文本"来搭，事后改会很痛。
3. **旧版"消息卡片返回交互"回调不支持长连接。** 必须用 Card 2.0 的回调。
   这一条是从 SDK 文档里读到的限制，**要在真机上验证它对新卡片是否同样成立**。
4. **身份标识有多种口径。** 企微只有 `userid`，飞书有 `open_id`（应用内）/ `union_id`（开发者内）/
   `user_id`（租户内）。白名单写在配置里的是哪一种，决定了"换一个应用之后它还成不成立"——
   这直接影响 7 号适配点的 fail-closed 怎么写。

---

## 待实测（没打勾之前不写"支持"）

- [ ] 长连接在企业自建应用上稳定运行 24 小时，断网后能自愈
- [ ] Card 2.0 的按钮回调能**走长连接**收到（这是 5 号适配点的成败点）
- [ ] 按钮文字的实际长度上限（抄企微的"2 个字"大概率不对）
- [ ] 单条消息的字节上限，以及超限时的错误码形状
- [ ] 推送频率上限：突发 50 条会不会被限流
- [ ] `/new` 之后模型拿不到上一轮内容（含本机会话存储）

---

## 参考

- [使用长连接接收事件](https://open.feishu.cn/document/server-docs/event-subscription-guide/event-subscription-configure-/request-url-configuration-case?lang=zh-CN)
- [使用长连接接收回调](https://open.feishu.cn/document/event-subscription-guide/callback-subscription/step-1-choose-a-subscription-mode/configure-callback-request-address?lang=zh-CN)
- [流式更新卡片（CardKit）](https://open.feishu.cn/document/cardkit-v1/streaming-updates-openapi-overview)
- [流式更新文本](https://open.feishu.cn/document/cardkit-v1/card-element/content?lang=zh-CN)
- 平台契约：[`../CONTRACT.md`](../CONTRACT.md)