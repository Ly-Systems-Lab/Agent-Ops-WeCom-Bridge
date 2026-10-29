#!/usr/bin/env python3
"""PreToolUse hook：把「需要审批」的工具调用交给桥，阻塞等用户在企微上点卡片。

为什么是这个形状（`DECISIONS` §25，全部实测）：
  · 本版 CLI 没有 `--permission-prompt-tool`，只剩 hook 一条路；
  · **harness 的 timeout 到点 = 放行（fail-open）**，hook 崩溃/输出垃圾也是放行
    ⇒ 否决权必须握在**这里**：自己算 deadline、到点自己拒绝，
      harness 的 `timeout`（`.claude/settings.json` 里写 300）只许比这里更**长**；
  · `matcher` 必须覆盖所有工具 —— 只拦 Bash 会被 Read/Write 绕过去。

协议（与 `app/bridge.py` 共享，两边都在 `DECISIONS` §25 里有记录）：

    <CC_APPROVAL_DIR>/<id>.req.json   ← 本脚本写：要审批的这次调用
    <CC_APPROVAL_DIR>/<id>.seen       ← 桥写：我看到了，卡片已经发出去了
    <CC_APPROVAL_DIR>/<id>.decision   ← 桥写：allow / deny

不设 `CC_APPROVAL_DIR` 时本脚本**立刻放行、什么也不做** —— 这样同一个
`.claude/settings.json` 可以随仓库一起发布，别人不启用审批时它是个空壳。

退出约定：`exit 0` = 放行；输出 JSON `permissionDecision=deny` = 拒绝
（实测 `exit 2` 也能拒，但理由会被裹上 "hook error"，JSON 更干净）。
"""

import json
import os
import sys
import time
import uuid

# ⚠️ 必须钉 UTF-8：本机控制台是 GBK，不钉的话中文拒绝理由会以**乱码**交给模型
#    （实测踩过；与 P4 那边"抓串口脚本必须钉 UTF-8"是同一课）
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 只读工具直接放行：一条长任务可能读十几次文件，全问会把 30 条/分钟打满，
# 而且每次工具调用都要起一个新进程、等一次人工点击，完全不可用。
DEFAULT_ALLOW = "Read,Grep,Glob,TodoWrite"
DEADLINE_SEC = 120          # hook 自己等多久（harness 那边给 300，必须比这个大）
SEEN_WAIT_SEC = 10          # 等桥"我看到了"的上限：桥没在跑就别干等两分钟


def allow():
    sys.exit(0)


def deny(reason):
    """按实测推荐的方式拒绝：JSON，理由**原样**交给模型。"""
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}, ensure_ascii=False))
    sys.exit(0)


def _write_atomic(path, text):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    os.replace(tmp, path)


def main():
    # 不用审批模式 ⇒ 这个 hook 是个空壳（仓库里带着它也不会影响别人）
    adir = os.environ.get("CC_APPROVAL_DIR", "").strip()
    if not adir:
        allow()

    # ⚠️ **最外层兜底：任何没预料到的异常一律拒绝**（2026-10-29 复核 #2③）。
    # 之前只有中间那段 try，读输入 / 解析 JSON / 建目录这些**更早**的步骤漏在外面：
    # 读失败或 JSON 坏了会直接 `allow()`，建目录失败会抛出去 ⇒ harness 那边
    # 同样是放行（它把异常、超时、非 JSON 输出统统当放行）。
    # 两条路都通向"审批形同虚设"，所以这里必须反过来：异常 ⇒ deny。
    try:
        _run(adir)
    except SystemExit:
        raise
    except BaseException as e:
        deny(f"审批 hook 内部错误（{type(e).__name__}: {e}），保守拒绝。")


def _run(adir):
    try:
        raw = sys.stdin.read()
    except Exception as e:
        deny(f"审批 hook 读不到本次事件（{e}），保守拒绝。")
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        deny(f"审批 hook 收到的事件不是合法 JSON（{e}），保守拒绝。")

    name = ev.get("tool_name") or ""
    allow_list = [x.strip() for x in
                  os.environ.get("CC_APPROVAL_ALLOW", DEFAULT_ALLOW).split(",") if x.strip()]
    if name in allow_list:
        allow()

    try:
        deadline = float(os.environ.get("CC_APPROVAL_WAIT_SEC", DEADLINE_SEC))
    except Exception:
        deadline = DEADLINE_SEC
    deadline = max(5.0, min(deadline, 280.0))     # 别超过 harness 的 300

    req = seen = dec = None       # 下面 finally 要用；建目录失败时它们是 None
    os.makedirs(adir, exist_ok=True)
    rid = uuid.uuid4().hex[:12]
    req = os.path.join(adir, rid + ".req.json")
    seen = os.path.join(adir, rid + ".seen")
    dec = os.path.join(adir, rid + ".decision")

    # 目标（发给哪个会话）由桥在起 claude 时注入；没有就发不出去，直接拒绝
    target = os.environ.get("CC_APPROVAL_TARGET", "").strip()
    body = {
        "id": rid, "tool_name": name, "tool_input": ev.get("tool_input") or {},
        "session_id": ev.get("session_id"), "cwd": ev.get("cwd"),
        "target": target, "ts": time.time(),
    }
    try:
        _write_atomic(req, json.dumps(body, ensure_ascii=False))
    except Exception as e:
        # 写不出请求 ⇒ 桥永远不会知道，别干等
        deny(f"审批请求写不出来（{e}），保守拒绝。")

    t0 = time.time()
    try:
        # ① 等桥"我看到了"（卡片真的发出去了）
        try:
            seen_sec = float(os.environ.get("CC_APPROVAL_SEEN_SEC", SEEN_WAIT_SEC))
        except Exception:
            seen_sec = SEEN_WAIT_SEC
        while time.time() - t0 < max(0.5, seen_sec):
            if os.path.exists(seen):
                break
            if os.path.exists(dec):          # 极端情况：桥直接给了决定
                break
            time.sleep(0.1)
        else:
            deny(f"{max(0.5, seen_sec):.1f} 秒内桥没有响应（它可能没在跑），"
                 f"已按未审批拒绝。这条请求是：{name}")

        # ② 等用户在企微上点卡片
        while time.time() - t0 < deadline:
            if os.path.exists(dec):
                break
            time.sleep(0.2)
        else:
            deny(f"{int(deadline)} 秒内没有点击，已按超时拒绝（这是刻意的默认："
                 f"没人应答就不执行）。要做的话，重新发一次让人守着。")

        try:
            with open(dec, encoding="utf-8") as f:
                verdict = f.read().strip().lower()
        except Exception:
            verdict = ""
        if verdict.startswith("allow"):
            allow()
        deny("用户点了「拒绝」，这条工具调用没有执行。")
    except SystemExit:
        raise
    except Exception as e:
        # **任何意外都要拒绝**：harness 那一层的所有异常路径都是放行，
        # 我们这一层必须反过来，否则"审批"就只是装饰。
        deny(f"审批通道出错（{type(e).__name__}: {e}），保守拒绝。")
    finally:
        for p in (req, seen, dec):
            if not p:
                continue
            try:
                os.remove(p)
            except OSError:
                pass


if __name__ == "__main__":
    main()