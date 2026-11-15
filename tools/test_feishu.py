"""飞书适配器的**离线**验证：解析 / 渲染 / 分片 / 降级 / 卡片回调闭环。

不发一条真实消息、不连长连接 —— 只验"我们能用手上的判据证明它对"的那部分。
真机验收另有一套（见 notes/PLATFORM-ADAPTER-EVAL.md §十 与 tools/feishu_*.py）。

用法： .venv/Scripts/python.exe -X utf8 tools/test_feishu.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

import bridge  # noqa: E402
from platforms.feishu.codec import strip_mentions  # noqa: E402
from platforms.feishu.outbound import FEISHU_TOO_LONG  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


class NS:
    """一个能当对象用的 dict（构造假事件用）。"""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FakeRunner:
    timeout = 60
    perm_mode = "dontAsk"

    def kill_current(self):
        return True

    def clear_stop(self):
        pass

    def run(self, *a, **kw):        # pragma: no cover - 离线测试不该跑到这里
        raise AssertionError("离线测试不该真跑 claude")


def mk_entry(chatid, ts, ct=2, rid="x"):
    """一条审批记录的形状（与 `_approval_scan_once` 里登记的一致）。"""
    return {"chatid": chatid, "chat_type": ct, "userid": "ou_ceo", "ts": ts,
            "tool": "Bash", "shown": False, "resolved": False,
            "req": {"id": rid, "tool_name": "Bash", "tool_input": {},
                    "target": f"{chatid}|{ct}|ou_ceo"}}


def mk_bridge(allow="ou_test_1"):
    tmp = tempfile.mkdtemp(prefix="ccbridge-feishu-")
    store = bridge.Store(os.path.join(tmp, "s.db"))
    b = bridge.FeishuBridge("cli_test", "secret", allow, "", "CC BOT",
                            store, FakeRunner(), tmp, 18000, 20, 0)
    b.feishu_chunk_bytes = 300       # 小一点，方便验分片
    return b, tmp


# ---------------- 解析 ----------------

def test_strip_mentions():
    print("\n== @ 剥离：飞书是占位符，不是企微的 <@id> ==")
    m = [NS(key="@_user_1", name="CC BOT")]
    text, prefix = strip_mentions("@_user_1 你好", m)
    check("占位符被剥掉", text == "你好", repr(text))
    check("剥掉的内容记进 at_prefix", "CC BOT" in prefix, repr(prefix))

    # 企微那条正则的思路在飞书正文里**一个字符都匹配不上**，这里必须靠 mentions
    text2, _ = strip_mentions("没有 at 的正文", [])
    check("没有 @ 时原样返回", text2 == "没有 at 的正文")

    text3, _ = strip_mentions("@_user_1 @_user_2 两个都 @", [
        NS(key="@_user_1", name="A"), NS(key="@_user_2", name="B")])
    check("多个 @ 都剥掉", text3 == "两个都 @", repr(text3))


def _msg(chat_type="p2p", text="hi", mid="om_1", sender_type="user",
         mentions=(), open_id="ou_ceo"):
    content = json.dumps({"text": text}, ensure_ascii=False)
    return NS(message=NS(message_id=mid, chat_id="oc_chat_1", chat_type=chat_type,
                         message_type="text", content=content, mentions=list(mentions)),
              sender=NS(sender_type=sender_type,
                        sender_id=NS(open_id=open_id)))


def test_normalize():
    print("\n== 规范化：单聊用 open_id 当 chatid（与企微同构）==")
    b, _ = mk_bridge()

    inc = b._feishu_normalize(_msg())
    check("单聊 chat_type=1", inc["chat_type"] == 1)
    check("单聊 chatid = open_id", inc["chatid"] == "ou_ceo", inc["chatid"])
    check("单聊 raw_chatid 留空（群白名单不该误杀单聊）", inc["raw_chatid"] == "")
    check("去重键是 message_id", inc["dedupe_field"] == "message_id"
          and inc["dedupe_key"] == "om_1")
    check("reply_token = message_id（气泡回在这条消息下面）",
          inc["reply_token"] == "om_1")

    grp = b._feishu_normalize(_msg(chat_type="group",
                                   mentions=[NS(key="@_user_1", name="CC")]))
    check("群聊 chat_type=2", grp["chat_type"] == 2)
    check("群聊 chatid = 真实 chat_id", grp["chatid"] == "oc_chat_1")
    check("群聊 raw_chatid 保留（供群白名单）", grp["raw_chatid"] == "oc_chat_1")

    check("群里没 @ 我 ⇒ 不处理",
          b._feishu_normalize(_msg(chat_type="group", mentions=[])) is None)
    check("机器人自己发的 ⇒ 不处理",
          b._feishu_normalize(_msg(sender_type="app")) is None)


# ---------------- 渲染 ----------------

def test_render():
    print("\n== 卡片渲染：按钮 value 是回调的唯一身份来源 ==")
    b, _ = mk_bridge()

    card = b._status_card("ou_ceo", 1, "ou_ceo")
    buttons = [e for e in card["body"]["elements"] if e.get("tag") == "button"]
    check("状态卡三个按钮", len(buttons) == 3, f"{len(buttons)} 个")
    vals = [bt["behaviors"][0]["value"] for bt in buttons]
    check("按钮带 key", [v["key"] for v in vals]
          == ["status_refresh", "status_clear", "status_stop"])
    check("按钮带 chatid/chat_type（飞书回调不告诉我们会话类型）",
          all(v["chatid"] == "ou_ceo" and v["chat_type"] == 1 for v in vals))

    req = {"id": "rid-1", "tool_name": "Bash",
           "tool_input": {"command": "rm -rf build"},
           "cwd": "C:\\work", "target": "oc_group_9|2|ou_ceo"}
    ap = b._approval_card(req)
    abtns = [e for e in ap["body"]["elements"] if e.get("tag") == "button"]
    check("审批卡两个按钮", len(abtns) == 2)
    avals = [bt["behaviors"][0]["value"] for bt in abtns]
    check("审批按钮 key", [v["key"] for v in avals] == ["approve_ok", "approve_no"])
    check("审批按钮带 task_id", all(v["task_id"] == "rid-1" for v in avals))
    check("审批按钮的 chatid/chat_type 取自 req.target",
          all(v["chatid"] == "oc_group_9" and v["chat_type"] == 2 for v in avals))

    done = b._approval_resolved_card(req, True, "ou_ceo")
    check("已处理卡**没有按钮**（留着就等于留个点了没用的入口）",
          not [e for e in done["body"]["elements"] if e.get("tag") == "button"])
    check("已处理卡不暴露 open_id 全串",
          "ou_ceo" not in json.dumps(done, ensure_ascii=False))


# ---------------- 分片 / 降级 ----------------

def test_chunks():
    print("\n== 分片：先整条，只有 230025 才切 ==")
    b, _ = mk_bridge()
    calls = []

    def fake_send(chatid, chat_type, text, wait=True):
        calls.append(len(text.encode("utf-8")))
        # 第 1 次（整条）超长，后面的分片都成功
        if len(calls) == 1:
            return {"sent": False, "errcode": FEISHU_TOO_LONG, "errmsg": "too long"}
        return {"sent": True, "errcode": 0, "errmsg": ""}

    b.send_text = fake_send
    text = "字" * 500            # 1500 字节 > 300 的粒度
    failed = b.send_chunks("oc_1", 2, text)
    check("超长被切开且全部送达", failed == [], f"发了 {len(calls)} 次")
    check("第一次是整条（不预切）", calls[0] == 1500, f"{calls[0]}B")
    check("分片都不超过粒度+前缀", all(c <= 300 + 40 for c in calls[1:]),
          f"最长 {max(calls[1:])}B")

    # 结果未知：**不许重发**
    calls2 = []

    def unknown(chatid, chat_type, text, wait=True):
        calls2.append(text)
        return {"sent": False, "errcode": None, "errmsg": "timeout"}

    b2, _ = mk_bridge()
    b2.send_text = unknown
    failed2 = b2.send_chunks("oc_1", 2, "hello")
    check("结果未知 ⇒ 不判失败、不重发", len(calls2) == 2 and failed2,
          f"发了 {len(calls2)} 次（第 2 次是'结果未知'提示）")
    check("提示里说清楚'别急着重发'", "别急着重发" in calls2[1])


def test_deliver_fallback():
    print("\n== 收口降级：气泡不行就整条改走文本（内容绝不能丢）==")

    class Dead:
        def send(self, content, finish):
            return {"sent": False, "errcode": None, "errmsg": "closed", "dead": True}

    class TooLong:
        def __init__(self):
            self.closed_with = None

        def send(self, content, finish):
            if finish and len(content) > 100:
                return {"sent": False, "errcode": FEISHU_TOO_LONG, "errmsg": "too long"}
            if finish:
                self.closed_with = content
                return {"sent": True, "errcode": 0, "errmsg": ""}
            return {"sent": True, "errcode": 0, "errmsg": ""}

    b, _ = mk_bridge()
    sent = []
    b.send_text = lambda cid, ct, t, wait=True: (
        sent.append(t) or {"sent": True, "errcode": 0, "errmsg": ""})

    b._deliver(Dead(), "oc_1", 2, "正文")
    check("气泡不可用 ⇒ 正文改走主动推送", sent == ["正文"], repr(sent))

    tl = TooLong()
    sent.clear()
    b._deliver(tl, "oc_1", 2, "长" * 300)
    check("太长 ⇒ 先把气泡收口（别停在'处理中'）",
          (tl.closed_with or "").startswith("✅"), repr(tl.closed_with))
    check("太长 ⇒ 正文仍完整送达", sent and "长" * 300 in sent[0])

    # 定稿成功就不该再发第二条
    class Ok:
        def send(self, content, finish):
            return {"sent": True, "errcode": 0, "errmsg": ""}

    sent.clear()
    b._deliver(Ok(), "oc_1", 2, "正常回答")
    check("气泡成功 ⇒ 不再另发", sent == [], repr(sent))


def test_card_callback():
    print("\n== 卡片回调闭环：点『允许』⇒ 响应里带回新卡（按钮当场消失）==")
    b, tmp = mk_bridge(allow="ou_ceo")
    b.approval_dir = tmp
    rid = "rid-xyz"
    b._approvals[rid] = {"chatid": "ou_ceo", "chat_type": 1, "userid": "ou_ceo",
                         "ts": time.time(), "tool": "Bash",
                         "req": {"id": rid, "tool_name": "Bash",
                                 "tool_input": {"command": "echo hi"},
                                 "cwd": tmp, "target": f"ou_ceo|1|ou_ceo"}}

    data = NS(event=NS(
        operator=NS(open_id="ou_ceo"),
        context=NS(open_chat_id="oc_1", open_message_id="om_card"),
        token="tok",
        action=NS(value={"key": "approve_ok", "task_id": rid,
                         "chatid": "ou_ceo", "chat_type": 1})))

    resp = b._feishu_on_card_action(data)

    check("响应里带了新卡片（这就是'就地改卡'的唯一通道）", resp.card is not None)
    if resp.card is not None:
        body = json.dumps(resp.card.data, ensure_ascii=False)
        check("新卡里没有按钮", '"tag": "button"' not in body)
        check("新卡写着'已允许'", "已允许" in body)
    check("响应里带了 toast（点完立刻有反馈）", resp.toast is not None)
    check("决定已落盘（hook 才读得到）",
          os.path.exists(os.path.join(tmp, rid + ".decision"))
          and open(os.path.join(tmp, rid + ".decision"), encoding="utf-8").read().strip()
          == "allow")
    # ⚠️ 是「标记」不是「删除」：删了的话，只要 .req.json 还没被 hook 删掉，
    # 下一轮扫描就会把同一个 rid 当新请求再发一张卡（真机踩到过）。
    check("审批记录被标记为已处理",
          (b._approvals.get(rid) or {}).get("resolved") is True)

    # 非白名单用户点 ⇒ 什么都不做
    rid2 = "rid-2"
    b._approvals[rid2] = {"chatid": "ou_x", "chat_type": 1, "userid": "ou_x",
                          "ts": time.time(), "tool": "Bash",
                          "req": {"id": rid2, "target": "ou_x|1|ou_x"}}
    data2 = NS(event=NS(operator=NS(open_id="ou_stranger"),
                        context=NS(open_chat_id="oc_1", open_message_id="om2"),
                        token="t", action=NS(value={"key": "approve_ok",
                                                    "task_id": rid2,
                                                    "chatid": "ou_x",
                                                    "chat_type": 1})))
    resp2 = b._feishu_on_card_action(data2)
    check("非白名单点击 ⇒ 不改卡、不落决定",
          resp2.card is None and rid2 in b._approvals
          and not os.path.exists(os.path.join(tmp, rid2 + ".decision")))


def test_approval_queue():
    print("\n== 审批卡排队：同一会话同时只上一张 ==")
    b, _ = mk_bridge()
    sent = []

    def fake_card(chatid, chat_type, card, wait=True):
        sent.append((chatid, card))
        return {"sent": True, "errcode": 0, "errmsg": ""}

    b.plat_send_card = fake_card

    b._approvals = {"r1": mk_entry("oc_1", 1.0, rid="r1"),
                    "r2": mk_entry("oc_1", 2.0, rid="r2")}
    b._approval_show_next()
    check("同一会话只发队首那一张", len(sent) == 1 and sent[0][0] == "oc_1",
          f"{len(sent)} 张")
    b._approval_show_next()
    check("前一张没处理完 ⇒ 第二张不上屏", len(sent) == 1, f"{len(sent)} 张")

    b._approvals.pop("r1")          # 模拟用户点掉了第一张
    b._approval_show_next()
    check("前一张处理后 ⇒ 下一张才上屏",
          len(sent) == 2 and sent[1][0] == "oc_1", f"{len(sent)} 张")

    # 另一个会话有自己的排队，不被 oc_1 卡住
    b._approvals["r3"] = mk_entry("oc_2", 3.0, rid="r3")
    b._approval_show_next()
    check("别的会话照样能上自己那一张",
          len(sent) == 3 and sent[2][0] == "oc_2", f"{len(sent)} 张")


def test_approval_no_duplicate():
    print("\n== 审批去重：点完之后请求文件还在时，不能重发第二张卡 ==")
    b, tmp = mk_bridge()
    b.approval_dir = tempfile.mkdtemp(prefix="ccbridge-appr-")
    sent = []

    def fake_card(chatid, chat_type, card, wait=True):
        sent.append(chatid)
        return {"sent": True, "errcode": 0, "errmsg": ""}

    b.plat_send_card = fake_card
    rid = "abc123def456"
    req = {"id": rid, "tool_name": "Bash", "tool_input": {},
           "target": "oc_1|2|ou_ceo", "cwd": tmp}
    path = os.path.join(b.approval_dir, rid + ".req.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(req, ensure_ascii=False))

    b._approval_scan_once()
    check("扫描到请求 ⇒ 发一张卡", len(sent) == 1, f"{len(sent)} 张")

    # 模拟用户点了「允许」：核心写决定 + 标记已处理
    b._approval_decide(rid, True)
    b._approval_resolve(rid)
    # 此刻 hook 还没醒（0.2 秒轮询）⇒ `.req.json` 仍在，这是关键的时序
    b._approval_scan_once()
    check("请求文件还在 ⇒ **不重复发卡**", len(sent) == 1, f"{len(sent)} 张")
    check("记录被标记而非删除", rid in b._approvals
          and b._approvals[rid].get("resolved") is True)

    # hook 读到决定后删掉请求文件，记录这时才该消失
    os.remove(path)
    b._approval_scan_once()
    check("请求文件消失 ⇒ 记录清理", rid not in b._approvals)


def test_deny_stops():
    print("\n== 拒绝 = 终止本轮（不再弹下一张）==")
    b, tmp = mk_bridge()
    b.approval_dir = tempfile.mkdtemp(prefix="ccbridge-deny-")
    b._approvals = {"r1": mk_entry("oc_1", 1.0, rid="r1"),
                    "r2": mk_entry("oc_1", 2.0, rid="r2")}
    for r in ("r1", "r2"):
        for suf in (".req.json", ".seen"):
            open(os.path.join(b.approval_dir, r + suf), "w").close()
    b._approval_resolve("r1")        # 用户点的这一张，核心已标记为处理过

    b._deny_and_stop("oc_1", 2)

    dec2 = os.path.join(b.approval_dir, "r2.decision")
    check("同会话排队的那张也落 deny",
          os.path.exists(dec2) and open(dec2, encoding="utf-8").read().strip() == "deny")
    check("排队请求的文件被清掉（否则下一轮扫描会再弹一张）",
          not os.path.exists(os.path.join(b.approval_dir, "r2.req.json")))
    check("标记本轮为「被拒绝终止」",
          b._stopped_by_deny.get(("oc_1", 2)) is True)

    # 收尾措辞：不能报成"已按 /stop 停止"
    sent = []
    b.send_text = lambda cid, ct, t, wait=True: (
        sent.append(t) or {"sent": True, "errcode": 0, "errmsg": ""})
    b._report({"chatid": "oc_1", "chat_type": 2, "userid": "ou_ceo", "epoch": 0},
              {"stopped": True, "timed_out": False, "is_error": False,
               "subtype": "", "rc": 0, "text": "", "denials": []}, tmp, None)
    check("收尾说「已拒绝」而不是 /stop",
          bool(sent) and "已拒绝" in sent[0] and "/stop" not in sent[0],
          (sent[0][:30] if sent else "(没发)"))
    check("用完就清标记（不会污染下一轮）", not b._stopped_by_deny)


def test_deny_does_not_mute_chat():
    print("\n== 拒绝之后这个会话必须还能弹新卡（2026-11-05 真机 bug 回归）==")
    b, _ = mk_bridge()
    b.approval_dir = tempfile.mkdtemp(prefix="ccbridge-mute-")
    sent = []

    def fake_card(chatid, chat_type, card, wait=True):
        sent.append(chatid)
        return {"sent": True, "errcode": 0, "errmsg": ""}

    b.plat_send_card = fake_card

    # —— 现场：第一张已经上屏、用户点了「拒绝」，而 `.req.json` 还在磁盘上
    #    （进程树被强杀，hook 来不及删它）。记录因此是 shown=True / resolved=True。
    old = mk_entry("oc_g", 1.0, rid="old1")
    old["shown"] = True
    b._approvals = {"old1": old}
    stale = os.path.join(b.approval_dir, "old1.req.json")
    with open(stale, "w", encoding="utf-8") as f:
        f.write(json.dumps(old["req"]))
    b._approval_decide("old1", False)
    b._approval_resolve("old1")

    # —— 同一会话来了新请求 ——
    new = mk_entry("oc_g", 2.0, rid="new1")
    with open(os.path.join(b.approval_dir, "new1.req.json"), "w",
              encoding="utf-8") as f:
        f.write(json.dumps(new["req"]))

    b._approval_scan_once()
    check("同会话的新卡能上屏（修前：这个群被永久静音、一张都不发）",
          sent == ["oc_g"], f"发了 {len(sent)} 张")

    # —— 拒绝时要把「刚被点掉那张」的残留文件一并清掉 ——
    # 否则桥重启后它会被当成新请求，给一个早就结束的任务弹幽灵卡。
    b._deny_and_stop("oc_g", 2)
    check("拒绝后清掉已点掉那张的请求文件", not os.path.exists(stale))

    # —— 启动清理：上次运行留下的死请求不能复活 ——
    b2, _ = mk_bridge()
    b2.approval_dir = tempfile.mkdtemp(prefix="ccbridge-sweep-")
    for suf in (".req.json", ".req.json.tmp", ".seen", ".decision"):
        open(os.path.join(b2.approval_dir, "dead1" + suf), "w").close()
    b2._approval_sweep()
    check("启动清理删掉残留的请求/回执/决定",
          not any(n.startswith("dead1.") for n in os.listdir(b2.approval_dir)))


def test_approval_dir():
    print("\n== 审批决策目录：两个平台必须分开 ==")
    a = bridge.default_approval_dir("wecom")
    b = bridge.default_approval_dir("feishu")
    check("企微保持老路径（不破坏在跑的实例）",
          a.replace("\\", "/").endswith("/data/approvals"), a)
    check("飞书用独立目录", b.replace("\\", "/").endswith("/data/approvals-feishu"), b)
    # 共用会让对方把请求毙掉（2026-11-05 实测：卡片发出了、决定却是 deny）
    check("两者不是同一个目录", os.path.normcase(a) != os.path.normcase(b))


def test_threads():
    print("\n== 常驻线程清单：漏一个就是「机器人装死」==")
    b, _ = mk_bridge()
    names = [n for n, _ in b._feishu_thread_specs()]
    # 漏了 cc-worker：任务入队但没人消费 ⇒ 之后每条消息都回"忙碌中"（真机踩过）
    check("有队列消费者 cc-worker", "cc-worker" in names, str(names))
    check("有入站分发 feishu-inbound", "feishu-inbound" in names)
    check("有被动回复 reply-worker", "reply-worker" in names)
    check("没开审批 ⇒ 不起 approval-loop", "approval-loop" not in names)
    b.approval_dir = tempfile.mkdtemp(prefix="ccbridge-appr-")
    names2 = [n for n, _ in b._feishu_thread_specs()]
    check("开了审批 ⇒ 起 approval-loop", "approval-loop" in names2, str(names2))


def test_rate_limit():
    print("\n== 限流：同一秒内不会放行超过 5 条 ==")
    b, _ = mk_bridge()
    t0 = time.time()
    for _ in range(6):
        b._rate_wait()
    dt = time.time() - t0
    check("第 6 条被推迟（≥0.15s）", dt >= 0.15, f"{dt:.2f}s")
    check("窗口最多 5 条", len(b._rate_events) <= 5, f"{len(b._rate_events)} 条")


def test_gate():
    print("\n== 启动闸门：飞书的报错文案要指向 open_id ==")
    msg = bridge.whitelist_gate("", False, key="FEISHU_ALLOWED_OPENIDS",
                               what="open_id", anyone_key="FEISHU_ALLOW_ANYONE")
    check("空白名单 ⇒ 拒绝启动", msg is not None)
    check("文案里点名 FEISHU_ALLOWED_OPENIDS", "FEISHU_ALLOWED_OPENIDS" in msg)
    check("文案里说 open_id", "open_id" in msg)
    check("显式开关能放行",
          bridge.whitelist_gate("", True, key="FEISHU_ALLOWED_OPENIDS") is None)


def main():
    test_strip_mentions()
    test_normalize()
    test_render()
    test_chunks()
    test_deliver_fallback()
    test_card_callback()
    test_approval_queue()
    test_approval_no_duplicate()
    test_deny_stops()
    test_deny_does_not_mute_chat()
    test_approval_dir()
    test_threads()
    test_rate_limit()
    test_gate()
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项未通过：" + ", ".join(FAIL))
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())