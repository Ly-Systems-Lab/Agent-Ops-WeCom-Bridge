#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""扫 `dws` / `lark-cli` 的 read 命令，找「官方标 read、但会往本地写文件」的那些。

为什么需要它：官方风险等级（`effect` / `Risk:`）只描述**对平台数据的副作用**，
不描述**对本机磁盘的副作用**。`lark-cli event consume --output-dir` 就是典型：
官方整条标 `read`，却能把每个事件写成文件。闸门只看 `risk` 就会直接放行。

本工具不做判定，只做**对账**：
  · 扫到的、`_READ_WRITES_LOCAL` 里没有  ⇒ **新增**（官方新加了一条，得补表）；
  · 表里有、扫不到的                    ⇒ **消失**（命令改名/删了，表要清）；
  · 两边的写盘参数名对不上              ⇒ **档位存疑**（人工确认是"不传也落盘"还是"传了才落盘"）。

⚠️ 一条命令属于 A 档（无条件弹卡）还是 B 档（带参数才弹），**工具判断不了** ——
   `--help` 里 "omit to ..." / "(default: ...)" 的写法各命令都不一样。所以这里只列证据，
   档位由人拍板写进 `_READ_WRITES_LOCAL`。

用法：
    python -X utf8 tools/probe_local_writes.py            # 两边都扫（dws 一侧慢，约 25 分钟）
    python -X utf8 tools/probe_local_writes.py --cli lark-cli
    python -X utf8 tools/probe_local_writes.py --cli dws
有差异时退出码 1。
"""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

# 写盘型参数名。**只认这几种名字**，不按描述猜：
#   · `--path` / `--file` / `--dir` 不进这个集合 —— 它们多半是**输入**路径
#     （2026-11-13 实测：`apps +file-get --path` 是远端路径、`markdown +diff --file`
#      是拿来比对的本地输入文件，都不是落盘）；
#   · `docs +fetch --doc-format` 那种描述里带 "local" 的假阳性靠这条名字集合排掉。
WRITE_FLAGS = {"--output", "--output-dir", "--output-path", "--out",
               "--save", "--dest"}
# 再加一道描述口径：必须提到"本地/路径/目录/文件"这一族词。
DESC_HINT = {
    "lark-cli": re.compile(r"local|path|directory|dest|save", re.I),
    "dws": re.compile(r"本地|路径|目录|保存|落盘|文件"),
}
# ⚠️ 别再额外加"描述里带 remote 就排除"这种过滤器 —— 会误杀
#    `apps +file-download --output`（它的描述是 "local output path
#    (default: remote file basename in cwd)"，**落盘**，只是默认名取自远端文件名）。
#    靠上面的**参数名**集合就够把输入路径排干净了。
SNAPSHOT = {
    "lark-cli": os.path.join(REPO, "interop", "lark_effects.json"),
    "dws": os.path.join(REPO, "interop", "dws_effects.json"),
}
RISK_FIELD = {"lark-cli": "risk", "dws": "effect"}
READ_VALUE = {"lark-cli": "read", "dws": "read"}

_FLAG_RE = re.compile(r"^\s+(?:-\w,\s+)?(--[\w-]+)(?:\s+(\w+))?\s+(.*)$")


def load_hook_table():
    """从 approval_hook.py 读 `_READ_WRITES_LOCAL`（只导入常量，不跑 main）。"""
    spec = importlib.util.spec_from_file_location(
        "approval_hook", os.path.join(HERE, "approval_hook.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod._READ_WRITES_LOCAL


def _run(args):
    # Windows 上这两个 CLI 都是 npm 的 `.cmd`，CreateProcess 起不了批处理 ⇒ 必须过 shell。
    # 参数全部来自本仓快照里的命令名，没有自由输入。
    p = subprocess.run(" ".join(args), shell=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    return p.stdout or ""


def _flags_section(text):
    """"Flags:" 段里的 flag（**不含 Global Flags** —— 那是所有命令共用的）。"""
    out, inside = [], False
    for line in text.splitlines():
        if line.strip() == "Flags:":
            inside = True
            continue
        if inside and line and not line.startswith(" "):
            inside = False
        if inside:
            m = _FLAG_RE.match(line)
            if m:
                out.append((m.group(1), (m.group(3) or "").strip()))
    return out


def scan(cli):
    with open(SNAPSHOT[cli], encoding="utf-8") as f:
        table = json.load(f)["commands"]
    reads = sorted(k for k, v in table.items() if v.get(RISK_FIELD[cli]) == READ_VALUE[cli])
    print("%s：read 命令 %d 条，开始扫 --help……" % (cli, len(reads)), flush=True)

    found = {}
    for i, cmd in enumerate(reads, 1):
        for name, desc in _flags_section(_run([cli] + cmd.split() + ["--help"])):
            if name in WRITE_FLAGS and DESC_HINT[cli].search(desc):
                found.setdefault(cmd, []).append((name, desc))
        if i % 100 == 0:
            print("  … %d/%d（已命中 %d）" % (i, len(reads), len(found)), flush=True)
    return found


def diff(cli, found, ours):
    new = sorted(set(found) - set(ours))
    gone = sorted(set(ours) - set(found))
    drift = []
    for cmd in sorted(set(found) & set(ours)):
        want = ours[cmd][0]
        have = {f for f, _ in found[cmd]}
        if want is not None and want not in have:
            drift.append((cmd, want, sorted(have)))
        if want is None and have:
            pass        # A 档（不传也落盘）本来就允许同时带一个 flag，不算漂移

    print("\n===== %s =====" % cli)
    if not (new or gone or drift):
        print("✅ 与 `_READ_WRITES_LOCAL` 一致（%d 条）" % len(ours))
        return False
    if new:
        print("🆕 新增（扫描到、表里没有）—— 请确认档位后补进表：")
        for cmd in new:
            print("  %s" % cmd)
            for name, desc in found[cmd]:
                print("      %s  %s" % (name, desc))
    if gone:
        print("🗑  消失（表里有、这次扫不到）—— 命令可能改名或被删，清理表：")
        for cmd in gone:
            print("  %s" % cmd)
    if drift:
        print("❓ 档位存疑（表里记的写盘参数扫描不到）：")
        for cmd, want, have in drift:
            print("  %s：表里 %s，实际 %s" % (cmd, want, have or "（没有任何写盘参数）"))
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cli", choices=["lark-cli", "dws", "both"], default="both")
    ap.add_argument("--save", help="把扫描结果存成 JSON（可选，方便人工过一遍）")
    args = ap.parse_args()

    table = load_hook_table()
    targets = ["lark-cli", "dws"] if args.cli == "both" else [args.cli]
    everything, dirty = {}, False
    for cli in targets:
        found = scan(cli)
        everything[cli] = {k: [{"flag": f, "desc": d} for f, d in v] for k, v in found.items()}
        dirty = diff(cli, found, table.get(cli, {})) or dirty

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(everything, f, ensure_ascii=False, indent=1)
        print("\n扫描结果已写到 %s" % args.save)

    return 1 if dirty else 0


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.exit(main())