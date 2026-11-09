# Agent-Ops-WeCom-Bridge

**为企业微信打造的重算力 Agent 控制台与运维网关**

同一份内核已在**飞书**上真机打通 —— **平台是入口，不是身份。**

> 🔗 **三件套** · [🧠 控制面（本仓）](https://github.com/Ly-Systems-Lab/Agent-Ops-WeCom-Bridge) · [🔌 数据面 · Edge-Hub](https://github.com/Ly-Systems-Lab/Agent-Ops-Edge-Hub) · [🔭 总蓝图 · Architecture](https://github.com/Ly-Systems-Lab/Agent-Ops-Architecture)
>
> 企业微信（推荐）可作为你的调度台：Claude Code 等重算力 Agent 在这里被指挥、被看见、被拦下
> （流式进度、打断、权限拦截），原生打通 MQTT 还能直接触达物理世界。
>
> 它是 **Agent Ops 三件套里的「控制面」**：
>
> | 仓 | 角色 | 状态 |
> |---|---|---|
> | **本仓** · [`Agent-Ops-WeCom-Bridge`](https://github.com/Ly-Systems-Lab/Agent-Ops-WeCom-Bridge) | 控制面 · 人 ⇄ Agent | 已公开 |
> | [`Agent-Ops-Edge-Hub`](https://github.com/Ly-Systems-Lab/Agent-Ops-Edge-Hub) | 数据面 · Agent ⇄ 物理世界（MQTT / 端侧设备） | 已公开 |
> | [`Agent-Ops-Architecture`](https://github.com/Ly-Systems-Lab/Agent-Ops-Architecture) | 总蓝图 · 《协同宣言》与全景架构（只有文档，不含代码） | 已公开 |

## 🎬 演示

**[▶ 手机上发指令，结果落在 Windows 本地](https://www.bilibili.com/video/BV1nY4w6kEiy)**

包含任务卡片（允许 / 拒绝 / 实时进度 / 上下文余量）与 `/stop` 即刻停止的完整演示。

🖼️ **[实拍图集 →](https://github.com/Ly-Systems-Lab/Agent-Ops-Architecture/blob/main/docs/showcase/README.md)** · 手机端下指令、Windows 本地干活、Agent 自动修 Bug

## 🧩 一份内核，不同的聊天平台

这里要解决的问题从来不是「企业微信」这四个字，而是：

> **人不在电脑前的时候，怎么找到并驱动那台机器上的 Agent。**

企业微信只是跑得最远的那个入口。**同一份内核已经接上了形态很不一样的飞书** ——
长连接换成官方 SDK，消息模型、卡片回调、字节上限、推送频率全都不一样，
而「交互基准」那五条，一条都没改。

| 平台 | 状态 |
|---|---|
| 企业微信 | **参考实现** · 真机验收 + 两次外部 Agent 独立复现 |
| 飞书 | **真机验收** · 五项交互 + 连续在线 24 小时 + 断网自愈 |
| Slack | **真机验收** · 单聊 / 频道 / 审批卡 / 线程续接 / 断网自愈 / 24 小时连续运行（进程零重启） |
| 钉钉 | 能力已对照官方文档核实，**适配器未实现** |

👉 想换平台，从 [`platforms/`](platforms/README.md) 开始。

## 🌌 The Manifesto | 我们的协同宣言

**在这个模型狂飙的时代，我们拒绝“许愿式”的 AI 幻想。**

<details>
<summary><b>展开完整宣言</b></summary>

<br>

我们坚信，**AI 时代不是工具的替代，而是智慧的共存。**

**我们鼓励勇敢闯入“未至之境”**，别因底层知识的壁垒而不敢迈出第一步；但我们也必须保持工程师的严谨——**绝不不懂装懂，永远敬畏物理世界的客观规律。**

面对浩瀚的复杂术语与专业名词，大模型（军师）可作为我们最好的破壁机；
面对真实的业务逻辑与物理设备，Agent（将领）可作为我们最得力的执行官。

**让人类负责意图、决策与底线，让 AI 负责算力、检索与执行。**
敢用、会用、用好，这就是我们定义下一代人机协同的“基本标准”。

—— 献给所有敢于在 AI 时代重构自身工作流的探索者

</details>

## 🎣 这个仓真正想给的：授人以渔，而非死板代码

**代码只是参考实现（reference implementation）。** 换台机器、换个 Agent，下面这套东西照样成立 ——
这才是我们想留给读者的部分。

**五条约定**

1. 💬 **入口是聊天** —— 你在哪，工作台就在哪。不需要公网 IP、不需要服务器。
2. 🎯 **`@` 是路由，人是唯一的调度者** —— Agent 之间不直接通信（防打架、防串通）。
3. 🧠 **一个 Agent = 一个工作区 + 一份记忆 + 一个大脑**，换个窗口进来还是同一个 Agent。
4. 🧩 **能力分层，别重复造轮子** —— 官方已经有的一律用官方的，官方没覆盖的才自己补。
5. 🧾 **每个动作留痕** —— 谁点的、跑了什么、上下文还剩多少，都看得见。

**一个任务从「收到」到「干完」该长什么样**，这就是我们说的**交互基准**：

| 环节 | 应有的样子 |
|---|---|
| 收到 | 先回执，且和正文**共用同一个气泡**（不新开一条"处理中"） |
| 执行 | 气泡里给**真进度**：第几步、正在读哪个文件 —— 不是定时器编的"已跑 N 秒" |
| 打断 | 任何时候都能 `/stop`，而且杀的是**进程树** |
| 有副作用 | 危险动作先问「允许 / 拒绝」，**否决权在 hook 自己手里**（不在 harness） |
| 收尾 | 结果、拒绝、错误**一律如实回报**；超长才切片，每片等回执 |

> ⚠️ **能"直接抄"的只有这一段**：`Windows + Claude Code` 的最小闭环，就是本仓这份代码。
> 换成 macOS、换成 Codex、DeepSeek Harness 或任何能跑命令的 Agent，
> **约定照用，接入步骤要照官方教程重走一遍** —— 我们只写了官方没覆盖的入站与交互这一段。
>
> 🐧 **Linux 这条也要说清楚**：同一套最小闭环在**树莓派**上同样跑通了（连开机自启），
> 只是那边用的是另一套 Agent 实现与系统服务自启，不是本仓的 Windows 脚本。
> 平台相关的那几处（自启、单实例、进程树）按你的系统替换即可 ——
> **而且可以直接让 Agent 陪你搭**：把这份约定交给它，它会照着你的系统补。
>
> **换 harness 到底要重写哪几处？** 教程 [§8 的「6 个适配点」表](docs/claude-code-wecom-guide.md)
> 把通用契约和运行器适配点分开列了：通用部分照抄，这 6 处按你的 Agent 自己回答。
>
> 📄 **已经有人照着接过了**：[外部接入记录](docs/field-reports/README.md) ——
> OpenCode 与 Codex 两个 Agent 各按同一份文档从零接入，附它们自己的评价与踩坑。
>
> 目标很简单：**让更多人照着这套约定，更好、更便捷地人机协同学习、工作等**，
> 而不是又多一个“更先进的工具”。

## 👥 一个群，几种成员

我们自己的群里现在就是这么用的。下面写的是**角色**，不是某个具体的人：

| 成员 | 谁在跑 | 干什么 |
|---|---|---|
| **你** | 人 | 唯一的路由器，也是唯一的共享上下文 —— `@` 谁谁干活 |
| **CC（本项目）** | 你的电脑 | **动手**：读代码、改文件、跑命令，结果回到同一个气泡 |
| **官方云端大模型机器人** | 企微官方（**普通模式，免费**） | **答疑**：名词解释、代码讲解、思路讨论 —— 不动手，也不碰你的机器 |
| 其它 Agent 终端 | 树莓派 / 另一台电脑 / Codex… | 各自的工作区，各自闭环 |
| **端侧设备机器人** | P4 面板 / C3 小电视 | **物理世界**：显示、播放、采集 —— 而且它们也在群里、也能被 `@` |

> 关键点：这些机器人**互相看不见对方**（平台只把 `@` 自己的消息推给谁）。
> 所以"一个群里好几个 AI"不会变成"AI 互相打架"，也不会背着你串通 ——
> 这是**协议层隔离**，不是配置出来的。

```
【组织层】企微群 = 入口 / 会议室 / 审计记录        ← 你是唯一的路由器
   你 ──@──▶ CC（本机 Claude Code）        ← 动手
     ──@──▶ 官方云端大模型机器人            ← 答疑（免费）
     ──@──▶ 其它 Agent 终端（树莓派 / Codex / …）
     ──@──▶ 设备机器人（P4 面板 / C3 小电视）

【设备层】MQTT / HTTP = 设备总线
   Agent ──▶ 端侧设备（它们同时也在群里，可以被 @）
```

**为什么不是一个"全能大管家"**：一个 Agent = 一个「工作区 + 记忆 + 大脑」，跑在某台设备上；
企微群、私聊、直接开终端，默认是**同一个 Agent 的不同窗口**，换个窗口进来接着做，上下文不丢
（把私聊用 `CC_CWD_DM` 指到另一个工作区时，它就变成另一个 Agent 的窗口——这是可选项，见教程 §6.11）。
各 Agent 之间**不能直接通信是故意的**：能互通就会冒出"谁听谁的"和"私下串通"两件事 ——
前者还能用权限压住，后者压不住。不通信，这两件事就都不存在。
⇒ 于是**人既是唯一的路由器，也是唯一的共享上下文**，层级活在「人的判断 + 群聊 `@`」里。

## 🚀 快速开始

**接入指南（人 + AI Agent 双读者）见 [`docs/claude-code-wecom-guide.md`](docs/claude-code-wecom-guide.md)**
—— 开头那节「开始之前」就是安装，跳过它通常会卡在第一步。

```bash
python -m venv .venv
.venv\Scripts\activate            # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt   # 只有 requests + websocket-client

cp .env.example .env              # 填 Bot ID/Secret、你自己的 userid、ANTHROPIC_*
.venv\Scripts\python.exe -X utf8 app\bridge.py
```

- 依赖只有两个第三方包，其余全是标准库。**Windows 实测**；Linux 树莓派上同一套控制面闭环
  也已跑通（自启用的是系统服务，不是本仓脚本）；macOS 协议层同样通用，自启需自己换一套。
- **默认是只读档**（`dontAsk`）：能问答、能读代码，**不会动你的机器**。
  要让 Agent 真干活，显式改 `CC_PERMISSION_MODE` —— 见「[权限](#权限三种形态一个开关)」。
- 可选：让它常驻（登录自启、单实例、看门狗自愈、日志保留 14 天、运行期间不睡眠）

```bash
.venv\Scripts\python.exe -X utf8 app\bridge.py --install-autostart    # 装（不需要管理员）
.venv\Scripts\python.exe -X utf8 app\bridge.py --autostart-status     # 看
.venv\Scripts\python.exe -X utf8 app\bridge.py --uninstall-autostart  # 卸
```

## 🧰 它现在能做什么

**群聊 + 私聊都通**；流式气泡、模板卡片、审批卡片都已**真机验收**；
[`tools/test_phase1.py`](tools/test_phase1.py) 里 218 项检查（含会真调一次模型的那几项）全过 —— 你可以自己复跑。

| 能力 | 说明 |
|---|---|
| 长连接 | 主动外连 `wss://openws.work.weixin.qq.com`；30s 心跳；断线指数退避（3s→30s，连上即重置） |
| 鉴权 | `from.userid` **白名单**是唯一真正的鉴权；群里的 `@` 只是消息路由。**留空即拒绝启动**（fail-closed） |
| 群聊 / 私聊 | 群里 `@` 它、私聊直接发；两条路径**各自独立上下文**，默认共用一个 `cwd`，也可用 `CC_CWD_DM` 分开（见教程 §6.11） |
| 会话 | 每个 `(会话, 用户)` 一份 session，跨消息续接；`/new` 清上下文 |
| 控制指令 | `/status` `/new` `/stop` `/model` —— **忙碌时也即时可用**（`/status` 是按钮卡） |
| **模型热切换** | 聊天里 `/model` 看列表、`/model <编号\|名字>` 切换、`/model reset` 回默认。**下一条消息生效，不用重启**；切换前先真调一次验证，通过才写进 `data/model.json`；**全局**（群聊 + 私聊一起变）。⚠️ **切到/切离「有自己的工作目录」的模型（例如本地模型）会自动新开会话**（主动设计，见下方第 4 条） |
| 忙碌语义 | 处理中再发 → 回「忙碌中…本条已忽略」，**丢弃不排队**（防输入轰炸） |
| 长回答 | 先整条发；只有被平台 `40058` 拒了才切片（每片等回执，失败会明说哪段没送到） |
| 上下文余量 | 每条回复尾部 `📊 上下文 x% (m/n)`：分母取 `modelUsage.contextWindow`，分子取**本轮最后一次 assistant 调用的 usage**（不是累计值） |
| **真进度** | 气泡显示「正在跑（12s · 第 3 步）+ 当前工具」；**变化才推**，附 60s 兜底心跳 |
| **模板卡片** | `/status` = 刷新 / 清空 / 停止。⚠️ 按钮文字**只有 2 个字的余量**：4 个字会被截成「清...」 |
| **审批卡片** | `Bash`/`Write`/`Edit` 先发「允许 / 拒绝」，点了才执行；只读直放。**仅全自动档位下有效**；开启后群聊 + 私聊默认都挂卡（`CC_APPROVAL_DM=1`） |
| 常驻 | 登录自启、单实例、看门狗自愈、日志保留 14 天、运行期间不睡眠 |
| 企业能力 | 邮件 / 文档 / 表格 / 日程 / 会议 / 待办 / 微盘 / 通讯录 / 读 7 天群历史 —— **由官方 `wecom-cli` 提供**，本仓零代码接入 |
| 设备总线 | MQTT / HTTP 控端侧设备 —— **在数据面那个仓**（[`Agent-Ops-Edge-Hub`](https://github.com/Ly-Systems-Lab/Agent-Ops-Edge-Hub)），不在本仓库 |
| 多 Agent 同群 | 多个机器人同处一群、`@` 谁谁干活、互不串台 —— **形态成立**（已在同一群里用过） |

**关于「物理世界」这一半**：端侧设备（C3 小电视、P4 触控面板、具身小车）已经接在同一条设备总线上，
它们**本身就是群成员、可以被 `@`**。接法与实测数据在数据面那个仓的
[企微接入教程](https://github.com/Ly-Systems-Lab/Agent-Ops-Edge-Hub/blob/main/ai-agent-wecom/README.md) 里 ——
那是**架构思路**，不是另一份接入流程。**MQTT 数据层底座已经公开**（[`Agent-Ops-Edge-Hub`](https://github.com/Ly-Systems-Lab/Agent-Ops-Edge-Hub)）；
两个仓合起来，才是完整的「**人 ⇄ Agent ⇄ 物理世界**」这条路。

> 🎬 **[看实拍：一句话驱动三台端侧设备（36 秒）](https://www.bilibili.com/video/BV18Zx86KEDT/)** ——
> C3 小电视 → P4 触控面板 → 具身智能小车，由 Agent 依次执行。

## 🏛️ 和官方是什么关系：站在官方肩膀上

**先去看官方。** 这一页就是入口：**[企业微信 · AI 开放能力](https://work.weixin.qq.com/nl/index/aicli)**
（以下按该页 2026-10-29 的内容整理，**以官方当时的版本为准**；本文不复制、不改写它的教程）。

它当前列出 **6 个 Agent 工具**（WorkBuddy、DeepSeek Harness、MiniMax Code、Kimi Work、Codex、
企业自研智能体）、**11 项核心开放能力**、**CLI 与 MCP 两种调用方式**，FAQ 里还有
**「如何通过长连接模式访问机器人？」**—— 也就是本项目的入口。

⚠️ 这份名单里**没有 Claude Code**（另有 OpenClaw 这类工具由官方单独提供文档）。
不是官方漏了什么，各家路径本来就不同 —— 我们只是把 **Windows + Claude Code** 这一条的坑写下来。

| | 官方 aicli（`wecom-cli`） | 本项目 |
|---|---|---|
| 定位 | **Agent 的「手」**：发邮件、建文档、排日程、查通讯录 | **Agent 的「耳朵和嘴」**：收实时消息、把结果回到同一个气泡 |
| 入站 | ✅ 有，但是**轮询**：以**授权真人**身份拉最近 **7 天**的会话历史 | ✅ **事件驱动**：长连接回调，机器人只看得见 **@ 自己**的 |
| 出站 | 11 项核心能力（`wecom-cli schema list` 实测列出 **15 个服务**） | 回复同一个气泡（流式）+ 模板卡片（按钮可点） |
| 身份 | 你本人（能看到你这个人能看到的） | 机器人（只看得到 @ 它的） |
| 需要什么 | 装 CLI + `auth init` 授权 | 一个 Bot ID / Secret |
| 能否同时用 | ✅ 可以，而且**建议一起用** | ✅ 可以 |

**两者是叠加关系，不是替代关系。** 官方补的是"Agent 会使用企微的业务能力"；本项目补的是
"**人不在电脑前的时候，怎么找到并驱动那个 Agent**"—— 官方入口是命令行 / API 调用，
**没有常驻监听**；要"手机发一句话、电脑里的 Agent 立刻收到并回话"，得有人把长连接一直维持着。

> 我们不说"官方不行"。官方把地基、协议、教程都做好了，
> **这个项目只是在它没走到的地方，多修了一小段路。**

⚠️ **装了官方 CLI 也不等于能用**：默认档位 `dontAsk` 下模型跑不了 `Bash(wecom-cli:*)`
（实测 `--allowedTools` 也放不进来，2026-10-29）。要用企业能力，得开全自动或「逐次询问」。

## 🔐 权限：三种形态，一个开关

**怎么切换：改 `.env`，然后重启桥。** 聊天里**没有**切换指令 ——
`/status` 卡片上的「档位」一行只负责**显示**，三个按钮（刷新 / 清空 / 停止）里没有切换。
这是有意的：档位是"这台机器交给谁"的边界，不该能被聊天里的任何一句话改掉。

| 形态 | 配置 | 实际效果 |
|---|---|---|
| **只读**（默认） | `CC_PERMISSION_MODE=dontAsk` | 能问答、能读代码；**执行命令 / 写文件一律被拒**，拒绝原因如实回到聊天里 |
| **全自动** | `CC_PERMISSION_MODE=bypassPermissions` | 模型直接在你机器上执行命令、改文件，不弹任何确认 |
| **逐次询问** | `bypassPermissions` + `CC_APPROVAL=1` | 有副作用的调用（`Bash`/`Write`/`Edit`/`WebFetch`）先发「允许 / 拒绝」卡；只读工具（`Read`/`Grep`/`Glob`/`TodoWrite`）直放 |

**群聊 / 私聊的默认**：审批一旦开启（`CC_APPROVAL=1`），**群聊和私聊都挂卡**
（`CC_APPROVAL_DM` 默认 1）。私聊全自动是**显式可选项**：设 `CC_APPROVAL_DM=0`。
工作区也可以分开（`CC_CWD` / `CC_CWD_DM`）—— 完整教程见
[§6.11](docs/claude-code-wecom-guide.md)。

三条必须知道的事实（都是实测，细节见教程
[§3.5](docs/claude-code-wecom-guide.md) / [§6.10](docs/claude-code-wecom-guide.md)）：

1. **「逐次询问」不是第四个档位**，它是全自动档位**之上**加的一道闸。`--permission-mode`
   只有 `acceptEdits / auto / bypassPermissions / manual / dontAsk / plan` 六个合法值（**没有 `default`**）。
2. 🚨 **审批卡片不替代 `dontAsk`**：PreToolUse hook **不能越过权限档位** —— `dontAsk` 下
   就算你点了「允许」，命令照样不执行。它是"已经决定全自动之后"的第二道闸，不是"让全自动变安全"的东西。
3. **改配置要重启 —— 但模型例外**：`.env` 只在进程启动时读一次，运行期不会重读
   ⇒ 改了不重启 = 没改（常驻实例按 PID 停掉再拉起，或等任务计划程序的下一次兜底；
   桥是登录自启的，直接重启电脑同样生效）。
   **换 API key / 服务商 / 工作区属于这一类** —— 改完 `ANTHROPIC_*` 必须重启桥。
   **模型不用重启**：它是运行时状态，聊天里 `/model` 改完下一条消息就生效（见下）。

> 想确认**当前真的在跑哪一档**：聊天里发 `/status`，卡片「档位」一行就是实际生效的那个值。

**模型（本地 / 云端）怎么切**（不用碰 `.env`，也不用重启）—— 展开看用法与设计选择：

<details>
<summary><b>展开：<code>/model</code> 用法与四条设计选择</b></summary>

<br>

```
/model                 列出候选 + 当前值
/model 2               按编号切
/model glm-5.2         按名字切（任意名字都行，会先验证）
/model reset           回到 .env 里 CC_MODEL 的默认值
```

四条设计选择，都是有意的：

- **切换前先真调一次**。端点不提供模型枚举（实测 `GET /v1/models` 回 `404`），
  名字对不对只有调一次才知道 ⇒ 探测通过才写进 `data/model.json`，
  错的模型**永远进不了真源**。失败的原文会压成一句人能读的话。
- **模型是全局的**，群聊和私聊一起变；**正在跑的任务不受影响**（它启动时就定了）。
- **真源坏了不会哑**：`data/model.json` 读不出来 ⇒ 报警并用 `last_good`；
  整份文件坏 ⇒ 退到 `.env` 的 `CC_MODEL`（两个文件不会一起坏）；都不行才不传 `--model`。
- ⚠️ **切到「有自己的工作目录」的模型会自动新开会话 —— 这是主动设计，不是会话丢了。**
  不同模型的上下文窗口能差一个数量级（本地 9B 只有 32K，云端是 1M），
  把旧会话硬续上去会直接撑爆；而且撑爆是**静默**的：模型突然不会用工具了，不报错。
  所以换工作目录 = 新开会话，**下一条消息就稳稳落在新模型上**。
  **本地与云端模型切换：新开会话，上下文不互通。** 切换前请确认没有正在进行的工作。
  **云端模型之间互切（同一个工作目录）不新开会话**，上下文照常延续，方便接着干活。

</details>

## 🛡️ 安全边界

- `from.userid` **白名单**是唯一真正的鉴权；群里 `@` 只是消息路由。
  **留空 ⇒ 拒绝启动**；要开放全部必须显式 `WECOM_ALLOW_ANYONE=1`。
- 想要全自动就把 `CC_PERMISSION_MODE` 改成 `bypassPermissions` —— 那是**一次有意识的选择**。
  ⚠️ 别以为 `acceptEdits` 更安全：实测在 headless 下它**照样执行 Bash**，与全自动无区别。
- **审批整体默认关（`CC_APPROVAL=0`）**；一旦开启（`CC_APPROVAL=1`），**群聊 + 私聊默认都挂卡**（`CC_APPROVAL_DM=1`），而且**不替代 `dontAsk`**。它的否决权握在
  [`tools/approval_hook.py`](tools/approval_hook.py) 自己手里：harness 超时/崩溃一律**放行**，
  所以 hook 自己 120 秒到点主动拒绝，harness 的 300 秒只是兜底。
  开了它但**自检不过就拒绝启动**（不再静默降级成全自动）—— 自检查两件事：Claude 真的会加载
  这个 hook（`settings.json` 注册 + `--setting-sources` 含 `project`），以及 hook 脚本真的跑得起来。
  群聊 / 私聊用不同工作区时，**每个会挂卡的工作区各查一遍**，任一不过就拒绝启动。
- **凭证不进代码、不进文档、不进日志**：`bot_secret`、`ANTHROPIC_AUTH_TOKEN` 只从 `.env` 读，
  日志里最多出现键名和长度，不出现值。
- **但运行日志**会**记 `userid` / `chatid`，以及消息正文的前 80 个字符** —— 排查"谁发的、
  为什么被拒"离不开它。这些日志只写本机 `logs/bridge-YYYYMMDD.log`（已 gitignore、保留 14 天、
  不外发）；要把日志贴给别人，请先自己脱敏。
- `permission_denials` 与 `is_error` **一律回报**，不许假装成功。
- 杀进程要杀**进程树**（`taskkill /PID <pid> /T /F`），并且**看结果** —— 没确认退出就不能说"已清理"。
- 本项目涉及本地算力调度与物理硬件控制，**请务必在人类监督下使用**。因 AI 幻觉或网络延迟导致的设备损坏或数据泄露，**概不负责**。

## 📚 继续往下读

| 想知道什么 | 去哪 |
|---|---|
| **从零安装、建机器人、填 `.env`、首次自检** | [`docs/`](docs/claude-code-wecom-guide.md) 开头「开始之前」+ §2 |
| Claude Code 侧的**实测事实**（`is_error`、`stream-json`、权限档位矩阵…） | [`docs/`](docs/claude-code-wecom-guide.md) §3 |
| 企微长连接的**真实协议形状**（字段、三条回复通道、20480 上限） | [`docs/`](docs/claude-code-wecom-guide.md) §4 |
| **必须自己实现的 10 个机制**（每条附"不这么做会怎样"） | [`docs/`](docs/claude-code-wecom-guide.md) §6 |
| 让**另一个 AI Agent** 照着实现 | [`docs/`](docs/claude-code-wecom-guide.md) §8（自包含契约，可整段丢给它） |
| **换一个聊天平台**（飞书 / 钉钉 / Slack） | [`platforms/`](platforms/README.md)：同一套约定，四个平台各答一遍 |
| **验收清单** / 发布前红线 | [`docs/`](docs/claude-code-wecom-guide.md) §9 / §10 |
| 代码在哪 | 见下表「目录」 |

## 📁 目录

| 路径 | 内容 |
|---|---|
| [`app/bridge.py`](app/bridge.py) | 桥本体（单文件） |
| [`tools/approval_hook.py`](tools/approval_hook.py) | 审批卡片的 `PreToolUse` hook（独立进程，**否决权在这里**） |
| [`tools/verify_headless.py`](tools/verify_headless.py) | 首次自检①：依赖 + 认证 + headless 行为矩阵（会真调一次模型） |
| [`tools/env_from_settings.py`](tools/env_from_settings.py) | 把 `~/.claude/settings.json` 里 `ANTHROPIC*` 搬到 `.env`（只打键名和长度，不回显 token） |
| [`tools/test_phase1.py`](tools/test_phase1.py) | 单元测试（`--skip-llm` 只跑离线部分） |
| [`.claude/settings.json`](.claude/settings.json) | 注册 `approval_hook.py`；`CC_APPROVAL=0` 时它是空壳 |
| [`docs/`](docs/claude-code-wecom-guide.md) | 接入教程（人 + AI Agent 双读者） |
| [`platforms/`](platforms/README.md) | 平台适配：契约 + 企业微信 / 飞书 / 钉钉 / Slack 各自的答案 |
| [`LICENSE`](LICENSE) [`NOTICE`](NOTICE) | Apache-2.0 全文与版权声明（都在仓库根目录） |
| `logs/` `data/` `.env` | 运行产物与凭证，**不入库** |

> 开发侧的探针脚本（`probe_permission_modes.py` / `probe_hooks.py` 等）与台账（`notes/`）
> **不随公开仓发布** —— 它们是给我们自己复现结论用的，带着本机路径和实验痕迹。

## 🤝 贡献与支持

欢迎提交 Issue 或 PR。如果你觉得这个架构对你有启发，请点个 Star ⭐！

## 📜 许可

**Apache License 2.0** —— 全文见 [`LICENSE`](LICENSE)，版权声明见 [`NOTICE`](NOTICE)。
我们坚信开源与共享的力量。在遵守许可证条款的前提下，你可以自由地使用、修改、分发本项目（包括商业用途），甚至将其闭源集成到你的商业产品中。我们唯一的期望是：**保留原作者的版权声明（NOTICE 文件）。**

**开源不仅是给人看，更是给 AI 学习。我们希望这套架构理念能成为未来人机协同的基石。**