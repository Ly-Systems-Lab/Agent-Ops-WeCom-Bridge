"""平台热切换（真源 / 预检 / 控制目录 / 看守回滚）的单元验证。

**不联网、不起真进程、不动正在跑的桥**：
  · 真起进程会杀掉正在跑的任务 —— 包括"正在跟用户说话"的那个会话；
  · 所以这里的 spawn / wait_ready 全是假的，只验证判据本身。

用法： .venv/Scripts/python.exe -X utf8 tools/test_platform_switch.py
"""

import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from core.control import ControlInbox, submit, control_dir          # noqa: E402
from core.platform_store import PlatformStore, valid_platform       # noqa: E402
from core.platform_switch import Switcher, preflight, platform_list_text  # noqa: E402
from core.runtime_file import read_runtime, write_runtime           # noqa: E402
from platforms.feishu.preflight import preflight as feishu_pre     # noqa: E402
from platforms.slack.preflight import preflight as slack_pre       # noqa: E402
from platforms.wecom.preflight import preflight as wecom_pre       # noqa: E402

PREFLIGHTS = {"wecom": wecom_pre, "feishu": feishu_pre, "slack": slack_pre}

FAIL = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def tmpdir():
    return tempfile.mkdtemp(prefix="plsw-")


ENV_OK = {
    "WECOM_BOT_ID": "bot", "WECOM_BOT_SECRET": "sec",
    "WECOM_ALLOWED_USERIDS": "u1,u2,u3",
    "FEISHU_APP_ID": "cli_x", "FEISHU_APP_SECRET": "s",
    "FEISHU_ALLOWED_OPENIDS": "ou_aaaa1111,ou_bbbb2222",
    "SLACK_BOT_TOKEN": "xoxb-1", "SLACK_APP_TOKEN": "xapp-1",
    "SLACK_ALLOWED_USERIDS": "U1",
}


def get_of(d):
    return lambda k, default="": d.get(k, default)


# ---------------- 1. 名字校验 ----------------

def test_names():
    print("· 平台名的结构校验")
    for p in ("wecom", "feishu", "slack"):
        check(f"接受 {p}", valid_platform(p)[0])
    for bad, why in (("", "空"), ("  wecom", "首尾空白"), ("dingtalk", "不认识"),
                     ("WECom", "大小写不等价"), (None, "非字符串")):
        ok, reason = valid_platform(bad)
        check(f"拒绝 {why}", not ok, reason)


# ---------------- 2. 真源 ----------------

def test_store_seed():
    print("· 首次启动：用 `.env` 的种子初始化真源")
    d = tmpdir()
    p = os.path.join(d, "data", "platform.json")
    st = PlatformStore(p, seed="feishu")
    check("文件被建出来", os.path.exists(p))
    check("current = 种子", st.current() == "feishu", st.current())
    raw = json.load(open(p, encoding="utf-8"))
    check("last_good = 种子", raw.get("last_good") == "feishu")
    check("没有 .tmp 残留", not os.path.exists(p + ".tmp"))


def test_store_no_overwrite():
    print("· 重启不覆盖 `/platform` 选的值")
    d = tmpdir()
    p = os.path.join(d, "platform.json")
    PlatformStore(p, seed="wecom").set("feishu", actor="u1")
    st2 = PlatformStore(p, seed="wecom")          # 模拟重启
    check("重启后仍是 feishu", st2.current() == "feishu", st2.current())


def test_store_fallback():
    print("· 文件坏了的三层兜底")
    d = tmpdir()
    p = os.path.join(d, "platform.json")

    with open(p, "w", encoding="utf-8") as f:
        f.write('{"current": "dingtalk", "last_good": "slack"}')
    st = PlatformStore(p, seed="wecom")
    check("current 不认识 → 用 last_good", st.current() == "slack", st.current())
    check("坏值已被改回可用值",
          json.load(open(p, encoding="utf-8"))["current"] == "slack")

    with open(p, "w", encoding="utf-8") as f:
        f.write("{ 半截")
    st = PlatformStore(p, seed="wecom")
    check("整份坏掉 → 用 `.env` 种子", st.current() == "wecom", st.current())

    with open(p, "w", encoding="utf-8") as f:
        f.write('{"current": "nope", "last_good": "nope2"}')
    st = PlatformStore(p, seed="also-nope")
    check("全都不合法 → 返回空串（主流程据此拒绝启动）", st.current() == "")


