"""P1-1 离线验证：命令级只读白名单。

不连任何平台、不发消息、不等人工点击 —— 只证明三件事：
  ① 该放行的放行（读命令、帮助、本地过滤器）；
  ② 该弹卡的弹卡，而且**拿不准的一律弹卡**；
  ③ 弹卡的理由（`policy`）真的进了请求文件 —— 卡面才有「策略」那一行。

用法： python -X utf8 tools/test_approval_policy.py
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOOK = os.path.join(ROOT, "tools", "approval_hook.py")
EFFECTS = os.path.join(ROOT, "interop", "dws_effects.json")
DOC = os.path.join(ROOT, "interop", "dingtalk.md")

FAIL = []


def check(name, cond, extra=""):
    print(f"  {'✅' if cond else '❌'} {name}" + (f"  {extra}" if extra else ""))
    if not cond:
        FAIL.append(name)


def _load_hook():
    spec = importlib.util.spec_from_file_location("approval_hook", HOOK)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


hook = _load_hook()


def test_allow():
    print("\n== 直放：纯读命令 ==")
    cases = [
        ("dws todo +get-my-tasks", "最基础的一条读"),
        ("dws todo +get-my-tasks --status false --priority 40,30", "带筛选参数"),
        ("dws todo +get-my-tasks --jq '.data | .[0].id'", "jq 里的管道在引号内，不切段"),
        ("dws todo +get-my-tasks --jq '$(whoami)'", "单引号里是字面量，不执行"),
        ("dws todo +get --task-id 123", "按 id 查详情"),
        ("cd D:/work && dws todo task list", "cd 是无害外壳"),
        ("dws todo +get-my-tasks | head -20", "管道下端是纯过滤器"),
        ("dws todo +create --help", "写命令带 --help 只打印帮助，不执行"),
        ("dws todo --help", "分组帮助"),
        ("dws schema todo --compact", "schema 是本地契约，不联网"),
        ("dws --help", "只有全局帮助"),
        ("dws contact user get --userid abc", "通讯录查询"),
    ]
    real = shutil.which("dws")
    if real:
        cases.append((real + " todo +get-my-tasks", "写全路径的 dws 也认（就是 PATH 上那个）"))
    for cmd, why in cases:
        ok, note = hook.classify_bash(cmd)
        check(f"直放 · {why}", ok, "" if ok else f"被拦：{note}")


def test_ask():
    print("\n== 弹卡：写操作、拿不准、危险写法 ==")
    cases = [
        ("dws todo +create --title t --executors u", "写操作"),
        # 实测：pflag 会把紧跟 --title 的 --help 吃成"值"，命令照常执行 ⇒ 不能直放
        ("dws todo +create --title --help --executors abc", "--help 被当成 flag 的值"),
        ("dws todo +create --title x --executors --help", "末尾但前面是 flag ⇒ 不算帮助"),
        ("dws todo +get-my-tasks && dws todo +create --title t", "多段里有写"),
        ("dws todo +get-my-tasks $(whoami)", "命令替换"),
        ("dws todo +get-my-tasks `whoami`", "反引号"),
        ("dws todo +get-my-tasks > out.json", "输出重定向"),
        ("dws todo +get-my-tasks 2>&1", "任何重定向都按写处理"),
        ("dws todo +get-my-tasks & rm -rf /", "单独的 & 会再跑一条后台命令"),
        ('dws todo +get-my-tasks --jq "$(whoami)"', "双引号里的命令替换照样执行"),
        ("dws api /v1.0/todo/tasks", "raw OpenAPI 不在表内"),
        ("dws event consume", "官方标 write"),
        ("dws", "没有子命令"),
        ("dws --dry-run todo +create", "全局参数写在子命令前，定位不到"),
        ("rm -rf /", "不是只读命令"),
        ("echo dws todo +create", "第一个词不是 dws"),
        ("dws todo +get-my-tasks | sh", "管道喂给 shell"),
        ("PATH=/tmp dws todo +get-my-tasks", "环境变量前缀可以换掉 dws 本体"),
        ("dws todo +get-my-tasks | sort -o out.txt", "过滤器带写文件选项"),
        ("dws todo +get-my-tasks 'unclosed", "引号没闭合"),
        ("python -c \"print(1)\"", "非白名单程序"),
        ("./dws todo +get-my-tasks", "同名但不是 PATH 上那个 dws"),
        ("C:\\temp\\dws.exe todo +get-my-tasks", "路径不存在，不可能验明正身"),
    ]
    for cmd, why in cases:
        ok, note = hook.classify_bash(cmd)
        check(f"弹卡 · {why}", not ok and bool(note), note if not ok else "竟然直放了")


def test_dryrun_hint():
    print("\n== 写命令的卡面提示：官方标了 dry-run 才提 ==")
    ok, note = hook.classify_bash("dws todo +create --title t --executors u")
    check("todo +create 卡面提示可干跑", "--dry-run" in note, note)
    ok, note = hook.classify_bash("dws event consume")
    check("没有 dry-run 的命令不乱提", "--dry-run" not in note, note)
    # 带 --dry-run 的写命令**照样弹卡**（万一 CLI 的 dry-run 有 bug 呢），但卡面要说清
    ok, note = hook.classify_bash("dws todo +create --title t --executors u --dry-run")
    check("干跑也弹卡，且卡面写明是预览", (not ok) and "干跑预览" in note, note)


def test_sensitive():
    print("\n== 敏感读：官方说 read，仍然弹卡并写明会读出密钥 ==")
    for path in hook._SENSITIVE_READ:
        ok, note = hook.classify_bash("dws " + path)
        check(f"弹卡 · {path}", not ok and "密钥" in note, note)


def test_table():
    print("\n== 官方契约表 ==")
    check("表文件存在", os.path.exists(EFFECTS))
    with open(EFFECTS, encoding="utf-8") as f:
        data = json.load(f)
    cmds = data.get("commands") or {}
    check("表里命令数 > 1000", len(cmds) > 1000, f"{len(cmds)} 条")
    reads = sum(1 for v in cmds.values() if v.get("effect") == "read")
    writes = sum(1 for v in cmds.values() if v.get("effect") in ("write", "destructive"))
    check("读 / 写两类都在", reads > 100 and writes > 100, f"read={reads} write+={writes}")
    check("`todo +get-my-tasks` 是 read", cmds.get("todo +get-my-tasks", {}).get("effect") == "read")
    check("`todo +create` 是 write", cmds.get("todo +create", {}).get("effect") == "write")
    check("`event consume` 是 write", cmds.get("event consume", {}).get("effect") == "write")
    # 敏感清单必须都是"官方说 read"的 —— 否则卡面那句「会读出密钥」就没意义了
    bad = [p for p in hook._SENSITIVE_READ
           if cmds.get(p, {}).get("effect") != "read"]
    check("敏感清单里的命令都存在且官方标 read", not bad, str(bad))


def test_doc():
    print("\n== 文档与代码一致 ==")
    with open(DOC, encoding="utf-8") as f:
        doc = f.read()
    check("钉钉页有「只读白名单」一节", "只读白名单" in doc)
    missing = [p for p in hook._SENSITIVE_READ if p not in doc]
    check("敏感读逐条写进了文档", not missing, str(missing))


def _env(adir):
    env = os.environ.copy()
    env["CC_APPROVAL_DIR"] = adir
    env["CC_APPROVAL_TARGET"] = "chatid|2|userid"
    env["CC_APPROVAL_SEEN_SEC"] = "0.6"   # 别让测试等 10 秒
    env["CC_APPROVAL_WAIT_SEC"] = "5"
    env.pop("CC_APPROVAL_ALLOW", None)
    return env


def _event(cmd):
    return json.dumps({"tool_name": "Bash", "tool_input": {"command": cmd},
                       "session_id": "t", "cwd": ROOT}, ensure_ascii=False)


def test_hook_e2e():
    print("\n== 闸门端到端（真起一次 hook 进程）==")
    # ① 读命令：不产生请求文件、立即退出、账本里有这一行
    adir = tempfile.mkdtemp(prefix="approval-ro-")
    p = subprocess.run([sys.executable, HOOK], input=_event("dws todo +get-my-tasks"),
                       capture_output=True, text=True, encoding="utf-8",
                       env=_env(adir), cwd=ROOT, timeout=30)
    reqs = [n for n in os.listdir(adir) if n.endswith(".req.json")]
    check("读命令：退出码 0", p.returncode == 0, p.stderr.strip()[:120])
    check("读命令：没有生成审批请求", not reqs, str(reqs))
    check("读命令：输出为空（= 放行）", not p.stdout.strip(), p.stdout[:120])
    log = os.path.join(adir, "readonly.log")
    got = ""
    if os.path.exists(log):
        with open(log, encoding="utf-8") as f:
            got = f.read()
    check("读命令：记账了", "dws todo +get-my-tasks" in got)

    # ② 写命令：真的生成请求文件，且 policy 写了理由；桥不在 ⇒ 自己拒绝
    adir2 = tempfile.mkdtemp(prefix="approval-rw-")
    proc = subprocess.Popen([sys.executable, HOOK],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True, encoding="utf-8",
                            env=_env(adir2), cwd=ROOT)
    proc.stdin.write(_event("dws todo +create --title t --executors u"))
    proc.stdin.close()
    payload = None
    t0 = time.time()
    while time.time() - t0 < 3:
        names = [n for n in os.listdir(adir2) if n.endswith(".req.json")]
        if names:
            try:
                with open(os.path.join(adir2, names[0]), encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                pass
            if payload:
                break
        time.sleep(0.05)
    out, err = proc.communicate(timeout=30)
    check("写命令：生成了审批请求", payload is not None)
    check("写命令：请求里带 policy 理由",
          bool((payload or {}).get("policy")), (payload or {}).get("policy") or "")
    check("写命令：理由是「写操作」",
          "写操作" in ((payload or {}).get("policy") or ""), (payload or {}).get("policy") or "")
    check("写命令：桥没在跑 ⇒ 自己拒绝",
          "deny" in (out or ""), (out or "").strip()[:120])

    # ③ 非 Bash 工具照旧（Read 直放、Write 弹卡）
    adir3 = tempfile.mkdtemp(prefix="approval-read-")
    p = subprocess.run([sys.executable, HOOK],
                       input=json.dumps({"tool_name": "Read", "tool_input": {}}),
                       capture_output=True, text=True, encoding="utf-8",
                       env=_env(adir3), cwd=ROOT, timeout=30)
    check("Read 仍然直放", p.returncode == 0 and not p.stdout.strip())


def main():
    test_allow()
    test_ask()
    test_dryrun_hint()
    test_sensitive()
    test_table()
    test_doc()
    test_hook_e2e()
    print("\n" + ("❌ 失败 %d 项：%s" % (len(FAIL), "；".join(FAIL)) if FAIL else "✅ 全部通过"))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())