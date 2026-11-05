#!/usr/bin/env python3
"""企业微信 ⇄ 本机 Claude Code 的桥。

传输 + 去重 + 白名单 + 串行队列 + runner + session 表 + 长短文本推送
+ 控制指令 + 常驻化（单实例 / 自启 / 看门狗 / 日志保留 / 阻止睡眠）。

群聊与**私聊**两条路径都支持：私聊回调里没有 `chatid`（用 `from.userid` 兜底），
因此私聊天然是独立会话。两者的 `cwd` **可以分开**：群聊走 `CC_CWD`，私聊走
`CC_CWD_DM`（不设就跟群聊同一个目录），见 `Bridge._cwd_for()`。

群聊与私聊**默认都挂审批卡片**（`CC_APPROVAL_DM` 默认 1）：只要审批整体开着
（`CC_APPROVAL=1`），两边都会注入审批 env、按同一套流程弹卡。
私聊全自动是**显式可选项**：设 `CC_APPROVAL_DM=0`，此时私聊不注入审批 env；
本项目提供的 hook 在无 env 时立刻放行（换用别的 hook 时，行为由那个 hook 决定）。

复用自 Pi Hub 的 app/wecom_bot.py（真机跑通），删掉了 MQTT / devctl / 指令词表 / 告警格式化。
刻意保留的行为（都有踩坑记录，别"顺手简化"）：
  · 心跳线程整个进程只起一个（放在重连循环里会无限累积）；
  · 退避连上即重置（原先只增不减，一次抖动之后每次断线都按封顶等）；
  · 长连接期间只有 ping 由我们发，run_forever(ping_interval=0)。

几条用实测换来的硬约束：
  · 判成功**只看 is_error**，不看 subtype —— 实测失败那次 subtype 仍报 'success'。
  · `--disallowedTools` 的拦截**不进 permission_denials**，模型只看到工具不存在。
  · 取消/超时必须杀**进程树**（taskkill /T /F），只杀父进程会留子进程。
  · 超时的语义是「结果未知」，不许报成「失败」。
"""

import argparse
import json
import logging
import os
import queue
import re
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from datetime import datetime, timedelta

import requests
import websocket  # websocket-client


# ---------------- 常量 / 配置 / 宿主基建（已抽层）----------------
# 兼容旧调用：`bridge.XXX` 在测试与 tools/probe_hooks.py 里被直接引用。
from common import (  # noqa: F401  （常量与配置）
    APPROVAL_POLL_SEC, BACKOFF_MAX, BACKOFF_START, DEFAULT_DB,
    DEFAULT_DISALLOWED, DEFAULT_PERM_MODE, ENV, HEARTBEAT_SEC, ID_FIELDS,
    LOGDIR, LOG_KEEP_DAYS, MUTEX_NAME, PROGRESS_HEARTBEAT_SEC,
    PROGRESS_RAW_CAP, ROOT, SEEN_KEEP_DAYS, SEND_WAIT_SEC, STALE_RX_SEC,
    STREAM_LATCH_SEC, STREAM_LIMIT, STREAM_MAX_SEC, TASK_NAME, WATCHDOG_SEC,
    WS_URL, cfg, cfg_bool, cfg_int, load_env, log, parse_labels, parse_list,
    whitelist_gate,
)
from host import (  # noqa: F401  （宿主基建）
    DailyFileHandler, _autostart_xml, _schtasks, _xml_escape,
    acquire_single_instance, autostart_status, install_autostart,
    prune_old_logs, setup_logging, start_keep_awake, uninstall_autostart,
)

# ---------------- 内核层（已抽层）----------------
from core.store import Store  # noqa: F401
from core.runner import (  # noqa: F401
    Runner, _clip, _kill_tree, _short_path, approval_configured,
    approval_selfcheck, tool_desc,
)
from core.text import (  # noqa: F401
    _hard_split, _looks_like_resume_failure, chunk_md, safe_to_retry_resume,
    split_half, usage_footer,
)


