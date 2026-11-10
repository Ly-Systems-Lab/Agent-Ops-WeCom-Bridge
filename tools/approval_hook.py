#!/usr/bin/env python3
"""PreToolUse hook：把「需要审批」的工具调用交给桥，阻塞等用户在企微上点卡片。

为什么是这个形状（`DECISIONS` §25，全部实测）：
  · 本版 CLI 没有 `--permission-prompt-tool`，只剩 hook 一条路；
  · **harness 的 timeout 到点 = 放行（fail-open）**，hook 崩溃/输出垃圾也是放行
    ⇒ 否决权必须握在**这里**：自己算 deadline、到点自己拒绝，
      harness 的 `timeout`（`.claude/settings.json` 里写 300）只许比这里更**长**；
  · `matcher` 必须覆盖所有工具 —— 只拦 Bash 会被 Read/Write 绕过去。

协议（与 `app/bridge.py` 共享，两边都在 `DECISIONS` §25 里有记录）：

    <CC_APPROVAL_DIR>/<id>.req.json   ← 本脚本写：要审批的这次调用
    <CC_APPROVAL_DIR>/<id>.seen       ← 桥写：我看到了，卡片已经发出去了
    <CC_APPROVAL_DIR>/<id>.decision   ← 桥写：allow / deny

不设 `CC_APPROVAL_DIR` 时本脚本**不做审批**（不写请求、不等人）—— 这样同一个
`.claude/settings.json` 可以随仓库一起发布，别人不启用审批时它基本上是空壳。

**唯一的例外是「凭据存储硬 deny」**（P1-2，2026-11-10）：它不弹卡、不等桥、
不看审批开关，命中就直接拒绝 —— 所以没有 `CC_APPROVAL_DIR` 时它**照样生效**。
理由：那是"这一类路径永远不该被 Agent 读"，和"这次要不要问用户"是两件事。

**Bash 不是"一律弹卡"，而是按命令判**（P1-1，2026-11-10）：纯读命令直放，
其余照旧弹卡。判据与三道护栏见下面「只读白名单」那一节 —— 一句话：
**官方契约（`interop/dws_effects.json`）+ 本地护栏 + 人工标注，三者都过才直放。**
读不出来、认不出、拿不准，一律弹卡。

退出约定：`exit 0` = 放行；输出 JSON `permissionDecision=deny` = 拒绝
（实测 `exit 2` 也能拒，但理由会被裹上 "hook error"，JSON 更干净）。
"""

import json
import os
import re
import shlex
import shutil
import sys
import time
import uuid

# ⚠️ 必须钉 UTF-8：本机控制台是 GBK，不钉的话中文拒绝理由会以**乱码**交给模型
#    （实测踩过；与 P4 那边"抓串口脚本必须钉 UTF-8"是同一课）
try:
    # stdin must also be pinned to UTF-8: on Windows the pipe defaults to GBK,
    # so non-ASCII tool input decodes into lone surrogates and writing req.json
    # then fails ("surrogates not allowed"), which denies the call.
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 只读工具直接放行：一条长任务可能读十几次文件，全问会把 30 条/分钟打满，
# 而且每次工具调用都要起一个新进程、等一次人工点击，完全不可用。
DEFAULT_ALLOW = "Read,Grep,Glob,TodoWrite"
DEADLINE_SEC = 120          # hook 自己等多久（harness 那边给 300，必须比这个大）
SEEN_WAIT_SEC = 10          # 等桥"我看到了"的上限：桥没在跑就别干等两分钟


