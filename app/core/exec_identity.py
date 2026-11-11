"""审批卡上的「执行身份」。

跨平台命令（`dws` / `lark-cli`）用的是**用户本人的 OAuth** —— 同事看到的是你，
不是机器人。所以这类命令的审批卡必须把执行身份写出来，否则"允许"这两个字
会被理解成"以机器人的名义做"。

⚠️ 本模块**只负责显示**：它不参与放行与否的判断。判断错了最坏是卡上少一行 /
多一行提示，不会让该拦的没拦住（拦不拦是 `approval_hook` 的事）。
"""

import re
import shlex

# 命令名（basename，小写） → 平台名
_PLATFORMS = {
    "dws": "钉钉", "dws.exe": "钉钉", "dws.cmd": "钉钉",
    "lark-cli": "飞书", "lark-cli.exe": "飞书", "lark-cli.cmd": "飞书",
}

# 一条 Bash 命令可能串好几段（`cd x && dws ...`），按 shell 分隔符切开逐段看
_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# `lark-cli ... --as bot` / `--as=bot` —— 飞书侧同一件事有两种身份，
# 卡面必须按**命令实际带的那个**写（默认 user）。
_AS_BOT = re.compile(r"(?:^|\s)--as(?:=|\s+)(bot)\b", re.I)


def _basename(tok):
    return tok.replace("/", "\\").rsplit("\\", 1)[-1].strip("\"'").lower()


def _first_tokens(command):
    """把命令切成若干段，逐段给出**第一个真正被执行的词**（跳过 `FOO=bar` 前缀）。"""
    for seg in _SPLIT.split(command or ""):
        seg = seg.strip()
        if not seg:
            continue
        try:
            parts = shlex.split(seg, posix=False)
        except ValueError:
            parts = seg.split()
        while parts and _ENV_PREFIX.match(parts[0]):
            parts = parts[1:]
        if parts:
            yield parts[0]


def platform_of(command):
    """命令里第一个跨平台执行面的名字；没有就返回空串。

    只看**每段的第一个词** —— `echo dws` 不算，`cd x && dws todo list` 算。
    """
    for tok in _first_tokens(command):
        name = _basename(tok)
        if name in _PLATFORMS:
            return _PLATFORMS[name]
    return ""


def describe(req):
    """审批卡上那一行文案。不是跨平台命令 ⇒ 返回空串（卡面保持原样）。

    ⚠️ 飞书侧的 `--as bot` 是**应用自己的身份**，不是用户本人 —— 卡面不能写
    "你的账号"。这一条来自官方文档（`interop/feishu.md` §4）：同一件事可能由
    用户或机器人执行，写错了等于把责任安在错的人头上。
    """
    if (req.get("tool_name") or "") != "Bash":
        return ""
    cmd = str((req.get("tool_input") or {}).get("command") or "")
    plat = platform_of(cmd)
    if not plat:
        return ""
    if plat == "飞书" and _AS_BOT.search(cmd):
        return f"{plat} · 机器人身份（应用自己的身份）"
    return f"{plat} · 你的账号（不是机器人）"