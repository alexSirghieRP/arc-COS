"""Persistent companion memory (goal pillar 1).

A durable store the companion reads on every turn and writes to as it learns:
who the user is, their projects and people, preferences, decisions, corrections,
and open threads. Survives restarts (plain sqlite). No embeddings - relevance is
keyword overlap + recency + weight, which is fast (local) and good enough for
one person's working context.

Kinds carry weight so a correction ("don't force-push, ever") always outranks
a stray fact. Pinned entries are always in context.
"""

import re

from . import db

_KINDS = ("fact", "preference", "correction", "person", "project",
          "decision", "thread")
# corrections and preferences are behavior-shaping - they should almost always
# make it into context, so they carry more weight by default.
_KIND_WEIGHT = {"correction": 3.0, "preference": 2.5, "decision": 2.0,
                "thread": 1.5, "person": 1.2, "project": 1.2, "fact": 1.0}

_STOP = set("the a an and or but is are was to of in on for it this that with "
            "i you we he she they do does what who when where how my your".split())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9#]+", (text or "").lower())
            if w not in _STOP and len(w) > 1}


def add(content: str, *, kind: str = "fact", topic: str = "",
        pinned: bool = False, source: str = "user") -> int:
    kind = kind if kind in _KINDS else "fact"
    content = content.strip()
    if not content:
        return 0
    # dedupe: if a near-identical statement exists, refresh it instead
    existing = db.query(
        "SELECT id FROM companion_memory WHERE content=? LIMIT 1", (content,))
    if existing:
        db.execute("UPDATE companion_memory SET updated_at=?, kind=?, topic=?, "
                   "pinned=? WHERE id=?",
                   (db.now(), kind, topic or None, 1 if pinned else 0, existing[0]["id"]))
        return existing[0]["id"]
    return db.execute(
        "INSERT INTO companion_memory(kind, topic, content, pinned, weight, "
        "source, created_at, updated_at) VALUES(?,?,?,?,?,?,?,?)",
        (kind, topic or None, content, 1 if pinned else 0,
         _KIND_WEIGHT.get(kind, 1.0), source, db.now(), db.now()))


def forget(query: str) -> int:
    """Delete memories matching a query (topic or content substring)."""
    q = f"%{query.strip()}%"
    rows = db.query("SELECT id FROM companion_memory WHERE content LIKE ? "
                    "OR topic LIKE ?", (q, q))
    for r in rows:
        db.execute("DELETE FROM companion_memory WHERE id=?", (r["id"],))
    db.audit("companion_memory_forget", {"query": query, "removed": len(rows)},
             actor="user")
    return len(rows)


def recall(query: str = "", limit: int = 12) -> list[dict]:
    """Memories relevant to a query (or everything, most-weighted first)."""
    rows = [dict(r) for r in db.query("SELECT * FROM companion_memory")]
    if not query.strip():
        rows.sort(key=lambda r: (r["pinned"], r["weight"], r["updated_at"]),
                  reverse=True)
        return rows[:limit]
    qt = _tokens(query)
    scored = []
    for r in rows:
        blob = (r["content"] + " " + (r["topic"] or "")).lower()
        rt = _tokens(blob)
        # exact token overlap + substring match so 'commit' hits 'commits',
        # 'deploy' hits 'deployment', etc. (stemming-lite for one-person recall)
        overlap = len(qt & rt) + sum(
            1 for w in qt if len(w) >= 4 and w not in rt and w in blob)
        if overlap or r["pinned"]:
            score = overlap * 2 + r["weight"] + (5 if r["pinned"] else 0)
            scored.append((score, r))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [r for _, r in scored[:limit]]


def context(message: str, limit: int = 10) -> str:
    """The memory block injected into the companion prompt each turn: pinned +
    the entries most relevant to the current message. Marks them used."""
    hits = recall(message, limit=limit)
    if not hits:
        return ""
    ids = [h["id"] for h in hits]
    db.execute(f"UPDATE companion_memory SET last_used=? WHERE id IN "
               f"({','.join('?' * len(ids))})", (db.now(), *ids))
    lines = [f"- ({h['kind']}) {h['content']}" for h in hits]
    return "\n".join(lines)


def all_memories() -> list[dict]:
    return [dict(r) for r in db.query(
        "SELECT * FROM companion_memory ORDER BY pinned DESC, weight DESC, "
        "updated_at DESC")]


# Parse "remember that ...", "remember: ...", "note that ...", "forget ..."
_REMEMBER = re.compile(r"^\s*(?:remember|note|keep in mind)(?:\s+that)?\s*[:,-]?\s*(.+)$", re.I)
_FORGET = re.compile(r"^\s*forget(?:\s+(?:that|about))?\s*[:,-]?\s*(.+)$", re.I)
_RECALL = re.compile(r"^\s*(?:what do you (?:know|remember)(?:\s+about)?|recall|what have you got on)\s*[:,-]?\s*(.*)$", re.I)


def parse_directive(text: str) -> tuple[str, str] | None:
    """('remember'|'forget'|'recall', payload) if the message is a memory op."""
    m = _FORGET.match(text)
    if m:
        return ("forget", m.group(1).strip())
    m = _REMEMBER.match(text)
    if m:
        return ("remember", m.group(1).strip())
    m = _RECALL.match(text)
    if m:
        return ("recall", m.group(1).strip())
    return None


def _classify(text: str) -> str:
    low = text.lower()
    if re.search(r"\b(don'?t|never|always|stop|prefer|i like|i want|instead)\b", low):
        return "correction" if re.search(r"\b(don'?t|never|stop|instead)\b", low) else "preference"
    if re.search(r"\b(decided|we'?ll|going with|chose)\b", low):
        return "decision"
    return "fact"


def remember_from_text(payload: str, source: str = "user") -> int:
    return add(payload, kind=_classify(payload), source=source, pinned=True)


# Baseline the companion knows on day one - generic example facts in the shape
# real ones take. Replace or extend with your own people/projects (or just let
# the companion learn them over time via the memory command).
_SEED = [
    ("person", "Jane Doe", "Jane Doe runs an agent-heavy setup and posts detailed findings in the PR review chat; she's usually ahead on the main project's work."),
    ("person", "John Smith", "John Smith is a UAT tester and owns decisions on data-mapping questions; clarifications on his tickets wait on him."),
    ("person", "Sam Lee", "Sam Lee approves PRs in the review chat - a quick approval from them unblocks merges."),
    ("project", "example-repo", "The main project: GitHub repo acme-org/example-repo, ADO project 'Example Project'. Promo PRs merge dev into the 'uat' branch to deploy to UAT."),
    ("project", "chief-of-staff", "This app - the user's CoS/automation harness (swarms, watchers, the companion)."),
    ("preference", "PRs", "Swarm PRs are drafts, pushed with --no-verify (CI is the verifier), titled with an AB# prefix so Azure Boards auto-links them."),
    ("preference", "comments", "Keep ADO comments simple and conversational, not walls of evidence; tag the person who should answer."),
]


def seed_defaults() -> None:
    if db.query("SELECT 1 FROM companion_memory LIMIT 1"):
        return
    for kind, topic, content in _SEED:
        add(content, kind=kind, topic=topic, source="seed", pinned=(kind == "preference"))
