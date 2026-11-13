"""Slack 的「切换前预检」。理由与口径同 `platforms/wecom/preflight.py`。

三条 Slack 独有的：
  · **两个 token 来源不同**：`SLACK_BOT_TOKEN`（`xoxb-`）发消息，
    `SLACK_APP_TOKEN`（`xapp-`）建 Socket Mode。拿一个顶两个是最常见的错，
    而它只会在连不上时才暴露 ⇒ 在这里按前缀先挡一次；
  · 白名单口径是 **member ID（`U…`）**，不是 @handle（handle 能改、能被抢）；
  · 国内**不必**走代理（2026-11-08 实测推翻过这条），所以预检不管代理。
"""

from common import env_bool, parse_labels, parse_list, render_whitelist


def preflight(get, importable):
    lines, bad = [], []
    bot = get("SLACK_BOT_TOKEN", "")
    app = get("SLACK_APP_TOKEN", "")

    if not bot or not app:
        bad.append("缺 `SLACK_BOT_TOKEN` / `SLACK_APP_TOKEN`")
    else:
        if not bot.startswith("xoxb-"):
            bad.append("`SLACK_BOT_TOKEN` 不像 bot token（应以 `xoxb-` 开头）")
        if not app.startswith("xapp-"):
            bad.append("`SLACK_APP_TOKEN` 不像 app-level token（应以 `xapp-` 开头）")
        if not bad:
            lines.append("· 凭据：两个 token 都在，且类型对得上")

    ids = parse_list(get("SLACK_ALLOWED_USERIDS", ""))
    if not ids:
        if env_bool(get, "SLACK_ALLOW_ANYONE", False):
            lines.append("· 入口白名单：`SLACK_ALLOW_ANYONE=1`（**任何人都能指挥它**）")
        else:
            bad.append("`SLACK_ALLOWED_USERIDS` 为空 —— 桥会拒绝启动")
    else:
        names, unnamed = render_whitelist(ids, parse_labels(get("SLACK_USER_LABELS", "")))
        extra = f"（其中 {unnamed} 人没配显示名，只露了尾号）" if unnamed else ""
        lines.append(f"· 入口白名单：{len(ids)} 人 —— {'、'.join(names)}{extra}")
        lines.append("  ⚠️ 我**不认识你在这个平台的 member ID**：切过去之前请你自己确认"
                     "上面有你在，否则就再也发不出「切回来」了。")

    return not bad, ([f"目标平台 `slack` 过不了启动检查，**没有做任何改动**："]
                     + [f"  · {b}" for b in bad] + lines if bad else
                     ["`slack` 预检通过（和真启动用的是同一套判据）："] + lines)