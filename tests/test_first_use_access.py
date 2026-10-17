"""First-use checks break the UNKNOWN/first-success cycle without launching tasks."""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from saberops.model_access import (
    AccessStore,
    DiscoveredModel,
    ExecutionSupport,
    ModelCatalogStore,
    ReadinessService,
    ReadinessStore,
    build_access_registry,
    build_model_catalog,
    default_account_connections,
)
from saberops.model_access.model_verification import verify_native_model
from saberops.model_access.readiness import ReadinessEvidenceSource
from saberops.models import ProviderReadiness
from saberops.process import ProcessResult


@pytest.mark.parametrize("backend", ["opencode", "antigravity"])
@pytest.mark.parametrize("outcome", ["success", "empty", "failure", "timeout", "error", "partial"])
def test_model_check_requires_completed_challenge_and_denies_tools(
    tmp_path: Path, backend: str, outcome: str,
) -> None:
    """Covers: HV-2 isolation contract (opencode check cwd + deny inline config)."""

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        directory = Path(kwargs["cwd"])
        assert directory != tmp_path and directory.is_dir()
        assert kwargs["timeout"] == 45 and kwargs["input_text"] == ""
        assert argv[argv.index("--model") + 1] == "chosen-model"
        assert "--dangerously-skip-permissions" not in argv and "--auto" not in argv
        challenge = re.search(r"SABEROPS_ACCESS_[a-f0-9]+", " ".join(argv))
        assert challenge is not None
        if backend == "opencode":
            config = json.loads(kwargs["env"]["OPENCODE_CONFIG_CONTENT"])
            agent = config["agent"][argv[argv.index("--agent") + 1]]
            assert agent["permission"] == "deny" and agent["steps"] == 2
            # v2.0.29 documents neither ``--pure`` nor ``--dir`` (HV-1 F1,
            # fixed in Step 024/HV-2): the directory is the child cwd, ancestor
            # discovery is disabled, and isolation is the owned private
            # config/home root plus these deny-by-default permissions.
            assert "--pure" not in argv and "--dir" not in argv
            assert config["permission"] == "deny"
            assert kwargs["inherit_env"] is False
            assert kwargs["env"]["OPENCODE_DISABLE_PROJECT_CONFIG"] == "1"
            assert "OPENCODE_CONFIG" not in kwargs["env"]
            assert "OPENCODE_CONFIG_DIR" not in kwargs["env"]
            root = Path(kwargs["env"]["XDG_CONFIG_HOME"]).parent
            assert "saberops-opencode-isolation-" in str(root)
            assert Path(kwargs["env"]["HOME"]).parent == root
            assert {"action": "integration.use", "resource": "plugin:*",
                    "effect": "deny"} in config["policies"]
            output = json.dumps({"type": "text", "part": {"text": challenge[0]}})
            if outcome != "partial":
                output += '\n' + json.dumps({"type": "step_finish", "part": {"reason": "stop"}})
            if outcome == "error":
                output += '\n' + json.dumps({"type": "error", "secret": "never-save"})
        else:
            definition = next((directory / ".agents/agents").glob("*.md")).read_text()
            assert "tools: []" in definition and "commandExecutionPolicy: off" in definition
            assert "--sandbox" in argv and argv[argv.index("--mode") + 1] == "plan"
            # Antigravity keeps its pre-fixup contract: no inherit_env
            # override, so run_process applies its default (inherit_env=True)
            # and the login session resolves exactly as before.
            assert "inherit_env" not in kwargs
            assert kwargs["env"] == {}
            output = json.dumps({"status": "ERROR" if outcome == "error" else "SUCCESS",
                                 "response": challenge[0], "secret": "never-save"})
            if outcome == "partial":
                output = json.dumps({"type": "assistant", "result": challenge[0]})
        return ProcessResult(argv, 1 if outcome == "failure" else 0,
                             "" if outcome == "empty" else output, "private-cli-error", 0.01,
                             timed_out=outcome == "timeout")

    reason = verify_native_model("/fake/cli", backend, "chosen-model", runner=runner)
    assert reason == {
        "success": "READY", "empty": "MODEL_CHECK_UNCONFIRMED",
        "failure": "MODEL_CHECK_FAILED", "timeout": "MODEL_CHECK_TIMEOUT",
        "error": "MODEL_CHECK_UNCONFIRMED", "partial": "MODEL_CHECK_UNCONFIRMED",
    }[outcome]


