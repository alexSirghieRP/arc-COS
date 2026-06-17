"""Knowledge sync: keeps Chieff/Knowledge/AI Platform current.

Whenever someone edits or adds something in the watched sources, the changed
bits are re-pulled and the matching vault files rewritten:
- Confluence: pages in the configured spaces (CQL lastModified watermark)
- Azure DevOps: work items in the configured projects (ChangedDate watermark;
  any change triggers a full regen of that project's file)

Auth reuses the credentials already configured for the MCP servers in the
user-level Claude settings; REST is called directly so no extra MCP session
is needed. Every sync leaves a trace in Chieff's Log + domain folders.
"""

import asyncio
import base64
import json
import logging
import re
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db, vault
from ..config import mcp_server_defs, policy, vault_root

log = logging.getLogger("chief.sweep.knowledge")

CONF_WM = "knowledge_confluence_watermark"
ADO_WM = "knowledge_ado_watermark"


def _cfg() -> dict:
    return policy().get("knowledge", {})


def _kdir() -> Path:
    return vault_root() / _cfg().get("dir", "Chieff/Knowledge/AI Platform")


def _get(url: str, auth: str, body: dict | None = None) -> dict:
    req = urllib.request.Request(url, headers={
        "Authorization": auth, "Content-Type": "application/json"})
    if body is not None:
        req.data = json.dumps(body).encode()
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def _safe_name(title: str) -> str:
    return re.sub(r'[/\\:|#^\[\]]', "-", title).strip()


# ---- Confluence -------------------------------------------------------------

