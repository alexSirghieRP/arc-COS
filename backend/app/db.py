"""SQLite state: triaged items, drafts, audit log, settings."""

import json
import sqlite3
import threading
import time as _time
from contextlib import contextmanager
from datetime import datetime, timezone

from .config import DB_PATH

_lock = threading.RLock()  # reentrant: transaction() + execute() can both acquire in same thread
_conn: sqlite3.Connection | None = None
_in_transaction = threading.local()  # per-thread flag to suppress per-call commits

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
CREATE TABLE IF NOT EXISTS scrum_reports (
    id INTEGER PRIMARY KEY,
    report_date TEXT NOT NULL UNIQUE,    -- day the ticket is for, ISO date, e.g. "2026-07-21"
    window_start TEXT,                   -- ISO datetime, start of the covered window
    window_end TEXT,                     -- ISO datetime, end of the covered window
    summary_text TEXT,                   -- the copyable scrum update (editable)
    generated_text TEXT,                 -- last LLM-generated text, kept so edits can be reverted
    evidence TEXT,                       -- JSON: raw gathered PRs/commits/ADO/Confluence/sessions/tests
    edited INTEGER NOT NULL DEFAULT 0,   -- 1 once the user has hand-edited summary_text
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS job_runs (
    id INTEGER PRIMARY KEY,
    job TEXT NOT NULL,                   -- scheduler job id (teams_sweep, pr_review, ...)
    trigger TEXT NOT NULL DEFAULT 'schedule',  -- schedule | manual | startup
    started_at TEXT NOT NULL,
    finished_at TEXT,
    duration_ms INTEGER,
    ok INTEGER,                          -- 1 ok | 0 failed | NULL still running
    error TEXT,
    summary TEXT                         -- short JSON/text of what the run did
);
CREATE TABLE IF NOT EXISTS knowledge_pages (
  id TEXT PRIMARY KEY,           -- confluence page id
  source TEXT NOT NULL,          -- 'confluence'
  space TEXT,
  title TEXT,
  url TEXT,
  content_md TEXT,               -- markdown body
  version INTEGER,
  last_edited_by TEXT,
  last_edited_at TEXT,
  parent_id TEXT,
  parent_title TEXT,
  word_count INTEGER,
  has_code INTEGER DEFAULT 0,
  has_diagrams INTEGER DEFAULT 0,
  use_case TEXT,                 -- detected use-case bucket, e.g. 'Intake','Platform','Other'
  doc_type TEXT,                 -- detected: 'architecture','runbook','guide','meeting-notes','other'
  linked_repos TEXT,             -- JSON array of repo names mentioned in content
  synced_at TEXT
);
CREATE TABLE IF NOT EXISTS osca_snapshots (
  id INTEGER PRIMARY KEY,
  ts TEXT NOT NULL,
  environment TEXT,              -- 'uat' | 'nonprod'
  status TEXT,                   -- 'green' | 'amber' | 'red'
  active_clients INTEGER,
  escalations INTEGER,
  paused INTEGER,
  blocked INTEGER,
  orphaned INTEGER,
  dead_letter INTEGER,
  detail TEXT                    -- JSON: full health snapshot
);
CREATE TABLE IF NOT EXISTS osca_problems (
  key TEXT PRIMARY KEY,          -- stable group signature (kind:reason)
  kind TEXT,                     -- escalation | orphaned | dead_letter | blocked | stalled
  severity TEXT,                 -- high | medium | low
  title TEXT,
  detail TEXT,
  entity TEXT,                   -- representative entity (or blank for a group)
  stage TEXT,                    -- pipeline stage / milestone
  reason TEXT,
  count INTEGER DEFAULT 1,       -- how many instances in this group
  recent_count INTEGER DEFAULT 0,
  examples TEXT,                 -- JSON: sample instances (entity/detail/ts)
  first_seen TEXT,
  last_seen TEXT,
  source_ts TEXT,                -- newest source timestamp in the group
  status TEXT DEFAULT 'active',  -- active | acknowledged | resolved
  acked_at TEXT,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS swarm_runs (
  id INTEGER PRIMARY KEY,
  trigger TEXT NOT NULL DEFAULT 'manual', -- manual | schedule
  started_at TEXT NOT NULL,
  finished_at TEXT,
  duration_ms INTEGER,
  status TEXT NOT NULL DEFAULT 'running', -- running | done | error
  error TEXT,
  totals TEXT,                   -- JSON {jobs, items, pass, fail, blocked, error, cost_usd, receipts}
  summary TEXT                   -- reviewer-model run summary (markdown)
);
CREATE TABLE IF NOT EXISTS swarm_jobs (
  id INTEGER PRIMARY KEY,
  run_id INTEGER NOT NULL REFERENCES swarm_runs(id),
  item_id INTEGER,               -- ADO work item id, or PR number (see kind)
  item_title TEXT,
  item_type TEXT,
  item_state TEXT,
  item_project TEXT,
  item_url TEXT,
  runbook TEXT NOT NULL,
  model TEXT,
  status TEXT NOT NULL DEFAULT 'queued', -- queued | running | pass | fail | blocked | error
  verdict TEXT,                  -- JSON {status, headline, evidence[], suggested_next_action}
  error TEXT,
  cost_usd REAL,
  duration_ms INTEGER,
  started_at TEXT,
  finished_at TEXT
);
CREATE TABLE IF NOT EXISTS swarm_worktrees (
  id INTEGER PRIMARY KEY,
  run_id INTEGER,
  job_id INTEGER,
  kind TEXT,                     -- profile that spawned it: ticket | tech_debt
  repo TEXT NOT NULL,            -- owner/name
  branch TEXT NOT NULL,          -- swarm/<slug>
  path TEXT NOT NULL,
  base_ref TEXT,
  target_ref TEXT,               -- what it addresses: 'AB#123' | 'repo#674' | finding
  title TEXT,
  status TEXT NOT NULL DEFAULT 'active', -- active | ready | pr_created | discarded | error
  summary TEXT,                  -- coder's account of what it changed
  diff_stat TEXT,
  error TEXT,
  pr_url TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS watchers (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  kind TEXT NOT NULL,            -- teams_chat | process
  target TEXT NOT NULL,          -- chat id / health-job name
  enabled INTEGER NOT NULL DEFAULT 1,
  guardrails TEXT,               -- editable rules the watcher must follow
  interval_minutes INTEGER NOT NULL DEFAULT 5,
  cursor TEXT,                   -- last processed marker (message ts etc.)
  last_run TEXT,
  last_note TEXT,                -- what the last pass found/did
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS companion_memory (
  id INTEGER PRIMARY KEY,
  kind TEXT NOT NULL DEFAULT 'fact', -- fact | preference | correction | person | project | decision | thread
  topic TEXT,                    -- short handle for grouping/recall (e.g. '12345', 'Jane', 'commits')
  content TEXT NOT NULL,         -- the durable statement, in the companion's words
  pinned INTEGER NOT NULL DEFAULT 0,  -- always in context regardless of relevance
  weight REAL NOT NULL DEFAULT 1.0,   -- corrections/decisions outrank stray facts
  source TEXT,                   -- 'user' (told to remember) | 'learned' | 'seed'
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  last_used TEXT
);
CREATE TABLE IF NOT EXISTS pr_thread (
  id INTEGER PRIMARY KEY,
  item_id INTEGER REFERENCES items(id),
  team_id TEXT NOT NULL,
  channel_id TEXT NOT NULL,
  root_message_id TEXT NOT NULL,  -- Teams message to reply into (threaded)
  pr_url TEXT NOT NULL,
  author_name TEXT,               -- colleague's Teams display name (who asked)
  last_reviewed_sha TEXT,         -- PR head commit as of the last full review
  reply_cursor TEXT,              -- last-processed reply timestamp
  status TEXT NOT NULL DEFAULT 'active',  -- active | closed
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(channel_id, root_message_id, pr_url)
);
CREATE INDEX IF NOT EXISTS idx_pr_thread_status ON pr_thread(status);
CREATE INDEX IF NOT EXISTS idx_companion_memory_topic ON companion_memory(topic);
CREATE INDEX IF NOT EXISTS idx_swarm_worktrees_status ON swarm_worktrees(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_swarm_jobs_run ON swarm_jobs(run_id);
CREATE INDEX IF NOT EXISTS idx_swarm_runs_status ON swarm_runs(status, id DESC);
CREATE INDEX IF NOT EXISTS idx_osca_snapshots_ts ON osca_snapshots(ts);
CREATE INDEX IF NOT EXISTS idx_osca_problems_status ON osca_problems(status, severity);
CREATE INDEX IF NOT EXISTS idx_job_runs_job ON job_runs(job, id DESC);
CREATE INDEX IF NOT EXISTS idx_job_runs_started ON job_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_items_status ON items(status);
CREATE INDEX IF NOT EXISTS idx_items_received ON items(received_at);
CREATE INDEX IF NOT EXISTS idx_items_source_type ON items(source, type);
CREATE INDEX IF NOT EXISTS idx_items_type ON items(type);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_action_ts ON audit_log(action, ts);
CREATE INDEX IF NOT EXISTS idx_audit_item ON audit_log(item_id);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_log(ts);
CREATE INDEX IF NOT EXISTS idx_cos_cost_ts ON cos_cost(ts);
CREATE INDEX IF NOT EXISTS idx_items_tier_status ON items(tier, status);
CREATE INDEX IF NOT EXISTS idx_items_source_status ON items(source, status);
CREATE INDEX IF NOT EXISTS idx_items_sender ON items(sender);
CREATE INDEX IF NOT EXISTS idx_drafts_status ON drafts(status);
CREATE INDEX IF NOT EXISTS idx_drafts_item ON drafts(item_id);
CREATE INDEX IF NOT EXISTS idx_items_type_status ON items(type, status);
CREATE INDEX IF NOT EXISTS idx_items_source_urgent ON items(source, urgent DESC, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_item_action ON audit_log(item_id, action, id DESC);
"""

# Columns added after a table first shipped; CREATE TABLE IF NOT EXISTS won't
# add them to existing databases, so they're applied via ALTER TABLE on boot.
MIGRATIONS: dict[str, list[tuple[str, str]]] = {
    "cos_cost": [("duration_ms", "INTEGER"), ("ok", "INTEGER"), ("error", "TEXT")],
    "items": [("snoozed_until", "TEXT")],
    "swarm_jobs": [("kind", "TEXT"),   # 'ado' | 'pr' — target type the worker checked
                   ("phase", "TEXT"),  # live zone for the strategy board (evidence|reasoning)
                   ("phase_note", "TEXT"),
                   ("thread", "INTEGER")],  # thread-pipeline lane that owns this job
    "swarm_runs": [("profile", "TEXT")],  # board | ticket | pr_review | tech_debt
    "watchers": [("prompt", "TEXT")],  # per-watcher agent prompt override (null = default)
    "swarm_worktrees": [("ci_status", "TEXT"),   # running | failed | fixing | green
                        ("review_status", "TEXT"),  # in_review | fixing | ready_to_merge | needs_human
                        ("review_cursor", "TEXT"),  # newest review comment already handled
                        ("uat_status", "TEXT"),  # deployed | smoke_pass | smoke_fail | skipped
                        ("uat_note", "TEXT")],   # deploy/smoke evidence line
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in MIGRATIONS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, ddl in columns:
            if name not in have:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")


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
        _conn.execute("PRAGMA busy_timeout=5000")
        _conn.execute("PRAGMA cache_size=-16000")        # 16 MB page cache (default is 2 MB)
        _conn.execute("PRAGMA synchronous=NORMAL")       # WAL mode safe; faster than FULL
        _conn.execute("PRAGMA wal_autocheckpoint=1000")  # checkpoint every 1000 WAL pages
        _conn.execute("PRAGMA mmap_size=134217728")      # 128 MB memory-mapped I/O for reads
        _conn.execute("PRAGMA temp_store=MEMORY")        # sort/temp tables in RAM, not disk
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        # snoozed_until is migration-only (added via ALTER TABLE above), so this index
        # can't live in SCHEMA — CREATE TABLE there doesn't declare the column, and a
        # fresh DB would fail with "no such column: snoozed_until" before _migrate runs.
        _conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_items_snoozed ON items(snoozed_until) "
            "WHERE snoozed_until IS NOT NULL")
        _conn.commit()
    return _conn


@contextmanager
def transaction():
    """Batch multiple db.execute / db.upsert_item / db.update_item calls into
    a single SQLite commit.  All writes inside the `with` block are held until
    the block exits cleanly; a raised exception rolls back automatically."""
    with _lock:
        _in_transaction.active = True
        try:
            yield
            get_conn().commit()
        except BaseException:
            get_conn().rollback()
            raise
        finally:
            _in_transaction.active = False


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = get_conn().execute(sql, params)
        if not getattr(_in_transaction, "active", False):
            get_conn().commit()
        return cur.lastrowid


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        rows = get_conn().execute(sql, params).fetchall()
        return [dict(r) for r in rows]


# Settings cache: avoids per-send DB reads for kill_switch, dry_run, etc.
# Writes invalidate immediately; reads are fresh within 2 s.
_settings_cache: dict[str, tuple[bool, "str | None", float]] = {}  # key -> (found, value, expires)
_SETTINGS_TTL = 2.0


def get_setting(key: str, default: str | None = None) -> str | None:
    entry = _settings_cache.get(key)
    if entry is not None:
        found, val, exp = entry
        if _time.monotonic() < exp:
            return val if found else default
    rows = query("SELECT value FROM settings WHERE key=?", (key,))
    if rows:
        val = rows[0]["value"]
        _settings_cache[key] = (True, val, _time.monotonic() + _SETTINGS_TTL)
        return val
    _settings_cache[key] = (False, None, _time.monotonic() + _SETTINGS_TTL)
    return default


def get_settings_multi(keys: list, defaults: dict | None = None) -> dict:
    """Fetch N settings keys in a single SELECT rather than N individual queries.
    Uses and populates the per-key TTL cache so subsequent get_setting() calls
    for the same keys are served from cache."""
    now = _time.monotonic()
    result = {}
    miss = []
    for k in keys:
        entry = _settings_cache.get(k)
        if entry is not None:
            found, val, exp = entry
            if now < exp:
                result[k] = val if found else (defaults or {}).get(k)
                continue
        miss.append(k)
    if miss:
        ph = ",".join("?" * len(miss))
        rows = query(f"SELECT key, value FROM settings WHERE key IN ({ph})", tuple(miss))
        found_keys = {r["key"] for r in rows}
        for r in rows:
            _settings_cache[r["key"]] = (True, r["value"], now + _SETTINGS_TTL)
            result[r["key"]] = r["value"]
        for k in miss:
            if k not in found_keys:
                _settings_cache[k] = (False, None, now + _SETTINGS_TTL)
                result[k] = (defaults or {}).get(k)
    return result


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
    _settings_cache.pop(key, None)  # invalidate so next read hits DB


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
    """Insert an item if unseen. Returns (item_id, is_new).

    Hot path (new item): 1 DB op via INSERT OR IGNORE + rowcount check.
    Cold path (duplicate): 2 ops — the OR IGNORE no-op + a SELECT for the id.
    Sweeps are dominated by new items, so this halves the DB round-trips there.
    """
    ts = now()
    with _lock:
        cur = get_conn().execute(
            "INSERT OR IGNORE INTO items(source,type,external_id,conversation_id,sender,"
            "sender_id,subject,content,received_at,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (source, type_, external_id, conversation_id, sender, sender_id,
             subject, content, received_at, ts, ts))
        if not getattr(_in_transaction, "active", False):
            get_conn().commit()
        if cur.rowcount == 1:
            return cur.lastrowid, True
        row = get_conn().execute(
            "SELECT id FROM items WHERE source=? AND external_id=?",
            (source, external_id)).fetchone()
        return row["id"], False


# Only real columns may be set through update_item; field names are
# interpolated into SQL, so anything else must be rejected, not trusted.
_ITEM_FIELDS = {"source", "type", "external_id", "conversation_id", "sender", "sender_id",
                "subject", "content", "received_at", "tier", "tier_reasoning", "status",
                "action_taken", "urgent", "updated_at", "snoozed_until"}


def update_item(item_id: int, **fields) -> None:
    unknown = set(fields) - _ITEM_FIELDS
    if unknown:
        raise ValueError(f"update_item: unknown fields {sorted(unknown)}")
    fields["updated_at"] = now()
    sets = ", ".join(f"{k}=?" for k in fields)
    execute(f"UPDATE items SET {sets} WHERE id=?", (*fields.values(), item_id))
