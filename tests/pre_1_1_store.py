"""Restore the committed pre-1.1 store fixture into an isolated test store.

The fixture under ``tests/fixtures/pre_1_1_store`` is a captured copy of an
owner store built on pre-1.1 ``main`` (see its ``capture.py`` and
``manifest.json``).  Later phase-1.1 lanes read it to prove that old settings
still start the new code and print their migration notice once; this helper is
the one place that knows how to install it.

Restoring rewrites the capture machine's paths (``{python}``, ``{repo}``,
``{state}``, ``{config}``, ``{home}``, ``{root}``) and refreshes runtime
evidence that is expected to be stale: CLI readiness and quota readings have a
short freshness window, and a test host cannot inherit the capture host's.  The
*owner settings* the fixture exists for are never rewritten.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from lifecycle_rig import _install_shim, create_toy_repo

from saberops.db import Database

FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "pre_1_1_store"

#: Runtime evidence older than this is refreshed on restore.
EVIDENCE_HOURS = 4
#: Remaining quota for each captured pool after the refresh.
REFRESHED_PERCENT = 80


@dataclass(frozen=True)
class Pre11Store:
    """An isolated store restored from the fixture plus a fresh toy repository."""

    root: Path
    home_dir: Path
    state_home: Path
    config_home: Path
    cache_home: Path
    data_home: Path
    bin_dir: Path
    repo: Path
    base_sha: str
    orchestrator_binding_id: str
    cline_route: str
    old_views: tuple[str, ...]
    old_recovery_verbs: tuple[str, ...]

    @property
    def state_dir(self) -> Path:
        return self.state_home / "saberops"

    @property
    def config_dir(self) -> Path:
        return self.config_home / "saberops"

    def db(self) -> Database:
        return Database()


def _fixture_json(name: str) -> object:
    return json.loads((FIXTURE_DIR / name).read_text(encoding="utf-8"))


def _tokens(
    store_root: Path, repo: Path, state_home: Path, config_home: Path, home: Path
) -> dict[str, str]:
    return {
        "{root}": str(store_root),
        "{repo}": str(repo),
        "{state}": str(state_home / "saberops"),
        "{config}": str(config_home / "saberops"),
        "{home}": str(home),
        "{python}": sys.executable,
    }


def _rewrite(payload: object, tokens: dict[str, str]) -> object:
    if isinstance(payload, str):
        for token, value in tokens.items():
            payload = payload.replace(token, value)
        return payload
    if isinstance(payload, list):
        return [_rewrite(item, tokens) for item in payload]
    if isinstance(payload, dict):
        return {key: _rewrite(value, tokens) for key, value in payload.items()}
    return payload


def _write_json(target: Path, payload: object) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _refresh_runtime_evidence(store: Pre11Store) -> None:
    """Refresh the CLI readiness and quota evidence a job needs to dispatch."""
    from saberops.access_ops import test_access_connection, verify_access_model

    # The same public setup calls the lifecycle rig uses.  OpenCode's status
    # probe is the model verification (the ``models`` command), not
    # ``test_access_connection``: the fake CLI's ``opencode auth list`` path
    # falls through to a worker run and commits in the caller's directory.
    test_access_connection("codex-chatgpt-account")
    test_access_connection("claude-code-subscription")
    verify_access_model("opencode-account", "opencode-go/deepseek-v4-flash")

    import sqlite3

    from saberops.cli import main as cli_main

    reset_at = (datetime.now(UTC) + timedelta(hours=EVIDENCE_HOURS)).isoformat()
    connection = sqlite3.connect(store.state_dir / "orchestrator.db")
    try:
        pools = connection.execute(
            "select provider, pool from quota_state order by provider"
        ).fetchall()
    finally:
        connection.close()
    for provider, pool in pools:
        assert cli_main(
            [
                "quota",
                "set",
                provider,
                pool,
                "--remaining",
                str(REFRESHED_PERCENT),
                "--reset-at",
                reset_at,
            ]
        ) == 0


def restore_pre_1_1_store(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    refresh_evidence: bool = True,
) -> Pre11Store:
    """Install the captured pre-1.1 store into a fresh isolated environment."""
    home_dir = tmp_path / "home"
    state_home = tmp_path / "state"
    config_home = tmp_path / "config"
    cache_home = tmp_path / "cache"
    data_home = tmp_path / "data"
    bin_dir = tmp_path / "bin"
    for directory in (home_dir, state_home, config_home, cache_home, data_home, bin_dir):
        directory.mkdir(parents=True, exist_ok=True)

    monkeypatch.setenv("HOME", str(home_dir))
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_home))
    monkeypatch.setenv("XDG_DATA_HOME", str(data_home))
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    # The host disk floor belongs to the test host, not to the restored store.
    monkeypatch.setenv("SABEROPS_GATE_MIN_FREE_GB", "0")
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    for var in (
        "SABEROPS_PROFILE",
        "ORCH_ACCESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_BINDING_STORE",
        "ORCH_READINESS_STORE",
        "ORCH_EXECUTABLE",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "ORCH_FAKE_WORKER_MODE",
        "ORCH_FAKE_WORKER_HANG_SECONDS",
        "ORCH_FAKE_WORKER_EXIT_CODE",
        "ORCH_FAKE_WORKER_FILE",
        "ORCH_FAKE_WORKER_CONTENT",
        "ORCH_FAKE_REVIEWER_VERDICT",
    ):
        monkeypatch.delenv(var, raising=False)

    state_dir = state_home / "saberops"
    state_dir.mkdir(parents=True, exist_ok=True)
    for cli_name in ("codex", "opencode", "claude"):
        _install_shim(bin_dir, cli_name, state_dir)
    # The fixture is a fake-mode store: the shims read these controls.
    (state_dir / "fake-worker-mode.json").write_text(
        json.dumps({"mode": "commit"}), encoding="utf-8"
    )
    (state_dir / "fake-reviewer-mode.json").write_text(
        json.dumps({"verdict": "PASS"}), encoding="utf-8"
    )

    repo, base_sha = create_toy_repo(tmp_path)
    tokens = _tokens(tmp_path, repo, state_home, config_home, home_dir)

    # 1. Config documents: authority preset, bindings (with the orchestrator
    #    policy and reviewer), control plane and routing (naming Cline).
    for name in ("authority.json", "bindings.json", "control_plane.json", "routing.json"):
        payload = _rewrite(_fixture_json(f"config/saberops/{name}"), tokens)
        _write_json(config_home / "saberops" / name, payload)

    # 2. State documents and the captured database (old schema; opening it runs
    #    whatever migrations the current code has).
    for name in ("access-models.json", "access-readiness.json"):
        payload = _rewrite(_fixture_json(f"state/saberops/{name}"), tokens)
        _write_json(state_dir / name, payload)
    (state_dir / "orchestrator.db").write_bytes(
        (FIXTURE_DIR / "state/saberops/orchestrator.db").read_bytes()
    )

    # 3. Project settings: the project id derives from this repository's path,
    #    so the captured per-project documents move to the new state directory.
    from saberops.project import bind_project_state_dir, compute_project_identity

    identity = compute_project_identity(repo)
    project_dir = bind_project_state_dir(identity)
    for name in ("project-settings.json", "review-policy.json"):
        payload = _rewrite(_fixture_json(f"project/{name}"), tokens)
        _write_json(project_dir / name, payload)

    manifest = _fixture_json("manifest.json")
    surfaces = _fixture_json("surfaces.json")
    assert isinstance(manifest, dict) and isinstance(surfaces, dict)
    store = Pre11Store(
        root=tmp_path,
        home_dir=home_dir,
        state_home=state_home,
        config_home=config_home,
        cache_home=cache_home,
        data_home=data_home,
        bin_dir=bin_dir,
        repo=repo,
        base_sha=base_sha,
        orchestrator_binding_id=str(manifest["contains"]["orchestrator_binding"]),
        cline_route=str(manifest["contains"]["cline_route"]),
        old_views=tuple(str(item) for item in surfaces["old_views"]),
        old_recovery_verbs=tuple(str(item) for item in surfaces["old_recovery_verbs"]),
    )
    if refresh_evidence:
        _refresh_runtime_evidence(store)
    return store
