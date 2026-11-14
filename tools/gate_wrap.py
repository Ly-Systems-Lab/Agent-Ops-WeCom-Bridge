#!/usr/bin/env python3
"""`dws` / `lark-cli` 的**包装器**：让执行面自己持有闸门。

为什么要有它
    hook 只在"harness 愿意调它"时生效。谁绕过 harness 直接敲 `dws chat +send`，
    中间就没有任何判定。包装器的位置不同：它**就是**那条命令的入口，
    判读写、发卡、等人点，做完才把命令交给真身。

    2026-11-15 定的分工（v1）：
        · 包装器 = **owner**：读写由它判，卡由它发，审批协议与 hook 共用一份
          （`approval_hook.ask_card`）；
        · hook   = **让路**：看到命令解析到我们的 shim 就直放，不重复弹卡；
          看到命令绕过 shim（全路径 / 改过 PATH）就拒绝。

    ⚠️ v1 是**减速带，不是墙**：它只覆盖"经过 PATH 解析"的调用。真身还没改名
       （`GATE_REAL_BIN` 是给 v2 改名用的），所以有人写全路径仍然到得了真身 ——
       那一条由 hook 的"绕过就拒"兜住，两条路都关着才算墙。

用法（shim 就是这么调它的）
    python tools/gate_wrap.py -- dws todo +create --title "x"
    python tools/gate_wrap.py -- lark-cli calendar +agenda

退出码
    与真身一致；被闸门拒绝时 **126**（理由在 stderr）。
"""

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import approval_hook as gate                      # noqa: E402

# 只包这两个执行面；名字里带不带后缀都认（shim 传的是裸名）
KNOWN = ("dws", "lark-cli")


def _usage():
    print("用法：gate_wrap.py -- <dws|lark-cli> <参数…>", file=sys.stderr)
    return 2


def _split_args(argv):
    """剥掉 `--shim-dir X` 和可选的 `--`，返回 (shim 目录, 程序名, 参数表)。

    ⚠️ `--shim-dir` 是 shim **自己带上来**的（不是从环境变量读），因为包装器要靠它
    "跳过自己这一层"去找真身。少了它、又恰好是手工垫的 PATH ⇒ 解析回 shim 自己 ⇒
    无限递归（2026-11-15 实测：一个 `dws --help` 起了几百层进程，只能按 PID 收树）。
    """
    shim = ""
    if len(argv) >= 2 and argv[0] == "--shim-dir":
        shim, argv = argv[1], argv[2:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv:
        return shim, "", []
    return shim, argv[0], argv[1:]


def _base(prog):
    p = os.path.basename(prog.strip("\"'")).lower()
    for ext in (".exe", ".cmd", ".bat"):
        if p.endswith(ext):
            p = p[: -len(ext)]
    return p


def _real_bin(base, shim_dir):
    """真身在哪。`GATE_REAL_BIN` 优先（v2 改名后靠它指回去）。

    否则在 PATH 里找，**跳过 shim 目录**（那里面是我们自己，找到就是死循环）。
    顺序按 Windows 的习惯：.exe → .cmd → .bat → 裸名（给 POSIX）。
    """
    forced = os.environ.get("GATE_REAL_BIN", "").strip()
    if forced:
        # 配到 shim 自己身上 = 死循环，直接判"找不到"，交给上层报错
        if shim_dir and os.path.normcase(os.path.dirname(os.path.abspath(forced))) == \
                os.path.normcase(os.path.abspath(shim_dir)):
            return ""
        return forced
    want = os.path.normcase(os.path.abspath(shim_dir)) if shim_dir else ""
    for d in (os.environ.get("PATH") or "").split(os.pathsep):
        if not d.strip():
            continue
        if want and os.path.normcase(os.path.abspath(d)) == want:
            continue
        for ext in (".exe", ".cmd", ".bat", ""):
            p = os.path.join(d, base + ext)
            if os.path.isfile(p):
                return p
    return ""


def _run(real, args):
    """把命令交给真身，返回它的退出码。**不捕获 stdout**：那是用户的。"""
    # 递归保险丝：万一真身又解析回包装器（PATH 配错、shim 又被拷了一份），
    # 深度一到就报错退出，别把那棵进程树一直养下去。
    depth = os.environ.get("GATE_WRAP_DEPTH", "0")
    try:
        depth = int(depth)
    except ValueError:
        depth = 0
    env = dict(os.environ)
    env["GATE_WRAP_DEPTH"] = str(depth + 1)
    try:
        return subprocess.call([real] + args, env=env)
    except FileNotFoundError:
        print("闸门：找不到 %s 的本体（%s）。" % (args[0] if args else "命令", real),
              file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


def main(argv=None):
    arg_shim, prog, args = _split_args(list(sys.argv[1:] if argv is None else argv))
    base = _base(prog)
    if base not in KNOWN:
        return _usage()

    # 递归保险丝（同上）：**在解析真身之前**就挡，别等进程树长起来。
    try:
        depth = int(os.environ.get("GATE_WRAP_DEPTH", "0") or 0)
    except ValueError:
        depth = 0
    if depth >= 3:
        print("闸门：包装器嵌套 %d 层，疑似 PATH 把 shim 又解析回自己 ⇒ 停止。" % depth,
              file=sys.stderr)
        return 127

    # 包装器的结论用"人话"说：理由走 stderr，退出码 126。
    gate.DIALECT = "text"

    # shim 自己带上来的目录优先；没有就退回环境变量（预检/手工调用时用）。
    shim_dir = arg_shim or os.environ.get("CC_GATE_SHIM_DIR", "").strip()
    real = _real_bin(base, shim_dir)
    if not real:
        print("闸门：PATH 上找不到 %s 的本体（排除包装器自己的目录后）。" % base,
              file=sys.stderr)
        return 127

    adir = os.environ.get("CC_APPROVAL_DIR", "").strip()

    # 免卡的人（桥按**发消息的人**注入 `CC_APPROVAL_AUTO=1`）：与 hook 同一条语义 ——
    # 不弹卡，直接执行。记一行账，否则"这一轮为什么没卡"事后没法解释。
    if os.environ.get("CC_APPROVAL_AUTO", "").strip() == "1":
        if adir:
            gate._log_readonly(adir, "[auto-wrap] %s %s" % (base, " ".join(args)))
        return _run(real, args)

    # 判定：**与 hook 完全同一套**（`_cli_ok` 认 dws 的 effect 表、lark-cli 的 risk 表）
    ok, policy = gate._cli_ok(base, args)
    if ok:
        if adir:
            gate._log_readonly(adir, "[wrap] %s %s" % (base, " ".join(args)))
        return _run(real, args)

    # 要授权：先证明"能发卡"，再发。没有审批目录 ⇒ 直接拒绝（不降级成放行）。
    # `exit_on_allow=False`：批下来之后**还要执行这条命令**，不能让 allow() 把进程带走。
    gate.ask_card(base, {"command": " ".join([base] + args)}, policy,
                  exit_on_allow=False)
    return _run(real, args)


if __name__ == "__main__":
    sys.exit(main())