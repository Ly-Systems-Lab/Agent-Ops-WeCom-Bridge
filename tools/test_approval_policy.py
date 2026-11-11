"""P1-1 离线验证：命令级只读白名单。

不连任何平台、不发消息、不等人工点击 —— 只证明三件事：
  ① 该放行的放行（读命令、帮助、本地过滤器）；
  ② 该弹卡的弹卡，而且**拿不准的一律弹卡**；
  ③ 弹卡的理由（`policy`）真的进了请求文件 —— 卡面才有「策略」那一行。

用法： python -X utf8 tools/test_approval_policy.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "tools", "approval_hook.py")
EFFECTS = os.path.join(ROOT, "interop", "dws_effects.json")
LARK_EFFECTS = os.path.join(ROOT, "interop", "lark_effects.json")
DOC = os.path.join(ROOT, "interop", "dingtalk.md")
DOC_LARK = os.path.join(ROOT, "interop", "feishu.md")

FAIL = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def _load_hook():
    spec = importlib.util.spec_from_file_location("approval_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


def test_allow():
    print("\n== 直放：纯读命令 ==")
    cases = [
        ("dws todo +get-my-tasks", "最基础的一条读"),
        ("dws todo +get-my-tasks --status false --priority 40,30", "带筛选参数"),
        ("dws todo +get-my-tasks --jq '.data | .[0].id'", "jq 里的管道在引号内，不切段"),
        ("dws todo +get-my-tasks --jq '$(whoami)'", "单引号里是字面量，不执行"),
        ("dws todo +get --task-id 123", "按 id 查详情"),
        ("cd D:/work && dws todo task list", "cd 是无害外壳"),
        ("dws todo +get-my-tasks | head -20", "管道下端是纯过滤器"),
        ("dws todo +get-my-tasks 2>&1", "fd 复制不写文件，不算重定向"),
        ("dws todo +get-my-tasks 2>&1 | head -20", "fd 复制 + 纯过滤器"),
        ("dws todo +get-my-tasks 1>&2", "另一种 fd 复制"),
        ("dws todo +create --help", "写命令带 --help 只打印帮助，不执行"),
        ("dws todo +create --help 2>&1 | head -20",
         "fd 复制不能把 --help 从'最后一个参数'挤开"),
        ("lark-cli task +create --help 2>&1", "同上（飞书侧那次 9 张卡里的 bug）"),
        ("dws todo --help", "分组帮助"),
        ("dws schema todo --compact", "schema 是本地契约，不联网"),
        ("dws --help", "只有全局帮助"),
        ("dws contact user get --userid abc", "通讯录查询"),
        ("echo hello", "echo 只往 stdout 打字"),
        ("echo dws todo +create", "echo 只是把后面的字打出来，不会执行 dws"),
        ("date", "纯读本地命令"),
        ('date "+%Y-%m-%d %A %H:%M %Z"', "带格式串"),
        ("date -u", "带只读参数"),
        ("pwd", "纯读本地命令"),
        ("printf '%s\\n' hi", "printf 是 shell 内建"),
        ("lark-cli --help; echo '---- api help ----'; lark-cli api --help",
         "两个帮助 + 分隔线，整条都是读"),
        ("MSYS_NO_PATHCONV=1 lark-cli calendar +agenda",
         "Git Bash 路径改写变量不影响执行哪个程序"),
        ("MSYS_NO_PATHCONV=1 MSYS2_ARG_CONV_EXCL=* dws todo +get-my-tasks",
         "两个 MSYS 变量叠加"),
    ]
    real = shutil.which("dws")
    if real:
        cases.append((real + " todo +get-my-tasks", "写全路径的 dws 也认（就是 PATH 上那个）"))
    for cmd, why in cases:
        ok, note = hook.classify_bash(cmd)
        check(f"直放 · {why}", ok, "" if ok else f"被拦：{note}")


def test_ask():
    print("\n== 弹卡：写操作、拿不准、危险写法 ==")
    cases = [
        ("dws todo +create --title t --executors u", "写操作"),
        # 实测：pflag 会把紧跟 --title 的 --help 吃成"值"，命令照常执行 ⇒ 不能直放
        ("dws todo +create --title --help --executors abc", "--help 被当成 flag 的值"),
        ("dws todo +create --title x --executors --help", "末尾但前面是 flag ⇒ 不算帮助"),
        ("dws todo +get-my-tasks && dws todo +create --title t", "多段里有写"),
        ("dws todo +get-my-tasks $(whoami)", "命令替换"),
        ("dws todo +get-my-tasks `whoami`", "反引号"),
        ("dws todo +get-my-tasks > out.json", "输出重定向"),
        # fd 复制放宽之后，这三种形态仍然要拦 —— 别把放宽写成了放空
        ("dws todo +get-my-tasks 2>&1 > out.json", "fd 复制之后还有真正的文件重定向"),
        ("dws todo +get-my-tasks &> out.txt", "&> 是真正的重定向（不是 fd 复制）"),
        ("dws todo +get-my-tasks 2>&1 & rm -rf /", "fd 复制之后还有后台符号"),
        ("dws todo +get-my-tasks & rm -rf /", "单独的 & 会再跑一条后台命令"),
        ('dws todo +get-my-tasks --jq "$(whoami)"', "双引号里的命令替换照样执行"),
        ("dws api /v1.0/todo/tasks", "raw OpenAPI 不在表内"),
        ("dws event consume", "官方标 write"),
        ("dws", "没有子命令"),
        ("dws --dry-run todo +create", "全局参数写在子命令前，定位不到"),
        ("rm -rf /", "不是只读命令"),
        ("grep dws todo list", "不是只读命令（grep 不带文件、也不是管道下端）"),
        ("dws todo +get-my-tasks | sh", "管道喂给 shell"),
        ("PATH=/tmp dws todo +get-my-tasks", "环境变量前缀可以换掉 dws 本体"),
        ("MSYS_NO_PATHCONV=1 PATH=/tmp dws todo +get-my-tasks",
         "叠了一个会影响执行的变量 ⇒ 整条按写"),
        ("PYTHONPATH=/tmp lark-cli calendar +agenda", "不在无害清单里的前缀"),
        ("MSYS_NO_PATHCONV=1", "只有前缀、没有命令"),
        ("/tmp/echo hi", "带路径的同名程序不算内建（可以是任意东西）"),
        ('date -s "2026-01-01"', "`date -s` 会改系统时间"),
        ("date --set=2026-01-01", "另一种改时间的写法"),
        ("/usr/bin/date", "带路径的 date 不认"),
        ("dws todo +get-my-tasks | sort -o out.txt", "过滤器带写文件选项"),
        ("dws todo +get-my-tasks 'unclosed", "引号没闭合"),
        ("python -c \"print(1)\"", "非白名单程序"),
        ("./dws todo +get-my-tasks", "同名但不是 PATH 上那个 dws"),
        ("C:\\temp\\dws.exe todo +get-my-tasks", "路径不存在，不可能验明正身"),
    ]
    for cmd, why in cases:
        ok, note = hook.classify_bash(cmd)
        check(f"弹卡 · {why}", not ok and bool(note), note if not ok else "竟然直放了")


def test_lark():
    """P1-4：飞书执行面。分类来源 = 官方 schema + 每条 --help 的 Risk 行。"""
    print("\n== 飞书 lark-cli：直放 ==")
    allow = [
        ("lark-cli calendar +agenda", "快捷命令里的读"),
        ("lark-cli calendar events get --calendar-id primary", "API 命令里的读"),
        ("lark-cli auth status", "管理命令里的读"),
        ("lark-cli config show", "看配置（secret 是打码的）"),
        ("lark-cli doctor", "健康检查"),
        ("lark-cli skills read lark-calendar", "读本地 skill 文档，不联网"),
        ("lark-cli schema", "schema 是本地契约"),
        ("lark-cli schema im messages delete", "查单条方法的参数"),
        ("cd D:/work && lark-cli calendar +agenda", "cd 是无害外壳"),
        ("lark-cli calendar +agenda | head -20", "管道下端是纯过滤器"),
        ("lark-cli calendar +agenda 2>&1 | head -20", "模型最常用的写法，fd 复制不算重定向"),
        ("lark-cli im +messages-send --help", "写命令带 --help 只打印帮助"),
    ]
    for cmd, why in allow:
        ok, note = hook.classify_bash(cmd)
        check(f"直放 · {why}", ok, "" if ok else f"被拦：{note}")

    print("\n== 飞书 lark-cli：弹卡 ==")
    ask = [
        ("lark-cli im +messages-send --chat-id oc_x --text hi", "写操作"),
        ("lark-cli im messages delete --message-id om_x", "high-risk-write"),
        ("lark-cli api GET /open-apis/calendar/v4/calendars",
         "通用 API 逃生舱 —— 官方整体标 write，不按 GET 猜"),
        ("lark-cli auth login", "授权会写本地凭据"),
        ("lark-cli update", "high-risk-write"),
        ("lark-cli", "没有子命令"),
        ("lark-cli --dry-run calendar +agenda", "全局参数写在子命令前，定位不到"),
        ("lark-cli calendar +nope", "表外按写"),
        ("lark-cli im +messages-send --chat-id oc_x --text hi && lark-cli calendar +agenda",
         "多段里有写"),
        ("grep lark-cli /dev/null", "不是只读命令（grep 不在管道下端）"),
    ]
    for cmd, why in ask:
        ok, note = hook.classify_bash(cmd)
        check(f"弹卡 · {why}", not ok and bool(note), note if not ok else "竟然直放了")

    # 实测（2026-11-11）：模型习惯性加 `2>&1 | head`，以前卡面会显示「含 `>`」，
    # 把真正的判据遮住了。放宽 fd 复制之后，理由必须是真正的那个。
    ok, note = hook.classify_bash(
        "lark-cli api GET /open-apis/calendar/v4/calendars 2>&1")
    check("带 2>&1 的 api 仍弹卡，且理由是真判据（不是「含 >」）",
          (not ok) and "通用 API 逃生舱" in note, note)
    # MSYS 前缀放行之后，api 的理由还是真正的那个（不是「环境变量前缀」）
    ok, note = hook.classify_bash(
        "MSYS_NO_PATHCONV=1 lark-cli api GET /open-apis/calendar/v4/calendars")
    check("带 MSYS 前缀的 api 仍弹卡，理由是通用 API 逃生舱",
          (not ok) and "通用 API 逃生舱" in note, note)

    print("\n== 飞书 lark-cli：干跑预览 ==")
    ok, note = hook.classify_bash(
        "lark-cli im +messages-send --chat-id oc_x --text hi --dry-run")
    check("带 --dry-run 的写命令照样弹卡，且卡面写明是预览",
          (not ok) and "干跑预览" in note, note)


def test_lark_table():
    print("\n== 飞书官方风险表 ==")
    check("表文件存在", os.path.exists(LARK_EFFECTS))
    with open(LARK_EFFECTS, encoding="utf-8") as f:
        data = json.load(f)
    cmds = data.get("commands") or {}
    check("表里命令数 > 700", len(cmds) > 700, f"{len(cmds)} 条")
    risks = {v.get("risk") for v in cmds.values()}
    check("三档风险都在", risks == {"read", "write", "high-risk-write"}, str(risks))
    check("`calendar +agenda` 是 read",
          cmds.get("calendar +agenda", {}).get("risk") == "read")
    check("`im +messages-send` 是 write",
          cmds.get("im +messages-send", {}).get("risk") == "write")
    check("`im messages delete` 是 high-risk-write",
          cmds.get("im messages delete", {}).get("risk") == "high-risk-write")
    check("`api` 是 write",
          cmds.get("api", {}).get("risk") == "write")
    # schema 与 --help 两条来源对不上时，同步工具会喊；这里再确认没有"漏网的空 risk"
    check("没有空 risk 的条目", all(v.get("risk") for v in cmds.values()))


def test_exec_identity():
    print("\n== 卡面「执行身份」（P1-4 补 --as bot）==")
    spec = importlib.util.spec_from_file_location(
        "exec_identity", os.path.join(ROOT, "app", "core", "exec_identity.py"))
    ei = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ei)

    def line(cmd):
        return ei.describe({"tool_name": "Bash", "tool_input": {"command": cmd}})

    check("dws ⇒ 钉钉 · 你的账号",
          line("dws todo +get-my-tasks").startswith("钉钉 · 你的账号"),
          line("dws todo +get-my-tasks"))
    check("lark-cli 默认 ⇒ 飞书 · 你的账号",
          line("lark-cli calendar +agenda").startswith("飞书 · 你的账号"),
          line("lark-cli calendar +agenda"))
    got = line("lark-cli im +messages-send --as bot --chat-id oc_x --text hi")
    check("--as bot ⇒ 卡面写机器人身份", "机器人身份" in got, got)
    got = line("lark-cli im +messages-send --as=bot --chat-id oc_x --text hi")
    check("--as=bot 同样认", "机器人身份" in got, got)
    got = line("lark-cli calendar +agenda --as user")
    # ⚠️ 别用 `"机器人" not in got` —— 默认文案是「你的账号（不是机器人）」，
    # 那样会把正确结果判成失败（我第一版就这么写的）。
    check("--as user ⇒ 仍是你的账号",
          "你的账号" in got and "机器人身份" not in got, got)
    check("非 Bash 工具不显示这一行",
          ei.describe({"tool_name": "Read", "tool_input": {}}) == "")
    check("普通命令不显示这一行", line("ls -la") == "")


def test_dryrun_hint():
    print("\n== 写命令的卡面提示：官方标了 dry-run 才提 ==")
    ok, note = hook.classify_bash("dws todo +create --title t --executors u")
    check("todo +create 卡面提示可干跑", "--dry-run" in note, note)
    ok, note = hook.classify_bash("dws event consume")
    check("没有 dry-run 的命令不乱提", "--dry-run" not in note, note)
    # 带 --dry-run 的写命令**照样弹卡**（万一 CLI 的 dry-run 有 bug 呢），但卡面要说清
    ok, note = hook.classify_bash("dws todo +create --title t --executors u --dry-run")
    check("干跑也弹卡，且卡面写明是预览", (not ok) and "干跑预览" in note, note)


def test_sensitive():
    print("\n== 敏感读：官方说 read，仍然弹卡并写明会读出密钥 ==")
    for path in hook._SENSITIVE_READ:
        ok, note = hook.classify_bash("dws " + path)
        check(f"弹卡 · {path}", not ok and "密钥" in note, note)


def test_table():
    print("\n== 官方契约表 ==")
    check("表文件存在", os.path.exists(EFFECTS))
    with open(EFFECTS, encoding="utf-8") as f:
        data = json.load(f)
    cmds = data.get("commands") or {}
    check("表里命令数 > 1000", len(cmds) > 1000, f"{len(cmds)} 条")
    reads = sum(1 for v in cmds.values() if v.get("effect") == "read")
    writes = sum(1 for v in cmds.values() if v.get("effect") in ("write", "destructive"))
    check("读 / 写两类都在", reads > 100 and writes > 100, f"read={reads} write+={writes}")
    check("`todo +get-my-tasks` 是 read", cmds.get("todo +get-my-tasks", {}).get("effect") == "read")
    check("`todo +create` 是 write", cmds.get("todo +create", {}).get("effect") == "write")
    check("`event consume` 是 write", cmds.get("event consume", {}).get("effect") == "write")
    # 敏感清单必须都是"官方说 read"的 —— 否则卡面那句「会读出密钥」就没意义了
    bad = [p for p in hook._SENSITIVE_READ
           if cmds.get(p, {}).get("effect") != "read"]
    check("敏感清单里的命令都存在且官方标 read", not bad, str(bad))


def test_cred():
    print("\n== 凭据硬 deny：路径判定（P1-2）==")
    home = os.path.expanduser("~")
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    # Git Bash 把 `$HOME` 展开成 `/c/Users/x` —— 这个写法必须单独认。
    # 实测踩过：少了这条，`cat /c/Users/x/.dws/token.json` 从字面上与受保护目录毫无关系。
    msys = "/" + home.replace(":", "").replace("\\", "/")
    cases = [
        ("Bash", {"command": "cat ~/.dws/token.json"}, "", True, "波浪号"),
        ("Bash", {"command": "cat " + msys + "/.dws/token.json"}, "", True,
         "MSYS 绝对路径（/c/Users/…）"),
        ("Read", {"file_path": msys + "/.dws/token.json"}, "", True,
         "Read 收到 MSYS 路径也认"),
        ("Read", {"file_path": home + "/.dws/token.json"}, "", True,
         "Windows 绝对路径"),
        ("Bash", {"command": "ls $HOME/.dws"}, "", True, "$HOME"),
        ("Bash", {"command": "type %USERPROFILE%\\.dws\\token.json"}, "", True,
         "%USERPROFILE%"),
        ("Bash", {"command": "cat data/wecom-cli-cc/credentials.enc"}, repo, True,
         "本仓凭据（正斜杠）"),
        ("Bash", {"command": "cat data\\wecom-cli-cc\\credentials.enc"}, repo, True,
         "本仓凭据（反斜杠）"),
        ("Read", {"file_path": os.path.join(home, ".dws", "token.json")}, "", True,
         "Read 直放表里的工具也拦"),
        ("Read", {"file_path": os.path.join(home, "Desktop", "x", "CLAUDE.md")}, "",
         False, "普通文件不误伤"),
        ("Read", {"file_path": os.path.join(home, ".dwsfoo", "x")}, "", False,
         "名字里带 .dws 但不是一个目录"),
        ("Read", {"file_path": "../data/wecom-cli-cc/credentials.enc"},
         os.path.join(repo, "tools"), True, "相对路径 + .."),
        ("Glob", {"pattern": "~/.dws/*"}, "", True, "Glob 只给 pattern 也拦"),
        ("Grep", {"pattern": "secret"}, os.path.join(home, ".dws"), True,
         "搜索根就是存储目录"),
        ("Grep", {"pattern": "secret", "path": os.path.join(home, ".dws", "audit")},
         "", True, "搜索根在存储里"),
        ("Bash", {"command": "dws todo +get-my-tasks"}, "", False, "正常 dws 读不受影响"),
        ("Bash", {"command": "ls interop/"}, "", False, "普通 ls 不受影响"),
        ("Read", {"file_path": os.path.join(repo, "interop", "dws_effects.json")}, "",
         False, "仓库里的 dws_ 文件不误伤"),
    ]
    for name, ti, cwd, want, why in cases:
        got = hook._cred_hit(name, ti, cwd)
        check(f"{'拦' if want else '放'} · {why}", got == want, str(ti)[:60])


def test_secret_files():
    print("\n== 凭据硬 deny：密钥类文件名（2026-11-11 补）==")
    home = os.path.expanduser("~")
    repo = ROOT
    cases = [
        ("Bash", {"command": "cat .env"}, repo, True, "相对 .env"),
        # 起因：小号在群聊里让模型"看看 api key"，模型用 awk 读 .env ⇒
        # 桥弹了卡、用户点了允许，然后被 **CC 自己的** `--disallowedTools` 拒掉，
        # 模型只能回一句"命令被拒绝了"。这条命令必须**连卡都不出**。
        ("Bash", {"command": "if [ -f .env ]; then awk '{print}' .env; fi"}, repo,
         True, "事故里那条命令的形态"),
        ("Bash", {"command": "cat .env.local"}, repo, True, ".env.local 同样算"),
        ("Bash", {"command": "cat .env.example"}, repo, False, "公开模板必须放行"),
        ("Bash", {"command": "cp .env.example .env"}, repo, True, "从模板生成也算命中"),
        ("Bash", {"command": "find . -name \".env*\""}, repo, False, "只列名字不读内容"),
        ("Bash", {"command": "cat --env-file=.env x"}, repo, True, "--env-file=.env"),
        ("Bash", {"command": "printenv PATH"}, repo, False, "environment 不误伤"),
        ("Bash", {"command": "cat " + os.path.join(home, "Desktop", "x", ".env")}, "",
         True, "cwd 之外的 .env（CLI 那条规则管不到）"),
        ("Read", {"file_path": os.path.join(home, "Desktop", "x", ".env")}, "", True,
         "Read 按路径判"),
        ("Write", {"file_path": os.path.join(repo, ".env.example")}, "", False,
         "写模板不拦"),
        ("Bash", {"command": "cat ~/.ssh/id_rsa"}, "", True, "ssh 私钥"),
        ("Bash", {"command": "openssl rsa -in a.key"}, repo, True, ".key"),
        ("Bash", {"command": "cat server.pem"}, repo, True, ".pem"),
        ("Bash", {"command": "cat ~/.claude/settings.json"}, "", True,
         "CC 配置（明文 token）"),
        ("Bash", {"command": "cat ~/.claude.json"}, "", True, "~/.claude.json"),
        ("Read", {"file_path": os.path.join(home, ".claude", "projects", "x.jsonl")},
         "", True, "会话记录（跨会话可读 = 泄漏别人的聊天）"),
        ("Read", {"file_path": os.path.join(repo, ".claude", "settings.json")}, "",
         False, "项目里的 .claude 不受影响"),
        ("Read", {"file_path": os.path.join(home, ".claude", "projects", "p", "memory",
                                            "note.md")}, "", False,
         "记忆目录要放行（否则写笔记也被掐死）"),
        ("Write", {"file_path": os.path.join(home, ".claude", "projects", "p", "memory",
                                             "x", "note.md")}, "", False,
         "记忆子目录同样放行"),
        ("Read", {"file_path": os.path.join(home, "Desktop", "x", "CLAUDE.md")}, "",
         False, "普通文件不误伤"),
    ]
    for name, ti, cwd, want, why in cases:
        got = hook._cred_hit(name, ti, cwd)
        check(f"{'拦' if want else '放'} · {why}", got == want, str(ti)[:60])


def _ev(name, tool_input, cwd=None):
    d = {"tool_name": name, "tool_input": tool_input, "session_id": "t"}
    if cwd:
        d["cwd"] = cwd
    return json.dumps(d, ensure_ascii=False)


def _run_hook(payload, env):
    return subprocess.run([sys.executable, HOOK], input=payload,
                          capture_output=True, text=True, encoding="utf-8",
                          env=env, cwd=ROOT, timeout=30)


def _denied(proc):
    try:
        d = json.loads(proc.stdout)["hookSpecificOutput"]
    except Exception:
        return None
    return d if d.get("permissionDecision") == "deny" else None


def test_cred_e2e():
    print("\n== 凭据硬 deny：真起 hook 进程 ==")
    home = os.path.expanduser("~")
    cred = os.path.join(home, ".dws", "token.json")

    # ① 开了审批：Read 一个凭据文件 ⇒ 直接拒绝，**不产生卡片请求**
    adir = tempfile.mkdtemp(prefix="approval-cred-")
    p = _run_hook(_ev("Read", {"file_path": cred}), _env(adir))
    d = _denied(p)
    check("Read 凭据 ⇒ 拒绝", d is not None, (p.stdout or p.stderr).strip()[:120])
    reason = (d or {}).get("permissionDecisionReason") or ""
    check("拒绝理由就是那一句", reason == hook.CRED_DENY_REASON, reason)
    check("理由里不含存储名/路径",
          ".dws" not in reason.lower() and home.lower() not in reason.lower(), reason)
    check("没有生成审批请求（没给用户一张可点错的卡）",
          not [n for n in os.listdir(adir) if n.endswith(".req.json")])

    # ② **没开审批**（没有 CC_APPROVAL_DIR）也照样拒绝 —— 这是这条规则的意义所在
    env2 = os.environ.copy()
    env2.pop("CC_APPROVAL_DIR", None)
    env2.pop("CC_APPROVAL_AUTO", None)
    p2 = _run_hook(_ev("Read", {"file_path": cred}), env2)
    check("没配审批目录 ⇒ 凭据仍然拒绝", _denied(p2) is not None,
          (p2.stdout or p2.stderr).strip()[:120])

    # ③ 免卡白名单（CC_APPROVAL_AUTO=1）也绕不过去
    env3 = _env(tempfile.mkdtemp(prefix="approval-cred-auto-"))
    env3["CC_APPROVAL_AUTO"] = "1"
    p3 = _run_hook(_ev("Read", {"file_path": cred}), env3)
    check("免卡用户 ⇒ 凭据仍然拒绝", _denied(p3) is not None,
          (p3.stdout or p3.stderr).strip()[:120])

    # ④ Bash 里写出路径也算（cat 本来就要弹卡，但它连卡都不该出）
    adir4 = tempfile.mkdtemp(prefix="approval-cred-bash-")
    p4 = _run_hook(_ev("Bash", {"command": "cat ~/.dws/token.json"}), _env(adir4))
    check("Bash 写路径 ⇒ 拒绝", _denied(p4) is not None,
          (p4.stdout or p4.stderr).strip()[:120])
    check("Bash 写路径 ⇒ 也不生成请求",
          not [n for n in os.listdir(adir4) if n.endswith(".req.json")])

    # ⑤ 普通文件不受影响（别把闸门整体拦死）
    adir5 = tempfile.mkdtemp(prefix="approval-cred-ok-")
    p5 = _run_hook(_ev("Read", {"file_path": os.path.join(ROOT, "CLAUDE.md")}),
                   _env(adir5))
    check("普通 Read 仍然直放", p5.returncode == 0 and not p5.stdout.strip(),
          (p5.stdout or "").strip()[:120])

    # ⑥ `.env` 也走同一条硬 deny —— 2026-11-11 那次"弹卡→允许→命令被拒绝了"的回归
    adir6 = tempfile.mkdtemp(prefix="approval-cred-env-")
    p6 = _run_hook(_ev("Bash", {"command": "cat .env"}), _env(adir6))
    d6 = _denied(p6)
    check(".env ⇒ 直接拒绝", d6 is not None, (p6.stdout or p6.stderr).strip()[:120])
    check(".env ⇒ 理由就是那一句",
          ((d6 or {}).get("permissionDecisionReason") or "") == hook.CRED_DENY_REASON)
    check(".env ⇒ 不生成审批请求",
          not [n for n in os.listdir(adir6) if n.endswith(".req.json")])


def test_auto():
    print("\n== 免卡白名单（P1-3）：闸门照样跑，只是不弹卡 ==")
    adir = tempfile.mkdtemp(prefix="approval-auto-")
    env = _env(adir)
    env["CC_APPROVAL_AUTO"] = "1"
    p = _run_hook(_ev("Bash", {"command": "dws todo +create --title t --executors u"}),
                  env)
    check("免卡用户跑写命令 ⇒ 放行", p.returncode == 0 and not p.stdout.strip(),
          (p.stdout or p.stderr).strip()[:120])
    check("免卡用户跑写命令 ⇒ 不生成卡片",
          not [n for n in os.listdir(adir) if n.endswith(".req.json")])
    log = os.path.join(adir, "readonly.log")
    got = ""
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            got = f.read()
    check("免卡放行记了账（事后能解释'为什么没弹卡'）", "[auto]" in got, got[:120])


def test_doc():
    print("\n== 文档与代码一致 ==")
    with open(DOC, encoding="utf-8") as f:
        doc = f.read()
    check("钉钉页有「只读白名单」一节", "只读白名单" in doc)
    missing = [p for p in hook._SENSITIVE_READ if p not in doc]
    check("敏感读逐条写进了文档", not missing, str(missing))
    check("钉钉页有「凭据存储」一节", "凭据存储" in doc)
    check("钉钉页写了 .env 与私钥", ".env" in doc and "私钥" in doc)

    with open(DOC_LARK, encoding="utf-8") as f:
        dl = f.read()
    check("飞书页有「只读白名单」一节", "只读白名单" in dl)
    check("飞书页写了三条命令层的分类来源", "schema" in dl and "Risk:" in dl)
    check("飞书页写了凭据存储路径", ".lark-cli" in dl)
    check("飞书页写了 --as bot 的卡面区别", "--as bot" in dl)


def _env(adir):
    env = os.environ.copy()
    env["CC_APPROVAL_DIR"] = adir
    env["CC_APPROVAL_TARGET"] = "chatid|2|userid"
    env["CC_APPROVAL_SEEN_SEC"] = "0.6"   # 别让测试等 10 秒
    env["CC_APPROVAL_WAIT_SEC"] = "5"
    env.pop("CC_APPROVAL_ALLOW", None)
    return env


def _event(cmd):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                       "session_id": "t", "cwd": ROOT}, ensure_ascii=False)


def test_hook_e2e():
    print("\n== 闸门端到端（真起一次 hook 进程）==")
    # ① 读命令：不产生请求文件、立即退出、账本里有这一行
    adir = tempfile.mkdtemp(prefix="approval-ro-")
    p = subprocess.run([sys.executable, HOOK], input=_event("dws todo +get-my-tasks"),
                       capture_output=True, text=True, encoding="utf-8",
                       env=_env(adir), cwd=ROOT, timeout=30)
    reqs = [n for n in os.listdir(adir) if n.endswith(".req.json")]
    check("读命令：退出码 0", p.returncode == 0, p.stderr.strip()[:120])
    check("读命令：没有生成审批请求", not reqs, str(reqs))
    check("读命令：输出为空（= 放行）", not p.stdout.strip(), p.stdout[:120])
    log = os.path.join(adir, "readonly.log")
    got = ""
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            got = f.read()
    check("读命令：记账了", "dws todo +get-my-tasks" in got)

    # ② 写命令：真的生成请求文件，且 policy 写了理由；桥不在 ⇒ 自己拒绝
    adir2 = tempfile.mkdtemp(prefix="approval-rw-")
    proc = subprocess.Popen([sys.executable, HOOK],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=_env(adir2), cwd=ROOT)
    proc.stdin.write(_event("dws todo +create --title t --executors u"))
    proc.stdin.close()
    payload = None
    t0 = time.time()
    while time.time() - t0 < 3:
        names = [n for n in os.listdir(adir2) if n.endswith(".req.json")]
        if names:
            try:
                with open(os.path.join(adir2, names[0]), encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                pass
            if payload:
                break
        time.sleep(0.05)
    out, err = proc.communicate(timeout=30)
    check("写命令：生成了审批请求", payload is not None)
    check("写命令：请求里带 policy 理由",
          bool((payload or {}).get("policy")), (payload or {}).get("policy") or "")
    check("写命令：理由是「写操作」",
          "写操作" in ((payload or {}).get("policy") or ""), (payload or {}).get("policy") or "")
    check("写命令：桥没在跑 ⇒ 自己拒绝",
          "deny" in (out or ""), (out or "").strip()[:120])

    # 弹卡也要记账 —— 否则事后没法复盘"这张卡为什么弹、用户点了什么"
    clog = os.path.join(adir2, "carded.log")
    ctext = ""
    if os.path.exists(clog):
        with open(clog, encoding="utf-8") as f:
            ctext = f.read()
    check("弹卡记账：[ask] 有请求", "[ask]" in ctext, ctext[:160])
    check("弹卡记账：[deny] 有结果", "[deny]" in ctext, ctext[:160])
    check("弹卡记账：带策略理由", "写操作" in ctext, ctext[:160])

    # ③ 非 Bash 工具照旧（Read 直放、Write 弹卡）
    adir3 = tempfile.mkdtemp(prefix="approval-read-")
    p = subprocess.run([sys.executable, HOOK],
                       input=json.dumps({"tool_name": "Read", "tool_input": {}}),
                       capture_output=True, text=True, encoding="utf-8",
                       env=_env(adir3), cwd=ROOT, timeout=30)
    check("Read 仍然直放", p.returncode == 0 and not p.stdout.strip())


def main():
    test_allow()
    test_ask()
    test_lark()
    test_dryrun_hint()
    test_sensitive()
    test_table()
    test_lark_table()
    test_exec_identity()
    test_cred()
    test_secret_files()
    test_cred_e2e()
    test_auto()
    test_doc()
    test_hook_e2e()
    print("\n" + ("❌ 失败 %d 项：%s" % (len(FAIL), "；".join(FAIL)) if FAIL else "✅ 全部通过"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())