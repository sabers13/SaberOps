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

import os
import time
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

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

    @property
    def submit_to_start(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, self.started_at - self.submitted_at)

    @property
    def time_to_candidate(self) -> float | None:
        """Submit -> the run reaching its terminal state (candidate ready)."""
        if self.terminal_at is None:
            return None
        return max(0.0, self.terminal_at - self.submitted_at)

    @property
    def time_to_accepted(self) -> float | None:
        if self.accepted_at is None:
            return None
        return max(0.0, self.accepted_at - self.submitted_at)

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
        return self.block_verdicts > 0


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

    @property
    def passed(self) -> bool:
        return all(result.passed for result in self.results) and all(
            drill.passed for drill in self.drills
        )

    @property
    def elapsed(self) -> float:
        return max(0.0, self.finished_at - self.started_at)

    def table(self) -> str:
        """Render the results table printed by both modes."""
        show_reviewer = any(result.reviewer for result in self.results)
        header = (
            f"{'task':<16} {'kind':<18} {'expect':<15} {'final':<10} "
            f"{'accepted commit':<15} {'sub->start':>10} {'to candidate':>12} "
            f"{'to accepted':>11} {'blocks':>6} "
            f"{'notice':>7} {'owner':>5} {'subscription':<38}"
            + (f" {'reviewer':<38}" if show_reviewer else "")
        )
        lines = [f"SaberOps phase 1 benchmark ({self.mode} mode)", "", header,
                 "-" * len(header)]
        for result in self.results:
            lines.append(
                f"{result.task_id:<16} {result.kind:<18} {result.expect:<15} "
                f"{result.final_state:<10} "
                f"{(result.accepted_commit or '-')[:12]:<15} "
                f"{_seconds(result.submit_to_start):>10} "
                f"{_seconds(result.time_to_candidate):>12} "
                f"{_seconds(result.time_to_accepted):>11} "
                f"{result.block_verdicts:>6} "
                f"{_seconds(result.notice_delay):>7} "
                f"{result.owner_actions:>5} {result.subscription:<38}"
                + (f" {result.reviewer:<38}" if show_reviewer else "")
            )
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


class RealModeRefused(RuntimeError):
    """Real mode was asked to start without its safety conditions."""


def owner_store_paths(home: Path | None = None) -> tuple[Path, Path]:
    """The owner's real SaberOps state and config directories."""
    root = Path(home) if home is not None else Path.home()
    return (root / ".local" / "state" / "saberops", root / ".config" / "saberops")


def validate_real_mode(
    *, acknowledged: bool, store: Path, home: Path | None = None
) -> Path:
    """Refuse real mode unless it is explicit and points at an isolated store.

    Returns the resolved store path.  This runs before any vendor CLI is
    started or any SaberOps state is written.
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


def wait_for_batch(
    *,
    tasks: Sequence[BenchmarkTask],
    jobs: Mapping[str, str],
    tick: Callable[[], object],
    job_state: Callable[[str], str | None],
    accept_job: Callable[[str], str],
    notice_delivered: Callable[[str, str], bool],
    max_wait: float,
    clock: Callable[[], float] = time.time,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    poll_interval: float = 0.5,
) -> BatchOutcome:
    """Drive one batch to terminal states, accepting each candidate on arrival.

    Acceptance waits for exactly one event -- this task's candidate becoming
    eligible -- instead of the slowest task in the batch.  That keeps the
    reported "to accepted" latency honest (N5): the measured time is the
    owner-equivalent action, not the batch tail.
    """
    terminal: dict[str, str] = {}
    noticed: dict[str, float] = {}
    accepted: dict[str, tuple[str, float]] = {}
    deadline = monotonic() + max_wait
    while monotonic() < deadline:
        tick()
        for task in tasks:
            job_id = jobs[task.task_id]
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
        if len(terminal) == len(jobs) and len(noticed) == len(jobs):
            return BatchOutcome(
                terminal=terminal, noticed=noticed, accepted=accepted
            )
        sleep(poll_interval)
    raise RealModeRefused(
        f"real benchmark batch did not finish within {max_wait:g}s; "
        f"terminal {len(terminal)}/{len(jobs)}"
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


@dataclass(frozen=True)
class RealBatch:
    """One real-mode batch: the whole catalogue on one worker CLI."""

    subscription: str
    reviewer_subscription: str
    tasks: tuple[BenchmarkTask, ...]


def real_batches() -> tuple[RealBatch, ...]:
    """The three real-mode batches, each with a different-vendor reviewer."""
    return tuple(
        RealBatch(
            subscription=subscription,
            reviewer_subscription=REVIEWER_SUBSCRIPTION[subscription],
            tasks=TASKS,
        )
        for subscription in REAL_SUBSCRIPTION_ORDER
    )


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
    subscription run cannot silently fall back to API billing.

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
    for key in (
        "OPENAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "CODEX_API_KEY",
        "CODEX_ACCESS_TOKEN",
        "CLAUDE_API_KEY",
        "OPENCODE_API_KEY",
    ):
        env.pop(key, None)
    return env


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
    jobs: Mapping[str, str],
    max_wait: float,
) -> BatchOutcome:
    """Drive one real batch with the engine's own scheduler tick."""
    from saberops.accept import AcceptEngine
    from saberops.capacity.loop import run_scheduler_loop
    from saberops.jobs.service import get_job

    def job_state(job_id: str) -> str | None:
        job = get_job(db, job_id)
        return None if job is None else job.state.value

    def accept_job(job_id: str) -> str:
        job = get_job(db, job_id)
        assert job is not None and job.current_run_id is not None
        accepted = AcceptEngine(db=db).accept_run(job.current_run_id)
        assert accepted.accepted, f"{batch.subscription}/{job_id}: {accepted}"
        return accepted.final_sha

    return wait_for_batch(
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
    )


