"""平台无关的公共底座：路径 / 常量 / .env 加载 / 配置读取 / 白名单解析。

**为什么单独一个文件**：核心层与各平台适配器都要用这些，而它们互相不该 import。
这里只依赖标准库，是整棵 import 树的根。

⚠️ `ENV` 在**导入时**读一次 `.env`（原行为，没改）。改动 .env 需要重启进程。
"""

import logging
import os

# ---------------- 常量 ----------------
WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_SEC = 30
BACKOFF_START = 3
BACKOFF_MAX = 30

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(ROOT, "logs")
DEFAULT_DB = os.path.join(ROOT, "data", "bridge.db")

# 流式消息（aibot_respond_msg + stream.id）：让「收到，处理中…」和正文**共用一个气泡**。
# 两条硬约束来自官方文档 path/101463（原文已逐条核对）：
#   · 从首帧起 **10 分钟**内必须 finish=true，否则平台自动结束、消息不再可更新；
#   · 单帧 content ≤ **20480 字节**（和 aibot_send_msg 的 markdown 上限一致）。
STREAM_MAX_SEC = 570          # 10 分钟窗口留 30 秒余量，别卡着边界发
STREAM_LATCH_SEC = 555        # 提前 15 秒把气泡收口：别让它永远停在"收到，处理中…"
STREAM_LIMIT = 20480          # 平台硬上限，超了必然 40058
SEND_WAIT_SEC = 15            # 等平台回执的上限（超时 ⇒ 语义是"结果未知"，不是失败）

# 真进度气泡（runner 增量读 `stream-json`，每读到一次工具调用就更新气泡）。
# 节流口径是用户 2026-10-29 定的：**只在「工具/目标变了」时推**，没变化就按这个间隔补一条兜底。
# 为什么要节流：一次工具调用 = 一帧 `aibot_respond_msg`，而平台限流 30 条/分钟、1000 条/小时。
PROGRESS_HEARTBEAT_SEC = 60   # 兜底心跳间隔（0 = 不补，只在变化时推）
# 失败时落盘原始流的**内存**上限：只在"跑完拿不到 result"时才会写文件，
# 但为了能写，得先全留在内存里 —— 给它一个上限，超了只计数不占内存。
PROGRESS_RAW_CAP = 8 * 1024 * 1024

# 审批卡片：hook 写请求文件 → 桥扫到就发卡 → 点击后桥写决定文件。
# 扫得勤一点（用户点了要立刻生效），但别太勤（一个目录 listdir 而已）。
APPROVAL_POLL_SEC = 0.3

# 去重键：实测 body.msgid 存在（32 位 hex）。保留几个候选，防止平台改字段时静默失效。
ID_FIELDS = ("msgid", "msg_id", "message_id", "id")

# 默认 deny：实测路径模式有效（tools/probe_deny_syntax.py，对照组先证明读得到）。
# ⚠️ 只挡 Read 工具 —— 挡不住 Bash(cat …)。是减速带，不是墙。
DEFAULT_DISALLOWED = ",".join([
    "Read(**/.env)", "Read(**/.env.*)", "Read(**/.ssh/**)",
    "Read(**/.claude/**)", "Read(**/id_rsa*)",
])

SEEN_KEEP_DAYS = 7

# ---------------- 安全默认值 ----------------
# 默认档位必须是**实测真的拦得住**的那几个（见 tools/probe_permission_modes.py）：
# plan / dontAsk / manual 会拒绝执行；acceptEdits / auto / bypassPermissions 照样跑 Bash。
# 选 dontAsk 而不是 plan，是因为 plan 会往 ~/.claude/plans/ 写计划文件，且回复是
# "请批准后我即执行"——对聊天场景不自然。想让机器人干活，用户自己改成全自动。
DEFAULT_PERM_MODE = "dontAsk"