def test_store_reset():
    print("· reset 回到 `.env` 的种子")
    d = tmpdir()
    p = os.path.join(d, "platform.json")
    st = PlatformStore(p, seed="wecom")
    st.set("slack", actor="u")
    ok, msg = st.reset(actor="u")
    check("reset 成功", ok, msg)
    check("回到种子", st.current() == "wecom", st.current())


# ---------------- 3. 预检 ----------------

def test_preflight():
    print("· 预检：和真启动用的是同一套判据")
    ok, lines = preflight("wecom", PREFLIGHTS, get=get_of(ENV_OK))
    check("wecom 全配好 → 通过", ok, " / ".join(lines)[:80])
    check("说出了白名单人数", any("3 人" in x for x in lines))

    ok, lines = preflight("wecom", PREFLIGHTS, get=get_of({}))
    check("缺凭据 → 拒绝", not ok)
    check("点名缺哪个键", any("WECOM_BOT_ID" in x for x in lines))

    ok, lines = preflight("wecom", PREFLIGHTS, get=get_of({"WECOM_BOT_ID": "b",
                                               "WECOM_BOT_SECRET": "s"}))
    check("白名单为空 → 拒绝（fail-closed）", not ok)
    check("点明白名单键", any("WECOM_ALLOWED_USERIDS" in x for x in lines))

    ok, lines = preflight("wecom", PREFLIGHTS, get=get_of({"WECOM_BOT_ID": "b",
                                               "WECOM_BOT_SECRET": "s",
                                               "WECOM_ALLOW_ANYONE": "1"}))
    check("显式 ALLOW_ANYONE → 通过", ok)
    check("并明说任何人都能指挥", any("任何人" in x for x in lines))

    ok, lines = preflight("feishu", PREFLIGHTS, get=get_of(ENV_OK), importable=lambda m: False)
    check("飞书缺依赖 → 拒绝", not ok)
    check("点名 lark-oapi", any("lark-oapi" in x for x in lines))

    ok, _ = preflight("feishu", PREFLIGHTS, get=get_of(ENV_OK), importable=lambda m: True)
    check("飞书齐全 → 通过", ok)

    bad = dict(ENV_OK, SLACK_BOT_TOKEN="xapp-swapped", SLACK_APP_TOKEN="xoxb-swapped")
    ok, lines = preflight("slack", PREFLIGHTS, get=get_of(bad))
    check("Slack 两个 token 填反 → 拒绝", not ok)
    check("说出了 xoxb 前缀要求", any("xoxb-" in x for x in lines))

    ok, lines = preflight("feishu", PREFLIGHTS, get=get_of(dict(
        ENV_OK, FEISHU_USER_LABELS="ou_aaaa1111=张三")), importable=lambda m: True)
    check("白名单显示名生效", any("张三" in x for x in lines))
    check("没配名字的只露尾号", any("…2222" in x for x in lines))
    check("不整条打印 open_id", not any("ou_bbbb2222" in x for x in lines))


# ---------------- 4. 控制目录 ----------------

