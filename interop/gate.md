# 闸门的通用入口：`tools/gate_cli.py`

**给谁看：要把自己的 harness 接到这套审批上的，以及将来接第五个 harness 的人。**

先说结论：**这个仓里只有"触发点"绑 harness，判定不绑。**

```
触发（每个 harness 自己的）        判定（本仓，一份）
  Claude PreToolUse  ─┐
  DSH pre-execute    ─┼→ gate_cli → 凭据硬 deny → 免卡白名单 → 只读分类 → 发卡等点击
  下一个 harness     ─┘
```

---

## 一、调用契约

```text
入（stdin，一行 JSON）
  {"tool_name": "Bash",
   "tool_input": {"command": "git status"},
   "cwd": "C:/work/项目"}

出
  放行 → stdout 空，退出码 0
  拒绝 → stdout 一行 {"permissionDecision":"deny","reason":"…"}，退出码 2
```

入参的键**认常见别名**，不必为此改你们那边的结构：

| 位置 | 认这些键 |
|---|---|
| 工具名 | `tool_name` / `tool` / `name` |
| 参数 | `tool_input` / `input` / `arguments`（值是 JSON 字符串也认） |
| 工作目录 | `cwd`（缺了用进程当前目录） |

⚠️ **调用方必须把「非 0 退出 / stdout 解析不了 / 进程起不来」一律当成拒绝。**
我们这边所有意外路径都汇到 `deny`，但如果执行方把异常当放行，闸门就只是装饰 ——
"没跑起来"和"跑了但放行"在调用方看来一模一样，这一点对任何 harness 都成立。

环境变量与 [`tools/approval_hook.py`](../tools/approval_hook.py) 完全一致：
`CC_APPROVAL_DIR` 决定**有没有卡片能力**（不设 = 只有静态安全过滤）。

---

## 二、工具名映射：宁可少，不要猜

判定层只认识 `Bash` / `Read` / `Write` / `Edit` 这些名字。别家的叫法要在**入口**翻译：

| 你们的叫法 | 映射成 | 依据 |
|---|---|---|
| `read_image` / `view_image` / `read_file` | `Read` | 语义相同：按路径读 |
| 其它（`shell`、`fetch_url`…） | **不内置**，用 `GATE_TOOL_MAP` 自己补 | 见下 |

```bash
GATE_TOOL_MAP="shell:Bash,fetch_url:WebFetch" python -X utf8 tools/gate_cli.py
```

**没映射的名字不会被"猜"成某个安全工具** —— 它会走"要弹卡"那一支（保守），
而不是被塞进只读白名单里放过去。这也是为什么内置表只有三个。

---

## 三、一个已经验证过的实例

DSH 的 `tools/pre-execute` 把它的 `read_image` 映射成 `Read`，再进**同一套**分类器，
日志里是：

```text
[dsh][gate] read_image→Read ⇒ allow
```

同一套凭据硬 deny 也随之生效：无论工具叫什么，参数里出现 `~/.ssh`、`~/.dws`、
`.env` 这类路径，一律拒绝。

---

## 四、包装器 v1：让 CLI 自己经过闸门

`gate_cli` 解决的是"harness 愿意调我们"的情形。**不经过 harness 的调用**（人手敲、
别的脚本调）由包装器接住：`tools/gate_wrap.py`。

```text
data/gate-bin/dws          ← 裸名 shim（bash 只认这个）
data/gate-bin/dws.cmd      ← 后缀 shim（cmd.exe 只认这个）
        │
        └→ python tools/gate_wrap.py -- dws <参数…>
                 ├─ 只读 → 直接执行真身（退出码原样透传）
                 ├─ 要写 → 发卡等点击（与 hook 同一份 ask_card）
                 └─ 没有 CC_APPROVAL_DIR → **拒绝**（126，理由在 stderr）
```

桥启动时生成这两个文件、把目录垫进 harness 的 PATH 最前，并注入
`CC_GATE_SHIM_DIR`。**hook 靠它分工**：

| 命令 | hook 怎么办 | 为什么 |
|---|---|---|
| `dws …`（PATH 解析到 shim） | **放行**，交给包装器 | 再判一次就是同一件事弹两张卡 |
| `C:\…\npm\dws.cmd …`（绕过 shim） | **拒绝** | 绕过之后没人判读写 —— 那正是要堵的缝 |

两个名字都要有，是因为 **bash 只认裸名、cmd.exe 只认 PATHEXT**；优先级靠**目录顺序**，
不是后缀顺序。所以"往真身同一个目录里塞一个同名 `.cmd`"永远轮不到它。

⚠️ v1 是**减速带**：真身还没改名（`GATE_REAL_BIN` 留给 v2），写全路径仍然到得了它 ——
这一条由 hook 的"绕过就拒"兜住。两条路都关着才算墙。

⚠️ **shim 会把自己的目录用 `--shim-dir` 传给包装器**，包装器据此跳过自己去找真身。
少了这一步、又恰好是手工垫的 PATH，就会解析回 shim 自己 ⇒ 无限递归
（2026-11-15 实测：一个 `dws --help` 起了几百层进程）。包装器另有 3 层深度保险丝。

---

## 五、状态

| 项 | 状态 |
|---|---|
| 入参归一 / 工具名映射 / 两种方言 | ✅ 离线单测 24 项（`tools/test_gate_cli.py`） |
| 凭据硬 deny / 免卡 / 弹卡等待 / 桥不在就拒 | ✅ 同上（判定复用 `approval_hook.py`，非另写一份） |
| 在 DSH 上改指本入口 | ⏳ **未接线实测** —— DSH 目前仍走它自己的 `pre-execute` |
| shim 生成 / 包装器判读写 / 拒绝时不执行 / 让路与反绕过 | ✅ 离线单测 31 项（`tools/test_gate_wrap.py`）+ 手工端到端 `dws --help` |
| 桥注入 PATH 后的**真机一轮**（读直跑、写弹卡） | ⏳ 待重启后实测 |