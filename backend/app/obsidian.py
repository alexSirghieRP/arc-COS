"""Obsidian vault reader for the Chief-of-Staff board.

Exposes vault_stats(), recent_notes(), read_note(), and note_graph().
All I/O is synchronous and read-only; hidden dirs/files are always skipped.
"""

from __future__ import annotations

import re
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from .config import vault_root

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_HIDDEN = re.compile(r"(^|[\\/])\.")

_TAG_RE = re.compile(r"#([a-zA-Z][a-zA-Z0-9_/-]*)")
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)")
_MD_LINK_RE = re.compile(r"\[(?:[^\]]*)\]\(([^)]+\.md[^)]*)\)")
# GitHub permalink line-anchor fragments (#L601-L609) pasted into notes match the
# tag pattern above but aren't tags — filter them out of tag extraction/counts.
_LINE_ANCHOR_RE = re.compile(r"^L\d+(-L\d+)?$", re.I)


def _is_hidden(path: Path, root: Path) -> bool:
    """Return True if any component of *path* relative to *root* starts with '.'."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return True
    return any(part.startswith(".") for part in rel.parts)


def _all_md_files(root: Path) -> list[Path]:
    """Return all non-hidden .md files under *root*, sorted for determinism."""
    results: list[Path] = []
    try:
        for p in root.rglob("*.md"):
            if not _is_hidden(p, root):
                results.append(p)
    except (PermissionError, OSError):
        pass
    return sorted(results)


def _read_safe(path: Path) -> str:
    """Read a file, returning '' on any error."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (PermissionError, OSError):
        return ""


def _top_level_folder(path: Path, root: Path) -> str:
    """Return the first path component below *root*, or '' for vault-root files."""
    try:
        rel = path.relative_to(root)
    except ValueError:
        return ""
    return rel.parts[0] if len(rel.parts) > 1 else ""


# ---------------------------------------------------------------------------
# vault_stats — 60-second TTL cache
# ---------------------------------------------------------------------------

_stats_cache: dict[str, Any] = {}
_stats_cache_ts: float = 0.0
_STATS_TTL = 60.0


def vault_stats() -> dict:
    """Return aggregate statistics about the Obsidian vault.

    {
      "total_notes": int,
      "total_folders": int,
      "folders": [{"name": str, "count": int}],   # top-level dirs, desc by count
      "recent_modified": [{"path": str, "mtime": float, "name": str}],  # 10 most recent
      "total_words": int,
      "top_tags": [{"tag": str, "count": int}],   # top 20 #tags
    }
    """
    global _stats_cache, _stats_cache_ts

    now = time.monotonic()
    if _stats_cache and (now - _stats_cache_ts) < _STATS_TTL:
        return _stats_cache

    root = vault_root()
    md_files = _all_md_files(root)

    total_words = 0
    tag_counter: Counter = Counter()
    folder_counter: Counter = Counter()
    file_infos: list[dict] = []

    for p in md_files:
        content = _read_safe(p)
        total_words += len(content.split())
        for tag in _TAG_RE.findall(content):
            if not _LINE_ANCHOR_RE.match(tag):
                tag_counter[tag] += 1

        folder = _top_level_folder(p, root)
        if folder:
            folder_counter[folder] += 1

        try:
            mtime = p.stat().st_mtime
        except (PermissionError, OSError):
            mtime = 0.0

        rel = str(p.relative_to(root))
        file_infos.append({"path": rel, "name": p.stem, "mtime": mtime})

    # Top-level directory count
    total_folders = 0
    try:
        total_folders = sum(
            1
            for d in root.iterdir()
            if d.is_dir() and not d.name.startswith(".")
        )
    except (PermissionError, OSError):
        pass

    # 10 most-recently-modified notes
    file_infos.sort(key=lambda x: x["mtime"], reverse=True)
    recent_modified = file_infos[:10]

    folders = [
        {"name": name, "count": count}
        for name, count in folder_counter.most_common()
    ]

    top_tags = [
        {"tag": tag, "count": count}
        for tag, count in tag_counter.most_common(20)
    ]

    _stats_cache = {
        "total_notes": len(md_files),
        "total_folders": total_folders,
        "folders": folders,
        "recent_modified": recent_modified,
        "total_words": total_words,
        "top_tags": top_tags,
    }
    _stats_cache_ts = now
    return _stats_cache


# ---------------------------------------------------------------------------
# recent_notes
# ---------------------------------------------------------------------------


