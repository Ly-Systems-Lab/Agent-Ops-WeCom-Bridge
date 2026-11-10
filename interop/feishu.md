# 飞书 · 执行面（`lark-cli`）

**状态：⏳ 待实测（本机尚未安装）。以下事实来自 npm 包内 `README.zh.md`（2026-11-10 核对，v1.0.99）。**

> 入口那一层（长连接、审批卡、线程续聊）在 [`platforms/feishu/`](../platforms/feishu/README.md)。
> 这一页只讲 **Agent 用它干活** 的那一面。

---

## 1. 官方给了什么

- **18 个业务域、200+ 命令、20+ Skills**：日历、即时通讯、云文档、云空间、Markdown、
  多维表格、电子表格、幻灯片、任务、知识库、通讯录、邮箱、视频会议、考勤、审批、OKR、应用。
- **事件订阅**：`lark-event` skill 提供 WebSocket 实时事件，支持正则路由。
- **三层命令粒度**：`+快捷命令` / API 命令 / 通用 API 调用。
- ⚠️ **飞书项目（Meegle）不在这个包**：由独立的 `meegle-cli` 提供，需单独安装。

## 2. Agent 怎么用

【待实测】官方给出的 Agent 四步：

```bash
npx @larksuite/cli@latest install     # 1. 安装
lark-cli config init --new            # 2. 配置应用凭证（输出授权链接给用户）
lark-cli auth login --recommend       # 3. 登录（同上，链接给用户）
lark-cli auth status                  # 4. 验证
```

命令面本身：

```bash
lark-cli <domain> +<shortcut> --dry-run        # 快捷命令，内置智能默认值
lark-cli <domain> <method> --json              # API 命令
lark-cli auth check --scope "<scope>"          # exit 0 = 有权限，1 = 缺失
```

## 3. Agent Skills

【待实测】

```bash
npx skills add larksuite/cli -y -g
```

装到 `~/.claude/skills/`，包含 `lark-shared`（所有其他 skill 自动加载，管配置 / 认证 / 身份切换）
和 `lark-calendar` / `lark-im` / `lark-doc` / `lark-base` / `lark-task` / `lark-approval` 等。

## 4. 执行身份

**OAuth 登录 —— 默认是你本人**，也可以用 `--as bot` 切成机器人身份。

```bash
lark-cli calendar +agenda --as user
lark-cli im +messages-send --as bot --chat-id "oc_xxx" --text "Hello"
```

⇒ 对本项目来说：**同一件事可能有两种身份**，所以审批卡的"执行身份"一栏不能写死，
必须按命令实际带的 `--as` 显示。

## 5. 安装前置

需要浏览器配合：`config init --new` 与 `auth login` 都会输出授权链接，
**用户自己在浏览器里完成**，命令随后自动退出（Agent 模式可 `--no-wait` + `--device-code` 续上）。

## 6. 已知差异

- 🔴 **官方明确建议：把接了这个 CLI 的机器人当私人助手用，不要拉群、不要允许其他用户与它交互。**
  原因写在官方安全提示里：授权后 AI 会以你的用户身份在授权范围内操作。
  ⇒ 本项目用它时，**入口白名单是硬前提**；群聊入口要格外小心。
- **风控信号默认开**：向飞书官方 HTTPS 域名发请求时会带 OS 类型与硬件型号，
  可用 `lark-cli config risk-control off` 关闭（默认建议保持开启）。
- **DPoP 默认 `preferred`**：新签发凭证优先用 DPoP 而非 Bearer。
- 凭据存系统 Keychain（macOS）/ 对应凭据保护（Windows）。
- **输出契约稳定**：JSON 输出、分页、dry-run、schema 自省都有明确约定 —— 适合 Agent 解析。

## 7. 状态

| 项 | 状态 |
|---|---|
| 安装 / 授权 | ⏳ 未做 |
| Skills 落盘 | ⏳ 未做 |
| 经本项目闸门执行一条读 | ⏳ 未做 |
| 经本项目闸门执行一条写 | ⏳ 未做 |
| `--as user` / `--as bot` 身份在卡面正确显示 | ⏳ 未做 |