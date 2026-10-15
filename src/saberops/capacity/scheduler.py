"""Pure deterministic pacing scheduler (ADR-0002 D7, pure).

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
    Job,
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
) -> list[SchedulerDecision]:
    """Pure deterministic pacing scheduler (ADR-0002 D7).

    Evaluates queued jobs in strict D7 order:
    - URGENT > NORMAL > BACKGROUND
    - Within class: earliest deadline first, then FIFO by created_at
    - Evaluates role fallback groups: best score ONLY within the first group
      with admitted candidates.
    - BACKGROUND jobs require binding pace >= background_min_pace.
    - Honors prefer_wait_minutes on fallback groups when exact/parsed ETA matches.
    - A global aggregate ceiling (``global_slot_limit``) blocks dispatch once
      the number of top-level RUNNING jobs plus same-tick simulated starts
      reaches the ceiling, independent of pool mapping.
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
            groups = routing_snapshot.groups_for(role)
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

        pacing_meta = routing_snapshot.prefer_wait_minutes_for(role)
        size_bucket = job.requirements.size_hint or SizeBucket.M
        all_group_decisions: list[list[AdmissionDecision]] = []
        job_dispatched = False

        for g_idx, group in enumerate(groups):
            # Evaluate admission for every candidate in this group
            decisions_for_group: list[AdmissionDecision] = []
            for cand in group:
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
            admitted_in_group = [d for d in decisions_for_group if d.admitted]

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
            # Check for needed probes across denied candidates
            for g_dec in all_group_decisions:
                for d in g_dec:
                    if d.needs_probe and d.probe_harness and d.pool_id:
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
            has_probe = bool(probes_needed)
            is_concurrency = any(
                d.reason in ("CONCURRENCY_LIMIT", "GLOBAL_CONCURRENCY_LIMIT")
                for g in all_group_decisions
                for d in g
            )
            wait_reason = (
                WaitingReason.PROBE_PENDING.value
                if has_probe
                else (
                    WaitingReason.CONCURRENCY.value
                    if is_concurrency
                    else WaitingReason.NO_CAPACITY.value
                )
            )
            eta = dec_for_wait.eta if dec_for_wait else None
            confidence = dec_for_wait.wait_confidence if dec_for_wait else None
            decisions.append(
                WaitDecision(
                    job=job,
                    reason=wait_reason,
                    eta=eta,
                    confidence=confidence,
                    candidate=dec_for_wait.candidate if dec_for_wait else None,
                    pool_id=dec_for_wait.pool_id if dec_for_wait else None,
                    binding_window=dec_for_wait.binding_window if dec_for_wait else None,
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
