"""Hermetic child-environment contracts for native CLI worker adapters.

No provider, model, or network call is made: every adapter runs against a
small stub executable that records its own environment and exits.  The
``os.environ`` canaries prove that nothing ambient reaches a worker child,
including when ``request.env`` is built the way the orchestrator builds it
(``_filtered_worker_env({**os.environ, ...})``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from saberops.config import RunConfig
from saberops.db import Database
from saberops.git import GitManager
from saberops.models import WorkerRequest
from saberops.orchestrator import Orchestrator, _filtered_worker_env
from saberops.workers.antigravity import AntigravityAdapter
from saberops.workers.claude import ClaudeCodeAdapter
from saberops.workers.cline import ClineAdapter
from saberops.workers.codex import CodexAdapter
from saberops.workers.env import (
    BASE_ENV_ALLOWLIST,
    HARNESS_ENV_ADDITIONS,
    hermetic_env,
)
from saberops.workers.opencode import OpenCodeAdapter
from saberops.workers.registry import AdapterRegistry

#: Ambient credentials a native CLI child must never inherit.
CANARIES: dict[str, str] = {
    "CODEX_API_KEY": "canary-codex",
    "CODEX_ACCESS_TOKEN": "canary-codex-token",
    "OPENAI_API_KEY": "canary-openai",
    "ANTHROPIC_API_KEY": "canary-anthropic",
    "ANTHROPIC_AUTH_TOKEN": "canary-anthropic-token",
    "OPENROUTER_API_KEY": "canary-openrouter",
    "GEMINI_API_KEY": "canary-gemini",
    "GOOGLE_API_KEY": "canary-google",
    "CLAUDE_CODE_OAUTH_TOKEN": "canary-claude-oauth",
    "AWS_SECRET_ACCESS_KEY": "canary-aws",
    "SOME_RANDOM_SECRET": "canary-random",
}

HARNESSES: tuple[str, ...] = ("claude", "codex", "opencode", "antigravity", "cline")


def _adapter_for(harness: str, binary: str) -> Any:
    if harness == "claude":
        return ClaudeCodeAdapter(claude_bin=binary)
    if harness == "codex":
        return CodexAdapter(codex_bin=binary)
    if harness == "opencode":
        return OpenCodeAdapter(opencode_bin=binary)
    if harness == "antigravity":
        return AntigravityAdapter(agy_bin=binary)
    if harness == "cline":
        return ClineAdapter(cline_bin=binary)
    raise AssertionError(harness)


# ---------------------------------------------------------------------------
# Unit tests for :func:`hermetic_env`.
# ---------------------------------------------------------------------------


def test_base_allowlist_admits_exact_names_and_drops_everything_else() -> None:
    parent = {name: f"value-{name}" for name in BASE_ENV_ALLOWLIST}
    parent.update(
        {"SOME_RANDOM_SECRET": "no", "OPENAI_API_KEY": "no", "AWS_SECRET_ACCESS_KEY": "no"}
    )
    env = hermetic_env("cline", None, parent)
    for name in BASE_ENV_ALLOWLIST:
        assert env[name] == f"value-{name}"
    assert "SOME_RANDOM_SECRET" not in env
    assert "OPENAI_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env


def test_lc_prefixed_names_are_admitted_by_prefix() -> None:
    parent = {"PATH": "/p", "LC_ALL": "C", "LC_CTYPE": "UTF-8", "LC_MESSAGES": "en", "LCX": "no"}
    env = hermetic_env("cline", None, parent)
    assert env["LC_ALL"] == "C"
    assert env["LC_CTYPE"] == "UTF-8"
    assert env["LC_MESSAGES"] == "en"
    assert "LCX" not in env


def test_per_harness_additions_are_harness_specific() -> None:
    parent = {"PATH": "/p", "CLAUDE_CONFIG_DIR": "/claude", "CODEX_HOME": "/codex"}
    claude = hermetic_env("claude", None, parent)
    assert claude["CLAUDE_CONFIG_DIR"] == "/claude"
    assert "CODEX_HOME" not in claude
    codex = hermetic_env("codex", None, parent)
    assert codex["CODEX_HOME"] == "/codex"
    assert "CLAUDE_CONFIG_DIR" not in codex
    for harness in ("opencode", "antigravity", "cline"):
        assert HARNESS_ENV_ADDITIONS[harness] == ()
        env = hermetic_env(harness, None, parent)
        assert "CLAUDE_CONFIG_DIR" not in env
        assert "CODEX_HOME" not in env


def test_overlay_allowlist_keeps_safe_and_saberops_keys_only() -> None:
    overlay = {
        **CANARIES,
        "PATH": "/custom-bin",
        "HOME": "/custom-home",
        "ORCH_RUN_ID": "run-1",
        "ORCH_DB_PATH": "/state/runs.db",
        "OPENCODE_CONFIG_CONTENT": "{}",
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
        "FROZEN": "not-saberops-owned",
    }
    for harness in HARNESSES:
        env = hermetic_env(harness, overlay, {})
        for canary in CANARIES:
            assert canary not in env, f"{canary} reached {harness}"
        assert env["PATH"] == "/custom-bin"
        assert env["HOME"] == "/custom-home"
        assert env["ORCH_RUN_ID"] == "run-1"
        assert env["ORCH_DB_PATH"] == "/state/runs.db"
        assert env["OPENCODE_CONFIG_CONTENT"] == "{}"
        assert env["CLAUDE_CODE_DISABLE_FAST_MODE"] == "1"
        assert "FROZEN" not in env


def test_overlay_is_applied_last_over_parent() -> None:
    env = hermetic_env("codex", {"PATH": "/custom", "ORCH_RUN_ID": "r"}, {"PATH": "/parent"})
    assert env["PATH"] == "/custom"
    assert env["ORCH_RUN_ID"] == "r"


def test_claude_overlay_denylist_drops_billing_credentials() -> None:
    overlay = {
        "ANTHROPIC_API_KEY": "no",
        "ANTHROPIC_AUTH_TOKEN": "no",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDE_CODE_USE_VERTEX": "1",
        "CLAUDE_CODE_DISABLE_FAST_MODE": "1",
        "ORCH_RUN_ID": "r",
    }
    env = hermetic_env("claude", overlay, {})
    assert "ANTHROPIC_API_KEY" not in env
    assert "ANTHROPIC_AUTH_TOKEN" not in env
    assert "CLAUDE_CODE_USE_BEDROCK" not in env
    assert "CLAUDE_CODE_USE_VERTEX" not in env
    assert env["CLAUDE_CODE_DISABLE_FAST_MODE"] == "1"
    assert env["ORCH_RUN_ID"] == "r"


def test_antigravity_overlay_denylist_drops_api_key_configuration() -> None:
    overlay = {"GEMINI_API_KEY": "no", "GOOGLE_API_KEY": "no", "ORCH_RUN_ID": "r"}
    env = hermetic_env("antigravity", overlay, {})
    assert "GEMINI_API_KEY" not in env
    assert "GOOGLE_API_KEY" not in env
    assert env["ORCH_RUN_ID"] == "r"


GATEWAY_OVERLAY: dict[str, str] = {
    "ORCH_GATEWAY_ENDPOINT": "http://gateway",
    "ORCH_GATEWAY_CONNECTION": "acct-1",
    "ORCH_GATEWAY_AUTHORIZATION_ENV": "OMNIROUTE_API_KEY",
    "ORCH_GATEWAY_LOOPBACK_URL": "http://127.0.0.1:9",
    "ORCH_GATEWAY_PROVIDER_ID": "orch_omniroute",
    "_ORCH_BRIDGE_AUTH_SENTINEL": "loopback-sentinel-not-a-secret",
}


def test_codex_gateway_overlay_and_bridge_sentinel_are_preserved() -> None:
    assert hermetic_env("codex", GATEWAY_OVERLAY, {}) == GATEWAY_OVERLAY


@pytest.mark.parametrize("harness", ["claude", "opencode", "antigravity", "cline"])
def test_gateway_overlay_is_denied_for_non_codex_harnesses(harness: str) -> None:
    overlay = {**GATEWAY_OVERLAY, "ORCH_RUN_ID": "r"}
    env = hermetic_env(harness, overlay, {})
    for key in GATEWAY_OVERLAY:
        assert key not in env, f"{key} reached {harness}"
    assert env["ORCH_RUN_ID"] == "r"


def test_parent_env_none_reads_os_environ(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", "/only-source")
    monkeypatch.setenv("CANARY", "no")
    env = hermetic_env("cline", None)
    assert env["PATH"] == "/only-source"
    assert "CANARY" not in env


# ---------------------------------------------------------------------------
# Per-adapter end-to-end canary tests against a real stub executable.
# ---------------------------------------------------------------------------


def _write_stub(bindir: Path, name: str, outfile: Path) -> None:
    """Write an executable that records its environment as JSON and exits 0."""
    auth_json = json.dumps(
        {"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"}
    )
    result_json = json.dumps(
        {"type": "result", "subtype": "success", "is_error": False, "result": "Done"}
    )
    script = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(outfile)!r}, 'w', encoding='utf-8') as fh:\n"
        "    json.dump(dict(os.environ), fh, sort_keys=True)\n"
        f"if 'auth' in sys.argv:\n    print({auth_json!r})\nelse:\n    print({result_json!r})\n"
    )
    path = bindir / name
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


@pytest.fixture()
def stub_worker_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Path, Path, Path]:
    """Install a canary-filled ambient environment plus a stub bin directory."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    outfile = tmp_path / "child_env.json"
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USER", "worker-user")
    monkeypatch.setenv("LANG", "C.UTF-8")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")
    for name, value in CANARIES.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(GitManager, "get_changed_paths", lambda _path: [])
    return bindir, outfile, home


