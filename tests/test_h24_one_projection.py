"""H24: one run-report projection serves every kept read-only view.

These are golden tests on one recorded run: ``orch status``'s check/review
facts, ``orch report`` (JSON and text), ``orch watch``'s monitor frame and
``catch_up``'s check/review/switch adapters must all state the same facts.
The retired observability snapshot's evidence is still reachable from that
same projection, and the unused trace/phase readers are gone.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from saberops.db import Database
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    DispatchOutcome,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    Handoff,
    PhaseKind,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerCheckpoint,
    WorkerResult,
)
from saberops.observability import new_dispatch_evidence, update_dispatch_from_result
from saberops.observability.phase_telemetry import mark_phase_unknown
from saberops.observability.run_report import (
    build_run_report,
    canonical_report_json,
    is_terminal_report,
    render_monitor_frame,
    render_report_text,
)
from saberops.telemetry import persist_worker_usage

_CREATED = "2026-11-24T09:00:00+00:00"
_COMPLETED = "2026-11-24T09:05:00+00:00"
_RUN_ID = "run_h24"
_CANDIDATE = "cand-a"
_REPAIR_HEAD = "cand-b"
_CHECKPOINT = "checkpoint-sha"


def _recorded_run(db: Database) -> None:
    """Record one run with the evidence every kept view reads."""
    db.create_run(
        Run(
            id=_RUN_ID,
            task="one projection",
            target_repo=str(db.db_path.parent),
            base_commit="base-sha",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="make check",
            status=RunStatus.RUNNING,
            created_at=_CREATED,
            final_attempt_id=f"{_RUN_ID}_a2",
            project_id="proj_h24",
            config_json=json.dumps({"provider_override": "test", "model_override": "worker"}),
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{_RUN_ID}_a1",
            run_id=_RUN_ID,
            attempt_number=1,
            provider="test",
            model="worker",
            worktree_path=str(db.db_path.parent),
            branch_name="attempt-a",
            status=AttemptStatus.FAILED,
            base_sha="base-sha",
            commit_sha=_CANDIDATE,
            interruption_reason="QUOTA_EXHAUSTED",
            created_at=_CREATED,
            completed_at=_COMPLETED,
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{_RUN_ID}_a2",
            run_id=_RUN_ID,
            attempt_number=2,
            provider="other",
            model="other-model",
            worktree_path=str(db.db_path.parent),
            branch_name="attempt-b",
            status=AttemptStatus.SUCCESS,
            base_sha="base-sha",
            commit_sha=_CANDIDATE,
            created_at=_CREATED,
            completed_at=_COMPLETED,
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{_RUN_ID}_a3",
            run_id=_RUN_ID,
            attempt_number=3,
            provider="other",
            model="other-model",
            worktree_path=str(db.db_path.parent),
            branch_name="attempt-c",
            status=AttemptStatus.FAILED,
            base_sha=_CANDIDATE,
            commit_sha=_REPAIR_HEAD,
            created_at=_CREATED,
            completed_at=_COMPLETED,
        )
    )
    db.create_worker_checkpoint(
        WorkerCheckpoint(
            id="checkpoint-1",
            run_id=_RUN_ID,
            source_attempt_id=f"{_RUN_ID}_a1",
            source_base_sha="base-sha",
            checkpoint_sha=_CHECKPOINT,
            reason="quota",
            changed_paths=("src/app.py",),
            created_at=_CREATED,
        )
    )
    db.create_handoff(
        Handoff(
            id="handoff-1",
            run_id=_RUN_ID,
            kind="handoff_full",
            version=1,
            content="resume from the checkpoint",
            created_at=_CREATED,
        )
    )
    review_dispatch = new_dispatch_evidence(
        run_id=_RUN_ID,
        association_id="rev-1",
        attempt_id=f"{_RUN_ID}_a2",
        review_id="rev-1",
        stage=DispatchStage.REVIEW,
        role=DispatchRole.CHECK,
        tier=Tier.T1,
        dispatch_reason=DispatchReason.INITIAL_IMPLEMENTATION,
        requested_provider="test",
        requested_model="reviewer",
        task="review the candidate",
    )
    db.create_dispatch_evidence(review_dispatch)
    update_dispatch_from_result(
        review_dispatch,
        WorkerResult(
            provider="test",
            model="reviewer",
            status=AttemptStatus.SUCCESS,
            summary="reviewed",
            stdout="",
            stderr="",
            actual_provider="test",
            actual_model="reviewer",
            outcome=DispatchOutcome.UNKNOWN,
            commit_sha=_CANDIDATE,
        ),
    )
    db.update_dispatch_evidence(review_dispatch)
    persist_worker_usage(
        db,
        run_id=_RUN_ID,
        association_id="rev-1",
        attempt_id=f"{_RUN_ID}_a2",
        review_id="rev-1",
        stage=DispatchStage.REVIEW,
        tier=Tier.T1,
        provider="test",
        model="reviewer",
        prompt_bytes=10,
        transport="stdin",
        result=WorkerResult(
            provider="test",
            model="reviewer",
            status=AttemptStatus.SUCCESS,
            summary="reviewed",
            stdout="",
            stderr="",
            actual_provider="test",
            actual_model="reviewer",
            outcome=DispatchOutcome.UNKNOWN,
        ),
    )
    db.create_check(
        CheckResult(
            id="chk-1",
            attempt_id=f"{_RUN_ID}_a3",
            gate_command="make check",
            exit_code=1,
            stdout="",
            stderr="FAILED tests/test_app.py::test_greeting - 1 failed",
            passed=False,
            created_at=_COMPLETED,
        )
    )
    db.create_review(
        ReviewResult(
            id="rev-1",
            run_id=_RUN_ID,
            provider="test",
            model="reviewer",
            verdict=ReviewVerdict.PASS,
            summary="looks right",
            details="reviewer detail line",
            created_at=_COMPLETED,
        )
    )
    db.record_event(_RUN_ID, "run_started", payload={})
    db.record_event(_RUN_ID, "gate_failed", attempt_id=f"{_RUN_ID}_a3", payload={})
    db.record_event(
        _RUN_ID,
        "run_accepted",
        payload={"candidate_sha": _CANDIDATE, "final_branch": "main"},
    )


def test_kept_views_state_the_same_facts(tmp_path: Path) -> None:
    from saberops.mcp.tools import _catch_up_check, _catch_up_review, _catch_up_switch

    db = Database(tmp_path / "state.db")
    _recorded_run(db)
    report = build_run_report(db, _RUN_ID)

    # The candidate is the run's frozen final attempt, never "the latest commit".
    assert report["identity"]["candidate_sha"] == _CANDIDATE
    assert report["identity"]["candidate_source"] == "final_attempt"

    # The failed check assessed the repair head, not the accepted candidate.
    check = report["gate"]["checks"][-1]
    assert check["assessed_commit"] == _REPAIR_HEAD
    assert check["assessed_current_candidate"] is False
    assert check["passed"] is False
    assert "1 failed" in check["output_excerpt"]

    # The review assessed the candidate through its REVIEW dispatch row.
    review = report["review"]["round_detail"][-1]
    assert review["assessed_commit"] == _CANDIDATE
    assert review["assessed_current_candidate"] is True
    assert review["summary_excerpt"] == "looks right"

    # catch_up reads exactly these facts, not its own copies.
    assert _catch_up_check(report, _CANDIDATE) == {
        "check_id": "chk-1",
        "attempt_id": f"{_RUN_ID}_a3",
        "assessed_commit": _REPAIR_HEAD,
        "assessed_current_candidate": False,
        "passed": False,
        "skipped": False,
        "gate_command": "make check",
        "exit_code": 1,
        "excerpt": check["output_excerpt"],
    }
    assert _catch_up_review(report, _CANDIDATE) == {
        "review_id": "rev-1",
        "assessed_commit": _CANDIDATE,
        "assessed_current_candidate": True,
        "verdict": "PASS",
        "provider": "test",
        "model": "reviewer",
        "created_at": _COMPLETED,
        "excerpt": "looks right",
    }
    switch = _catch_up_switch(report)
    assert switch is not None
    assert switch["reason"] == "QUOTA_EXHAUSTED"
    assert switch["from"]["attempt_id"] == f"{_RUN_ID}_a1"
    assert switch["to"]["attempt_id"] == f"{_RUN_ID}_a2"
    assert switch["checkpoint_sha"] == _CHECKPOINT
    assert switch["continued_from_checkpoint"] is False
    assert switch["work_location_preserved"] is True

    # report text and the live frame are rendered from the same mapping.
    text = render_report_text(report)
    assert f"candidate_sha     {_CANDIDATE} (final_attempt)" in text
    assert "[FAIL] exit=1" in text
    assert "verdicts     PASS" in text
    frame = render_monitor_frame(report, now_iso=_COMPLETED)
    assert f"Run {_RUN_ID}  [RUNNING]" in frame
    assert "Pipeline worker=0x" in frame
    assert "acceptance=ACCEPTED" in frame

    # Deterministic: the same store yields byte-identical canonical JSON.
    assert canonical_report_json(report) == canonical_report_json(build_run_report(db, _RUN_ID))


def test_report_evidence_carries_the_retired_snapshot_facts(tmp_path: Path) -> None:
    db = Database(tmp_path / "state.db")
    _recorded_run(db)
    bindings = [{"ref": "read_file"}, {"ref": "write_file"}]
    digest = hashlib.sha256(
        json.dumps(
            sorted(bindings, key=lambda item: item["ref"]),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()
    run = db.get_run(_RUN_ID)
    assert run is not None
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            "UPDATE runs SET tool_contract_json = ?, tool_contract_digest = ? WHERE id = ?",
            (
                json.dumps({"schema_version": 1, "bindings": bindings, "digest": digest}),
                digest,
                _RUN_ID,
            ),
        )
        conn.commit()
    mark_phase_unknown(
        db,
        run_id=_RUN_ID,
        phase=PhaseKind.REVIEW,
        source_kind="test",
        notes="no boundary in this fixture",
    )

    evidence = build_run_report(db, _RUN_ID)["evidence"]

    assert [row["id"] for row in evidence["candidate_lineage"]] == [
        f"{_RUN_ID}_a1",
        f"{_RUN_ID}_a2",
        f"{_RUN_ID}_a3",
    ]
    assert len(evidence["dispatches"]) == 1
    assert evidence["dispatches"][0]["review_id"] == "rev-1"
    assert [row["id"] for row in evidence["review_evidence"]] == ["rev-1"]
    assert evidence["review_evidence"][0]["details"] == "reviewer detail line"
    assert evidence["project_scope"] is None
    assert evidence["continuation"]["checkpoint_count"] == 1
    assert evidence["continuation"]["handoffs"][0]["byte_count"] == len(
        "resume from the checkpoint"
    )
    assert evidence["frozen_tool_contract"] is not None
    assert evidence["frozen_tool_contract"]["digest"] == digest
    assert evidence["phases"]["summary"]["REVIEW"]["UNKNOWN"] == 1
    assert evidence["phases"]["rows"][0]["status"] == "UNKNOWN"
    completeness = evidence["completeness"]
    assert completeness["dispatch_count"] == 1
    assert completeness["usage_row_count"] == 1
    assert completeness["dispatch_contract_complete"] is True
    assert completeness["usage_association_complete"] is True
    assert completeness["reconstruction_complete"] is True


def test_terminal_status_comes_from_the_canonical_set(tmp_path: Path) -> None:
    """The follow view and the report agree on every terminal status."""
    db = Database(tmp_path / "state.db")
    db.create_run(
        Run(
            id="run_preempted",
            task="preempted",
            target_repo=str(tmp_path),
            base_commit="base-sha",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.PREEMPTED,
            created_at=_CREATED,
            completed_at=_COMPLETED,
        )
    )
    report = build_run_report(db, "run_preempted")
    assert report["lifecycle"]["status"] == "PREEMPTED"
    assert is_terminal_report(report) is True


def test_unused_trace_and_phase_readers_are_gone() -> None:
    from saberops import db as db_module

    for name in (
        "get_attempt_trace",
        "get_attempt_traces_for_run",
        "get_tool_effects_for_run",
        "get_worker_activations_for_run",
        "get_phase_observation",
    ):
        assert not hasattr(db_module.Database, name)
