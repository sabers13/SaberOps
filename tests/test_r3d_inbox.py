"""R3-D: cross-project owner Inbox regressions.

Builds every scenario with real project-local state directories, real
Database APIs, the real ``project_candidate_state`` seam, the real
service Reject seam, the real AcceptEngine-shaped acceptance evidence,
and the real Web app -- never mocks of the seams themselves.

Required regressions A-V (see the R3-D contract).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from saberops.candidate_lifecycle import project_candidate_state
from saberops.db import Database
from saberops.inbox import (
    DEFAULT_MAX_ITEMS,
    InboxAttentionKind,
    InboxItem,
    InboxSnapshot,
    build_inbox,
    classify_run,
)
from saberops.models import (
    Attempt,
    AttemptStatus,
    CandidateState,
    CheckResult,
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    RiskLevel,
    Run,
    RunStatus,
    Tier,
)
from saberops.project import compute_project_identity, new_run_id
from saberops.projects import ProjectRegistry
from saberops.review_adaptive import (
    ChangeClass,
    ChangeClassification,
    ReviewPolicyMode,
    decide_review,
)
from saberops.runtime import ProjectRuntimeManager
from saberops.service import OrchestratorService

_CREATED = "2026-09-24T10:00:00+00:00"


def _sha(tag: str) -> str:
    """Deterministic 40-hex candidate SHA for ``tag``."""
    return hashlib.sha1(tag.encode("utf-8")).hexdigest()


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def _make_repo(base: Path, name: str) -> Path:
    repo = base / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "r3d@test")
    _git(repo, "config", "user.name", "R3-D Probe")
    (repo / "README.md").write_text("# probe\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "initial")
    return repo


def _manager(tmp: Path) -> ProjectRuntimeManager:
    state_root = tmp / "projects"
    state_root.mkdir(parents=True, exist_ok=True)
    owner_path = tmp / "owner.db"
    owner_path.parent.mkdir(parents=True, exist_ok=True)
    return ProjectRuntimeManager(state_root=state_root, owner_db_path=owner_path)


def _project_db(tmp: Path, manager: ProjectRuntimeManager, repo: Path) -> Database:
    return manager.for_repo(repo).db


def _seed_run(
    db: Database,
    run_id: str,
    *,
    status: RunStatus = RunStatus.COMPLETED,
    created: str = _CREATED,
    completed: str | None = _CREATED,
    task: str = "r3d inbox probe",
    repo: str = "/tmp/r3d-target",
    review_json: str | None = None,
    project_id: str | None = None,
) -> None:
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=repo,
            base_commit=_sha(f"base-{run_id}"),
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=status,
            created_at=created,
            completed_at=completed,
            review_risk_decision_json=review_json,
            project_id=project_id,
        )
    )


def _gate_pass(
    db: Database, run_id: str, attempt_id: str, num: int, sha: str
) -> None:
    base = _sha(f"base-{run_id}")
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=num,
            provider="opencode",
            model="test-model",
            worktree_path="/tmp/r3d-wt",
            branch_name="r3d-candidate",
            status=AttemptStatus.SUCCESS,
            commit_sha=sha,
            created_at=_CREATED,
            base_sha=base,
        )
    )
    db.create_check(
        CheckResult(
            id=f"check-{attempt_id}",
            attempt_id=attempt_id,
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=_CREATED,
        )
    )


def _review_dispatch(db: Database, run_id: str, assoc: str, sha: str | None) -> None:
    db.create_dispatch_evidence(
        DispatchEvidence(
            id=f"dispatch-{assoc}",
            run_id=run_id,
            association_id=assoc,
            stage=DispatchStage.REVIEW,
            role=DispatchRole.REVIEW,
            tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="opencode",
            requested_model="reviewer",
            candidate_sha=sha,
            started_at=_CREATED,
        )
    )


def _review(db: Database, run_id: str, review_id: str, verdict: ReviewVerdict) -> None:
    db.create_review(
        ReviewResult(
            id=review_id,
            run_id=run_id,
            provider="opencode",
            model="reviewer",
            verdict=verdict,
            summary=f"{verdict.value} summary",
            details="details",
            created_at=_CREATED,
        )
    )


def _classification() -> ChangeClassification:
    return ChangeClassification(
        change_class=ChangeClass.DOCS_ONLY,
        risk_level=RiskLevel.LOW,
        changed_paths=("README.md",),
        crosses_ownership_boundary=False,
        changes_hard_governance=False,
        changes_security_boundary=False,
        changes_acceptance_or_gate_authority=False,
        changes_publication_authority=False,
        changes_recovery_or_exactly_once=False,
        deterministic_coverage_available=True,
        signals=("docs_only",),
        reason="docs only",
    )


def _skip_json(run_id: str) -> str:
    decision = decide_review(
        classification=_classification(),
        review_policy_mode=ReviewPolicyMode.RISK_ADAPTIVE,
        owner_explicit_review=False,
        is_autonomous=False,
        decision_id=f"r3d-skip-{run_id}",
    )
    assert decision.independent_review_required is False
    return decision.to_json()


def _required_json(run_id: str) -> str:
    decision = decide_review(
        classification=_classification(),
        review_policy_mode=ReviewPolicyMode.RISK_ADAPTIVE,
        owner_explicit_review=True,
        is_autonomous=False,
        decision_id=f"r3d-required-{run_id}",
    )
    assert decision.independent_review_required is True
    return decision.to_json()


def _accept(db: Database, run_id: str, sha: str) -> None:
    """Record valid authoritative AcceptEngine-shaped acceptance evidence."""
    db.record_event(
        run_id,
        "run_accepted",
        payload={
            "candidate_sha": sha,
            "final_branch": "main",
            "gate_command": "true",
            "provenance": {
                "candidate_sha": sha,
                "run_id": run_id,
                "attempt_id": "probe",
                "attempt_number": 1,
            },
        },
    )


def _decision_run(
    db: Database,
    run_id: str,
    candidate: str,
    *,
    review_json: str | None = None,
    status: RunStatus = RunStatus.COMPLETED,
    project_id: str | None = None,
) -> None:
    _seed_run(
        db,
        run_id,
        status=status,
        review_json=review_json,
        project_id=project_id,
    )
    _gate_pass(db, run_id, f"{run_id}-a1", 1, candidate)


def _by_id(snapshot: InboxSnapshot) -> dict[str, InboxItem]:
    return {item.run_id: item for item in snapshot.items}


# ---------------------------------------------------------------------------
# A. Cross-project aggregation.
# ---------------------------------------------------------------------------


def test_a_cross_project_aggregation(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-a")
    repo_b = _make_repo(repos, "proj-b")
    manager = _manager(tmp_path)
    db_a = _project_db(tmp_path, manager, repo_a)
    db_b = _project_db(tmp_path, manager, repo_b)
    id_a = compute_project_identity(repo_a)
    id_b = compute_project_identity(repo_b)

    run_a = new_run_id(id_a)
    candidate_a = _sha("candidate-r3d-a")
    _decision_run(
        db_a, run_a, candidate_a, review_json=_skip_json(run_a),
        project_id=id_a.project_id,
    )
    run_b = new_run_id(id_b)
    _seed_run(db_b, run_b, status=RunStatus.FAILED, project_id=id_b.project_id)

    registry = ProjectRegistry(path=tmp_path / "ui.json")
    assert registry.add_project(repo_a)[0] is True
    assert registry.select(str(repo_a))[0] is True

    snapshot = build_inbox(manager)
    assert snapshot.unreadable_projects == 0
    got = _by_id(snapshot)
    assert set(got) == {run_a, run_b}
    assert got[run_a].attention_kind is InboxAttentionKind.DECISION_REQUIRED
    assert got[run_b].attention_kind is InboxAttentionKind.RUN_FAILED
    assert got[run_a].project_id == id_a.project_id
    assert got[run_b].project_id == id_b.project_id


# ---------------------------------------------------------------------------
# B. Active-project independence.
# ---------------------------------------------------------------------------


def test_b_active_project_independence(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-a")
    repo_b = _make_repo(repos, "proj-b")
    manager = _manager(tmp_path)
    db_a = _project_db(tmp_path, manager, repo_a)
    db_b = _project_db(tmp_path, manager, repo_b)
    id_a = compute_project_identity(repo_a)
    id_b = compute_project_identity(repo_b)

    run_a = new_run_id(id_a)
    _decision_run(
        db_a, run_a, _sha("candidate-r3d-b-a"),
        review_json=_skip_json(run_a), project_id=id_a.project_id,
    )
    run_b = new_run_id(id_b)
    _seed_run(db_b, run_b, status=RunStatus.FAILED, project_id=id_b.project_id)

    registry = ProjectRegistry(path=tmp_path / "ui.json")
    assert registry.add_project(repo_a)[0] is True
    assert registry.add_project(repo_b)[0] is True

    assert registry.select(str(repo_a))[0] is True
    before = build_inbox(manager)
    assert registry.select(str(repo_b))[0] is True
    after = build_inbox(manager)

    assert [i.run_id for i in before.items] == [i.run_id for i in after.items]
    assert [i.attention_kind for i in before.items] == [
        i.attention_kind for i in after.items
    ]
    assert {run_a, run_b} == {i.run_id for i in after.items}


# ---------------------------------------------------------------------------
# C-I. Attention classification matrix (single project store).
# ---------------------------------------------------------------------------


def _single_db(tmp_path: Path) -> Database:
    path = tmp_path / "one" / "orch.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    return Database(path)


def test_c_reviewed_candidate_needs_decision(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_c_case"
    candidate = _sha("candidate-r3d-c")
    _decision_run(db, run_id, candidate)
    _review_dispatch(db, run_id, "assoc-c", candidate)
    _review(db, run_id, "assoc-c", ReviewVerdict.PASS)
    assert project_candidate_state(db, run_id).state is CandidateState.REVIEWED

    item = classify_run(db, db.get_run(run_id), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.attention_kind is InboxAttentionKind.DECISION_REQUIRED
    assert item.review_verdict is ReviewVerdict.PASS
    assert item.candidate_sha == candidate


def test_d_ready_without_review_required_needs_decision(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_d_case"
    candidate = _sha("candidate-r3d-d")
    _decision_run(db, run_id, candidate, review_json=_skip_json(run_id))
    assert project_candidate_state(db, run_id).state is CandidateState.READY

    item = classify_run(db, db.get_run(run_id), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.attention_kind is InboxAttentionKind.DECISION_REQUIRED


def test_e_ready_with_review_required_needs_review(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_e_case"
    candidate = _sha("candidate-r3d-e")
    _decision_run(db, run_id, candidate, review_json=_required_json(run_id))

    item = classify_run(db, db.get_run(run_id), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.attention_kind is InboxAttentionKind.REVIEW_REQUIRED


def test_f_ready_with_unknown_requirement_needs_review(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_f_case"
    candidate = _sha("candidate-r3d-f")
    _decision_run(db, run_id, candidate)
    projection = project_candidate_state(db, run_id)
    assert projection.state is CandidateState.READY
    assert projection.review_required is None

    item = classify_run(db, db.get_run(run_id), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    # Unknown/legacy requirement is never assumed decision-ready.
    assert item.attention_kind is InboxAttentionKind.REVIEW_REQUIRED


def test_g_in_review_excluded(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_g_case"
    candidate = _sha("candidate-r3d-g")
    _decision_run(db, run_id, candidate)
    _review_dispatch(db, run_id, "assoc-g", candidate)
    assert project_candidate_state(db, run_id).state is CandidateState.IN_REVIEW

    assert classify_run(db, db.get_run(run_id), project_id=None, project_path="p") is None  # type: ignore[arg-type]


def test_h_accepted_excluded(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_h_case"
    candidate = _sha("candidate-r3d-h")
    _decision_run(db, run_id, candidate)
    _accept(db, run_id, candidate)
    assert project_candidate_state(db, run_id).state is CandidateState.ACCEPTED

    assert classify_run(db, db.get_run(run_id), project_id=None, project_path="p") is None  # type: ignore[arg-type]


def test_i_rejected_excluded(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_i_case"
    candidate = _sha("candidate-r3d-i")
    _decision_run(db, run_id, candidate)
    OrchestratorService(db=db).reject_candidate(run_id)
    assert project_candidate_state(db, run_id).state is CandidateState.REJECTED

    assert classify_run(db, db.get_run(run_id), project_id=None, project_path="p") is None  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# J-K. Run fallback with no trustworthy candidate.
# ---------------------------------------------------------------------------


def test_j_failed_without_candidate_is_run_failed(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    _seed_run(db, "run_r3d_j_case", status=RunStatus.FAILED)

    item = classify_run(db, db.get_run("run_r3d_j_case"), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.attention_kind is InboxAttentionKind.RUN_FAILED
    assert item.candidate_sha is None


def test_k_orphaned_without_candidate_is_run_orphaned(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    _seed_run(db, "run_r3d_k_case", status=RunStatus.ORPHANED)

    item = classify_run(db, db.get_run("run_r3d_k_case"), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.attention_kind is InboxAttentionKind.RUN_ORPHANED


# ---------------------------------------------------------------------------
# L. Candidate precedence over run status.
# ---------------------------------------------------------------------------


def test_l_candidate_precedence_over_failed_status(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_l_case"
    candidate = _sha("candidate-r3d-l")
    _decision_run(
        db, run_id, candidate, review_json=_skip_json(run_id),
        status=RunStatus.FAILED,
    )
    assert project_candidate_state(db, run_id).state is CandidateState.READY

    item = classify_run(db, db.get_run(run_id), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.attention_kind is InboxAttentionKind.DECISION_REQUIRED
    assert item.candidate_sha == candidate


def test_l_snapshot_has_exactly_one_row(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-l")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    run_id = new_run_id(identity)
    _decision_run(
        db, run_id, _sha("candidate-r3d-l2"),
        review_json=_skip_json(run_id), status=RunStatus.FAILED,
        project_id=identity.project_id,
    )
    snapshot = build_inbox(manager)
    matches = [i for i in snapshot.items if i.run_id == run_id]
    assert len(matches) == 1
    assert matches[0].attention_kind is InboxAttentionKind.DECISION_REQUIRED


# ---------------------------------------------------------------------------
# M. Candidate A history cannot affect current candidate B.
# ---------------------------------------------------------------------------


def test_m_old_candidate_review_does_not_leak(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_m_case"
    sha_a = _sha("candidate-r3d-m-a")
    sha_b = _sha("candidate-r3d-m-b")
    _seed_run(db, run_id)
    _gate_pass(db, run_id, f"{run_id}-a1", 1, sha_a)
    _review_dispatch(db, run_id, "assoc-m", sha_a)
    _review(db, run_id, "assoc-m", ReviewVerdict.BLOCK)
    assert project_candidate_state(db, run_id).state is CandidateState.REVIEWED
    # A newer gate-passed candidate B supersedes A.
    _gate_pass(db, run_id, f"{run_id}-a2", 2, sha_b)

    projection = project_candidate_state(db, run_id)
    assert projection.candidate_sha == sha_b
    assert projection.state is CandidateState.READY

    item = classify_run(db, db.get_run(run_id), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.candidate_sha == sha_b
    assert item.attention_kind is InboxAttentionKind.REVIEW_REQUIRED


# ---------------------------------------------------------------------------
# N. Malformed/noncanonical gate-passed candidate fails closed.
# ---------------------------------------------------------------------------


def test_n_noncanonical_candidate_never_decision(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_n_case"
    malformed = "abc"
    _seed_run(db, run_id)
    _gate_pass(db, run_id, f"{run_id}-a1", 1, malformed)

    # Characterize the existing R3-A result: still READY for the SHA.
    projection = project_candidate_state(db, run_id)
    assert projection.candidate_sha == malformed
    assert projection.state is CandidateState.READY

    # Inbox fails closed: no decision action on noncanonical authority.
    assert classify_run(db, db.get_run(run_id), project_id=None, project_path="p") is None  # type: ignore[arg-type]


def test_n_noncanonical_failed_run_is_run_level_only(tmp_path: Path) -> None:
    db = _single_db(tmp_path)
    run_id = "run_r3d_n2_case"
    _seed_run(db, run_id, status=RunStatus.FAILED)
    _gate_pass(db, run_id, f"{run_id}-a1", 1, "not-a-canonical-sha")

    item = classify_run(db, db.get_run(run_id), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert item is not None
    assert item.attention_kind is InboxAttentionKind.RUN_FAILED
    assert item.candidate_sha is None


# ---------------------------------------------------------------------------
# O. Terminal transition removes the row automatically.
# ---------------------------------------------------------------------------


def test_o_reject_removes_inbox_row(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-o")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    run_id = new_run_id(identity)
    candidate = _sha("candidate-r3d-o")
    _decision_run(
        db, run_id, candidate, review_json=_skip_json(run_id),
        project_id=identity.project_id,
    )
    before = build_inbox(manager)
    assert before.items and any(
        i.run_id == run_id
        and i.attention_kind is InboxAttentionKind.DECISION_REQUIRED
        for i in before.items
    )

    OrchestratorService(db=Database(db.db_path)).reject_candidate(run_id)

    after = build_inbox(manager)
    assert all(i.run_id != run_id for i in after.items)
    assert project_candidate_state(
        Database(db.db_path), run_id
    ).state is CandidateState.REJECTED


# ---------------------------------------------------------------------------
# P. Real AcceptEngine-shaped ACCEPTED evidence removes the row.
# ---------------------------------------------------------------------------


def test_p_accepted_evidence_removes_inbox_row(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-p")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    run_id = new_run_id(identity)
    candidate = _sha("candidate-r3d-p")
    _decision_run(
        db, run_id, candidate, review_json=_skip_json(run_id),
        project_id=identity.project_id,
    )
    assert any(i.run_id == run_id for i in build_inbox(manager).items)

    _accept(Database(db.db_path), run_id, candidate)
    fresh = Database(db.db_path)
    assert project_candidate_state(fresh, run_id).state is CandidateState.ACCEPTED

    assert all(i.run_id != run_id for i in build_inbox(manager).items)


# ---------------------------------------------------------------------------
# Q. Corrupt project marker.
# ---------------------------------------------------------------------------


def test_q_corrupt_marker_isolated(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-a")
    manager = _manager(tmp_path)
    db_a = _project_db(tmp_path, manager, repo_a)
    id_a = compute_project_identity(repo_a)
    run_a = new_run_id(id_a)
    _decision_run(
        db_a, run_a, _sha("candidate-r3d-q"),
        review_json=_skip_json(run_a), project_id=id_a.project_id,
    )

    corrupt = manager.state_root / "bogus-project-dir"
    corrupt.mkdir(parents=True, exist_ok=True)
    (corrupt / "project.json").write_text("not json{{{", encoding="utf-8")

    registry = ProjectRegistry(path=tmp_path / "ui.json")
    assert registry.add_project(repo_a)[0] is True
    assert registry.select(str(repo_a))[0] is True
    active = str(repo_a.resolve())

    snapshot = build_inbox(manager)
    assert snapshot.unreadable_projects >= 1
    got = [i for i in snapshot.items if i.run_id == run_a]
    assert len(got) == 1
    # No row is invented from the corrupt directory, and nothing from
    # it is attributed to the active project.
    assert all(i.project_id == id_a.project_id for i in snapshot.items)
    assert all(i.project_path != str(corrupt) for i in snapshot.items)
    assert got[0].project_path != active or got[0].project_id == id_a.project_id


# ---------------------------------------------------------------------------
# R. Legacy/global store.
# ---------------------------------------------------------------------------


def test_r_legacy_failed_run_appears_once(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-r")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    scoped = new_run_id(identity)
    _decision_run(
        db, scoped, _sha("candidate-r3d-r"),
        review_json=_skip_json(scoped), project_id=identity.project_id,
    )

    legacy_db = manager.legacy().db
    legacy_db.create_run(
        Run(
            id="run_legacy0001",
            task="legacy failure",
            target_repo="/tmp/legacy-target",
            base_commit=_sha("base-legacy"),
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.FAILED,
            created_at=_CREATED,
            completed_at=_CREATED,
        )
    )

    snapshot = build_inbox(manager)
    ids = [i.run_id for i in snapshot.items]
    # No duplication anywhere: project-scoped state never repeats as
    # legacy rows and every run appears at most once.
    assert len(ids) == len(set(ids))
    assert ids.count("run_legacy0001") == 1
    assert ids.count(scoped) == 1
    legacy_item = next(i for i in snapshot.items if i.run_id == "run_legacy0001")
    assert legacy_item.attention_kind is InboxAttentionKind.RUN_FAILED
    assert legacy_item.project_path == "/tmp/legacy-target"


# ---------------------------------------------------------------------------
# S. Deterministic ordering.
# ---------------------------------------------------------------------------


def test_s_newest_first_stable_tiebreak(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-s")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)

    def _failed(run_id: str, created: str) -> None:
        _seed_run(
            db, run_id, status=RunStatus.FAILED, created=created,
            completed=created, project_id=identity.project_id,
        )

    _failed("run_s_old", "2026-09-24T09:00:00+00:00")
    _failed("run_s_new", "2026-09-24T11:00:00+00:00")
    _failed("run_s_tie_b", "2026-09-24T10:00:00+00:00")
    _failed("run_s_tie_a", "2026-09-24T10:00:00+00:00")

    snapshot = build_inbox(manager)
    assert [i.run_id for i in snapshot.items] == [
        "run_s_new",
        "run_s_tie_a",
        "run_s_tie_b",
        "run_s_old",
    ]


# ---------------------------------------------------------------------------
# T. Bound.
# ---------------------------------------------------------------------------


def test_t_projection_bound_is_deterministic(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-t")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    for index in range(12):
        run_id = f"run_t_{index:02d}"
        created = f"2026-09-24T10:00:{index:02d}+00:00"
        _seed_run(
            db,
            run_id,
            status=RunStatus.FAILED,
            created=created,
            completed=created,
            project_id=identity.project_id,
        )
    first = build_inbox(manager, limit=5)
    second = build_inbox(manager, limit=5)
    assert len(first.items) == 5
    assert [i.run_id for i in first.items] == [i.run_id for i in second.items]
    assert first.items[0].run_id == "run_t_11"
    assert first.items[-1].run_id == "run_t_07"


def test_t_default_bound_caps_at_100(tmp_path: Path) -> None:
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-t2")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    for index in range(DEFAULT_MAX_ITEMS + 10):
        _seed_run(
            db,
            f"run_t2_{index:03d}",
            status=RunStatus.FAILED,
            created="2026-09-24T10:00:00+00:00",
            project_id=identity.project_id,
        )
    snapshot = build_inbox(manager)
    assert len(snapshot.items) == DEFAULT_MAX_ITEMS


# ---------------------------------------------------------------------------
# U. Web parity.
# ---------------------------------------------------------------------------


def test_u_web_parity(tmp_path: Path, monkeypatch: object) -> None:
    import pytest as _pytest

    assert isinstance(monkeypatch, _pytest.MonkeyPatch)
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-a")
    state = tmp_path / "xdg-state"
    config = tmp_path / "xdg-config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    from fastapi.testclient import TestClient

    from saberops.web import create_app

    state_root = tmp_path / "projects"
    state_root.mkdir(parents=True, exist_ok=True)
    manager = ProjectRuntimeManager(state_root=state_root)
    db_a = manager.for_repo(repo_a).db
    id_a = compute_project_identity(repo_a)
    run_a = new_run_id(id_a)
    candidate = _sha("candidate-r3d-u")
    _decision_run(
        db_a, run_a, candidate, review_json=_skip_json(run_a),
        project_id=id_a.project_id,
    )
    run_b = new_run_id(id_a)
    _seed_run(db_a, run_b, status=RunStatus.FAILED, project_id=id_a.project_id)

    registry = ProjectRegistry(path=tmp_path / "ui.json")
    assert registry.add_project(repo_a)[0] is True
    assert registry.select(str(repo_a))[0] is True
    client = TestClient(create_app(projects=registry, state_root=state_root))

    payload = client.get("/inbox.json").json()
    assert payload["count"] == 2
    by_id = {row["run_id"]: row for row in payload["items"]}
    assert set(by_id) == {run_a, run_b}
    assert by_id[run_a]["attention_kind"] == "DECISION_REQUIRED"
    assert by_id[run_b]["attention_kind"] == "RUN_FAILED"
    assert by_id[run_a]["candidate_sha"] == candidate

    page = client.get("/inbox")
    assert page.status_code == 200
    assert "Nothing needs your attention." not in page.text
    assert "Needs decision" in page.text
    assert "Run failed" in page.text
    assert candidate[:8] in page.text
    for rid in (run_a, run_b):
        assert f"/runs/{rid}" in page.text
    # Inbox is in the primary navigation.
    assert 'href="/inbox"' in page.text
    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert 'href="/inbox"' in dashboard.text


def test_u_empty_state(tmp_path: Path, monkeypatch: object) -> None:
    import pytest as _pytest

    assert isinstance(monkeypatch, _pytest.MonkeyPatch)
    state = tmp_path / "xdg-state"
    config = tmp_path / "xdg-config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    from fastapi.testclient import TestClient

    from saberops.web import create_app

    state_root = tmp_path / "projects"
    state_root.mkdir(parents=True, exist_ok=True)
    registry = ProjectRegistry(path=tmp_path / "ui.json")
    client = TestClient(create_app(projects=registry, state_root=state_root))

    assert client.get("/inbox.json").json()["items"] == []
    page = client.get("/inbox")
    assert page.status_code == 200
    assert "Nothing needs your attention." in page.text


# ---------------------------------------------------------------------------
# V. Read-only.
# ---------------------------------------------------------------------------


def _durable_counts(manager: ProjectRuntimeManager) -> dict[str, int]:
    runtimes, _ = manager.list_known_runtimes()
    totals = {
        "runs": 0,
        "attempts": 0,
        "checks": 0,
        "reviews": 0,
        "events": 0,
    }
    for runtime in runtimes:
        db = runtime.db
        runs = db.list_runs(limit=100_000)
        totals["runs"] += len(runs)
        for run in runs:
            attempts = db.get_attempts_for_run(run.id)
            totals["attempts"] += len(attempts)
            totals["checks"] += len(db.get_checks_for_run(run.id))
            totals["reviews"] += len(db.get_reviews_for_run(run.id))
            totals["events"] += len(db.get_events(run.id, limit=100_000))
    return totals


def test_v_reads_add_no_durable_state(tmp_path: Path) -> None:
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-a")
    repo_b = _make_repo(repos, "proj-b")
    manager = _manager(tmp_path)
    db_a = _project_db(tmp_path, manager, repo_a)
    db_b = _project_db(tmp_path, manager, repo_b)
    id_a = compute_project_identity(repo_a)
    id_b = compute_project_identity(repo_b)
    run_a = new_run_id(id_a)
    candidate = _sha("candidate-r3d-v")
    _decision_run(
        db_a, run_a, candidate, review_json=_skip_json(run_a),
        project_id=id_a.project_id,
    )
    _review_dispatch(db_a, run_a, "assoc-v", candidate)
    _review(db_a, run_a, "assoc-v", ReviewVerdict.PASS)
    run_b = new_run_id(id_b)
    _seed_run(db_b, run_b, status=RunStatus.FAILED, project_id=id_b.project_id)

    registry = ProjectRegistry(path=tmp_path / "ui.json")
    assert registry.add_project(repo_a)[0] is True
    assert registry.select(str(repo_a))[0] is True

    build_inbox(manager)  # warm caches/databases before measuring
    before = _durable_counts(manager)
    registry_before = (tmp_path / "ui.json").read_text(encoding="utf-8")

    build_inbox(manager)
    build_inbox(manager, limit=5)
    after = _durable_counts(ProjectRuntimeManager(
        state_root=manager.state_root, owner_db_path=tmp_path / "owner.db"
    ))
    assert after == before
    assert (tmp_path / "ui.json").read_text(encoding="utf-8") == registry_before


def test_v_web_reads_add_no_durable_state(
    tmp_path: Path, monkeypatch: object
) -> None:
    import pytest as _pytest

    assert isinstance(monkeypatch, _pytest.MonkeyPatch)
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-a")
    state = tmp_path / "xdg-state"
    config = tmp_path / "xdg-config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    from fastapi.testclient import TestClient

    from saberops.web import create_app

    state_root = tmp_path / "projects"
    state_root.mkdir(parents=True, exist_ok=True)
    seed_manager = ProjectRuntimeManager(state_root=state_root)
    db_a = seed_manager.for_repo(repo_a).db
    id_a = compute_project_identity(repo_a)
    run_a = new_run_id(id_a)
    _decision_run(
        db_a, run_a, _sha("candidate-r3d-vw"),
        review_json=_skip_json(run_a), project_id=id_a.project_id,
    )

    registry = ProjectRegistry(path=tmp_path / "ui.json")
    assert registry.add_project(repo_a)[0] is True
    assert registry.select(str(repo_a))[0] is True
    client = TestClient(create_app(projects=registry, state_root=state_root))

    probe = ProjectRuntimeManager(state_root=state_root)
    before = _durable_counts(probe)
    registry_before = (tmp_path / "ui.json").read_text(encoding="utf-8")

    assert client.get("/inbox").status_code == 200
    assert client.get("/inbox.json").status_code == 200
    assert client.get("/inbox").status_code == 200

    after = _durable_counts(ProjectRuntimeManager(state_root=state_root))
    assert after == before
    assert (tmp_path / "ui.json").read_text(encoding="utf-8") == registry_before
    assert json.loads(registry_before)["active"] == str(repo_a.resolve())


# ---------------------------------------------------------------------------
# R3-D.1 regressions: read-only enumeration, single SHA authority, no
# universal scan.
# ---------------------------------------------------------------------------


def _fs_snapshot(root: Path) -> set[Path]:
    """Return every path under ``root`` (files and directories)."""
    return {p.relative_to(root) for p in root.rglob("*")}


def test_r3d1_marker_without_db_is_read_only(tmp_path: Path) -> None:
    """A persisted marker with no usable DB is skipped, never materialized.

    Negative control for the R3-D.1 read-only contract: only
    ``state_root/<ns>/project.json`` exists (no project database, no
    owner database).  The real Inbox enumeration/build path must not
    create either database, must create no other file, must omit the
    source, and must count it as unreadable.
    """
    state_root = tmp_path / "projects"
    state_root.mkdir(parents=True, exist_ok=True)
    owner_path = tmp_path / "owner.db"
    repo = _make_repo(tmp_path / "repos", "proj-nodb")
    identity = compute_project_identity(repo)
    marker_dir = state_root / identity.namespace
    marker_dir.mkdir(parents=True, exist_ok=True)
    (marker_dir / "project.json").write_text(identity.to_json(), encoding="utf-8")

    manager = ProjectRuntimeManager(
        state_root=state_root, owner_db_path=owner_path
    )
    project_db = marker_dir / "orchestrator.db"
    assert not project_db.exists()
    assert not owner_path.exists()
    before = _fs_snapshot(tmp_path)

    snapshot = build_inbox(manager)
    manager.shutdown()

    assert not project_db.exists()
    assert not owner_path.exists()
    assert _fs_snapshot(tmp_path) == before
    assert snapshot.items == ()
    assert snapshot.unreadable_projects >= 1


def test_r3d1_existing_db_opened_read_only(tmp_path: Path) -> None:
    """An Inbox read writes no durable content and creates no database.

    SQLite's own read-only open may materialize transient ``-shm``
    shared-memory backing (and a zero-length ``-wal``); that is not
    durable state.  The proof is: the expected row appears, every
    ``*.db`` file's bytes (and schema version) are unchanged, no new
    database file appears, and any ``-wal`` left behind is empty.
    """
    from saberops.db import get_db_schema_version

    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-ro")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    run_id = new_run_id(identity)
    candidate = _sha("candidate-r3d1-ro")
    _decision_run(
        db, run_id, candidate, review_json=_skip_json(run_id),
        project_id=identity.project_id,
    )
    manager.shutdown()

    project_db = manager.state_root / identity.namespace / "orchestrator.db"
    owner_db = tmp_path / "owner.db"
    assert project_db.is_file()
    tracked = [path for path in (project_db, owner_db) if path.is_file()]
    assert tracked
    content_before = {path: path.read_bytes() for path in tracked}
    version_before = {
        path: get_db_schema_version(path) for path in tracked
    }
    db_files_before = {
        path for path in _fs_snapshot(tmp_path) if path.suffix == ".db"
    }

    fresh = ProjectRuntimeManager(
        state_root=manager.state_root, owner_db_path=tmp_path / "owner.db"
    )
    snapshot = build_inbox(fresh)
    fresh.shutdown()

    assert {item.run_id for item in snapshot.items} == {run_id}
    assert {
        path for path in _fs_snapshot(tmp_path) if path.suffix == ".db"
    } == db_files_before
    for path, content in content_before.items():
        assert path.read_bytes() == content
        assert get_db_schema_version(path) == version_before[path]
    # No durable content in any WAL left behind by the read.
    for wal in tmp_path.rglob("*.db-wal"):
        assert wal.stat().st_size == 0


def test_r3d1_missing_legacy_db_not_created(tmp_path: Path) -> None:
    """A valid project appears without materializing an absent legacy DB."""
    manager = _manager(tmp_path)
    repo = _make_repo(tmp_path / "repos", "proj-nolegacy")
    db = _project_db(tmp_path, manager, repo)
    identity = compute_project_identity(repo)
    run_id = new_run_id(identity)
    _seed_run(db, run_id, status=RunStatus.FAILED, project_id=identity.project_id)
    manager.shutdown()

    owner_path = tmp_path / "owner.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        sidecar = tmp_path / f"owner.db{suffix}"
        if sidecar.exists():
            sidecar.unlink()
    assert not owner_path.exists()

    fresh = ProjectRuntimeManager(
        state_root=manager.state_root, owner_db_path=owner_path
    )
    snapshot = build_inbox(fresh)
    fresh.shutdown()

    assert [item.run_id for item in snapshot.items] == [run_id]
    assert snapshot.items[0].attention_kind is InboxAttentionKind.RUN_FAILED
    assert not owner_path.exists()
    # An absent legacy store is normal: no duplicate source, no warning.
    assert snapshot.unreadable_projects == 0


def test_r3d1_corrupt_db_counted_unreadable(tmp_path: Path) -> None:
    """A corrupt store invents no rows but still counts as unreadable."""
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-good")
    repo_b = _make_repo(repos, "proj-bad")
    manager = _manager(tmp_path)
    db_a = _project_db(tmp_path, manager, repo_a)
    db_b = _project_db(tmp_path, manager, repo_b)
    id_a = compute_project_identity(repo_a)
    id_b = compute_project_identity(repo_b)
    run_a = new_run_id(id_a)
    _decision_run(
        db_a, run_a, _sha("candidate-r3d1-good"),
        review_json=_skip_json(run_a), project_id=id_a.project_id,
    )
    run_b = new_run_id(id_b)
    _seed_run(db_b, run_b, status=RunStatus.FAILED, project_id=id_b.project_id)
    manager.shutdown()

    bad_db = manager.state_root / id_b.namespace / "orchestrator.db"
    assert bad_db.is_file()
    garbage = b"this is not a sqlite database" * 64
    bad_db.write_bytes(garbage)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = bad_db.parent / f"{bad_db.name}{suffix}"
        if sidecar.exists():
            sidecar.unlink()

    fresh = ProjectRuntimeManager(
        state_root=manager.state_root, owner_db_path=tmp_path / "owner.db"
    )
    snapshot = build_inbox(fresh)
    fresh.shutdown()

    got = {item.run_id for item in snapshot.items}
    assert run_a in got
    assert run_b not in got
    assert snapshot.unreadable_projects >= 1
    # No replacement database or schema was written over the corrupt file.
    assert bad_db.read_bytes() == garbage


def test_r3d1_canonical_sha_single_authority(tmp_path: Path) -> None:
    """Inbox consumes the candidate-lifecycle SHA predicate (no second one)."""
    import saberops.inbox as inbox_module
    from saberops.candidate_lifecycle import is_canonical_candidate_sha

    # The Inbox reuses the one lifecycle predicate object; it defines no
    # regex/table/policy of its own.
    assert inbox_module.is_canonical_candidate_sha is is_canonical_candidate_sha
    assert not hasattr(inbox_module, "_is_canonical_sha")
    assert not hasattr(inbox_module, "_CANONICAL_SHA_RE")

    # The shared predicate itself is strict: canonical passes, upper
    # case, truncated, non-string, and malformed values fail closed.
    canonical = _sha("candidate-r3d1-auth")
    assert is_canonical_candidate_sha(canonical) is True
    assert is_canonical_candidate_sha(canonical.upper()) is False
    assert is_canonical_candidate_sha(canonical[:39]) is False
    assert is_canonical_candidate_sha("abc") is False
    assert is_canonical_candidate_sha(None) is False
    assert is_canonical_candidate_sha(123) is False

    # Behavior preserved through the single authority: canonical
    # READY and REVIEWED need a decision, malformed never does.
    db = _single_db(tmp_path)
    run_ok = "run_r3d1_auth_ok"
    _decision_run(db, run_ok, canonical, review_json=_skip_json(run_ok))
    ready = classify_run(db, db.get_run(run_ok), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert ready is not None
    assert ready.attention_kind is InboxAttentionKind.DECISION_REQUIRED
    assert ready.candidate_sha == canonical

    _review_dispatch(db, run_ok, "assoc-r3d1", canonical)
    _review(db, run_ok, "assoc-r3d1", ReviewVerdict.PASS)
    reviewed = classify_run(db, db.get_run(run_ok), project_id=None, project_path="p")  # type: ignore[arg-type]
    assert reviewed is not None
    assert reviewed.attention_kind is InboxAttentionKind.DECISION_REQUIRED
    assert reviewed.candidate_sha == canonical

    run_bad = "run_r3d1_auth_bad"
    _seed_run(db, run_bad)
    _gate_pass(db, run_bad, f"{run_bad}-a1", 1, "abc")
    assert classify_run(db, db.get_run(run_bad), project_id=None, project_path="p") is None  # type: ignore[arg-type]


def test_r3d1_no_universal_inbox_scan(tmp_path: Path, monkeypatch: object) -> None:
    """Ordinary pages never scan the Inbox; /inbox(/json) scan exactly once."""
    import pytest as _pytest

    assert isinstance(monkeypatch, _pytest.MonkeyPatch)
    repos = tmp_path / "repos"
    repo_a = _make_repo(repos, "proj-a")
    state = tmp_path / "xdg-state"
    config = tmp_path / "xdg-config"
    state.mkdir(parents=True)
    config.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))

    from fastapi.testclient import TestClient

    import saberops.inbox as inbox_module
    from saberops.web import create_app

    state_root = tmp_path / "projects"
    state_root.mkdir(parents=True, exist_ok=True)
    seed_manager = ProjectRuntimeManager(state_root=state_root)
    db_a = seed_manager.for_repo(repo_a).db
    id_a = compute_project_identity(repo_a)
    run_a = new_run_id(id_a)
    candidate = _sha("candidate-r3d1-scan")
    _decision_run(
        db_a, run_a, candidate, review_json=_skip_json(run_a),
        project_id=id_a.project_id,
    )
    seed_manager.shutdown()

    registry = ProjectRegistry(path=tmp_path / "ui.json")
    assert registry.add_project(repo_a)[0] is True
    assert registry.select(str(repo_a))[0] is True
    client = TestClient(create_app(projects=registry, state_root=state_root))

    real_build = inbox_module.build_inbox
    calls = {"n": 0}

    def _counting(
        manager: ProjectRuntimeManager, *, limit: int = DEFAULT_MAX_ITEMS
    ) -> InboxSnapshot:
        calls["n"] += 1
        return real_build(manager, limit=limit)

    monkeypatch.setattr(inbox_module, "build_inbox", _counting)

    dashboard = client.get("/")
    assert dashboard.status_code == 200
    assert calls["n"] == 0
    # The plain Inbox navigation link remains; no count badge is computed.
    assert 'href="/inbox"' in dashboard.text
    assert "rail-badge" not in dashboard.text

    calls["n"] = 0
    page = client.get("/inbox")
    assert page.status_code == 200
    assert calls["n"] == 1
    assert f"/runs/{run_a}" in page.text
    assert candidate[:8] in page.text

    calls["n"] = 0
    response = client.get("/inbox.json")
    assert response.status_code == 200
    assert calls["n"] == 1
    payload = response.json()
    assert payload["count"] == 1
    assert [row["run_id"] for row in payload["items"]] == [run_a]
    assert payload["items"][0]["attention_kind"] == "DECISION_REQUIRED"
    # HTML/JSON parity: the same single projection supplies both.
    assert f"/runs/{payload['items'][0]['run_id']}" in page.text