def _child_env(outfile: Path) -> dict[str, str]:
    assert outfile.is_file(), "stub child never recorded its environment"
    loaded = json.loads(outfile.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    return {str(k): str(v) for k, v in loaded.items()}


def _assert_hermetic(child: dict[str, str], home: Path) -> None:
    for name in CANARIES:
        assert name not in child, f"canary {name} reached the worker child"
    assert child.get("PATH")
    assert child.get("HOME") == str(home)


def test_codex_adapter_child_env_is_hermetic(
    stub_worker_env: tuple[Path, Path, Path], tmp_path: Path
) -> None:
    bindir, outfile, home = stub_worker_env
    _write_stub(bindir, "stub-codex", outfile)
    adapter = CodexAdapter(codex_bin="stub-codex")
    adapter.run(
        WorkerRequest(
            "task",
            tmp_path,
            "model",
            env={
                "ORCH_GATEWAY_ENDPOINT": "http://gateway",
                "_ORCH_BRIDGE_AUTH_SENTINEL": "loopback-sentinel-not-a-secret",
            },
        )
    )
    child = _child_env(outfile)
    _assert_hermetic(child, home)
    assert child["ORCH_GATEWAY_ENDPOINT"] == "http://gateway"
    assert child["_ORCH_BRIDGE_AUTH_SENTINEL"] == "loopback-sentinel-not-a-secret"


@pytest.mark.parametrize("harness", ["claude", "opencode", "antigravity", "cline"])
def test_non_codex_adapters_never_see_gateway_env(
    stub_worker_env: tuple[Path, Path, Path], tmp_path: Path, harness: str
) -> None:
    bindir, outfile, home = stub_worker_env
    name = f"stub-{harness}"
    _write_stub(bindir, name, outfile)
    adapter = _adapter_for(harness, name)
    adapter.run(WorkerRequest("task", tmp_path, "model", env=dict(GATEWAY_OVERLAY)))
    child = _child_env(outfile)
    _assert_hermetic(child, home)
    for key in GATEWAY_OVERLAY:
        assert key not in child, f"{key} reached {harness}"


def _production_shaped_env() -> dict[str, str]:
    """Build ``request.env`` exactly the way the orchestrator dispatch sites do."""
    return _filtered_worker_env(
        {
            **os.environ,
            "ORCH_RUN_ID": "run-1",
            "ORCH_ATTEMPT_ID": "attempt-1",
            "ORCH_ASSOCIATION_ID": "attempt-1",
            "ORCH_DB_PATH": "/state/runs.db",
        }
    )


@pytest.mark.parametrize("harness", HARNESSES)
def test_production_shaped_overlay_is_hermetic(
    stub_worker_env: tuple[Path, Path, Path], tmp_path: Path, harness: str
) -> None:
    bindir, outfile, home = stub_worker_env
    name = f"stub-{harness}"
    _write_stub(bindir, name, outfile)
    production_env = _production_shaped_env()
    # The overlay really does carry ambient canaries in production shape.
    assert any(canary in production_env for canary in CANARIES)
    adapter = _adapter_for(harness, name)
    adapter.run(WorkerRequest("task", tmp_path, "model", env=production_env))
    child = _child_env(outfile)
    _assert_hermetic(child, home)
    assert child["ORCH_RUN_ID"] == "run-1"
    assert child["ORCH_ATTEMPT_ID"] == "attempt-1"
    assert child["ORCH_DB_PATH"] == "/state/runs.db"


# ---------------------------------------------------------------------------
# Orchestrator real-dispatch integration proof (stub worker binary).
# ---------------------------------------------------------------------------


def _write_dispatch_stub(bindir: Path, name: str, outfile: Path) -> None:
    """Write a stub that records its env and drops a file in the ``--cd`` worktree."""
    script = (
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(outfile)!r}, 'w', encoding='utf-8') as fh:\n"
        "    json.dump(dict(os.environ), fh, sort_keys=True)\n"
        "args = sys.argv\n"
        "if '--cd' in args:\n"
        "    wt = args[args.index('--cd') + 1]\n"
        "    with open(os.path.join(wt, 'feature.txt'), 'w', encoding='utf-8') as fh:\n"
        "        fh.write('implemented\\n')\n"
    )
    path = bindir / name
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)


