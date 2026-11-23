"""Pure deterministic pacing scheduler (pure).

Evaluates queued jobs against routing configuration, billing posture,
quota pools/windows, and current reservations/running counts.

Produces typed DispatchDecision, WaitDecision, and ProbeDecision objects.
Contains no I/O, no DB operations, no subprocess execution, and no wall-clock reads.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from saberops.capacity.admission import (
    AdmissionDecision,
    _get_cost_estimate,
    admit,
)
from saberops.capacity.cost_model import CostState, SizeBucket
from saberops.capacity.exclusions import parse_iso
from saberops.capacity.ledger import PoolDraw
from saberops.control_plane.policy import ControlPlanePolicy
from saberops.jobs.models import (
    DISPATCHABLE_ROLES,
    NO_ROUTE_FOR_REQUIREMENTS,
    DependencyBlocker,
    Difficulty,
    Job,
    JobKind,
    JobRole,
    Priority,
    WaitingReason,
)
from saberops.models import QuotaState, WorkerCandidate
from saberops.routing_config import RoutingSnapshot

# Priority rank: URGENT (0) > NORMAL (1) > BACKGROUND (2)
_PRIORITY_RANK: dict[Priority, int] = {
    Priority.URGENT: 0,
    Priority.NORMAL: 1,
    Priority.BACKGROUND: 2,
}

_FAR_FUTURE: datetime = datetime.max.replace(tzinfo=UTC)


@dataclass(frozen=True)
class DispatchDecision:
    """Decision to dispatch a queued job to an admitted candidate route."""

    job: Job
    candidate: WorkerCandidate
    pool_id: str | None
    binding_window: str | None
    score: float
    cost_estimate: float
    multiplier: float = 1.0
    drawn_pools: tuple[str, ...] = ()
    draws: tuple[PoolDraw, ...] = ()
    # Probeable stale-quota exclusions evaluated before this dispatch (N13 /
    # item 5).  Persisted as a job event so an owner can see that a candidate
    # was skipped for expired evidence even when another route dispatched.
    skipped_candidates: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class WaitDecision:
    """Decision that a queued job must wait for capacity, concurrency, or time."""

    job: Job
    reason: str
    eta: datetime | None = None
    confidence: str | None = None
    candidate: WorkerCandidate | None = None
    pool_id: str | None = None
    binding_window: str | None = None
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ProbeDecision:
    """Decision that a zero-inference probe is required to refresh stale evidence."""

    pool_id: str
    harness: str
    reason: str = "QUOTA_EVIDENCE_REQUIRED"


SchedulerDecision = DispatchDecision | WaitDecision | ProbeDecision


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def _job_sort_key(job: Job) -> tuple[int, int, datetime, str, str]:
    """Sort key for queued jobs:

    1. Priority class: URGENT > NORMAL > BACKGROUND
    2. Deadline presence: jobs with deadlines beat jobs without
    3. Deadline timestamp: earliest deadline first
    4. FIFO: earliest created_at timestamp
    5. Deterministic tie-break: job_id
    """
    pri_rank = _PRIORITY_RANK.get(job.priority, 1)
    has_deadline = 0 if job.deadline else 1
    dl_dt = parse_iso(job.deadline) if job.deadline else _FAR_FUTURE
    if dl_dt is None:
        dl_dt = _FAR_FUTURE
    created = job.created_at or ""
    return (pri_rank, has_deadline, dl_dt, created, job.job_id)


def schedule(
    *,
    now: datetime,
    queued: Sequence[Job],
    running: Sequence[Job] = (),
    routing_snapshot: RoutingSnapshot,
    policy: ControlPlanePolicy | None = None,
    quota_states: Iterable[QuotaState] = (),
    cost_states: Mapping[tuple[str, str, str, str], CostState] | None = None,
    running_counts: Mapping[str, int] | None = None,
    reservations: Mapping[tuple[str, str], float] | None = None,
    global_slot_limit: int | None = None,
    background_min_pace: float = 4.0,
    readiness_states: Mapping[Any, Any] | None = None,
    dependency_blockers: Mapping[str, DependencyBlocker] | None = None,
    blocked_providers: Mapping[str, str] | None = None,
    scout_routes: Mapping[str, str] | None = None,
) -> list[SchedulerDecision]:
    """Pure deterministic pacing scheduler.

    Evaluates queued jobs in strict D7 order:
    - Dependency-blocked jobs wait with their recorded reason (F8) before any
      routing, capacity or pacing evaluation.
    - URGENT > NORMAL > BACKGROUND
    - Within class: earliest deadline first, then FIFO by created_at
    - Evaluates role fallback groups: best score ONLY within the first group
      with admitted candidates.
    - BACKGROUND jobs require binding pace >= background_min_pace.
    - Honors prefer_wait_minutes on fallback groups when exact/parsed ETA matches.
    - A scout job (F19) prefers the cheapest plan model of each subscription
      present on the build route (F27), keeping every candidate as a fallback;
      ``scout_routes[job_id]`` carries its project override.
    - A global aggregate ceiling (``global_slot_limit``) blocks dispatch once
      the number of top-level RUNNING jobs plus same-tick simulated starts
      reaches the ceiling, independent of pool mapping.
    - ``blocked_providers`` (F18) is a provider -> reason map for vendor CLIs
      whose version change is unverified or whose repair is outstanding.  A
      blocked candidate is never admitted; the job falls through to the next
      candidate or waits, and the reason is reported on the wait decision.
    """
    moment = _utcnow(now)
    decisions: list[SchedulerDecision] = []
    probes_needed: dict[str, ProbeDecision] = {}

    # Track simulated concurrency and reservations across the scheduling batch (F2 & F3)
    simulated_running: dict[str, int] = dict(running_counts or {})
    simulated_reservations: dict[tuple[str, str], float] = dict(reservations or {})
    # Counts every top-level RUNNING job supplied by the caller, including
    # jobs with no capacity_reservations row or unknown pool mapping (F3).
    global_running = len(running)

    # Sort queued jobs according to D7 deterministic priority order
    sorted_jobs = sorted(queued, key=_job_sort_key)

    for job in sorted_jobs:
        # F8: a job whose dependencies are not all accepted never reaches
        # routing.  The reason is decided by the caller (DEPENDENCY, or
        # DEPENDENCY_FAILED when a dependency can never be accepted).
        blocker = (dependency_blockers or {}).get(job.job_id)
        if blocker is not None:
            decisions.append(
                WaitDecision(
                    job=job,
                    reason=blocker.reason.value,
                    eta=None,
                    confidence=None,
                    details={
                        "depends_on": list(job.depends_on),
                        "blocked_by": list(blocker.blocked_by),
                        "detail": blocker.detail,
                    },
                )
            )
            continue

        # Check not_before delay
        if job.not_before:
            nb_dt = parse_iso(job.not_before)
            if nb_dt and moment < nb_dt:
                decisions.append(
                    WaitDecision(
                        job=job,
                        reason=WaitingReason.NEW.value,
                        eta=nb_dt,
                        confidence="EXACT",
                        details={"not_before": job.not_before},
                    )
                )
                continue

        # Global aggregate ceiling (D7/F3). Enforced here rather than only
        # through admit() so a full ceiling blocks dispatch even when the
        # capacity subsystem is unconfigured and admit() would otherwise
        # short-circuit to "unconfigured". Two jobs from distinct pools count
        # against the same ceiling.
        if global_slot_limit is not None and global_running >= global_slot_limit:
            decisions.append(
                WaitDecision(
                    job=job,
                    reason=WaitingReason.CONCURRENCY.value,
                    details={
                        "global_running": global_running,
                        "limit": global_slot_limit,
                    },
                )
            )
            continue

        # Check role dispatchability
        role = job.requirements.role
        if role not in DISPATCHABLE_ROLES:
            role_val = role.value if isinstance(role, JobRole) else str(role)
            decisions.append(
                WaitDecision(
                    job=job,
                    reason=WaitingReason.NO_CAPACITY.value,
                    details={"diagnostic": NO_ROUTE_FOR_REQUIREMENTS, "unsupported_role": role_val},
                )
            )
            continue

        try:
            # H1: the planner's recorded difficulty selects the route, not a
            # keyword score.  ``hard`` asks for the build route's
            # ``strong_start`` chain; ``routine`` or a missing value stays on
            # the ordinary build chain.  An empty strong-start setting falls
            # back to the build chain, so a planner omission can never park a
            # job on a route the owner does not have.
            strong_start = (
                job.requirements.difficulty is Difficulty.HARD
                and bool(getattr(routing_snapshot, "strong_start", ()))
            )
            groups = routing_snapshot.groups_for(role, strong_start=strong_start)
        except Exception:
            decisions.append(
                WaitDecision(
                    job=job,
                    reason=WaitingReason.NO_CAPACITY.value,
                    details={"diagnostic": NO_ROUTE_FOR_REQUIREMENTS},
                )
            )
            continue

        if not groups:
            decisions.append(
                WaitDecision(
                    job=job,
                    reason=WaitingReason.NO_CAPACITY.value,
                    details={"diagnostic": NO_ROUTE_FOR_REQUIREMENTS},
                )
            )
            continue

        pacing_meta = routing_snapshot.prefer_wait_minutes_for(
            role, strong_start=strong_start
        )
        scout_preferred: frozenset[tuple[str, str]] | None = None
        if job.kind is JobKind.SCOUT:
            from saberops.scout_routing import scout_candidate_view

            view = scout_candidate_view(
                groups,
                pacing_meta,
                route=(scout_routes or {}).get(job.job_id),
                requirements=job.requirements,
            )
            groups = view.groups
            pacing_meta = view.pacing
            scout_preferred = view.preferred
        size_bucket = job.requirements.size_hint or SizeBucket.M
        all_group_decisions: list[list[AdmissionDecision]] = []
        job_dispatched = False
        vendor_blocked: dict[str, str] = {}
        stale_skips: list[dict[str, Any]] = []

        for g_idx, group in enumerate(groups):
            # Evaluate admission for every candidate in this group
            decisions_for_group: list[AdmissionDecision] = []
            for cand in group:
                if blocked_providers and cand.provider in blocked_providers:
                    vendor_blocked[cand.provider] = blocked_providers[cand.provider]
                    decisions_for_group.append(
                        AdmissionDecision(
                            admitted=False,
                            candidate=cand,
                            reason="VENDOR_CLI_UNVERIFIED",
                            details={
                                "diagnostic": "VENDOR_CLI_UNVERIFIED",
                                "provider": cand.provider,
                                "detail": blocked_providers[cand.provider],
                            },
                        )
                    )
                    continue
                rd = None
                if readiness_states:
                    rd = (
                        readiness_states.get((cand.provider, cand.model))
                        or readiness_states.get(cand.provider)
                    )
                decisions_for_group.append(
                    admit(
                        candidate=cand,
                        now=moment,
                        policy=policy,
                        quota_states=quota_states,
                        requirements=job.requirements,
                        size_bucket=size_bucket,
                        cost_states=cost_states,
                        running_counts=simulated_running,
                        global_running=global_running,
                        max_global_concurrent=global_slot_limit,
                        reservations=simulated_reservations,
                        readiness_state=getattr(rd, "state", None) if rd is not None else None,
                        readiness_reason=getattr(rd, "reason", None) if rd is not None else None,
                        readiness_executable_available=(
                            getattr(rd, "executable_available", True) if rd is not None else True
                        ),
                    )
                )
            all_group_decisions.append(decisions_for_group)
            for skipped in decisions_for_group:
                if (
                    not skipped.admitted
                    and skipped.reason == "QUOTA_EVIDENCE_REQUIRED"
                ):
                    stale_skips.append(
                        {
                            "provider": skipped.candidate.provider,
                            "model": skipped.candidate.model,
                            "reason": skipped.reason,
                            "pool_id": skipped.pool_id,
                            "binding_window": skipped.binding_window,
                            "evidence_state": "stale",
                            "needs_probe": skipped.needs_probe,
                            "probe_harness": skipped.probe_harness,
                        }
                    )
            admitted_in_group = [d for d in decisions_for_group if d.admitted]
            if scout_preferred and admitted_in_group:
                cheap_admitted = [
                    d
                    for d in admitted_in_group
                    if (d.candidate.provider, d.candidate.model) in scout_preferred
                ]
                if cheap_admitted:
                    admitted_in_group = cheap_admitted

            if admitted_in_group:
                # First group with ANY admitted candidate wins!
                if job.priority == Priority.BACKGROUND:
                    bg_candidates = [
                        d for d in admitted_in_group
                        if (d.score or 0.0) >= background_min_pace
                    ]
                    if not bg_candidates:
                        # Pacing below threshold for background job: wait
                        best_below = max(admitted_in_group, key=lambda d: d.score or 0.0)
                        decisions.append(
                            WaitDecision(
                                job=job,
                                reason=WaitingReason.NO_CAPACITY.value,
                                eta=best_below.eta,
                                confidence=best_below.wait_confidence,
                                candidate=best_below.candidate,
                                pool_id=best_below.pool_id,
                                binding_window=best_below.binding_window,
                                details={
                                    "score": best_below.score,
                                    "min_pace": background_min_pace,
                                },
                            )
                        )
                        job_dispatched = True
                        break
                    admitted_in_group = bg_candidates

                # Pick candidate with highest score; tie-break: group index, then lower cost
                best = max(
                    admitted_in_group,
                    key=lambda d: (
                        d.score or 0.0,
                        -group.index(d.candidate),
                        -(d.cost_estimate or 0.0),
                    ),
                )

                # Increment simulated running count for ALL drawn pools (F3)
                all_drawn = best.drawn_pools or ((best.pool_id,) if best.pool_id else ())
                for pid in all_drawn:
                    simulated_running[pid] = simulated_running.get(pid, 0) + 1
                global_running += 1

                # Decrement budget across ALL drawn pools and windows (F2)
                for draw in best.draws:
                    cost = _get_cost_estimate(
                        best.candidate, draw.pool_id, draw.window_id, size_bucket, cost_states
                    ) * float(draw.multiplier)
                    p_key = (draw.pool_id, draw.window_id)
                    simulated_reservations[p_key] = (
                        simulated_reservations.get(p_key, 0.0) + cost
                    )

                decisions.append(
                    DispatchDecision(
                        job=job,
                        candidate=best.candidate,
                        pool_id=best.pool_id,
                        binding_window=best.binding_window,
                        score=best.score or 0.0,
                        cost_estimate=best.cost_estimate or 0.0,
                        multiplier=best.multiplier,
                        drawn_pools=best.drawn_pools,
                        draws=best.draws,
                        skipped_candidates=tuple(stale_skips),
                    )
                )
                job_dispatched = True
                break

            # No candidates in this group admitted. Check prefer_wait_minutes
            prefer_wait = pacing_meta[g_idx] if g_idx < len(pacing_meta) else 0
            if prefer_wait > 0:
                wait_candidates = [
                    d
                    for d in decisions_for_group
                    if d.eta is not None
                    and d.wait_confidence in ("EXACT", "PARSED")
                    and d.eta <= moment + timedelta(minutes=prefer_wait)
                ]
                if wait_candidates:
                    # Prefer waiting for this group rather than falling back to lower groups
                    best_wait = min(
                        wait_candidates,
                        key=lambda d: d.eta or _FAR_FUTURE,
                    )
                    decisions.append(
                        WaitDecision(
                            job=job,
                            reason=WaitingReason.NO_CAPACITY.value,
                            eta=best_wait.eta,
                            confidence=best_wait.wait_confidence,
                            candidate=best_wait.candidate,
                            pool_id=best_wait.pool_id,
                            binding_window=best_wait.binding_window,
                            details={"prefer_wait_minutes": prefer_wait},
                        )
                    )
                    job_dispatched = True
                    break

        if not job_dispatched:
            # All groups exhausted without dispatch or prefer_wait
            # Check for needed probes across denied candidates.  A harness the
            # probe subsystem never runs automatically (Claude's interactive
            # usage reader) must not be promised as a probe: that parked the
            # job in PROBE_PENDING forever with no remedy (D9).  Collect those
            # denials as an owner-only quota-evidence refusal instead.
            from saberops.capacity.probes import AUTO_SAFE_HARNESSES

            owner_evidence: list[AdmissionDecision] = []
            job_probe_needed = False
            red_line_reasons = {
                "WEEKLY_QUOTA_RED_LINE",
                "RED_LINE_5H",
                "RED_LINE_DAILY",
                "RED_LINE_WEEKLY",
                "RED_LINE_MONTHLY",
                "RED_LINE",
            }
            for g_dec in all_group_decisions:
                for d in g_dec:
                    if not (d.needs_probe and d.probe_harness and d.pool_id):
                        continue
                    if d.reason == "QUOTA_EVIDENCE_REQUIRED":
                        if d.probe_harness not in AUTO_SAFE_HARNESSES:
                            owner_evidence.append(d)
                            continue
                        job_probe_needed = True
                        if d.probe_harness not in probes_needed:
                            probes_needed[d.probe_harness] = ProbeDecision(
                                pool_id=d.pool_id,
                                harness=d.probe_harness,
                            )
                    elif d.reason in red_line_reasons:
                        # A red line tipped by synthetic post-observation
                        # charges is re-checked with a fresh safe probe before
                        # it refuses (N7).  A genuinely low pool reads low and
                        # keeps the refusal on the next tick; an unsafe harness
                        # (Claude's interactive reader) keeps today's
                        # conservative wait with no probe demand.
                        if d.probe_harness not in AUTO_SAFE_HARNESSES:
                            continue
                        job_probe_needed = True
                        if d.probe_harness not in probes_needed:
                            probes_needed[d.probe_harness] = ProbeDecision(
                                pool_id=d.pool_id,
                                harness=d.probe_harness,
                            )

            first_dec = (
                all_group_decisions[0][0]
                if all_group_decisions and all_group_decisions[0]
                else None
            )
            # Pick wait ETA as minimum ETA over decisions with EXACT/PARSED confidence
            best_eta_dec: AdmissionDecision | None = None
            for g in all_group_decisions:
                for d in g:
                    if d.eta is not None and d.wait_confidence in ("EXACT", "PARSED"):
                        if best_eta_dec is None or (
                            best_eta_dec.eta is not None and d.eta < best_eta_dec.eta
                        ):
                            best_eta_dec = d

            dec_for_wait = best_eta_dec if best_eta_dec is not None else first_dec
            # Per job: another queued job's probe never makes this job's
            # owner-only denial look probeable.
            has_probe = job_probe_needed
            quota_refusal = owner_evidence[0] if owner_evidence else None
            is_concurrency = any(
                d.reason in ("CONCURRENCY_LIMIT", "GLOBAL_CONCURRENCY_LIMIT")
                for g in all_group_decisions
                for d in g
            )
            if quota_refusal is not None and not has_probe:
                dec_for_wait = quota_refusal
            wait_reason = (
                WaitingReason.PROBE_PENDING.value
                if has_probe
                else (
                    WaitingReason.QUOTA_EVIDENCE_REQUIRED.value
                    if quota_refusal is not None
                    else (
                        WaitingReason.CONCURRENCY.value
                        if is_concurrency
                        else WaitingReason.NO_CAPACITY.value
                    )
                )
            )
            eta = dec_for_wait.eta if dec_for_wait else None
            confidence = dec_for_wait.wait_confidence if dec_for_wait else None
            details: dict[str, Any] = {}
            if quota_refusal is not None and not has_probe:
                refusal_provider = (
                    quota_refusal.candidate.provider
                    if quota_refusal.candidate is not None
                    else None
                )
                refusal_pool = quota_refusal.pool_id
                remedy = (
                    f"orch quota set {refusal_provider} {refusal_pool} "
                    "--remaining <percent>"
                    if refusal_provider and refusal_pool
                    else "orch quota set <provider> <pool> --remaining <percent>"
                )
                details = {
                    "diagnostic": "QUOTA_EVIDENCE_REQUIRED",
                    "provider": refusal_provider,
                    "pool_id": refusal_pool,
                    "harness": quota_refusal.probe_harness,
                    "remedy": remedy,
                }
            if vendor_blocked:
                details = {
                    **details,
                    "diagnostic": (
                        details.get("diagnostic") or "VENDOR_CLI_UNVERIFIED"
                    ),
                    "vendor_unverified": dict(sorted(vendor_blocked.items())),
                }
            if stale_skips:
                details["excluded_candidates"] = list(stale_skips)
            decisions.append(
                WaitDecision(
                    job=job,
                    reason=wait_reason,
                    eta=eta,
                    confidence=confidence,
                    candidate=dec_for_wait.candidate if dec_for_wait else None,
                    pool_id=dec_for_wait.pool_id if dec_for_wait else None,
                    binding_window=dec_for_wait.binding_window if dec_for_wait else None,
                    details=details,
                )
            )

    # Return probe decisions followed by job decisions
    return list(probes_needed.values()) + decisions


__all__ = [
    "DispatchDecision",
    "ProbeDecision",
    "SchedulerDecision",
    "WaitDecision",
    "schedule",
]
