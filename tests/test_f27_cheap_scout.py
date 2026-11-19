"""F27: scout jobs prefer each subscription's cheapest plan model.

A scout (F19) is read-only, so the cheapest plan model of an available
subscription is the default.  The owner's build route stays the only source of
routes; the default reorders it, keeps every candidate as a fallback, and is
overridable per project (``scout_route``) and per job (``effort``/``size_hint``).

Hermetic: the A6 lifecycle rig's fake worker CLI, a fresh isolated store and no
vendor CLI, model or network call.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.capacity.loop import run_scheduler_loop
from saberops.db import Database
from saberops.jobs.models import (
    Effort,
    Job,
    JobKind,
    JobRequirements,
    JobState,
    Priority,
    WaitingReason,
)
from saberops.jobs.service import get_job, job_usage_view, submit_job
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import RunStatus
from saberops.paths import owner_state_dir
from saberops.project_settings import set_project_scout_route
from saberops.routing_config import (
    add_candidate_to_chain,
    load_effective_routing,
    load_effective_routing_payload,
    save_user_routing,
)
from saberops.scout_routing import (
    is_cheap_scout_model,
    order_candidates_for_scout,
    subscription_key,
)

pytestmark = pytest.mark.packaged_routing_default

CHEAP_CODEX = "codex/gpt-5.6-luna"
NORMAL_CODEX = "codex/gpt-5.6-terra"
EXPLICIT_CODEX = "codex/gpt-5.6-sol"

REPORT_TEXT = """\
## Summary
The helper is safe to reuse.

