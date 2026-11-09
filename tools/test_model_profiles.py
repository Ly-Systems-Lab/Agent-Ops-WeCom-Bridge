"""模型级配置（core/model_profiles.py）与接线验证。

**不联网、不调模型**：只证明"读/白名单/覆盖顺序/cwd 路由"这几条判据成立。
真机上的 `/model` 切换和 Ollama 往返要在聊天里验收。

用法： .venv/Scripts/python.exe -X utf8 tools/test_model_profiles.py
"""

import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), "app"))

from core.bridge_core import CoreMixin  # noqa: E402
from core.model_profiles import ModelProfiles  # noqa: E402
from core.runner import Runner  # noqa: E402

FAIL = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def write(p, text):
    with open(p, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def mk(spec=None, raw=None):
    """建一个临时配置表，返回 ModelProfiles。"""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "model_profiles.json")
    if raw is not None:
        write(p, raw)
    elif spec is not None:
        write(p, json.dumps(spec, ensure_ascii=False))
    return ModelProfiles(p)


def test_missing_and_broken():
    print("· 文件不存在 / 不是 JSON：不崩，且视作「没有配置」")
    d = tempfile.mkdtemp()
    mp = ModelProfiles(os.path.join(d, "nope.json"))
    check("不存在 ⇒ 空表", mp.names() == [] and mp.env_for("x") == {})

    mp = mk(raw="{ this is not json")
    check("坏 JSON ⇒ 空表", mp.names() == [] and mp.cwd_for("x") is None)

    mp = mk(raw="[1,2,3]")
    check("顶层不是对象 ⇒ 空表", mp.names() == [])


def test_env_whitelist():
    print("· env 白名单：只放 ANTHROPIC_* / CLAUDE_CODE_*")
    mp = mk({"m1": {"env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:11434",
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "32768",
        "PATH": "/evil",
        "PYTHONPATH": "/evil",
        "COMSPEC": "x",
    }}})
    env = mp.env_for("m1")
    check("保留 ANTHROPIC_BASE_URL", env.get("ANTHROPIC_BASE_URL") == "http://127.0.0.1:11434")
    check("保留 CLAUDE_CODE_*", env.get("CLAUDE_CODE_MAX_CONTEXT_TOKENS") == "32768")
    check("丢弃 PATH", "PATH" not in env)
    check("丢弃 PYTHONPATH", "PYTHONPATH" not in env)
    check("丢弃 COMSPEC", "COMSPEC" not in env)
    check("env_for 返回副本", mp.env_for("m1") is not mp.env_for("m1"))


def test_cwd():
    print("· cwd：存在才用，不存在就忽略（别路由进死路）")
    d = tempfile.mkdtemp()
    mp = mk({
        "good": {"cwd": d},
        "bad": {"cwd": os.path.join(d, "does-not-exist")},
        "none": {},
    })
    check("存在 ⇒ 返回", mp.cwd_for("good") == d, mp.cwd_for("good"))
    check("不存在 ⇒ None", mp.cwd_for("bad") is None)
    check("没配 ⇒ None", mp.cwd_for("none") is None)
    check("不认识的模型 ⇒ None", mp.cwd_for("other") is None)


def test_label():
    print("· label 给 /model 列表用")
    mp = mk({"m1": {"label": "本地 · 9B"}})
    check("有 label", mp.label_for("m1") == "本地 · 9B")
    check("没 label ⇒ 空串", mp.label_for("other") == "")