def run_real_benchmark(
    *,
    store: Path,
    repo_root: Path,
    models: dict[str, str],
    max_wait: float = 3600.0,
) -> BenchmarkReport:
    """Drive the ten-task catalogue once per real CLI in an isolated store.

    Each batch uses one worker subscription and a different-vendor reviewer
    (D4).  The caller must have applied :func:`isolated_environment` to
    ``os.environ`` first.  This function starts vendor CLIs, so it is only
    reached after :func:`validate_real_mode` and the explicit acknowledgement
    flag.
    """
    from saberops.cli import main as cli_main
    from saberops.db import Database
    from saberops.jobs.models import JobRequirements
    from saberops.jobs.service import get_job, submit_job
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.models import ReviewPolicyMode
    from saberops.paths import owner_state_dir
    from saberops.preflight import collect_preflight
    from saberops.project_settings import set_project_review_policy

    started = time.time()
    store.mkdir(parents=True, exist_ok=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    # This loop ticks the scheduler in-process; a detached auto-start would race
    # it on the single isolated store (D3).
    os.environ["SABEROPS_DISABLE_AUTO_SCHEDULER"] = "1"
    db = Database()

    batches = real_batches()
    # The shipped entry point must be runnable from a fresh store: discovery,
    # routing, posture, quota and reviewer setup are part of the run (N1).
    prepare_real_store(
        db=db,
        models=models,
        reviewer_subscriptions=(
            batch.reviewer_subscription for batch in batches
        ),
    )

    # Every batch starts from its own clean, identically seeded repositories so
    # a later task never inherits an earlier batch's accepted change.
    gate_command = "python -B -c 'import app'"
    batches = real_batches()
    repos_by_batch: dict[str, dict[str, Path]] = {}
    for batch in batches:
        batch_root = repo_root / batch.subscription
        repos: dict[str, Path] = {}
        for task in batch.tasks:
            repo, _sha = create_toy_repository(
                batch_root, f"toy-{task.task_id}", task_id=task.task_id
            )
            set_project_review_policy(repo, ReviewPolicyMode.ALWAYS_REQUIRED)
            assert (
                cli_main(["project", "set-gate", "--repo", str(repo), gate_command]) == 0
            )
            repos[task.task_id] = repo
        repos_by_batch[batch.subscription] = repos

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
    lines = ["real-mode preflight:"]
    for finding in findings:
        lines.append(f"  [{finding.severity}] {finding.message}")
        if finding.remedy:
            lines.append(f"        fix: {finding.remedy}")
    print("\n".join(lines))
    if failures:
        raise RealModeRefused(
            "real-mode preflight is not green; see the findings above"
        )

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    results: list[TaskResult] = []
    all_jobs: list[str] = []
    with real_mode_presence(db):
        for batch in batches:
            reviewer_model = real_model_for(batch.reviewer_subscription, models)
            reviewer = f"{batch.reviewer_subscription}/{reviewer_model}"
            configure_real_reviewer(batch.reviewer_subscription, reviewer_model)

            repos = repos_by_batch[batch.subscription]
            submitted_at: dict[str, float] = {}
            jobs: dict[str, str] = {}
            for task in batch.tasks:
                requirements = JobRequirements(
                    model_family_exclude=real_family_requirements(batch.subscription)
                )
                submitted_at[task.task_id] = time.time()
                submitted = submit_job(
                    db,
                    repo=repos[task.task_id],
                    goal=task.goal,
                    acceptance=task.acceptance,
                    requirements=requirements,
                )
                jobs[task.task_id] = submitted.job.job_id
                all_jobs.append(submitted.job.job_id)

            outcome = _drive_real_batch(
                batch=batch,
                db=db,
                readiness=readiness,
                jobs=jobs,
                max_wait=max_wait,
            )
            for task in batch.tasks:
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
                results.append(
                    TaskResult(
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
                    )
                )

    return BenchmarkReport(
        mode="real",
        results=tuple(results),
        started_at=started,
        finished_at=time.time(),
        usage=aggregate_subscription_usage(db, all_jobs),
    )
