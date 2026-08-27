"""policy.yaml loader with mtime-based hot reload."""

import json
import os
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
POLICY_PATH = REPO_ROOT / "policy.yaml"
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "chief.sqlite3"
CLAUDE_SETTINGS = Path.home() / ".claude" / "settings.json"

_cache: dict = {}
_cache_mtime: float = 0.0


def policy() -> dict:
    """Current policy.yaml contents, reloaded whenever the file changes."""
    global _cache, _cache_mtime
    mtime = POLICY_PATH.stat().st_mtime
    if mtime != _cache_mtime:
        _cache = yaml.safe_load(POLICY_PATH.read_text())
        _cache_mtime = mtime
    return _cache


def mcp_server_defs() -> dict:
    """mcpServers from the user-level Claude settings. Tokens never leave this process."""
    settings = json.loads(CLAUDE_SETTINGS.read_text())
    return settings.get("mcpServers", {})


def vault_root() -> Path:
    return Path(policy()["vault"]["root"])


DATA_DIR.mkdir(exist_ok=True)