# ============================================================
# 只读白名单（P1-1）：`Bash` 不再"一律弹卡"，改成按命令判
#
# 起因（实测）：一轮 10 条工具调用 = 10 张卡，人点了 10 次，其中大半是
# `--help` / `schema` / 纯查询。这些命令不写数据，却和"创建待办"一样贵。
#
# 判据只有一条：**这条命令会不会改数据？** 三个来源，缺一不可：
#   ① 官方契约 —— `dws schema --all --compact` 给的 effect / confirmation
#      （快照在 `interop/dws_effects.json`，由 `tools/dws_effect_sync.py` 生成）；
#   ② 本地护栏 —— 命令替换、重定向、环境变量前缀、管道、多段拼接；
#   ③ 人工标注 —— `SENSITIVE_READ`：官方说 read，但读出来的是密钥。
#
# ⚠️ **三道都不满足就弹卡**（fail-closed）。表读不出来、命令不认识、
#    有个词拿不准 —— 一律弹卡，绝不"猜它是读"。
# ============================================================

# 执行面 CLI（basename，小写）。dws 有只读表；lark-cli 还没有（P1-4 再接）。
_PLATFORM_CLIS = {"dws", "dws.exe", "dws.cmd",
                  "lark-cli", "lark-cli.exe", "lark-cli.cmd"}

# 不联网、不改数据、只打印本地帮助的子命令
_LOCAL_SUBCOMMANDS = {"help", "schema", "version"}

# 管道下端允许出现的**纯过滤器**：只读 stdin、写 stdout，没有写文件的选项。
# `sort` 的 `-o` / `--output` 由 `_writes_file()` 单独挡。
_LOCAL_FILTERS = {"head", "tail", "grep", "wc", "jq", "cut", "tr", "sort"}

# 官方标注 effect=read，但**读出来的东西本身就是机密** —— 仍然弹卡，
# 卡面写明"会读出密钥"，让用户自己判断要不要允许。
# ⚠️ 这不是"拒绝"，也不是"黑名单"：用户点允许照常执行。
_SENSITIVE_READ = {
    "dev app credentials get": "客户端凭证（AppSecret）",
    "dev liteapp credential": "轻应用 OAuth 凭证（含 secret 明文）",
    "devapp +credentials-get": "开放平台应用凭证",
    "dev mcp credential get": "MCP 凭证账号详情",
    "dev mcp credential list": "MCP 凭证账号列表",
    "dev mcp auth get": "MCP 下游鉴权配置",
    "aitable supabase api-key list": "SQL Sheet API Key 元数据",
}

# 命令替换与重定向：出现任何一个，这条命令就不再是"纯读"。
# ⚠️ 检查的是**单引号之外**的原文：
#   · 单引号里一切是字面量 ⇒ 可以安全忽略；
#   · **双引号里 `$()` 和反引号照样执行** ⇒ 必须算进来（踩过：只查引号外会漏）；
#   · 单独的 `&` 是"后台再跑一条命令" ⇒ 和 `;` 等价，必须拦。
_HAZARDS = ("$(", "`", ">", "<", "&")
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_EFFECTS = None            # {"cli_path": {...}}，进程内只读一次
_EFFECTS_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             "..", "interop", "dws_effects.json")


# ============================================================
# 凭据存储硬 deny（P1-2）：CLI 的令牌落盘位置，**任何工具、任何用户**都不许读
#
# 为什么是「硬拒绝」而不是「弹卡」：
#   · `dws` / `lark-cli` 的令牌是**别人替我们保管**的，Agent 没有任何正当理由
#     去读它们 —— 一张"要不要允许读令牌"的卡，正确答案永远是"不要"，
#     把它做成卡只会变成一个可以被点错的按钮；
#   · 而且这块**必须与审批开关无关**：免卡白名单里的用户（P1-3）能跳过的
#     只是"点一下"，不该跳过"这条不能读"。所以它排在所有开关之前。
#
# ⚠️ **路径一律在运行时算，源码里不出现任何用户名/绝对路径** ——
#    这个文件要能原样进公开仓。
# ⚠️ `~/.lark-cli` 目前是**占位**：lark-cli 本机还没装（2026-11-10 实测
#    `which lark-cli` 为空），真实落盘位置等 P1-4 接上后按实测修正。
# ============================================================

# 拒绝理由是**固定一句**，不带路径、不带命令原文：
# 模型看到的就是"这条路走不通"，不给它拼出正确路径的线索。
CRED_DENY_REASON = "这是凭据存储，安全策略禁止访问。"

