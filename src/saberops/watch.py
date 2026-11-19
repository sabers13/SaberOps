"""Read-only tmux watch view: one window per running job (Phase 1 Wave 3 F23).

``orch watch`` gives the owner the tmux habit back without reopening the
tmux handoff ADR-0001 cut.  Workers stay headless (``claude -p``,
``codex exec``, ``opencode run``); there is no worker pane to attach to.
The view only *observes* durable records:

* one window per RUNNING job, running the same read-only tail F3 exposes
  (``saberops.mcp.tail.tail_run`` over the run's event log and terminal
  transcript), plus a status line with route, subscription, attempt and
  elapsed time;
* one summary window listing every job and the owner pause state.

Nothing here writes to the engine or talks to a worker.  No keystroke in a
watch window can reach a worker because no window has a worker input path:
each window runs ``orch watch --follow``, which opens the store read-only.
Steering stays in the F13 note channel (``orch job note``).

Lifecycle rules
---------------

* A window is created when a job first becomes RUNNING.
* When the job leaves RUNNING the window is renamed with a suffix
  (``-ok``, ``-failed``, ``-cancelled``, ``-queued``) and kept; the job
  window prints the final state and the exact next owner action and then
  idles until the owner closes it.
* The summary window reconciles windows on every tick, so jobs that start
  after the session opened get a window without running ``orch watch``
  again.
* The tmux session carries the ``@saberops_watch`` marker.  A session with
  our default name but no marker belongs to someone else: the watcher
  picks a free suffixed name instead, and an explicitly requested foreign
  name is refused rather than touched.
* Without a usable tmux binary the command falls back to ``orch monitor``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from saberops.db import Database, get_default_db_path
from saberops.jobs.models import TERMINAL_JOB_STATES, Job, JobState
from saberops.jobs.service import get_job, job_status_view, list_jobs, subscription_for_provider
from saberops.mcp.tail import MAX_TAIL_BYTES, TailCursor, parse_cursor, tail_run

__all__ = [
    "DEFAULT_SESSION_BASE",
    "SESSION_OPTION",
    "WINDOW_OPTION",
    "Tmux",
    "TmuxError",
    "WatchError",
    "WindowAction",
    "base_window_label",
    "handle_watch",
    "job_status_line",
    "next_owner_action",
    "plan_window_actions",
    "render_summary",
    "resolve_session_name",
    "select_jobs",
]

#: tmux session option proving the session was created by ``orch watch``.
SESSION_OPTION = "@saberops_watch"

#: tmux window option carrying the job id a window follows.
WINDOW_OPTION = "@saberops_job"

#: Name of the summary window (also the session's first window).
SUMMARY_WINDOW = "summary"

DEFAULT_SESSION_BASE = "saberops-watch"

#: Marker appended to a window name once its job leaves RUNNING.
STATE_WINDOW_SUFFIX: dict[JobState, str] = {
    JobState.QUEUED: "queued",
    JobState.SUCCEEDED: "ok",
    JobState.FAILED: "failed",
    JobState.CANCELLED: "cancelled",
}

_MAX_SUFFIX = 50
_LABEL_LIMIT = 40
_ANSI_CLEAR = "\x1b[H\x1b[2J"
_SESSION_NAME = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

#: Reprint the (unchanged) status line this often so elapsed time stays fresh
#: without burying worker output under a per-tick counter.
STATUS_REFRESH_SECONDS = 30.0


class WatchError(RuntimeError):
    """A watch request that cannot be honoured without touching something else."""


class TmuxError(RuntimeError):
    """The tmux binary refused a command."""


def _sanitize(value: str, *, fallback: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_-]+", "-", value).strip("-")
    return (cleaned or fallback)[:_LABEL_LIMIT]


def _slug(value: str, *, limit: int = 16) -> str:
    words = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()
    return words[:limit].strip("-")


def window_db_path(explicit: str | None) -> Path:
    """Resolve the store the view reads (default: the owner-wide store)."""
    return Path(explicit).expanduser().resolve() if explicit else get_default_db_path().resolve()


def open_watch_db(path: Path) -> Database | None:
    """Open the store strictly read-only; ``None`` when it does not exist."""
    if not path.is_file():
        return None
    return Database(path, read_only=True)


def select_jobs(
    db: Any,
    *,
    repo: Path | None = None,
    job_id: str | None = None,
    limit: int = 200,
) -> list[Job]:
    """Return the jobs this view covers, optionally narrowed to one repo/job."""
    if job_id is not None:
        job = get_job(db, job_id)
        if job is None:
            raise WatchError(f"unknown job: {job_id}")
        jobs = [job]
    else:
        jobs = list_jobs(db, limit=limit)
    if repo is not None:
        wanted = Path(repo).resolve()
        jobs = [job for job in jobs if Path(job.repo).resolve() == wanted]
    return sorted(jobs, key=lambda job: (job.created_at, job.job_id))


def base_window_label(job: Job) -> str:
    """Window label from the job and its lane (the goal slug when no lane id exists)."""
    lane = getattr(job, "lane_id", None)
    label = _slug(str(lane)) if lane else _slug(job.goal)
    prefix = _sanitize(f"job-{job.job_id[-8:]}", fallback=f"job-{job.job_id[-8:]}")
    return _sanitize(f"{prefix}-{label}" if label else prefix, fallback=prefix)


def marked_window_label(job: Job) -> str:
    """Final window label for a job that is no longer RUNNING."""
    suffix = STATE_WINDOW_SUFFIX.get(job.state, job.state.value.lower())
    return _sanitize(f"{base_window_label(job)}-{suffix}", fallback=base_window_label(job))


@dataclass(frozen=True)
class WindowAction:
    """One reconciliation step for the tmux session."""

    kind: Literal["create", "rename"]
    job_id: str
    target: str
    label: str


def plan_window_actions(
    jobs: list[Job], existing: list[tuple[str, str | None]]
) -> list[WindowAction]:
    """Diff the durable job states against the windows that already exist.

    Pure: the caller applies the actions.  A window is created only for a
    RUNNING job and is renamed, never closed, when the job leaves RUNNING.
    """
    by_job = {job_id: name for name, job_id in existing if job_id}
    names = {name for name, _ in existing}
    actions: list[WindowAction] = []
    for job in jobs:
        current = by_job.get(job.job_id)
        if job.state == JobState.RUNNING:
            if current is not None:
                continue
            label = base_window_label(job)
            if label in names:
                index = 2
                while f"{label}-{index}" in names:
                    index += 1
                label = f"{label}-{index}"
            actions.append(WindowAction("create", job.job_id, label, label))
            names.add(label)
            continue
        if current is None:
            continue
        label = marked_window_label(job)
        if current != label:
            actions.append(WindowAction("rename", job.job_id, current, label))
            names.discard(current)
            names.add(label)
    return actions


class Tmux:
    """Thin, allowlisted tmux client.

    Every method here is observation or window layout.  There is deliberately
    no ``send-keys``, ``pipe-pane`` or ``respawn`` method: a watch session can
    never deliver input to a process.
    """

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary or os.environ.get("ORCH_TMUX_BIN") or "tmux"
        self._path = shutil.which(self.binary)

    def available(self) -> bool:
        return self._path is not None

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        if self._path is None:
            raise TmuxError(f"tmux binary not found: {self.binary}")
        proc = subprocess.run(
            [self._path, *args],
            capture_output=True,
            text=True,
            check=False,
        )
        if check and proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise TmuxError(f"tmux {' '.join(args)} failed: {detail}")
        return proc

    def has_session(self, name: str) -> bool:
        return self._run("has-session", "-t", name, check=False).returncode == 0

    def session_is_ours(self, name: str) -> bool:
        proc = self._run("show-options", "-t", name, "-v", SESSION_OPTION, check=False)
        return proc.returncode == 0 and proc.stdout.strip() == "1"

    def list_windows(self, session: str) -> list[tuple[str, str | None]]:
        fmt = f"#{{window_name}}\t#{{{WINDOW_OPTION}}}"
        proc = self._run("list-windows", "-t", session, "-F", fmt)
        windows: list[tuple[str, str | None]] = []
        for line in proc.stdout.splitlines():
            if not line.strip():
                continue
            name, _, job_id = line.partition("\t")
            windows.append((name.strip(), job_id.strip() or None))
        return windows

    def new_session(self, name: str, argv: list[str]) -> None:
        self._run(
            "new-session",
            "-d",
            "-s",
            name,
            "-n",
            SUMMARY_WINDOW,
            "--",
            *argv,
        )

    def mark_session_ours(self, name: str) -> None:
        self._run("set-option", "-t", name, SESSION_OPTION, "1")

    def new_window(self, session: str, label: str, argv: list[str], job_id: str) -> None:
        self._run("new-window", "-d", "-t", session, "-n", label, "--", *argv)
        self._run(
            "set-option",
            "-w",
            "-t",
            f"{session}:{label}",
            WINDOW_OPTION,
            job_id,
        )

    def rename_window(self, session: str, current: str, label: str) -> None:
        self._run("rename-window", "-t", f"{session}:{current}", label)

    def attach(self, session: str) -> int:
        if os.environ.get("TMUX"):
            proc = self._run("switch-client", "-t", session, check=False)
        else:
            proc = self._run("attach-session", "-t", session, check=False)
        return proc.returncode

    def kill_session(self, session: str) -> None:
        """Remove a session this process just created and failed to mark."""
        self._run("kill-session", "-t", session, check=False)


def resolve_session_name(
    tmux: Tmux, desired: str, *, explicit: bool
) -> str:
    """Pick the session name, never reusing one that is not ours.

    An explicitly requested name that exists without our marker is refused:
    attaching to it or renaming its windows would touch another owner's
    session.  A default name is made unique by suffixing.
    """
    wanted = _sanitize(desired, fallback=DEFAULT_SESSION_BASE)
    if explicit and not _SESSION_NAME.match(desired):
        raise WatchError(
            f"invalid tmux session name {desired!r}; use letters, digits, '-' and '_'"
        )
    if not tmux.has_session(wanted):
        return wanted
    if tmux.session_is_ours(wanted):
        return wanted
    if explicit:
        raise WatchError(
            f"tmux session {wanted!r} already exists and was not created by "
            "orch watch; pass a different --session name"
        )
    for index in range(2, _MAX_SUFFIX + 2):
        candidate = f"{wanted}-{index}"
        if not tmux.has_session(candidate):
            return candidate
        if tmux.session_is_ours(candidate):
            return candidate
    raise WatchError(f"no free tmux session name near {wanted!r}")


def default_session_name(repo: Path | None, db_path: Path) -> str:
    """A stable, recognisable default session name."""
    seed = str(repo.resolve()) if repo is not None else str(db_path)
    slug = _slug(Path(seed).name, limit=24) or "watch"
    digest = hashlib.sha1(seed.encode("utf-8")).hexdigest()[:6]
    return _sanitize(f"{DEFAULT_SESSION_BASE}-{slug}-{digest}", fallback=DEFAULT_SESSION_BASE)


def _parse_instant(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        value = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value


def format_elapsed(raw: str | None, *, now: datetime | None = None) -> str:
    started = _parse_instant(raw)
    if started is None:
        return "unknown"
    moment = now or datetime.now(UTC)
    seconds = max(0, int((moment - started).total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m{seconds:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


def job_status_line(db: Any, job: Job, *, now: datetime | None = None) -> str:
    """One line: route, subscription, attempt, elapsed, activity."""
    route, subscription, attempt, activity = _status_fields(db, job)
    return (
        f"State   {job.state.value}  activity={activity}  "
        f"route={route}  subscription={subscription}  "
        f"attempt={attempt}  elapsed={format_elapsed(job.created_at, now=now)}"
    )


def _status_fields(db: Any, job: Job) -> tuple[str, str, int, str]:
    view = job_status_view(db, job)
    build = (view.get("roles") or {}).get("build") or {}
    provider = str(build.get("provider") or "UNKNOWN")
    model = str(build.get("model") or "UNKNOWN")
    route = "unassigned" if provider == "UNKNOWN" else f"{provider}/{model}"
    subscription = "unknown" if provider == "UNKNOWN" else subscription_for_provider(provider)
    attempts = (
        db.get_attempts_for_run(job.current_run_id) if job.current_run_id else []
    )
    attempt = attempts[-1].attempt_number if attempts else 0
    activity = str(view.get("activity") or job.state.value.lower())
    return route, subscription, attempt, activity


def job_status_key(db: Any, job: Job) -> tuple[str, str, str, str, int]:
    """The parts of the status line that make a redraw worthwhile.

    Elapsed time is not one of them: a window redrawn every tick with only a
    changing second counter would bury the worker's output.
    """
    route, subscription, attempt, activity = _status_fields(db, job)
    return (job.state.value, activity, route, subscription, attempt)


def next_owner_action(db: Any, job: Job) -> str:
    """The exact command the owner needs for this job's current state."""
    if job.state == JobState.RUNNING:
        return f"none; steer with `orch job note {job.job_id} --text ...`"
    if job.state == JobState.QUEUED:
        reason = job.waiting_reason.value if job.waiting_reason else "waiting"
        return f"none; queued ({reason})"
    run_id = job.current_run_id or (job.run_ids[-1] if job.run_ids else None)
    if job.state == JobState.SUCCEEDED:
        if run_id is None:
            return f"`orch job get {job.job_id}` (no run id recorded)"
        from saberops.accept import AcceptEngine

        eligible = AcceptEngine(db).check_accept_eligibility(run_id).eligible
        if eligible:
            return f"`orch accept {run_id}`"
        return f"`orch status {run_id}` (candidate not ready; inspect the refusal)"
    if job.state == JobState.FAILED:
        if job.failure_reason == "NO_ROUTE_FOR_REQUIREMENTS":
            return (
                "`orch routing add build <provider/model>` then resubmit "
                "(no build route satisfied the requirements)"
            )
        if run_id is None:
            return f"`orch job get {job.job_id}` (no run id recorded)"
        return f"`orch status {run_id}`, then `orch retry {run_id}` or `orch continue {run_id}`"
    if job.state == JobState.CANCELLED:
        return "`orch job submit ...` to run it again (cancelled jobs stay cancelled)"
    return "none"


