"""Deployed agent pipeline — live monitor.

Traces what's actually happening in a deployed agent pipeline and flags
failures. The GKE gateway (gke-l7-rilb) is a *regional internal* load
balancer, so the HTTP surface isn't reachable from here — but the pipeline's
backing Firestore *is* queryable with the developer's gcloud ADC, so we read
live pipeline state directly and infer health from it.

Signals (all read-only, collection names from the monitored deployment):
  Agentic_state ............. per-client flow state. status=ESCALATION,
                              flow_status=PAUSED_*, node_action_status=BLOCKED
                              are the live problem signals. milestone_step
                              (A/B/C/D) maps to the four pipeline agents.
  csr_stream_runs ........... ingress runs — throughput + last-activity + stalls.
  orphaned_csrs ............. service requests that couldn't be assigned.
  cdc_*_dead_letter ......... CDC events that failed to resolve/create.

Everything here feeds two consumers:
  - the arch-map Flow overlay (per-step live badges), and
  - the pipeline notification center (get_problems()).

A short in-process cache keeps UI polling from hammering Firestore; the
scheduled sweep (sweeps/osca.py) persists snapshots + problem records.
"""

from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from datetime import datetime, timedelta, timezone

import httpx

from .config import policy

log = logging.getLogger("chief.osca")

_DEFAULTS = {
    "project": "your-gcp-project",
    "database": "your-firestore-db",
    "environment": "uat",
    "cache_seconds": 45,
    "max_state_docs": 500,
    "max_problem_docs": 250,
    # a stream run with no event for this long (and not in a terminal status) is stalled
    "stall_hours": 24,
    # dead-letter entries older than this are considered "known/archived" (still shown, lower sev)
    "dead_letter_fresh_days": 14,
    # Cloud Logging: which k8s namespaces to watch and how far back
    "log_namespaces": ["your-app-uat", "your-app-dev"],
    "log_hours": 24,
    # a probe/scheduling failure within this window means the check is failing NOW
    "check_fail_minutes": 60,
}

# milestone_step -> (flow step number it maps to, agent label)
_MILESTONE_STEP = {
    "A": (5, "Communications"),
    "B": (8, "Document Intelligence"),
    "C": (9, "DSF Assembly"),
    "D": (11, "Delivery"),
}

# current_stage -> representative flow step (fallback when milestone is absent)
_STAGE_STEP = {
    "INGRESS": 1, "INIT": 1, "INITIALIZATION": 1,
    "ENGAGE": 3, "COMMUNICATION": 5,
    "DATA_COLLECTION": 8,
    "PROCESSING": 10, "DSF_ASSEMBLY": 10,
    "DELIVERY": 11, "COMPLETE": 12, "COMPLETED": 12,
}

_TERMINAL_STREAM_STATUS = {"complete", "completed", "delivered", "done", "closed", "archived"}


def _cfg() -> dict:
    c = dict(_DEFAULTS)
    c.update(policy().get("osca", {}) or {})
    return c


# ---------------------------------------------------------------------------
# gcloud access token (cached ~50 min; tokens live 60 min)
# ---------------------------------------------------------------------------

_token: dict = {"value": None, "exp": 0.0}


def _get_token() -> str | None:
    now = time.time()
    if _token["value"] and now < _token["exp"]:
        return _token["value"]
    try:
        out = subprocess.run(
            ["gcloud", "auth", "print-access-token"],
            capture_output=True, text=True, timeout=25,
        )
        if out.returncode != 0:
            log.warning("gcloud token failed: %s", (out.stderr or "").strip()[:200])
            return None
        tok = out.stdout.strip()
        if not tok:
            return None
        _token["value"] = tok
        _token["exp"] = now + 50 * 60
        return tok
    except Exception as e:  # noqa: BLE001
        log.warning("gcloud token error: %s", e)
        return None


# ---------------------------------------------------------------------------
# Firestore REST helpers
# ---------------------------------------------------------------------------

def _val(v):
    """Decode a Firestore typed value into a plain Python value."""
    if not isinstance(v, dict):
        return v
    for k, x in v.items():
        if k == "integerValue":
            try:
                return int(x)
            except (TypeError, ValueError):
                return x
        if k == "doubleValue":
            return float(x)
        if k == "booleanValue":
            return bool(x)
        if k == "nullValue":
            return None
        if k == "mapValue":
            return {kk: _val(vv) for kk, vv in (x.get("fields", {}) or {}).items()}
        if k == "arrayValue":
            return [_val(e) for e in (x.get("values", []) or [])]
        return x  # stringValue, timestampValue, referenceValue, etc.
    return None


def _fields(doc: dict) -> dict:
    return {k: _val(v) for k, v in (doc.get("fields", {}) or {}).items()}


def _doc_id(doc: dict) -> str:
    return (doc.get("name") or "").split("/")[-1]


