"""Azure DevOps: the user's assigned work items and their states.

Reuses the PAT already configured for the azure-devops MCP server (in the
user-level Claude settings); queries the REST API directly so no MCP session
is needed. Cached briefly in the API layer.
"""

import base64
import html as _html
import logging
import re
import urllib.parse

import httpx

from . import certs, db
from .config import mcp_server_defs, policy

log = logging.getLogger("chief.ado")
_HTTP = httpx.Client(verify=certs.ca_bundle(), timeout=60)

ORG = policy().get("azure_devops", {}).get("org_url", "https://dev.azure.com/your-ado-org")
FIELDS = ["System.Id", "System.Title", "System.WorkItemType", "System.State",
          "System.IterationPath", "System.ChangedDate", "System.TeamProject",
          "System.AssignedTo", "System.Tags", "System.Reason"]

# Coarse state buckets so the board can group consistently across work-item types.
STATE_BUCKET = {
    "new": "To do", "approved": "To do", "to do": "To do", "proposed": "To do",
    "active": "In progress", "committed": "In progress", "in progress": "In progress",
    "doing": "In progress", "open": "In progress",
    "resolved": "In review", "in review": "In review", "code review": "In review",
    "done": "Done", "closed": "Done", "completed": "Done",
    "removed": "Removed",
}


def configured_projects() -> list[str]:
    return policy().get("knowledge", {}).get(
        "ado_projects", ["Your ADO Project"])


def disabled_projects() -> set[str]:
    import json
    return set(json.loads(db.get_setting("ado_projects_disabled", "[]")))


def _auth() -> str | None:
    pat = (mcp_server_defs().get("azure-devops", {}).get("env", {})
           .get("AZURE_DEVOPS_EXT_PAT"))
    return "Basic " + base64.b64encode(f":{pat}".encode()).decode() if pat else None


