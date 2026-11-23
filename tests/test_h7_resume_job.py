"""H7: one recovery tool (``resume_job``) over MCP and the one owner verb.

The lane's contract:

* ``resume_job`` resumes one failed job ``from: scratch | last_candidate`` with
  an optional note, mapping onto the existing retry and continue paths.
* It fails with a reason when neither applies.
* One successor per call (idempotent).
* A rejection note reaches the next attempt.
* ``try-worker`` is folded in as an option; the old verbs are aliases for one
  release and say so.

These tests use a real durable store, a real toy repository and the real
service/CLI/MCP seams; only the detached execution launch is stubbed.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from saberops.cli import main
from saberops.config import resolve_run_config
from saberops.db import Database, current_iso_timestamp
from saberops.jobs.models import JobState
from saberops.jobs.service import submit_job
from saberops.jobs.store import (
    get_job,
    mark_dispatched,
    pending_steering_notes_for_run,
    reconcile_transition,
)
from saberops.mcp.errors import InvalidParamsError, ToolError
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.models import (
    Attempt,
    AttemptStatus,
    CheckResult,
    DispatchEvidence,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    ReviewResult,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.steering import steering_section_for_run
from saberops.supervisor import RunSupervisor

PROBE_TASK = "H7 resume probe"


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


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "h7@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "H7"], cwd=path, check=True)
    (path / "README.md").write_text("# h7\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return path


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _seed_run(
    db: Database,
    repo: Path,
    run_id: str,
    status: RunStatus,
    *,
    with_contracts: bool = False,
) -> Run:
    config = resolve_run_config(
        task=PROBE_TASK,
        target_repo=repo,
        manual_tier=Tier.T1,
        routing_mode="manual",
        gate_command="true",
    )
    routing_json: str | None = None
    control_json: str | None = None
    control_digest: str | None = None
    if with_contracts:
        from saberops.control_plane import (
            freeze_control_plane_policy,
            load_control_plane_policy,
        )
        from saberops.routing_config import load_effective_routing, snapshot_to_payload

        created = current_iso_timestamp()
        routing_json = json.dumps(snapshot_to_payload(load_effective_routing(), created))
        frozen_control = freeze_control_plane_policy(load_control_plane_policy())
        control_json = frozen_control.as_json()
        control_digest = frozen_control.digest
    run = Run(
        id=run_id,
        task=PROBE_TASK,
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
    )
    db.create_run(run)
    return run


def _failed_job(db: Database, repo: Path, run_id: str) -> str:
    """A FAILED job whose one run is ``run_id`` (the ordinary dispatch chain)."""
    submitted = submit_job(db, repo=repo, goal=PROBE_TASK)
    job_id = submitted.job.job_id
    mark_dispatched(db, job_id, run_id)
    reconcile_transition(db, job_id, to=JobState.FAILED, failure_reason="RUN_FAILED")
    return job_id


def _stub_launch(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    launches: list[str] = []
    monkeypatch.setattr(
        RunSupervisor, "_launch", lambda self, run_id, config: launches.append(run_id)
    )
    return launches


def _registry(tmp_path: Path, db: Database) -> Any:
    lease = OrchestratorLease(tmp_path / "lease.json", holder="h7-test", pid=1)
    lease.acquire()
    context = ServerContext(
        db_path=db.db_path,
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(tmp_path,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=lease,
    )
    return build_default_registry(context)


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


# ---------------------------------------------------------------------------
# MCP: the tool exists and resumes a failed job from scratch
# ---------------------------------------------------------------------------


def test_resume_job_is_exposed_over_mcp() -> None:
    from saberops.mcp.tools import MUTATING_TOOLS, TOOL_NAMES

    assert "resume_job" in TOOL_NAMES
    assert "resume_job" in MUTATING_TOOLS


def test_resume_job_from_scratch_links_one_successor_and_delivers_the_note(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "scratch-repo")
    db = Database(isolated / "scratch.db")
    _seed_run(db, repo, "run_scratch_src", RunStatus.FAILED)
    job_id = _failed_job(db, repo, "run_scratch_src")
    launches = _stub_launch(monkeypatch)
    registry = _registry(isolated, db)

    payload = registry.call(
        "resume_job",
        {"job_id": job_id, "from": "scratch", "note": "Use the existing helper."},
    )
    successor = payload["run_id"]
    assert payload["operation"] == "resume_job"
    assert payload["job_id"] == job_id
    assert payload["from"] == "scratch"
    assert payload["source_run_id"] == "run_scratch_src"
    assert payload["already_resumed"] is False
    assert launches == [successor]

    job = get_job(db, job_id)
    assert job is not None
    assert job.state is JobState.RUNNING
    assert job.current_run_id == successor
    assert job.failure_reason is None
    assert job.run_ids == ("run_scratch_src", successor)

    # The note is durable and is the section the successor's first attempt
    # renders; it reaches the next attempt rather than being lost with the job.
    notes = pending_steering_notes_for_run(db, successor)
    assert [note.body for note in notes] == ["Use the existing helper."]
    section, covered = steering_section_for_run(db, successor)
    assert "Use the existing helper." in section
    assert [note.note_id for note in covered] == [notes[0].note_id]

    # One successor per call: a repeat returns the running successor.
    again = registry.call("resume_job", {"job_id": job_id, "from": "scratch"})
    assert again["run_id"] == successor
    assert again["already_resumed"] is True
    assert launches == [successor]


def test_rejection_note_reaches_the_continued_candidate_attempt(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The owner rejects a candidate with a note; that note is the resume note."""
    from saberops.candidate_lifecycle import (
        project_candidate_state,
        reject_current_candidate,
    )
    from saberops.models import CandidateState

    repo = _git_repo(isolated / "continue-repo")
    db = Database(isolated / "continue.db")
    _seed_run(db, repo, "run_candidate_src", RunStatus.FAILED, with_contracts=True)
    candidate = _head(repo)
    db.create_attempt(
        Attempt(
            id="run_candidate_src-a1",
            run_id="run_candidate_src",
            attempt_number=1,
            provider="codex",
            model="h7-model",
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=current_iso_timestamp(),
        )
    )
    db.create_check(
        CheckResult(
            id="run_candidate_src-a1-check",
            attempt_id="run_candidate_src-a1",
            gate_command="true",
            exit_code=0,
            stdout="ok",
            stderr="",
            passed=True,
            created_at=current_iso_timestamp(),
        )
    )
    db.create_dispatch_evidence(
        DispatchEvidence(
            id="run_candidate_src-review",
            run_id="run_candidate_src",
            association_id="run_candidate_src-review",
            stage=DispatchStage.REVIEW,
            role=DispatchRole.CHECK,
            tier=Tier.T1,
            dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
            requested_provider="opencode",
            requested_model="reviewer",
            candidate_sha=candidate,
            started_at=current_iso_timestamp(),
        )
    )
    db.create_review(
        ReviewResult(
            id="run_candidate_src-review",
            run_id="run_candidate_src",
            provider="opencode",
            model="reviewer",
            verdict=ReviewVerdict.BLOCK,
            summary="BLOCK summary",
            details="the public API changed",
            created_at=current_iso_timestamp(),
        )
    )
    job_id = _failed_job(db, repo, "run_candidate_src")

    rejection_note = "Rejected: keep the public API unchanged."
    rejected = reject_current_candidate(db, "run_candidate_src")
    assert rejected.state is CandidateState.REJECTED
    assert project_candidate_state(db, "run_candidate_src").state is CandidateState.REJECTED

    launches = _stub_launch(monkeypatch)
    registry = _registry(isolated, db)

    payload = registry.call(
        "resume_job",
        {
            "job_id": job_id,
            "from": "last_candidate",
            "note": rejection_note,
        },
    )
    successor = payload["run_id"]
    assert payload["from"] == "last_candidate"
    assert launches == [successor]
    child = db.get_run(successor)
    assert child is not None and child.config_json
    assert child.base_commit == _head(repo)
    assert db.get_continuation("run_candidate_src") == successor
    job = get_job(db, job_id)
    assert job is not None and job.state is JobState.RUNNING
    assert job.current_run_id == successor
    # The rejection note is the pending steering note of the successor's
    # first attempt: it reaches the next attempt instead of being lost.
    assert [
        note.body for note in pending_steering_notes_for_run(db, successor)
    ] == [rejection_note]


