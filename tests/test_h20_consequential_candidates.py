"""H20: modes stop at consequential candidates.

Away and critical-only auto-accept a green, reviewed candidate.  That must not
cover a candidate that is consequential: the planner may mark one (data
migrations, deleting data, auth and security, irreversible operations, effects
outside the work copy) and the engine adds a fixed floor it checks itself from
the candidate diff.  Either one parks the candidate for the owner; independent
work continues.  Ask-me is unchanged, and nothing here can push.

Fake CLIs and a toy repository only; no vendor, model or real owner store.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import (
    AcceptanceFixture,
    RigEnvironment,
    build_acceptance_fixture,
    build_lifecycle_rig,
)

from saberops.auto_accept import (
    AUTO_ACCEPT_EVENT,
    AUTO_ACCEPT_PARKED_EVENT,
    auto_accept_candidate,
    consequential_floor,
    evaluate_auto_accept,
)
from saberops.candidate_lifecycle import project_candidate_state
from saberops.db import Database
from saberops.jobs.models import JobRequirements
from saberops.jobs.service import job_dispatch_spec, submit_job
from saberops.models import CandidateState
from saberops.owner_mode import set_owner_mode

pytestmark = pytest.mark.packaged_routing_default

FUTURE = "2999-01-01T00:00:00Z"

_MIGRATION = ("db/migrations/0001_add_widgets.sql", "CREATE TABLE widgets (id INTEGER);\n")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _recorder() -> tuple[list[tuple[Any, ...]], Any]:
    calls: list[tuple[Any, ...]] = []

    def notify(ledger: Path, key: str, title: str, body: str) -> None:
        calls.append((ledger, key, title, body))

    return calls, notify


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _away_fixture(rig: RigEnvironment, **kwargs: Any) -> AcceptanceFixture:
    fixture = build_acceptance_fixture(rig, **kwargs)
    fixture.record_passing_review_for_b()
    set_owner_mode("away", until=FUTURE)
    return fixture


def test_a_migration_candidate_parks_and_notifies_in_away(
    rig: RigEnvironment,
) -> None:
    """The engine floor parks a migration even though the candidate is green."""
    fixture = _away_fixture(rig, candidate_files=(_MIGRATION,))
    db = rig.db()

    decision = evaluate_auto_accept(db=db, run_id=fixture.run_id)
    assert decision.allowed is False, decision.as_dict()
    assert decision.park_for_owner is True, decision.as_dict()
    assert "migration" in decision.consequence

    calls, notify = _recorder()
    outcome = auto_accept_candidate(db=db, run_id=fixture.run_id, notifier=notify)
    assert outcome.applied is False
    assert "consequential" in outcome.reason

    # Nothing was accepted: the target stays at the base commit and the
    # candidate is still the owner's to accept or discard.
    assert _git(rig.repo, "rev-parse", "HEAD") == fixture.base_sha
    assert project_candidate_state(db, fixture.run_id).state is not CandidateState.ACCEPTED
    assert db.get_events_by_type(fixture.run_id, AUTO_ACCEPT_EVENT) == []
    parked = db.get_events_by_type(fixture.run_id, AUTO_ACCEPT_PARKED_EVENT)
    assert len(parked) == 1
    assert parked[0].payload is not None
    assert parked[0].payload["candidate_sha"] == fixture.candidate_b_sha
    assert "migration" in parked[0].payload["reason"]
    assert len(calls) == 1
    assert calls[0][1] == f"auto-accept-parked:{fixture.run_id}:{fixture.candidate_b_sha}"
    assert "migration" in calls[0][3]


def test_an_ordinary_green_candidate_still_auto_accepts(rig: RigEnvironment) -> None:
    """The floor only stops consequential candidates."""
    fixture = _away_fixture(rig)
    db = rig.db()

    decision = evaluate_auto_accept(db=db, run_id=fixture.run_id)
    assert decision.allowed is True, decision.as_dict()
    assert decision.park_for_owner is False

    calls, notify = _recorder()
    outcome = auto_accept_candidate(db=db, run_id=fixture.run_id, notifier=notify)
    assert outcome.applied is True, outcome.reason
    assert _git(rig.repo, "rev-parse", "HEAD") == fixture.candidate_b_sha
    assert db.get_events_by_type(fixture.run_id, AUTO_ACCEPT_PARKED_EVENT) == []
    assert len(calls) == 1
    assert calls[0][1].startswith(f"auto-accept:{fixture.run_id}:")


def test_a_planner_marked_candidate_parks_without_a_floor_hit(
    rig: RigEnvironment,
) -> None:
    """The planner can add consequential for what the floor cannot see."""
    fixture = _away_fixture(rig, extra_config={"consequential": True})
    db = rig.db()

    decision = evaluate_auto_accept(db=db, run_id=fixture.run_id)
    assert decision.allowed is False, decision.as_dict()
    assert decision.park_for_owner is True
    assert "planner" in decision.consequence
    assert _git(rig.repo, "rev-parse", "HEAD") == fixture.base_sha


def test_the_planner_cannot_unmark_an_engine_floor_hit(rig: RigEnvironment) -> None:
    """A recorded ``consequential: false`` never removes the engine's floor."""
    fixture = _away_fixture(
        rig,
        candidate_files=(_MIGRATION,),
        extra_config={"consequential": False},
    )
    db = rig.db()

    decision = evaluate_auto_accept(db=db, run_id=fixture.run_id)
    assert decision.allowed is False, decision.as_dict()
    assert decision.park_for_owner is True
    assert "migration" in decision.consequence


