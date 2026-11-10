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

不设 `CC_APPROVAL_DIR` 时本脚本**立刻放行、什么也不做** —— 这样同一个
`.claude/settings.json` 可以随仓库一起发布，别人不启用审批时它是个空壳。

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
    # 不用审批模式 ⇒ 这个 hook 是个空壳（仓库里带着它也不会影响别人）
    adir = os.environ.get("CC_APPROVAL_DIR", "").strip()
    if not adir:
        allow()

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
    allow_list = [x.strip() for x in
                  os.environ.get("CC_APPROVAL_ALLOW", DEFAULT_ALLOW).split(",") if x.strip()]
    if name in allow_list:
        allow()

    # Bash 按**命令**判：纯读直放，其余（含"拿不准"）一律弹卡。
    # `policy` 会写进请求文件，卡面上多一行「策略：…」，让用户一眼看出
    # 这条为什么被拦 —— 否则"卡又来了"会变成没有信息量的噪音。
    policy = ""
    if name == "Bash":
        cmd = str((ev.get("tool_input") or {}).get("command") or "")
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
        "id": rid, "tool_name": name, "tool_input": ev.get("tool_input") or {},
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