"""飞书的「切换前预检」。理由与口径同 `platforms/wecom/preflight.py`。

两条飞书独有的：
  · 白名单是 **open_id**（应用内唯一）—— 换一个飞书应用就要重填；
  · 依赖 `lark-oapi` **必须**装，缺了 `main()` 直接 return 2。
"""

from common import env_bool, parse_labels, parse_list, render_whitelist


def preflight(get, importable):
    lines, bad = [], []

    if not get("FEISHU_APP_ID") or not get("FEISHU_APP_SECRET"):
        bad.append("缺 `FEISHU_APP_ID` / `FEISHU_APP_SECRET`")
    else:
        lines.append("· 凭据：`FEISHU_APP_ID` / `FEISHU_APP_SECRET` 已配置")

    if not importable("lark_oapi"):
        bad.append("没装依赖 `lark-oapi`（飞书适配器要用它）")
    else:
        lines.append("· 依赖：`lark-oapi` 已装")

    ids = parse_list(get("FEISHU_ALLOWED_OPENIDS", ""))
    if not ids:
        if env_bool(get, "FEISHU_ALLOW_ANYONE", False):
            lines.append("· 入口白名单：`FEISHU_ALLOW_ANYONE=1`（**任何人都能指挥它**）")
        else:
            bad.append("`FEISHU_ALLOWED_OPENIDS` 为空 —— 桥会拒绝启动")
    else:
        names, unnamed = render_whitelist(ids, parse_labels(get("FEISHU_USER_LABELS", "")))
        extra = f"（其中 {unnamed} 人没配显示名，只露了尾号）" if unnamed else ""
        lines.append(f"· 入口白名单：{len(ids)} 人 —— {'、'.join(names)}{extra}")
        lines.append("  ⚠️ 我**不认识你在这个平台的 open_id**：切过去之前请你自己确认"
                     "上面有你在，否则就再也发不出「切回来」了。")

    return not bad, ([f"目标平台 `feishu` 过不了启动检查，**没有做任何改动**："]
                     + [f"  · {b}" for b in bad] + lines if bad else
                     ["`feishu` 预检通过（和真启动用的是同一套判据）："] + lines)