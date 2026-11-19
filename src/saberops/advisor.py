"""Advisor checkpoints: a stronger model on call, never coding (F26).

The owner's workflow (2026-11-19): a strong model reads the attempt and advises
only at key moments, while a cheaper model does the building.  This module is
the engine side of that workflow:

* a per-project **advisor route** (off by default): one plan-subscription
  provider/model plus a per-job call budget (default 3);
* the advisor runs **read-only** in a disposable detached worktree of the
  run's base, never in the run's own worktree, and is asked for bounded text;
* three checkpoints: before the first worker attempt plans, after the same
  failure twice in a row, and before the result goes to independent review;
* its advice is recorded on the run and delivered to the worker through the
  ordinary steering-note channel (ADR-0016), so it reaches a worker only at an
  attempt boundary;
* calls are bounded by the budget, counted in per-job usage, refused while the
  work gate is closed (F17), and skipped with a visible reason when the route
  is unavailable;
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
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Final

from saberops.models import Attempt, AttemptStatus, DispatchStage, Tier, WorkerRequest

__all__ = [
    "ADVISOR_CALL_EVENT",
    "ADVISOR_ADVICE_EVENT",
    "ADVISOR_SKIPPED_EVENT",
    "ADVISOR_UNAVAILABLE_EVENT",
    "CHECKPOINT_BEFORE_PLAN",
    "CHECKPOINT_BEFORE_REVIEW",
    "CHECKPOINT_REPEAT_FAILURE",
    "DEFAULT_ADVISOR_CALL_BUDGET",
    "MAX_ADVICE_CHARS",
    "AdvisorAdvice",
    "AdvisorConfigError",
    "AdvisorRoute",
    "advisor_before_attempt",
    "advisor_before_review",
    "advisor_route_path",
    "advisor_view_for_job",
    "clear_advisor_route",
    "load_advisor_route",
    "set_advisor_route",
]

_LOG = logging.getLogger(__name__)

CHECKPOINT_BEFORE_PLAN: Final[str] = "before_plan"
CHECKPOINT_REPEAT_FAILURE: Final[str] = "repeat_failure"
CHECKPOINT_BEFORE_REVIEW: Final[str] = "before_review"

ADVISOR_CALL_EVENT: Final[str] = "advisor_call_started"
ADVISOR_ADVICE_EVENT: Final[str] = "advisor_advice"
ADVISOR_SKIPPED_EVENT: Final[str] = "advisor_skipped"
ADVISOR_UNAVAILABLE_EVENT: Final[str] = "advisor_unavailable"

ADVISOR_FILENAME: Final[str] = "advisor.json"
ADVISOR_SCHEMA_VERSION: Final[int] = 1
DEFAULT_ADVISOR_CALL_BUDGET: Final[int] = 3
MAX_ADVISOR_CALL_BUDGET: Final[int] = 20
MAX_ADVICE_CHARS: Final[int] = 1200
DEFAULT_ADVISOR_TIMEOUT_SECONDS: Final[float] = 120.0
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


@dataclass(frozen=True)
class AdvisorRoute:
    """One optional advisor route for a project."""

    provider: str
    model: str
    call_budget: int = DEFAULT_ADVISOR_CALL_BUDGET

    @property
    def route_id(self) -> str:
        return f"{self.provider}/{self.model}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "route": self.route_id,
            "call_budget": self.call_budget,
        }


@dataclass(frozen=True)
class AdvisorAdvice:
    """One bounded piece of advice produced at a checkpoint."""

    checkpoint: str
    advice: str
    provider: str
    model: str
    call_id: str
    signature: str | None = None
    note: Any | None = None


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
    return AdvisorRoute(provider=provider, model=model, call_budget=raw_budget)


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
    route = AdvisorRoute(provider=clean_provider, model=clean_model, call_budget=call_budget)
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


def _events(db: Any, run_id: str, event_type: str) -> list[Any]:
    try:
        return list(db.get_events_by_type(run_id, event_type))
    except Exception:  # noqa: BLE001 - advisor reads never break a run
        return []


def _event_checkpoint(event: Any) -> str | None:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return None
    value = payload.get("checkpoint")
    return str(value) if isinstance(value, str) else None


def _event_signature(event: Any) -> str | None:
    payload = getattr(event, "payload", None)
    if not isinstance(payload, dict):
        return None
    value = payload.get("signature")
    return str(value) if isinstance(value, str) else None


def _call_count(db: Any, run_id: str) -> int:
    return len(_events(db, run_id, ADVISOR_CALL_EVENT))


def _checkpoint_already_fired(
    db: Any, run_id: str, checkpoint: str, *, signature: str | None = None
) -> bool:
    for event_type in (
        ADVISOR_CALL_EVENT,
        ADVISOR_ADVICE_EVENT,
        ADVISOR_UNAVAILABLE_EVENT,
        ADVISOR_SKIPPED_EVENT,
    ):
        for event in _events(db, run_id, event_type):
            if _event_checkpoint(event) != checkpoint:
                continue
            if signature is None or _event_signature(event) == signature:
                return True
    return False


def _failure_signature(attempt: Attempt) -> str | None:
    """Stable, low-cardinality identity for one failed attempt.

    Only engine-owned fields are used, so the signature never depends on
    timestamps or file order: two attempts with the same terminal status, the
    same interrupting reason and the same bounded error text are the *same
    failure twice in a row*.
    """
    if attempt.status is not AttemptStatus.FAILED:
        return None
    detail = " ".join((attempt.worker_error or "").split())[:200]
    return f"{attempt.status.value}|{attempt.interruption_reason or ''}|{detail}"


def _repeated_failure(attempts: Sequence[Attempt]) -> str | None:
    if len(attempts) < 2:
        return None
    latest = _failure_signature(attempts[-1])
    if latest is None:
        return None
    if _failure_signature(attempts[-2]) != latest:
        return None
    return latest


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
    checkpoint: str,
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
        f"[ADVISOR CHECKPOINT: {checkpoint}]",
        "You are a senior technical advisor for a coding worker. You do not",
        "write code and you never modify files: your answer is advisory text",
        "only. Read the attempt record below and give the worker concrete,",
        "bounded advice for its next turn.",
        "",
        "Rules:",
        "- At most 25 short lines of plain text.",
        "- Point out the most likely wrong assumption, the missing verification,",
        "  or the concrete next step that unblocks the failing check.",
        "- Do not restate the task and do not ask questions.",
        "- If the approach is sound, write exactly: No changes needed.",
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


def _record_usage(db: Any, *, run_id: str, attempt_id: str, call_id: str,
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


def _advise(
    db: Any,
    *,
    repo: Path | str,
    run_id: str,
    registry: Any,
    route: AdvisorRoute,
    checkpoint: str,
    attempt_id: str,
    task: str,
    plan_steps: Sequence[Any],
    attempts: Sequence[Attempt],
    base_sha: str,
    prior_worktree: Path | str | None,
    signature: str | None,
    timeout_seconds: float,
) -> AdvisorAdvice | None:
    open_gate, gate_reason = _gate_answer(db)
    if not open_gate:
        body = {
            "checkpoint": checkpoint,
            "reason": "paused",
            "detail": gate_reason,
            "provider": route.provider,
            "model": route.model,
        }
        if signature is not None:
            body["signature"] = signature
        _record_event(db, run_id, ADVISOR_SKIPPED_EVENT, attempt_id=attempt_id, payload=body)
        return None

    unavailable = _route_unavailable_reason(registry, route)
    if unavailable is not None:
        body = {
            "checkpoint": checkpoint,
            "reason": unavailable,
            "provider": route.provider,
            "model": route.model,
        }
        if signature is not None:
            body["signature"] = signature
        _record_event(
            db, run_id, ADVISOR_UNAVAILABLE_EVENT, attempt_id=attempt_id, payload=body
        )
        return None

    call_seq = _call_count(db, run_id) + 1
    call_id = f"advisor_{call_seq}"
    start_payload: dict[str, Any] = {
        "checkpoint": checkpoint,
        "call_id": call_id,
        "provider": route.provider,
        "model": route.model,
        "call_index": call_seq,
        "call_budget": route.call_budget,
    }
    if signature is not None:
        start_payload["signature"] = signature
    _record_event(db, run_id, ADVISOR_CALL_EVENT, attempt_id=attempt_id, payload=start_payload)

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
        checkpoint=checkpoint,
        task=task,
        plan_steps=plan_steps,
        attempts=attempts,
        checks=checks,
        diff_text=diff_text,
    )

    adapter = registry.get(route.provider)
    if adapter is None:  # pragma: no cover - checked above
        return None
    result: Any = None
    scratch: Path | None = None
    try:
        scratch = _create_scratch(repo, run_id, call_seq, base_sha)
        result = adapter.run(
            WorkerRequest(
                task=prompt,
                worktree_path=scratch,
                model=route.model,
                timeout_seconds=timeout_seconds,
                read_only=True,
            )
        )
    except Exception as exc:  # noqa: BLE001 - advice is best effort
        _record_event(
            db,
            run_id,
            ADVISOR_UNAVAILABLE_EVENT,
            attempt_id=attempt_id,
            payload={
                "checkpoint": checkpoint,
                "reason": f"advisor call failed: {exc.__class__.__name__}",
                "provider": route.provider,
                "model": route.model,
            },
        )
        return None
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
        _record_event(
            db,
            run_id,
            ADVISOR_UNAVAILABLE_EVENT,
            attempt_id=attempt_id,
            payload={
                "checkpoint": checkpoint,
                "reason": f"advisor call returned {getattr(status, 'value', status)}",
                "provider": route.provider,
                "model": route.model,
                "detail": _bounded(getattr(result, "error", None), 300),
            },
        )
        return None

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
            payload={
                "checkpoint": checkpoint,
                "reason": "advisor returned no advice text",
                "provider": route.provider,
                "model": route.model,
            },
        )
        return None

    note = _record_note(
        db,
        run_id=run_id,
        checkpoint=checkpoint,
        advice=text,
        provider=route.provider,
        model=route.model,
    )
    advice_payload: dict[str, Any] = {
        "checkpoint": checkpoint,
        "call_id": call_id,
        "provider": route.provider,
        "model": route.model,
        "advice": text,
        "note_id": getattr(note, "note_id", None),
    }
    if signature is not None:
        advice_payload["signature"] = signature
    _record_event(
        db, run_id, ADVISOR_ADVICE_EVENT, attempt_id=attempt_id, payload=advice_payload
    )
    return AdvisorAdvice(
        checkpoint=checkpoint,
        advice=text,
        provider=route.provider,
        model=route.model,
        call_id=call_id,
        signature=signature,
        note=note,
    )


def _record_note(
    db: Any,
    *,
    run_id: str,
    checkpoint: str,
    advice: str,
    provider: str,
    model: str,
) -> Any | None:
    """Record the advice as a durable steering note on the owning job."""
    try:
        from saberops.jobs.store import insert_steering_note
        from saberops.preemption import find_job_for_run

        job = find_job_for_run(db, run_id)
        if job is None:
            return None
        body = f"ADVISOR ({checkpoint}, {provider}/{model}): {advice}"
        return insert_steering_note(db, job.job_id, body, source="advisor")
    except Exception as exc:  # noqa: BLE001 - the advice event still records it
        _LOG.warning("could not record advisor steering note: %s", exc)
        return None


def _route_for_run(db: Any, repo: Path | str) -> AdvisorRoute | None:
    try:
        return load_advisor_route(repo)
    except AdvisorConfigError as exc:
        _LOG.warning("advisor route is invalid: %s", exc)
        return None


def advisor_before_attempt(
    db: Any,
    *,
    repo: Path | str,
    run_id: str,
    registry: Any,
    attempt_id: str,
    attempts: Sequence[Attempt],
    plan_steps: Sequence[Any],
    base_sha: str,
    task: str,
    prior_worktree: Path | str | None = None,
    timeout_seconds: float = DEFAULT_ADVISOR_TIMEOUT_SECONDS,
) -> AdvisorAdvice | None:
    """Run at most one checkpoint before a worker attempt starts.

    The first attempt of a run gets ``before_plan``; a retry whose two most
    recent attempts failed identically gets ``repeat_failure`` exactly once per
    failure signature.  Routine retries produce nothing.
    """
    route = _route_for_run(db, repo)
    if route is None:
        return None
    if _call_count(db, run_id) >= route.call_budget:
        return None
    checkpoint: str | None = None
    signature: str | None = None
    if not _checkpoint_already_fired(db, run_id, CHECKPOINT_BEFORE_PLAN):
        if not attempts:
            checkpoint = CHECKPOINT_BEFORE_PLAN
    if checkpoint is None:
        repeated = _repeated_failure(attempts)
        if repeated is not None and not _checkpoint_already_fired(
            db, run_id, CHECKPOINT_REPEAT_FAILURE, signature=repeated
        ):
            checkpoint = CHECKPOINT_REPEAT_FAILURE
            signature = repeated
    if checkpoint is None:
        return None
    return _advise(
        db,
        repo=repo,
        run_id=run_id,
        registry=registry,
        route=route,
        checkpoint=checkpoint,
        attempt_id=attempt_id,
        task=task,
        plan_steps=plan_steps,
        attempts=attempts,
        base_sha=base_sha,
        prior_worktree=prior_worktree,
        signature=signature,
        timeout_seconds=timeout_seconds,
    )


def advisor_before_review(
    db: Any,
    *,
    repo: Path | str,
    run_id: str,
    registry: Any,
    attempt_id: str,
    attempts: Sequence[Attempt],
    plan_steps: Sequence[Any],
    base_sha: str,
    task: str,
    worktree_path: Path | str | None = None,
    timeout_seconds: float = DEFAULT_ADVISOR_TIMEOUT_SECONDS,
) -> AdvisorAdvice | None:
    """Run the single ``before_review`` checkpoint for this run."""
    route = _route_for_run(db, repo)
    if route is None:
        return None
    if _call_count(db, run_id) >= route.call_budget:
        return None
    if _checkpoint_already_fired(db, run_id, CHECKPOINT_BEFORE_REVIEW):
        return None
    return _advise(
        db,
        repo=repo,
        run_id=run_id,
        registry=registry,
        route=route,
        checkpoint=CHECKPOINT_BEFORE_REVIEW,
        attempt_id=attempt_id,
        task=task,
        plan_steps=plan_steps,
        attempts=attempts,
        base_sha=base_sha,
        prior_worktree=worktree_path,
        signature=None,
        timeout_seconds=timeout_seconds,
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
    notes_by_id: dict[int, Any] = {}
    try:
        from saberops.jobs.store import list_steering_notes

        for note in list_steering_notes(db, job.job_id):
            notes_by_id[int(note.note_id)] = note
    except Exception:  # noqa: BLE001 - a view never breaks
        notes_by_id = {}

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
            delivered_note = notes_by_id.get(int(payload.get("note_id") or 0))
            payload["delivered"] = bool(getattr(delivered_note, "delivered", False))
            payload["delivered_attempt_id"] = getattr(
                delivered_note, "delivered_attempt_id", None
            )
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
