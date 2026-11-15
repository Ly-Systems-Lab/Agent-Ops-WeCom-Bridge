#!/usr/bin/env python3
"""把官方 `wecom-cli schema list` 抓成**读写快照**（`interop/wecom_effects.json`）。

**为什么需要自己标**：`wecom-cli` 的 schema 只有入参/出参/描述，**没有** `dws`
那种 `effect` / `confirmation` 字段（实测 2026-11-15）。所以"哪些是读"必须由
我们给出 —— 而且只能**白名单式**给：列在 `READ` 里的算读，**其余一律算写**。
这样官方将来新增方法时，默认是弹卡（安全方向错不了），而不是被自动当成读。

**为什么不做"动词猜"**：`list` / `get` 看着像读，但 `message.files.get` 会把
媒体**下载到本地**；`smartsheet.records.query` 是一段 SQL。名字像什么不能作数
（同 dws / lark 那两张表的教训：按 HTTP 动词猜 `api GET` 被否掉过）。

用法：
    python -X utf8 tools/wecom_effect_sync.py          # 重新生成快照
    python -X utf8 tools/wecom_effect_sync.py --check  # 只比对，有漂移就退出 1
"""

import argparse
import json
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "interop", "wecom_effects.json")

# ⚠️ **只有这里列出来的方法算读**。判断口径是"它会不会改动你的数据 / 别人的数据"，
# 不是"名字里有没有 get"。加一条之前先问：这一步会不会让任何人收到消息、改到文档、
# 写本地文件、或者产生新的数据？会 ⇒ 不放这里。
READ = {
    # 日历：查日程 / 查闲忙
    "calendar.schedules.get", "calendar.schedules.list",
    "calendar.schedules.search", "calendar.schedules.free.list",
    # 会话：群列表 / 消息列表（官方只给最近 7 天，且登记在案）
    "chat.groups.list", "chat.messages.list",
    # 通讯录：按关键词搜人（只读元信息）
    "contact.users.search",
    # 微盘：基本信息 / 列表 / 搜索（下载会落盘，不在白名单里）
    "disk.files.get", "disk.files.list", "disk.files.search",
    # 文档：搜索 / 读正文
    "doc.search", "doc.contents.get",
    # 邮件：读 / 搜
    "mail.get", "mail.search",
    # 消息：机器人会话列表；媒体内容会落盘 ⇒ 见下面的 LOCAL_WRITE
    "message.aibot.sessions.list", "message.files.get",
    # 会议：详情 / 列表 / 搜索 / 转写原文 / 会议室
    "meeting.get", "meeting.list", "meeting.search", "meeting.original.get",
    "meeting.rooms.search", "meeting.rooms.buildings.list",
    # 表格：所有 `get` / `list` / `find` 形态
    "sheet.get", "sheet.cells.find", "sheet.cells.get", "sheet.ranges.get",
    "sheet.cf.list", "sheet.filter.list", "sheet.float.image.list",
    "sheet.version.list", "sheet.version.get", "sheet.info.get",
    "sheet.merge.get", "sheet.protects.list", "sheet.table.style.list",
    "sheet.validation.list", "sheet.workbook.get",
    # 智能主页 / 智能表格：只读的那几个
    "smartpage.databases.get", "smartpage.pages.get",
    "smartsheet.get", "smartsheet.charts.list", "smartsheet.fields.list",
    "smartsheet.records.list", "smartsheet.sheets.list", "smartsheet.views.list",
    # 待办：读
    "todo.get", "todo.list",
    # 身份：只回"我是谁"
    "identity.whoami",
    # 幻灯片：元数据 / 页面摘要 / 设计契约
    "slide.get", "slide.pages.get", "slide.designs.get",
}

# 不在 schema 里的顶层子命令（`wecom-cli auth …`）。同样白名单式。
EXTRA_READ = {"auth.show"}
EXTRA_WRITE = {"auth.init"}          # 写凭据目录，必须弹卡

# 官方标"读"、但一定会往本地写文件的（由 approval_hook 的 `_READ_WRITES_LOCAL` 使用）。
# 这里只声明，实际拦截在 hook 里 —— 一张快照管两件事，别让两边各记一份。
LOCAL_WRITE = {
    "message.files.get": "把媒体内容下载到本地（落盘路径由官方 CLI 决定）",
}


