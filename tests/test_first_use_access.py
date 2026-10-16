"""First-use checks break the UNKNOWN/first-success cycle without launching tasks."""

from __future__ import annotations

import json
import re
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from saberops.model_access import (
    ReadinessService,
    ReadinessStore,
    build_access_registry,
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