_HOME_STORES = (".dws", ".lark-cli")          # 用户主目录下的 CLI 数据目录
# 主目录下**同样是凭据**、但不属于上面两个 CLI 的位置（2026-11-11 补）：
#   `.claude/`、`.claude.json` —— CC 自己的配置与会话记录，里面是明文 token
#       和**对话全文**（跨会话可读 ⇒ 等于泄漏别人的聊天）。
#   `.ssh/` —— 私钥。
_HOME_SECRETS = (".claude", ".claude.json", ".ssh")
# 本仓自己的 CLI 凭据。**写成正斜杠**：下面要同时生成 `/` 与 `\` 两种形态去比对，
# 用 `os.path.join` 的话在 Windows 上只剩反斜杠一种，正斜杠写法就漏了。
_REPO_STORES = ("data/wecom-cli-cc",)
# 按**文件名**判的密钥类文件（任何目录下都算）。为什么必须按名字判：
# 桥给 CC 带的 `--disallowedTools Read(**/.env)` 只管**工作目录内**的文件访问
# （2026-11-11 实测：cwd 之外的 `.env` 照样被读出来），所以这条底线得由我们兜。
_ENV_RE = re.compile(r"^\.env(?:\.[A-Za-z0-9_-]+)*$", re.I)
_ENV_EXEMPT = ".env.example"          # 公开模板，没有密钥，必须放行
_SECRET_RE = re.compile(r"(?:^.*\.(?:pem|key)$)|(?:^id_rsa(?:\..*)?$)", re.I)
# 命令原文按"词"切开再看文件名 —— 不做整串包含，否则 `environment`、
# `.env.example`、`.env*` 这类都会误伤。
_TOKEN_SPLIT = re.compile(r"[\s\"'`;|&()<>,\[\]]+")
# 这些字段装的是**路径**；只查它们，不查 `content` / `command` 之类的正文
# （否则"在文档里写一句 `~/.dws`"也会被拒，那是误伤）。
_PATH_FIELDS = ("file_path", "notebook_path", "path", "cwd")


def _protected_roots():
    """受保护的目录/文件（绝对路径、已规范化）。每次现算 —— 不缓存，便于测试替换 HOME。"""
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    roots = [os.path.join(home, n) for n in _HOME_STORES + _HOME_SECRETS]
    roots += [os.path.normpath(os.path.join(here, "..", *n.split("/")))
              for n in _REPO_STORES]
    return roots


def _is_secret_name(name):
    """文件名本身就是密钥类（`.env` / `.env.local` / `x.pem` / `id_rsa`）。"""
    b = str(name or "").strip().strip("\"'")
    if not b:
        return False
    low = b.lower()
    if low == _ENV_EXEMPT or low.startswith(_ENV_EXEMPT + "."):
        return False                      # `.env.example` 是公开模板
    if _ENV_RE.match(b):
        return True
    return bool(_SECRET_RE.match(b))


def _norm_path(p, base=""):
    """把一个可能是相对路径/带 `~`/带引号的字符串，化成可比较的绝对路径。"""
    p = str(p or "").strip().strip("\"'")
    if not p:
        return ""
    # Git Bash / MSYS 风格的 `/c/Users/x/...` —— Windows 上 `abspath` 会把它当成
    # "当前盘符下的 \c\Users\x"，于是**解析到完全另一个位置**。实测踩到过。
    if os.name == "nt":
        m = re.match(r"^/([A-Za-z])/(.*)$", p)
        if m:
            p = m.group(1).upper() + ":\\" + m.group(2)
    p = os.path.expanduser(os.path.expandvars(p))
    if not os.path.isabs(p):
        p = os.path.join(base or os.getcwd(), p)
    p = os.path.abspath(p)
    try:
        p = os.path.realpath(p)      # 顺手解开符号链接/目录联接
    except OSError:
        pass
    return os.path.normcase(p)


