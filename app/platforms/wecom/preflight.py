"""企业微信的「切换前预检」。

**为什么在这里而不在 core**：预检要说的全都是**平台专属**的东西（凭据键名、
白名单口径、依赖）。放到 core 里就等于"内核认识企业微信"——`tools/check_layers.py`
会把这种越界当场抓出来，这条边界是刻意的。

判据必须是 `main()` 里**会拒绝启动**的那几条：预检过了、一启动就退，是最坏的一种
失败（旧平台的连接已经断了）。改 `main()` 的启动闸门时，**这里要同步改**。
"""

from common import env_bool, parse_labels, parse_list, render_whitelist


def preflight(get, importable):
    """返回 `(ok, lines)`。`get(key, default)` / `importable(模块名) -> bool` 都是注入的。"""
    lines, bad = [], []

    if not get("WECOM_BOT_ID") or not get("WECOM_BOT_SECRET"):
        bad.append("缺 `WECOM_BOT_ID` / `WECOM_BOT_SECRET`")
    else:
        lines.append("· 凭据：`WECOM_BOT_ID` / `WECOM_BOT_SECRET` 已配置")

    ids = parse_list(get("WECOM_ALLOWED_USERIDS", ""))
    if not ids:
        if env_bool(get, "WECOM_ALLOW_ANYONE", False):
            lines.append("· 入口白名单：`WECOM_ALLOW_ANYONE=1`（**任何人都能指挥它**）")
        else:
            bad.append("`WECOM_ALLOWED_USERIDS` 为空 —— 桥会拒绝启动")
    else:
        names, unnamed = render_whitelist(ids, parse_labels(get("WECOM_USER_LABELS", "")))
        extra = f"（其中 {unnamed} 人没配显示名，只露了尾号）" if unnamed else ""
        lines.append(f"· 入口白名单：{len(ids)} 人 —— {'、'.join(names)}{extra}")
        lines.append("  ⚠️ 我**不认识你在这个平台的 userid**：切过去之前请你自己确认"
                     "上面有你在，否则就再也发不出「切回来」了。")

    return not bad, ([f"目标平台 `wecom` 过不了启动检查，**没有做任何改动**："]
                     + [f"  · {b}" for b in bad] + lines if bad else
                     ["`wecom` 预检通过（和真启动用的是同一套判据）："] + lines)