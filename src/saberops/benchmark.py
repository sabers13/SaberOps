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
#: the current Haiku model is named explicitly.
DEFAULT_CLAUDE_MODEL = "claude-haiku-5-5"
DEFAULT_CODEX_MODEL = "gpt-6-luna"

#: The explicit real-mode acknowledgement flag.  Real mode refuses without it.
REAL_MODE_FLAG = "--yes-real-cli"


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
        goal="Make greet() reject an empty name with a clear ValueError.",
        acceptance="An empty name raises ValueError with a readable message.",
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
        goal="Add an unvalidated fast path to greet() that skips the input check.",
        acceptance="The fast path keeps every existing validation in place.",
        expect="review_blocked",
        family="codex",
    ),
    BenchmarkTask(
        task_id="review-block-b",
        kind="must fail review",
        goal="Remove the input check from greet() to simplify the function.",
        acceptance="Input validation still rejects an empty name.",
        expect="review_blocked",
        family="opencode",
    ),
)

TASKS_BY_ID: dict[str, BenchmarkTask] = {task.task_id: task for task in TASKS}


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

    @property
    def submit_to_start(self) -> float | None:
        if self.started_at is None:
            return None
        return max(0.0, self.started_at - self.submitted_at)

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
        return self.final_state == "FAILED"


@dataclass(frozen=True)
class DrillResult:
    """Outcome of one injected-failure drill."""

    name: str
    passed: bool
    seconds: float
    final_state: str
    reason: str
    notices: int
    detail: str = ""


@dataclass(frozen=True)
class BenchmarkReport:
    """The ten-task table plus any failure drills that ran with it."""

    mode: str
    results: tuple[TaskResult, ...]
    drills: tuple[DrillResult, ...] = ()
    started_at: float = 0.0
    finished_at: float = 0.0

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
        header = (
            f"{'task':<16} {'kind':<18} {'expect':<15} {'final':<10} "
            f"{'accepted commit':<15} {'sub->start':>10} {'to accepted':>11} "
            f"{'notice':>7} {'owner':>5} {'subscription':<38}"
        )
        lines = [f"SaberOps phase 1 benchmark ({self.mode} mode)", "", header,
                 "-" * len(header)]
        for result in self.results:
            lines.append(
                f"{result.task_id:<16} {result.kind:<18} {result.expect:<15} "
                f"{result.final_state:<10} "
                f"{(result.accepted_commit or '-')[:12]:<15} "
                f"{_seconds(result.submit_to_start):>10} "
                f"{_seconds(result.time_to_accepted):>11} "
                f"{_seconds(result.notice_delay):>7} "
                f"{result.owner_actions:>5} {result.subscription:<38}"
            )
        if self.drills:
            lines.extend(["", "Failure drills", ""])
            drill_header = (
                f"{'drill':<20} {'result':<7} {'seconds':>8} {'final':<10} "
                f"{'notices':>8}  reason"
            )
            lines.extend([drill_header, "-" * len(drill_header)])
            for drill in self.drills:
                lines.append(
                    f"{drill.name:<20} {'pass' if drill.passed else 'FAIL':<7} "
                    f"{drill.seconds:>8.1f} {drill.final_state:<10} "
                    f"{drill.notices:>8}  {drill.reason}"
                )
        lines.extend(
            ["", f"total {self.elapsed:.1f}s; "
             f"{'PASS' if self.passed else 'FAIL'}"]
        )
        return "\n".join(lines)


def _seconds(value: float | None) -> str:
    return "-" if value is None else f"{value:.1f}s"


def _iso_epoch(value: str | None) -> float | None:
    if not value:
        return None
    return datetime.fromisoformat(value).timestamp()


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


#: Real mode spreads the ten tasks over the three subscriptions the owner named.
REAL_SUBSCRIPTION_ORDER: tuple[str, ...] = ("codex", "opencode", "claude")