def recent_notes(limit: int = 20) -> list[dict]:
    """Return the *limit* most-recently-modified notes with a short preview.

    Each item:
    {"path": str, "name": str, "mtime": float, "preview": str, "size": int}
    *path* is relative to the vault root.
    *preview* is the first 200 chars of file content.
    """
    root = vault_root()
    md_files = _all_md_files(root)

    infos: list[dict] = []
    for p in md_files:
        try:
            stat = p.stat()
            mtime = stat.st_mtime
            size = stat.st_size
        except (PermissionError, OSError):
            mtime = 0.0
            size = 0

        infos.append({"_path": p, "mtime": mtime, "size": size})

    infos.sort(key=lambda x: x["mtime"], reverse=True)
    infos = infos[:limit]

    results: list[dict] = []
    for item in infos:
        p: Path = item["_path"]
        content = _read_safe(p)
        preview = content[:200]
        rel = str(p.relative_to(root))
        results.append(
            {
                "path": rel,
                "name": p.stem,
                "mtime": item["mtime"],
                "preview": preview,
                "size": item["size"],
            }
        )
    return results


# ---------------------------------------------------------------------------
# read_note
# ---------------------------------------------------------------------------


def read_note(rel_path: str) -> dict | None:
    """Read a specific note by its vault-relative path.

    Returns:
    {"path": str, "name": str, "content": str, "mtime": float,
     "links": [str], "tags": [str]}

    *links* includes both [[wikilinks]] and markdown [text](file.md) hrefs.
    Returns None if the file is not found or falls outside the vault.
    """
    root = vault_root()
    target = (root / rel_path).resolve()

    # Security: must stay inside the vault
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None

    if not target.exists() or not target.is_file():
        return None
    if _is_hidden(target, root):
        return None

    content = _read_safe(target)

    try:
        mtime = target.stat().st_mtime
    except (PermissionError, OSError):
        mtime = 0.0

    wikilinks = _WIKILINK_RE.findall(content)
    md_links = _MD_LINK_RE.findall(content)
    links = list(dict.fromkeys(wikilinks + md_links))  # dedupe, preserve order

    tags = list(dict.fromkeys(t for t in _TAG_RE.findall(content) if not _LINE_ANCHOR_RE.match(t)))

    return {
        "path": str(target.relative_to(root)),
        "name": target.stem,
        "content": content,
        "mtime": mtime,
        "links": links,
        "tags": tags,
    }


# ---------------------------------------------------------------------------
# note_graph — 120-second TTL cache
# ---------------------------------------------------------------------------

_graph_cache: dict[str, Any] = {}
_graph_cache_ts: float = 0.0
_GRAPH_TTL = 120.0


def create_note(title: str, content: str, folder: str = "", append: bool = False) -> dict | None:
    """Create or append to a note in the vault. Returns {path, created}, or None if
    *folder* would escape the vault root."""
    root = vault_root()
    # sanitize title → filename
    safe = re.sub(r'[\\/:*?"<>|]', '-', title).strip()
    if not safe:
        return None
    if folder:
        target_dir = (root / folder).resolve()
        # Security: must stay inside the vault (blocks absolute paths and `..` escapes —
        # see read_note()'s identical check above).
        try:
            target_dir.relative_to(root.resolve())
        except ValueError:
            return None
        target_dir.mkdir(parents=True, exist_ok=True)
    else:
        target_dir = root
    path = target_dir / f"{safe}.md"
    existed = path.exists()
    if append and existed:
        existing = path.read_text(encoding="utf-8")
        path.write_text(existing + "\n\n" + content, encoding="utf-8")
        return {"path": str(path.relative_to(root)), "created": False}
    else:
        path.write_text(content, encoding="utf-8")
        return {"path": str(path.relative_to(root)), "created": not existed}


def search_vault(q: str, limit: int = 20) -> list[dict]:
    """Return notes containing q (case-insensitive), with a snippet."""
    root = vault_root()
    needle = q.lower()
    results = []
    for path in _all_md_files(root):
        try:
            text = _read_safe(path)
            idx = text.lower().find(needle)
            if idx == -1:
                continue
            start = max(0, idx - 60)
            end = min(len(text), idx + len(q) + 60)
            snippet = text[start:end].replace("\n", " ").strip()
            results.append({
                "name": path.stem,
                "path": str(path.relative_to(root)),
                "snippet": snippet,
                "folder": _top_level_folder(path, root),
                "mtime": path.stat().st_mtime,
            })
            if len(results) >= limit:
                break
        except Exception:
            continue
    return results


