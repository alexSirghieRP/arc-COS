"""Provider/model picker for the CoS chat (owner request, 2026-08-26).

The point of validating in set_selection() is that a bad pair would otherwise
surface as an opaque CLI failure on the owner's next message, several layers
away from the dropdown that caused it.
"""

import pytest

from app import db, providers

CATALOG = [
    {"id": "anthropic", "label": "Anthropic", "cli": "claude", "warm": True,
     "default": "claude-sonnet-4-6",
     "models": ["claude-sonnet-4-6", "claude-opus-5"]},
    {"id": "codex", "label": "Codex", "cli": "codex", "warm": False,
     "default": "gpt-5.6-sol", "models": ["gpt-5.6-sol"]},
]


@pytest.fixture(autouse=True)
def _catalog(monkeypatch):
    """Pin the catalog so these tests don't move when policy.yaml is edited."""
    monkeypatch.setattr(providers, "catalog", lambda: CATALOG)
    monkeypatch.setattr(providers.db, "set_setting", db.set_setting)
    for k in ("companion_provider", "companion_model"):
        db.set_setting(k, "")
    yield


def test_defaults_to_anthropic_before_any_pick():
    prov, model = providers.selection()
    assert prov == "anthropic"
    assert model in CATALOG[0]["models"]


def test_set_and_read_back():
    providers.set_selection("codex", "gpt-5.6-sol")
    assert providers.selection() == ("codex", "gpt-5.6-sol")


def test_rejects_unknown_provider():
    with pytest.raises(ValueError):
        providers.set_selection("bedrock", "claude-opus-5")


def test_rejects_model_from_the_wrong_provider():
    """The bug this guards: picking codex while a claude-* model id is still
    stored would ship a claude id to the codex CLI."""
    with pytest.raises(ValueError):
        providers.set_selection("codex", "claude-opus-5")


def test_stored_model_from_another_provider_is_not_used():
    """Switching provider must not inherit the previous provider's model even
    if the raw setting still holds it."""
    db.set_setting("companion_provider", "codex")
    db.set_setting("companion_model", "claude-opus-5")  # stale, wrong provider
    prov, model = providers.selection()
    assert prov == "codex"
    assert model == "gpt-5.6-sol", "should fall back to the codex default"


def test_unknown_stored_provider_falls_back_rather_than_wedging():
    db.set_setting("companion_provider", "removed-provider")
    prov, _ = providers.selection()
    assert prov == "anthropic"


def test_policy_model_only_used_when_it_belongs_to_the_provider(monkeypatch):
    """policy.yaml's companion.model is a claude id; it must not leak through
    as codex's model."""
    monkeypatch.setattr(providers, "policy",
                        lambda: {"companion": {"model": "claude-opus-5"}})
    db.set_setting("companion_provider", "codex")
    _, model = providers.selection()
    assert model == "gpt-5.6-sol"

    db.set_setting("companion_provider", "anthropic")
    _, model = providers.selection()
    assert model == "claude-opus-5", "in-provider policy model should win"


def test_strictify_matches_what_openai_demanded():
    """codex returned HTTP 400 invalid_json_schema until every nested object
    carried additionalProperties:false with all properties in required."""
    schema = {"type": "object", "properties": {
        "reply": {"type": "string"},
        "commands": {"type": "array", "items": {"type": "object", "properties": {
            "action": {"type": "string"},
            "args": {"type": "object"},          # open map - the 400's cause
        }, "required": ["action", "args"]}},
    }, "required": ["reply", "commands"]}

    strict, converted = providers._strictify(schema)

    assert strict["additionalProperties"] is False
    assert set(strict["required"]) == {"reply", "commands"}
    item = strict["properties"]["commands"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == {"action", "args"}
    # the open map cannot be expressed under strict mode - carried as a string
    assert item["properties"]["args"]["type"] == "string"
    assert converted == [("commands", "[]", "args")]


def test_args_round_trip_through_the_string_encoding():
    """The live smoke test returns commands:[] , so this is the only cover for
    the path that actually fires when CoS issues a command."""
    result = {"reply": "on it", "commands": [
        {"action": "terminal_send", "args": '{"id": "3", "text": "ls"}'},
        {"action": "swarm_run", "args": '{"profile": "prs"}'},
    ]}
    decoded = providers._decode_at(result, ("commands", "[]", "args"))
    assert decoded["commands"][0]["args"] == {"id": "3", "text": "ls"}
    assert decoded["commands"][1]["args"] == {"profile": "prs"}


def test_malformed_args_does_not_crash_the_turn():
    """A non-JSON args string must degrade to {} - companion._exec would
    otherwise blow up on a string where it expects a mapping."""
    result = {"commands": [{"action": "run", "args": "not json at all"}]}
    decoded = providers._decode_at(result, ("commands", "[]", "args"))
    assert decoded["commands"][0]["args"] == {}


def test_catalog_marks_availability_from_path(monkeypatch):
    """A provider whose CLI is missing stays listed but flagged, so the UI can
    say why instead of failing on send."""
    monkeypatch.undo()  # drop the catalog stub; exercise the real one
    monkeypatch.setattr(providers, "policy",
                        lambda: {"companion": {"providers": [
                            {"id": "ghost", "cli": "definitely-not-installed",
                             "models": ["m"]}]}})
    entry = providers.catalog()[0]
    assert entry["available"] is False
    assert entry["id"] == "ghost"
