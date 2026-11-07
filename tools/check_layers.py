#!/usr/bin/env python3
"""分层不变量检查 —— 抽层之后，用机器而不是靠自觉守住边界。

三条判据（任何一条破了都说明「分层」只是文件级搬家）：

  ① **核心层不认识平台**：`app/core/` 里不许出现平台帧名、平台错误码、WebSocket 字面量。
  ② **核心层不反向依赖**：`app/core/` 不许 import `platforms`。
  ③ **平台层不反向依赖入口**：`app/platforms/` 不许 import `bridge`（否则成环）。

注释与文档串**会被剥掉**再检查 —— 判据针对的是代码。核心层的说明文字里也不该
出现这些名字；本文件是唯一例外（令牌就写在这里）。

⚠️ 契约里的规范化字段名（`response_url` / `chattype` / `errcode`）算**通用词汇**，
不在此列：它们描述的是"平台给的一次性回复通道 / 会话类型 / 结果码"，每个平台都会
有对应物，只是名字沿用了企微的叫法。

用法： `.venv/Scripts/python.exe -X utf8 tools/check_layers.py`
退出码 0 = 全部通过。
"""

import ast
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app")

# 平台专有令牌：出现在核心层就是越界。新增平台时往这里加。
# 每加一条都必须是"只有某个平台才认识"的东西 —— 通用词汇（如 chat_type / errcode）
# 不算，那些是契约的一部分。
FORBIDDEN_IN_CORE = [
    # 企微
    "aibot_", "websocket", "40058", "42045", "template_card",
    # 飞书
    "lark_oapi", "cardkit", "im.message.receive_v1", "card.action.trigger",
    "230020", "230025", "open_id",
]

REVERSE_DEPS = {
    "core": ["platforms"],
    "platforms": ["bridge"],
}


def strip_comments(src):
    """删掉每行 `#` 之后的部分（引号内的 `#` 不算）。"""
    lines = []
    for line in src.splitlines():
        quote = None
        cut = len(line)
        for i, ch in enumerate(line):
            if quote:
                if ch == quote:
                    quote = None
            elif ch in ("'", '"'):
                quote = ch
            elif ch == "#":
                cut = i
                break
        lines.append(line[:cut])
    return lines


def docstring_rows(tree):
    rows = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                c = body[0].value
                for ln in range(c.lineno, (c.end_lineno or c.lineno) + 1):
                    rows.add(ln)
    return rows


def imports_of(path):
    tree = ast.parse(open(path, encoding="utf-8").read())
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            out |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            out.add(node.module.split(".")[0])
    return out


def py_files(sub):
    base = os.path.join(APP, sub)
    for dirpath, dirnames, names in os.walk(base):
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for n in sorted(names):
            if n.endswith(".py"):
                yield os.path.join(dirpath, n)


def rel(p):
    return os.path.relpath(p, ROOT).replace("\\", "/")


def main():
    fails = []
    for path in py_files("core"):
        src = open(path, encoding="utf-8").read()
        rows = docstring_rows(ast.parse(src))
        for i, line in enumerate(strip_comments(src), 1):
            if i in rows:
                continue
            for tok in FORBIDDEN_IN_CORE:
                if tok in line:
                    fails.append(f"{rel(path)}:{i}  核心层出现平台令牌 {tok!r}")
        for m in imports_of(path):
            if m in REVERSE_DEPS["core"]:
                fails.append(f"{rel(path)}  核心层反向 import {m!r}")

    for path in py_files("platforms"):
        for m in imports_of(path):
            if m in REVERSE_DEPS["platforms"]:
                fails.append(f"{rel(path)}  平台层 import 了入口模块 {m!r}")

    if fails:
        print("分层不变量：❌ 未通过")
        for f in fails:
            print("  " + f)
        return 1
    n_core = len(list(py_files("core")))
    n_plat = len(list(py_files("platforms")))
    print(f"分层不变量：✅ 全部通过（核心层 {n_core} 个文件，平台层 {n_plat} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())