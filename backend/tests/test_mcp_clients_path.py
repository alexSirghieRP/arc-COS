"""MCP servers are spawned as bare `node`/`npx` commands (settings.json), but
node lives under nvm, which is loaded only from .zshrc - a shell file that
non-interactive shells never source. A backend started from the macOS app
(zsh -lc) or launchd therefore has no node on PATH and every stdio spawn dies
with FileNotFoundError (the owner noted, 2026-08-11: this took out ms-graph entirely, so
every Graph sweep - pr_channel_review included - failed in ~20ms for hours).
"""

import os
import stat

from app import mcp_clients


def _fake_nvm(root, versions, default_alias=None):
    """Build an nvm-shaped tree with a runnable `node` in each version."""
    for v in versions:
        bin_dir = root / "versions" / "node" / v / "bin"
        bin_dir.mkdir(parents=True)
        node = bin_dir / "node"
        node.write_text("#!/bin/sh\necho fake\n")
        node.chmod(node.stat().st_mode | stat.S_IEXEC)
    if default_alias is not None:
        alias = root / "alias"
        alias.mkdir(parents=True, exist_ok=True)
        (alias / "default").write_text(default_alias + "\n")
    return str(root)


def test_spawn_path_adds_nvm_node_when_missing(tmp_path, monkeypatch):
    nvm = _fake_nvm(tmp_path / "nvm", ["v24.14.1"])
    monkeypatch.setenv("NVM_DIR", nvm)
    base = "/usr/bin:/bin"

    result = mcp_clients._spawn_path(base)

    expected = os.path.join(nvm, "versions", "node", "v24.14.1", "bin")
    assert expected in result.split(os.pathsep)
    assert result.startswith(base)  # never reorders what the process already had


def test_spawn_path_prefers_the_nvm_default_alias(tmp_path, monkeypatch):
    nvm = _fake_nvm(tmp_path / "nvm", ["v20.11.0", "v24.14.1"], default_alias="v20.11.0")
    monkeypatch.setenv("NVM_DIR", nvm)

    result = mcp_clients._spawn_path("/usr/bin:/bin").split(os.pathsep)

    assert os.path.join(nvm, "versions", "node", "v20.11.0", "bin") in result
    assert os.path.join(nvm, "versions", "node", "v24.14.1", "bin") not in result


def test_spawn_path_falls_back_to_newest_version(tmp_path, monkeypatch):
    """No default alias: highest version wins, compared numerically - a plain
    string sort would put v9 above v24."""
    nvm = _fake_nvm(tmp_path / "nvm", ["v9.1.0", "v24.14.1"])
    monkeypatch.setenv("NVM_DIR", nvm)

    result = mcp_clients._spawn_path("/usr/bin:/bin").split(os.pathsep)

    assert os.path.join(nvm, "versions", "node", "v24.14.1", "bin") in result


def test_spawn_path_untouched_when_node_already_resolves(tmp_path, monkeypatch):
    """A backend started from a terminal already has node; don't append
    anything (and never shadow the node the user's shell would pick)."""
    real_bin = tmp_path / "real"
    real_bin.mkdir()
    node = real_bin / "node"
    node.write_text("#!/bin/sh\necho real\n")
    node.chmod(node.stat().st_mode | stat.S_IEXEC)
    nvm = _fake_nvm(tmp_path / "nvm", ["v24.14.1"])
    monkeypatch.setenv("NVM_DIR", nvm)

    base = f"{real_bin}{os.pathsep}/usr/bin"
    assert mcp_clients._spawn_path(base) == base


def test_spawn_path_survives_missing_nvm(tmp_path, monkeypatch):
    monkeypatch.setenv("NVM_DIR", str(tmp_path / "does-not-exist"))
    base = "/usr/bin:/bin"

    assert mcp_clients._spawn_path(base) == base
