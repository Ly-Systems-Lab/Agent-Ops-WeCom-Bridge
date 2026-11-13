"""运行时凭据文件：`data/runtime/bridge-<实例>.json`。

它回答一个**只有落盘才能回答**的问题：**现在这个平台是谁在跑？**

三个用途（2026-11-13）：
    · 切换的看守窗口拿它当"新进程真的起来了"的判据 —— 不看进程退出码，
      也不看"看起来启动了"，只看它有没有把这份文件写出来；
    · `tools/bridgectl.py status` / 控制目录的 `status` 命令靠它说话；
    · 人排查时一眼看到"上次启动装的是哪个平台、什么时间、哪个 pid"。

**写在哪一步很关键**：`main()` 里所有会「拒绝启动」的检查跑完之后、`plat_run()`
之前。写在更早（比如刚拿到互斥量）会把"过不了闸门的进程"也算成就绪；
写在更晚（连上之后）会让回滚等上 100 多秒。
"""

import json
import os
import time

from common import log


def write_runtime(path, platform, instance="", extra=None):
    """原子写。**失败只记日志** —— 写不出来不该让桥起不来（它只是观测手段）。"""
    d = {
        "platform": platform,
        "instance": instance,
        "pid": os.getpid(),
        "started_at": time.time(),
        "started_at_h": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    if extra:
        d.update(extra)
    tmp = path + ".tmp"
    try:
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(d, f, ensure_ascii=False, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        return True
    except Exception as e:                          # noqa: BLE001
        log.warning("[runtime] 写 %s 失败：%s", path, e)
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        return False


def read_runtime(path):
    """返回 dict 或 None（不存在 / 半截 / 不是对象都算 None）。**从不抛。**"""
    try:
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:                               # noqa: BLE001
        return None
    return d if isinstance(d, dict) else None