def _is_cred_path(p, base=""):
    n = _norm_path(p, base)
    if not n:
        return False
    for r in _protected_roots():
        rn = os.path.normcase(os.path.abspath(r))
        if n == rn or n.startswith(rn + os.sep):
            return True
    # 最后按**文件名**兜一层：`.env` / 私钥放在哪里都不能读（含 cwd 之外）。
    return _is_secret_name(os.path.basename(n))


def _cred_text_hit(cmd):
    """命令原文里有没有**直接写出**受保护路径。

    ⚠️ 这是文本层判断，**故意偏保守**：宁可多拒一条无关命令，也不放过一条
       能读到令牌的命令。代价是 `grep -rn "data/wecom-cli-cc" docs/` 这种
       "只是提到这个路径"的搜索也会被拒 —— 换个写法即可。
       **连 `git commit -m "...~/.dws..."` 这种"只是在写说明"的命令也会被拒**
       （实测踩到：本功能的提交信息就被自己拦了）。这不是 bug，是上面那条取舍的代价：
       要写含这个路径的文本，走 `Write` 工具或 `git commit -F <文件>`，**别改松这条规则**。

    ⚠️ 抓不到形态：运行时拼出来的路径（`chr(126)+'/.dws'`、先 `cd` 再 `cat token.json`）。
       前者会被"非白名单命令一律弹卡"接住；后者同理。这条**残余风险**写在
       `interop/dingtalk.md` 里，不假装已经封死。
    """
    low = (cmd or "").lower()
    if not low:
        return False
    home = os.path.expanduser("~")
    here = os.path.dirname(os.path.abspath(__file__))
    sigs = []
    # Git Bash 里 `$HOME` 展开成 `/c/Users/x` —— 这个写法**必须**单独算一条，
    # 否则 `cat /c/Users/x/.dws/token.json` 从字面上看和受保护目录毫不相干。
    msys_home = "/" + home.replace(":", "").replace("\\", "/")
    for name in _HOME_STORES + _HOME_SECRETS:
        p = os.path.join(home, name)
        sigs += ["~/" + name, "~\\" + name,
                 "$home/" + name, "$home\\" + name,
                 "${home}/" + name, "${home}\\" + name,
                 "%userprofile%/" + name, "%userprofile%\\" + name,
                 msys_home + "/" + name,
                 p, p.replace("/", "\\")]
    for rel in _REPO_STORES:
        p = os.path.normpath(os.path.join(here, "..", *rel.split("/")))
        sigs += [rel, rel.replace("/", "\\"), p, p.replace("/", "\\")]
    if any(s.lower() in low for s in sigs):
        return True
    return _token_secret_hit(low)


def _token_secret_hit(low):
    """命令原文里的**单词**有没有直接指向密钥类文件（`.env` / `x.pem` / `id_rsa`）。

    走"切词 → 取 basename"而不是整串包含：`environment`、`.env.example`、
    `find -name ".env*"`（只列名字）都不该被拦。
    `--env-file=.env` 这种带 `=` 的写法会把等号两边都看一遍。
    """
    for tok in _TOKEN_SPLIT.split(low):
        for part in tok.split("="):
            part = part.strip("\"'")
            if not part:
                continue
            base = part.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            if _is_secret_name(base):
                return True
    return False


def _cred_hit(name, tool_input, cwd):
    """这次工具调用会不会碰到凭据存储。"""
    # 整轮工作目录本身就在存储里 ⇒ 它碰什么都算
    # （`Grep` / `Glob` 不带 `path` 时，搜的正是 cwd）
    if bool(cwd) and _is_cred_path(cwd):
        return True
    # 路径字段：直接按路径判（相对路径按本轮 cwd 展开，`..` / 符号链接一并解开）
    for key in _PATH_FIELDS:
        v = tool_input.get(key)
        if isinstance(v, str) and _is_cred_path(v, cwd):
            return True
    # 命令 / 模式字段：按文本判 —— `Bash` 的 command、`Glob`/`Grep` 的 pattern
    # 都可能直接把存储路径写出来（`Glob ~/.dws/*` 不带 path 字段）。
    for key in ("command", "pattern"):
        v = tool_input.get(key)
        if isinstance(v, str) and _cred_text_hit(v):
            return True
    return False


