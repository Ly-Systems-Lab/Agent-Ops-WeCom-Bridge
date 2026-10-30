# 把 Claude Code 接入企业微信（实战指南）

> `Agent-Ops-WeCom-Bridge` · 企业微信 ⇄ 本机 Claude Code 的接入实战。
>
> 本文给的是**原理 + 实测数据 + 最小示例**，不绑定某一版实现。
> 文里标注「实测」的结论都在 Windows + Claude Code 2.1.x + 企业微信「智能机器人·长连接」上跑过，
> 标注「未验证」的请你自己再验一遍。
>
> 读者有两种：**① 人**（要把这件事做出来）；**② AI Agent**（要照着这份契约去实现，或要学会怎么用它）。
> 第 8 节是**自包含**的一段，可以整段丢给你的 agent。

---

## 这份教程给谁看

| 段 | 内容 | 读者 |
|---|---|---|
| ⓪ | **开始之前：环境、依赖与安装**（跳过它通常会卡在第一步） | **人** |
| ① | 企业微信侧怎么建机器人、访问控制怎么定 | **人** |
| ② | Claude Code 侧的真实行为（实测数据 + 踩过的坑） | **人**（带去喂给 agent 实现） |
| ③ | 必须自己实现的 10 个机制，每条附「不这么做会怎样」 | 人 + Agent |
| ④ | **自包含实现契约**（可整段复制给 Agent） | **AI Agent** |
| ⑤ | 验收清单 / 发布前红线 | 人 |

---

## 0. 一句话架构

```
手机企微 ──WS 长连接（桥主动外连）──► 你的电脑上的桥进程
                                        │  去重 → 白名单 → 回执 → 单任务串行
                                        ▼
                           claude -p [--resume <sid>] --output-format stream-json --verbose
                                        ▼
        工具调用 → 更新同一个气泡（真进度）；result / is_error → 先整条推回（40058 才切片）
```

- **不需要公网 IP、不需要服务器**：长连接是桥主动连企微网关，出站即可。
- **不需要树莓派**：Claude Code、你的仓库、登录态都在本机。
- **同一 bot 只能有一个实例**：第二个实例会把第一个踢掉（服务端下发 `disconnected_event`）。

---

## 1. 为什么必须要一个「桥」

Claude Code 是**终端里的 agent**，没有「聊天渠道」这个概念 —— 它既不知道企微是什么，也不会自己监听消息。
所以需要在中间放一个进程，替它保管「这个聊天窗口对应哪个会话」，并在每次收到消息时把
`claude -p --resume <session_id>` 拉起来。

三个必须知道的事实：

1. **headless 模式不会停下来问你。** 权限档位必须在**启动参数**里定好，运行中无法补救。
2. **会话靠 `--resume <session_id>` 续接**，而 `session_id` 由**上一次调用**返回 —— 所以桥必须把它存下来。
3. **`--resume` 与 cwd 绑定。** 一个 session 属于一个启动目录。换仓库 = 换会话。

