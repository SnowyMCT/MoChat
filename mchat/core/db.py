"""本地聊天历史：SQLite 单文件 + FTS5 全文索引（两个都是原生 C 实现）。

为什么用它，而不是 pickle / json / 内存大列表
--------------------------------------------
* **占用少**：消息常驻在引擎的页缓存里，进程里不必再留一份全量消息列表；
  界面只取当前会话最近的一批（``LIMIT``），翻多少取多少。
* **快**：写盘、检索、排序都在 C 层完成，Python 只递参数。FTS5 是 SQLite 自带的
  倒排索引，``trigram`` 分词对中文按**子串**命中（实测 ``unicode61`` 对
  "这周要去日本"搜"日本"是 0 条，trigram 是 1 条），比在 Python 里逐条 ``in`` 快几个数量级。
* 无服务、无依赖（``sqlite3`` 是标准库），文件可以整份拷走。

写入策略
--------
* WAL + ``synchronous=NORMAL``：已提交的数据崩了也不丢，又不至于每条都 fsync；
* 一条连接、``INSERT … ON CONFLICT DO UPDATE`` 幂等落库（同一 id 重复写只是刷新状态）；
* 批量导入用 ``executemany`` 包在一个事务里 —— 实测 2 万条 < 1 秒，
  逐条 commit 会慢两个数量级。

本模块和 ``store`` 一样**不依赖 Qt**：界面层把仓库事件接到这里，反过来启动时用它
回填历史。所以"持久化"既能脱离界面单测，也能随时换成别的实现。
"""
from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from typing import Iterable

from .models import (Attachment, Message, PinRef, merged_from_json,   # noqa: F401
                     merged_to_json, pins_from_json, pins_to_json,
                     reply_from_json, reply_to_json)

__all__ = ["ChatLog", "SearchHit", "DEFAULT_LIMIT", "SEARCH_LIMIT", "TRIGRAM_MIN"]

SCHEMA_VERSION = 4
DEFAULT_LIMIT = 200        # 单个会话一次取多少条（界面只画这么多）
SEARCH_LIMIT = 60          # 搜索一次最多返回多少条
TRIGRAM_MIN = 3            # trigram 索引能命中的最短关键词长度

# 表。注意 messages 的三列是 v2 新增的（老库由 _migrate 补上）：
# * attachment  —— 附件（JSON：path/name/size），没有就是 NULL
# * file_name   —— 附件文件名（单独一列，给下面的生成列用）
# * search_text —— **被 FTS 索引的那一列**：正文 + 附件文件名，且是 VIRTUAL 生成列。
#   为什么要这么绕：这样"搜文件名"也能命中（用户丢进来一个「评审材料-v3.pdf」，
#   搜「评审材料」就找得到），而 text 仍然是干净的正文（读回来原样显示）；
#   同时因为是 VIRTUAL 生成列，**磁盘上不会把正文存两遍**（2 万条实测体积不变），
#   也不存在"手写代码维持索引串"那种漂移风险 —— SQLite 自己算。
SEARCH_TEXT_EXPR = ("CASE WHEN file_name = '' THEN text"
                    " ELSE trim(text || ' ' || file_name) END")