def _load_effects():
    """读官方安全语义快照。读不出来就返回空表 —— 空表 = 全弹卡，这是安全的。"""
    global _EFFECTS
    if _EFFECTS is None:
        try:
            with open(_EFFECTS_PATH, encoding="utf-8") as f:
                _EFFECTS = json.load(f).get("commands") or {}
        except Exception:
            _EFFECTS = {}
    return _EFFECTS


def _basename(tok):
    return tok.replace("/", "\\").rsplit("\\", 1)[-1].strip("\"'").lower()


def _tokens(seg):
    try:
        return shlex.split(seg, posix=False)
    except ValueError:
        return seg.split()


def _split_segments(cmd):
    """按 `&& || ; | 换行` 切段（**引号内不切**）。

    返回 (segs, code, balanced)：
      segs     = [(段原文, 这段前面是不是管道 `|`)]
      code     = **单引号之外**的原文 —— 危险写法只可能出现在这里
                 （双引号里 `$()` 照样执行，所以双引号的内容要算进来）
      balanced = 引号有没有闭合（没有闭合就整条弹卡，别猜）
    """
    segs, buf, code, quote, prev_op, i = [], [], [], "", "", 0
    while i < len(cmd):
        ch = cmd[i]
        if quote:
            if ch == quote:
                quote = ""
            elif quote == '"':       # 双引号内仍会展开 ⇒ 也要进 code
                code.append(ch)
            buf.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        two = cmd[i:i + 2]
        if two in ("&&", "||"):
            segs.append(("".join(buf), prev_op))
            buf, prev_op, i = [], two, i + 2
            continue
        if ch in ";|\n":
            segs.append(("".join(buf), prev_op))
            buf, prev_op = [], ch
            code.append(ch)
            i += 1
            continue
        buf.append(ch)
        code.append(ch)
        i += 1
    segs.append(("".join(buf), prev_op))
    out = [(s, op == "|") for s, op in segs if s.strip()]
    return out, "".join(code), quote == ""


def _writes_file(args):
    """过滤器带着"写到文件"的选项（`sort -o` 之类）就不算只读。"""
    return any(a.startswith("-o") or a.startswith("--output") for a in args)


def _trusted_cli(tok, prog):
    """这个名字叫 `dws`，跑的就真是官方那一个吗？

    `./dws` 可以是任何东西 —— 只要有人往工作目录扔一个同名脚本，
    "`dws todo +get` 是只读的"这条判断就跟它没关系了。
    所以：**裸名字**（交给 PATH 解析）放行；**带路径**的必须与 `PATH` 上那个是同一个文件。
    """
    if "/" not in tok and "\\" not in tok:
        return True
    real = shutil.which(prog)
    if not real:
        return False
    try:
        return os.path.samefile(os.path.abspath(tok.strip("\"'")), real)
    except OSError:
        return False


def _is_help(args):
    """`--help` / `-h` 真的会打印帮助吗？

    ⚠️ **不能只看"命令里有没有 --help"** —— 实测（2026-11-10）：

        dws todo +create --title --help --executors abc --dry-run
        → {"operation": "create", "subject": "--help"}   ← 它去建待办了！

    pflag 把紧跟在 `--title` 后面的 `--help` 吃成了**值**，命令照常往下走。
    所以判据收紧成两种形状：
      · 它是第一个参数（`dws --help` / `dws todo --help`）；
      · 它是**最后一个**参数，且前一个不是 `-` 开头的 flag。
    其余一律当"不是帮助"，按命令本身判读写。
    """
    for i, a in enumerate(args):
        if a not in ("-h", "--help"):
            continue
        if i == 0:
            return True
        if i == len(args) - 1 and not args[i - 1].startswith("-"):
            return True
    return False