async def _list(client: httpx.AsyncClient, base: str, coll: str,
                cap: int, mask: list[str] | None = None) -> list[dict]:
    """List documents in a collection, following pagination up to `cap`."""
    docs: list[dict] = []
    token: str | None = None
    while len(docs) < cap:
        params: list[tuple[str, str]] = [("pageSize", str(min(300, cap - len(docs))))]
        if token:
            params.append(("pageToken", token))
        if mask:
            params += [("mask.fieldPaths", f) for f in mask]
        r = await client.get(f"{base}/{coll}", params=params)
        r.raise_for_status()
        data = r.json()
        docs.extend(data.get("documents", []) or [])
        token = data.get("nextPageToken")
        if not token:
            break
    return docs[:cap]


async def _run_query(client: httpx.AsyncClient, base: str, coll: str,
                     order_field: str, limit: int = 15) -> list[dict]:
    """Firestore runQuery: newest-first over one collection. Returns [] if the
    order field isn't indexed / present — activity is best-effort."""
    body = {"structuredQuery": {
        "from": [{"collectionId": coll}],
        "orderBy": [{"field": {"fieldPath": order_field}, "direction": "DESCENDING"}],
        "limit": limit,
    }}
    try:
        r = await client.post(f"{base}:runQuery", json=body)
        r.raise_for_status()
        return [row["document"] for row in r.json() if row.get("document")]
    except Exception as e:  # noqa: BLE001
        log.debug("runQuery %s failed: %s", coll, e)
        return []


def _workload(pod: str) -> str:
    """Deployment name from a pod name: strip the replicaset hash + pod suffix.
    integration-service-cdc-98c45d887-hnggl -> integration-service-cdc.
    Some event payloads merge the two suffixes (…-6857d9d6cdjtvwp), so also
    strip a single 13-16 char trailing blob."""
    w = re.sub(r"-[a-z0-9]{5}$", "", pod or "")
    w = re.sub(r"-[0-9a-f]{7,10}$", "", w)
    w = re.sub(r"-[a-z0-9]{13,16}$", "", w)
    return w or pod or "unknown"


def _ns_filter(cfg: dict) -> str:
    """Namespace clause for Cloud Logging filters. Prefers substring matching
    (log_namespace_match, e.g. "your-app") so newly-deployed environments —
    your-app-prod, your-app-qa — are tracked automatically; falls back to
    the explicit log_namespaces list."""
    match = cfg.get("log_namespace_match")
    if match:
        return f'resource.labels.namespace_name:"{match}"'
    ns = " OR ".join(f'resource.labels.namespace_name="{n}"' for n in cfg["log_namespaces"])
    return f"({ns})"


async def _fetch_log_entries(client: httpx.AsyncClient, cfg: dict) -> tuple[list[dict], int]:
    """WARNING+ log entries for the watched namespaces (Cloud Logging REST),
    paginated up to 1000 so a busy day isn't silently under-counted.
    Returns (entries, latency_ms); raises nothing — logging is a best-effort signal."""
    since = (_now() - timedelta(hours=cfg["log_hours"])).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = {
        "resourceNames": [f"projects/{cfg['project']}"],
        "filter": f'timestamp>="{since}" AND severity>=WARNING AND {_ns_filter(cfg)}',
        "orderBy": "timestamp desc",
        "pageSize": 250,
    }
    t0 = time.monotonic()
    entries: list[dict] = []
    try:
        for _page in range(4):  # up to 1000 entries
            r = await client.post("https://logging.googleapis.com/v2/entries:list", json=body)
            r.raise_for_status()
            data = r.json()
            entries.extend(data.get("entries", []) or [])
            token = data.get("nextPageToken")
            if not token:
                break
            body["pageToken"] = token
        return entries, int((time.monotonic() - t0) * 1000)
    except Exception as e:  # noqa: BLE001
        log.warning("cloud logging fetch failed: %s", e)
        return entries, -1


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_ts(s) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _age_hours(ts) -> float | None:
    dt = _parse_ts(ts)
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (_now() - dt).total_seconds() / 3600.0


def _norm(reason: str) -> str:
    """Stable signature for a free-text reason so like failures group together.
    Strips specifics (ids, digits, quoted values) and truncates."""
    r = (reason or "").lower()
    r = re.sub(r"[a-z0-9]{8,}", "", r)      # drop long ids/hashes
    r = re.sub(r"\d+", "", r)                # drop numbers
    r = re.sub(r"['\"`].*?['\"`]", "", r)    # drop quoted values
    r = re.sub(r"\s+", " ", r).strip()
    return r[:60] or "unspecified"


# ---------------------------------------------------------------------------
# Fetch + compute (shared by health + problems, short-cached)
# ---------------------------------------------------------------------------

_cache: dict = {"data": None, "exp": 0.0}