def pause_state(db: Any) -> str:
    """Owner pause state, read from a durable pause record when one exists.

    F17 (closing the planning CLI pauses all work) has not landed yet; this
    reads its record through an optional store accessor and otherwise says so
    plainly instead of guessing from transient state.
    """
    getter = getattr(db, "get_owner_pause", None)
    if callable(getter):
        try:
            record = getter()
        except Exception:
            record = None
        if record is not None:
            reason = getattr(record, "reason", None) or "paused by the owner"
            return f"paused ({reason})"
    return "not paused (no pause record)"


def _mcp_access_state() -> str:
    from saberops.mcp.switch import mcp_enabled

    return "on" if mcp_enabled() else "off (`orch mcp on` to restore)"


def render_summary(
    db: Any,
    jobs: list[Job],
    *,
    session: str,
    repo: Path | None = None,
    now: datetime | None = None,
) -> str:
    """The summary window/snapshot text."""
    lines: list[str] = [
        f"SaberOps watch  session={session}  (read-only; keystrokes never reach a worker)",
        f"Store   {getattr(db, 'db_path', 'unknown')}",
        f"Filter  repo={repo if repo is not None else 'all jobs'}",
        f"Pause    {pause_state(db)}",
        f"Agent access (MCP)  {_mcp_access_state()}",
        "",
    ]
    if not jobs:
        lines.append("(no jobs in this store)")
    for job in jobs:
        route, subscription, attempt, activity = _status_fields(db, job)
        goal = job.goal.splitlines()[0] if job.goal else ""
        lines.append(
            f"{job.job_id}  {job.state.value:<10} "
            f"{format_elapsed(job.created_at, now=now):>7}  "
            f"attempt={attempt}  activity={activity}"
        )
        lines.append(f"    {route} ({subscription})  {goal}")
    lines.append("")
    lines.append(
        "Windows follow RUNNING jobs; finished jobs keep a marked window."
    )
    return "\n".join(lines)