## Findings
- It has one caller.
"""


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _add_routes(*candidate_ids: str) -> None:
    payload = load_effective_routing_payload()
    for candidate_id in candidate_ids:
        payload = add_candidate_to_chain(payload, role="WORKER", candidate_id=candidate_id)
    save_user_routing(payload)


def _tick(db: Database) -> object:
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    return run_scheduler_loop(db, once=True, readiness_service=readiness)


def _run_scout(
    rig: RigEnvironment,
    *,
    size_hint: str | None = None,
    effort: Effort | None = None,
) -> tuple[str, str, str, str]:
    """Submit and finish one scout; return (job_id, run_id, provider, model)."""
    rig.set_worker_mode("commit", report_text=REPORT_TEXT)
    db = rig.db()
    submitted = submit_job(
        db,
        repo=rig.repo,
        goal="Check whether the helper is reusable",
        kind=JobKind.SCOUT,
        requirements=JobRequirements(size_hint=size_hint, effort=effort),
    )
    assert submitted.created is True
    _tick(db)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=30.0)
    assert run.status == RunStatus.COMPLETED, run.result_summary
    attempts = db.get_attempts_for_run(run.id)
    assert attempts
    attempt = attempts[-1]
    _tick(db)
    return submitted.job.job_id, run.id, attempt.provider, attempt.model


def _queued_scout(
    rig: RigEnvironment,
    job_id: str,
    *,
    effort: Effort | None = None,
    size_hint: str | None = None,
) -> Job:
    now = datetime.now(UTC).isoformat()
    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="f27-test",
        repo=str(rig.repo),
        base_ref="HEAD",
        base_sha=rig.base_sha,
        goal="Cheap scout admission check",
        acceptance=None,
        requirements=JobRequirements(effort=effort, size_hint=size_hint),
        priority=Priority.NORMAL,
        kind=JobKind.SCOUT,
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        created_at=now,
        updated_at=now,
        request_digest=f"f27-digest-{job_id}",
    )


def test_default_scout_uses_the_cheapest_codex_model(rig: RigEnvironment) -> None:
    _add_routes(CHEAP_CODEX)
    job_id, _, provider, model = _run_scout(rig)

    assert (provider, model) == ("codex", "gpt-5.6-luna")
    db = rig.db()
    job = get_job(db, job_id)
    assert job is not None and job.kind is JobKind.SCOUT
    usage = job_usage_view(db, job)
    assert usage["subscriptions"]
    assert usage["subscriptions"][0]["subscription"] == "codex"


def test_explicit_project_scout_route_wins(rig: RigEnvironment) -> None:
    _add_routes(CHEAP_CODEX, EXPLICIT_CODEX)
    set_project_scout_route(rig.repo, EXPLICIT_CODEX)

    _, _, provider, model = _run_scout(rig)

    assert (provider, model) == ("codex", "gpt-5.6-sol")


def test_build_route_override_disables_the_cheap_default(rig: RigEnvironment) -> None:
    _add_routes(CHEAP_CODEX)
    set_project_scout_route(rig.repo, "build")

    _, _, _, model = _run_scout(rig)

    assert model == "gpt-5.6-terra"


def test_large_scout_keeps_the_ordinary_build_route(rig: RigEnvironment) -> None:
    _add_routes(CHEAP_CODEX)

    _, _, _, model = _run_scout(rig, size_hint="L")

    assert model == "gpt-5.6-terra"


def test_parallel_scouts_respect_the_global_admission_ceiling(rig: RigEnvironment) -> None:
    _add_routes(CHEAP_CODEX)
    db = rig.db()
    queued = (
        _queued_scout(rig, "job-f27-a"),
        _queued_scout(rig, "job-f27-b"),
    )
    from saberops.capacity.scheduler import DispatchDecision, WaitDecision, schedule
    from saberops.capacity.snapshot import effective_quota_states
    from saberops.control_plane.policy import load_control_plane_policy

    decisions = schedule(
        now=datetime.now(UTC),
        queued=queued,
        routing_snapshot=load_effective_routing(),
        policy=load_control_plane_policy(),
        quota_states=effective_quota_states(db),
        global_slot_limit=1,
    )

    dispatched = [d for d in decisions if isinstance(d, DispatchDecision)]
    waited = [d for d in decisions if isinstance(d, WaitDecision)]
    assert len(dispatched) == 1
    assert len(waited) == 1
    assert waited[0].reason == WaitingReason.CONCURRENCY.value
    assert dispatched[0].candidate.model == "gpt-5.6-luna"

    # Without the ceiling, both scouts are admitted in the same tick and both
    # use the cheap model; no new admission path is involved.
    parallel = schedule(
        now=datetime.now(UTC),
        queued=queued,
        routing_snapshot=load_effective_routing(),
        policy=load_control_plane_policy(),
        quota_states=effective_quota_states(db),
    )
    parallel_dispatched = [d for d in parallel if isinstance(d, DispatchDecision)]
    assert len(parallel_dispatched) == 2
    assert [d.candidate.model for d in parallel_dispatched] == [
        "gpt-5.6-luna",
        "gpt-5.6-luna",
    ]


def test_project_cli_sets_and_clears_the_scout_route(rig: RigEnvironment) -> None:
    from saberops.cli import main as cli_main
    from saberops.project_settings import load_project_settings

    assert (
        cli_main(
            ["project", "set-scout-route", "--repo", str(rig.repo), EXPLICIT_CODEX]
        )
        == 0
    )
    assert load_project_settings(rig.repo).scout_route == EXPLICIT_CODEX
    from saberops.run_creation import effective_policy_summary

    summary = effective_policy_summary(rig.repo)
    assert summary["scout_route"] == EXPLICIT_CODEX
    assert summary["scout_route_explicit"] is True
    assert cli_main(["project", "set-scout-route", "--repo", str(rig.repo), "nope"]) == 1
    assert load_project_settings(rig.repo).scout_route == EXPLICIT_CODEX
    assert cli_main(["project", "clear-scout-route", "--repo", str(rig.repo)]) == 0
    assert load_project_settings(rig.repo).scout_route is None


def test_cheap_family_matching_and_scout_order_are_deterministic() -> None:
    assert is_cheap_scout_model("codex", "gpt-6-luna")
    assert is_cheap_scout_model("codex", "gpt-5.6-luna")
    assert not is_cheap_scout_model("codex", "gpt-5.6-terra")
    assert is_cheap_scout_model("anthropic", "claude-haiku-5-5")
    assert is_cheap_scout_model("opencode", "opencode-go/deepseek-v4.1-flash")
    assert is_cheap_scout_model("opencode", "opencode-go/deepseek-v4-flash")
    assert not is_cheap_scout_model("opencode", "opencode/openai/gpt-5.6")
    assert not is_cheap_scout_model("opencode", "opencode-go/deepseek-v4-pro")
    assert subscription_key("opencode", "opencode-go/deepseek-v4-flash") == "opencode:opencode-go"
    assert subscription_key("opencode", "opencode/openai/gpt-5.6") == "opencode:opencode"
    assert subscription_key("codex", "gpt-5.6-luna") == "codex"

    from saberops.models import WorkerCandidate

    chain = [
        WorkerCandidate(provider="opencode", model="opencode-go/deepseek-v4-pro"),
        WorkerCandidate(provider="codex", model="gpt-5.6-terra"),
        WorkerCandidate(provider="opencode", model="opencode-go/deepseek-v4-flash"),
        WorkerCandidate(provider="codex", model="gpt-5.6-luna"),
    ]
    ordered = order_candidates_for_scout(chain, route="cheap")
    assert [(c.provider, c.model) for c in ordered] == [
        ("opencode", "opencode-go/deepseek-v4-flash"),
        ("codex", "gpt-5.6-luna"),
        ("opencode", "opencode-go/deepseek-v4-pro"),
        ("codex", "gpt-5.6-terra"),
    ]
    assert order_candidates_for_scout(chain, route="build") == chain
