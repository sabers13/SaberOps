"""C7 tests: routing discovery, readiness remedy, connect path, effort, diversity, OpenCode."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

import saberops.review as review_mod
from saberops.agent_connect import connect
from saberops.capacity.loop import _default_run_launcher
from saberops.capacity.scheduler import DispatchDecision
from saberops.cli import main
from saberops.config import reconstruct_run_config
from saberops.control_plane import (
    BindingRole,
    load_owner_bindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.db import Database
from saberops.jobs.models import Effort, JobRequirements, JobRole
from saberops.jobs.service import submit_job
from saberops.model_access import (
    DiscoveredModel,
    ExecutionSupport,
    ModelCatalog,
    ProviderReadinessEvidence,
    ReadinessEvidenceSource,
    TriState,
)
from saberops.models import (
    ProviderReadiness,
    QuotaState,
    ReasoningEffort,
    RoutingState,
    WorkerCandidate,
    WorkerRequest,
)
from saberops.workers.opencode import OpenCodeAdapter


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "c7@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "C7"], cwd=path, check=True)
    (path / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    (path / "README.md").write_text("# c7\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "init"], cwd=path, check=True, capture_output=True)
    return path


@pytest.fixture()
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, sub in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        d = tmp_path / sub
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(d))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(tmp_path / "access-models.json"))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(tmp_path / "access-readiness.json"))
    monkeypatch.setenv("ORCH_BINDING_STORE", str(tmp_path / "bindings.json"))
    return tmp_path


def _seed_discovered_model(
    *,
    connection_id: str = "codex-acct",
    provider: str = "codex",
    backend: str = "codex",
    model_id: str = "gpt-6-luna",
) -> None:
    from saberops.access_ops import model_catalog_store

    store = model_catalog_store()
    existing = list(store.load().list_models())
    existing.append(
        DiscoveredModel(
            connection_id=connection_id,
            provider=provider,
            backend=backend,
            model_id=model_id,
            execution_support=ExecutionSupport.SUPPORTED,
        )
    )
    store.save(ModelCatalog(models=tuple(existing)))


def test_discovered_model_can_be_added_and_unknown_lists_valid_ids(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_discovered_model(provider="codex", backend="codex", model_id="gpt-6-luna")
    assert main(["routing", "init"]) == 0
    capsys.readouterr()

    code = main(["routing", "add", "worker", "codex/gpt-6-luna"])
    out = capsys.readouterr().out
    assert code == 0
    assert "codex/gpt-6-luna" in out

    bad_code = main(["routing", "add", "worker", "codex/nonexistent-xyz"])
    bad_err = capsys.readouterr().err
    assert bad_code == 1
    assert "valid ids:" in bad_err
    assert "codex/gpt-6-luna" in bad_err


def test_suggest_and_accept_default_route_in_one_command(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _seed_discovered_model(provider="codex", backend="codex", model_id="gpt-6-luna")
    assert main(["posture", "verify", "codex"]) == 0
    capsys.readouterr()

    assert main(["routing", "suggest", "--json"]) == 0
    suggest_out = json.loads(capsys.readouterr().out)
    assert [item["candidate_id"] for item in suggest_out["suggestions"]] == ["codex/gpt-6-luna"]

    assert main(["routing", "accept-default", "--json"]) == 0
    accept_out = json.loads(capsys.readouterr().out)
    assert accept_out["accepted"] == ["codex/gpt-6-luna"]
    assert any("codex/gpt-6-luna" in grp["routes"] for grp in accept_out["build_chain"])


def test_opencode_readiness_names_missing_evidence_and_remedy(
    isolated_env: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class _StubReadinessService:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def refresh(self, connection: Any) -> ProviderReadinessEvidence:
            return ProviderReadinessEvidence(
                connection_id=connection.connection_id,
                provider=connection.provider,
                backend=connection.backend,
                state=ProviderReadiness.UNKNOWN,
                reason="INSUFFICIENT_EVIDENCE",
                observed_at="2026-11-19T12:00:00+00:00",
                source=ReadinessEvidenceSource.CONNECTION_TEST,
            )

        def last_discovery(self, _cid: str) -> Any:
            class _Disc:
                installed = TriState.TRUE
                authenticated = TriState.UNKNOWN
                reachable = TriState.UNKNOWN

            return _Disc()

    monkeypatch.setattr("saberops.access_ops.ReadinessService", _StubReadinessService)
    code = main(["access", "test", "opencode-account"])
    out = capsys.readouterr().out
    assert code == 0
    assert "INSUFFICIENT_EVIDENCE" in out
    assert "authenticated" in out
    assert "reachable" in out
    assert "orch access discover opencode-account" in out
    assert "orch access test opencode-account" in out


def test_orch_connect_uses_running_orch_and_warns_on_conflicting_path(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    venv_bin = isolated_env / "venv" / "bin"
    global_bin = isolated_env / "global" / "bin"
    venv_bin.mkdir(parents=True)
    global_bin.mkdir(parents=True)

    venv_py = venv_bin / "python"
    venv_orch = venv_bin / "orch"
    global_orch = global_bin / "orch"
    for exe in (venv_py, venv_orch, global_orch):
        exe.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)

    monkeypatch.setattr(sys, "executable", str(venv_py))
    monkeypatch.setenv("PATH", f"{global_bin}{os.pathsep}{os.environ.get('PATH', '')}")

    res = connect("codex", dry_run=True, runner=lambda *_args: subprocess.CompletedProcess([], 0))
    assert res.ok is True
    assert str(venv_orch.resolve()) in res.commands[0]
    assert str(global_orch.resolve()) in res.message
    assert "Warning:" in res.message


def test_default_run_launcher_applies_supported_effort_and_refuses_unsupported(
    isolated_env: Path,
) -> None:
    repo = _git_repo(isolated_env / "effort-repo")
    db = Database(isolated_env / "orch.db")

    b_high, prof_high, pool_high = materialize_discovered_binding(
        connection_id="codex-acct",
        provider="codex",
        model="gpt-5.3-codex",
        binding_role=BindingRole.BUILD,
        reasoning_effort_capabilities=frozenset(
            {
                ReasoningEffort.LOW,
                ReasoningEffort.HIGH,
                ReasoningEffort.XHIGH,
            }
        ),
    )
    b_low_only, prof_low, pool_low = materialize_discovered_binding(
        connection_id="codex-acct",
        provider="codex",
        model="gpt-5.3-codex-mini",
        binding_role=BindingRole.BUILD,
        reasoning_effort_capabilities=frozenset({ReasoningEffort.LOW}),
    )
    owner = load_owner_bindings()
    owner = upsert_owner_binding(owner, binding=b_high, profile=prof_high, pool=pool_high)
    owner = upsert_owner_binding(owner, binding=b_low_only, profile=prof_low, pool=pool_low)
    save_owner_bindings(owner)

    job_xhigh = submit_job(
        db,
        repo=repo,
        goal="xhigh task",
        acceptance="true",
        requirements=JobRequirements(role=JobRole.BUILD, effort=Effort.xhigh),
    ).job
    dec_ok = DispatchDecision(
        job=job_xhigh,
        candidate=WorkerCandidate(
            provider="codex", model="gpt-5.3-codex", binding_id=b_high.binding_id
        ),
        pool_id=None,
        binding_window=None,
        score=1.0,
        cost_estimate=1.0,
    )
    _default_run_launcher(db, job_xhigh, dec_ok, "run_xhigh_ok")
    saved_run = db.get_run("run_xhigh_ok")
    assert saved_run is not None
    assert saved_run.config_json is not None
    assert reconstruct_run_config(json.loads(saved_run.config_json)).effort == "xhigh"

    dec_bad = DispatchDecision(
        job=job_xhigh,
        candidate=WorkerCandidate(
            provider="codex", model="gpt-5.3-codex-mini", binding_id=b_low_only.binding_id
        ),
        pool_id=None,
        binding_window=None,
        score=1.0,
        cost_estimate=1.0,
    )
    with pytest.raises(ValueError, match="EFFORT_UNSUPPORTED"):
        _default_run_launcher(db, job_xhigh, dec_bad, "run_xhigh_bad")


def test_reviewer_diversity_shared_and_unknown_families(isolated_env: Path) -> None:
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    quota_states = [
        QuotaState(
            provider=prov,
            pool=prov,
            remaining_percent=80.0,
            reset_at=(now + timedelta(hours=3)).isoformat(),
            routing_state=RoutingState.NORMAL,
            updated_at=now.isoformat(),
        )
        for prov in ("codex", "opencode")
    ]
    worker = WorkerCandidate(provider="codex", model="gpt-5.3-codex")
    shared_family_reviewer = WorkerCandidate(provider="opencode", model="openai/gpt-5.4")
    skip_fn = review_mod._pre_dispatch_skip_reason
    assert (
        skip_fn(
            candidate=shared_family_reviewer,
            adapter=None,
            training_allowed=False,
            quota_states=quota_states,
            prompt_bytes=100,
            worker_candidate=worker,
        )
        == "model_family_excluded"
    )

    unknown_family_worker = WorkerCandidate(provider="codex", model="gpt-6-luna")
    distinct_reviewer = WorkerCandidate(provider="opencode", model="opencode-go/mimo-v2.5")
    assert (
        skip_fn(
            candidate=distinct_reviewer,
            adapter=None,
            training_allowed=False,
            quota_states=quota_states,
            prompt_bytes=100,
            worker_candidate=unknown_family_worker,
        )
        != "model_family_excluded"
    )


def test_opencode_version_reported_vs_requested_and_poisoned_parallel_pins(
    isolated_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_bin = isolated_env / "bin" / "opencode"
    fake_bin.parent.mkdir(parents=True)
    fake_bin.write_text(
        """#!/usr/bin/env python3
