"""Phase 1 单元级验证：分片 / 去重 / 会话表 / runner（真跑 claude）。

不发企微消息，先证明内部逻辑对，再接群消息。
用法： .venv/Scripts/python.exe -X utf8 tools/test_phase1.py [--skip-llm]
"""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

import bridge  # noqa: E402

FAIL = []
BOT_ID = "aibTESTBOTID000000000000000000000"   # 打桩用，非真实凭据


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def test_chunk():
    print("\n== 分片（按 UTF-8 字节，不是字符数）==")
    limit = 400
    # 中文 3 字节/字：200 个汉字 = 600 字节 > 400，但字符数 200 < 400
    text = "字" * 200
    parts = bridge.chunk_md(text, limit)
    check("中文超限被切开", len(parts) > 1, f"{len(parts)} 片")
    check("每片 ≤ 上限", all(len(p.encode("utf-8")) <= limit for p in parts),
          f"最大 {max(len(p.encode('utf-8')) for p in parts)}B")
    check("拼接后可还原", "".join(parts) == text)

    # 段落优先
    para = "\n\n".join(["第一段" * 10, "第二段" * 10, "第三段" * 10])
    parts2 = bridge.chunk_md(para, 200)
    check("段落文本能切开且可还原", "".join(parts2) == para, f"{len(parts2)} 片")
    check("每片 ≤ 上限(段落)", all(len(p.encode("utf-8")) <= 200 for p in parts2))

    # 不超限时原样返回一片
    short = "短文本"
    check("短文本一片", bridge.chunk_md(short, 18000) == [short])


def test_send_strategy():
    """先整条发、只有 40058 才切片 —— 用**打桩的平台**验证，不发真实消息。"""
    print("\n== 发送策略：先整条，40058 才分片 ==")
    import re

    def mk(chunk_bytes=18000):
        tmp = tempfile.mkdtemp(prefix="ccbridge-send-")
        store = bridge.Store(os.path.join(tmp, "s.db"))

        class FakeRunner:
            perm_mode = "bypassPermissions"
            timeout = 600

            def kill_current(self):
                return True

        return bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store,
                             FakeRunner(), tmp, chunk_bytes, 20, 0)

    def sender(limit):
        """假平台：超过 limit 字节就回 errcode 40058（和真平台同一个错误码）。"""
        got = []

        def fake(chatid, chat_type, content, wait=True):
            if len(content.encode("utf-8")) > limit:
                return {"sent": False, "errcode": 40058,
                        "errmsg": f"exceed max length {limit}"}
            got.append(content)
            return {"sent": True, "errcode": 0, "errmsg": ""}

        return got, fake

    marker = re.compile(r"\(\d+/\d+(?: 续\d+)?\)\n")

    def body_of(got):
        return marker.sub("", "".join(got))

    # A. 19.2 KB（6400 汉字）原本一条就能发 —— 现在必须**一次发完**
    b = mk()
    text = "答" * 6400
    got, fake = sender(20480)
    b.send_markdown = fake
    failed = b.send_chunks("cid", 2, text)
    check("19.2KB 只发 1 次（不再预切）", len(got) == 1 and not failed,
          f"{len(got)} 次发送 failed={len(failed)}")
    check("19.2KB 正文未被改动", bool(got) and got[0] == text)

    # B. 24 KB 超上限 ⇒ 先撞 40058，再切成 2 片、顺序正确
    b = mk()
    text = "答" * 8000
    got, fake = sender(20480)
    b.send_markdown = fake
    failed = b.send_chunks("cid", 2, text)
    check("24KB 被 40058 拒后切成 2 片", len(got) == 2 and not failed,
          f"{len(got)} 次发送 failed={len(failed)}")
    check("分片带 (1/2)(2/2) 标记",
          bool(got) and got[0].startswith("(1/2)\n") and got[1].startswith("(2/2)\n"))
    check("分片顺序可还原全文", body_of(got) == text)

    # C. 平台实际上限比 CHUNK_BYTES 更小 ⇒ 单片仍被拒 ⇒ 按字节对半自愈
    b = mk(chunk_bytes=18000)
    text = "答" * 8000
    got, fake = sender(12000)
    b.send_markdown = fake
    failed = b.send_chunks("cid", 2, text)
    check("单片仍超长时对半自愈，全部送达", not failed,
          f"failed={len(failed)} 次发送={len(got)}")
    check("自愈后仍可还原全文", body_of(got) == text)
    check("自愈后每片 ≤ 平台上限",
          all(len(g.encode("utf-8")) <= 12000 for g in got),
          f"最大 {max((len(g.encode('utf-8')) for g in got), default=0)}B")

    # D. 不是 40058 的失败 ⇒ 不切片重试，但要**如实告知**用户
    b = mk()
    calls = []

    def bad(chatid, chat_type, content, wait=True):
        calls.append(content)
        return {"sent": False, "errcode": 93000, "errmsg": "invalid chatid"}

    b.send_markdown = bad
    failed = b.send_chunks("cid", 2, "你好")
    check("非 40058 不切片重试", len(failed) == 1 and failed[0][1]["errcode"] == 93000)
    check("非 40058 会尝试告知用户", len(calls) == 2 and "没能送达" in calls[-1],
          f"{len(calls)} 次调用")


def test_store():
    print("\n== 存储：去重 + 会话 ==")
    tmp = tempfile.mkdtemp(prefix="ccbridge-db-")
    st = bridge.Store(os.path.join(tmp, "t.db"))
    check("首次见到 msgid → False（不是重复）", st.seen("m1") is False)
    check("第二次同 msgid → True（重复）", st.seen("m1") is True)
    check("另一个 msgid → False", st.seen("m2") is False)

    sid, cwd = st.get_session("c1", "u1", r"C:\default")
    check("无记录时返回默认 cwd", sid is None and cwd == r"C:\default")
    st.save_session("c1", "u1", "sess-abc", r"C:\repo")
    sid2, cwd2 = st.get_session("c1", "u1", r"C:\default")
    check("保存后可取回", sid2 == "sess-abc" and cwd2 == r"C:\repo")
    st.clear_session("c1", "u1")
    sid3, cwd3 = st.get_session("c1", "u1", r"C:\default")
    check("/new 后 session 为空、但 cwd 保留", sid3 is None and cwd3 == r"C:\repo")


def test_tool_desc():
    print("\n== 工具名 → 进度短语（气泡里那一行）==")
    d = bridge.tool_desc
    check("Read → 相对路径（不含盘符）",
          d("Read", {"file_path": r"C:\a\b\app\bridge.py"}) == "读 app/bridge.py",
          d("Read", {"file_path": r"C:\a\b\app\bridge.py"}))
    check("Edit / Write 都算「改」",
          d("Edit", {"file_path": "x/y.py"}).startswith("改 ")
          and d("Write", {"file_path": "x/y.py"}).startswith("改 "))
    check("Bash 优先用 description（人写的短标签）",
          d("Bash", {"command": "pytest -q --tb=short", "description": "跑单测"}) == "运行 跑单测",
          d("Bash", {"command": "pytest -q", "description": "跑单测"}))
    check("Bash 没有 description 时用命令并截断",
          d("Bash", {"command": "x" * 100}) == "运行 " + "x" * 39 + "…",
          d("Bash", {"command": "x" * 100}))
    check("Bash 命令里的换行被压平",
          "\n" not in d("Bash", {"command": "echo a\necho b"}),
          repr(d("Bash", {"command": "echo a\necho b"})))
    check("Grep / Glob / WebSearch 各有自己的动词",
          d("Grep", {"pattern": "p"}).startswith("搜 ")
          and d("Glob", {"pattern": "*.py"}).startswith("找 ")
          and d("WebSearch", {"query": "q"}).startswith("搜网 "))
    check("未知工具不会哑掉（走兜底）",
          d("BrandNewTool", {}) == "用 BrandNewTool")
    check("空 name 也不炸", d(None, None) == "用 ?")
    check("进度行是**单行**（不会把气泡撑开）",
          "\n" not in d("Task", {"description": "多\n行"}))
    check("超长路径从左边截断、保留文件名",
          bridge._short_path(r"C:\a\b\c\d\e\f\g.py", limit=20).endswith("/g.py"),
          bridge._short_path(r"C:\a\b\c\d\e\f\g.py", limit=20))


