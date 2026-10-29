#!/usr/bin/env python3
"""企业微信 ⇄ 本机 Claude Code 的桥。

传输 + 去重 + 白名单 + 串行队列 + runner + session 表 + 长短文本推送
+ 控制指令 + 常驻化（单实例 / 自启 / 看门狗 / 日志保留 / 阻止睡眠）。

群聊与**私聊**两条路径都支持：私聊回调里没有 `chatid`（用 `from.userid` 兜底），
因此私聊天然是独立会话，但与群聊共用同一个 `cwd`。

复用自 Pi Hub 的 app/wecom_bot.py（真机跑通），删掉了 MQTT / devctl / 指令词表 / 告警格式化。
刻意保留的行为（都有踩坑记录，别"顺手简化"）：
  · 心跳线程整个进程只起一个（放在重连循环里会无限累积）；
  · 退避连上即重置（原先只增不减，一次抖动之后每次断线都按封顶等）；
  · 长连接期间只有 ping 由我们发，run_forever(ping_interval=0)。

几条用实测换来的硬约束：
  · 判成功**只看 is_error**，不看 subtype —— 实测失败那次 subtype 仍报 'success'。
  · `--disallowedTools` 的拦截**不进 permission_denials**，模型只看到工具不存在。
  · 取消/超时必须杀**进程树**（taskkill /T /F），只杀父进程会留子进程。
  · 超时的语义是「结果未知」，不许报成「失败」。
"""

import argparse
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
import websocket  # websocket-client

# ---------------- 常量 ----------------
WS_URL = "wss://openws.work.weixin.qq.com"
HEARTBEAT_SEC = 30
BACKOFF_START = 3
BACKOFF_MAX = 30

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGDIR = os.path.join(ROOT, "logs")
DEFAULT_DB = os.path.join(ROOT, "data", "bridge.db")

# 流式消息（aibot_respond_msg + stream.id）：让「收到，处理中…」和正文**共用一个气泡**。
# 两条硬约束来自官方文档 path/101463（原文已核对，见 notes/DECISIONS-2026-10-27.md §17）：
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


def whitelist_gate(allowed, allow_anyone):
    """启动闸门：白名单为空就**拒绝启动**（fail-closed）。

    为什么不是"警告一下继续跑"：空白名单意味着谁都能指挥它，而档位一旦是全自动，
    就等于把这台机器交出去。所以"没配"必须等于"起不来"，想开放得显式写
    `WECOM_ALLOW_ANYONE=1` —— 让开放成为一次有意识的选择。

    判据是**解析后的集合**（`parse_list`），不是原始字符串 —— 见 `parse_list` 的注释。

    返回 None = 放行；返回字符串 = 拒绝启动的原因。
    单独抽成函数是为了能被单测直接调（`main()` 还牵着平台凭据和互斥量，整段不好测）。
    """
    if parse_list(allowed):
        return None
    if allow_anyone:
        return None
    return ("WECOM_ALLOWED_USERIDS 为空 —— 拒绝启动。"
            "白名单是唯一的鉴权（群里的 @ 只是消息路由，不是身份）。"
            "请填上允许指挥的 userid（逗号分隔）；"
            "确实要让所有人都能用，就显式设 WECOM_ALLOW_ANYONE=1。")


def prune_old_logs(days=LOG_KEEP_DAYS):
    """删掉 N 天前的 `bridge-YYYYMMDD.log`（以及 `raw-stream/` 里的原始流）。

    按**文件名里的日期**判，不看 mtime —— mtime 会被"打开过/复制过"改写。
    启动时跑一次，**看门狗每天再跑一次**（常驻进程可能几个星期不重启，
    只在启动时清理等于根本不清理 —— 2026-10-29 复核 #9）。
    """
    cut = (datetime.now() - timedelta(days=days)).date()
    n = 0
    try:
        names = os.listdir(LOGDIR)
    except FileNotFoundError:
        return 0
    for name in names:
        if not (name.startswith("bridge-") and name.endswith(".log")):
            continue
        try:
            d = datetime.strptime(name[len("bridge-"):-len(".log")], "%Y%m%d").date()
        except ValueError:
            continue
        if d < cut:
            try:
                os.remove(os.path.join(LOGDIR, name))
                n += 1
            except OSError as e:
                log.warning("[log] 删不掉 %s: %s", name, e)
    # `raw-stream/` 里是"跑完没拿到 result"时落盘的原始流（排查用）。
    # 文件名带 `YYYYMMDD-` 前缀，同样按名字里的日期清，不看 mtime。
    d2 = os.path.join(LOGDIR, "raw-stream")
    try:
        for name in os.listdir(d2):
            if not name.endswith(".jsonl"):
                continue
            try:
                d = datetime.strptime(name[:8], "%Y%m%d").date()
            except ValueError:
                continue
            if d < cut:
                try:
                    os.remove(os.path.join(d2, name))
                    n += 1
                except OSError as e:
                    log.warning("[log] 删不掉 raw-stream/%s: %s", name, e)
    except FileNotFoundError:
        pass
    return n


class DailyFileHandler(logging.FileHandler):
    """按**日期**自动换文件的 FileHandler：跨天那一刻自己切到 `bridge-<今天>.log`。

    为什么不用 `TimedRotatingFileHandler`：它切出来的名字是 `bridge.log.2026-10-30`，
    而 `prune_old_logs` 认的是 `bridge-YYYYMMDD.log`，两边对不上就清不掉。
    ⚠️ 早先用的是普通 `FileHandler` + 启动时算一次文件名 ⇒ 常驻几周不重启的话，
    日志会一直写进**启动那天**的文件，和 README 里"按天保留 14 天"不符。
    """

    def __init__(self, dirpath, encoding="utf-8"):
        self._dir = dirpath
        self._day = datetime.now().strftime("%Y%m%d")
        self._last_pruned = self._day
        super().__init__(os.path.join(dirpath, f"bridge-{self._day}.log"),
                         encoding=encoding, delay=True)

    def emit(self, record):
        day = datetime.now().strftime("%Y%m%d")
        if day != self._day:
            try:
                self.close()          # 关掉旧文件
                self._day = day
                self.baseFilename = os.path.abspath(
                    os.path.join(self._dir, f"bridge-{day}.log"))
                self.stream = None    # 下次 emit 时按新路径重新打开
                log.info("日志跨天，切到 %s", self.baseFilename)
            except Exception:
                pass                  # 切不动就继续写旧文件，别把日志本身搞挂
        # 保留期也每天做一次（与切文件解耦：即使没切，也顺手清理）
        if day != self._last_pruned:
            self._last_pruned = day
            try:
                n = prune_old_logs()
                if n:
                    log.info("[log] 清理了 %d 个超过 %d 天的日志", n, LOG_KEEP_DAYS)
            except Exception as e:
                log.warning("[log] 清理旧日志失败: %s", e)
        super().emit(record)


def setup_logging(verbose=False):
    os.makedirs(LOGDIR, exist_ok=True)
    path = os.path.join(LOGDIR, f"bridge-{datetime.now():%Y%m%d}.log")
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")
    fh = DailyFileHandler(LOGDIR, encoding="utf-8")
    fh.setFormatter(fmt)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    # ⚠️ 自启（pythonw）时 **`sys.stdout` 是 None** —— 不加这道判断，
    # StreamHandler(None) 会在每次写日志时抛异常（被 logging 内部吞掉，但日志就少了）。
    if sys.stdout is not None:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8")   # Windows 控制台默认 GBK
        sh = logging.StreamHandler(sys.stdout)
        sh.setFormatter(fmt)
        log.addHandler(sh)
    log.info("日志文件: %s", path)
    gone = prune_old_logs()
    if gone:
        log.info("[log] 清理了 %d 个超过 %d 天的日志", gone, LOG_KEEP_DAYS)
    return path


# ---------------- Phase 4：常驻化 ----------------
_MUTEX_HANDLE = None


def acquire_single_instance(name=MUTEX_NAME):
    """单实例保护：Windows 命名互斥量，进程退出由系统自动释放（不会留脏锁）。

    为什么必须有：自启 + 手动启动（或 `--send-test` 测试）同时跑 ⇒
    两条长连接、同一条消息回两遍、两个 claude 抢同一个 sqlite 和同一个仓库。
    """
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    try:
        import ctypes
        from ctypes import wintypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateMutexW.restype = wintypes.HANDLE
        k32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
        h = k32.CreateMutexW(None, False, name)
        if not h:
            log.warning("[single] 建互斥量失败，跳过单实例保护")
            return True
        if ctypes.get_last_error() == 183:        # ERROR_ALREADY_EXISTS
            k32.CloseHandle(h)
            return False
        _MUTEX_HANDLE = h                         # 持有到进程退出
        return True
    except Exception as e:
        # 保护机制自己出错时**放行**：宁可多开一个，也别让它起不来
        log.warning("[single] 单实例保护不可用（%s），继续启动", e)
        return True


def start_keep_awake():
    """阻止系统**睡眠**（不阻止关屏），让长连接一直在。

    ⚠️ `SetThreadExecutionState` 是**按线程**生效的，线程一退出状态就没了
    ⇒ 必须有一个自己活着的线程持有它，所以这里起了一个常驻线程。
    ⚠️ 它**挡不住合盖睡眠** —— 那是电源计划里"关闭盖子时"的设置，
    要在那里也选"不采取任何操作"。文档里已写明。
    """
    if os.name != "nt":
        return None

    def run():
        import ctypes
        ES_CONTINUOUS, ES_SYSTEM_REQUIRED = 0x80000000, 0x00000001
        if not ctypes.windll.kernel32.SetThreadExecutionState(
                ES_CONTINUOUS | ES_SYSTEM_REQUIRED):
            log.warning("[power] 阻止睡眠失败（可能被组策略禁用）")
            return
        log.info("[power] 已阻止系统睡眠（不阻止关屏），进程退出时自动解除")
        while True:
            time.sleep(3600)          # 状态挂在本线程上 ⇒ 这里必须一直活着

    t = threading.Thread(target=run, daemon=True, name="keep-awake")
    t.start()
    return t


