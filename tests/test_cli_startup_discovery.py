"""Fresh-launch CLI discovery uses temporary installations, never real providers."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from saberops.cli import handle_ui
from saberops.cli_environment import prepare_cli_environment
from saberops.model_access import (
    AccessStore,
    ReadinessService,
    ReadinessStore,
    build_access_registry,
    default_account_connections,
)
from saberops.models import ProviderReadiness
from saberops.paths import get_user_routing_path, owner_state_dir
from saberops.web import _list_account_models, create_app, initialize_account_readiness


@pytest.fixture()
def owner_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for key in (
        "ORCH_ACCESS_STORE",
        "ORCH_READINESS_STORE",
        "ORCH_ACCESS_MODELS_STORE",
        "ORCH_BINDING_STORE",
        "NVM_DIR",
        "NVM_BIN",
        "FNM_MULTISHELL_PATH",
        "APPDATA",
    ):
        monkeypatch.delenv(key, raising=False)
    # The launcher writes the selected profile directly. Track even an absent
    # original key so its launch-time choice cannot leak to subsequent tests.
    monkeypatch.setenv("SABEROPS_PROFILE", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(tmp_path / "system-bin"))
    for name in ("STATE", "CONFIG", "CACHE"):
        monkeypatch.setenv(f"XDG_{name}_HOME", str(tmp_path / name.lower()))
    root = tmp_path / "owner"
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(root / AccessStore.FILENAME))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(root / ReadinessStore.FILENAME))
    return root


def _executable(path: Path, body: str = "raise SystemExit(99)\n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"#!{sys.executable}\n{body}", encoding="utf-8")
    path.chmod(0o755)


def test_discovery_finds_user_bins_and_runtime_preserving_path(
    owner_state: Path, tmp_path: Path
) -> None:
    home = Path.home()
    paths = [
        home / ".local/bin/codex",
        home / ".opencode/bin/opencode",
        home / ".nvm/versions/node/v9.0.0/bin/node",
        home / ".nvm/versions/node/v24.20.0/bin/node",
        home / ".nvm/versions/node/v24.20.0/bin/cline",
    ]
    for path in paths:
        _executable(path)
    original = os.environ["PATH"]
    prepare_cli_environment()
    assert os.environ["PATH"].split(os.pathsep)[0] == original
    for name in ("codex", "opencode", "node", "cline"):
        assert shutil.which(name) is not None
    discovered_paths = os.environ["PATH"].split(os.pathsep)
    assert discovered_paths.index(str(paths[3].parent)) < discovered_paths.index(
        str(paths[2].parent)
    )
    first = os.environ["PATH"]
    prepare_cli_environment()
    assert os.environ["PATH"] == first
    _executable(tmp_path / "system-bin/codex")
    assert shutil.which("codex") == str(tmp_path / "system-bin/codex")


def test_fresh_ui_launch_detects_and_verifies_without_model_calls(
    owner_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home = Path.home()
    for name, argv, payload, code in (
        (
            "codex",
            ["doctor", "--json"],
            {"checks": {"auth.credentials": {"status": "ok"}}, "secret": "never-persist"},
            0,
        ),
        ("claude", ["auth", "status", "--json"], {"loggedIn": False}, 1),
    ):
        _executable(
            home / ".local/bin" / name,
            "import sys\n"
            f"assert sys.argv[1:] == {argv!r}\n"
            f"print({json.dumps(payload)!r})\n"
            f"raise SystemExit({code})\n",
        )
    # Any invocation of these CLIs would write a marker. Installation checks
    # must never invoke an unverified prompt or a guessed auth command.
    marker = tmp_path / "unexpected-command"
    for path in (home / ".opencode/bin/opencode", home / ".local/bin/agy"):
        _executable(path, f"open({str(marker)!r}, 'w').write('invoked')\n")
    apps: list[FastAPI] = []

    def serve(app: FastAPI, **kwargs: Any) -> None:
        apps.append(app)

    monkeypatch.setattr("uvicorn.run", serve)
    # Exercise the actual profile namespace rather than injected store paths.
    monkeypatch.delenv("ORCH_ACCESS_STORE")
    monkeypatch.delenv("ORCH_READINESS_STORE")
    handle_ui(
        argparse.Namespace(
            db=str(tmp_path / "web.db"), repo=None, host="127.0.0.1", port=8765, profile="new-user"
        )
    )
    assert os.environ["SABEROPS_PROFILE"] == "new-user"
    owner_state = owner_state_dir()
    assert owner_state == tmp_path / "state/saberops/profiles/new-user"
    assert not (tmp_path / "state/saberops" / ReadinessStore.FILENAME).exists()
    assert not get_user_routing_path().exists()
    assert not marker.exists()
    store = ReadinessStore(owner_state / ReadinessStore.FILENAME)
    evidence = {row.connection_id: row for row in store.load().evidence}
    assert evidence["codex-chatgpt-account"].state == ProviderReadiness.READY
    assert evidence["claude-code-subscription"].reason == "NOT_AUTHENTICATED"
    for connection_id in ("opencode-account", "antigravity-google-account"):
        assert evidence[connection_id].state == ProviderReadiness.UNKNOWN
    assert "never-persist" not in (owner_state / ReadinessStore.FILENAME).read_text(
        encoding="utf-8"
    )
    assert not (owner_state / AccessStore.FILENAME).exists()
    page = TestClient(apps[0]).get("/access")
    assert page.status_code == 200
    assert 'data-od-id="provider-antigravity-google-account"' in page.text
    assert "Its credential list shows configuration" in page.text
    assert "successful run" in page.text


@pytest.mark.parametrize("failure", ["timeout", "invalid-json", "nonzero"])
def test_native_status_failure_stays_unknown_and_is_bounded(
    owner_state: Path, failure: str
) -> None:
    registry = build_access_registry(default_account_connections())

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert argv == ["codex", "doctor", "--json"]
        assert kwargs["timeout"] == 8
        if failure == "timeout":
            raise subprocess.TimeoutExpired(argv, 8, output="sensitive-output")
        return subprocess.CompletedProcess(
            argv,
            1 if failure == "nonzero" else 0,
            "not-json"
            if failure == "invalid-json"
            else '{"checks":{"auth.credentials":{"status":"ok"}}}',
        )

    store = ReadinessStore(owner_state / ReadinessStore.FILENAME)
    service = ReadinessService(registry, store, which=lambda _: "/fake/codex", run=run)
    result = service.refresh(registry.get("codex-chatgpt-account"))
    assert result.state == ProviderReadiness.UNKNOWN
    assert "sensitive-output" not in (owner_state / ReadinessStore.FILENAME).read_text(
        encoding="utf-8"
    )


def test_startup_retains_execution_evidence_and_excludes_api_connections(
    owner_state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.model_access import AccessProtocol, AuthMode, ProviderConnection

    api = ProviderConnection(
        connection_id="custom-api",
        provider="openai",
        backend="openai-compatible-api",
        protocol=AccessProtocol.OPENAI_COMPATIBLE,
        auth_mode=AuthMode.API_KEY,
        endpoint="https://example.invalid",
        credential_ref="env:DO_NOT_READ",
    )
    registry = build_access_registry(default_account_connections() + (api,))
    AccessStore(owner_state / AccessStore.FILENAME).save(registry)
    store = ReadinessStore(owner_state / ReadinessStore.FILENAME)
    service = ReadinessService(registry, store)
    service.record_success("opencode-account")
    before = store.load().try_get("opencode-account")
    _executable(Path(os.environ["PATH"]) / "opencode")
    initialize_account_readiness()
    assert store.load().try_get("opencode-account") == before
    assert store.load().try_get("custom-api") is None


def test_antigravity_model_discovery_preserves_native_ids_without_verifying_login(
    owner_state: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("shutil.which", lambda name: "/fake/agy" if name == "agy" else None)

    def listing(argv: list[str]) -> str:
        assert argv == ["/fake/agy", "models"]
        return "Fetching models...\ngemini-pro-high\tGemini Pro (High)\nclaude-low\tClaude (Low)\n"

    monkeypatch.setattr("saberops.web._run_backend_listing", listing)
    registry = AccessStore(owner_state / AccessStore.FILENAME).load()
    assert _list_account_models(registry.get("antigravity-google-account")) == [
        "gemini-pro-high",
        "claude-low",
    ]
    client = TestClient(create_app(db_path=tmp_path / "web.db", seed_cwd_if_empty=False))
    response = client.post(
        "/access/discover",
        data={
            "connection_id": "antigravity-google-account",
        },
    )
    assert response.status_code == 200
    assert "gemini-pro-high" in response.text
    assert "claude-low" in response.text
    assert (
        ReadinessStore(owner_state / ReadinessStore.FILENAME)
        .load()
        .try_get("antigravity-google-account")
        is None
    )


@pytest.mark.parametrize("change", ["disabled", "removed", "expired"])
def test_retained_execution_evidence_cannot_bypass_admission(
    owner_state: Path, change: str
) -> None:
    registry = build_access_registry(default_account_connections())
    store = ReadinessStore(owner_state / ReadinessStore.FILENAME)
    service = ReadinessService(
        registry,
        store,
        which=lambda _: None if change == "removed" else "/fake/opencode",
        now=(lambda: "2020-01-01T00:00:00Z")
        if change == "expired"
        else (lambda: "2026-10-04T00:00:00Z"),
    )
    service.record_success("opencode-account")
    connection = registry.get("opencode-account")
    if change == "disabled":
        connection = replace(connection, enabled=False)
    service.refresh(connection)
    if change == "expired":
        admission = service.admission_for(provider="opencode")
        assert admission.state == ProviderReadiness.UNKNOWN
        assert admission.reason == "STALE_READINESS_EVIDENCE"
    else:
        evidence = store.load().try_get("opencode-account")
        assert evidence is not None
        assert evidence.state == ProviderReadiness.NOT_READY
        assert evidence.reason == (
            "CONNECTION_DISABLED" if change == "disabled" else "BACKEND_NOT_INSTALLED"
        )
