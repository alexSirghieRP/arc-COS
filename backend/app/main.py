"""Chief-of-Staff backend: FastAPI app, MCP sessions, scheduler."""

import logging
from contextlib import asynccontextmanager
from datetime import date

from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from . import certs, db, graph, health, mcp_clients, scheduler, vault
from .api import router as api_router
from .board import router as board_router
from .config import REPO_ROOT, policy

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
health.install_log_buffer()  # board Logs tab reads from this ring buffer
log = logging.getLogger("chief")

# Trust the corporate TLS-interception CA so Atlassian/Azure REST calls work.
certs.install()

MCP_SERVERS = ["ms-graph", "azure-devops"]


@asynccontextmanager
async def lifespan(app: FastAPI):
    mcp_clients.manager = mcp_clients.MCPManager(MCP_SERVERS)
    app.state.mcp_status = await mcp_clients.manager.start()
    log.info("MCP servers: %s", app.state.mcp_status)
    db.get_conn()
    if db.get_setting("dry_run") is None:
        db.set_setting("dry_run", "true" if policy().get("dry_run", True) else "false")
    if db.get_setting("kill_switch") is None:
        db.set_setting("kill_switch", "false")
    from . import vault
    vault.ensure_chieff_dirs()
    scheduler.start()
    await scheduler.run_startup_jobs()
    # the companion runs its own tight adaptive loop (not the 60s scheduler
    # tick) so it responds in seconds while the user is chatting
    import asyncio
    from . import companion
    companion_task = asyncio.create_task(companion.run_loop())
    yield
    companion_task.cancel()
    await companion._session_reset()  # drop the warm CLI session cleanly
    scheduler.scheduler.shutdown(wait=False)
    await mcp_clients.manager.stop()


app = FastAPI(title="chief-of-staff", lifespan=lifespan)

# This app can send messages as the user, so defense-in-depth on top of the
# 127.0.0.1 uvicorn bind: reject any non-loopback client outright (e.g. if the
# bind is ever changed or forwarded), and set conservative response headers.
_LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}


@app.middleware("http")
async def _local_only_and_headers(request: Request, call_next):
    client = request.client.host if request.client else ""
    if client not in _LOOPBACK:
        return JSONResponse({"detail": "local access only"}, status_code=403)
    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.url.path.startswith("/api"):
        response.headers.setdefault("Cache-Control", "no-store")
    elif request.url.path in ("/", "/index.html"):
        # The HTML shell references content-hashed JS/CSS that vite REPLACES on
        # every rebuild. A cached shell (WKWebView in the Mac app) then 404s its
        # dead hashes and renders a black window — force revalidation instead.
        response.headers["Cache-Control"] = "no-cache"
    return response


app.include_router(api_router)
app.include_router(board_router)

_dist = REPO_ROOT / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="frontend")


@app.get("/api/smoke")
async def smoke(agent: bool = False):
    """Connectivity proof: presence, 5 email subjects, today's calendar, latest daily note."""
    out: dict = {"mcp_status": app.state.mcp_status}

    pres = await graph.presence()
    out["presence"] = {"availability": pres.get("availability"), "activity": pres.get("activity")}

    emails = await graph.list_emails(top=5)
    out["latest_emails"] = [
        {"subject": e.get("subject"), "from": e.get("from", {}).get("emailAddress", {}).get("name")}
        for e in emails
    ]

    events = await graph.calendar_events_cached(date.today())
    out["today_calendar"] = [
        {"subject": e.get("subject"), "start": e.get("start_local"), "end": e.get("end_local")}
        for e in events
    ]

    recent = vault.most_recent_note()
    if recent:
        d, p = recent
        note = vault.read_note(d)
        top3 = note["sections"].get("Top 3 for Today", {}).get("checkboxes", [])
        out["latest_daily_note"] = {"path": str(p), "date": d.isoformat(),
                                    "top3": [c["text"] for c in top3]}
    else:
        out["latest_daily_note"] = None

    try:
        from . import ado as _ado
        projects = _ado.configured_projects()
        ado = await mcp_clients.get_manager().call(
            "azure-devops", "core_list_projects",
            {"projectNameFilter": projects[0] if projects else "", "top": 3})
        names = [p.get("name") for p in ado] if isinstance(ado, list) else str(ado)[:200]
        out["ado"] = {"ok": True, "projects": names}
    except Exception as e:
        out["ado"] = {"ok": False, "error": str(e)[:300]}

    if agent:
        from .agents import CHIEF, run_agent
        try:
            text = await run_agent(CHIEF, "Reply with exactly: OK", with_tools=False, max_turns=1)
            out["agent_sdk"] = {"ok": True, "reply": (text or "").strip()[:50], "model": CHIEF.model}
        except Exception as e:
            out["agent_sdk"] = {"ok": False, "error": str(e)[:300]}

    return out
