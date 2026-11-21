"""Phase 1 F8: job dependencies.

Covers the dependency contract:

* a chain starts only after its dependency is *accepted* (not merely finished)
  through a real run, gate, review and ``AcceptEngine`` acceptance;
* fan-in waits for every dependency, names the one still outstanding, and lifts
  only when the last dependency is accepted;
* a failed or cancelled dependency parks the dependant with a reason;
* independent jobs keep dispatching in parallel beside a parked dependant;
* a cycle is refused at the durable choke point and unknown ids are refused;
* dependency lists are part of the request identity and survive the store;
* a store created before F8 opens, reads and gains the column in place.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.accept import AcceptEngine
from saberops.capacity.loop import run_scheduler_loop
from saberops.capacity.scheduler import DispatchDecision
from saberops.db import Database, current_iso_timestamp
from saberops.jobs import store
from saberops.jobs.dependencies import (
    DEPENDENCY_FAILED_GUIDANCE,
    DependencyCycleError,
    UnknownDependencyError,
    validate_job_dependencies,
)
from saberops.jobs.models import (
    RUN_FAILED,
    Job,
    JobRequirements,
    JobState,
    WaitingReason,
)
from saberops.jobs.service import (
    cancel_job,
    get_job,
    list_jobs,
    submit_job,
)
from saberops.jobs.store import IdempotencyConflictError
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import Run, RunStatus, Tier
from saberops.paths import owner_state_dir
from saberops.provenance import mint_run_provenance

pytestmark = pytest.mark.packaged_routing_default


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _tick(db: Database, *, stub: bool = False) -> list[str]:
    """Run one scheduler tick and return the job ids it dispatched.

    ``stub`` replaces the launcher with one that records the dispatch and
    creates no process; tests that must leave a running job behind use it so no
    detached worker outlives the test.
    """
    launcher = (lambda job, decision, run_id: run_id) if stub else None
    tick = run_scheduler_loop(
        db,
        once=True,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
        run_launcher=launcher,
    )
    assert tick is not None and tick.ran is True, tick
    return [
        decision.job.job_id
        for decision in tick.decisions
        if isinstance(decision, DispatchDecision)
    ]


def _finish(db: Database, job_id: str, run_id: str) -> None:
    """Drive a job to SUCCEEDED without a worker."""
    store.mark_dispatched(db, job_id, run_id)
    store.reconcile_transition(db, job_id, to=JobState.SUCCEEDED)


def _seed_completed_run(rig: RigEnvironment, db: Database) -> str:
    """Create a real completed run row (no attempts) in the isolated store."""
    provenance = mint_run_provenance(rig.repo, "fan-in dependency")
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=provenance.run_id,
            task="fan-in dependency",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json=json.dumps({"review_enabled": True}),
        )
    )
    return provenance.run_id


def _accepted_dependency(rig: RigEnvironment, db: Database, job_id: str) -> str:
    """Finish a dependency on a real run row and return that run id."""
    run_id = _seed_completed_run(rig, db)
    _finish(db, job_id, run_id)
    return run_id


def _accept(db: Database, run_id: str) -> None:
    """Record the durable ``run_accepted`` event the ``AcceptEngine`` writes.

    The acceptance boundary itself is exercised end to end through the engine in
    :func:`test_chain_starts_only_after_the_dependency_is_accepted`.
    """
    db.record_event(run_id, "run_accepted", payload={"candidate_sha": "c" * 40})


def test_chain_starts_only_after_the_dependency_is_accepted(
    rig: RigEnvironment,
) -> None:
    """Finishing a dependency is not enough; the owner's acceptance releases it."""
    db = rig.db()
    dependency = submit_job(db, repo=rig.repo, goal="first step").job
    dependant = submit_job(
        db, repo=rig.repo, goal="second step", depends_on=(dependency.job_id,)
    ).job
    assert dependant.depends_on == (dependency.job_id,)

    # The scheduler really dispatches the dependency (default launcher).
    assert _tick(db) == [dependency.job_id]
    running = get_job(db, dependency.job_id)
    assert running is not None and running.current_run_id is not None
    run = rig.wait_for_run(running.current_run_id, timeout=180.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary

    parked = get_job(db, dependant.job_id)
    assert parked is not None
    assert parked.state == JobState.QUEUED
    assert parked.waiting_reason == WaitingReason.DEPENDENCY

    # One tick reconciles the finished run; the dependant still may not start.
    assert _tick(db, stub=True) == []
    finished = get_job(db, dependency.job_id)
    assert finished is not None and finished.state == JobState.SUCCEEDED
    still_parked = get_job(db, dependant.job_id)
    assert still_parked is not None
    assert still_parked.waiting_reason == WaitingReason.DEPENDENCY

    accepted = AcceptEngine(db=db).accept_run(run.id)
    assert accepted.accepted is True

    assert _tick(db, stub=True) == [dependant.job_id]
    started = get_job(db, dependant.job_id)
    assert started is not None and started.state == JobState.RUNNING


def test_dependant_dispatches_on_the_accepted_dependency_landing_sha(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A dependant's run starts from what the dependency landed, not its own base.

    The submit-time ``base_sha`` is request identity: it records where the plan
    was when the job was written.  By the time the dependent is eligible the
    dependency has been accepted and integrated, so the run must be anchored on
    that landing commit or the worker sees the old tree.
    """
    db = rig.db()
    dependency = submit_job(db, repo=rig.repo, goal="first step").job
    dependant = submit_job(
        db, repo=rig.repo, goal="second step", depends_on=(dependency.job_id,)
    ).job
    run_id = _accepted_dependency(rig, db, dependency.job_id)

    import subprocess

    subprocess.run(
        ["git", "-C", str(rig.repo), "commit", "--allow-empty", "-m", "landed"],
        check=True,
        capture_output=True,
    )
    landing = subprocess.run(
        ["git", "-C", str(rig.repo), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    assert landing != rig.base_sha
    db.record_event(run_id, "run_accepted", payload={"candidate_sha": landing})

    # The default launcher would spawn a worker; the run row is what records
    # the base, so a stub supervisor is enough to verify the anchor.
    monkeypatch.setattr(
        "saberops.supervisor.RunSupervisor.ensure_running",
        lambda self, run_id, config: True,
    )
    assert _tick(db) == [dependant.job_id]
    started = get_job(db, dependant.job_id)
    assert started is not None and started.current_run_id is not None
    run = db.get_run(started.current_run_id)
    assert run is not None
    assert run.base_commit == landing


def test_fan_in_waits_for_every_dependency(rig: RigEnvironment) -> None:
    """A joined job lifts only when the last of its dependencies is accepted."""
    db = rig.db()
    first = submit_job(db, repo=rig.repo, goal="first leg").job
    second = submit_job(db, repo=rig.repo, goal="second leg").job
    joined = submit_job(
        db,
        repo=rig.repo,
        goal="joined step",
        depends_on=(first.job_id, second.job_id),
    ).job

    first_run = _accepted_dependency(rig, db, first.job_id)
    second_run = _accepted_dependency(rig, db, second.job_id)

    assert _tick(db, stub=True) == []
    parked = get_job(db, joined.job_id)
    assert parked is not None
    assert parked.waiting_reason == WaitingReason.DEPENDENCY

    _accept(db, first_run)
    assert _tick(db, stub=True) == []
    waiting_event = [
        event for event in store.list_events(db, joined.job_id) if event["kind"] == "waiting"
    ][-1]
    blockers = json.loads(str(waiting_event["payload_json"]))["details"]["blocked_by"]
    assert blockers == [second.job_id]

    _accept(db, second_run)
    assert _tick(db, stub=True) == [joined.job_id]
    started = get_job(db, joined.job_id)
    assert started is not None and started.state == JobState.RUNNING


def test_independent_jobs_run_in_parallel_beside_a_parked_dependant(
    rig: RigEnvironment,
) -> None:
    """Dependencies never serialise work that does not depend on them."""
    db = rig.db()
    dependency = submit_job(db, repo=rig.repo, goal="root").job
    dependant = submit_job(
        db, repo=rig.repo, goal="dependent", depends_on=(dependency.job_id,)
    ).job
    left = submit_job(db, repo=rig.repo, goal="independent left").job
    right = submit_job(db, repo=rig.repo, goal="independent right").job

    assert sorted(_tick(db, stub=True)) == sorted(
        [dependency.job_id, left.job_id, right.job_id]
    )
    parked = get_job(db, dependant.job_id)
    assert parked is not None
    assert parked.state == JobState.QUEUED
    assert parked.waiting_reason == WaitingReason.DEPENDENCY
    registry = build_default_registry(
        ServerContext(
            db_path=db.db_path,
            questions_store=rig.root / "questions.json",
        )
    )
    guidance = registry.call("get_job", {"job_id": dependant.job_id})["guidance"]
    assert dependency.job_id in guidance


@pytest.mark.parametrize(
    "terminal",
    [JobState.FAILED, JobState.CANCELLED],
    ids=["failed-dependency", "cancelled-dependency"],
)
def test_failed_or_cancelled_dependency_parks_the_dependant(
    rig: RigEnvironment, terminal: JobState
) -> None:
    """A dependency that can never be accepted parks the dependant for good."""
    db = rig.db()
    dependency = submit_job(db, repo=rig.repo, goal="doomed step").job
    dependant = submit_job(
        db, repo=rig.repo, goal="blocked step", depends_on=(dependency.job_id,)
    ).job

    if terminal == JobState.FAILED:
        store.mark_dispatched(db, dependency.job_id, "run_doomed")
        store.reconcile_transition(
            db, dependency.job_id, to=JobState.FAILED, failure_reason=RUN_FAILED
        )
    else:
        cancel_job(db, dependency.job_id, run_canceller=lambda _run_id: False)

    assert _tick(db, stub=True) == []
    parked = get_job(db, dependant.job_id)
    assert parked is not None
    assert parked.state == JobState.QUEUED
    assert parked.waiting_reason == WaitingReason.DEPENDENCY_FAILED

    # Deterministic and permanent: a later tick repeats the same reason.
    assert _tick(db, stub=True) == []
    again = get_job(db, dependant.job_id)
    assert again is not None
    assert again.waiting_reason == WaitingReason.DEPENDENCY_FAILED

    registry = build_default_registry(
        ServerContext(
            db_path=db.db_path,
            questions_store=rig.root / "questions.json",
        )
    )
    waited = registry.call(
        "wait_job", {"job_id": dependant.job_id, "timeout_seconds": 30}
    )
    # Waiting cannot help, so the wait returns at once with the reason.
    assert waited["timed_out"] is False
    assert waited["outcome"] == "needs_attention"
    assert waited["reason"] == DEPENDENCY_FAILED_GUIDANCE
    assert registry.call("get_job", {"job_id": dependant.job_id})["guidance"] == (
        DEPENDENCY_FAILED_GUIDANCE
    )


def test_dependencies_must_exist_and_cycles_are_refused(rig: RigEnvironment) -> None:
    """Unknown dependencies and graph cycles never reach the scheduler."""
    db = rig.db()
    first = submit_job(db, repo=rig.repo, goal="first").job
    second = submit_job(db, repo=rig.repo, goal="second", depends_on=(first.job_id,)).job

    with pytest.raises(UnknownDependencyError, match="unknown dependency job"):
        submit_job(db, repo=rig.repo, goal="bad", depends_on=("not-a-job",))

    # The durable invariant: an edge that would close first -> second -> first
    # is refused, so no writer can deadlock the queue.
    with pytest.raises(DependencyCycleError):
        validate_job_dependencies(db, job_id=first.job_id, depends_on=(second.job_id,))
    with pytest.raises(DependencyCycleError, match="cannot depend on itself"):
        validate_job_dependencies(db, job_id=first.job_id, depends_on=(first.job_id,))

    # A job cannot even be constructed as its own dependency.
    with pytest.raises(ValueError, match="cannot depend on itself"):
        Job(
            job_id="looping",
            idempotency_key=None,
            caller_id="owner",
            repo=str(rig.repo),
            base_ref="HEAD",
            base_sha=rig.base_sha,
            goal="loop",
            acceptance=None,
            requirements=JobRequirements(),
            depends_on=("looping",),
            state=JobState.QUEUED,
            waiting_reason=WaitingReason.NEW,
        )


def test_dependency_list_is_part_of_the_request_identity(rig: RigEnvironment) -> None:
    """The same idempotency key with a different dependency list is a conflict."""
    db = rig.db()
    dependency = submit_job(db, repo=rig.repo, goal="root").job
    first = submit_job(
        db,
        repo=rig.repo,
        goal="dependent",
        depends_on=(dependency.job_id,),
        idempotency_key="f8-identity",
    )
    replay = submit_job(
        db,
        repo=rig.repo,
        goal="dependent",
        depends_on=(dependency.job_id,),
        idempotency_key="f8-identity",
    )
    assert replay.created is False
    assert replay.job.job_id == first.job.job_id
    assert replay.job.depends_on == (dependency.job_id,)

    # Duplicates normalise to the same request, so a replay is still a replay.
    deduplicated = submit_job(
        db,
        repo=rig.repo,
        goal="dependent",
        depends_on=(dependency.job_id, dependency.job_id),
        idempotency_key="f8-identity",
    )
    assert deduplicated.job.job_id == first.job.job_id

    other = submit_job(db, repo=rig.repo, goal="other root").job
    with pytest.raises(IdempotencyConflictError):
        submit_job(
            db,
            repo=rig.repo,
            goal="dependent",
            depends_on=(dependency.job_id, other.job_id),
            idempotency_key="f8-identity",
        )
    with pytest.raises(IdempotencyConflictError):
        submit_job(
            db,
            repo=rig.repo,
            goal="dependent",
            idempotency_key="f8-identity",
        )


def test_store_from_before_f8_gains_the_column_in_place(rig: RigEnvironment) -> None:
    """A live store created without ``depends_on_json`` keeps working and upgrades."""
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="legacy row").job
    with sqlite3.connect(str(db.db_path)) as conn:
        conn.execute("ALTER TABLE jobs DROP COLUMN depends_on_json")

    # Reads through a read-only connection tolerate the missing column.
    read_only = Database(db.db_path, read_only=True)
    legacy = get_job(read_only, job.job_id)
    assert legacy is not None
    assert legacy.depends_on == ()

    # The next writable open adds the column with the fresh-store default.
    store.ensure_tables(db)
    with sqlite3.connect(str(db.db_path)) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(jobs)")}
    assert "depends_on_json" in columns
    upgraded = get_job(db, job.job_id)
    assert upgraded is not None and upgraded.depends_on == ()
    # And a new dependency now persists.
    dependant = submit_job(
        db, repo=rig.repo, goal="after upgrade", depends_on=(job.job_id,)
    ).job
    stored = get_job(db, dependant.job_id)
    assert stored is not None and stored.depends_on == (job.job_id,)


def test_cli_submit_accepts_depends_on(rig: RigEnvironment) -> None:
    """``orch job submit --depends-on`` reaches the stored job."""
    db = rig.db()
    dependency = submit_job(db, repo=rig.repo, goal="cli root").job
    result = rig.run_cli(
        "job",
        "submit",
        "--repo",
        str(rig.repo),
        "--goal",
        "cli dependent",
        "--depends-on",
        dependency.job_id,
        "--idempotency-key",
        "f8-cli",
    )
    assert result.returncode == 0, result.stderr
    assert f"Depends on : {dependency.job_id}" in result.stdout
    stored = [job for job in list_jobs(db) if job.depends_on == (dependency.job_id,)]
    assert len(stored) == 1
