"""Single run-creation authority shared by Web and CLI.

Both owner surfaces (``POST /runs`` in :mod:`saberops.web` and
``orch run`` in :mod:`saberops.cli`) resolve the effective run policy
through :func:`resolve_effective_run_config` before any supervisor or
worker/model execution.  There is exactly one precedence table, defined
here; no per-surface duplication is permitted.

Frozen per run (at minimum):

* effective gate command
* effective gate-scratch mode
* the mandatory independent-review guarantee (or the owner's manual switch)
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

from saberops.config import (
    DEFAULT_REVIEW_LIMIT,
    RunConfig,
    resolve_run_config,
)
from saberops.control_plane.capabilities import Capability
from saberops.gate_runner import GateScratchMode, GateScratchPolicyError
from saberops.jobs.models import Effort
from saberops.models import Tier
from saberops.project_settings import (
    DataPolicy,
    ProjectSettings,
    ProjectSettingsError,
    detect_authoritative_gate,
    load_project_settings,
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

#: F19 run kinds.  A scout is a read-only report run on the build route.
SCOUT_RUN_KIND: str = "scout"
BUILD_RUN_KIND: str = "build"


def _resolve_run_kind(value: str | None) -> str:
    cleaned = str(value or BUILD_RUN_KIND).strip().lower()
    if cleaned not in (BUILD_RUN_KIND, SCOUT_RUN_KIND):
        raise ValueError(
            f"kind must be {BUILD_RUN_KIND!r} or {SCOUT_RUN_KIND!r}; got {value!r}"
        )
    return cleaned


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
    accept_target_branch: str | None = None
    gate_scratch_mode: GateScratchMode | str | None = None
    # Request-level run tuning (kept for compatibility; centralized here).
    routing_mode: str = "auto"
    # F19: "build" (default) or "scout".  A scout run is read-only and ends
    # with a report, never a candidate; roles are unchanged.
    kind: str = "build"
    #: Planner-recorded judgement (H1): ``routine`` (default) or ``hard``.
    #: Never a provider, model, tier or route name.
    difficulty: str | None = None
    #: Planner-recorded judgement (H20): the candidate this run produces is
    #: consequential, so away and critical-only park it for the owner.
    consequential: bool = False
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
    #: The owner's one review switch.  Only ``orch run --no-review`` sets it;
    #: no scheduler job, plan lane or MCP submission can.
    review_disabled: bool = False
    review_focus: str | None = None
    review_timeout: float | None = None
    review_mode: str = "bounded"
    review_limit: int = DEFAULT_REVIEW_LIMIT
    publish_candidate: bool | None = None
    tier: Tier | str | None = None
    effort: str | None = None


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
    AcceptEngine keeps reading live repo state, future rebase/recovery work owns moved-HEAD).
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
    gate = "" if not snapshot.gate_enabled else _resolve_gate_from_settings(
        repo_path=request.repo_path,
        explicit_gate=request.gate_command,
        settings=snapshot,
    )
    # The stored training/data-use bit is historical evidence and never
    # restricts admission (H11).
    training_allowed = True
    gate_scratch_mode = resolve_effective_gate_scratch_mode(
        repo_path=request.repo_path, explicit_mode=request.gate_scratch_mode
    )
    target_branch = _resolve_target_branch_from_settings(
        repo_path=request.repo_path,
        explicit_branch=request.accept_target_branch,
        settings=snapshot,
    )
    review_disabled = bool(request.review_disabled)
    review_enabled = request.review_enabled
    if review_enabled is None:
        review_enabled = True
    # Review is mandatory for anything that can be accepted.  A scout never
    # produces an acceptability candidate, and the owner's direct manual
    # switch is the only other way the guarantee is absent.
    try:
        kind = _resolve_run_kind(request.kind)
    except ValueError as exc:
        raise RunCreationError(str(exc)) from exc
    required_capabilities = tuple(request.required_capabilities)
    effort = request.effort
    if kind == SCOUT_RUN_KIND:
        # A scout is read-only by construction, on the build route: the
        # capability requirement is what makes admission pick a provider whose
        # CLI can enforce it.  Gate, review and publication do not apply to a
        # report, so they are frozen off instead of left to project policy.
        # An unstated effort is the cheap default (H8, ADR-0035): a scout is
        # ``effort: low`` exactly like a job of the same kind.
        required_capabilities = tuple(
            sorted({*required_capabilities, Capability.READ_ONLY.value})
        )
        if not effort:
            effort = Effort.low.value
    try:
        config = resolve_run_config(
            task=task,
            repo_path=request.repo_path,
            routing_mode=request.routing_mode,
            kind=kind,
            difficulty=request.difficulty,
            consequential=request.consequential,
            manual_tier=request.manual_tier,
            max_auto_tier=request.max_auto_tier,
            training_allowed=training_allowed,
            provider_override=request.provider_override,
            model_override=request.model_override,
            reserve_override=request.reserve_override,
            required_capabilities=required_capabilities,
            gate_enabled=snapshot.gate_enabled and kind != SCOUT_RUN_KIND,
            review_disabled=review_disabled or kind == SCOUT_RUN_KIND,
            gate_command=gate,
            max_attempts=1 if kind == SCOUT_RUN_KIND else request.max_attempts,
            worker_timeout=request.worker_timeout,
            gate_timeout=request.gate_timeout,
            review_enabled=review_enabled and not review_disabled and kind != SCOUT_RUN_KIND,
            review_focus=request.review_focus,
            review_timeout=request.review_timeout,
            review_mode=request.review_mode,
            review_limit=request.review_limit,
            publish_candidate=(
                False if kind == SCOUT_RUN_KIND else request.publish_candidate
            ),
            tier=request.tier,
            effort=effort,
            gate_scratch_mode=gate_scratch_mode,
            accept_target_branch=target_branch,
        )
    except ValueError as exc:
        raise RunCreationError(str(exc)) from exc
    return config


def effective_policy_summary(repo_path: Path | str) -> dict[str, object]:
    """Return the owner-facing effective policy for ``repo_path``.

    Used by Web/CLI project-settings surfaces to show the values a new
    run would freeze.  Never raises for missing decisions (they report
    safe defaults); corrupt project authority surfaces as an
    ``"error"`` entry so the surface can refuse to render.
    """
    from saberops.gate_runner import load_project_gate_scratch_policy

    try:
        settings = load_project_settings(repo_path)
    except ProjectSettingsError as exc:
        return {"error": str(exc)}
    try:
        scratch_mode = load_project_gate_scratch_policy(repo_path)
    except Exception as exc:  # noqa: BLE001 - surface must render the failure
        return {"error": f"Project gate-scratch policy is invalid: {exc}"}
    training_allowed = True
    data_policy = DataPolicy.ALLOWED
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
        "gate_enabled": settings.gate_enabled,
        "review_mandatory": True,
        "gate_command": gate_configured,
        "gate_configured": gate_configured is not None,
        "gate_detected": gate_detected,
        "gate_effective": gate_configured or gate_detected,
        "data_policy": data_policy.value,
        "data_policy_label": ("Allowed" if data_policy is DataPolicy.ALLOWED else "Denied"),
        "data_policy_explicit": settings.data_policy is not None,
        "training_allowed": training_allowed,
        "gate_scratch_mode": scratch_mode.value,
        "accept_target_branch": settings.accept_target_branch,
        "accept_target_branch_explicit": settings.accept_target_branch is not None,
        "accept_target_branch_effective": effective_branch,
        "scout_route": settings.scout_route or "cheap",
        "scout_route_explicit": settings.scout_route is not None,
    }


__all__ = [
    "GATE_REQUIRED_MESSAGE",
    "TARGET_BRANCH_REQUIRED_MESSAGE",
    "RunCreationError",
    "RunRequest",
    "effective_policy_summary",
    "resolve_effective_gate_scratch_mode",
    "resolve_effective_run_config",
    "resolve_effective_target_branch",
]
