#!/usr/bin/env python3
"""把 `lark-cli` 官方的「风险等级」落成一张可 diff 的本地表。

## 为什么要有这个工具

和 `dws` 一样：审批闸门要回答 **这条命令会不会改数据**，而手抄白名单
既抄不全、又会过期。飞书官方把答案放在两个地方：

  · `lark-cli schema` —— 256 条 API 命令的 JSON，每条 `_meta.risk`
    取值 `read` / `write` / `high-risk-write`，另有 `danger`、`access_tokens`、
    `scopes`。面向 Agent 的机器可读契约。
  · `lark-cli <命令...> --help` 末尾的 `Risk:` 一行 —— 覆盖 `+快捷命令`、
    `auth` / `config` / `update` 这类管理命令（它们不在 schema 里）。

所以**不手抄，只同步**：本工具把两边抓下来合并、入库、可 diff、可复核。
运行时闸门只读快照（毫秒级），不联网、不起子进程。

## 用法

    python tools/lark_effect_sync.py            # 重新抓取并写入 interop/lark_effects.json
    python tools/lark_effect_sync.py --check    # 只比对，不改文件；有漂移则退出码 1

`lark-cli update` 之后跑一次 `--check`。看到 `read → write` 的条目要停下来看一眼：
那意味着某个我们原本直放的命令，官方改口说它会写数据了。

## 两条边界（别越过去）

⚠️ **`risk=read` 是官方的说法，不是我们的判断。** 它读的是不是密钥，
    本工具不区分 —— 那件事在 `tools/approval_hook.py` 的 `_SENSITIVE_READ` 里
    由人逐条标注。
⚠️ **`high-risk-write` 需要 `--yes`**（官方契约）—— 闸门据此在卡面写「危险操作」，
    但**不替用户点击**：`--yes` 本身是那条命令的参数，不是审批的替代品。
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
OUT = os.path.join(ROOT, "interop", "lark_effects.json")
SUMMARY_MAX = 60          # 每条只留一句话，够判断"这是不是读"就行
MAX_DEPTH = 4             # 命令树最深就这么深（domain resource method）

_RISK_RE = re.compile(r"^Risk:\s*(read|write|high-risk-write)\s*$", re.M)
# 帮助里的 "Available Commands:" 块：两空格缩进 + 命令名 + 两个以上空格 + 说明
_SUB_RE = re.compile(r"^  (\S+)\s{2,}\S", re.M)
_SECTION_RE = re.compile(r"^Available Commands:\s*$", re.M)


def _run(args):
    # ⚠️ Windows 上 `lark-cli` 是 npm 的 `lark-cli.cmd`，CreateProcess 起不了批处理
    # ⇒ 必须过 shell。参数全是本文件里的常量与官方输出里抓来的命令名，没有自由输入。
    p = subprocess.run(" ".join(args), shell=True, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=180)
    return p.stdout or "", p.returncode


def _help(path):
    """跑 `lark-cli <命令> --help`，返回原文（不判 rc —— 帮助失败也当"没这条命令"）。"""
    out, _ = _run(["lark-cli"] + list(path) + ["--help"])
    return out


def _subcommands(text):
    """从帮助里抠出子命令名。

    ⚠️ 只认 "Available Commands:" 之后的块 —— 帮助正文里也有两空格缩进的
    示例行，整篇扫会把示例当成命令。
    """
    m = _SECTION_RE.search(text)
    if not m:
        return []
    block = text[m.end():]
    # 块到空行结束（Flags/Use 段之前）
    end = block.find("\n\n")
    if end >= 0:
        block = block[:end]
    return [x for x in _SUB_RE.findall(block) if not x.startswith("-")]


def _risk(text):
    """帮助末尾的 `Risk:` 一行；没有就返回空串（没标 = 按写）。"""
    found = _RISK_RE.findall(text or "")
    return found[-1] if found else ""


def _summary(text):
    """帮助的第一行非空内容当摘要（官方自己写的，比我们编的准）。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if line and not line.startswith("Usage:"):
            return " ".join(line.split())[:SUMMARY_MAX]
    return ""


def fetch_schema():
    """`lark-cli schema` 的 JSON 数组 → {命令路径: 条目}。"""
    raw, rc = _run(["lark-cli", "schema"])
    if rc != 0 or not raw.strip():
        raise RuntimeError("`lark-cli schema` 没有输出（rc=%s）" % rc)
    try:
        data = json.loads(raw)
    except Exception as e:
        raise RuntimeError("schema 输出不是合法 JSON：%s" % e)

    commands = {}
    for e in data if isinstance(data, list) else []:
        name = (e.get("name") or "").strip()
        meta = e.get("_meta") or {}
        risk = str(meta.get("risk") or "").strip()
        if not name or not risk:
            continue
        commands[name] = {
            "risk": risk,
            "kind": "api",
            "danger": bool(meta.get("danger")),
            "identity": list(meta.get("access_tokens") or []),
            "scopes": list(meta.get("scopes") or []),
            "summary": " ".join((e.get("description") or "").split())[:SUMMARY_MAX],
        }
    if not commands:
        raise RuntimeError("schema 里一条命令都没抓到 —— 表结构和预期不一致？")
    return commands