def vault_activity(days: int = 7) -> dict:
    """Count notes modified per calendar day for the last N days.
    Returns {dates: [str], counts: [int], total_modified: int}"""
    import datetime
    root = vault_root()
    now = datetime.datetime.now()
    day_counts = {}
    for d in range(days):
        dt = (now - datetime.timedelta(days=d)).date()
        day_counts[dt.isoformat()] = 0
    total = 0
    for path in _all_md_files(root):
        try:
            mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime)
            delta = now - mtime
            if delta.days < days:
                key = mtime.date().isoformat()
                if key in day_counts:
                    day_counts[key] = day_counts.get(key, 0) + 1
                    total += 1
        except Exception:
            continue
    dates = sorted(day_counts.keys())
    counts = [day_counts[d] for d in dates]
    return {"dates": dates, "counts": counts, "total_modified": total}


def note_graph(max_nodes: int = 150) -> dict:
    """Build a wikilink graph of the vault for the second-brain map.

    Returns:
    {
      "nodes": [{"id": str, "label": str, "size": int, "folder": str}],
      "edges": [{"source": str, "target": str}],
    }

    Algorithm:
    1. Scan all .md files; extract [[wikilinks]] from each.
    2. Build edges (source_rel_path -> target_stem).
    3. Resolve target stems to rel_paths where possible.
    4. Count in-degree + out-degree for each node.
    5. Keep the top *max_nodes* by combined degree.
    6. Filter edges so both endpoints are in the node set.
    """
    global _graph_cache, _graph_cache_ts

    now = time.monotonic()
    if _graph_cache and (now - _graph_cache_ts) < _GRAPH_TTL:
        return _graph_cache

    root = vault_root()
    md_files = _all_md_files(root)

    # Map stem (lower-cased) -> rel_path for resolution
    stem_to_rel: dict[str, str] = {}
    for p in md_files:
        rel = str(p.relative_to(root))
        stem_to_rel[p.stem.lower()] = rel

    # Raw edges: list of (source_rel, target_stem_raw)
    raw_edges: list[tuple[str, str]] = []
    out_degree: Counter = Counter()

    for p in md_files:
        content = _read_safe(p)
        source_rel = str(p.relative_to(root))
        targets = _WIKILINK_RE.findall(content)
        for t in targets:
            t = t.strip()
            if t:
                raw_edges.append((source_rel, t))
                out_degree[source_rel] += 1

    # Resolve targets to rel paths (fallback to stem as synthetic id)
    resolved_edges: list[tuple[str, str]] = []
    in_degree: Counter = Counter()

    for source_rel, target_raw in raw_edges:
        target_key = target_raw.lower()
        target_rel = stem_to_rel.get(target_key, target_raw)
        resolved_edges.append((source_rel, target_rel))
        in_degree[target_rel] += 1

    # Combined degree for all known nodes
    all_node_ids: set[str] = set(str(p.relative_to(root)) for p in md_files)
    # also include synthetic targets that aren't real files
    for _, t in resolved_edges:
        all_node_ids.add(t)

    combined: Counter = Counter()
    for nid in all_node_ids:
        combined[nid] = in_degree[nid] + out_degree[nid]

    top_nodes = {nid for nid, _ in combined.most_common(max_nodes)}

    # Build node list
    nodes: list[dict] = []
    for nid in top_nodes:
        p = root / nid
        label = Path(nid).stem
        folder = _top_level_folder(root / nid, root) if nid in set(
            str(f.relative_to(root)) for f in md_files
        ) else ""
        nodes.append(
            {
                "id": nid,
                "label": label,
                "size": in_degree[nid],
                "folder": folder,
            }
        )

    # Filter edges
    edges: list[dict] = [
        {"source": src, "target": tgt}
        for src, tgt in resolved_edges
        if src in top_nodes and tgt in top_nodes and src != tgt
    ]

    # Deduplicate edges
    seen_edges: set[tuple[str, str]] = set()
    deduped_edges: list[dict] = []
    for e in edges:
        key = (e["source"], e["target"])
        if key not in seen_edges:
            seen_edges.add(key)
            deduped_edges.append(e)

    result = {"nodes": nodes, "edges": deduped_edges}
    _graph_cache = result
    _graph_cache_ts = now
    return result
