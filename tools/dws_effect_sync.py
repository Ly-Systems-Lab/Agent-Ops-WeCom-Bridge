#!/usr/bin/env python3
"""把 `dws` 官方的「安全语义」落成一张可 diff 的本地表。

## 为什么要有这个工具

审批闸门要回答一个问题：**这条命令会不会改数据？** 手抄一张白名单
有两个毛病 —— 抄不全（官方 1500+ 条命令）、会过期（跟着 CLI 版本走）。

`dws` 自己发布了这个答案：

    dws schema --all --compact      # 1559 条工具，每条带 effect / confirmation / risk

`effect` 取值 `read` / `write` / `destructive`，`confirmation` 取值
`not_required` / `user_required`。官方文档明写 `--compact` 面向 Agent，
输出"选参、约束、**安全语义**和已评审的返回契约"。

所以白名单**不手抄，只同步**：官方维护分类，我们把它抓下来入库、可 diff、
可复核。运行时闸门只读这张表（毫秒级），不联网、不起子进程。

## 用法

    python tools/dws_effect_sync.py            # 重新抓取并写入 interop/dws_effects.json
    python tools/dws_effect_sync.py --check    # 只比对，不改文件；有漂移则退出码 1

`dws upgrade` 之后跑一次 `--check`。看到 `read → write` 的条目要停下来看一眼：
那意味着某个我们原本直放的命令，官方改口说它会写数据了。

⚠️ **`effect=read` 是官方的说法，不是我们的判断。** 它读的是数据还是密钥，
本工具不区分 —— 那件事在 `tools/approval_hook.py` 的 `SENSITIVE_READ` 里，
由人逐条标注（见 `interop/dingtalk.md` 的「敏感读」一节）。
"""

import json
import os
import re
import subprocess
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "interop", "dws_effects.json")
SUMMARY_MAX = 60          # 每条只留一句话，够判断"这是不是读"就行


def _run(args):
    # ⚠️ Windows 上 `dws` 是 `dws.cmd`，CreateProcess 起不了批处理 ⇒ 必须过 shell。
    # 参数全是本文件里的常量，没有外部输入拼进去。
    p = subprocess.run(" ".join(args), shell=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    if p.returncode != 0:
        raise RuntimeError("%s 失败 rc=%s：%s"
                           % (" ".join(args), p.returncode, (p.stderr or "").strip()[:300]))
    return p.stdout


def fetch():
    """抓官方表，返回 (commands, cli_version)。"""
    raw = _run(["dws", "schema", "--all", "--compact"])
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError("schema 输出不是合法 JSON：%s" % e)
    try:
        raw_ver = _run(["dws", "--version"]).strip().splitlines()[0]
        m = re.search(r"v\d+\.\d+\.\d+", raw_ver)
        ver = m.group(0) if m else raw_ver[:40]
    except Exception:
        ver = ""

    commands = {}
    for prod in data.get("products") or []:
        for t in prod.get("tools") or []:
            path = (t.get("cli_path") or "").strip()
            if not path:
                continue
            summary = (t.get("agent_summary") or t.get("description") or "").strip()
            summary = " ".join(summary.split())      # 折掉换行，一行装下
            if len(summary) > SUMMARY_MAX:
                summary = summary[:SUMMARY_MAX - 1] + "…"
            commands[path] = {
                "effect": t.get("effect") or "",
                "confirmation": t.get("confirmation") or "",
                "risk": t.get("risk") or "",
                # 写命令能不能 --dry-run 预演。卡面据此多一句提示，不自动执行。
                "dry_run": bool(t.get("dry_run")),
                "summary": summary,
            }
    if not commands:
        raise RuntimeError("schema 里一条命令都没抓到 —— 表结构和预期不一致？")
    return commands, ver


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def diff(old, new):
    """返回 [(kind, path, 说明)]，kind ∈ {新增, 消失, 变了}。"""
    o = old.get("commands") or {}
    out = []
    for k in sorted(set(new) - set(o)):
        out.append(("新增", k, "%s / %s" % (new[k]["effect"], new[k]["confirmation"])))
    for k in sorted(set(o) - set(new)):
        out.append(("消失", k, "%s / %s" % (o[k].get("effect"), o[k].get("confirmation"))))
    for k in sorted(set(o) & set(new)):
        a, b = o[k], new[k]
        if (a.get("effect"), a.get("confirmation")) != (b.get("effect"), b.get("confirmation")):
            out.append(("变了", k, "%s/%s → %s/%s"
                        % (a.get("effect"), a.get("confirmation"),
                           b.get("effect"), b.get("confirmation"))))
    return out


def main(argv):
    check = "--check" in argv
    commands, ver = fetch()
    print("抓到 %d 条命令（%s）" % (len(commands), ver or "版本未知"))

    old = None
    if os.path.exists(OUT):
        try:
            old = _load(OUT)
        except Exception as e:
            print("⚠️ 现有文件读不出来（%s），按全新生成处理" % e)

    if check and old is not None:
        d = diff(old, commands)
        if not d:
            print("✅ 与 %s 一致，无漂移" % os.path.relpath(OUT, ROOT))
            return 0
        print("⚠️ 有 %d 处漂移：" % len(d))
        for kind, path, note in d[:60]:
            print("  [%s] %-40s %s" % (kind, path, note))
        if len(d) > 60:
            print("  …… 还有 %d 处" % (len(d) - 60))
        dangerous = [x for x in d if x[0] == "变了" and "→ write" in x[2]]
        if dangerous:
            print("\n🔴 其中 %d 条由 read 变成 write —— 闸门会自动跟着收紧（弹卡），"
                  "但要确认不是我们把某条读命令误当成了写。" % len(dangerous))
        return 1

    payload = {
        "_source": "dws schema --all --compact",
        "_note": "官方安全语义快照。effect=read/write/destructive；confirmation=not_required/user_required。"
                 "只由 tools/dws_effect_sync.py 生成，别手改。",
        "cli_version": ver,
        "count": len(commands),
        "commands": dict(sorted(commands.items())),
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1, sort_keys=False)
        f.write("\n")
    os.replace(tmp, OUT)
    print("✅ 写入 %s（%d 条，%.0f KB）"
          % (os.path.relpath(OUT, ROOT), len(commands), os.path.getsize(OUT) / 1024))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Exception as e:
        print("❌ %s" % e)
        sys.exit(2)