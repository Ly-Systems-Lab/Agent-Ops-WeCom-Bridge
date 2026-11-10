# 钉钉

**状态：不重复实现通道层；已作为「执行面」接入并实测（2026-11-10）。**
官方 `dws` 已覆盖通道层，本项目把钉钉的**工作区能力**当作「执行面」接入。

这不是"没做"，是**核对过官方实现后的取舍**（2026-11-10，逐条读了
[`DingTalk-Real-AI/dingtalk-workspace-cli`](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli)
的 README 与 `docs/`）。

---

## 为什么不做这条通道

官方 `dws dev connect` 已经提供（全部来自官方文档原文）：

| 能力 | 官方做法 |
|---|---|
| 群 @ 机器人 → 本机 AI 回答 | `dws dev connect --channel auto` |
| 多轮上下文 | 默认开（`--agent-memory`） |
| `/new` `/clear` | 原生支持，整条消息才生效、不消耗 AI 调用 |
| 白名单 | `--allowed-users 工号` / `--allowed-groups` |
| 限流 | `--user-rate-limit`，默认 20 条/人/分钟 |
| 工作目录 / 模型 / 图片 | `--agent-workdir` / `--agent-model` / 支持 |

它还有我们没做的：工作区 20+ 服务（文档、表格、待办、审批、日历、会议、邮箱）、
28 个事件订阅、数字员工。**再写一条传输通道，收益只剩"工具级审批 + 聊天里切模型"两项，
维护成本却要我们自己扛**（协议、卡片模板、Windows 常驻缺口）。

> 已完成的离线实现（手写 Stream 协议 + 90 项测试）存档在开发仓
> `exp/dingtalk-adapter-20261110` 分支，不进公开仓、不对外承诺。
> 将来如果"钉钉里必须用我们的审批闸门"成为刚需，再捡回来真机验收。

---

## 我们怎么用它：联邦式中枢

**入口平台**（人跟 Agent 说话的地方）和**执行面**（事情真正落地的地方）解耦：

| 层 | 现在有什么 |
|---|---|
| 入口 | 企业微信 / 飞书 / Slack（本项目已真机验收） |
| 内核 | 本项目的队列、会话、**审批闸门**、模型热切换、分片 |
| 执行面 | 本机（Bash/文件）、P4 设备（MQTT）、**钉钉工作区（`dws`）✅ 读 + 写已实测**、飞书工作区（`lark-cli`）⏳ |

也就是说：**在企微里说一句话，Agent 可以到钉钉里查待办、发消息、建文档。**
钉钉不是第四条通道，而是 Agent 的一只"手" —— **这一条已经跑通了**（2026-11-10，
细节与原始数字见 [`interop/dingtalk.md`](../../interop/dingtalk.md)）。

设计、身份边界与审批策略见 [`docs/federation.md`](../../docs/federation.md)。

---

## 参考

- 官方 CLI 仓库：[DingTalk-Real-AI/dingtalk-workspace-cli](https://github.com/DingTalk-Real-AI/dingtalk-workspace-cli)
- 官方产品页：[钉钉 DWS CLI](https://open.dingtalk.com/dingtalk-cli)
- 机器人快速上手：`docs/robot-quickstart.md`（仓库内）
- 常驻服务：`docs/connect-daemon-service.md`（仓库内；⚠️ `--daemon` 不支持 Windows）
- 平台契约：[`../CONTRACT.md`](../CONTRACT.md)