SCHEMA = f"""
CREATE TABLE IF NOT EXISTS messages(
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL,
    sender_id       TEXT NOT NULL,
    text            TEXT NOT NULL,
    ts              REAL NOT NULL,
    outgoing        INTEGER NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'sent',
    attachment      TEXT,
    file_name       TEXT NOT NULL DEFAULT '',
    reply_to        TEXT,                              -- v3：被回复那条的快照（JSON）
    forwarded_from  TEXT NOT NULL DEFAULT '',          -- v3：转发自哪个会话
    merged          TEXT NOT NULL DEFAULT '',          -- v3：合并转发的内容（JSON 数组）
    search_text     TEXT GENERATED ALWAYS AS ({SEARCH_TEXT_EXPR}) VIRTUAL
);
CREATE INDEX IF NOT EXISTS idx_messages_conv ON messages(conversation_id, ts);

CREATE TABLE IF NOT EXISTS reactions(
    message_id TEXT NOT NULL,
    emoji      TEXT NOT NULL,
    count      INTEGER NOT NULL DEFAULT 0,
    mine       INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (message_id, emoji)
) WITHOUT ROWID;

CREATE TABLE IF NOT EXISTS drafts(
    conversation_id TEXT PRIMARY KEY,
    text            TEXT NOT NULL,
    updated         REAL NOT NULL
);

-- 会话级旗标：侧栏右键菜单里的置顶/免打扰，以及"清空过历史"的标记。
-- cleared 是给启动时看的：演示数据每次都重新生成，没这个标记的话清空过的会话
-- 下次启动又会满血复活。pinned_message_id 是 v3 加的：会话内被置顶的那条消息。
CREATE TABLE IF NOT EXISTS chats(
    conversation_id TEXT PRIMARY KEY,
    pinned          INTEGER NOT NULL DEFAULT 0,
    muted           INTEGER NOT NULL DEFAULT 0,
    cleared         INTEGER NOT NULL DEFAULT 0,
    pinned_message_id TEXT NOT NULL DEFAULT '',
    cleared_at      REAL NOT NULL DEFAULT 0      -- v4：本地清空到哪一刻（见下）
);
"""

# v4 的 ``cleared_at`` 解决的是"联网之后清空历史会复活"这个坑：
# 清空只是删本地库，服务端那边消息还在 —— 下次打开这个会话会把旧消息**再发回来**，
# 用户会觉得"我删了它又自己长回来了"。记住一个时间戳，凡是 ts 早于它的消息
# 一律不再显示（历史回填与推送事件两处都按它过滤），清空才真的持久。

# 全文索引 + 触发器。触发器每次都重建（DROP + CREATE）：这样改索引列名/索引内容时，
# 老库不需要额外判断，重新打开就自动跟上。
SCHEMA_FTS = """
-- trigram：按 3 字符滑窗建索引，中文/英文都能做"子串"检索
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    search_text, content='messages', content_rowid='rowid', tokenize='trigram'
);
DROP TRIGGER IF EXISTS messages_ai;
DROP TRIGGER IF EXISTS messages_ad;
DROP TRIGGER IF EXISTS messages_au;
CREATE TRIGGER messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;
CREATE TRIGGER messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
END;
CREATE TRIGGER messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, search_text)
    VALUES ('delete', old.rowid, old.search_text);
    INSERT INTO messages_fts(rowid, search_text) VALUES (new.rowid, new.search_text);
END;
"""

_UPSERT = """
INSERT INTO messages(id, conversation_id, sender_id, text, ts, outgoing, status,
                     attachment, file_name, reply_to, forwarded_from, merged)
VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    text = excluded.text, ts = excluded.ts, status = excluded.status,
    attachment = excluded.attachment, file_name = excluded.file_name,
    reply_to = excluded.reply_to, forwarded_from = excluded.forwarded_from,
    merged = excluded.merged
"""


@dataclass(frozen=True)
class SearchHit:
    """一条搜索结果：够界面画一行（会话、片段、时间、谁发的）。"""

    conversation_id: str
    message_id: str
    snippet: str
    ts: float
    outgoing: bool