def _cli_ok(prog, args):
    """`dws` / `lark-cli` 这一段是不是只读。"""
    # `dws.cmd` / `dws.exe` 是同一个东西的另外两种后缀 —— 归一后再查表
    for name in ("dws", "lark-cli"):
        if prog in (name + ".exe", name + ".cmd"):
            prog = name
            break
    # 打印帮助不执行任何东西 —— 写命令带 --help 也是安全的（但见 `_is_help`）
    if _is_help(args):
        return True, ""
    if prog != "dws":
        return False, "%s 还没有只读表 ⇒ 按写处理" % prog
    if not args:
        return False, "只写了 %s、没有子命令 ⇒ 按写处理" % prog
    if args[0].startswith("-"):
        return False, "全局参数写在子命令之前，定位不到命令 ⇒ 按写处理"

    parts = []
    for a in args:
        if a.startswith("-"):        # 子命令路径在前、flag 在后
            break
        parts.append(a.lower())
    if parts and parts[0] in _LOCAL_SUBCOMMANDS:
        return True, ""

    table = _load_effects()
    entry, matched = None, ""
    # **最长前缀匹配**：`dws doc +read <docId>` 里 `<docId>` 是位置参数，
    # 不能把它当成命令路径的一部分 —— 所以从长到短试，命中哪条算哪条。
    for n in range(min(len(parts), 5), 0, -1):
        key = " ".join(parts[:n])
        if key in table:
            entry, matched = table[key], key
            break
    if entry is None:
        shown = " ".join(parts[:3]) + (" …" if len(parts) > 3 else "")
        return False, "表外按写（%s）" % shown

    eff = str(entry.get("effect") or "")
    conf = str(entry.get("confirmation") or "")
    if eff != "read" or conf != "not_required":
        # 官方说这条命令可以 `--dry-run` 预演 —— 卡面告诉用户，但**不替他跑**：
        # 在用户点头之前先执行一次命令，本身就是闸门最不该做的事。
        # ⚠️ `--dry-run` 本身**照样弹卡**：官方契约说它不执行，可万一是 CLI 的 bug，
        # 那就是"没经批准就写了数据" —— 这条底线不能押在别人的实现上。
        if entry.get("dry_run") and any(a == "--dry-run" for a in args):
            return False, "干跑预览（%s）· 官方标注不会执行" % matched
        hint = "· 支持 --dry-run 预演" if entry.get("dry_run") else ""
        if eff == "destructive":
            return False, "危险操作（%s · destructive）%s" % (matched, hint)
        if eff == "write":
            return False, "写操作（%s）%s" % (matched, hint)
        return False, "官方标注 %s（%s / %s）%s" % (matched, eff or "?", conf or "?", hint)
    sensitive = _SENSITIVE_READ.get(matched)
    if sensitive:
        return False, "会读出密钥：%s（%s）" % (sensitive, matched)
    return True, ""


def _segment_ok(text, is_tail):
    toks = _tokens(text)
    if not toks:
        return True, ""
    if _ENV_ASSIGN.match(toks[0]):
        return False, "带环境变量前缀（%s）⇒ 按写处理" % toks[0].split("=")[0]
    prog = _basename(toks[0])
    if prog == "cd":
        if is_tail:
            return False, "`cd` 接在管道上 ⇒ 按写处理"
        if len([t for t in toks[1:] if t]) != 1:
            return False, "`cd` 的参数不是恰好一个 ⇒ 按写处理"
        return True, ""
    if prog in _PLATFORM_CLIS:
        if is_tail:
            return False, "%s 接在管道下端 ⇒ 按写处理" % prog
        if not _trusted_cli(toks[0], prog.rsplit(".", 1)[0]):
            return False, "同名但不是 PATH 上那个 %s（%s）⇒ 按写处理" % (
                prog.rsplit(".", 1)[0], toks[0])
        return _cli_ok(prog, toks[1:])
    if is_tail and prog in _LOCAL_FILTERS and not _writes_file(toks[1:]):
        return True, ""
    return False, "不是只读命令（%s）" % prog


