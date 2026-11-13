"""换平台：**预检 → 写真源 → 拉新进程 → 看守 → 停旧（或回滚）**。

为什么必须是"重启进程"而不是像 `/model` 那样热改（2026-11-13 查证）：
    · 平台决定了**装配哪个类**（`WeComBridge` / `FeishuBridge` / `SlackBridge`），
      而这三个类在**导入期**就被拉进来了 —— 活着改不了；
    · 飞书的长连接**没有公开的关闭接口**（`feishu_stop()` 的注释里写着"靠进程退出把
      socket 带走"）。⇒ 就算在进程里换一个桥对象，那条 ws 也停不下来。
    所以"切换"只有一个诚实的实现：写配置 + 换进程。速度上不可能"秒切"，
    回执里必须如实写"约 1~2 分钟"（2026-11-13 实测：17:05:35 起进程、17:07:23
    才 `subscribe ok`，中间四次连接失败在退避重试）。

**看守窗口**（用户 2026-11-13 拍板要的）：新进程起不来就自动回滚。
    宁可多花二十秒，也不要出现"旧平台已经断了、新平台又起不来"这种**两边都没人**
    的状态 —— 那种情况只能跑到机器前手动救。
"""

import ctypes
import importlib.util
import json
import os
import subprocess
import sys
import time

from common import default_approval_dir, env_bool, log
from core.platform_store import PLATFORMS, valid_platform
from core.runtime_file import read_runtime  # noqa: F401（对外转发，测试直接从这里取）

# 上次启动**真的通过了所有闸门**的证据。放在 `data/runtime/` 下，按实例分开
# —— 同机可以跑多套桥（`BRIDGE_INSTANCE`），共用一份会互相覆盖。
RUNTIME_DIR = "runtime"


def runtime_path(root, instance=""):
    return os.path.join(root, "data", RUNTIME_DIR,
                        f"bridge-{instance or 'default'}.json")


# ---------------- 预检 ----------------

def _fresh_env():
    """**现读一遍 `.env`**，不用进程启动时那份快照。

    为什么：用户可能刚把飞书凭据写进 `.env` 就想切换，而 `common.ENV` 是导入时读的。
    新进程会看到新值 —— 预检必须和**新进程看到的是同一份**，否则会出现
    "预检说缺凭据、其实新进程能起来"这种把人挡在门外的假失败。
    """
    from common import load_env
    try:
        return load_env()
    except Exception as e:                          # noqa: BLE001
        log.warning("[switch] 现读 .env 失败（%s），退回启动时的快照", e)
        return {}


def _getter(env):
    def get(key, default=""):
        return os.environ.get(key) or env.get(key) or default
    return get


def preflight(platform, preflights, get=None, importable=None):
    """切换前的静态检查。返回 `(ok, lines)`；lines 无论成败都要回给发起人。

    **平台专属的判据不在这个文件里**（凭据键名 / 白名单口径 / 依赖），
    在 `platforms/<平台>/preflight.py` —— 内核不认识任何一个平台，
    `tools/check_layers.py` 会盯着这条边界。这里只做三件平台无关的事：
    校验名字、找到对应的预检、把审批决策目录准备好。

    检查的必须和 `main()` 里**会拒绝启动**的那几条对齐，否则
    "预检过了、一启动就退"就是最坏的一种失败（旧平台的连接已经断了）。
    """
    get = get or _getter(_fresh_env())
    importable = importable or (lambda mod: importlib.util.find_spec(mod) is not None)
    ok, why = valid_platform(platform)
    if not ok:
        return False, [f"❌ 目标平台不合法：{why}"]
    fn = (preflights or {}).get(platform)
    if fn is None:
        # 接线漏了。**必须拒绝**：没有预检就等于"闭着眼切"，而失败要回滚一次进程。
        return False, [f"❌ 没有为 `{platform}` 注册预检（`bridge.py` 的接线漏了）"]

    ok, lines = fn(get, importable)
    lines = list(lines or [])

    # 审批决策目录按平台分开（共用会互相毙请求）。这里只是提前建出来，
    # 和 `main()` 里那句 makedirs 是同一件事 —— 免得"预检过了、一启动就退 5"。
    if ok and env_bool(get, "CC_APPROVAL", False):
        d = get("CC_APPROVAL_DIR", "") or default_approval_dir(platform)
        try:
            os.makedirs(d, exist_ok=True)
            lines.append(f"· 审批：开着，决策目录 `{d}` 已就绪")
        except OSError as e:
            ok = False
            lines.append(f"  · 审批决策目录建不出来（{d}）：{e}")

    mark = "✅ " if ok else "❌ "
    return ok, [mark + (lines[0] if lines else f"{platform} 预检")] + lines[1:]