def test_control_inbox():
    print("· 控制目录：投递 → 认领 → 回执")
    base = control_dir(tmpdir())
    calls = []

    def h_switch(args, by):
        calls.append((args, by))
        return True, "已受理"

    def h_boom(args, by):
        raise RuntimeError("炸了")

    ib = ControlInbox(base, {"switch_platform": h_switch, "boom": h_boom}, poll_sec=0.01)
    check("空目录这一拍处理 0 条", ib.poll_once() == 0)

    cid, _ = submit(base, "switch_platform", {"to": "feishu"}, by="tester")
    check("投递后处理 1 条", ib.poll_once() == 1)
    check("回调收到了参数", calls and calls[0][0].get("to") == "feishu")
    check("回调收到了 by", calls and calls[0][1] == "tester")
    done = json.load(open(os.path.join(base, "done", cid + ".json"), encoding="utf-8"))
    check("回执 ok=true", done["ok"] is True)
    check("回执带原文", done["text"] == "已受理")
    check("inbox 已清空", os.listdir(os.path.join(base, "inbox")) == [])

    cid, done = submit(base, "nope", {}, wait=0.01)
    ib.poll_once()
    done = json.load(open(os.path.join(base, "done", cid + ".json"), encoding="utf-8"))
    check("不认识的命令 → 回执 ok=false", done["ok"] is False)
    check("并把可用命令列出来", "switch_platform" in done["text"])

    cid, _ = submit(base, "boom", {}, wait=0)
    ib.poll_once()
    done = json.load(open(os.path.join(base, "done", cid + ".json"), encoding="utf-8"))
    check("回调抛异常 → 回执 ok=false 且不带走整条通道", done["ok"] is False)
    check("异常信息进了回执", "炸了" in done["text"])

    p = os.path.join(base, "inbox", "bad.json")
    with open(p, "w", encoding="utf-8") as f:
        f.write("{不是 JSON")
    ib.poll_once()
    done = json.load(open(os.path.join(base, "done", "bad.json"), encoding="utf-8"))
    check("坏文件 → 回执 ok=false", done["ok"] is False)


def test_control_post_after_reply():
    print("· 「后续动作」必须在**回执写完之后**才跑（否则投递方永远等不到回执）")
    base = control_dir(tmpdir())
    seen = {}

    def h(args, by):
        def post():
            seen["done_file_exists"] = os.path.exists(
                os.path.join(base, "done", "cid1.json"))
        return True, "ok", post

    ib = ControlInbox(base, {"switch_platform": h})
    payload = {"id": "cid1", "cmd": "switch_platform", "args": {}, "by": "t"}
    os.makedirs(os.path.join(base, "inbox"), exist_ok=True)
    with open(os.path.join(base, "inbox", "cid1.json"), "w", encoding="utf-8") as f:
        json.dump(payload, f)
    ib.poll_once()
    check("post 确实跑了", "done_file_exists" in seen)
    check("跑的时候回执已经在盘上", seen.get("done_file_exists") is True)


# ---------------- 5. 切换器 ----------------

class FakeProc:
    pid = 4242


def make_switcher(root, ready=True, spawn_raises=False):
    st = PlatformStore(os.path.join(root, "data", "platform.json"), seed="wecom")
    env = dict(ENV_OK)

    def _spawn():
        if spawn_raises:
            raise OSError("拉不起来")
        return FakeProc()

    killed = []
    sw = Switcher(
        st, root, os.path.join(root, "app", "bridge.py"), current="wecom",
        instance="t", guard_sec=0.01, guard_hard_sec=0.01, spawn=_spawn,
        wait_ready=lambda t, s, p: (ready, "fake"),
        env_get=get_of(env), import_ok=lambda m: True, preflights=PREFLIGHTS,
        # **必须注入**：默认的 kill 是真 `taskkill`，测试里的 FakeProc.pid=4242
        # 万一是某个真实进程的 pid 就会误杀。测试永远不碰真进程。
        kill=lambda pid: killed.append(pid))
    sw.test_killed = killed
    return st, sw