# ---------------- 分层装配（抽层第 3 刀）----------------
# 内核只通过 core/contract.py 的 plat_* 与平台交互；平台只通过 core_handle_*
# 把规范化事件交回内核。两者共用同一个实例（见 notes/PLATFORM-ADAPTER-EVAL.md 方案 A）。
from core.bridge_core import CoreMixin, ProgressPump  # noqa: F401
from platforms.wecom.adapter import WeComPlatformMixin
from platforms.wecom.codec import (  # noqa: F401
    WeComCodecMixin, find_msg_id, strip_at,
)
from platforms.wecom.outbound import (  # noqa: F401
    STREAM_LIMIT, Stream, WeComOutboundMixin,
)
from platforms.wecom.render import WeComRenderMixin  # noqa: F401
from platforms.wecom.transport import WeComTransportMixin


# ---------------- 桥本体：组装 ----------------
# ⚠️ **入口与行为不变**：`python app\bridge.py` 照旧；构造签名照旧；
# 测试与 tools/probe_hooks.py 直接摸的内部属性（ws / joined 的 _q / _receipt…）
# 全部仍挂在这同一个实例上。
class Bridge(CoreMixin, WeComPlatformMixin, WeComCodecMixin,
             WeComTransportMixin, WeComOutboundMixin, WeComRenderMixin):

    def __init__(self, bot_id, secret, allow_users, allow_chats, bot_name,
                 store, runner, cc_cwd, chunk_bytes, queue_max, progress_sec,
                 dump_raw=False, stale_rx_sec=0, progress_hb_sec=PROGRESS_HEARTBEAT_SEC,
                 approval_dir=None, approval_wait_sec=120, approval_allow="",
                 user_labels="", cc_cwd_dm="", approval_dm=True):
        self.bot_id = bot_id
        self.secret = secret
        self.allow_users = parse_list(allow_users)
        self.allow_chats = parse_list(allow_chats)
        self.bot_name = bot_name
        self.store = store
        self.runner = runner
        self.cc_cwd = cc_cwd
        # 私聊专用目录（`CC_CWD_DM`）。留空 = 和群聊共用 `CC_CWD`。
        self.cc_cwd_dm = cc_cwd_dm or cc_cwd
        # 私聊是否也走审批卡片（`CC_APPROVAL_DM`）。默认开 ⇒ 群聊+私聊都挂卡；
        # 设 0 才是“私聊全自动”的显式选择。
        self.approval_dm = bool(approval_dm)
        self.chunk_bytes = chunk_bytes
        self.progress_sec = progress_sec
        # 真进度：有气泡时由 ProgressPump 按"变化时推 + 这个间隔兜底"更新；
        # `progress_sec` 那个定时器只在**没有气泡**时兜底（见 `_do_job`）。
        self.progress_hb_sec = float(progress_hb_sec or 0)
        # 审批卡片：`approval_dir` 为 None = 关闭（默认）。开了之后每轮任务都把
        # 「决策目录 + 目标会话」注入 claude 的环境，PreToolUse hook 才肯干活。
        self.approval_dir = approval_dir or ""
        self.approval_wait_sec = float(approval_wait_sec or 0)
        self.approval_allow = approval_allow or ""
        # 卡片上给人看的名字（`WECOM_USER_LABELS`）：userid 是账号 ID，不是昵称。
        self.user_labels = parse_labels(user_labels)
        self._approvals = {}       # id -> {chatid, chat_type, userid, ts, tool}
        self._approval_lock = threading.Lock()
        # 「旁路消息」计数：本轮任务**除了流式气泡之外**又发出去的消息条数。
        # 气泡的位置由**创建时间**锁定，后面只改内容 ⇒ 中途发过卡（审批/状态）时，
        # 答案若写进气泡就会停在那几张卡**上方**。`_finish` 用它决定要不要另发一条。
        self._side_msgs = 0
        self._side_lock = threading.Lock()
        self.dump_raw = dump_raw
        self.stale_rx_sec = int(stale_rx_sec or 0)   # 0 = 关闭惰性连接检测（先量再开）

        self.ws = None
        self.subscribed = threading.Event()
        self.stop = False
        self._send_lock = threading.Lock()
        self._backoff = BACKOFF_START
        self._shape_logged = False

        self._q = queue.Queue(maxsize=queue_max)
        self._qmax = queue_max
        self._job_lock = threading.Lock()
        self._current = None       # {started, text, userid, chatid}

        # 会话"代次"：`/new` / 卡片「清空」时 +1。
        # 为什么需要（2026-10-29 外部复核 #5）：`/new` 清完库，**正在跑**的旧任务
        # 结束后仍会把旧 session 写回去 ⇒ 用户以为清空了，下一轮却又接上了旧上下文。
        # 任务入队时记下当时的代次，落库前比一次，变了就丢弃。
        self._epoch_lock = threading.Lock()
        self._epoch = {}           # (chatid, userid) -> int

        # 最近一次跑完的上下文占用（`📊 上下文 x% (m/n)` 那一行）。
        # 只放内存：/status 的卡片要显示它，但落库要改表结构，暂时不值得。
        # 重启后为空 ⇒ 卡片上就显示"—"，不编造。
        self._last_usage = {}      # (chatid, userid) -> "📊 上下文 …"

        # 推送回执等待：`_send` 返回 True 只说明 WS 帧发出去了，
        # 平台是**异步**用 errcode 回执拒收的（实测 40058 content exceed max length）。
        self._receipt_lock = threading.Lock()   # 同一时刻只让一个发送方在等
        self._receipt_cv = threading.Condition()
        self._receipt = None
        self._receipt_pending = False
        self._receipt_want = ""    # 正在等哪一帧的回执（req_id），用于过滤无关回执

        # Phase 4：看门狗用。`_last_rx` 是最后一次收到**任何**帧的时刻。
        self._last_rx = time.time()
        self._threads = {}          # name -> Thread，看门狗据此判断谁死了

        # 被动回复走独立队列 + 独立线程（不能卡住 websocket 读线程，见 reply()）
        self._reply_q = queue.Queue(maxsize=32)

    # -- 底层 --


