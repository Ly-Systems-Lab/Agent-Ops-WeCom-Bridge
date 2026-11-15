"""Slack 适配器的**离线**验证：@ 剥离 / 线程准入 / 去重键 / 分片 / 卡片形状。

不发一条真实消息、不连长连接 —— 只验"能用手上的判据证明它对"的那部分。
真机验收另一套（见 tools/slack_probe.py 与 notes/PLATFORM-ADAPTER-EVAL.md）。

用法： .venv/Scripts/python.exe -X utf8 tools/test_slack.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from platforms.slack.codec import normalize_mentions  # noqa: E402
from platforms.slack.codec import SlackCodecMixin  # noqa: E402
from platforms.slack.outbound import (  # noqa: E402
    SLACK_TOO_LONG, SlackBubble, SlackOutboundMixin, _split_chars, TEXT_MAX_CHARS,
)
from platforms.slack.render import SlackRenderMixin  # noqa: E402

FAIL = []
BOT = "U0TESTBOT01"


def check(name, cond, extra=""):
    print("  %s %s%s" % ("✅" if cond else "❌", name,
                         ("  " + extra) if extra else ""))
    if not cond:
        FAIL.append(name)


class FakeCodec(SlackCodecMixin):
    """只装 codec 需要的那几个成员，线程表放内存里（不碰 data/ 下的真文件）。"""

    def __init__(self):
        self.slack_bot_user_id = BOT
        self._shape_logged = True
        self.threads = set()
        self.cards = []
        # 断线积压过滤的基准：默认"还没连上" ⇒ 不过滤（老测试保持原行为）
        self._slack_connected_at = 0.0
        self._slack_skew = 0.0
        self._slack_backlog_dropped = 0

    def core_handle_card_action(self, action):
        self.cards.append(action)

    def _slack_thread_ok(self, channel, thread_ts):
        return (channel, thread_ts) in self.threads

    def _slack_mark_thread(self, channel, thread_ts):
        if channel and thread_ts:
            self.threads.add((channel, thread_ts))


class FakeOut(SlackOutboundMixin):
    def __init__(self):
        self.posts = []
        self._slack_thread_of = {}

    def slack_post(self, channel, text, blocks=None, thread_ts=None):
        self.posts.append({"channel": channel, "text": text,
                           "thread_ts": thread_ts})
        return {"ok": True, "ts": "1794000000.000001"}

    def slack_update(self, channel, ts, text, blocks=None):
        return {"ok": True}


class FakeRender(SlackRenderMixin):
    bot_name = "CC BOT"
    approval_wait_sec = 120
    user_labels = {BOT: "CEO"}

    def _status_pairs(self, chatid, chat_type, userid):
        return [("会话", "%s/%s" % (chatid, chat_type)), ("状态", "空闲")]


def ev(**kw):
    d = {"type": "message", "user": "U0TESTUSER01", "channel": "C0TESTCHAN01",
         "channel_type": "group", "text": "hi", "ts": "1794000001.000100"}
    d.update(kw)
    return d


def norm(codec, evt):
    return codec._slack_normalize({"event": evt})


def main():
    print("一、@ 剥离")
    t, p = normalize_mentions("<@%s> 你好" % BOT, BOT)
    check("剥掉 @机器人", t == "你好" and p, repr((t, p)))
    t, p = normalize_mentions("<@U0OTHERUSR01> 看这个", BOT)
    check("别人的 @ 降级成纯文本", t == "@U0OTHERUSR01 看这个" and not p, repr(t))
    t, p = normalize_mentions("<@%s|cc> hi" % BOT, BOT)
    check("带 |name 的形式也能剥", t == "hi", repr(t))
    t, p = normalize_mentions("没有 mention", BOT)
    check("没有 mention 时原样", t == "没有 mention" and not p)

    print("\n二、事件准入")
    c = FakeCodec()
    # 实测顺序：同一条顶层 @ 消息，`message` 事件**先到**、`app_mention` **后到**。
    # 前者没有 thread_ts ⇒ 走"频道顶层未 @"那条 ⇒ 拒；真正接住它的是后者。
    r0 = norm(c, ev(text="<@%s> 你好" % BOT))
    check("顶层 @ 的 message 事件先到 ⇒ 先拒（等 app_mention）", r0 is None)

    r = norm(c, ev(type="app_mention", text="<@%s> 你好" % BOT))
    check("频道顶层 @ 我 ⇒ 接", r is not None)
    check("  去重键 = channel:ts", r and r["dedupe_key"] == "C0TESTCHAN01:1794000001.000100")
    check("  reply_token = channel|ts",
          r and r["reply_token"] == "C0TESTCHAN01|1794000001.000100")
    check("  response_url 与之同源（/stop、/new、忙碌中回执全靠它）",
          r and r["response_url"] == r["reply_token"])
    check("  群聊 chat_type=2 且 raw_chatid 非空",
          r and r["chat_type"] == 2 and r["raw_chatid"] == "C0TESTCHAN01")
    check("  @ 已被剥掉、记进 at_prefix",
          r and r["text"] == "你好" and BOT in r["at_prefix"])
    check("  管辖线程已记下", ("C0TESTCHAN01", "1794000001.000100") in c.threads)

    # 线程里的 @：两个事件都会过准入 ⇒ 去重键**必须**相同
    c3 = FakeCodec()
    c3._slack_mark_thread("C0TESTCHAN01", "1794000001.000100")
    a = norm(c3, ev(type="message", text="<@%s> 线程里再 @ 一次" % BOT,
                    thread_ts="1794000001.000100"))
    b = norm(c3, ev(type="app_mention", text="<@%s> 线程里再 @ 一次" % BOT,
                    thread_ts="1794000001.000100"))
    check("线程内 @ 的两个事件都过准入", a is not None and b is not None)
    check("  去重键相同（用 channel:ts，不是 event_id）",
          a and b and a["dedupe_key"] == b["dedupe_key"])

    c2 = FakeCodec()
    r = norm(c2, ev(text="频道顶层闲聊"))
    check("频道顶层未 @ 我 ⇒ 不接", r is None)

    r = norm(c2, ev(text="线程里的回复", thread_ts="1794000001.000100"))
    check("未参与过的线程 ⇒ 不接", r is None)

    c2._slack_mark_thread("C0TESTCHAN01", "1794000001.000100")
    r = norm(c2, ev(text="线程里的回复", thread_ts="1794000001.000100"))
    check("参与过的线程 ⇒ 不 @ 也接", r is not None)
    check("  回复落在同一线程（reply_token 带 thread_ts）",
          r and r["reply_token"] == "C0TESTCHAN01|1794000001.000100")

    r = norm(c2, ev(text="广播到频道", thread_ts="1794000001.000100",
                    subtype="thread_broadcast"))
    check("thread_broadcast（同时发到频道）也接", r is not None)

    r = norm(c2, ev(channel="D0TESTDM0001", channel_type="im",
                    text="单聊消息", ts="1794000002.000200"))
    check("单聊 ⇒ 接，chat_type=1，raw_chatid 为空",
          r and r["chat_type"] == 1 and r["raw_chatid"] == "" and r["is_single"])

    print("\n三、必须挡住的东西")
    check("机器人自己发的（bot_id）⇒ 不接",
          norm(c2, ev(bot_id="B0CF", text="我自己说的")) is None)
    check("message_changed（编辑）⇒ 不接",
          norm(c2, ev(subtype="message_changed", text="改过的")) is None)
    check("非消息类事件 ⇒ 不接（实测 app_home_opened 也会推过来）",
          norm(c2, ev(type="app_home_opened", channel="D0TESTDM0001",
                      text="有正文也不能接")) is None)
    check("剥完 @ 正文为空 ⇒ 不接",
          norm(c2, ev(type="app_mention", text="<@%s>" % BOT)) is None)

    print("\n四、分片（按字符，且**逐字符**不许变）")
    parts = _split_chars("短文本", 3800)
    check("短文本不切", len(parts) == 1)

    body = ("中文段落测试" * 700) + "\n\n" + ("英文段落 " * 600)
    parts = _split_chars(body, 3800)
    check("长文本按 3800 字符切开", len(parts) > 1, "%d 片" % len(parts))
    check("每片都不超过上限", all(len(p) <= 3800 for p in parts),
          "最大 %d" % len(max(parts, key=len)))
    # ⚠️ 判据必须是**逐字符**。旧版本这里写成 `"".join(s.split())`（抹掉所有空白再比），
    # 恰好把"每段多补两个换行"整类差异容忍掉了 —— 2026-11-08 实发前才暴露。
    check("拼回去与原文**逐字符**相等", "".join(parts) == body,
          "%d vs %d" % (len("".join(parts)), len(body)))

    # 三个都超过上限的段落：旧实现在这里会切出**只含空行**的碎片片
    many = ("A" * 3900) + "\n\n" + ("B" * 3900) + "\n\n" + ("C" * 3900)
    mp = _split_chars(many, 3800)
    check("多段落长文本拼回逐字符相等", "".join(mp) == many,
          "%d vs %d" % (len("".join(mp)), len(many)))
    check("没有只含空白的片", all(p.strip() for p in mp),
          "空白片 %d 个" % sum(1 for p in mp if not p.strip()))
    check("片数不虚高（11,706 字符 ≤ 5 片）", len(mp) <= 5, "%d 片" % len(mp))

    # 单行超长：必须硬切，且不许把行尾吃掉
    longline = "x" * 9999
    lp = _split_chars(longline, 3800)
    check("单行超长：硬切且拼回相等", "".join(lp) == longline,
          "%d 片" % len(lp))

    print("\n五、气泡长度上限")
    o = FakeOut()
    b = SlackBubble(o, "C0TESTCHAN01", "1794000001.000100")
    r = b.send("x" * (TEXT_MAX_CHARS + 1), finish=False)
    check("超 4000 字符 ⇒ 返回 SLACK_TOO_LONG（让调用方改走分片）",
          r.get("errcode") == SLACK_TOO_LONG and not r.get("dead"))
    r = b.send("正常长度", finish=False)
    check("首帧建消息并记住 ts", r["sent"] and b.created and b.ts)
    check("  建消息用的是频道 ID，不是 ts", o.posts[0]["channel"] == "C0TESTCHAN01")
    r = b.send("定稿", finish=True)
    check("定稿后气泡关闭", r["sent"] and b.closed)
    r = b.send("再发", finish=False)
    check("关闭后再发 ⇒ dead（可安全改道）", r.get("dead"))

    print("\n六、卡片形状")
    rd = FakeRender()
    blocks = rd._status_card("C0TESTCHAN01", 2, BOT)
    kinds = [b["type"] for b in blocks]
    check("状态卡有 header/section/actions", "actions" in kinds and "section" in kinds)
    btns = blocks[-1]["elements"]
    check("三个按钮", len(btns) == 3)
    vals = [json.loads(x["value"]) for x in btns]
    check("按钮 value 带 key/chatid/chat_type",
          all({"key", "chatid", "chat_type"} <= set(v) for v in vals))

    ap = rd._approval_card({"id": "r1", "tool_name": "Bash",
                            "tool_input": {"command": "rm -rf /tmp/x"},
                            "target": "C0TESTCHAN01|2|U0C"})
    abtns = ap[-1]["elements"]
    avals = [json.loads(x["value"]) for x in abtns]
    check("审批卡两个按钮（允许/拒绝）",
          sorted(v["key"] for v in avals) == ["approve_no", "approve_ok"])
    check("  value 里带 task_id", all(v["task_id"] == "r1" for v in avals))

    print("\n七、网络异常不许抛穿（2026-11-07 断网事故的回归）")
    import requests
    import platforms.slack.transport as T
    from platforms.slack.transport import _slack_unsent, SlackTransportMixin

    check("ConnectTimeout ⇒ 确定没送到，可重试",
          _slack_unsent(requests.exceptions.ConnectTimeout()))
    check("ProxyError ⇒ 确定没送到（代理没起来）",
          _slack_unsent(requests.exceptions.ProxyError()))
    check("SSLError ⇒ 确定没送到（握手就断了）",
          _slack_unsent(requests.exceptions.SSLError()))
    check("ReadTimeout ⇒ **结果未知，不许重试**",
          not _slack_unsent(requests.exceptions.ReadTimeout()))
    check("裸 ConnectionError ⇒ 结果未知，不许重试（可能已送达）",
          not _slack_unsent(requests.exceptions.ConnectionError()))

    class FakeTr(SlackTransportMixin):
        def _slack_proxies(self):
            return None

    real_post = T.requests.post
    real_sleep = T.time.sleep
    calls = {"n": 0}

    def boom(exc):
        def _p(url, **kw):
            calls["n"] += 1
            raise exc
        return _p

    T.time.sleep = lambda s: None          # 测试里别真睡
    try:
        tr = FakeTr()
        calls["n"] = 0
        T.requests.post = boom(requests.exceptions.SSLError("handshake eof"))
        r = tr.slack_http("chat.update", "xoxb-test")
        check("握手失败 ⇒ 返回 dict 而不是抛", isinstance(r, dict) and not r.get("ok"),
              repr(r.get("error")))
        check("  标成「已送达未知 = False」，上层可安全重发",
              r.get("sent_unknown") is False)
        check("  确实重试过（1 次首发 + 4 次重试）", calls["n"] == 5,
              "%d 次" % calls["n"])
        check("  抖动窗口累计 15s（1+2+4+8）",
              sum(1.0 * (2 ** i) for i in range(4)) == 15.0)

        calls["n"] = 0
        T.requests.post = boom(requests.exceptions.ReadTimeout("read eof"))
        r = tr.slack_http("chat.postMessage", "xoxb-test")
        check("读超时 ⇒ 也返回 dict", isinstance(r, dict) and not r.get("ok"))
        check("  但**不重试**（重发会变两条消息）", calls["n"] == 1,
              "%d 次" % calls["n"])
        check("  标成「已送达未知 = True」", r.get("sent_unknown") is True)

        class OkResp:
            status_code = 200
            headers = {}

            def json(self):
                return {"ok": True, "ts": "1.2"}

        calls["n"] = 0

        def _ok(url, **kw):
            calls["n"] += 1
            return OkResp()

        T.requests.post = _ok
        r = tr.slack_http("chat.postMessage", "xoxb-test")
        check("正常时原样返回平台 dict", r.get("ok") and r.get("ts") == "1.2")
        check("  只调一次", calls["n"] == 1)
    finally:
        T.requests.post = real_post
        T.time.sleep = real_sleep

    print("\n八、断线积压：丢掉（用户 2026-11-13 选的那条）")
    T0 = 1794001000.0                       # 本次连接（hello）的时刻

    def evts(codec, ts, **kw):
        return norm(codec, ev(type="app_mention", text="<@%s> 说话" % BOT, ts=ts, **kw))

    c8 = FakeCodec()
    c8._slack_connected_at = 0.0
    check("还没连上过（基准=0）⇒ 不过滤，一切照收",
          evts(c8, "1794000001.000100") is not None)

    c8._slack_connected_at = T0
    check("比本次连接早 600 秒 ⇒ 丢掉", evts(c8, "%.6f" % (T0 - 600)) is None)
    check("比本次连接早 30 秒 ⇒ 也丢（那就是断线期间说的）",
          evts(c8, "%.6f" % (T0 - 30)) is None)
    check("窗口内（早 3 秒）⇒ 照收，宁可不误伤",
          evts(c8, "%.6f" % (T0 - 3)) is not None)
    check("连上之后的新消息 ⇒ 照收", evts(c8, "%.6f" % (T0 + 1)) is not None)
    check("  丢弃有记账（可自证生效了几次）", c8._slack_backlog_dropped == 2,
          "%d 次" % c8._slack_backlog_dropped)

    c8._slack_mark_thread("C0TESTCHAN01", "1794000001.000100")
    before = len(c8.threads)
    evts(c8, "%.6f" % (T0 - 600), thread_ts="1794001999.000900")
    check("积压的 @ 不许登记线程（否则以后在那个线程里说什么都收）",
          len(c8.threads) == before)

    check("ts 解不出来 ⇒ 放行（不敢拿坏值拦真消息）",
          evts(c8, "not-a-ts") is not None)

    # 时钟偏移：本机比平台快 60 秒。不修正的话，**正常**消息会被当成积压丢掉
    c9 = FakeCodec()
    c9._slack_connected_at = T0
    c9._slack_skew = 60.0                    # 本机 - 平台 = +60s
    check("本机快 60 秒时，平台的『刚才』消息仍然照收（偏移已修正）",
          evts(c9, "%.6f" % (T0 - 60)) is not None)
    check("  但真的积压（早 600 秒）照样丢",
          evts(c9, "%.6f" % (T0 - 600)) is None)

    # 关掉过滤（排障口）
    os.environ["CC_SLACK_BACKLOG_GRACE_SEC"] = "0"
    try:
        check("GRACE=0 ⇒ 过滤关闭，积压也照收",
              evts(c8, "%.6f" % (T0 - 600)) is not None)
        os.environ["CC_SLACK_BACKLOG_GRACE_SEC"] = "-5"
        check("GRACE<0 ⇒ 同样关闭", evts(c8, "%.6f" % (T0 - 600)) is not None)
    finally:
        os.environ.pop("CC_SLACK_BACKLOG_GRACE_SEC", None)

    # 按钮回调：断线期间点的"允许/拒绝"重连后补上来，卡片早已作废
    def iact(codec, ts):
        codec._slack_on_interactive({
            "actions": [{"action_ts": ts,
                         "value": json.dumps({"key": "approve_ok", "task_id": "t1",
                                              "chatid": "C0TESTCHAN01",
                                              "chat_type": 2})}],
            "user": {"id": "U0TESTUSER01"},
            "channel": {"id": "C0TESTCHAN01"},
            "container": {"message_ts": "1794000001.000100"},
        })

    iact(c8, "%.6f" % (T0 - 600))
    check("断线期间点的按钮 ⇒ 不执行", not c8.cards)
    iact(c8, "%.6f" % (T0 - 1))
    check("连着的时候点的按钮 ⇒ 照常执行", len(c8.cards) == 1)

    print("\n九、时钟偏移的量法（拿 HTTP Date 头，不用本机时钟硬当基准）")
    import time as _t
    from email.utils import formatdate
    from platforms.slack.transport import SlackTransportMixin as _STM

    class SkewObj:
        _slack_skew = 0.0
        _slack_note_skew = _STM._slack_note_skew

    s = SkewObj()
    s._slack_note_skew(formatdate(usegmt=True))
    check("正常 Date 头 ⇒ 偏移接近 0", abs(s._slack_skew) < 5, "%.2fs" % s._slack_skew)
    s2 = SkewObj()
    s2._slack_skew = 7.5
    s2._slack_note_skew("garbage")
    check("坏头 ⇒ 保持原值（不把基准弄坏）", s2._slack_skew == 7.5)
    s2._slack_note_skew(None)
    check("没有 Date 头 ⇒ 同样不动", s2._slack_skew == 7.5)
    s3 = SkewObj()
    s3._slack_note_skew(formatdate(_t.time() - 30 * 86400, usegmt=True))
    check("差 30 天 ⇒ 判定坏头，忽略", s3._slack_skew == 0.0)

    if FAIL:
        print("\n❌ 失败 %d 项：%s" % (len(FAIL), ", ".join(FAIL)))
        return 1
    print("\n✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())