def test_switcher_request():
    print("· 切换器：预检没过时**一个字都不许动**")
    root = tmpdir()
    st, sw = make_switcher(root)
    ok, text = sw.request("wecom")
    check("切到当前平台 → 拒绝", not ok)
    check("说的是「已经是」", "已经是" in text)

    ok, text = sw.request("dingtalk")
    check("不认识的目标 → 拒绝", not ok)
    check("真源没被改", st.current() == "wecom", st.current())

    ok, text = sw.request("feishu", actor="u1")
    check("预检通过 → ok", ok, text[:60])
    check("真源已写入 feishu", st.current() == "feishu")
    check("回执里说清了要重启", "重启" in text)
    # 反向守卫：旧口径「约 1~2 分钟」是 2026-11-13 一次冷启动最坏样本，
    # 2026-11-15 五次实测都是 2~5 秒 —— 不许它再写回来。
    check("回执里不再写「1~2 分钟」", "1~2 分钟" not in text)
    ok2, text2 = sw.request("slack")
    check("看守中拒绝第二次切换", not ok2 and "看守中" in text2)


def test_switcher_rollback():
    print("· 看守失败 → 自动回滚 + 通知")
    root = tmpdir()
    st, sw = make_switcher(root, ready=False)
    ok, _ = sw.request("feishu")
    check("预检通过", ok)
    got = []
    sw.on_ready = lambda t, d: got.append(("ready", t))
    sw.begin("feishu", "wecom", lambda text: got.append(("fail", text)))
    check("没有触发交接", not any(x[0] == "ready" for x in got))
    fails = [x[1] for x in got if x[0] == "fail"]
    check("发了失败通知", len(fails) == 1, (fails or [""])[0][:40])
    check("通知里说了回滚", fails and "回滚" in fails[0])
    check("真源回到 wecom", st.current() == "wecom")
    check("锁已放开（可以再切）", sw.switching is False)
    # 🔴 2026-11-13 真机的教训：放手之前必须把这个孩子收掉，否则它会**迟到接管**
    check("把没就绪的新进程收掉了", sw.test_killed == [FakeProc.pid],
          str(sw.test_killed))

    rec = json.load(open(sw.last_switch_path, encoding="utf-8"))
    check("落了一份切换结果", rec["ok"] is False and rec["origin"] == "wecom")


def test_switcher_success():
    print("· 看守成功 → 交接（调用方负责退出）")
    root = tmpdir()
    st, sw = make_switcher(root, ready=True)
    ok, _ = sw.request("slack")
    check("预检通过", ok)
    got = []
    sw.on_ready = lambda t, d: got.append((t, d))
    sw.begin("slack", "wecom", lambda text: got.append(("fail", text)))
    check("触发了交接", got == [("slack", "fake")], str(got))
    check("真源是 slack", st.current() == "slack")
    rec = json.load(open(sw.last_switch_path, encoding="utf-8"))
    check("结果落盘 ok=true", rec["ok"] is True and rec["target"] == "slack")


def test_switcher_spawn_error():
    print("· 新进程根本拉不起来 → 回滚")
    root = tmpdir()
    st, sw = make_switcher(root, spawn_raises=True)
    ok, _ = sw.request("feishu")
    check("预检通过", ok)
    got = []
    sw.begin("feishu", "wecom", lambda text: got.append(text))
    check("真源回到 wecom", st.current() == "wecom")
    check("通知里带上了异常", any("拉不起来" in x for x in got))


def test_switcher_no_notify():
    print("· 控制目录那条路没有会话可通知 → 不许因此崩掉")
    root = tmpdir()
    st, sw = make_switcher(root, ready=False)
    sw.request("feishu")
    sw.begin("feishu", "wecom", None)
    check("回滚照做", st.current() == "wecom")


# ---------------- 6. 运行时凭据 ----------------

def test_runtime_file():
    print("· 就绪凭据")
    d = tmpdir()
    p = os.path.join(d, "runtime", "bridge-t.json")
    check("写成功", write_runtime(p, "feishu", instance="t"))
    rt = read_runtime(p)
    check("读回来的平台对", rt and rt["platform"] == "feishu")
    check("带 pid", rt and rt["pid"] == os.getpid())
    check("带时间", rt and rt["started_at"] > 0)
    check("没有 .tmp 残留", not os.path.exists(p + ".tmp"))
    with open(p, "w", encoding="utf-8") as f:
        f.write("{坏")
    check("坏文件 → None（不抛）", read_runtime(p) is None)
    check("文件不存在 → None", read_runtime(p + ".nope") is None)