def demo_card(kind):
    """构造一张**探针用**的模板卡片。只用来回答两个问题：
    ① 长连接机器人到底能不能发卡片？② 点了按钮，回调里回来的是什么？
    字段按官方 `101032` 的最小必填集写，故意不带 `card_action`（那要外链）。
    """
    ts = datetime.now().strftime("%H:%M:%S")
    # ⚠️ 官方文档把 `card_action` 标成"否"，但**实测必填**：
    # 不带它平台直接拒收 `errcode=42045 Template_Card card_action Missing or Invalid`
    # （2026-10-29 01:22，text_notice）。所以两种卡都带上一个跳转。
    jump = {"type": 1, "url": "https://work.weixin.qq.com"}
    if kind == "button":
        # 必填：main_title / button_list / task_id（task_id 同一机器人不许重复）
        return {
            "card_type": "button_interaction",
            "source": {"desc": "Agent-Ops-WeCom-Bridge 探针"},
            "main_title": {"title": "按钮卡片测试", "desc": "点任意一个，看回调里回来什么"},
            "sub_title_text": "这张卡片只用来验证长连接模式能否下发可点击卡片。",
            "button_list": [
                {"text": "允许", "style": 1, "key": "allow"},
                {"text": "只读", "style": 2, "key": "readonly"},
                {"text": "拒绝", "style": 3, "key": "deny"},
            ],
            "task_id": "ccprobe-" + datetime.now().strftime("%Y%m%d%H%M%S"),
            "card_action": jump,
        }
    # text_notice：main_title / sub_title_text 至少一项
    return {
        "card_type": "text_notice",
        "source": {"desc": "Agent-Ops-WeCom-Bridge 探针"},
        "main_title": {"title": "文本卡片测试", "desc": "长连接模式 · 无回调要求"},
        "sub_title_text": f"如果你看到这张卡片，说明长连接机器人能发 template_card（{ts}）。",
        "horizontal_content_list": [
            {"keyname": "card_type", "value": "text_notice"},
            {"keyname": "发起时间", "value": ts},
        ],
        "card_action": jump,
    }




