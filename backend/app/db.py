"""SQLite state: triaged items, drafts, audit log, settings."""

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .config import DB_PATH

_lock = threading.Lock()
_conn: sqlite3.Connection | None = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,            -- teams_chat | teams_channel | email | github_pr | ado | vault
    type TEXT NOT NULL,              -- ping | mention | email | pr_review | open_loop | ...
    external_id TEXT NOT NULL,       -- chat msg id, email id, PR url...
    conversation_id TEXT,            -- chat id / email conversationId, for rate limiting
    sender TEXT,
    sender_id TEXT,
    subject TEXT,
    content TEXT,
    received_at TEXT,
    tier TEXT,                       -- A | B | C (null until classified)
    tier_reasoning TEXT,
    status TEXT NOT NULL DEFAULT 'new',  -- new | classified | auto_replied | drafted | held | answered | dismissed | done
    action_taken TEXT,
    urgent INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS drafts (
    id INTEGER PRIMARY KEY,
    item_id INTEGER REFERENCES items(id),
    channel TEXT NOT NULL,           -- teams | email
    recipient TEXT,
    reply_to_ref TEXT,               -- chat id or email id to reply into
    body TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | approved_sent | rejected | superseded
    reject_reason TEXT,
    created_at TEXT NOT NULL,
    resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    actor TEXT NOT NULL DEFAULT 'chief',
    action TEXT NOT NULL,            -- classified | auto_reply | holding_message | draft_created | sent | dry_run_send | moved_email | note_written | ...
    item_id INTEGER,
    detail TEXT                      -- full content / reasoning, JSON
);
CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS cos_cost (
    id INTEGER PRIMARY KEY,
    ts TEXT NOT NULL,
    label TEXT,                          -- purpose: triage | pr_review | weekly_status | roadmap | chieff | ...
    model TEXT,
    cost_usd REAL NOT NULL DEFAULT 0,    -- SDK-reported cost of this agent run
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS weekly_reports (
    id INTEGER PRIMARY KEY,
    week_key TEXT NOT NULL UNIQUE,       -- ISO year-week, e.g. "2026-W25"
    week_label TEXT,                     -- "Week 25"
    date_range TEXT,                     -- "Jun 15-19, 2026"
    title TEXT,                          -- Confluence page title
    confluence_page_id TEXT,
    confluence_url TEXT,                 -- live URL once published
    edit_url TEXT,                       -- draft editor URL
    storage_html TEXT,                   -- Confluence storage body
    teams_html TEXT,                     -- Teams summary body
    metrics TEXT,                        -- JSON: PR counts, ADO count, etc.
    status TEXT NOT NULL DEFAULT 'draft',-- draft | published | discarded
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def cutoff(*, minutes: int = 0, hours: int = 0, days: int = 0) -> str:
    """UTC ISO string for 'now minus delta', safe to string-compare against the
    ISO timestamps we store (sqlite datetime('now') uses a space, not 'T', so it
    must never be compared against our columns)."""
    from datetime import timedelta
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes, hours=hours, days=days)).isoformat()


def get_conn() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = sqlite3.connect(DB_PATH, check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute("PRAGMA journal_mode=WAL")
        _conn.executescript(SCHEMA)
        _conn.commit()
    return _conn


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = get_conn().execute(sql, params)
        get_conn().commit()
        return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        rows = get_conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]


def get_setting(key: str, default: str | None = None) -> str | None:
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    return rows[0]["value"] if rows else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def audit(action: str, detail: dict | str, item_id: int | None = None, actor: str = "chief") -> None:
    if not isinstance(detail, str):
        detail = json.dumps(detail, ensure_ascii=False, default=str)
    execute(
        "INSERT INTO audit_log(ts, actor, action, item_id, detail) VALUES(?,?,?,?,?)",
        (now(), actor, action, item_id, detail),
    )


def upsert_item(
    source: str,
    type_: str,
    external_id: str,
    *,
    conversation_id: str | None = None,
    sender: str | None = None,
    sender_id: str | None = None,
    subject: str | None = None,
    content: str | None = None,
    received_at: str | None = None,
) -> tuple[int, bool]:
    """Insert an item if unseen. Returns (item_id, is_new)."""
    existing = query(
        "SELECT id FROM items WHERE source=? AND external_id=?", (source, external_id)
    )
    if existing:
        return existing[0]["id"], False
    ts = now()
    item_id = execute(
        "INSERT INTO items(source,type,external_id,conversation_id,sender,sender_id,"
        "subject,content,received_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (source, type_, external_id, conversation_id, sender, sender_id,
         subject, content, received_at, ts, ts),
    )
    return item_id, True


def update_item(item_id: int, **fields) -> None:
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))