class ChatLog:
    """聊天历史库。``path`` 传 ``None`` 或 ``":memory:"`` 就是纯内存（测试/关闭持久化）。"""

    def __init__(self, path: str | None = None):
        self.path = path or ":memory:"
        if self.path != ":memory:":
            directory = os.path.dirname(os.path.abspath(self.path))
            if directory:
                os.makedirs(directory, exist_ok=True)
        self._db = sqlite3.connect(self.path, timeout=5.0)
        self._db.row_factory = sqlite3.Row
        if self.path != ":memory:":                       # 内存库没有 WAL 可言
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)
        migrated = self._migrate()                        # 老库（v1）补列 + 打标记
        self._db.executescript(SCHEMA_FTS)
        if migrated:
            # 索引列变了（text -> search_text），整份重建一次；老库只有 v1 那一次
            self._db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        elif self._db.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
            self._db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
        self._db.commit()

    def _migrate(self) -> bool:
        """把老库升到当前版本，返回**消息表是否被动过**（动了就要重建 FTS 索引）。

        ``CREATE TABLE IF NOT EXISTS`` 不会给已存在的表加列，所以老库必须显式 ALTER。
        各版本加了什么：

        * v1 -> v2：``attachment`` / ``file_name`` / ``search_text``（VIRTUAL 生成列）。
          ``search_text`` 由 SQLite 按现有行现算，不需要回填数据，v1 里也没有附件，
          所以老消息的索引内容就等于正文，语义完全不变。
        * v2 -> v3：``reply_to``（回复的引用快照）/ ``forwarded_from``（转发自哪）/
          ``merged``（合并转发的内容）；会话表加 ``pinned_message_id``（会话内置顶）。
          这几列都是 NULL/空串默认值，老库里的消息语义不受影响，FTS 索引也不用重建。

        用 ``table_xinfo`` 而不是 ``table_info``：后者**不列生成列**，拿它判断"列在不在"
        会把已有的 search_text 当成缺列，再来一次 ALTER 直接报 duplicate column。
        """
        columns = {row["name"] for row in self._db.execute("PRAGMA table_xinfo(messages)")}
        changed = False
        # ---- v1 -> v2 ----
        if "attachment" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN attachment TEXT")
            changed = True
        if "file_name" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN file_name TEXT "
                             "NOT NULL DEFAULT ''")
            changed = True
        if "search_text" not in columns:
            self._db.execute(
                f"ALTER TABLE messages ADD COLUMN search_text TEXT GENERATED ALWAYS AS "
                f"({SEARCH_TEXT_EXPR}) VIRTUAL")
            changed = True
        # ---- v2 -> v3 ----
        if "reply_to" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN reply_to TEXT")
        if "forwarded_from" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN forwarded_from TEXT "
                             "NOT NULL DEFAULT ''")
        if "merged" not in columns:
            self._db.execute("ALTER TABLE messages ADD COLUMN merged TEXT NOT NULL DEFAULT ''")
        chat_columns = {row["name"] for row in self._db.execute("PRAGMA table_xinfo(chats)")}
        if "pinned_message_id" not in chat_columns:
            self._db.execute("ALTER TABLE chats ADD COLUMN pinned_message_id TEXT "
                             "NOT NULL DEFAULT ''")
        # ---- v3 -> v4 ----
        if "cleared_at" not in chat_columns:
            self._db.execute("ALTER TABLE chats ADD COLUMN cleared_at REAL "
                             "NOT NULL DEFAULT 0")
        if changed:
            # v1 的 FTS 表索引的是 text 这一列，列名对不上新的触发器，只能整表重建
            self._db.execute("DROP TABLE IF EXISTS messages_fts")
            self._db.commit()
        return changed

    def close(self) -> None:
        self._db.close()

    def compact(self) -> None:
        """整理文件：WAL 收回主库 + 重排页。启动或退出时调一次就够（占用更小）。"""
        self._db.execute("VACUUM")
        if self.path != ":memory:":
            # 顺序有讲究：VACUUM 自己会往 WAL 里写一大堆页，最后再 checkpoint 才收得干净
            self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    # ================================================================== 写
    def save_message(self, message: Message) -> None:
        with self._db:                                    # with = 自动 commit/rollback
            self._db.execute(_UPSERT, self._row(message))

    def save_messages(self, messages: Iterable[Message]) -> int:
        """批量落库：一条事务、一次 executemany（导入演示数据/历史时用它）。"""
        rows = [self._row(m) for m in messages]
        if not rows:
            return 0
        with self._db:
            self._db.executemany(_UPSERT, rows)
        return len(rows)

    def set_status(self, message_id: str, status: str) -> None:
        with self._db:
            self._db.execute("UPDATE messages SET status = ? WHERE id = ?",
                             (status, message_id))

    def delete_message(self, message_id: str) -> bool:
        with self._db:
            cursor = self._db.execute("DELETE FROM messages WHERE id = ?", (message_id,))
            self._db.execute("DELETE FROM reactions WHERE message_id = ?", (message_id,))
        return cursor.rowcount > 0

    def clear(self) -> None:
        """清空全部本地历史（消息、互动、草稿）—— 危险操作，界面侧必须二次确认。"""
        with self._db:
            self._db.execute("DELETE FROM messages")
            self._db.execute("DELETE FROM reactions")
            self._db.execute("DELETE FROM drafts")
            self._db.execute("INSERT INTO messages_fts(messages_fts) VALUES('rebuild')")
        self.compact()               # 清完顺势把文件收小（原来这里只有一句 VACUUM）

    # ------------------------------------------------------------------ 会话旗标
    FLAGS = ("pinned", "muted", "cleared")

    def set_flag(self, conversation_id: str, flag: str, value: bool = True) -> None:
        """记一个会话旗标（置顶 / 免打扰 / 已清空）：一条 upsert，够便宜。"""
        if flag not in self.FLAGS:
            raise ValueError(f"未知旗标：{flag}")
        with self._db:
            self._db.execute(
                f"INSERT INTO chats(conversation_id, {flag}) VALUES (?, ?) "
                f"ON CONFLICT(conversation_id) DO UPDATE SET {flag} = excluded.{flag}",
                (conversation_id, 1 if value else 0))

    def flags(self) -> dict[str, set[str]]:
        """``{会话id: {旗标名…}}`` —— 启动时一次读回来，别一条条查。"""
        rows = self._db.execute(
            "SELECT conversation_id, pinned, muted, cleared FROM chats").fetchall()
        return {row[0]: {name for index, name in enumerate(self.FLAGS, start=1) if row[index]}
                for row in rows}

    # ------------------------------------------------------------------ 会话内置顶
    def pinned_refs(self) -> dict[str, tuple[PinRef, ...]]:
        """``{会话id: (PinRef, ...)}`` —— 一个会话可以置顶多条，每条带"给不给对方看"。

        这一列先后用过三种存法，读的时候全认（见 ``models.pins_from_json``）：
        单个 id 字符串（v3 第一版）→ 纯 id 数组（"多条置顶"那版）→
        现在 ``[{"id":..,"both":..}, ...]``。所以不用加列、也不用升版本。
        """
        rows = self._db.execute(
            "SELECT conversation_id, pinned_message_id FROM chats "
            "WHERE pinned_message_id != ''").fetchall()
        pinned: dict[str, tuple[PinRef, ...]] = {}
        for row in rows:
            refs = pins_from_json(row["pinned_message_id"])
            if refs:
                pinned[row["conversation_id"]] = refs
        return pinned

    def set_pinned_refs(self, conversation_id: str, pins) -> None:
        """把"这个会话置顶了哪几条、各是什么可见范围"整串写进库（空 = 清掉置顶）。"""
        payload = pins_to_json(pins)
        with self._db:
            self._db.execute(
                "INSERT INTO chats(conversation_id, pinned_message_id) VALUES (?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET "
                "pinned_message_id = excluded.pinned_message_id",
                (conversation_id, payload))

    def clear_conversation(self, conversation_id: str) -> int:
        """清掉**这一个**会话的历史（消息 + 回应 + 草稿），返回删掉几条消息。

        同时打上 ``cleared`` 旗标：演示数据每次启动都会重新生成，没有这个标记的话
        清空过的会话下次启动又会"活过来"。别动 ``chats`` 表里的其它旗标。
        ``cleared_at`` 记的是"清到哪一刻"，联网时用它挡住服务端回填的旧消息。
        """
        stamp = time.time()
        with self._db:
            cursor = self._db.execute(
                "DELETE FROM messages WHERE conversation_id = ?", (conversation_id,))
            self._db.execute("DELETE FROM reactions WHERE message_id NOT IN "
                             "(SELECT id FROM messages)")
            self._db.execute("DELETE FROM drafts WHERE conversation_id = ?", (conversation_id,))
            self._db.execute(
                "INSERT INTO chats(conversation_id, cleared, cleared_at) VALUES (?, 1, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET cleared = 1,"
                " cleared_at = excluded.cleared_at", (conversation_id, stamp))
            # 置顶的那条也被删了，顺手把置顶清掉 —— 否则下次进来会指一条不存在的消息
            self._db.execute("UPDATE chats SET pinned_message_id = '' "
                             "WHERE conversation_id = ?", (conversation_id,))
        self.compact()               # 腾出来的页还给系统（几十毫秒），文件真的变小
        return cursor.rowcount

    # ------------------------------------------------------------------ 清空点
    def set_cleared_at(self, conversation_id: str, stamp: float) -> None:
        """记下"这个会话被清空到哪一刻"（联网时用它不再显示更早的消息）。"""
        with self._db:
            self._db.execute(
                "INSERT INTO chats(conversation_id, cleared_at) VALUES (?, ?) "
                "ON CONFLICT(conversation_id) DO UPDATE SET cleared_at = excluded.cleared_at",
                (conversation_id, float(stamp)))

    def cleared_at(self, conversation_id: str) -> float:
        row = self._db.execute("SELECT cleared_at FROM chats WHERE conversation_id = ?",
                               (conversation_id,)).fetchone()
        return float(row["cleared_at"]) if row else 0.0

    def cleared_map(self) -> dict[str, float]:
        """``{会话id: 清空时间戳}``（一次读回来，别在循环里逐个查库）。"""
        return {row["conversation_id"]: float(row["cleared_at"]) for row in
                self._db.execute("SELECT conversation_id, cleared_at FROM chats"
                                 " WHERE cleared_at > 0").fetchall()}

    # ================================================================== 读
    def load(self, conversation_id: str, *, limit: int = DEFAULT_LIMIT) -> list[Message]:
        """取某会话最近 ``limit`` 条，按时间正序返回（界面上是"由上到下"）。"""
        rows = self._db.execute(
            "SELECT * FROM (SELECT * FROM messages WHERE conversation_id = ?"
            " ORDER BY ts DESC LIMIT ?) ORDER BY ts ASC",
            (conversation_id, int(limit))).fetchall()
        return [self._message(row) for row in rows]

    def count(self, conversation_id: str | None = None) -> int:
        if conversation_id is None:
            return self._db.execute("SELECT count(*) FROM messages").fetchone()[0]
        return self._db.execute("SELECT count(*) FROM messages WHERE conversation_id = ?",
                                (conversation_id,)).fetchone()[0]

    def conversations(self) -> list[str]:
        return [row[0] for row in self._db.execute(
            "SELECT DISTINCT conversation_id FROM messages").fetchall()]

    def search(self, query: str, *, limit: int = SEARCH_LIMIT,
               conversation_id: str | None = None) -> list[SearchHit]:
        """全文检索。

        * 关键词 ≥ 3 字符：走 FTS5（trigram，子串命中，中文也行）；
        * 更短的词（"日本"本身就 2 字、"ab"）：trigram 索引命不了，退化成
          ``LIKE '%词%'`` —— 全表扫，但 SQLite 全在 C 里跑，小库毫秒级，
          而且短词本来就搜不出多少东西。

        ``conversation_id`` 给了就只在这个会话里找（界面"从哪个会话进来就搜哪个会话"），
        过滤条件拼在 SQL 里，不是查完再筛 —— 命中多少取多少，不做无用功。

        ``snippet`` 由 SQLite 直接给出（``snippet()`` / ``substr(instr())``），
        Python 侧不用再切字符串。

        检索的是 ``search_text``（正文 + 附件文件名），所以"搜附件名"也能找到消息。
        """
        query = (query or "").strip()
        if not query:
            return []
        if len(query) >= TRIGRAM_MIN:
            scope = " AND m.conversation_id = ?" if conversation_id else ""
            params: list = [self._match_query(query)]
            if conversation_id:
                params.append(conversation_id)
            params.append(int(limit))
            rows = self._db.execute(
                "SELECT m.conversation_id, m.id, m.ts, m.outgoing,"
                " snippet(messages_fts, 0, '', '', '…', 16) AS snip"
                " FROM messages_fts JOIN messages m ON m.rowid = messages_fts.rowid"
                f" WHERE messages_fts MATCH ?{scope} ORDER BY m.ts DESC LIMIT ?",
                params).fetchall()
        else:
            scope = " AND conversation_id = ?" if conversation_id else ""
            params = [query, f"%{query}%"]
            if conversation_id:
                params.append(conversation_id)
            params.append(int(limit))
            rows = self._db.execute(
                "SELECT conversation_id, id, ts, outgoing,"
                " substr(search_text, max(1, instr(lower(search_text), lower(?)) - 12),"
                " 60) AS snip"
                f" FROM messages WHERE search_text LIKE ?{scope}"
                " ORDER BY ts DESC LIMIT ?",
                params).fetchall()
        return [SearchHit(row["conversation_id"], row["id"], row["snip"],
                          row["ts"], bool(row["outgoing"])) for row in rows]

    def stats(self) -> dict:
        """给菜单/自检用：条数、会话数、互动数、附件数、文件占用。"""
        size = 0
        for suffix in ("", "-wal", "-shm"):
            try:
                size += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return {
            "messages": self.count(),
            "conversations": len(self.conversations()),
            "reactions": self._db.execute("SELECT count(*) FROM reactions").fetchone()[0],
            "drafts": self._db.execute("SELECT count(*) FROM drafts").fetchone()[0],
            "attachments": self._db.execute(
                "SELECT count(*) FROM messages WHERE attachment IS NOT NULL").fetchone()[0],
            "bytes": size,
        }

    # ================================================================== 互动
    def toggle_reaction(self, message_id: str, emoji: str) -> dict[str, int]:
        """点一下表情：自己没有就加、有了就撤，返回该消息最新的 ``{表情: 数量}``。"""
        row = self._db.execute("SELECT count, mine FROM reactions"
                               " WHERE message_id = ? AND emoji = ?",
                               (message_id, emoji)).fetchone()
        already = bool(row and row["mine"])
        count = ((row["count"] if row else 0) + (-1 if already else 1))
        mine = 0 if already else 1
        with self._db:
            if count > 0:
                self._db.execute(
                    "INSERT INTO reactions(message_id, emoji, count, mine)"
                    " VALUES(?, ?, ?, ?) ON CONFLICT(message_id, emoji) DO UPDATE"
                    " SET count = excluded.count, mine = excluded.mine",
                    (message_id, emoji, count, mine))
            else:
                self._db.execute("DELETE FROM reactions WHERE message_id = ? AND emoji = ?",
                                 (message_id, emoji))
        return self.reactions(message_id)

    def reactions(self, message_id: str) -> dict[str, int]:
        return {row["emoji"]: row["count"] for row in self._db.execute(
            "SELECT emoji, count FROM reactions WHERE message_id = ? AND count > 0"
            " ORDER BY count DESC, emoji", (message_id,)).fetchall()}

    def reactions_for(self, conversation_id: str) -> dict[str, dict[str, int]]:
        """整个会话一次取完（气泡逐个查库太浪费）。"""
        out: dict[str, dict[str, int]] = {}
        for row in self._db.execute(
                "SELECT r.message_id, r.emoji, r.count FROM reactions r"
                " JOIN messages m ON m.id = r.message_id"
                " WHERE m.conversation_id = ? AND r.count > 0"
                " ORDER BY r.emoji", (conversation_id,)).fetchall():
            out.setdefault(row["message_id"], {})[row["emoji"]] = row["count"]
        return out

    def mine(self, message_id: str) -> set[str]:
        """这条消息上"我"点过的表情（气泡上要高亮）。"""
        return {row["emoji"] for row in self._db.execute(
            "SELECT emoji FROM reactions WHERE message_id = ? AND mine = 1",
            (message_id,)).fetchall()}

    def mine_for(self, conversation_id: str) -> dict[str, set[str]]:
        """整个会话里"我"点过的表情（气泡逐个查库太浪费）。"""
        out: dict[str, set[str]] = {}
        for row in self._db.execute(
                "SELECT r.message_id, r.emoji FROM reactions r"
                " JOIN messages m ON m.id = r.message_id"
                " WHERE m.conversation_id = ? AND r.mine = 1",
                (conversation_id,)).fetchall():
            out.setdefault(row["message_id"], set()).add(row["emoji"])
        return out

    # ================================================================== 草稿
    def set_draft(self, conversation_id: str, text: str) -> None:
        """存草稿；空文本等于删掉（不留空行在库里）。"""
        with self._db:
            if text.strip():
                self._db.execute(
                    "INSERT INTO drafts(conversation_id, text, updated) VALUES(?, ?, ?)"
                    " ON CONFLICT(conversation_id) DO UPDATE SET text = excluded.text,"
                    " updated = excluded.updated",
                    (conversation_id, text, time.time()))
            else:
                self._db.execute("DELETE FROM drafts WHERE conversation_id = ?",
                                 (conversation_id,))

    def draft(self, conversation_id: str) -> str:
        row = self._db.execute("SELECT text FROM drafts WHERE conversation_id = ?",
                               (conversation_id,)).fetchone()
        return row["text"] if row else ""

    def drafts(self) -> dict[str, str]:
        return {row["conversation_id"]: row["text"] for row in self._db.execute(
            "SELECT conversation_id, text FROM drafts").fetchall()}

    # ================================================================== 内部
    @staticmethod
    def _match_query(query: str) -> str:
        """FTS5 查询串：整体当短语（trigram 下即"子串"），引号要转义。"""
        return '"' + query.replace('"', '""') + '"'

    @staticmethod
    def _row(message: Message) -> tuple:
        attachment = message.attachment
        return (message.id, message.conversation_id, message.sender_id, message.text,
                float(message.ts), int(bool(message.outgoing)), message.status,
                attachment.dump() if attachment is not None else None,
                attachment.name if attachment is not None else "",
                reply_to_json(message.reply_to), message.forwarded_from,
                merged_to_json(message.merged_items))

    @staticmethod
    def _message(row: sqlite3.Row) -> Message:
        return Message(id=row["id"], conversation_id=row["conversation_id"],
                       sender_id=row["sender_id"], text=row["text"], ts=row["ts"],
                       outgoing=bool(row["outgoing"]), status=row["status"],
                       attachment=_attachment(row["attachment"]),
                       reply_to=reply_from_json(row["reply_to"]),
                       forwarded_from=row["forwarded_from"],
                       merged_items=merged_from_json(row["merged"]))


def _attachment(raw) -> Attachment | None:
    """把库里那一列 JSON 还原成附件；空值/坏数据都当"没有附件"（别炸界面）。"""
    if not raw:
        return None
    try:
        return Attachment.from_json(json.loads(raw))
    except (TypeError, ValueError):
        return None
