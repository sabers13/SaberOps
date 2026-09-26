"""R2-A single run-creation authority shared by Web and CLI.

Both owner surfaces (``POST /runs`` in :mod:`saberops.web` and
``orch run`` in :mod:`saberops.cli`) resolve the effective run policy
through :func:`resolve_effective_run_config` before any supervisor or
worker/model execution.  There is exactly one precedence table, defined
here; no per-surface duplication is permitted.

Frozen per run (at minimum):

* effective gate command
* effective gate-scratch mode
* effective review policy
* effective data/training policy
* effective accept target branch

The result is an ordinary :class:`~saberops.config.RunConfig` whose
``as_dict()`` payload is suitable for restart/recovery without
re-reading mutable project settings.  An already-created Run is never
rewritten when project settings later change.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.config import (
    DEFAULT_REVIEW_LIMIT,
    RunConfig,
    resolve_run_config,
)
from saberops.control_plane.capabilities import Capability
from saberops.gate_runner import GateScratchMode, GateScratchPolicyError
from saberops.models import ReviewPolicyMode, Tier
from saberops.project_settings import (
    DataPolicy,
    ProjectSettings,
    ProjectSettingsError,
    detect_authoritative_gate,
    load_project_settings,
)
from saberops.project_settings import (
    resolve_effective_training_allowed as _settings_training_allowed,
)

GATE_REQUIRED_MESSAGE: str = (
    "This project has no gate configured. Set the project gate first "
    "(for example: orch project set-gate --repo <path> '<command>'), "
    "or add a 'gate:' target to the project's Makefile. "
    "SaberOps never runs an implicit default gate."
)

TARGET_BRANCH_REQUIRED_MESSAGE: str = (
    "Cannot determine the accept target branch: the repository is in "
    "detached HEAD state and no explicit target branch is configured. "
    "Set one first (for example: orch project set-target-branch "
    "--repo <path> <branch>)."
)


class RunCreationError(RuntimeError):
    """Fail-closed error raised before any worker/model execution."""


@dataclass(frozen=True)
class RunRequest:
    """Legitimate explicit per-run options for one new project run.

    ``None`` on an owner-policy field means "not specified for this run:
    follow persisted project policy".  This is what lets Web and CLI
    share one precedence table: an empty gate box and an omitted
    ``--gate`` flag both arrive here as ``None``.
    """

    task: str
    repo_path: Path | str
    # Owner-policy dimensions: None = follow project settings.
    gate_command: str | None = None
    training_allowed: bool | None = None
    review_policy: ReviewPolicyMode | str | None = None
    accept_target_branch: str | None = None
    gate_scratch_mode: GateScratchMode | str | None = None
    # Request-level run tuning (kept for compatibility; centralized here).
    routing_mode: str = "auto"
    manual_tier: Tier | str | None = None
    max_auto_tier: Tier | str | None = None
    provider_override: str | None = None
    model_override: str | None = None
    reserve_override: bool = False
    required_capabilities: tuple[str | Capability, ...] = ()
    max_attempts: int = 2
    worker_timeout: float | None = None
    gate_timeout: float | None = None
    review_enabled: bool | None = None
    review: bool | None = None
    review_timeout: float | None = None
    review_mode: str = "bounded"
    review_limit: int = DEFAULT_REVIEW_LIMIT
    publish_candidate: bool | None = None
    tool_refs: tuple[str, ...] = ()
    gateway_frozen_config: dict[str, Any] | None = None
    gateway_frozen_digest: str | None = None
    tier: Tier | str | None = None


def _clean_gate(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _clean_branch(value: str | None) -> str | None:
    if value is None:
        return None
    text = value.strip()
    return text or None


def _resolve_gate_from_settings(
    *,
    repo_path: Path | str,
    explicit_gate: str | None,
    settings: ProjectSettings,
) -> str:
    """Resolve gate from one immutable project-settings snapshot."""
    cleaned = _clean_gate(explicit_gate)
    if cleaned is not None:
        return cleaned
    if settings.gate_command is not None:
        return settings.gate_command
    detected = detect_authoritative_gate(repo_path)
    if detected is not None:
        return detected
    raise RunCreationError(GATE_REQUIRED_MESSAGE)


def resolve_effective_gate_command(
    *,
    repo_path: Path | str,
    explicit_gate: str | None,
    settings: ProjectSettings | None = None,
) -> str:
    """Resolve the effective gate command or raise :class:`RunCreationError`.

    Precedence: explicit per-run gate > persisted project gate >
    narrow deterministic detection (Makefile ``gate:`` target).
    There is deliberately no silent ``make gate`` fallback: when none
    of the three yields a gate, creation fails before any
    worker/model execution with an actionable message.

    When ``settings`` is supplied, that immutable snapshot is used
    directly; otherwise the persisted project settings are loaded once
    for this standalone call.
    """
    if settings is not None:
        snapshot = settings
    else:
        try:
            snapshot = load_project_settings(repo_path)
        except ProjectSettingsError as exc:
            raise RunCreationError(str(exc)) from exc
    return _resolve_gate_from_settings(
        repo_path=repo_path, explicit_gate=explicit_gate, settings=snapshot
    )


def _resolve_training_from_settings(
    *,
    repo_path: Path | str,
    explicit_training_allowed: bool | None,
    settings: ProjectSettings,
) -> bool:
    """Resolve training bit from one immutable project-settings snapshot."""
    if explicit_training_allowed is not None:
        return bool(explicit_training_allowed)
    return _settings_training_allowed(settings, repo_path=repo_path)


def resolve_effective_training_allowed(
    *,
    repo_path: Path | str,
    explicit_training_allowed: bool | None,
    settings: ProjectSettings | None = None,
) -> bool:
    """Resolve the effective training/data bit with the R2-A safe default.

    An explicit per-run decision wins; otherwise the persisted project
    data policy applies; an unset policy resolves conservatively to
    Denied (fresh private/unknown projects are never Allowed by
    default).  The resolved bit is what freezes into
    ``RunConfig.training_allowed``.

    When ``settings`` is supplied, that immutable snapshot is used
    directly; otherwise the persisted project settings are loaded once
    for this standalone call.
    """
    if explicit_training_allowed is not None:
        return bool(explicit_training_allowed)
    if settings is not None:
        snapshot = settings
    else:
        try:
            snapshot = load_project_settings(repo_path)
        except ProjectSettingsError as exc:
            raise RunCreationError(str(exc)) from exc
    return _settings_training_allowed(snapshot, repo_path=repo_path)


def resolve_effective_review_policy_mode(
    *,
    repo_path: Path | str,
    explicit_run_mode: ReviewPolicyMode | str | None,
) -> ReviewPolicyMode:
    """Resolve the effective review policy via the tightening-only merge.

    Delegates to the canonical
    :func:`saberops.review_adaptive.resolve_effective_review_policy_mode`
    over the canonical persisted project mode: this authority composes
    the existing store, never a divergent copy.
    """
    from saberops.review_adaptive import (
        ReviewPolicyError,
        load_project_review_policy,
    )
    from saberops.review_adaptive import (
        resolve_effective_review_policy_mode as canonical_merge,
    )

    try:
        project_mode = load_project_review_policy(repo_path)
    except ReviewPolicyError as exc:
        raise RunCreationError(str(exc)) from exc
    if explicit_run_mode is None:
        explicit: ReviewPolicyMode | None = None
    elif isinstance(explicit_run_mode, ReviewPolicyMode):
        explicit = explicit_run_mode
    else:
        token = str(explicit_run_mode).strip().upper().replace("-", "_")
        try:
            explicit = ReviewPolicyMode(token)
        except ValueError as exc:
            known = ", ".join(m.value for m in ReviewPolicyMode)
            raise RunCreationError(
                f"review policy must be one of {known}; got {explicit_run_mode!r}"
            ) from exc
    return canonical_merge(project_mode=project_mode, explicit_run_mode=explicit)


def resolve_effective_gate_scratch_mode(
    *,
    repo_path: Path | str,
    explicit_mode: GateScratchMode | str | None,
) -> GateScratchMode:
    """Resolve the effective gate-scratch mode from the canonical store.

    An explicit per-run mode wins when supplied; otherwise the
    canonical persisted project policy applies (missing file = DISK).
    A corrupt project authority fails closed.
    """
    if explicit_mode is not None:
        if isinstance(explicit_mode, GateScratchMode):
            return explicit_mode
        try:
            return GateScratchMode(str(explicit_mode).strip())
        except ValueError as exc:
            known = ", ".join(m.value for m in GateScratchMode)
            raise RunCreationError(
                f"gate-scratch mode must be one of {known}; got {explicit_mode!r}"
            ) from exc
    from saberops.gate_runner import load_project_gate_scratch_policy

    try:
        return load_project_gate_scratch_policy(repo_path)
    except GateScratchPolicyError as exc:
        raise RunCreationError(str(exc)) from exc


def _resolve_target_branch_from_settings(
    *,
    repo_path: Path | str,
    explicit_branch: str | None,
    settings: ProjectSettings,
) -> str:
    """Resolve target branch from one immutable project-settings snapshot."""
    cleaned = _clean_branch(explicit_branch)
    if cleaned is not None:
        return cleaned
    if settings.accept_target_branch is not None:
        return settings.accept_target_branch
    from saberops.git import GitManager

    current = GitManager.get_current_branch(repo_path)
    if current is not None and current.strip():
        return current.strip()
    raise RunCreationError(TARGET_BRANCH_REQUIRED_MESSAGE)


def resolve_effective_target_branch(
    *,
    repo_path: Path | str,
    explicit_branch: str | None,
    settings: ProjectSettings | None = None,
) -> str:
    """Resolve and freeze the accept target branch for a new run.

    An explicit per-run branch wins; otherwise an explicit persisted
    project setting wins; otherwise the current symbolic branch is
    resolved once at creation and frozen verbatim (compatibility: the
    AcceptEngine keeps reading live repo state, R4 owns moved-HEAD).
    A detached HEAD with no explicit setting fails with an actionable
    message rather than freezing a guess.

    When ``settings`` is supplied, that immutable snapshot is used
    directly; otherwise the persisted project settings are loaded once
    for this standalone call.
    """
    if settings is not None:
        snapshot = settings
    else:
        try:
            snapshot = load_project_settings(repo_path)
        except ProjectSettingsError as exc:
            raise RunCreationError(str(exc)) from exc
    return _resolve_target_branch_from_settings(
        repo_path=repo_path, explicit_branch=explicit_branch, settings=snapshot
    )


def resolve_effective_run_config(request: RunRequest) -> RunConfig:
    """Resolve one frozen :class:`RunConfig` from project + request policy.

    This is the ONE run-creation authority for Web and CLI.  It loads
    ``project-settings.json`` exactly once for the Run being created
    and composes gate, data policy, and target branch from that same
    immutable snapshot, so a mid-resolution settings change can never
    freeze a hybrid policy.  The review and gate-scratch authorities
    remain separate and are each read once.  Nothing after this point
    re-reads mutable project settings for the created run.
    """
    task = request.task.strip()
    if not task:
        raise RunCreationError("Task is required.")
    try:
        snapshot = load_project_settings(request.repo_path)
    except ProjectSettingsError as exc:
        raise RunCreationError(str(exc)) from exc
    gate = _resolve_gate_from_settings(
        repo_path=request.repo_path,
        explicit_gate=request.gate_command,
        settings=snapshot,
    )
    training_allowed = _resolve_training_from_settings(
        repo_path=request.repo_path,
        explicit_training_allowed=request.training_allowed,
        settings=snapshot,
    )
    review_policy_mode = resolve_effective_review_policy_mode(
        repo_path=request.repo_path, explicit_run_mode=request.review_policy
    )
    gate_scratch_mode = resolve_effective_gate_scratch_mode(
        repo_path=request.repo_path, explicit_mode=request.gate_scratch_mode
    )
    target_branch = _resolve_target_branch_from_settings(
        repo_path=request.repo_path,
        explicit_branch=request.accept_target_branch,
        settings=snapshot,
    )
    review_enabled = request.review_enabled
    if review_enabled is None:
        review_enabled = bool(request.review)
    try:
        return resolve_run_config(
            task=task,
            repo_path=request.repo_path,
            routing_mode=request.routing_mode,
            manual_tier=request.manual_tier,
            max_auto_tier=request.max_auto_tier,
            training_allowed=training_allowed,
            provider_override=request.provider_override,
            model_override=request.model_override,
            reserve_override=request.reserve_override,
            required_capabilities=request.required_capabilities,
            gate_command=gate,
            max_attempts=request.max_attempts,
            worker_timeout=request.worker_timeout,
            gate_timeout=request.gate_timeout,
            review_enabled=review_enabled,
            review_timeout=request.review_timeout,
            review_mode=request.review_mode,
            review_limit=request.review_limit,
            review_policy_mode=review_policy_mode,
            publish_candidate=request.publish_candidate,
            tool_refs=request.tool_refs,
            gateway_frozen_config=request.gateway_frozen_config,
            gateway_frozen_digest=request.gateway_frozen_digest,
            tier=request.tier,
            gate_scratch_mode=gate_scratch_mode,
            accept_target_branch=target_branch,
        )
    except ValueError as exc:
        raise RunCreationError(str(exc)) from exc


def effective_policy_summary(repo_path: Path | str) -> dict[str, object]:
    """Return the owner-facing effective policy for ``repo_path``.

    Used by Web/CLI project-settings surfaces to show the values a new
    run would freeze.  Never raises for missing decisions (they report
    safe defaults); corrupt project authority surfaces as an
    ``"error"`` entry so the surface can refuse to render.
    """
    from saberops.gate_runner import load_project_gate_scratch_policy
    from saberops.review_adaptive import load_project_review_policy

    try:
        settings = load_project_settings(repo_path)
    except ProjectSettingsError as exc:
        return {"error": str(exc)}
    try:
        review_mode = load_project_review_policy(repo_path)
    except Exception as exc:  # noqa: BLE001 - surface must render the failure
        return {"error": f"Project review policy is invalid: {exc}"}
    try:
        scratch_mode = load_project_gate_scratch_policy(repo_path)
    except Exception as exc:  # noqa: BLE001 - surface must render the failure
        return {"error": f"Project gate-scratch policy is invalid: {exc}"}
    try:
        from saberops.project import bind_project_state_dir, compute_project_identity
        from saberops.review_adaptive import project_review_policy_path

        _state_dir = bind_project_state_dir(compute_project_identity(repo_path))
        review_explicit = project_review_policy_path(_state_dir).exists()
    except Exception:  # noqa: BLE001 - display hint only; policy already resolved
        review_explicit = False
    training_allowed = _settings_training_allowed(settings, repo_path=repo_path)
    data_policy = (
        settings.data_policy
        if settings.data_policy is not None
        else (DataPolicy.ALLOWED if training_allowed else DataPolicy.DENIED)
    )
    gate_configured = settings.gate_command
    gate_detected = detect_authoritative_gate(repo_path) if gate_configured is None else None
    from saberops.git import GitManager

    live_branch: str | None
    try:
        live_branch = GitManager.get_current_branch(repo_path)
    except Exception:  # noqa: BLE001 - a best-effort display hint only
        live_branch = None
    effective_branch = (
        settings.accept_target_branch if settings.accept_target_branch is not None else live_branch
    )
    return {
        "gate_command": gate_configured,
        "gate_configured": gate_configured is not None,
        "gate_detected": gate_detected,
        "gate_effective": gate_configured or gate_detected,
        "data_policy": data_policy.value,
        "data_policy_label": (
            "Allowed" if data_policy is DataPolicy.ALLOWED else "Denied"
        ),
        "data_policy_explicit": settings.data_policy is not None,
        "training_allowed": training_allowed,
        "review_policy_mode": review_mode.value,
        "review_policy_explicit": review_explicit,
        "gate_scratch_mode": scratch_mode.value,
        "accept_target_branch": settings.accept_target_branch,
        "accept_target_branch_explicit": settings.accept_target_branch is not None,
        "accept_target_branch_effective": effective_branch,
    }


__all__ = [
    "GATE_REQUIRED_MESSAGE",
    "TARGET_BRANCH_REQUIRED_MESSAGE",
    "RunCreationError",
    "RunRequest",
    "effective_policy_summary",
    "resolve_effective_gate_command",
    "resolve_effective_gate_scratch_mode",
    "resolve_effective_review_policy_mode",
    "resolve_effective_run_config",
    "resolve_effective_target_branch",
    "resolve_effective_training_allowed",
]
