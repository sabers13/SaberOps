"""On-demand advisor: a stronger model on call, never coding (ADR-0034).

The owner's workflow (2026-11-19): a strong model reads the attempt and advises
only when the worker or the planner asks, while a cheaper model does the
building.  This module is the engine side of that workflow:

* a per-project **advisor route** (off by default): one plan-subscription
  provider/model plus a per-job call budget (default 3);
* the advisor runs **read-only** in a disposable detached worktree of the
  run's base, never in the run's own worktree, and is asked for bounded text;
* the worker asks from its own worktree with ``orch advisor ask`` (the prompt
  names the exact command while the route is on); the planner asks over MCP
  with ``ask_advisor``;
* its advice is returned to the caller and recorded on the run, so it reaches
  a worker at the moment it is needed rather than at the next attempt boundary;
* calls are bounded by the per-job budget, counted in per-job usage, refused
  while the work gate is closed, and refused with a visible reason when
  the route is unavailable;
* the advisor is not the independent reviewer and never produces a candidate
  or a commit.

Authority is unchanged: the advisor only writes advisory text.  It cannot
widen billing consent (only plan-subscription providers are accepted, and
API-key billing providers are refused), bypass the gate, the independent
review, acceptance or isolation.
"""

from __future__ import annotations

import json
import logging
import shlex
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from saberops.models import Attempt, AttemptStatus, DispatchStage, Tier, WorkerRequest

__all__ = [
    "ADVISOR_ADVICE_EVENT",
    "ADVISOR_CALL_EVENT",
    "ADVISOR_CHECKPOINT_ON_DEMAND",
    "ADVISOR_SKIPPED_EVENT",
    "ADVISOR_UNAVAILABLE_EVENT",
    "DEFAULT_ADVISOR_CALL_BUDGET",
    "MAX_ADVICE_CHARS",
    "AdvisorAdvice",
    "AdvisorConfigError",
    "AdvisorRefused",
    "AdvisorRoute",
    "advisor_command",
    "advisor_prompt_section",
    "advisor_route_or_none",
    "advisor_route_path",
    "advisor_view_for_job",
    "ask_advisor",
    "clear_advisor_route",
    "load_advisor_route",
    "set_advisor_route",
]

_LOG = logging.getLogger(__name__)

ADVISOR_CHECKPOINT_ON_DEMAND: Final[str] = "on_demand"

ADVISOR_CALL_EVENT: Final[str] = "advisor_call_started"
ADVISOR_ADVICE_EVENT: Final[str] = "advisor_advice"
ADVISOR_SKIPPED_EVENT: Final[str] = "advisor_skipped"
ADVISOR_UNAVAILABLE_EVENT: Final[str] = "advisor_unavailable"

ADVISOR_FILENAME: Final[str] = "advisor.json"
ADVISOR_SCHEMA_VERSION: Final[int] = 1
DEFAULT_ADVISOR_CALL_BUDGET: Final[int] = 3
MAX_ADVISOR_CALL_BUDGET: Final[int] = 20
MAX_ADVICE_CHARS: Final[int] = 1200
#: One advisor turn reads the task, the attempt record and a bounded diff, so it
#: is a normal model turn: H37 saw a real one outlive 120 s.  The bound is five
#: times that failure point and half the reviewer's 1200 s bound, so a hung
#: advisor ends long before it holds a planner call as long as a review
#: (ADR-0107).
DEFAULT_ADVISOR_TIMEOUT_SECONDS: Final[float] = 600.0
MIN_ADVISOR_TIMEOUT_SECONDS: Final[float] = 1.0
MAX_ADVISOR_TIMEOUT_SECONDS: Final[float] = 3600.0
_MAX_QUESTION_CHARS: Final[int] = 2000
_MAX_RECENT_OUTPUT_CHARS: Final[int] = 2000
_MAX_FAILING_CHECK_CHARS: Final[int] = 1500
_MAX_DIFF_CHARS: Final[int] = 3000
_MAX_PLAN_LINES: Final[int] = 12

#: Providers whose CLI bills the owner's existing subscription/plan.  The
#: advisor route is restricted to these so an API-key (pay-per-token) route can
#: never be silently introduced as an advisor.
PLAN_ROUTE_PROVIDERS: Final[frozenset[str]] = frozenset(
    {"codex", "opencode", "anthropic", "claude"}
)


