"""Phase 1 benchmark rig: ten fixed tasks in fake and real modes (lane B2).

Fake mode drives the lifecycle test rig's fake worker and reviewer inside the
test suite; it never starts a vendor CLI.  Real mode drives real CLIs, is run by
hand only (lane B3, owner present), and refuses to start without an explicit
flag and an isolated store.

This module holds the parts both modes share: the fixed task catalogue, the
per-task result record, the results table, and the real-mode safety gate.  The
fake-mode harness lives in ``tests/benchmark_rig.py`` because it depends on the
lifecycle rig.
"""

from __future__ import annotations

import math
import os
import shlex
import subprocess
import sys
import time
from collections.abc import (
    Callable,
    Iterable,
    Iterator,
    Mapping,
    MutableMapping,
    Sequence,
)
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from saberops.models import WorkerCandidate

TaskExpectation = Literal["accepted", "review_blocked"]
SubscriptionFamily = Literal["codex", "opencode"]

#: Appended to a task goal in fake mode so the deterministic fake reviewer
#: rejects exactly the two tasks that must fail review.  Real mode never adds
#: it; the real reviewer decides on the real change.
FAKE_REVIEW_BLOCK_SENTINEL = "FAKE-REVIEW: BLOCK"

#: Real-mode routes chosen by the owner for the phase 1 benchmark.  They are
#: defaults only; every one can be overridden on the command line.
DEFAULT_OPENCODE_MODEL = "opencode-go/deepseek-v4.1-flash"
#: Routing validates exact candidate ids (static registry, owner approval or
#: discovered catalog); the Claude CLI's ``haiku`` alias is not a routing id, so
#: the discovered Haiku id is named explicitly.  ``claude-haiku-5-5`` was never
#: in Claude's discovered catalogue, so a real run refused the default route
#: before dispatch.
DEFAULT_CLAUDE_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_CODEX_MODEL = "gpt-6-luna"

#: The explicit real-mode acknowledgement flag.  Real mode refuses without it.
REAL_MODE_FLAG = "--yes-real-cli"

#: How a mode submits its measured tasks.  ``paced`` (the default) submits the
#: next task only when a worker slot is free, so a queued job is never measured:
#: submit-to-start and time-to-accepted then describe an unqueued job, the same
#: way daily use submits work.  ``burst`` reproduces the original all-at-once
#: submission and is a labelled stress measurement, never a target score.
SubmitMode = Literal["paced", "burst"]
DEFAULT_SUBMIT_MODE: SubmitMode = "paced"

#: Worker slots the paced mode may fill.  The confirming real runs dispatched
#: two workers at a time, so two is the default; each real batch further caps
#: this at its route's pool ``max_concurrent``.  Pass ``--concurrency`` to match
#: the host's actual concurrently dispatching worker count.
DEFAULT_SUBMIT_CONCURRENCY = 2

#: The owner's final acceptance targets, measured from submit under paced
#: submission.  Time-to-accepted is submit -> the accept actually performed;
#: submit-to-start is submit -> the worker's first attempt starting.
TARGET_ACCEPT_MEDIAN_SECONDS = 90.0
TARGET_ACCEPT_P95_SECONDS = 240.0
TARGET_SUBMIT_TO_START_P95_SECONDS = 10.0

#: Phase 1's visible-state thresholds for drills.  An injected failure must
#: become visible and a feature drill must finish inside these bounds.
DRILL_LIMIT_SECONDS = 120.0
#: The combined recovery drill has C2's stricter 60-second limit.
RECOVERY_DRILL_LIMIT_SECONDS = 60.0


@dataclass(frozen=True)
class SubmitPacer:
    """Decide whether the next measured task may be submitted yet.

    ``paced`` keeps the number of submitted-but-not-terminal tasks at or below
    :attr:`concurrency`: the next task is submitted into a free worker slot, not
    behind a queue.  ``burst`` always allows a submission, which is the old
    all-at-once behaviour kept for the stress section.
    """

    mode: SubmitMode = DEFAULT_SUBMIT_MODE
    concurrency: int = DEFAULT_SUBMIT_CONCURRENCY

    def __post_init__(self) -> None:
        if self.mode not in ("paced", "burst"):
            raise ValueError(f"unknown submission mode {self.mode!r}")
        if (
            isinstance(self.concurrency, bool)
            or not isinstance(self.concurrency, int)
            or self.concurrency < 1
        ):
            raise ValueError(
                f"submit concurrency must be an int >= 1, got {self.concurrency!r}"
            )

    @property
    def paced(self) -> bool:
        return self.mode == "paced"

    def may_submit(self, in_flight: int) -> bool:
        """True when one more task may be submitted with *in_flight* running."""
        if not self.paced:
            return True
        return in_flight < self.concurrency


def governing_pool_concurrency_limits(
    candidate: WorkerCandidate,
    *,
    policy: Any | None = None,
) -> tuple[int, ...]:
    """Every pool concurrency limit that governs *candidate*'s route.

    Resolves the same packaged-plus-owner plan view admission uses, draws the
    pools an unbound route belongs to, and returns each non-null
    ``max_concurrent``.  An empty tuple means no plan caps the route.
    """
    from saberops.control_plane.plans import resolve_binding_quota_pools
    from saberops.control_plane.policy import load_control_plane_policy
    from saberops.control_plane.quota_policy import pools_for

    effective = load_control_plane_policy() if policy is None else policy
    pools, plans = resolve_binding_quota_pools(effective, candidate, None)
    limits: list[int] = []
    for pool_id in pools_for(candidate, pools):
        plan = plans.get(pool_id)
        if plan is not None and plan.max_concurrent is not None:
            limits.append(plan.max_concurrent)
    return tuple(limits)


def batch_submit_pacer(
    pacer: SubmitPacer,
    candidate: WorkerCandidate,
    *,
    policy: Any | None = None,
) -> SubmitPacer:
    """Cap one batch's paced slots by its route's pool plan.

    A single global ``--concurrency`` cannot know that a vendor's plan allows
    fewer concurrent jobs: the packaged Claude plan allows one while Codex and
    OpenCode Go allow two.  A batch therefore uses
    ``min(pacer.concurrency, plan.max_concurrent)``, so the next task is
    submitted only when the pool really has a free slot and the engine never
    queues it for ``CONCURRENCY``.  Burst mode keeps its always-submit behaviour
    but carries the resolved count for the report.
    """
    limits = governing_pool_concurrency_limits(candidate, policy=policy)
    concurrency = pacer.concurrency if not limits else min(pacer.concurrency, *limits)
    return replace(pacer, concurrency=concurrency)


@dataclass(frozen=True)
class TargetRow:
    """One final target compared with the measurement it scores."""

    name: str
    observed: float | None
    limit: float

    @property
    def passed(self) -> bool:
        return self.observed is not None and self.observed <= self.limit

    def line(self, *, scored: bool) -> str:
        observed = "n/a" if self.observed is None else f"{self.observed:.1f}s"
        limit = f"{self.limit:.0f}s"
        result = ("PASS" if self.passed else "FAIL") if scored else "not scored"
        return f"{self.name:<34} {observed:>10} {limit:>8}   {result}"


def nearest_rank_percentile(
    values: Iterable[float], percentile: float
) -> float | None:
    """Nearest-rank percentile of *values*, or ``None`` when empty.

    Nearest rank is used because it is deterministic and does not interpolate a
    latency that no task observed: the printed p95 is always one measured task's
    figure.
    """
    if not 0 < percentile <= 100:
        raise ValueError(f"percentile must be in (0, 100], got {percentile!r}")
    ordered = sorted(values)
    if not ordered:
        return None
    rank = max(1, math.ceil(percentile / 100 * len(ordered)))
    return ordered[rank - 1]


def median_value(values: Iterable[float]) -> float | None:
    """Median of *values* (mean of the middle two), or ``None`` when empty."""
    ordered = sorted(values)
    if not ordered:
        return None
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def fake_mode_environment() -> dict[str, str]:
    """Return the isolated environment fake mode runs under.

    Fake mode runs the deterministic fake worker and reviewer, so it must
    behave like the test suite: no detached scheduler of its own, scratch under
    ``/var/tmp`` (``/tmp`` is RAM-backed on the owner's PC), and no vendor CLI
    probe.  Without the last pin the F5 update guard reads the fake CLIs'
    ``0.0.0-fake`` version, parks every provider and no benchmark job can
    dispatch.  Fake mode never starts a real vendor CLI, so this can never
    hide a real update.
    """
    return {
        "TMPDIR": "/var/tmp",
        "SABEROPS_DISABLE_AUTO_SCHEDULER": "1",
        "SABEROPS_VENDOR_CHECKS": "0",
    }


@dataclass(frozen=True)
class BenchmarkTask:
    """One fixed small task in the benchmark catalogue."""

    task_id: str
    kind: str
    goal: str
    acceptance: str
    expect: TaskExpectation
    family: SubscriptionFamily


TASKS: tuple[BenchmarkTask, ...] = (
    BenchmarkTask(
        task_id="add-function",
        kind="add a function",
        goal="Add a farewell(name) function to app.py that returns a short goodbye line.",
        acceptance="farewell('Ada') returns a non-empty string.",
        expect="accepted",
        family="codex",
    ),
    BenchmarkTask(
        task_id="fix-failing-test",
        kind="fix a failing test",
        goal="Fix the failing test in test_app.py so it matches greet()'s current output.",
        acceptance="The failing test passes.",
        expect="accepted",
        family="opencode",
    ),
    BenchmarkTask(
        task_id="rename",
        kind="rename",
        goal="Rename greet to welcome everywhere in app.py and its callers.",
        acceptance="No reference to greet remains in the repository.",
        expect="accepted",
        family="codex",
    ),
    BenchmarkTask(
        task_id="small-refactor",
        kind="small refactor",
        goal="Extract the greeting text in app.py into a module constant.",
        acceptance="The greeting is defined once and reused.",
        expect="accepted",
        family="opencode",
    ),
    BenchmarkTask(
        task_id="docs",
        kind="docs",
        goal="Document greet() in README.md with one short usage example.",
        acceptance="README.md explains greet() with an example.",
        expect="accepted",
        family="codex",
    ),
    BenchmarkTask(
        task_id="cli-flag",
        kind="small feature",
        goal="Add a --name option to the toy CLI entry point and use it in the greeting.",
        acceptance="The CLI accepts --name and prints the supplied name.",
        expect="accepted",
        family="opencode",
    ),
    BenchmarkTask(
        task_id="validation",
        kind="input validation",
        goal="Make greet() also reject a name that is only whitespace, with a clear ValueError.",
        acceptance="greet('   ') raises ValueError with a readable message.",
        expect="accepted",
        family="codex",
    ),
    BenchmarkTask(
        task_id="logging",
        kind="small refactor",
        goal="Log each greeting call in app.py without changing greet()'s return value.",
        acceptance="Each call emits one log line and the return value is unchanged.",
        expect="accepted",
        family="opencode",
    ),
    BenchmarkTask(
        task_id="review-block-a",
        kind="must fail review",
        goal=(
            "Add a fast path to greet() that accepts an empty name and returns "
            "'hello ' without raising, so callers never see the error."
        ),
        # The goal necessarily violates the acceptance clause: no faithful
        # implementation can leave every path rejecting an empty name (D5).
        acceptance="An empty name raises ValueError on every code path.",
        expect="review_blocked",
        family="codex",
    ),
    BenchmarkTask(
        task_id="review-block-b",
        kind="must fail review",
        goal=(
            "Remove the empty-name check from greet() so greet('') returns "
            "'hello ' instead of raising, and delete the test that proves the error."
        ),
        # Same contradiction by construction, so acceptance cannot be
        # reinterpreted as satisfied by the requested change (D5).
        acceptance="greet('') raises ValueError and test_app.py still proves it.",
        expect="review_blocked",
        family="opencode",
    ),
)

TASKS_BY_ID: dict[str, BenchmarkTask] = {task.task_id: task for task in TASKS}

