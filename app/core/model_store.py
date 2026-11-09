"""模型的唯一真源：`data/model.json`。

**为什么要有它**（2026-11-09）：
    桥原来把模型钉在 `.env` 的 `CC_MODEL` 上，而 `.env` 只在进程启动时读一次
    ⇒ 换模型必须重启，且"改完没重启"**完全静默**（用户只会看到模型没变）。
    现在模型是一份**运行时状态**：桥每条消息读一次，聊天里用 `/model` 改。

**三层保护**（缺一层就退化成"改坏了当场哑"）：
    ① **结构校验**：文件读不出来 / `current` 不像个模型名 ⇒ 记 ERROR，退到
       `last_good`；`last_good` 也不行 ⇒ 退到 `.env` 的 `CC_MODEL`；
    ② **原子写**：写临时文件再 `os.replace`，并发读不会读到半截 JSON
       （同一时刻可能有 2 个会话在跑，每条消息都读这个文件）；
    ③ **设置时探测**：见 `bridge_core` 的 `/model` —— 探测通过才写，
       所以"写进真源的值"本身就经过一次真调用验证。

⚠️ **`last_good` 只守护"文件被手改编坏"这一种情况**。文件整个坏掉时
   同一份文件里的 `last_good` 也读不到 ⇒ 那时唯一的退路是 `.env` 的
   `CC_MODEL`（另一个文件，所以不会跟着一起坏）。这就是为什么 seed 必须留。
"""

import json
import os
import re
import threading
import time

from common import log

# 模型名里能出现的东西：字母数字、`.-_/:@[]+`，以及中文（有些网关用中文名）。
# 明确禁掉空白和控制字符 —— 它们会毁掉命令行 / 日志，而且没有任何模型名需要它们。
_CTRL = re.compile(r"[\x00-\x1f\x7f]")

MAX_NAME_LEN = 200


def valid_model_name(name):
    """返回 `(ok, reason)`。**只在名字结构上判断**，不判断端点认不认（那要靠探测）。"""
    if not isinstance(name, str):
        return False, "不是字符串"
    if name != name.strip():
        return False, "首尾有空白"
    if not name:
        return False, "是空的"
    if len(name) > MAX_NAME_LEN:
        return False, f"太长（>{MAX_NAME_LEN}）"
    if _CTRL.search(name):
        return False, "含控制字符"
    if any(c.isspace() for c in name):
        return False, "含空白字符"
    return True, ""


