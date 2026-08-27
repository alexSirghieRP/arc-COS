"""Desktop agent: the companion's secure hands on the user's machine.

The CoS backend already RUNS on the desktop, so this is an in-process agent —
no inbound ports, no public endpoints, nothing exposed. The Teams companion
(48:notes, writable only by the user, sender-verified) is the only caller, and
every operation is bounded by this module's rules:

- approved project ROOTS only (policy desktop.roots); canonical-path checks
  block traversal and symlink escapes
- sensitive paths (.env, keys, credentials, keychains...) are never readable
- commands are risk-classified: read-only runs freely inside roots; controlled
  writes run when clearly requested; HIGH-RISK returns a confirmation token
  and executes only after the user replies "confirm <token>" (exact command shown
  first, 10-minute expiry)
- output is redacted (token/key/password patterns) and tail-truncated
- persistent named terminal sessions (term-N) with output buffers; they die
  with the backend process and are reported as lost after a restart, never
  silently resurrected
- every operation is audited (desktop_command)
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import time
import uuid
from collections import deque
from pathlib import Path

from . import db
from .config import policy

log = logging.getLogger("chief.desktop")

# ---- configuration ----------------------------------------------------------

def _cfg() -> dict:
    return policy().get("desktop", {}) or {}


def roots() -> list[Path]:
    out = []
    for r in _cfg().get("roots") or ["~/RP"]:
        p = Path(os.path.expanduser(str(r))).resolve()
        if p.exists():
            out.append(p)
    return out


def projects() -> dict[str, dict]:
    """Configured projects + auto-discovered top-level git repos in the roots."""
    out: dict[str, dict] = {}
    for name, meta in (_cfg().get("projects") or {}).items():
        m = dict(meta or {})
        m["path"] = os.path.expanduser(str(m.get("path", "")))
        out[name] = m
    for root in roots():
        try:
            for child in sorted(root.iterdir()):
                if child.name.startswith(".") or not (child / ".git").exists():
                    continue
                if not any(v["path"] == str(child) for v in out.values()):
                    out.setdefault(child.name, {"path": str(child)})
        except OSError:
            continue
    return out


def _project_path(name_or_path: str) -> Path:
    """Resolve a project name or path to a canonical dir INSIDE the roots."""
    projs = projects()
    if name_or_path in projs:
        p = Path(projs[name_or_path]["path"]).resolve()
    else:
        # fuzzy: case-insensitive substring on project names
        low = name_or_path.strip().lower()
        hit = next((v for k, v in projs.items() if low in k.lower()), None)
        p = Path(hit["path"]).resolve() if hit \
            else Path(os.path.expanduser(name_or_path)).resolve()
    if not any(p == r or r in p.parents for r in roots()):
        raise PermissionError(f"'{name_or_path}' is outside the approved roots")
    if not p.exists():
        raise FileNotFoundError(f"{p} does not exist")
    return p


# ---- path safety -------------------------------------------------------------

_SENSITIVE = re.compile(
    r"(^|/)(\.ssh|\.aws|\.azure|\.gnupg)(/|$)"          # credential DIRECTORIES
    r"|(^|/)(\.env[^/]*|id_rsa[^/]*|id_ed25519[^/]*|credentials|\.netrc|\.npmrc"
    r"|\.pypirc|[^/]*\.pem|[^/]*\.p12|[^/]*keychain[^/]*|secrets?\.[^/]+"
    r"|token[^/]*\.txt)$", re.I)


def safe_path(project: str, rel: str) -> Path:
    """Canonicalize project-relative path; block escapes and sensitive files."""
    base = _project_path(project)
    p = (base / rel).resolve() if rel else base
    if not (p == base or base in p.parents):
        raise PermissionError(f"path escapes the project: {rel}")
    if _SENSITIVE.search(str(p)):
        raise PermissionError(f"sensitive path blocked: {rel}")
    return p


_REDACT = re.compile(
    r"(ghp_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|AKIA[A-Z0-9]{16}"
    r"|xox[baprs]-[A-Za-z0-9-]{10,}|eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}"
    r"|(?i:(password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*)[^\s\"']{6,})")


def redact(text: str) -> str:
    return _REDACT.sub(lambda m: (m.group(2) + "«redacted»") if m.group(2)
                       else "«redacted»", text or "")


# ---- command risk classification ---------------------------------------------

_HIGH = re.compile(
    r"(\brm\b|\brmdir\b|git\s+reset\s+--hard|git\s+clean|git\s+push\s+.*(-f\b|--force)"
    r"|\bsudo\b|\bchmod\b|\bchown\b|\bkillall\b|pkill\s+-9|\bdd\b|\bmkfs"
    r"|\bshutdown\b|\breboot\b|launchctl|\bsecurity\b|keychain"
    r"|npm\s+i(nstall)?\s+(-g|--global)|pip\d?\s+install\s+--user\s+-g"
    r"|curl[^|]*\|\s*(ba)?sh|wget[^|]*\|\s*(ba)?sh|>\s*/dev/|:\(\)\s*\{"
    r"|git\s+checkout\s+--\s|\bdroptable\b|prod(uction)?\s+deploy)", re.I)

_READ = re.compile(
    r"^\s*(pwd|ls|cat|head|tail|wc|du|df|ps|lsof|which|env\s*$|echo"
    r"|git\s+(status|log|diff|branch|worktree\s+list|show|remote|stash\s+list)"
    r"|rg|grep|find|tree|uv\s+run\s+pytest|pytest|npm\s+test|make\s+test"
    r"|node\s+--version|python\d?\s+--version|uname)\b", re.I)


def classify(cmd: str) -> str:
    """'read' | 'write' | 'high' — high risk requires explicit confirmation."""
    if _HIGH.search(cmd) or "$(" in cmd or "`" in cmd:
        return "high"
    if _READ.match(cmd.strip()):
        return "read"
    return "write"


# ---- pending confirmations ----------------------------------------------------

_pending: dict[str, dict] = {}  # token -> {cmd, project, expires}


def _make_pending(project: str, cmd: str) -> str:
    token = uuid.uuid4().hex[:6]
    _pending[token] = {"cmd": cmd, "project": project, "expires": time.time() + 600}
    return token


def take_pending(token: str) -> dict | None:
    p = _pending.pop(token.strip().lower(), None)
    if p and p["expires"] > time.time():
        return p
    return None


# ---- command execution ---------------------------------------------------------

_MAX_TAIL = 1400


async def run_command(project: str, cmd: str, timeout: int = 120,
                      confirmed: bool = False) -> dict:
    """Run one command in a project dir with classification + audit."""
    cwd = _project_path(project)
    risk = classify(cmd)
    detail = {"project": project, "cwd": str(cwd), "cmd": cmd, "risk": risk,
              "confirmed": confirmed}
    if risk == "high" and not confirmed:
        token = _make_pending(project, cmd)
        db.audit("desktop_command", {**detail, "state": "needs_confirm",
                                     "token": token}, actor="user")
        return {"needs_confirm": token, "cmd": cmd, "cwd": str(cwd),
                "note": f"HIGH RISK - reply 'confirm {token}' to run exactly: {cmd}"}
    t0 = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"})
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            code = proc.returncode
            running = False
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            out, code, running = b"(timed out)", -1, False
    except Exception as e:
        db.audit("desktop_command", {**detail, "state": "error",
                                     "error": str(e)[:200]}, actor="user")
        return {"cmd": cmd, "cwd": str(cwd), "exit": -1,
                "tail": f"failed to start: {e}", "running": False}
    tail = redact(out.decode(errors="replace"))[-_MAX_TAIL:]
    db.audit("desktop_command", {**detail, "state": "done", "exit": code,
                                 "ms": int((time.monotonic() - t0) * 1000)},
             actor="user")
    return {"cmd": cmd, "cwd": str(cwd), "exit": code, "tail": tail,
            "running": running}


# ---- persistent terminal sessions ----------------------------------------------

_terms: dict[str, dict] = {}
_term_seq = 0


async def open_terminal(project: str, cmd: str, timeout_note: int = 0) -> dict:
    """Start a long-running process in a named session (term-N)."""
    global _term_seq
    cwd = _project_path(project)
    risk = classify(cmd)
    if risk == "high":
        token = _make_pending(project, cmd)
        return {"needs_confirm": token,
                "note": f"HIGH RISK - reply 'confirm {token}' to run exactly: {cmd}"}
    _term_seq += 1
    sid = f"term-{_term_seq}"
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT, stdin=asyncio.subprocess.PIPE,
        env={**os.environ, "GIT_PAGER": "cat", "PAGER": "cat"})
    buf: deque[str] = deque(maxlen=300)
    sess = {"id": sid, "project": project, "cwd": str(cwd), "pid": proc.pid,
            "cmd": cmd, "started": db.now(), "last": db.now(),
            "proc": proc, "buf": buf}
    _terms[sid] = sess

    async def _pump():
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                buf.append(redact(line.decode(errors="replace").rstrip()))
                sess["last"] = db.now()
        except Exception:
            pass

    asyncio.create_task(_pump())
    db.audit("desktop_command", {"project": project, "cwd": str(cwd), "cmd": cmd,
                                 "risk": risk, "state": "terminal_started",
                                 "session": sid, "pid": proc.pid}, actor="user")
    return {"session": sid, "project": project, "cmd": cmd, "pid": proc.pid,
            "running": True}


def list_terminals() -> list[dict]:
    out = []
    for s in _terms.values():
        running = s["proc"].returncode is None
        out.append({"id": s["id"], "project": s["project"], "cmd": s["cmd"],
                    "pid": s["pid"], "started": s["started"], "last": s["last"],
                    "running": running,
                    "exit": None if running else s["proc"].returncode})
    return out


def terminal_output(sid: str, lines: int = 25) -> dict:
    s = _terms.get(sid)
    if not s:
        return {"error": f"no session {sid} (sessions do not survive backend restarts)"}
    running = s["proc"].returncode is None
    return {"id": sid, "running": running, "cmd": s["cmd"],
            "exit": None if running else s["proc"].returncode,
            "tail": "\n".join(list(s["buf"])[-lines:])[-_MAX_TAIL:]}


async def terminal_send(sid: str, text: str) -> dict:
    s = _terms.get(sid)
    if not s:
        return {"error": f"no session {sid}"}
    if s["proc"].returncode is not None:
        return {"error": f"{sid} already exited ({s['proc'].returncode})"}
    if classify(text) == "high":
        token = _make_pending(s["project"], text)
        return {"needs_confirm": token,
                "note": f"HIGH RISK input - reply 'confirm {token}' to send: {text}"}
    s["proc"].stdin.write((text.rstrip("\n") + "\n").encode())
    await s["proc"].stdin.drain()
    db.audit("desktop_command", {"session": sid, "cmd": f"(stdin) {text}",
                                 "state": "sent"}, actor="user")
    return {"id": sid, "sent": text}


async def terminal_stop(sid: str) -> dict:
    s = _terms.get(sid)
    if not s:
        return {"error": f"no session {sid}"}
    with contextlib.suppress(ProcessLookupError):
        s["proc"].terminate()
    db.audit("desktop_command", {"session": sid, "state": "stopped"}, actor="user")
    return {"id": sid, "stopped": True}


# ---- VS Code -------------------------------------------------------------------

async def vscode_open(project: str, file: str = "", line: int = 0) -> dict:
    target = safe_path(project, file) if file else _project_path(project)
    args = ["code", "-g", f"{target}:{line}"] if (file and line) else ["code", str(target)]
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    _, err = await proc.communicate()
    if proc.returncode != 0:
        return {"error": f"code CLI failed: {err.decode()[:120]}"}
    db.audit("desktop_command", {"state": "vscode_open", "target": str(target),
                                 "line": line}, actor="user")
    return {"opened": str(target) + (f":{line}" if line else "")}


# ---- files ---------------------------------------------------------------------

def files_ls(project: str, rel: str = "", depth: int = 2) -> dict:
    base = safe_path(project, rel)
    out = []
    base_depth = len(base.parts)
    for p in sorted(base.rglob("*")):
        if len(p.parts) - base_depth > depth:
            continue
        if any(part in (".git", "node_modules", "__pycache__", ".venv", "dist")
               for part in p.parts):
            continue
        out.append(("📁 " if p.is_dir() else "· ")
                   + str(p.relative_to(base)))
        if len(out) >= 80:
            out.append("… (truncated at 80 entries)")
            break
    return {"path": str(base), "entries": out}


def file_read(project: str, rel: str, max_chars: int = 2500) -> dict:
    p = safe_path(project, rel)
    if not p.is_file():
        return {"error": f"not a file: {rel}"}
    text = redact(p.read_text(errors="replace")[:max_chars])
    db.audit("desktop_command", {"state": "file_read", "path": str(p)}, actor="user")
    return {"path": str(p), "content": text,
            "truncated": p.stat().st_size > max_chars}


async def search_code(project: str, query: str, max_hits: int = 20) -> dict:
    cwd = _project_path(project)
    q = shlex.quote(query)
    r = await run_command(project, f"rg -n --no-heading -m 3 {q} . | head -{max_hits}",
                          timeout=30)
    return {"query": query, "cwd": str(cwd), "hits": r.get("tail", "")}


async def changed_files(project: str) -> dict:
    r = await run_command(project, "git status --short", timeout=20)
    return {"project": project, "changes": r.get("tail", "").strip() or "clean"}


# ---- status --------------------------------------------------------------------

async def _git_brief(name: str) -> str:
    """One subprocess per project: branch + change count in a single call."""
    try:
        cwd = _project_path(name)
    except Exception:
        return ""
    try:
        proc = await asyncio.create_subprocess_shell(
            "printf '%s|' \"$(git branch --show-current)\"; "
            "git status --short | wc -l | tr -d ' '",
            cwd=str(cwd), stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=8)
        branch, _, count = out.decode(errors="replace").strip().partition("|")
        return f"{name}: branch {branch or '?'}, {count or '0'} changed file(s)"
    except Exception:
        return ""


async def status() -> str:
    """Mobile-friendly desktop digest. Per-project git runs concurrently so the
    whole status is one round of parallel subprocesses, not a serial chain."""
    projs = projects()
    lines = ["Desktop online (agent runs in-process with CoS)",
             f"approved roots: {', '.join(str(r) for r in roots())}",
             f"projects: {len(projs)} — " + ", ".join(list(projs)[:8])
             + ("…" if len(projs) > 8 else "")]
    briefs = await asyncio.gather(*[_git_brief(n) for n in list(projs)[:4]])
    lines += [b for b in briefs if b]
    terms = list_terminals()
    lines.append("terminals: " + (", ".join(
        f"{t['id']} ({'running' if t['running'] else 'exited'}: {t['cmd'][:30]})"
        for t in terms) or "none"))
    return "\n".join(lines)