def render_job_header(job: Job) -> str:
    return "\n".join(
        [
            f"SaberOps watch — job {job.job_id} "
            "(read-only; keystrokes never reach a worker)",
            f"Goal    {job.goal.splitlines()[0] if job.goal else ''}",
            f"Repo    {job.repo} @ {job.base_sha[:12]}",
        ]
    )


def _format_event(event: dict[str, Any]) -> str:
    payload = event.get("payload")
    if isinstance(payload, dict) and payload:
        detail = json.dumps(payload, sort_keys=True, default=str)
        if len(detail) > 200:
            detail = detail[:199] + "…"
        detail = f"  {detail}"
    else:
        detail = ""
    attempt_id = event.get("attempt_id")
    attempt = f" [{attempt_id}]" if attempt_id else ""
    return f"  · {event.get('type', 'event')}{attempt}{detail}"


def render_job_final(db: Any, job: Job) -> str:
    return "\n".join(
        [
            "",
            "─" * 72,
            f"Final: {job.state.value}"
            + (f" ({job.failure_reason})" if job.failure_reason else ""),
            f"Next owner action: {next_owner_action(db, job)}",
            "Window kept for review; it sends nothing to any worker.",
            "─" * 72,
            "",
        ]
    )


def _tail_once(db: Any, job: Job, cursor: TailCursor, max_bytes: int) -> tuple[str, TailCursor]:
    """Read the next tail chunk for one job without writing anything."""
    run_id = job.current_run_id or (job.run_ids[-1] if job.run_ids else None)
    if run_id is None:
        return ("(no run yet — waiting for dispatch)\n", cursor)
    chunk = tail_run(db, run_id=run_id, cursor=cursor, max_bytes=max_bytes)
    parts: list[str] = []
    for event in chunk["events"]:
        parts.append(_format_event(event))
    transcript = chunk["transcript"]
    if isinstance(transcript, dict) and transcript.get("text"):
        parts.append(str(transcript["text"]))
    if not parts:
        return ("", parse_cursor(chunk["next_sequence"]))
    text = "\n".join(parts)
    if not text.endswith("\n"):
        text += "\n"
    return (text, parse_cursor(chunk["next_sequence"]))


