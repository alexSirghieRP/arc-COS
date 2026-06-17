"""Confluence activity feed: recent updates to pages the user follows or authored.

Uses the Atlassian token already configured for the confluence MCP server (in
the user-level Claude settings) via the REST API. Two CQL queries -- pages
the user created/contributed to ("mine") and pages he watches ("following") --
merged and tagged, newest first.
"""

import base64
import logging

import httpx

from . import certs
from .config import mcp_server_defs, policy

log = logging.getLogger("chief.confluence")
_HTTP = httpx.Client(verify=certs.ca_bundle(), timeout=60)


def _conf() -> dict:
    return mcp_server_defs().get("atlassian", {}).get("env", {})


def _auth(env: dict) -> str:
    return "Basic " + base64.b64encode(
        f"{env['ATLASSIAN_EMAIL']}:{env['ATLASSIAN_API_TOKEN']}".encode()).decode()


def _search(base: str, auth: str, cql: str, limit: int) -> list[dict]:
    url = (f"{base}/wiki/rest/api/content/search"
           f"?cql={httpx.QueryParams({'cql': cql})['cql']}"
           f"&expand=version,space,history.lastUpdated&limit={limit}")
    r = _HTTP.get(url, headers={"Authorization": auth, "Accept": "application/json"})
    r.raise_for_status()
    return r.json().get("results", [])


def my_updates() -> dict:
    env = _conf()
    base = (env.get("ATLASSIAN_URL") or "").rstrip("/")
    if not (base and env.get("ATLASSIAN_EMAIL") and env.get("ATLASSIAN_API_TOKEN")):
        return {"error": "no Confluence credentials configured"}
    auth = _auth(env)
    cfg = policy().get("confluence_feed", {})
    days = int(cfg.get("lookback_days", 30))
    # The REST token is anonymous-scoped, so currentUser() resolves to nobody;
    # query by the user's explicit Confluence accountId instead.
    aid = cfg.get("account_id")
    if not aid:
        return {"error": "set confluence_feed.account_id in policy.yaml"}
    queries = {
        "mine": f'(creator = "{aid}" OR contributor = "{aid}") '
                f'AND type = page AND lastModified >= now("-{days}d") ORDER BY lastmodified DESC',
        "following": f'watcher = "{aid}" AND type = page '
                     f'AND lastModified >= now("-{days}d") ORDER BY lastmodified DESC',
    }
    by_id: dict[str, dict] = {}
    for relation, cql in queries.items():
        try:
            results = _search(base, auth, cql, 50)
        except Exception as e:
            log.warning("confluence %s query failed: %s", relation, e)
            continue
        for p in results:
            pid = p.get("id")
            if not pid:
                continue
            ver = p.get("version") or {}
            entry = by_id.get(pid)
            if entry:
                if relation not in entry["relations"]:
                    entry["relations"].append(relation)
                continue
            by_id[pid] = {
                "id": pid,
                "title": p.get("title", ""),
                "space": (p.get("space") or {}).get("name")
                         or (p.get("space") or {}).get("key", ""),
                "url": base + "/wiki" + (p.get("_links", {}).get("webui", "")
                                         if p.get("_links") else f"/spaces/pages/{pid}"),
                "version": ver.get("number"),
                "by": (ver.get("by") or {}).get("displayName", "?"),
                "when": (ver.get("when") or "")[:19].replace("T", " "),
                "relations": [relation],
            }
    items = sorted(by_id.values(), key=lambda x: x["when"] or "", reverse=True)
    return {"items": items, "total": len(items),
            "following": sum(1 for i in items if "following" in i["relations"]),
            "mine": sum(1 for i in items if "mine" in i["relations"])}


# ---- read/write a specific page (used by the weekly status report) ----------

def _base_auth() -> tuple[str, str]:
    """(base_url, auth_header). Raises if Confluence credentials are missing."""
    env = _conf()
    base = (env.get("ATLASSIAN_URL") or "").rstrip("/")
    if not (base and env.get("ATLASSIAN_EMAIL") and env.get("ATLASSIAN_API_TOKEN")):
        raise RuntimeError("no Confluence credentials configured (atlassian MCP env)")
    return base, _auth(env)


def web_url(base: str, page: dict) -> str:
    link = (page.get("_links") or {}).get("webui", "")
    return base + "/wiki" + link if link else f"{base}/wiki/spaces/pages/{page.get('id')}"


def get_page(page_id: str, expand: str = "body.storage,version,space,ancestors") -> dict:
    base, auth = _base_auth()
    r = _HTTP.get(f"{base}/wiki/rest/api/content/{page_id}?expand={expand}",
                  headers={"Authorization": auth, "Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def child_pages(parent_id: str, limit: int = 25) -> list[dict]:
    """Direct child pages of a parent, newest version first."""
    base, auth = _base_auth()
    r = _HTTP.get(f"{base}/wiki/rest/api/content/{parent_id}/child/page"
                  f"?limit={limit}&expand=version",
                  headers={"Authorization": auth, "Accept": "application/json"})
    r.raise_for_status()
    return r.json().get("results", [])


def create_page(space_key: str, parent_id: str, title: str, storage_html: str,
                draft: bool = True) -> dict:
    """Create a page under parent_id. draft=True leaves it unpublished (not in
    the tree) until publish_page() is called. Returns {id, title, status, url, edit_url}."""
    base, auth = _base_auth()
    payload = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "ancestors": [{"id": str(parent_id)}],
        "status": "draft" if draft else "current",
        "body": {"storage": {"value": storage_html, "representation": "storage"}},
    }
    r = _HTTP.post(f"{base}/wiki/rest/api/content", json=payload,
                   headers={"Authorization": auth, "Content-Type": "application/json"})
    r.raise_for_status()
    p = r.json()
    pid = p.get("id")
    return {"id": pid, "title": p.get("title"), "status": p.get("status"),
            "url": web_url(base, p),
            # canonical link, stable whether draft or published
            "page_url": f"{base}/wiki/spaces/{space_key}/pages/{pid}",
            "edit_url": f"{base}/wiki/spaces/{space_key}/pages/edit-v2/{pid}"}


def publish_page(page_id: str) -> dict:
    """Publish a draft page (status draft -> current) at the next version.
    Returns {id, title, status, url}."""
    base, auth = _base_auth()
    cur = get_page(page_id, expand="version,space,body.storage")
    ver = (cur.get("version") or {}).get("number", 1)
    payload = {
        "id": str(page_id),
        "type": "page",
        "title": cur.get("title"),
        "status": "current",
        "version": {"number": ver + 1},
        "body": {"storage": {
            "value": ((cur.get("body") or {}).get("storage") or {}).get("value", ""),
            "representation": "storage"}},
    }
    r = _HTTP.put(f"{base}/wiki/rest/api/content/{page_id}", json=payload,
                  headers={"Authorization": auth, "Content-Type": "application/json"})
    r.raise_for_status()
    p = r.json()
    return {"id": p.get("id"), "title": p.get("title"), "status": p.get("status"),
            "url": web_url(base, p)}
