"""平台的唯一真源：`data/platform.json`。

**为什么要有它**（2026-11-13）：
    桥原来把平台钉死在 `.env` 的 `PLATFORM` 上，而 `.env` 只在**导入时**读一次
    （`common.py` 顶部那句）。于是"换平台"对聊天里的人是一件**做不到**的事：
    他们能发消息，但改不了配置，更找不到那个常驻进程去重启。
    现在平台是一份**运行时状态** —— 任务计划程序每 3 分钟敲门时、我们自己的重启时，
    读的都是同一个文件。

**和 `model.json` 是同一个套路，但生效时机不同（这点最容易想错）**：
    · 模型是**每条消息**读一次 ⇒ 热切换，下一条消息就生效；
    · 平台是**每次进程启动**读一次 ⇒ 因为平台决定了装配哪个类、连哪条长连接，
      活着的进程改不了它。**切换 = 写这个文件 + 重启进程**（见 `core/platform_switch.py`）。

**三层保护**（照抄 `model_store`，理由相同）：
    ① 结构校验 + `last_good` 兜底 + `.env` 种子兜底（三个都坏才会拒绝启动）；
    ② 原子写（临时文件 + `os.replace`）—— 启动时可能同时有两个进程在读（定时任务敲门）；
    ③ 只认白名单里的三个平台名。写进来一个不存在的平台名，后果不是"报错"，
       而是**桥再也起不来**，所以这一层必须在校验里挡住。
"""

import json
import os
import threading
import time

from common import log

# 认识的平台。**这三个名字是装配开关**：`main()` 按它选类、选凭据、选互斥量。
# 加第四个平台时要改的不止这里 —— `mutex_name()` / `default_approval_dir()` /
# `main()` 的三段凭据检查都要跟着改，少一处就是"能启动但行为不对"。
PLATFORMS = ("wecom", "feishu", "slack")


def valid_platform(name, allowed=PLATFORMS):
    """返回 `(ok, reason)`。只判结构，不判"这个平台能不能真的连上"。"""
    if not isinstance(name, str):
        return False, "不是字符串"
    if name != name.strip():
        return False, "首尾有空白"
    if not name:
        return False, "是空的"
    if name not in allowed:
        return False, f"不认识（只支持 {' / '.join(allowed)}）"
    return True, ""


def still_current(store, seen):
    """我读到的平台**现在还是真源吗**？空/坏值一律放行（不敢用坏文件拦住启动）。

    为什么需要这道复核（2026-11-13 真机事故）：
        任务计划程序每 3 分钟起一个"看它还在不在"的进程。它**在切换之前**就读了真源
        （读到的还是 `feishu`），等它走到"取互斥量"那一步时，被切走的那个进程刚好退出、
        互斥量空出来 ⇒ 它把 **刚刚被切走的平台又拉起来** ⇒ 企微和飞书**同时在线**。
        所以**取到互斥量之后必须再读一次**：谁当值由文件说话，不由"谁先读到"说话。
    """
    now = store.current()
    return (not now) or now == seen