# ---------------- 6b. 真子进程（只测握手，不起真桥）----------------

def _helper_src(ready=True):
    if not ready:
        return "pass\n"
    return '''\
import json, os, time
here = os.path.dirname(os.path.abspath(__file__))
p = os.path.join(here, "data", "runtime", "bridge-t.json")
os.makedirs(os.path.dirname(p), exist_ok=True)
with open(p, "w", encoding="utf-8") as f:
    json.dump({"platform": "feishu", "pid": os.getpid(),
               "started_at": time.time()}, f)
time.sleep(8)
'''


def test_real_subprocess_ready():
    print("· 真子进程：写出就绪凭据 = 交接成功（这条链路不起任何真桥）")
    root = tmpdir()
    helper = os.path.join(root, "helper.py")
    with open(helper, "w", encoding="utf-8") as f:
        f.write(_helper_src(ready=True))
    st = PlatformStore(os.path.join(root, "data", "platform.json"), seed="wecom")
    sw = Switcher(st, root, helper, current="wecom", instance="t", guard_sec=10,
                  env_get=get_of(ENV_OK), import_ok=lambda m: True,
                  preflights=PREFLIGHTS)
    got = []
    sw.on_ready = lambda t, d: got.append((t, d))
    sw.begin("feishu", "wecom", None)
    check("认出了新进程", bool(got) and got[0][0] == "feishu", str(got))
    rt = read_runtime(sw.runtime_path)
    check("就绪凭据在盘上", rt and rt["platform"] == "feishu")
    # 收拾干净：别把测试用的进程留在后台
    if rt and rt.get("pid"):
        import subprocess
        subprocess.run(["taskkill", "/PID", str(rt["pid"]), "/F"],
                       capture_output=True)


def test_real_subprocess_gives_up():
    print("· 真子进程：进程退了却没写凭据 = **立刻**回滚（不用等满）")
    root = tmpdir()
    helper = os.path.join(root, "helper.py")
    with open(helper, "w", encoding="utf-8") as f:
        f.write(_helper_src(ready=False))
    st = PlatformStore(os.path.join(root, "data", "platform.json"), seed="wecom")
    killed = []
    sw = Switcher(st, root, helper, current="wecom", instance="t",
                  guard_sec=30, guard_hard_sec=30,
                  env_get=get_of(ENV_OK), import_ok=lambda m: True,
                  preflights=PREFLIGHTS, kill=lambda pid: killed.append(pid))
    ok, _ = sw.request("feishu")
    check("预检通过（真源已写）", ok and st.current() == "feishu")
    got = []
    t0 = time.time()
    sw.begin("feishu", "wecom", lambda text: got.append(text))
    spent = time.time() - t0
    check("没有交接", not got or "回滚" in got[0])
    check("回滚到 wecom", st.current() == "wecom")
    check("看出来了它是**退出**而不是慢", any("退出" in x for x in got),
          (got or [""])[0][:70])
    check("没有傻等满 30 秒", spent < 10, f"{spent:.1f}s")
    check("也调了一次收尾（对已退的 pid 无害）", len(killed) == 1, str(killed))


def test_wait_ready_existing_instance():
    print("· 目标平台**早就**有实例在跑 → 也算交接成功（不能因为文件不新鲜就回滚）")
    root = tmpdir()
    sw = Switcher(PlatformStore(os.path.join(root, "data", "platform.json"),
                                seed="wecom"),
                  root, os.path.join(root, "bridge.py"), current="wecom",
                  instance="t", guard_sec=3)
    # 一份"很久以前写的、但 pid 还活着"的凭据（pid 用自己，保证活着）
    old = time.time() - 3600
    os.makedirs(os.path.dirname(sw.runtime_path), exist_ok=True)
    with open(sw.runtime_path, "w", encoding="utf-8") as f:
        json.dump({"platform": "feishu", "pid": os.getpid(),
                   "started_at": old}, f)
    ok, detail = sw._wait_ready("feishu", time.time(), None)
    check("沿用已有实例", ok, detail)
    check("说明了是「已有实例」", "已有实例" in detail)