def test_the_deleted_files_floor_counts_a_mass_deletion(tmp_path: Path) -> None:
    """A large deletion is consequential; one deleted file is ordinary."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "h20@example.com")
    _git(repo, "config", "user.name", "H20")
    for index in range(25):
        (repo / f"file_{index}.txt").write_text("data\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    base = _git(repo, "rev-parse", "HEAD")

    (repo / "file_0.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete one")
    one_deleted = _git(repo, "rev-parse", "HEAD")
    assert consequential_floor(repo, base, one_deleted).hit is False

    for index in range(1, 25):
        (repo / f"file_{index}.txt").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "delete the rest")
    mass_deleted = _git(repo, "rev-parse", "HEAD")
    floor = consequential_floor(repo, base, mass_deleted)
    assert floor.hit is True
    assert "deletes" in " ".join(floor.reasons)


_CONSEQUENTIAL_PLAN = """# SaberOps plan: h20

```plan
{"format": "saberops-plan/1", "plan_id": "h20", "base": "main"}
```

## Wave 0 — schema

### H20a. Replace the billing schema

- **Why:** the old schema cannot hold invoices.
- **Do:** add the migration and update the model.
- **Out of scope:** none
- **Done when:**
  - the migration applies
- **Needs:** none

```plan
{"id": "H20a", "wave": 0, "depends_on": [], "owns": ["db/migrations"],
 "consequential": true}
```
"""


def test_a_native_claim_keeps_the_consequential_marker(
    rig: RigEnvironment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A natively claimed job freezes the planner's marker on its run."""
    from saberops import native_workers

    monkeypatch.setenv("ORCH_NATIVE_WORKERS", "1")
    submitted = submit_job(
        rig.db(),
        repo=rig.repo,
        goal="apply the billing migration natively",
        requirements=JobRequirements(consequential=True),
    )
    claim = native_workers.claim_native_job(
        rig.db(),
        submitted.job.job_id,
        vendor="codex",
        model="codex-native-test-model",
    )
    run = rig.db().get_run(claim["run_id"])
    assert run is not None and run.config_json is not None
    assert json.loads(run.config_json)["consequential"] is True


def test_a_plan_lane_can_mark_its_candidate_consequential() -> None:
    """The planner's PLAN.md lane carries the marker through the plan store."""
    from saberops.plan_file import parse_plan_file
    from saberops.project_scope.plan_store import PlanTaskDef

    parsed = parse_plan_file(_CONSEQUENTIAL_PLAN)
    (lane,) = parsed.lanes
    assert lane.consequential is True
    task = lane.to_task()
    assert task.consequential is True
    stored = PlanTaskDef.from_dict(task.as_dict(format_version=2), format_version=2)
    assert stored.consequential is True


def test_a_planner_marked_job_freezes_consequential_on_the_run(
    tmp_path: Path,
) -> None:
    """submit_job carries the marker to the job and its dispatch spec."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "h20@example.com")
    _git(repo, "config", "user.name", "H20")
    (repo / "README.md").write_text("# h20\n", encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")

    db = Database(tmp_path / "jobs.db")
    submitted = submit_job(
        db,
        repo=repo,
        goal="replace the billing schema",
        requirements=JobRequirements(consequential=True),
        idempotency_key="h20-consequential",
    )
    assert submitted.job.requirements.consequential is True
    assert job_dispatch_spec(submitted.job).request.consequential is True

    from saberops.config import reconstruct_run_config, resolve_run_config

    frozen = resolve_run_config(
        task="replace the billing schema",
        repo_path=repo,
        consequential=True,
    )
    assert frozen.consequential is True
    assert reconstruct_run_config(frozen.as_dict()).consequential is True
    assert (
        reconstruct_run_config(
            resolve_run_config(task="tidy", repo_path=repo).as_dict()
        ).consequential
        is False
    )

    default = submit_job(
        db,
        repo=repo,
        goal="tidy a docstring",
        idempotency_key="h20-ordinary",
    )
    assert default.job.requirements.consequential is False
    assert job_dispatch_spec(default.job).request.consequential is False


def test_the_week_old_record_still_reads_as_ordinary() -> None:
    """A stored job written before H20 has no marker and means ordinary."""
    requirements = JobRequirements.from_dict({"role": "BUILD"})
    assert requirements.consequential is False
    assert "consequential" not in JobRequirements().to_dict()
