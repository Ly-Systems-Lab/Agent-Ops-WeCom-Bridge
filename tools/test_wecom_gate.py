#!/usr/bin/env python3
"""`wecom-cli` / `wecom-bot` 过闸门：读写分类 + 包装器 + 让路/反绕过。

要钉住四件事：
    ① **默认按写**：不在快照白名单里的方法一律弹卡（官方 schema 没有风险字段，
       所以这条是安全的唯一方向）；
    ② **`wecom-bot` 就是 `wecom-cli`**：同一个官方 CLI、同一张快照，只是换凭据目录；
       判定一致、卡面写它自己的名字；
    ③ **包装器真的把凭据目录带进去了**（`WECOM_CLI_CONFIG_DIR`），而且调用方显式设了就不覆盖；
    ④ **hook 在 shim 模式下让路 / 反绕过**（与 dws / lark-cli 同一套）。

用法：`python -X utf8 tools/test_wecom_gate.py`
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
EFFECTS = os.path.join(ROOT, "interop", "wecom_effects.json")
PY = sys.executable

sys.path.insert(0, TOOLS)
sys.path.insert(0, os.path.join(ROOT, "app"))

import approval_hook as gate                      # noqa: E402

OK = True


def check(name, cond, extra=""):
    global OK
    OK = OK and bool(cond)
    print(("  ✅ " if cond else "  ❌ ") + name + (("  " + extra) if extra else ""))


# ---------------- ① 读写分类 ----------------

def test_classify():
    print("\n== 读写分类：白名单算读，其余按写 ==")
    ok, why = gate._cli_ok("wecom-cli", ["message", "aibot", "sessions", "list"])
    check("机器人会话列表 ⇒ 读", ok is True, why)

    ok, why = gate._cli_ok("wecom-cli", ["message", "aibot", "send",
                                         "--chat-id", "x", "--msg-type", "markdown",
                                         "--markdown", '{"content":"hi"}'])
    check("以机器人身份发消息 ⇒ 写", ok is False, why)
    check("…理由点明是发消息那条", "message.aibot.send" in why, why)

    ok, why = gate._cli_ok("wecom-cli", ["message", "aibot", "send", "--dry-run"])
    check("--dry-run 也照样弹卡（只改卡面文案）", ok is False and "干跑" in why, why)

    ok, why = gate._cli_ok("wecom-cli", ["message", "files", "get",
                                         "--media-id", "x"])
    check("媒体内容会落盘 ⇒ 按写", ok is False and "本地" in why, why)

    ok, why = gate._cli_ok("wecom-cli", ["auth", "show"])
    check("`auth show`（只看授权状态）⇒ 读", ok is True, why)
    ok, why = gate._cli_ok("wecom-cli", ["auth", "init", "--manual"])
    check("`auth init`（写凭据）⇒ 写", ok is False, why)

    ok, why = gate._cli_ok("wecom-cli", ["schema", "list"])
    check("`schema list` ⇒ 读", ok is True, why)
    ok, why = gate._cli_ok("wecom-cli", ["chat", "groups", "list"])
    check("群列表 ⇒ 读", ok is True, why)
    ok, why = gate._cli_ok("wecom-cli", ["todo", "create", "--title", "x"])
    check("建待办 ⇒ 写", ok is False, why)

    ok, why = gate._cli_ok("wecom-cli", ["some", "unknown", "method"])
    check("表外 ⇒ 按写（fail-closed）", ok is False and "表外" in why, why)

    ok, why = gate._cli_ok("wecom-cli", [])
    check("只写了个 CLI 名 ⇒ 按写", ok is False, why)

    ok, why = gate._cli_ok("wecom-cli", ["message", "aibot", "send", "--help"])
    check("--help ⇒ 直放", ok is True, why)

    ok, why = gate._cli_ok("wecom-cli", ["--version"])
    check("--version ⇒ 直放", ok is True, why)

    # 别名：判定必须与 wecom-cli 完全一致
    for args in (["message", "aibot", "send"], ["message", "aibot", "sessions", "list"]):
        a = gate._cli_ok("wecom-cli", args)
        b = gate._cli_ok("wecom-bot", args)
        check("wecom-bot 与 wecom-cli 判定一致（%s）" % " ".join(args), a == b,
              "%s / %s" % (a, b))
    ok, why = gate._cli_ok("wecom-bot.cmd", ["message", "aibot", "send"])
    check(".cmd 后缀也归一（按写）", ok is False, why)

    # 快照本身的自洽性
    with open(EFFECTS, encoding="utf-8") as f:
        snap = json.load(f)
    cmds = snap.get("commands") or {}
    check("快照够全（>150 条）", len(cmds) > 150, str(len(cmds)))
    check("发消息在快照里且是写", cmds.get("message.aibot.send", {}).get("effect") == "write")
    check("会话列表在快照里且是读",
          cmds.get("message.aibot.sessions.list", {}).get("effect") == "read")
    check("快照声明了「默认按写」",
          "白名单" in (snap.get("read_rule") or ""), snap.get("read_rule", "")[:40])


# ---------------- ②③ 包装器 ----------------

def _fake_real(dirpath, sentinel, rc=7):
    """假 `wecom-cli`：把参数与凭据目录写进哨兵，然后按指定码退出。"""
    p = os.path.join(dirpath, "wecom-cli.cmd")
    with open(p, "w", encoding="utf-8", newline="\r\n") as f:
        f.write("@echo off\r\n")
        f.write('echo ARGS %%*>>"%s"\r\n' % sentinel)
        f.write('echo CFG %%WECOM_CLI_CONFIG_DIR%%>>"%s"\r\n' % sentinel)
        f.write("exit /b %d\r\n" % rc)
    return p


def _env(**extra):
    e = {k: v for k, v in os.environ.items() if not k.startswith("CC_APPROVAL")}
    e.pop("GATE_REAL_BIN", None)
    e.pop("WECOM_CLI_CONFIG_DIR", None)
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
        except Exception as e:                     # noqa: BLE001
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
    print("\n== 包装器：身份选择 / 判定 / 不执行 ==")
    d = tempfile.mkdtemp(prefix="wecom-wrap-")
    sentinel = os.path.join(d, "ran.txt")
    real = _fake_real(d, sentinel)

    def ran():
        if not os.path.exists(sentinel):
            return ""
        with open(sentinel, encoding="utf-8", errors="replace") as f:
            return f.read()

    env = _env(GATE_REAL_BIN=real)
    rc, out, err, _ = run_wrap(["--", "wecom-bot", "message", "aibot",
                                "sessions", "list"], env)
    check("只读 ⇒ 执行真身并透传退出码", rc == 7, "rc=%s %s" % (rc, err[:60]))
    body = ran()
    check("…参数原样转发", "message aibot sessions list" in body, body[:80])
    check("…自动带上 CC 的凭据目录",
          os.path.join(ROOT, "data", "wecom-cli-cc") in body, body[:160])

    # 调用方显式设了凭据目录 ⇒ 不覆盖
    os.path.exists(sentinel) and os.remove(sentinel)
    mine = os.path.join(d, "my-creds")
    rc, out, err, _ = run_wrap(["--", "wecom-bot", "message", "aibot",
                                "sessions", "list"], _env(GATE_REAL_BIN=real,
                                                          WECOM_CLI_CONFIG_DIR=mine))
    check("显式 WECOM_CLI_CONFIG_DIR 不被覆盖", mine in ran(), ran()[:120])

    # 写 + 没有审批目录 ⇒ fail-closed，且不执行
    os.path.exists(sentinel) and os.remove(sentinel)
    rc, out, err, _ = run_wrap(["--", "wecom-bot", "message", "aibot", "send",
                                "--chat-id", "x", "--msg-type", "markdown",
                                "--markdown", '{"content":"hi"}'], env)
    check("要发消息但没审批目录 ⇒ 126", rc == 126, "rc=%s" % rc)
    check("…理由说清是缺审批目录", "CC_APPROVAL_DIR" in err, err[:70])
    check("…真身一次都没被执行", ran() == "", ran()[:60])

    # 有目录 + 点允许 ⇒ 执行，卡面写 wecom-bot 自己的名字
    d2 = tempfile.mkdtemp(prefix="wecom-adir-")
    env2 = _env(GATE_REAL_BIN=real, CC_APPROVAL_DIR=d2, CC_APPROVAL_TARGET="u|1|u",
                CC_APPROVAL_SEEN_SEC=8, CC_APPROVAL_WAIT_SEC=8)
    rc, out, err, box = run_wrap(["--", "wecom-bot", "message", "aibot", "send",
                                  "--chat-id", "CEO", "--msg-type", "markdown",
                                  "--markdown", '{"content":"hello"}'],
                                 env2, bridge=play_bridge(d2, "allow"))
    req = box.get("req") or {}
    check("点允许 ⇒ 执行真身", rc == 7, "rc=%s %s" % (rc, err[:60]))
    check("…卡面工具名是 wecom-bot", req.get("tool_name") == "wecom-bot",
          str(req.get("tool_name")))
    check("…卡面带了「写操作」的理由", "写操作" in (req.get("policy") or ""),
          (req.get("policy") or "")[:60])

    # 点拒绝 ⇒ 不执行
    os.path.exists(sentinel) and os.remove(sentinel)
    rc, out, err, _ = run_wrap(["--", "wecom-bot", "message", "aibot", "send"],
                               env2, bridge=play_bridge(d2, "deny"))
    check("点拒绝 ⇒ 126 且不执行", rc == 126 and ran() == "", "rc=%s" % rc)


# ---------------- ④ hook 的让路/拒绝 ----------------

def test_hook_shim_mode():
    print("\n== hook：解析到 shim 就让路，绕过就拒绝 ==")
    shim = tempfile.mkdtemp(prefix="wecom-shim-")
    other = tempfile.mkdtemp(prefix="wecom-other-")
    for d in (shim, other):
        with open(os.path.join(d, "wecom-bot.cmd"), "w", encoding="utf-8") as f:
            f.write("@echo off\r\n")

    old_path = os.environ.get("PATH", "")
    old_shim = os.environ.get("CC_GATE_SHIM_DIR")
    try:
        os.environ["CC_GATE_SHIM_DIR"] = shim
        os.environ["PATH"] = shim + os.pathsep + other + os.pathsep + old_path

        ok, note = gate.classify_bash("wecom-bot message aibot send --chat-id x")
        check("解析到 shim ⇒ 让路（读写交给包装器）", ok is True, note)

        ok, note = gate.classify_bash("wecom-bot message aibot sessions list")
        check("只读命令也让路（包装器自己会直跑）", ok is True, note)

        bypass = os.path.join(other, "wecom-bot.cmd")
        ok, note = gate.classify_bash('"%s" message aibot send --chat-id x' % bypass)
        check("全路径绕过包装器 ⇒ 拒绝", ok is False, note)
        check("…理由点明没经过包装器", "没经过闸门包装器" in note, note)
    finally:
        os.environ["PATH"] = old_path
        if old_shim is None:
            os.environ.pop("CC_GATE_SHIM_DIR", None)
        else:
            os.environ["CC_GATE_SHIM_DIR"] = old_shim


def main():
    test_classify()
    test_wrapper()
    test_hook_shim_mode()
    print("\n" + ("✅ 全部通过" if OK else "❌ 有失败项"))
    return 0 if OK else 2


if __name__ == "__main__":
    sys.exit(main())