def _load_cli_schema():
    # Windows 上 `wecom-cli` 是 npm 的 `.cmd` —— 裸名交给 CreateProcess 找不到，
    # 必须先 `which` 解析成带后缀的完整路径（2026-11-15 实测）。
    exe = os.environ.get("WECOM_CLI_BIN") or shutil.which("wecom-cli") or ""
    if not exe:
        print("PATH 上找不到 wecom-cli —— 先装上官方 CLI（npm i -g @wecom/cli）",
              file=sys.stderr)
        return None
    try:
        out = subprocess.run([exe, "schema", "list"], capture_output=True,
                             text=True, encoding="utf-8", timeout=60)
    except FileNotFoundError:
        print("找不到 %s —— 先装上官方 CLI（npm i -g @wecom/cli）" % exe,
              file=sys.stderr)
        return None
    if out.returncode != 0:
        print("`%s schema list` 失败（rc=%s）：%s"
              % (exe, out.returncode, (out.stderr or "")[:300]), file=sys.stderr)
        return None
    try:
        return json.loads(out.stdout)
    except Exception as e:                     # noqa: BLE001
        print("schema list 不是合法 JSON：%s" % e, file=sys.stderr)
        return None


def build(schema):
    cmds = {}
    for svc in schema:
        for m in svc.get("methods") or []:
            name = str(m.get("name") or "")
            if not name:
                continue
            if name in READ:
                cmds[name] = {"effect": "read", "confirmation": "not_required"}
            else:
                cmds[name] = {"effect": "write", "confirmation": "required"}
    for name in EXTRA_READ:
        cmds[name] = {"effect": "read", "confirmation": "not_required"}
    for name in EXTRA_WRITE:
        cmds[name] = {"effect": "write", "confirmation": "required"}
    # `--dry-run` 是官方每条方法都支持的本地预演；卡面只负责提示，**不替他跑**
    for v in cmds.values():
        v["dry_run"] = True
    return {"cli": "wecom-cli",
            "source": "wecom-cli schema list（运行时自省，非技能文档）",
            "read_rule": "只有 tools/wecom_effect_sync.py 的 READ 白名单算读，其余按写",
            "local_write": LOCAL_WRITE,
            "commands": dict(sorted(cmds.items()))}


def main(argv=None):
    ap = argparse.ArgumentParser(description="生成/核对 wecom-cli 读写快照")
    ap.add_argument("--check", action="store_true", help="只比对，有漂移退出 1")
    args = ap.parse_args(argv)

    schema = _load_cli_schema()
    if schema is None:
        return 2
    data = build(schema)

    if args.check:
        try:
            with open(OUT, encoding="utf-8") as f:
                old = json.load(f)
        except Exception as e:                 # noqa: BLE001
            print("读不到快照 %s：%s" % (OUT, e), file=sys.stderr)
            return 2
        a, b = old.get("commands") or {}, data["commands"]
        added = sorted(set(b) - set(a))
        removed = sorted(set(a) - set(b))
        changed = sorted(k for k in set(a) & set(b)
                         if (a[k].get("effect") != b[k].get("effect")))
        if not (added or removed or changed):
            print("✅ 快照与官方 schema 一致（%d 条）" % len(b))
            return 0
        for k in added:
            print("＋ 官方新增（默认按写）：%s" % k)
        for k in removed:
            print("－ 官方已移除：%s" % k)
        for k in changed:
            print("≠ 分类变了：%s %s → %s"
                  % (k, a[k].get("effect"), b[k].get("effect")))
        print("共 %d 条漂移 —— 复核后再跑一次不带 --check 的生成。"
              % (len(added) + len(removed) + len(changed)), file=sys.stderr)
        return 1

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    reads = sum(1 for v in data["commands"].values() if v.get("effect") == "read")
    print("已写入 %s（%d 条：读 %d / 写 %d）"
          % (OUT, len(data["commands"]), reads, len(data["commands"]) - reads))
    return 0


if __name__ == "__main__":
    sys.exit(main())