#!/usr/bin/env python3
"""包装器（`tools/gate_wrap.py`）+ PATH shim + hook 的"让路/拒绝"。

要钉住四件事：
    ① **shim 的形状**：裸名（bash 用）+ `.cmd`（cmd.exe 用）两个都要有，
       内容指向 `gate_wrap.py`，且路径是正斜杠（sh 里反斜杠是转义符）；
    ② **包装器自己判读写**：只读直跑、要写就发卡；**没有审批目录时拒绝**（fail-closed），
       而且拒绝时那条真身命令**一次都没被执行**（用哨兵文件证明）；
    ③ **批下来才执行**：桥回 allow 才跑真身；回 deny 不跑；
    ④ **hook 在 shim 模式下的两条路**：解析到 shim ⇒ 让路；全路径绕过 ⇒ 拒绝。

用法：`python -X utf8 tools/test_gate_wrap.py`
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOOLS = os.path.join(ROOT, "tools")
WRAP = os.path.join(TOOLS, "gate_wrap.py")
PY = sys.executable

sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(ROOT, "app"))

import approval_hook as gate                      # noqa: E402
from common import write_gate_shims               # noqa: E402

OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))


# ---------------- ① shim 形状 ----------------

def test_shims():
    print("\n== shim：bash 认裸名、cmd 认 .cmd ==")
    d = tempfile.mkdtemp(prefix="shim-")
    made = write_gate_shims(d, names=("dws", "lark-cli"))
    check("四个文件都生成了（2 个名字 × 2 种形态）", len(made) == 4, str(len(made)))
    sh = os.path.join(d, "dws")
    with open(sh, encoding="utf-8") as f:
        body = f.read()
    check("裸名 shim 是 sh 脚本", body.startswith("#!/bin/sh"), body.splitlines()[0][:40])
    check("shim 调的是 gate_wrap.py", "gate_wrap.py" in body)
    check("shim 里没有反斜杠路径（sh 会把它当转义）",
          "\\" not in body.split("exec ", 1)[1], body.splitlines()[1][:70])
    check("shim 自己带上 --shim-dir（否则会解析回自己 ⇒ 无限递归）",
          "--shim-dir" in body, body.splitlines()[2][:80])
    with open(os.path.join(d, "dws.cmd"), encoding="utf-8") as f:
        cmd = f.read()
    check(".cmd shim 会转发全部参数（%*）", "%*" in cmd)
    check(".cmd shim 会透传退出码（errorlevel）", "%errorlevel%" in cmd)
    check(".cmd shim 也带 --shim-dir", "--shim-dir" in cmd)

    # pythonw 没有控制台 ⇒ 包出来的命令 stdout/stderr 全丢（2026-11-15 真机踩到）
    d2 = tempfile.mkdtemp(prefix="shim-py-")
    py = os.path.join(d2, "python.exe")
    pw = os.path.join(d2, "pythonw.exe")
    for p in (py, pw):
        open(p, "w").close()
    made2 = write_gate_shims(os.path.join(d2, "bin"), python_exe=pw, names=("dws",))
    with open(made2[0], encoding="utf-8") as f:
        body2 = f.read()
    check("pythonw 自动换成 python.exe（否则输出全丢）",
          "pythonw" not in body2 and "python.exe" in body2, body2.splitlines()[2][:70])


# ---------------- ②③ 包装器本体 ----------------

def _fake_real(dirpath, sentinel, rc=7):
    """造一个"真身"：把收到的参数写进哨兵文件，然后按指定码退出。"""
    p = os.path.join(dirpath, "dws.cmd")
    with open(p, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("@echo off\r\n")
        f.write('echo %%*>>"%s"\r\n' % sentinel)     # `%%*` → cmd 的 `%*`
        f.write("exit /b %d\r\n" % rc)
    return p


def _env(**extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("CC_APPROVAL")}
    e.pop("GATE_REAL_BIN", None)
    e["PYTHONIOENCODING"] = "utf-8"
    for k, v in extra.items():
        e[k] = str(v)
    return e


def run_wrap(args, env, bridge=None, timeout=25):
    proc = subprocess.Popen([PY, "-X", "utf8", WRAP] + args,
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=env)
    box = {}

    def play():
        try:
            box["req"] = bridge()
        except Exception as e:                       # noqa: BLE001
            box["err"] = e

    th = threading.Thread(target=play, daemon=True) if bridge else None
    if th:
        th.start()
    out, err = proc.communicate(timeout=timeout)
    if th:
        th.join(timeout=5)
    return proc.returncode, (out or "").strip(), (err or "").strip(), box


def play_bridge(adir, verdict, wait=8.0):
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


def test_wrapper():
    print("\n== 包装器：判读写 / 发卡 / 拒绝时**不执行** ==")
    d = tempfile.mkdtemp(prefix="wrap-")
    sentinel = os.path.join(d, "ran.txt")
    real = _fake_real(d, sentinel)

    def ran():
        if not os.path.exists(sentinel):
            return ""
        with open(sentinel, encoding="utf-8", errors="replace") as f:
            return f.read()

    # 只读 + 没有任何审批目录 ⇒ 直接跑（它不需要授权）
    env = _env(GATE_REAL_BIN=real)
    rc, out, err, _ = run_wrap(["--", "dws", "--help"], env)
    check("只读命令 ⇒ 执真身并透传退出码", rc == 7, f"rc={rc} {err[:60]}")
    check("…真的执行了（哨兵有记录）", "--help" in ran(), ran()[:40])

    # 要写 + 没有审批目录 ⇒ fail-closed，而且**不许执行**
    os.path.exists(sentinel) and os.remove(sentinel)
    rc, out, err, _ = run_wrap(["--", "dws", "todo", "+create", "--title", "x"], env)
    check("要写但没审批目录 ⇒ 退出码 126", rc == 126, f"rc={rc}")
    check("…理由说清是缺审批目录", "CC_APPROVAL_DIR" in err, err[:70])
    check("…真身一次都没被执行", ran() == "", ran()[:40])

    # 要写 + 有目录 + 用户点允许 ⇒ 执行
    d2 = tempfile.mkdtemp(prefix="wrap-adir-")
    env2 = _env(GATE_REAL_BIN=real, CC_APPROVAL_DIR=d2, CC_APPROVAL_TARGET="u|1|u",
                CC_APPROVAL_SEEN_SEC=8, CC_APPROVAL_WAIT_SEC=8)
    # ⚠️ 这里用 **ASCII + 空格**，不用中文：cmd.exe 的 `echo` 按控制台代码页写文件，
    #    用 UTF-8 读回来必然是乱码 —— 那是测试夹具的问题，不是转发的证据。
    #    中文会不会被拆散由上面那条卡面断言覆盖（它走 JSON，没有 shell 参与）。
    rc, out, err, box = run_wrap(["--", "dws", "todo", "+create", "--title", "买牛奶",
                                  "--desc", "hello world"],
                                 env2, bridge=play_bridge(d2, "allow"))
    req = box.get("req") or {}
    check("用户点允许 ⇒ 执行真身", rc == 7, f"rc={rc} {err[:60]}")
    check("…卡面上的命令带完整参数",
          "买牛奶" in json.dumps(req.get("tool_input"), ensure_ascii=False),
          json.dumps(req.get("tool_input"), ensure_ascii=False)[:70])
    check("…带了策略理由（卡面那行）", bool(req.get("policy")), str(req.get("policy"))[:50])
    check("…带空格的参数到真身时没有被拆散", "hello world" in ran(), ran()[:60])

    # 要写 + 用户拒绝 ⇒ 不执行
    os.path.exists(sentinel) and os.remove(sentinel)
    rc, out, err, _ = run_wrap(["--", "dws", "todo", "+create"], env2,
                               bridge=play_bridge(d2, "deny"))
    check("用户点拒绝 ⇒ 退出码 126", rc == 126, f"rc={rc}")
    check("…真身没被执行", ran() == "", ran()[:40])

    # 免卡的人 ⇒ 不弹卡、直接执行（与 hook 同一语义）
    d3 = tempfile.mkdtemp(prefix="wrap-auto-")
    env3 = _env(GATE_REAL_BIN=real, CC_APPROVAL_DIR=d3, CC_APPROVAL_AUTO="1")
    rc, out, err, box = run_wrap(["--", "dws", "todo", "+create"], env3,
                                 bridge=play_bridge(d3, "allow", wait=1.0))
    check("免卡 ⇒ 直接执行", rc == 7, f"rc={rc}")
    check("免卡 ⇒ 不产生请求文件", box.get("req") is None, str(box.get("req"))[:60])

    # 不认识的名字 ⇒ 用法错误，不执行
    rc, out, err, _ = run_wrap(["--", "rm", "-rf", "x"], env)
    check("不认识的程序名 ⇒ 用法错误（2）", rc == 2, f"rc={rc}")

    # 递归保险丝：PATH 把 shim 又解析回自己时，必须在第 3 层就停（实测踩过）
    rc, out, err, _ = run_wrap(["--", "dws", "--help"],
                               _env(GATE_REAL_BIN=real, GATE_WRAP_DEPTH="3"))
    check("嵌套过深 ⇒ 127 而不是继续套娃", rc == 127, f"rc={rc}")
    check("…理由点明疑似 PATH 解析回自己", "嵌套" in err, err[:60])


# ---------------- ④ hook 的让路/拒绝 ----------------

def test_hook_shim_mode():
    print("\n== hook：解析到 shim 就让路，绕过就拒绝 ==")
    shim = tempfile.mkdtemp(prefix="hook-shim-")
    other = tempfile.mkdtemp(prefix="hook-other-")
    # 两边都放假文件，**只靠 PATH 顺序**让 which 命中 shim
    for d in (shim, other):
        with open(os.path.join(d, "dws.cmd"), "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")

    old_path, old_shim = os.environ.get("PATH", ""), os.environ.get("CC_GATE_SHIM_DIR")
    try:
        os.environ["CC_GATE_SHIM_DIR"] = shim
        os.environ["PATH"] = shim + os.pathsep + other + os.pathsep + old_path

        ok, note = gate.classify_bash("dws todo +create --title x")
        check("解析到 shim ⇒ 让路（hook 不重复弹卡）", ok is True, note)

        ok, note = gate.classify_bash("dws todo +list | head -3")
        check("接在管道上也让路（读写由包装器判）", ok is True, note)

        bypass = os.path.join(other, "dws.cmd")
        ok, note = gate.classify_bash('"%s" todo +create' % bypass)
        check("全路径绕过包装器 ⇒ 拒绝", ok is False, note)
        check("…理由里点明没经过包装器", "没经过闸门包装器" in note, note)
        check("…并且说出了实际解析到哪", other in note, note)

        # 关掉 shim 模式 ⇒ 回到 hook 自己判读写
        del os.environ["CC_GATE_SHIM_DIR"]
        ok, note = gate.classify_bash("dws todo +create --title x")
        check("非 shim 模式：写命令仍然弹卡", ok is False, note)
        ok, note = gate.classify_bash("dws --help")
        check("非 shim 模式：帮助仍然直放", ok is True, note)
    finally:
        os.environ["PATH"] = old_path
        if old_shim is None:
            os.environ.pop("CC_GATE_SHIM_DIR", None)
        else:
            os.environ["CC_GATE_SHIM_DIR"] = old_shim


def main():
    test_shims()
    test_wrapper()
    test_hook_shim_mode()
    print("\n" + ("✅ 全部通过" if OK else "❌ 有失败项"))
    return 0 if OK else 2


if __name__ == "__main__":
    sys.exit(main())