async def _fetch_env(client: httpx.AsyncClient, cfg: dict, env: dict) -> dict:
    """All monitored collections for one environment (one Firestore database)."""
    base = (f"https://firestore.googleapis.com/v1/projects/{cfg['project']}"
            f"/databases/{env['database']}/documents")
    (state, orphaned, dl_res, dl_create, runs, cadence,
     recent_runs, recent_emails, recent_actions) = await asyncio.gather(
        _list(client, base, "Agentic_state", cfg["max_state_docs"]),
        _list(client, base, "orphaned_csrs", cfg["max_problem_docs"]),
        _list(client, base, "cdc_resolution_dead_letter", cfg["max_problem_docs"]),
        _list(client, base, "cdc_create_dead_letter", cfg["max_problem_docs"]),
        _list(client, base, "csr_stream_runs", cfg["max_problem_docs"]),
        _list(client, base, "scheduler_cadence_jobs", cfg["max_problem_docs"]),
        # activity feed sources (newest first, best-effort)
        _run_query(client, base, "csr_stream_runs", "last_event_at", 15),
        _run_query(client, base, "inbound_email", "contact_resolution_bound_at", 15),
        _run_query(client, base, "idempotency_log", "recorded_at", 15),
    )
    return {
        "name": env["name"], "database": env["database"],
        "state": state, "orphaned": orphaned, "dead_letter": dl_res + dl_create,
        "runs": runs, "cadence": cadence, "recent_runs": recent_runs,
        "recent_emails": recent_emails, "recent_actions": recent_actions,
    }


def _env_list(cfg: dict) -> list[dict]:
    """Environments to monitor. New style: osca.environments list; falls back
    to the single database/environment keys."""
    envs = cfg.get("environments")
    if envs:
        return [{"name": e.get("name", "?"), "database": e["database"]} for e in envs]
    return [{"name": cfg["environment"], "database": cfg["database"]}]


async def _fetch_raw() -> dict:
    """Fetch all monitored collections across every environment. Returns an
    'error' key on auth/transport failure so callers can surface a clear reason."""
    cfg = _cfg()
    tok = await asyncio.to_thread(_get_token)
    if not tok:
        return {"error": "gcloud auth unavailable — run `gcloud auth login` "
                         "(or `gcloud auth application-default login`)"}
    headers = {"Authorization": f"Bearer {tok}"}
    envs_cfg = _env_list(cfg)
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(headers=headers, timeout=30) as client:
            results = await asyncio.gather(
                *[_fetch_env(client, cfg, e) for e in envs_cfg],
                _fetch_log_entries(client, cfg),  # spans all namespaces at once
            )
    except httpx.HTTPStatusError as e:
        return {"error": f"Firestore {e.response.status_code}: {e.response.text[:150]}"}
    except Exception as e:  # noqa: BLE001
        return {"error": f"Firestore query failed: {str(e)[:150]}"}
    log_entries, log_ms = results[-1]
    return {
        "envs": list(results[:-1]),
        "log_entries": log_entries, "log_ms": log_ms,
        "fetch_ms": int((time.monotonic() - t0) * 1000),
        "environment": "+".join(e["name"] for e in envs_cfg),
        "project": cfg["project"],
        "database": ", ".join(e["database"] for e in envs_cfg),
    }