def test_opencode_model_check_refuses_without_inheriting_ambient_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-2 first-use refusal (isolation unavailable, no spawn)."""
    import saberops.model_access.model_verification as verification
    from saberops.workers.opencode_isolation import OpenCodeIsolationUnavailable

    monkeypatch.setenv("OPENCODE_CONFIG", "/tmp/hostile.json")
    monkeypatch.setenv("OPENCODE_CONFIG_DIR", "/tmp/hostile-dir")
    monkeypatch.setenv("OPENCODE_DISABLE_PROJECT_CONFIG", "0")

    def fail(_env: object) -> object:
        raise OpenCodeIsolationUnavailable(
            "OpenCode system configuration is present"
        )

    monkeypatch.setattr(verification, "opencode_isolation", fail)
    calls: list[list[str]] = []

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        calls.append(argv)
        raise AssertionError("must not launch when isolation is unavailable")

    assert (
        verify_native_model("/fake/cli", "opencode", "chosen-model", runner=runner)
        == "MODEL_CHECK_FAILED"
    )
    assert calls == []
    assert tmp_path.is_dir()


def test_antigravity_model_check_keeps_inherited_login_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: HV-2 antigravity contract (effective inheritance, no vendor run)."""
    import os

    # Synthetic login-session prerequisites: visible to the real runner
    # through run_process's default inheritance (no inherit_env override).
    monkeypatch.setenv("SABEROPS_LOGIN_CANARY", "present-for-inheritance")
    monkeypatch.setenv(
        "PATH", "/synthetic-bin" + os.pathsep + os.environ.get("PATH", "")
    )

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        assert argv[0] == "/fake/agy" and "--sandbox" in argv
        # Pre-fixup contract restored: kwargs carries no inherit_env key, so
        # run_process applies its default (True); the env overlay keeps its
        # original empty shape.
        assert "inherit_env" not in kwargs
        assert kwargs["env"] == {}
        assert os.environ["SABEROPS_LOGIN_CANARY"] == "present-for-inheritance"
        assert os.environ["PATH"].split(os.pathsep)[0] == "/synthetic-bin"
        assert Path(kwargs["cwd"]) != tmp_path and Path(kwargs["cwd"]).is_dir()
        return ProcessResult(argv, 0, "", "", 0.01)

    assert (
        verify_native_model("/fake/agy", "antigravity", "m", runner=runner)
        == "MODEL_CHECK_UNCONFIRMED"
    )


def test_fresh_account_can_verify_before_any_task_and_survives_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = build_access_registry(default_account_connections())
    connection = registry.get("opencode-account")
    store = ReadinessStore(tmp_path / "readiness.json")
    service = ReadinessService(registry, store, which=lambda _: "/fake/cli")
    assert service.refresh(connection).state == ProviderReadiness.UNKNOWN
    calls: list[tuple[str, str, str]] = []

    def verify(executable: str, backend: str, model: str) -> str:
        calls.append((executable, backend, model))
        return "READY"

    monkeypatch.setattr("saberops.model_access.model_verification.verify_native_model", verify)
    evidence = service.verify_model(connection, "upstream/exact-model")
    assert evidence.model_id == "upstream/exact-model"
    assert evidence.source == ReadinessEvidenceSource.MODEL_ACCESS_TEST
    assert calls == [("/fake/cli", "opencode", "upstream/exact-model")]
    assert store.load().try_get(connection.connection_id) == evidence
    assert service.refresh(connection) == evidence
    assert service.admission_for(provider="opencode").state == ProviderReadiness.READY
    # A failed second upstream test must not overwrite the prior success.
    monkeypatch.setattr("saberops.model_access.model_verification.verify_native_model",
                        lambda *_: "MODEL_CHECK_FAILED")
    assert service.verify_model(connection, "other/model").state == ProviderReadiness.UNKNOWN
    assert store.load().try_get(connection.connection_id) == evidence
    with pytest.raises(ValueError):
        service.verify_model(replace(connection, enabled=False), "upstream/exact-model")


