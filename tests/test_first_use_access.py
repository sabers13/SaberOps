"""First-use checks break the UNKNOWN/first-success cycle without launching tasks."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

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
from saberops.web import create_app


@pytest.mark.parametrize("backend", ["opencode", "antigravity"])
@pytest.mark.parametrize("outcome", ["success", "empty", "failure", "timeout", "error", "partial"])
def test_model_check_requires_completed_challenge_and_denies_tools(
    tmp_path: Path, backend: str, outcome: str,
) -> None:
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
            assert "--pure" in argv
            output = json.dumps({"type": "text", "part": {"text": challenge[0]}})
            if outcome != "partial":
                output += '\n' + json.dumps({"type": "step_finish", "part": {"reason": "stop"}})
            if outcome == "error":
                output += '\n' + json.dumps({"type": "error", "secret": "never-save"})
        else:
            definition = next((directory / ".agents/agents").glob("*.md")).read_text()
            assert "tools: []" in definition and "commandExecutionPolicy: off" in definition
            assert "--sandbox" in argv and argv[argv.index("--mode") + 1] == "plan"
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


def test_web_first_use_requires_exact_current_model_and_explicit_local_post(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
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

    monkeypatch.setattr("saberops.model_access.model_verification.verify_native_model", verify)
    app = create_app(db_path=tmp_path / "runs.db")
    monkeypatch.setattr("shutil.which", lambda _: "/fake/cli")
    client = TestClient(app)
    page = client.get("/access")
    assert "Verify model access" in page.text and "Uses account allowance" in page.text
    assert "Check login" in page.text and "copilot login" not in page.text
    assert not calls and not paths["ORCH_READINESS_STORE"].exists()
    assert client.get("/access/verify-model").status_code == 405
    for model in ("", "invented/model", "other/model", "stale/model"):
        assert client.post("/access/verify-model", data={
            "connection_id": "opencode-account", "model_id": model,
        }).status_code == 400
    data = {"connection_id": "opencode-account", "model_id": "upstream/model"}
    assert client.post("/access/verify-model", data=data,
                       headers={"Origin": "https://evil.example"}).status_code == 403
    assert not calls
    response = client.post("/access/verify-model", data=data)
    assert response.status_code == 200
    assert "Access verified" in response.text and "upstream/model" in response.text
    assert calls == ["upstream/model"]
    assert "private" not in paths["ORCH_READINESS_STORE"].read_text()
    evidence = ReadinessStore(paths["ORCH_READINESS_STORE"]).load().try_get("opencode-account")
    assert evidence is not None and evidence.model_id == "upstream/model"
    assert evidence.state == ProviderReadiness.READY