class FakeLive:
    """活着但永远不就绪（模拟 `import lark_oapi` 冷加载 68 秒那种情形）。"""
    pid = 4243

    def poll(self):
        return None


class FakeDead:
    pid = 4244

    def __init__(self, rc=3):
        self.rc = rc

    def poll(self):
        return self.rc


def test_wait_ready_slow_but_alive():
    print("· 过了软窗口但进程还活着 → 继续等，直到硬上限（2026-11-13 的真机 bug）")
    root = tmpdir()
    sw = Switcher(PlatformStore(os.path.join(root, "data", "platform.json"),
                                seed="wecom"),
                  root, os.path.join(root, "bridge.py"), current="wecom",
                  instance="t", guard_sec=0.2, guard_hard_sec=1.2)
    t0 = time.time()
    ok, detail = sw._wait_ready("feishu", time.time(), FakeLive())
    spent = time.time() - t0
    check("判定为失败", not ok)
    check("等到了硬上限，不是软窗口", spent >= 1.0, f"{spent:.2f}s")
    check("说的是「还活着但没走完闸门」", "没走完启动闸门" in detail, detail[:60])


def test_wait_ready_dead_fast():
    print("· 进程已经退出 → 立刻判失败，不用等满")
    root = tmpdir()
    sw = Switcher(PlatformStore(os.path.join(root, "data", "platform.json"),
                                seed="wecom"),
                  root, os.path.join(root, "bridge.py"), current="wecom",
                  instance="t", guard_sec=30, guard_hard_sec=30)
    t0 = time.time()
    ok, detail = sw._wait_ready("feishu", time.time(), FakeDead(rc=5))
    spent = time.time() - t0
    check("判定为失败", not ok)
    check("没有傻等", spent < 3, f"{spent:.2f}s")
    check("带上了退出码", "exit=5" in detail, detail[:60])


# ---------------- 6c. 双活守卫 / 交接回执 ----------------

def test_still_current():
    print("· 取到互斥量之后复核真源（防「两个平台同时在线」）")
    from core.platform_store import still_current
    root = tmpdir()
    st = PlatformStore(os.path.join(root, "platform.json"), seed="wecom")
    check("没变 → 继续当值", still_current(st, "wecom"))
    st.set("feishu", actor="u")
    check("被切走了 → 让位", not still_current(st, "wecom"))
    with open(st.path, "w", encoding="utf-8") as f:
        f.write("{坏")
    check("文件坏了 → 不敢拦，放行", still_current(st, "wecom"))


def test_platform_command_sets_notice():
    print("· `/platform` 会把「回哪条消息报结果」记下来（成功后要在断开前说一声）")
    from core.bridge_core import CoreMixin

    class Mini(CoreMixin):
        def __init__(self, sw):
            self.platform_switcher = sw
            self._switch_notice = None
            self.sent = []

        def plat_send_text(self, chat, ctype, text, wait=True):
            self.sent.append((chat, ctype, text))
            return {"sent": True}

    root = tmpdir()
    st, sw = make_switcher(root, ready=False)      # 让它回滚，别真起进程
    m = Mini(sw)
    text, post = m._platform_command("feishu", "u1", "chat-1", 2, "chan|ts-1")
    check("先回了预检结果", "预检通过" in text, text[:40])
    check("还没记 notice（要等回执发完才动手）", m._switch_notice is None)

    # ① begin 被调用的那一刻，notice 必须已经记好（成功路径靠它发最后一条）
    #    ⚠️ 必须**连 token 一起**记：Slack 靠它把回执落进"你那条命令"的线程。
    seen = {}
    sw.begin = lambda t, o, n: seen.setdefault("notice", m._switch_notice)
    post()
    check("动手之前就记下了通知目标（含 token）",
          seen.get("notice") == ("chat-1", 2, "chan|ts-1"), str(seen))

    # ② 失败路径：notice 要被清掉，并把回滚通知发到原会话
    m2 = Mini(sw)
    st2, sw2 = make_switcher(tmpdir(), ready=False)
    m2.platform_switcher = sw2
    _, post2 = m2._platform_command("feishu", "u1", "chat-2", 1)
    post2()
    check("失败后 notice 被清掉（不挂到下一次切换上）", m2._switch_notice is None)
    check("失败通知发到了原会话",
          bool(m2.sent) and "回滚" in m2.sent[0][2], str(m2.sent)[:60])