def test_orchestrator_real_dispatch_forwards_hermetic_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for key in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(key, str(tmp_path / key))
    for key in (
        "SABEROPS_PROFILE",
        "ORCH_BINDING_STORE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_READINESS_STORE",
    ):
        monkeypatch.delenv(key, raising=False)
    for name, value in CANARIES.items():
        monkeypatch.setenv(name, value)

    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.name", "Hermetic Test"], cwd=repo, check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "t@localhost"],
        cwd=repo,
        check=True,
        capture_output=True,
    )
    (repo / "README.md").write_text("Original project\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=repo, check=True, capture_output=True)

    bindir = tmp_path / "bin"
    bindir.mkdir()
    outfile = tmp_path / "child_env.json"
    _write_dispatch_stub(bindir, "stub-dispatch-codex", outfile)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))

    db = Database(tmp_path / "project-state" / "runs.db")
    registry = AdapterRegistry([CodexAdapter(codex_bin="stub-dispatch-codex")])
    Orchestrator(db=db, registry=registry, worktree_base=tmp_path / "trees").run(
        config=RunConfig(
            task="Build a useful application",
            target_repo=str(repo),
            provider_override="codex",
            model_override="test-model",
            max_attempts=1,
            gate_command="true",
            review_disabled=True,
        )
    )
    child = _child_env(outfile)
    for name in CANARIES:
        assert name not in child, f"canary {name} reached the orchestrator dispatch"
    assert child["ORCH_RUN_ID"]
    assert child["ORCH_DB_PATH"]
    assert child.get("PATH")
