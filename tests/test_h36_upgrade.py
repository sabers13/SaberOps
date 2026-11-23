"""H36: the captured pre-1.1 store starts on the current code.

Wave 0 installs the fixture and proves it is usable end to end: the old owner
settings are still read, preflight is green, and a rig job runs to acceptance.
Each later lane that removes or renames something the old store carries appends
one :class:`NoticeRow` here and extends :func:`current_code_notices`, so the
upgrade check grows with the lane that caused it instead of being guessed now.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest
from pre_1_1_store import Pre11Store, restore_pre_1_1_store

from saberops.accept import AcceptEngine
from saberops.authority import AuthorityMode, load_authority_mode
from saberops.capacity.loop import run_scheduler_loop
from saberops.control_plane.binding_store import load_owner_bindings
from saberops.jobs.service import get_job, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.paths import owner_state_dir
from saberops.preflight import collect_preflight
from saberops.project_settings import load_project_settings
from saberops.routing_config import load_effective_routing_payload

pytestmark = [
    pytest.mark.packaged_routing_default,
    pytest.mark.slow,
]

JOB_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True)
class NoticeRow:
    """One migration notice an old store surface must print exactly once.

    ``probe`` runs on the restored store and returns every notice text it saw;
    ``expected`` is the fragment that identifies this row's notice.  A lane
    adds the row when it removes the surface, never before.
    """

    key: str
    lane: str
    surface: str
    expected: str
    probe: Callable[[Pre11Store], list[str]]


#: Empty in wave 0: no phase-1.1 lane has removed or renamed anything yet.
#: H3 (orchestrator binding), H4 (review knobs), H7 (recovery verbs), H9 (view
#: names), H17 (Cline route) and H22 (authority presets) each add their row.
NOTICE_ROWS: tuple[NoticeRow, ...] = ()


def current_code_notices(store: Pre11Store) -> list[str]:
    """Return every migration notice the current code prints for this store.

    Later lanes extend this with their probes; wave 0 has nothing to collect.
    """
    del store
    return []


def _run_job_to_acceptance(store: Pre11Store) -> None:
    db = store.db()
    submitted = submit_job(
        db,
        repo=str(store.repo),
        goal="Add a short greeting to the toy repository",
        acceptance="the check command passes and fake_worker_output.txt exists",
        idempotency_key="h36-upgrade-first-job",
    )
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    deadline = time.monotonic() + JOB_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        result = run_scheduler_loop(db, once=True, readiness_service=readiness)
        assert result is not None
        job = get_job(db, submitted.job.job_id)
        assert job is not None
        if job.state.value in {"SUCCEEDED", "FAILED", "CANCELLED"}:
            break
        time.sleep(0.05)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.state.value == "SUCCEEDED", (
        None if job is None else (job.state, job.failure_reason)
    )
    assert job.current_run_id is not None
    accepted = AcceptEngine(db=db).accept_run(job.current_run_id)
    assert accepted.accepted, accepted
    assert (store.repo / "fake_worker_output.txt").is_file()


def test_pre_1_1_store_keeps_its_settings_and_runs_a_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = restore_pre_1_1_store(tmp_path, monkeypatch)

    # 1. Every captured owner setting still reads back on the current code.
    assert load_authority_mode() is AuthorityMode.AUTONOMOUS
    bindings = load_owner_bindings()
    assert bindings.policy is not None
    assert bindings.policy.pinned_binding_id == store.orchestrator_binding_id
    settings = load_project_settings(store.repo)
    assert settings.review_mode == "max"
    assert settings.review_limit == 5
    assert settings.gate_enabled is True
    routing = load_effective_routing_payload()
    routes = [
        route
        for group in routing["roles"].values()
        for entry in group
        for route in entry.get("routes", ())
    ]
    assert store.cline_route in routes, routes

    # 2. The old store starts on the new code.  The scheduler is the one
    #    runtime item preflight reports; this test drives the same engine
    #    in-process (below), so it is the only expected FAIL here.
    failures = [
        finding
        for finding in collect_preflight(store.repo)
        if finding.severity == "FAIL" and not finding.message.startswith("scheduler:")
    ]
    assert failures == [], failures

    # 3. No lane has claimed a notice yet; the table is the growth point.
    kinds = [row.key for row in NOTICE_ROWS]
    assert len(kinds) == len(set(kinds)), kinds
    notices = current_code_notices(store)
    for row in NOTICE_ROWS:
        assert notices.count(row.expected) == 1, (row.key, notices)

    # 4. And a rig job still runs to acceptance on the upgraded store.
    _run_job_to_acceptance(store)


def test_fixture_carries_every_surface_the_lanes_will_remove() -> None:
    """The fixture's own contents are the pre-1.1 surfaces, not code state."""
    import json

    from pre_1_1_store import FIXTURE_DIR

    manifest = json.loads((FIXTURE_DIR / "manifest.json").read_text(encoding="utf-8"))
    surfaces = json.loads((FIXTURE_DIR / "surfaces.json").read_text(encoding="utf-8"))
    assert manifest["fake_mode"] is True
    assert manifest["contains"]["orchestrator_binding"]
    assert manifest["contains"]["cline_route"]
    assert manifest["contains"]["authority_preset"] == "autonomous"
    assert manifest["contains"]["review"]["limit"] == 5
    assert surfaces["old_views"]
    assert surfaces["old_recovery_verbs"]
    assert (FIXTURE_DIR / "state/saberops/orchestrator.db").is_file()