async def _compute() -> dict:
    """Fetch + aggregate into a full monitor payload. Short-cached."""
    now = time.time()
    if _cache["data"] is not None and now < _cache["exp"]:
        return _cache["data"]

    cfg = _cfg()
    raw = await _fetch_raw()
    polled_at = _now().isoformat()
    if "error" in raw:
        payload = {
            "ok": False, "error": raw["error"], "status": "unknown",
            "polled_at": polled_at, "environment": cfg["environment"],
            "totals": {}, "stages": [], "milestones": {}, "flow": {},
            "throughput": {}, "problems": [], "groups": [],
            "checks": [], "activity": [], "upcoming": [], "log_summary": {},
        }
        _cache["data"] = payload
        _cache["exp"] = now + min(15, cfg["cache_seconds"])  # retry sooner on error
        return payload

    envs = raw["envs"]
    total_state = sum(len(e["state"]) for e in envs)
    total_orphaned = sum(len(e["orphaned"]) for e in envs)
    total_dead = sum(len(e["dead_letter"]) for e in envs)
    total_runs = sum(len(e["runs"]) for e in envs)
    by_env: dict[str, dict] = {
        e["name"]: {"active_clients": len(e["state"]), "escalations": 0,
                    "orphaned": len(e["orphaned"]), "dead_letter": len(e["dead_letter"]),
                    "stalled": 0, "checks_failing": 0, "last_activity_at": None}
        for e in envs
    }

    problems: list[dict] = []          # flat, capped — for drill-down
    groups: dict[str, dict] = {}        # aggregated by signature — the notification feed
    stage_agg: dict[str, dict] = {}
    milestone_agg: dict[str, dict] = {}
    flow: dict[int, dict] = {}

    def _flow_bump(step: int, key: str):
        if not step:
            return
        f = flow.setdefault(step, {"count": 0, "escalations": 0, "blocked": 0, "paused": 0})
        f[key] = f.get(key, 0) + 1

    sev_rank = {"high": 0, "medium": 1, "low": 2}

    def _emit(group_key: str, group_title: str, kind: str, severity: str, *,
              entity: str, detail: str, reason: str, stage: str,
              source_ts=None, recent: bool = True, step: int | None = None):
        """Record one problem into its group (and the capped flat list)."""
        g = groups.get(group_key)
        if g is None:
            g = groups[group_key] = {
                "key": group_key, "kind": kind, "severity": severity,
                "title": group_title, "count": 0, "recent_count": 0,
                "reason": reason, "stage": stage, "step": step,
                "examples": [], "latest_ts": None,
            }
        g["count"] += 1
        if recent:
            g["recent_count"] += 1
        if sev_rank.get(severity, 3) < sev_rank.get(g["severity"], 3):
            g["severity"] = severity
        if len(g["examples"]) < 8:
            g["examples"].append({"entity": str(entity), "detail": detail, "ts": source_ts})
        if source_ts and (g["latest_ts"] is None or str(source_ts) > str(g["latest_ts"])):
            g["latest_ts"] = source_ts
        if len(problems) < 250:
            problems.append({
                "key": f"{group_key}#{entity}", "group": group_key,
                "kind": kind, "severity": severity, "title": f"{entity} — {group_title}",
                "detail": detail, "entity": str(entity), "stage": stage,
                "reason": reason, "source_ts": source_ts,
            })

    escalations = paused = blocked = 0

    for _env in envs:
      env_name = _env["name"]
      for doc in _env["state"]:
        f = _fields(doc)
        cid = f.get("Client_ID") or _doc_id(doc)
        status = (f.get("status") or "").upper()
        flow_status = (f.get("flow_status") or "").upper()
        stage = (f.get("current_stage") or "UNKNOWN").upper()
        milestone = (f.get("milestone_step") or "").upper()
        reason = f.get("reason") or ""
        worker = f.get("worker_id") or ""
        node_action = f.get("node_action_status") or {}

        # which flow step does this client sit at?
        step = _MILESTONE_STEP.get(milestone, (None, None))[0] or _STAGE_STEP.get(stage)

        sa = stage_agg.setdefault(stage, {"stage": stage, "count": 0, "escalations": 0,
                                          "paused": 0, "blocked": 0})
        sa["count"] += 1
        if milestone:
            ma = milestone_agg.setdefault(milestone, {
                "milestone": milestone,
                "agent": _MILESTONE_STEP.get(milestone, (None, milestone))[1],
                "count": 0, "escalations": 0, "blocked": 0})
            ma["count"] += 1
        _flow_bump(step, "count")

        is_blocked = any(str(v).upper() == "BLOCKED" for v in node_action.values()) if isinstance(node_action, dict) else False
        is_escalation = status == "ESCALATION"
        is_paused = flow_status.startswith("PAUSED")

        if is_escalation:
            escalations += 1
            by_env[env_name]["escalations"] += 1
            sa["escalations"] += 1
            if milestone:
                milestone_agg[milestone]["escalations"] += 1
            _flow_bump(step, "escalations")
        if is_paused:
            paused += 1
            sa["paused"] += 1
            _flow_bump(step, "paused")
        if is_blocked:
            blocked += 1
            sa["blocked"] += 1
            if milestone:
                milestone_agg[milestone]["blocked"] += 1
            _flow_bump(step, "blocked")

        if is_escalation or is_blocked or is_paused:
            kind = "escalation" if is_escalation else ("blocked" if is_blocked else "paused")
            sev = "high" if (is_escalation or is_blocked) else "medium"
            agent = _MILESTONE_STEP.get(milestone, (None, stage))[1] or stage
            _emit(
                group_key=f"{kind}:{env_name}:{milestone or stage}:{_norm(reason)}",
                group_title=f"{kind.capitalize()} at {agent}"
                            + (f" — {reason}" if reason else "") + f" [{env_name}]",
                kind=kind, severity=sev,
                entity=cid, detail=reason or f"{status} / {flow_status}".strip(" /"),
                reason=reason, stage=milestone or stage, step=step)

    # orphaned service requests (orphaned_csrs)
    orphaned_recent = 0
    fresh_days = cfg["dead_letter_fresh_days"]
    for _env in envs:
      env_name = _env["name"]
      for doc in _env["orphaned"]:
        f = _fields(doc)
        cid = f.get("firestore_csr_id") or _doc_id(doc)
        reason = f.get("reason") or ""
        ts = f.get("orphaned_at")
        age = _age_hours(ts)
        recent = age is not None and age <= fresh_days * 24
        if recent:
            orphaned_recent += 1
        _emit(
            group_key=f"orphaned:{env_name}:{_norm(reason)}",
            group_title=(f"Orphaned CSRs — {reason}" if reason else "Orphaned CSRs")
                        + f" [{env_name}]",
            kind="orphaned", severity="medium" if recent else "low",
            entity=cid, detail=reason or (f.get("account_name") or ""),
            reason=reason, stage="INGRESS", source_ts=ts, recent=recent, step=1)

    # dead-letter CDC events
    dead_recent = 0
    for _env in envs:
      env_name = _env["name"]
      for doc in _env["dead_letter"]:
        f = _fields(doc)
        rid = f.get("replay_id") or f.get("salesforce_record_id") or _doc_id(doc)
        phase = f.get("phase") or ""
        msg = f.get("error_message") or ""
        ts = f.get("updated_at") or f.get("created_at")
        age = _age_hours(ts)
        recent = age is not None and age <= fresh_days * 24
        if recent:
            dead_recent += 1
        _emit(
            group_key=f"deadletter:{env_name}:{phase or 'unresolved'}",
            group_title=f"CDC dead-letter — {phase or 'unresolved'} [{env_name}]",
            kind="dead_letter", severity="high" if recent else "medium",
            entity=rid,
            detail=msg or f"{f.get('entity_name','')} {f.get('salesforce_record_id','')}".strip(),
            reason=msg, stage="INGRESS", source_ts=ts, recent=recent, step=1)

    # csr_stream_runs — throughput + stalled detection
    stall_hours = cfg["stall_hours"]
    run_status = {}
    ingress_24h = 0
    stalled = 0
    for _env in envs:
      env_name = _env["name"]
      for doc in _env["runs"]:
        f = _fields(doc)
        st = (f.get("status") or "unknown").lower()
        run_status[st] = run_status.get(st, 0) + 1
        trig_age = _age_hours(f.get("triggered_at"))
        if trig_age is not None and trig_age <= 24:
            ingress_24h += 1
        last_age = _age_hours(f.get("last_event_at") or f.get("triggered_at"))
        if (st not in _TERMINAL_STREAM_STATUS and st != "orphaned"
                and last_age is not None and last_age >= stall_hours):
            stalled += 1
            by_env[env_name]["stalled"] += 1
            cid = f.get("firestore_csr_id") or _doc_id(doc)
            _emit(
                group_key=f"stalled:{env_name}:{st}",
                group_title=f"Stalled runs — stuck in '{st}' [{env_name}]",
                kind="stalled", severity="medium",
                entity=cid,
                detail=f"no event in {int(last_age)}h · {f.get('account_name','')}",
                reason=f.get("error_hint") or "", stage=st,
                source_ts=f.get("last_event_at"), step=_STAGE_STEP.get(st.upper()))

    # ---- Cadence scheduler: upcoming triggers + overdue events + SLA risks --
    upcoming: list[dict] = []
    cadence_fired: list[dict] = []   # recently-fired events → activity feed
    sla_risks = 0
    cadence_overdue = 0
    for _env in envs:
      env_name = _env["name"]
      for doc in _env.get("cadence", []):
        f = _fields(doc)
        job = f.get("job") or {}
        if not isinstance(job, dict):
            continue
        csr = job.get("csr_id") or f.get("csr_id") or _doc_id(doc)
        prop = job.get("property_name") or job.get("pmc_name") or ""
        jstatus = (job.get("status") or "").lower()
        # deadline already passed while still waiting on the customer → SLA risk
        dl_age = _age_hours(job.get("deadline_date"))
        if jstatus == "waiting" and dl_age is not None and dl_age > 0:
            sla_risks += 1
            _emit(
                group_key=f"sla:{env_name}",
                group_title=f"Deadline passed, still waiting on customer [{env_name}]",
                kind="sla_breach", severity="medium", entity=csr,
                detail=f"deadline {str(job.get('deadline_date'))[:10]} · {prop}",
                reason="deadline_date in the past while job status=waiting",
                stage="CADENCE", source_ts=job.get("deadline_date"))
        for ev in (job.get("events") or []):
            if not isinstance(ev, dict):
                continue
            ev_status = (ev.get("status") or "").lower()
            # fired recently → show in the live activity feed
            if ev_status in ("completed", "sent", "fired") and ev.get("completed_at"):
                fired_age = _age_hours(ev.get("completed_at"))
                if fired_age is not None and fired_age <= 72:
                    cadence_fired.append({
                        "ts": ev.get("completed_at"), "env": env_name, "kind": "cadence",
                        "entity": str(csr),
                        "message": f"{ev.get('label') or 'follow-up'} sent · {prop}"[:90],
                    })
                continue
            if ev_status != "scheduled" or not ev.get("scheduled_at"):
                continue
            sat = ev.get("scheduled_at")
            age_h = _age_hours(sat)  # positive = already due
            if age_h is not None and age_h > 6:
                cadence_overdue += 1
                _emit(
                    group_key=f"cadence_overdue:{env_name}",
                    group_title=f"Cadence events overdue — scheduler not firing [{env_name}]",
                    kind="cadence_overdue", severity="medium", entity=csr,
                    detail=f"{ev.get('label') or 'follow-up'} was due {int(age_h)}h ago · {prop}",
                    reason="scheduled cadence event never fired",
                    stage="CADENCE", source_ts=sat)
            elif age_h is not None and age_h > -96:  # due in the next 4 days
                upcoming.append({
                    "ts": sat, "env": env_name, "entity": str(csr),
                    "label": ev.get("label") or ev.get("type") or "follow-up",
                    "target": prop[:60],
                })
    upcoming.sort(key=lambda u: str(u["ts"]))
    upcoming = upcoming[:12]

    # ---- Cloud Logging: probe failures, scheduling pressure, app errors -----
    fail_min = cfg["check_fail_minutes"]
    log_warn = log_err = 0
    latest_log_ts = None
    seen_ns: set = set()
    # workload -> {"kind", "ns", "count", "last_ts", "msg"}
    wl_issues: dict[str, dict] = {}
    for e in raw.get("log_entries", []):
        rl = (e.get("resource") or {}).get("labels", {}) or {}
        jp = e.get("jsonPayload") or {}
        msg = str(e.get("textPayload") or jp.get("message")
                  or jp.get("reason") or "")[:250]
        sev = (e.get("severity") or "WARNING").upper()
        ts = e.get("timestamp")
        pod = rl.get("pod_name") or (jp.get("involvedObject") or {}).get("name") or ""
        ns = rl.get("namespace_name") or ""
        if ns:
            seen_ns.add(ns)
        wl = _workload(pod)
        if latest_log_ts is None:
            latest_log_ts = ts  # entries arrive newest-first

        low = msg.lower()
        if sev in ("ERROR", "CRITICAL", "ALERT", "EMERGENCY"):
            log_err += 1
            kind = "pod_error"
        else:
            log_warn += 1
            if "probe failed" in low:
                kind = "probe_failure"
            elif "nodes are available" in low or "insufficient" in low or "failedscheduling" in low:
                kind = "scheduling"
            else:
                kind = "log_warning"

        issue = wl_issues.setdefault(f"{kind}:{wl}", {
            "kind": kind, "workload": wl, "ns": ns, "count": 0,
            "last_ts": ts, "msg": msg})
        issue["count"] += 1
        if ts and str(ts) > str(issue["last_ts"] or ""):
            issue["last_ts"], issue["msg"] = ts, msg

    log_fail_now = 0     # a workload is failing checks right now
    log_flapping = 0
    for ik, issue in wl_issues.items():
        age_min = ((_age_hours(issue["last_ts"]) or 0) * 60)
        failing_now = age_min <= fail_min
        if issue["kind"] == "pod_error":
            sev = "high"
        elif issue["kind"] in ("probe_failure", "scheduling"):
            sev = "high" if failing_now else "medium"
        else:
            sev = "low"
        if failing_now and sev == "high":
            log_fail_now += 1
        elif issue["kind"] in ("probe_failure", "scheduling"):
            log_flapping += 1
        titles = {
            "probe_failure": "Health probes failing",
            "scheduling": "Pods unschedulable (capacity)",
            "pod_error": "Errors in logs",
            "log_warning": "Warnings in logs",
        }
        _emit(
            group_key=f"logs:{ik}",
            group_title=f"{titles[issue['kind']]} — {issue['workload']}",
            kind=issue["kind"], severity=sev,
            entity=issue["workload"],
            detail=f"[{issue['ns']}] ×{issue['count']} in {cfg['log_hours']}h · {issue['msg'][:140]}",
            reason=issue["msg"][:200], stage=issue["ns"],
            source_ts=issue["last_ts"], recent=failing_now)
        # the group represents issue['count'] log events, not one
        groups[f"logs:{ik}"]["count"] = issue["count"]
        groups[f"logs:{ik}"]["recent_count"] = issue["count"] if failing_now else 0

    log_summary = {
        "window_hours": cfg["log_hours"],
        "warnings": log_warn, "errors": log_err,
        "workloads_affected": len({v["workload"] for v in wl_issues.values()}),
        "latest_ts": latest_log_ts,
        "namespaces": cfg["log_namespaces"],
    }

    # ---- Live activity feed (what the services are doing right now) ---------
    activity: list[dict] = []
    for _env in envs:
      env_name = _env["name"]
      for doc in _env["recent_runs"]:
        f = _fields(doc)
        activity.append({
            "ts": f.get("last_event_at") or f.get("triggered_at"), "env": env_name,
            "kind": "run", "entity": f.get("firestore_csr_id") or _doc_id(doc),
            "message": f"{(f.get('status') or '?')} · {f.get('account_name') or ''}".strip(" ·"),
        })
      for doc in _env["recent_emails"]:
        f = _fields(doc)
        envlp = f.get("envelope") or {}
        n_att = len(f.get("attachments") or [])
        activity.append({
            "ts": f.get("contact_resolution_bound_at"), "env": env_name,
            "kind": "email", "entity": f.get("mapped_csr") or (envlp.get("from") or "")[:40],
            "message": (f.get("subject") or "(no subject)")[:90]
                       + (f" · {n_att} attachment{'s' if n_att != 1 else ''}" if n_att else ""),
        })
      for doc in _env["recent_actions"]:
        f = _fields(doc)
        res = f.get("result") or {}
        meta = f.get("metadata") or {}
        snap = res.get("snapshot_type") or meta.get("snapshot_type") or "processed"
        activity.append({
            "ts": f.get("recorded_at"), "env": env_name,
            "kind": "agent", "entity": f.get("csr_id") or meta.get("csr_id") or "",
            "message": str(snap)[:90],
        })
    activity.extend(cadence_fired)
    activity = [a for a in activity if a.get("ts")]
    activity.sort(key=lambda a: str(a["ts"]), reverse=True)
    # Per-env last activity, from the full (pre-dedup, pre-slice) feed — an
    # env with only a handful of events shouldn't lose its latest timestamp
    # to the top-30-overall cutoff below.
    for a in activity:
        env_name = a.get("env")
        if env_name in by_env and by_env[env_name]["last_activity_at"] is None:
            by_env[env_name]["last_activity_at"] = a["ts"]
    # Collapse bursts of near-identical events (e.g. the same email fanned out
    # to N recipients) — keep the newest of each (kind, message) signature.
    seen_sig: set = set()
    deduped: list[dict] = []
    for a in activity:
        sig = (a["kind"], a.get("env"), a["message"][:70].lower())
        if sig in seen_sig:
            continue
        seen_sig.add(sig)
        deduped.append(a)
    activity = deduped[:30]
    latest_activity_ts = activity[0]["ts"] if activity else None

    # Surface ingress-side failures (orphans / dead-letters) on flow step 1 so
    # the map flags them where they happen.
    fi = flow.setdefault(1, {"count": 0, "escalations": 0, "blocked": 0, "paused": 0})
    fi["ingress_issues"] = orphaned_recent + dead_recent

    # ---- Health checks (the "is everything up?" strip) -----------------------
    flow_age_h = _age_hours(latest_activity_ts)
    checks: list[dict] = [
        {"name": "Firestore", "status": "ok",
         "message": " · ".join(f"{e['name']}: {len(e['state'])} clients" for e in envs)
                    + f" · {raw.get('fetch_ms', 0)}ms round"},
        {"name": "Cloud Logging",
         "status": "ok" if raw.get("log_ms", -1) >= 0 else "fail",
         "message": (f"{log_warn + log_err} warn+ events / {cfg['log_hours']}h"
                     if raw.get("log_ms", -1) >= 0 else "query failed")},
        {"name": "Data flow",
         "status": "ok" if (flow_age_h is not None and flow_age_h < 24) else "warn",
         "message": (f"last event {int(flow_age_h)}h ago" if flow_age_h is not None
                     else "no recent events")},
    ]
    wl_problem_kinds = ("probe_failure", "scheduling", "pod_error")
    for issue in wl_issues.values():
        if issue["kind"] not in wl_problem_kinds:
            continue
        age_min = ((_age_hours(issue["last_ts"]) or 0) * 60)
        status = "fail" if age_min <= fail_min else "warn"
        checks.append({
            "name": issue["workload"],
            "status": status,
            "message": {
                "probe_failure": "readiness probe failing",
                "scheduling": "unschedulable — insufficient CPU",
                "pod_error": "errors in logs",
            }[issue["kind"]] + f" · last {int(age_min)}m ago · {issue['ns']}",
        })
        # Namespaces are named after their environment (your-app-uat,
        # your-app-dev, …) — attribute the failing check back to it.
        if status == "fail":
            for env_name in by_env:
                if env_name in issue["ns"]:
                    by_env[env_name]["checks_failing"] += 1
    if not any(i["kind"] in wl_problem_kinds for i in wl_issues.values()):
        checks.append({"name": "Workload probes", "status": "ok",
                       "message": f"no failing probes / scheduling issues in {cfg['log_hours']}h"})

    # Per-environment status — same red/amber/green logic as the overall
    # status below, scoped to what's actually happening in that one env.
    for env_stats in by_env.values():
        if env_stats["escalations"] > 0 or env_stats["checks_failing"] > 0:
            env_stats["status"] = "red"
        elif env_stats["stalled"] > 0 or env_stats["orphaned"] > 0 or env_stats["dead_letter"] > 0:
            env_stats["status"] = "amber"
        else:
            env_stats["status"] = "green"

    totals = {
        "active_clients": total_state,
        "escalations": escalations,
        "paused": paused,
        "blocked": blocked,
        "orphaned": total_orphaned,
        "orphaned_recent": orphaned_recent,
        "dead_letter": total_dead,
        "dead_letter_recent": dead_recent,
        "stalled": stalled,
        "log_warnings": log_warn,
        "log_errors": log_err,
        "sla_risks": sla_risks,
        "cadence_overdue": cadence_overdue,
        "checks_failing": sum(1 for c in checks if c["status"] == "fail"),
        "by_env": by_env,
    }

    # Overall status (pipeline state + live infra checks)
    if escalations > 0 or blocked > 0 or dead_recent > 0 or log_fail_now > 0 or log_err > 0:
        status = "red"
    elif paused > 0 or orphaned_recent > 0 or stalled > 0 or log_flapping > 0:
        status = "amber"
    else:
        status = "green"

    # sort problems + groups: severity, then most-recent activity
    problems.sort(key=lambda p: (sev_rank.get(p["severity"], 3), p["kind"]))
    group_list = sorted(
        groups.values(),
        key=lambda g: (sev_rank.get(g["severity"], 3), -g["recent_count"], -g["count"]))

    payload = {
        "ok": True,
        "status": status,
        "polled_at": polled_at,
        "environment": raw["environment"],
        "project": raw["project"],
        "database": raw["database"],
        "totals": totals,
        "stages": sorted(stage_agg.values(), key=lambda s: -s["count"]),
        "milestones": milestone_agg,
        "flow": {str(k): v for k, v in flow.items()},
        "throughput": {"ingress_24h": ingress_24h, "run_status": run_status,
                       "sampled_runs": total_runs},
        "checks": checks,
        "activity": activity,
        "upcoming": upcoming,
        "log_summary": log_summary,
        "groups": group_list,
        "problems": problems,
    }
    _cache["data"] = payload
    _cache["exp"] = now + cfg["cache_seconds"]
    return payload


