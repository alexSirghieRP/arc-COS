"""Azure DevOps: the user's assigned work items and their states.

Reuses the PAT already configured for the azure-devops MCP server (in the
user-level Claude settings); queries the REST API directly so no MCP session
is needed. Cached briefly in the API layer.
"""

import base64
import logging
import urllib.parse

import httpx

from . import certs
from .config import mcp_server_defs, policy

log = logging.getLogger("chief.ado")
_HTTP = httpx.Client(verify=certs.ca_bundle(), timeout=60)

ORG = policy().get("azure_devops", {}).get("org_url", "https://dev.azure.com/your-ado-org")
FIELDS = ["System.Id", "System.Title", "System.WorkItemType", "System.State",
          "System.IterationPath", "System.ChangedDate", "System.TeamProject"]

# Coarse state buckets so the board can group consistently across work-item types.
STATE_BUCKET = {
    "new": "To do", "approved": "To do", "to do": "To do", "proposed": "To do",
    "active": "In progress", "committed": "In progress", "in progress": "In progress",
    "doing": "In progress", "open": "In progress",
    "resolved": "In review", "in review": "In review", "code review": "In review",
    "done": "Done", "closed": "Done", "completed": "Done",
    "removed": "Removed",
}


def _auth() -> str | None:
    pat = (mcp_server_defs().get("azure-devops", {}).get("env", {})
           .get("AZURE_DEVOPS_EXT_PAT"))
    return "Basic " + base64.b64encode(f":{pat}".encode()).decode() if pat else None


def _post(url: str, auth: str, body: dict) -> dict:
    r = _HTTP.post(url, json=body, headers={
        "Authorization": auth, "Content-Type": "application/json"})
    r.raise_for_status()
    return r.json()


def my_items() -> dict:
    auth = _auth()
    if not auth:
        return {"error": "no Azure DevOps PAT configured"}
    projects = policy().get("knowledge", {}).get(
        "ado_projects", ["Your ADO Project"])
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
            batch = _post(f"{ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                          {"ids": ids[i:i + 200], "fields": FIELDS})
            for wi in batch.get("value", []):
                f = wi.get("fields", {})
                state = f.get("System.State", "")
                items.append({
                    "id": wi.get("id"),
                    "title": f.get("System.Title", ""),
                    "type": f.get("System.WorkItemType", ""),
                    "state": state,
                    "bucket": STATE_BUCKET.get(state.lower(), state or "Other"),
                    "project": f.get("System.TeamProject", project),
                    "iteration": (f.get("System.IterationPath", "") or "").split("\\")[-1],
                    "changed": str(f.get("System.ChangedDate", ""))[:10],
                    "url": f"{ORG}/{urllib.parse.quote(f.get('System.TeamProject', project))}"
                           f"/_workitems/edit/{wi.get('id')}",
                })
    order = ["In progress", "In review", "To do", "Done", "Other"]
    items.sort(key=lambda it: (order.index(it["bucket"]) if it["bucket"] in order else 99,
                               it["changed"]), reverse=False)
    counts: dict[str, int] = {}
    for it in items:
        counts[it["bucket"]] = counts.get(it["bucket"], 0) + 1
    return {"items": items, "counts": counts, "total": len(items)}
