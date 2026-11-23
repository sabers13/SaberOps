"""H36: the captured pre-1.1 store starts on the current code.

Wave 0 installs the fixture and proves it is usable end to end: the old owner
settings are still read, preflight is green, and a rig job runs to acceptance.
Each later lane that removes or renames something the old store carries appends
one :class:`NoticeRow` here and extends :func:`current_code_notices`, so the
upgrade check grows with the lane that caused it instead of being guessed now.
"""

from __future__ import annotations

import io
import time
from collections.abc import Callable
from contextlib import redirect_stderr
from dataclasses import dataclass
from pathlib import Path

import pytest
from pre_1_1_store import Pre11Store, restore_pre_1_1_store

from saberops.accept import AcceptEngine
from saberops.capacity.loop import run_scheduler_loop
from saberops.control_plane.binding_store import (
    RETIRED_SELECTION_NOTICE,
    load_owner_bindings,
)
from saberops.jobs.service import get_job, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.owner_mode import HostAccessMode, OwnerMode, load_owner_mode_settings
from saberops.paths import owner_state_dir
from saberops.preflight import collect_preflight
from saberops.project_settings import load_project_settings
from saberops.routing_config import (
    RETIRED_ROUTE_NOTICE,
    load_effective_routing,
    load_effective_routing_payload,
)

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


#: The stable fragment of H22's one-shot migration notice.
AUTHORITY_FOLD_NOTICE = "authority preset 'autonomous' is folded into the owner mode"


def _probe_authority_fold(store: Pre11Store) -> list[str]:
    """H22: the authority preset is folded into the owner mode, once."""
    del store
    settings = load_owner_mode_settings()
    notice = settings.notice or ""
    return [AUTHORITY_FOLD_NOTICE] if AUTHORITY_FOLD_NOTICE in notice else []


def _probe_orchestrator_retired(store: Pre11Store) -> list[str]:
    """H3: the in-engine orchestrator selection is ignored, once."""
    del store
    printed = io.StringIO()
    with redirect_stderr(printed):
        load_owner_bindings()
    text = printed.getvalue()
    return [RETIRED_SELECTION_NOTICE] if RETIRED_SELECTION_NOTICE in text else []


def _probe_cline_route(store: Pre11Store) -> list[str]:
    """H17: the retired Cline route is dropped on load, with one notice."""
    del store
    snapshot = load_effective_routing()
    return [RETIRED_ROUTE_NOTICE] if RETIRED_ROUTE_NOTICE in "\n".join(
        snapshot.notices
    ) else []


#: H22 (authority presets), H3 (orchestrator binding) and H17 (Cline route) add
#: their rows; H4 (review knobs), H7 (recovery verbs) and H9 (view names)
#: follow.
NOTICE_ROWS: tuple[NoticeRow, ...] = (
    NoticeRow(
        key="authority-fold",
        lane="H22",
        surface="config/saberops/authority.json",
        expected=AUTHORITY_FOLD_NOTICE,
        probe=_probe_authority_fold,
    ),
    NoticeRow(
        key="orchestrator-retired",
        lane="H3",
        surface="config/saberops/bindings.json",
        expected=RETIRED_SELECTION_NOTICE,
        probe=_probe_orchestrator_retired,
    ),
    NoticeRow(
        key="cline-route",
        lane="H17",
        surface="config/saberops/routing.json",
        expected=RETIRED_ROUTE_NOTICE,
        probe=_probe_cline_route,
    ),
)


def current_code_notices(store: Pre11Store) -> list[str]:
    """Return every migration notice the current code prints for this store.

    The owner-mode migration writes its v2 document on the first read, so this
    must run before the test reads the settings (the notice is one-shot).
    """
    notices: list[str] = []
    for row in NOTICE_ROWS:
        notices.extend(row.probe(store))
    return notices


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

    # 0. The one-shot migration notices are collected on the first read.
    notices = current_code_notices(store)

    # 1. Every captured owner setting still reads back on the current code.
    mode_settings = load_owner_mode_settings()
    assert mode_settings.mode is OwnerMode.ASK_ME
    assert mode_settings.host_access is HostAccessMode.GOVERNED
    # The pre-1.1 effective permission was autonomous ∩ ask-me: start, gate,
    # review, and no autonomous recovery or acceptance.
    assert mode_settings.permissions.auto_start is True
    assert mode_settings.permissions.auto_retry is False
    assert mode_settings.permissions.auto_accept is False
    # H3 (ADR-0064) removed the in-engine orchestrator selection: the store
    # still loads and keeps its exact binding, and the ignored selection is
    # covered by the notice row collected above.
    bindings = load_owner_bindings()
    assert store.orchestrator_binding_id in {
        binding.binding_id for binding in bindings.registry.list_bindings()
    }
    settings = load_project_settings(store.repo)
    # H4 retires the review knobs: the store keeps its gate decision and the
    # retired review fields are ignored (review is mandatory on the new code).
    assert settings.gate_enabled is True
    assert not hasattr(settings, "review_mode")
    assert not hasattr(settings, "review_limit")
    routing = load_effective_routing_payload()
    routes = [
        route
        for group in routing["roles"].values()
        for entry in group
        for route in entry.get("routes", ())
    ]
    assert store.cline_route not in routes, routes

    # 2. The old store starts on the new code.  The scheduler is the one
    #    runtime item preflight reports; this test drives the same engine
    #    in-process (below), so it is the only expected FAIL here.
    failures = [
        finding
        for finding in collect_preflight(store.repo)
        if finding.severity == "FAIL" and not finding.message.startswith("scheduler:")
    ]
    assert failures == [], failures

    # 3. Each removed surface prints one notice; the table is the growth point.
    kinds = [row.key for row in NOTICE_ROWS]
    assert len(kinds) == len(set(kinds)), kinds
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
