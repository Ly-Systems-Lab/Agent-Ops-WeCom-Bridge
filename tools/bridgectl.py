#!/usr/bin/env python3
"""`bridgectl` —— 用**文件**跟跑着的桥说话（不联网、不开端口、不 import 桥）。

    python -X utf8 tools/bridgectl.py status
    python -X utf8 tools/bridgectl.py platforms
    python -X utf8 tools/bridgectl.py switch feishu

**它是控制目录协议（`app/core/control.py`）的一个客户端**，不是另一套机制：
往 `data/control/inbox/` 投一个 JSON，等桥往 `done/` 写回执。
任何 Agent / 脚本 / 人学会这一个协议，换哪个 harness 都能用 —— 这就是选它
而不是"写进 CLAUDE.md 让模型去跑脚本"的理由。

⚠️ 桥没在跑时**拒绝投递**：`inbox` 里的文件会在桥下次启动时被执行，
   那时候用户早就以为"没成功"了 —— 这种延迟生效比直接报错更糟。
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "app"))

from core.control import control_dir, submit            # noqa: E402
from core.platform_switch import pid_alive, runtime_path  # noqa: E402
from core.runtime_file import read_runtime              # noqa: E402


def _instance():
    """和桥用同一套口径（`BRIDGE_INSTANCE`，先环境变量、再 `.env`）。"""
    inst = (os.environ.get("BRIDGE_INSTANCE") or "").strip()
    if inst:
        return inst
    try:
        with open(os.path.join(ROOT, ".env"), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("BRIDGE_INSTANCE="):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
    except OSError:
        pass
    return ""


def _live():
    """桥活着吗？`(bool, runtime_dict_or_None)`。判据是**pid 还活着**，不是"文件在"。"""
    rt = read_runtime(runtime_path(ROOT, _instance()))
    if not rt:
        return False, None
    return pid_alive(rt.get("pid")), rt


def _local_status():
    """桥不应答时的只读视图 —— **只读**，不建文件（诊断命令不该改现场）。"""
    p = os.path.join(ROOT, "data", "platform.json")
    try:
        with open(p, encoding="utf-8") as f:
            snap = json.load(f)
    except Exception:                          # noqa: BLE001（不存在 / 坏了都算"没有"）
        snap = {}
    rt = read_runtime(runtime_path(ROOT, _instance()))
    lines = [f"platform.json.current = {snap.get('current') or '（还没有这个文件）'}",
             f"platform.json.updated = {snap.get('updated_at') or '—'}"
             f"{(' by ' + snap['updated_by']) if snap.get('updated_by') else ''}"]
    if rt:
        lines += [f"runtime.platform      = {rt.get('platform')}",
                  f"runtime.pid           = {rt.get('pid')}（还活着吗：{pid_alive(rt.get('pid'))}）",
                  f"runtime.started_at    = {rt.get('started_at_h') or ''}"]
    else:
        lines.append("runtime               = （没有就绪凭据）")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="用文件跟本机的桥说话")
    ap.add_argument("cmd", nargs="?", default="status",
                    choices=["status", "platforms", "switch"],
                    help="status / platforms / switch")
    ap.add_argument("to", nargs="?", default="",
                    help="switch 的目标平台（wecom / feishu / slack）")
    ap.add_argument("--wait", type=float, default=0,
                    help="等回执的秒数（默认 status 5 秒、switch 10 秒）")
    ap.add_argument("--by", default="", help="谁发起的（写进日志与回执）")
    ap.add_argument("--json", action="store_true", help="机器可读输出")
    args = ap.parse_args()

    alive, rt = _live()
    if not alive:
        msg = ("桥没有应答：`data/runtime/bridge-*.json` 里没有存活的 pid。\n"
               "两种可能：① 它没在跑；② 它跑的是**旧版本**（新版本才会写这份就绪凭据）。\n"
               "  ②的话重启一次就好：`schtasks /Run /TN wecom-cc-bridge`"
               "（在跑的话互斥量会挡住，安全）。\n\n"
               "现在磁盘上是什么：\n" + _local_status())
        print(msg)
        return 2

    if args.cmd == "status":
        wait = args.wait or 5
        cid, done = submit(control_dir(ROOT), "status", {}, by=args.by, wait=wait)
        text = (done or {}).get("text") if done else None
        if not text:
            # 桥活着但控制循环没回 —— 如实说，并给出磁盘上的事实。
            print("桥活着，但控制目录没有在等的时间里回执。磁盘上的事实：\n" + _local_status())
            return 3
        print(text)
        return 0

    if args.cmd == "platforms":
        wait = args.wait or 5
        cid, done = submit(control_dir(ROOT), "platforms", {}, by=args.by, wait=wait)
        text = (done or {}).get("text") if done else None
        if not text:
            print("桥没有在规定时间内回执；磁盘上的事实：\n" + _local_status())
            return 3
        print(text)
        return 0

    # switch
    if not args.to:
        print("用法：bridgectl.py switch <wecom|feishu|slack>")
        return 2
    wait = args.wait or 10
    cid, done = submit(control_dir(ROOT), "switch_platform", {"to": args.to},
                       by=args.by or f"cli:{os.environ.get('USERNAME') or 'user'}",
                       wait=wait)
    if done is None:
        print("桥没有在规定时间内回执 —— **结果未知**（不代表没切）。\n"
              "别重复投递；先跑 `bridgectl.py status` 看它现在在哪。")
        return 3
    if args.json:
        print(json.dumps(done, ensure_ascii=False, indent=2))
    else:
        print(done.get("text") or "")
        if done.get("ok"):
            print("\n（桥会在看守通过后自己退出，新进程接管。约 1~2 分钟可用；"
                  "期间再投命令会失败。）")
    return 0 if done.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())