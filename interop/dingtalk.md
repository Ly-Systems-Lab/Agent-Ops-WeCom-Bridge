# 钉钉 · 执行面（`dws`）

**状态：⏳ 待实测（本机尚未安装）。以下事实来自官方仓库 README 与 `docs/`（2026-11-10 核对）。**

> 本项目**不写钉钉聊天通道** —— 官方 `dws dev connect` 已覆盖"群 @ → 本机 AI"。
> 那个取舍写在 [`platforms/dingtalk/`](../platforms/dingtalk/README.md)；
> 这一页只讲 **Agent 怎么用 `dws` 干活**。

---

## 1. 官方给了什么

| 层 | 能力 |
|---|---|
| **CLI** | 钉钉工作台命令行：文档、表格、待办、审批、日历、会议、邮箱、通讯录等工作区服务 |
| **事件** | `dws event consume`，公开事件流（NDJSON），可喂给常驻 Agent |
| **数字员工** | `dws dingtalk-tag`，可把 `local_agent` 类型绑定到本机 Agent |
| **通道（我们不用）** | `dws dev connect`：群 @ 机器人 → 本机 AI，多轮、`/new` `/clear`、白名单、限流 |

三者的区别值得说清：**CLI 是"手"，事件流是"耳朵"，`dev connect` 是"嘴巴"**。
本项目只缺"嘴巴"吗？不缺 —— 我们已经有入口了；缺的是**手**，所以只接前两个。

## 2. Agent 怎么用

【待实测】官方给出的 Agent 调用模式：

```bash
dws <path> --help                      # 命令是否存在、当前二进制接受哪些 flag
dws schema "<path>" --compact          # Agent 该读的契约视图（正向字段白名单）
dws contact user search --query "张三" --dry-run
dws contact user get-self --jq '.result[0].orgEmployeeModel | {name: .orgUserName, userId}'
dws todo task create --title "..." --executors "<userId>" --yes
```

🔴 **`--yes` 是这一页的重点。**
官方要求 Agent 调用时**必须**带 `--yes`，它会**跳过 CLI 自身的交互确认**
⇒ 对本项目来说，**审批闸门就是唯一那道闸**。所以：

- `dws` 的任何命令**一律弹卡**，不设"读直放"的例外（直到命令级白名单落地）；
- 卡面必须带 `--yes` 这个事实 —— 让人一眼看出"CLI 不会替你确认"。

## 3. Agent Skills

【待实测】

```bash
npx skills add DingTalk-Real-AI/dingtalk-workspace-cli -g
```

装到 `~/.claude/skills/`（同时链接到 Cursor 等已登记的 Agent home）。
默认 **multi 模式**：每个产品一个 skill —— `dingtalk-calendar` / `dingtalk-chat` /
`dingtalk-aitable` / `dingtalk-todo` …

国内网络：用 `dws skill setup`（负 Gitee 回退），或给安装脚本设 `DWS_GITEE_REPO`。

## 4. 执行身份

**用户 OAuth —— 也就是你本人。** 同事收到的消息是你发的，不是你授权的机器人发的。

前置：企业管理员在**开发者平台 → CLI 访问管理**里开启；没开的话 `dws auth login`
会引导你向管理员发申请，审批通过后重新登录。

## 5. 安装前置

【待实测】

- Windows：PowerShell 一行安装脚本，或 `npm install -g dingtalk-workspace-cli`；
- macOS / Linux：`install.sh`，或 Homebrew；
- 装完 `dws auth login`（无浏览器环境用 `--device`）。

## 6. 已知差异

- ⚠️ **`dws dev connect --daemon` 不支持 Windows**（官方文档明写：无 `setsid`、无 POSIX 信号）。
  我们不用这条通道，所以对当前方案无影响 —— 但如果你打算同时用官方那个机器人心智，要知道这一点。
- **Token 加密存储**：PBKDF2（600,000 次迭代 + SHA-256）+ AES-256-GCM，密钥绑设备；
  Windows 上叠加 DPAPI。**跨设备无法解密** —— 换机器就要重新登录。
- `--dry-run` 展示调用参数但不执行；`-f table/json/raw` 切换输出格式。
- **Schema 与 `--help` 冲突时，官方要求按更保守的安全语义处理** —— 这条我们照抄，闸门同理。

## 7. 状态

| 项 | 状态 |
|---|---|
| 安装 / 授权 | ⏳ 未做 |
| Skills 落盘 | ⏳ 未做 |
| 经本项目闸门执行一条读 | ⏳ 未做 |
| 经本项目闸门执行一条写 | ⏳ 未做 |
| 旗舰验收（企微进 → 钉钉出） | ⏳ 未做，判据见 [联邦式中枢 §四](../docs/federation.md) |