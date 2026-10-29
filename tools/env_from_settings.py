"""把 `~/.claude/settings.json` 里 `env` 段的 `ANTHROPIC*` 搬到本项目的 `.env`。

为什么需要这一步：
    桥用 `--setting-sources project,local` 排掉 user 设置，以挡掉桌面宠物那类 hook
    （实测它们会把 headless 卡死）；**代价是认证也一起没了**（负对照实测过：
    排掉后报 `Not logged in`）。所以桥必须自己把认证注入子进程环境变量。

为什么不是"拷整份 settings.json"：
    那份文件里同时有明文 token 和一堆与本项目无关的 hook。**只搬 ANTHROPIC* 开头的键。**

安全：本脚本**不打印任何值**，只打印键名和长度。写完请自己 `git status` 确认 `.env` 没被跟踪。

用法：
    .venv\\Scripts\\python.exe -X utf8 tools\\env_from_settings.py            # 写入 .env
    .venv\\Scripts\\python.exe -X utf8 tools\\env_from_settings.py --dry-run  # 只看看会搬什么
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS = os.path.expanduser(r"~\.claude\settings.json")
ENV_PATH = os.path.join(ROOT, ".env")
EXAMPLE = os.path.join(ROOT, ".env.example")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只报告会搬哪些键，不写文件")
    ap.add_argument("--settings", default=SETTINGS, help="settings.json 路径")
    args = ap.parse_args()

    if not os.path.exists(args.settings):
        print(f"[x] 找不到 {args.settings}")
        print("    如果你是用 OAuth 登录的（没有 settings.json 的 env 段），")
        print("    就把 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 直接填进 .env。")
        return 1

    with open(args.settings, encoding="utf-8") as f:
        data = json.load(f)
    env = {k: v for k, v in (data.get("env") or {}).items() if k.startswith("ANTHROPIC")}

    if not env:
        print("[x] settings.json 的 env 段里没有 ANTHROPIC* 键（认证可能走的是登录态）。")
        print("    OAuth 登录态在这条 `--setting-sources project,local` 路径上**未实测**；")
        print("    最稳的做法是显式把 ANTHROPIC_AUTH_TOKEN / ANTHROPIC_BASE_URL 填进 .env。")
        return 1

    print(f"[i] 从 {args.settings} 读到 {len(env)} 个 ANTHROPIC* 键：")
    for k, v in env.items():
        print(f"      {k:<34} 长度={len(str(v))}")
    if not env.get("ANTHROPIC_AUTH_TOKEN"):
        print("[!] 没有 ANTHROPIC_AUTH_TOKEN —— 桥会拒绝启动（见 app/bridge.py 的凭据检查）")

    if args.dry_run:
        print("[i] --dry-run，未写文件")
        return 0

    # 目标文件：.env 不存在就用 .env.example 起头，免得漏掉其它键的注释
    if os.path.exists(ENV_PATH):
        text = open(ENV_PATH, encoding="utf-8").read()
        origin = ".env（已存在，原地更新）"
    elif os.path.exists(EXAMPLE):
        text = open(EXAMPLE, encoding="utf-8").read()
        origin = ".env.example → .env（新建）"
    else:
        text = ""
        origin = "空文件（新建）"

    lines = text.splitlines()
    changed = []
    for k, v in env.items():
        hit = 0
        for i, line in enumerate(lines):
            if line.startswith(k + "="):
                lines[i] = f"{k}={v}"
                hit += 1
        if hit:
            changed.append(f"更新 {k}")
        else:
            lines.append(f"{k}={v}")
            changed.append(f"追加 {k}")

    with open(ENV_PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[✓] {origin}")
    for c in changed:
        print("      " + c)
    print("[i] 提醒：`.env` 已在 .gitignore 第一段；提交前请 `git status` 复核一次。")
    return 0


if __name__ == "__main__":
    sys.exit(main())