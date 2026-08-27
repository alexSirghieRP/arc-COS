"""A launched-but-never-completed sign-in must not pin the re-auth lock.

auth-node.js waits on the owner clicking through a real Microsoft login page.
If they never click, the process runs forever -- and before max_inflight_minutes that
kept maybe_reauth() returning "already in flight" indefinitely, so re-auth was
silently disabled while every Graph sweep failed on a dead token.
"""

import pytest

from app import db, graph_reauth


@pytest.fixture(autouse=True)
def _clean_settings():
    db.set_setting("graph_reauth_session", "")
    db.set_setting("graph_reauth_last_attempt", "")
    yield


@pytest.fixture
def env(monkeypatch):
    """Token is dead, re-auth is enabled, and launching is stubbed out."""
    monkeypatch.setattr(graph_reauth, "recent_graph_auth_failure", lambda: True)
    monkeypatch.setattr(graph_reauth, "policy",
                        lambda: {"graph_reauth": {"enabled": True,
                                                  "cooldown_minutes": 60,
                                                  "max_inflight_minutes": 30}})

    state = {"terminals": [], "stopped": [], "launched": []}

    from app import desktop
    monkeypatch.setattr(desktop, "list_terminals", lambda: state["terminals"])

    async def _stop(sid):
        state["stopped"].append(sid)
        return {"id": sid, "stopped": True}

    async def _open(project, cmd, timeout_note=0):
        state["launched"].append(cmd)
        return {"session": "term-new"}

    monkeypatch.setattr(desktop, "terminal_stop", _stop)
    monkeypatch.setattr(desktop, "open_terminal", _open)
    return state


def _terminal(sid, started_minutes_ago, running=True):
    return {"id": sid, "running": running,
            "started": db.cutoff(minutes=started_minutes_ago)}


@pytest.mark.asyncio
async def test_fresh_signin_still_holds_the_lock(env):
    """A sign-in the owner may still be clicking through is left alone."""
    db.set_setting("graph_reauth_session", "term-6")
    env["terminals"] = [_terminal("term-6", started_minutes_ago=5)]

    result = await graph_reauth.maybe_reauth()

    assert result == {"skipped": "re-auth already in flight", "session": "term-6"}
    assert env["stopped"] == []
    assert db.get_setting("graph_reauth_session") == "term-6"


@pytest.mark.asyncio
async def test_abandoned_signin_is_stopped_and_lock_released(env, monkeypatch):
    """Past max_inflight_minutes the sign-in is abandoned, not in flight.

    This is the regression: term-6 sat running for five days and every call
    returned "already in flight", so auth-node.js was never relaunched.
    """
    db.set_setting("graph_reauth_session", "term-6")
    env["terminals"] = [_terminal("term-6", started_minutes_ago=5 * 24 * 60)]
    monkeypatch.setattr(graph_reauth, "_graph_dir", lambda: None)  # stop before launch

    result = await graph_reauth.maybe_reauth()

    assert result != {"skipped": "re-auth already in flight", "session": "term-6"}
    assert env["stopped"] == ["term-6"], "stale auth-node.js must be terminated"
    assert db.get_setting("graph_reauth_session") == ""


@pytest.mark.asyncio
async def test_abandoned_signin_relaunches(env):
    """After releasing the lock the normal launch path runs again."""
    db.set_setting("graph_reauth_session", "term-6")
    env["terminals"] = [_terminal("term-6", started_minutes_ago=5 * 24 * 60)]

    result = await graph_reauth.maybe_reauth()

    assert result.get("started") is True, result
    assert env["launched"] and "auth-node.js" in env["launched"][0]
    assert db.get_setting("graph_reauth_session") == "term-new"


@pytest.mark.asyncio
async def test_cooldown_still_applies_after_abandoning(env):
    """Releasing the lock must not bypass the relaunch cooldown."""
    db.set_setting("graph_reauth_session", "term-6")
    db.set_setting("graph_reauth_last_attempt", db.cutoff(minutes=10))
    env["terminals"] = [_terminal("term-6", started_minutes_ago=5 * 24 * 60)]

    result = await graph_reauth.maybe_reauth()

    assert "cooldown" in result.get("skipped", ""), result
    assert env["launched"] == []


@pytest.mark.asyncio
async def test_session_from_a_restarted_backend_is_cleared(env):
    """Terminals are in-memory; after a restart the stored id matches nothing."""
    db.set_setting("graph_reauth_session", "term-6")
    env["terminals"] = []

    result = await graph_reauth.maybe_reauth()

    assert result.get("started") is True, result
    assert env["stopped"] == [], "nothing to stop -- the process is already gone"