def test_stream_parse():
    """增量读的解析器：喂假进程，不调模型。"""
    print("\n== stream-json 增量读：解析 / 失败落盘 ==")

    class FakeProc:
        """只提供 `_read_stdout` / `_read_stderr` 用到的那几个属性。"""

        def __init__(self, lines, stderr_lines=()):
            self._lines = list(lines)
            self.stdout = self
            self.stderr = iter(list(stderr_lines))

        def __iter__(self):
            return iter(self._lines)

        def close(self):
            pass

    r = bridge.Runner(exe="x", model="", perm_mode="", sources="", timeout=10,
                      allowed="", disallowed="", inject_env={}, show_usage=True)

    def ev(**kw):
        return json.dumps(kw, ensure_ascii=False)

    init = ev(type="system", subtype="init", session_id="sess-init", model="m",
              permissionMode="dontAsk", tools=["Read"])
    noisy = [ev(type="system", subtype="thinking_tokens", estimated_tokens=1)] * 3
    tool1 = ev(type="assistant", message={"content": [
        {"type": "tool_use", "name": "Read", "input": {"file_path": r"C:\p\a.py"}}]})
    text_only = ev(type="assistant", message={"content": [
        {"type": "text", "text": "我先看看"}]})
    tool2 = ev(type="assistant", message={"content": [
        {"type": "tool_use", "name": "Bash",
         "input": {"command": "ls", "description": "列目录"}}]})
    bad = "{not json"
    result = ev(type="result", subtype="success", is_error=False, result="答案",
                session_id="sess-final", permission_denials=[],
                modelUsage={"m": {"inputTokens": 10, "cacheReadInputTokens": 0,
                                  "cacheCreationInputTokens": 0, "contextWindow": 1000}})

    acc = {"result": None, "init": None, "events": 0, "last": None, "tools": 0,
           "bad": 0, "raw": [], "raw_bytes": 0, "raw_dropped": 0, "stderr": ""}
    seen = []
    p = FakeProc([init] + noisy + [tool1, text_only, tool2, bad, result])
    r._read_stdout(p, acc, lambda d, n: seen.append((d, n)))
    check("拿到 init（进程中途死也能知道是哪个 session）",
          (acc["init"] or {}).get("session_id") == "sess-init")
    check("拿到 result 事件", (acc["result"] or {}).get("result") == "答案")
    check("数清工具调用次数（thinking_tokens 不算）", acc["tools"] == 2, str(acc["tools"]))
    check("坏行只计数、不中断读取", acc["bad"] == 1 and acc["events"] == 8,
          f"bad={acc['bad']} events={acc['events']}")
    check("on_progress 收到两条（含工具名 + 目标）",
          seen == [("读 p/a.py", 1), ("运行 列目录", 2)], repr(seen))
    check("最后一条事件类型被记下来（排障用）", acc["last"] == "result", acc["last"])

    # on_progress 抛错必须被吞掉 —— 进度是锦上添花，不能拖垮这一轮
    def boom(d, n):
        raise RuntimeError("故意的")
    acc2 = dict(acc, result=None, init=None, events=0, tools=0, bad=0,
                raw=[], raw_bytes=0, raw_dropped=0)
    r._read_stdout(FakeProc([tool1, result]), acc2, boom)
    check("on_progress 抛错不影响解析结果",
          (acc2["result"] or {}).get("result") == "答案")

    # stderr 必须被读走（不读会让子进程写满管道卡死）；只留尾部
    acc3 = dict(acc, stderr="")
    r._read_stderr(FakeProc([], stderr_lines=["e" * 100] * 100), acc3)
    check("stderr 被读且截到尾部 4000 字符",
          len(acc3["stderr"]) == 4000, str(len(acc3["stderr"])))

    # 失败落盘
    acc4 = dict(acc, raw=[init, tool1], raw_bytes=len(init) + len(tool1))
    fn = r._dump_raw_stream(acc4)
    ok = fn and os.path.exists(os.path.join(bridge.ROOT, fn)) if fn else False
    check("原始流能落盘", bool(ok), repr(fn))
    if ok:
        with open(os.path.join(bridge.ROOT, fn), encoding="utf-8") as f:
            body = f.read()
        check("落盘内容就是原始行", init in body and tool1 in body, repr(body[:60]))
        os.remove(os.path.join(bridge.ROOT, fn))


def test_runner_stream():
    """真跑 runner（但是假可执行文件）—— 验增量读、超时不挂、stderr 不堵、失败不重跑。"""
    print("\n== runner：增量读 / 超时 / stderr / 失败兜底（不调模型）==")

    class FakeExe(bridge.Runner):
        def __init__(self, script, timeout=30):
            super().__init__(exe=sys.executable, model="", perm_mode="", sources="",
                             timeout=timeout, allowed="", disallowed="",
                             inject_env={}, show_usage=True)
            self.script = script

        def build_cmd(self, session_id):
            # 故意不返回 claude 的 argv —— 用一个 python 小程序冒充子进程
            return [sys.executable, "-X", "utf8", "-c", self.script]

    work = tempfile.mkdtemp(prefix="ccbridge-fake-")
    mk = ("import json,sys\n"
          "def ev(**kw): print(json.dumps(kw, ensure_ascii=False), flush=True)\n")

    # ① 正常：init → 两次工具 → result
    script = mk + (
        "ev(type='system', subtype='init', session_id='S1', model='m', permissionMode='x', tools=[])\n"
        "ev(type='assistant', message={'content':[{'type':'tool_use','name':'Read',"
        "'input':{'file_path':r'C:\\p\\a.py'}}]})\n"
        "ev(type='assistant', message={'content':[{'type':'tool_use','name':'Bash',"
        "'input':{'command':'ls','description':'列目录'}}]})\n"
        "ev(type='result', subtype='success', is_error=False, result='好的', session_id='S1',"
        " permission_denials=[], modelUsage={'m':{'inputTokens':5,'cacheReadInputTokens':5,"
        "'cacheCreationInputTokens':0,'contextWindow':1000}})\n")
    prog = []
    res = FakeExe(script).run("喂给我", None, work, on_progress=lambda d, n: prog.append(d))
    check("增量读：正常一轮判成功", res["ok"] and res["text"] == "好的",
          f"ok={res['ok']} text={res['text']!r}")
    check("增量读：进度回调按顺序到达",
          prog == ["读 p/a.py", "运行 列目录"], repr(prog))
    check("增量读：工具计数与事件数都对", res["tools"] == 2 and res["events"] == 4,
          f"tools={res['tools']} events={res['events']}")
    check("增量读：usage footer 照旧（modelUsage 字段不变）",
          "上下文" in res["usage_footer"], res["usage_footer"].strip()[:30])
    check("增量读：session_id 取 result 的", res["session_id"] == "S1", repr(res["session_id"]))
    check("增量读：正常路径不落盘原始流", res["raw_path"] is None and not res["stream_failed"])

    # ② 没有 result 事件 ⇒ 「结果未知」+ 落盘 + **不重跑**
    script2 = mk + (
        "ev(type='system', subtype='init', session_id='S2', model='m', permissionMode='x', tools=[])\n"
        "ev(type='assistant', message={'content':[{'type':'tool_use','name':'Write',"
        "'input':{'file_path':r'C:\\p\\out.txt'}}]})\n"
        "sys.exit(0)\n")
    res2 = FakeExe(script2).run("喂给我", None, work)
    check("没有 result ⇒ 判失败但写明「结果未知」",
          res2["is_error"] and "结果未知" in res2["text"], repr(res2["text"][:40]))
    check("没有 result ⇒ 标记 stream_failed", res2["stream_failed"] is True)
    check("没有 result ⇒ 原始流落盘", bool(res2["raw_path"]), repr(res2["raw_path"]))
    check("没有 result ⇒ 报出已经跑了几个工具（别让人以为啥都没干）",
          "1 次工具" in res2["text"], repr(res2["text"][:80]))
    check("没有 result ⇒ 记下 init 的 session（只是记录，不写库）",
          res2["init_session_id"] == "S2" and res2["session_id"] is None,
          f"init={res2['init_session_id']} res={res2['session_id']}")
    if res2["raw_path"]:
        os.remove(os.path.join(bridge.ROOT, res2["raw_path"]))

    # ③ 超时：子进程睡 10 秒，1 秒就应当被杀掉并立刻返回（不许挂住）
    script3 = "import time; time.sleep(10)\n"
    t0 = time.time()
    res3 = FakeExe(script3, timeout=1).run("喂给我", None, work)
    cost = time.time() - t0
    check("超时能被识别", res3["timed_out"] is True, f"timed_out={res3['timed_out']}")
    check("超时后立刻返回（读线程不会把主线程挂住）", cost < 8, f"{cost:.1f}s")

    # ④ stderr 灌 200 KB：必须有人读走，否则子进程写满管道会卡死
    script4 = mk + (
        "sys.stderr.write('E'*200000); sys.stderr.flush()\n"
        "ev(type='result', subtype='success', is_error=False, result='没堵死', session_id='S4',"
        " permission_denials=[], modelUsage={})\n")
    res4 = FakeExe(script4, timeout=60).run("喂给我", None, work)
    check("stderr 大量输出不会把子进程堵死",
          res4["ok"] and res4["text"] == "没堵死", f"ok={res4['ok']} text={res4['text'][:30]!r}")


def test_progress_pump():
    print("\n== 进度节流泵：变化时推 + 兜底心跳 ==")

    class FakeSend:
        def __init__(self):
            self.frames = []
            self.dead = False

        def send(self, content, finish):
            if self.dead:
                return {"sent": False, "errcode": None, "errmsg": "closed", "dead": True}
            self.frames.append((finish, content))
            return {"sent": True, "errcode": 0, "errmsg": ""}

    fs = FakeSend()
    pump = bridge.ProgressPump(fs, "cid", 2, heartbeat_sec=0)
    pump.update("读 a.py", 1)
    time.sleep(0.3)
    check("变化时推一帧", len(fs.frames) == 1, f"{len(fs.frames)} 帧")
    check("帧内容含工具目标且未定稿",
          "读 a.py" in fs.frames[0][1] and fs.frames[0][0] is False,
          repr(fs.frames[0][1]))
    pump.update("读 a.py", 1)
    time.sleep(0.25)
    check("同一步重复 ⇒ 不再推（省限流额度）", len(fs.frames) == 1, f"{len(fs.frames)} 帧")
    pump.update("跑单测", 2)
    time.sleep(0.3)
    check("目标变了 ⇒ 再推一帧", len(fs.frames) == 2, f"{len(fs.frames)} 帧")
    check("第几步会被带出来", "第 2 步" in fs.frames[-1][1], fs.frames[-1][1])
    pump.close()
    n = len(fs.frames)
    pump.update("关掉之后还来", 3)
    time.sleep(0.2)
    check("close 之后不再发帧（否则会盖掉答案）", len(fs.frames) == n, f"{len(fs.frames)} 帧")

    # 兜底心跳：没有任何变化，也要让用户知道它还活着
    fs2 = FakeSend()
    pump2 = bridge.ProgressPump(fs2, "cid", 2, heartbeat_sec=0.15)
    pump2.update("读 a.py", 1)
    time.sleep(0.6)
    check("没有新进展时按间隔补兜底心跳", len(fs2.frames) >= 3, f"{len(fs2.frames)} 帧")
    pump2.close()

    # 气泡不可用 ⇒ 立刻停，别继续往一个关掉的气泡里发
    fs3 = FakeSend()
    fs3.dead = True
    pump3 = bridge.ProgressPump(fs3, "cid", 2, heartbeat_sec=0.1)
    pump3.update("读 a.py", 1)
    time.sleep(0.35)
    check("气泡不可用 ⇒ 停止真进度", fs3.frames == [], f"{len(fs3.frames)} 帧")