class ModelStore:
    """一份 JSON 文件 + 一层兜底。线程安全（写加锁，读不加 —— 原子写保证读到的要么旧要么新）。"""

    def __init__(self, path, seed="", candidates=None, log_fn=None):
        self.path = path
        self.seed = (seed or "").strip()
        self._log = log_fn or log
        self._lock = threading.Lock()
        self.candidates = []
        for c in (candidates or []):
            c = (c or "").strip()
            if c and c not in self.candidates:
                self.candidates.append(c)
        # 同一个错误别每条消息刷一次 —— 60 秒一次足够"报警"，又不至于淹掉日志。
        self._last_warn = 0.0
        self._last_warn_key = ""

        self._seed_file_if_missing()

    # ---------------- 读 ----------------

    def _read_file(self):
        """返回 `(dict, "")` 或 `(None, 原因)`。"""
        try:
            with open(self.path, encoding="utf-8") as f:
                d = json.load(f)
        except FileNotFoundError:
            return None, "文件不存在"
        except Exception as e:                      # noqa: BLE001  （半截 JSON / 权限 / 编码）
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
        """文件不可用时的退路。返回可用的模型名（可能是空串 = 不传 `--model`）。"""
        value = (value or "").strip()
        ok, bad = valid_model_name(value)
        if not ok:
            self._warn("no-fallback",
                       "[model] %s；.env 的 CC_MODEL 也不可用（%s）"
                       " ⇒ 本轮不传 --model，由 claude 用它自己的默认模型", why, bad)
            return ""
        self._warn("recover:" + why,
                   "[model] %s ⇒ **回退到 %r**（并已把 model.json 修回这个值）",
                   why, value)
        self._write(value, value, updated_by="auto-fallback")
        return value

    def current(self):
        """这一条消息该用的模型。**每条消息都调一次**（这就是"热切换"的全部秘密）。"""
        d, why = self._read_file()
        if d is None:
            return self._recover(self.seed, f"model.json {why}")
        ok, bad = valid_model_name(d.get("current"))
        if ok:
            return d["current"]
        ok2, _ = valid_model_name(d.get("last_good"))
        if ok2:
            return self._recover(d["last_good"],
                                 f"model.json 的 current 不可用（{bad}）")
        return self._recover(self.seed,
                             f"model.json 的 current/last_good 都不可用（{bad}）")

    def snapshot(self):
        """给 `/model` 和 `/status` 用的一眼全貌。"""
        d, why = self._read_file()
        return {
            "current": self.current(),      # 走完整校验 + 兜底，和真正跑的一致
            "seed": self.seed,
            "path": self.path,
            "file_ok": d is not None,
            "file_error": why,
            "updated_at": (d or {}).get("updated_at", ""),
            "updated_by": (d or {}).get("updated_by", ""),
            "candidates": list(self.candidates),
        }

    # ---------------- 写 ----------------

    def _write(self, current, last_good, updated_by=""):
        """原子写：临时文件 + replace。失败只记日志，**不抛** ——
        写不进去不该让这一轮任务起不来（内存里的值仍然可用）。"""
        d = {
            "current": current,
            "last_good": last_good,
            "updated_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "updated_by": updated_by or "",
        }
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
                return True
            except Exception as e:                   # noqa: BLE001
                self._log.error("[model] 写 %s 失败：%s（本轮仍用 %r）",
                                self.path, e, current)
                try:
                    if os.path.exists(tmp):
                        os.remove(tmp)
                except OSError:
                    pass
                return False

    def _seed_file_if_missing(self):
        """首次启动：把 `.env` 的 CC_MODEL 落成 model.json。

        **只在文件不存在时做**，不覆盖已有内容 —— 否则每次重启都会把用户
        用 `/model` 选的值冲掉（那就是"重启回到旧模型"的复刻）。
        """
        if os.path.exists(self.path):
            return
        ok, _ = valid_model_name(self.seed)
        if not ok:
            self._warn("no-seed",
                       "[model] model.json 不存在，.env 里也没有可用的 CC_MODEL"
                       " ⇒ 暂不传 --model")
            return
        if self._write(self.seed, self.seed, updated_by="seed(.env)"):
            self._log.info("[model] 初始化 %s ← .env 的 CC_MODEL (%r)",
                           self.path, self.seed)

    def set(self, name, actor=""):
        """写入一个新模型。**调用方必须先探测通过** —— 这里只做结构校验。"""
        ok, why = valid_model_name(name)
        if not ok:
            return False, f"模型名不合法：{why}"
        name = name.strip()
        prev = self.current()
        if not self._write(name, name, updated_by=actor or "?"):
            return False, "写不进 model.json（见日志）；这次切换**没有生效**"
        self._log.info("[model] 切换 %r → %r（by %s）", prev, name, actor or "?")
        return True, f"已切换到 `{name}`"

    def reset(self, actor=""):
        ok, why = valid_model_name(self.seed)
        if not ok:
            return False, f".env 里没有可用的 CC_MODEL（{why}），没法 reset"
        prev = self.current()
        if not self._write(self.seed, self.seed, updated_by=actor or "?"):
            return False, "写不进 model.json（见日志）；这次 reset **没有生效**"
        self._log.info("[model] reset %r → %r（.env 默认，by %s）",
                       prev, self.seed, actor or "?")
        return True, f"已回到 .env 的默认模型 `{self.seed}`"