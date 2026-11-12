"""宿主基建：日志落盘与保留、单实例互斥量、防睡眠、任务计划程序自启。

这些是"这个进程怎么活在 Windows 上"的事，与聊天平台无关，也与 Agent 内核无关。
"""

import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timedelta

from common import (ENV, LOGDIR, LOG_KEEP_DAYS, ROOT,
                    bridge_instance, bridge_instance_problem, cfg, cfg_bool,
                    cfg_int, knob, log, mutex_name, task_name)


def prune_old_logs(days=LOG_KEEP_DAYS):
    """删掉 N 天前的 `bridge-YYYYMMDD.log`（以及 `raw-stream/` 里的原始流）。

    按**文件名里的日期**判，不看 mtime —— mtime 会被"打开过/复制过"改写。
    启动时跑一次，**看门狗每天再跑一次**（常驻进程可能几个星期不重启，
    只在启动时清理等于根本不清理 —— 2026-10-29 复核 #9）。
    """
    cut = (datetime.now() - timedelta(days=days)).date()
    n = 0
    try:
        names = os.listdir(knob("LOGDIR"))
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
                os.remove(os.path.join(knob("LOGDIR"), name))
                n += 1
            except OSError as e:
                log.warning("[log] 删不掉 %s: %s", name, e)
    # `raw-stream/` 里是"跑完没拿到 result"时落盘的原始流（排查用）。
    # 文件名带 `YYYYMMDD-` 前缀，同样按名字里的日期清，不看 mtime。
    d2 = os.path.join(knob("LOGDIR"), "raw-stream")
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


class _RedactFilter(logging.Filter):
    """把日志里的「凭据类查询参数」打码。

    触发点：飞书 SDK 建连成功会打
    `connected to wss://…?…&access_key=xxxx&ticket=xxxx [conn_id=…]` ——
    这两个是**这一条连接的身份**，不该出现在任何会被传阅的日志里。
    只打码值、保留键名，排障时仍然看得出"它带了什么参数"。
    """

    _PAT = re.compile(r"(access_key|ticket|app_secret|secret)=([^&\s\]]+)")

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True                      # 取不出消息就别拦，交给 logging 自己报
        fixed = self._PAT.sub(r"\1=***", msg)
        if fixed != msg:
            record.msg, record.args = fixed, ()
        return True


def setup_logging(verbose=False):
    os.makedirs(LOGDIR, exist_ok=True)
    path = os.path.join(LOGDIR, f"bridge-{datetime.now():%Y%m%d}.log")
    fmt = logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")
    fh = DailyFileHandler(LOGDIR, encoding="utf-8")
    fh.setFormatter(fmt)
    log.setLevel(logging.DEBUG if verbose else logging.INFO)
    log.addHandler(fh)
    # 飞书 SDK 自己的 logger（`lark_oapi.core.log`）叫 "Lark"，它在 import 时就挂了
    # `StreamHandler(sys.stdout)` —— 而 **pythonw 下 `sys.stdout` 是 None**，
    # 于是它所有的 `connected to …` / `receive message loop exit` / `trying to reconnect`
    # **全部蒸发**（2026-11-06 实测：断网 10 分钟，我们的日志一个字都没有）。
    # ⇒ 把同一份文件 handler 也挂给它，掉线与重连才留得下证据。同一个 handler 换天一起切。
    sdk_log = logging.getLogger("Lark")
    sdk_log.addHandler(fh)
    sdk_log.setLevel(logging.DEBUG if verbose else logging.INFO)
    # ⚠️ 接上日志的同时会**接到凭据**：SDK 每次建连都把整条 URL 打出来，
    # 里面带 `access_key=` 与 `ticket=`（2026-11-06 实测）。落盘前必须打码 ——
    # 日志会被打包发给别人排障，那不是贴凭据的地方。
    sdk_log.addFilter(_RedactFilter())
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


def acquire_single_instance(name=None):
    """单实例保护：Windows 命名互斥量，进程退出由系统自动释放（不会留脏锁）。

    为什么必须有：自启 + 手动启动（或 `--send-test` 测试）同时跑 ⇒
    两条长连接、同一条消息回两遍、两个 claude 抢同一个 sqlite 和同一个仓库。

    `name` 留空 = 用 `mutex_name()` 派生的名字（基名 + 平台 + `BRIDGE_INSTANCE`）。
    """
    global _MUTEX_HANDLE
    if os.name != "nt":
        return True
    name = name or mutex_name()
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
    # 说明里带实例名：同机两套桥的任务在「任务计划程序」里长得一模一样，
    # 光看名字分不清谁是谁（而这两个任务恰恰是最容易互相覆盖的东西）。
    inst = bridge_instance()
    desc = "wecom-cc-bridge: WeCom bot bridge to local Claude Code"
    if inst:
        desc = f"{desc} [instance: {inst}]"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{_xml_escape(desc)}</Description>
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
    """装开机自启。⚠️ `schtasks /Create … /F` **会静默覆盖同名任务** —— 见 `task_name()`。"""
    problem = bridge_instance_problem()
    if problem:
        log.error("[autostart] %s —— 不装（装上去也分不清是哪一套桥）", problem)
        return False, problem
    tn = task_name()
    tmp = os.path.join(tempfile.gettempdir(), f"{tn}-task.xml")   # 按任务名分开，别撞
    with open(tmp, "w", encoding="utf-16") as f:   # schtasks /XML 要 UTF-16
        f.write(_autostart_xml())
    rc, out, err = _schtasks("/Create", "/TN", tn, "/XML", tmp, "/F")
    log.info("[autostart] 安装 %r rc=%s %s %s", tn, rc, out.replace("\n", " "), err)
    return rc == 0, (out or err)


def uninstall_autostart():
    tn = task_name()
    rc, out, err = _schtasks("/Delete", "/TN", tn, "/F")
    log.info("[autostart] 卸载 %r rc=%s %s %s", tn, rc, out.replace("\n", " "), err)
    return rc == 0, (out or err)


def autostart_status():
    tn = task_name()
    rc, out, err = _schtasks("/Query", "/TN", tn, "/FO", "LIST")
    return rc, (out or err)