def test_approval():
    """审批卡片：hook 侧（真跑脚本）+ 桥侧（发卡 / 点击写决定）。"""
    print("\n== 审批卡片：hook ↔ 桥 的文件协议 ==")
    hook = os.path.join(bridge.ROOT, "tools", "approval_hook.py")

    def run_hook(ev, env_extra):
        env = dict(os.environ)
        env.pop("CC_APPROVAL_DIR", None)
        env.update({"PYTHONIOENCODING": "utf-8"})
        env.update(env_extra)
        p = subprocess.run([sys.executable, "-X", "utf8", hook],
                           input=json.dumps(ev), capture_output=True, text=True,
                           encoding="utf-8", errors="replace", env=env, timeout=60)
        return p

    wr = {"hook_event_name": "PreToolUse", "tool_name": "Write",
          "tool_input": {"file_path": "a.txt"}, "session_id": "s1", "cwd": "c"}

    # ① 没开审批 ⇒ hook 是空壳（随仓库发布也不会影响别人）
    p = run_hook(wr, {})
    check("没设 CC_APPROVAL_DIR ⇒ 直接放行、无输出",
          p.returncode == 0 and not p.stdout.strip(), repr(p.stdout[:40]))

    # ② 只读工具直放（不弹卡、不占限流）
    d = tempfile.mkdtemp(prefix="ccbridge-approve-")
    p = run_hook({"hook_event_name": "PreToolUse", "tool_name": "Read",
                  "tool_input": {"file_path": "a.txt"}},
                 {"CC_APPROVAL_DIR": d, "CC_APPROVAL_TARGET": "cid|2|u1"})
    check("Read 走白名单直接放行", p.returncode == 0 and not p.stdout.strip(),
          repr(p.stdout[:40]))
    check("直放不留下请求文件", not os.listdir(d), repr(os.listdir(d)))

    # ③ 写工具：写出请求 → 桥给 allow ⇒ 放行
    def decide_after(d, verdict, delay=0.4):
        """模拟桥：先落 see，再写决定。"""
        def go():
            time.sleep(delay)
            open(os.path.join(d, "dummy.seen"), "w").close()
            reqs = [f for f in os.listdir(d) if f.endswith(".req.json")]
            for f in reqs:
                open(os.path.join(d, f[:-len(".req.json")] + ".seen"), "w").close()
                open(os.path.join(d, f[:-len(".req.json")] + ".decision"),
                     "w").write(verdict)
        t = threading.Thread(target=go, daemon=True)
        t.start()
        return t

    d = tempfile.mkdtemp(prefix="ccbridge-approve-")
    th = decide_after(d, "allow")
    p = run_hook(wr, {"CC_APPROVAL_DIR": d, "CC_APPROVAL_TARGET": "cid|2|u1",
                      "CC_APPROVAL_SEEN_SEC": "5", "CC_APPROVAL_WAIT_SEC": "10"})
    th.join()
    check("桥说 allow ⇒ hook 静默放行", p.returncode == 0 and not p.stdout.strip(),
          repr(p.stdout[:60]))
    check("收尾把请求文件清掉（不留垃圾）", not [f for f in os.listdir(d)
                                               if f.endswith(".req.json")],
          repr(os.listdir(d)))

    # ④ 桥说 deny ⇒ JSON 拒绝，理由交给模型
    d = tempfile.mkdtemp(prefix="ccbridge-approve-")
    th = decide_after(d, "deny")
    p = run_hook(wr, {"CC_APPROVAL_DIR": d, "CC_APPROVAL_TARGET": "cid|2|u1",
                      "CC_APPROVAL_SEEN_SEC": "5", "CC_APPROVAL_WAIT_SEC": "10"})
    th.join()
    out = {}
    try:
        out = json.loads(p.stdout.strip() or "{}")
    except Exception:
        pass
    check("桥说 deny ⇒ 输出 permissionDecision=deny",
          (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny",
          repr(p.stdout[:80]))
    check("拒绝理由交给模型", "拒绝" in
          ((out.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""),
          repr((out.get("hookSpecificOutput") or {}).get("permissionDecisionReason")))

    # ⑤ 没人应答 ⇒ 超时拒绝（**不依赖** harness 的 fail-open）
    d = tempfile.mkdtemp(prefix="ccbridge-approve-")
    t0 = time.time()
    p = run_hook(wr, {"CC_APPROVAL_DIR": d, "CC_APPROVAL_TARGET": "cid|2|u1",
                      "CC_APPROVAL_SEEN_SEC": "0.5", "CC_APPROVAL_WAIT_SEC": "1"})
    cost = time.time() - t0
    out = {}
    try:
        out = json.loads(p.stdout.strip() or "{}")
    except Exception:
        pass
    check("桥不在线 ⇒ 不等满 120 秒就拒绝",
          (out.get("hookSpecificOutput") or {}).get("permissionDecision") == "deny"
          and cost < 10, f"{cost:.1f}s {p.stdout[:40]!r}")
    check("超时/掉线的理由写清楚",
          "没有响应" in
          ((out.get("hookSpecificOutput") or {}).get("permissionDecisionReason") or ""),
          repr((out.get("hookSpecificOutput") or {}).get("permissionDecisionReason"))[:60])

    # ⑥ 桥侧：扫到请求就发卡，点击写决定
    tmp = tempfile.mkdtemp(prefix="ccbridge-approve-")
    store = bridge.Store(os.path.join(tmp, "ap.db"))

    class FakeRunner:
        perm_mode = "bypassPermissions"
        timeout = 600

        def kill_current(self):
            return True

    ad = os.path.join(tmp, "approvals")
    os.makedirs(ad, exist_ok=True)
    b = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, FakeRunner(),
                      tmp, 18000, 20, 0, approval_dir=ad,
                      approval_wait_sec=120)
    cards = []
    b.send_card = lambda cid, ct, card, wait=True: (
        cards.append((cid, card)) or {"sent": True, "errcode": 0, "errmsg": ""})
    b.send_markdown = lambda *a, **k: {"sent": True, "errcode": 0, "errmsg": ""}

    req = {"id": "rid1", "tool_name": "Bash",
           "tool_input": {"command": "rm -rf build"}, "target": "cid|2|u1",
           "cwd": r"C:\repo", "session_id": "s"}
    with open(os.path.join(ad, "rid1.req.json"), "w", encoding="utf-8") as f:
        json.dump(req, f, ensure_ascii=False)
    b._approval_scan_once()
    check("扫到请求就发卡片", len(cards) == 1 and cards[0][0] == "cid", str(len(cards)))
    card = cards[0][1] if cards else {}
    check("卡片带 task_id=请求 id", card.get("task_id") == "rid1", repr(card.get("task_id")))
    check("卡片是 button_interaction + 有 card_action",
          card.get("card_type") == "button_interaction" and "card_action" in card)
    btns = [x.get("text") for x in (card.get("button_list") or [])]
    check("两个按钮且都 ≤2 个汉字（实测上限）",
          btns == ["允许", "拒绝"] and all(len(t) <= 2 for t in btns), repr(btns))
    check("卡片正文显示得出来要执行什么",
          "rm -rf build" in json.dumps(card, ensure_ascii=False),
          repr(card.get("horizontal_content_list")))
    check("给 hook 落了 seen（它只等 10 秒）",
          os.path.exists(os.path.join(ad, "rid1.seen")))
    check("重复扫不会重复发卡",
          (b._approval_scan_once(), len(cards))[1] == 1, str(len(cards)))

    # 点击 → 写决定文件
    def ev(key, uid="u1", tid="rid1"):
        return {"cmd": "aibot_event_callback", "headers": {"req_id": "R"},
                "body": {"aibotid": BOT_ID, "chatid": "cid", "chattype": "group",
                         "from": {"userid": uid}, "msgtype": "event",
                         "event": {"eventtype": "template_card_event",
                                   "template_card_event": {
                                       "card_type": "button_interaction",
                                       "event_key": key, "task_id": tid}}}}

    b._handle_card_event(ev("approve_ok", uid="u2"))
    check("非发起人点「允许」不算数",
          not os.path.exists(os.path.join(ad, "rid1.decision")))
    b._handle_card_event(ev("approve_ok"))
    dp = os.path.join(ad, "rid1.decision")
    check("发起人点「允许」⇒ 写出 allow",
          os.path.exists(dp) and open(dp, encoding="utf-8").read().strip() == "allow",
          open(dp, encoding="utf-8").read().strip() if os.path.exists(dp) else "-")

    # 过期（hook 已退出、请求文件没了）⇒ 提示失效，不写决定
    os.remove(os.path.join(ad, "rid1.req.json"))
    b._approval_scan_once()
    with open(os.path.join(ad, "rid9.decision"), "w", encoding="utf-8") as f:
        f.write("")
    n0 = len(cards)
    b._handle_card_event(ev("approve_no", tid="rid9"))
    check("过期卡片的点击不会写进别人的决定",
          open(os.path.join(ad, "rid9.decision"), encoding="utf-8").read() == "",
          repr(open(os.path.join(ad, "rid9.decision"), encoding="utf-8").read()))

    # 请求没带目标会话 ⇒ 直接拒绝，别干等
    with open(os.path.join(ad, "rid2.req.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "rid2", "tool_name": "Bash", "tool_input": {}}, f)
    b._approval_scan_once()
    d2 = os.path.join(ad, "rid2.decision")
    check("请求没带目标 ⇒ 直接拒绝（不干等 120 秒）",
          os.path.exists(d2) and open(d2, encoding="utf-8").read().strip() == "deny")

    # 卡片发不出去 ⇒ 立刻拒绝（用户根本点不到）
    with open(os.path.join(ad, "rid3.req.json"), "w", encoding="utf-8") as f:
        json.dump({"id": "rid3", "tool_name": "Bash", "tool_input": {},
                   "target": "cid|2|u1"}, f)
    b.send_card = lambda *a, **k: {"sent": False, "errcode": 40058,
                                   "errmsg": "exceed max length"}
    b._approval_scan_once()
    d3 = os.path.join(ad, "rid3.decision")
    check("卡片发不出去 ⇒ 立刻拒绝",
          os.path.exists(d3) and open(d3, encoding="utf-8").read().strip() == "deny")

    # 关掉审批时不该注入任何环境变量
    b2 = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, FakeRunner(),
                       tmp, 18000, 20, 0)
    check("没开审批 ⇒ 不注入 CC_APPROVAL_*",
          b2._approval_env("cid", 2, "u1") is None)
    check("开了审批 ⇒ 注入目录 / 等待秒数 / 目标会话",
          (b._approval_env("cid", 2, "u1") or {}).get("CC_APPROVAL_TARGET") == "cid|2|u1",
          repr(b._approval_env("cid", 2, "u1")))

    # ⑦ 点击之后：卡片要**就地改成"已处理"**（平台只给点击后 5 秒的窗口）
    class FakeWS:
        def __init__(self, b, rc=0):
            self.b, self.frames, self.rc = b, [], rc

        def send(self, raw):
            f = json.loads(raw)
            self.frames.append(f)
            rc = self.rc(f) if callable(self.rc) else self.rc
            if rc is not None:
                self.b._deliver_receipt(rc)

        def close(self):
            pass

    def fresh(rid="rid4", rc=0):
        # ⚠️ 前面的用例把 send_card 改成了"永远失败"，这里必须复原，
        #    否则请求会被当成"卡片没发出去"直接拒绝，压根进不了审批表
        b.send_card = lambda cid, ct, card, wait=True: (
            cards.append(card) or {"sent": True, "errcode": 0, "errmsg": ""})
        b.ws = FakeWS(b, rc)
        with open(os.path.join(ad, rid + ".req.json"), "w", encoding="utf-8") as f:
            json.dump({"id": rid, "tool_name": "Bash",
                       "tool_input": {"command": "rm -rf build"},
                       "target": "cid|2|u1", "cwd": "C:/repo"}, f, ensure_ascii=False)
        b._approval_scan_once()
        b.ws.frames.clear()
        return b.ws

    def click(key="approve_ok", rid="rid4", req_id="RID-CLICK", uid="u1"):
        return {"cmd": "aibot_event_callback", "headers": {"req_id": req_id},
                "body": {"aibotid": BOT_ID, "chatid": "cid", "chattype": "group",
                         "from": {"userid": uid}, "msgtype": "event",
                         "event": {"eventtype": "template_card_event",
                                   "template_card_event": {
                                       "card_type": "button_interaction",
                                       "event_key": key, "task_id": rid}}}}

    wsx = fresh()
    b._handle_card_event(click())
    ups = [f for f in wsx.frames if f.get("cmd") == "aibot_respond_update_msg"]
    check("点击后发出「更新卡片」帧", len(ups) == 1, f"{len(wsx.frames)} 帧")
    if ups:
        f = ups[0]
        check("req_id 透传的是**那次点击**的（自己编的平台不认）",
              f["headers"]["req_id"] == "RID-CLICK", f["headers"]["req_id"])
        check("body.response_type = update_template_card",
              f["body"]["response_type"] == "update_template_card")
        card2 = f["body"]["template_card"]
        check("改后的卡片写着「已允许」", card2["main_title"]["title"] == "已允许",
              repr(card2["main_title"]["title"]))
        check("改后的卡片**没有按钮**（不留点了没用的按钮）",
              not card2.get("button_list"), repr(card2.get("button_list")))
        check("改后的卡片仍显示执行的是什么",
              "rm -rf build" in json.dumps(card2, ensure_ascii=False))
    check("更新成功后不再多发一条文字（卡片本身就是反馈）",
          not [f for f in wsx.frames if f.get("cmd") == "aibot_send_msg"],
          f"{[f.get('cmd') for f in wsx.frames]}")

    # 拒绝也走同一条路
    wsx = fresh("rid5")
    b._handle_card_event(click("approve_no", rid="rid5"))
    ups = [f for f in wsx.frames if f.get("cmd") == "aibot_respond_update_msg"]
    check("点「拒绝」同样把卡片改成「已拒绝」",
          bool(ups) and ups[0]["body"]["template_card"]["main_title"]["title"] == "已拒绝",
          repr(ups[0]["body"]["template_card"]["main_title"]["title"]) if ups else "-")

    # 平台不认"无按钮"的卡片类型 ⇒ 退回仍带一个「已处理」按钮的形态
    def rc_text_bad(f):
        c = (f.get("body") or {}).get("template_card") or {}
        return 42045 if c.get("card_type") == "text_notice" else 0

    wsx = fresh("rid6", rc=rc_text_bad)
    b._handle_card_event(click(rid="rid6"))
    ups = [f for f in wsx.frames if f.get("cmd") == "aibot_respond_update_msg"]
    kinds = [f["body"]["template_card"]["card_type"] for f in ups]
    check("text_notice 被拒 ⇒ 自动退回 button_interaction", kinds ==
          ["text_notice", "button_interaction"], repr(kinds))
    check("降级形态只有一个「已处理」按钮",
          [x["text"] for x in ups[-1]["body"]["template_card"]["button_list"]] == ["已处理"],
          repr(ups[-1]["body"]["template_card"]["button_list"]) if ups else "-")

    # 降级形态的那个按钮点了什么都不做（不弹"已失效"）
    wsx.frames.clear()
    b.send_markdown = lambda *a, **k: (
        wsx.frames.append({"cmd": "md"}) or {"sent": True, "errcode": 0, "errmsg": ""})
    b._handle_card_event(click("approve_done", rid="rid6"))
    check("「已处理」按钮点击后静默忽略", wsx.frames == [], repr(wsx.frames))

    # 更新彻底失败 ⇒ 退回一条文字确认（不能让用户点了没反应）
    wsx = fresh("rid7", rc=lambda f: 93000)
    sent_md = []
    b.send_markdown = lambda cid, ct, text, wait=True: (
        sent_md.append(text) or {"sent": True, "errcode": 0, "errmsg": ""})
    b._handle_card_event(click(rid="rid7"))
    check("更新卡片失败 ⇒ 退回文字确认", sent_md and "已允许" in sent_md[-1], repr(sent_md))

    # ⑧ 开了审批之后，_do_job 必须把环境变量真的交给 runner（否则 hook 是空壳）
    got = {}

    class RecRunner:
        perm_mode = "bypassPermissions"
        timeout = 600

        def clear_stop(self):
            pass

        def kill_current(self):
            return True

        def run(self, text, sid, cwd, on_progress=None, extra_env=None):
            got["env"] = extra_env
            return {"ok": True, "text": "好", "session_id": "s", "is_error": False,
                    "subtype": "success", "denials": [], "usage_footer": "",
                    "timed_out": False, "stopped": False, "rc": 0, "duration": 0.0}

    store.save_session("cid9", "u1", None, tmp)
    b3 = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, RecRunner(),
                       tmp, 18000, 20, 0, approval_dir=ad)
    b3.send_card = lambda *a, **k: {"sent": True, "errcode": 0, "errmsg": ""}
    b3.send_markdown = lambda *a, **k: {"sent": True, "errcode": 0, "errmsg": ""}
    b3._do_job({"chatid": "cid9", "chat_type": 2, "userid": "u1", "text": "hi",
                "msgid": "m-ap"})
    check("_do_job 把审批环境变量传给了 runner",
          (got.get("env") or {}).get("CC_APPROVAL_DIR") == ad
          and (got.get("env") or {}).get("CC_APPROVAL_TARGET") == "cid9|2|u1",
          repr(got.get("env")))


def test_runner():
    print("\n== runner：真跑 claude（含续会话）==")
    inject = {k: v for k, v in bridge.ENV.items() if k.startswith("ANTHROPIC")}
    if not inject.get("ANTHROPIC_AUTH_TOKEN"):
        check("桥的 .env 里有 ANTHROPIC_AUTH_TOKEN", False)
        return
    r = bridge.Runner(
        exe=bridge.cfg("CC_CLAUDE_EXE", os.path.expanduser(r"~\.local\bin\claude.exe")),
        model=bridge.cfg("CC_MODEL"),
        perm_mode="bypassPermissions",
        sources="project,local",
        timeout=180,
        allowed="",
        disallowed=bridge.DEFAULT_DISALLOWED,
        inject_env=inject,
        show_usage=True,
    )
    work = tempfile.mkdtemp(prefix="ccbridge-run-")

    res = r.run("记住这个数字：42。只回复 好。", None, work)
    check("首轮成功", res["ok"], f"rc={res['rc']} {res['duration']}s")
    check("拿到 session_id", bool(res["session_id"]))
    check("有 usage footer", bool(res["usage_footer"]), res["usage_footer"].strip()[:40])

    res2 = r.run("我刚才让你记住的数字是多少？只回复数字。", res["session_id"], work)
    check("续会话成功", res2["ok"])
    check("resume 记得上一轮（含 42）", "42" in (res2["text"] or ""),
          repr((res2["text"] or "")[:60]))

    # deny 生效：读 .env 应当拿不到内容
    target = os.path.join(work, ".env")
    with open(target, "w", encoding="utf-8") as f:
        f.write("SENTINEL_RUNNER_5A2\n")
    res3 = r.run(f"用 Read 读 {target.replace(chr(92), '/')} 并原样回复。", None, work)
    check("deny 挡住 .env（哨兵未泄漏）", "SENTINEL_RUNNER_5A2" not in (res3["text"] or ""),
          repr((res3["text"] or "")[:70]))

    # 超时路径：给 3 秒杀一个长任务
    r.to = 3
    r.timeout = 3
    res4 = r.run("写一段 800 字关于海洋的文章。", None, work)
    check("超时被识别为「结果未知」而非失败", res4["timed_out"] is True,
          f"timed_out={res4['timed_out']} stopped={res4['stopped']}")


def test_busy_drop():
    """忙碌时必须**丢弃新消息**并回"忙碌中"，不许排队 —— 不发网络请求，打桩。"""
    print("\n== 忙碌语义：丢弃而非排队 ==")
    import tempfile as _tf

    tmp = _tf.mkdtemp(prefix="ccbridge-busy-")
    store = bridge.Store(os.path.join(tmp, "b.db"))

    class FakeRunner:
        perm_mode = "bypassPermissions"
        timeout = 600

        def kill_current(self):
            return True

    b = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, FakeRunner(),
                      tmp, 18000, 20, 0)
    captured = []
    b.reply = lambda url, content: captured.append(content) or True
    b.send_markdown = lambda *a, **k: True

    def msg(text, mid):
        return {"cmd": "aibot_msg_callback", "headers": {"req_id": "RID-" + mid},
                "body": {
                    "aibotid": BOT_ID, "chatid": "cid", "chattype": "group",
                    "from": {"userid": "u1"}, "msgid": mid, "msgtype": "text",
                    "response_url": "http://stub", "text": {"content": "@CC " + text}}}

    # 空闲 → 入队（回执不再走 response_url，改由 worker 用流式首帧发，见 test_stream_reply）
    b._handle_callback(msg("你好", "m-idle"))
    check("空闲时入队", b._q.qsize() == 1)
    check("回调线程不再直接回「收到」", captured == [], repr(captured))
    check("任务带上了回调的 req_id", b._q.queue[0]["req_id"] == "RID-m-idle",
          repr(b._q.queue[0].get("req_id")))
    check("任务也留着 response_url（没 req_id 时退回老路径）",
          b._q.queue[0]["response_url"] == "http://stub")

    # 忙碌 → 丢弃 + 忙碌提示
    b._current = {"started": 0, "text": "长任务", "userid": "u1", "chatid": "cid"}
    before = b._q.qsize()
    b._handle_callback(msg("你好", "m-busy"))
    check("忙碌时**没有**入队", b._q.qsize() == before, f"q={b._q.qsize()}")
    check("忙碌时回「忙碌中/已忽略」",
          "忙碌中" in captured[-1] and "忽略" in captured[-1], captured[-1][:26])

    # 忙碌时控制指令仍然可用：/status 现在发**卡片**，而且不能占住读线程
    cards = []
    b.send_card = lambda cid, ctype, card, wait=True: (
        cards.append((cid, ctype, card)) or {"sent": True, "errcode": 0, "errmsg": ""})
    b._handle_callback(msg("/status", "m-status"))
    for _ in range(100):                       # 卡片在旁路线程里发，等它一下
        if cards:
            break
        time.sleep(0.02)
    check("忙碌时 /status 仍可用（发了卡片）", bool(cards), f"captured={captured[-1:]}")
    card = cards[0][2] if cards else {}
    check("/status 卡片是按钮卡", card.get("card_type") == "button_interaction",
          str(card.get("card_type")))
    keys = [x.get("key") for x in card.get("button_list", [])]
    check("/status 卡片带三个按钮",
          keys == ["status_refresh", "status_clear", "status_stop"], str(keys))
    texts = [x.get("text", "") for x in card.get("button_list", [])]
    # 实测（2026-10-29）：4 个字的按钮在手机上被截断（「清上...」）⇒ 一律 2 个字
    check("按钮文字不超过 2 个字（否则手机端会被截断）",
          all(len(t) <= 2 for t in texts), str(texts))
    check("卡片带 card_action（实测平台必填）", bool(card.get("card_action")),
          str(card.get("card_action")))
    check("卡片有 task_id（回调要靠它认领）", bool(card.get("task_id")),
          str(card.get("task_id")))

    # 忙碌时 /stop 仍可用（且确实转到 runner）
    b._handle_callback(msg("/stop", "m-stop"))
    check("忙碌时 /stop 仍可用", "停止" in captured[-1], captured[-1][:20])