def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dump-raw", action="store_true")
    ap.add_argument("--send-card", choices=["text", "button"], metavar="{text,button}",
                    help="发一张模板卡片探针（配合 --chatid；不传就用最近会话）")
    ap.add_argument("--send-test", metavar="TEXT")
    ap.add_argument("--send-test-file", metavar="PATH",
                    help="把文件内容整体推一条（测平台大小上限用，命令行放不下大文本）")
    ap.add_argument("--chatid")
    ap.add_argument("--chat-type", type=int, default=None,
                    help="1=单聊 2=群聊；不传则按最近会话自动判断")
    ap.add_argument("--send-auto", action="store_true",
                    help="配合 --send-test/-file：走 send_chunks（先整条、40058 才切成多片），"
                         "用来验证分片策略本身")
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("--install-autostart", action="store_true",
                    help="装进任务计划程序：登录时自动启动（pythonw，无窗口）")
    ap.add_argument("--uninstall-autostart", action="store_true")
    ap.add_argument("--autostart-status", action="store_true")
    args = ap.parse_args()

    setup_logging(args.verbose)

    # 这三个子命令不连平台，先处理掉，免得被后面的凭据检查挡住
    if args.install_autostart:
        ok, msg = install_autostart()
        print(msg)
        return 0 if ok else 1
    if args.uninstall_autostart:
        ok, msg = uninstall_autostart()
        print(msg)
        return 0 if ok else 1
    if args.autostart_status:
        rc, out = autostart_status()
        print(out)
        return 0 if rc == 0 else 1

    bot_id, secret = cfg("WECOM_BOT_ID"), cfg("WECOM_BOT_SECRET")
    if not bot_id or not secret:
        log.error("缺少 WECOM_BOT_ID / WECOM_BOT_SECRET —— 请填 %s",
                  os.path.join(ROOT, ".env"))
        return 2
    refusal = whitelist_gate(cfg("WECOM_ALLOWED_USERIDS"),
                             cfg_bool("WECOM_ALLOW_ANYONE", False))
    if refusal:
        log.error(refusal)
        return 3
    if not cfg("WECOM_ALLOWED_USERIDS"):
        log.warning("WECOM_ALLOW_ANYONE=1 ⇒ **任何人都能指挥它**，请确认这是你要的")

    # 除认证外，还要把 `wecom-cli` 的凭据目录透传给子进程:
    # 不透传的话，模型跑 `wecom-cli` 会落到默认目录
    # （那是另一个机器人的身份）。只透传 WECOM_CLI_*，别的变量不外泄。
    inject = {k: v for k, v in ENV.items()
              if k.startswith("ANTHROPIC") or k.startswith("WECOM_CLI_")}
    if not inject.get("ANTHROPIC_AUTH_TOKEN"):
        log.error("桥的 .env 里没有 ANTHROPIC_AUTH_TOKEN。"
                  "因为用 --setting-sources 排掉了 user 设置，认证必须自带。")
        return 2

    cc_cwd = cfg("CC_CWD", ROOT)
    # 私聊专用目录。留空 = 跟群聊共用 `CC_CWD`（老行为，原样兼容）。
    cc_cwd_dm = cfg("CC_CWD_DM") or cc_cwd
    if cc_cwd_dm != cc_cwd and not os.path.isdir(cc_cwd_dm):
        # **拒绝启动**，别静默退化成"私聊还在旧目录" —— 用户会以为切过去了，
        # 实际在那个旧项目里动手。
        log.error("[main] CC_CWD_DM 指向的目录不存在：%s", cc_cwd_dm)
        log.error("[main] 私聊会路由到这里；建好目录或改掉这个配置再启。")
        return 4
    # 私聊是否也挂审批卡。默认开：审批一旦启用（CC_APPROVAL=1），群聊和私聊都挂卡。
    # 设 0 是显式选择“私聊全自动”。
    approval_dm_raw = cfg("CC_APPROVAL_DM", "")
    approval_dm = (approval_dm_raw.strip().lower() not in ("0", "false", "no", "off", "")
                   ) if approval_dm_raw else True
    runner = Runner(
        exe=cfg("CC_CLAUDE_EXE", os.path.expanduser(r"~\.local\bin\claude.exe")),
        model=cfg("CC_MODEL"),
        # 默认 dontAsk：实测（tools/probe_permission_modes.py，2026-10-28）在 headless 下
        # **只有 plan / dontAsk / manual 真的拦住 Bash+Write**；acceptEdits 与 auto 照样执行。
        # dontAsk 比 plan 干净（plan 会往 ~/.claude/plans/ 写计划文件），且拒绝会进
        # permission_denials，桥能如实回报给用户。想让机器人干活再自己改成全自动。
        perm_mode=cfg("CC_PERMISSION_MODE", DEFAULT_PERM_MODE),
        sources=cfg("CC_SETTING_SOURCES", "project,local"),
        timeout=cfg_int("CC_TIMEOUT_SEC", 600),
        allowed=cfg("CC_ALLOWED_TOOLS"),
        disallowed=cfg("CC_DISALLOWED_TOOLS", DEFAULT_DISALLOWED),
        inject_env=inject,
        show_usage=cfg_bool("CC_SHOW_USAGE", True),
    )
    # 单实例保护：自启 + 手动启动/测试同时跑 = 双份回复、双份执行
    if not acquire_single_instance():
        # 返回 0：这是**正常**结果，不是失败 —— 任务计划程序每 3 分钟来敲一次门，
        # 活着的时候它就该安静地什么都不做（返回非 0 会被记成失败）。
        log.info("[main] 已有实例在跑（互斥量 %s），本次启动放弃。"
                 "要重启请先停掉常驻实例；测试发送同理。", MUTEX_NAME)
        return 0

    store = Store(cfg("CC_DB", DEFAULT_DB))
    # 审批卡片（默认关）：开了才会把决策目录注入子进程环境，hook 也才会干活。
    approval_dir = ""
    if cfg_bool("CC_APPROVAL", False):
        approval_dir = cfg("CC_APPROVAL_DIR") or os.path.join(ROOT, "data", "approvals")
        try:
            os.makedirs(approval_dir, exist_ok=True)
        except OSError as e:
            log.error("[approval] 建不出决策目录 %s：%s", approval_dir, e)
            return 5
        # 两道自检缺一不可：
        #   ① 静态 —— Claude **会不会加载**这个 hook（settings 注册 + setting-sources）
        #   ② 运行 —— 这个 hook **跑不跑得起来**（真起一个进程，要求它写出请求）
        # ⚠️ **每个实际会用到审批的 cwd 都要过一遍**：私聊路由到另一个工作区却只检
        # 群聊那个目录，等于给私聊留一道"看起来有、实际不跑"的空闸门。
        cwd_checks = [("群聊", cc_cwd)]
        if approval_dm and cc_cwd_dm != cc_cwd:
            cwd_checks.append(("私聊", cc_cwd_dm))
        for label, d in cwd_checks:
            ok_cfg, why_cfg = approval_configured(d, runner.sources)
            ok_run, why_run = (False, "（静态检查没过，未执行）")
            if ok_cfg:
                ok_run, why_run = approval_selfcheck(d, approval_dir)
            if not (ok_cfg and ok_run):
                # ⚠️ **拒绝启动**，不再"降级成不启用审批"（2026-10-29 复核 #2①）。
                # 原写法：自检失败只打一行 ERROR，然后把 approval_dir 置空照常启动 ——
                # 用户明明按了"要审批"，实际却在**全自动无闸**状态下跑。那和"假装有闸门"没区别。
                log.error("[approval] 你设了 CC_APPROVAL=1，但 %s 的审批**没法真正生效**"
                          " —— 拒绝启动。目录：%s", label, d)
                log.error("[approval] ① 加载链路：%s", why_cfg)
                log.error("[approval] ② 脚本可跑：%s", why_run)
                log.error("[approval] 修好再启；或不想要审批就把 .env 里的 CC_APPROVAL 改成 0"
                          "（那是一次显式选择，不会有闸门的错觉）。")
                return 5
            log.info("[approval] %s 自检通过：%s；hook 能写出请求。", label, why_cfg)
        log.info("[approval] 审批已开启（群聊%s）—— 等待 %ss、只读直放=%s",
                 " + 私聊" if approval_dm else "；私聊已显式设为全自动",
                 cfg_int("CC_APPROVAL_WAIT_SEC", 120),
                 cfg("CC_APPROVAL_ALLOW") or "(默认 Read,Grep,Glob,TodoWrite)")
    elif approval_dm_raw:
        # 显式设了 CC_APPROVAL_DM、但审批整体没开 ⇒ 别让用户以为私聊在挂卡。
        log.warning("[approval] 你显式设了 CC_APPROVAL_DM，但 CC_APPROVAL=0 ⇒ "
                    "审批整体未启用，群聊和私聊都不会弹卡。")
    bridge = Bridge(bot_id, secret, cfg("WECOM_ALLOWED_USERIDS"),
                    cfg("WECOM_ALLOWED_CHATIDS"), cfg("WECOM_BOT_NAME"),
                    store, runner, cc_cwd,
                    cfg_int("CHUNK_BYTES", 18000), cfg_int("QUEUE_MAX", 20),
                    cfg_int("CC_PROGRESS_SEC", 90), dump_raw=args.dump_raw,
                    stale_rx_sec=cfg_int("CC_STALE_RX_SEC", STALE_RX_SEC),
                    progress_hb_sec=cfg_int("CC_PROGRESS_HEARTBEAT_SEC",
                                            PROGRESS_HEARTBEAT_SEC),
                    approval_dir=approval_dir,
                    approval_wait_sec=cfg_int("CC_APPROVAL_WAIT_SEC", 120),
                    approval_allow=cfg("CC_APPROVAL_ALLOW"),
                    user_labels=cfg("WECOM_USER_LABELS"),
                    cc_cwd_dm=cc_cwd_dm, approval_dm=approval_dm)

    if cfg_bool("CC_KEEP_AWAKE", True):
        start_keep_awake()

    log.info("[main] cwd=%s%s 档位=%s 模型=%s", cc_cwd,
             (f"（私聊={cc_cwd_dm}，审批卡={'开' if approval_dm else '关'}）"
              if cc_cwd_dm != cc_cwd else
              f"（私聊与群聊同目录，审批卡={'开' if approval_dm else '关'}）"),
             runner.perm_mode, runner.model or "(默认别名)")
    if runner.perm_mode in ("bypassPermissions", "auto", "acceptEdits"):
        log.warning("[main] 档位=%s ⇒ 模型会**直接执行本机命令、直接改文件**，"
                    "不弹任何确认（实测这三档在 headless 下都不拦 Bash）。"
                    "请确认白名单里只有你自己。", runner.perm_mode)

    if args.send_test or args.send_test_file or args.send_card:
        payload = args.send_test or ""
        if args.send_test_file:
            with open(args.send_test_file, encoding="utf-8") as f:
                payload = f.read()
        chatid, derived_type = (args.chatid, None) if args.chatid else store.last_chat()
        if not chatid:
            log.error("[test] 无目标 chatid：先让用户给机器人发条消息，或显式 --chatid")
            return 4
        chat_type = args.chat_type if args.chat_type is not None else (derived_type or 2)
        if args.send_card:
            log.info("[test] 目标 chatid=%s type=%s 卡片=%s",
                     chatid, chat_type, args.send_card)
        else:
            nbytes = len(payload.encode("utf-8"))
            log.info("[test] 目标 chatid=%s type=%s 内容 %d 字节（%d 字符）",
                     chatid, chat_type, nbytes, len(payload))

        bridge.ws = websocket.WebSocketApp(
            WS_URL, on_open=bridge._on_open, on_message=bridge._on_message,
            on_error=bridge._on_error, on_close=bridge._on_close)
        threading.Thread(target=bridge.ws.run_forever,
                         kwargs={"ping_interval": 0}, daemon=True).start()
        if not bridge.subscribed.wait(timeout=15):
            log.error("[test] 订阅超时（15s）")
            return 3
        time.sleep(1)
        if args.send_card:
            r = bridge.send_card(chatid, chat_type, demo_card(args.send_card))
            log.info("[test] 卡片回执: %s", r)
            log.info("[test] >>> %s", "卡片送达" if r["sent"] else "卡片被拒收/未知")
            ok = r["sent"]
            if ok and args.send_card == "button":
                # 卡片是**要人点**的，不睡一会儿就 close，事件还没回来连接就没了。
                log.info("[test] 等 180 秒收点击回调（点了按钮才会打印 event）…")
                time.sleep(180)
        elif args.send_auto:
            failed = bridge.send_chunks(chatid, chat_type, payload)
            log.info("[test] send_chunks 未送达段数: %d", len(failed))
            log.info("[test] >>> %s", "全部送达" if not failed else "有段落未送达")
            ok = not failed
        else:
            r = bridge.send_markdown(chatid, chat_type, payload)
            log.info("[test] 平台回执结果: %s", r)
            log.info("[test] >>> %s", "送达" if r["sent"] else "被拒收/未知")
            ok = r["sent"]
        time.sleep(2)
        bridge.stop_bot()
        return 0 if ok else 1

    try:
        bridge.run_forever()
    except KeyboardInterrupt:
        log.info("[main] 退出")
        bridge.stop_bot()
    return 0


if __name__ == "__main__":
    sys.exit(main())