def _xml_escape(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def _autostart_xml():
    """任务计划程序 XML。几条**不写就白干**的项：

    · `DisallowStartIfOnBatteries=false` —— 笔记本默认"用电池时不许启动"，
      不写这条，一拔电源自启就废（最经典的一条）。
    · `StopIfGoingOnBatteries=false`   —— 拔电源时别杀进程。
    · `ExecutionTimeLimit=PT0S`        —— 默认 3 小时/3 天会把常驻进程收掉。
    · `RestartOnFailure`               —— 进程非 0 退出后自动重来。
    · `MultipleInstancesPolicy=IgnoreNew` —— 与互斥量双保险。
    """
    py_dir = os.path.dirname(sys.executable)
    pythonw = os.path.join(py_dir, "pythonw.exe")      # 无控制台窗口
    exe = pythonw if os.path.exists(pythonw) else sys.executable
    script = os.path.join(ROOT, "app", "bridge.py")
    user = f"{os.environ.get('USERDOMAIN', '')}\\{os.environ.get('USERNAME', '')}".strip("\\")
    today = datetime.now().strftime("%Y-%m-%dT00:00:00")
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>wecom-cc-bridge: WeCom bot bridge to local Claude Code</Description>
  </RegistrationInfo>
  <Triggers>
    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>{_xml_escape(user)}</UserId>
      <Delay>PT20S</Delay>
    </LogonTrigger>
    <!-- 每 3 分钟一次的"看它还在不在"：活着时 MultipleInstancesPolicy=IgnoreNew
         直接忽略（不产生任何进程），死了就拉起来。
         实测：「失败后重启」对"进程被强杀"这条路径**不可靠**（杀掉后等 95s 没动静），
         所以用重复触发器兜底。 -->
    <TimeTrigger>
      <StartBoundary>{today}</StartBoundary>
      <Enabled>true</Enabled>
      <Repetition>
        <Interval>PT3M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{_xml_escape(user)}</UserId>
      <LogonType>InteractiveToken</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <RestartOnFailure>
      <Interval>PT1M</Interval>
      <Count>999</Count>
    </RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{_xml_escape(exe)}</Command>
      <Arguments>-X utf8 "{_xml_escape(script)}"</Arguments>
      <WorkingDirectory>{_xml_escape(ROOT)}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _schtasks(*args):
    """schtasks 的本地化输出是 GBK（mbcs），别用 utf-8 解，否则中文全变问号。"""
    p = subprocess.run(["schtasks", *args], capture_output=True, text=True,
                       encoding="mbcs", errors="replace", timeout=60)
    return p.returncode, (p.stdout or "").strip(), (p.stderr or "").strip()


def install_autostart():
    tmp = os.path.join(tempfile.gettempdir(), "wecom-cc-bridge-task.xml")
    with open(tmp, "w", encoding="utf-16") as f:   # schtasks /XML 要 UTF-16
        f.write(_autostart_xml())
    rc, out, err = _schtasks("/Create", "/TN", TASK_NAME, "/XML", tmp, "/F")
    log.info("[autostart] 安装 rc=%s %s %s", rc, out.replace("\n", " "), err)
    return rc == 0, (out or err)


def uninstall_autostart():
    rc, out, err = _schtasks("/Delete", "/TN", TASK_NAME, "/F")
    log.info("[autostart] 卸载 rc=%s %s %s", rc, out.replace("\n", " "), err)
    return rc == 0, (out or err)


def autostart_status():
    rc, out, err = _schtasks("/Query", "/TN", TASK_NAME, "/FO", "LIST")
    return rc, (out or err)


# ---------------- 文本 ----------------
_AT_LEAD = re.compile(r"^\s*(?:@\S+[ \t]*)+")


def strip_at(text, bot_name=""):
    """去掉正文开头的 @机器人。

    企微下发的 text.content **带着 @ 前缀**。实测（2026-10-27）：机器人配名 "CC BOT"，
    但正文是 **"@CC 你好"** —— 平台只写"@ + 第一个词"。所以两条路都要走：
      ① 名字完整匹配；② 兜底剥 @token 后，再把名字剩下的部分也剥掉。
    """
    if not text:
        return text, ""
    s = text.lstrip()
    if bot_name and s.startswith("@" + bot_name):
        cut = len("@" + bot_name)
        return s[cut:].lstrip(), s[:cut]
    m = _AT_LEAD.match(s)
    if not m or not m.group(0).strip():
        return text, ""
    prefix, rest = m.group(0), s[m.end():]
    if bot_name:
        tail = " ".join(bot_name.split()[1:])
        if tail and rest.startswith(tail):
            prefix += rest[:len(tail)]
            rest = rest[len(tail):].lstrip()
    return rest, prefix


def _hard_split(s, limit):
    """按 UTF-8 字节硬切，绝不切开一个字符。"""
    out, cur, n = [], [], 0
    for ch in s:
        b = len(ch.encode("utf-8"))
        if n + b > limit and cur:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += b
    if cur:
        out.append("".join(cur))
    return out


def split_half(s):
    """按 UTF-8 字节尽量对半切（绝不切开一个字符）。切不动就原样返回。"""
    total = len(s.encode("utf-8"))
    if total < 1024:
        return [s]
    target = total // 2
    acc = 0
    for i, ch in enumerate(s):
        acc += len(ch.encode("utf-8"))
        if acc >= target:
            head, tail = s[:i + 1], s[i + 1:]
            return [head, tail] if head and tail else [s]
    return [s]


def chunk_md(text, limit_bytes=18000, reserve=24):
    """按 **UTF-8 字节**切分（不是字符数 —— 中文 3 字节/字）。优先在段落处切。"""
    lim = max(256, limit_bytes - reserve)
    if len((text or "").encode("utf-8")) <= lim:
        return [text or ""]
    paras = re.split(r"(\n{2,})", text)
    chunks, cur, cur_b = [], "", 0
    for p in paras:
        pb = len(p.encode("utf-8"))
        if pb > lim:
            if cur:
                chunks.append(cur)
                cur, cur_b = "", 0
            pieces = _hard_split(p, lim)
            # 除最后一片外都已塞满，直接落定；**最后一片留作当前片**，
            # 后面的段落还能接上 —— 否则会多出一条只含尾声（比如一行 END 标记）的
            # 空荡荡的留言（实测 21KB 被切成 17976 + 2957 + 35 三片）。
            chunks.extend(pieces[:-1])
            cur = pieces[-1]
            cur_b = len(cur.encode("utf-8"))
            continue
        if cur_b + pb > lim:
            chunks.append(cur)
            cur, cur_b = "", 0
        cur += p
        cur_b += pb
    if cur:
        chunks.append(cur)
    return chunks or [""]


def usage_footer(data):
    """上下文占用。分母直接用 modelUsage 里的 contextWindow（实测存在），不猜。"""
    mu = data.get("modelUsage") or {}
    if not mu:
        return ""
    info = next(iter(mu.values()))
    win = info.get("contextWindow") or 0
    if not win:
        return ""
    used = (info.get("inputTokens", 0) + info.get("cacheReadInputTokens", 0)
            + info.get("cacheCreationInputTokens", 0))
    pct = used * 100.0 / win

    def k(n):
        return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k"

    return f"\n\n> 📊 上下文 {pct:.1f}% ({k(used)}/{k(win)})"


def find_msg_id(msg):
    body = msg.get("body") or {}
    for container, label in ((body, "body"), (msg.get("headers") or {}, "headers"),
                             (msg, "root")):
        for f in ID_FIELDS:
            v = container.get(f)
            if v:
                return f"{label}.{f}", str(v)
    return None, None


# ---------------- 存储 ----------------
class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._c = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
            chat_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT,
            cwd TEXT NOT NULL, last_active INTEGER NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS seen_msg(
            msg_id TEXT PRIMARY KEY, seen_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_msg(seen_at);
        """)
        self._c.commit()
        self._prune()

    def _prune(self):
        cutoff = int(time.time()) - SEEN_KEEP_DAYS * 86400
        with self._lock:
            n = self._c.execute("DELETE FROM seen_msg WHERE seen_at < ?", (cutoff,)).rowcount
            self._c.commit()
        if n:
            log.info("[db] 清理过期去重记录 %d 条", n)

    def seen(self, msg_id):
        """原子地"记下并判断"：返回 True 表示**之前见过**（重复消息）。"""
        with self._lock:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO seen_msg(msg_id, seen_at) VALUES(?, ?)",
                (msg_id, int(time.time())))
            self._c.commit()
            return cur.rowcount == 0

    def get_session(self, chat_id, user_id, default_cwd):
        with self._lock:
            row = self._c.execute(
                "SELECT session_id, cwd FROM sessions WHERE chat_id=? AND user_id=?",
                (chat_id, user_id)).fetchone()
        if not row:
            return None, default_cwd
        return row["session_id"], (row["cwd"] or default_cwd)

    def save_session(self, chat_id, user_id, session_id, cwd):
        now = int(time.time())
        with self._lock:
            self._c.execute("""
                INSERT INTO sessions(chat_id,user_id,session_id,cwd,last_active,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    cwd=excluded.cwd,
                    last_active=excluded.last_active
            """, (chat_id, user_id, session_id, cwd, now, now))
            self._c.commit()

    def last_chat(self):
        """最近活跃的会话 → `(chat_id, chat_type)`，给运维命令/自愈通知兜底。

        `chat_type` 是**推出来的**：本项目的会话键约定是
        「单聊 = (userid, userid)、群聊 = (群chatid, userid)」
        ⇒ `chat_id == user_id` 就是单聊(1)，否则群聊(2)。
        （写死 2 的话，最近只在私聊说过话时，通知会带着错的类型发不出去。）
        """
        with self._lock:
            row = self._c.execute(
                "SELECT chat_id, user_id FROM sessions "
                "ORDER BY last_active DESC LIMIT 1").fetchone()
        if not row:
            return None, None
        return row["chat_id"], (1 if row["chat_id"] == row["user_id"] else 2)

    def clear_session(self, chat_id, user_id):
        with self._lock:
            self._c.execute(
                "UPDATE sessions SET session_id=NULL, last_active=? WHERE chat_id=? AND user_id=?",
                (int(time.time()), chat_id, user_id))
            self._c.commit()


# ---------------- Claude runner ----------------
def _clip(s, n):
    """压成一行、截到 n 字符。气泡里的一行进度，不许换行撑开。"""
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: max(1, n - 1)] + "…"


def _short_path(p, limit=48):
    """长路径压成「上一级/文件名」。绝对路径会把气泡那一行挤爆。"""
    s = str(p or "").replace("\\", "/").strip()
    parts = [x for x in s.split("/") if x]
    if not parts:
        return ""
    s = "/".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    return s if len(s) <= limit else "…" + s[-(limit - 1):]


# 工具名 → 进度短语。**只做"看得懂现在在干什么"**，不回显完整入参。
# 未列出的工具走最后一行的兜底（新工具出现时也不会哑掉）。
def tool_desc(name, inp):
    """把一次 `tool_use` 压成气泡里的一行进度。"""
    inp = inp or {}
    name = name or "?"
    if name == "Read":
        return "读 " + _short_path(inp.get("file_path"))
    if name in ("Edit", "Write", "MultiEdit"):
        return "改 " + _short_path(inp.get("file_path"))
    if name == "NotebookEdit":
        return "改 " + _short_path(inp.get("notebook_path"))
    if name == "Bash":
        # 命令常常很长且带换行；`description` 是模型自己写的短标签，优先用它
        d = str(inp.get("description") or "").strip()
        return "运行 " + _clip(d or str(inp.get("command") or ""), 40)
    if name == "Grep":
        return "搜 " + _clip(str(inp.get("pattern") or ""), 40)
    if name == "Glob":
        return "找 " + _clip(str(inp.get("pattern") or ""), 40)
    if name in ("Task", "Agent"):
        return "派子任务 " + _clip(str(inp.get("description")
                                       or inp.get("subagent_type") or ""), 34)
    if name == "WebFetch":
        return "取网页 " + _clip(str(inp.get("url") or ""), 40)
    if name == "WebSearch":
        return "搜网 " + _clip(str(inp.get("query") or ""), 40)
    if name == "TodoWrite":
        return "更新待办"
    if name == "Skill":
        return "用技能 " + _clip(str(inp.get("skill") or ""), 34)
    return "用 " + name


class Runner:
    def __init__(self, exe, model, perm_mode, sources, timeout,
                 allowed, disallowed, inject_env, show_usage):
        self.exe = exe
        self.model = model
        self.perm_mode = perm_mode
        self.sources = sources
        self.timeout = timeout
        self.allowed = allowed
        self.disallowed = disallowed
        self.inject_env = inject_env
        self.show_usage = show_usage
        self._lock = threading.Lock()
        self._proc = None
        self._stopped = False
        # 上一次 kill_current 的结果：idle / killed / failed（见 kill_current）
        self.kill_status = "idle"

    def build_env(self):
        """剥掉继承的 ANTHROPIC*/CLAUDE*，再用桥自己 .env 里的值注入。

        理由（实测）：排掉 user 设置后认证就没了；而继承来的环境变量会让
        "认证成功"变成假阳性。桥的真实运行环境本来也不该依赖父进程。
        """
        e = {k: v for k, v in os.environ.items()
             if not (k.upper().startswith("ANTHROPIC") or k.upper().startswith("CLAUDE"))}
        e.update(self.inject_env)
        return e

    def build_cmd(self, session_id):
        # `stream-json` + 增量读：`json` 模式要等进程结束才有输出，**没有拦截点**
        # ⇒ 真进度气泡和审批卡片都做不出来。`result` 事件字段与 `json` 模式完全一致（实测）。
        # ⚠️ `--verbose` 是**硬要求**：不带它 `claude` 直接
        #    `rc=1 / --output-format=stream-json requires --verbose`（实测 2.1.216）。
        cmd = [self.exe, "-p", "--output-format", "stream-json", "--verbose"]
        if self.sources:
            cmd += ["--setting-sources", self.sources]
        if self.perm_mode:
            cmd += ["--permission-mode", self.perm_mode]
        if self.model:
            cmd += ["--model", self.model]
        if self.allowed:
            cmd += ["--allowedTools", self.allowed]
        if self.disallowed:
            cmd += ["--disallowedTools", self.disallowed]
        if session_id:
            cmd += ["--resume", session_id]
        return cmd

    def kill_current(self):
        """终止当前任务。返回 True **仅当确认进程已退出**。

        ⚠️ 早先只要"调 taskkill 前进程还活着"就返回 True —— `taskkill` 失败、
        或者进程根本没死，用户也会收到「已停止（进程树已清理）」。
        现在按 `p.poll()` 复核，并把结果写进 `kill_status`：
        `idle`（本来就没在跑）/ `killed`（确认已退出）/ `failed`（发了但没退）。
        """
        with self._lock:
            p = self._proc
            self._stopped = True
        if p is None or p.poll() is not None:
            self.kill_status = "idle"
            return False
        _kill_tree(p.pid)
        deadline = time.time() + 5
        while time.time() < deadline:
            if p.poll() is not None:
                self.kill_status = "killed"
                return True
            time.sleep(0.1)
        self.kill_status = "failed"
        log.error("[kill] pid=%s 5 秒内没有退出 —— 可能还在跑，别告诉用户已清理", p.pid)
        return False

    def clear_stop(self):
        with self._lock:
            self._stopped = False

    def run(self, prompt, session_id, cwd, on_progress=None, extra_env=None):
        """跑一轮。返回 dict：ok / text / session_id / is_error / denials / usage / 超时·停止标志。

        `on_progress(desc, n_tools)` 每读到一次**工具调用**就回调一次（可能很密，
        节流交给调用方 —— 这里只保证"不阻塞读管道"）。

        **为什么不用 `subprocess.run` 一把拿完整 JSON**（2026-10-29 改）：
        那样只能等进程结束才知道发生了什么，没有拦截点 ⇒ 真进度气泡做不出来，
        将来的审批卡片也做不出来。现在的形状是"三条读线程 + 主线程等退出"：
          · stdout：逐行解析 `stream-json`，**只认** `system/init`、`assistant(tool_use)`、`result`；
          · stderr：必须有人读，否则管道写满会把子进程卡死（这是 `communicate` 替我们做的）；
          · stdin：单独一个线程喂 prompt。prompt 很大时阻塞写会卡住"等退出"那一步。
        """
        cmd = self.build_cmd(session_id)
        log.info("[run] cmd=%s cwd=%s", " ".join(cmd[1:]), cwd)
        t0 = time.time()
        # 审批卡片要靠环境变量把「决策目录 + 发给哪个会话」传给 hook 子进程
        # （实测：hook 能完整继承桥注入的环境变量，DECISIONS §25 E7）
        env = self.build_env()
        if extra_env:
            env.update(extra_env)
        try:
            # CREATE_NO_WINDOW: 桥常驻后别让子进程弹黑框
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            p = subprocess.Popen(
                cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, encoding="utf-8",
                errors="replace", cwd=cwd, env=env,
                creationflags=flags)
        except FileNotFoundError as e:
            return {"ok": False, "fatal": True,
                    "text": f"找不到 claude 可执行文件：{self.exe}\n{e}"}
        with self._lock:
            self._proc = p
            stop_now = self._stopped      # `/stop` 恰好落在 clear_stop() 与 Popen 之间
        if stop_now:
            # 不补这一枪的话：用户看到"当前没有正在跑的任务"，任务却**真跑完了**
            # （副作用照做），而结果被报成"已停止"扔掉了。
            log.warning("[run] 启动瞬间已收到 /stop，立即终止 pid=%s", p.pid)
            _kill_tree(p.pid)

        acc = {"result": None, "init": None, "events": 0, "last": None,
               "tools": 0, "bad": 0, "raw": [], "raw_bytes": 0, "raw_dropped": 0,
               "stderr": ""}
        t_out = threading.Thread(target=self._read_stdout, args=(p, acc, on_progress),
                                 daemon=True, name="run-stdout")
        t_err = threading.Thread(target=self._read_stderr, args=(p, acc),
                                 daemon=True, name="run-stderr")
        t_in = threading.Thread(target=self._feed_stdin, args=(p, prompt),
                                daemon=True, name="run-stdin")
        t_out.start()
        t_err.start()
        t_in.start()

        timed_out = False
        kill_failed = False
        try:
            p.wait(timeout=self.timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_tree(p.pid)
            try:
                p.wait(timeout=15)
            except Exception as e:
                kill_failed = True
                log.error("[kill] pid=%s 强杀后仍不退出（%s）⇒ 可能残留进程树，请按 PID 核查",
                          p.pid, e)
        finally:
            with self._lock:
                # 杀不掉的进程**保留引用**，让后续 /stop 还能再试一次；清掉就再也够不着它了
                self._proc = p if kill_failed else None
                stopped = self._stopped
        # 等读线程把尾巴收干净：正常退出会立刻 EOF；被强杀时也应当很快 EOF。
        # ⚠️ 杀不掉的进程不会给 EOF ⇒ 绝不能无限等（那样 /stop 就变成"卡住"）。
        t_out.join(timeout=5)
        t_err.join(timeout=5)
        if t_out.is_alive():
            log.error("[run] pid=%s 退出后 stdout 读线程仍未收尾（残留进程？），按已读到的内容继续",
                      p.pid)
        dur = round(time.time() - t0, 1)

        data = acc["result"] or {}
        init = acc["init"] or {}
        err = acc["stderr"] or ""
        if init.get("session_id"):
            # 这行很有用：**进程中途死掉时**（超时/被杀）也能知道本轮是哪个 session
            log.info("[run] init session=%s model=%s perm=%s tools=%d",
                     init["session_id"], init.get("model"), init.get("permissionMode"),
                     len(init.get("tools") or []))
        if err.strip():
            log.info("[run] stderr: %s", err.strip()[:400])
        log.info("[run] 事件 %d 条（工具 %d，坏行 %d）", acc["events"], acc["tools"], acc["bad"])

        res = {
            "ok": False,
            "text": data.get("result") or "",
            "session_id": data.get("session_id"),
            "is_error": data.get("is_error"),
            "subtype": data.get("subtype"),
            "denials": data.get("permission_denials") or [],
            "usage_footer": usage_footer(data) if self.show_usage else "",
            "timed_out": timed_out,
            "stopped": stopped and not timed_out,
            "rc": p.returncode,
            "duration": dur,
            "tools": acc["tools"],
            "events": acc["events"],
            "init_session_id": init.get("session_id"),
            "raw_path": None,
            "stream_failed": False,
        }
        # 判成功**只看 is_error**（实测失败那次 subtype 仍报 'success'）；没拿到 result 也算失败
        res["ok"] = (not timed_out) and (not stopped) and bool(data) and not data.get("is_error")
        if not data and not timed_out and not stopped:
            res["stream_failed"] = True
            res["raw_path"] = self._dump_raw_stream(acc)
            where = (f"\n原始流已落盘：`{res['raw_path']}`" if res["raw_path"]
                     else "\n原始流落盘失败，只剩上面这些统计。")
            res["text"] = (
                f"没拿到 claude 的 result 事件（rc={p.returncode}，用时 {dur}s，"
                f"收到 {acc['events']} 条事件，最后一条 `{acc['last']}`，"
                f"已执行 {acc['tools']} 次工具）。\n"
                f"> 语义是**结果未知**：工具可能已经真的跑过了 —— 重试前先核实实际状态，别直接重发。"
                f"{where}"
                + (f"\nstderr: {err.strip()[:300]}" if err.strip() else ""))
            res["is_error"] = True
        log.info("[run] rc=%s is_error=%s 耗时=%ss 超时=%s 停止=%s denials=%d",
                 p.returncode, res["is_error"], dur, timed_out, res["stopped"],
                 len(res["denials"]))
        return res

    # -- 三条读线程（都不许抛出去：抛了没人接，主线程会误判成"正常结束"）--
    def _read_stdout(self, p, acc, on_progress):
        """逐行解析 `stream-json`。

        ⚠️ 其余事件必须**便宜地丢掉**：一个很小的任务里 `system/thinking_tokens`
        就有 44 条（实测），逐条深解析纯属浪费，还会拖慢读管道。
        """
        try:
            for raw in p.stdout:
                line = raw.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    acc["bad"] += 1
                    continue
                acc["events"] += 1
                t = d.get("type")
                acc["last"] = t if t != "system" else f"system/{d.get('subtype')}"
                # 原始流只为"失败时落盘"留，正常跑完就随 acc 一起扔掉
                b = len(line) + 1
                if acc["raw_bytes"] + b <= PROGRESS_RAW_CAP:
                    acc["raw"].append(line)
                    acc["raw_bytes"] += b
                else:
                    acc["raw_dropped"] += 1
                if t == "result":
                    acc["result"] = d
                elif t == "system":
                    if d.get("subtype") == "init":
                        acc["init"] = d
                elif t == "assistant":
                    for c in (d.get("message") or {}).get("content") or []:
                        if c.get("type") != "tool_use":
                            continue
                        acc["tools"] += 1
                        if on_progress:
                            try:
                                on_progress(tool_desc(c.get("name"), c.get("input")),
                                            acc["tools"])
                            except Exception as e:
                                # 进度是"锦上添花"，它出错绝不能拖垮这一轮
                                log.warning("[run] on_progress 抛错（已忽略）: %s", e)
        except Exception as e:
            log.warning("[run] 读 stdout 中断: %s", e)
        finally:
            try:
                p.stdout.close()
            except Exception:
                pass

    def _read_stderr(self, p, acc):
        """stderr **必须有人读** —— 管道写满会把子进程卡死。只留尾部 4 KB。"""
        buf = []
        size = 0
        try:
            for raw in p.stderr:
                if size < 65536:
                    buf.append(raw)
                    size += len(raw)
        except Exception as e:
            log.info("[run] 读 stderr 中断: %s", e)
        finally:
            acc["stderr"] = "".join(buf)[-4000:]
            try:
                p.stderr.close()
            except Exception:
                pass

    def _feed_stdin(self, p, prompt):
        """喂 prompt 后**立刻关掉** stdin —— 不关的话子进程会一直等输入，永远不结束。"""
        try:
            p.stdin.write(prompt)
        except Exception as e:
            # 进程可能已经被 /stop 杀了；这不是错误，交给上层判定
            log.info("[run] 写 stdin 失败（进程可能已退出）: %s", e)
        finally:
            try:
                p.stdin.close()
            except Exception:
                pass

    def _dump_raw_stream(self, acc):
        """失败时把原始流落盘（用户 2026-10-29 定：**不重跑**，留证据）。

        不重跑的理由：工具副作用可能已经发生了，重跑等于把同一批工具再执行一遍。
        """
        try:
            d = os.path.join(LOGDIR, "raw-stream")
            os.makedirs(d, exist_ok=True)
            fn = os.path.join(d, datetime.now().strftime("%Y%m%d-%H%M%S-%f") + ".jsonl")
            with open(fn, "w", encoding="utf-8", newline="\n") as f:
                f.write("\n".join(list(acc["raw"])))
                f.write("\n")
            log.warning("[run] 原始流已落盘 %s（%d 字节，丢弃 %d 行）",
                        fn, acc["raw_bytes"], acc["raw_dropped"])
            try:
                return os.path.relpath(fn, ROOT)
            except Exception:
                return fn
        except Exception as e:
            log.error("[run] 原始流落盘失败: %s", e)
            return None


def approval_configured(cc_cwd, sources):
    """静态检查：**Claude 到底会不会加载这个 hook**。

    为什么还要这一道（2026-10-29 复核 #2②）：`approval_selfcheck` 只是桥自己把
    `python tools/approval_hook.py` 跑一遍 —— 它证明"脚本能跑"，**不证明 Claude 会调它**。
    把 `.claude/settings.json` 删掉、或者 `CC_SETTING_SOURCES` 里没有 `project`，
    自检照样通过，而实际一个审批卡片都不会弹。那就又成了"假闸门"。

    查三件事：
      ① `.claude/settings*.json` 存在且是合法 JSON
         （⚠️ 校验失败时 CLI 是**静默忽略**整份文件，`-p` 模式不弹提示）；
      ② 里面有 `hooks.PreToolUse`，且命令串指向 `approval_hook.py`；
      ③ `--setting-sources` 含 `project`。（⚠️ `CC_SETTING_SOURCES` **留空不等于不传**：
         `cfg()` 把空值当未设置 ⇒ 落到 `main()` 的默认值 `project,local`，见那里。
         只有显式写成不含 `project` 的值才会真的把它排掉。）

    返回 (ok, 说明)。说明文字会被打进日志，所以写成"现在是什么状态"，别只写"失败"。
    """
    srcs = [s.strip() for s in (sources or "").split(",") if s.strip()]
    if srcs and "project" not in srcs:
        return False, (f"CC_SETTING_SOURCES={sources!r} 不含 project ⇒ "
                       f"{os.path.join(cc_cwd, '.claude')} 下的 settings 不会被加载，hook 不会跑")
    d = os.path.join(cc_cwd, ".claude")
    cands = [os.path.join(d, n) for n in ("settings.json", "settings.local.json")]
    found = []
    for p in cands:
        if not os.path.isfile(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                found.append((p, json.load(f)))
        except Exception as e:
            return False, (f"{p} 不是合法 JSON（{e}）—— settings 校验失败会被**静默忽略**，"
                           f"等于 hook 不存在")
    if not found:
        return False, (f"找不到 {cands[0]} —— 没有它就没有 PreToolUse hook，"
                       f"审批一张卡都不会弹")
    for p, data in found:
        for group in ((data.get("hooks") or {}).get("PreToolUse") or []):
            for item in (group.get("hooks") or []):
                cmd = str(item.get("command") or "")
                if "approval_hook.py" in cmd:
                    return True, f"{os.path.basename(p)} 已注册 PreToolUse（{cmd[:70]}）"
    return False, (f"{os.path.basename(found[0][0])} 里没有注册 tools/approval_hook.py 的 "
                   f"PreToolUse hook")


def approval_selfcheck(cc_cwd, approval_dir, wait_sec=3.0):
    """运行时自检：确认**那条 hook 脚本真的跑得起来**。

    为什么必须自检（`DECISIONS` §25）：harness 的所有异常路径都是**放行**
    （超时、hook 崩溃、字段写错）。如果 hook 根本没跑起来（比如 PATH 里没有
    `python`），审批会**静默失效**而桥自己完全不知道 —— 那就等于假装有闸门。
    所以：跑一次假的 Write 事件，要求它在临时目录里吐出 `.req.json`。

    用 `shell=True` + 和 `.claude/settings.json` 里**同一个命令串**，能抓到
    "这条命令本身跑不起来"（PATH 没有 python、路径不对、解释器坏了）。

    ⚠️ 但它**不证明 Claude 会调它** —— 那是 `approval_configured` 的活。两道都要过。
    """
    script = os.path.join(cc_cwd, "tools", "approval_hook.py")
    if not os.path.isfile(script):
        return False, f"找不到 {script}"
    tmp = tempfile.mkdtemp(prefix="ccbridge-selfcheck-")
    env = dict(os.environ)
    env.update({
        "CC_APPROVAL_DIR": tmp,
        "CC_APPROVAL_TARGET": "selfcheck|2|selfcheck",
        "CC_APPROVAL_WAIT_SEC": "5",
        "PYTHONIOENCODING": "utf-8",
    })
    ev = json.dumps({"hook_event_name": "PreToolUse", "tool_name": "Write",
                     "tool_input": {"file_path": "selfcheck.txt"},
                     "session_id": "selfcheck", "cwd": cc_cwd})
    try:
        p = subprocess.Popen("python tools/approval_hook.py", shell=True,
                             cwd=cc_cwd, env=env, stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding="utf-8", errors="replace")
        p.stdin.write(ev)
        p.stdin.close()
    except Exception as e:
        return False, f"hook 起不来：{e}"
    t0 = time.time()
    found = None
    while time.time() - t0 < wait_sec:
        try:
            names = [n for n in os.listdir(tmp) if n.endswith(".req.json")]
        except FileNotFoundError:
            names = []
        if names:
            found = names[0]
            break
        time.sleep(0.1)
    if not found:
        err = ""
        try:
            err = (p.stderr.read() or "")[:300]
        except Exception:
            pass
        try:
            _kill_tree(p.pid)
        except Exception:
            pass
        return False, f"{wait_sec}s 内没有写出审批请求（hook 没跑起来？）{err}"
    # 放它走，别留一个挂着等 5 秒的进程
    try:
        with open(os.path.join(tmp, found[:-len(".req.json")] + ".decision"),
                  "w", encoding="utf-8") as f:
            f.write("deny\n")
        p.wait(timeout=5)
    except Exception:
        try:
            _kill_tree(p.pid)
        except Exception:
            pass
    return True, "ok"


def _kill_tree(pid):
    """Windows 上必须杀进程树 —— Claude 会拉起子进程。

    ⚠️ **要看返回码**：`taskkill` 失败（进程已换主、权限不足、PID 被复用）时
    它只往 stderr 说话、退出码非 0，早先这里把结果整个吞掉了。
    返回 True = 命令自报成功（**不等于进程已退出**，那要 `p.poll()` 复核）。
    """
    try:
        r = subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                           capture_output=True, timeout=20)
        if r.returncode == 0:
            log.info("[kill] 已终止进程树 pid=%s", pid)
            return True
        log.warning("[kill] taskkill 退出码=%s pid=%s stderr=%s", r.returncode, pid,
                    (r.stderr or b"")[:200])
        return False
    except Exception as e:
        log.warning("[kill] 失败 pid=%s: %s", pid, e)
        return False


# ---------------- 流式消息（一个气泡装 ack + 正文）----------------
class Stream:
    """一次回调对应的一条流式消息：`aibot_respond_msg` + 自定义 `stream.id`。

    **为什么要有它**（用户 2026-10-28 提的调整）：原来 ack 走 `response_url` HTTP POST、
    正文走 `aibot_send_msg`，手机上就是**两条独立气泡** —— 看着像"还有个对话在跑"。
    平台本身支持 `stream`：首次 `finish=false` 建气泡，**同一个 `stream.id`** 再发就
    覆盖内容（`content` 是**全文**，不是增量），`finish=true` 定稿。
    ⇒ ack 和正文落在同一个气泡里，正文直接把「收到，处理中…」盖掉。

    ⚠️ 三条约束（少了任何一条都会**静默坏掉**）：
      · `headers.req_id` 必须是**回调里的那个** req_id（平台拿它关联回调与回复）；
        我们自己编的 req_id 平台不认。
      · 首帧起 10 分钟内必须定稿，超了平台自动结束 ⇒ 之后所有更新无效，
        只能改走 `aibot_send_msg` 另发一条（`send()` 会返回 `dead=True`）。
      · 已 `finish=true` 的气泡**不可再更新** ⇒ 内部记 `closed`，别指望"再补一句"。
    """

    def __init__(self, bridge, req_id, chatid, chat_type):
        self.b = bridge
        self.req_id = req_id
        self.chatid = chatid
        self.chat_type = chat_type
        self.id = "s" + uuid.uuid4().hex[:24]
        self.started = time.time()
        self.closed = False      # 已定稿，或已判定不可用
        self.created = False     # 首帧是否成功建出气泡（仅用于日志/诊断）
        self._lock = threading.Lock()

    def remaining(self):
        return STREAM_MAX_SEC - (time.time() - self.started)

    def send(self, content, finish):
        """发/更新气泡内容。返回值同 `_send_wait`，另加 `dead`：

        `dead=True` ⇒ **这一帧什么都没发出去**，气泡不可用（已关闭或超窗口），
        调用方可以安全地改走主动推送 —— 不会重复投递。
        （`errcode is None` 且没有 `dead` 才是"结果未知"，那种不许重发。）
        """
        nbytes = len(content.encode("utf-8"))
        with self._lock:
            # 「气泡还能不能用」优先于「内容多长」：已关闭就别再让调用方去切片，
            # 否则它会先切一轮、再发现气泡早就没了。
            if self.closed:
                return {"sent": False, "errcode": None, "errmsg": "stream closed", "dead": True}
            if self.remaining() <= 0:
                self.closed = True
                log.warning("[stream] 已超过 %ds 窗口（气泡 %s），本条改走主动推送",
                            STREAM_MAX_SEC, self.id[:8])
                return {"sent": False, "errcode": None, "errmsg": "stream expired", "dead": True}
            if nbytes > STREAM_LIMIT:
                # 注定被 40058 拒，别浪费一次往返；语义保持和平台一致，让调用方走切片分支
                return {"sent": False, "errcode": 40058,
                        "errmsg": f"local: content {nbytes}B > {STREAM_LIMIT}"}

            frame = {
                "cmd": "aibot_respond_msg",
                "headers": {"req_id": self.req_id},
                "body": {"msgtype": "stream",
                         "stream": {"id": self.id, "finish": bool(finish),
                                    "content": content}},
            }
            r = self.b._send_wait(
                frame, f"stream {self.id[:8]} finish={finish} {nbytes}B")
            if r["sent"]:
                self.created = True
                if finish:
                    self.closed = True
            elif r.get("unsent"):
                # 连接不在，这一帧压根没出去 ⇒ 气泡没建出来，交给调用方改道推送
                self.closed = True
                r = dict(r, dead=True)
            elif r["errcode"] is None:
                # 帧发出去了但没有回执 ⇒ **结果未知**（可能已经落地）。
                # ⚠️ 两种处理都不能错：
                #   · 不能标 `dead` —— dead 的约定是"什么都没发出去、可以安全改道"，
                #     而这次可能恰恰发出去了，标了就会重复投递；
                #   · 也**不要**判死气泡 —— 回执丢一次（心跳回执被误当、平台慢）很常见，
                #     真正落地时后续同一 id 仍能覆盖。留下来继续用，代价只是"ack 可能卡着"；
                #     判死反而是**确定**退化成两个气泡。
                log.warning("[stream] 气泡 %s 这一帧没回执（结果未知），仍继续用这个 id",
                            self.id[:8])
            return r


# ---------------- 真进度：把工具进度节流后写进同一个气泡 ----------------
class ProgressPump:
    """收 `Runner` 报来的工具进度，节流后更新**流式气泡**（与 ack/正文同一个 id）。

    **为什么必须是单独一个线程**：`aibot_respond_msg` 要等平台回执（最多 `SEND_WAIT_SEC`
    15 秒）。如果直接在读 stdout 的线程上发帧，一次网络抖动就会让 stdout 管道没人读、
    写满 ⇒ **把 claude 子进程堵死**。所以：读线程只调 `update()`（写字符串 + 置事件，
    不碰网络），真正发帧在这个线程里。

    节流口径（用户 2026-10-29 定）：
      · **只在「工具/目标变了」时推** —— 同一条命令连跑 5 次只算一次；
      · 没变化时每 `heartbeat` 秒补一条兜底，让用户知道它还活着。
    一帧就是一条限流额度（30 条/分钟、1000 条/小时），所以宁少勿多。
    """

    def __init__(self, st, chatid, chat_type, heartbeat_sec=PROGRESS_HEARTBEAT_SEC):
        self.st = st
        self.chatid = chatid
        self.chat_type = chat_type
        self.hb = max(0.0, float(heartbeat_sec or 0))
        self.started = time.time()
        self._lock = threading.Lock()
        self._desc = ""
        self._tools = 0
        self._dirty = threading.Event()
        self._stop = threading.Event()
        self._last_sent = 0.0
        self.sent = 0            # 真发出去的帧数（测试/诊断用）
        self._t = threading.Thread(target=self._loop, daemon=True, name="progress-pump")
        self._t.start()

    def _content(self, desc, n):
        head = f"⏳ 正在跑（{time.time() - self.started:.0f}s · 第 {n} 步）"
        return head + (f"\n🛠 {desc}" if desc else "")

    def _push(self, desc, n):
        self._last_sent = time.time()
        r = self.st.send(self._content(desc, n), finish=False)
        if r.get("dead"):
            # 气泡已经不可用（超 10 分钟窗口 / 已定稿）⇒ 别再拿它发任何东西
            log.warning("[progress] 气泡不可用（%s）⇒ 停止真进度", r.get("errmsg"))
            self._stop.set()
            return
        if r["sent"]:
            self.sent += 1
        elif r["errcode"] is None:
            log.warning("[progress] 这一帧没回执（结果未知），继续用同一个 id")

    def _loop(self):
        while not self._stop.is_set():
            timeout = None
            if self.hb:
                timeout = max(0.05, self.hb - (time.time() - self._last_sent))
            fired = self._dirty.wait(timeout=timeout)
            if self._stop.is_set():
                return
            with self._lock:
                desc, n = self._desc, self._tools
            if fired:
                self._dirty.clear()
                self._push(desc, n)
            elif self.hb and n:
                self._push(desc, n)      # 兜底心跳：没新进展也要让用户知道它还活着

    def update(self, desc, n_tools=0):
        """读线程调用。**只在变化时才置位** —— 这就是"变化时推"。"""
        with self._lock:
            if desc == self._desc and n_tools == self._tools:
                return
            self._desc, self._tools = desc, int(n_tools or 0)
        self._dirty.set()

    def close(self):
        """收口。**必须在正文定稿之前调**，否则迟到的进度会盖掉答案。"""
        self._stop.set()
        self._dirty.set()        # 让 wait 立刻返回
        if self._t.is_alive():
            self._t.join(timeout=SEND_WAIT_SEC + 2)


# ---------------- 桥本体 ----------------
class Bridge:
    def __init__(self, bot_id, secret, allow_users, allow_chats, bot_name,
                 store, runner, cc_cwd, chunk_bytes, queue_max, progress_sec,
                 dump_raw=False, stale_rx_sec=0, progress_hb_sec=PROGRESS_HEARTBEAT_SEC,
                 approval_dir=None, approval_wait_sec=120, approval_allow=""):
        self.bot_id = bot_id
        self.secret = secret
        self.allow_users = parse_list(allow_users)
        self.allow_chats = parse_list(allow_chats)
        self.bot_name = bot_name
        self.store = store
        self.runner = runner
        self.cc_cwd = cc_cwd
        self.chunk_bytes = chunk_bytes
        self.progress_sec = progress_sec
        # 真进度：有气泡时由 ProgressPump 按"变化时推 + 这个间隔兜底"更新；
        # `progress_sec` 那个定时器只在**没有气泡**时兜底（见 `_do_job`）。
        self.progress_hb_sec = float(progress_hb_sec or 0)
        # 审批卡片：`approval_dir` 为 None = 关闭（默认）。开了之后每轮任务都把
        # 「决策目录 + 目标会话」注入 claude 的环境，PreToolUse hook 才肯干活。
        self.approval_dir = approval_dir or ""
        self.approval_wait_sec = float(approval_wait_sec or 0)
        self.approval_allow = approval_allow or ""
        self._approvals = {}       # id -> {chatid, chat_type, userid, ts, tool}
        self._approval_lock = threading.Lock()
        self.dump_raw = dump_raw
        self.stale_rx_sec = int(stale_rx_sec or 0)   # 0 = 关闭惰性连接检测（先量再开）

        self.ws = None
        self.subscribed = threading.Event()
        self.stop = False
        self._send_lock = threading.Lock()
        self._backoff = BACKOFF_START
        self._shape_logged = False

        self._q = queue.Queue(maxsize=queue_max)
        self._qmax = queue_max
        self._job_lock = threading.Lock()
        self._current = None       # {started, text, userid, chatid}

        # 会话"代次"：`/new` / 卡片「清空」时 +1。
        # 为什么需要（2026-10-29 外部复核 #5）：`/new` 清完库，**正在跑**的旧任务
        # 结束后仍会把旧 session 写回去 ⇒ 用户以为清空了，下一轮却又接上了旧上下文。
        # 任务入队时记下当时的代次，落库前比一次，变了就丢弃。
        self._epoch_lock = threading.Lock()
        self._epoch = {}           # (chatid, userid) -> int

        # 最近一次跑完的上下文占用（`📊 上下文 x% (m/n)` 那一行）。
        # 只放内存：/status 的卡片要显示它，但落库要改表结构，暂时不值得。
        # 重启后为空 ⇒ 卡片上就显示"—"，不编造。
        self._last_usage = {}      # (chatid, userid) -> "📊 上下文 …"

        # 推送回执等待：`_send` 返回 True 只说明 WS 帧发出去了，
        # 平台是**异步**用 errcode 回执拒收的（实测 40058 content exceed max length）。
        self._receipt_lock = threading.Lock()   # 同一时刻只让一个发送方在等
        self._receipt_cv = threading.Condition()
        self._receipt = None
        self._receipt_pending = False
        self._receipt_want = ""    # 正在等哪一帧的回执（req_id），用于过滤无关回执

        # Phase 4：看门狗用。`_last_rx` 是最后一次收到**任何**帧的时刻。
        self._last_rx = time.time()
        self._threads = {}          # name -> Thread，看门狗据此判断谁死了

        # 被动回复走独立队列 + 独立线程（不能卡住 websocket 读线程，见 reply()）
        self._reply_q = queue.Queue(maxsize=32)

    # -- 底层 --
    def _new_req_id(self):
        return uuid.uuid4().hex

    def _deliver_receipt(self, rc, emsg="", req_id=None):
        """把平台回执交给**正在等待的那个**发送方（同一时刻只允许一个发送方等）。

        `rc is None` = 这个帧没有 errcode，不是回执，别去叫醒等待方。

        ⚠️ **能对上 req_id 就必须对上**（2026-10-29 复核 #4）：早先任何带 errcode 的
        帧都会叫醒等待方 —— 上一条消息的迟到回执、甚至心跳相关帧，都可能被当成
        "这一条发成功了"。现在：
          · 回执**带** `req_id` ⇒ 必须等于我正在等的那一个，否则忽略（记日志）；
          · 回执**不带** ⇒ 保持旧行为（平台当前的回执到底带不带，我们还没实测过，
            `DECISIONS` §16.3 仍是待办）—— 不能凭空收紧，否则会把真回执也丢掉。
        """
        if rc is None:
            return
        with self._receipt_cv:
            want = self._receipt_want
            if req_id and want and req_id != want:
                log.info("[send] 忽略无关回执 req_id=%s（正在等 %s）", req_id[:8], want[:8])
                return
            if self._receipt_pending and self._receipt is None:
                self._receipt = (rc, emsg)
                self._receipt_cv.notify_all()

    def _send(self, obj):
        with self._send_lock:
            if self.ws is None:
                return False
            try:
                self.ws.send(json.dumps(obj, ensure_ascii=False))
                return True
            except Exception as e:
                log.warning("[ws] send 失败: %s", e)
                return False

    # -- 两条回复通道 --
    def send_markdown(self, chatid, chat_type, content, wait=True):
        """主动推送。返回 {sent, errcode, errmsg}。

        ⚠️ 必须**等平台回执**才算数：`_send` 的 True 只代表 WS 帧发出去了。
        实测平台会异步回 errcode 拒收（40058 = body.markdown.content exceed max length 20480）。
        早先的版本把 True 当成功 ⇒ 分片被拒也会当成送达，属于静默失败。
        """
        if not chatid:
            log.warning("[send] 无 chatid，跳过推送")
            return {"sent": False, "errcode": None, "errmsg": "no chatid"}
        frame = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._new_req_id()},
            "body": {"chatid": chatid, "chat_type": int(chat_type),
                     "msgtype": "markdown", "markdown": {"content": content}},
        }
        nbytes = len(content.encode("utf-8"))
        if not wait:
            self._send(frame)
            log.info("[send] markdown -> %s (type=%s) %dB 不等回执", chatid, chat_type, nbytes)
            return {"sent": True, "errcode": None, "errmsg": ""}
        return self._send_wait(frame, f"markdown -> {chatid} (type={chat_type}) {nbytes}B")

    def send_card(self, chatid, chat_type, card, wait=True):
        """主动推送**模板卡片**（`msgtype=template_card`）。

        卡片是 markdown 之外唯一被 `aibot_send_msg` 支持的富消息类型。
        能发哪种卡由 `template_card.card_type` 决定：
          · `text_notice` —— 纯展示，无回调要求；
          · `button_interaction` —— 带按钮，点了会回调 `template_card_event`。
        ⚠️ 官方文档写着「按钮类卡片要求机器人**设置了回调URL**」(`101032:1307`)，
        而我们是**长连接**模式（没有回调 URL）⇒ 这类卡片能不能下发**必须实测**，
        所以这里不假设成功，照样走 `_send_wait` 等平台回执。
        """
        if not chatid:
            log.warning("[send] 无 chatid，跳过卡片")
            return {"sent": False, "errcode": None, "errmsg": "no chatid"}
        frame = {
            "cmd": "aibot_send_msg",
            "headers": {"req_id": self._new_req_id()},
            "body": {"chatid": chatid, "chat_type": int(chat_type),
                     "msgtype": "template_card", "template_card": card},
        }
        label = f"card[{(card or {}).get('card_type', '?')}] -> {chatid} (type={chat_type})"
        if not wait:
            self._send(frame)
            log.info("[send] %s 不等回执", label)
            return {"sent": True, "errcode": None, "errmsg": ""}
        return self._send_wait(frame, label)

    def send_update_card(self, req_id, card):
        """把**已经发出去的那张卡**就地改掉（`aibot_respond_update_msg`）。

        平台的三条硬约束（官方长连接文档原文，已核对）：
          · `headers.req_id` 必须是**那次点击事件**的 req_id（自己编的不认）；
          · **只能在收到点击事件后 5 秒内发**，超时平台就不让改了；
          · 只适用于模板卡片点击事件。
        ⇒ 因此**超时没人点的卡片是改不掉的**（没有点击就没有 req_id），
          只有"点了"的那张能就地改成「已允许 / 已拒绝」。
        """
        if not req_id:
            return {"sent": False, "errcode": None, "errmsg": "no req_id"}
        frame = {
            "cmd": "aibot_respond_update_msg",
            "headers": {"req_id": req_id},
            "body": {"response_type": "update_template_card", "template_card": card},
        }
        return self._send_wait(frame, f"update_card[{card.get('card_type')}]")

    def _send_wait(self, frame, label):
        """发一帧并**等平台回执**。返回 {sent, errcode, errmsg}。

        `errcode is None` 的语义是「**结果未知**」——帧可能到了也可能没到，
        调用方不许据此重发（重发会让模型把同一件事再干一遍）。
        ⚠️ 这个方法会阻塞，**绝不能**在 websocket 读线程上调（回执就在那条线程上收）。
        """
        with self._receipt_lock:
            with self._receipt_cv:
                self._receipt = None
                self._receipt_pending = True
                # 这次要等哪一帧的回执（供 `_deliver_receipt` 过滤无关回执）
                self._receipt_want = ((frame.get("headers") or {}).get("req_id") or "")
            try:
                if not self._send(frame):
                    # 帧**根本没发出去**（连接不在）⇒ 这不是"结果未知"，是"没发"。
                    # 必须和"发出去了但没回执"区分开：前者可以安全改道，后者不行。
                    log.error("[send] %s 没发出去：连接不在", label)
                    return {"sent": False, "errcode": None,
                            "errmsg": "ws 未连接", "unsent": True}
                with self._receipt_cv:
                    got = self._receipt_cv.wait_for(
                        lambda: self._receipt is not None, timeout=SEND_WAIT_SEC)
                    rc, emsg = self._receipt if got else (None, "未收到平台回执")
            finally:
                with self._receipt_cv:
                    self._receipt_pending = False

        sent = (rc == 0)
        level = log.info if sent else log.error
        level("[send] %s errcode=%s %s", label, rc, "" if sent else (emsg or "")[:160])
        return {"sent": sent, "errcode": rc, "errmsg": emsg or ""}

    def _post_reply(self, response_url, content):
        r = requests.post(response_url, json={
            "msgtype": "markdown", "markdown": {"content": content}}, timeout=6)
        log.info("[reply] -> %s %s", r.status_code, r.text[:120])
        return r.status_code == 200

    def reply(self, response_url, content):
        """被动回复。**一次性、有时效** —— 只用来回"收到/忙碌"。

        ⚠️ **必须异步**：这个方法是在 **websocket 读线程**上调用的（`_handle_callback` /
        `_handle_control`），而 HTTP POST 最长要等 6 秒。同步做的话，连发消息时读线程会被
        一条条卡住：期间收不到任何帧 ⇒ `_last_rx` 不前进 ⇒ 看门狗会把**健康的连接**判死。
        队列满就丢（这类回执本来就是"知道了"性质的，丢了不影响正确性）。
        """
        if not response_url:
            return False
        if self._reply_q.full():
            log.warning("[reply] 回复队列已满，丢弃一条：%r", content[:20])
            return False
        self._reply_q.put_nowait((response_url, content))
        return True

    def _reply_worker(self):
        while not self.stop:
            try:
                url, content = self._reply_q.get(timeout=1)
            except queue.Empty:
                continue
            try:
                self._post_reply(url, content)
            except Exception as e:
                log.warning("[reply] 失败: %s", e)
            finally:
                self._reply_q.task_done()

    def send_chunks(self, chatid, chat_type, text):
        """**先整条发**；只有被平台以 `40058` 拒收，才切开重发。

        为什么不预切：平台的上限按**字节**算（实测 20480），不是按条数。
        3000 字的中文回答 ≈ 9 KB，本来一条就读得完 —— 预先切片会把它硬拆成
        `(1/2)` `(2/2)`，既碎又多一次失败机会（用户 2026-10-27 认可这条策略）。
        ⇒ `CHUNK_BYTES` 的语义现在是**切分粒度**（真的超长时每片多大），不是"超过就切"。

        返回未送达列表 `[(label, {errcode, errmsg}), …]`，空列表 = 全部送达。
        """
        text = text or ""
        if not text:
            return []
        nbytes = len(text.encode("utf-8"))

        # ① 整条先发（绝大多数回答走到这里就结束了）
        r = self.send_markdown(chatid, chat_type, text)
        if r["sent"]:
            return []

        if r["errcode"] is None:
            # 没收到回执 = **结果未知**，不是"失败"。按本项目自己的约定（超时 ≠ 失败），
            # 不能让用户以为没发出去而重发 —— 重发会让 Claude 把刚才那件事再做一遍。
            log.error("[send] 没拿到平台回执（%s），推送结果未知", (r["errmsg"] or "")[:80])
            self.send_markdown(
                chatid, chat_type,
                "❔ **推送结果未知**：没收到平台回执。\n"
                "> 它可能已经发出去了，也可能没有。**先翻一下有没有**，别急着重发 —— "
                "重发会让它把刚才那件事再做一遍。")
            return [(0, r)]

        if r["errcode"] != 40058:
            # 不是"太长"，切片重发没有意义 —— 直接如实告诉用户
            log.error("[send] 整条发送失败 %dB errcode=%s %s",
                      nbytes, r["errcode"], (r["errmsg"] or "")[:160])
            self.send_markdown(
                chatid, chat_type,
                f"⚠️ **结果没能送达**（errcode=`{r['errcode']}`，{nbytes} 字节）。\n"
                f"> 平台明确拒收了，内容没发出去。看桥的日志。")
            return [(0, r)]

        # ② 被 40058 拒 ⇒ 按切分粒度切开，逐片重发（每片仍等回执）
        parts = chunk_md(text, self.chunk_bytes)
        log.warning("[send] 整条 %dB 被 40058 拒收，切成 %d 片重发", nbytes, len(parts))
        return self._send_parts(chatid, chat_type, parts)

    def _send_parts(self, chatid, chat_type, parts, first_idx=1):
        """逐片主动推送（带 `(i/n)` 前缀 + 失败汇总）。

        `first_idx` 让流式那条路可以从第 2 片开始发（第 1 片已经进了气泡）。
        """
        n = len(parts)
        failed = []
        for i in range(first_idx, n + 1):
            p = parts[i - 1]
            body = f"({i}/{n})\n{p}" if n > 1 else p
            failed.extend(self._send_piece(chatid, chat_type, body, i, n))
            if i < n:
                time.sleep(0.4)
        if failed:
            label, rf = failed[0]
            log.error("[send] 有 %d 段未送达，首段 %s/%d errcode=%s",
                      len(failed), label, n, rf["errcode"])
            self.send_markdown(
                chatid, chat_type,
                f"⚠️ **有 {len(failed)} 段内容没送达**（首段：第 {label}/{n} 段，"
                f"errcode=`{rf['errcode']}`）。\n> 上面的回答可能不完整，别把它当全文。")
        return failed

    def _send_piece(self, chatid, chat_type, body, idx, n, depth=0):
        """发一片；**仍**被 40058 拒就按字节对半再发（最多 3 层）。

        正常情况走不到这里 —— 只有"平台实际上限比我们以为的更小"才会触发
        （比如将来平台把 20480 改小、或加前缀后刚好越界）。
        这里按**实际字节**对半，不按 `CHUNK_BYTES` 再切：这一片本来就 ≤ 粒度，
        再按粒度切等于没切。
        """
        r = self.send_markdown(chatid, chat_type, body)
        if r["sent"]:
            return []
        if r["errcode"] == 40058 and depth < 3:
            halves = split_half(body)
            if len(halves) == 2:
                log.warning("[send] 第 %d/%d 片仍超长（%dB），对半重发",
                            idx, n, len(body.encode("utf-8")))
                out = []
                for k, h in enumerate(halves, 1):
                    out.extend(self._send_piece(
                        chatid, chat_type, f"({idx}/{n} 续{k})\n{h}", idx, n, depth + 1))
                    time.sleep(0.3)
                return out
            log.error("[send] 第 %d/%d 片仍超长但已切不动（%dB）",
                      idx, n, len(body.encode("utf-8")))
        return [(str(idx), r)]

    def _deliver(self, st, chatid, chat_type, text):
        """把最终文本落到**同一个气泡**里；太长 / 窗口用尽时退回主动推送。

        用户 2026-10-28 定的效果：ack 和正文是**一个气泡**，正文直接覆盖 ack。
        退化路径的硬要求是**不丢内容**：
          · 正文 > 20480 字节 ⇒ 切片，第 1 片顶替气泡，其余照旧推送（编号接续）；
          · 气泡已关闭 / 超过 10 分钟窗口 ⇒ 整条改走 `send_chunks`（另发一条）；
          · 平台明确拒收（非 40058）⇒ 同上；**结果未知 ⇒ 不重发**（沿用老约定）。
        """
        text = text or ""
        if not text:
            return []
        nbytes = len(text.encode("utf-8"))

        # ① 整条塞进气泡（绝大多数回答走到这里就结束了：一个气泡、零重复）
        r = st.send(text, finish=True)
        if r["sent"]:
            return []
        if r.get("dead"):
            log.warning("[deliver] 气泡不可用（%s）⇒ 正文改走主动推送（另发一条）",
                        r["errmsg"])
            return self.send_chunks(chatid, chat_type, text)
        if r["errcode"] is None:
            log.error("[deliver] 定稿帧没拿到回执，推送结果未知")
            self.send_markdown(
                chatid, chat_type,
                "❔ **推送结果未知**：没收到平台回执。\n"
                "> 它可能已经发出去了，也可能没有。**先翻一下有没有**，别急着重发 —— "
                "重发会让它把刚才那件事再做一遍。")
            return [(0, r)]
        if r["errcode"] != 40058:
            log.error("[deliver] 定稿被拒 errcode=%s ⇒ 整条改走主动推送", r["errcode"])
            return self.send_chunks(chatid, chat_type, text)

        # ② 超长：第 1 片顶替气泡，其余主动推送（顺序与编号和以前完全一致）
        parts = chunk_md(text, self.chunk_bytes)
        n = len(parts)
        log.warning("[deliver] 正文 %dB 超过上限，切成 %d 片（第 1 片进气泡）", nbytes, n)
        r1 = st.send(f"(1/{n})\n{parts[0]}", finish=True)
        if r1["sent"]:
            return self._send_parts(chatid, chat_type, parts, first_idx=2)
        if r1.get("dead") or r1["errcode"] is not None:
            # 第 1 片明确没进气泡 ⇒ 整条推送兜底（一条都没发出去，不会重复）
            log.warning("[deliver] 第 1 片没进气泡（%s）⇒ 整条改走主动推送",
                        r1.get("errmsg") or r1["errcode"])
            return self.send_chunks(chatid, chat_type, text)
        # 第 1 片结果未知 ⇒ 绝不重发它，只补后面的，并把话说清楚
        log.error("[deliver] 第 1 片结果未知，只补第 2 片之后")
        self.send_markdown(chatid, chat_type,
                           f"❔ 第 1/{n} 段推送结果未知（可能已发出，也可能没有）。")
        return self._send_parts(chatid, chat_type, parts, first_idx=2)

    def _finish(self, st, chatid, chat_type, text):
        """收尾统一出口：有气泡就复用气泡，没有就走主动推送。"""
        if st is None:
            return self.send_chunks(chatid, chat_type, text)
        return self._deliver(st, chatid, chat_type, text)

    # -- 连接生命周期（同 Phase 0）--
    def subscribe(self):
        self._send({
            "cmd": "aibot_subscribe",
            "headers": {"req_id": self._new_req_id()},
            "body": {"bot_id": self.bot_id, "secret": self.secret},
        })

    def _on_open(self, ws):
        log.info("[ws] connected, subscribe bot_id=%s", self.bot_id)
        if self._backoff != BACKOFF_START:
            log.info("[ws] 退避重置 %ss -> %ss", self._backoff, BACKOFF_START)
        self._backoff = BACKOFF_START
        self.subscribe()

    def _on_error(self, ws, err):
        log.warning("[ws] error: %s", err)

    def _on_close(self, ws, code, msg):
        self.subscribed.clear()
        log.info("[ws] closed code=%s msg=%s", code, msg)

    def _on_message(self, ws, raw):
        self._last_rx = time.time()      # 任何帧都算"连接还活着"（看门狗用）
        try:
            msg = json.loads(raw)
        except Exception:
            log.warning("[ws] 非 JSON: %r", raw[:200])
            return
        cmd = msg.get("cmd", "")
        log.debug("[ws] rx cmd=%r %dB", cmd, len(raw))

        if cmd == "aibot_subscribe" or (not cmd and "errcode" in msg
                                        and not self.subscribed.is_set()):
            # `.get(k, default)` 在 k 存在但值为 null 时**不会**退回默认值 ⇒ 显式兜底
            rc = msg.get("errcode")
            if rc is None:
                rc = (msg.get("result") or {}).get("errcode")
            if rc == 0:
                self.subscribed.set()
                log.info("[ws] subscribe ok")
            else:
                log.error("[ws] subscribe 失败: %s",
                          json.dumps(msg, ensure_ascii=False)[:400])
                # 🚨 订阅失败**不能挂着不动**：心跳和惰性检测都以 subscribed 为前提，
                # 连接又不会自己断 ⇒ 进程活着、TCP 连着，但再也收不到一条消息。
                # 主动断开，让主循环带退避重连（凭据错的话会一直重试并刷日志，那是想要的）。
                self.subscribed.clear()
                try:
                    ws.close()
                except Exception:
                    pass
            return

        if cmd == "aibot_msg_callback":
            self._handle_callback(msg)
            return

        if cmd == "aibot_event_callback":
            ev = ((msg.get("body") or {}).get("event") or {}).get("eventtype", "")
            if ev == "disconnected_event":
                log.warning("[ws] 服务端断开旧连接（同一 bot 不要起两个实例）")
                self.subscribed.clear()
                try:
                    ws.close()
                except Exception:
                    pass
            elif ev == "template_card_event":
                # 卡片按钮被点。**必须离开读线程**：下面的动作要么发卡片（等平台回执）、
                # 要么发 markdown，两条都会阻塞，而回执就在这条读线程上收。
                log.info("[card] template_card_event 到达")
                if self.dump_raw:
                    self._dump_raw(msg)
                threading.Thread(target=self._handle_card_event,
                                 args=(msg,), daemon=True).start()
            else:
                log.info("[ws] event: %s", ev or msg)
                # 其余事件（含以后平台新增的类型）只在 --dump-raw 下整个落盘，
                # 免得"来了个没见过的 event 却什么都没留下"。
                if self.dump_raw:
                    self._dump_raw(msg)
            return

        if cmd in ("aibot_respond_msg", "aibot_send_msg", "aibot_respond_welcome_msg",
                   "aibot_respond_update_msg", "ping"):
            rc = msg.get("errcode")
            if rc not in (0, None):
                log.warning("[ws] %s 回执错误: %s", cmd,
                            json.dumps(msg, ensure_ascii=False)[:300])
            elif cmd == "aibot_send_msg":
                log.info("[ws] aibot_send_msg 回执 errcode=0")
            # 实测回执**不带 cmd**，但万一哪天带上了：也要能交给等待方，
            # 否则发送方会白等到超时，然后错误地报"没能送达"（还不走切片分支）。
            self._deliver_receipt(rc, msg.get("errmsg") or "",
                                  (msg.get("headers") or {}).get("req_id"))
            return

        if not cmd:
            rc = msg.get("errcode")
            if rc not in (0, None):
                log.warning("[ws] 响应错误: %s", json.dumps(msg, ensure_ascii=False)[:300])
            else:
                log.debug("[ws] 回执 errcode=0")
            # 实测：`aibot_send_msg` 的回执**没有 cmd 字段**，落在这个分支。
            self._deliver_receipt(rc, msg.get("errmsg") or "",
                                  (msg.get("headers") or {}).get("req_id"))
            return

        log.info("[ws] 未处理 cmd=%r raw=%s", cmd, raw[:300])

    # -- 收消息 --
    def _handle_callback(self, msg):
        body = msg.get("body") or {}
        from_ = body.get("from") or {}
        userid = from_.get("userid") or ""
        raw_chatid = body.get("chatid") or ""
        chattype = body.get("chattype") or "unknown"
        is_single = chattype == "single"
        chat_type = 1 if is_single else 2
        # 🚨 **单聊回调里没有 `chatid`**（实测 2026-10-27 20:48：日志 `chat= type=single`）
        # ⇒ 用 `from.userid` 当推送目标。不兜底的话：模型跑完了、`send_markdown` 却
        # 「无 chatid，跳过推送」，用户在手机上永远停在"收到，处理中…"。
        # 单聊因此天然拥有**独立会话**（session 键是 (chatid, userid)，
        # 群聊是 (群 chatid, userid)，两者不会串）。
        chatid = raw_chatid or userid
        if not raw_chatid and not is_single:
            # chattype 缺失/改名的兜底：**宁可发到私聊，也别把结果丢掉**
            chat_type = 1
            log.warning("[msg] chattype=%r 且没有 chatid ⇒ 按单聊发给 userid", chattype)
        response_url = body.get("response_url") or ""
        raw_text = (body.get("text") or {}).get("content", "") or ""

        if body.get("aibotid") and self.bot_id and body["aibotid"] != self.bot_id:
            log.warning("[msg] aibotid 不匹配: %s != %s", body["aibotid"], self.bot_id)

        if not self._shape_logged:
            self._shape_logged = True
            log.info("[shape] body keys=%s  from keys=%s  text keys=%s",
                     sorted(body.keys()), sorted(from_.keys()),
                     sorted((body.get("text") or {}).keys()))
            f, v = find_msg_id(msg)
            log.info("[shape] 去重键 %s = %s", f, v)
        if self.dump_raw:
            self._dump_raw(msg)

        if not userid:
            log.warning("[msg] 无 from.userid，丢弃")
            return

        # ⚠️ 白名单是唯一真正的鉴权；群里 @ 只是消息路由
        if self.allow_users and userid not in self.allow_users:
            log.warning("[msg] 非白名单 userid=%s，静默丢弃", userid)
            return
        if self.allow_chats and raw_chatid and raw_chatid not in self.allow_chats:
            # 只对**真实的** chatid 生效：单聊那个"chatid"其实是 userid，
            # 拿它去比群白名单只会误杀。
            log.warning("[msg] 非白名单 chatid=%s，静默丢弃", raw_chatid)
            return

        f, key = find_msg_id(msg)
        if key and self.store.seen(key):
            log.warning("[msg] 重复消息 %s=%s，跳过", f, key)
            return

        if (body.get("msgtype") or "text") != "text":
            log.info("[msg] 非文本 msgtype=%s，暂不支持", body.get("msgtype"))
            self.reply(response_url, "目前只支持文本消息。")
            return

        text, at_prefix = strip_at(raw_text, self.bot_name)
        log.info("[msg] user=%s chat=%s type=%s%s 去@=%r 正文=%.80r",
                 userid, chatid, chattype,
                 "（单聊无 chatid，用 userid 兜底）" if is_single and not raw_chatid else "",
                 at_prefix, text)

        # —— 控制指令：**在回调线程里立即处理**，否则 /stop 会排在正在跑的任务后面 ——
        lowered = text.strip().lower()
        if lowered in ("/new", "/stop", "/status"):
            self._handle_control(lowered, chatid, chat_type, userid, response_url)
            return

        # —— 普通任务：忙碌时**直接丢弃，不排队** ——
        # 用户 2026-10-27 明确要求：处理中收到的新消息回"忙碌中…"并抛弃，
        # 目的是防短时间输入轰炸。
        # ⚠️ 这**推翻了交接文档 §附录C** 的"聊天桥应该排队"——按用户决定执行。
        if self._is_busy() or self._q.qsize() > 0:
            with self._job_lock:
                cur = dict(self._current) if self._current else None
            waited = (time.time() - cur["started"]) if cur else 0.0
            log.warning("[msg] 忙碌中，丢弃 user=%s 正文=%.40r（已跑 %.0fs）",
                        userid, text, waited)
            self.reply(response_url,
                       f"⏳ **忙碌中**（当前任务已跑 {waited:.0f} 秒），**本条已忽略**。\n\n"
                       f"> 要打断请发 `/stop`；查看进度发 `/status`；"
                       f"换新话题请等当前任务结束。")
            return

        # ⚠️ `req_id` 是**平台**给这次回调发的关联键，回复时必须原样透传
        # （`aibot_respond_msg` 的 headers.req_id）；自己编一个平台不认。
        job = {"chatid": chatid, "chat_type": chat_type, "userid": userid,
               "text": text, "msgid": key or "",
               "req_id": (msg.get("headers") or {}).get("req_id") or "",
               "response_url": response_url,
               # 入队那一刻的会话代次；跑完落库前要再比一次（见 `_epoch`）
               "epoch": self._epoch_of(chatid, userid)}
        try:
            self._q.put_nowait(job)
        except queue.Full:      # 理论上到不了这里（上面已拦掉忙碌），留个兜底
            log.warning("[q] 入队失败（队列满 %d）", self._qmax)
            self.reply(response_url, "⏳ 忙碌中，本条已忽略。")
            return
        # 「收到，处理中…」**不在这里发**：它要在 worker 线程里以**流式首帧**发出，
        # 这样正文才能用同一个 stream.id 覆盖它（见 Stream / _deliver）。
        # 在这里发就等于两套通道，手机上就是两条独立气泡 —— 正是要修掉的那个问题。

    def _is_busy(self):
        with self._job_lock:
            return self._current is not None

    def _epoch_of(self, chatid, userid):
        with self._epoch_lock:
            return self._epoch.get((chatid, userid), 0)

    def _bump_epoch(self, chatid, userid):
        with self._epoch_lock:
            k = (chatid, userid)
            self._epoch[k] = self._epoch.get(k, 0) + 1

    def _stop_text(self, killed):
        """按 runner 的**实际**结果给话。`failed` 不能说成"已清理"。

        `kill_status` 是 `kill_current()` 复核过 `p.poll()` 之后写的：
        `killed` = 确认退出；`failed` = 命令发了但进程没退；`idle` = 本来就没在跑。
        （老的单测 FakeRunner 只返回 bool、不写 kill_status，这里照样按 killed 处理。）
        """
        status = getattr(self.runner, "kill_status", "")
        if killed or status == "killed":
            return "⏹ 已停止当前任务（进程树已清理）。"
        if status == "failed":
            return ("⚠️ 停止命令已发出，但**进程 5 秒内没有退出** —— 它可能还在跑。\n\n"
                    "> 请到电脑上按 PID 确认（`tasklist /FI \"PID eq <pid>\"`），"
                    "别以为已经停了。")
        return "当前没有正在跑的任务。"

    def _handle_control(self, cmd, chatid, chat_type, userid, response_url):
        if cmd == "/new":
            self.store.clear_session(chatid, userid)
            self._bump_epoch(chatid, userid)     # 让**正在跑**的旧任务别把旧会话写回来
            log.info("[ctl] /new user=%s", userid)
            self.reply(response_url, "已开新会话：下一轮不会记得之前的内容。")
            return
        if cmd == "/stop":
            killed = self.runner.kill_current()
            log.info("[ctl] /stop 终止=%s 状态=%s", killed,
                     getattr(self.runner, "kill_status", "?"))
            self.reply(response_url, self._stop_text(killed))
            return
        if cmd == "/status":
            # 改成**卡片**：一眼能看，还带三个按钮（刷新 / 清上下文 / 停止）。
            # 卡片发不出去就退回文本 —— 状态是"必须能看到"的东西，不能因为卡片失败就沉默。
            self._status_async(chatid, chat_type, userid)
            return

    # -- /status 卡片 --
    def _status_pairs(self, chatid, userid):
        """状态键值对。文本版和卡片版都用它，保证两处**永远一致**。"""
        sid, cwd = self.store.get_session(chatid, userid, self.cc_cwd)
        with self._job_lock:
            cur = dict(self._current) if self._current else None
        usage = (self._last_usage.get((chatid, userid)) or "").replace("📊 ", "").strip()
        return [
            ("队列", f"{self._q.qsize()} 条"),
            ("当前任务", ("运行中 %.0f 秒" % (time.time() - cur["started"])) if cur else "空闲"),
            ("上下文", usage or "—（重启后首轮还没跑）"),
            ("会话", (sid or "")[:8] or "（无，下一轮新建）"),
            ("档位", f"{self.runner.perm_mode or '默认'} / 超时 {self.runner.timeout}s"),
            ("工作区", cwd),
        ]

    def _status_card(self, chatid, userid):
        """按钮卡。`card_action` 是**恒填**的：官方文档标"否"，实测不带它平台拒收 42045
        （2026-10-29，见 DECISIONS §21.9）。"""
        return {
            "card_type": "button_interaction",
            "source": {"desc": "WeCom-Agent-Bridge"},
            # 标题用**群里的机器人名**（`WECOM_BOT_NAME`），不硬编码项目名 ——
            # 卡片上写一个群里不存在的名字比不写更糟。
            "main_title": {"title": f"{self.bot_name} · 状态",
                           "desc": "企微 ⇄ 本机 Claude Code"},
            "horizontal_content_list": [{"keyname": k, "value": v}
                                         for k, v in self._status_pairs(chatid, userid)],
            # ⚠️ 按钮文字**只能是 2 个字**：实测 4 个字会被截断，
            # 手机上显示成「清...」、PC 上「清上...」（2026-10-29 用户实测）。
            # 语义放到 `sub_title_text` 里解释，别指望按钮放得下。
            "sub_title_text": "点按钮直接操作；「清空」= 开新会话，下一轮不记得之前的内容。",
            "button_list": [
                {"text": "刷新", "style": 1, "key": "status_refresh"},
                {"text": "清空", "style": 2, "key": "status_clear"},
                {"text": "停止", "style": 3, "key": "status_stop"},
            ],
            "task_id": "status-%d" % int(time.time() * 1000),
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        }

    def _status_text(self, chatid, userid):
        return ("**/status**\n"
                + "\n".join(f"> {k}：{v}" for k, v in self._status_pairs(chatid, userid)))

    def _status_async(self, chatid, chat_type, userid):
        """在旁路线程里发状态卡（会等平台回执，不能占读线程）。"""
        def _go():
            r = self.send_card(chatid, chat_type, self._status_card(chatid, userid))
            if not r["sent"]:
                log.warning("[card] /status 卡片未送达（%s），退回文本", r["errmsg"])
                self.send_chunks(chatid, chat_type, self._status_text(chatid, userid))
        threading.Thread(target=_go, daemon=True).start()

    def _handle_card_event(self, msg):
        """处理卡片按钮点击。跑在旁路线程里（见 `_on_message` 的分派）。"""
        body = msg.get("body") or {}
        tce = ((body.get("event") or {}).get("template_card_event") or {})
        key = tce.get("event_key") or ""
        userid = (body.get("from") or {}).get("userid") or ""
        raw_chatid = body.get("chatid") or ""
        chat_type = 1 if (body.get("chattype") or "") == "single" else 2
        chatid = raw_chatid or userid      # 单聊没有 chatid，与消息回调同一套兜底
        if not userid:
            log.warning("[card] 无 from.userid，丢弃（key=%r）", key)
            return
        # 按钮和文字一样是**可执行入口** ⇒ 同一道白名单，不能因为是点卡片就放行
        if self.allow_users and userid not in self.allow_users:
            log.warning("[card] 非白名单 userid=%s key=%s，忽略", userid, key)
            return
        # ⚠️ 群白名单也要查。早先这里只查了用户、没查群 ⇒ 白名单用户在
        # **已经不被允许的群**里点旧卡片，照样能查状态 / 清空 / 停止（2026-10-29 复核 #6）。
        # 条件与文字消息那条完全一致：只对**真实的** chatid 生效，单聊那个"chatid"是 userid。
        if self.allow_chats and raw_chatid and raw_chatid not in self.allow_chats:
            log.warning("[card] 非白名单 chatid=%s key=%s，忽略", raw_chatid, key)
            return
        log.info("[card] user=%s key=%s task=%s chat=%s type=%s",
                 userid, key, tce.get("task_id"), chatid, chat_type)
        if key == "status_refresh":
            self._status_async(chatid, chat_type, userid)
            return
        if key == "status_clear":
            self.store.clear_session(chatid, userid)
            self._bump_epoch(chatid, userid)     # 与 `/new` 同一条语义
            self.send_markdown(chatid, chat_type,
                               "✅ 已开新会话：下一轮不会记得之前的内容。")
            return
        if key == "status_stop":
            killed = self.runner.kill_current()
            log.info("[card] status_stop 终止=%s 状态=%s", killed,
                     getattr(self.runner, "kill_status", "?"))
            self.send_markdown(chatid, chat_type, self._stop_text(killed))
            return
        if key == "approve_done":
            # 更新后的卡片若带着这个"已处理"按钮（降级形态），点了什么都别做
            log.info("[card] 已处理卡片的按钮被点，忽略")
            return
        if key in ("approve_ok", "approve_no"):
            rid = tce.get("task_id") or ""
            allow = key == "approve_ok"
            with self._approval_lock:
                info = self._approvals.get(rid)
            if not info:
                # 超时后 hook 已经自己拒绝了、请求文件也没了 ⇒ 这次点击无效。
                # 明确告诉用户，别让他以为点上了。
                log.info("[card] 审批 task_id=%r 未知或已过期，忽略", rid)
                self.send_markdown(chatid, chat_type,
                                   "⌛ 这张审批已失效（超时或任务已结束），这次点击没有生效。")
                return
            if info.get("userid") and userid != info["userid"]:
                # 卡片是发给"发起这条任务的人"的，别人点不算数
                log.warning("[card] 审批被非发起人点击（%s != %s），忽略",
                            userid, info["userid"])
                return
            # ⚠️ 顺序有讲究：平台的更新窗口只有 **5 秒**（实测文档原文），
            #    而 `send_markdown` 要等回执（最多 15 秒）⇒ **先决定、再更新卡片**，
            #    卡片更新成功后**不再发文字**（卡片本身就是反馈，少一条噪音）。
            self._approval_decide(rid, allow)
            req_id = ((msg.get("headers") or {}).get("req_id") or "")
            if not self._approval_update_card(req_id, info.get("req") or {},
                                              allow, userid):
                self.send_markdown(chatid, chat_type,
                                   "✅ 已允许，继续执行。" if allow
                                   else "🚫 已拒绝，这条不会执行。")
            with self._approval_lock:
                self._approvals.pop(rid, None)
            return
        log.info("[card] 未知 event_key=%r，忽略", key)

    # -- 审批卡片（Phase 3）：hook 写请求 → 桥发卡 → 点击写决定 --
    def _approval_env(self, chatid, chat_type, userid):
        """这一轮要注入给 claude 子进程的环境变量（hook 靠它找目录 + 找会话）。"""
        if not self.approval_dir:
            return None
        e = {
            "CC_APPROVAL_DIR": self.approval_dir,
            "CC_APPROVAL_WAIT_SEC": str(int(self.approval_wait_sec or 120)),
            "CC_APPROVAL_TARGET": f"{chatid}|{chat_type}|{userid}",
        }
        if self.approval_allow:
            e["CC_APPROVAL_ALLOW"] = self.approval_allow
        return e

    def _approval_detail(self, tool, inp):
        """卡片正文里那一行"到底要干什么"。比 `tool_desc` 详细，但同样截断。"""
        inp = inp or {}
        if tool == "Bash":
            return _clip(str(inp.get("command") or ""), 300)
        if tool in ("Edit", "Write", "MultiEdit"):
            return _clip(str(inp.get("file_path") or ""), 300)
        if tool == "WebFetch":
            return _clip(str(inp.get("url") or ""), 300)
        try:
            return _clip(json.dumps(inp, ensure_ascii=False), 300)
        except Exception:
            return _clip(str(inp), 300)

    def _approval_card(self, req):
        """一张"允许 / 拒绝"按钮卡。`card_action` 恒填（实测不带它 42045）。"""
        tool = req.get("tool_name") or "?"
        rid = req.get("id") or ""
        return {
            "card_type": "button_interaction",
            "source": {"desc": "WeCom-Agent-Bridge"},
            "main_title": {"title": "需要你确认",
                           "desc": f"{tool_desc(tool, req.get('tool_input'))}"},
            "horizontal_content_list": [
                {"keyname": "工具", "value": tool},
                {"keyname": "内容", "value": self._approval_detail(tool, req.get("tool_input"))},
                {"keyname": "会话", "value": _short_path(req.get("cwd") or "", 60) or "—"},
            ],
            # 按钮文字**只能 2 个字**（实测），语义只能放这里
            "sub_title_text": f"点「允许」才会执行；{int(self.approval_wait_sec or 120)} 秒内不点按拒绝处理。",
            "button_list": [
                {"text": "允许", "style": 1, "key": "approve_ok"},
                {"text": "拒绝", "style": 3, "key": "approve_no"},
            ],
            "task_id": rid,
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        }

    def _approval_resolved_card(self, req, allow, userid="", kind="text_notice"):
        """点击之后把那张卡改成"已处理"。**没有按钮** —— 否则会留下一个点了没用的按钮。"""
        tool = req.get("tool_name") or "?"
        return {
            "card_type": kind,
            "source": {"desc": "WeCom-Agent-Bridge"},
            "main_title": {"title": "已允许" if allow else "已拒绝",
                           "desc": tool_desc(tool, req.get("tool_input"))},
            "horizontal_content_list": [
                {"keyname": "工具", "value": tool},
                {"keyname": "内容", "value": self._approval_detail(tool, req.get("tool_input"))},
                {"keyname": "处理", "value": (userid or "?") + " · " +
                                             time.strftime("%H:%M:%S")},
            ],
            "sub_title_text": ("这次调用已经执行。" if allow else "这次调用没有执行。"),
            "task_id": req.get("id") or "",
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        } if kind == "text_notice" else {
            "card_type": "button_interaction",
            "source": {"desc": "WeCom-Agent-Bridge"},
            "main_title": {"title": "已允许" if allow else "已拒绝",
                           "desc": tool_desc(tool, req.get("tool_input"))},
            "horizontal_content_list": [
                {"keyname": "工具", "value": tool},
                {"keyname": "内容", "value": self._approval_detail(tool, req.get("tool_input"))},
            ],
            "sub_title_text": "这条已经处理过了。",
            # 平台若不允许把 button_interaction 换成无按钮的类型，就退成这一个
            # "已处理" 按钮；点它会被 `_handle_card_event` 静默忽略（不弹"已失效"）
            "button_list": [{"text": "已处理", "style": 2, "key": "approve_done"}],
            "task_id": req.get("id") or "",
            "card_action": {"type": 1, "url": "https://work.weixin.qq.com"},
        }

    def _approval_update_card(self, req_id, req, allow, userid):
        """点击后 5 秒内把卡片改成已处理。失败返回 False，让调用方退回一条文字确认。"""
        # 先试"无按钮"的 text_notice（最干净的"已处理"形态），
        # 平台不认再退回仍带一个"已处理"按钮的 button_interaction。
        for kind in ("text_notice", "button_interaction"):
            card = self._approval_resolved_card(req, allow, userid, kind)
            r = self.send_update_card(req_id, card)
            if r.get("sent"):
                log.info("[approval] 卡片已就地更新为「%s」(kind=%s)",
                         card["main_title"]["title"], kind)
                return True
            log.warning("[approval] 更新卡片失败 kind=%s: errcode=%s %s",
                        kind, r.get("errcode"), r.get("errmsg"))
        return False

    def _approval_decide(self, rid, allow, why=""):
        """把决定写成文件交给 hook —— **只有 hook 真读到了才算数**。"""
        path = os.path.join(self.approval_dir, rid + ".decision")
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write("allow\n" if allow else "deny\n")
            log.info("[approval] rid=%s 决定=%s %s", rid, "allow" if allow else "deny", why)
            return True
        except Exception as e:
            log.error("[approval] 写决定失败 rid=%s: %s", rid, e)
            return False

    def _approval_scan_once(self):
        """扫一遍待审批请求。**跑在专用线程上**：发卡片会等平台回执（最多 15s）。"""
        try:
            names = os.listdir(self.approval_dir)
        except FileNotFoundError:
            return
        except Exception as e:
            log.warning("[approval] 列目录失败: %s", e)
            return
        live = {n[:-len(".req.json")] for n in names if n.endswith(".req.json")}
        with self._approval_lock:
            # hook 退出时会删掉请求文件 ⇒ 过期的直接忘掉（免得点击写进一个没人读的文件）
            for rid in [r for r in self._approvals if r not in live]:
                self._approvals.pop(rid, None)
            known = set(self._approvals)
        for rid in live - known:
            path = os.path.join(self.approval_dir, rid + ".req.json")
            try:
                with open(path, encoding="utf-8") as f:
                    req = json.load(f)
            except Exception as e:
                log.warning("[approval] 读请求失败 rid=%s: %s", rid, e)
                continue
            target = str(req.get("target") or "").split("|")
            if len(target) < 3 or not target[0]:
                log.warning("[approval] 请求没带目标会话 rid=%s ⇒ 直接拒绝", rid)
                self._approval_decide(rid, False, "无目标")
                continue
            chatid, chat_type, userid = target[0], int(target[1] or 2), target[2]
            with self._approval_lock:
                self._approvals[rid] = {"chatid": chatid, "chat_type": chat_type,
                                        "userid": userid, "ts": time.time(),
                                        "tool": req.get("tool_name"),
                                        # 留着整份请求：点击后要把卡片就地改成
                                        # 「已允许/已拒绝」，得知道刚才要干什么
                                        "req": req}
            # ① 先告诉 hook「我看到了」（它只等 10 秒；不然桥没在跑时它会干等两分钟）
            try:
                open(os.path.join(self.approval_dir, rid + ".seen"), "w").close()
            except Exception:
                pass
            # ② 发卡片。发不出去 = 用户永远点不到 ⇒ 立刻拒绝，别让他白等 120 秒
            r = self.send_card(chatid, chat_type, self._approval_card(req),
                               wait=True)
            if not r.get("sent"):
                log.warning("[approval] 卡片没发出去（%s）⇒ 直接拒绝 rid=%s",
                            r.get("errmsg") or r.get("errcode"), rid)
                self._approval_decide(rid, False, "卡片未送达")
                with self._approval_lock:
                    self._approvals.pop(rid, None)

    def _approval_loop(self):
        while not self.stop:
            try:
                self._approval_scan_once()
            except Exception as e:
                log.exception("[approval] 扫描异常: %s", e)
            time.sleep(APPROVAL_POLL_SEC)

    def _dump_raw(self, msg):
        os.makedirs(LOGDIR, exist_ok=True)
        with open(os.path.join(LOGDIR, "raw-callback.jsonl"), "a", encoding="utf-8") as f:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
        log.info("[raw] 原始回调已追加")

    # -- worker：单线程串行 --
    def _worker(self):
        while not self.stop:
            try:
                job = self._q.get(timeout=1)
            except queue.Empty:
                continue
            # 一出队就占住"忙碌"位：`_do_job` 要读库、查目录之后才设 `_current`，
            # 那个空档里新消息会被当成"空闲"收进队列，破坏"忙碌即丢弃"的约定。
            with self._job_lock:
                if self._current is None:
                    self._current = {"started": time.time(),
                                     "text": (job.get("text") or "")[:60],
                                     "userid": job.get("userid"),
                                     "chatid": job.get("chatid")}
            try:
                self._do_job(job)
            except Exception as e:
                log.exception("[worker] 任务异常: %s", e)
                self.send_markdown(job["chatid"], job["chat_type"],
                                   f"❌ 桥内部错误：{type(e).__name__}: {e}")
            finally:
                self._q.task_done()
                # 占位必须在这里收：`_do_job` 有几条**提前 return**（空消息、目录不存在），
                # 那些路径不会走到它内部的清理，占位留着就会永久显示"忙碌中"。
                with self._job_lock:
                    self._current = None

    def _do_job(self, job):
        chatid, chat_type, userid, text = (job["chatid"], job["chat_type"],
                                           job["userid"], job["text"])

        # 这一条**流式气泡**装下 ack 与正文（同一个 stream.id，正文覆盖 ack）。
        # 回调没带 req_id 时退回老路径（response_url 回执 + 主动推送），别把消息吞掉。
        st = Stream(self, job["req_id"], chatid, chat_type) if job.get("req_id") else None
        if st is None:
            log.warning("[stream] 回调没带 req_id ⇒ 退回 response_url + 主动推送")
            self.reply(job.get("response_url") or "", "收到，处理中…")

        if not text.strip():
            self._finish(st, chatid, chat_type, "（空消息，未执行）")
            return

        session_id, cwd = self.store.get_session(chatid, userid, self.cc_cwd)
        if not os.path.isdir(cwd):
            # 会话表里记着的老 cwd 可能已被删/搬走（或者用户改了 `.env` 的 CC_CWD）。
            # 直接报错会让这个会话**永久失败**（连 /new 都救不回来，因为 cwd 不跟着清），
            # 所以先回落到配置的 cwd 并就地修好记录。
            if os.path.isdir(self.cc_cwd) and cwd != self.cc_cwd:
                log.warning("[job] 记录的 cwd 不存在（%s），回落到 CC_CWD=%s",
                            cwd, self.cc_cwd)
                cwd = self.cc_cwd
                self.store.save_session(chatid, userid, session_id, cwd)
                session_id = None          # 目录都换了，别拿旧 session 去 resume
            else:
                self._finish(st, chatid, chat_type,
                             f"❌ 启动目录不存在：`{cwd}`\n> 请检查 `.env` 的 `CC_CWD`")
                return

        with self._job_lock:
            self._current = {"started": time.time(), "text": text[:60],
                             "userid": userid, "chatid": chatid}
        self.runner.clear_stop()

        # ack：流式首帧（`finish=false`），正文到点时用**同一个 id** 覆盖它。
        # ⚠️ 必须**在 worker 线程**发（要等回执），不能放在回调的读线程上。
        if st is not None:
            r = st.send("收到，处理中…", finish=False)
            if r.get("dead"):
                log.warning("[stream] ack 建不出气泡（%s）⇒ 正文将改走主动推送",
                            r["errmsg"])
                st = None

        # 真进度（runner 增量读 `stream-json`）：
        #   有气泡 ⇒ `ProgressPump` 按「变化时推 + 兜底心跳」更新**同一个**气泡；
        #   没气泡 ⇒ 退回那条只看时间的定时文案 —— 没有气泡就没地方显示进度，
        #            主动推送一条"还在跑"已经是能做到的全部。
        pump = None
        if st is not None:
            pump = ProgressPump(st, chatid, chat_type,
                                heartbeat_sec=self.progress_hb_sec)
        timer = None
        if st is None and self.progress_sec > 0:
            def _progress():
                self.send_markdown(chatid, chat_type,
                                   f"⏳ 还在跑（已 {self.progress_sec} 秒）…")
            timer = threading.Timer(self.progress_sec, _progress)
            timer.daemon = True
            timer.start()

        # 长任务的收口闸：快到 10 分钟窗口时**主动结束气泡**，别让它永远停在
        # 「收到，处理中…」。用户的原始诉求就是"这个气泡不能误导人"。
        # 结束后气泡定型，正文由 `_deliver` 改走主动推送（另发一条，内容不丢）。
        latch = None
        if st is not None:
            def _latch():
                r2 = st.send("⏳ 任务仍在运行（已超过 9 分钟），结果稍后单独发一条。",
                             finish=True)
                if r2["sent"]:
                    log.warning("[stream] 到 9 分钟闸口，气泡已收口，正文将另发一条")
            latch = threading.Timer(STREAM_LATCH_SEC, _latch)
            latch.daemon = True
            latch.start()
        try:
            cb = pump.update if pump else None
            # 审批环境变量只在开了审批时才传（没开就别给 runner 加无关参数）
            aenv = self._approval_env(chatid, chat_type, userid)
            kw = {"on_progress": cb}
            if aenv:
                kw["extra_env"] = aenv
            res = self.runner.run(text, session_id, cwd, **kw)
            note = ""
            # resume 失效（会话文件被清理/损坏）⇒ 新建并在回复里说明，不静默失忆。
            #
            # ⚠️ 但**只在"确认这一轮什么都没干"时才敢重跑**（2026-10-29 复核 #3）。
            # 重跑 = 把整条指令再执行一遍；如果第一轮已经改过文件 / 提交过 / 发过消息，
            # 就会**重复副作用**（重复写入、重复提交、重复调外部服务）。三条否决：
            #   · tools > 0         —— 已经调用过工具，可能已经动手了
            #   · stream_failed     —— 流断了、结果未知
            #   · stopped/timed_out —— 被我们打断或卡住过，执行到哪一步不知道
            # 这些情况下即使错误文本像"会话失效"，也**不重跑**，如实告诉用户。
            if session_id and res.get("is_error") and _looks_like_resume_failure(res["text"]):
                retry_ok, why = safe_to_retry_resume(res)
                if retry_ok:
                    log.warning("[run] resume 失败，改新建会话重跑")
                    res = self.runner.run(text, None, cwd, **kw)
                    note = "> ⚠️ 上一轮的会话已失效，已为你新建会话（所以这轮不记得之前的内容）。\n\n"
                else:
                    # 不能重跑：宁可让用户核实，也不冒重复副作用的风险
                    log.warning("[run] resume 失败，但**不重跑**（%s）", why)
                    note = (f"> ⚠️ 上一轮的会话已失效，**但没有自动重跑**：{why}，"
                            "重跑可能造成重复副作用。\n"
                            "> 请先核实实际状态，再决定要不要重发。\n\n")
            # ⚠️ 必须**在定稿之前**收口：否则迟到的进度帧会把答案盖掉
            if pump:
                pump.close()
                pump = None
            self._report(job, res, cwd, session_id, note, st)
        except Exception as e:
            # 这里兜住：否则 _worker 那个兜底会**另发一条**，用户就又是两个气泡了
            log.exception("[job] 任务异常: %s", e)
            self._finish(st, chatid, chat_type,
                         f"❌ 桥内部错误：{type(e).__name__}: {e}")
        finally:
            if pump:
                # 异常路径也必须收口，否则它会在 `_worker` 那条兜底推送之后继续发帧
                pump.close()
            if timer:
                timer.cancel()
            if latch:
                latch.cancel()
            with self._job_lock:
                self._current = None

    def _report(self, job, res, cwd, session_id, note="", st=None):
        chatid, chat_type = job["chatid"], job["chat_type"]
        if res.get("fatal"):
            self._finish(st, chatid, chat_type, "❌ " + res["text"])
            return

        if res["timed_out"]:
            msg = (f"⏱ **超时（{self.runner.timeout}s）**\n\n"
                   f"> 语义是**结果未知**：任务可能已经执行了一部分，也可能没有。\n"
                   f"> 重试前请先核实实际状态，别直接重发。\n"
                   f"> 想改超时：`.env` 里的 `CC_TIMEOUT_SEC`。")
        elif res["stopped"]:
            msg = "⏹ 已按 `/stop` 停止（进程树已清理）。"
        elif res["is_error"]:
            msg = (f"❌ **执行出错**（subtype=`{res['subtype']}`，rc={res['rc']}）\n\n"
                   f"```\n{(res['text'] or '（无输出）')[:3000]}\n```")
        else:
            msg = res["text"] or "（无文本输出）"

        if res["denials"]:
            names = ", ".join(sorted({d.get("tool_name", "?") for d in res["denials"]}))
            msg += f"\n\n> ⚠️ 本次被权限拦截：{names}"
        if res.get("usage_footer"):
            self._last_usage[(chatid, job["userid"])] = res["usage_footer"]
            msg += res["usage_footer"]
        msg = note + msg

        self._finish(st, chatid, chat_type, msg)

        # 会话落库：拿到就存（失败那次也会带 session_id，存下来无妨，但别据此认为成功）
        if res.get("session_id"):
            # ⚠️ 但**跑的过程中用户可能发过 /new**（或点了卡片「清空」）——
            # 那时清库已经发生，这里再把旧 session 写回去，就等于"清空了又接上旧上下文"。
            # 代次变了就丢弃；日志留痕，免得以后有人以为会话丢了。
            if job.get("epoch", 0) != self._epoch_of(chatid, job["userid"]):
                log.info("[session] 任务期间被 /new 清过（代次 %s→%s），本轮 session 不落库",
                         job.get("epoch"), self._epoch_of(chatid, job["userid"]))
            else:
                self.store.save_session(chatid, job["userid"], res["session_id"], cwd)

    # -- 主循环 --
    def _heartbeat_loop(self):
        while not self.stop:
            if self.subscribed.is_set():
                self._send({"cmd": "ping", "headers": {"req_id": self._new_req_id()}})
            time.sleep(HEARTBEAT_SEC)

    def _watchdog_loop(self):
        """看门狗：旁路线程死了就重启；连接静默太久就主动断开重连。

        为什么需要：重连循环在**主线程**，而 worker / 心跳是旁路线程。
        它们意外退出时主线程照样"看起来正常"（连着、也在发心跳），
        但消息再也不会被处理 —— 这种**静默失效**最难发现。
        """
        while not self.stop:
            time.sleep(WATCHDOG_SEC)
            if self.stop:
                break
            for name, target in (("cc-worker", self._worker),
                                 ("wecom-heartbeat", self._heartbeat_loop),
                                 ("reply-worker", self._reply_worker),
                                 ("approval-loop", self._approval_loop)):
                t = self._threads.get(name)
                if t is None or t.is_alive():
                    continue
                log.error("[watchdog] 线程 %s 已退出，重启", name)
                nt = threading.Thread(target=target, daemon=True, name=name)
                self._threads[name] = nt
                nt.start()
                try:                     # 自愈要出声，别静默
                    cid, ctype = self.store.last_chat()
                    if cid:
                        self.send_markdown(cid, ctype,
                                           f"⚠️ 桥自愈：`{name}` 线程已重启。"
                                           f"上一轮任务若有回答缺失，请重发。")
                except Exception:
                    pass

            # 连接活性：订阅之后长期收不到任何帧 ⇒ 认为这条连接已死
            if self.stale_rx_sec > 0 and self.subscribed.is_set() and self.ws is not None:
                idle = time.time() - self._last_rx
                if idle > self.stale_rx_sec:
                    log.warning("[watchdog] 已 %.0fs 没收到任何帧（阈值 %ds），主动断开重连",
                                idle, self.stale_rx_sec)
                    self._last_rx = time.time()   # 别每 20s 重复触发
                    self.subscribed.clear()
                    try:
                        self.ws.close()
                    except Exception:
                        pass
                else:
                    log.debug("[watchdog] 距上帧 %.0fs，线程正常", idle)

    def run_forever(self):
        self._threads["wecom-heartbeat"] = threading.Thread(
            target=self._heartbeat_loop, daemon=True, name="wecom-heartbeat")
        self._threads["cc-worker"] = threading.Thread(
            target=self._worker, daemon=True, name="cc-worker")
        self._threads["reply-worker"] = threading.Thread(
            target=self._reply_worker, daemon=True, name="reply-worker")
        if self.approval_dir:
            self._threads["approval-loop"] = threading.Thread(
                target=self._approval_loop, daemon=True, name="approval-loop")
        for t in self._threads.values():
            t.start()
        threading.Thread(target=self._watchdog_loop, daemon=True,
                         name="watchdog").start()
        log.info("[main] 队列上限 %d，切分粒度 %dB，超时 %ds，看门狗 %ds/惰性 %ds",
                 self._qmax, self.chunk_bytes, self.runner.timeout,
                 WATCHDOG_SEC, self.stale_rx_sec)
        while not self.stop:
            try:
                log.info("[ws] 连接 %s ...", WS_URL)
                self.ws = websocket.WebSocketApp(
                    WS_URL, on_open=self._on_open, on_message=self._on_message,
                    on_error=self._on_error, on_close=self._on_close)
                self.ws.run_forever(ping_interval=0)
            except Exception as e:
                log.warning("[ws] run_forever 异常: %s", e)
            if self.stop:
                break
            log.info("[ws] %ss 后重连", self._backoff)
            time.sleep(self._backoff)
            self._backoff = min(self._backoff * 2, BACKOFF_MAX)

    def stop_bot(self):
        self.stop = True
        self.runner.kill_current()
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass


def _looks_like_resume_failure(text):
    t = (text or "").lower()
    return any(s in t for s in ("no conversation found", "session not found",
                                "could not find session", "invalid session"))


def safe_to_retry_resume(res):
    """resume 失效时，**这一轮能不能安全重跑**？返回 (ok, 原因)。

    判据是"第一轮到底动没动手"，不是"错误文本像不像"（2026-10-29 复核 #3）：
      · `tools > 0`        —— 调过工具，可能已经改了文件 / 提交过 / 发过消息
      · `stream_failed`    —— 流断了，执行到哪一步不知道
      · `stopped`          —— 被 `/stop` 打断过
      · `timed_out`        —— 超时过，可能已执行了一部分
    占任意一条就**不能**重跑：重跑 = 把整条指令再执行一遍 ⇒ 副作用重复
    （重复写入、重复提交、重复调外部服务）。宁可让用户自己核实后再发。
    """
    if not (res.get("is_error") and _looks_like_resume_failure(res.get("text") or "")):
        return False, "不是 resume 失效"
    if res.get("tools"):
        return False, f"本轮已执行 {res['tools']} 次工具调用"
    if res.get("stream_failed"):
        return False, "流中断，结果未知"
    if res.get("stopped"):
        return False, "本轮被 /stop 打断过"
    if res.get("timed_out"):
        return False, "本轮超时过"
    return True, "确认这一轮没动手"


def demo_card(kind):
    """构造一张**探针用**的模板卡片。只用来回答两个问题：
    ① 长连接机器人到底能不能发卡片？② 点了按钮，回调里回来的是什么？
    字段按官方 `101032` 的最小必填集写，故意不带 `card_action`（那要外链）。
    """
    ts = datetime.now().strftime("%H:%M:%S")
    # ⚠️ 官方文档把 `card_action` 标成"否"，但**实测必填**：
    # 不带它平台直接拒收 `errcode=42045 Template_Card card_action Missing or Invalid`
    # （2026-10-29 01:22，text_notice）。所以两种卡都带上一个跳转。
    jump = {"type": 1, "url": "https://work.weixin.qq.com"}
    if kind == "button":
        # 必填：main_title / button_list / task_id（task_id 同一机器人不许重复）
        return {
            "card_type": "button_interaction",
            "source": {"desc": "WeCom-Agent-Bridge 探针"},
            "main_title": {"title": "按钮卡片测试", "desc": "点任意一个，看回调里回来什么"},
            "sub_title_text": "这张卡片只用来验证长连接模式能否下发可点击卡片。",
            "button_list": [
                {"text": "允许", "style": 1, "key": "allow"},
                {"text": "只读", "style": 2, "key": "readonly"},
                {"text": "拒绝", "style": 3, "key": "deny"},
            ],
            "task_id": "ccprobe-" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "card_action": jump,
        }
    # text_notice：main_title / sub_title_text 至少一项
    return {
        "card_type": "text_notice",
        "source": {"desc": "WeCom-Agent-Bridge 探针"},
        "main_title": {"title": "文本卡片测试", "desc": "长连接模式 · 无回调要求"},
        "sub_title_text": f"如果你看到这张卡片，说明长连接机器人能发 template_card（{ts}）。",
        "horizontal_content_list": [
            {"keyname": "card_type", "value": "text_notice"},
            {"keyname": "发起时间", "value": ts},
        ],
        "card_action": jump,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump-raw", action="store_true")
    ap.add_argument("--send-card", choices=["text", "button"], metavar="{text,button}",
                    help="发一张模板卡片探针（配合 --chatid；不传就用最近会话）")
    ap.add_argument("--send-test", metavar="TEXT")
    ap.add_argument("--send-test-file", metavar="PATH",
                    help="把文件内容整体推一条（测平台大小上限用，命令行放不下大文本）")
    ap.add_argument("--chatid")
    ap.add_argument("--chat-type", type=int, default=None,
                    help="1=单聊 2=群聊；不传则按最近会话自动判断")
    ap.add_argument("--send-auto", action="store_true",
                    help="配合 --send-test/-file：走 send_chunks（先整条、40058 才切成多片），"
                         "用来验证分片策略本身")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--install-autostart", action="store_true",
                    help="装进任务计划程序：登录时自动启动（pythonw，无窗口）")
    ap.add_argument("--uninstall-autostart", action="store_true")
    ap.add_argument("--autostart-status", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)

    # 这三个子命令不连平台，先处理掉，免得被后面的凭据检查挡住
    if args.install_autostart:
        ok, msg = install_autostart()
        print(msg)
        return 0 if ok else 1
    if args.uninstall_autostart:
        ok, msg = uninstall_autostart()
        print(msg)
        return 0 if ok else 1
    if args.autostart_status:
        rc, out = autostart_status()
        print(out)
        return 0 if rc == 0 else 1

    bot_id, secret = cfg("WECOM_BOT_ID"), cfg("WECOM_BOT_SECRET")
    if not bot_id or not secret:
        log.error("缺少 WECOM_BOT_ID / WECOM_BOT_SECRET —— 请填 %s",
                  os.path.join(ROOT, ".env"))
        return 2
    refusal = whitelist_gate(cfg("WECOM_ALLOWED_USERIDS"),
                             cfg_bool("WECOM_ALLOW_ANYONE", False))
    if refusal:
        log.error(refusal)
        return 3
    if not cfg("WECOM_ALLOWED_USERIDS"):
        log.warning("WECOM_ALLOW_ANYONE=1 ⇒ **任何人都能指挥它**，请确认这是你要的")

    # 除认证外，还要把 `wecom-cli` 的凭据目录透传给子进程:
    # 不透传的话，模型跑 `wecom-cli` 会落到默认目录
    # （那是另一个机器人的身份）。只透传 WECOM_CLI_*，别的变量不外泄。
    inject = {k: v for k, v in ENV.items()
              if k.startswith("ANTHROPIC") or k.startswith("WECOM_CLI_")}
    if not inject.get("ANTHROPIC_AUTH_TOKEN"):
        log.error("桥的 .env 里没有 ANTHROPIC_AUTH_TOKEN。"
                  "因为用 --setting-sources 排掉了 user 设置，认证必须自带。")
        return 2

    cc_cwd = cfg("CC_CWD", ROOT)
    runner = Runner(
        exe=cfg("CC_CLAUDE_EXE", os.path.expanduser(r"~\.local\bin\claude.exe")),
        model=cfg("CC_MODEL"),
        # 默认 dontAsk：实测（tools/probe_permission_modes.py，2026-10-28）在 headless 下
        # **只有 plan / dontAsk / manual 真的拦住 Bash+Write**；acceptEdits 与 auto 照样执行。
        # dontAsk 比 plan 干净（plan 会往 ~/.claude/plans/ 写计划文件），且拒绝会进
        # permission_denials，桥能如实回报给用户。想让机器人干活再自己改成全自动。
        perm_mode=cfg("CC_PERMISSION_MODE", DEFAULT_PERM_MODE),
        sources=cfg("CC_SETTING_SOURCES", "project,local"),
        timeout=cfg_int("CC_TIMEOUT_SEC", 600),
        allowed=cfg("CC_ALLOWED_TOOLS"),
        disallowed=cfg("CC_DISALLOWED_TOOLS", DEFAULT_DISALLOWED),
        inject_env=inject,
        show_usage=cfg_bool("CC_SHOW_USAGE", True),
    )
    # 单实例保护：自启 + 手动启动/测试同时跑 = 双份回复、双份执行
    if not acquire_single_instance():
        # 返回 0：这是**正常**结果，不是失败 —— 任务计划程序每 3 分钟来敲一次门，
        # 活着的时候它就该安静地什么都不做（返回非 0 会被记成失败）。
        log.info("[main] 已有实例在跑（互斥量 %s），本次启动放弃。"
                 "要重启请先停掉常驻实例；测试发送同理。", MUTEX_NAME)
        return 0

    store = Store(cfg("CC_DB", DEFAULT_DB))
    # 审批卡片（默认关）：开了才会把决策目录注入子进程环境，hook 也才会干活。
    approval_dir = ""
    if cfg_bool("CC_APPROVAL", False):
        approval_dir = cfg("CC_APPROVAL_DIR") or os.path.join(ROOT, "data", "approvals")
        try:
            os.makedirs(approval_dir, exist_ok=True)
        except OSError as e:
            log.error("[approval] 建不出决策目录 %s：%s", approval_dir, e)
            return 5
        # 两道自检缺一不可：
        #   ① 静态 —— Claude **会不会加载**这个 hook（settings 注册 + setting-sources）
        #   ② 运行 —— 这个 hook **跑不跑得起来**（真起一个进程，要求它写出请求）
        ok_cfg, why_cfg = approval_configured(cc_cwd, runner.sources)
        ok_run, why_run = (False, "（静态检查没过，未执行）")
        if ok_cfg:
            ok_run, why_run = approval_selfcheck(cc_cwd, approval_dir)
        if not (ok_cfg and ok_run):
            # ⚠️ **拒绝启动**，不再"降级成不启用审批"（2026-10-29 复核 #2①）。
            # 原写法：自检失败只打一行 ERROR，然后把 approval_dir 置空照常启动 ——
            # 用户明明按了"要审批"，实际却在**全自动无闸**状态下跑。那和"假装有闸门"没区别。
            log.error("[approval] 你设了 CC_APPROVAL=1，但审批**没法真正生效** —— 拒绝启动。")
            log.error("[approval] ① 加载链路：%s", why_cfg)
            log.error("[approval] ② 脚本可跑：%s", why_run)
            log.error("[approval] 修好再启；或不想要审批就把 .env 里的 CC_APPROVAL 改成 0"
                      "（那是一次显式选择，不会有闸门的错觉）。")
            return 5
        log.info("[approval] 自检通过：%s；hook 能写出请求。审批已开启 —— 等待 %ss、"
                 "只读直放=%s", why_cfg, cfg_int("CC_APPROVAL_WAIT_SEC", 120),
                 cfg("CC_APPROVAL_ALLOW") or "(默认 Read,Grep,Glob,TodoWrite)")
    bridge = Bridge(bot_id, secret, cfg("WECOM_ALLOWED_USERIDS"),
                    cfg("WECOM_ALLOWED_CHATIDS"), cfg("WECOM_BOT_NAME"),
                    store, runner, cc_cwd,
                    cfg_int("CHUNK_BYTES", 18000), cfg_int("QUEUE_MAX", 20),
                    cfg_int("CC_PROGRESS_SEC", 90), dump_raw=args.dump_raw,
                    stale_rx_sec=cfg_int("CC_STALE_RX_SEC", STALE_RX_SEC),
                    progress_hb_sec=cfg_int("CC_PROGRESS_HEARTBEAT_SEC",
                                            PROGRESS_HEARTBEAT_SEC),
                    approval_dir=approval_dir,
                    approval_wait_sec=cfg_int("CC_APPROVAL_WAIT_SEC", 120),
                    approval_allow=cfg("CC_APPROVAL_ALLOW"))

    if cfg_bool("CC_KEEP_AWAKE", True):
        start_keep_awake()

    log.info("[main] cwd=%s 档位=%s 模型=%s", cc_cwd, runner.perm_mode,
             runner.model or "(默认别名)")
    if runner.perm_mode in ("bypassPermissions", "auto", "acceptEdits"):
        log.warning("[main] 档位=%s ⇒ 模型会**直接执行本机命令、直接改文件**，"
                    "不弹任何确认（实测这三档在 headless 下都不拦 Bash）。"
                    "请确认白名单里只有你自己。", runner.perm_mode)

    if args.send_test or args.send_test_file or args.send_card:
        payload = args.send_test or ""
        if args.send_test_file:
            with open(args.send_test_file, encoding="utf-8") as f:
                payload = f.read()
        chatid, derived_type = (args.chatid, None) if args.chatid else store.last_chat()
        if not chatid:
            log.error("[test] 无目标 chatid：先让用户给机器人发条消息，或显式 --chatid")
            return 4
        chat_type = args.chat_type if args.chat_type is not None else (derived_type or 2)
        if args.send_card:
            log.info("[test] 目标 chatid=%s type=%s 卡片=%s",
                     chatid, chat_type, args.send_card)
        else:
            nbytes = len(payload.encode("utf-8"))
            log.info("[test] 目标 chatid=%s type=%s 内容 %d 字节（%d 字符）",
                     chatid, chat_type, nbytes, len(payload))

        bridge.ws = websocket.WebSocketApp(
            WS_URL, on_open=bridge._on_open, on_message=bridge._on_message,
            on_error=bridge._on_error, on_close=bridge._on_close)
        threading.Thread(target=bridge.ws.run_forever,
                         kwargs={"ping_interval": 0}, daemon=True).start()
        if not bridge.subscribed.wait(timeout=15):
            log.error("[test] 订阅超时（15s）")
            return 3
        time.sleep(1)
        if args.send_card:
            r = bridge.send_card(chatid, chat_type, demo_card(args.send_card))
            log.info("[test] 卡片回执: %s", r)
            log.info("[test] >>> %s", "卡片送达" if r["sent"] else "卡片被拒收/未知")
            ok = r["sent"]
            if ok and args.send_card == "button":
                # 卡片是**要人点**的，不睡一会儿就 close，事件还没回来连接就没了。
                log.info("[test] 等 180 秒收点击回调（点了按钮才会打印 event）…")
                time.sleep(180)
        elif args.send_auto:
            failed = bridge.send_chunks(chatid, chat_type, payload)
            log.info("[test] send_chunks 未送达段数: %d", len(failed))
            log.info("[test] >>> %s", "全部送达" if not failed else "有段落未送达")
            ok = not failed
        else:
            r = bridge.send_markdown(chatid, chat_type, payload)
            log.info("[test] 平台回执结果: %s", r)
            log.info("[test] >>> %s", "送达" if r["sent"] else "被拒收/未知")
            ok = r["sent"]
        time.sleep(2)
        bridge.stop_bot()
        return 0 if ok else 1

    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        log.info("[main] 退出")
        bridge.stop_bot()
    return 0


if __name__ == "__main__":
    sys.exit(main())