def _invalidate_cache():
    _cache["data"] = None
    _cache["exp"] = 0.0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_warming = False


def _persisted_snapshot() -> dict | None:
    """Last sweep snapshot from SQLite, reshaped like a live payload. Served on
    cold start (marked stale:true) so the UI paints instantly instead of
    waiting ~7s for the full Firestore + Logging round."""
    import json as _json

    from . import db
    rows = db.query("SELECT * FROM osca_snapshots ORDER BY id DESC LIMIT 1")
    if not rows:
        return None
    r = rows[0]
    try:
        detail = _json.loads(r["detail"] or "{}")
    except (ValueError, TypeError):
        detail = {}
    return {
        "ok": True, "stale": True, "status": r["status"] or "unknown",
        "polled_at": r["ts"], "environment": r["environment"],
        "totals": {"active_clients": r["active_clients"], "escalations": r["escalations"],
                   "paused": r["paused"], "blocked": r["blocked"],
                   "orphaned": r["orphaned"], "dead_letter": r["dead_letter"]},
        "stages": detail.get("stages") or [],
        "milestones": detail.get("milestones") or {},
        "flow": detail.get("flow") or {},
        "throughput": detail.get("throughput") or {},
        "checks": [], "activity": [], "upcoming": [], "log_summary": {},
    }