# ---------------- 进程存活 ----------------

def pid_alive(pid):
    """这个 pid 还活着吗。**别用 `os.kill(pid, 0)`** —— Windows 上它的语义不一样。"""
    if os.name != "nt":
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.restype = ctypes.c_void_p
        h = k32.OpenProcess(0x1000, False, int(pid))      # QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not k32.GetExitCodeProcess(ctypes.c_void_p(h), ctypes.byref(code)):
                return False
            return code.value == 259                       # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(h))
    except Exception:                                      # noqa: BLE001
        # 判活自己出错时**按活着处理**：宁可少回滚一次，也别把好进程当死的收拾。
        return True


# ---------------- 切换器 ----------------

class Switcher:
    """把"写配置 + 拉新进程 + 看守 + 停旧"收在一处。

    可注入 `spawn` / `wait_ready`，所以离线单测不需要真起进程
    （真起进程会杀掉正在跑的任务 —— 包括正在跟我说话的那个会话）。
    """

    def __init__(self, store, root, script, current="", instance="",
                 guard_sec=25, guard_hard_sec=120, log_fn=None, spawn=None,
                 wait_ready=None, env_get=None, import_ok=None, preflights=None,
                 kill=None):
        self.store = store
        self.root = root
        self.script = script
        self.current = current
        self.instance = instance
        # 软窗口 / 硬上限。**软窗口不是"多久算失败"**，而是"多久之后开始催"：
        # 只要新进程还活着就继续等，直到硬上限。
        # 为什么要有这条（2026-11-13 真机）：`import lark_oapi` 冷加载实测 **68 秒**，
        # 固定 25 秒的判断在那种情况下必然误判 —— 而误判的代价不是"慢"，是
        # "用户看到失败、系统实际已经切走"。
        self.guard_sec = float(guard_sec or 25)
        self.guard_hard_sec = max(self.guard_sec, float(guard_hard_sec or 120))
        self.runtime_path = runtime_path(root, instance)
        self._log = log_fn or log
        self._spawn = spawn or self._default_spawn
        self._wait_ready = wait_ready or self._default_wait_ready
        self._kill = kill or self._default_kill
        # 配置来源。默认是"现读 `.env`"（见 `_fresh_env`）；单测注入假表。
        self._env_get = env_get
        self._import_ok = import_ok
        # 平台 -> 预检函数（`platforms/<平台>/preflight.py`）。由 `bridge.py` 接线 ——
        # 内核**不 import 任何平台包**，这条边界有 `tools/check_layers.py` 盯着。
        self.preflights = preflights or {}
        # 已经有一次性切换在看守中 —— **必须拦住第二次**：第二次会拉起同一个平台的
        # 第二个进程，而它会和第一个抢同一把互斥量（后到的那份正常退出=浪费一次），
        # 中间还夹着一次真源写入，日志会变得没法读。
        self.switching = False
        self.last_switch_path = os.path.join(
            os.path.dirname(self.runtime_path), f"last-switch-{instance or 'default'}.json")

    # -- 预检 + 写真源（同步，快） --

    def request(self, target, actor=""):
        """返回 `(ok, text)`。**不动进程** —— 调用方要先把 text 发出去。"""
        ok, why = valid_platform(target)
        if not ok:
            return False, f"❌ {why}"
        if self.switching:
            return False, "⚠️ 已经有一次切换在看守中，等它结束（约 25 秒）再说。"
        if target == self.current:
            return False, f"当前已经是 `{target}`，没有改动。"
        ok, lines = preflight(target, self.preflights, get=self._env_get,
                              importable=self._import_ok)
        if not ok:
            return False, "\n".join(lines)
        ok, msg = self.store.set(target, actor=actor)
        if not ok:
            return False, f"❌ {msg}"
        self.switching = True
        lines = lines + [
            "",
            f"🔄 正在拉起 `{target}`：新进程起得来才停掉当前这个"
            f"（软窗口 {int(self.guard_sec)} 秒，还活着就最多等 "
            f"{int(self.guard_hard_sec)} 秒）。",
            "> 生效需要约 1~2 分钟（重新建连）。**正在跑的任务会被终止。**",
        ]
        return True, "\n".join(lines)

    # -- 拉起 + 看守（后台） --

    def begin(self, target, origin, notify):
        """在**后台线程**里跑。`notify(text)` 用来在失败时往原平台说话。

        成功路径上**不再发消息**：写完配置就该把旧连接让出来，
        多发一条只会挤在那 1~2 分钟的窗口里，让用户以为"还没切"。
        """
        since = time.time()
        try:
            proc = self._spawn()
        except Exception as e:                     # noqa: BLE001
            self._rollback(origin, notify, f"拉新进程就失败了：{type(e).__name__}: {e}")
            return
        try:
            ok, detail = self._wait_ready(target, since, proc)
        except Exception as e:                     # noqa: BLE001
            # 看守自己出错也要走回滚 —— 否则真源停在一个没验证过的平台上，
            # 而这条线程已经死了，没人会再来收拾。
            self._kill_spawned(proc)
            self._rollback(origin, notify, f"看守过程出错：{type(e).__name__}: {e}")
            return
        if not ok:
            # 🔴 **放手之前必须把这个孩子收掉**（2026-11-13 真机踩到）：
            # 第一次切飞书时它 `import lark_oapi` 花了 **68 秒**（冷加载），
            # 25 秒的看守窗口没等到就回滚并给用户报了"失败" ——
            # 可那个孩子**还活着**，一分钟后自己起来了、真的接管了飞书。
            # 结果：用户看到"失败"，实际已经切走；`platform.json` 和现场**不一致**。
            # 不杀它，就永远有这条"迟到的接管"。
            self._kill_spawned(proc)
            self._rollback(origin, notify, detail)
            return
        self._log.info("[switch] `%s` 新进程就绪（%s），停掉旧进程", target, detail)
        self._record(ok=True, target=target, origin=origin, detail=detail)
        # 交接完成。旧进程怎么退由调用方决定（它要 hold 住互斥量到最后一刻）。
        if getattr(self, "on_ready", None):
            try:
                self.on_ready(target, detail)
            except Exception as e:                 # noqa: BLE001
                self._log.error("[switch] 交接回调失败：%s", e)
                self.switching = False
        else:
            self.switching = False                  # 测试桩：没人接手就自己放开

    def _kill_spawned(self, proc):
        """把**这一次我们自己拉起来的**孩子收掉。别碰任何别的实例 —— 只认这个句柄。

        ⚠️ 判活不用 `proc.poll()` 决定要不要杀：venv 的 `pythonw.exe` 是**等待型启动器**
        （实测：子进程活着时 `poll()` 一直是 None，直到它退出）。所以这里只看
        "还能不能拿到 pid"，杀失败也无所谓（可能它刚好自己退了）。
        """
        if proc is None:
            return
        pid = getattr(proc, "pid", None)
        if not pid:
            return
        try:
            self._kill(pid)
            self._log.info("[switch] 已收掉没就绪的新进程 pid=%s", pid)
        except Exception as e:                     # noqa: BLE001
            self._log.error("[switch] 收不掉新进程 pid=%s：%s —— 它可能会迟到接管",
                            pid, e)

    def _rollback(self, origin, notify, detail):
        ok, msg = self.store.set(origin, actor="auto-rollback")
        self.switching = False
        self._record(ok=False, target="", origin=origin, detail=detail)
        head = (f"⚠️ 切到新平台**失败**，已回滚到 `{origin}`，桥继续在原平台跑。")
        if not ok:
            head += ("\n\n🚨 回滚也写不进去（见日志）—— 下次重启会照着 `platform.json` "
                     "里那个起不来的平台走。请手动改回 `data/platform.json`。")
        if notify is not None:
            try:
                notify(f"{head}\n\n原因：{detail}")
            except Exception as e:                 # noqa: BLE001
                self._log.error("[switch] 回滚通知发不出去：%s", e)
        self._log.error("[switch] 回滚到 %r（%s）；写入结果=%s", origin, detail, msg)

    def _record(self, ok, target, origin, detail):
        """把"上一次切换的结果"落一份盘 —— 控制目录那条路没有聊天会话可通知，
        事后只能靠这个文件回答"昨晚那次切到底成没成"。"""
        try:
            parent = os.path.dirname(self.last_switch_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            tmp = self.last_switch_path + ".tmp"
            with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                json.dump({"ok": bool(ok), "target": target, "origin": origin,
                           "detail": detail, "ts": time.time(),
                           "ts_h": time.strftime("%Y-%m-%dT%H:%M:%S")},
                          f, ensure_ascii=False, indent=2)
                f.write("\n")
            os.replace(tmp, self.last_switch_path)
        except Exception as e:                     # noqa: BLE001
            self._log.warning("[switch] 落切换结果失败：%s", e)

    # -- 默认实现（真机用） --

    def _default_kill(self, pid):
        """杀**进程树**（venv 的启动器还有个真的解释器孩子）。"""
        from core.runner import _kill_tree
        _kill_tree(pid)

    def _default_spawn(self):
        """起一个新的桥进程。**同一份脚本、同一个 cwd** —— 入口不变。

        `platform.json` 已经写好了，所以新进程自己会装配另一个平台。
        互斥量按平台分（`mutex_name()`），所以新旧两个能短暂共存，
        这正是"看守窗口"成立的前提；反过来说，**同一个平台**的新旧两份会互相顶掉，
        所以这里绝不重复拉起同一平台。
        """
        py = sys.executable
        flags = 0
        if os.name == "nt":
            flags = (getattr(subprocess, "DETACHED_PROCESS", 0)
                     | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        self._log.info("[switch] 拉起新进程：%s -X utf8 %s", py, self.script)
        return subprocess.Popen(
            [py, "-X", "utf8", self.script], cwd=self.root,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, creationflags=flags, close_fds=True)

    def _default_wait_ready(self, target, since, proc):
        """等新进程写出"启动检查全过"的凭据文件。

        ⚠️ **判据是"过完所有闸门"，不是"连上了"**：连上最长要 100 多秒，
        把它算进看守窗口只会让回滚来得太晚；而起不来的原因（凭据错 / 白名单空 /
        缺依赖）全在闸门那一段，早就暴露了。

        🔴 **软窗口 / 硬上限**（2026-11-13 真机修）：
        过了软窗口**不等于失败**。只要新进程还活着，就继续等（每 10 秒喊一声），
        直到硬上限。理由：冷启动可能有一次性开销（`import lark_oapi` 实测 68 秒），
        拿固定秒数判死会把"正在起来"当成"起不来" —— 而那个孩子还会迟到接管。
        反过来，**进程真的退出**（`proc.poll()` 非 None）就立刻判失败，不用等满。

        ⚠️ `proc.poll()` 在这里是**可信的**：venv 的 `pythonw.exe` 是等待型启动器
        （实测：子解释器活着时 `poll()` 一直 None，直到它退出）。
        但它只用来判"死了没有"，**不用来判"起来了没有"** —— 那只看凭据文件。
        """
        soft = time.time() + self.guard_sec
        hard = time.time() + self.guard_hard_sec
        said = False
        while time.time() < hard:
            d = read_runtime(self.runtime_path)
            if d:
                started = float(d.get("started_at") or 0)
                plat = d.get("platform") or ""
                fresh = started >= since - 2
                # 目标平台已经在跑（不管是不是这一次拉起来的）就算交接成功：
                # 可能是同机的另一套实例已经在服务它（互斥量按平台分，允许并存）。
                # 判活只看 pid —— 陈旧文件的 pid 早就死了。
                if plat == target and pid_alive(d.get("pid")):
                    return True, (f"pid {d.get('pid')}" if fresh
                                  else f"pid {d.get('pid')}（已有实例在跑，沿用）")
                if plat != target and fresh:
                    # 有进程刚起来，只不过它装配的是别的平台 —— 那是配置没生效
                    return False, (f"新进程起来了但装配的是 `{plat}`（应为 `{target}`）；"
                                   "`platform.json` 可能被别的进程改过")
            try:
                rc = proc.poll() if proc is not None else None
            except Exception:                      # noqa: BLE001
                rc = None
            if rc is not None:
                return False, (f"新进程退出了（exit={rc}），没写出就绪凭据 —— "
                               f"查 `logs/` 里它最后几行")
            now = time.time()
            if not said and now > soft:
                said = True
                self._log.warning("[switch] 新进程还活着但还没就绪（已等 %ds），"
                                  "继续等到 %ds", int(self.guard_sec),
                                  int(self.guard_hard_sec))
            time.sleep(0.5)
        return False, (f"等了 {int(self.guard_hard_sec)} 秒仍没等到 `{target}` 的就绪凭据"
                       f"（{self.runtime_path}）；新进程还活着但没走完启动闸门 —— "
                       "查 `logs/` 里它停在哪一步")


# ---------------- 给人看的清单 ----------------

def platform_list_text(store):
    """`/platform` 不带参数时的回答。"""
    snap = store.snapshot()
    lines = [f"🌉 **当前平台**：`{snap['current'] or '（未设置）'}`"]
    if snap["updated_at"]:
        by = f"，by {snap['updated_by']}" if snap["updated_by"] else ""
        lines.append(f"> 更新于 {snap['updated_at']}{by}")
    if not snap["file_ok"]:
        lines.append(f"> ⚠️ 真源文件有问题：{snap['file_error']}（已用兜底值）")
    lines.append("")
    lines.append("**可切换**：" + " / ".join(f"`{p}`" for p in PLATFORMS))
    lines.append("")
    lines.append("发 `/platform <名字>` 切换；发 `/platform` 看当前值。")
    lines.append("> 切换会**重启桥**（约 1~2 分钟不可用），并**终止正在跑的任务**。")
    lines.append("> 起不来会自动回滚到当前平台并将原因回给你。")
    return "\n".join(lines)