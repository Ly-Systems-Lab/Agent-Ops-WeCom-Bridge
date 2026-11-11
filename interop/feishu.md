# 飞书 · 执行面（`lark-cli`）

**状态：✅ 已接入（2026-11-11 实测，`lark-cli` 1.0.100）。**

> 入口那一层（长连接、审批卡、线程续聊）在 [`platforms/feishu/`](../platforms/feishu/README.md)。
> 这一页只讲 **Agent 用它干活** 的那一面。

---

## 1. 官方给了什么

- **18 个业务域、200+ 快捷命令、256 条 API 命令、26 个 Skills**：日历、即时通讯、云文档、
  云空间、Markdown、多维表格、电子表格、幻灯片、任务、知识库、通讯录、邮箱、视频会议、
  考勤、审批、OKR、应用。
- **三层命令粒度**：`+快捷命令` / API 命令（`域 资源 方法`）/ 通用逃生舱（`api GET|POST <路径>`）。
- **事件订阅**：`lark-event` skill 提供 WebSocket 实时事件。
- ⚠️ **飞书项目（Meegle）不在这个包**：由独立的 `meegle-cli` 提供，需单独安装。

## 2. 安装与授权（2026-11-11 实测）

```bash
npm i -g @larksuite/cli                            # 装到 PATH，闸门才认得出「裸名字」
lark-cli config init --new                         # 后台跑，输出授权链接；浏览器里建应用
lark-cli auth login --recommend --no-wait --json   # 拿 device_code + 授权链接
lark-cli auth login --device-code <码>             # 用户点完后收口
lark-cli auth status                               # 验证
```

两条实测注意：

- **npm 11 会拦 `postinstall`**（`allow-scripts`），原生二进制不会自动下载 ——
  但首次运行 `lark-cli` 时 `run.js` 会自己补下载，不用额外处理。
- **`config init --new` 的设备码很短命**：浏览器里慢慢建档就会 `app registration timed out`，
  而 CLI 已停止轮询、凭证收不回来（`config show` 仍是 `not configured`）。重跑一次即可；
  超时那次在建应用页面上可能留下一个空壳应用，事后自行清理。
- `--recommend` 会申请全业务域 scope。被企业策略禁止的 scope 会出现在 `warning.missing`
  里，不影响其余授权。

## 3. Agent 怎么用

```bash
lark-cli <域> +<快捷命令>          # 优先用快捷命令
lark-cli <域> <资源> <方法>        # 与开放平台端点一一对应
lark-cli schema <域.资源.方法>     # 查参数、身份、scope
lark-cli api GET|POST <路径>       # 通用逃生舱（没有封装时用）
```

输出契约（官方明写，别按 `code == 0` 判成功）：

```json
{ "ok": true,  "identity": "user", "data": {...}, "meta": {...} }   // stdout，退出码 0
{ "ok": false, "identity": "user", "error": { "type": "...", "message": "..." } }  // stderr
```

**判据是 `ok == true`（或退出码）**，不是 `code == 0` —— 成功信封里根本没有 `code` 字段。

## 4. 执行身份

**默认是你本人**（OAuth 用户身份）；`--as bot` 切到应用自己的机器人身份。

```bash
lark-cli calendar +agenda --as user
lark-cli im +messages-send --as bot --chat-id "oc_xxx" --text "Hello"
```

⇒ 对本项目来说：**同一件事可能有两种身份**，所以审批卡的「执行身份」一栏按命令实际带的
`--as` 显示：用户身份写「飞书 · 你的账号（不是机器人）」，`--as bot` 写
「飞书 · 机器人身份（应用自己的身份）」。默认（不带 `--as`）按 `auto` 走，卡面写用户身份。

## 5. 凭据存储（实测路径）

| 东西 | 位置 |
|---|---|
| 应用凭证 + 用户 token | `~/.lark-cli/config.json` |
| DPoP 密钥 | Windows 平台密钥存储（`windows-platform-ksp`），`dpopMode=preferred` |

⇒ 已进闸门的**硬 deny** 名单（`~/.lark-cli`），命中直接拒绝、不出卡、不看审批开关。
⚠️ DPoP 私钥在系统 KSP 里，**不是文件** —— 这条路径 Agent 拿不到，也不该拿。

## 6. 只读白名单（P1-4，与 `dws` 同一套做法）

**官方的答案照抄成快照，闸门运行时只读快照。**

| 层 | 分类来源 | 覆盖 |
|---|---|---|
| API 命令 | `lark-cli schema` 的 `_meta.risk` | 256 条 |
| `+快捷命令` / 管理命令 | 每条 `<命令> --help` 末尾的 `Risk:` | 550 条 |
| `api` 逃生舱 | 官方整体标 `write` | 一律弹卡 |

- 快照：`interop/lark_effects.json`（806 条：**324 read / 372 write / 110 high-risk-write**），
  由 `tools/lark_effect_sync.py` 生成，`--check` 查漂移。
- **两条来源会互相校对**：schema 与 `--help` 对同一条命令给出不同风险时，同步工具会喊出来。
- 闸门规矩与 `dws` 完全一致：**表外按写；读不出来按写；`--dry-run` 照样弹卡**
  （干跑只写在卡面上，不替用户执行）；`high-risk-write` 卡面写「危险操作，官方要求 `--yes`」。
- **别按 HTTP 动词猜 `api`**：官方把它整体标成 write，`api GET` 也弹卡。
- ⚠️ **Git Bash 里 `lark-cli api GET /open-apis/...` 会被 MSYS 改写成 Windows 路径** ⇒
  前面加 `MSYS_NO_PATHCONV=1`。闸门已放行这个前缀（只控制路径改写，换不掉被执行的程序），
  卡面理由仍然是真正的「通用 API 逃生舱」。

## 7. 已知差异与风险

- 🔴 **官方明确建议：把接了 CLI 的机器人当私人助手用，不要拉群、不要允许其他用户与它交互。**
  授权后 AI 会以你的用户身份在授权范围内操作 ⇒ **入口白名单是硬前提**。
- **风控信号默认开**：向官方 HTTPS 域名发请求时带 OS 类型与硬件型号；
  `lark-cli config risk-control off` 可关（建议保持默认）。
- **DPoP 默认 `preferred`**：新签发凭证优先 DPoP 而非 Bearer。
- **Skills 与桥的兼容性**：`npx skills add larksuite/cli -y -g` 装到 `~/.claude/skills/`，
  而桥用 `--setting-sources project,local`（不含 user）⇒ **桥里看不见这些 Skills**。
  用法兜底写在仓库 `CLAUDE.md` 里（与 `dws` 同一条约束）。

## 8. 状态

| 项 | 状态 |
|---|---|
| 安装 / 授权 | ✅ `lark-cli` 1.0.100，用户 OAuth 已授权 |
| 风险表快照 | ✅ 806 条，`tools/lark_effect_sync.py --check` 可复核 |
| 闸门接入 | ✅ `_lark_ok`：读直放 / 写弹卡 / 表外按写 / `api` 一律弹卡 |
| 卡面执行身份 | ✅ `--as bot` 显示机器人身份（离线用例已覆盖） |
| 经本项目闸门执行一条读 | ⏳ 待真机 |
| 经本项目闸门执行一条写 | ⏳ 待真机 |