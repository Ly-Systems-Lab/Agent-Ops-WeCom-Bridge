#!/usr/bin/env python3
"""企业微信 ⇄ 本机 Claude Code 的桥。

传输 + 去重 + 白名单 + 串行队列 + runner + session 表 + 长短文本推送
+ 控制指令 + 常驻化（单实例 / 自启 / 看门狗 / 日志保留 / 阻止睡眠）。

群聊与**私聊**两条路径都支持：私聊回调里没有 `chatid`（用 `from.userid` 兜底），
因此私聊天然是独立会话。两者的 `cwd` **可以分开**：群聊走 `CC_CWD`，私聊走
`CC_CWD_DM`（不设就跟群聊同一个目录），见 `Bridge._cwd_for()`。

**谁弹卡，按发消息的人分，不按聊天类型**（2026-11-10 起）：只要审批整体开着
（`CC_APPROVAL=1`），群聊私聊**都注入**审批 env；列进 `CC_APPROVAL_AUTO_USERS`
的 userid 带一个 `CC_APPROVAL_AUTO=1` 标志，hook 见到它就放行、不弹卡，其余人照常弹卡。

为什么不用"不注入 env"来表达免卡（旧 `CC_APPROVAL_DM` 的写法）：那样免卡用户的
**静态安全规则也会一起失效**（`tools/approval_hook.py` 里的凭据存储硬 deny），
而且"没配上"会静默退化成"谁都能直接执行"。现在配错只会退化成"弹卡"。

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

import requests
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
    WS_URL, cfg, cfg_bool, cfg_int, default_approval_dir, load_env, log,
    parse_labels, parse_list, whitelist_gate,
)
from host import (  # noqa: F401  （宿主基建）
    DailyFileHandler, _autostart_xml, _schtasks, _xml_escape,
    acquire_single_instance, autostart_status, install_autostart,
    prune_old_logs, setup_logging, start_keep_awake, uninstall_autostart,
)

# ---------------- 内核层（已抽层）----------------
from core.store import Store  # noqa: F401
from core.model_store import ModelStore  # noqa: F401
from core.model_profiles import ModelProfiles  # noqa: F401
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
# 把规范化事件交回内核。两者共用同一个实例。
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

# 飞书（`PLATFORM=feishu` 时装配这一套）。两套适配器**互不 import**，
# 装配只在下面两个桥类里发生。
from platforms.feishu.adapter import FeishuPlatformMixin
from platforms.feishu.codec import FeishuCodecMixin
from platforms.feishu.outbound import FeishuOutboundMixin
from platforms.feishu.render import FeishuRenderMixin
from platforms.feishu.transport import FeishuTransportMixin

# Slack（`PLATFORM=slack` 时装配这一套）。三套适配器**互不 import**，
# 装配只在下面几个桥类里发生。
from platforms.slack.adapter import SlackPlatformMixin
from platforms.slack.codec import SlackCodecMixin
from platforms.slack.outbound import SlackOutboundMixin
from platforms.slack.render import SlackRenderMixin
from platforms.slack.transport import SlackTransportMixin


# ---------------- 桥本体：组装 ----------------
# ⚠️ **入口与行为不变**：`python app\bridge.py` 照旧；企微桥的构造签名照旧；
# 测试与 tools/probe_hooks.py 直接摸的内部属性（ws / _q / _receipt…）
# 全部仍挂在同一个实例上。
class _BridgeCommon(CoreMixin):
    """两个平台共用的状态。**平台无关**，所以抽在这里而不是各抄一份。

    判据很简单：这些东西内核会直接用（`_worker` / `_approval_loop` / `_finish`…），
    与"用哪个聊天平台"无关。抄成两份的唯一后果是将来改一处忘一处。
    """

    def _init_common(self, allow_users, allow_chats, bot_name,
                     store, runner, cc_cwd, chunk_bytes, queue_max, progress_sec,
                     dump_raw=False, stale_rx_sec=0,
                     progress_hb_sec=PROGRESS_HEARTBEAT_SEC,
                     approval_dir=None, approval_wait_sec=120, approval_allow="",
                     approval_auto_users="", user_labels="", cc_cwd_dm="",
                     model_store=None):
        self.allow_users = parse_list(allow_users)
        self.allow_chats = parse_list(allow_chats)
        self.bot_name = bot_name
        self.store = store
        self.runner = runner
        # 模型的唯一真源（core/model_store.py）。None = 本进程没启用热切换
        # （老单测这么构造）⇒ `/model` 会如实回"没有启用"，而不是静默不干活。
        self.model_store = model_store
        # 哪些工作目录的审批 hook 已经查过（每个目录只查/只报一次）。
        self._gate_checked = set()
        self.cc_cwd = cc_cwd
        # 私聊专用目录（`CC_CWD_DM`）。留空 = 和群聊共用 `CC_CWD`。
        self.cc_cwd_dm = cc_cwd_dm or cc_cwd
        # 免卡白名单（`CC_APPROVAL_AUTO_USERS`）：**按发消息的人**，不按聊天类型。
        # 列进来的 userid 不弹卡，其余一律弹卡（群聊、私聊都一样）。
        # ⚠️ 免卡 ≠ 免检：凭据存储的硬 deny 在 hook 里排在这个标志**之前**，
        #    所以白名单用户也读不到 `~/.dws` 那一类路径。
        self.approval_auto_users = (parse_list(approval_auto_users)
                                    if isinstance(approval_auto_users, str)
                                    else set(approval_auto_users or ()))
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
        # 卡片上给人看的名字（`WECOM_USER_LABELS` / `FEISHU_USER_LABELS`）。
        self.user_labels = parse_labels(user_labels)
        self._approvals = {}       # id -> {chatid, chat_type, userid, ts, tool}
        self._approval_lock = threading.Lock()
        # 「旁路消息」计数：本轮任务**除了流式气泡之外**又发出去的消息条数。
        # 气泡的位置由**创建时间**锁定，后面只改内容 ⇒ 中途发过卡（审批/状态）时，
        # 答案若写进气泡就会停在那几张卡**上方**。`_finish` 用它决定要不要另发一条。
        # ⚠️ **按会话记**（2026-11-05 起会话可并行）：别的窗口发了一张审批卡，
# 不能让这个窗口以为自己中途发过东西、把答案另发一条。
        # 键 = (chatid, chat_type)；`None` 那一格给"只知道 token、不知道会话"的被动回复用。
        self._side_msgs = {}
        self._side_lock = threading.Lock()
        # 「这一轮是被『拒绝』终止的」——收尾时要把话说得和 `/stop` 不一样，
        # 否则用户点了拒绝，看到的却是"已按 /stop 停止"，会以为是别的东西停了。
        # ⚠️ 按**会话**记（2026-11-05 起不同会话可并行）：键 = (chatid, chat_type)。
        self._stopped_by_deny = {}
        self.dump_raw = dump_raw
        self.stale_rx_sec = int(stale_rx_sec or 0)   # 0 = 关闭惰性连接检测（先量再开）

        self.stop = False
        self._backoff = BACKOFF_START

        self._q = queue.Queue(maxsize=queue_max)
        self._qmax = queue_max
        self._job_lock = threading.Lock()
        # 「正在跑的任务」是**按会话**记的（2026-11-05 用户拍板：同一会话串行、
        # 不同会话并行）。键 = (chatid, chat_type)。
        # ⚠️ 入队那一刻就占位，不是等 worker 拿到才占 —— 中间那个空档里如果
        # 同一会话再来一条，会被误判成"空闲"。
        self._current = {}         # (chatid, chat_type) -> {started, text, userid, chatid}
        self._runners = {}         # (chatid, chat_type) -> Runner（停任务要按会话停）
        # 同时最多几个任务。两个窗口（群聊/私聊）够用，再多人也读不过来；
        # 每个任务是一个 claude 进程（约 250MB），别让它无限长。
        self.max_concurrent = max(1, int(os.environ.get("CC_MAX_CONCURRENT") or 2))

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

        self._threads = {}          # name -> Thread，看门狗据此判断谁死了


class WeComBridge(_BridgeCommon, WeComPlatformMixin, WeComCodecMixin,
                  WeComTransportMixin, WeComOutboundMixin, WeComRenderMixin):

    def __init__(self, bot_id, secret, allow_users, allow_chats, bot_name,
                 store, runner, cc_cwd, chunk_bytes, queue_max, progress_sec,
                 dump_raw=False, stale_rx_sec=0, progress_hb_sec=PROGRESS_HEARTBEAT_SEC,
                 approval_dir=None, approval_wait_sec=120, approval_allow="",
                 approval_auto_users="", user_labels="", cc_cwd_dm="",
                 model_store=None):
        self.bot_id = bot_id
        self.secret = secret
        self._init_common(allow_users, allow_chats, bot_name, store, runner, cc_cwd,
                          chunk_bytes, queue_max, progress_sec,
                          dump_raw=dump_raw, stale_rx_sec=stale_rx_sec,
                          progress_hb_sec=progress_hb_sec, approval_dir=approval_dir,
                          approval_wait_sec=approval_wait_sec,
                          approval_allow=approval_allow, user_labels=user_labels,
                          approval_auto_users=approval_auto_users,
                          cc_cwd_dm=cc_cwd_dm,
                          model_store=model_store)

        self.ws = None
        self.subscribed = threading.Event()
        self._send_lock = threading.Lock()
        self._shape_logged = False

        # 推送回执等待：`_send` 返回 True 只说明 WS 帧发出去了，
        # 平台是**异步**用 errcode 回执拒收的（实测 40058 content exceed max length）。
        self._receipt_lock = threading.Lock()   # 同一时刻只让一个发送方在等
        self._receipt_cv = threading.Condition()
        self._receipt = None
        self._receipt_pending = False
        self._receipt_want = ""    # 正在等哪一帧的回执（req_id），用于过滤无关回执

        # Phase 4：看门狗用。`_last_rx` 是最后一次收到**任何**帧的时刻。
        self._last_rx = time.time()

        # 被动回复走独立队列 + 独立线程（不能卡住 websocket 读线程，见 reply()）
        self._reply_q = queue.Queue(maxsize=32)


class FeishuBridge(_BridgeCommon, FeishuPlatformMixin, FeishuCodecMixin,
                   FeishuTransportMixin, FeishuOutboundMixin, FeishuRenderMixin):
    """飞书桥。与 `WeComBridge` 同一个内核，只换平台层。

    ⚠️ 两处与企微**本质不同**、都在各自的文件里写了原因：
      · 事件在 asyncio 循环线程上**同步派发** ⇒ 消息入口只入队（codec）；
      · 卡片回调必须**同步返回**新卡片 ⇒ 就地改卡走回调上下文（outbound/codec）。
    """

    def __init__(self, app_id, app_secret, allow_users, allow_chats, bot_name,
                 store, runner, cc_cwd, chunk_bytes, queue_max, progress_sec,
                 dump_raw=False, stale_rx_sec=0, progress_hb_sec=PROGRESS_HEARTBEAT_SEC,
                 approval_dir=None, approval_wait_sec=120, approval_allow="",
                 approval_auto_users="", user_labels="", cc_cwd_dm="",
                 model_store=None, feishu_chunk_bytes=130000):
        self.app_id = app_id
        self.app_secret = app_secret
        self._init_common(allow_users, allow_chats, bot_name, store, runner, cc_cwd,
                          chunk_bytes, queue_max, progress_sec,
                          dump_raw=dump_raw, stale_rx_sec=stale_rx_sec,
                          progress_hb_sec=progress_hb_sec, approval_dir=approval_dir,
                          approval_wait_sec=approval_wait_sec,
                          approval_allow=approval_allow, user_labels=user_labels,
                          approval_auto_users=approval_auto_users,
                          cc_cwd_dm=cc_cwd_dm,
                          model_store=model_store)

        # 切分粒度按**飞书**的上限来（150 KiB），不照搬企微的 18000 ——
        # 照搬只会把一条 60 KB 的回答无谓地拆成四片。
        self.feishu_chunk_bytes = int(feishu_chunk_bytes or 130000)

        self.cli = self.feishu_new_client()   # HTTP 侧（推送 / 建卡 / 改卡）
        self.feishu_ws = None                 # 长连接客户端（transport 里建）
        self._shape_logged = False

        # 入站队列：事件循环线程只负责入队，处理在 `feishu-inbound` 线程上。
        # 消息本来就是低频的，64 条足够；满了会**出声丢弃**（见 codec）。
        self._in_q = queue.Queue(maxsize=64)
        self._in_qmax = 64

        # 卡片回调上下文：**一次回调一个**，所以用 threading.local。
        # 成功路径上它会持有 `resp`（= P2CardActionTriggerResponse），
        # `plat_approval_update_card` 把新卡片挂进去，codec 在返回时交出去。
        self._card_ctx = threading.local()

        # 发送限流（实测 12 条/秒安全，取 5 条/秒）
        self._rate_lock = threading.Lock()
        self._rate_events = []

        # 被动回复（与企微同一个理由：不能占住事件循环线程）
        self._reply_q = queue.Queue(maxsize=32)


class SlackBridge(_BridgeCommon, SlackPlatformMixin, SlackCodecMixin,
                  SlackTransportMixin, SlackOutboundMixin, SlackRenderMixin):
    """Slack 桥。与另外两个同一个内核，只换平台层。

    ⚠️ 两处与企微**本质不同**、都在各自的文件里写了原因：
      · **超长是静默截断**（不是报错）⇒ 分片必须预切（outbound）；
      · **空频道里没有任何帧**（没有我们自己的心跳）⇒ 活性判据不能照抄企微的 90 秒。
    """

    def __init__(self, bot_token, app_token, allow_users, allow_chats, bot_name,
                 store, runner, cc_cwd, chunk_bytes, queue_max, progress_sec,
                 dump_raw=False, stale_rx_sec=0, progress_hb_sec=PROGRESS_HEARTBEAT_SEC,
                 approval_dir=None, approval_wait_sec=120, approval_allow="",
                 approval_auto_users="", user_labels="", cc_cwd_dm="",
                 model_store=None, slack_chunk_chars=3800, bot_user_id=""):
        self.slack_bot_token = bot_token
        self.slack_app_token = app_token
        self.slack_bot_user_id = bot_user_id
        self._init_common(allow_users, allow_chats, bot_name, store, runner, cc_cwd,
                          chunk_bytes, queue_max, progress_sec,
                          dump_raw=dump_raw, stale_rx_sec=stale_rx_sec,
                          progress_hb_sec=progress_hb_sec, approval_dir=approval_dir,
                          approval_wait_sec=approval_wait_sec,
                          approval_allow=approval_allow, user_labels=user_labels,
                          approval_auto_users=approval_auto_users,
                          cc_cwd_dm=cc_cwd_dm,
                          model_store=model_store)

        # 切分粒度按**字符**算（Slack 的上限是字符，不是字节）。
        self.slack_chunk_chars = max(500, int(slack_chunk_chars or 3800))

        self.ws = None
        self.subscribed = threading.Event()
        self._send_lock = threading.Lock()
        self._shape_logged = False

        # 入站队列：websocket 读线程只负责入队，处理在 `slack-inbound` 线程上。
        self._in_q = queue.Queue(maxsize=64)
        self._in_qmax = 64

        # 节流表：post 按频道 1 条/秒，update 按频道约 1 条/1.25 秒。
        self._rate_lock = threading.Lock()
        self._rate_last = {}

        # 「这个频道最近在哪个线程里」—— 让主动推送也落进同一个线程，
        # 否则单聊之外的回答会散在频道顶层，用户得自己拼上下文。
        self._slack_thread_of = {}

        # 线程准入表（`app_mention` 记过哪些线程）
        self._slack_load_threads()


# 兼容旧写法：测试 / `tools/probe_hooks.py` / 文档里都写 `bridge.Bridge(...)`。
# 它一直是企微那套，现在只是换成一个说清楚平台的名字。
Bridge = WeComBridge


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

    # ---------------- 平台选择 ----------------
    # `.env` 的 `PLATFORM` 决定装配哪一套适配器。**入口本身不变** ——
    # `python app\bridge.py` 照旧，别人接过的文档不失效。
    platform = (cfg("PLATFORM", "wecom") or "wecom").strip().lower()
    if platform not in ("wecom", "feishu", "slack"):
        log.error("PLATFORM=%r 不认识（只支持 wecom / feishu / slack）", platform)
        return 2

    app_id = app_secret = bot_id = secret = ""
    slack_bot_token = slack_app_token = ""
    if platform == "slack":
        # 两个 token 缺一不可，而且**来源不同**：
        #   SLACK_BOT_TOKEN（xoxb-）发消息用；SLACK_APP_TOKEN（xapp-）建 Socket Mode 用。
        # 拿一个顶两个是最常见的错 —— 自检见 tools/slack_cred.py。
        slack_bot_token = cfg("SLACK_BOT_TOKEN")
        slack_app_token = cfg("SLACK_APP_TOKEN")
        if not slack_bot_token or not slack_app_token:
            log.error("缺少 SLACK_BOT_TOKEN / SLACK_APP_TOKEN —— 请填 %s"
                      "（不确定类型就跑 tools/slack_cred.py）",
                      os.path.join(ROOT, ".env"))
            return 2
        # 白名单口径是 **member ID（U…）**，不是 @handle：handle 能改、能被别人抢。
        refusal = whitelist_gate(cfg("SLACK_ALLOWED_USERIDS"),
                                 cfg_bool("SLACK_ALLOW_ANYONE", False),
                                 key="SLACK_ALLOWED_USERIDS", what="member ID",
                                 anyone_key="SLACK_ALLOW_ANYONE")
        if refusal:
            log.error(refusal)
            return 3
        if not cfg("SLACK_ALLOWED_USERIDS"):
            log.warning("SLACK_ALLOW_ANYONE=1 ⇒ **任何人都能指挥它**，请确认这是你要的")
    elif platform == "feishu":
        app_id, app_secret = cfg("FEISHU_APP_ID"), cfg("FEISHU_APP_SECRET")
        if not app_id or not app_secret:
            log.error("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET —— 请填 %s",
                      os.path.join(ROOT, ".env"))
            return 2
        # ⚠️ 白名单口径是 **open_id**（应用内唯一）：换一个飞书应用要重填。
        # 这一点写进 .env 注释里，免得将来"换了应用突然全被拒"被当成 bug 排查。
        refusal = whitelist_gate(cfg("FEISHU_ALLOWED_OPENIDS"),
                                 cfg_bool("FEISHU_ALLOW_ANYONE", False),
                                 key="FEISHU_ALLOWED_OPENIDS", what="open_id",
                                 anyone_key="FEISHU_ALLOW_ANYONE")
        if refusal:
            log.error(refusal)
            return 3
        if not cfg("FEISHU_ALLOWED_OPENIDS"):
            log.warning("FEISHU_ALLOW_ANYONE=1 ⇒ **任何人都能指挥它**，请确认这是你要的")
    else:
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
    if platform == "feishu":
        try:
            import lark_oapi  # noqa: F401
        except ImportError:
            log.error("[main] 缺少依赖 lark-oapi —— 飞书适配器要用它。"
                      "装：%s -m pip install lark-oapi", sys.executable)
            return 2

    slack_bot_user_id = ""
    if platform == "slack":
        # `auth.test` 一次拿三样：token 是否有效、机器人自己的 user_id
        # （剥 `<@U…>` 和过滤"自己发的消息"都要它）、以及所属 workspace。
        # 放在启动时做，是为了**凭据错了当场拒绝启动**，而不是等第一条消息不来。
        try:
            _r = requests.post(
                "https://slack.com/api/auth.test",
                headers={"Authorization": "Bearer " + slack_bot_token},
                timeout=15).json()
        except requests.RequestException as e:
            # ⚠️ **网络不通 ≠ 凭据错**，不能因此拒绝启动（2026-11-07 实测踩到：
            # 开机时 slack.com 连接超时 15 秒，桥直接 return 2 ⇒ 常驻服务起不来，
            # 只能等下一次计划任务触发）。网络恢复后由 transport 在建连成功时补解析身份。
            log.warning("[main] 暂时连不上 slack.com（%s）—— 先启动，"
                        "连上后再补校验 token。", type(e).__name__)
        else:
            if not _r.get("ok"):
                log.error("[main] SLACK_BOT_TOKEN 无效：%s"
                          "（是不是填成了 user token？跑 tools/slack_cred.py 看类型）",
                          _r.get("error"))
                return 2
            slack_bot_user_id = _r.get("user_id") or ""
            log.info("[main] Slack workspace=%s（%s）bot=%s",
                     _r.get("team"), _r.get("team_id"), slack_bot_user_id)
    log.info("[main] 平台 = %s", platform)

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
    # 审批卡**按发消息的人**分，不按聊天类型（2026-11-10，P1-3）：
    #   列进 `CC_APPROVAL_AUTO_USERS` 的 userid 不弹卡（全自动），其余一律弹卡。
    #   空 = 谁都要卡（fail-closed 的默认值）。
    approval_auto_users_raw = cfg("CC_APPROVAL_AUTO_USERS")
    approval_auto_users = parse_list(approval_auto_users_raw)
    # 旧开关 `CC_APPROVAL_DM` 已废弃：只用来打一句迁移提示，**不再影响任何行为**。
    approval_dm_raw = cfg("CC_APPROVAL_DM", "").strip()
    if approval_dm_raw:
        log.warning("[approval] `CC_APPROVAL_DM` 已废弃（2026-11-10）—— 它按「聊天类型」"
                    "分，现在改成按「发消息的人」分。请把它从 .env 里删掉，改用 "
                    "`CC_APPROVAL_AUTO_USERS=<userid,...>`。这个键现在**不再影响任何行为**。")
    # 模型的**唯一真源**：`data/model.json`。`.env` 的 `CC_MODEL` 只当"种子/兜底"
    # —— 文件不存在时用它的值初始化，文件坏了时退到它（两个文件不会一起坏）。
    # `model_fn` 让 Runner **每条消息**现读一次，这就是热切换的全部机制。
    model_store = ModelStore(
        cfg("CC_MODEL_FILE") or os.path.join(ROOT, "data", "model.json"),
        seed=cfg("CC_MODEL"),
        # ⚠️ 这里**不能**用 `parse_list()` —— 它返回 set，`/model 1` 指向哪个
        # 模型会随进程的哈希顺序变。候选表必须保持用户写的顺序。
        candidates=[c.strip() for c in (cfg("CC_MODELS") or "").split(",") if c.strip()],
    )
    # 模型级的端点/上下文/工作目录。**没有这个文件也能跑**（全部走 .env 那一套）。
    # 有它，同一张候选表里才能同时放"云端"和"本地 Ollama"两种端点。
    model_profiles = ModelProfiles(
        cfg("CC_MODEL_PROFILES") or os.path.join(ROOT, "data", "model_profiles.json"))
    runner = Runner(
        exe=cfg("CC_CLAUDE_EXE", os.path.expanduser(r"~\.local\bin\claude.exe")),
        model=cfg("CC_MODEL"),
        model_fn=model_store.current,
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
        profiles=model_profiles,
    )
    # 单实例保护：自启 + 手动启动/测试同时跑 = 双份回复、双份执行。
    # ⚠️ 互斥量**按平台分开**：两个平台是不同的机器人、不同的长连接，
    #    同时跑不冲突；而同**一个**平台起两份就会分摊事件（飞书 3c 实测，
    #    企微则是服务端踢旧连接）—— 那才是这条锁真正要挡的。
    mutex = MUTEX_NAME if platform == "wecom" else f"{MUTEX_NAME}-{platform}"
    if not acquire_single_instance(mutex):
        # 返回 0：这是**正常**结果，不是失败 —— 任务计划程序每 3 分钟来敲一次门，
        # 活着的时候它就该安静地什么都不做（返回非 0 会被记成失败）。
        log.info("[main] 已有实例在跑（互斥量 %s），本次启动放弃。"
                 "要重启请先停掉常驻实例；测试发送同理。", mutex)
        return 0

    store = Store(cfg("CC_DB", DEFAULT_DB))
    # 审批卡片（默认关）：开了才会把决策目录注入子进程环境，hook 也才会干活。
    approval_dir = ""
    if cfg_bool("CC_APPROVAL", False):
        # ⚠️ **决策目录必须按平台分开**（2026-11-05 实测踩到）：
        # 两个平台的桥如果共用一个目录，A 平台会扫到"发给 B 平台"的请求，
        # 往自己这边发卡片必然失败（会话 ID 不通）⇒ 按 fail-closed **立刻写拒绝**
        # ⇒ 用户在 B 平台刚看到卡片，点下去已经是"已失效"。
        # 现场日志：`卡片没发出去（invalid chatid … 93006）⇒ 直接拒绝`。
        # 这不是"偶发竞态"，是**必然**：谁的扫描线程先跑到，谁就把另一边的请求毙掉。
        approval_dir = cfg("CC_APPROVAL_DIR") or default_approval_dir(platform)
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
        # 闸门现在对**所有**人都启动（免卡用户也走 hook，只是不弹卡），
        # 所以私聊目录**无条件**一起检 —— 不再有"这条路上没闸门"的盲区。
        cwd_checks = [("群聊", cc_cwd)]
        if cc_cwd_dm != cc_cwd:
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
        log.info("[approval] 审批已开启 —— 等待 %ss、只读直放=%s",
                 cfg_int("CC_APPROVAL_WAIT_SEC", 120),
                 cfg("CC_APPROVAL_ALLOW") or "(默认 Read,Grep,Glob,TodoWrite)")
        # 谁免卡 —— 这行必须打出来。"一张卡都没弹"能不能被解释，
        # 全靠"是预授权"还是"闸门没跑"这两个可能性当场分开。
        if approval_auto_users:
            log.info("[approval] 免卡白名单（按发消息的人）：%s",
                     "、".join(sorted(approval_auto_users)))
        else:
            log.info("[approval] 免卡白名单为空 ⇒ 每个人、每条写操作都要点卡")
        # 免卡名单里混进"本来就不许说话"的人 = 一条永远不会生效的配置，
        # 将来排查"为什么他还是弹卡"会白绕一圈，所以现在就点出来。
        # ⚠️ 要拿**三个平台的并集**去比：免卡名单是全局的，而启动时只跑一个平台 ——
        #    只看当前平台的话，另外两个平台的 id 每次启动都会被误报成"永不生效"
        #    （实测踩到：同一份 .env 下 Slack / 飞书的 id 天天被点名）。
        all_allow = set()
        for _k in ("WECOM_ALLOWED_USERIDS", "FEISHU_ALLOWED_OPENIDS",
                   "SLACK_ALLOWED_USERIDS"):
            all_allow |= parse_list(cfg(_k))
        dead = sorted(approval_auto_users - all_allow) if all_allow else []
        if dead:
            log.warning("[approval] 免卡白名单里有 %d 个 userid 不在**任何平台**的"
                        "入口白名单里，**永远不会生效**：%s", len(dead), "、".join(dead))
        # 命令级只读白名单（P1-1）到底加载到了没有。
        # ⚠️ 这行日志是"这次为什么一张卡都没弹"能不能被解释的前提：
        #    **白名单没加载**和**闸门没跑**，在聊天窗口里看起来一模一样（都没卡）。
        for label, d in cwd_checks:
            p = os.path.join(d, "interop", "dws_effects.json")
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                table = data.get("commands") or {}
                n = sum(1 for v in table.values()
                        if v.get("effect") == "read"
                        and v.get("confirmation") == "not_required")
                log.info("[approval] %s 只读白名单：%d/%d 条可直放（快照 %s）",
                         label, n, len(table), data.get("cli_version") or "?")
            except Exception as e:
                log.warning("[approval] %s 没读到只读白名单（%s：%s）⇒ 跨平台命令**全部弹卡**"
                            "（安全，但会更吵）", label, p, e)
        # 打出来：这是两个平台最容易互相踩的地方（见上面那段注释）
        log.info("[approval] 决策目录 = %s（平台 %s 独占）", approval_dir, platform)
    elif approval_auto_users:
        # 配了免卡名单、但审批整体没开 ⇒ 别让用户以为"名单生效了"。
        # 没有审批就没有卡片，名单也就没有意义 —— 这种配置最容易被误读成"已启用"。
        log.warning("[approval] 你设了 CC_APPROVAL_AUTO_USERS，但 CC_APPROVAL=0 ⇒ "
                    "审批整体未启用，谁都**不会**弹卡（名单也就没有意义）。")
    if platform == "slack":
        bridge = SlackBridge(
            slack_bot_token, slack_app_token,
            cfg("SLACK_ALLOWED_USERIDS"), cfg("SLACK_ALLOWED_CHANNELIDS"),
            cfg("SLACK_BOT_NAME", "CC BOT"),
            store, runner, cc_cwd,
            cfg_int("CHUNK_BYTES", 18000), cfg_int("QUEUE_MAX", 20),
            cfg_int("CC_PROGRESS_SEC", 90), dump_raw=args.dump_raw,
            stale_rx_sec=cfg_int("CC_STALE_RX_SEC", STALE_RX_SEC),
            progress_hb_sec=cfg_int("CC_PROGRESS_HEARTBEAT_SEC",
                                    PROGRESS_HEARTBEAT_SEC),
            approval_dir=approval_dir,
            approval_wait_sec=cfg_int("CC_APPROVAL_WAIT_SEC", 120),
            approval_allow=cfg("CC_APPROVAL_ALLOW"),
            user_labels=cfg("SLACK_USER_LABELS"),
            cc_cwd_dm=cc_cwd_dm, approval_auto_users=approval_auto_users_raw,
            model_store=model_store,
            slack_chunk_chars=cfg_int("SLACK_CHUNK_CHARS", 3800),
            bot_user_id=slack_bot_user_id)
    elif platform == "feishu":
        bridge = FeishuBridge(
            app_id, app_secret,
            cfg("FEISHU_ALLOWED_OPENIDS"), cfg("FEISHU_ALLOWED_CHATIDS"),
            cfg("FEISHU_BOT_NAME", "CC BOT"),
            store, runner, cc_cwd,
            cfg_int("CHUNK_BYTES", 18000), cfg_int("QUEUE_MAX", 20),
            cfg_int("CC_PROGRESS_SEC", 90), dump_raw=args.dump_raw,
            stale_rx_sec=cfg_int("CC_STALE_RX_SEC", STALE_RX_SEC),
            progress_hb_sec=cfg_int("CC_PROGRESS_HEARTBEAT_SEC",
                                    PROGRESS_HEARTBEAT_SEC),
            approval_dir=approval_dir,
            approval_wait_sec=cfg_int("CC_APPROVAL_WAIT_SEC", 120),
            approval_allow=cfg("CC_APPROVAL_ALLOW"),
            user_labels=cfg("FEISHU_USER_LABELS"),
            cc_cwd_dm=cc_cwd_dm, approval_auto_users=approval_auto_users_raw,
            model_store=model_store,
            feishu_chunk_bytes=cfg_int("FEISHU_CHUNK_BYTES", 130000))
    else:
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
                        cc_cwd_dm=cc_cwd_dm, approval_auto_users=approval_auto_users_raw,
                        model_store=model_store)

    if cfg_bool("CC_KEEP_AWAKE", True):
        start_keep_awake()

    log.info("[main] cwd=%s%s 档位=%s 模型=%s", cc_cwd,
             (f"（私聊={cc_cwd_dm}）" if cc_cwd_dm != cc_cwd else "（私聊与群聊同目录）"),
             runner.perm_mode, runner.live_model() or "(默认别名)")
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

        # ---- Slack 的测试发送：同样不需要长连接（chat.postMessage 就够）----
        if platform == "slack":
            if args.send_card:
                log.error("[test] --send-card 的样例卡是**企微模板卡**，Slack 不认。"
                          "Slack 的卡片是 Block Kit，形状完全不同。")
                return 4
            if not chatid:
                log.error("[test] 无目标会话：先给机器人发条消息，或显式 --chatid "
                          "（频道 ID `C…` 或单聊 `D…`）")
                return 4
            chat_type = (args.chat_type if args.chat_type is not None
                         else (derived_type or 2))
            log.info("[test] 目标 %s type=%s 内容 %d 字符",
                     chatid, chat_type, len(payload))
            if args.send_auto:
                failed = bridge.send_chunks(chatid, chat_type, payload)
                log.info("[test] >>> %s", "全部送达" if not failed
                         else f"有 {len(failed)} 段未送达")
                ok = not failed
            else:
                r = bridge.send_text(chatid, chat_type, payload)
                log.info("[test] 平台结果: %s", r)
                log.info("[test] >>> %s", "送达" if r["sent"] else "被拒收/结果未知")
                ok = r["sent"]
            return 0 if ok else 1

        # ---- 飞书的测试发送：不需要先起长连接（HTTP 就能发）----
        if platform == "feishu":
            if args.send_card:
                log.error("[test] --send-card 的样例卡是**企微模板卡**，飞书不认。"
                          "飞书请用 tools/feishu_card_probe.py。")
                return 4
            if not chatid:
                log.error("[test] 无目标会话：先给机器人发条消息，或显式 --chatid")
                return 4
            chat_type = (args.chat_type if args.chat_type is not None
                         else (derived_type or 2))
            nbytes = len(payload.encode("utf-8"))
            log.info("[test] 目标 %s type=%s 内容 %d 字节（%d 字符）",
                     chatid, chat_type, nbytes, len(payload))
            if args.send_auto:
                failed = bridge.send_chunks(chatid, chat_type, payload)
                log.info("[test] >>> %s", "全部送达" if not failed
                         else f"有 {len(failed)} 段未送达")
                ok = not failed
            else:
                r = bridge.send_text(chatid, chat_type, payload)
                log.info("[test] 平台结果: %s", r)
                log.info("[test] >>> %s", "送达" if r["sent"] else "被拒收/结果未知")
                ok = r["sent"]
            return 0 if ok else 1
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
        # ⚠️ 走契约的 `plat_run()`，**不要**直接调 `run_forever()` ——
        # 后者是企微平台层的方法名，飞书那边叫 `feishu_run_forever`。
        # 这一步就是契约存在的意义：入口不该知道平台方法叫什么。
        bridge.plat_run()
    except KeyboardInterrupt:
        log.info("[main] 退出")
        bridge.plat_stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