import json, os, sys
if sys.argv[1:] == ["--version"]:
    print("opencode 2.0.99-fake")
    raise SystemExit(0)
# Fail if poisoned ambient config leaked into child env.
assert "POISONED" not in os.environ.get("OPENCODE_CONFIG_CONTENT", "")
assert "poison" not in os.environ.get("OPENCODE_CONFIG", "")
model = sys.argv[sys.argv.index("--model") + 1]
if "unreported" in model:
    print(json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}))
else:
    print(
        json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "providerID": "opencode-go",
                    "modelID": "reported-alt",
                    "variant": "fast",
                },
            }
        )
    )
    print(json.dumps({"type": "text", "part": {"type": "text", "text": "done"}}))
""",
        encoding="utf-8",
    )
    fake_bin.chmod(fake_bin.stat().st_mode | stat.S_IEXEC)

    # Poison ambient environment and prior state dir.
    monkeypatch.setenv("OPENCODE_CONFIG_CONTENT", '{"model":"POISONED"}')
    monkeypatch.setenv("OPENCODE_CONFIG", "/tmp/poison.json")

    wt1 = _git_repo(isolated_env / "wt1")
    wt2 = _git_repo(isolated_env / "wt2")
    adapter = OpenCodeAdapter(opencode_bin=str(fake_bin))

    req1 = WorkerRequest(task="t1", worktree_path=wt1, model="opencode-go/mimo-v2.5")
    req2 = WorkerRequest(task="t2", worktree_path=wt2, model="opencode-go/unreported-model")

    with ThreadPoolExecutor(max_workers=2) as pool:
        res1, res2 = list(pool.map(adapter.run, (req1, req2)))

    assert res1.cli_version == "opencode 2.0.99-fake"
    assert res1.model == "opencode-go/mimo-v2.5"
    assert res1.actual_model == "opencode-go/reported-alt"
    assert res1.requested_variant == "unreported"
    assert res1.reported_variant == "fast"

    assert res2.cli_version == "opencode 2.0.99-fake"
    assert res2.model == "opencode-go/unreported-model"
    assert res2.actual_model == "unreported"
    assert res2.requested_variant == "unreported"
    assert res2.reported_variant == "unreported"


def test_default_route_suggestion_prefers_the_cheapest_admitted_model(
    isolated_env: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """D11: a first-run default must not spend the strongest model by default."""
    # The catalog lists the strong model first, as a vendor's listing does.
    _seed_discovered_model(model_id="gpt-6.2-sol")
    _seed_discovered_model(model_id="gpt-6-luna")
    assert main(["posture", "verify", "codex"]) == 0
    capsys.readouterr()

    assert main(["routing", "suggest", "--json"]) == 0
    suggestions = json.loads(capsys.readouterr().out)["suggestions"]
    assert [item["candidate_id"] for item in suggestions] == ["codex/gpt-6-luna"]

    assert main(["routing", "accept-default", "--json"]) == 0
    accepted = json.loads(capsys.readouterr().out)["accepted"]
    assert accepted == ["codex/gpt-6-luna"]