def walk_help(commands, warnings):
    """扫命令树，补上 schema 覆盖不到的那些（`+快捷命令`、管理命令）。

    返回新增条数。与 schema 重叠的条目**互相校对**：两边 risk 不一致要喊出来 ——
    这是两条独立来源，不一致说明官方内部对不上，比我们拍脑袋判读写严重。
    """
    top, _ = _run(["lark-cli", "--help"])
    # 顶层帮助里，域与命令在同一张列表 —— 直接扫 "Available Commands:" 之外的写法：
    # `Lark domains:` 段每行一个域名。
    domains = []
    in_domains = False
    for line in top.splitlines():
        if line.strip().startswith("Lark domains:"):
            in_domains = True
            continue
        if in_domains:
            if not line.strip():
                break
            tok = line.strip().split()
            if tok and not tok[0].startswith("-"):
                domains.append(tok[0])
    if not domains:
        raise RuntimeError("顶层帮助里没解析出业务域 —— 输出格式变了？")

    # 管理类命令不在 "Lark domains:" 段里，单独补
    roots = domains + ["api", "auth", "config", "doctor", "profile", "skills", "update"]

    added = 0
    for root in roots:
        added += _walk([root], commands, warnings, 1)
    return added


def _walk(prefix, commands, warnings, depth):
    text = _help(prefix)
    subs = _subcommands(text)
    if subs and depth < MAX_DEPTH:
        n = 0
        for s in subs:
            n += _walk(prefix + [s], commands, warnings, depth + 1)
        return n
    risk = _risk(text)
    if not risk:
        return 0                       # 没标风险 = 不收录（表外按写，安全）
    key = " ".join(prefix)
    entry = {
        "risk": risk,
        "kind": "shortcut" if prefix[-1].startswith("+") else "command",
        "danger": risk == "high-risk-write",
        "summary": _summary(text),
    }
    old = commands.get(key)
    if old is None:
        commands[key] = entry
        return 1
    # schema 里已有 —— 校对，不改
    if old.get("risk") != risk:
        warnings.append("%s：schema 说 %s，--help 说 %s" % (key, old.get("risk"), risk))
    return 0


def _version():
    out, _ = _run(["lark-cli", "--version"])
    m = re.search(r"\d+\.\d+\.\d+", out or "")
    return m.group(0) if m else ""


def fetch():
    commands = fetch_schema()
    warnings = []
    added = walk_help(commands, warnings)
    if warnings:
        print("⚠️ 两条来源对不上的条目（以 schema 为准入库）：")
        for w in warnings[:20]:
            print("   " + w)
        if len(warnings) > 20:
            print("   …… 还有 %d 条" % (len(warnings) - 20))
    return commands, _version()


def _load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def diff(old, new):
    """返回 [(kind, path, 说明)]，kind ∈ {新增, 消失, 变了}。"""
    o = old.get("commands") or {}
    out = []
    for k in sorted(set(new) - set(o)):
        out.append(("新增", k, new[k]["risk"]))
    for k in sorted(set(o) - set(new)):
        out.append(("消失", k, o[k].get("risk") or "?"))
    for k in sorted(set(o) & set(new)):
        a, b = o[k].get("risk"), new[k].get("risk")
        if a != b:
            out.append(("变了", k, "%s → %s" % (a, b)))
    return out


def main(argv):
    check = "--check" in argv
    commands, ver = fetch()
    print("抓到 %d 条命令（lark-cli %s）" % (len(commands), ver or "版本未知"))

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
            print("  [%s] %-45s %s" % (kind, path, note))
        if len(d) > 60:
            print("  …… 还有 %d 处" % (len(d) - 60))
        loosened = [x for x in d if x[0] == "变了" and "read →" in x[2]]
        if loosened:
            print("\n🔴 其中 %d 条从 read 变成写 —— 闸门会自动收紧（更安全），"
                  "但要确认不是官方把只读命令改坏了。" % len(loosened))
        return 1

    payload = {
        "_source": "lark-cli schema 的 _meta.risk + 每条命令 --help 的 Risk 行",
        "_note": "官方风险等级快照。read / write / high-risk-write。"
                 "只由 tools/lark_effect_sync.py 生成，别手改。"
                 "high-risk-write 官方要求带 --yes。",
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