def _follow_argv(
    job_id: str,
    *,
    session: str,
    db_path: Path,
    interval: float,
) -> list[str]:
    """The read-only command a job window runs."""
    return [
        sys.executable or "python",
        "-m",
        "saberops.cli",
        "watch",
        "--follow",
        "--job",
        job_id,
        "--session",
        session,
        "--db",
        str(db_path),
        "--interval",
        str(max(0.5, interval)),
    ]


def _summary_argv(
    *,
    session: str,
    db_path: Path,
    repo: Path | None,
    job_id: str | None,
    interval: float,
) -> list[str]:
    argv = [
        sys.executable or "python",
        "-m",
        "saberops.cli",
        "watch",
        "--summary",
        "--session",
        session,
        "--db",
        str(db_path),
        "--interval",
        str(max(0.5, interval)),
    ]
    if repo is not None:
        argv.extend(["--repo", str(repo)])
    if job_id is not None:
        argv.extend(["--job", job_id])
    return argv


def apply_window_actions(
    tmux: Tmux,
    session: str,
    actions: list[WindowAction],
    *,
    db_path: Path,
    interval: float,
) -> None:
    """Apply one reconciliation plan; never closes a window."""
    for action in actions:
        if action.kind == "create":
            tmux.new_window(
                session,
                action.label,
                _follow_argv(
                    action.job_id,
                    session=session,
                    db_path=db_path,
                    interval=interval,
                ),
                action.job_id,
            )
        else:
            tmux.rename_window(session, action.target, action.label)


