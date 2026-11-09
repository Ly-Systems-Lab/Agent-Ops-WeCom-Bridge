"""模型唯一真源（core/model_store.py）与 Runner 接线（每条消息读一次）的单元验证。

**不联网、不调模型**：只证明"读/写/校验/兜底"这几条判据成立。
真机上的"探测通过才切换"要在聊天里验收（`/model`）。

用法： .venv/Scripts/python.exe -X utf8 tools/test_model_store.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from core.bridge_core import CoreMixin  # noqa: E402
from core.model_store import ModelStore, valid_model_name  # noqa: E402
from core.runner import Runner  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def write(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def test_name_rules():
    print("· 模型名的结构校验")
    for good in ("deepseek-v4.1-flash-1017[1M]", "a/b", "glm-5.2", "名字"):
        check(f"接受 {good!r}", valid_model_name(good)[0])
    for bad, why in (("", "空"), ("  x", "首尾空白"), ("a b", "内部空白"),
                     ("a\nb", "换行"), ("x" * 201, "超长"), (None, "非字符串")):
        ok, reason = valid_model_name(bad)
        check(f"拒绝 {why}", not ok, reason)


def test_seed_and_read():
    print("· 首次启动：用 .env 的种子初始化真源")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "data", "model.json")
    ms = ModelStore(p, seed="m-a")
    check("文件被建出来", os.path.exists(p))
    check("目录被建出来", os.path.isdir(os.path.dirname(p)))
    check("current = 种子", ms.current() == "m-a", ms.current())
    raw = json.load(open(p, encoding="utf-8"))
    check("last_good = 种子", raw.get("last_good") == "m-a")
    check("没有 .tmp 残留", not os.path.exists(p + ".tmp"))


def test_no_overwrite_on_restart():
    print("· 重启不覆盖用户用 /model 选的值")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model.json")
    ModelStore(p, seed="seed-v").set("picked-by-user")
    ms2 = ModelStore(p, seed="seed-v")          # 模拟进程重启
    check("仍是用户选的值", ms2.current() == "picked-by-user", ms2.current())


def test_corrupt_file():
    print("· 文件坏了：报警 + 退回种子 + 修回去")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model.json")
    write(p, "{ this is not json ")
    ms = ModelStore(p, seed="seed-v")
    check("回退到种子", ms.current() == "seed-v", ms.current())
    check("文件已被修成合法 JSON",
          json.load(open(p, encoding="utf-8")).get("current") == "seed-v")


def test_bad_current_good_lastgood():
    print("· current 坏了但 last_good 还在：优先用 last_good")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model.json")
    write(p, json.dumps({"current": "bad name", "last_good": "good-v"}))
    ms = ModelStore(p, seed="seed-v")
    check("用 last_good", ms.current() == "good-v", ms.current())
    check("文件被修回 last_good",
          json.load(open(p, encoding="utf-8")).get("current") == "good-v")


def test_both_bad_falls_to_seed():
    print("· current 和 last_good 都坏：退到 .env 的种子")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model.json")
    write(p, json.dumps({"current": "", "last_good": 123}))
    ms = ModelStore(p, seed="seed-v")
    check("用种子", ms.current() == "seed-v", ms.current())


def test_no_seed_at_all():
    print("· 没有种子、文件也坏：返回空串（= 不传 --model）")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model.json")
    write(p, "not json")
    ms = ModelStore(p, seed="")
    check("返回空串", ms.current() == "", repr(ms.current()))


def test_set_and_reset():
    print("· set / reset")
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model.json")
    ms = ModelStore(p, seed="seed-v", candidates=["m1", "m2"])
    ok, msg = ms.set("m2", actor="u1")
    check("set 成功", ok, msg)
    check("current = m2", ms.current() == "m2")
    snap = ms.snapshot()
    check("快照带 updated_by", snap["updated_by"] == "u1", snap["updated_by"])
    ok, msg = ms.set("bad name")
    check("拒绝非法名", not ok, msg)
    check("非法名没改动 current", ms.current() == "m2")
    ok, msg = ms.reset(actor="u1")
    check("reset 成功", ok, msg)
    check("回到种子", ms.current() == "seed-v", ms.current())


def test_candidates():
    print("· 候选名单去重 / 去空")
    ms = ModelStore(os.path.join(tempfile.mkdtemp(), "model.json"),
                    seed="s", candidates=["a", " b ", "", "a", None])
    check("去重去空", ms.candidates == ["a", "b"], ms.candidates)


def test_runner_live_model():
    print("· Runner：每条消息现取一次")
    box = {"m": "old-v"}
    r = Runner(exe="x", model="static-v", perm_mode="", sources="project,local",
               timeout=10, allowed="", disallowed="", inject_env={"K": "V"},
               show_usage=False, model_fn=lambda: box["m"])
    check("build_cmd 用当前值", "old-v" in r.build_cmd(None))
    box["m"] = "new-v"
    cmd = r.build_cmd(None)
    check("改完立刻生效（不用重建 Runner）",
          "new-v" in cmd and "old-v" not in cmd, cmd)
    env = r.build_env()
    check("ANTHROPIC_MODEL 跟着对齐", env.get("ANTHROPIC_MODEL") == "new-v",
          env.get("ANTHROPIC_MODEL"))
    box["m"] = ""
    cmd = r.build_cmd(None)
    check("取到空串就不传 --model", "--model" not in cmd, cmd)
    check("也不注入 ANTHROPIC_MODEL", "ANTHROPIC_MODEL" not in r.build_env())


def test_runner_backward_compat():
    print("· Runner：没有 model_fn 时行为不变（老单测/工具依赖）")
    r = Runner(exe="x", model="static-v", perm_mode="", sources="",
               timeout=10, allowed="", disallowed="", inject_env={},
               show_usage=False)
    check("build_cmd 仍用静态值", "static-v" in r.build_cmd(None))
    check("clone 也保留静态值", "static-v" in r.clone().build_cmd(None))


def test_runner_model_fn_raises():
    print("· Runner：取模型抛异常时退回静态值（不让任务起不来）")
    def boom():
        raise RuntimeError("disk gone")
    r = Runner(exe="x", model="static-v", perm_mode="", sources="",
               timeout=10, allowed="", disallowed="", inject_env={},
               show_usage=False, model_fn=boom)
    check("退回静态值", "static-v" in r.build_cmd(None))


class ProbeRunner:
    """假 Runner：只实现 `/model` 用到的那一个方法。"""
    def __init__(self, ok=True, detail="1.2s"):
        self.ok, self.detail = ok, detail
        self.calls = []

    def probe(self, name, cwd=None):
        self.calls.append(name)
        return self.ok, self.detail


class Stub(CoreMixin):
    def __init__(self, ms, runner):
        self.model_store = ms
        self.runner = runner
        self.cc_cwd = "."


def test_model_command():
    print("· /model 指令：列表 / 编号 / 名字 / reset / 探测失败")
    d = tempfile.mkdtemp()
    ms = ModelStore(os.path.join(d, "model.json"), seed="seed-v",
                    candidates=["m1", "m2"])
    r = ProbeRunner(ok=True)
    b = Stub(ms, r)

    text = b._model_command("", "u1")
    check("列表含当前值", "seed-v" in text, text.splitlines()[0])
    check("列表含候选", "m1" in text and "m2" in text)
    check("列列表时不探测", r.calls == [], r.calls)

    text = b._model_command("1", "u1")
    check("按编号探测 m1", r.calls == ["m1"], r.calls)
    check("切换成功文案", "已切换到" in text, text.splitlines()[0])
    check("真源已改", ms.current() == "m1", ms.current())

    r.calls.clear()
    text = b._model_command("m1", "u1")
    check("同名不重复探测", r.calls == [], r.calls)
    check("同名提示没改动", "没有改动" in text)

    r.ok, r.detail = False, "Model not exist."
    r.calls.clear()
    text = b._model_command("m2", "u1")
    check("探测失败 ⇒ 拒绝", "不可用" in text and "Model not exist" in text, text)
    check("探测失败 ⇒ 真源没变", ms.current() == "m1", ms.current())

    r.calls.clear()
    text = b._model_command("bad name", "u1")
    check("非法名 ⇒ 不探测就拒", r.calls == [] and "不合法" in text)

    text = b._model_command("9", "u1")
    check("越界编号被拒", "不在 1~2" in text, text.splitlines()[0])

    r.ok = True
    text = b._model_command("reset", "u1")
    check("reset 回种子", ms.current() == "seed-v", ms.current())
    check("reset 文案", "默认模型" in text)


def test_model_command_no_store():
    print("· /model：没启用热切换时如实回答")
    class Bare(CoreMixin):
        model_store = None
    check("如实说没启用", "没有启用" in Bare()._model_command("", "u1"))


def test_model_command_no_probe():
    print("· /model：Runner 不会探测时拒绝（不把未验证的值写进真源）")
    d = tempfile.mkdtemp()
    ms = ModelStore(os.path.join(d, "model.json"), seed="seed-v")
    class NoProbe:
        pass
    b = Stub(ms, NoProbe())
    text = b._model_command("m2", "u1")
    check("拒绝切换", "不支持探测" in text, text.splitlines()[0])
    check("真源没变", ms.current() == "seed-v", ms.current())


def test_probe_error_text():
    print("· 探测失败原文 → 一句话（别把 request_id 糊到用户脸上）")
    from core.runner import _probe_error_text
    raw = ('API Error: 400 event:error data:{"request_id":"abc",'
           '"code":"InvalidParameter","message":"Model not exist."}')
    got = _probe_error_text(raw)
    check("压成一句", got == "API Error: 400 · Model not exist.", got)
    check("空串 → 空", _probe_error_text("") == "")
    check("压不出就截断", len(_probe_error_text("x" * 300)) <= 160)


def test_usage_footer_model_pick():
    print("· 上下文分母：按「这一轮请求的模型」取，不按 assistant 消息里的模型名")
    from core.text import usage_footer
    mu = {"deepseek-v4.1-flash-1017": {"contextWindow": 200000},
          "deepseek-v4.1-flash-1017[1m]": {"contextWindow": 1000000}}
    data = {"modelUsage": mu}
    usage = {"input_tokens": 75700}

    # 用户 2026-11-09 的实际现象：换过模型的会话里两个 key 都在，
    # assistant 消息只报基础名 ⇒ 旧逻辑精确命中 200k。
    f = usage_footer(data, usage, "deepseek-v4.1-flash-1017",
                     want_model="deepseek-v4.1-flash-1017[1M]")
    check("请求 [1M] ⇒ 分母 1.0M", "1.0M" in f, f.strip())

    f2 = usage_footer(data, usage, "deepseek-v4.1-flash-1017[1m]",
                      want_model="deepseek-v4.1-flash-1017")
    check("请求基础模型 ⇒ 分母 200k", "200.0k" in f2, f2.strip())

    f3 = usage_footer(data, usage, "deepseek-v4.1-flash-1017")
    check("老调用方（无 want_model）行为不变", "200.0k" in f3, f3.strip())

    data2 = {"modelUsage": {"deepseek-v4.1-flash-1017[1m]": {"contextWindow": 1000000}}}
    f4 = usage_footer(data2, usage, "deepseek-v4.1-flash-1017")
    check("只有 [1m] 时靠前缀命中 1.0M", "1.0M" in f4, f4.strip())


def main():
    test_name_rules()
    test_seed_and_read()
    test_no_overwrite_on_restart()
    test_corrupt_file()
    test_bad_current_good_lastgood()
    test_both_bad_falls_to_seed()
    test_no_seed_at_all()
    test_set_and_reset()
    test_candidates()
    test_runner_live_model()
    test_runner_backward_compat()
    test_runner_model_fn_raises()
    test_model_command()
    test_model_command_no_store()
    test_model_command_no_probe()
    test_probe_error_text()
    test_usage_footer_model_pick()
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：")
        for f in FAIL:
            print("   -", f)
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())