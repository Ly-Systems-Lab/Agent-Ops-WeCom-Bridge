#!/usr/bin/env python3
"""`tools/gate_cli.py`：通用闸门入口（harness 无关）。

要证明的只有四条：
    ① 方言对：放行=退出码 0 且**不输出**；拒绝=退出码 2 + 一行 JSON；
    ② 判定**没有**被入口改写：凭据硬 deny、弹卡等待、桥没响应时拒绝，全都与
       `approval_hook.py` 直连时一致；
    ③ 工具名映射真的生效（`read_image` → `Read`），且**只有**映射了才生效；
    ④ 入口自己出的错（坏 JSON / 读不到）一律拒绝，不是放行。

用法：`python -X utf8 tools/test_gate_cli.py`
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GATE = os.path.join(ROOT, "tools", "gate_cli.py")
PY = sys.executable

OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))


def base_env(**extra):
    """从当前环境出发，**剥掉所有 CC_APPROVAL_***，再按用例补。"""
    env = {k: v for k, v in os.environ.items() if not k.startswith("CC_APPROVAL")}
    env["PYTHONIOENCODING"] = "utf-8"
    for k, v in extra.items():
        env[k] = str(v)
    return env


def run_gate(payload, env, bridge=None, timeout=25):
    """跑一次闸门。`bridge` 是一个线程函数（模拟桥把卡片发出去并回填决定）。"""
    proc = subprocess.Popen([PY, "-X", "utf8", GATE],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True,
                            encoding="utf-8", env=env)
    box = {}

    def play():
        try:
            box["req"] = bridge()
        except Exception as e:                      # noqa: BLE001
            box["err"] = e

    th = None
    if bridge is not None:
        th = threading.Thread(target=play, daemon=True)
        th.start()
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    out, err = proc.communicate(raw, timeout=timeout)
    if th is not None:
        th.join(timeout=5)
    return proc.returncode, (out or "").strip(), (err or "").strip(), box


def play_bridge(verdict, adir, wait=10.0):
    """模拟桥：看到 `.req.json` → 写 `.seen` → 写 `.decision`。返回请求体。"""
    def go():
        end = time.time() + wait
        req = None
        while time.time() < end:
            try:
                names = [n for n in os.listdir(adir) if n.endswith(".req.json")]
            except OSError:
                names = []
            if names:
                req = os.path.join(adir, names[0])
                break
            time.sleep(0.05)
        if not req:
            return None
        rid = req[: -len(".req.json")]
        with open(rid + ".seen", "w"):
            pass
        with open(rid + ".decision", "w") as f:
            f.write(verdict)
        with open(req, encoding="utf-8") as f:
            return json.load(f)
    return go


def test_static_filter():
    print("\n== 没开审批（CC_APPROVAL_DIR 为空）：只剩静态安全过滤 ==")
    env = base_env()

    rc, out, err, _ = run_gate({"tool_name": "Bash",
                                "tool_input": {"command": "git status"}}, env)
    check("只读 Bash ⇒ 放行（rc=0）", rc == 0, f"rc={rc} {err[:80]}")
    check("放行时不输出任何东西", out == "", out[:80])

    rc, out, err, _ = run_gate({"tool_name": "Read",
                                "tool_input": {"file_path": "~/.ssh/id_rsa"}}, env)
    check("读凭据 ⇒ 拒绝（rc=2）", rc == 2, f"rc={rc}")
    body = json.loads(out) if out else {}
    check("拒绝是一行 JSON，带 permissionDecision=deny",
          body.get("permissionDecision") == "deny", out[:120])
    check("理由原样是那句硬 deny", "凭据存储" in (body.get("reason") or ""),
          str(body.get("reason")))

    # 凭据硬 deny 与**工具名无关**：任何工具的参数里出现凭据路径都要拦
    # （这是刻意的：名字可以随便改，路径不会）
    rc, out, err, _ = run_gate({"tool_name": "read_thing_unknown",
                                "tool_input": {"file_path": "~/.ssh/id_rsa"}}, env)
    check("凭据硬 deny 不挑工具名（未知工具也拦）", rc == 2, f"rc={rc}")

    rc, out, err, _ = run_gate({"tool_name": "read_thing_unknown",
                                "tool_input": {"file_path": "/tmp/whatever.txt"}}, env)
    check("没开审批时未知工具只剩静态过滤 ⇒ 放行（不冒充安全）", rc == 0,
          f"rc={rc}")

    rc, out, err, _ = run_gate({"tool_name": "Bash",
                                "tool_input": "{\"command\": \"git status\"}"}, env)
    check("tool_input 是 JSON 字符串也认", rc == 0, f"rc={rc}")

    rc, out, err, _ = run_gate("这不是 JSON", env)
    check("坏入参 ⇒ 拒绝（rc=2）", rc == 2, f"rc={rc}")
    check("坏入参的理由说得清", "不是合法 JSON" in out, out[:100])


def test_card():
    print("\n== 开了审批：发卡、等决定、桥不在就拒 ==")
    d = tempfile.mkdtemp(prefix="gate-adir-")
    env = base_env(CC_APPROVAL_DIR=d, CC_APPROVAL_TARGET="ou_test",
                   CC_APPROVAL_SEEN_SEC=8, CC_APPROVAL_WAIT_SEC=8)

    rc, out, err, box = run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "x.txt", "content": "a"}},
        env, bridge=play_bridge("allow", d))
    req = box.get("req") or {}
    check("用户点允许 ⇒ 放行（rc=0）", rc == 0, f"rc={rc} {out[:80]}")
    check("请求文件里的工具名原样是 Write", req.get("tool_name") == "Write",
          str(req.get("tool_name")))
    check("请求里带上了审批目标", req.get("target") == "ou_test",
          str(req.get("target")))

    # 工具名映射的证据在这里：`read_image` 若**没**被映射成 `Read`，
    # 就会落到"要弹卡"这一支（名字不在只读白名单里）⇒ 会看到请求文件。
    rc, out, err, box = run_gate(
        {"tool_name": "read_image",
         "tool_input": {"file_path": os.path.join(ROOT, "README.md")}},
        env, bridge=play_bridge("allow", d, wait=1.5))
    check("read_image 映射成 Read ⇒ 直放（rc=0）", rc == 0, f"rc={rc} {out[:80]}")
    check("…而且没有产生请求文件（映射真的生效）",
          box.get("req") is None, str(box.get("req"))[:80])

    rc, out, err, _ = run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "y.txt", "content": "a"}},
        env, bridge=play_bridge("deny", d))
    body = json.loads(out) if out else {}
    check("用户点拒绝 ⇒ 拒绝（rc=2）", rc == 2, f"rc={rc}")
    check("理由写明是用户拒绝", "拒绝" in (body.get("reason") or ""),
          str(body.get("reason")))

    # 桥没在跑：不能干等，也不能放行
    env2 = base_env(CC_APPROVAL_DIR=d, CC_APPROVAL_TARGET="ou_test",
                    CC_APPROVAL_SEEN_SEC=0.6, CC_APPROVAL_WAIT_SEC=5)
    t0 = time.time()
    rc, out, err, _ = run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "z.txt", "content": "a"}},
        env2)
    cost = time.time() - t0
    body = json.loads(out) if out else {}
    check("桥没响应 ⇒ 拒绝（rc=2）", rc == 2, f"rc={rc}")
    check("桥没响应时不干等（<6s）", cost < 6, f"{cost:.1f}s")
    check("理由写明桥没响应", "没有响应" in (body.get("reason") or ""),
          str(body.get("reason")))


def test_auto():
    print("\n== 免卡白名单（按人）：照过凭据那一关，只是不弹卡 ==")
    d = tempfile.mkdtemp(prefix="gate-auto-")
    env = base_env(CC_APPROVAL_DIR=d, CC_APPROVAL_TARGET="ou_test",
                   CC_APPROVAL_AUTO="1")
    rc, out, err, _ = run_gate(
        {"tool_name": "Write", "tool_input": {"file_path": "x.txt", "content": "a"}},
        env)
    check("免卡 ⇒ 放行（rc=0）", rc == 0, f"rc={rc} {out[:80]}")
    left = [n for n in os.listdir(d) if n.endswith(".req.json")]
    check("免卡 ⇒ 不产生请求文件", not left, str(left))
    log = os.path.join(d, "readonly.log")
    has = os.path.exists(log) and "auto" in open(log, encoding="utf-8").read()
    check("免卡 ⇒ 直放记账里有这一条", has)

    rc, out, err, _ = run_gate({"tool_name": "Read",
                                "tool_input": {"file_path": "~/.dws/config.json"}},
                               env)
    check("免卡也拦凭据（硬 deny 排在免卡之前）", rc == 2, f"rc={rc}")


def main():
    test_static_filter()
    test_card()
    test_auto()
    print("\n" + ("✅ 全部通过" if OK else "❌ 有失败项"))
    return 0 if OK else 2


if __name__ == "__main__":
    sys.exit(main())