def test_single_chat():
    """单聊：回调里**没有 chatid** ⇒ 用 userid 兜底，且与群聊是两个独立会话。"""
    print("\n== 单聊（无 chatid）==")
    tmp = tempfile.mkdtemp(prefix="ccbridge-single-")
    store = bridge.Store(os.path.join(tmp, "s.db"))

    class FakeRunner:
        perm_mode = "bypassPermissions"
        timeout = 600

        def kill_current(self):
            return True

    def mk(allow_chats=""):
        b = bridge.Bridge(BOT_ID, "sec", "u1", allow_chats, "CC BOT", store,
                          FakeRunner(), tmp, 18000, 20, 0)
        b.reply = lambda url, content: True
        b.send_markdown = lambda *a, **k: {"sent": True, "errcode": 0, "errmsg": ""}
        return b

    def msg(chatid, chattype, text, mid):
        body = {"aibotid": BOT_ID, "chattype": chattype,
                "from": {"userid": "u1"}, "msgid": mid, "msgtype": "text",
                "response_url": "http://stub", "text": {"content": text}}
        if chatid:
            body["chatid"] = chatid
        return {"cmd": "aibot_msg_callback", "body": body}

    b = mk()
    b._handle_callback(msg(None, "single", "你好", "s-1"))
    check("单聊能入队", b._q.qsize() == 1, f"q={b._q.qsize()}")
    job = b._q.get_nowait()
    check("单聊 chatid 用 userid 兜底", job["chatid"] == "u1", repr(job["chatid"]))
    check("单聊 chat_type=1", job["chat_type"] == 1, str(job["chat_type"]))

    b._handle_callback(msg("group-chat", "group", "@CC 你好", "g-1"))
    gjob = b._q.get_nowait()
    check("群聊 chatid 保持原值", gjob["chatid"] == "group-chat")
    check("群聊 chat_type=2", gjob["chat_type"] == 2)

    # 会话隔离：私聊与群聊各自一份 session
    store.save_session("u1", "u1", "sid-single", tmp)
    check("私聊有独立会话（群聊读不到）",
          store.get_session("group-chat", "u1", tmp)[0] is None)
    check("私聊能续自己的会话", store.get_session("u1", "u1", tmp)[0] == "sid-single")

    # 群 chatid 白名单不该误杀单聊（单聊那个"chatid"其实是 userid）
    b2 = mk(allow_chats="group-chat")
    b2._handle_callback(msg(None, "single", "你好", "s-2"))
    check("单聊不被群白名单误杀", b2._q.qsize() == 1, f"q={b2._q.qsize()}")
    b3 = mk(allow_chats="group-chat")
    b3._handle_callback(msg("other-group", "group", "@CC hi", "g-2"))
    check("群白名单仍然生效", b3._q.qsize() == 0)


