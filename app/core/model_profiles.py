"""模型级的端点 / 上下文窗口 / 工作目录：`data/model_profiles.json`。

**为什么需要它**（2026-11-09）：
    热切换（`core/model_store.py`）只改**模型名**这一个字符串，而
    `ANTHROPIC_BASE_URL` / `ANTHROPIC_AUTH_TOKEN` 是**进程级**的（`.env` 启动读一次）。
    于是"云端 deepseek"和"本地 Ollama"这种**不同端点**的模型放进同一张候选表时，
    切过去之后请求照样发往云端 —— 名字换了、路没换，必然回 "Model not exist"。

    这份文件把「模型名 → 环境变量 + 工作目录」变成静态配置：
      · **不在表里的模型** ⇒ 用 `.env` 那一套（老行为，一个字节都不变）；
      · **在表里的模型**   ⇒ 用它自己那一套，逐字段覆盖 `.env`。

    `cwd` 也是同一类问题：本地 9B 只有 32K 上下文，而私聊默认目录
    （`CC_CWD_DM`）的 `CLAUDE.md` 可能就有几万字符 ⇒ 光项目说明就撑爆。
    给本地模型一个只含它需要信息的小目录，才是"能跑"和"跑得动"的区别。

⚠️ **只允许注入 `ANTHROPIC_*` / `CLAUDE_CODE_*`。**
   别的键（`PATH` / `HOME` / `PYTHONPATH` / `COMSPEC`…）一旦能从这个数据文件改，
   就等于让一个配置文件拿到任意进程的控制权。白名单之外一律丢弃并报 ERROR。
"""

import json
import os
import re

from common import log

# 只有这两族能被模型配置覆盖。见模块头 "⚠️ 只允许注入"。
_ALLOWED_ENV = re.compile(r"^(ANTHROPIC_|CLAUDE_CODE_)", re.I)


class ModelProfiles:
    """静态配置表。**启动时读一次**（和 `.env` 同一档）—— 改它要重启桥。

    为什么不每条消息重读：候选名单 `CC_MODELS` 本来就来自 `.env`（也要重启
    才能加），而 `data/model.json` 只存"选了谁"。两者职责不同，别混。
    """

    def __init__(self, path, log_fn=None):
        self.path = path or ""
        self._log = log_fn or log
        self._profiles = {}
        self._load()

    # ---------------- 读 ----------------

    def _load(self):
        if not self.path:
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                raw = json.load(f)
        except FileNotFoundError:
            self._log.info("[model] 没有 %s ⇒ 所有模型都用 .env 那一套端点",
                           self.path)
            return
        except Exception as e:                       # noqa: BLE001
            self._log.error("[model] 读 %s 失败（%s: %s）"
                            " ⇒ 所有模型都用 .env 那一套",
                            self.path, type(e).__name__, e)
            return
        if not isinstance(raw, dict):
            self._log.error("[model] %s 顶层不是对象 ⇒ 整份忽略", self.path)
            return

        for name, spec in raw.items():
            if not isinstance(spec, dict):
                self._log.error("[model] %s：%r 的配置不是对象 ⇒ 忽略",
                                self.path, name)
                continue

            env = {}
            for k, v in (spec.get("env") or {}).items():
                k = str(k)
                if not _ALLOWED_ENV.match(k):
                    self._log.error(
                        "[model] %s：%r 的 env 键 %r 不在白名单"
                        "（只允许 ANTHROPIC_* / CLAUDE_CODE_*）⇒ 已丢弃",
                        self.path, name, k)
                    continue
                env[k] = str(v)

            cwd = spec.get("cwd") or None
            if cwd:
                cwd = os.path.expanduser(str(cwd))
                if not os.path.isdir(cwd):
                    # 宁可不切目录，也不要路由进一个不存在的路径（那样每一轮都
                    # 起不来，而错误看起来跟"模型坏了"一模一样）。
                    self._log.error(
                        "[model] %s：%r 的 cwd 不存在（%s）⇒ 已忽略，仍用默认目录",
                        self.path, name, cwd)
                    cwd = None

            self._profiles[str(name)] = {
                "env": env,
                "cwd": cwd,
                "label": str(spec.get("label") or ""),
            }

        if self._profiles:
            self._log.info("[model] 载入 %d 个模型配置：%s",
                           len(self._profiles), ", ".join(self._profiles))

    # ---------------- 查 ----------------

    def has(self, model):
        return bool(model) and model in self._profiles

    def env_for(self, model):
        """这个模型要额外注入的环境变量。**返回副本**，调用方随便改。"""
        p = self._profiles.get(model or "")
        return dict(p["env"]) if p else {}

    def cwd_for(self, model):
        """这个模型专用工作目录；没配就返回 None（= 用会话默认目录）。"""
        p = self._profiles.get(model or "")
        return p["cwd"] if p else None

    def label_for(self, model):
        p = self._profiles.get(model or "")
        return p["label"] if p else ""

    def names(self):
        return list(self._profiles)