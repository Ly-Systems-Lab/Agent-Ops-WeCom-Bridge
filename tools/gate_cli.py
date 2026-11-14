#!/usr/bin/env python3
"""闸门的**通用入口** —— 任何一个 harness 都能把它的"执行前"指到这里。

    入：stdin   {"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": "…"}
    出：stdout  放行 = 空；拒绝 = {"permissionDecision":"deny","reason":"…"}
        退出码  0 = 放行   2 = 拒绝

**判定一行都不重写。** 直接复用 `approval_hook.py`（凭据硬 deny → 免卡白名单 →
Bash 只读分类 → 发卡等用户点）。这里只做三件"入口"该做的事：

    ① 收各家的入参形状，归一成 `tool_name / tool_input / cwd`；
    ② 把工具名映射成判定层听得懂的名字（`read_image` → `Read`）；
    ③ 把最后一句话换成通用方言（`approval_hook.DIALECT = "generic"`）。

为什么这个切法是对的（2026-11-14 DSH 实机证据）：
    DSH 的 `tools/pre-execute` 把它的 `read_image` 映射成 `Read` 再进**同一套**分类器，
    日志里是 `[dsh][gate] read_image→Read ⇒ allow`。
    ⇒ 与 harness 绑死的只有**触发点**和**工具名**；判定本来就是通用的。

⚠️ **调用方必须把「非 0 退出 / 输出解析不了 / 进程起不来」一律当成拒绝。**
    我们这边所有意外路径都会汇到 deny，但如果调用方把异常当放行，闸门就只是装饰
    —— 这和 2026-11-09 那条"hook 没跑和 hook 放行长得一模一样"是同一个病。

用法（以 DSH 为例，把它的 pre-execute 指向本文件即可）：

    python -X utf8 tools/gate_cli.py        # stdin 收事件，stdout 回结论

环境变量与 `approval_hook.py` 完全一致（`CC_APPROVAL_DIR` 决定有没有卡片能力），
另外多认一个 `GATE_TOOL_MAP`，给 harness 自己补工具名映射：

    GATE_TOOL_MAP="shell:Bash,fetch_url:WebFetch"

    ⚠️ 映射要**宁可少、不要猜**：名字对不上时它会走"弹卡"那条路（保守），
       乱映射反而会把一个不认识的工具塞进只读白名单里放过去。
"""

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import approval_hook as gate                     # noqa: E402


# 内置映射：**只收语义与 Claude Code 同名的那几个**。
#   · `read_image` 是 DSH 的真名（实机日志 `read_image→Read ⇒ allow`）；
#   · 其余两个是同类 harness 里的常见叫法，语义同样是"按路径读文件"。
# 没有证据的工具名**不进这张表** —— 认不出来 ⇒ 弹卡，比猜错安全。
TOOL_ALIASES = {
    "read_image": "Read",
    "view_image": "Read",
    "read_file": "Read",
}


def _alias_map():
    """内置表 + `GATE_TOOL_MAP`（`旧名:新名,…`，逗号分隔）。"""
    m = dict(TOOL_ALIASES)
    raw = os.environ.get("GATE_TOOL_MAP", "")
    for item in raw.split(","):
        if ":" not in item:
            continue
        k, v = item.split(":", 1)
        k, v = k.strip().lower(), v.strip()
        if k and v:
            m[k] = v
    return m


def _normalize(ev):
    """把各家的入参形状归一成 `approval_hook` 认的那一套。

    认这些键（有就取，没有就退回默认）：
        tool_name | tool | name
        tool_input | input | arguments   （值是 JSON 字符串也认）
        cwd                              （缺了就用当前工作目录）

    **只动键名，不动值** —— 参数里的路径、命令原样交给判定层。
    """
    name = ev.get("tool_name") or ev.get("tool") or ev.get("name") or ""
    tinput = ev.get("tool_input")
    if tinput is None:
        tinput = ev.get("input")
    if tinput is None:
        tinput = ev.get("arguments")
    if isinstance(tinput, str):
        try:
            tinput = json.loads(tinput)
        except Exception:
            tinput = {}
    if not isinstance(tinput, dict):
        tinput = {}

    raw_name = str(name).strip()
    name = _alias_map().get(raw_name.lower(), raw_name)
    cwd = str(ev.get("cwd") or os.getcwd())

    out = dict(ev)
    out["tool_name"] = name
    out["tool_input"] = tinput
    out["cwd"] = cwd
    return out


def _deny(reason):
    """入口自己出的错（读不到 / 不是 JSON）也按同一方言拒绝。"""
    print(json.dumps({"permissionDecision": "deny", "reason": reason},
                     ensure_ascii=False))
    return 2


def main():
    gate.DIALECT = "generic"

    try:
        raw = sys.stdin.read()
    except Exception as e:                        # noqa: BLE001
        return _deny(f"闸门读不到本次事件（{type(e).__name__}: {e}），保守拒绝。")
    try:
        ev = json.loads(raw) if raw.strip() else {}
    except Exception as e:
        return _deny(f"闸门收到的事件不是合法 JSON（{e}），保守拒绝。")
    if not isinstance(ev, dict):
        return _deny("闸门收到的事件不是 JSON 对象，保守拒绝。")

    ev = _normalize(ev)

    # 归一后放回 stdin，交给**同一段**判定（它自己 sys.exit，不会返回）。
    sys.stdin = io.StringIO(json.dumps(ev, ensure_ascii=False))
    gate.main()
    return 0        # 走不到；`main()` 里所有分支都 exit


if __name__ == "__main__":
    sys.exit(main())