def reconcile_session(
    tmux: Tmux,
    session: str,
    db: Any,
    *,
    repo: Path | None,
    job_id: str | None,
    db_path: Path,
    interval: float,
) -> list[Job]:
    """Create/mark windows for the current durable job states."""
    jobs = select_jobs(db, repo=repo, job_id=job_id)
    existing = tmux.list_windows(session)
    actions = plan_window_actions(jobs, existing)
    apply_window_actions(
        tmux,
        session,
        actions,
        db_path=db_path,
        interval=interval,
    )
    return jobs


def _open_or_none(path: Path) -> Database | None:
    try:
        return open_watch_db(path)
    except Exception as exc:  # unreadable store: report, never crash the window
        print(f"watch: cannot read {path}: {exc}", file=sys.stderr, flush=True)
        return None


def run_snapshot(
    *,
    db_path: Path,
    repo: Path | None,
    job_id: str | None,
    session: str,
    interval: float,
) -> int:
    """Print the summary once (the ``--once`` and no-tmux path)."""
    db = _open_or_none(db_path)
    if db is None:
        print(f"watch: no SaberOps store at {db_path}", file=sys.stderr)
        return 1
    try:
        jobs = select_jobs(db, repo=repo, job_id=job_id)
    except WatchError as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 1
    print(
        render_summary(
            db,
            jobs,
            session=session,
            repo=repo,
        )
    )
    running = [job for job in jobs if job.state == JobState.RUNNING]
    for job in running:
        print("")
        print(render_job_header(job))
        print(job_status_line(db, job))
    return 0