class PlatformStore:
    """一份 JSON 文件 + 两层兜底。线程安全（写加锁，读不加 —— 原子写保证读到整份）。"""

    def __init__(self, path, seed="wecom", allowed=PLATFORMS, log_fn=None):
        self.path = path
        self.seed = (seed or "").strip()
        self.allowed = tuple(allowed)
        self._log = log_fn or log
        self._lock = threading.Lock()
        # 同一个错误别每次启动都刷一遍（定时任务每 3 分钟就会起一个"检查"进程）。
        self._last_warn = 0.0
        self._last_warn_key = ""
        self._seed_file_if_missing()

    # ---------------- 读 ----------------

    def _read_file(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return None, "文件不存在"
        except Exception as e:                      # noqa: BLE001（半截 JSON / 权限 / 编码）
            return None, f"读不出来（{type(e).__name__}: {e}）"
        if not isinstance(d, dict):
            return None, "顶层不是对象"
        return d, ""

    def _warn(self, key, msg, *args):
        now = time.time()
        if key == self._last_warn_key and now - self._last_warn < 60:
            return
        self._last_warn, self._last_warn_key = now, key
        self._log.error(msg, *args)

    def _recover(self, value, why):
        """文件不可用时的退路。返回可用的平台名（**可能是空串** = 连种子都不合法）。"""
        value = (value or "").strip()
        ok, bad = valid_platform(value, self.allowed)
        if not ok:
            self._warn("no-fallback",
                       "[platform] %s；`.env` 的 PLATFORM 也不可用（%s）"
                       " ⇒ **拒绝启动**（不猜你要哪个入口）", why, bad)
            return ""
        self._warn("recover:" + why,
                   "[platform] %s ⇒ **回退到 %r**（并已把 platform.json 修回这个值）",
                   why, value)
        self._write(value, value, updated_by="auto-fallback")
        return value

    def current(self):
        """这次**启动**该装配哪个平台。每次进程启动调一次（不热）。"""
        d, why = self._read_file()
        if d is None:
            return self._recover(self.seed, f"platform.json {why}")
        ok, bad = valid_platform(d.get("current"), self.allowed)
        if ok:
            return d["current"]
        ok2, _ = valid_platform(d.get("last_good"), self.allowed)
        if ok2:
            return self._recover(d["last_good"],
                                 f"platform.json 的 current 不可用（{bad}）")
        return self._recover(self.seed,
                             f"platform.json 的 current/last_good 都不可用（{bad}）")

    def snapshot(self):
        d, why = self._read_file()
        return {
            "current": self.current(),     # 走完整校验 + 兜底，和真正启动用的那个一致
            "seed": self.seed,
            "path": self.path,
            "allowed": list(self.allowed),
            "file_ok": d is not None,
            "file_error": why,
            "updated_at": (d or {}).get("updated_at", ""),
            "updated_by": (d or {}).get("updated_by", ""),
        }

    # ---------------- 写 ----------------

    def _write(self, current, last_good, updated_by=""):
        """原子写。失败只记日志、**不抛** —— 写不进去不该让这次切换假装成功。"""
        d = {
            "current": current,
            "last_good": last_good,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_by": updated_by or "",
        }
        ok, _ = self._write_raw(d)
        return ok

    def _write_raw(self, d):
        with self._lock:
            tmp = self.path + ".tmp"
            try:
                parent = os.path.dirname(self.path)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    json.dump(d, f, ensure_ascii=False, indent=2)
                    f.write("\n")
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
                return True, ""
            except Exception as e:                   # noqa: BLE001
                self._log.error("[platform] 写 %s 失败：%s（内存里仍是 %r）",
                                self.path, e, d.get("current"))
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return False, str(e)

    def _seed_file_if_missing(self):
        """首次启动：把 `.env` 的 PLATFORM 落成 platform.json。

        **只在文件不存在时做**，不覆盖已有内容 —— 否则每次重启都会把
        `/platform` 选的值冲掉（那就是"切完一重启又回去了"）。
        """
        if os.path.exists(self.path):
            return
        ok, _ = valid_platform(self.seed, self.allowed)
        if not ok:
            self._warn("no-seed",
                       "[platform] platform.json 不存在，`.env` 里也没有可用的 PLATFORM"
                       " ⇒ 拒绝启动（不猜你要哪个入口）")
            return
        if self._write(self.seed, self.seed, updated_by="seed(.env)"):
            self._log.info("[platform] 初始化 %s ← `.env` 的 PLATFORM (%r)",
                           self.path, self.seed)

    def set(self, name, actor=""):
        """写入一个新平台。**调用方必须先做预检**（凭据 / 依赖 / 白名单）。

        ⚠️ 写进来之后**当前进程不会变** —— 它是下次启动时读的。真正生效要靠
        `core/platform_switch.py` 拉起新进程。
        """
        ok, why = valid_platform(name, self.allowed)
        if not ok:
            return False, f"平台名不合法：{why}"
        name = name.strip()
        prev = self.current()
        if not self._write(name, name, updated_by=actor or "?"):
            return False, "写不进 platform.json（见日志）；这次切换**没有生效**"
        self._log.info("[platform] 真源切换 %r → %r（by %s）", prev, name, actor or "?")
        return True, f"已写入 `{name}`"

    def reset(self, actor=""):
        ok, why = valid_platform(self.seed, self.allowed)
        if not ok:
            return False, f"`.env` 里没有可用的 PLATFORM（{why}），没法 reset"
        prev = self.current()
        if not self._write(self.seed, self.seed, updated_by=actor or "?"):
            return False, "写不进 platform.json（见日志）；这次 reset **没有生效**"
        self._log.info("[platform] reset %r → %r（`.env` 默认，by %s）",
                       prev, self.seed, actor or "?")
        return True, f"已回到 `.env` 的默认平台 `{self.seed}`"