def test_first_use_requires_exact_current_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First-use verification needs an exact current catalog model.

    L-A step 5 removed the access page; this drives the same engine seam
    the removed verify-model route used (exact-model catalog gate +
    ReadinessService.verify_model with sanitized persistence).
    """
    from saberops.model_access import ReadinessService
    from saberops.web import _access_store, _readiness_store

    for name in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(name, str(tmp_path / name))
    for name in ("SABEROPS_PROFILE", "ORCH_BINDING_STORE"):
        monkeypatch.delenv(name, raising=False)
    paths = {"ORCH_ACCESS_STORE": tmp_path / "access.json",
             "ORCH_ACCESS_MODELS_STORE": tmp_path / "models.json",
             "ORCH_READINESS_STORE": tmp_path / "readiness.json"}
    for key, path in paths.items():
        monkeypatch.setenv(key, str(path))
    AccessStore(paths["ORCH_ACCESS_STORE"]).save(build_access_registry(default_account_connections()))
    catalog = build_model_catalog([
        DiscoveredModel("opencode-account", "opencode", "opencode", "upstream/model",
                        execution_support=ExecutionSupport.SUPPORTED),
        DiscoveredModel("opencode-account", "opencode", "opencode", "stale/model",
                        execution_support=ExecutionSupport.SUPPORTED, stale=True),
        DiscoveredModel("antigravity-google-account", "antigravity", "antigravity", "other/model",
                        execution_support=ExecutionSupport.SUPPORTED),
    ])
    ModelCatalogStore(paths["ORCH_ACCESS_MODELS_STORE"]).save(catalog)
    calls: list[str] = []

    def verify(_executable: str, _backend: str, model: str) -> str:
        calls.append(model)
        return "READY"

    def current_model(connection_id: str, model_id: str) -> str:
        registry = _access_store().load()
        connection = registry.try_get(connection_id)
        assert connection is not None
        from saberops.web import _load_catalog_safely

        model = next((m for m in _load_catalog_safely().for_connection(connection_id)
                      if m.model_id == model_id and m.is_execution_supported
                      and not m.is_stale and m.provider == connection.provider
                      and m.backend == connection.backend), None)
        if model is None:
            raise ValueError("Choose a current model from this provider's catalog.")
        return model.model_id

    for model in ("", "invented/model", "other/model", "stale/model"):
        with pytest.raises((ValueError, AssertionError)):
            current_model("opencode-account", model)
    assert not calls
    monkeypatch.setattr("saberops.model_access.model_verification.verify_native_model", verify)
    monkeypatch.setattr("shutil.which", lambda _: "/fake/cli")
    registry = _access_store().load()
    connection = registry.try_get("opencode-account")
    assert connection is not None
    service = ReadinessService(registry, _readiness_store(), which=shutil.which)
    evidence = service.verify_model(connection, current_model("opencode-account", "upstream/model"))
    assert evidence.state == ProviderReadiness.READY
    assert calls == ["upstream/model"]
    assert "private" not in paths["ORCH_READINESS_STORE"].read_text()
    stored = ReadinessStore(paths["ORCH_READINESS_STORE"]).load().try_get("opencode-account")
    assert stored is not None and stored.model_id == "upstream/model"
    assert stored.state == ProviderReadiness.READY