def run_summary_loop(
    *,
    db_path: Path,
    repo: Path | None,
    job_id: str | None,
    session: str,
    interval: float,
    once: bool,
) -> int:
    """The summary window: redraw the list and reconcile job windows."""
    tmux = Tmux()
    if not tmux.available():
        print("watch: tmux is not available; running a one-shot summary", file=sys.stderr)
        return run_snapshot(
            db_path=db_path, repo=repo, job_id=job_id, session=session, interval=interval
        )
    while True:
        db = _open_or_none(db_path)
        if db is None:
            return 1
        try:
            jobs = reconcile_session(
                tmux,
                session,
                db,
                repo=repo,
                job_id=job_id,
                db_path=db_path,
                interval=interval,
            )
        except (TmuxError, WatchError) as exc:
            print(f"watch: {exc}", file=sys.stderr)
            return 1
        frame = render_summary(db, jobs, session=session, repo=repo)
        if once:
            print(frame)
            return 0
        print(_ANSI_CLEAR + frame, flush=True)
        time.sleep(interval)


def run_follow(
    *,
    db_path: Path,
    job_id: str,
    interval: float,
    once: bool,
) -> int:
    """One job window: append new events/output and the next owner action."""
    cursor = TailCursor()
    printed_header = False
    last_status: tuple[str, str, str, str, int] | None = None
    last_status_at = 0.0
    last_state: JobState | None = None
    while True:
        db = _open_or_none(db_path)
        if db is None:
            return 1
        try:
            job = get_job(db, job_id)
            if job is None:
                print(f"watch: unknown job: {job_id}", file=sys.stderr)
                return 1
            if not printed_header:
                print(render_job_header(job), flush=True)
                printed_header = True
            status = job_status_key(db, job)
            now_monotonic = time.monotonic()
            if status != last_status or now_monotonic - last_status_at >= STATUS_REFRESH_SECONDS:
                print(job_status_line(db, job), flush=True)
                last_status = status
                last_status_at = now_monotonic
            text, cursor = _tail_once(db, job, cursor, MAX_TAIL_BYTES)
        except Exception as exc:  # a live view survives a transient store error
            print(f"watch: cannot refresh {job_id}: {exc}", file=sys.stderr, flush=True)
            if once:
                return 1
            time.sleep(max(1.0, interval))
            continue
        if text:
            print(text, end="", flush=True)
        if job.state in TERMINAL_JOB_STATES and job.state != last_state:
            print(render_job_final(db, job), flush=True)
        last_state = job.state
        if once:
            return 0
        if job.state in TERMINAL_JOB_STATES:
            # Keep the window (marked, never closed) until the owner closes it.
            time.sleep(max(5.0, interval))
            continue
        time.sleep(interval)


def run_monitor_fallback(
    *,
    db_path: Path,
    repo: Path | None,
    job_id: str | None,
    once: bool,
    interval: float,
    timeout: float,
) -> int:
    """No tmux: hand the single selected run to the existing ``orch monitor``."""
    import argparse

    from saberops.cli import handle_monitor

    db = _open_or_none(db_path)
    if db is None:
        print(f"watch: no SaberOps store at {db_path}", file=sys.stderr)
        return 1
    try:
        jobs = select_jobs(db, repo=repo, job_id=job_id)
    except WatchError as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 1
    running = [job for job in jobs if job.state == JobState.RUNNING and job.current_run_id]
    if len(running) != 1:
        print(
            render_summary(
                db,
                jobs,
                session="(no tmux: orch monitor fallback)",
                repo=repo,
            )
        )
        if not running:
            print(
                "\ntmux is not installed and no job is running; nothing to follow.",
                file=sys.stderr,
            )
            return 0
        print(
            "\ntmux is not installed; pass `--job ID` to follow one of the "
            "running jobs with `orch monitor`.",
            file=sys.stderr,
        )
        return 0
    run_id = running[0].current_run_id
    assert run_id is not None
    print(
        f"tmux is not installed; following {running[0].job_id} with "
        f"`orch monitor {run_id}` instead.",
        file=sys.stderr,
    )
    ns = argparse.Namespace(
        run_id=run_id,
        db=str(db_path),
        interval=interval,
        once=once,
        timeout=timeout,
    )
    return handle_monitor(ns)


