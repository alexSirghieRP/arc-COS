"""Model providers for the companion chat (owner decision, 2026-08-26).

Both providers usable today are authenticated agent CLIs, not HTTP APIs.
There is no OPENAI_API_KEY or ANTHROPIC_API_KEY on this machine (same
constraint companion.py already documents for the Claude side), but `claude`
and `codex` are both logged in. So a "provider" here is a CLI that takes a
prompt plus a JSON Schema and returns a structured final message.

- anthropic: the Claude Agent SDK's persistent client, owned by companion.py.
  WARM - one CLI process is reused across turns, so only the first message
  after a (re)connect pays the ~5s spawn.
- codex: `codex exec` per turn. COLD - every turn spawns a process, so it is
  noticeably slower in chat than the warm Anthropic path. Structured output
  via --output-schema, result read back via --output-last-message. This is
  also the eventual route to local/on-prem models, since codex itself fronts
  them (`codex exec --oss --local-provider ollama|lmstudio`) - which means no
  separate local adapter is needed when the user gets there.

The model catalog lives in policy.yaml (companion.providers) rather than in
this file, so a model can be added without a code change. There is no `codex
models` command to discover them from, and hardcoding guessed IDs is how you
end up with a picker full of 404s - the defaults below are only IDs already
observed working on these accounts.
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile

from . import db
from .config import policy

log = logging.getLogger("chief.providers")

CODEX_PATH = shutil.which("codex")

# Fallback catalog, used only when policy.yaml has no companion.providers
# block. Every ID here is one this machine has actually run:
# claude-sonnet-4-6 is the companion's current default, haiku-4-5 is the
# agents.py default, and gpt-5.6-sol is what ~/.codex/config.toml is set to.
# claude-fable-5 is deliberately absent - it is NOT accessible on this
# account and returns empty structured output (see agents.py).
_DEFAULTS = [
    {
        "id": "anthropic",
        "label": "Anthropic (Claude)",
        "cli": "claude",
        "warm": True,
        "default": "claude-sonnet-4-6",
        "models": [
            "claude-sonnet-4-6",
            "claude-opus-5",
            "claude-sonnet-5",
            "claude-haiku-4-5-20251001",
        ],
    },
    {
        "id": "codex",
        "label": "OpenAI (Codex CLI)",
        "cli": "codex",
        "warm": False,
        "default": "gpt-5.6-sol",
        "models": ["gpt-5.6-sol", "gpt-5.4"],
    },
]


def catalog() -> list[dict]:
    """Providers offered in the picker, annotated with whether the CLI is
    actually installed. An uninstalled provider is still listed (so the UI can
    grey it out and say why) rather than hidden, because a silently missing
    option is harder to debug than a disabled one."""
    cfg = policy().get("companion", {}) or {}
    entries = cfg.get("providers") or _DEFAULTS
    out = []
    for p in entries:
        cli = p.get("cli") or p.get("id")
        out.append({**p, "available": bool(shutil.which(cli)) if cli else False})
    return out


def _provider(pid: str) -> dict | None:
    return next((p for p in catalog() if p.get("id") == pid), None)


def selection() -> tuple[str, str]:
    """Currently selected (provider, model). DB settings are the live override;
    policy.yaml's companion.model is the fallback so behaviour is unchanged
    until the user actually picks something in the UI."""
    cfg = policy().get("companion", {}) or {}
    prov = db.get_setting("companion_provider") or "anthropic"
    p = _provider(prov)
    if p is None:  # a provider removed from policy shouldn't wedge the chat
        prov, p = "anthropic", _provider("anthropic")
    model = db.get_setting("companion_model")
    if not model or (p and model not in (p.get("models") or [])):
        # Only fall back to policy's model when it belongs to this provider -
        # otherwise picking codex would silently run a claude-* model id.
        pol_model = cfg.get("model")
        allowed = (p or {}).get("models") or []
        model = (pol_model if pol_model in allowed
                 else (p or {}).get("default") or (allowed[0] if allowed else ""))
    return prov, model


def set_selection(provider: str, model: str) -> tuple[str, str]:
    """Validate against the catalog before persisting - a typo'd model id here
    would otherwise surface as an opaque CLI failure on the user's next message."""
    p = _provider(provider)
    if p is None:
        raise ValueError(f"unknown provider {provider!r}")
    allowed = p.get("models") or []
    if model not in allowed:
        raise ValueError(f"{provider} has no model {model!r} (have: {allowed})")
    db.set_setting("companion_provider", provider)
    db.set_setting("companion_model", model)
    db.audit("companion_provider_changed", {"provider": provider, "model": model},
             actor="user")
    return provider, model