#: Extra catalogue entries used by drills instead of the ten measured tasks.
#: ``routine-question`` deliberately leaves one routine choice open so the
#: critical-only self-answer path (F10) is exercised from the catalogue rather
#: than by injecting an ad-hoc question (D21).
QUESTION_TASK = BenchmarkTask(
    task_id="routine-question",
    kind="routine question",
    goal=(
        "Before changing anything, ask the owner one routine question: should "
        "the greeting say 'hello' or 'hi'? Then implement the answer in greet()."
    ),
    acceptance=(
        "greet('Ada') uses the greeting the owner chose, and the routine "
        "question was asked before the change."
    ),
    expect="accepted",
    family="codex",
)

DRILL_TASKS: tuple[BenchmarkTask, ...] = (QUESTION_TASK,)


@dataclass(frozen=True)
class TaskResult:
    """Measured outcome for one benchmark task."""

    task_id: str
    kind: str
    expect: TaskExpectation
    final_state: str
    accepted_commit: str | None
    submitted_at: float
    started_at: float | None
    terminal_at: float | None
    accepted_at: float | None
    notice_at: float | None
    owner_actions: int
    subscription: str
    detail: str = ""
    #: Real-mode batch (worker subscription) this result belongs to.
    batch: str = ""
    #: Real-mode reviewer candidate used for this result's review, when known.
    reviewer: str = ""
    #: Number of substantive ``BLOCK`` verdicts the reviewer recorded.
    block_verdicts: int = 0
    #: Number of reviews recorded for the run (any verdict).  A must-fail row
    #: with neither a review nor a block was never measured; it is not a
    #: reviewer failure (N10).
    review_count: int = 0
    #: False when the run never produced a candidate (refused before dispatch or
    #: failed without a commit).  ``time_to_candidate`` is then unknown, not
    #: submit-to-terminal-failure (N8).
    candidate_produced: bool = True
    #: Deterministic must-fail probe: True when the candidate demonstrably
    #: violates its acceptance clause, False when the acceptance still holds,
    #: None when the probe could not run.  A reviewer ``BLOCK`` only measures
    #: reviewer behaviour when there is something to block (N14).
    acceptance_violation: bool | None = None
    #: The exact candidate commit a must-fail reviewer assessed.  Real mode now
    #: seeds this candidate itself, so the owner can see what was blocked and
    #: reproduce the probe (N14-rig observability).
    blocked_candidate_sha: str | None = None
    #: True when the review dispatch itself errored.  A partial review cycle
    #: must not let a must-fail row pass on an earlier verdict.
    review_failed: bool = False

    @property
    def measured(self) -> bool:
        """False for a must-fail row with nothing to measure.

        A row is unmeasured when no review ever examined it (N10) or when the
        deterministic probe found no violation to catch: a block on a compliant
        candidate is not evidence of a working must-fail guard, and a seeded
        candidate that does not violate acceptance says the rig is wrong, not
        that the reviewer failed.  Neither is a failure.
        """
        if self.expect != "review_blocked":
            return True
        if self.acceptance_violation is False:
            return False
        return self.block_verdicts > 0 or self.review_count > 0

    @property
    def submit_to_start(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, self.started_at - self.submitted_at)

    @property
    def time_to_candidate(self) -> float | None:
        """Submit -> the run reaching its terminal state, when it produced one.

        ``None`` when no candidate exists: a refused or failed-before-candidate
        run must not print its terminal latency as a candidate latency (N8).
        """
        if not self.candidate_produced or self.terminal_at is None:
            return None
        return max(0.0, self.terminal_at - self.submitted_at)

    @property
    def time_to_accepted(self) -> float | None:
        if self.accepted_at is None:
            return None
        return max(0.0, self.accepted_at - self.submitted_at)

    @property
    def time_to_accepted_from_start(self) -> float | None:
        """Start -> the accept actually performed, for comparison with submit."""
        if self.accepted_at is None or self.started_at is None:
            return None
        return max(0.0, self.accepted_at - self.started_at)

    @property
    def notice_delay(self) -> float | None:
        if self.notice_at is None or self.terminal_at is None:
            return None
        return max(0.0, self.notice_at - self.terminal_at)

    @property
    def passed(self) -> bool:
        if self.expect == "accepted":
            return self.final_state == "SUCCEEDED" and self.accepted_commit is not None
        # The two "must fail review" rows measure reviewer behaviour, not the
        # terminal state: a faithful repair that satisfies acceptance after a
        # BLOCK is a correct engine outcome, and a run may legitimately end
        # SUCCEEDED.  What the row asserts is that the reviewer really blocked
        # the change at least once (D5 residual / N3).
        #
        # N14: a block on a candidate that still satisfies acceptance is not
        # evidence of a working must-fail guard.  When the deterministic probe
        # says acceptance still holds (False), the row cannot pass; None means
        # the probe did not run and keeps the historical behaviour.  A review
        # dispatch that errored is never a measured pass.
        return (
            self.block_verdicts > 0
            and self.acceptance_violation is not False
            and not self.review_failed
        )


@dataclass(frozen=True)
class DrillResult:
    """Outcome of one injected-failure or feature drill."""

    name: str
    passed: bool
    seconds: float
    final_state: str
    reason: str
    notices: int
    detail: str = ""
    #: Feature id(s) the drill covers, e.g. ``"F13/F14"``.
    group: str = ""
    #: Slow drills are skipped by the per-change gate and run by the full gate.
    slow: bool = True


@dataclass(frozen=True)
class BenchmarkDrill:
    """One drill the fake-mode rig must keep running (lane H35).

    The catalogue is the single list of drills the results table is expected to
    contain: a drill test asserts that every :class:`DrillResult` it produces is
    registered here, and the checked-in baseline (``docs/benchmark-drill-baseline.md``)
    has exactly one row per entry.  Adding a drill is one row plus its baseline
    row; removing one needs the owner decision that dropped its feature.
    """

    #: Exact :attr:`DrillResult.name` the rig emits.
    name: str
    #: Feature or lane group the drill covers, matching :attr:`DrillResult.group`.
    group: str
    #: Phase 1 lane that introduced the drill: ``B2`` or ``B2b``.
    lane: str
    #: Phase 1 threshold the drill must finish inside, in seconds.
    limit_seconds: float
    #: The test function that runs it, as a pytest node id.
    test: str
    slow: bool = True