def _post(url: str, auth: str, body: dict) -> dict:
    r = _HTTP.post(url, json=body, headers={
        "Authorization": auth, "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def _get(url: str, auth: str) -> dict:
    r = _HTTP.get(url, headers={"Authorization": auth})
    r.raise_for_status()
    return r.json()


def _shape(wi: dict, project_fallback: str = "") -> dict:
    """One work item in the board's item shape (shared by my_items/items_by_ids)."""
    f = wi.get("fields", {})
    state = f.get("System.State", "")
    project = f.get("System.TeamProject", project_fallback)
    assigned = f.get("System.AssignedTo")
    return {
        "id": wi.get("id"),
        "assigned_to": assigned.get("displayName", "") if isinstance(assigned, dict)
                       else str(assigned or ""),
        "tags": [t.strip() for t in (f.get("System.Tags") or "").split(";") if t.strip()],
        "title": f.get("System.Title", ""),
        "type": f.get("System.WorkItemType", ""),
        "state": state,
        "bucket": STATE_BUCKET.get(state.lower(), state or "Other"),
        "project": project,
        "iteration": (f.get("System.IterationPath", "") or "").split("\\")[-1],
        "changed": str(f.get("System.ChangedDate", ""))[:10],
        # Reason is a standard ADO field the workflow sets alongside State -
        # most process templates (Agile/Scrum/CMMI) set it to "Reopened" when
        # an item moves back from Resolved/Closed to Active/New. Cheap regression
        # signal (same batch fetch, no extra API calls) for the scrum report's
        # "was this reopened" check (owner decision, 2026-07-23).
        "reason": f.get("System.Reason", ""),
        "url": f"{ORG}/{urllib.parse.quote(project)}/_workitems/edit/{wi.get('id')}",
    }


def my_items() -> dict:
    auth = _auth()
    if not auth:
        return {"error": "no Azure DevOps PAT configured"}
    disabled = disabled_projects()
    projects = [p for p in configured_projects() if p not in disabled]
    items = []
    for project in projects:
        wiql = {"query": (
            "SELECT [System.Id] FROM WorkItems WHERE [System.AssignedTo] = @Me "
            f"AND [System.TeamProject] = '{project}' "
            "AND [System.State] <> 'Removed' ORDER BY [System.ChangedDate] DESC")}
        try:
            res = _post(f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/wiql"
                        "?api-version=7.1&$top=200", auth, wiql)
        except Exception as e:
            log.warning("ado wiql failed for %s: %s", project, e)
            continue
        ids = [w["id"] for w in res.get("workItems", [])]
        for i in range(0, len(ids), 200):
            try:
                batch = _post(f"{ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                              {"ids": ids[i:i + 200], "fields": FIELDS})
            except Exception as e:
                log.warning("ado workitemsbatch failed for %s: %s", project, e)
                continue
            items.extend(_shape(wi, project) for wi in batch.get("value", []))
    order = ["In progress", "In review", "To do", "Done", "Other"]
    items.sort(key=lambda it: (order.index(it["bucket"]) if it["bucket"] in order else 99,
                               it["changed"]), reverse=False)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["bucket"]] = counts.get(it["bucket"], 0) + 1
    return {"items": items, "counts": counts, "total": len(items)}


# ---- swarm support: item detail bundles and evidence comments -----------------

_TAG_RE = re.compile(r"<[^>]+>")


def _strip_html(s: str) -> str:
    return _html.unescape(_TAG_RE.sub(" ", s or "")).strip()


def _display_name(identity) -> str:
    return identity.get("displayName", "") if isinstance(identity, dict) else str(identity or "")


def my_recent_changes(days: int, projects: list[str] | None = None) -> list[dict]:
    """Work items the user themselves changed (comment, edit, state move, ...) in
    the last `days` days - the ADO-side evidence for the daily scrum report.
    ChangedBy rather than AssignedTo: an item can be touched without being
    assigned to them, and a currently-assigned item they haven't touched recently
    isn't "yesterday's work"."""
    auth = _auth()
    if not auth:
        return []
    items: list[dict] = []
    for project in projects or configured_projects():
        wiql = {"query": (
            "SELECT [System.Id] FROM WorkItems WHERE "
            f"[System.TeamProject] = '{project}' "
            "AND [System.State] <> 'Removed' "
            f"AND [System.ChangedBy] = @Me "
            f"AND [System.ChangedDate] >= @Today - {int(days)} "
            "ORDER BY [System.ChangedDate] DESC")}
        try:
            res = _post(f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/wiql"
                        "?api-version=7.1&$top=200", auth, wiql)
        except Exception as e:
            log.warning("ado my_recent_changes wiql failed for %s: %s", project, e)
            continue
        ids = [w["id"] for w in res.get("workItems", [])]
        for i in range(0, len(ids), 200):
            try:
                batch = _post(f"{ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                              {"ids": ids[i:i + 200], "fields": FIELDS})
            except Exception as e:
                log.warning("ado workitemsbatch failed for %s: %s", project, e)
                continue
            items.extend(_shape(wi, project) for wi in batch.get("value", []))
    return items


def open_items_matching(title_contains: str, projects: list[str] | None = None) -> list[dict]:
    """Every still-open work item (any assignee, any last-changed date) whose
    title contains `title_contains` - the full remaining backlog for a given
    context (e.g. "TC-UAT" for the UAT test-case tickets), not just what the
    user touched recently. Feeds the scrum report's "what's left" section
    (owner decision, 2026-07-22)."""
    auth = _auth()
    if not auth:
        return []
    items: list[dict] = []
    for project in projects or configured_projects():
        wiql = {"query": (
            "SELECT [System.Id] FROM WorkItems WHERE "
            f"[System.TeamProject] = '{project}' "
            "AND [System.State] NOT IN ('Closed','Done','Resolved','Removed') "
            f"AND [System.Title] CONTAINS '{title_contains}' "
            "ORDER BY [System.ChangedDate] DESC")}
        try:
            res = _post(f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/wiql"
                        "?api-version=7.1&$top=200", auth, wiql)
        except Exception as e:
            log.warning("ado open_items_matching wiql failed for %s: %s", project, e)
            continue
        ids = [w["id"] for w in res.get("workItems", [])]
        for i in range(0, len(ids), 200):
            try:
                batch = _post(f"{ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                              {"ids": ids[i:i + 200], "fields": FIELDS})
            except Exception as e:
                log.warning("ado workitemsbatch failed for %s: %s", project, e)
                continue
            items.extend(_shape(wi, project) for wi in batch.get("value", []))
    return items


def project_items(projects: list[str] | None = None, days: int = 30) -> list[dict]:
    """Recently-changed items across whole projects (everyone's, not just @Me).
    Feeds the swarm's owner/type/state filter dropdowns and filtered runs."""
    auth = _auth()
    if not auth:
        raise RuntimeError("no Azure DevOps PAT configured")
    items: list[dict] = []
    for project in projects or configured_projects():
        wiql = {"query": (
            "SELECT [System.Id] FROM WorkItems WHERE "
            f"[System.TeamProject] = '{project}' "
            "AND [System.State] <> 'Removed' "
            f"AND [System.ChangedDate] >= @Today - {int(days)} "
            "ORDER BY [System.ChangedDate] DESC")}
        try:
            res = _post(f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/wiql"
                        "?api-version=7.1&$top=600", auth, wiql)
        except Exception as e:
            log.warning("ado project wiql failed for %s: %s", project, e)
            continue
        ids = [w["id"] for w in res.get("workItems", [])]
        for i in range(0, len(ids), 200):
            try:
                batch = _post(f"{ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                              {"ids": ids[i:i + 200], "fields": FIELDS})
            except Exception as e:
                log.warning("ado workitemsbatch failed for %s: %s", project, e)
                continue
            items.extend(_shape(wi, project) for wi in batch.get("value", []))
    return items


def items_by_ids(ids: list[int]) -> list[dict]:
    """Explicitly-targeted work items, shaped like my_items()['items']."""
    auth = _auth()
    if not auth:
        raise RuntimeError("no Azure DevOps PAT configured")
    items = []
    for i in range(0, len(ids), 200):
        batch = _post(f"{ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                      {"ids": ids[i:i + 200], "fields": FIELDS})
        items.extend(_shape(wi) for wi in batch.get("value", []))
    return items


def item_details(item_id: int) -> dict:
    """Everything a swarm worker may use as evidence for one work item:
    full fields, recent comments, and PR/branch/commit relations. Fetched here
    so workers stay tool-free (the bundle is embedded in their prompt)."""
    auth = _auth()
    if not auth:
        raise RuntimeError("no Azure DevOps PAT configured")
    wi = _get(f"{ORG}/_apis/wit/workitems/{item_id}?$expand=relations&api-version=7.1", auth)
    f = wi.get("fields", {})
    project = f.get("System.TeamProject", "")
    relations = [{
        "kind": (rel.get("attributes") or {}).get("name") or rel.get("rel", ""),
        "url": urllib.parse.unquote(rel.get("url", ""))[:300],
    } for rel in (wi.get("relations") or [])[:25]]
    comments = get_comments(project, item_id)
    return {
        "id": item_id,
        "project": project,
        "type": f.get("System.WorkItemType", ""),
        "state": f.get("System.State", ""),
        "title": f.get("System.Title", ""),
        "assigned_to": _display_name(f.get("System.AssignedTo")),
        "iteration": f.get("System.IterationPath", ""),
        "tags": f.get("System.Tags", ""),
        "created": str(f.get("System.CreatedDate", ""))[:10],
        "changed": str(f.get("System.ChangedDate", ""))[:16],
        "description": _strip_html(f.get("System.Description", ""))[:4000],
        "acceptance_criteria": _strip_html(
            f.get("Microsoft.VSTS.Common.AcceptanceCriteria", ""))[:2000],
        "repro_steps": _strip_html(f.get("Microsoft.VSTS.TCM.ReproSteps", ""))[:2000],
        "relations": relations,
        "comments": comments,
        "url": f"{ORG}/{urllib.parse.quote(project)}/_workitems/edit/{item_id}",
    }


def get_comments(project: str, item_id: int, top: int = 15) -> list[dict]:
    """Recent comments on a work item, newest first, tags stripped."""
    auth = _auth()
    if not auth:
        raise RuntimeError("no Azure DevOps PAT configured")
    try:
        res = _get(f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/workItems/{item_id}"
                   f"/comments?$top={top}&order=desc&api-version=7.1-preview.3", auth)
        return [{
            "by": _display_name(c.get("createdBy")),
            "by_id": (c.get("createdBy") or {}).get("id", ""),  # for @mentions
            "at": str(c.get("createdDate", ""))[:16],
            "text": _strip_html(c.get("text", ""))[:800],
        } for c in res.get("comments", [])]
    except Exception as e:
        log.warning("ado comments fetch failed for %s: %s", item_id, e)
        return []


def post_comment(project: str, item_id: int, text: str) -> dict:
    """Comment on a work item. Outbound: callers must go through
    actions.ado_comment_post, where the kill switch and dry run are enforced."""
    auth = _auth()
    if not auth:
        raise RuntimeError("no Azure DevOps PAT configured")
    return _post(f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/workItems/{item_id}"
                 "/comments?api-version=7.1-preview.3", auth, {"text": text})


def set_iteration(project: str, item_id: int, iteration_path: str) -> dict:
    """Move a work item to another iteration (board-hygiene fix: deferred work
    parked back to the backlog). Outbound: callers must go through
    actions.ado_iteration_update, where the kill switch and dry run are enforced."""
    auth = _auth()
    if not auth:
        raise RuntimeError("no Azure DevOps PAT configured")
    r = _HTTP.patch(
        f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/workitems/{item_id}"
        "?api-version=7.1",
        json=[{"op": "add", "path": "/fields/System.IterationPath",
               "value": iteration_path}],
        headers={"Authorization": auth,
                 "Content-Type": "application/json-patch+json"})
    r.raise_for_status()
    return r.json()


# Values for required-on-transition custom fields the process may demand
# (e.g. Bug -> Resolved can require Fix Type + Resolutions in a custom template).
_TRANSITION_FILLS = {"Custom.FixType": "Code Fix"}


def set_state(project: str, item_id: int, state: str,
              resolution_note: str = "") -> dict:
    """Move a work item to a new state. Custom process templates can require
    extra fields for a transition; when the first PATCH 400s with required-
    field rule errors, retry once filling the fields we know how to answer
    (fix type, resolution text). Outbound: callers must go through
    actions.ado_state_update, where the kill switch and dry run are enforced."""
    auth = _auth()
    if not auth:
        raise RuntimeError("no Azure DevOps PAT configured")
    url = (f"{ORG}/{urllib.parse.quote(project)}/_apis/wit/workitems/{item_id}"
           "?api-version=7.1")

    def _patch(ops: list) -> httpx.Response:
        return _HTTP.patch(url, json=ops, headers={
            "Authorization": auth,
            "Content-Type": "application/json-patch+json"})

    ops = [{"op": "add", "path": "/fields/System.State", "value": state}]
    r = _patch(ops)
    if r.status_code == 400:
        missing = set(re.findall(r'"fieldReferenceName":"([^"]+)"', r.text or ""))
        fills = {}
        for f in missing:
            if f in _TRANSITION_FILLS:
                fills[f] = _TRANSITION_FILLS[f]
            elif "resolution" in f.lower():
                fills[f] = resolution_note or f"Moved to {state} by the swarm"
        if fills:
            log.info("ado set_state %s -> %s: filling required fields %s",
                     item_id, state, sorted(fills))
            r = _patch(ops + [{"op": "add", "path": f"/fields/{k}", "value": v}
                              for k, v in fills.items()])
    r.raise_for_status()
    return r.json()