def test_switch_notice_routing():
    print("· 交接回执落在哪：支持 `plat_reply_sync` 就回在**你那条命令下面**")
    from core.bridge_core import CoreMixin

    class Base(CoreMixin):
        def __init__(self, notice):
            self._switch_notice = notice
            self.sent, self.replied = [], []

        def plat_send_text(self, chat, ctype, text, wait=True):
            self.sent.append((chat, ctype, text))
            return {"sent": True}

    class WithSync(Base):
        def plat_reply_sync(self, token, text):
            self.replied.append((token, text))
            return True

    class SyncFails(Base):
        def plat_reply_sync(self, token, text):
            self.replied.append((token, text))
            return False

    class SyncRaises(Base):
        def plat_reply_sync(self, token, text):
            raise RuntimeError("炸了")

    notice = ("D123", 1, "D123|ts9")

    m = WithSync(notice)
    sent, how = m._switch_notice_send("slack", "pid 1")
    check("有同步回复 → 走那条", sent and how == "reply", how)
    check("回在 token 指的那条下面", m.replied and m.replied[0][0] == "D123|ts9")
    check("没有额外的按会话推送", m.sent == [])
    check("文案里有目标平台", "已交接给 `slack`" in m.replied[0][1])

    m = SyncFails(notice)
    sent, how = m._switch_notice_send("feishu", "pid 2")
    check("同步回复失败 → 退回按会话发", sent and how == "send", how)
    check("按会话发的是同一个 chat", m.sent and m.sent[0][0] == "D123")

    m = SyncRaises(notice)
    sent, how = m._switch_notice_send("wecom", "pid 3")
    check("同步回复抛异常也不影响退路", sent and how == "send", how)

    m = Base(None)
    sent, how = m._switch_notice_send("wecom", "pid 4")
    check("没有发起人（控制目录那条路）→ 什么都不发", (not sent) and how == "")
    check("也确实没发", m.sent == [] and m.replied == [])


# ---------------- 7. 文案 ----------------

def test_list_text():
    print("· `/platform` 的列表文案")
    d = tmpdir()
    st = PlatformStore(os.path.join(d, "platform.json"), seed="wecom")
    text = platform_list_text(st)
    check("写出了当前平台", "`wecom`" in text)
    for p in ("wecom", "feishu", "slack"):
        check(f"列出了 {p}", f"`{p}`" in text)
    check("说清了会重启", "重启" in text)
    check("说清了会终止任务", "终止" in text)
    check("说清了会自动回滚", "回滚" in text)


def main():
    for fn in (test_names, test_store_seed, test_store_no_overwrite,
               test_store_fallback, test_store_reset, test_preflight,
               test_control_inbox, test_control_post_after_reply,
               test_switcher_request, test_switcher_rollback,
               test_switcher_success, test_switcher_spawn_error,
               test_switcher_no_notify, test_runtime_file,
               test_real_subprocess_ready, test_real_subprocess_gives_up,
               test_wait_ready_existing_instance,
               test_wait_ready_slow_but_alive, test_wait_ready_dead_fast,
               test_still_current, test_platform_command_sets_notice,
               test_switch_notice_routing,
               test_list_text):
        fn()
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项没过：")
        for f in FAIL:
            print("   ·", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())