def test_robustness():
    """独立评审挖出来的几条：订阅失败、回执分发、异步回复、cwd 失效自愈、未知≠失败。"""
    print("\n== 健壮性（订阅失败 / 回执 / 异步回复 / cwd / 未知≠失败）==")
    tmp = tempfile.mkdtemp(prefix="ccbridge-rob-")
    store = bridge.Store(os.path.join(tmp, "r.db"))

    class FakeRunner:
        perm_mode = "bypassPermissions"
        timeout = 600

        def kill_current(self):
            return False

        def clear_stop(self):
            pass

    def mk(cwd=tmp):
        b = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store,
                          FakeRunner(), cwd, 18000, 20, 0)
        b.send_markdown = lambda *a, **k: {"sent": True, "errcode": 0, "errmsg": ""}
        return b

    # 1) 订阅失败必须**断开重连**，不能"连着但不再工作"
    b = mk()
    closed = []

    class FakeWS:
        def close(self):
            closed.append(1)

    b._on_message(FakeWS(), json.dumps(
        {"cmd": "aibot_subscribe", "errcode": 853000, "errmsg": "invalid bot_id"}))
    check("订阅失败会主动断开（触发重连）", closed == [1] and not b.subscribed.is_set())

    # 2) errcode 为 null 时回退到 result.errcode（.get 的默认值不生效）
    b2 = mk()
    b2._on_message(FakeWS(), json.dumps({"errcode": None, "result": {"errcode": 0}}))
    check("errcode=null 时读 result.errcode → 判定订阅成功", b2.subscribed.is_set())

    # 3) 回执分发给等待方；没有等待方时不能被"吃掉"成状态
    b3 = mk()
    b3._receipt_pending = True
    b3._deliver_receipt(0, "ok")
    check("回执能交给等待方", b3._receipt == (0, "ok"))
    b3._receipt_pending = False
    b3._receipt = None
    b3._deliver_receipt(0, "ok")
    check("无人等待时不写回执（避免下次误认）", b3._receipt is None)

    # 4) reply() 必须异步：不在读线程上做同步 HTTP
    b4 = mk()
    got = []
    b4._post_reply = lambda url, content: got.append(content) or True
    th = threading.Thread(target=b4._reply_worker, daemon=True)
    th.start()
    t0 = time.time()
    b4.reply("http://stub", "收到，处理中…")
    cost = time.time() - t0
    time.sleep(0.3)
    b4.stop = True
    th.join(timeout=2)
    check("reply() 立刻返回（不阻塞读线程）", cost < 0.05, f"{cost * 1000:.1f}ms")
    check("回复最终由独立线程发出", got == ["收到，处理中…"], repr(got))

    # 5) 记录的 cwd 失效 ⇒ 回落到配置的 cwd，且不拿旧 session 去 resume
    store.save_session("c-dead", "u1", "sid-old", r"C:\definitely-not-here-xyz")
    b5 = mk()

    class RunRec(FakeRunner):
        def __init__(self):
            self.calls = []

        def run(self, text, sid, cwd, on_progress=None):
            self.calls.append((sid, cwd))
            return {"ok": True, "text": "done", "session_id": "sid-new",
                    "is_error": False, "subtype": "success", "denials": [],
                    "usage_footer": "", "timed_out": False, "stopped": False,
                    "rc": 0, "duration": 0.0}

    rr = RunRec()
    b5.runner = rr
    b5._do_job({"chatid": "c-dead", "chat_type": 2, "userid": "u1",
                "text": "hi", "msgid": "m1"})
    check("坏 cwd 自动回落到 CC_CWD", rr.calls and rr.calls[0][1] == tmp,
          repr(rr.calls[0][1] if rr.calls else None))
    check("回落时不用旧 session", rr.calls and rr.calls[0][0] is None)

    # 6) 没收到回执 = "结果未知"；被明确拒收才是"没能送达"（不能混为一谈）
    def case(errcode, expect):
        b = mk()
        calls = []

        def sm(cid, ct, content, wait=True):
            calls.append(content)
            if len(calls) == 1:
                return {"sent": False, "errcode": errcode, "errmsg": "stub"}
            return {"sent": True, "errcode": 0, "errmsg": ""}

        b.send_markdown = sm
        failed = b.send_chunks("cid", 2, "hello")
        ok = len(calls) == 2 and expect in calls[1] and len(failed) == 1
        check(f"errcode={errcode} 的文案含「{expect}」", ok,
              repr(calls[1][:34]) if len(calls) > 1 else "没发第二条说明")

    case(None, "推送结果未知")
    case(93000, "结果没能送达")

    # 7) 空消息这类**提前 return** 的路径，不能把"忙碌"占位留在那儿
    b7 = mk()
    b7._q.put({"chatid": "cid", "chat_type": 2, "userid": "u1",
               "text": "   ", "msgid": "m-blank"})
    th = threading.Thread(target=b7._worker, daemon=True)
    th.start()
    time.sleep(0.4)
    b7.stop = True
    th.join(timeout=2)
    check("提前返回后不残留「忙碌中」", b7._current is None, repr(b7._current))