def test_runner_env_merge():
    print("· Runner.build_env：模型配置覆盖 .env，且 ANTHROPIC_MODEL 仍对齐")
    mp = mk({"local-x": {"env": {
        "ANTHROPIC_BASE_URL": "http://127.0.0.1:11434",
        "ANTHROPIC_AUTH_TOKEN": "ollama",
        "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "32768",
    }}})
    r = Runner(exe="x", model="cloud-x", perm_mode="", sources="",
               timeout=10, allowed="", disallowed="",
               inject_env={"ANTHROPIC_BASE_URL": "https://cloud.example",
                           "ANTHROPIC_AUTH_TOKEN": "cloud-token"},
               show_usage=False, model_fn=lambda: "local-x", profiles=mp)
    e = r.build_env()
    check("端点被覆盖", e["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:11434", e["ANTHROPIC_BASE_URL"])
    check("token 被覆盖", e["ANTHROPIC_AUTH_TOKEN"] == "ollama")
    check("上下文窗口注入", e["CLAUDE_CODE_MAX_CONTEXT_TOKENS"] == "32768")
    check("ANTHROPIC_MODEL 对齐", e["ANTHROPIC_MODEL"] == "local-x")
    check("clone 保留配置表", r.clone().profiles is mp)

    # 不认识的模型 ⇒ 一个字节都不改（老行为）
    r2 = Runner(exe="x", model="cloud-x", perm_mode="", sources="",
                timeout=10, allowed="", disallowed="",
                inject_env={"ANTHROPIC_BASE_URL": "https://cloud.example"},
                show_usage=False, model_fn=lambda: "cloud-x", profiles=mp)
    e2 = r2.build_env()
    check("非配置模型走 .env", e2["ANTHROPIC_BASE_URL"] == "https://cloud.example")
    check("非配置模型不注入上下文变量",
          "CLAUDE_CODE_MAX_CONTEXT_TOKENS" not in e2)


class FakeStore:
    def __init__(self):
        self.sid, self.cwd = "old-session", None
        self.saved = []

    def get_session(self, chatid, userid, default):
        return self.sid, (self.cwd or default)

    def save_session(self, chatid, userid, sid, cwd):
        self.saved.append((chatid, userid, sid, cwd))
        self.sid, self.cwd = sid, cwd


class Stub(CoreMixin):
    def __init__(self, runner, store, cc_cwd, cc_cwd_dm):
        self.runner = runner
        self.store = store
        self.cc_cwd = cc_cwd
        self.cc_cwd_dm = cc_cwd_dm


def test_session_cwd_routing():
    print("· 切模型 = 切工作目录；路由变了就清会话")
    small = tempfile.mkdtemp()
    big = tempfile.mkdtemp()
    mp = mk({"local-x": {"cwd": small}})
    box = {"m": "cloud-x"}
    r = Runner(exe="x", model="cloud-x", perm_mode="", sources="",
               timeout=10, allowed="", disallowed="", inject_env={},
               show_usage=False, model_fn=lambda: box["m"], profiles=mp)
    store = FakeStore()
    b = Stub(r, store, cc_cwd=big, cc_cwd_dm=big)

    sid, cwd = b._session_for("c", 1, "u")      # 1 = 单聊
    check("云端模型走默认目录", cwd == big and sid == "old-session", (sid, cwd))
    # 模拟"上一轮跑完，把 cwd 落库"——真实 Store 就是这么存会话的。
    store.save_session("c", "u", "old-session", big)
    store.saved.clear()

    box["m"] = "local-x"
    sid, cwd = b._session_for("c", 1, "u")
    check("本地模型走自己的目录", cwd == small, cwd)
    check("换目录 ⇒ 新建会话", sid is None and store.saved, (sid, store.saved))

    box["m"] = "cloud-x"
    sid, cwd = b._session_for("c", 1, "u")
    check("切回云端 ⇒ 回默认目录", cwd == big, cwd)


def test_hook_command_lint():
    print("· 审批 hook 命令串：反斜杠路径会被 bash 吃掉（静默放行）")
    from core.runner import _has_unquoted_backslash, approval_configured

    check("未加引号的反斜杠 ⇒ 判坏",
          _has_unquoted_backslash(r"C:\a\b\python.exe C:\a\b\approval_hook.py"))
    check("正斜杠 ⇒ 判好",
          not _has_unquoted_backslash("C:/a/b/python.exe C:/a/b/approval_hook.py"))
    check("引号内的反斜杠 ⇒ 判好（bash 保留字面量）",
          not _has_unquoted_backslash(
              r'"C:\a\b\python.exe" "C:\a\b\approval_hook.py"'))

    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, ".claude"))
    write(os.path.join(d, ".claude", "settings.json"), json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{
            "type": "command",
            "command": r"C:\a\b\python.exe C:\a\b\approval_hook.py",
            "timeout": 300}]}]}}))
    ok, why = approval_configured(d, "project,local")
    check("反斜杠命令 ⇒ 判为不可用", not ok, why)

    write(os.path.join(d, ".claude", "settings.json"), json.dumps({
        "hooks": {"PreToolUse": [{"hooks": [{
            "type": "command",
            "command": "C:/a/b/python.exe C:/a/b/approval_hook.py",
            "timeout": 300}]}]}}))
    ok, why = approval_configured(d, "project,local")
    check("正斜杠命令 ⇒ 判为可用", ok, why)


def main():
    test_missing_and_broken()
    test_env_whitelist()
    test_cwd()
    test_label()
    test_runner_env_merge()
    test_session_cwd_routing()
    test_hook_command_lint()
    print()
    if FAIL:
        print(f"❌ {len(FAIL)} 项失败：" + "；".join(FAIL))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())