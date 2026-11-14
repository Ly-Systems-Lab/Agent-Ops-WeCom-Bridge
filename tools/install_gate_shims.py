#!/usr/bin/env python3
"""生成 `dws` / `lark-cli` 的 PATH shim —— 包装器的入口。

内容由 `app/common.py` 的 `write_gate_shims()` 负责（桥启动时用的是同一份，
这里只是给人用的一个命令行壳，免得"生成出来的东西"和"桥实际用的"不是一份）。

为什么是"放两个同名文件"
    · **bash（Claude Code 的 Bash 工具走这个）** 只认**裸名**，不看 PATHEXT
      ⇒ 目录里必须有一个没有后缀的 `dws`（`#!/bin/sh` 脚本）。
    · **cmd.exe / CreateProcess** 只认 `PATHEXT` 里的后缀
      ⇒ 同一个目录里还要有 `dws.cmd`。
    · 优先级靠 **目录顺序**，不是后缀顺序：把本目录垫在 PATH 最前，两边都先命中我们。
      （这也是为什么"往真身同一个目录里塞一个同名 .cmd"不管用 —— 那里 `.exe` 排在
       `.cmd` 前面，永远轮不到它。）

用法
    python -X utf8 tools/install_gate_shims.py            # 生成到 data/gate-bin
    python -X utf8 tools/install_gate_shims.py --dir D    # 指定目录
    python -X utf8 tools/install_gate_shims.py --only dws # 只做其中一个

⚠️ **桥自己会做这件事**（启动时写 `data/gate-bin/` 并垫进子进程 PATH）。
   这个脚本是给"手工装/看一眼"用的，不是必经步骤。
"""

import argparse
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "app"))

from common import GATE_SHIM_NAMES, gate_shim_dir, write_gate_shims   # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成 dws / lark-cli 的 PATH shim")
    ap.add_argument("--dir", default=gate_shim_dir(),
                    help="输出目录（默认 data/gate-bin）")
    ap.add_argument("--only", default="", help="只做这些名字，逗号分隔")
    args = ap.parse_args(argv)

    names = [n.strip() for n in args.only.split(",") if n.strip()] or list(GATE_SHIM_NAMES)
    bad = [n for n in names if n not in GATE_SHIM_NAMES]
    if bad:
        print("不认识的名字：%s（只支持 %s）"
              % (", ".join(bad), ", ".join(GATE_SHIM_NAMES)), file=sys.stderr)
        return 2

    made = write_gate_shims(args.dir, names=tuple(names))
    print("已生成 %d 个文件：" % len(made))
    for p in made:
        print("  " + p)
    print("\n把下面这个目录垫在 PATH 最前面，`dws` / `lark-cli` 才会经过包装器：")
    print("  " + os.path.abspath(args.dir))
    for n in names:
        if not shutil.which(n):
            print("⚠️ PATH 上找不到 %s 的本体 —— 包装器会报「找不到本体」并退出 127"
                  "（先装上它再用）" % n, file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())