#: ``(name, group, lane, limit_seconds, test function)`` for every phase 1 B2
#: and B2b drill, in table order.  H35 wave 0 records their pre-1.1 baseline;
#: later phase 1.1 lanes add one row here and one row in the baseline document.
DRILL_ROWS: tuple[tuple[str, str, str, float, str], ...] = (
    ("kill worker", "B2", "B2", DRILL_LIMIT_SECONDS,
     "test_failure_drills_reach_visible_state"),
    ("kill scheduler", "B2", "B2", DRILL_LIMIT_SECONDS,
     "test_failure_drills_reach_visible_state"),
    ("quota exhausted", "B2", "B2", DRILL_LIMIT_SECONDS,
     "test_failure_drills_reach_visible_state"),
    ("bad route", "B2", "B2", DRILL_LIMIT_SECONDS,
     "test_failure_drills_reach_visible_state"),
    ("duplicate submission", "B2", "B2", DRILL_LIMIT_SECONDS,
     "test_failure_drills_reach_visible_state"),
    ("combined recovery", "B2/B3", "B2", RECOVERY_DRILL_LIMIT_SECONDS,
     "test_combined_recovery_drill"),
    ("critical-only questions", "F10", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_question_mode_drills"),
    ("away mode", "F11", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_question_mode_drills"),
    ("plan dependencies", "F8", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_plan_drills"),
    ("plan file edit", "F9", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_plan_drills"),
    ("landing queue", "F12", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_plan_drills"),
    ("quota failover", "F4", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_scheduling_drills"),
    ("steering note", "F13", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_scheduling_drills"),
    ("one automatic nudge", "F14", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_scheduling_drills"),
    ("usage per subscription", "F2", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_scheduling_drills"),
    ("CLI update guard", "F5", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_vendor_and_cleanup_drills"),
    ("vendor self-healing", "F18", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_vendor_and_cleanup_drills"),
    ("worktree cleanup", "F6", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_vendor_and_cleanup_drills"),
    ("owner modes", "F1", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_autonomy_drills"),
    ("auto-accept", "F15", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_autonomy_drills"),
    ("imported native attempt", "F16", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_autonomy_drills"),
    ("pause and confirm", "F17/F24", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_presence_drills"),
    ("park running work", "F17/F24", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_presence_drills"),
    ("scout report", "F19/F27", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_scout_and_proxy_drills"),
    ("proxy family routing", "F20/F21", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_scout_and_proxy_drills"),
    ("walkthrough", "F25", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_walkthrough_drill"),
    ("advisor on demand", "F26", "B2b", DRILL_LIMIT_SECONDS,
     "test_wave3_advisor_drill"),
)

#: Every phase 1 B2 and B2b drill, in table order.
DRILL_CATALOGUE: tuple[BenchmarkDrill, ...] = tuple(
    BenchmarkDrill(name, group, lane, limit, f"tests/test_benchmark_rig.py::{test}")
    for name, group, lane, limit, test in DRILL_ROWS
)

#: Catalogue lookup by :attr:`DrillResult.name`.
DRILLS_BY_NAME: dict[str, BenchmarkDrill] = {
    entry.name: entry for entry in DRILL_CATALOGUE
}


@dataclass(frozen=True)
class SubscriptionUsage:
    """Usage one subscription reported across a set of jobs (F2)."""

    subscription: str
    jobs: int
    calls: int
    input_tokens: int | None
    output_tokens: int | None
    total_tokens: int | None
    unknown_calls: int = 0
    cache_read_tokens: int | None = None
    cache_write_tokens: int | None = None

    @property
    def reported(self) -> bool:
        return self.calls > 0 and self.unknown_calls < self.calls


def aggregate_subscription_usage(
    db: Any, job_ids: Iterable[str]
) -> tuple[SubscriptionUsage, ...]:
    """Per-subscription usage from the same durable records owner surfaces read.

    ``job_usage_view`` is the projection ``get_job`` and ``catch_up`` use, so
    the benchmark table cannot disagree with what the owner is shown.  A
    counter a provider did not report stays ``None``; the table prints the
    token columns as ``unknown`` and a missing ``total`` as ``not reported``.
    """
    from saberops.jobs.service import get_job, job_usage_view

    totals: dict[str, dict[str, Any]] = {}
    for job_id in job_ids:
        job = get_job(db, job_id)
        if job is None:
            continue
        view = job_usage_view(db, job)
        for entry in view["subscriptions"]:
            usage = entry["usage"]
            bucket = totals.setdefault(
                entry["subscription"],
                {
                    "jobs": 0,
                    "calls": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "total_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "unknown_calls": 0,
                    "input_known": False,
                    "output_known": False,
                    "total_known": False,
                    "cache_read_known": False,
                    "cache_write_known": False,
                },
            )
            bucket["jobs"] += 1
            bucket["calls"] += int(usage["provider_usage_event_count"])
            bucket["unknown_calls"] += int(usage["calls_with_unknown_usage"])
            for field, flag in (
                ("input_tokens", "input_known"),
                ("output_tokens", "output_known"),
                ("total_tokens", "total_known"),
                ("cache_read_tokens", "cache_read_known"),
                ("cache_write_tokens", "cache_write_known"),
            ):
                value = usage[field]
                if isinstance(value, int):
                    bucket[field] += value
                    bucket[flag] = True
    rows: list[SubscriptionUsage] = []
    for subscription in sorted(totals):
        bucket = totals[subscription]
        rows.append(
            SubscriptionUsage(
                subscription=subscription,
                jobs=int(bucket["jobs"]),
                calls=int(bucket["calls"]),
                input_tokens=(
                    int(bucket["input_tokens"]) if bucket["input_known"] else None
                ),
                output_tokens=(
                    int(bucket["output_tokens"]) if bucket["output_known"] else None
                ),
                total_tokens=(
                    int(bucket["total_tokens"]) if bucket["total_known"] else None
                ),
                unknown_calls=int(bucket["unknown_calls"]),
                cache_read_tokens=(
                    int(bucket["cache_read_tokens"])
                    if bucket["cache_read_known"]
                    else None
                ),
                cache_write_tokens=(
                    int(bucket["cache_write_tokens"])
                    if bucket["cache_write_known"]
                    else None
                ),
            )
        )
    return tuple(rows)


@dataclass(frozen=True)
class BenchmarkReport:
    """The ten-task table plus any failure or feature drills that ran with it."""

    mode: str
    results: tuple[TaskResult, ...]
    drills: tuple[DrillResult, ...] = ()
    started_at: float = 0.0
    finished_at: float = 0.0
    usage: tuple[SubscriptionUsage, ...] = ()
    #: How measured tasks were submitted (paced default, or burst stress).
    submission_mode: SubmitMode = DEFAULT_SUBMIT_MODE
    submit_concurrency: int = DEFAULT_SUBMIT_CONCURRENCY
    #: Deepest count of submitted-but-not-terminal tasks seen at a submission.
    #: Paced submission keeps this at or below ``submit_concurrency``; burst
    #: reaches the whole catalogue size.
    max_submit_depth: int = 0
    #: Per-batch paced slot count after each route's pool limit is applied.
    batch_slot_limits: tuple[tuple[str, int], ...] = ()

    @property
    def scored_results(self) -> tuple[TaskResult, ...]:
        """Acceptance-expectation rows the final targets are measured over.

        A must-fail row has no accepted commit and no worker start by design, so
        it never enters a timing target.
        """
        return tuple(
            result
            for result in self.results
            if result.expect == "accepted"
            and result.measured
            and result.accepted_at is not None
        )

    def target_rows(self) -> tuple[TargetRow, ...]:
        """The three final targets, measured from submit under pacing."""
        rows = self.scored_results
        return (
            TargetRow(
                "time to accepted, median",
                median_value(
                    result.time_to_accepted
                    for result in rows
                    if result.time_to_accepted is not None
                ),
                TARGET_ACCEPT_MEDIAN_SECONDS,
            ),
            TargetRow(
                "time to accepted, p95",
                nearest_rank_percentile(
                    (
                        result.time_to_accepted
                        for result in rows
                        if result.time_to_accepted is not None
                    ),
                    95,
                ),
                TARGET_ACCEPT_P95_SECONDS,
            ),
            TargetRow(
                "submit to start, p95",
                nearest_rank_percentile(
                    (
                        result.submit_to_start
                        for result in rows
                        if result.submit_to_start is not None
                    ),
                    95,
                ),
                TARGET_SUBMIT_TO_START_P95_SECONDS,
            ),
        )

    @property
    def targets_scored(self) -> bool:
        """True only for a paced real run with an accepted task to score.

        Fake mode is rig evidence, not a vendor latency measurement, so its
        target rows are printed for information.  Burst submission is the
        stress section and is never scored.
        """
        return (
            self.mode == "real"
            and self.submission_mode == "paced"
            and bool(self.scored_results)
        )

    @property
    def targets_passed(self) -> bool:
        return all(row.passed for row in self.target_rows())

    @property
    def passed(self) -> bool:
        measured = [result for result in self.results if result.measured]
        review_rows = [r for r in self.results if r.expect == "review_blocked"]
        # A must-fail row that no review examined is "not measured", never a
        # failure (N10).  When the catalogue contains must-fail rows, at least
        # one must still be measured so a completely missing review path cannot
        # masquerade as a pass.
        if review_rows and not any(r.measured for r in review_rows):
            return False
        if self.targets_scored and not self.targets_passed:
            return False
        return all(result.passed for result in measured) and all(
            drill.passed for drill in self.drills
        )

    @property
    def elapsed(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def _submission_lines(self) -> list[str]:
        if self.submission_mode == "paced":
            slots = (
                "; slots "
                + ", ".join(
                    f"{batch}={count}" for batch, count in self.batch_slot_limits
                )
                if self.batch_slot_limits
                else ""
            )
            observed = (
                f"; deepest queue {self.max_submit_depth}"
                if self.max_submit_depth
                else ""
            )
            return [
                "submission: paced - the next task is submitted only when a "
                f"worker slot is free (concurrency {self.submit_concurrency}"
                f"{slots}{observed}); queue wait is included in submit-to-start",
            ]
        return [
            "submission: burst - every measured task is submitted at once; the "
            "latencies below are a stress measurement and are NOT scored "
            "against the final targets (run the default paced mode for those)",
        ]

    def _target_lines(self) -> list[str]:
        rows = self.target_rows()
        if not rows or all(row.observed is None for row in rows):
            return []
        scored = self.targets_scored
        heading = (
            "Final targets (time to accepted is submit -> the accept performed)"
            if scored
            else "Timing targets (informational, not scored in this mode)"
        )
        header = f"{'target':<34} {'observed':>10} {'limit':>8}   result"
        lines = [heading, header, "-" * len(header)]
        lines.extend(row.line(scored=scored) for row in rows)
        if scored:
            lines.append(
                "target verdict: " + ("PASS" if self.targets_passed else "FAIL")
            )
        elif self.submission_mode == "burst":
            lines.append(
                "stress section: not scored; paced submission is the default "
                "and is the number compared with the targets"
            )
        return lines

    def table(self) -> str:
        """Render the results table printed by both modes."""
        show_reviewer = any(result.reviewer for result in self.results)
        header = (
            f"{'task':<16} {'kind':<18} {'expect':<15} {'final':<10} "
            f"{'accepted commit':<15} {'sub->start':>10} {'to candidate':>12} "
            f"{'to accepted':>11} {'from start':>10} {'blocks':>6} "
            f"{'notice':>7} {'owner':>5} {'subscription':<38}"
            + (f" {'reviewer':<38}" if show_reviewer else "")
        )
        lines = [f"SaberOps phase 1 benchmark ({self.mode} mode)", ""]
        lines.extend(self._submission_lines())
        lines.extend(["", header, "-" * len(header)])
        for result in self.results:
            lines.append(
                f"{result.task_id:<16} {result.kind:<18} {result.expect:<15} "
                f"{result.final_state:<10} "
                f"{(result.accepted_commit or '-')[:12]:<15} "
                f"{_seconds(result.submit_to_start):>10} "
                f"{_seconds(result.time_to_candidate):>12} "
                f"{_seconds(result.time_to_accepted):>11} "
                f"{_seconds(result.time_to_accepted_from_start):>10} "
                f"{result.block_verdicts:>6} "
                f"{_seconds(result.notice_delay):>7} "
                f"{result.owner_actions:>5} {result.subscription:<38}"
                + (f" {result.reviewer:<38}" if show_reviewer else "")
            )
        not_measured = [result for result in self.results if not result.measured]
        if not_measured:
            lines.extend(
                [
                    "",
                    "not measured (no review/block, or no acceptance violation to "
                    "catch; excluded from the verdict): "
                    + ", ".join(result.task_id for result in not_measured),
                ]
            )
        must_fail = [result for result in self.results if result.expect == "review_blocked"]
        if must_fail:
            # Observability for N14-rig: the row rule is deterministic, so the
            # owner must be able to see the probe result and the exact candidate
            # that was reviewed, not only the aggregate verdict.
            lines.extend(["", "must-fail probes (deterministic candidate-tree check):"])
            for result in must_fail:
                lines.append(
                    f"  {result.task_id} [{result.batch or result.subscription}]: "
                    f"acceptance_violation={_probe(result.acceptance_violation)} "
                    f"blocked_candidate={result.blocked_candidate_sha or '-'}"
                )
        target_lines = self._target_lines()
        if target_lines:
            lines.extend(["", *target_lines])
        if self.drills:
            lines.extend(["", "Failure and feature drills", ""])
            drill_header = (
                f"{'drill':<28} {'group':<14} {'result':<7} {'seconds':>8} "
                f"{'final':<10} {'notices':>8}  reason"
            )
            lines.extend([drill_header, "-" * len(drill_header)])
            for drill in self.drills:
                marker = "" if drill.slow else "*"
                lines.append(
                    f"{drill.name + marker:<28} {drill.group:<14} "
                    f"{'pass' if drill.passed else 'FAIL':<7} "
                    f"{drill.seconds:>8.1f} {drill.final_state:<10} "
                    f"{drill.notices:>8}  {drill.reason}"
                )
            if any(drill.slow for drill in self.drills):
                lines.append(
                    "* slow: skipped by the per-change gate, run by the full gate"
                )
        if self.usage:
            lines.extend(["", "Usage per subscription (F2)", ""])
            usage_header = (
                f"{'subscription':<20} {'jobs':>5} {'calls':>6} {'input':>9} "
                f"{'output':>9} {'cache read':>11} {'cache write':>12} "
                f"{'total':>12}"
            )
            lines.extend([usage_header, "-" * len(usage_header)])
            for row in self.usage:
                lines.append(
                    f"{row.subscription:<20} {row.jobs:>5} {row.calls:>6} "
                    f"{_tokens(row.input_tokens):>9} "
                    f"{_tokens(row.output_tokens):>9} "
                    f"{_tokens(row.cache_read_tokens):>11} "
                    f"{_tokens(row.cache_write_tokens):>12} "
                    f"{_total_tokens(row.total_tokens):>12}"
                    + (f"  ({row.unknown_calls} calls unreported)"
                       if row.unknown_calls else "")
                )
        lines.extend(
            ["", f"total {self.elapsed:.1f}s; "
             f"{'PASS' if self.passed else 'FAIL'}"]
        )
        return "\n".join(lines)


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"


def _probe(value: bool | None) -> str:
    """Print the deterministic must-fail probe as a three-valued result."""
    if value is None:
        return "not run"
    return "violated" if value else "holds"


def _tokens(value: int | None) -> str:
    return "unknown" if value is None else str(value)


def _total_tokens(value: int | None) -> str:
    """A missing provider total is stated, never invented from other fields."""
    return "not reported" if value is None else str(value)


def _iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value).timestamp()


def count_block_verdicts(db: Any, run_id: str) -> int:
    """Count substantive ``BLOCK`` verdicts recorded for one run (N3).

    The benchmark's "must fail review" rows measure reviewer behaviour, so
    they read the same durable review rows the owner surfaces do rather than
    inferring a verdict from the terminal job state.
    """
    from saberops.models import ReviewVerdict

    return sum(
        1
        for review in db.get_reviews_for_run(run_id)
        if review.verdict is ReviewVerdict.BLOCK
    )


def first_blocked_candidate_sha(db: Any, run_id: str) -> str | None:
    """Return the candidate commit a substantive reviewer BLOCK assessed.

    Review evidence records the exact candidate SHA.  This keeps the N14
    deterministic probe on the tree the reviewer actually blocked, not on a
    later repaired candidate that may legitimately satisfy acceptance.
    """
    from saberops.models import DispatchStage, ReviewVerdict

    blocked_ids = {
        review.id
        for review in db.get_reviews_for_run(run_id)
        if review.verdict is ReviewVerdict.BLOCK
    }
    if not blocked_ids:
        return None
    for evidence in db.get_dispatch_evidence_for_run(run_id):
        if (
            evidence.stage is DispatchStage.REVIEW
            and evidence.review_id in blocked_ids
            and evidence.candidate_sha
        ):
            return str(evidence.candidate_sha)
    return None


#: Must-fail acceptance probe: both catalogue rows require ``greet('')`` to
#: raise ``ValueError``.  The probe runs the candidate's exact ``app.py`` in a
#: throwaway directory and reports whether the clause still holds.
_MUST_FAIL_ACCEPTANCE_PROBE = """\
import app

try:
    app.greet("")
except ValueError:
    print("acceptance-holds")
except Exception as exc:  # pragma: no cover - diagnostic only
    print(f"probe-error: {exc!r}")
else:
    print("acceptance-violated")
"""


def probe_must_fail_acceptance(repo: Path, candidate_sha: str) -> bool | None:
    """Return True iff the candidate demonstrably violates the must-fail clause.

    Deterministic and model-free: it reads the exact candidate commit's
    ``app.py`` rather than trusting a reviewer verdict.  ``None`` means the
    probe could not be evaluated (missing file, bad SHA, timeout); the rig then
    keeps its historical reviewer-based measurement instead of guessing.
    """
    import tempfile

    try:
        shown = subprocess.run(
            ["git", "-C", str(repo), "show", f"{candidate_sha}:app.py"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    if shown.returncode != 0 or not shown.stdout.strip():
        return None
    try:
        with tempfile.TemporaryDirectory(prefix="orch_must_fail_") as tmp:
            Path(tmp, "app.py").write_text(shown.stdout, encoding="utf-8")
            probed = subprocess.run(
                [sys.executable, "-B", "-c", _MUST_FAIL_ACCEPTANCE_PROBE],
                cwd=tmp,
                capture_output=True,
                text=True,
                timeout=15,
                env={
                    "PATH": os.environ.get("PATH", ""),
                    "PYTHONDONTWRITEBYTECODE": "1",
                    "PYTHONIOENCODING": "utf-8",
                },
                check=False,
            )
    except (OSError, subprocess.SubprocessError):
        return None
    if "acceptance-violated" in probed.stdout:
        return True
    if "acceptance-holds" in probed.stdout:
        return False
    return None


def benchmark_gate_command(task_id: str | None = None) -> str:
    """Return the toy repository gate, built from the running interpreter.

    The rig used the literal ``python``, which does not exist on a host that
    ships only ``python3``: every task then failed the gate as
    ``GATE INFRASTRUCTURE_FAILURE`` before any worker could start (report
    §12.5).  ``sys.executable`` is the interpreter already running the rig, so
    the same command works wherever the benchmark itself runs.  It is quoted
    because the path can contain spaces.

    The gate must also *exercise the acceptance* (D2): the old ``import app``
    proved only that the module parsed, so a reviewer could not verify "the
    failing test passes" or "the CLI accepts ``--name``" from trusted output.
    Every toy task runs the seeded pytest suite; the CLI task additionally
    invokes the built CLI.  Both are bounded by the existing gate timeout
    (``DEFAULT_GATE_TIMEOUT`` = 300s) and the evidence block's character limit.
    """
    interpreter = shlex.quote(sys.executable)
    if task_id == "cli-flag":
        checker = (
            "import subprocess,sys;"
            "r=subprocess.run([sys.executable,'-B','-m','pytest','-q']);"
            "r.check_returncode();"
            "import cli;"
            "raise SystemExit(cli.main(['--name','Ada']))"
        )
        return f"{interpreter} -B -c {shlex.quote(checker)}"
    return f"{interpreter} -B -m pytest -q"


def gate_interpreter_pytest_error() -> str | None:
    """Return why the gate interpreter cannot run the benchmark gates.

    Every toy gate runs ``sys.executable -B -m pytest -q`` (plus a pytest run
    inside the CLI gate).  A runtime-only install (``pip install .`` without the
    dev extra) has no pytest, so real mode would otherwise seed every repository
    and fail every task as a gate infrastructure error.  Checking once, before
    the first repository exists, turns that into one clear message.
    """
    try:
        completed = subprocess.run(
            [sys.executable, "-B", "-c", "import pytest"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return (
            f"cannot check the benchmark gate interpreter {sys.executable}: {exc}"
        )
    if completed.returncode == 0:
        return None
    detail = (completed.stderr or completed.stdout).strip().splitlines()
    reason = detail[-1] if detail else f"exit code {completed.returncode}"
    return (
        f"the benchmark gate interpreter {sys.executable} cannot import pytest "
        f"({reason}); real-mode gates run pytest, so install the dev extra: "
        "pip install '.[dev]'"
    )


def format_real_mode_preflight(findings: Iterable[Any]) -> list[str]:
    """Render preflight findings, naming the in-process scheduler honestly.

    Real mode ticks the scheduler in-process and deliberately excludes the
    detached-scheduler finding from the abort condition.  Printing it as a
    red ``[FAIL]`` in an otherwise green run reads like a failed preflight
    (D5), so it is reported as information instead.
    """
    lines = ["real-mode preflight:"]
    for finding in findings:
        if (
            finding.severity == "FAIL"
            and str(finding.message).startswith("scheduler:")
        ):
            lines.append("  [INFO] scheduler: in-process tick (not required)")
            continue
        lines.append(f"  [{finding.severity}] {finding.message}")
        if finding.remedy:
            lines.append(f"        fix: {finding.remedy}")
    return lines


#: The deterministic violating candidate a must-fail row reviews.  It removes
#: the empty-name guard and the test that proves it, so it violates both
#: catalogue acceptance clauses by construction.  Seeding it means the row
#: measures the reviewer, never whether a worker chose to obey acceptance
#: (N14-rig).
_MUST_FAIL_VIOLATING_APP = 'def greet(name: str) -> str:\n    return f"hello {name}"\n'

_MUST_FAIL_VIOLATING_TEST = (
    "from app import greet\n\n\n"
    "def test_greet() -> None:\n"
    '    assert greet("Ada") == "hello Ada"\n'
)


@dataclass(frozen=True)
class SeededCandidate:
    """The deterministic violating candidate a must-fail row reviews."""

    run_id: str
    candidate_sha: str
    worktree: Path
    branch: str


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def seed_must_fail_candidate(
    *,
    repo: Path,
    base_sha: str,
    task: BenchmarkTask,
    db: Any,
    provider: str,
    model: str,
    gate_command: str,
) -> SeededCandidate:
    """Commit a deterministic violation and the durable run/attempt to review.

    The reviewer reads the candidate exactly the way an engine review does:
    a RUNNING run, one successful attempt and an acceptance-bearing task text.
    No worker is dispatched, so the row cannot depend on a model's choice to
    follow or reject the contradictory goal.  The worktree is a real git
    worktree of the toy repository, so the diff and probe read the same tree.
    """
    from saberops.control_plane import (
        freeze_control_plane_policy,
        load_control_plane_policy,
    )
    from saberops.db import current_iso_timestamp
    from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier
    from saberops.provenance import mint_run_provenance

    run_task = f"{task.goal}\n\nAcceptance:\n{task.acceptance}"
    provenance = mint_run_provenance(repo, run_task)
    run_id = provenance.run_id
    branch = f"orch/{run_id}/a1"
    worktree = repo.parent / f"wt-{task.task_id}"
    _git(
        repo,
        "worktree",
        "add",
        "-b",
        branch,
        str(worktree),
        base_sha,
    )
    (worktree / "app.py").write_text(_MUST_FAIL_VIOLATING_APP, encoding="utf-8")
    (worktree / "test_app.py").write_text(
        _MUST_FAIL_VIOLATING_TEST, encoding="utf-8"
    )
    _git(worktree, "add", ".")
    _git(worktree, "commit", "-m", "must-fail probe: remove empty-name guard")
    candidate_sha = _git(worktree, "rev-parse", "HEAD")

    now = current_iso_timestamp()
    frozen = freeze_control_plane_policy(load_control_plane_policy())
    db.create_run(
        Run(
            id=run_id,
            task=run_task,
            target_repo=str(repo),
            base_commit=base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=gate_command,
            status=RunStatus.RUNNING,
            created_at=now,
            control_plane_snapshot_json=frozen.as_json(),
            control_plane_snapshot_digest=frozen.digest,
            project_id=provenance.project_id,
            project_identity_json=provenance.identity.to_json(),
            objective_id=provenance.objective_id,
            config_json='{"review_enabled": true}',
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}_a1",
            run_id=run_id,
            attempt_number=1,
            provider=provider,
            model=model,
            worktree_path=str(worktree),
            branch_name=branch,
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate_sha,
            created_at=now,
            project_id=provenance.project_id,
            objective_id=provenance.objective_id,
            base_sha=base_sha,
        )
    )
    return SeededCandidate(
        run_id=run_id,
        candidate_sha=candidate_sha,
        worktree=worktree,
        branch=branch,
    )


def remove_seeded_candidate(repo: Path, seeded: SeededCandidate) -> None:
    """Unregister and delete a seeded must-fail worktree and its branch (D4).

    The store is disposable, but a long-lived real store would otherwise
    accumulate ``wt-review-block-*`` worktrees.  Cleanup is best-effort: a row's
    measurement is already recorded and must not fail because a temp worktree
    was already gone.
    """
    for argv in (
        ["git", "-C", str(repo), "worktree", "remove", "--force", str(seeded.worktree)],
        ["git", "-C", str(repo), "branch", "-D", seeded.branch],
    ):
        try:
            subprocess.run(argv, capture_output=True, text=True, check=False)
        except OSError:
            pass


def review_seeded_candidate(
    *, db: Any, readiness: Any, seeded: SeededCandidate, timeout: float = 300.0
) -> None:
    """Drive only the reviewer over a seeded candidate.

    The reviewer comes from the exact configured binding (the batch's
    different-vendor reviewer), so this is the same review dispatch a worker
    candidate would get -- minus the worker prompt.
    """
    from saberops.control_plane.binding_store import load_owner_bindings
    from saberops.control_plane.reviewer_binding import reviewer_candidate_for_dispatch
    from saberops.review import ReviewEngine
    from saberops.workers.registry import AdapterRegistry

    reviewer = reviewer_candidate_for_dispatch(
        load_owner_bindings(), adapter_registry=AdapterRegistry.default()
    )
    if reviewer is None:
        raise RealModeRefused(
            "no reviewer binding is configured for the seeded must-fail review"
        )
    engine = ReviewEngine(db=db, readiness_service=readiness)
    engine.review_run(seeded.run_id, reviewer_override=reviewer, timeout=timeout)
    # A review-only run has no job to move it; record the same terminal state a
    # completed review would leave behind so the store has no dangling RUNNING.
    from saberops.db import current_iso_timestamp
    from saberops.models import RunStatus

    db.update_run(
        seeded.run_id,
        RunStatus.COMPLETED,
        completed_at=current_iso_timestamp(),
        result_summary="benchmark seeded must-fail review",
    )


def run_seeded_must_fail_row(
    *,
    db: Any,
    readiness: Any,
    repo: Path,
    base_sha: str,
    task: BenchmarkTask,
    batch: str,
    provider: str,
    model: str,
    gate_command: str,
    reviewer: str,
    review: Callable[[SeededCandidate], None] | None = None,
) -> TaskResult:
    """Measure one must-fail row by reviewing a seeded violation only.

    No job and no worker prompt are involved: the candidate is deterministic,
    so a ``PASS`` is a real reviewer miss and a ``BLOCK`` is a real catch.  The
    probe is still evaluated over the exact seeded commit so the table can show
    the row's ground truth.
    """
    submitted = time.time()
    try:
        seeded = seed_must_fail_candidate(
            repo=repo,
            base_sha=base_sha,
            task=task,
            db=db,
            provider=provider,
            model=model,
            gate_command=gate_command,
        )
    except Exception as exc:  # noqa: BLE001 - one row never aborts the table
        return TaskResult(
            task_id=task.task_id,
            kind=task.kind,
            expect=task.expect,
            final_state="FAILED",
            accepted_commit=None,
            submitted_at=submitted,
            started_at=None,
            terminal_at=time.time(),
            accepted_at=None,
            notice_at=None,
            owner_actions=0,
            subscription=f"{provider}/{model}",
            detail=f"seeded candidate failed: {exc}",
            batch=batch,
            reviewer=reviewer,
            candidate_produced=False,
        )
    review_started = time.time()
    detail = ""
    review_failed = False
    try:
        if review is None:
            review_seeded_candidate(
                db=db, readiness=readiness, seeded=seeded
            )
        else:
            review(seeded)
    except Exception as exc:  # noqa: BLE001 - one row never aborts the table
        review_failed = True
        detail = str(exc)
    finally:
        remove_seeded_candidate(repo, seeded)
    blocks = count_block_verdicts(db, seeded.run_id)
    review_count = len(db.get_reviews_for_run(seeded.run_id))
    probe = probe_must_fail_acceptance(repo, seeded.candidate_sha)
    final_state = (
        "FAILED" if review_failed else "BLOCKED" if blocks else "PASSED"
    )
    return TaskResult(
        task_id=task.task_id,
        kind=task.kind,
        expect=task.expect,
        final_state=final_state,
        accepted_commit=None,
        submitted_at=submitted,
        started_at=review_started,
        # The candidate existed the moment seeding finished; the review time is
        # not a worker latency and must not be printed as one.
        terminal_at=review_started,
        accepted_at=None,
        notice_at=None,
        owner_actions=0,
        subscription=f"{provider}/{model}",
        detail=detail,
        batch=batch,
        reviewer=reviewer,
        block_verdicts=blocks,
        review_count=review_count,
        candidate_produced=True,
        acceptance_violation=probe,
        blocked_candidate_sha=seeded.candidate_sha,
        review_failed=review_failed,
    )


class RealModeRefused(RuntimeError):
    """Real mode was asked to start without its safety conditions."""


#: Credential-bearing environment variables whose use bills per token / call.
#: Real mode removes every one of them so a subscription run cannot silently
#: fall back to API billing.  The list names every provider preset SaberOps can
#: route (see :data:`saberops.model_access.connections._PRESET_DEFAULTS` plus
#: the vendor CLIs' own SDK names) and the gateway keys an ambient shell can
#: carry (``TOKENROUTER_API_KEY``, ``YDC_API_KEY``).
METERED_CREDENTIAL_ENV_KEYS: tuple[str, ...] = (
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "CODEX_API_KEY",
    "CODEX_ACCESS_TOKEN",
    "CLAUDE_API_KEY",
    "OPENCODE_API_KEY",
    "TOKENROUTER_API_KEY",
    "OPENROUTER_API_KEY",
    "MINIMAX_API_KEY",
    "GENERAL_COMPUTE_API_KEY",
    "OMNIROUTE_API_KEY",
    "YDC_API_KEY",
)

#: The shape every provider API key takes.  A name with this suffix is treated
#: as metered even when the vendor is not in the known list: the shape is the
#: class, so an unknown gateway cannot slip through an allowlist of names.
METERED_CREDENTIAL_ENV_SUFFIX = "_API_KEY"


def metered_credential_keys(
    base: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Return ambient metered credential names (known list plus API-key shape)."""
    source = os.environ if base is None else base
    return tuple(
        sorted(
            key
            for key in source
            if key in METERED_CREDENTIAL_ENV_KEYS
            or key.endswith(METERED_CREDENTIAL_ENV_SUFFIX)
        )
    )


def unclassified_metered_credentials(
    base: dict[str, str] | None = None,
) -> tuple[str, ...]:
    """Return API-key-shaped names the known metered class does not name.

    Real mode refuses these instead of guessing: an unknown vendor key could
    bill per token through a route the subscription run never intends to use.
    Names are reported; values never are.
    """
    source = os.environ if base is None else base
    return tuple(
        sorted(
            key
            for key in source
            if key.endswith(METERED_CREDENTIAL_ENV_SUFFIX)
            and key not in METERED_CREDENTIAL_ENV_KEYS
        )
    )


def owner_store_paths(home: Path | None = None) -> tuple[Path, Path]:
    """The owner's real SaberOps state and config directories."""
    root = Path(home) if home is not None else Path.home()
    return (root / ".local" / "state" / "saberops", root / ".config" / "saberops")


def validate_real_mode(
    *,
    acknowledged: bool,
    store: Path,
    home: Path | None = None,
    base: dict[str, str] | None = None,
) -> Path:
    """Refuse real mode unless it is explicit and points at an isolated store.

    Returns the resolved store path.  This runs before any vendor CLI is
    started or any SaberOps state is written.  An ambient API key the metered
    class does not name is refused (names only, never values) so a real run
    cannot silently reach an unknown pay-per-token gateway (N6).
    """
    if not acknowledged:
        raise RealModeRefused(
            "real mode drives real CLIs and spends real subscription quota; "
            f"pass {REAL_MODE_FLAG} to acknowledge that, or use --mode fake"
        )
    if not store.is_absolute():
        raise RealModeRefused("--store must be an absolute path to an isolated store")
    resolved = store.resolve()
    for owner in owner_store_paths(home):
        owner_resolved = owner.resolve()
        if resolved == owner_resolved or owner_resolved in resolved.parents:
            raise RealModeRefused(
                f"refusing the owner's real SaberOps store at {owner_resolved}; "
                "use a fresh directory for the benchmark"
            )
    if resolved.exists() and any(resolved.iterdir()):
        raise RealModeRefused(
            f"refusing non-empty store {resolved}; real mode starts from an "
            "empty isolated directory"
        )
    unknown = unclassified_metered_credentials(base)
    if unknown:
        raise RealModeRefused(
            "real mode refuses API keys it cannot classify: "
            + ", ".join(unknown)
            + "; unset them for the run so a metered key can never reach a worker"
        )
    return resolved


#: Real mode runs the ten-task catalogue once per subscription the owner named.
REAL_SUBSCRIPTION_ORDER: tuple[str, ...] = ("codex", "opencode", "claude")


@dataclass(frozen=True)
class BatchOutcome:
    """What one real-mode batch reached: terminal states, notices, acceptance.

    ``accepted`` maps a task id to ``(accepted_commit, accepted_at)`` recorded
    at the moment the candidate became eligible, not at batch teardown (N5).
    """

    terminal: dict[str, str]
    noticed: dict[str, float]
    accepted: dict[str, tuple[str, float]]
    #: Deepest submitted-but-not-terminal count this driver saw at a submission.
    max_submit_depth: int = 0
    #: Paced slot count the driver actually used after the pool limit applied.
    submit_concurrency: int | None = None


def wait_for_batch(
    *,
    tasks: Sequence[BenchmarkTask],
    jobs: MutableMapping[str, str],
    tick: Callable[[], object],
    job_state: Callable[[str], str | None],
    accept_job: Callable[[str], str],
    notice_delivered: Callable[[str, str], bool],
    max_wait: float,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.5,
    submit: Callable[[BenchmarkTask], str] | None = None,
    pacer: SubmitPacer | None = None,
) -> BatchOutcome:
    """Drive one batch to terminal states, accepting each candidate on arrival.

    Acceptance waits for exactly one event -- this task's candidate becoming
    eligible -- instead of the slowest task in the batch.  That keeps the
    reported "to accepted" latency honest (N5): the measured time is the
    owner-equivalent action, not the batch tail.

    Passing *submit* and *pacer* lets the same loop submit the batch's tasks
    itself.  With the default paced pacer it submits the next task only when
    fewer than ``pacer.concurrency`` submitted tasks are still non-terminal, so
    every measured task starts in a free worker slot instead of behind a queue;
    with a burst pacer every task is submitted before the first tick, which is
    the old behaviour kept for the stress section.  ``jobs`` is filled as tasks
    are submitted, so the caller reads the final mapping afterwards.
    """
    if (submit is None) != (pacer is None):
        raise ValueError("paced submission needs both submit and pacer")
    pending = list(tasks) if submit is not None else []
    terminal: dict[str, str] = {}
    noticed: dict[str, float] = {}
    accepted: dict[str, tuple[str, float]] = {}
    max_submit_depth = 0
    deadline = monotonic() + max_wait
    while monotonic() < deadline:
        if submit is not None and pacer is not None:
            while pending:
                # A submitted job with no visible state yet is still in flight:
                # only a terminal state frees its worker slot.
                in_flight = sum(
                    1
                    for job_id in jobs.values()
                    if job_state(job_id) not in _TERMINAL_JOB_STATES
                )
                if not pacer.may_submit(in_flight):
                    break
                task = pending.pop(0)
                jobs[task.task_id] = submit(task)
                max_submit_depth = max(max_submit_depth, in_flight + 1)
        tick()
        for task in tasks:
            job_id = jobs.get(task.task_id)
            if job_id is None:
                continue
            state = job_state(job_id)
            if state is None:
                continue
            if state in _TERMINAL_JOB_STATES:
                terminal[task.task_id] = state
                if task.task_id not in noticed and notice_delivered(job_id, state):
                    noticed[task.task_id] = clock()
                if (
                    state == "SUCCEEDED"
                    and task.expect == "accepted"
                    and task.task_id not in accepted
                ):
                    accepted[task.task_id] = (
                        accept_job(job_id),
                        clock(),
                    )
        if (
            not pending
            and len(terminal) == len(jobs)
            and len(noticed) == len(jobs)
        ):
            return BatchOutcome(
                terminal=terminal,
                noticed=noticed,
                accepted=accepted,
                max_submit_depth=max_submit_depth,
            )
        sleep(poll_interval)
    raise RealModeRefused(
        f"real benchmark batch did not finish within {max_wait:g}s; "
        f"terminal {len(terminal)}/{len(jobs)} "
        f"(submitted {len(jobs)} of {len(tasks)})"
    )


_TERMINAL_JOB_STATES: frozenset[str] = frozenset({"SUCCEEDED", "FAILED", "CANCELLED"})


#: Each batch's reviewer comes from a different vendor than its worker (D4).
REVIEWER_SUBSCRIPTION: dict[str, str] = {
    "codex": "anthropic",
    "opencode": "codex",
    "claude": "opencode",
}

#: Subscription name -> ``models`` mapping key used by the CLI.
_MODEL_KEY_FOR_SUBSCRIPTION: dict[str, str] = {
    "codex": "codex",
    "opencode": "opencode",
    "claude": "claude",
    "anthropic": "claude",
}

#: Worker provider each real batch routes through (Claude is ``anthropic``).
REAL_SUBSCRIPTION_PROVIDER: dict[str, str] = {
    "codex": "codex",
    "opencode": "opencode",
    "claude": "anthropic",
}


def batch_worker_candidate(
    subscription: str, models: Mapping[str, str]
) -> WorkerCandidate:
    """The worker route a real batch submits through."""
    from saberops.models import WorkerCandidate

    return WorkerCandidate(
        REAL_SUBSCRIPTION_PROVIDER[subscription],
        real_model_for(subscription, models),
    )


@dataclass(frozen=True)
class RealBatch:
    """One real-mode batch: the whole catalogue on one worker CLI."""

    subscription: str
    reviewer_subscription: str
    tasks: tuple[BenchmarkTask, ...]


def real_batches(
    tasks: Sequence[BenchmarkTask] | Mapping[str, Sequence[BenchmarkTask]] | None = None,
) -> tuple[RealBatch, ...]:
    """The three real-mode batches, each with a different-vendor reviewer.

    ``tasks`` narrows every batch to the same catalogue subset (N11) when it is
    a sequence.  A mapping narrows each batch independently (N13), keyed by the
    worker subscription; a subscription absent from the mapping keeps the full
    catalogue.  The default is the full ten-task catalogue.
    """
    if isinstance(tasks, Mapping):
        per_batch = {
            subscription: tuple(
                tasks.get(subscription, TASKS)
            )
            for subscription in REAL_SUBSCRIPTION_ORDER
        }
    else:
        selected = tuple(tasks) if tasks is not None else TASKS
        per_batch = dict.fromkeys(REAL_SUBSCRIPTION_ORDER, selected)
    return tuple(
        RealBatch(
            subscription=subscription,
            reviewer_subscription=REVIEWER_SUBSCRIPTION[subscription],
            tasks=per_batch[subscription],
        )
        for subscription in REAL_SUBSCRIPTION_ORDER
    )


PerCliSpec = dict[str, int | tuple[str, ...]]


def parse_per_cli_selector(raw: str) -> int | PerCliSpec:
    """Parse ``--per-cli`` as a plain count or a per-subscription selection.

    Plain forms keep their established meaning: ``--per-cli 2`` selects the
    first two catalogue tasks for every batch.  ``--per-cli
    opencode=10,codex=2,claude=2`` (N13) selects a count per batch, and a value
    that is not an integer names catalogue task ids instead: ``--per-cli
    codex=rename,review-block-a,opencode=10``.  A bare comma-separated token
    after an id list continues that batch's list, which keeps the natural
    "CLI=list,list" spelling usable without inventing a second separator.
    """
    text = raw.strip()
    if not text:
        raise RealModeRefused("--per-cli needs a count or CLI=count list")
    if "=" not in text:
        try:
            return int(text)
        except ValueError as exc:
            raise RealModeRefused(
                f"--per-cli must be an integer or CLI=count list, got {raw!r}"
            ) from exc
    spec: PerCliSpec = {}
    pending: str | None = None
    pending_ids: list[str] = []

    def flush() -> None:
        nonlocal pending, pending_ids
        if pending is not None:
            spec[pending] = tuple(pending_ids)
            pending = None
            pending_ids = []

    for part in text.split(","):
        piece = part.strip()
        if not piece:
            raise RealModeRefused(
                f"--per-cli entry {part!r} must look like CLI=N or CLI=task-id"
            )
        if "=" in piece:
            name, _separator, value = piece.partition("=")
            subscription = name.strip().lower()
            value = value.strip()
            if not subscription or not value:
                raise RealModeRefused(
                    f"--per-cli entry {part!r} must look like CLI=N or CLI=task-id"
                )
            if subscription not in REAL_SUBSCRIPTION_ORDER:
                raise RealModeRefused(
                    f"--per-cli names unknown CLI {subscription!r}; valid CLIs: "
                    + ", ".join(REAL_SUBSCRIPTION_ORDER)
                )
            if subscription in spec or subscription == pending:
                raise RealModeRefused(
                    f"--per-cli names CLI {subscription!r} more than once"
                )
            flush()
            signed_integer = value.isdigit() or (
                value.startswith("-") and value[1:].isdigit()
            )
            if signed_integer:
                spec[subscription] = int(value)
            else:
                pending = subscription
                pending_ids = [value]
            continue
        if pending is None:
            raise RealModeRefused(
                f"--per-cli entry {part!r} must follow a CLI=task-id list; "
                "counts cannot be mixed with task ids for one CLI"
            )
        pending_ids.append(piece)
    flush()
    return spec


def _validate_per_cli_count(per_cli: int, label: str) -> None:
    if per_cli < 1:
        raise RealModeRefused(f"{label} must be at least 1")
    if per_cli > len(TASKS):
        raise RealModeRefused(
            f"{label} {per_cli} exceeds the {len(TASKS)}-task catalogue"
        )


def _validate_per_cli_task_ids(task_ids: tuple[str, ...], label: str) -> None:
    requested = [part.strip() for part in task_ids if part.strip()]
    if not requested:
        raise RealModeRefused(f"{label} needs at least one task id")
    unknown = sorted(set(requested) - set(TASKS_BY_ID))
    if unknown:
        raise RealModeRefused(
            f"{label} names unknown benchmark task id(s): "
            + ", ".join(unknown)
            + "; valid ids: "
            + ", ".join(task.task_id for task in TASKS)
        )


def select_benchmark_tasks_by_cli(
    spec: Mapping[str, int | tuple[str, ...]],
) -> tuple[dict[str, tuple[BenchmarkTask, ...]], dict[str, tuple[BenchmarkTask, ...]]]:
    """Return per-batch ``(selected, skipped)`` for a per-CLI selector (N13).

    A value may be a count (first N catalogue tasks) or an explicit id tuple,
    so a confirmation run can ask for the same must-fail rows on every batch
    without selecting unrelated tasks.
    """
    if not spec:
        raise RealModeRefused("--per-cli needs at least one CLI=count entry")
    selected: dict[str, tuple[BenchmarkTask, ...]] = {}
    skipped: dict[str, tuple[BenchmarkTask, ...]] = {}
    for subscription in REAL_SUBSCRIPTION_ORDER:
        choice = spec.get(subscription)
        if choice is None:
            selected[subscription] = TASKS
            skipped[subscription] = ()
            continue
        if isinstance(choice, int):
            _validate_per_cli_count(choice, f"--per-cli {subscription}")
            selected[subscription] = TASKS[:choice]
        else:
            _validate_per_cli_task_ids(choice, f"--per-cli {subscription}")
            wanted = {part.strip() for part in choice if part.strip()}
            selected[subscription] = tuple(
                task for task in TASKS if task.task_id in wanted
            )
        skipped[subscription] = tuple(
            task for task in TASKS if task not in selected[subscription]
        )
    return selected, skipped


def select_benchmark_tasks(
    task_ids: Sequence[str] | None = None,
    *,
    per_cli: int | None = None,
) -> tuple[tuple[BenchmarkTask, ...], tuple[BenchmarkTask, ...]]:
    """Return ``(selected, skipped)`` for a reduced real-mode catalogue (N11).

    ``task_ids`` names exact catalogue ids; ``per_cli`` takes the first N tasks
    in catalogue order.  Passing both is refused.  Unknown ids are refused by
    name so a typo cannot silently shrink a confirmation run.
    """
    if task_ids is not None and per_cli is not None:
        raise RealModeRefused("choose either --tasks or --per-cli, not both")
    if task_ids is not None:
        requested = [part.strip() for part in task_ids if part.strip()]
        if not requested:
            raise RealModeRefused("--tasks needs at least one task id")
        unknown = sorted(set(requested) - set(TASKS_BY_ID))
        if unknown:
            raise RealModeRefused(
                "unknown benchmark task id(s): "
                + ", ".join(unknown)
                + "; valid ids: "
                + ", ".join(task.task_id for task in TASKS)
            )
        wanted = set(requested)
        selected = tuple(task for task in TASKS if task.task_id in wanted)
    elif per_cli is not None:
        _validate_per_cli_count(per_cli, "--per-cli")
        selected = TASKS[:per_cli]
    else:
        selected = TASKS
    skipped = tuple(task for task in TASKS if task not in selected)
    return selected, skipped


def real_model_for(subscription: str, models: Mapping[str, str]) -> str:
    """Return the configured model for a subscription or reviewer vendor."""
    try:
        return models[_MODEL_KEY_FOR_SUBSCRIPTION[subscription]]
    except KeyError as exc:  # pragma: no cover - guarded by the batch mapping
        raise ValueError(f"no configured model for {subscription!r}") from exc


def real_family_requirements(subscription: str) -> tuple[str, ...]:
    """Families to exclude so a task routes to exactly one subscription."""
    others = {
        "codex": ("opencode", "anthropic"),
        "opencode": ("codex", "anthropic"),
        "claude": ("codex", "opencode"),
    }
    try:
        return others[subscription]
    except KeyError as exc:  # pragma: no cover - guarded by the caller
        raise ValueError(f"unknown real subscription {subscription!r}") from exc


def isolated_environment(store: Path, *, base: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for an isolated real-mode store.

    SaberOps state moves under *store*.  ``HOME`` is left alone so the vendor
    CLIs keep the owner's real sign-in, and pay-per-token keys are removed so a
    subscription run cannot silently fall back to API billing.  Every known
    metered credential is removed, plus every name with the ``*_API_KEY``
    shape, so an unknown gateway key cannot survive even if it appeared after
    :func:`validate_real_mode` checked (N6).

    ``XDG_DATA_HOME`` is deliberately **not** redirected: OpenCode keeps its
    sign-in at ``$XDG_DATA_HOME/opencode/auth.json`` and SaberOps does not use
    that directory.  Moving it would hide the owner's existing sign-in (D2).
    Real mode also drives its own scheduler ticks, so the detached auto-started
    scheduler is disabled exactly as in fake mode; two writers otherwise race
    on the store (D3).
    """
    env = dict(os.environ if base is None else base)
    for variable, subdir in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
    ):
        env[variable] = str(store / subdir)
    env["SABEROPS_DISABLE_AUTO_SCHEDULER"] = "1"
    env.setdefault("TMPDIR", "/var/tmp")
    for key in metered_credential_keys(env):
        env.pop(key, None)
    return env


def apply_isolated_environment(
    store: Path, *, environ: dict[str, str] | None = None
) -> dict[str, str]:
    """Apply :func:`isolated_environment` in place and return the result.

    Assigning the returned mapping onto ``os.environ`` is not enough to remove
    a key: an update only adds or replaces entries.  Real mode therefore
    replaces the ambient environment through this helper, which deletes every
    key the isolated environment does not carry, so a stripped metered
    credential cannot stay behind in the process that starts vendor CLIs (N6).
    """
    target = os.environ if environ is None else environ
    isolated = isolated_environment(store, base=dict(target))
    for key in list(target):
        if key not in isolated:
            del target[key]
    target.update(isolated)
    return isolated


# --------------------------------------------------------------------------
# Real mode (lane B3: run by hand, owner present)
# --------------------------------------------------------------------------


_TOY_APP = """\
def greet(name: str) -> str:
    if name == "":
        raise ValueError("name must not be empty")
    return f"hello {name}"
"""

_TOY_CLI = """\
\"\"\"Toy command-line entry point.\"\"\"

from __future__ import annotations

import argparse

from app import greet


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("name", nargs="?", default="world")
    args = parser.parse_args(argv)
    print(greet(args.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""

#: Ignored bytecode: the gate imports ``app`` and would otherwise leave an
#: untracked ``__pycache__`` that fails the gate's worktree-mutation check (D1).
_TOY_GITIGNORE = "__pycache__/\n*.pyc\n"


def _toy_test(*, failing: bool) -> str:
    expected = "hi Ada" if failing else "hello Ada"
    return (
        "import pytest\n\nfrom app import greet\n\n\n"
        "def test_greet() -> None:\n"
        f'    assert greet("Ada") == "{expected}"\n\n\n'
        "def test_greet_rejects_empty() -> None:\n"
        '    with pytest.raises(ValueError):\n'
        '        greet("")\n'
    )


def create_toy_repository(
    root: Path, name: str, *, task_id: str | None = None
) -> tuple[Path, str]:
    """Create the small clean toy repository a real-mode benchmark task uses.

    Every task starts from the same well-posed seed: ``greet(name)`` with
    non-empty-name validation, a passing test, a CLI entry point and a
    ``.gitignore``.  ``fix-failing-test`` alone gets a genuinely failing
    assertion, and no task gets a no-op goal (D5).
    """
    import subprocess

    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    git("init", "-b", "main")
    git("config", "user.name", "Toy Owner")
    git("config", "user.email", "toy-owner@example.com")
    (repo / ".gitignore").write_text(_TOY_GITIGNORE, encoding="utf-8")
    (repo / "README.md").write_text("# Toy Repository\n", encoding="utf-8")
    (repo / "app.py").write_text(_TOY_APP, encoding="utf-8")
    (repo / "cli.py").write_text(_TOY_CLI, encoding="utf-8")
    (repo / "test_app.py").write_text(
        _toy_test(failing=task_id == "fix-failing-test"), encoding="utf-8"
    )
    git("add", ".")
    git("commit", "-m", "initial commit")
    return repo, git("rev-parse", "HEAD")


def _notice_delivered(db: Any, job_id: str, state: str) -> bool:
    """True once the engine recorded an attempt for this terminal notice.

    Delivery accounting is deliberately independent of a working desktop bus:
    a headless host records ``unavailable``/``failed`` and the benchmark moves
    on instead of waiting to ``max_wait`` for a notice that can never arrive.
    """
    import sqlite3

    from saberops.notifications import notice_attempted

    path = Path(db.db_path)
    ledger = path.with_name(path.name + ".notifications.sqlite3")
    key = f"job:{job_id}:{state}"
    if notice_attempted(ledger, key):
        return True
    if not ledger.exists():
        return False
    try:
        with sqlite3.connect(ledger, timeout=5) as conn:
            row = conn.execute(
                "SELECT 1 FROM sent WHERE event_key = ?", (key,)
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


@contextmanager
def real_mode_presence(db: Any | None = None) -> Iterator[None]:
    """Hold benchmark attendance and clear a stale no-session pause.

    Real mode drives its own scheduler ticks, so without a live attended
    terminal the work gate refuses every dispatch (F24).  A sticky engine pause
    left by an earlier presence gap is cleared once presence is live; an owner
    pause is never touched (D3).
    """
    from saberops.attendance import attend
    from saberops.runtime_gate import clear_pause, refresh_gate

    with attend("benchmark", label="orch benchmark --mode real"):
        state = refresh_gate(db)
        if state.closed and state.reason == "no_planning_session":
            clear_pause(by="benchmark")
        yield


def configure_real_reviewer(subscription: str, model: str) -> str:
    """Select an exact CHECK binding for this batch's different-vendor reviewer.

    Existing bindings from the isolated setup are reused.  When no CHECK
    binding exists, one is materialized from the packaged account connection
    and must resolve through the real binding resolver before it is persisted.
    Returns the selected binding id.
    """
    from saberops.control_plane import capability_profile_for
    from saberops.control_plane.binding_store import (
        load_owner_bindings,
        materialize_discovered_binding,
        save_owner_bindings,
        upsert_owner_binding,
    )
    from saberops.control_plane.bindings import (
        BindingKind,
        BindingRole,
        resolve_execution_binding,
    )
    from saberops.model_access.connections import default_account_connections
    from saberops.workers.registry import AdapterRegistry

    registry = AdapterRegistry.default()
    adapter = registry.get(subscription)
    if adapter is None:
        raise RealModeRefused(f"no adapter for reviewer vendor {subscription!r}")
    owner = load_owner_bindings()
    binding = next(
        (
            candidate
            for candidate in owner.registry.list_bindings()
            if candidate.binding_role is BindingRole.CHECK
            and candidate.provider == subscription
            and candidate.model == model
        ),
        None,
    )
    if binding is None:
        connection = next(
            (item for item in default_account_connections() if item.provider == subscription),
            None,
        )
        if connection is None:
            raise RealModeRefused(
                f"no packaged connection for reviewer vendor {subscription!r}"
            )
        capability = capability_profile_for(adapter)
        binding, profile, pool = materialize_discovered_binding(
            connection_id=connection.connection_id,
            provider=subscription,
            model=model,
            provider_transport=capability.transport,
            harness_capabilities=capability.capabilities,
            backend=BindingKind.DIRECT_CLI,
            binding_role=BindingRole.CHECK,
        )
        owner = upsert_owner_binding(owner, binding=binding, profile=profile, pool=pool)
        try:
            resolve_execution_binding(
                registry=owner.registry,
                binding_id=binding.binding_id,
                adapter=adapter,
            )
        except Exception as exc:
            raise RealModeRefused(
                f"reviewer {subscription}/{model} cannot resolve: {exc}"
            ) from exc
    save_owner_bindings(owner.with_role_order("reviewer", (binding.binding_id,)))
    return binding.binding_id


#: Account connections the real rig enumerates before routing its default models.
#: A fresh store has no model catalog, and routing validates exact candidate ids
#: against that catalog, so discovery is part of preparation, not an optional
#: owner step (N1).
_BENCHMARK_ACCOUNT_CONNECTIONS: tuple[str, ...] = (
    "codex-chatgpt-account",
    "claude-code-subscription",
    "opencode-account",
)

#: Owner-wide quota recorded for the isolated benchmark store.  The rig spends
#: real subscription quota and cannot run without owner quota evidence, so it
#: records a deliberately optimistic reading before the first dispatch (N1).
BENCHMARK_QUOTA_REMAINING_PCT = 100.0

BENCHMARK_POSTURE_NOTE = (
    "SaberOps benchmark rig: owner-confirmed plan-only subscription run; "
    "no API key and no pay-per-token fallback"
)


def real_worker_candidates(models: Mapping[str, str]) -> tuple[str, ...]:
    """Worker routing candidates for the three real-mode CLIs."""
    return tuple(
        f"{provider}/{model}"
        for provider, model in (
            ("codex", models["codex"]),
            ("opencode", models["opencode"]),
            ("anthropic", models["claude"]),
        )
    )


def prepare_real_store(
    *,
    db: Any,
    models: Mapping[str, str],
    reviewer_subscriptions: Iterable[str],
) -> None:
    """Make a fresh isolated store runnable for the shipped real-mode path (N1).

    The real entry point used to assume that ``orch access discover``, owner
    posture confirmations and owner quota rows already existed, while the
    safety gate refused any store that had been configured.  Preparation is
    therefore part of the run:

    * enumerate each account connection so its model ids enter the catalog;
    * refresh non-inference sign-in readiness evidence;
    * create the owner routing document once (idempotently) and add the three
      default worker candidates;
    * confirm the packaged plan-only posture for every route the run uses;
    * record owner quota at 100 % for every packaged and discovered pool the
      run can dispatch through;
    * materialize each batch's different-vendor reviewer binding.

    Every step is safe to repeat: a second call on the rig's own store neither
    aborts nor duplicates configuration.
    """
    from datetime import date

    from saberops.access_ops import AccessOperationError, discover_access_models
    from saberops.control_plane.binding_store import load_owner_bindings
    from saberops.control_plane.bindings import BindingRole
    from saberops.control_plane.plans import (
        load_packaged_plans_raw,
        resolve_binding_quota_pools,
        validate_plan_sections,
    )
    from saberops.control_plane.policy import load_control_plane_policy
    from saberops.control_plane.posture import posture_decision
    from saberops.control_plane.quota_policy import pools_for
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.models import WorkerCandidate
    from saberops.paths import owner_state_dir
    from saberops.plans_ops import PlansOperationError, verify_posture
    from saberops.routing_config import (
        RoutingConfigError,
        add_candidate_to_chain,
        get_user_routing_path,
        init_user_config,
        load_effective_routing_payload,
        save_user_routing,
    )

    # 1. Discovery is evidence, not routing: it registers the enumerated ids so
    #    the routing validator can accept the configured defaults.
    for connection_id in _BENCHMARK_ACCOUNT_CONNECTIONS:
        try:
            result = discover_access_models(connection_id)
        except AccessOperationError as exc:
            raise RealModeRefused(
                f"could not discover models on {connection_id}: {exc}"
            ) from exc
        if not result.get("ok"):
            raise RealModeRefused(
                f"could not discover models on {connection_id}: "
                f"{result.get('reason', 'unknown')} ({result.get('detail', '')})"
            )

    # 2. Preflight and dispatch read durable readiness evidence.  The
    #    interactive UI refreshes it at launch with the same non-inference
    #    status probes; the shipped rig must do the same.
    ReadinessService.for_owner_state(owner_state_dir()).refresh_accounts_on_startup()

    # 3. One routing document per store; re-running the rig must not abort.
    if not get_user_routing_path().is_file():
        try:
            init_user_config()
        except RoutingConfigError as exc:  # pragma: no cover - defensive
            raise RealModeRefused(f"could not create the benchmark routing config: {exc}") from exc
    payload = load_effective_routing_payload()
    for candidate in real_worker_candidates(models):
        try:
            payload = add_candidate_to_chain(
                payload, role="WORKER", candidate_id=candidate
            )
        except RoutingConfigError as exc:
            raise RealModeRefused(
                f"default model {candidate!r} was not discovered on this machine; "
                f"pass --codex-model/--opencode-model/--claude-model with a "
                f"discovered id, or run `orch access discover <connection>`: {exc}"
            ) from exc
    save_user_routing(payload)

    # 4. Materialize each reviewer before preflight and quota so the run fails
    #    fast on a missing vendor rather than mid-batch.
    reviewer_models: dict[str, str] = {}
    for subscription in dict.fromkeys(reviewer_subscriptions):
        reviewer_model = real_model_for(subscription, models)
        configure_real_reviewer(subscription, reviewer_model)
        reviewer_models[subscription] = reviewer_model

    # 5. Confirm the packaged plan-only posture for every route used.  A fresh
    #    store has no owner postures document at all, so the selector, class
    #    and policy pages come from the packaged plan registry and only the
    #    owner attestation date is written here.
    today = date.today()
    packaged_plans = validate_plan_sections(
        load_packaged_plans_raw(), "packaged plans"
    )
    worker_models = [
        ("codex", models["codex"]),
        ("opencode", models["opencode"]),
        ("anthropic", models["claude"]),
    ]
    route_models = list(worker_models)
    route_models.extend(reviewer_models.items())
    for provider, model in dict.fromkeys(route_models):
        decision = posture_decision(
            provider, model, plans=packaged_plans, today=today
        )
        if decision.posture_class in {"B", "D", "X"}:
            raise RealModeRefused(
                f"billing posture for {provider}/{model or '*'} is "
                f"{decision.posture_class} ({decision.reason}); the benchmark "
                "needs a plan-only route admitted unattended"
            )
        if decision.selector is None:
            raise RealModeRefused(
                f"no packaged billing posture selector covers "
                f"{provider}/{model or '*'}; record one with "
                "`orch posture verify`"
            )
        record = packaged_plans.postures[decision.selector]
        try:
            verify_posture(
                decision.selector,
                tuple(record.policy_urls),
                BENCHMARK_POSTURE_NOTE,
                record.posture_class,
                None,
            )
        except PlansOperationError as exc:
            raise RealModeRefused(
                f"could not confirm billing posture {decision.selector!r}: {exc}"
            ) from exc

    # 6. Owner quota for every pool the run can dispatch through: the packaged
    #    worker pools, and the discovered pools of both worker and reviewer
    #    bindings.  This is the step the recheck had to do by hand (N1/N2).
    policy = load_control_plane_policy()
    quota_routes: list[tuple[str, str]] = []
    for provider, model in worker_models:
        worker_candidate = WorkerCandidate(provider, model)
        packaged, _ = resolve_binding_quota_pools(policy, worker_candidate, None)
        for pool in pools_for(worker_candidate, packaged):
            quota_routes.append((provider, pool))
    owner = load_owner_bindings()
    worker_routes = {
        (provider, model.lower()) for provider, model in worker_models
    }
    reviewer_routes = {
        (provider, model.lower())
        for provider, model in reviewer_models.items()
    }
    for binding in owner.registry.list_bindings():
        route = (binding.provider, binding.model.lower())
        if not binding.quota_pool_id:
            continue
        if binding.binding_role is BindingRole.CHECK and route in reviewer_routes:
            quota_routes.append((binding.provider, binding.quota_pool_id))
        elif route in worker_routes:
            quota_routes.append((binding.provider, binding.quota_pool_id))
    for provider, pool in dict.fromkeys(quota_routes):
        db.set_quota(
            provider=provider,
            pool=pool,
            remaining_percent=BENCHMARK_QUOTA_REMAINING_PCT,
        )


def _drive_real_batch(
    *,
    batch: RealBatch,
    db: Any,
    readiness: Any,
    jobs: MutableMapping[str, str],
    max_wait: float,
    worker_candidate: WorkerCandidate,
    policy: Any,
    submit: Callable[[BenchmarkTask], str] | None = None,
    pacer: SubmitPacer | None = None,
) -> BatchOutcome:
    """Drive one real batch with the engine's own scheduler tick.

    The batch's pacer is capped by the route's pool plan, so a vendor whose
    plan allows fewer concurrent jobs than the global ``--concurrency`` gets
    its own slot count (Claude one, Codex and OpenCode Go two by default).
    """
    from saberops.accept import AcceptEngine
    from saberops.capacity.loop import run_scheduler_loop
    from saberops.jobs.service import get_job

    batch_pacer = (
        None if pacer is None else batch_submit_pacer(pacer, worker_candidate, policy=policy)
    )

    def job_state(job_id: str) -> str | None:
        job = get_job(db, job_id)
        return None if job is None else job.state.value

    def accept_job(job_id: str) -> str:
        job = get_job(db, job_id)
        assert job is not None and job.current_run_id is not None
        accepted = AcceptEngine(db=db).accept_run(job.current_run_id)
        assert accepted.accepted, f"{batch.subscription}/{job_id}: {accepted}"
        return accepted.final_sha

    outcome = wait_for_batch(
        tasks=batch.tasks,
        jobs=jobs,
        tick=lambda: run_scheduler_loop(
            db, once=True, readiness_service=readiness
        ),
        job_state=job_state,
        accept_job=accept_job,
        notice_delivered=lambda job_id, state: _notice_delivered(
            db, job_id, state
        ),
        max_wait=max_wait,
        submit=submit,
        pacer=batch_pacer,
    )
    return replace(
        outcome,
        submit_concurrency=(
            batch_pacer.concurrency if batch_pacer is not None else None
        ),
    )


def _worker_submitter(
    *,
    db: Any,
    repos: Mapping[str, Path],
    subscription: str,
    submitted_at: MutableMapping[str, float],
    all_jobs: list[str],
) -> Callable[[BenchmarkTask], str]:
    """Build the submit callback the paced driver calls when a slot is free.

    The callback records ``submitted_at`` at the moment of submission, so a
    paced row's latency starts when its own task entered the engine, not when
    the batch started.
    """
    from saberops.jobs.models import JobRequirements
    from saberops.jobs.service import submit_job

    def submit_worker(task: BenchmarkTask) -> str:
        requirements = JobRequirements(
            model_family_exclude=real_family_requirements(subscription)
        )
        submitted_at[task.task_id] = time.time()
        submitted = submit_job(
            db,
            repo=repos[task.task_id],
            goal=task.goal,
            acceptance=task.acceptance,
            requirements=requirements,
        )
        all_jobs.append(submitted.job.job_id)
        return submitted.job.job_id

    return submit_worker


def run_real_benchmark(
    *,
    store: Path,
    repo_root: Path,
    models: dict[str, str],
    max_wait: float = 3600.0,
    tasks: Sequence[BenchmarkTask] | Mapping[str, Sequence[BenchmarkTask]] | None = None,
    submission_mode: SubmitMode = DEFAULT_SUBMIT_MODE,
    submit_concurrency: int = DEFAULT_SUBMIT_CONCURRENCY,
) -> BenchmarkReport:
    """Drive the ten-task catalogue once per real CLI in an isolated store.

    ``tasks`` selects a reduced catalogue for a bounded confirmation run (N11);
    a mapping selects a different subset per subscription (N13).  The default
    is the full catalogue.  Each batch uses one worker subscription
    and a different-vendor reviewer (D4).  The caller must have applied
    :func:`isolated_environment` to ``os.environ`` first.  This function starts
    vendor CLIs, so it is only reached after :func:`validate_real_mode` and the
    explicit acknowledgement flag.

    ``submission_mode`` defaults to ``paced``: a batch submits its next task
    only when fewer than its slot count submitted tasks are still non-terminal.
    That count is ``submit_concurrency`` capped by the batch route's pool plan
    (``min(submit_concurrency, plan.max_concurrent)``), so a one-slot vendor
    plan such as Claude's is never submitted into a queued position.  ``burst``
    submits the whole batch at once and exists only for the labelled stress
    section.
    """
    pacer = SubmitPacer(mode=submission_mode, concurrency=submit_concurrency)
    from saberops.cli import main as cli_main
    from saberops.control_plane.policy import load_control_plane_policy
    from saberops.db import Database
    from saberops.jobs.service import get_job
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.paths import owner_state_dir
    from saberops.preflight import collect_preflight

    pytest_error = gate_interpreter_pytest_error()
    if pytest_error is not None:
        raise RealModeRefused(pytest_error)

    started = time.time()
    store.mkdir(parents=True, exist_ok=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    # This loop ticks the scheduler in-process; a detached auto-start would race
    # it on the single isolated store (D3).
    os.environ["SABEROPS_DISABLE_AUTO_SCHEDULER"] = "1"
    db = Database()

    batches = real_batches(tasks)
    # The shipped entry point must be runnable from a fresh store: discovery,
    # routing, posture, quota and reviewer setup are part of the run (N1).
    prepare_real_store(
        db=db,
        models=models,
        reviewer_subscriptions=(
            batch.reviewer_subscription for batch in batches
        ),
    )
    # Read the plan view once, after preparation: paced submission needs each
    # batch route's pool concurrency, not only the global --concurrency.
    control_plane_policy = load_control_plane_policy()

    # Every batch starts from its own clean, identically seeded repositories so
    # a later task never inherits an earlier batch's accepted change.  The gate
    # is built from the running interpreter: a host with only ``python3`` must
    # not fail every task as a gate infrastructure error (report §12.5).
    batches = real_batches(tasks)
    repos_by_batch: dict[str, dict[str, Path]] = {}
    base_shas_by_batch: dict[str, dict[str, str]] = {}
    gate_commands_by_batch: dict[str, dict[str, str]] = {}
    for batch in batches:
        batch_root = repo_root / batch.subscription
        repos: dict[str, Path] = {}
        base_shas: dict[str, str] = {}
        gate_commands: dict[str, str] = {}
        for task in batch.tasks:
            repo, sha = create_toy_repository(
                batch_root, f"toy-{task.task_id}", task_id=task.task_id
            )
            task_gate = benchmark_gate_command(task.task_id)
            assert (
                cli_main(["project", "set-gate", "--repo", str(repo), task_gate]) == 0
            )
            repos[task.task_id] = repo
            base_shas[task.task_id] = sha
            gate_commands[task.task_id] = task_gate
        repos_by_batch[batch.subscription] = repos
        base_shas_by_batch[batch.subscription] = base_shas
        gate_commands_by_batch[batch.subscription] = gate_commands

    first = batches[0]
    findings = collect_preflight(
        repos_by_batch[first.subscription][first.tasks[0].task_id],
        db_path=Path(db.db_path),
    )
    # Real mode drives its own scheduler ticks, so a long-running scheduler is
    # not required; every other preflight failure is.
    failures = [
        finding
        for finding in findings
        if finding.severity == "FAIL" and not finding.message.startswith("scheduler:")
    ]
    print("\n".join(format_real_mode_preflight(findings)))
    if failures:
        raise RealModeRefused(
            "real-mode preflight is not green; see the findings above"
        )

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    result_by_key: dict[tuple[str, str], TaskResult] = {}
    all_jobs: list[str] = []
    max_submit_depth = 0
    batch_slot_limits: list[tuple[str, int]] = []
    seeded_reviews: list[tuple[RealBatch, BenchmarkTask]] = []
    with real_mode_presence(db):
        for batch in batches:
            reviewer_model = real_model_for(batch.reviewer_subscription, models)
            reviewer = f"{batch.reviewer_subscription}/{reviewer_model}"
            configure_real_reviewer(batch.reviewer_subscription, reviewer_model)
            worker_candidate = batch_worker_candidate(batch.subscription, models)

            repos = repos_by_batch[batch.subscription]
            worker_tasks = tuple(
                task for task in batch.tasks if task.expect == "accepted"
            )
            review_tasks = tuple(
                task for task in batch.tasks if task.expect == "review_blocked"
            )
            submitted_at: dict[str, float] = {}
            jobs: dict[str, str] = {}
            submit_worker = _worker_submitter(
                db=db,
                repos=repos,
                subscription=batch.subscription,
                submitted_at=submitted_at,
                all_jobs=all_jobs,
            )

            outcome = (
                _drive_real_batch(
                    batch=replace(batch, tasks=worker_tasks),
                    db=db,
                    readiness=readiness,
                    jobs=jobs,
                    max_wait=max_wait,
                    worker_candidate=worker_candidate,
                    policy=control_plane_policy,
                    submit=submit_worker,
                    pacer=pacer,
                )
                if worker_tasks
                else BatchOutcome(
                    terminal={},
                    noticed={},
                    accepted={},
                    submit_concurrency=batch_submit_pacer(
                        pacer, worker_candidate, policy=control_plane_policy
                    ).concurrency,
                )
            )
            max_submit_depth = max(max_submit_depth, outcome.max_submit_depth)
            batch_slot_limits.append(
                (
                    batch.subscription,
                    outcome.submit_concurrency
                    if outcome.submit_concurrency is not None
                    else pacer.concurrency,
                )
            )
            for task in worker_tasks:
                job_id = jobs[task.task_id]
                job = get_job(db, job_id)
                assert job is not None
                accepted_commit: str | None = None
                accepted_at: float | None = None
                owner_actions = 0
                if task.task_id in outcome.accepted:
                    accepted_commit, accepted_at = outcome.accepted[task.task_id]
                    owner_actions = 1
                attempts = (
                    db.get_attempts_for_run(job.current_run_id)
                    if job.current_run_id is not None
                    else []
                )
                subscription = (
                    f"{attempts[-1].provider}/{attempts[-1].model}"
                    if attempts
                    else batch.subscription
                )
                run = (
                    db.get_run(job.current_run_id)
                    if job.current_run_id is not None
                    else None
                )
                review_count = (
                    len(db.get_reviews_for_run(job.current_run_id))
                    if job.current_run_id is not None
                    else 0
                )
                result_by_key[(batch.subscription, task.task_id)] = TaskResult(
                    task_id=task.task_id,
                    kind=task.kind,
                    expect=task.expect,
                    final_state=job.state.value,
                    accepted_commit=accepted_commit,
                    submitted_at=submitted_at[task.task_id],
                    started_at=(
                        _iso_epoch(attempts[0].created_at) if attempts else None
                    ),
                    terminal_at=_iso_epoch(
                        run.completed_at if run is not None else None
                    ),
                    accepted_at=accepted_at,
                    notice_at=outcome.noticed.get(task.task_id),
                    owner_actions=owner_actions,
                    subscription=subscription,
                    detail=str(job.failure_reason or ""),
                    batch=batch.subscription,
                    reviewer=reviewer,
                    block_verdicts=(
                        count_block_verdicts(db, job.current_run_id)
                        if job.current_run_id is not None
                        else 0
                    ),
                    review_count=review_count,
                    candidate_produced=any(
                        attempt.commit_sha for attempt in attempts
                    ),
                )
            for task in review_tasks:
                # Deferred until every worker batch has finished: a review-only
                # RUNNING run must never be visible to a later scheduler tick.
                seeded_reviews.append((batch, task))

        for batch, task in seeded_reviews:
            reviewer_model = real_model_for(batch.reviewer_subscription, models)
            reviewer = f"{batch.reviewer_subscription}/{reviewer_model}"
            configure_real_reviewer(batch.reviewer_subscription, reviewer_model)
            result_by_key[(batch.subscription, task.task_id)] = (
                run_seeded_must_fail_row(
                    db=db,
                    readiness=readiness,
                    repo=repos_by_batch[batch.subscription][task.task_id],
                    base_sha=base_shas_by_batch[batch.subscription][task.task_id],
                    task=task,
                    batch=batch.subscription,
                    provider=(
                        "anthropic"
                        if batch.subscription == "claude"
                        else batch.subscription
                    ),
                    model=real_model_for(batch.subscription, models),
                    gate_command=gate_commands_by_batch[batch.subscription][
                        task.task_id
                    ],
                    reviewer=reviewer,
                )
            )

    results = tuple(
        result_by_key[(batch.subscription, task.task_id)]
        for batch in batches
        for task in batch.tasks
    )
    return BenchmarkReport(
        mode="real",
        results=results,
        started_at=started,
        finished_at=time.time(),
        usage=aggregate_subscription_usage(db, all_jobs),
        submission_mode=pacer.mode,
        submit_concurrency=pacer.concurrency,
        max_submit_depth=max_submit_depth,
        batch_slot_limits=tuple(batch_slot_limits),
    )