def test_phase4():
    """常驻化四项：单实例 / 日志清理 / 电源 / 看门狗。"""
    print("\n== Phase 4：单实例 / 日志清理 / 电源 / 看门狗 ==")
    tmp = tempfile.mkdtemp(prefix="ccbridge-p4-")

    # --- 日志清理：按**文件名里的日期**判，不看 mtime
    old_dir = bridge.LOGDIR
    bridge.LOGDIR = tmp

    def mklog(delta_days):
        stamp = (bridge.datetime.now()
                 - bridge.timedelta(days=delta_days)).strftime("%Y%m%d")
        p = os.path.join(tmp, f"bridge-{stamp}.log")
        open(p, "w").close()
        return p

    keep0, keep13, gone20 = mklog(0), mklog(13), mklog(20)
    other = os.path.join(tmp, "notes.txt")
    open(other, "w").close()
    removed = bridge.prune_old_logs(14)
    bridge.LOGDIR = old_dir
    check("清理掉 20 天前的日志", (not os.path.exists(gone20)) and removed == 1,
          f"removed={removed}")
    check("保留 14 天内的", os.path.exists(keep0) and os.path.exists(keep13))
    check("不碰非 bridge-*.log", os.path.exists(other))

    # --- 单实例：同名互斥量第二次必须被拒
    name = "Local\\ccbridge-selftest"
    first = bridge.acquire_single_instance(name)
    second = bridge.acquire_single_instance(name)
    check("单实例：第一次拿到", first is True)
    check("单实例：第二次被拒", second is False)

    # --- 电源：线程活着才有效（状态挂在那个线程上）
    t = bridge.start_keep_awake()
    check("电源线程在跑", t is not None and t.is_alive())

    # --- 看门狗：订阅后长时间收不到帧 ⇒ 主动断开
    class FakeRunner:
        perm_mode = "bypassPermissions"
        timeout = 600

        def kill_current(self):
            return True

    b = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT",
                      bridge.Store(os.path.join(tmp, "p4.db")), FakeRunner(),
                      tmp, 18000, 20, 0, stale_rx_sec=1)
    closed = []

    class FakeWS:
        def close(self):
            closed.append(1)

    b.ws = FakeWS()
    b.subscribed.set()
    b._last_rx = time.time() - 5
    old_wd = bridge.WATCHDOG_SEC
    bridge.WATCHDOG_SEC = 0.05
    th = threading.Thread(target=b._watchdog_loop, daemon=True)
    th.start()
    time.sleep(0.3)
    b.stop = True
    th.join(timeout=2)
    bridge.WATCHDOG_SEC = old_wd
    check("静默超时触发主动断开", closed == [1], f"close 调用 {len(closed)} 次")

    # 阈值 0 = 关闭检测（等实测过再开）
    b2 = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT",
                       bridge.Store(os.path.join(tmp, "p4b.db")), FakeRunner(),
                       tmp, 18000, 20, 0, stale_rx_sec=0)
    check("CC_STALE_RX_SEC=0 表示不检测", b2.stale_rx_sec == 0)


def test_stream_reply():
    """「收到，处理中…」和正文**共用一个气泡**（aibot_respond_msg + stream.id）。

    全部打桩：FakeWS 收帧、FakeRunner 出结果，不碰真平台。
    判定标准全部是**帧内容本身**（cmd / req_id / stream.id / finish / content），
    不是"函数返回了 True"。
    """
    print("\n== 流式消息：ack 与正文同一个气泡 ==")

    class FakeRunner:
        perm_mode = "bypassPermissions"
        timeout = 600

        def __init__(self, text="答案是 42", is_error=False, fatal=False, delay=0.0,
                     progress=None, progress_gap=0.0):
            self.res = {"ok": not is_error, "text": text, "is_error": is_error,
                        "subtype": "success" if not is_error else "error", "rc": 0,
                        "denials": [], "usage_footer": "", "session_id": "sid-1",
                        "timed_out": False, "stopped": False, "fatal": fatal}
            self.delay = delay
            # [(desc, n), ...] 依次回调给桥上，用来测真进度气泡。
            # `progress_gap` 是每次工具调用之间的真实间隔 —— 没有它，几毫秒内连报三次
            # 会被节流合并成一条（那是**对的**，见 bridge.ProgressPump）。
            self.progress = progress or []
            self.progress_gap = progress_gap

        def clear_stop(self):
            pass

        def kill_current(self):
            return True

        def run(self, text, session_id, cwd, on_progress=None):
            for i, (desc, n) in enumerate(self.progress):
                if on_progress:
                    on_progress(desc, n)
                if self.progress_gap and i < len(self.progress) - 1:
                    time.sleep(self.progress_gap)
            if self.delay:
                time.sleep(self.delay)
            return self.res

    class FakeWS:
        """收下每一帧，并立刻回一条 errcode 回执（模拟平台在若干毫秒内应答）。"""

        def __init__(self, b, rc=0):
            self.b = b
            self.frames = []
            self.rc = rc

        def send(self, raw):
            f = json.loads(raw)
            self.frames.append(f)
            rc = self.rc(f) if callable(self.rc) else self.rc
            if rc is not None:
                self.b._deliver_receipt(rc)

        def close(self):
            pass

    def mk(runner, rc=0, chunk_bytes=18000, progress_sec=0, hb=60):
        tmp = tempfile.mkdtemp(prefix="ccbridge-stream-")
        b = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT",
                          bridge.Store(os.path.join(tmp, "st.db")), runner,
                          tmp, chunk_bytes, 20, progress_sec, progress_hb_sec=hb)
        ws = FakeWS(b, rc)
        b.ws = ws
        b.replied = []
        b.reply = lambda url, content: b.replied.append(content) or True
        return b, ws

    def cb(text, mid="m1", headers=True):
        m = {"cmd": "aibot_msg_callback", "body": {
            "aibotid": BOT_ID, "chatid": "cid", "chattype": "group",
            "from": {"userid": "u1"}, "msgid": mid, "msgtype": "text",
            "response_url": "http://stub", "text": {"content": "@CC " + text}}}
        if headers:
            m["headers"] = {"req_id": "RID-" + mid}
        return m

    def run_job(b, text="你好", **kw):
        b._handle_callback(cb(text, **kw))
        job = b._q.get_nowait()
        b._do_job(job)
        return job

    # --- ① 正常：ack 一帧 + 定稿一帧，同一个 stream.id
    b, ws = mk(FakeRunner("答案是 42"))
    job = run_job(b)
    streams = [f for f in ws.frames if f.get("cmd") == "aibot_respond_msg"]
    check("走 aibot_respond_msg 流式（2 帧）", len(streams) == 2,
          f"{[f.get('cmd') for f in ws.frames]}")
    check("req_id 透传的是**回调里的**那个",
          all(f["headers"]["req_id"] == "RID-m1" for f in streams),
          streams[0]["headers"]["req_id"] if streams else "-")
    ids = {f["body"]["stream"]["id"] for f in streams}
    check("ack 与正文同一个 stream.id", len(ids) == 1, repr(ids))
    check("首帧 finish=False", streams[0]["body"]["stream"]["finish"] is False)
    check("首帧内容是「收到」", "收到" in streams[0]["body"]["stream"]["content"])
    check("定稿 finish=True", streams[-1]["body"]["stream"]["finish"] is True)
    check("定稿内容就是正文（覆盖 ack）",
          streams[-1]["body"]["stream"]["content"] == "答案是 42",
          repr(streams[-1]["body"]["stream"]["content"][:20]))
    check("ack 在正文之前（顺序不可反）",
          ws.frames.index(streams[0]) < ws.frames.index(streams[-1]))
    check("没有多推一条 markdown（一个气泡搞定）",
          not any(f.get("cmd") == "aibot_send_msg" for f in ws.frames),
          f"{len(ws.frames)} 帧")
    check("回调线程不回「收到」（否则就是两个气泡了）", b.replied == [], repr(b.replied))

    # --- ② 超过 20480 字节：第 1 片顶替气泡，其余照旧推送
    b, ws = mk(FakeRunner("字" * 7000))          # 21000 字节 > 20480
    run_job(b)
    streams = [f for f in ws.frames if f.get("cmd") == "aibot_respond_msg"]
    pushes = [f for f in ws.frames if f.get("cmd") == "aibot_send_msg"]
    check("超长：气泡里有第 1 片", len(streams) == 2
          and "(1/2)" in streams[-1]["body"]["stream"]["content"],
          repr(streams[-1]["body"]["stream"]["content"][:8]) if streams else "-")
    check("超长：第 2 片走主动推送", len(pushes) == 1
          and "(2/2)" in pushes[0]["body"]["markdown"]["content"],
          f"{len(pushes)} 条推送")
    check("超长：两片合起来还原全文",
          streams[-1]["body"]["stream"]["content"].split("\n", 1)[1]
          + pushes[0]["body"]["markdown"]["content"].split("\n", 1)[1]
          == "字" * 7000)

    # --- ③ 超过 10 分钟窗口（跑在中途过期）⇒ 正文改走主动推送，不丢内容
    old_max = bridge.STREAM_MAX_SEC
    bridge.STREAM_MAX_SEC = 1
    b, ws = mk(FakeRunner("迟到的答案", delay=1.2))
    run_job(b)
    bridge.STREAM_MAX_SEC = old_max
    streams = [f for f in ws.frames if f.get("cmd") == "aibot_respond_msg"]
    pushes = [f for f in ws.frames if f.get("cmd") == "aibot_send_msg"]
    check("过期：ack 已建出气泡", len(streams) == 1, f"{len(streams)} 帧流式")
    check("过期：正文另发一条，内容不丢", len(pushes) == 1
          and pushes[0]["body"]["markdown"]["content"] == "迟到的答案",
          f"{len(pushes)} 条推送")

    # --- ④ 平台明确拒收定稿（非 40058）⇒ 整条推送兜底
    def rc_500(f):
        body = f.get("body") or {}
        if body.get("msgtype") == "stream" and body["stream"]["finish"]:
            return 500
        return 0

    b, ws = mk(FakeRunner("答案乙"), rc=rc_500)
    run_job(b)
    pushes = [f for f in ws.frames if f.get("cmd") == "aibot_send_msg"]
    check("定稿被拒 ⇒ 整条推送兜底", len(pushes) == 1
          and pushes[0]["body"]["markdown"]["content"] == "答案乙",
          f"{len(pushes)} 条推送")

    # --- ⑤ 没回执 = 结果未知 ⇒ 气泡不判死、正文**不重发**（沿用老约定）
    old_wait = bridge.SEND_WAIT_SEC
    bridge.SEND_WAIT_SEC = 0.3
    b, ws = mk(FakeRunner("结果未知的答案"), rc=None)
    run_job(b)
    bridge.SEND_WAIT_SEC = old_wait
    streams = [f for f in ws.frames if f.get("cmd") == "aibot_respond_msg"]
    pushes = [f for f in ws.frames if f.get("cmd") == "aibot_send_msg"]
    check("结果未知：仍在同一个气泡上再试一次定稿", len(streams) == 2
          and streams[-1]["body"]["stream"]["finish"] is True)
    check("结果未知：不把正文重复推一遍（只发一条提示）", len(pushes) == 1
          and "推送结果未知" in pushes[0]["body"]["markdown"]["content"],
          repr(pushes[0]["body"]["markdown"]["content"][:14]) if pushes else "-")

    # --- ⑥ 回调没带 req_id ⇒ 退回老路径（response_url 回执 + 主动推送）
    b, ws = mk(FakeRunner("答案丙"))
    run_job(b, headers=False)
    check("无 req_id：回执走 response_url", any("收到" in c for c in b.replied),
          repr(b.replied))
    check("无 req_id：不发流式帧",
          not any(f.get("cmd") == "aibot_respond_msg" for f in ws.frames))
    check("无 req_id：正文照常推送",
          any(f.get("cmd") == "aibot_send_msg"
              and f["body"]["markdown"]["content"] == "答案丙" for f in ws.frames))

    # --- ⑦ 真进度进同一个气泡（stream-json 增量读 ⇒ 内容是**真的在干什么**）
    b, ws = mk(FakeRunner("慢任务答案", delay=0.1,
                          progress=[("读 app/bridge.py", 1),
                                    ("读 app/bridge.py", 1),   # 重复 ⇒ 不推
                                    ("跑单测", 2)], progress_gap=0.25), hb=0)
    run_job(b)
    streams = [f for f in ws.frames if f.get("cmd") == "aibot_respond_msg"]
    check("进度刷在同一个气泡里",
          all(f["body"]["stream"]["id"] == streams[0]["body"]["stream"]["id"]
              for f in streams), f"{len(streams)} 帧")
    check("进度内容是工具名 + 目标",
          "读 app/bridge.py" in streams[1]["body"]["stream"]["content"],
          repr(streams[1]["body"]["stream"]["content"]))
    check("进度帧未定稿（finish=False）",
          streams[1]["body"]["stream"]["finish"] is False)
    check("同一步重复出现 ⇒ 不重复推（省限流额度）",
          sum(1 for f in streams[:-1] if "读 app/bridge.py" in f["body"]["stream"]["content"]) == 1,
          f"{len(streams)} 帧")
    check("最后仍以正文定稿（进度不会盖掉答案）",
          streams[-1]["body"]["stream"]["finish"] is True
          and streams[-1]["body"]["stream"]["content"] == "慢任务答案")

    # ⑦b 有气泡但**没有任何进度事件** ⇒ 不再按时间塞一条"还在跑"（旧行为已移除）
    b, ws = mk(FakeRunner("慢任务答案", delay=0.35), progress_sec=0.15, hb=0)
    run_job(b)
    streams = [f for f in ws.frames if f.get("cmd") == "aibot_respond_msg"]
    check("有气泡时不再按时间刷「还在跑」（交给真进度）", len(streams) == 2,
          f"{len(streams)} 帧")

    # ⑦c 没有气泡（回调没带 req_id）⇒ 退回那条只看时间的主动推送
    b, ws = mk(FakeRunner("慢任务答案", delay=0.35), progress_sec=0.15)
    run_job(b, headers=False)
    pushes = [f for f in ws.frames if f.get("cmd") == "aibot_send_msg"]
    check("没气泡时仍按时间推「还在跑」",
          any("还在跑" in f["body"]["markdown"]["content"] for f in pushes),
          f"{[f['body']['markdown']['content'][:12] for f in pushes]}")

    # --- ⑧ 长任务到 9 分钟闸口：气泡先收口，别永远停在「收到，处理中…」
    old_max, old_latch = bridge.STREAM_MAX_SEC, bridge.STREAM_LATCH_SEC
    bridge.STREAM_MAX_SEC, bridge.STREAM_LATCH_SEC = 100, 0.15
    b, ws = mk(FakeRunner("终于跑完了", delay=0.5))
    run_job(b)
    bridge.STREAM_MAX_SEC, bridge.STREAM_LATCH_SEC = old_max, old_latch
    streams = [f for f in ws.frames if f.get("cmd") == "aibot_respond_msg"]
    pushes = [f for f in ws.frames if f.get("cmd") == "aibot_send_msg"]
    check("闸口：气泡以一个「仍在运行」定稿帧收口", len(streams) == 2
          and streams[-1]["body"]["stream"]["finish"] is True
          and "仍在运行" in streams[-1]["body"]["stream"]["content"],
          repr(streams[-1]["body"]["stream"]["content"][:16]) if streams else "-")
    check("闸口：正文改走另发一条（内容不丢）", len(pushes) == 1
          and pushes[0]["body"]["markdown"]["content"] == "终于跑完了")


