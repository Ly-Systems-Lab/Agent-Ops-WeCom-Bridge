"""Phase 0 验证：headless `claude -p` 在「剥掉继承环境」后的行为矩阵。

为什么必须剥环境：
    在 Claude Code 自己的会话里跑 `claude -p`，子进程会继承 ANTHROPIC_*（认证）
    与 CLAUDE_CODE_*（含 MESSAGING_SOCKET / SESSION_ID）。那样测出来的「认证成功」
    是继承来的，跟 settings.json 无关 —— 会得到假阳性。桥的真实运行环境没有这些。

绝不打印 token：只打印 rc / 耗时 / is_error / 文本预览 / 脱敏计数。

用法：
    python tools/verify_headless.py <case>
    python tools/verify_headless.py all
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

CLAUDE_EXE = os.environ.get(
    "CC_CLAUDE_EXE", os.path.expanduser(r"~\.local\bin\claude.exe")
)
SETTINGS = os.path.expanduser(r"~\.claude\settings.json")

SENTINEL = "SENTINEL_7F3A_DO_NOT_LEAK"
TIMEOUT = 180


def load_user_env():
    """只取 settings.json 里的 env 段（认证 + 模型别名）。"""
    with open(SETTINGS, encoding="utf-8") as f:
        return json.load(f).get("env", {})


def scrubbed_env():
    """剥掉所有 ANTHROPIC* / CLAUDE* —— 模拟桥被任务计划程序拉起的干净环境。"""
    out = {}
    for k, v in os.environ.items():
        ku = k.upper()
        if ku.startswith("ANTHROPIC") or ku.startswith("CLAUDE"):
            continue
        out[k] = v
    return out


def run_case(name, *, sources, inject_env, perm_mode, disallow, prompt, cwd):
    env = scrubbed_env()
    if inject_env:
        env.update(load_user_env())

    cmd = [CLAUDE_EXE, "-p", prompt, "--output-format", "json"]
    if sources:
        cmd += ["--setting-sources", sources]
    if perm_mode:
        cmd += ["--permission-mode", perm_mode]
    if disallow:
        cmd += ["--disallowedTools", *disallow]

    t0 = time.time()
    try:
        p = subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=TIMEOUT,
        )
        rc, out, err = p.returncode, p.stdout, p.stderr
    except subprocess.TimeoutExpired:
        rc, out, err = "TIMEOUT", "", ""
    dur = round(time.time() - t0, 1)

    data = {}
    try:
        data = json.loads(out.strip().splitlines()[-1]) if out.strip() else {}
    except Exception:
        pass

    result = data.get("result") or ""
    denials = data.get("permission_denials") or []

    print(f"\n===== {name} =====")
    print(f"  cmd     : {' '.join(cmd[:1])} … sources={sources!r} "
          f"inject_env={inject_env} perm={perm_mode!r} disallow={disallow!r}")
    print(f"  rc      : {rc}   耗时 {dur}s")
    print(f"  is_error: {data.get('is_error')}   subtype: {data.get('subtype')!r}")
    print(f"  session : {'有' if data.get('session_id') else '无'}")
    print(f"  usage   : {'有' if data.get('usage') else '无'}")
    print(f"  denials : {len(denials)}"
          + (f" -> {[d.get('tool_name') for d in denials]}" if denials else ""))
    print(f"  result  : {result[:160]!r}")
    if SENTINEL in result:
        print(f"  ⚠️ 哨兵出现在输出里 ⇒ 该文件**被读到了**")
    if err.strip():
        print(f"  stderr  : {err.strip()[:300]!r}")
    return {"name": name, "rc": rc, "is_error": data.get("is_error"),
            "has_session": bool(data.get("session_id")),
            "denied": len(denials), "sentinel_leaked": SENTINEL in result}


def main():
    case = sys.argv[1] if len(sys.argv) > 1 else "all"

    work = tempfile.mkdtemp(prefix="ccbridge-verify-")
    target = os.path.join(work, "secret.txt")
    with open(target, "w", encoding="utf-8") as f:
        f.write(SENTINEL)
    target_fwd = target.replace("\\", "/")

    read_prompt = f"用 Read 工具读取文件 {target_fwd} 并把文件内容原样回复给我。"
    cases = {
        # 1. 基线：干净环境 + 只靠 settings.json 认证（= 桥未来的真实形态）
        "auth_default": dict(sources=None, inject_env=False, perm_mode=None,
                             disallow=None, prompt="只回复 OK"),
        # 2. 排掉 user 设置、也不注入 ⇒ 预期认证失败（证明隔离真的剥掉了认证）
        "auth_isolate_none": dict(sources="project,local", inject_env=False,
                                  perm_mode=None, disallow=None, prompt="只回复 OK"),
        # 3. 排掉 user 设置、自己注入 ⇒ 预期成功（证明 workaround 成立）
        "auth_isolate_inject": dict(sources="project,local", inject_env=True,
                                    perm_mode=None, disallow=None, prompt="只回复 OK"),
        # 4. bypassPermissions 单独是否可用
        "bypass_alone": dict(sources="project,local", inject_env=True,
                             perm_mode="bypassPermissions", disallow=None,
                             prompt="只回复 OK"),
        # 5. bypass 下 Write 是否放行
        "bypass_write": dict(sources="project,local", inject_env=True,
                             perm_mode="bypassPermissions", disallow=None,
                             prompt="用 Write 工具把 hello 写入 ./_perm_test.txt"),
        # 6. bypass 下 deny 是否仍生效（整条 Read 工具禁掉）
        "bypass_deny_readtool": dict(sources="project,local", inject_env=True,
                                     perm_mode="bypassPermissions",
                                     disallow=["Read", "Grep", "Bash"],
                                     prompt=read_prompt),
    }

    todo = list(cases) if case == "all" else [case]
    rows = []
    for name in todo:
        if name not in cases:
            print(f"未知 case: {name}\n可选: {', '.join(cases)}, all")
            return 2
        rows.append(run_case(name, cwd=work, **cases[name]))

    print("\n===== 汇总 =====")
    for r in rows:
        print(f"  {r['name']:<22} rc={str(r['rc']):<8} is_error={r['is_error']!s:<6} "
              f"session={r['has_session']!s:<6} denied={r['denied']} "
              f"哨兵泄漏={r['sentinel_leaked']}")
    print(f"\n临时目录（含哨兵文件，可删）: {work}")
    return 0


if __name__ == "__main__":
    sys.exit(main())