def _strictify(node, path=(), converted=None):
    """Rewrite an Anthropic-shaped JSON Schema into one OpenAI's structured
    output validator accepts, and report which fields had to change shape.

    OpenAI is stricter in two ways Anthropic is not: every object must carry
    `additionalProperties: false` AND list every property in `required`. The
    harder difference is that an OPEN map - `{"type": "object"}` with no
    declared properties - cannot be expressed at all under strict mode. The
    companion's command `args` is exactly that by design (each action takes
    different arguments), so it is carried as a JSON string and decoded on the
    way back out. Discovered the hard way: codex returned HTTP 400
    `invalid_json_schema` naming that exact field (2026-08-26).

    Returns (schema, converted_paths) where each path is a tuple of keys with
    "[]" marking an array hop, e.g. ("commands", "[]", "args").
    """
    if converted is None:
        converted = []
    if not isinstance(node, dict):
        return node, converted
    kind = node.get("type")
    if kind == "object":
        props = node.get("properties")
        if not props:
            converted.append(path)
            desc = (node.get("description") or "").strip()
            return {"type": "string",
                    "description": (desc + " A JSON object, encoded as a "
                                    "string.").strip()}, converted
        out = {}
        for k, v in props.items():
            out[k], converted = _strictify(v, path + (k,), converted)
        return ({**node, "properties": out, "additionalProperties": False,
                 "required": list(props.keys())}, converted)
    if kind == "array" and isinstance(node.get("items"), dict):
        items, converted = _strictify(node["items"], path + ("[]",), converted)
        return {**node, "items": items}, converted
    return node, converted


def _decode_at(value, path):
    """Undo one _strictify string-ification, in place in the result tree."""
    if not path:
        if isinstance(value, str):
            try:
                return json.loads(value) if value.strip() else {}
            except json.JSONDecodeError:
                log.warning("codex: %r not decodable as JSON args", value[:120])
                return {}
        return value
    head, rest = path[0], path[1:]
    if head == "[]":
        return [_decode_at(v, rest) for v in value] if isinstance(value, list) else value
    if isinstance(value, dict) and head in value:
        return {**value, head: _decode_at(value[head], rest)}
    return value


async def ask_codex(prompt: str, model: str, schema: dict, timeout: float) -> dict:
    """One `codex exec` turn with a JSON-Schema-constrained final message.

    Sandboxed read-only: the companion's own command execution goes through
    companion._exec (an explicit allowlist), so the model itself must never
    get a shell here. --ephemeral keeps codex from accumulating session files
    for a conversation whose history we already own in sqlite.
    """
    if not CODEX_PATH:
        raise RuntimeError("codex CLI not found on PATH")
    strict, converted = _strictify(schema)
    with tempfile.TemporaryDirectory(prefix="cos-codex-") as td:
        schema_path = os.path.join(td, "schema.json")
        out_path = os.path.join(td, "out.json")
        with open(schema_path, "w") as f:
            json.dump(strict, f)
        args = [
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--sandbox", "read-only",
            "--model", model,
            "--output-schema", schema_path,
            "--output-last-message", out_path,
            "-",  # prompt on stdin: avoids argv length limits on long snapshots
        ]
        proc = await asyncio.create_subprocess_exec(
            CODEX_PATH, *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE)
        try:
            _, err = await asyncio.wait_for(
                proc.communicate(prompt.encode()), timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError):
            proc.kill()
            raise
        if proc.returncode != 0:
            raise RuntimeError(
                f"codex exec failed ({proc.returncode}): {err.decode()[:300]}")
        try:
            with open(out_path) as f:
                raw = f.read().strip()
        except FileNotFoundError:
            raise RuntimeError("codex exec wrote no final message")
        if not raw:
            raise RuntimeError("codex exec returned an empty final message")
        try:
            out = json.loads(raw)
        except json.JSONDecodeError:
            # --output-schema should guarantee JSON, but a refusal or a wrapper
            # line would otherwise crash the whole turn with a stack trace.
            raise RuntimeError(f"codex returned non-JSON: {raw[:200]}")
        for path in converted:
            out = _decode_at(out, path)
        return out