def test_resume_job_refuses_with_a_reason_when_neither_path_applies(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "refuse-repo")
    db = Database(isolated / "refuse.db")
    submissions = submit_job(db, repo=repo, goal=PROBE_TASK)
    queued_job = submissions.job.job_id
    _seed_run(db, repo, "run_done_src", RunStatus.FAILED, with_contracts=True)
    job_id = _failed_job(db, repo, "run_done_src")
    _stub_launch(monkeypatch)
    registry = _registry(isolated, db)

    with pytest.raises(ToolError, match="only a failed job can be resumed"):
        registry.call("resume_job", {"job_id": queued_job, "from": "scratch"})

    # A failed run with no committed candidate cannot continue.
    with pytest.raises(ToolError, match="No committed candidate"):
        registry.call(
            "resume_job", {"job_id": job_id, "from": "last_candidate"}
        )

    with pytest.raises(InvalidParamsError):
        registry.call("resume_job", {"job_id": job_id, "from": "rewind"})


def test_resume_job_refuses_while_the_owner_has_paused_work(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from saberops.pause_work import pause_now

    repo = _git_repo(isolated / "paused-repo")
    db = Database(isolated / "paused.db")
    _seed_run(db, repo, "run_paused_src", RunStatus.FAILED)
    job_id = _failed_job(db, repo, "run_paused_src")
    pause_now(db, detail="test pause", by="owner")
    launches = _stub_launch(monkeypatch)
    registry = _registry(isolated, db)

    with pytest.raises(ToolError, match="Resume is paused"):
        registry.call("resume_job", {"job_id": job_id, "from": "scratch"})
    assert launches == []
    job = get_job(db, job_id)
    assert job is not None and job.state is JobState.FAILED


def test_resume_job_folds_try_worker_in_as_an_option(
    isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import saberops.routing as routing_module
    from saberops.models import WorkerCandidate

    repo = _git_repo(isolated / "worker-repo")
    db = Database(isolated / "worker.db")
    _seed_run(db, repo, "run_worker_src", RunStatus.FAILED)
    db.record_event(
        "run_worker_src",
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
    job_id = _failed_job(db, repo, "run_worker_src")
    candidate = WorkerCandidate(
        provider="opencode", model="exact-model", binding_id="owner-picked"
    )
    monkeypatch.setattr(
        routing_module, "resolve_candidate_chain", lambda **kwargs: [replace(candidate)]
    )
    launches = _stub_launch(monkeypatch)
    registry = _registry(isolated, db)

    payload = registry.call(
        "resume_job", {"job_id": job_id, "worker_binding": "owner-picked"}
    )
    assert payload["operation"] == "resume_job"
    assert payload["worker_binding"] == "owner-picked"
    assert payload["run_id"] in launches
    child = db.get_run(payload["run_id"])
    assert child is not None and child.config_json
    child_config = json.loads(child.config_json)
    assert child_config["provider_override"] == "opencode"
    assert child_config["model_override"] == "exact-model"


# ---------------------------------------------------------------------------
# CLI: the one owner verb, and the old verbs as aliases for one release
# ---------------------------------------------------------------------------


def test_cli_resume_job_is_the_one_owner_verb(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "cli-repo")
    db_path = isolated / "cli.db"
    db = Database(db_path)
    _seed_run(db, repo, "run_cli_src", RunStatus.FAILED)
    job_id = _failed_job(db, repo, "run_cli_src")
    launches = _stub_launch(monkeypatch)

    code, out, err = _cli(
        "resume-job",
        job_id,
        "--from",
        "scratch",
        "--note",
        "Resume after the rejection.",
        "--db",
        str(db_path),
        "--json",
        capsys=capsys,
    )
    assert code == 0, err
    payload = json.loads(out)
    assert payload["operation"] == "resume_job"
    assert payload["already_resumed"] is False
    assert launches == [payload["run_id"]]
    resumed = get_job(db, job_id)
    assert resumed is not None and resumed.state is JobState.RUNNING


def test_cli_old_verbs_still_work_and_say_they_are_aliases(
    isolated: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _git_repo(isolated / "alias-repo")
    db_path = isolated / "alias.db"
    db = Database(db_path)
    _seed_run(db, repo, "run_alias_src", RunStatus.FAILED)
    launches = _stub_launch(monkeypatch)

    code, out, err = _cli(
        "retry", "run_alias_src", "--db", str(db_path), "--json", capsys=capsys
    )
    assert code == 0
    payload = json.loads(out)
    assert payload["operation"] == "retry"
    assert "resume-job" in err and "alias" in err
    assert launches == [payload["run_id"]]

    # A run that no job owns still resumes by run id through the one verb.
    code, out, err = _cli(
        "resume-job", "run_alias_src", "--from", "scratch", "--db", str(db_path),
        "--json", capsys=capsys,
    )
    assert code == 0, err
    second = json.loads(out)
    assert second["source_run_id"] == "run_alias_src"
    assert second["run_id"] != payload["run_id"]