def _repo_path(raw: str | None) -> Path | None:
    if not raw:
        return None
    path = Path(raw).expanduser().resolve()
    if not path.exists():
        raise WatchError(f"--repo path does not exist: {path}")
    return path


def _ensure_session(
    tmux: Tmux,
    desired: str,
    *,
    explicit: bool,
    db_path: Path,
    repo: Path | None,
    job_id: str | None,
    interval: float,
) -> str:
    """Create the session, or reuse ours; retry if another owner wins the name."""
    last: TmuxError | None = None
    for _ in range(5):
        name = resolve_session_name(tmux, desired, explicit=explicit)
        if tmux.has_session(name):
            return name
        try:
            tmux.new_session(
                name,
                _summary_argv(
                    session=name,
                    db_path=db_path,
                    repo=repo,
                    job_id=job_id,
                    interval=interval,
                ),
            )
        except TmuxError as exc:
            if explicit:
                raise
            last = exc
            continue
        try:
            tmux.mark_session_ours(name)
        except TmuxError:
            tmux.kill_session(name)
            raise
        return name
    raise TmuxError(f"could not create a tmux session near {desired!r}: {last}")


def handle_watch(args: Any) -> int:
    """Entry point for ``orch watch``."""
    interval = max(0.5, float(getattr(args, "interval", 2.0) or 2.0))
    once = bool(getattr(args, "once", False))
    timeout = max(0.0, float(getattr(args, "timeout", 0.0) or 0.0))
    db_path = window_db_path(getattr(args, "db", None))
    try:
        repo = _repo_path(getattr(args, "repo", None))
    except WatchError as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 2
    job_id = getattr(args, "job_id", None)

    if getattr(args, "summary", False):
        session = getattr(args, "session", None) or DEFAULT_SESSION_BASE
        return run_summary_loop(
            db_path=db_path,
            repo=repo,
            job_id=job_id,
            session=session,
            interval=interval,
            once=once,
        )
    if getattr(args, "follow", False):
        if not job_id:
            print("watch: --follow requires --job", file=sys.stderr)
            return 2
        return run_follow(
            db_path=db_path,
            job_id=job_id,
            interval=interval,
            once=once,
        )
    if once:
        session = getattr(args, "session", None) or default_session_name(repo, db_path)
        return run_snapshot(
            db_path=db_path,
            repo=repo,
            job_id=job_id,
            session=session,
            interval=interval,
        )

    tmux = Tmux()
    if not tmux.available():
        print(
            "watch: tmux is not installed; falling back to `orch monitor`.",
            file=sys.stderr,
        )
        return run_monitor_fallback(
            db_path=db_path,
            repo=repo,
            job_id=job_id,
            once=False,
            interval=interval,
            timeout=timeout,
        )

    db = _open_or_none(db_path)
    if db is None:
        print(f"watch: no SaberOps store at {db_path}", file=sys.stderr)
        return 1
    explicit = getattr(args, "session", None) is not None
    desired = args.session or default_session_name(repo, db_path)
    try:
        session = _ensure_session(
            tmux,
            desired,
            explicit=explicit,
            db_path=db_path,
            repo=repo,
            job_id=job_id,
            interval=interval,
        )
    except (TmuxError, WatchError) as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 2 if isinstance(exc, WatchError) else 1
    try:
        reconcile_session(
            tmux,
            session,
            db,
            repo=repo,
            job_id=job_id,
            db_path=db_path,
            interval=interval,
        )
    except (TmuxError, WatchError) as exc:
        print(f"watch: {exc}", file=sys.stderr)
        return 1

    if not sys.stdin.isatty() and not os.environ.get("TMUX"):
        print(
            f"watch: session {session!r} is running; attach with "
            f"`tmux attach-session -t {session}`."
        )
        return 0
    print(
        f"watch: attaching to {session!r} (read-only; keystrokes never reach a "
        "worker).",
        file=sys.stderr,
    )
    return tmux.attach(session)