def _html_to_md(html: str) -> str:
    t = html or ""
    for i in range(6, 0, -1):
        t = re.sub(rf"<h{i}[^>]*>(.*?)</h{i}>", lambda m, i=i: "\n" + "#" * i + " " + m.group(1) + "\n", t, flags=re.S)
    t = re.sub(r"<a [^>]*href=\"([^\"]+)\"[^>]*>(.*?)</a>", r"[\2](\1)", t, flags=re.S)
    t = re.sub(r"<li[^>]*>", "\n- ", t)
    t = re.sub(r"<(strong|b)>(.*?)</\1>", r"**\2**", t, flags=re.S)
    t = re.sub(r"<(em|i)>(.*?)</\1>", r"*\2*", t, flags=re.S)
    t = re.sub(r"<code[^>]*>(.*?)</code>", r"`\1`", t, flags=re.S)
    t = re.sub(r"<br ?/?>|</p>|</div>|</tr>", "\n", t)
    t = re.sub(r"</td><td[^>]*>", " | ", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = t.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _confluence_sync() -> list[str]:
    defs = mcp_server_defs().get("atlassian", {}).get("env", {})
    base = (defs.get("ATLASSIAN_URL") or "").rstrip("/")
    email, token = defs.get("ATLASSIAN_EMAIL"), defs.get("ATLASSIAN_API_TOKEN")
    if not (base and email and token):
        return []
    auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
    spaces = _cfg().get("confluence_spaces", ["AP"])
    wm = db.get_setting(CONF_WM)
    since = (datetime.fromisoformat(wm) if wm
             else datetime.now(timezone.utc)) - timedelta(minutes=90)  # overlap window
    cql = (f"type=page and lastModified >= \"{since.strftime('%Y/%m/%d %H:%M')}\" and ("
           + " or ".join(f'space="{s}"' for s in spaces) + ")")
    url = (f"{base}/wiki/rest/api/content/search?cql={urllib.parse.quote(cql)}"
           f"&expand=body.storage,version,space,history.lastUpdated&limit=50")
    res = _get(url, auth)
    changed = []
    out = _kdir() / "Confluence"
    out.mkdir(parents=True, exist_ok=True)
    for page in res.get("results", []):
        title = page.get("title", "untitled")
        space = (page.get("space") or {}).get("key", "?")
        ver = (page.get("version") or {}).get("number")
        who = ((page.get("version") or {}).get("by") or {}).get("displayName", "?")
        body = _html_to_md(((page.get("body") or {}).get("storage") or {}).get("value", ""))
        if title.lower().startswith("template -") or not body:
            continue
        link = f"{base}/wiki/spaces/{space}/pages/{page.get('id')}"
        md = (f"---\nsource: {link}\nspace: {space}\nconfluence_id: {page.get('id')}\n"
              f"version: {ver}\nlast_edited_by: {who}\nsynced: "
              f"{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')} by Chieff (auto-sync)\n---\n\n"
              f"# {title}\n\n{body}\n")
        (out / f"{space} - {_safe_name(title)}.md").write_text(md)
        changed.append(f"{space}/{title} v{ver} (by {who})")
    db.set_setting(CONF_WM, datetime.now(timezone.utc).isoformat())
    return changed


# ---- Azure DevOps -----------------------------------------------------------

ADO_ORG = policy().get("azure_devops", {}).get("org_url", "https://dev.azure.com/your-ado-org")
ADO_FIELDS = ["System.Id", "System.Title", "System.WorkItemType", "System.State",
              "System.AssignedTo", "System.IterationPath", "System.Tags",
              "System.ChangedDate", "System.Description"]


def _ado_auth() -> str | None:
    pat = (mcp_server_defs().get("azure-devops", {}).get("env", {})
           .get("AZURE_DEVOPS_EXT_PAT"))
    return "Basic " + base64.b64encode(f":{pat}".encode()).decode() if pat else None


def _strip_html(h: str) -> str:
    t = re.sub(r"<br ?/?>|</p>|</div>|</li>", "\n", h or "")
    t = re.sub(r"<[^>]+>", "", t)
    return re.sub(r"\n{3,}", "\n\n", t.replace("&nbsp;", " ")).strip()


def _ado_regen(project: str, auth: str) -> int:
    q = {"query": f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '{project}' ORDER BY [System.ChangedDate] DESC"}
    res = _get(f"{ADO_ORG}/{urllib.parse.quote(project)}/_apis/wit/wiql?api-version=7.1&$top=2000", auth, q)
    ids = [w["id"] for w in res.get("workItems", [])]
    items = []
    for i in range(0, len(ids), 200):
        batch = _get(f"{ADO_ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                     {"ids": ids[i:i + 200], "fields": ADO_FIELDS})
        items += batch.get("value", [])
    by_type: dict[str, list] = {}
    for it in items:
        f = it["fields"]
        by_type.setdefault(f.get("System.WorkItemType", "Other"), []).append(f)
    order = ["Epic", "Feature", "Product Backlog Item", "User Story", "Bug", "Task"]
    lines = [f"# ADO: {project}", "",
             f"Synced by Chieff on {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}. "
             f"{len(items)} work items. Source: {ADO_ORG}/{urllib.parse.quote(project)}", ""]
    for t in order + sorted(k for k in by_type if k not in order):
        if t not in by_type:
            continue
        group = by_type[t]
        lines.append(f"## {t}s ({len(group)})\n")
        for f in sorted(group, key=lambda x: x.get("System.ChangedDate", ""), reverse=True):
            wid = f.get("System.Id")
            who = f.get("System.AssignedTo")
            who = who.get("displayName", "unassigned") if isinstance(who, dict) else "unassigned"
            url = f"{ADO_ORG}/{urllib.parse.quote(project)}/_workitems/edit/{wid}"
            lines.append(f"### {t} {wid}: {f.get('System.Title', '')}")
            tags = f.get("System.Tags", "")
            lines.append(f"- state: **{f.get('System.State', '')}** | assigned: {who}"
                         + (f" | tags: {tags}" if tags else ""))
            lines.append(f"- iteration: {f.get('System.IterationPath', '')} | changed: "
                         f"{str(f.get('System.ChangedDate', ''))[:10]} | [open]({url})")
            desc = _strip_html(f.get("System.Description", ""))
            if desc and t in ("Epic", "Feature", "Product Backlog Item", "User Story"):
                lines.append("")
                lines.append(desc[:1200] + ("…" if len(desc) > 1200 else ""))
            lines.append("")
    out = _kdir() / "ADO"
    out.mkdir(parents=True, exist_ok=True)
    (out / f"{project}.md").write_text("\n".join(lines))
    return len(items)


def _ado_sync() -> list[str]:
    auth = _ado_auth()
    if not auth:
        return []
    projects = _cfg().get("ado_projects", ["AI Platform", "OS Conversions Agent"])
    wm = db.get_setting(ADO_WM)
    since = (datetime.fromisoformat(wm) if wm
             else datetime.now(timezone.utc)) - timedelta(minutes=90)
    changed = []
    for project in projects:
        q = {"query": (f"SELECT [System.Id] FROM WorkItems WHERE [System.TeamProject] = '{project}' "
                       f"AND [System.ChangedDate] >= '{since.strftime('%Y-%m-%dT%H:%M:%SZ')}' "
                       f"ORDER BY [System.ChangedDate] DESC")}
        res = _get(f"{ADO_ORG}/{urllib.parse.quote(project)}/_apis/wit/wiql"
                   f"?api-version=7.1&timePrecision=true&$top=200", auth, q)
        ids = [w["id"] for w in res.get("workItems", [])]
        if not ids:
            continue
        batch = _get(f"{ADO_ORG}/_apis/wit/workitemsbatch?api-version=7.1", auth,
                     {"ids": ids[:20], "fields": ["System.Id", "System.Title", "System.State"]})
        heads = [f"WI {it['fields'].get('System.Id')} '{it['fields'].get('System.Title', '')[:60]}' "
                 f"({it['fields'].get('System.State', '')})" for it in batch.get("value", [])]
        n = _ado_regen(project, auth)
        changed.append(f"{project}: {len(ids)} changed of {n}; " + "; ".join(heads[:5]))
    db.set_setting(ADO_WM, datetime.now(timezone.utc).isoformat())
    return changed


async def sync() -> dict:
    conf, ado = [], []
    try:
        conf = await asyncio.to_thread(_confluence_sync)
        for c in conf:
            vault.chieff_trace("Confluence", f"knowledge sync: updated page {c}")
    except Exception as e:
        log.exception("confluence knowledge sync failed")
        db.audit("knowledge_error", {"source": "confluence", "error": str(e)[:300]})
    try:
        ado = await asyncio.to_thread(_ado_sync)
        for a in ado:
            vault.chieff_trace("AzureDevOps", f"knowledge sync: {a}")
    except Exception as e:
        log.exception("ado knowledge sync failed")
        db.audit("knowledge_error", {"source": "ado", "error": str(e)[:300]})
    if conf or ado:
        db.audit("knowledge_synced", {"confluence": conf, "ado": ado})
    return {"confluence_changed": len(conf), "ado_projects_changed": len(ado)}