def _warm_in_background() -> None:
    """Kick one background _compute() so the next poll is live. Deduped."""
    global _warming
    if _warming:
        return
    _warming = True

    async def _warm():
        global _warming
        try:
            await _compute()
        except Exception:  # noqa: BLE001
            log.exception("osca background warm failed")
        finally:
            _warming = False

    asyncio.get_running_loop().create_task(_warm())


async def get_health(force: bool = False) -> dict:
    """Full live health payload (status, totals, stage/milestone/flow breakdown,
    throughput, checks, activity, logs). The heavy problems/groups lists are
    served separately. Cold start returns the persisted snapshot instantly
    (stale:true) while the live poll warms in the background."""
    if force:
        _invalidate_cache()
    elif _cache["data"] is None:
        snap = _persisted_snapshot()
        if snap is not None:
            _warm_in_background()
            return snap
    data = await _compute()
    drop = {"problems", "groups"}
    return {k: v for k, v in data.items() if k not in drop}


async def get_problems(force: bool = False) -> list[dict]:
    """Live grouped problem feed for the notification center."""
    if force:
        _invalidate_cache()
    data = await _compute()
    return data.get("groups", [])


async def get_problem_detail(group_key: str, force: bool = False) -> list[dict]:
    """Individual problem instances belonging to one group (drill-down)."""
    if force:
        _invalidate_cache()
    data = await _compute()
    return [p for p in data.get("problems", []) if p.get("group") == group_key]


