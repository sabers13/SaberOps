"""H3: the engine has no in-engine Orchestrator model (ADR-0064).

A run may call a model only as worker, reviewer or advisor.  A store saved
before phase 1.1 that still selects an Orchestrator binding keeps working:
the selection is ignored and the owner sees one notice.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from saberops.cli import build_parser
from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    load_owner_bindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.db import Database
from saberops.model_access import (
    ReadinessService,
    ReadinessStore,
    build_access_registry,
    default_account_connections,
)
from saberops.models import AttemptStatus, WorkerRequest, WorkerResult
from saberops.process import run_process
from saberops.service import OrchestratorService
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

_PROVIDER = "opencode"
_CONNECTION = "opencode-account"
_MODEL = "opencode/muse-spark-1.2-contributor-free"


class _CountingWorker(WorkerAdapter):
    """Deterministic stand-in for the vendor CLI; counts every invocation."""

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[WorkerRequest] = []

    @property
    def provider_name(self) -> str:
        return _PROVIDER

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        self.calls += 1
        self.requests.append(request)
        if request.read_only:
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.SUCCESS,
                summary="Scripted reviewer approved",
                stdout="VERDICT: PASS\n",
                stderr="",
            )
        (request.worktree_path / "calc.py").write_text("def add(a, b): return a + b\n")
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="implemented add",
            stdout="done",
            stderr="",
            changed_paths=["calc.py"],
            has_changes=True,
        )


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    run_process(["git", "init", "-b", "main"], cwd=path)
    run_process(["git", "config", "user.name", "Test Runner"], cwd=path)
    run_process(["git", "config", "user.email", "test@localhost"], cwd=path)
    (path / "calc.py").write_text("# placeholder\n")
    run_process(["git", "add", "."], cwd=path)
    run_process(["git", "commit", "-m", "init"], cwd=path)


def _ready_readiness_service(tmp_path: Path) -> ReadinessService:
    service = ReadinessService(
        build_access_registry(default_account_connections()),
        ReadinessStore(tmp_path / "access-readiness.json"),
        which=lambda _name: "/fake/bin",
        max_age=timedelta(days=36500),
    )
    service.record_success(_CONNECTION)
    service.record_success("codex-chatgpt-account")
    return service


def _seed_verified_worker_posture(config_home: Path) -> None:
    """Allow the deterministic fake route through a synthetic Billing posture."""
    cfg_dir = config_home / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    (cfg_dir / "control_plane.json").write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {_PROVIDER: {"state": "ALLOWED"}},
                "quota_pools": {
                    "opencode-go": {"mode": "UNENFORCED", "overage_possible": False},
                    "opencode-zen": {"mode": "UNENFORCED", "overage_possible": False},
                },
                "postures": {
                    _PROVIDER: {
                        "class": "A",
                        "policy_urls": ["https://example.com/opencode-policy"],
                        "ttl_days": 90,
                        "verified_on": today_iso,
                        "note": "synthetic verified test posture for the fake worker",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _write_pre_phase_1_1_store(path: Path) -> str:
    """Write the exact store a pre-1.1 release saved with a pinned PLAN binding."""
    binding, profile, pool = materialize_discovered_binding(
        connection_id=_CONNECTION,
        provider=_PROVIDER,
        model=_MODEL,
        binding_role=BindingRole.PLAN,
    )
    payload = upsert_owner_binding(
        OwnerBindings(), binding=binding, profile=profile, pool=pool
    ).to_dict()
    payload["orch_binding_policy"] = {
        "mode": "PINNED",
        "pinned_binding_id": binding.binding_id,
        "preferred_binding_ids": [],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return binding.binding_id


def test_run_makes_no_model_call_outside_worker_reviewer_and_advisor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    # The gate's host memory lease is not this test's subject (the rig does the
    # same); keeping it off makes the run deterministic on a busy host.
    monkeypatch.setenv("SABEROPS_GATE_MEMORY_GUARD", "0")
    _seed_verified_worker_posture(tmp_path / "xdg-config")
    store = tmp_path / "bindings.json"
    _write_pre_phase_1_1_store(store)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(store))
    worker = _CountingWorker()
    repo = tmp_path / "repo"
    _init_repo(repo)
    db = Database(tmp_path / "orch.db")
    service = OrchestratorService(
        db=db,
        registry=AdapterRegistry([worker]),
        worktree_base=tmp_path / "worktrees",
        readiness_service=_ready_readiness_service(tmp_path),
    )

    result = service.run_sync(
        task="Add function",
        repo_path=repo,
        gate_command="true",
    )

    # The subject is which model calls happened, not the gate outcome: under
    # machine load the configured-gate step can fail on its own.
    assert result.run_id
    assert worker.calls >= 1
    assert not any("bounded advisory guidance" in request.task for request in worker.requests)
    guidance = db.get_events_by_type(result.run_id, "orchestrator_guidance_result")
    assert guidance == []


def test_pre_phase_1_1_orchestrator_selection_is_ignored_with_one_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = tmp_path / "bindings.json"
    binding_id = _write_pre_phase_1_1_store(store)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(store))

    owner = load_owner_bindings()
    assert [b.binding_id for b in owner.registry.list_bindings()] == [binding_id]
    first = capsys.readouterr().err
    assert "orchestrator" in first.lower()
    assert "ignored" in first.lower()

    # One notice per store, never one per load: the process is already told.
    assert load_owner_bindings().registry.list_bindings()[0].binding_id == binding_id
    assert capsys.readouterr().err == ""

    # The next write drops the retired selection instead of carrying it forward.
    save_owner_bindings(owner)
    saved = json.loads(store.read_text(encoding="utf-8"))
    assert "orch_binding_policy" not in saved


def test_orchestrator_owner_verb_is_gone() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["orchestrator", "show"])
    assert excinfo.value.code == 2
