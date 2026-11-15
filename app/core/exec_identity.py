"""审批卡上的「执行身份」。

三家官方 CLI 都支持**两种身份**，卡面必须写清这一条命是由谁做的：

    · 用户身份 —— 同事看到的是你本人；
    · 机器人身份 —— 同事看到的是那只机器人（飞书 / 钉钉是应用自己的机器人，
      企微是 CC；钉钉的 bot 还要额外传 `--robot-code`）。

写错的后果不是"显示难看"，而是**把责任安在错的人头上** —— 用户点「允许」时
以为是以自己的名义做，实际是机器人发的（或者反过来）。

⚠️ 本模块**只负责显示**：它不参与放行与否的判断。判断错了最坏是卡上少一行 /
多一行提示，不会让该拦的没拦住（拦不拦是 `approval_hook` 的事）。
"""

import re
import shlex

# 命令名（basename，小写） → 平台名
_PLATFORMS = {
    "dws": "钉钉", "dws.exe": "钉钉", "dws.cmd": "钉钉",
    "lark-cli": "飞书", "lark-cli.exe": "飞书", "lark-cli.cmd": "飞书",
    "wecom-cli": "企业微信", "wecom-cli.exe": "企业微信", "wecom-cli.cmd": "企业微信",
    "wecom-bot": "企业微信", "wecom-bot.exe": "企业微信", "wecom-bot.cmd": "企业微信",
}

# 一条 Bash 命令可能串好几段（`cd x && dws ...`），按 shell 分隔符切开逐段看
_SPLIT = re.compile(r"&&|\|\||[;|\n]")
_ENV_PREFIX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")

# 身份开关。飞书是 `--as user|bot`；钉钉是 `--identity user|bot|webhook`
# （`--as` 是官方给的 lark-cli 对齐别名，两者互斥）。
# ⚠️ 必须**按段**匹配：`lark-cli … --as bot && dws …` 是两条命令，身份各算各的。
_BOT = re.compile(r"(?:^|\s)--(?:as|identity)(?:=|\s+)(bot)\b", re.I)
_WEBHOOK = re.compile(r"(?:^|\s)--(?:as|identity)(?:=|\s+)(webhook)\b", re.I)


def _basename(tok):
    return tok.replace("/", "\\").rsplit("\\", 1)[-1].strip("\"'").lower()


def _segments(command):
    """把命令切成若干段，每段给出**分词后的参数表**（跳过 `FOO=bar` 前缀）。

    ⚠️ 返回的是**参数表**而不是"第一个词"：判定身份要看这一段的 `--as/--identity`，
    而这些开关出现在段中间（`lark-cli im … --as bot`）。
    """
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
            yield parts


def _find(command):
    """第一段跨平台执行面的 `(平台名, 参数表)`；没有就返回 `("", [])`。

    只看**每段的第一个词** —— `echo dws` 不算，`cd x && dws todo list` 算。
    """
    for parts in _segments(command):
        name = _basename(parts[0])
        if name in _PLATFORMS:
            return _PLATFORMS[name], parts
    return "", []


def platform_of(command):
    """命令里第一个跨平台执行面的名字；没有就返回空串。"""
    return _find(command)[0]


def _wecom_identity(parts):
    """企微的身份不看开关，看**调的是哪条方法**（官方 CLI 没有 `--as`）。

    · `wecom-bot …` 这个命令本身就是"换一份凭据目录" ⇒ CC 的机器人身份；
    · `message aibot …` 是"以智能机器人身份发"（官方方法名）；
    · `message send` 是以真人身份发；
    · 其余（文档 / 表格 / 日程…）官方身份上下文的原话是
      「CLI 调用一定由你的机器人身份代用户执行」⇒ 如实写成"机器人代真人执行"。
    """
    name = _basename(parts[0])
    args = [p.lower().strip('"\'') for p in parts[1:]]
    # **方法优先于命令名**：`wecom-bot` 只是换凭据目录，用哪条方法仍决定身份
    # （`wecom-bot message send` 还是以真人身份发）。
    if args[:2] == ["message", "send"]:
        return "企业微信 · 你的账号（不是机器人）"
    if len(args) >= 2 and args[0] == "message" and args[1] == "aibot":
        return "企业微信 · 机器人身份（CC）"
    if name.startswith("wecom-bot"):
        return "企业微信 · 机器人身份（CC）"
    return "企业微信 · 机器人（CC）代真人执行"


def describe(req):
    """审批卡上那一行文案。不是跨平台命令 ⇒ 返回空串（卡面保持原样）。"""
    if (req.get("tool_name") or "") != "Bash":
        return ""
    cmd = str((req.get("tool_input") or {}).get("command") or "")
    plat, parts = _find(cmd)
    if not plat:
        return ""
    if plat == "企业微信":
        return _wecom_identity(parts)
    seg = " ".join(parts)
    if _WEBHOOK.search(seg):
        return f"{plat} · Webhook 机器人"
    if _BOT.search(seg):
        return f"{plat} · 机器人身份（应用自己的身份）"
    return f"{plat} · 你的账号（不是机器人）"