async def get_full(force: bool = False) -> dict:
    """Everything (health + problems) in one shot — used by the sweep."""
    if force:
        _invalidate_cache()
    return await _compute()


async def get_workload_logs(workload: str, hours: int = 6, limit: int = 50) -> dict:
    """Raw recent log lines for one workload (all severities) — drill-down for
    the Mission Control alerts. `workload` is matched as a pod-name prefix."""
    cfg = _cfg()
    tok = await asyncio.to_thread(_get_token)
    if not tok:
        return {"entries": [], "error": "gcloud auth unavailable"}
    since = (_now() - timedelta(hours=max(1, min(hours, 72)))).strftime("%Y-%m-%dT%H:%M:%SZ")
    # match both container logs (pod_name label) and k8s events (involvedObject)
    wl = workload.replace('"', "")[:80]
    body = {
        "resourceNames": [f"projects/{cfg['project']}"],
        "filter": (f'timestamp>="{since}" AND {_ns_filter(cfg)} AND '
                   f'(resource.labels.pod_name:"{wl}" OR jsonPayload.involvedObject.name:"{wl}")'),
        "orderBy": "timestamp desc",
        "pageSize": max(1, min(limit, 200)),
    }
    try:
        async with httpx.AsyncClient(headers={"Authorization": f"Bearer {tok}"}, timeout=30) as client:
            r = await client.post("https://logging.googleapis.com/v2/entries:list", json=body)
            r.raise_for_status()
            raw_entries = r.json().get("entries", []) or []
    except Exception as e:  # noqa: BLE001
        return {"entries": [], "error": f"logging query failed: {str(e)[:150]}"}
    out = []
    for e in raw_entries:
        rl = (e.get("resource") or {}).get("labels", {}) or {}
        jp = e.get("jsonPayload") or {}
        msg = str(e.get("textPayload") or jp.get("message") or jp.get("reason")
                  or jp.get("MESSAGE") or "")[:400]
        out.append({
            "ts": e.get("timestamp"),
            "severity": (e.get("severity") or "DEFAULT").upper(),
            "pod": rl.get("pod_name") or (jp.get("involvedObject") or {}).get("name") or "",
            "ns": rl.get("namespace_name") or "",
            "message": msg,
        })
    return {"entries": out, "workload": workload, "hours": hours}
