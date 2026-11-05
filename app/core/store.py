"""会话/去重/运行结果表（SQLite）。

与平台无关：键永远是 `(chatid, userid)` 这样的字符串，谁提供的不重要。
"""

import os
import sqlite3
import threading
import time

from common import SEEN_KEEP_DAYS, log

class Store:
    def __init__(self, path):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        self._lock = threading.Lock()
        self._c = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._c.row_factory = sqlite3.Row
        self._c.executescript("""
        CREATE TABLE IF NOT EXISTS sessions(
            chat_id TEXT NOT NULL, user_id TEXT NOT NULL, session_id TEXT,
            cwd TEXT NOT NULL, last_active INTEGER NOT NULL, created_at INTEGER NOT NULL,
            PRIMARY KEY(chat_id, user_id));
        CREATE TABLE IF NOT EXISTS seen_msg(
            msg_id TEXT PRIMARY KEY, seen_at INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS idx_seen_at ON seen_msg(seen_at);
        """)
        self._c.commit()
        self._prune()

    def _prune(self):
        cutoff = int(time.time()) - SEEN_KEEP_DAYS * 86400
        with self._lock:
            n = self._c.execute("DELETE FROM seen_msg WHERE seen_at < ?", (cutoff,)).rowcount
            self._c.commit()
        if n:
            log.info("[db] 清理过期去重记录 %d 条", n)

    def seen(self, msg_id):
        """原子地"记下并判断"：返回 True 表示**之前见过**（重复消息）。"""
        with self._lock:
            cur = self._c.execute(
                "INSERT OR IGNORE INTO seen_msg(msg_id, seen_at) VALUES(?, ?)",
                (msg_id, int(time.time())))
            self._c.commit()
            return cur.rowcount == 0

    def get_session(self, chat_id, user_id, default_cwd):
        with self._lock:
            row = self._c.execute(
                "SELECT session_id, cwd FROM sessions WHERE chat_id=? AND user_id=?",
                (chat_id, user_id)).fetchone()
        if not row:
            return None, default_cwd
        return row["session_id"], (row["cwd"] or default_cwd)

    def save_session(self, chat_id, user_id, session_id, cwd):
        now = int(time.time())
        with self._lock:
            self._c.execute("""
                INSERT INTO sessions(chat_id,user_id,session_id,cwd,last_active,created_at)
                VALUES(?,?,?,?,?,?)
                ON CONFLICT(chat_id,user_id) DO UPDATE SET
                    session_id=excluded.session_id,
                    cwd=excluded.cwd,
                    last_active=excluded.last_active
            """, (chat_id, user_id, session_id, cwd, now, now))
            self._c.commit()

    def last_chat(self):
        """最近活跃的会话 → `(chat_id, chat_type)`，给运维命令/自愈通知兜底。

        `chat_type` 是**推出来的**：本项目的会话键约定是
        「单聊 = (userid, userid)、群聊 = (群chatid, userid)」
        ⇒ `chat_id == user_id` 就是单聊(1)，否则群聊(2)。
        （写死 2 的话，最近只在私聊说过话时，通知会带着错的类型发不出去。）
        """
        with self._lock:
            row = self._c.execute(
                "SELECT chat_id, user_id FROM sessions "
                "ORDER BY last_active DESC LIMIT 1").fetchone()
        if not row:
            return None, None
        return row["chat_id"], (1 if row["chat_id"] == row["user_id"] else 2)

    def clear_session(self, chat_id, user_id):
        with self._lock:
            self._c.execute(
                "UPDATE sessions SET session_id=NULL, last_active=? WHERE chat_id=? AND user_id=?",
                (int(time.time()), chat_id, user_id))
            self._c.commit()