def test_safety_defaults():
    """克隆下来直接跑的人，不该因为"忘了配"就把机器交出去。

    这两条是**对外版**的安全默认值，必须被单测钉住，否则下次调参时会被无声改回去。
    """
    print("\n== 安全默认值：白名单 fail-closed / 默认档位只读 ==")

    gate = bridge.whitelist_gate
    r1 = gate("", False)
    check("空白名单 ⇒ 拒绝启动", bool(r1), f"gate('',False)={r1!r}")
    check("纯空格也算空", bool(gate("   ", False)))
    check("拒绝原因里给了解决办法（提到 WECOM_ALLOW_ANYONE）",
          bool(r1) and "WECOM_ALLOW_ANYONE" in r1)
    check("填了白名单 ⇒ 放行", gate("zhangsan,lisi", False) is None)
    check("显式 WECOM_ALLOW_ANYONE=1 ⇒ 放行", gate("", True) is None)

    # 默认档位必须是**实测真拦得住**的那几个（tools/probe_permission_modes.py）：
    #   plan / dontAsk / manual 会拒；acceptEdits / auto / bypassPermissions 照样执行 Bash。
    blocking = {"plan", "dontAsk", "manual"}
    mode = bridge.DEFAULT_PERM_MODE
    check(f"默认档位 {mode!r} 属于会拒绝执行的那一档", mode in blocking, f"实测可拦：{blocking}")
    check("默认档位不是全自动", mode not in {"bypassPermissions", "acceptEdits", "auto"})


def test_card_events():
    """卡片按钮点击：白名单 + 三个动作 + 未知 key 不炸。

    2026-10-29 实测：长连接模式能下发按钮卡，点击走 `aibot_event_callback`
    （`card_type` / `event_key` / `task_id`），见 DECISIONS §21.9。
    """
    print("\n== 卡片按钮事件 ==")
    tmp = tempfile.mkdtemp(prefix="ccbridge-card-")
    store = bridge.Store(os.path.join(tmp, "s.db"))

    class FakeRunner:
        perm_mode = "bypassPermissions"
        timeout = 600
        killed = 0

        def kill_current(self):
            FakeRunner.killed += 1
            return True

    b = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, FakeRunner(),
                      tmp, 18000, 20, 0)
    sent = []
    b.send_markdown = lambda cid, ctype, text, wait=True: (
        sent.append(text) or {"sent": True, "errcode": 0, "errmsg": ""})
    cards = []
    b.send_card = lambda cid, ctype, card, wait=True: (
        cards.append(card) or {"sent": True, "errcode": 0, "errmsg": ""})

    def ev(key, uid="u1"):
        return {"cmd": "aibot_event_callback", "headers": {"req_id": "R"},
                "body": {"aibotid": BOT_ID, "chatid": "cid", "chattype": "group",
                         "from": {"userid": uid}, "msgtype": "event",
                         "event": {"eventtype": "template_card_event",
                                   "template_card_event": {
                                       "card_type": "button_interaction",
                                       "event_key": key, "task_id": "t1"}}}}

    # 非白名单点按钮 = 可执行入口，必须和文字消息同一道白名单
    store.save_session("cid", "u1", "SID123", tmp)
    b._handle_card_event(ev("status_clear", uid="u2"))
    check("非白名单点卡片不生效", store.get_session("cid", "u1", tmp)[0] == "SID123")

    # 清上下文
    b._handle_card_event(ev("status_clear"))
    check("点「清上下文」把 session 清掉",
          store.get_session("cid", "u1", tmp)[0] is None)

    # 停止
    before = FakeRunner.killed
    b._handle_card_event(ev("status_stop"))
    check("点「停止」转到 runner.kill_current", FakeRunner.killed == before + 1)

    # 刷新 → 又发一张卡
    n = len(cards)
    b._handle_card_event(ev("status_refresh"))
    for _ in range(100):
        if len(cards) > n:
            break
        time.sleep(0.02)
    check("点「刷新」重新发卡片", len(cards) > n)

    # 未知 key 不能炸
    b._handle_card_event(ev("who_knows"))
    check("未知 event_key 不抛异常", True)


