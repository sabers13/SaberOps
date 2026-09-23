"""Focused C16-B1 coverage: canonical run report + live monitor projection."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from saberops.cli import main as orch_main
from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    DispatchOutcome,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ProviderUsage,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerCheckpoint,
    WorkerResult,
)
from saberops.observability import new_dispatch_evidence, update_dispatch_from_result
from saberops.observability.run_report import (
    REPORT_SCHEMA,
    UNKNOWN,
    build_run_report,
    canonical_report_json,
    is_terminal_report,
    render_monitor_frame,
    render_report_text,
)
from saberops.telemetry import persist_worker_usage
from saberops.web import create_app

_CREATED = "2026-09-23T10:00:00+00:00"
_COMPLETED = "2026-09-23T10:05:00+00:00"


def _run(
    db: Database,
    run_id: str = "run_c16b1",
    *,
    status: RunStatus = RunStatus.RUNNING,
    completed_at: str | None = None,
    final_attempt_id: str | None = None,
    config: dict[str, Any] | None = None,
) -> Run:
    cfg = {"provider_override": "test", "model_override": "requested"} if config is None else config
    run = Run(
        id=run_id,
        task="docs task",
        target_repo="/tmp/does-not-exist-repo",
        base_commit="base-commit-sha",
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=status,
        created_at=_CREATED,
        completed_at=completed_at,
        final_attempt_id=final_attempt_id,
        project_id="proj_c16b1",
        config_json=json.dumps(cfg),
    )
    db.create_run(run)
    return run


def _attempt(
    db: Database,
    run_id: str,
    attempt_id: str,
    number: int,
    *,
    status: AttemptStatus = AttemptStatus.RUNNING,
    commit_sha: str | None = None,
    worktree: str = "/tmp/does-not-exist-wt",
) -> Attempt:
    attempt = Attempt(
        id=attempt_id,
        run_id=run_id,
        attempt_number=number,
        provider="test",
        model="requested",
        worktree_path=worktree,
        branch_name=f"attempt-{number}",
        status=status,
        commit_sha=commit_sha,
        created_at=_CREATED,
    )
    db.create_attempt(attempt)
    return attempt


def _dispatch(
    db: Database,
    run_id: str,
    assoc: str,
    stage: DispatchStage,
    role: DispatchRole,
    *,
    attempt_id: str | None = None,
    review_id: str | None = None,
    actual_provider: str | None = None,
    actual_model: str | None = None,
    outcome: DispatchOutcome | None = None,
    usage: list[ProviderUsage] | None = None,
) -> str:
    evidence = new_dispatch_evidence(
        run_id=run_id,
        association_id=assoc,
        stage=stage,
        role=role,
        tier=Tier.T1,
        dispatch_reason=DispatchReason.INITIAL_IMPLEMENTATION,
        requested_provider="test",
        requested_model="requested",
        task="docs task",
        attempt_id=attempt_id,
        review_id=review_id,
    )
    db.create_dispatch_evidence(evidence)
    if outcome is not None or actual_provider is not None or usage is not None:
        result = WorkerResult(
            provider="test",
            model="requested",
            status=AttemptStatus.SUCCESS,
            summary="done",
            stdout="",
            stderr="",
            actual_provider=actual_provider,
            actual_model=actual_model,
            outcome=outcome,
            usage=usage or [],
        )
        update_dispatch_from_result(evidence, result)
        db.update_dispatch_evidence(evidence)
        persist_worker_usage(
            db,
            run_id=run_id,
            association_id=assoc,
            attempt_id=attempt_id,
            review_id=review_id,
            stage=stage,
            tier=Tier.T1,
            provider="test",
            model="requested",
            prompt_bytes=10,
            transport="stdin",
            result=result,
        )
    return evidence.id


def _review_decision(*, required: bool, skip_reason: str = "", reason: str = "low risk") -> str:
    return json.dumps(
        {
            "independent_review_required": required,
            "review_requirement_source": "test",
            "review_skip_reason": skip_reason,
            "classification": {"reason": reason, "risk_level": "LOW"},
        },
        sort_keys=True,
    )


# --- running projection -----------------------------------------------------


def test_c16b1_unknown_sentinel_matches_replay_contract() -> None:
    """The local UNKNOWN sentinel mirrors replay/contracts.py by value.

    The import lives in tests only: production observability must not
    depend on the replay module (architecture ownership invariant).
    """
    from saberops.replay.contracts import UNKNOWN as REPLAY_UNKNOWN

    assert UNKNOWN == REPLAY_UNKNOWN == "UNKNOWN"


def test_c16b1_running_projection(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    _dispatch(
        db, "run_c16b1", "assoc_1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
        attempt_id="att_1",
    )
    db.record_event("run_c16b1", "run_started", payload={})
    db.record_event("run_c16b1", "worker_started", attempt_id="att_1", payload={})

    report = build_run_report(db, "run_c16b1")
    assert report["schema"] == REPORT_SCHEMA
    assert report["lifecycle"]["status"] == "RUNNING"
    assert report["lifecycle"]["current_stage"] == "worker"
    assert report["lifecycle"]["current_stage_source"].startswith("worker_started#")
    assert report["lifecycle"]["completed_at"] == UNKNOWN
    assert report["lifecycle"]["total_wall_seconds"] == UNKNOWN
    assert report["worker"]["requested_provider"] == "test"
    assert report["worker"]["requested_model"] == "requested"
    assert report["worker"]["actual_provider"] == UNKNOWN
    assert report["worker"]["actual_model"] == UNKNOWN
    assert report["worker"]["actual_identity_status"] == UNKNOWN
    assert report["execution"]["attempt_count"] == 1
    assert report["execution"]["worker_dispatch_count"] == 1
    assert report["acceptance"]["acceptance_status"] == "NOT_ACCEPTED"
    assert not is_terminal_report(report)
    frame = render_monitor_frame(report, now_iso="2026-09-23T10:01:00+00:00")
    assert "stage=worker" in frame
    assert "elapsed=60s" in frame


def test_c16b1_unknown_run_raises(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    try:
        build_run_report(db, "run_missing")
    except ValueError:
        return
    raise AssertionError("expected ValueError for unknown run")


# --- completed successful run ----------------------------------------------


def test_c16b1_completed_successful_run(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db, status=RunStatus.COMPLETED, completed_at=_COMPLETED, final_attempt_id="att_1")
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.SUCCESS, commit_sha="cand-sha")
    _dispatch(
        db, "run_c16b1", "assoc_1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
        attempt_id="att_1",
        actual_provider="test",
        actual_model="actual-model",
        outcome=DispatchOutcome.CAPABILITY_FAILURE,
        usage=[ProviderUsage(input_tokens=4, output_tokens=2, total_tokens=6,
                             usage_source="fixture")],
    )
    db.create_check(
        CheckResult(id="chk_1", attempt_id="att_1", gate_command="true", exit_code=0,
                    stdout="", stderr="", passed=True, created_at=_COMPLETED)
    )
    db.update_review_risk_decision("run_c16b1", _review_decision(required=True))
    db.create_review(
        ReviewResult(id="rev_1", run_id="run_c16b1", provider="test", model="reviewer",
                     verdict=ReviewVerdict.PASS, summary="ok", created_at=_COMPLETED)
    )
    for event_type in ("run_started", "attempt_completed", "gate_completed",
                       "review_completed", "run_completed"):
        db.record_event("run_c16b1", event_type, payload={})
    db.record_event("run_c16b1", "run_accepted",
                    payload={"candidate_sha": "cand-sha", "final_branch": "main",
                             "gate_command": "true"})

    report = build_run_report(db, "run_c16b1")
    assert report["identity"]["candidate_sha"] == "cand-sha"
    assert report["identity"]["candidate_source"] == "final_attempt"
    assert report["lifecycle"]["current_stage"] == "terminal:COMPLETED"
    assert report["lifecycle"]["current_stage_source"] == "run_row:status"
    assert report["lifecycle"]["total_wall_seconds"] == 300.0
    assert report["worker"]["actual_provider"] == "test"
    assert report["worker"]["actual_model"] == "actual-model"
    assert report["worker"]["actual_identity_status"] == "KNOWN"
    assert report["gate"]["summary"] == {"check_total": 1, "check_passed": 1,
                                         "check_failed": 0, "receipt_total": 0}
    assert report["review"]["review_required"] is True
    assert report["review"]["rounds"] == 1
    assert report["review"]["verdicts"] == ["PASS"]
    assert report["acceptance"]["acceptance_status"] == "ACCEPTED"
    assert report["acceptance"]["accepted_candidate_sha"] == "cand-sha"
    assert is_terminal_report(report)
    text = render_report_text(report)
    assert "candidate_sha     cand-sha (final_attempt)" in text


def test_c16b1_failed_run(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db, status=RunStatus.FAILED, completed_at=_COMPLETED)
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.FAILED)
    _dispatch(db, "run_c16b1", "assoc_1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1", outcome=DispatchOutcome.GATE_FAILURE,
              usage=[ProviderUsage(usage_source="unavailable")])
    db.create_check(
        CheckResult(id="chk_1", attempt_id="att_1", gate_command="true", exit_code=1,
                    stdout="", stderr="boom", passed=False, created_at=_COMPLETED)
    )
    db.record_event("run_c16b1", "gate_failed", payload={})
    db.record_event("run_c16b1", "run_failed", payload={})

    report = build_run_report(db, "run_c16b1")
    assert report["lifecycle"]["status"] == "FAILED"
    assert report["lifecycle"]["current_stage"] == "terminal:FAILED"
    assert report["gate"]["checks"][0]["verdict"] == "FAIL"
    assert report["acceptance"]["acceptance_status"] == "NOT_ACCEPTED"
    assert is_terminal_report(report)


# --- dispatch counting semantics -------------------------------------------


def test_c16b1_retry_vs_repair_separation(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    _dispatch(db, "run_c16b1", "a1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1")
    _dispatch(db, "run_c16b1", "a2", DispatchStage.RETRY, DispatchRole.REPAIR,
              attempt_id="att_1")
    _dispatch(db, "run_c16b1", "a3", DispatchStage.RETRY, DispatchRole.REPAIR,
              attempt_id="att_1")
    _dispatch(db, "run_c16b1", "a4", DispatchStage.REPAIR, DispatchRole.REPAIR,
              attempt_id="att_1")

    report = build_run_report(db, "run_c16b1")
    execution = report["execution"]
    assert execution["worker_dispatch_count"] == 1
    assert execution["retry_dispatch_count"] == 2
    assert execution["repair_dispatch_count"] == 1
    assert execution["reviewer_dispatch_count"] == 0


def test_c16b1_worker_vs_reviewer_dispatch_counts(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    _dispatch(db, "run_c16b1", "a1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1")
    _dispatch(db, "run_c16b1", "a2", DispatchStage.RETRY, DispatchRole.REPAIR,
              attempt_id="att_1")
    _dispatch(db, "run_c16b1", "a3", DispatchStage.REPAIR, DispatchRole.REPAIR,
              attempt_id="att_1")
    _dispatch(db, "run_c16b1", "r1", DispatchStage.REVIEW, DispatchRole.REVIEW,
              review_id="rev_1")
    _dispatch(db, "run_c16b1", "r2", DispatchStage.REVIEW, DispatchRole.REVIEW,
              review_id="rev_2")

    report = build_run_report(db, "run_c16b1")
    execution = report["execution"]
    assert execution["worker_dispatch_count"] == 1
    assert execution["reviewer_dispatch_count"] == 2
    # Reviewer dispatches never leak into worker identity.
    assert report["worker"]["actual_identity_status"] == UNKNOWN


def test_c16b1_requested_never_becomes_actual(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    _dispatch(db, "run_c16b1", "a1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1")

    report = build_run_report(db, "run_c16b1")
    assert report["worker"]["requested_provider"] == "test"
    assert report["worker"]["requested_model"] == "requested"
    assert report["worker"]["actual_provider"] == UNKNOWN
    assert report["worker"]["actual_model"] == UNKNOWN


def test_c16b1_mixed_actual_identities_flagged(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    _dispatch(db, "run_c16b1", "a1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1", actual_provider="prov-a", actual_model="model-a",
              outcome=DispatchOutcome.CAPABILITY_FAILURE,
              usage=[ProviderUsage(usage_source="unavailable")])
    _dispatch(db, "run_c16b1", "a2", DispatchStage.RETRY, DispatchRole.REPAIR,
              attempt_id="att_1", actual_provider="prov-b", actual_model="model-b",
              outcome=DispatchOutcome.CAPABILITY_FAILURE,
              usage=[ProviderUsage(usage_source="unavailable")])

    report = build_run_report(db, "run_c16b1")
    assert report["worker"]["actual_identity_status"] == "MIXED"
    assert report["worker"]["actual_provider"] == "prov-b"
    assert len(report["worker"]["dispatch_identities"]) == 2


# --- usage semantics --------------------------------------------------------


def test_c16b1_usage_events_are_not_dispatch_counts(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    result = WorkerResult(
        provider="test", model="requested", status=AttemptStatus.SUCCESS,
        summary="done", stdout="", stderr="",
        usage=[
            ProviderUsage(input_tokens=10, output_tokens=5, cache_read_tokens=3,
                          cache_write_tokens=1, reasoning_tokens=2, total_tokens=15,
                          usage_source="fixture"),
            ProviderUsage(usage_source="unavailable"),
        ],
    )
    evidence = new_dispatch_evidence(
        run_id="run_c16b1", association_id="assoc_1",
        stage=DispatchStage.IMPLEMENTATION, role=DispatchRole.BULK, tier=Tier.T1,
        dispatch_reason=DispatchReason.INITIAL_IMPLEMENTATION,
        requested_provider="test", requested_model="requested",
        task="docs task", attempt_id="att_1",
    )
    db.create_dispatch_evidence(evidence)
    update_dispatch_from_result(evidence, result)
    db.update_dispatch_evidence(evidence)
    persist_worker_usage(
        db, run_id="run_c16b1", association_id="assoc_1", attempt_id="att_1",
        review_id=None, stage=DispatchStage.IMPLEMENTATION, tier=Tier.T1,
        provider="test", model="requested", prompt_bytes=10, transport="stdin",
        result=result,
    )

    report = build_run_report(db, "run_c16b1")
    usage = report["usage"]
    assert usage["provider_usage_event_count"] == 2
    assert report["execution"]["worker_dispatch_count"] == 1
    assert usage["input_tokens"] == 10
    assert usage["output_tokens"] == 5
    assert usage["cache_read_tokens"] == 3
    assert usage["cache_write_tokens"] == 1
    assert usage["reasoning_tokens"] == 2
    assert usage["total_tokens"] == 15
    assert usage["calls_with_known_usage"] == 1
    assert usage["calls_with_unknown_usage"] == 1


def test_c16b1_all_unknown_usage_stays_unknown(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    _dispatch(db, "run_c16b1", "a1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1", outcome=DispatchOutcome.CAPABILITY_FAILURE,
              usage=[ProviderUsage(usage_source="unavailable")])

    report = build_run_report(db, "run_c16b1")
    for field in ("input_tokens", "output_tokens", "cache_read_tokens",
                  "cache_write_tokens", "reasoning_tokens", "total_tokens"):
        assert report["usage"][field] == UNKNOWN, field


# --- review semantics -------------------------------------------------------


def test_c16b1_risk_adaptive_review_skip(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db, status=RunStatus.COMPLETED, completed_at=_COMPLETED, final_attempt_id="att_1")
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.SUCCESS, commit_sha="cand")
    db.update_review_risk_decision(
        "run_c16b1", _review_decision(required=False, skip_reason="LOW (docs only)"))

    report = build_run_report(db, "run_c16b1")
    assert report["review"]["review_required"] is False
    assert report["review"]["review_skip_reason"] == "LOW (docs only)"
    assert report["review"]["rounds"] == 0
    assert report["review"]["verdicts"] == []


def test_c16b1_independent_review(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    db.update_review_risk_decision("run_c16b1", _review_decision(required=True))
    db.create_review(
        ReviewResult(id="rev_1", run_id="run_c16b1", provider="test", model="reviewer",
                     verdict=ReviewVerdict.BLOCK, summary="issue", created_at=_CREATED))
    db.create_review(
        ReviewResult(id="rev_2", run_id="run_c16b1", provider="test", model="reviewer",
                     verdict=ReviewVerdict.PASS, summary="fixed", created_at=_COMPLETED))
    _dispatch(db, "run_c16b1", "r1", DispatchStage.REVIEW, DispatchRole.REVIEW,
              review_id="rev_1")
    _dispatch(db, "run_c16b1", "r2", DispatchStage.REVIEW, DispatchRole.REVIEW,
              review_id="rev_2")

    report = build_run_report(db, "run_c16b1")
    assert report["review"]["review_required"] is True
    assert report["review"]["rounds"] == 2
    assert report["review"]["verdicts"] == ["BLOCK", "PASS"]
    assert report["execution"]["reviewer_dispatch_count"] == 2


def test_c16b1_malformed_review_decision_fails_closed(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    db.update_review_risk_decision("run_c16b1", "{not-json")

    report = build_run_report(db, "run_c16b1")
    assert report["review"]["review_required"] is True
    assert any("fail closed" in gap for gap in report["evidence_gaps"])


# --- acceptance semantics ---------------------------------------------------


def test_c16b1_durable_acceptance_without_repo_present(tmp_path: Path) -> None:
    """Acceptance comes from the durable event even though the target repo path
    does not exist on disk: the live HEAD is never consulted."""
    assert not Path("/tmp/does-not-exist-repo").exists()
    db = Database(tmp_path / "state.db")
    _run(db, status=RunStatus.COMPLETED, completed_at=_COMPLETED, final_attempt_id="att_1")
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.SUCCESS, commit_sha="cand-sha")
    db.record_event("run_c16b1", "run_accepted",
                    payload={"candidate_sha": "cand-sha", "final_branch": "main"})

    report = build_run_report(db, "run_c16b1")
    assert report["acceptance"]["acceptance_status"] == "ACCEPTED"
    assert report["acceptance"]["accepted_candidate_sha"] == "cand-sha"
    assert "current_head" not in json.dumps(report)


def test_c16b1_no_acceptance_inference(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db, status=RunStatus.COMPLETED, completed_at=_COMPLETED, final_attempt_id="att_1")
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.SUCCESS, commit_sha="cand-sha")

    report = build_run_report(db, "run_c16b1")
    assert report["acceptance"]["acceptance_status"] == "NOT_ACCEPTED"
    assert report["acceptance"]["accepted_candidate_sha"] == UNKNOWN


# --- failures, resumes, determinism ----------------------------------------


def test_c16b1_provider_runtime_failure_warns(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.FAILED)
    _dispatch(db, "run_c16b1", "a1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1", outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
              usage=[ProviderUsage(usage_source="unavailable")])
    db.create_review(
        ReviewResult(id="rev_1", run_id="run_c16b1", provider="test", model="reviewer",
                     verdict=ReviewVerdict.REVIEW_EXECUTION_FAILED, summary="down",
                     created_at=_CREATED))

    report = build_run_report(db, "run_c16b1")
    assert any("provider/runtime failure" in warning for warning in report["warnings"])
    assert any("REVIEW_EXECUTION_FAILED" in warning for warning in report["warnings"])
    frame = render_monitor_frame(report)
    assert "Warnings (2)" in frame


def test_c16b1_resume_count_from_events(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    db.record_event("run_c16b1", "worker_started", payload={})
    db.record_event("run_c16b1", "worker_resumed", payload={})
    db.record_event("run_c16b1", "worker_resumed", payload={})

    report = build_run_report(db, "run_c16b1")
    assert report["execution"]["resume_count"] == 2


def test_c16b1_json_stability_and_determinism(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db, status=RunStatus.COMPLETED, completed_at=_COMPLETED, final_attempt_id="att_1")
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.SUCCESS, commit_sha="cand")
    _dispatch(db, "run_c16b1", "a1", DispatchStage.IMPLEMENTATION, DispatchRole.BULK,
              attempt_id="att_1", actual_provider="test", actual_model="m",
              outcome=DispatchOutcome.CAPABILITY_FAILURE,
              usage=[ProviderUsage(input_tokens=1, output_tokens=1, total_tokens=2,
                                   usage_source="fixture")])

    first = canonical_report_json(build_run_report(db, "run_c16b1"))
    second = canonical_report_json(build_run_report(db, "run_c16b1"))
    assert first == second
    # Canonical form: sorted keys, compact separators.
    assert first == json.dumps(json.loads(first), sort_keys=True,
                               separators=(",", ":"),
                               ensure_ascii=False).encode("utf-8")


def test_c16b1_changed_paths_from_durable_checkpoint(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _run(db)
    _attempt(db, "run_c16b1", "att_1", 1)
    db.create_worker_checkpoint(
        WorkerCheckpoint(id="ckpt_1", run_id="run_c16b1", source_attempt_id="att_1",
                         source_base_sha="base", checkpoint_sha="ckpt",
                         reason="test", changed_paths=("b.md", "a.md"),
                         created_at=_CREATED))

    report = build_run_report(db, "run_c16b1")
    assert report["workspace"]["changed_paths"] == ["a.md", "b.md"]
    assert report["workspace"]["cleanup_state"] == UNKNOWN


# --- CLI + API surfaces -----------------------------------------------------


def test_c16b1_cli_report_and_monitor(tmp_path: Path, capsys: Any) -> None:
    db_path = tmp_path / "state.db"
    db = Database(db_path)
    _run(db, status=RunStatus.COMPLETED, completed_at=_COMPLETED, final_attempt_id="att_1")
    _attempt(db, "run_c16b1", "att_1", 1, status=AttemptStatus.SUCCESS, commit_sha="cand")

    assert orch_main(["report", "run_c16b1", "--json", "--db", str(db_path)]) == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["identity"]["run_id"] == "run_c16b1"
    assert payload["schema"] == REPORT_SCHEMA

    assert orch_main(["report", "run_c16b1", "--db", str(db_path)]) == 0
    text = capsys.readouterr().out
    assert "Run report  run_c16b1" in text
    assert "evidence_gaps" in text

    assert orch_main(["monitor", "run_c16b1", "--once", "--db", str(db_path)]) == 0
    frame = capsys.readouterr().out
    assert "Run run_c16b1" in frame
    assert "terminal:COMPLETED" in frame

    # Terminal runs exit cleanly even without --once.
    assert orch_main(["monitor", "run_c16b1", "--db", str(db_path),
                      "--interval", "0.5"]) == 0

    assert orch_main(["report", "run_missing", "--db", str(db_path)]) == 1


def test_c16b1_api_report_endpoint(tmp_path: Path) -> None:
    db_path = tmp_path / "web.db"
    db = Database(db_path)
    _run(db)
    client = TestClient(create_app(db_path=db_path))
    response = client.get("/runs/run_c16b1/report")
    assert response.status_code == 200
    payload = response.json()
    assert payload["identity"]["run_id"] == "run_c16b1"
    assert payload["schema"] == REPORT_SCHEMA
    missing = client.get("/runs/run_missing/report")
    assert missing.status_code == 404


def test_c16b1_run_page_shows_monitoring_section(tmp_path: Path) -> None:
    db_path = tmp_path / "page.db"
    db = Database(db_path)
    _run(db)
    client = TestClient(create_app(db_path=db_path))
    response = client.get("/runs/run_c16b1")
    assert response.status_code == 200
    assert "Live Monitoring" in response.text
    assert "stage" in response.text.lower()