# ---------------- Phase 4：常驻化 ----------------
LOG_KEEP_DAYS = 14        # 按天日志保留天数（启动时清理）
WATCHDOG_SEC = 20         # 看门狗巡检间隔
STALE_RX_SEC = 90         # 订阅后多久没有任何帧就判定连接已死（心跳 30s，给 3 个周期）
# ⚠️ 这两个是**基名**，不是最终名字：它们**都不在项目目录里**，是整台机器共用的
# 两个系统级对象。同机跑多套桥（Claude Code / Codex / OpenCode / DSH）时，
# 第二套要么起不来（互斥量被占），要么把第一套的开机自启**静默覆盖**掉
# （`schtasks /Create … /F`）。⇒ 真正的名字一律经 `mutex_name()` / `task_name()`
# 派生，由 `.env` 的 `BRIDGE_INSTANCE` 追加后缀；留空 = 与上游完全一致。
MUTEX_NAME = "Local\\wecom-cc-bridge"   # 单实例命名互斥量（Local\ = 当前会话）
TASK_NAME = "wecom-cc-bridge"           # 任务计划程序里的名字

log = logging.getLogger("bridge")

# ---------------- 配置 ----------------
def load_env(path=None):
    path = path or os.path.join(ROOT, ".env")
    out = {}
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    return out

ENV = load_env()

def knob(name):
    """读一个**允许被外部打桩**的配置值。

    为什么需要它（抽层带来的新问题，必须正面解决）：
    代码分散到多个模块后，每处都会 import 到自己的一份副本；而测试与旧调用习惯
    `bridge.STREAM_MAX_SEC = 1` 这样在**入口模块**上打桩。各读各的副本 ⇒ 打桩
    静默失效（测试看着绿，其实根本没测那个分支）。

    ⇒ 这类值一律在**调用时**经本函数读：优先取入口模块 `bridge` 的当前值，
    拿不到才退回本模块的默认值。名字只放那些**真的被外部打桩**的（见
    tools/test_phase1.py：LOGDIR / WATCHDOG_SEC / STREAM_MAX_SEC /
    STREAM_LATCH_SEC / SEND_WAIT_SEC）。
    """
    import sys
    m = sys.modules.get("bridge")
    if m is not None and hasattr(m, name):
        return getattr(m, name)
    return globals().get(name)


def cfg(key, default=""):
    return os.environ.get(key) or ENV.get(key) or default

def cfg_int(key, default):
    try:
        return int(float(cfg(key, str(default))))
    except (TypeError, ValueError):
        return default

def cfg_bool(key, default=True):
    v = cfg(key, "1" if default else "0").strip().lower()
    return v not in ("0", "false", "no", "off", "")

def parse_list(s):
    """逗号分隔的 ID 列表 → 集合。**只认非空项**。

    ⚠️ 启动闸门和消息鉴权必须用**同一个**解析结果。早先闸门只看 `s.strip()` 是否非空，
    鉴权却把字符串解析成集合 —— 两边对"空"的定义不一致：
    `WECOM_ALLOWED_USERIDS=", ,"` 能**通过启动闸门**，解析出来却是空集，
    而 `if self.allow_users and …` 把空集当成"不限制" ⇒ 谁都能指挥。
    （2026-10-29 外部代码复核指出，已用代码复现。）
    """
    return {x.strip() for x in (s or "").split(",") if x.strip()}

def parse_labels(s):
    """`userid=显示名` 列表（逗号分隔）→ dict。

    **为什么要它**（2026-10-30 用户实测）：审批卡上的「处理」一行原来直接写
    `from.userid`，而企微回调的 `from` **只有 `userid`、没有任何显示名**
    （日志 `[shape] from keys=['userid']`）⇒ 把企微昵称改成 "CEO" 也改不掉卡片上
    那个账号 ID，而群里**所有人都看得见**那张卡。映射只能由桥这侧维护。

    按**第一个** `=` 切（显示名本身带 `=` 也不会被切坏）；格式不对的项静默跳过 ——
    宁可少一个名字，也不要因为一个笔误起不来。
    """
    out = {}
    for item in (s or "").split(","):
        item = item.strip()
        if "=" not in item:
            continue
        k, v = item.split("=", 1)
        k, v = k.strip(), v.strip()
        if k and v:
            out[k] = v
    return out

# 实例标识允许的字符。**排掉 `\` 是关键**：命名互斥量里它被当成命名空间分隔符
# （只允许 `Local\` 那一层），再出现一个就让 `CreateMutexW` 失败 —— 而本工程的
# 兜底是**放行**（宁可多开一个也别起不来）⇒ 单实例保护静默失效 ⇒ 双份回复。
_INSTANCE_CHARS = frozenset(
    "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ._-")