class AdvisorConfigError(RuntimeError):
    """Raised for an unreadable or invalid advisor route document."""


class AdvisorRefused(RuntimeError):
    """One refused on-demand advisor call, with a stable reason code.

    ``code`` lets the CLI and MCP report the same refusal vocabulary:
    ``ADVISOR_OFF``, ``ADVISOR_PAUSED``, ``ADVISOR_BUDGET_EXHAUSTED`` or
    ``ADVISOR_UNAVAILABLE``.  A refusal never stops the run.
    """

    def __init__(self, code: str, message: str, *, detail: str | None = None):
        super().__init__(message)
        self.code = code
        self.detail = detail


@dataclass(frozen=True)
class AdvisorRoute:
    """One optional advisor route for a project."""

    provider: str
    model: str
    call_budget: int = DEFAULT_ADVISOR_CALL_BUDGET
    timeout_seconds: float = DEFAULT_ADVISOR_TIMEOUT_SECONDS

    @property
    def route_id(self) -> str:
        return f"{self.provider}/{self.model}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "route": self.route_id,
            "call_budget": self.call_budget,
            "timeout_seconds": self.timeout_seconds,
        }


@dataclass(frozen=True)
class AdvisorAdvice:
    """One bounded piece of on-demand advice."""

    advice: str
    provider: str
    model: str
    call_id: str
    checkpoint: str = ADVISOR_CHECKPOINT_ON_DEMAND


def _project_state_dir(repo: Path | str, *, create: bool) -> Path:
    from saberops.project import (
        ProjectIdentityError,
        bind_project_state_dir,
        compute_project_identity,
        state_home,
    )

    try:
        identity = compute_project_identity(repo)
    except ProjectIdentityError as exc:  # pragma: no cover - caller passes a repo
        raise AdvisorConfigError(
            f"cannot resolve project state directory for {Path(repo)}: {exc}"
        ) from exc
    # Reads never bind a project: ``catch_up``/``get_job`` are read-only, and an
    # advisor that is off must not create any project state.
    if create:
        return bind_project_state_dir(identity)
    # Status views ("watch", ``list_jobs``) read this for every job, so the
    # non-creating lookup is memoized per (state home, repository) for the
    # life of the process; the state home is part of the key so an isolated
    # test store can never reuse another store's answer.
    return Path(_cached_project_state_dir(str(state_home()), str(Path(repo).resolve())))


@lru_cache(maxsize=256)
def _cached_project_state_dir(state_home_path: str, repo_path: str) -> str:
    from saberops.project import compute_project_identity, project_state_dir

    return str(project_state_dir(compute_project_identity(repo_path)))


def advisor_route_path(repo: Path | str) -> Path:
    """Return the per-project advisor route document path."""
    return _project_state_dir(repo, create=False) / ADVISOR_FILENAME


