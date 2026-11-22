"""A blocking overall verdict wins over all-PASS clause answers, visibly (ADR-0055)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.control_plane import (
    freeze_control_plane_policy,
    load_control_plane_policy,
    load_owner_bindings,
)
from saberops.control_plane.reviewer_binding import reviewer_candidate_for_dispatch
from saberops.db import current_iso_timestamp
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import (
    Attempt,
    AttemptStatus,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.paths import owner_state_dir
from saberops.provenance import mint_run_provenance
from saberops.review import ReviewEngine
from saberops.workers.registry import AdapterRegistry

_TASK = (
    "Rename greet to welcome.\n\n"
    "Acceptance:\nNo reference to greet remains in the repository."
)

#: Recorded shape rev_daadda8e67: every clause answered PASS but the overall
#: verdict still blocked.
_CONFLICT_TEXT = (
    "VERDICT: FINAL_BLOCK\n\n"
    "CLAUSE 1: PASS — app.py raises ValueError and no greet reference remains "
    "in the engine search.\n\n"
    "I still do not approve this candidate.\n"
)


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _seed(rig: RigEnvironment, task: str) -> str:
    db = rig.db()
    provenance = mint_run_provenance(rig.repo, task)
    run_id = provenance.run_id
    worktree = rig.root / "wt_conflict"
    _git(
        rig.repo,
        "worktree",
        "add",
        "-b",
        f"orch/{run_id}/a1",
        str(worktree),
        rig.base_sha,
    )
    (worktree / "app.py").write_text(
        "def welcome() -> str:\n    return 'hello'\n", encoding="utf-8"
    )
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "rename")
    candidate_sha = _git(worktree, "rev-parse", "HEAD").strip()

    now = current_iso_timestamp()
    frozen_cp = freeze_control_plane_policy(load_control_plane_policy())
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.RUNNING,
            created_at=now,
            control_plane_snapshot_json=frozen_cp.as_json(),
            control_plane_snapshot_digest=frozen_cp.digest,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_enabled": True}),
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model="gpt-5.6-terra",
            worktree_path=str(worktree),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate_sha,
            created_at=now,
            project_id=provenance.project_id,
            objective_id=provenance.objective_id,
            base_sha=rig.base_sha,
        )
    )
    return run_id


def _review(rig: RigEnvironment, run_id: str, *, is_final: bool) -> ReviewResult:
    db = rig.db()
    reviewer = reviewer_candidate_for_dispatch(
        load_owner_bindings(),
        adapter_registry=AdapterRegistry.default(),
    )
    assert reviewer is not None
    engine = ReviewEngine(
        db=db,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
    )
    return engine.review_run(
        run_id,
        reviewer_override=reviewer,
        timeout=20.0,
        is_final=is_final,
        final_attempt=1,
        final_limit=1,
    )


@pytest.mark.parametrize("is_final", [False, True])
def test_all_pass_clauses_with_a_blocking_verdict_record_the_conflict(
    rig: RigEnvironment, is_final: bool
) -> None:
    run_id = _seed(rig, _TASK)
    rig.set_reviewer_verdict("BLOCK", review_text=_CONFLICT_TEXT)

    result = _review(rig, run_id, is_final=is_final)

    expected = ReviewVerdict.FINAL_BLOCK if is_final else ReviewVerdict.BLOCK
    assert result.verdict is expected
    assert "clause/verdict conflict" in result.summary
    assert "The overall verdict wins" in result.details

    events = [
        event
        for event in rig.db().get_events(run_id, limit=2000)
        if event.event_type == "review_clause_verdict_conflict"
    ]
    assert len(events) == 1
    payload = events[0].payload or {}
    assert payload["verdict"] == expected.value
    assert payload["clause_count"] == 1
    assert payload["is_final"] is is_final