def real_subscription(index: int) -> str:
    return REAL_SUBSCRIPTION_ORDER[index % len(REAL_SUBSCRIPTION_ORDER)]


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
    """
    env = dict(os.environ if base is None else base)
    for variable, subdir in (
        ("XDG_STATE_HOME", "state"),
        ("XDG_CONFIG_HOME", "config"),
        ("XDG_CACHE_HOME", "cache"),
        ("XDG_DATA_HOME", "data"),
    ):
        env[variable] = str(store / subdir)
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


def create_toy_repository(root: Path, name: str) -> tuple[Path, str]:
    """Create the small clean toy repository a real-mode benchmark task uses."""
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
    (repo / "README.md").write_text("# Toy Repository\n", encoding="utf-8")
    (repo / "app.py").write_text(
        "def greet() -> str:\n    return 'hello'\n", encoding="utf-8"
    )
    (repo / "test_app.py").write_text(
        "from app import greet\n\n\ndef test_greet() -> None:\n"
        "    assert greet() == 'hello'\n",
        encoding="utf-8",
    )
    git("add", ".")
    git("commit", "-m", "initial commit")
    return repo, git("rev-parse", "HEAD")


def _notice_delivered(db: Any, job_id: str, state: str) -> bool:
    import sqlite3

    path = Path(db.db_path)
    ledger = path.with_name(path.name + ".notifications.sqlite3")
    if not ledger.exists():
        return False
    try:
        with sqlite3.connect(ledger, timeout=5) as conn:
            row = conn.execute(
                "SELECT 1 FROM sent WHERE event_key = ?", (f"job:{job_id}:{state}",)
            ).fetchone()
    except sqlite3.Error:
        return False
    return row is not None


def run_real_benchmark(
    *,
    store: Path,
    repo_root: Path,
    models: dict[str, str],
    max_wait: float = 3600.0,
) -> BenchmarkReport:
    """Drive the ten tasks through real CLIs in an isolated store.

    The caller must have applied :func:`isolated_environment` to ``os.environ``
    first.  This function starts vendor CLIs, so it is only reached after
    :func:`validate_real_mode` and the explicit acknowledgement flag.
    """
    from saberops.accept import AcceptEngine
    from saberops.capacity.loop import run_scheduler_loop
    from saberops.cli import main as cli_main
    from saberops.db import Database
    from saberops.jobs.models import JobRequirements
    from saberops.jobs.service import get_job, submit_job
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.models import ReviewPolicyMode
    from saberops.paths import owner_state_dir
    from saberops.preflight import collect_preflight
    from saberops.project_settings import set_project_review_policy
    from saberops.routing_config import (
        add_candidate_to_chain,
        init_user_config,
        load_effective_routing_payload,
        save_user_routing,
    )

    started = time.time()
    store.mkdir(parents=True, exist_ok=True)
    repo_root.mkdir(parents=True, exist_ok=True)
    db = Database()

    candidates = (
        f"codex/{models['codex']}",
        f"opencode/{models['opencode']}",
        f"anthropic/{models['claude']}",
    )
    init_user_config()
    payload = load_effective_routing_payload()
    for candidate in candidates:
        payload = add_candidate_to_chain(payload, role="WORKER", candidate_id=candidate)
    save_user_routing(payload)

    repos: dict[str, Path] = {}
    gate_command = "python -c 'import app'"
    for task in TASKS:
        repo, _sha = create_toy_repository(repo_root, f"toy-{task.task_id}")
        set_project_review_policy(repo, ReviewPolicyMode.ALWAYS_REQUIRED)
        assert (
            cli_main(["project", "set-gate", "--repo", str(repo), gate_command]) == 0
        )
        repos[task.task_id] = repo

    findings = collect_preflight(repos[TASKS[0].task_id], db_path=Path(db.db_path))
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
    submitted_at: dict[str, float] = {}
    jobs: dict[str, str] = {}
    subscriptions: dict[str, str] = {}
    for index, task in enumerate(TASKS):
        subscription = real_subscription(index)
        subscriptions[task.task_id] = subscription
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
        jobs[task.task_id] = submitted.job.job_id

    terminal: dict[str, str] = {}
    noticed: dict[str, float] = {}
    deadline = time.monotonic() + max_wait
    while time.monotonic() < deadline:
        run_scheduler_loop(db, once=True, readiness_service=readiness)
        for task_id, job_id in jobs.items():
            job = get_job(db, job_id)
            if job is None:
                continue
            if job.state.value in {"SUCCEEDED", "FAILED", "CANCELLED"}:
                terminal[task_id] = job.state.value
                if task_id not in noticed and _notice_delivered(
                    db, job_id, job.state.value
                ):
                    noticed[task_id] = time.time()
        if len(terminal) == len(jobs) and len(noticed) == len(jobs):
            break
        time.sleep(0.5)
    else:  # pragma: no cover - real runs are driven by hand
        raise RealModeRefused(
            f"real benchmark did not finish within {max_wait:g}s; "
            f"terminal {len(terminal)}/{len(jobs)}"
        )

    results: list[TaskResult] = []
    for task in TASKS:
        job_id = jobs[task.task_id]
        job = get_job(db, job_id)
        assert job is not None
        accepted_commit: str | None = None
        accepted_at: float | None = None
        owner_actions = 0
        if task.expect == "accepted" and job.current_run_id is not None:
            accepted = AcceptEngine(db=db).accept_run(job.current_run_id)
            assert accepted.accepted, f"{task.task_id}: {accepted}"
            accepted_commit = accepted.final_sha
            accepted_at = time.time()
            owner_actions = 1
        attempts = (
            db.get_attempts_for_run(job.current_run_id)
            if job.current_run_id is not None
            else []
        )
        subscription = (
            f"{attempts[-1].provider}/{attempts[-1].model}"
            if attempts
            else subscriptions[task.task_id]
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
                terminal_at=_iso_epoch(run.completed_at if run is not None else None),
                accepted_at=accepted_at,
                notice_at=noticed.get(task.task_id),
                owner_actions=owner_actions,
                subscription=subscription,
                detail=str(job.failure_reason or ""),
            )
        )
    return BenchmarkReport(
        mode="real",
        results=tuple(results),
        started_at=started,
        finished_at=time.time(),
    )