> **先看官方，再看这篇。** 入口是 [企业微信 · AI 开放能力](https://work.weixin.qq.com/nl/index/aicli)：
> 官方已经为 **WorkBuddy、DeepSeek Harness、MiniMax Code、Kimi Work、Codex、企业自研智能体**
> 这 6 家提供了接入教程（CLI + MCP 两种调用方式），FAQ 里还有「如何通过长连接模式访问机器人？」。
> **那些内容这里一律不复制、不改写**，请以官方页面当时的版本为准。
> 这篇只写官方没覆盖的那一段：**Windows + Claude Code + 常驻入站**。
> 两者不冲突 —— 官方的 `wecom-cli` 是本桥里 Agent 的「手」，建议一起用。

---

## 开始之前：环境、依赖与安装

> 这一节单独拎出来，是因为**克隆下来直接跑是跑不起来的** —— 差依赖、差凭证、差白名单。
> 做完再去第 2 节建机器人（建完把 Bot ID / Secret 填回这里说的 `.env`）。

### 环境要求

| 项 | 要求 | 备注 |
|---|---|---|
| 操作系统 | **Windows** | 协议层与平台无关，但**自启/无窗口启动只实现了 Windows**（任务计划程序 + `pythonw`）。macOS / Linux 要自己换一套自启方式，**未实测** |
| Python | **3.10+** | 实测环境 3.14.6；更老的解释器没测过 |
| 第三方依赖 | **只有两个**：`requests`、`websocket-client` | 其余全部是标准库 |
| Claude Code | 已装好，且 `claude -p "hi"` 能出结果 | 版本见 §3.1 |
| 企业微信 | 一个**智能机器人**，能力选 **API 模式 + 长连接** | 见 §2.1 |

### ① 装依赖

```bash
git clone <this-repo> Agent-Ops-WeCom-Bridge
cd Agent-Ops-WeCom-Bridge

python -m venv .venv
.venv\Scripts\activate              # macOS/Linux：source .venv/bin/activate
pip install -r requirements.txt
```

### ② 建机器人，拿 Bot ID / Secret

按 **§2.1** 做，回来后继续。

### ③ 填 `.env`

```bash
cp .env.example .env
```

必填的就四类：

| 键 | 从哪来 |
|---|---|
| `WECOM_BOT_ID` / `WECOM_BOT_SECRET` | §2.1 建的机器人 |
| `WECOM_ALLOWED_USERIDS` | **你自己的 userid**，逗号分隔。**留空会拒绝启动**（见下面「安全模式」） |
| `ANTHROPIC_*` | 见下 |
| `CC_CWD` | 你想让机器人待在哪个目录干活。一个 session 与一个 cwd 绑定 |

**为什么 `ANTHROPIC_*` 必须自己填**：桥用 `--setting-sources project,local` 排掉 user 设置，
用来挡掉桌面宠物那类 hook（它们会把 headless 卡死，见 §3.7）。
**代价是认证也一起没了** —— 这是**实测的负对照**：排掉 user 设置且不注入环境变量时，
`claude -p` 直接报 `Not logged in · Please run /login`。

一条命令从 `settings.json` 搬（只搬 `ANTHROPIC*`，屏幕上只打键名和长度，**不回显 token**）：

```bash
.venv\Scripts\python.exe -X utf8 tools\env_from_settings.py
```

> ⚠️ **OAuth 登录**（`settings.json` 里没有 `env` 段）在这条路径上**未实测**。
> 最稳的做法是显式填 `ANTHROPIC_AUTH_TOKEN` + `ANTHROPIC_BASE_URL`。

> ⚠️ `.env` **不做变量展开**：写 `%USERPROFILE%\...` 会被当成字面路径。
> 路径一律写展开后的绝对路径（`where claude` 能给准确位置）。

### ④ 首次自检（三步都过再往下）

```bash
# ① 依赖齐了（无输出 = OK）
.venv\Scripts\python.exe -c "import requests, websocket"

# ② 认证 + headless 行为矩阵（会真调一次模型）
.venv\Scripts\python.exe -X utf8 tools\verify_headless.py all

# ③ 起桥 —— 日志里出现 subscribe ok 就通了
.venv\Scripts\python.exe -X utf8 app\bridge.py
```

然后在手机企微里**私聊**机器人（单聊不用 @）发一句「你好」。

### 默认是「安全模式」：会说话，但不动你的机器

克隆下来直接跑，它是**只读**的，原因有两条，都是刻意设计的：

| 设计 | 效果 | 为什么 |
|---|---|---|
| `CC_PERMISSION_MODE` 默认 **`dontAsk`** | 能读文件、能回答问题；**执行命令和写文件会被拒**，拒绝原因如实回到聊天里 | 见 §3.5 的实测表：只有 `plan`/`dontAsk`/`manual` 在 headless 下真的拦得住 |
| `WECOM_ALLOWED_USERIDS` 留空 ⇒ **拒绝启动** | 忘配白名单 = 起不来，而不是"默默对所有人开放" | `@` 只是消息路由，白名单才是**唯一**的鉴权 |

确认通路没问题、并且清楚后果之后，再把 `CC_PERMISSION_MODE` 改成 `bypassPermissions`
（全自动：模型可以直接在你机器上执行命令、改文件）。

**三种形态，切换方式都是「改 `.env` → 重启桥」**（聊天里**没有**切换指令，理由见 §6.10）：

| 形态 | 配置 |
|---|---|
| 只读（默认） | `CC_PERMISSION_MODE=dontAsk` |
| 全自动 | `CC_PERMISSION_MODE=bypassPermissions` |
| 逐次询问（每次有副作用的调用都问一句） | `bypassPermissions` + `CC_APPROVAL=1` |

### ⑤ 常驻（可选）

见 §6.8。

---

## 2. 企业微信侧（人来做，不写代码）

### 2.1 建机器人

1. 登录[企业微信管理后台](https://work.weixin.qq.com/) → 应用管理 → **机器人 / 智能机器人**。
2. 新建一个**智能机器人**，起个名字。
3. 记下 **BotId** 和 **Secret**（只放本地 `.env`，绝不进 git、绝不截图外发）。
4. 能力里选 **API 模式 + 长连接**。这就是「不用公网回调 URL」的关键。

### 2.2 访问控制（重点，别搞错）

- **`@机器人` 只是消息路由，不是身份鉴权。** 群里任何能 @ 到它的成员都能指挥它。
- 真正的边界是 **① 谁能进这个群** + **② 你自己实现的 `from.userid` 白名单**。
- ⇒ 所以本项目把白名单做成 **fail-closed**：`WECOM_ALLOWED_USERIDS` 留空 ⇒ **拒绝启动**；
  真想开放全部，必须显式设 `WECOM_ALLOW_ANYONE=1`。让"开放"成为一次有意识的选择。
- 群里**没 @ 它**的消息，它**收不到** —— 这是企微的天然隔离，不是配置。
- **私聊（单聊）不需要 @，发过去就到** —— 所以白名单在私聊场景下更关键：
  没有白名单的话，谁能搜到这个机器人、谁就能直接指挥你电脑上的 Claude。

### 2.3 `@` 必须「点选」

在输入框打 `@` 后**从弹出列表里选中机器人**。手打 `@你的机器人名` 只是普通文字，
企微**不会**把消息下发给它 —— 群里看着像发了，实际毫无反应。
**排障铁律：机器人"没反应"时，先确认是点选还是手打。**

---

## 3. Claude Code 侧：实测事实

以下每条都是跑过的，不是从文档抄的。

### 3.1 `--output-format json` 返回什么

```jsonc
{
  "type": "result",
  "subtype": "success",
  "is_error": false,
  "result": "最终文本",
  "session_id": "…",
  "num_turns": 1,
  "permission_denials": [],
  "usage": { "input_tokens": 23161, "cache_read_input_tokens": 0, "output_tokens": 3, … },
  "modelUsage": { "<model>": { "inputTokens": 23161, "contextWindow": 1000000, … } },
  "total_cost_usd": 0.001,
  "duration_ms": 1234
}
```

### 3.2 🚨 判成功**只能看 `is_error`**，不能看 `subtype`

实测：一次「未登录」的失败调用返回
`is_error: true` 但 **`subtype` 仍是 `"success"`**，而且**照样生成了一个 `session_id`**。

⇒ 如果你按 `subtype == "success"` 判成功，会把失败当成功；
⇒ 如果你按「有没有拿到 session_id」判成功，同样会错。

### 3.3 提示词用 **stdin** 传，别塞进命令行

```bash
claude -p --output-format json < prompt.txt
```

Windows 命令行约 32KB 上限，长提示词（贴一段代码/日志）会直接失败或触发古怪的转义问题。stdin 没这个限制。

### 3.4 `usage` / `modelUsage` 里能拿到上下文占用

`modelUsage[<model>].contextWindow` 直接给了**上下文窗口大小**，不用自己猜。
当前占用 ≈ `inputTokens + cacheReadInputTokens + cacheCreationInputTokens`。

⇒ 这就是「回复尾部附一行 `📊 上下文 9.7% (97.4k/1.0M)`」的实现方式，**零额外模型调用**：

```python
def usage_footer(data):
    info = next(iter((data.get("modelUsage") or {}).values()), None)
    if not info or not info.get("contextWindow"):
        return ""
    used = (info.get("inputTokens", 0) + info.get("cacheReadInputTokens", 0)
            + info.get("cacheCreationInputTokens", 0))
    return f"\n\n> 📊 上下文 {used*100/info['contextWindow']:.1f}% " \
           f"({used/1000:.1f}k/{info['contextWindow']/1e6:.1f}M)"
```

好处很实际：**在你决定「要不要 /new」之前，先能看到还剩多少上下文。**

### 3.5 权限档位：合法值，以及每一档在 headless 下**真的**拦不拦得住

`--permission-mode` 只接受：`acceptEdits` / `auto` / `bypassPermissions` / `manual` / `dontAsk` / `plan`。

**没有 `default` 这个值。** 传一个不存在的值会启动即失败 —— 而很多教程/模板里写的就是 `default`。

名字很会骗人，所以下面这张表是**跑出来的**（我们自己的权限档位探针，2026-10-28，
Windows + `claude -p`）。判据是**副作用是否发生**（文件真的被创建了吗），不是看模型怎么描述：

| 档位 | Bash 执行 | 写文件 | `permission_denials` | 备注 |
|---|---|---|---|---|
| `plan` | ❌ | ❌ | 空 | 不执行，但**会往 `~/.claude/plans/` 写一份计划文件** —— 也不是零副作用 |
| **`dontAsk`** | ❌ | ❌ | 有记录 | 拒绝干净、可回报。**本项目的默认档** |
| `manual` | ❌ | ❌ | 有记录 | 模型会先换几种写法（含 `tee`、关沙箱）再放弃 |
| `acceptEdits` | ✅ | ✅ | 空 | ⚠️ **等同全自动** —— 它照样跑 `echo x > f` |
| `auto` | ✅ | ✅ | 空 | ⚠️ 同上 |
| `bypassPermissions` | ✅ | ✅ | 空 | 对照组（本项目的所有实测都在这一档做的） |

三条结论：

1. **别按名字推断安全等级。** `acceptEdits` 听起来"只接受编辑"，实际执行 Bash 一点不含糊。
2. **`plan` 不是零副作用**，它会写计划文件到用户主目录。
3. ⇒ 想"只读可用、不动机器"，用 **`dontAsk`**；要让机器人干活，才用 `bypassPermissions`。
4. 🚨 **hook 不能越过档位**（2026-10-29 实测）：`PreToolUse` hook 返回 `allow`，
   `dontAsk` 下命令**照样不执行** —— 档位优先。所以想"每次执行前问一句"，前提是
   **档位已经在 `bypassPermissions`**，再靠 hook 加一道闸（见 §6.10）。

### 3.6 超时与杀进程

- `subprocess` 一定要设总超时。
- **Windows 上必须杀进程树**：`taskkill /PID <pid> /T /F`。只 `terminate()` 父进程会留下一堆子进程。
- **超时的语义是「结果未知」**，不是「失败」：任务可能已经执行了一部分。
  ⇒ 回复里要明确写「结果未知，重试前先核实状态」，不要让用户以为是没执行。

### 3.7 🚨 全局配置里的 hook 会把 headless 卡死

如果你的 `~/.claude/settings.json` 里有 `PermissionRequest` hook 指向某个**本地端口**（桌面宠物、
权限弹窗之类的 UI 工具），headless 任务可能停在那里等一个永远不会来的点击 —— 超时值常常是 600 秒。

而且这类工具往往不止一个 hook：`UserPromptSubmit` / `Stop` / `PreToolUse` / `PostToolUse` 都可能被挂上，
表示**每收一条消息都要弹一次动画、每个工具调用都要起一个进程**。

**隔离办法与代价（实测）：**

```bash
claude --setting-sources project,local   # 不加载 user 设置 ⇒ hooks 没了
```

⚠️ 但**认证也一起没了** —— 如果你的认证是写在 `settings.json` 的 `env` 里（`ANTHROPIC_AUTH_TOKEN` /
`ANTHROPIC_BASE_URL`），排掉 user 设置后调用会报 `Not logged in`。

⇒ 正确做法：**排掉 user 设置，同时把 `ANTHROPIC_*` 作为进程环境变量注入子进程。**
注意要先把继承来的 `ANTHROPIC*` / `CLAUDE*` 一并剥掉，否则「认证成功」可能只是父进程环境变量的假象。

### 3.8 deny 规则在 bypass 下**仍然生效**

如果要「全自动但不许读某些文件」：

```bash
claude --permission-mode bypassPermissions \
       --disallowedTools "Read(**/.env)" "Read(**/.ssh/**)" "Read(**/.claude/**)"
```

实测这三种写法（`Read(**/.env)` / `Read(.env)` / `Read(绝对路径)`）都能挡住，
且**应该是先用「不设 deny」的对照组确认文件本来就读得到**，否则"没泄漏"什么都证明不了。

⚠️ 两个限制必须知道：

1. **`--disallowedTools` 造成的拦截不进 `permission_denials`**（该字段是空数组）。模型只看到"工具不存在"，
   会自己换个工具绕。⇒ **不能靠 `permission_denials` 判断"是否被拦"**。
2. **它只挡 `Read` 工具，挡不住 `Bash(cat .env)`。** 是减速带，不是墙。

### 3.9 🚨 `stream-json` 是拿"真进度"的唯一办法，而它**必须带 `--verbose`**

想在气泡里显示"它现在正在读哪个文件"，就不能等进程结束 —— `json` 模式是**一次性输出**，
中间过程全丢。要 **`--output-format stream-json`**：

```bash
claude -p --output-format stream-json --verbose
```

四条实测（`claude` 2.1.216，Windows，2026-10-29）：

| 事实 | 后果 |
|---|---|
| **不带 `--verbose` 直接 rc=1** | 不是警告，是**跑不起来**；很多示例漏了它 |
| `result` 事件的字段与 `json` 模式**完全一致** | 判成功仍只信 `is_error`（§3.2），不用改判断逻辑 |
| `system/thinking_tokens` **极吵**（一个很小的任务 44 条） | 别把每条事件都推给用户，只取**工具调用**那一类 |
| **必须同时读 stderr** | 只读 stdout 时，stderr 管道写满会把 claude **卡死**（和 `json` 模式同一个坑） |

⇒ 正确姿势是**三条线程**：读 stdout、读 stderr、喂 stdin；主线程只负责 `wait()` 和超时。
别用 `communicate()` —— 它会等进程退出，等于没有流式。

---

## 4. 企业微信长连接协议（实测形状）

### 4.1 连接与心跳

| 步骤 | 内容 |
|---|---|
| 连接 | WebSocket 到 `wss://openws.work.weixin.qq.com` |
| 鉴权 | 发 `aibot_subscribe`，body `{"bot_id": …, "secret": …}`；回执 `errcode=0` 即成功 |
| 心跳 | 每 30s 发 `{"cmd":"ping"}` |
| 重连退避 | 3s 起、翻倍、封顶 30s；**连上后必须重置为 3s** |

> 心跳线程**整个进程只起一个**。把它放在重连循环里，每次重连都会多一个线程，最后 N 个线程同时发 ping。

### 4.2 消息回调的真实字段

```jsonc
{
  "cmd": "aibot_msg_callback",
  "headers": { "req_id": "…" },
  "body": {
    "aibotid": "…",          // 你自己的 bot id，可用来做防御性校验
    "chatid": "…",
    "chattype": "group",      // "group" | "single"
    "msgid": "…",             // ★ 去重键，32 位 hex
    "msgtype": "text",
    "from": { "userid": "…" },// ★ 白名单键
    "response_url": "https://qyapi.weixin.qq.com/cgi-bin/aibot/response?…",
    "text": { "content": "@你的机器人 你好" }
  }
}
```

**两个容易踩的点：**

- `text.content` **带着 `@` 前缀**，喂给模型前必须剥掉。
- 而且**只写第一个词**：机器人显示名是 `CC BOT`，下发的却是 **`@CC 你好`**。
  ⇒ 只按"名字完整匹配"剥会漏；要兜底剥 `@token` **再把名字剩下的部分也剥掉**。

### 4.3 三条回复通道，分工必须明确

| 通道 | 机制 | 特点 |
|---|---|---|
| `aibot_respond_msg`（流式） | WS 发帧，`headers.req_id` **透传回调里的那个**，body `{"msgtype":"stream","stream":{"id":…,"finish":false,"content":…}}` | **一个气泡装下 ack 和正文**（正文用同一个 `stream.id` + `finish:true` 覆盖 ack）。只对**这一次回调**有效，24 小时内可回 |
| `response_url` | HTTP POST `{"msgtype":"markdown","markdown":{"content":…}}` | **一次性、有时效**。只留给控制指令（`/status` 等）和「忙碌中」这类一次性回执 |
| `aibot_send_msg` | WS 发帧，body `{"chatid":…, "chat_type":1|2, "msgtype":"markdown", …}` | 主动推送。**流式气泡不可用时的兜底**（超过 20480 字节的第 2 片起、超过 10 分钟窗口、平台拒收） |

> **为什么 ack 不能走 `response_url`**（我们最初的写法，后来改掉）：
> ack 和正文走两条通道 ⇒ 手机上就是**两条独立气泡**，用户看到「收到，处理中…」
> 会以为"还有一个对话在跑"，而且那条气泡永远不会消失。用流式发 ack，正文覆盖它，才是一个气泡。
> 细节与退化路径见 **4.4**。

`chat_type`：**单聊 = 1，群聊 = 2**。
🚨 **单聊回调里 `chatid` 是空的**，必须用 `from.userid` 当推送目标（`chat_type` 仍填 1）。

> **实测（2026-10-27 20:48）**：漏了这一条时，表现为——
> `response_url` 那句「收到，处理中…」**正常送达**（用户以为它在干活），
> 模型也确实跑完了（`rc=0`，5.9s），但推送那一步打的是
> `[send] 无 chatid，跳过推送` ⇒ 用户在手机上**永远停在"收到，处理中…"**。
> 识别信号很干净：**"收到"有、结果永远没有** ⇒ 先看推送目标是不是空的，
> 别去怀疑模型或超时。

顺带一个好处：单聊因此天然有**独立会话**（session 键是 `(chatid, userid)`，
单聊的 chatid 就是 userid，和任何群 chatid 都不相等）⇒ 私聊里开的是新对话窗口，
但 `cwd`、`CLAUDE.md`、项目 memory 与群聊**共用同一套**。
`/new` `/stop` `/status` 在单聊里同样可用。

### 4.4 流式消息：让 ack 和正文共用一个气泡

```jsonc
// 第 1 帧：建气泡 + 显示 ack
{ "cmd": "aibot_respond_msg",
  "headers": { "req_id": "<回调里的 req_id，原样透传>" },
  "body": { "msgtype": "stream",
            "stream": { "id": "自己生成的唯一 id", "finish": false,
                        "content": "收到，处理中…" } } }

// 第 N 帧：同一个 stream.id + finish:true ⇒ 这条气泡**被覆盖**成正文
{ "cmd": "aibot_respond_msg",
  "headers": { "req_id": "<同一个 req_id>" },
  "body": { "msgtype": "stream",
            "stream": { "id": "<同一个 id>", "finish": true,
                        "content": "<完整正文>" } } }
```

三条**必须**知道的约束（来自官方文档 `path/101463`）：

| 约束 | 数值 | 越界的后果 |
|---|---|---|
| `stream.content` | ≤ **20480 字节**，且是**累积全文**（不是增量） | 超了被 `40058` 拒；切片策略见 6.6 |
| 流式窗口 | 首帧起 **10 分钟**内必须 `finish:true` | 超时平台**自动结束**，气泡不再可更新 ⇒ 正文只能另发一条 |
| `finish:true` 之后 | 该气泡**不可再更新** | 想补内容只能新开一条（`aibot_send_msg`） |

**两个实现要点（踩过就知道有多重要）：**

- 🚨 `req_id` 必须是**回调里的那个**。自己编一个平台不认，帧发出去没人回执。
- 🚨 发帧+等回执**不能放在 websocket 读线程上**：回执就在那条线程上收，同步等 = 自己把自己卡死
  （看门狗还会把健康连接判死）。ack 要放到 worker 线程里发。

**退化路径（一样都不能丢内容）**：

| 情况 | 行为 |
|---|---|
| 正文 ≤ 20480 且没超窗口 | 一个气泡，正文覆盖 ack |
| 正文 > 20480 | 切成多片：**第 1 片用 `finish:true` 顶替气泡**，其余 `aibot_send_msg`（编号接续） |
| 跑到 9 分钟还没结果 | 提前把气泡收口成「任务仍在运行，结果稍后单独发一条」，正文另发 —— **别让它永远停在"处理中"** |
| 气泡已关闭 / 超过窗口 / 平台拒收 | 整条改走 `aibot_send_msg`（另发一条） |
| 回执丢失（结果未知） | **不重发正文**（同 4.6 的约定），只补一条「结果未知」提示 |

### 4.5 🚨 内容上限 20480 字节，而且是**报错**不是截断

```
errcode 40058: body.markdown.content exceed max length 20480
```

实测：20,968 字节 → 被拒；17,968 字节 → 送达。

**好的一面**：平台会明确报错，内容不会"悄悄少一半"。
**坏的一面（更要命）**：见下一条。

### 4.6 🚨 `ws.send()` 成功 ≠ 送达

平台的判定是**异步回执**，而且 `aibot_send_msg` 的**回执帧没有 `cmd` 字段** ——
它落在你消息处理函数的"未知帧"分支里。如果你只看 `ws.send()` 的返回值，
**被拒的消息也会被记成"已发送"**。

⇒ 必须：发送后**等回执**（按 `errcode` 判定），再算送达。
⇒ 撞到 `40058` 再切成多片、**逐片等回执**；仍失败要明确告诉用户"有 N 段没送达"。
⚠️ **不要在发送前就按字数预切**（见 6.6）—— 3000 字的中文回答只有 9 KB，本来一条就读得完。

---

## 5. 消息流与状态机

```
收到 aibot_msg_callback
  → 解析目标：chat_type = 单聊1 / 群聊2；**单聊没有 chatid ⇒ 用 from.userid**（★ 见 4.3）
  → 记下 headers.req_id（★ 回复时原样透传）与 response_url
  → 按 msgid 去重（平台会重试！）
  → 校验 from.userid ∈ 白名单（不在则静默丢弃 + 记日志）
  → 控制指令？ /new /stop /status → 立即回（走 response_url）（★ 见 6.3）
  → 忙碌中？ → 回「忙碌中，本条已忽略」并丢弃（走 response_url）
  → 否则入队给单 worker：
       ① 用 req_id 发**流式首帧** `finish:false` «收到，处理中…»（★ 见 4.4）
       ② 取 session_id（无则新建）→ claude -p [--resume sid] --output-format stream-json --verbose
          读线程遇到工具调用 ⇒ 更新**同一个气泡**«⏳ 正在跑（12s · 第 3 步）🛠 读 xxx»（★ 见 6.9）
       ③ 解析 result / is_error / permission_denials
       ④ 更新 session_id → 同 stream.id `finish:true` 写正文（覆盖 ack）
          太长/超窗口/被拒 ⇒ 切片或改走 aibot_send_msg（兜底，内容不丢）

收到 aibot_event_callback（template_card_event，用户点了卡片按钮）
  → 取 event_key（按钮标识）与 headers.req_id
  → 5 秒内用 `aibot_respond_update_msg` 把那张卡**就地改写**（★ 见 6.10）
```

---

## 6. 必须自己实现的 10 个机制

每条都写清「不这么做会怎样」—— 这些是最容易被跳过、也最容易在深夜炸掉的部分。

### 6.1 白名单（`from.userid`）

**不做的后果**：群里（或私聊开放时）任何人 @ 它都能在你电脑上执行命令。
`@` 只是路由，不是鉴权。这是整个系统里**唯一真正的权限边界**。

### 6.2 去重（`body.msgid`）

**不做的后果**：平台重试 / 网络抖动重发时，**同一条命令被执行两次**。
对读操作无所谓，对 `git push`、`rm`、写文件就是灾难。
用 `msgid` 做主键 + 保留 7 天即可。

### 6.3 忙碌语义：**丢弃，不排队**（本项目选择）

**不排队时**：处理中收到新消息 → 回「⏳ 忙碌中（已跑 N 秒），**本条已忽略**」，直接丢弃。

- 好处：**防输入轰炸**。长任务跑着的时候，用户连发十条不会变成十条排队任务。
- 代价：用户可能想把两条消息合并成一次请求 —— 所以提示里要写清怎么继续（等结束 / `/stop`）。

> 这两种语义没有对错，**取决于你的使用场景**：
> 设备控制类（"开灯/调亮"）适合丢弃 —— 堆积的相对指令执行两次是实打实的副作用；
> 批处理类适合排队。**选之前先想清楚你的操作是不是幂等的。**

**关键**：`/status` `/stop` `/new` 这类控制指令**必须走另一条即时通道**，
否则长任务跑着的时候，用户连「停」都发不进去。

### 6.4 超时 = 「结果未知」，不是「失败」

**不做的后果**：用户在真失败与"可能已执行"之间无法区分，于是盲目重试 → 重复执行。

### 6.5 杀进程树

**不做的后果**：`terminate()` 只杀父进程，Claude 拉起的子进程留在那里继续跑、继续占资源。

### 6.6 分片：**先整条发**，只有被 `40058` 拒了才切；切的时候按 UTF-8 字节

**为什么先整条发**：上限是**字节**不是条数。先把整条发出去，绝大多数回答（包括
"3000 字"这种听起来很长的）一条就读完了；预先按字数切反而把一条完整回答拆成
`(1/2)(2/2)`，读起来碎，还平白多一次失败机会。

**不按字节切的后果**：中文 3 字节/字，一条"7000 字"的消息实际是 21000 字节 →
被 `40058` 拒掉，而且你会以为"7000 < 20480 所以没问题"。

实现要点（流式版本，见 4.4）：
1. 先**整条**塞进流式气泡（同一个 `stream.id` + `finish:true`，等回执）。
2. 回执 `errcode == 40058` ⇒ 才按字节切成多片：**第 1 片顶替气泡**（也是 `finish:true`），
   其余用 `aibot_send_msg` 逐片发。
3. 某片**仍**被 `40058` 拒 ⇒ 按**实际字节**对半再发（按粒度再切等于没切）；限个 2~3 层。
4. 有的段没送达 ⇒ **明确告知**"有 N 段没送达"，别让用户把残篇当全文。
5. 切的时候段落优先、**不要切开一个字符**、每片带 `(i/N)` 编号（丢了哪片一眼看出来）。
6. 第 1 片"结果未知"时 ⇒ **不重发第 1 片**（可能已经进气泡了），只补第 2 片起，并把话说清楚。

### 6.7 上下文余量提示（3.4 的实现）

`/status` 里、或者每条回复尾部加一行 `📊 上下文 12.3% (123k/1.0M)`。
**成本近乎为零**（进程内拼字符串，不额外调模型），但能让你提前知道什么时候该 `/new`。

### 6.8 常驻化：自启 / 单实例 / 自愈（Phase 4）

**单实例保护是必需的**：自启 + 手动启动（或跑一次发送测试）同时存在 ⇒ 两条长连接、
同一条消息回两遍、两个 claude 抢同一个工作目录。做法：命名互斥量
（Windows `CreateMutexW`，`ERROR_ALREADY_EXISTS` = 已有实例）。**别用锁文件** ——
进程被强杀会留下脏锁。

**自启用任务计划程序，不用启动文件夹**：无窗口（`pythonw.exe`）、不需要管理员、
可加兜底触发器。XML 里几条不写就白干的：

| 字段 | 值 | 不写的后果 |
|---|---|---|
| `DisallowStartIfOnBatteries` | `false` | 笔记本**用电池时根本不启动** |
| `StopIfGoingOnBatteries` | `false` | 一拔电源就被杀掉 |
| `ExecutionTimeLimit` | `PT0S` | 默认 3 小时/3 天把常驻进程收掉 |
| `MultipleInstancesPolicy` | `IgnoreNew` | 与互斥量双保险 |

🚨 **「失败后重启」（`RestartOnFailure`）对"进程被强杀"不可靠** —— 实测杀掉后等 95 秒
任务仍是"就绪"、没重启。**可靠的做法是重复触发器**：`TimeTrigger` + `Repetition`
每 3 分钟敲一次门，配 `IgnoreNew`（活着时零副作用，死了就在 3 分钟内被拉起）。
配套：**"已有实例在跑"要返回 0**，否则每次敲门都被记成任务失败。

**看门狗盯两件事**（都在旁路线程，主线程看不见它们死没死）：

1. **线程活性**：worker / 心跳线程 `is_alive()`，死了就重启并**主动告知用户**
   （否则表现为"连着、也在发心跳，但消息再也不会被处理"这种静默失效）。
2. **连接惰性**：多久没收到**任何帧**。阈值必须先量 —— 我们实测平台对每个 ping 都回
   一个 83 B、无 `cmd` 字段的帧，间隔 30 秒 ⇒ 定 90 秒（连丢 3 个心跳）。

**电源**：`SetThreadExecutionState(ES_CONTINUOUS | ES_SYSTEM_REQUIRED)`，可以阻止空闲睡眠，
但**不阻止关屏**，也**挡不住合盖睡眠**（那要在电源选项里把"关闭盖子时"设成"不采取操作"）。
⚠️ 这个 API 是**按线程**生效的 —— 调用它的线程一退出，状态就没了，所以要有一个常驻线程持有它。

#### 合盖睡眠：实测到的一整条链路

合盖 = 睡眠时，**长连接必然断**，这个程序层面绕不过去，只能改系统设置：

```
设置 → 系统 → 电源和电池 → 盖子、电源和睡眠按钮控件
  「已接通电源」和「使用电池」是**两组独立设置**，都要改
  （实测：只把"已接通电源"改成"不执行任何操作"，用电池合盖**仍然睡眠**）
```

改之前、改之后的实测对比（2026-10-27，同一台机器）：

| 现象 | 日志证据 |
|---|---|
| 睡眠期间进程**冻结**，零日志 | 18:15:27 → 19:16:35 之间没有任何行 |
| 唤醒后**DNS 还没准备好** | `getaddrinfo failed`，有一次连续失败了 **4 分钟**（18:11:51–18:15:27） |
| DNS 恢复后**自动重连**，退避重置 | `20:30:41 connected` + `subscribe ok` + `退避重置 30s -> 3s` |
| 唤醒后照常处理消息 | `20:31:38` 收到并回完一条，`errcode=0` |

⇒ **唤醒不需要人工干预**，退避封顶 30 秒会一直重试，网络一好就自己连上。

📌 **睡眠期间发的消息**：用户实测「开盖后会自动回复合盖时没回的那些」。
日志里确实在重连后 57 秒收到了一条回调。⚠️ 但**「平台重投」还是「用户恰好在重连后才发」没有隔离验证**
—— 不管是哪种，因为按 `msgid` 去重，重投也不会被重复执行。

**日志**：按天一个文件 + 启动时删掉超期的。注意 `pythonw` 下 `sys.stdout` 是 `None`，
加 `StreamHandler(sys.stdout)` 前必须判空。

### 6.9 真进度气泡：读 `stream-json`，把工具调用变成人话

**为什么需要**：一个任务跑 40 秒，气泡里只有「收到，处理中…」= 用户以为它卡死了。
「已跑 40 秒」也是假的 —— 它只证明进程还活着，不证明在干活。

**做法**：`--output-format stream-json --verbose`（§3.9），读到一次工具调用就更新一次气泡：

```
⏳ 正在跑（12s · 第 3 步）
🛠 读 app/bridge.py
```

四条实现要点：

1. **只推"变化"，再叠一个 60 秒兜底心跳。** 平台限流是 **30 条/分钟**，一条长任务可能
   几十次工具调用 —— 每次推一次会把额度打满，真正的回复反而发不出去。
   判据：`(工具名, 目标)` 和上一次相同就不推；连续 60 秒没变化才推一次"还活着"。
2. **改的是同一个气泡**（同一个 `stream.id`，`finish:false`），不是新发消息。
   否则聊天里会变成一串进度条。
3. **工具描述要翻成人话**：`Read → 读 <路径>`、`Bash → <它自己写的 description>`、
   `Grep → 搜 <关键词>`、未知 → `用 <工具名>`。直接把 JSON 贴给用户等于没写。
4. **进程异常退出时不要重跑。** 命令可能已经执行了一半。正确做法是**把原始流落盘**
   （`logs/raw-stream/*.jsonl`），回一句「结果未知 + 跑了多久 + 执行了几次工具」，
   让用户自己判断。这是 §6.4 那条"超时 = 结果未知"的同一条原则。

> ⚠️ 气泡更新是**尽力而为**：它失败了（回执超时、平台抖动）不能影响任务本身，
> 也不能让正文丢掉。正文必须走独立的一次 `finish:true`。

### 6.10 审批卡片：让"全自动"多一道人肉闸

> 🚨 **先说清楚它不是什么**：它**不替代 `dontAsk`**。`PreToolUse` hook 返回 `allow`，
> `dontAsk` 档下命令照样不执行（§3.5 第 4 条）—— 档位永远优先。
> 它是"**已经决定全自动**之后"的第二道闸，不是"让全自动变安全"的东西。

#### 权限怎么切换（这是本项目最常被问的一件事）

| 形态 | 配置 | 效果 |
|---|---|---|
| 只读（默认） | `CC_PERMISSION_MODE=dontAsk` | 命令 / 写文件一律被拒 |
| 全自动 | `CC_PERMISSION_MODE=bypassPermissions` | 直接执行，不弹确认 |
| **逐次询问** | `bypassPermissions` + `CC_APPROVAL=1` | 有副作用的调用先弹「允许 / 拒绝」卡 |

⚠️ **没有"聊天里改档位"这种指令**，也不该有 —— 档位是"这台机器交给谁"的边界，
放一个聊天指令进去，等于让任何能发消息的人改它。**改 `.env` + 重启**才是切换方式；
`/status` 卡片上的「档位」一行只负责显示**实际生效**的值。

#### 形状：两个进程 + 一个目录

```
桥（Python）                      claude 子进程
   │ 启动时注入 CC_APPROVAL_DIR      │ 每次工具调用前跑 .claude/settings.json 里的
   │                                 ▼
   │                         python tools/approval_hook.py   ← 独立进程，~100-200ms
   │                                 │ 写 <dir>/<id>.req.json（工具名 + 入参 + cwd）
   │ ◄── 扫目录（0.3s 一次）──────────┤ 等 <id>.decision（最多 120s）
   │ 发「允许 / 拒绝」模板卡片          │
   │ 用户点了 → 写 decision ──────────►│ 读到 decision ⇒ 打印 permissionDecision
   │                                 ▼
   │                            allow ⇒ 执行；deny ⇒ 拒绝
```

**为什么否决权必须握在 hook 手里**（这是这一节最重要的实测结论）：

| 异常路径 | harness 的实际行为 |
|---|---|
| hook 超时被截断 | 🔴 **放行** |
| hook `rc=1` / 抛异常 | 🔴 **放行** |
| 输出不是合法 JSON | 🔴 **放行** |
| `permissionDecision` 字段名拼错 | 🔴 **放行** |

⇒ **hook 自己算 deadline（本项目 120 秒），到点自己 `deny`**；harness 的 `timeout: 300`
只当**兜底**，永远比 hook 的 deadline 长，这样"做决定的永远是我们自己"。
指望 harness 的超时替我们拒绝，就是给自己造一个假闸门。

#### 五个工程细节（都是踩出来的）

1. **`matcher` 必须覆盖所有工具**（写 `"*"` 或不写）。只拦 `Bash` 没有意义 ——
   `Write`/`Edit` 同样能改文件。实测 `"*"` 确实覆盖到 `Write`。
2. 🚨 **模板卡片的按钮文字只有 2 个字的余量**：实测写 4 个字会被截成「清...」/「清上...」
   （手机和 PC 截断位置还不一样）。语义写进 `sub_title_text`，别指望按钮放得下。
3. **卡片发不出去就立刻 `deny`**，不要"发不出去所以先放行"。这是 fail-closed 的落点。
4. **只读工具直接放行**（`Read`/`Grep`/`Glob`/`TodoWrite`）。一条读文件的链路上可能十几次
   工具调用，全弹卡会**瞬间打满 30 条/分钟**，而且用户会疯。
5. **点击后要把卡片就地改掉**：`aibot_respond_update_msg` + 点击事件里的 `req_id`，
   **5 秒内**必须发出，把 `button_interaction` 改成无按钮的 `text_notice`（实测平台接受）。
   ⚠️ 平台限制：**没人点过的卡片改不了**（没有点击事件就没有 `req_id`），那种只能留在聊天里。

#### 已知取舍

- **一次工具调用一张卡**：长任务会连弹。「点过一次就把这一轮同类调用都放行」**没做** ——
  先看真实使用到底多烦，再决定值不值得。
- **每次工具调用多起一个 python 进程**（本机约 100~200ms）。这是 hook 机制的固有成本。
- **`CC_APPROVAL=0` 时 hook 是空壳**：立刻 `exit 0`、什么都不做 ⇒ `.claude/settings.json`
  可以安全地随仓库发布，别人不启用时它只是个 ~150ms 的摆设。
- ⚠️ `.claude/settings.json` 里**不要写 `//` 注释键**：校验失败会被**静默忽略**
  （`-p` 模式不弹提示），表现是 hook 悄悄消失。

---

## 7. 最小骨架（伪代码，不绑定具体实现）

```python
# —— 概念示意 ——
bot = WeComBot(bot_id, secret)                  # 长连接：subscribe + 心跳 + 退避重连

@bot.on_message
def handle(msg):
    if msg.msgid in seen: return                # 去重
    if msg.from_user not in ALLOW: return       # 白名单
    text = strip_at(msg.text)                   # 去掉 "@机器人 " 前缀

    if text.startswith("/"):                    # 控制指令：即时处理（response_url）
        return control(text, msg)

    if worker.busy:                             # 忙碌语义：丢弃
        return bot.reply(msg, "⏳ 忙碌中，本条已忽略")
    queue.put({**msg, "req_id": msg.headers.req_id})   # ★ req_id 要带进 worker
    # ⚠️ 这里**不要**回「收到」—— ack 由 worker 用流式首帧发，正文才能覆盖它

def worker_loop():
    msg = queue.get()
    st = Stream(req_id=msg.req_id)               # 一条气泡装下 ack + 正文
    st.send("收到，处理中…", finish=False)        # ★ 必须在 worker 线程发（要等回执）

    sid = db.get_session(msg.chatid, msg.from_user)
    # 真进度：读到工具调用就更新**同一个气泡**（变化才推；见 6.9）
    # 审批：把决策目录注入子进程环境，hook 才会干活（见 6.10）
    out = run_claude(text=msg.text, resume=sid, cwd=CC_CWD, timeout=600,
                     on_progress=lambda d: st.send(progress_text(d), finish=False),
                     extra_env=approval_env(chatid, msg.from_user))
    if out.session_id: db.save_session(..., out.session_id)
    if out.timed_out:   reply = "⏱ 超时：结果未知"
    elif out.is_error:  reply = "❌ " + out.result
    else:               reply = out.result + usage_footer(out)

    r = st.send(reply, finish=True)              # ① 正文覆盖 ack（等回执）
    if r.ok or r.unknown: return                 # 未知 ⇒ 不重发（见 4.6）
    if r.dead or r.errcode != 40058:             # ② 气泡不可用 ⇒ 整条推送
        return push_with_chunking(reply)
    parts = chunk_by_bytes(reply, 18000)          # ③ 超长：第 1 片进气泡，其余推送
    st.send(f"(1/{len(parts)})\n{parts[0]}", finish=True)
    for i, part in enumerate(parts[1:], 2):
        send(f"({i}/{len(parts)})\n{part}")       # 每片都等回执；仍 40058 就对半
```

---

## 8. 给 AI Agent 的自包含实现契约

> 把从这一行到本节末尾整段复制给你的 agent，它就能照着实现。

你要实现一个「**企业微信 ⇄ 本机 Claude Code**」的桥进程。请严格遵守以下契约。

**进程结构**

1. 一个 WebSocket 长连接，主动连 `wss://openws.work.weixin.qq.com`。
2. 一个**心跳线程**（30s 一次 ping）——全进程**只许一个**。
3. 一个**单 worker 线程**消费任务队列。**绝不要在 WebSocket 回调线程里同步跑 Claude**（会把连接卡死）。
4. 一个 SQLite 库：`sessions(chat_id, user_id, session_id, cwd, …)` 和 `seen_msg(msg_id, seen_at)`。

**收到消息时，严格按这个顺序**

1. `body.msgid` 去重（重复则直接丢弃）。
2. 校验 `body.from.userid` 在白名单内（不在则**静默丢弃并记日志**，不要回复）。
3. 剥掉 `text.content` 开头的 `@…` 前缀（注意平台可能只写"@+第一个词"）。
4. 是 `/new` `/stop` `/status`？**立即处理，不要入队。**
5. 忙碌中？回「⏳ 忙碌中，本条已忽略」，**丢弃**，不入队。
6. 否则放入队列，并把 `headers.req_id` 一起带进去（回复时要原样透传）。
   **不要在回调线程里回「收到，处理中…」** —— ack 由 worker 用流式首帧发（见下）。

**跑 Claude 时**

```bash
claude -p --output-format stream-json --verbose \
  --setting-sources project,local \
  --permission-mode <档位> \
  --model <模型> \
  [--resume <session_id>] \
  [--disallowedTools "Read(**/.env)" …]
```
（`stream-json` + `--verbose` = 要真进度，见 §3.9；不想要进度就用 `--output-format json`，
其余参数完全一样。）

提示词从 **stdin** 传。必须：剥掉继承的 `ANTHROPIC*`/`CLAUDE*` 环境变量、按需注入认证、
设总超时、超时用 `taskkill /T /F` 杀**进程树**。
用 `stream-json` 时**三条线程**分别读 stdout / stderr / 写 stdin，主线程只管 `wait()`。

**判结果**

- **只信 `is_error`**。不要用 `subtype`（失败时它也报 `success`），不要用「有没有 `session_id`」。
- `permission_denials` 非空 → **必须告诉用户被拦了什么**，不许假装成功。
- 超时 → 回「结果未知」，不许回「失败」。
- `--disallowedTools` 造成的拦截**不会**出现在 `permission_denials` 里。

**发结果时**

- **ack 和正文共用一个流式气泡**：`aibot_respond_msg` + `body.msgtype="stream"`，
  `headers.req_id` **透传回调里的那个**，`stream.id` 自己生成；先 `finish:false` 显示
  「收到，处理中…」，结果到了用**同一个 `stream.id`** + `finish:true` 覆盖。
  **不要**用 `response_url` 发 ack（那样会出现两条独立气泡）。
- ⚠️ 流式窗口 **10 分钟**、单帧 content **≤ 20480 字节**；`finish:true` 之后不可再更新。
  到 9 分钟还没结果 ⇒ 先把气泡收口成「仍在运行，结果稍后单独发一条」，别让它停在"处理中"。
- `response_url` 一次性 ⇒ 只给 `/status` 这类一次性回执用；`aibot_send_msg` 是**兜底**主动推送。
- `chat_type`：单聊 1 / 群聊 2。单聊缺 `chatid` 时用 `from.userid`。
- 单条内容 **≤ 20480 字节**（UTF-8）；超了平台回 `errcode 40058`。
- **`ws.send()` 成功不等于送达**：必须等异步回执的 `errcode==0` 才算送达。
  等回执**不可以在 websocket 读线程上做**（回执就在那条线程上收）。
- **先整条发**；只有回执是 `40058` 才按**字节**切成多片（带 `(i/N)`），单片仍被拒就对半再切。
  **不要预切** —— 上限是字节不是条数。某段没送达要明确告知用户。

**回复用户时的文案要求**

- 被权限拦、超时、出错 —— 三者要**区分开**，且都要说人话。
- 「结果未知」必须写清「重试前先核实」，不要诱导用户盲目重发。

**可选增强（本仓库已实现，不做也能用）**

| 增强 | 一句话要求 | 细节 |
|---|---|---|
| 真进度气泡 | 从 `stream-json` 的工具调用更新**同一个**气泡；**变化才推** + 60s 兜底心跳 | §3.9 / §6.9 |
| `/status` 卡片 | `button_interaction` 模板卡；⚠️ 按钮文字**最多 2 个汉字** | §6.10 |
| 审批卡片 | `PreToolUse` hook + 目录协议；**否决权在 hook 自己手里**（harness 超时全是放行）；**不替代 `dontAsk`** | §6.10 |
| 卡片就地更新 | 点击后 **5 秒内** `aibot_respond_update_msg` 改掉那张卡；**没人点过的改不了** | §6.10 |

---

## 9. 验收清单

| # | 测试 | 通过判据 |
|---|---|---|
| 0 | 装得上 | 照「开始之前」四步走完：`import requests, websocket` 无报错、`verify_headless.py all` 无 fail、日志出现 `subscribe ok` |
| 0b | 白名单 fail-closed | `WECOM_ALLOWED_USERIDS` 留空启动 → **拒绝启动**并打印原因（不是"默默对所有人开放"） |
| 0c | 默认只读 | `.env` 不动 `CC_PERMISSION_MODE`（= `dontAsk`）时，让它「删掉某个文件」→ **被拒**，且回复里能看懂为什么 |
| 1 | 订阅与心跳 | 日志 `subscribe ok`；断网后能重连，且退避重置为 3s |
| 2 | 基本往返 | 发「你好」→ 「收到，处理中…」**变成**结果（**同一个气泡**，不是两条） |
| 2b | 流式合并 | 私聊与群聊各发一次 → 都只有一条气泡；日志里 `aibot_respond_msg` 的 ack/定稿 `stream.id` 相同 |
| 2c | 超长仍可读 | >20480 字节 → 第 1 片在气泡里、其余几条接续，编号连续、能拼回全文 |
| 3 | 会话连续 | 第二轮问"我刚才说了什么"→ 答对 |
| 4 | 白名单 | 非白名单账号发消息 → **无回复**、日志有记录 |
| 5 | 去重 | 同一个 `msgid` 投两次 → 只执行一次 |
| 6 | 忙碌语义 | 长任务跑着时再发 → 回"忙碌中"，且**事后不会冒出**对第二条的回答 |
| 7 | 控制指令 | 忙碌时 `/status` `/stop` **仍然可用** |
| 8 | `/new` | 清上下文后，下一轮**明确不知道**上一轮 |
| 9 | 超时 | 设短超时跑长任务 → 杀进程树、回「结果未知」 |
| 10 | 超长内容 | 发 >20480 字节 → **先撞 `40058`**，再自动切成多片、顺序正确、编号连续 |
| 11 | 不预切 | 发 19 KB（≈6400 汉字）→ **必须是单条**，不能出现 `(1/2)` |
| 12 | 凭据 | `git status` 看不到 `.env`；日志里没有 secret |
| 13 | hook 隔离 | 桌面宠物之类的 UI 工具启动后，headless 任务**不会被卡住** |
| 14 | 单实例 | 常驻实例在跑时再启动一个 → 第二个**自己退出**、日志说明原因，**不会双份回复** |
| 15 | 自启 | 装完任务后 `schtasks /Run` ⇒ 出现无窗口的 `pythonw` 进程并 `subscribe ok` |
| 16 | 自愈 | 强杀进程 → **3 分钟内**自动拉起（靠重复触发器，不靠 `RestartOnFailure`） |
| 17 | 不重复拉起 | 进程正常运行时，重复触发器命中不产生新进程（`IgnoreNew`） |
| 18 | 睡眠→唤醒 | 合盖睡眠后唤醒：无需人工干预，DNS 一恢复就自动重连并处理消息（退避封顶 30s） |
| 19 | 电源设置 | 「已接通电源」与「使用电池」两组都要把"关闭盖子时"改成"不执行任何操作"，否则电池下仍会睡 |
| 20 | 私聊 | 直接私聊（不 @）→ 能收到结果；且私聊的上下文与群聊**互不串**（各自独立 session） |
| 21 | 私聊控制指令 | 私聊里 `/status` `/new` `/stop` 同样可用 |
| 22 | `/status` 卡片 | 发 `/status` 出一张按钮卡；点「刷新」卡内数据更新、点「清空」下一轮忘记上文 —— **两个都当场生效** |
| 22b | 卡片布局 | 按钮文字**两个字**完整显示（实测 4 个字会被截成「清...」） |
| 23 | **真进度** | 让它跑一个 30 秒以上的任务 → 气泡里出现 `⏳ 正在跑（Ns · 第 k 步）🛠 读/运行 xxx`，**并且是同一个气泡**，不是一串新消息 |
| 23b | 进度不刷屏 | 同一段任务里，工具没变化时不产生新推送（看日志里 `respond_msg` 的条数明显少于工具调用次数） |
| 24 | 审批卡片弹得出来 | `bypassPermissions` + `CC_APPROVAL=1`，让它执行一条 `Bash` → 聊天里出现「允许 / 拒绝」卡；**不点它就一直等**（hook 120 秒后自动拒绝） |
| 24b | 允许真的执行 | 点「允许」→ 命令真的执行了；日志里 `[approval] 决定=allow` |
| 24c | 拒绝真的不执行 | 点「拒绝」→ 命令**没有**执行（判据是副作用，不是它怎么说） |
| 24d | 卡片就地更新 | 点击后 **5 秒内**卡片变成「已允许 / 已拒绝」且**按钮消失**（日志 `卡片已就地更新`） |
| 24e | 只读不弹卡 | 让它读一个文件 → **不弹卡**、直接返回（只读白名单生效） |
| 24f | 非发起人点无效 | 换一个白名单内的其他账号点同一张卡 → 被拒，命令不被执行 |
| 24g | 🚨 不替代只读 | `CC_PERMISSION_MODE=dontAsk` + `CC_APPROVAL=1` 时点「允许」→ 命令**仍然不执行**（档位优先）。这条用来确认你**没有**误以为审批能让只读变全自动 |

---

## 10. 发布前红线

- ✅ **无凭证**：BotId / Secret / token / `ANTHROPIC_AUTH_TOKEN` 一律占位符。
- ✅ **无真实 id**：真实 `chatid`、`userid` 用 `<YOUR_USERID>` 这类占位符。
- ✅ **无本机路径**：别把 `C:\Users\<你的名字>\…` 原样发出去。
- ✅ **不塞整个配置文件**：只给脱敏示例片段。
- ✅ **日志与台账不进公开仓**：`logs/`、`*.db`、原始回调、私有的决策记录都应 gitignore。
- ✅ **截图打码**：群名、成员昵称、机器人头像、二维码。
- ⚠️ **图片要多层检查**：EXIF、XMP、以及**文件里是不是塞了不止一张图**（手机相册"重新编辑"常常
  把原图完整留在同一个 `.jpg` 里 —— 你涂掉的内容可以被挖出来）。
- ⚠️ **`.env` 一旦推上去不可撤销**（已进缓存与可能的 fork，删了也没用）。
  **建仓第一件事就是 `.gitignore`，第一段就是密钥排除。**
- ⚠️ **对外版本的默认值必须是收紧的**：`CC_PERMISSION_MODE` 默认 `dontAsk`、
  白名单留空即拒启动。**别把自己机器上调好的 `bypassPermissions` 当默认值发出去** ——
  那等于给陌生人一个"把这台机子交给群里任何人"的模板（§3.5 的实测表说明，
  除了 `plan`/`dontAsk`/`manual`，其它档位在 headless 下都会真的执行命令）。
- 🚨 **别把审批卡片宣传成"安全机制"**：它**不替代 `dontAsk`**，只在 `bypassPermissions`
  下有效（§6.10）。写文档时如果让人以为"开了审批就能安全地全自动"，那是我们在
  **制造一种虚假的安全感** —— 比不提供这个功能更糟。

---

## 附录：错误码速查（本项目遇到过的）

| errcode | 含义 | 处理 |
|---|---|---|
| `853000` | `invalid bot_id or secret` | 后台重新获取 Secret；确认是**智能机器人**的 Secret，不是自建应用的 CorpSecret |
| `40058` | `body.markdown.content exceed max length 20480` | 先整条发；被它拒了才按 **UTF-8 字节**分片 |
| `Not logged in`（非 errcode） | 排掉 user 设置后没自带认证 | 把 `ANTHROPIC_*` 注入子进程环境变量 |

> 更多错误码：`https://open.work.weixin.qq.com/devtool/query?e=<errcode>`

---

## 许可

本项目（代码与本文档）采用 **Apache License 2.0** —— 全文见仓库根目录的
[`LICENSE`](../LICENSE)，版权声明见 [`NOTICE`](../NOTICE)。
文中引用的企业微信能力以其官方文档为准。
