"""文本后处理：按字节切分、上下文用量 footer、resume 失败判据。

这里**只做纯字符串运算**，不碰网络、不碰数据库 ⇒ 可单独测、可被任何平台复用。
⚠️ `chunk_md` 的默认上限是**调用方给的**（企微 20480 字节），不是本模块的假设。
"""

import re

def _hard_split(s, limit):
    """按 UTF-8 字节硬切，绝不切开一个字符。"""
    out, cur, n = [], [], 0
    for ch in s:
        b = len(ch.encode("utf-8"))
        if n + b > limit and cur:
            out.append("".join(cur))
            cur, n = [], 0
        cur.append(ch)
        n += b
    if cur:
        out.append("".join(cur))
    return out


def split_half(s):
    """按 UTF-8 字节尽量对半切（绝不切开一个字符）。切不动就原样返回。"""
    total = len(s.encode("utf-8"))
    if total < 1024:
        return [s]
    target = total // 2
    acc = 0
    for i, ch in enumerate(s):
        acc += len(ch.encode("utf-8"))
        if acc >= target:
            head, tail = s[:i + 1], s[i + 1:]
            return [head, tail] if head and tail else [s]
    return [s]


def chunk_md(text, limit_bytes=18000, reserve=24):
    """按 **UTF-8 字节**切分（不是字符数 —— 中文 3 字节/字）。优先在段落处切。"""
    lim = max(256, limit_bytes - reserve)
    if len((text or "").encode("utf-8")) <= lim:
        return [text or ""]
    paras = re.split(r"(\n{2,})", text)
    chunks, cur, cur_b = [], "", 0
    for p in paras:
        pb = len(p.encode("utf-8"))
        if pb > lim:
            if cur:
                chunks.append(cur)
                cur, cur_b = "", 0
            pieces = _hard_split(p, lim)
            # 除最后一片外都已塞满，直接落定；**最后一片留作当前片**，
            # 后面的段落还能接上 —— 否则会多出一条只含尾声（比如一行 END 标记）的
            # 空荡荡的留言（实测 21KB 被切成 17976 + 2957 + 35 三片）。
            chunks.extend(pieces[:-1])
            cur = pieces[-1]
            cur_b = len(cur.encode("utf-8"))
            continue
        if cur_b + pb > lim:
            chunks.append(cur)
            cur, cur_b = "", 0
        cur += p
        cur_b += pb
    if cur:
        chunks.append(cur)
    return chunks or [""]


def usage_footer(data, last_usage=None, last_model=None):
    """上下文占用。

    ⚠️ **分子必须取"本轮最后一次 assistant 调用"的 usage，不能取 modelUsage 的
    累计值。** `modelUsage` 是一次 `claude -p` 里**所有 API 调用**的合计 ——
    只要这轮用过工具，同一段上下文就会被反复重发，累计值随调用次数线性膨胀。
    实测一个 4~5 次调用的普通任务：`modelUsage` 合计 93.7k，而最后一次调用的
    prompt 只有 45k（窗口 1M）⇒ 用累计值会报 9.4%，真实占用只有 4.5%。
    工具调用再多几轮，累计值就会超过窗口本身，出现 783.8% 这种假读数。

    分母仍取 `modelUsage[<model>].contextWindow`（实测存在，不猜）。
    """
    mu = data.get("modelUsage") or {}
    if not mu:
        return ""
    info = None
    if last_model:
        info = mu.get(last_model)
        if info is None:
            # 模型名可能带后缀（例如 `[1m]`），按前缀双向匹配一次
            for name, v in mu.items():
                if name.startswith(last_model) or last_model.startswith(name):
                    info = v
                    break
    if info is None:
        # 多模型（主模型 + 子任务模型）时取窗口最大的那个，比 next(iter()) 稳
        info = max(mu.values(), key=lambda x: x.get("contextWindow") or 0)
    win = info.get("contextWindow") or 0
    if not win or not last_usage:
        # 没拿到逐条 assistant usage 时**宁可不显示**，也不拿累计值冒充上下文
        return ""
    used = sum(int(last_usage.get(k) or 0) for k in (
        "input_tokens", "cache_read_input_tokens",
        "cache_creation_input_tokens", "output_tokens"))
    if used <= 0:
        return ""
    pct = used * 100.0 / win

    def k(n):
        return f"{n / 1_000_000:.1f}M" if n >= 1_000_000 else f"{n / 1000:.1f}k"

    return f"\n\n> 📊 上下文 {pct:.1f}% ({k(used)}/{k(win)})"


def _looks_like_resume_failure(text):
    t = (text or "").lower()
    return any(s in t for s in ("no conversation found", "session not found",
                                "could not find session", "invalid session"))


def safe_to_retry_resume(res):
    """resume 失效时，**这一轮能不能安全重跑**？返回 (ok, 原因)。

    判据是"第一轮到底动没动手"，不是"错误文本像不像"（2026-10-29 复核 #3）：
      · `tools > 0`        —— 调过工具，可能已经改了文件 / 提交过 / 发过消息
      · `stream_failed`    —— 流断了，执行到哪一步不知道
      · `stopped`          —— 被 `/stop` 打断过
      · `timed_out`        —— 超时过，可能已执行了一部分
    占任意一条就**不能**重跑：重跑 = 把整条指令再执行一遍 ⇒ 副作用重复
    （重复写入、重复提交、重复调外部服务）。宁可让用户自己核实后再发。
    """
    if not (res.get("is_error") and _looks_like_resume_failure(res.get("text") or "")):
        return False, "不是 resume 失效"
    if res.get("tools"):
        return False, f"本轮已执行 {res['tools']} 次工具调用"
    if res.get("stream_failed"):
        return False, "流中断，结果未知"
    if res.get("stopped"):
        return False, "本轮被 /stop 打断过"
    if res.get("timed_out"):
        return False, "本轮超时过"
    return True, "确认这一轮没动手"