def _coerce_timeout(value: object, source: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdvisorConfigError(f"{source}: timeout_seconds must be a number of seconds")
    timeout = float(value)
    if not (MIN_ADVISOR_TIMEOUT_SECONDS <= timeout <= MAX_ADVISOR_TIMEOUT_SECONDS):
        raise AdvisorConfigError(
            f"{source}: timeout_seconds must be between "
            f"{MIN_ADVISOR_TIMEOUT_SECONDS:g} and {MAX_ADVISOR_TIMEOUT_SECONDS:g}"
        )
    return timeout


def _coerce_route(data: object, source: str) -> AdvisorRoute | None:
    if not isinstance(data, dict):
        raise AdvisorConfigError(f"advisor route at {source}: payload must be a JSON object")
    version = data.get("version")
    if version != ADVISOR_SCHEMA_VERSION:
        raise AdvisorConfigError(
            f"advisor route at {source}: unknown schema version {version!r}; "
            f"expected {ADVISOR_SCHEMA_VERSION}"
        )
    if data.get("enabled") is False:
        return None
    provider = str(data.get("provider") or "").strip().lower()
    model = str(data.get("model") or "").strip()
    if not provider or not model:
        raise AdvisorConfigError(
            f"advisor route at {source}: provider and model are required"
        )
    raw_budget = data.get("call_budget", DEFAULT_ADVISOR_CALL_BUDGET)
    if type(raw_budget) is not int or not (1 <= raw_budget <= MAX_ADVISOR_CALL_BUDGET):
        raise AdvisorConfigError(
            f"advisor route at {source}: call_budget must be an integer between "
            f"1 and {MAX_ADVISOR_CALL_BUDGET}"
        )
    # A document written before ADR-0107 has no timeout_seconds: it keeps
    # working on the stated default (expand, never break an old document).
    timeout = _coerce_timeout(
        data.get("timeout_seconds", DEFAULT_ADVISOR_TIMEOUT_SECONDS),
        f"advisor route at {source}",
    )
    return AdvisorRoute(
        provider=provider,
        model=model,
        call_budget=raw_budget,
        timeout_seconds=timeout,
    )


def load_advisor_route(repo: Path | str) -> AdvisorRoute | None:
    """Load the project's advisor route; ``None`` means off (the default)."""
    path = advisor_route_path(repo)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise AdvisorConfigError(f"advisor route at {path} is unreadable: {exc}") from exc
    return _coerce_route(payload, str(path))


def advisor_route_or_none(repo: Path | str) -> AdvisorRoute | None:
    """Load the route, treating any read failure as off (for views/tools).

    This is the lenient reader used to decide whether the worker prompt gets an
    advisor section and whether the MCP tool exists.  A broken or unreadable
    route must never break a dispatch, so it means "off" here; the owner-facing
    ``orch project advisor show`` still reports the exact error.
    """
    try:
        return load_advisor_route(repo)
    except Exception as exc:  # noqa: BLE001 - reads never break dispatch or a session
        _LOG.warning("advisor route is unreadable; treating it as off: %s", exc)
        return None


def _atomic_write(path: Path, payload: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{id(payload)}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def set_advisor_route(
    repo: Path | str,
    *,
    provider: str,
    model: str,
    call_budget: int = DEFAULT_ADVISOR_CALL_BUDGET,
    timeout_seconds: float = DEFAULT_ADVISOR_TIMEOUT_SECONDS,
) -> AdvisorRoute:
    """Persist one advisor route for the project (owner action, CLI/agent)."""
    clean_provider = (provider or "").strip().lower()
    clean_model = (model or "").strip()
    if not clean_provider or not clean_model:
        raise AdvisorConfigError("advisor route needs a provider and a model")
    if type(call_budget) is not int or not (1 <= call_budget <= MAX_ADVISOR_CALL_BUDGET):
        raise AdvisorConfigError(
            f"advisor call budget must be an integer between 1 and {MAX_ADVISOR_CALL_BUDGET}"
        )
    timeout = _coerce_timeout(timeout_seconds, "advisor timeout")
    route = AdvisorRoute(
        provider=clean_provider,
        model=clean_model,
        call_budget=call_budget,
        timeout_seconds=timeout,
    )
    path = _project_state_dir(repo, create=True) / ADVISOR_FILENAME
    _atomic_write(
        path,
        {
            "version": ADVISOR_SCHEMA_VERSION,
            "enabled": True,
            **route.as_dict(),
        },
    )
    return route


def clear_advisor_route(repo: Path | str) -> Path:
    """Turn the advisor off for the project (removes the route document)."""
    path = advisor_route_path(repo)
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return path


def advisor_command() -> str:
    """The exact command a headless worker runs from its own worktree."""
    executable = sys.executable or "orch"
    return f"{shlex.quote(executable)} -m saberops.cli advisor ask --question"


def advisor_prompt_section(route: AdvisorRoute | None) -> str:
    """The worker-prompt instruction, or ``""`` when the advisor is off."""
    if route is None:
        return ""
    return (
        "[ADVISOR]\n"
        f"A stronger model is available for advice, up to {route.call_budget} times "
        "per job. Ask it from this worktree (it only reads and advises: it cannot "
        "edit files, change the task, run the gate or accept anything):\n"
        f'  {advisor_command()} "your question"\n'
        "The advice is printed and recorded on this run."
    )


def _events(db: Any, run_id: str, event_type: str) -> list[Any]:
    try:
        return list(db.get_events_by_type(run_id, event_type))
    except Exception:  # noqa: BLE001 - advisor reads never break a run
        return []


def _job_run_ids(db: Any, run_id: str) -> list[str]:
    """Every run of the job that owns ``run_id`` (the budget is per job)."""
    run_ids = [run_id]
    try:
        from saberops.jobs.store import find_job_for_run

        job = find_job_for_run(db, run_id)
    except Exception:  # noqa: BLE001 - a missing job table means a standalone run
        job = None
    if job is None:
        return run_ids
    for candidate in (getattr(job, "run_ids", ()) or ()):
        if candidate and candidate not in run_ids:
            run_ids.append(candidate)
    current = getattr(job, "current_run_id", None)
    if current and current not in run_ids:
        run_ids.append(current)
    return run_ids


def _call_count(db: Any, run_id: str) -> int:
    return sum(len(_events(db, rid, ADVISOR_CALL_EVENT)) for rid in _job_run_ids(db, run_id))


def _gate_answer(db: Any) -> tuple[bool, str | None]:
    """Return ``(open, reason)`` without mutating the gate document."""
    from saberops.runtime_gate import evaluate_gate

    try:
        state = evaluate_gate(db=db)
    except Exception as exc:  # noqa: BLE001 - fail closed, never crash the run
        return False, f"gate evaluation failed: {exc.__class__.__name__}"
    return (not state.closed), state.reason


def _route_unavailable_reason(registry: Any, route: AdvisorRoute) -> str | None:
    """Why this route cannot advise right now, or ``None`` when it can."""
    provider = route.provider
    if provider not in PLAN_ROUTE_PROVIDERS:
        return "advisor route is not on a plan subscription"
    try:
        from saberops.control_plane.policy import load_control_plane_policy

        policy = load_control_plane_policy()
    except Exception as exc:  # noqa: BLE001 - unreadable policy fails closed
        return f"provider policy is unreadable: {exc.__class__.__name__}"
    state = str(policy.provider(provider).state)
    if state == "DISABLED":
        return "advisor provider is disabled by owner policy"
    if provider in policy.api_billing_providers:
        return "advisor route would bill an API key, not a plan"
    adapter = registry.get(provider)
    if adapter is None:
        return "no worker adapter is registered for the advisor provider"
    try:
        available = bool(adapter.is_available())
    except Exception:  # noqa: BLE001
        available = False
    if not available:
        return "advisor provider CLI is not available"
    return None


def _bounded(text: str | None, limit: int) -> str:
    cleaned = (text or "").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _plan_lines(plan_steps: Sequence[Any]) -> list[str]:
    lines: list[str] = []
    for step in list(plan_steps)[:_MAX_PLAN_LINES]:
        title = str(getattr(step, "title", "") or "").strip()
        goal = str(getattr(step, "goal", "") or "").strip()
        status = getattr(step, "status", "")
        status_text = getattr(status, "value", status)
        line = f"- [{status_text}] {title}"
        if goal:
            line += f": {_bounded(goal, 120)}"
        lines.append(line)
    return lines


def _render_prompt(
    *,
    question: str,
    task: str,
    plan_steps: Sequence[Any],
    attempts: Sequence[Attempt],
    checks: Sequence[Any],
    diff_text: str,
) -> str:
    latest_attempt = attempts[-1] if attempts else None
    recent_output = ""
    if latest_attempt is not None:
        recent_output = _bounded(
            "\n".join(
                part
                for part in (
                    (latest_attempt.worker_stdout or "").strip(),
                    (latest_attempt.worker_stderr or "").strip(),
                )
                if part
            ),
            _MAX_RECENT_OUTPUT_CHARS,
        )
    failing_checks: list[str] = []
    for check in checks:
        if getattr(check, "passed", True):
            continue
        failing_checks.append(
            _bounded(
                f"exit {getattr(check, 'exit_code', '?')}: "
                f"{(getattr(check, 'stderr', '') or getattr(check, 'stdout', '') or '').strip()}",
                _MAX_FAILING_CHECK_CHARS,
            )
        )
    lines = [
        "[ADVISOR REQUEST]",
        "You are a senior technical advisor for a coding worker. You do not",
        "write code and you never modify files: your answer is advisory text",
        "only. Read the request and the attempt record below and give the",
        "worker concrete, bounded advice.",
        "",
        "Rules:",
        "- At most 25 short lines of plain text.",
        "- Point out the most likely wrong assumption, the missing verification,",
        "  or the concrete next step that unblocks the failing check.",
        "- Do not restate the task and do not ask questions.",
        "- If the approach is sound, write exactly: No changes needed.",
        "",
        "Question:",
        _bounded(question, _MAX_QUESTION_CHARS),
        "",
        "Task:",
        _bounded(task, 4000),
    ]
    plan = _plan_lines(plan_steps)
    if plan:
        lines.extend(["", "Plan:", *plan])
    if recent_output:
        lines.extend(["", "Recent worker output (truncated):", recent_output])
    if failing_checks:
        lines.extend(["", "Failing checks:", *failing_checks])
    if diff_text:
        lines.extend(["", "Diff so far (truncated):", diff_text])
    lines.extend(["", "Advice:"])
    return "\n".join(lines)


def _create_scratch(repo: Path | str, run_id: str, call_seq: int, base_sha: str) -> Path:
    import shutil

    from saberops.git import GitManager

    base = GitManager.get_default_worktree_base(repo)
    scratch = base / run_id / f"advisor-{call_seq}"
    if scratch.exists():
        # A previous crashed call may have left its scratch worktree behind.
        try:
            GitManager.remove_worktree(repo, scratch)
        except Exception:  # noqa: BLE001 - an unregistered leftover is removable
            shutil.rmtree(scratch, ignore_errors=True)
    GitManager.create_detached_worktree(repo, scratch, base_sha)
    return scratch


def _remove_scratch(repo: Path | str, scratch: Path) -> None:
    from saberops.git import GitManager

    try:
        GitManager.remove_worktree(repo, scratch)
    except Exception as exc:  # noqa: BLE001 - scratch removal never fails the run
        _LOG.warning("could not remove advisor scratch worktree %s: %s", scratch, exc)


def _record_usage(db: Any, *, run_id: str, attempt_id: str | None, call_id: str,
                  route: AdvisorRoute, prompt_bytes: int, result: Any) -> None:
    from saberops.telemetry import persist_worker_usage

    try:
        persist_worker_usage(
            db,
            run_id=run_id,
            association_id=call_id,
            attempt_id=attempt_id,
            review_id=None,
            stage=DispatchStage.ADVISOR,
            # The advisor's whole purpose is a stronger model; usage is booked
            # at the strong tier so per-job totals reflect what it cost.
            tier=Tier.T3,
            provider=route.provider,
            model=route.model,
            prompt_bytes=prompt_bytes,
            transport=str(getattr(result, "prompt_transport_used", None) or "unknown"),
            result=result,
        )
    except Exception as exc:  # noqa: BLE001 - usage evidence never breaks advice
        _LOG.warning("could not record advisor usage: %s", exc)


def _record_event(db: Any, run_id: str, event_type: str,
                  *, attempt_id: str | None, payload: dict[str, Any]) -> None:
    try:
        db.record_event(run_id, event_type, attempt_id=attempt_id, payload=payload)
    except Exception as exc:  # noqa: BLE001 - evidence never breaks the run
        _LOG.warning("could not record %s: %s", event_type, exc)


def _refusal_payload(advisor: AdvisorRoute, reason: str, **extra: Any) -> dict[str, Any]:
    return {
        "checkpoint": ADVISOR_CHECKPOINT_ON_DEMAND,
        "reason": reason,
        "provider": advisor.provider,
        "model": advisor.model,
        **extra,
    }


def ask_advisor(
    db: Any,
    *,
    repo: Path | str,
    run_id: str,
    registry: Any,
    attempt_id: str | None,
    task: str,
    question: str,
    plan_steps: Sequence[Any] = (),
    attempts: Sequence[Attempt] = (),
    base_sha: str,
    prior_worktree: Path | str | None = None,
) -> AdvisorAdvice:
    """Ask the configured advisor once; refuse with a reason when it cannot run.

    The run continues whatever the answer: :class:`AdvisorRefused` is evidence,
    never a failure of the work.  Off, paused, spent budget and an unavailable
    route are all refused before a process starts.
    """
    route = advisor_route_or_none(repo)
    if route is None:
        raise AdvisorRefused(
            "ADVISOR_OFF",
            "the advisor is off for this project; the owner enables it with "
            "`orch project advisor set <provider> <model>`",
        )

    open_gate, gate_reason = _gate_answer(db)
    if not open_gate:
        _record_event(
            db,
            run_id,
            ADVISOR_SKIPPED_EVENT,
            attempt_id=attempt_id,
            payload=_refusal_payload(
                route, "paused", detail=gate_reason, question=_bounded(question, 1000)
            ),
        )
        raise AdvisorRefused(
            "ADVISOR_PAUSED",
            f"work is paused ({gate_reason or 'gate closed'}); the advisor is "
            "refused until work resumes",
            detail=gate_reason,
        )

    used = _call_count(db, run_id)
    if used >= route.call_budget:
        _record_event(
            db,
            run_id,
            ADVISOR_SKIPPED_EVENT,
            attempt_id=attempt_id,
            payload=_refusal_payload(
                route,
                "budget exhausted",
                call_index=used,
                call_budget=route.call_budget,
                question=_bounded(question, 1000),
            ),
        )
        raise AdvisorRefused(
            "ADVISOR_BUDGET_EXHAUSTED",
            f"advisor budget exhausted: {used} of {route.call_budget} calls "
            "already used for this job",
        )

    unavailable = _route_unavailable_reason(registry, route)
    if unavailable is not None:
        _record_event(
            db,
            run_id,
            ADVISOR_UNAVAILABLE_EVENT,
            attempt_id=attempt_id,
            payload=_refusal_payload(route, unavailable),
        )
        raise AdvisorRefused("ADVISOR_UNAVAILABLE", unavailable)

    call_seq = used + 1
    call_id = f"advisor_{call_seq}"
    _record_event(
        db,
        run_id,
        ADVISOR_CALL_EVENT,
        attempt_id=attempt_id,
        payload={
            "checkpoint": ADVISOR_CHECKPOINT_ON_DEMAND,
            "call_id": call_id,
            "provider": route.provider,
            "model": route.model,
            "call_index": call_seq,
            "call_budget": route.call_budget,
            "question": _bounded(question, _MAX_QUESTION_CHARS),
        },
    )

    diff_text = ""
    if prior_worktree is not None:
        try:
            from saberops.git import GitManager

            diff_text = _bounded(
                GitManager.get_diff(prior_worktree, base_sha), _MAX_DIFF_CHARS
            )
        except Exception:  # noqa: BLE001 - the diff is context, never authority
            diff_text = ""
    checks: list[Any] = []
    try:
        checks = list(db.get_checks_for_run(run_id))
    except Exception:  # noqa: BLE001
        checks = []
    prompt = _render_prompt(
        question=question,
        task=task,
        plan_steps=plan_steps,
        attempts=attempts,
        checks=checks,
        diff_text=diff_text,
    )

    adapter = registry.get(route.provider)
    if adapter is None:  # pragma: no cover - checked above
        raise AdvisorRefused(
            "ADVISOR_UNAVAILABLE",
            "no worker adapter is registered for the advisor provider",
        )
    result: Any = None
    scratch: Path | None = None
    try:
        scratch = _create_scratch(repo, run_id, call_seq, base_sha)
        result = adapter.run(
            WorkerRequest(
                task=prompt,
                worktree_path=scratch,
                model=route.model,
                timeout_seconds=route.timeout_seconds,
                read_only=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - advice is best effort
        reason = f"advisor call failed: {exc.__class__.__name__}"
        _record_event(
            db,
            run_id,
            ADVISOR_UNAVAILABLE_EVENT,
            attempt_id=attempt_id,
            payload=_refusal_payload(route, reason),
        )
        raise AdvisorRefused("ADVISOR_UNAVAILABLE", reason) from exc
    finally:
        if scratch is not None:
            _remove_scratch(repo, scratch)

    _record_usage(
        db,
        run_id=run_id,
        attempt_id=attempt_id,
        call_id=call_id,
        route=route,
        prompt_bytes=len(prompt.encode("utf-8")),
        result=result,
    )
    status = getattr(result, "status", None)
    if status is not AttemptStatus.SUCCESS:
        extra: dict[str, Any] = {}
        if status is AttemptStatus.TIMEOUT:
            # Name the route and the bound, not just "TIMEOUT": that is what
            # tells the owner which advisor and how long to change (ADR-0107).
            reason = (
                f"advisor route {route.route_id} timed out after "
                f"{route.timeout_seconds:g}s"
            )
            extra = {
                "route": route.route_id,
                "timeout_seconds": route.timeout_seconds,
                "duration_seconds": getattr(result, "duration_seconds", None),
            }
        else:
            reason = f"advisor call returned {getattr(status, 'value', status)}"
        _record_event(
            db,
            run_id,
            ADVISOR_UNAVAILABLE_EVENT,
            attempt_id=attempt_id,
            payload=_refusal_payload(
                route,
                reason,
                detail=_bounded(getattr(result, "error", None), 300),
                **extra,
            ),
        )
        raise AdvisorRefused("ADVISOR_UNAVAILABLE", reason)

    text = _bounded(
        (getattr(result, "stdout", "") or "").strip()
        or (getattr(result, "summary", "") or "").strip(),
        MAX_ADVICE_CHARS,
    )
    if not text:
        _record_event(
            db,
            run_id,
            ADVISOR_UNAVAILABLE_EVENT,
            attempt_id=attempt_id,
            payload=_refusal_payload(route, "advisor returned no advice text"),
        )
        raise AdvisorRefused("ADVISOR_UNAVAILABLE", "advisor returned no advice text")

    _record_event(
        db,
        run_id,
        ADVISOR_ADVICE_EVENT,
        attempt_id=attempt_id,
        payload={
            "checkpoint": ADVISOR_CHECKPOINT_ON_DEMAND,
            "call_id": call_id,
            "provider": route.provider,
            "model": route.model,
            "question": _bounded(question, _MAX_QUESTION_CHARS),
            "advice": text,
        },
    )
    return AdvisorAdvice(
        advice=text,
        provider=route.provider,
        model=route.model,
        call_id=call_id,
    )


def advisor_view_for_job(db: Any, job: Any) -> dict[str, Any]:
    """Read-only projection of advisor activity for one job's runs.

    Shared by ``get_job``, ``tail_job`` and ``catch_up`` so one record drives
    every surface.  An absent route reads as ``enabled: False`` with no calls.
    """
    repo = getattr(job, "repo", None)
    route: AdvisorRoute | None = None
    route_error: str | None = None
    if repo:
        try:
            route = load_advisor_route(repo)
        except AdvisorConfigError as exc:
            route_error = str(exc)

    run_ids: list[str] = list(getattr(job, "run_ids", ()) or ())
    current = getattr(job, "current_run_id", None)
    if current and current not in run_ids:
        run_ids.append(current)

    calls: list[dict[str, Any]] = []
    advice: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    unavailable: list[dict[str, Any]] = []
    for run_id in run_ids:
        if not run_id:
            continue
        for event in _events(db, run_id, ADVISOR_CALL_EVENT):
            payload = dict(event.payload or {})
            payload["recorded_at"] = event.created_at
            calls.append(payload)
        for event in _events(db, run_id, ADVISOR_ADVICE_EVENT):
            payload = dict(event.payload or {})
            payload["recorded_at"] = event.created_at
            payload["attempt_id"] = event.attempt_id
            advice.append(payload)
        for event in _events(db, run_id, ADVISOR_SKIPPED_EVENT):
            payload = dict(event.payload or {})
            payload["recorded_at"] = event.created_at
            skipped.append(payload)
        for event in _events(db, run_id, ADVISOR_UNAVAILABLE_EVENT):
            payload = dict(event.payload or {})
            payload["recorded_at"] = event.created_at
            unavailable.append(payload)

    budget = route.call_budget if route is not None else None
    return {
        "enabled": route is not None,
        "route": route.as_dict() if route is not None else None,
        "call_budget": budget,
        "calls": len(calls),
        "call_records": calls,
        "advice": advice,
        "skipped": skipped,
        "unavailable": unavailable,
        "error": route_error,
    }