def test_review_fixes():
    """2026-10-29 外部代码复核（Codex）报的 9 条，逐条钉死，防止回退。"""
    print("\n== 外部复核 9 条：逐条钉住 ==")
    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

    # ① 白名单：只填逗号 == 没配（闸门与解析必须同一套判据）
    check("闸门：', ,' ⇒ 拒绝启动", bridge.whitelist_gate(", ,", False) is not None)
    check("闸门：', ,' + ALLOW_ANYONE ⇒ 放行", bridge.whitelist_gate(", ,", True) is None)
    check("闸门：'u1, ,' ⇒ 放行", bridge.whitelist_gate("u1, ,", False) is None)
    check("解析：', ,' ⇒ 空集", bridge.parse_list(", ,") == set())

    # ② 静态自检：Claude 到底会不会加载这个 hook
    tmp = tempfile.mkdtemp(prefix="ccbridge-review-")
    ok, why = bridge.approval_configured(tmp, "project,local")
    check("静态自检：没有 .claude/settings.json ⇒ 不通过", not ok, why[:44])
    os.makedirs(os.path.join(tmp, ".claude"), exist_ok=True)
    sp = os.path.join(tmp, ".claude", "settings.json")
    with open(sp, "w", encoding="utf-8") as f:
        f.write("{ 这不是 json")
    ok, why = bridge.approval_configured(tmp, "project,local")
    check("静态自检：settings 不是合法 JSON ⇒ 不通过", not ok, why[:44])
    with open(sp, "w", encoding="utf-8") as f:
        json.dump({"hooks": {"PreToolUse": [{"hooks": [
            {"type": "command", "command": "python tools/approval_hook.py",
             "timeout": 300}]}]}}, f)
    ok, why = bridge.approval_configured(tmp, "project,local")
    check("静态自检：注册了 hook ⇒ 通过", ok, why[:44])
    ok, why = bridge.approval_configured(tmp, "user")
    check("静态自检：setting-sources 不含 project ⇒ 不通过", not ok, why[:44])

    # ③ hook 自己也要 fail-closed（读输入 / 解析 / 建目录失败都不能放行）
    hook = os.path.join(ROOT, "tools", "approval_hook.py")
    env = dict(os.environ, CC_APPROVAL_DIR=os.path.join(tmp, "appr"),
               CC_APPROVAL_TARGET="cid|2|u1", PYTHONIOENCODING="utf-8")

    def run_hook(stdin, e):
        return subprocess.run([sys.executable, hook], input=stdin, capture_output=True,
                              text=True, encoding="utf-8", env=e, timeout=60)

    r = run_hook("{这不是 json", env)
    check("hook：输入不是合法 JSON ⇒ deny", '"deny"' in (r.stdout or ""), r.stdout[:60])
    notdir = os.path.join(tmp, "not-a-dir")
    open(notdir, "w").close()
    r2 = run_hook(json.dumps({"tool_name": "Bash", "tool_input": {"command": "x"}}),
                  dict(env, CC_APPROVAL_DIR=os.path.join(notdir, "sub")))
    check("hook：决策目录建不出来 ⇒ deny", '"deny"' in (r2.stdout or ""), r2.stdout[:60])

    # ④ 重试前必须确认"这一轮没动手"
    base = {"is_error": True, "text": "Error: invalid session id", "tools": 0,
            "stream_failed": False, "stopped": False, "timed_out": False}
    check("重试：确认没动手 ⇒ 允许重跑", bridge.safe_to_retry_resume(dict(base))[0])
    for field, val, name in [("tools", 3, "已执行 3 次工具"),
                             ("stream_failed", True, "流中断"),
                             ("stopped", True, "被 /stop 打断"),
                             ("timed_out", True, "超时过")]:
        rr = dict(base)
        rr[field] = val
        ok2, why2 = bridge.safe_to_retry_resume(rr)
        check(f"重试：{name} ⇒ 不重跑", not ok2, why2)

    # ⑤ /new 与正在跑的任务：代次变了就不许把旧会话写回去
    t2 = tempfile.mkdtemp(prefix="ccbridge-epoch-")
    store = bridge.Store(os.path.join(t2, "e.db"))

    class R0:
        perm_mode = "bypassPermissions"
        timeout = 600

        def kill_current(self):
            return False

    b = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, R0(), t2, 18000, 20, 0)
    b._finish = lambda *a, **k: None
    res = {"fatal": False, "timed_out": False, "stopped": False, "is_error": False,
           "subtype": "success", "rc": 0, "text": "ok", "denials": [],
           "session_id": "sid-OLD"}
    job = {"chatid": "c1", "chat_type": 2, "userid": "u1", "text": "x",
           "epoch": b._epoch_of("c1", "u1")}
    b._bump_epoch("c1", "u1")          # 相当于任务跑着的时候用户发了 /new
    b._report(job, dict(res), t2, "sid-OLD")
    sid, _ = store.get_session("c1", "u1", t2)
    check("/new 之后，旧任务不许把 session 写回来", sid is None, repr(sid))
    b._report(dict(job, epoch=b._epoch_of("c1", "u1")), dict(res, session_id="sid-NEW"), t2, "sid-OLD")
    sid2, _ = store.get_session("c1", "u1", t2)
    check("没动过 /new ⇒ 正常落库", sid2 == "sid-NEW", repr(sid2))

    # ⑥ 卡片按钮也要过**群**白名单（不只是用户）
    b2 = bridge.Bridge(BOT_ID, "sec", "u1", "cwanted", "CC BOT", store, R0(),
                       t2, 18000, 20, 0)
    acted = []
    b2.send_markdown = lambda *a, **k: (acted.append(a) or
                                        {"sent": True, "errcode": 0, "errmsg": ""})

    def card_ev(chatid):
        return {"body": {"from": {"userid": "u1"}, "chatid": chatid, "chattype": "group",
                         "event": {"template_card_event": {"event_key": "status_stop"}}}}

    b2._handle_card_event(card_ev("cOTHER"))
    check("卡片：不在白名单群里点「停止」⇒ 不执行", not acted, str(acted))
    b2._handle_card_event(card_ev("cwanted"))
    check("卡片：白名单群里点「停止」⇒ 执行", bool(acted))

    # ⑦ 杀进程失败不能报「已清理」
    class Rk(R0):
        kill_status = "failed"

    b3 = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, Rk(), t2, 18000, 20, 0)
    check("停止失败 ⇒ 明确说「可能还在跑」", "可能还在跑" in b3._stop_text(False))
    check("停止成功 ⇒ 才说「已停止」", "已停止" in b3._stop_text(True))
    check("本来没在跑 ⇒ 不说「已停止」", "没有正在跑" in b._stop_text(False))

    # ⑧ 回执要认 req_id（平台带的时候）；不带就保持旧行为
    b4 = bridge.Bridge(BOT_ID, "sec", "u1", "", "CC BOT", store, R0(), t2, 18000, 20, 0)
    with b4._receipt_cv:
        b4._receipt_pending = True
        b4._receipt_want = "AAA"
        b4._receipt = None
    b4._deliver_receipt(0, "", "BBB")
    check("回执：req_id 对不上 ⇒ 不叫醒等待方", b4._receipt is None)
    b4._deliver_receipt(0, "", "AAA")
    check("回执：req_id 对上 ⇒ 交付", b4._receipt == (0, ""), str(b4._receipt))
    with b4._receipt_cv:
        b4._receipt = None
    b4._deliver_receipt(40058, "too long", None)
    check("回执：不带 req_id ⇒ 保持旧行为", b4._receipt == (40058, "too long"),
          str(b4._receipt))

    # ⑨ 日志跨天要换文件（不能一直写启动那天的文件）
    d = tempfile.mkdtemp(prefix="ccbridge-log-")
    h = bridge.DailyFileHandler(d)
    h._day = "20000101"                  # 假装这个 handler 是"很久以前"开的
    lg = bridge.logging.getLogger("review-test")
    lg.setLevel(bridge.logging.INFO)
    lg.addHandler(h)
    lg.info("跨天测试")
    h.flush()
    today = bridge.datetime.now().strftime("%Y%m%d")
    check("日志：跨天切到新文件", h.baseFilename.endswith(f"bridge-{today}.log"),
          os.path.basename(h.baseFilename))
    check("日志：新文件真的写了", os.path.getsize(h.baseFilename) > 0)
    lg.removeHandler(h)
    h.close()

    # ⑩ 文档与代码对齐（2026-10-29 用户复核 ③）：`CC_SETTING_SOURCES` 留空
    #    **不是"不传该参数"**，而是落到默认值 `project,local`（不含 user）。
    #    `.env.example` 以前写成"留空 = 含 user"，与代码相反 —— 这条把行为钉住。
    old_env_val = bridge.ENV.get("CC_SETTING_SOURCES")
    old_os_val = os.environ.pop("CC_SETTING_SOURCES", None)
    bridge.ENV["CC_SETTING_SOURCES"] = ""
    try:
        got = bridge.cfg("CC_SETTING_SOURCES", "project,local")
    finally:
        if old_env_val is None:
            bridge.ENV.pop("CC_SETTING_SOURCES", None)
        else:
            bridge.ENV["CC_SETTING_SOURCES"] = old_env_val
        if old_os_val is not None:
            os.environ["CC_SETTING_SOURCES"] = old_os_val
    check("留空 ⇒ 回落 project,local（不含 user）", got == "project,local", repr(got))


def main():
    skip_llm = "--skip-llm" in sys.argv
    test_chunk()
    test_send_strategy()
    test_store()
    test_busy_drop()
    test_single_chat()
    test_stream_reply()
    test_robustness()
    test_safety_defaults()
    test_card_events()
    test_phase4()
    # 下面三个**都不调模型**（runner 用一个 python 小程序冒充子进程）⇒ 放在 skip 分支外
    test_tool_desc()
    test_stream_parse()
    test_runner_stream()
    test_progress_pump()
    test_approval()
    test_review_fixes()
    if skip_llm:
        print("\n（跳过 runner 真跑）")
    else:
        test_runner()
    print("\n" + ("全部通过" if not FAIL else f"失败 {len(FAIL)} 项：{FAIL}"))
    return 0 if not FAIL else 1


if __name__ == "__main__":
    sys.exit(main())