def classify_bash(command):
    """这条 Bash 命令能不能直放。返回 (allow, note)；note 是弹卡时给用户看的理由。"""
    cmd = command or ""
    if not cmd.strip():
        return False, "空命令 ⇒ 按写处理"
    segs, code, balanced = _split_segments(cmd)
    if not balanced:
        return False, "引号没有闭合，解析不了 ⇒ 按写处理"
    for h in _HAZARDS:
        if h in code:
            return False, "含 `%s`（命令替换/重定向）⇒ 按写处理" % h
    if not segs:
        return False, "没有可判定的命令 ⇒ 按写处理"
    for text, is_tail in segs:
        ok, note = _segment_ok(text, is_tail)
        if not ok:
            return False, note
    return True, ""


def _brief(tool_input, limit=120):
    """一行摘要，给直放账本用（不参与任何判定）。"""
    for key in ("command", "file_path", "notebook_path", "path", "pattern"):
        v = tool_input.get(key)
        if isinstance(v, str) and v.strip():
            v = " ".join(v.split())
            return v[:limit] + ("…" if len(v) > limit else "")
    return ""


def _log_readonly(adir, cmd):
    """记一行"直放"的账。

    ⚠️ 桥的日志只记卡片，不记命令正文 —— 没有这行，事后**没法证明**
    某条命令是被白名单放行的、还是根本没走闸门。超过 512 KB 就轮转一次。
    """
    try:
        os.makedirs(adir, exist_ok=True)
        path = os.path.join(adir, "readonly.log")
        try:
            if os.path.getsize(path) > 512 * 1024:
                os.replace(path, path + ".1")
        except OSError:
            pass
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write("%s\t%s\n" % (time.strftime("%Y-%m-%d %H:%M:%S"), cmd))
    except Exception:
        pass        # 记不了账不影响放行判断


def allow():
    sys.exit(0)


def deny(reason):
    """按实测推荐的方式拒绝：JSON，理由**原样**交给模型。"""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    adir = os.environ.get("CC_APPROVAL_DIR", "").strip()

    # ⚠️ **最外层兜底：任何没预料到的异常一律拒绝**（2026-10-29 复核 #2③）。
    # 之前只有中间那段 try，读输入 / 解析 JSON / 建目录这些**更早**的步骤漏在外面：
    # 读失败或 JSON 坏了会直接 `allow()`，建目录失败会抛出去 ⇒ harness 那边
    # 同样是放行（它把异常、超时、非 JSON 输出统统当放行）。
    # 两条路都通向"审批形同虚设"，所以这里必须反过来：异常 ⇒ deny。
    try:
        _run(adir)
    except SystemExit:
        raise
    except BaseException as e:
        deny(f"审批 hook 内部错误（{type(e).__name__}: {e}），保守拒绝。")