def bridge_instance():
    """同机多套桥的实例标识（`BRIDGE_INSTANCE`，如 `dsh` / `codex`）。留空 = 上游行为。"""
    return (cfg("BRIDGE_INSTANCE", "") or "").strip()


def bridge_instance_problem():
    """`BRIDGE_INSTANCE` 不合法时返回一句人话；合法或未设返回空串。

    ⚠️ **唯一真源必须是 `cfg()`（先环境变量、再 `.env`），而且互斥量与任务名
    必须由同一个值派生。** 曾经有一版任务名只读 `ENV`（`.env`）而互斥量读 `cfg()`：
    用环境变量设了实例时两者不一致 ⇒ 互斥量说"我独占"，任务名却照样去覆盖别人的自启。
    """
    inst = bridge_instance()
    bad = sorted({c for c in inst if c not in _INSTANCE_CHARS})
    if not bad:
        return ""
    return (f"BRIDGE_INSTANCE={inst!r} 含不支持的字符 {''.join(bad)!r}；"
            "只允许字母 / 数字 / `.` `_` `-`（它会拼进互斥量和计划任务名）")


def mutex_name(platform="wecom"):
    """单实例互斥量的最终名字：`基名[-平台][-实例]`。

    平台后缀是原有的（2026-11-05：两个平台是不同的机器人、不同的长连接，
    同时跑不冲突）；实例后缀是 2026-11-12 补的 —— **同一个平台也允许跑多套**，
    只要各自用不同的机器人（同一个机器人的多条长连接是分摊事件，不是广播）。
    """
    name = MUTEX_NAME if platform == "wecom" else f"{MUTEX_NAME}-{platform}"
    inst = bridge_instance()
    return f"{name}-{inst}" if inst else name


def task_name():
    """任务计划程序里的名字：`基名[-实例]`。

    ⚠️ **故意不加平台后缀**：已有部署的飞书 / Slack 自启任务都叫 `wecom-cc-bridge`，
    改名会让预置的 `uninstall` 删不到旧任务、而旧任务还在跑。要同机并存，
    显式设 `BRIDGE_INSTANCE` 即可。
    """
    inst = bridge_instance()
    return f"{TASK_NAME}-{inst}" if inst else TASK_NAME


def default_approval_dir(platform):
    """每个平台一个审批决策目录。

    ⚠️ **不能共用**（2026-11-05 实测）：两个平台的桥都会扫这个目录，谁的线程先跑到
    谁就处理 —— A 平台扫到"发给 B 平台"的请求时，往自己这边发卡片必然失败
    （会话 ID 不通），而失败路径是 fail-closed **立刻写拒绝** ⇒ 用户在 B 平台
    刚看到卡片，点下去已经是"已失效"。现场日志：
    `卡片没发出去（invalid chatid … 93006）⇒ 直接拒绝`。
    """
    name = "approvals" if platform == "wecom" else f"approvals-{platform}"
    return os.path.join(ROOT, "data", name)


def whitelist_gate(allowed, allow_anyone, key="WECOM_ALLOWED_USERIDS",
                   what="userid", anyone_key="WECOM_ALLOW_ANYONE"):
    """启动闸门：白名单为空就**拒绝启动**（fail-closed）。

    为什么不是"警告一下继续跑"：空白名单意味着谁都能指挥它，而档位一旦是全自动，
    就等于把这台机器交出去。所以"没配"必须等于"起不来"，想开放得显式写
    `WECOM_ALLOW_ANYONE=1` —— 让开放成为一次有意识的选择。

    判据是**解析后的集合**（`parse_list`），不是原始字符串 —— 见 `parse_list` 的注释。

    返回 None = 放行；返回字符串 = 拒绝启动的原因。
    单独抽成函数是为了能被单测直接调（`main()` 还牵着平台凭据和互斥量，整段不好测）。

    `key` / `what` / `anyone_key` 是给第二个平台用的：飞书的白名单是 **open_id**，
    企微是 userid —— 同一个判据，配置名不同。默认值保持企微原文案不变。
    """
    if parse_list(allowed):
        return None
    if allow_anyone:
        return None
    return (f"{key} 为空 —— 拒绝启动。"
            "白名单是唯一的鉴权（群里的 @ 只是消息路由，不是身份）。"
            f"请填上允许指挥的 {what}（逗号分隔）；"
            f"确实要让所有人都能用，就显式设 {anyone_key}=1。")
