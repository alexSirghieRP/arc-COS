"""Proactive-trigger tests (goal pillar 2): change detection, the audit-id
cursor that prevents double-pinging, material filtering, and that a real state
change actually produces a nudge."""

import pytest

from app import actions, companion, db


def test_change_line_formats():
    assert "AB#12345" in companion._change_line(
        "swarm_ado_state", {"ado_item": 12345, "state": "Resolved"})
    assert "Resolved" in companion._change_line(
        "swarm_ado_state", {"ado_item": 12345, "state": "Resolved"})
    assert "run #12" in companion._change_line(
        "swarm_run", {"run_id": 12, "profile": "thread", "pass": 3, "fail": 1})


@pytest.mark.asyncio
async def test_proactive_baselines_then_fires(monkeypatch):
    # capture the nudge instead of posting to Teams
    sent = {}

    async def fake_reply(body):
        sent["body"] = body
        return "sent"

    async def fake_agent(*a, **k):
        return {"nudge": "heads up - AB#12345 just went Resolved, nice"}

    monkeypatch.setattr(actions, "companion_reply", fake_reply)
    monkeypatch.setattr(companion, "run_agent", fake_agent)

    # first sweep baselines (no history dump)
    db.execute("DELETE FROM settings WHERE key=?", ("companion_proactive_cursor",)); db._settings_cache.pop("companion_proactive_cursor", None)
    r0 = await companion.proactive_sweep()
    assert "baselined" in r0

    # a real state change lands
    db.audit("swarm_ado_state", {"ado_item": 12345, "state": "Resolved"}, actor="swarm")

    # second sweep detects it and fires a nudge
    r1 = await companion.proactive_sweep()
    assert r1.get("changes", 0) >= 1
    assert "12345" in sent.get("body", "")


@pytest.mark.asyncio
async def test_proactive_does_not_double_ping(monkeypatch):
    calls = {"n": 0}

    async def fake_reply(body):
        calls["n"] += 1
        return "sent"

    async def fake_agent(*a, **k):
        return {"nudge": "something changed"}

    monkeypatch.setattr(actions, "companion_reply", fake_reply)
    monkeypatch.setattr(companion, "run_agent", fake_agent)

    db.execute("DELETE FROM settings WHERE key=?", ("companion_proactive_cursor",)); db._settings_cache.pop("companion_proactive_cursor", None)
    await companion.proactive_sweep()               # baseline
    db.audit("swarm_pr_chat", {"pr_url": "http://x/pull/9"}, actor="swarm")
    await companion.proactive_sweep()               # fires once
    before = calls["n"]
    await companion.proactive_sweep()               # nothing new -> no ping
    assert calls["n"] == before


@pytest.mark.asyncio
async def test_dry_run_changes_are_ignored(monkeypatch):
    async def fake_reply(body):
        return "sent"

    fired = {"n": 0}

    async def fake_agent(*a, **k):
        fired["n"] += 1
        return {"nudge": "x"}

    monkeypatch.setattr(actions, "companion_reply", fake_reply)
    monkeypatch.setattr(companion, "run_agent", fake_agent)
    db.execute("DELETE FROM settings WHERE key=?", ("companion_proactive_cursor",)); db._settings_cache.pop("companion_proactive_cursor", None)
    await companion.proactive_sweep()
    db.audit("swarm_ado_state", {"ado_item": 1, "state": "Active", "dry_run": True})
    r = await companion.proactive_sweep()
    assert r.get("idle") or r.get("changes", 0) == 0