def _run(adir):
    try:
        raw = sys.stdin.read()
    except Exception as e:
        deny(f"审批 hook 读不到本次事件（{e}），保守拒绝。")
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        deny(f"审批 hook 收到的事件不是合法 JSON（{e}），保守拒绝。")

    name = ev.get("tool_name") or ""
    tool_input = ev.get("tool_input") or {}
    cwd = ev.get("cwd") or ""

    # ① **凭据存储硬 deny**（P1-2）：排在一切开关之前 —— 不看 `CC_APPROVAL_DIR`、
    #    不看免卡白名单（`CC_APPROVAL_AUTO`）、不看 `CC_APPROVAL_ALLOW`。
    #    `Read` 默认直放，这条必须比它更早，否则 `Read <凭据文件>` 一声不响就执行了。
    if _cred_hit(name, tool_input, cwd):
        deny(CRED_DENY_REASON)

    # 没开审批 ⇒ 这里就是个"静态安全过滤 + 放行"的空壳
    if not adir:
        allow()

    # ② **免卡白名单**（P1-3，桥按发送者注入 `CC_APPROVAL_AUTO=1`）：
    #    闸门照样跑过上面那一关，只是不再弹卡。免卡用户读不到凭据，其余规则一条不少。
    #    记一行账 —— 否则"这轮没弹卡"事后没法解释是预授权还是闸门没跑。
    if os.environ.get("CC_APPROVAL_AUTO", "").strip() == "1":
        _log_readonly(adir, "[auto] %s %s" % (name, _brief(tool_input)))
        allow()

    allow_list = [x.strip() for x in
                  os.environ.get("CC_APPROVAL_ALLOW", DEFAULT_ALLOW).split(",") if x.strip()]
    if name in allow_list:
        allow()

    # Bash 按**命令**判：纯读直放，其余（含"拿不准"）一律弹卡。
    # `policy` 会写进请求文件，卡面上多一行「策略：…」，让用户一眼看出
    # 这条为什么被拦 —— 否则"卡又来了"会变成没有信息量的噪音。
    policy = ""
    if name == "Bash":
        cmd = str(tool_input.get("command") or "")
        ok, policy = classify_bash(cmd)
        if ok:
            _log_readonly(adir, cmd)
            allow()

    try:
        deadline = float(os.environ.get("CC_APPROVAL_WAIT_SEC", DEADLINE_SEC))
    except Exception:
        deadline = DEADLINE_SEC
    deadline = max(5.0, min(deadline, 280.0))     # 别超过 harness 的 300

    req = seen = dec = None       # 下面 finally 要用；建目录失败时它们是 None
    os.makedirs(adir, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    req = os.path.join(adir, rid + ".req.json")
    seen = os.path.join(adir, rid + ".seen")
    dec = os.path.join(adir, rid + ".decision")

    # 目标（发给哪个会话）由桥在起 claude 时注入；没有就发不出去，直接拒绝
    target = os.environ.get("CC_APPROVAL_TARGET", "").strip()
    body = {
        "id": rid, "tool_name": name, "tool_input": tool_input,
        "session_id": ev.get("session_id"), "cwd": ev.get("cwd"),
        "target": target, "ts": time.time(),
        "policy": policy,     # 卡面「策略」一行的文案；非 Bash / 没理由时为空串
    }
    try:
        _write_atomic(req, json.dumps(body, ensure_ascii=False))
    except Exception as e:
        # 写不出请求 ⇒ 桥永远不会知道，别干等
        deny(f"审批请求写不出来（{e}），保守拒绝。")

    t0 = time.time()
    try:
        # ① 等桥"我看到了"（卡片真的发出去了）
        try:
            seen_sec = float(os.environ.get("CC_APPROVAL_SEEN_SEC", SEEN_WAIT_SEC))
        except Exception:
            seen_sec = SEEN_WAIT_SEC
        while time.time() - t0 < max(0.5, seen_sec):
            if os.path.exists(seen):
                break
            if os.path.exists(dec):          # 极端情况：桥直接给了决定
                break
            time.sleep(0.1)
        else:
            deny(f"{max(0.5, seen_sec):.1f} 秒内桥没有响应（它可能没在跑），"
                 f"已按未审批拒绝。这条请求是：{name}")

        # ② 等用户在企微上点卡片
        while time.time() - t0 < deadline:
            if os.path.exists(dec):
                break
            time.sleep(0.2)
        else:
            deny(f"{int(deadline)} 秒内没有点击，已按超时拒绝（这是刻意的默认："
                 f"没人应答就不执行）。要做的话，重新发一次让人守着。")

        try:
            with open(dec, encoding="utf-8") as f:
                verdict = f.read().strip().lower()
        except Exception:
            verdict = ""
        if verdict.startswith("allow"):
            allow()
        deny("用户点了「拒绝」，这条工具调用没有执行。")
    except SystemExit:
        raise
    except Exception as e:
        # **任何意外都要拒绝**：harness 那一层的所有异常路径都是放行，
        # 我们这一层必须反过来，否则"审批"就只是装饰。
        deny(f"审批通道出错（{type(e).__name__}: {e}），保守拒绝。")
    finally:
        for p in (req, seen, dec):
            if not p:
                continue
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    main()