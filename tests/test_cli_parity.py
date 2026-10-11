"""CLI parity for owner operations removed from the console (T-007).

Every command here restores a write path the console cut removed; each
goes through ONE service-layer function (``OrchestratorService`` or
``saberops.access_ops``) and the CLI stays a thin wrapper.  Tests assert
behaviour (owner authority, validation, fail-closed refusal, ``--json``),
never HTML.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path

import pytest

from saberops.cli import main
from saberops.config import resolve_run_config
from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    load_owner_bindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.db import Database, current_iso_timestamp
from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier
from saberops.supervisor import RunSupervisor

_PROVIDER = "opencode"
_MODEL = "t7-parity-model"
_CONNECTION = "opencode-account"


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(tmp_path / "bindings.json"))
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(tmp_path / "models.json"))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(tmp_path / "readiness.json"))
    return tmp_path


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _seed_three_role_bindings() -> tuple[str, str, str]:
    owner = OwnerBindings()
    worker, profile, pool = materialize_discovered_binding(
        connection_id=_CONNECTION,
        provider=_PROVIDER,
        model=_MODEL,
        binding_role=BindingRole.WORKER,
    )
    reviewer, reviewer_profile, reviewer_pool = materialize_discovered_binding(
        connection_id=_CONNECTION,
        provider=_PROVIDER,
        model=_MODEL,
        binding_role=BindingRole.REVIEWER,
    )
    orch, orch_profile, orch_pool = materialize_discovered_binding(
        connection_id=_CONNECTION,
        provider=_PROVIDER,
        model=_MODEL,
        binding_role=BindingRole.ORCHESTRATOR,
    )
    owner = upsert_owner_binding(owner, binding=worker, profile=profile, pool=pool)
    owner = upsert_owner_binding(
        owner, binding=reviewer, profile=reviewer_profile, pool=reviewer_pool
    )
    owner = upsert_owner_binding(owner, binding=orch, profile=orch_profile, pool=orch_pool)
    save_owner_bindings(owner)
    return worker.binding_id, reviewer.binding_id, orch.binding_id


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "t7@example.com"], cwd=path, check=True
    )
    subprocess.run(["git", "config", "user.name", "T-007"], cwd=path, check=True)
    (path / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    (path / "README.md").write_text("# t7\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


# ---------------------------------------------------------------------------
# reviewer
# ---------------------------------------------------------------------------


def test_reviewer_show_unconfigured(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("reviewer", "show", capsys=capsys)
    assert code == 0
    assert "legacy review ladder" in out
    code, out, _ = _cli("reviewer", "show", "--json", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["selected_binding_id"] is None
    assert payload["eligible"] == []


def test_reviewer_select_persists_exact_binding(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, reviewer_id, _ = _seed_three_role_bindings()
    code, out, _ = _cli("reviewer", "select", reviewer_id, capsys=capsys)
    assert code == 0
    assert reviewer_id in out
    assert load_owner_bindings().reviewer_binding_id == reviewer_id
    code, out, _ = _cli("reviewer", "select", reviewer_id, "--json", capsys=capsys)
    assert code == 0
    assert json.loads(out)["selected_binding_id"] == reviewer_id
    code, out, _ = _cli("reviewer", "show", "--json", capsys=capsys)
    assert code == 0
    status = json.loads(out)
    assert status["status"] == "RESOLVED"
    assert status["selected_binding_id"] == reviewer_id
    assert any(row["binding_id"] == reviewer_id for row in status["eligible"])


def test_reviewer_select_lists_only_reviewer_bindings(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    worker_id, reviewer_id, orch_id = _seed_three_role_bindings()
    code, out, _ = _cli("reviewer", "show", "--json", capsys=capsys)
    assert code == 0
    eligible = [row["binding_id"] for row in json.loads(out)["eligible"]]
    assert reviewer_id in eligible
    assert worker_id not in eligible
    assert orch_id not in eligible


@pytest.mark.parametrize("slot", ["worker", "orchestrator"])
def test_reviewer_select_wrong_role_refused_without_mutation(
    isolated: Path, capsys: pytest.CaptureFixture[str], slot: str
) -> None:
    worker_id, reviewer_id, orch_id = _seed_three_role_bindings()
    wanted = worker_id if slot == "worker" else orch_id
    before = load_owner_bindings().to_dict()
    code, _, err = _cli("reviewer", "select", wanted, capsys=capsys)
    assert code == 1
    assert "not qualified for the Reviewer slot" in err
    assert load_owner_bindings().to_dict() == before
    assert load_owner_bindings().reviewer_binding_id != wanted


def test_reviewer_select_unknown_refused_without_mutation(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_three_role_bindings()
    before = load_owner_bindings().to_dict()
    code, _, err = _cli("reviewer", "select", "no-such-binding", capsys=capsys)
    assert code == 1
    assert "No such canonical binding" in err
    assert load_owner_bindings().to_dict() == before


def test_reviewer_select_leaves_other_roles_unchanged(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, reviewer_id, _ = _seed_three_role_bindings()
    before = load_owner_bindings()
    assert _cli("reviewer", "select", reviewer_id, capsys=capsys)[0] == 0
    after = load_owner_bindings()
    assert after.policy == before.policy
    assert after.registry.to_dict() == before.registry.to_dict()


def test_reviewer_clear_restores_legacy_ladder(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, reviewer_id, _ = _seed_three_role_bindings()
    assert _cli("reviewer", "select", reviewer_id, capsys=capsys)[0] == 0
    code, out, _ = _cli("reviewer", "clear", "--json", capsys=capsys)
    assert code == 0
    assert json.loads(out)["cleared"] is True
    assert load_owner_bindings().reviewer_binding_id is None


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


def test_orchestrator_show_unconfigured(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("orchestrator", "show", "--json", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["policy"] is None
    assert payload["status"] == "NOT_CONFIGURED"
    assert payload["eligible"] == []


def test_orchestrator_select_pinned_persists_exact_binding(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, orch_id = _seed_three_role_bindings()
    code, out, _ = _cli("orchestrator", "select", "--binding", orch_id, capsys=capsys)
    assert code == 0
    assert orch_id in out
    code, out, _ = _cli("orchestrator", "show", "--json", capsys=capsys)
    assert code == 0
    status = json.loads(out)
    assert status["policy"]["mode"] == "PINNED"
    assert status["status"] == "RESOLVED"
    assert status["resolved_binding"]["binding_id"] == orch_id


@pytest.mark.parametrize("slot", ["worker", "reviewer"])
def test_orchestrator_select_wrong_role_refused_without_mutation(
    isolated: Path, capsys: pytest.CaptureFixture[str], slot: str
) -> None:
    worker_id, reviewer_id, _ = _seed_three_role_bindings()
    wanted = worker_id if slot == "worker" else reviewer_id
    before = load_owner_bindings().to_dict()
    code, _, err = _cli("orchestrator", "select", "--binding", wanted, capsys=capsys)
    assert code == 1
    assert "not qualified for the Orchestrator slot" in err
    assert load_owner_bindings().to_dict() == before


def test_orchestrator_select_unknown_refused_without_mutation(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_three_role_bindings()
    before = load_owner_bindings().to_dict()
    code, _, err = _cli("orchestrator", "select", "--binding", "no-such", capsys=capsys)
    assert code == 1
    assert "No such canonical binding" in err
    assert load_owner_bindings().to_dict() == before


def test_orchestrator_select_auto_resolves(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, orch_id = _seed_three_role_bindings()
    code, out, _ = _cli("orchestrator", "select", "--auto", "--json", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["mode"] == "AUTO"
    assert payload["selected_binding_id"] == orch_id


def test_orchestrator_clear_removes_policy(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _, _, orch_id = _seed_three_role_bindings()
    assert _cli("orchestrator", "select", "--binding", orch_id, capsys=capsys)[0] == 0
    code, out, _ = _cli("orchestrator", "clear", "--json", capsys=capsys)
    assert code == 0
    assert json.loads(out)["cleared"] is True
    assert load_owner_bindings().policy is None


# ---------------------------------------------------------------------------
# access
# ---------------------------------------------------------------------------

_API_ID = "t7-local-api"


def _add_api_connection(
    capsys: pytest.CaptureFixture[str],
    connection_id: str = _API_ID,
    credential_ref: str = "env:T7_DEFINITELY_MISSING_KEY",
    endpoint: str = "http://127.0.0.1:1",
) -> tuple[int, str, str]:
    return _cli(
        "access",
        "add",
        "--id",
        connection_id,
        "--endpoint",
        endpoint,
        "--credential-ref",
        credential_ref,
        "--model",
        "t7-model",
        capsys=capsys,
    )


def test_access_list_shows_declared_connections_unverified(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli("access", "list", "--json", capsys=capsys)
    assert code == 0
    rows = {row["connection_id"]: row for row in json.loads(out)["connections"]}
    assert "codex-chatgpt-account" in rows
    assert rows["codex-chatgpt-account"]["readiness_state"] == "UNKNOWN"
    assert rows["codex-chatgpt-account"]["readiness_reason"] == "NO_EVIDENCE"


def test_access_add_saves_reference_only_and_never_invents_models(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, _ = _cli(
        "access",
        "add",
        "--id",
        _API_ID,
        "--endpoint",
        "http://127.0.0.1:1",
        "--credential-ref",
        "env:T7_DEFINITELY_MISSING_KEY",
        "--model",
        "t7-model",
        "--json",
        capsys=capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["connection_id"] == _API_ID
    assert payload["saved"] is True
    stored = json.loads((isolated / "access.json").read_text(encoding="utf-8"))
    blob = json.dumps(stored)
    assert "T7_DEFINITELY_MISSING_KEY" not in blob or "env:" in blob
    assert "no-such-secret-value" not in blob
    # The unreachable endpoint cannot enumerate: discovery reports the
    # classified failure and invents no catalog entries.
    assert payload["discovery"]["attempted"] is True
    assert payload["discovery"]["ok"] is False
    code, out, _ = _cli("access", "show", _API_ID, "--json", capsys=capsys)
    assert code == 0
    models = json.loads(out)["catalog_models"]
    # Only the owner-declared manual entry may exist; nothing enumerated.
    assert [entry["model_id"] for entry in models] == ["t7-model"]


def test_access_add_duplicate_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_api_connection(capsys)[0] == 0
    code, _, err = _add_api_connection(capsys)
    assert code == 1
    assert "already exists" in err


def test_access_add_invalid_headers_and_unknown_preset_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli(
        "access",
        "add",
        "--id",
        "t7-bad-headers",
        "--endpoint",
        "http://127.0.0.1:1",
        "--credential-ref",
        "env:T7_X",
        "--public-header",
        "no-colon-here",
        capsys=capsys,
    )
    assert code == 1
    assert "Invalid public headers" in err
    code, _, err = _cli(
        "access",
        "add",
        "--id",
        "t7-bad-preset",
        "--preset",
        "no-such-preset",
        capsys=capsys,
    )
    assert code == 1
    assert "not saved" in err.lower()


def test_access_add_redacts_credential_reference_from_errors(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli(
        "access",
        "add",
        "--id",
        "t7-redact",
        "--endpoint",
        "http://127.0.0.1:1",
        "--credential-ref",
        "env:T7_SECRET_SHAPED_HANDLE",
        "--public-header",
        "broken",
        capsys=capsys,
    )
    assert code == 1
    assert "T7_SECRET_SHAPED_HANDLE" not in err


def test_access_test_supported_api_without_credential_stays_unproven(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_api_connection(capsys)[0] == 0
    code, out, _ = _cli("access", "test", _API_ID, "--json", capsys=capsys)
    assert code == 0
    probe = json.loads(out)
    # The env credential is unset: metadata verification reports the
    # classified miss without transmitting anything and without
    # inventing readiness.
    assert probe["ok"] == "FALSE"
    assert probe["readiness_state"] in ("UNKNOWN", "NOT_READY")
    assert probe["readiness_reason"] == "CREDENTIAL_MISSING"


def test_access_test_probe_transmits_no_secret(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, _ = _cli(
        "access",
        "add",
        "--id",
        "t7-probe-only",
        "--endpoint",
        "http://127.0.0.1:1",
        "--credential-ref",
        "profile:t7-unreadable",
        "--json",
        capsys=capsys,
    )
    assert code == 0
    code, out, _ = _cli("access", "test", "t7-probe-only", "--json", capsys=capsys)
    assert code == 0
    probe = json.loads(out)
    # Unauthenticated reachability classification only; the probe never
    # resolves the credential handle (an unreadable reference is fine).
    assert probe["ok"] == "FALSE"
    assert probe["reason"] == "ENDPOINT_UNREACHABLE"


def test_access_test_unknown_connection_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli("access", "test", "no-such-connection", capsys=capsys)
    assert code == 1
    assert "Unknown connection" in err


def test_access_discover_unreachable_reports_classified_failure(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_api_connection(capsys)[0] == 0
    code, out, _ = _cli("access", "discover", _API_ID, "--json", capsys=capsys)
    assert code == 0
    result = json.loads(out)
    assert result["ok"] is False
    assert result["reason"] == "ENDPOINT_UNREACHABLE"
    # Failed discovery keeps prior evidence and invents nothing.
    code, out, _ = _cli("access", "show", _API_ID, "--json", capsys=capsys)
    assert code == 0
    models = json.loads(out)["catalog_models"]
    assert [entry["model_id"] for entry in models] == ["t7-model"]


def test_access_verify_model_requires_supported_connection_and_current_model(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _add_api_connection(capsys)[0] == 0
    code, _, err = _cli(
        "access", "verify-model", _API_ID, "--model", "t7-model", capsys=capsys
    )
    assert code == 1
    assert "supported account connection" in err
    code, _, err = _cli(
        "access", "verify-model", "codex-chatgpt-account", "--model", "no-such-model",
        capsys=capsys,
    )
    assert code == 1
    assert "supported account connection" in err or "current model" in err


# ---------------------------------------------------------------------------
# workflow presets
# ---------------------------------------------------------------------------


def test_project_set_workflow_quick_needs_no_gate(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "wf-quick")
    code, out, _ = _cli(
        "project", "set-workflow", "quick", "--repo", str(repo), "--json", capsys=capsys
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["preset"] == "quick"
    assert payload["gate_enabled"] is False


def test_project_set_workflow_non_quick_requires_gate(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "wf-balanced")
    (repo / "Makefile").unlink()
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "drop gate"], cwd=repo, check=True, capture_output=True)
    code, _, err = _cli(
        "project", "set-workflow", "balanced", "--repo", str(repo), capsys=capsys
    )
    assert code == 1
    assert "gate command" in err.lower()
    code, out, _ = _cli(
        "project",
        "set-workflow",
        "balanced",
        "--repo",
        str(repo),
        "--gate-command",
        "true",
        "--json",
        capsys=capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["preset"] == "balanced"
    assert payload["gate_effective"] == "true"


def test_project_set_workflow_custom_flags_and_invalid_limit(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "wf-custom")
    code, out, _ = _cli(
        "project",
        "set-workflow",
        "custom",
        "--repo",
        str(repo),
        "--gate-command",
        "true",
        "--gate-disabled",
        "--review",
        "always-required",
        "--limit",
        "7",
        "--json",
        capsys=capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["preset"] == "custom"
    assert payload["gate_enabled"] is False
    assert payload["review_limit"] == 7
    code, _, err = _cli(
        "project",
        "set-workflow",
        "custom",
        "--repo",
        str(repo),
        "--gate-command",
        "true",
        "--limit",
        "0",
        capsys=capsys,
    )
    assert code == 1
    assert "positive whole number" in err


def test_project_set_workflow_unknown_preset_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "wf-unknown")
    # Outside the preset vocabulary argparse itself refuses (exit 2).
    with pytest.raises(SystemExit) as exc:
        main(["project", "set-workflow", "turbo", "--repo", str(repo)])
    assert exc.value.code == 2


# ---------------------------------------------------------------------------
# run operations: retry / continue / try-worker
# ---------------------------------------------------------------------------


def _seed_run(
    db: Database,
    repo: Path,
    run_id: str,
    status: RunStatus,
    *,
    with_contracts: bool = False,
) -> Run:
    config = resolve_run_config(
        task="T-007 run-op probe",
        target_repo=repo,
        manual_tier=Tier.T1,
        routing_mode="manual",
        gate_command="true",
    )
    routing_json: str | None = None
    control_json: str | None = None
    control_digest: str | None = None
    tool_json: str | None = None
    tool_digest: str | None = None
    if with_contracts:
        from saberops.control_plane import (
            freeze_control_plane_policy,
            load_control_plane_policy,
        )
        from saberops.routing_config import load_effective_routing, snapshot_to_payload
        from saberops.tooling.contracts import freeze_tool_contract
        from saberops.tooling.registry import ToolRegistry

        created = current_iso_timestamp()
        routing_json = json.dumps(
            snapshot_to_payload(load_effective_routing(), created)
        )
        frozen_control = freeze_control_plane_policy(load_control_plane_policy())
        control_json = frozen_control.as_json()
        control_digest = frozen_control.digest
        frozen_tools = freeze_tool_contract((), ToolRegistry())
        tool_json = frozen_tools.to_json()
        tool_digest = frozen_tools.digest
    run = Run(
        id=run_id,
        task=config.task,
        target_repo=str(repo),
        base_commit=_head(repo),
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=status,
        created_at=current_iso_timestamp(),
        config_json=json.dumps(config.as_dict()),
        routing_snapshot_json=routing_json,
        control_plane_snapshot_json=control_json,
        control_plane_snapshot_digest=control_digest,
        tool_contract_json=tool_json,
        tool_contract_digest=tool_digest,
    )
    db.create_run(run)
    return run


def _stub_launch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    launches: list[str] = []
    monkeypatch.setattr(
        RunSupervisor, "_launch", lambda self, run_id, config: launches.append(run_id)
    )
    return launches


def test_retry_starts_fresh_attempt_with_frozen_policy(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "retry-repo")
    db_path = isolated / "retry.db"
    db = Database(db_path)
    _seed_run(db, repo, "run_retry_src", RunStatus.FAILED)
    launches = _stub_launch(monkeypatch)
    code, out, _ = _cli("retry", "run_retry_src", "--db", str(db_path), "--json", capsys=capsys)
    assert code == 0
    payload = json.loads(out)
    assert payload["operation"] == "retry"
    assert payload["source_run_id"] == "run_retry_src"
    assert launches == [payload["run_id"]]
    child = db.get_run(payload["run_id"])
    assert child is not None and child.config_json
    assert json.loads(child.config_json)["task"] == "T-007 run-op probe"
    assert json.loads(child.config_json)["gate_command"] == "true"


def test_retry_unknown_run_refused(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path = isolated / "retry-missing.db"
    code, _, err = _cli("retry", "run_missing", "--db", str(db_path), capsys=capsys)
    assert code == 1
    assert "Run not found" in err


def test_continue_starts_child_and_is_idempotent(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "continue-repo")
    db_path = isolated / "continue.db"
    db = Database(db_path)
    _seed_run(db, repo, "run_continue_src", RunStatus.FAILED, with_contracts=True)
    head = _head(repo)
    db.create_attempt(
        Attempt(
            id="run_continue_src-a1",
            run_id="run_continue_src",
            attempt_number=1,
            provider="codex",
            model="t7-model",
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.FAILED,
            commit_sha=head,
            created_at=current_iso_timestamp(),
        )
    )
    launches = _stub_launch(monkeypatch)
    code, out, _ = _cli(
        "continue", "run_continue_src", "--db", str(db_path), "--json", capsys=capsys
    )
    assert code == 0
    first = json.loads(out)
    assert first["operation"] == "continue"
    assert first["already_continued"] is False
    assert launches == [first["run_id"]]
    # Second request returns the existing continuation without launching.
    code, out, _ = _cli(
        "continue", "run_continue_src", "--db", str(db_path), "--json", capsys=capsys
    )
    assert code == 0
    second = json.loads(out)
    assert second["run_id"] == first["run_id"]
    assert second["already_continued"] is True
    assert launches == [first["run_id"]]


def test_continue_refused_for_non_failed_run(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "continue-done")
    db_path = isolated / "continue-done.db"
    db = Database(db_path)
    _seed_run(db, repo, "run_done", RunStatus.COMPLETED, with_contracts=True)
    _stub_launch(monkeypatch)
    code, _, err = _cli("continue", "run_done", "--db", str(db_path), capsys=capsys)
    assert code == 1
    assert "failed runs" in err.lower() or "cannot continue" in err.lower()


def test_try_worker_refusals(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "tryw-repo")
    db_path = isolated / "tryw.db"
    db = Database(db_path)
    _seed_run(db, repo, "run_tryw_src", RunStatus.FAILED)
    _seed_run(db, repo, "run_tryw_done", RunStatus.COMPLETED)
    _stub_launch(monkeypatch)
    code, _, err = _cli(
        "try-worker", "run_tryw_done", "--binding", "whatever", "--db", str(db_path),
        capsys=capsys,
    )
    assert code == 1
    assert "failed run" in err.lower()
    code, _, err = _cli(
        "try-worker", "run_tryw_src", "--binding", "owner-picked", "--db", str(db_path),
        capsys=capsys,
    )
    assert code == 1
    assert "no longer in the chosen routing list" in err


def test_try_worker_starts_exact_blocked_worker_and_keeps_policy(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import saberops.routing as routing_module
    from saberops.models import WorkerCandidate

    repo = _git_repo(isolated / "tryw-ok")
    db_path = isolated / "tryw-ok.db"
    db = Database(db_path)
    _seed_run(db, repo, "run_tryw_ok", RunStatus.FAILED)
    db.record_event(
        "run_tryw_ok",
        "run_failed",
        payload={
            "summary": "no eligible worker",
            "outcome": "NO_ELIGIBLE_WORKER",
            "routing_context": "T1.default",
            "skipped_candidates": [
                {
                    "provider": "opencode",
                    "model": "exact-model",
                    "reason": "provider_readiness_unknown",
                    "readiness_state": "UNKNOWN",
                    "binding_id": "owner-picked",
                }
            ],
        },
    )
    candidate = WorkerCandidate(
        provider="opencode", model="exact-model", binding_id="owner-picked"
    )
    monkeypatch.setattr(
        routing_module,
        "resolve_candidate_chain",
        lambda **kwargs: [replace(candidate)],
    )
    launches = _stub_launch(monkeypatch)
    code, out, _ = _cli(
        "try-worker",
        "run_tryw_ok",
        "--binding",
        "owner-picked",
        "--db",
        str(db_path),
        "--json",
        capsys=capsys,
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["operation"] == "try-worker"
    assert payload["binding_id"] == "owner-picked"
    assert launches == [payload["run_id"]]
    child = db.get_run(payload["run_id"])
    assert child is not None and child.config_json
    child_config = json.loads(child.config_json)
    assert child_config["task"] == "T-007 run-op probe"
    assert child_config["provider_override"] == "opencode"
    assert child_config["model_override"] == "exact-model"
    bootstrap = db.get_events_by_type(payload["run_id"], "owner_worker_bootstrap_requested")
    assert len(bootstrap) == 1
    assert bootstrap[0].payload is not None
    assert bootstrap[0].payload["source_run_id"] == "run_tryw_ok"
