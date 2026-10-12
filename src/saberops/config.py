"""One normalized run configuration shared by CLI and Web."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from saberops.authority import load_authority_policy
from saberops.classifier import classify_task_tier
from saberops.control_plane.capabilities import (
    Capability,
    normalize_required_capabilities,
)
from saberops.jobs.models import JobRole
from saberops.models import GateScratchMode, ReviewPolicyMode, Tier
from saberops.tooling.contracts import validate_logical_ref

RoutingMode = Literal["auto", "manual"]
ReviewMode = Literal["bounded", "max"]

_TIER_ORDER: tuple[Tier, ...] = (Tier.T1, Tier.T2, Tier.T3)
# Bounded default worker execution. These per-role defaults are the
# hard absolute deadline applied to every worker dispatch unless the owner
# explicitly supplies ``--worker-timeout`` (which remains authoritative).
# WORKER/INTEGRATOR keep the old T2 default (1200s); SURGEON keeps the old
# T3 default (1800s) so Surgeon output keeps today's top-tier review risk
# budget (ADR-0002 §11).
ROLE_WORKER_TIMEOUTS: dict[JobRole, float] = {
    JobRole.WORKER: 1200.0,
    JobRole.SURGEON: 1800.0,
    JobRole.INTEGRATOR: 1200.0,
}

# Soft inactivity expectation (seconds of quiet provider output before the
# worker is reported stalled/attention).  This is NEVER a kill boundary.
# Matches the supervisor's 300-second health rule so both layers agree on
# what "quiet" means.
WORKER_INACTIVITY_TIMEOUT: float = 300.0

DEFAULT_GATE_TIMEOUT: float = 300.0
DEFAULT_REVIEW_TIMEOUT: float = 1200.0
DEFAULT_REVIEW_MODE: ReviewMode = "bounded"
DEFAULT_REVIEW_LIMIT: int = 3

_REVIEW_MODES: tuple[str, ...] = ("bounded", "max")


def role_worker_timeout(role: JobRole | str) -> float:
    """Return the default hard deadline for a concrete routing role.

    This IS a termination boundary: unless the owner supplied an
    explicit ``--worker-timeout``, a worker dispatched on ``role`` is
    terminated through the fenced process-group mechanism once it exceeds
    this duration, and the attempt is classified TIMEOUT.  The soft
    inactivity expectation (``WORKER_INACTIVITY_TIMEOUT``) remains a
    monitoring/reporting signal only and never terminates a worker.
    """
    key = role if isinstance(role, JobRole) else JobRole(str(role).strip().upper())
    if key not in (JobRole.WORKER, JobRole.SURGEON, JobRole.INTEGRATOR):
        raise ValueError(
            f"no worker timeout for role {str(role)!r}: expected a routed role"
        )
    return ROLE_WORKER_TIMEOUTS[key]


def cap_tier(tier: Tier, maximum: Tier) -> Tier:
    """Apply an inclusive automatic-routing ceiling."""
    return _TIER_ORDER[min(_TIER_ORDER.index(tier), _TIER_ORDER.index(maximum))]


@dataclass(frozen=True)
class RunConfig:
    """Fully normalized launch policy; safe to persist as an event payload."""

    task: str
    target_repo: str
    routing_mode: RoutingMode = "auto"
    manual_tier: Tier | None = None
    max_auto_tier: Tier = Tier.T3
    training_allowed: bool = True
    provider_override: str | None = None
    model_override: str | None = None
    # Explicit owner emergency use of a RESERVE quota pool. Ordinary provider
    # and model overrides are intentionally separate and do not set this.
    reserve_override: bool = False
    required_capabilities: tuple[str, ...] = ()
    gate_enabled: bool = True
    review_disabled: bool = False
    workflow_mode: str = "custom"
    gate_command: str = "make gate"
    max_attempts: int = 2
    worker_timeout: float | None = None
    worker_timeout_explicit: bool = False
    worker_inactivity_timeout: float = WORKER_INACTIVITY_TIMEOUT
    gate_timeout: float = DEFAULT_GATE_TIMEOUT
    review_enabled: bool = False
    review_timeout: float = DEFAULT_REVIEW_TIMEOUT
    review_mode: ReviewMode = DEFAULT_REVIEW_MODE
    review_limit: int = DEFAULT_REVIEW_LIMIT
    # Project-scoped risk-adaptive review policy mode, FROZEN at
    # run creation.  ``RISK_ADAPTIVE`` is the canonical default; project
    # configuration may tighten to ``ALWAYS_REQUIRED`` (which forces an
    # independent semantic review even when the classifier would mark
    # the delta LOW).  Never allow weakening the hard minimums: there is
    # no ``NEVER_REQUIRED`` mode.  Later project policy changes affect
    # FUTURE runs only.
    review_policy_mode: ReviewPolicyMode = ReviewPolicyMode.RISK_ADAPTIVE
    # Frozen candidate-publication decision for this run: whether every
    # gate-passed candidate commit is published (non-force, exact-SHA) to the
    # target repository's ``origin`` as the Orch candidate branch for external
    # review.  Resolved once at run creation from the owner authority
    # (``auto_publish_candidate``) unless the owner explicitly overrides it
    # per run; retry/repair/review inherit this persisted decision and never
    # re-read live authority policy.  This is candidate visibility only --
    # never accepted-target/main publication.
    publish_candidate: bool = False
    tool_refs: tuple[str, ...] = ()
    # Autonomous execution is a run-scoped contract.  It contains
    # references and policy, never resolved secret material.
    autonomous_runtime: dict[str, Any] | None = None
    # Non-secret serialization of the effective owner
    # BindingRegistry frozen with the Run at admission.  Ordinary
    # exact-binding dispatch, retry, continuation, review repair, adoption
    # and cold recovery all resolve worker bindings from this frozen
    # registry, never from mutable live owner configuration; an owner
    # binding-store change after admission MUST NOT alter an existing Run's
    # executable profile / account / backend / quota-pool identity.
    # ``None`` means no owner bindings were present at admission (the
    # historical static/legacy path) or the value was not recorded for an
    # older Run; a binding-bearing candidate then fails closed
    # (``NO_REGISTRY``) instead of loading ambient owner state.  Only
    # non-secret identity references are stored -- never credential values.
    worker_binding_registry: dict[str, Any] | None = None
    # Project-scoped authoritative gate scratch backend preference,
    # FROZEN at run creation time.  Resolved once from the project's
    # persisted policy (or the global default when the project has none)
    # before the run is launched; retry/repair/review/acceptance always
    # inherit this frozen value and never re-read mutable project
    # authority.  The Gate Runner treats this as an execution-efficiency
    # preference at the lowest precedence band; it MUST NOT escalate
    # into governance, security, or acceptance semantics.
    gate_scratch_mode: GateScratchMode = GateScratchMode.DISK
    # Owner-selectable accept target branch, FROZEN at run creation.
    # An explicit project setting wins; otherwise the current symbolic
    # branch is resolved once at creation and frozen verbatim.  Legacy or
    # partial payloads carry "" (unknown); acceptance behavior for such
    # runs is unchanged (the AcceptEngine still reads live repo state).
    # Moved-HEAD acceptance is future work; the project-settings layer
    # establishes and freezes policy.
    accept_target_branch: str = ""
    # A linked repair run inherits a committed candidate, never a live provider session.
    continuation: dict[str, str] | None = None
    execution_mode: str = "single"
    max_parallel_workers: int = 3
    parallel_plan: tuple[dict[str, Any], ...] = ()
    parallel_parent_run_id: str | None = None
    parallel_task_index: int | None = None

    @property
    def initial_tier(self) -> Tier:
        """Return manual tier or deterministic auto classification capped by policy."""
        if self.routing_mode == "manual":
            assert self.manual_tier is not None
            return self.manual_tier
        return cap_tier(classify_task_tier(self.task).tier, self.max_auto_tier)

    @property
    def initial_role(self) -> JobRole:
        """Return the routing role the run starts on (ADR-0002 §11).

        SURGEON iff the run is manual with ``manual_tier == T3`` (the old
        T3 chain is exactly the SURGEON chain), else WORKER. Manual runs
        never escalate; auto runs start on WORKER and may escalate once
        to SURGEON. No new persisted config field: this derives from the
        frozen ``manual_tier``/``max_auto_tier`` back-compat fields.
        """
        if self.routing_mode == "manual" and self.manual_tier == Tier.T3:
            return JobRole.SURGEON
        return JobRole.WORKER

    @property
    def has_worker_deadline(self) -> bool:
        """True when the owner explicitly supplied an absolute runtime budget.

        A tier-derived default is still a deadline (see
        :meth:`worker_timeout_for_role`); this flag only reports whether the
        owner overrode it explicitly.
        """
        return (
            self.worker_timeout_explicit
            and self.worker_timeout is not None
            and self.worker_timeout > 0
        )

    def worker_timeout_for_role(self, role: JobRole | str) -> float:
        """Return the effective hard deadline for a worker dispatched on ``role``.

        When the owner supplied an explicit ``--worker-timeout``, that value
        is authoritative regardless of role.  Otherwise the role default
        from :data:`ROLE_WORKER_TIMEOUTS` applies: normal runs are
        always bounded and never depend on the monitor lifecycle alone.
        A degenerate non-positive explicit value falls back to the role
        default rather than leaving the run unbounded.
        """
        if self.has_worker_deadline:
            assert self.worker_timeout is not None
            return self.worker_timeout
        return role_worker_timeout(role)

    def as_dict(self) -> dict[str, object]:
        """Serialize enums into JSON-compatible values for durable run events."""
        data = asdict(self)
        data["manual_tier"] = self.manual_tier.value if self.manual_tier else None
        data["max_auto_tier"] = self.max_auto_tier.value
        return data


def _as_tier(value: Tier | str | None, *, default: Tier | None = None) -> Tier | None:
    if value is None or value == "":
        return default
    return value if isinstance(value, Tier) else Tier(str(value).upper())


def _reconstruct_gate_scratch_mode(data: dict[str, Any]) -> GateScratchMode:
    """Resolve a persisted gate-scratch mode, defaulting conservatively.

    Missing, empty, or non-string values resolve to the DISK default --
    the legacy contract never recorded a RAM preference, so replaying
    older runs with a current RAM setting would silently change their
    semantics.  A non-empty string that is not a recognized mode is also
    treated as DISK; the failure path here is documented and observable.
    """
    raw = data.get("gate_scratch_mode")
    if not raw:
        return GateScratchMode.DISK
    if not isinstance(raw, str):
        return GateScratchMode.DISK
    try:
        return GateScratchMode(raw.strip())
    except ValueError:
        return GateScratchMode.DISK


def _reconstruct_review_policy_mode(data: dict[str, Any]) -> ReviewPolicyMode:
    """Resolve a persisted review-policy mode, defaulting conservatively.

    Mirrors :func:`_reconstruct_gate_scratch_mode`.  Missing / empty /
    non-string values fall back to the canonical ``RISK_ADAPTIVE``
    default -- a legacy Run must never be re-classified into
    ``ALWAYS_REQUIRED`` by reconstruction (that would silently tighten
    the policy on legacy runs).  An unrecognized string is also
    ``RISK_ADAPTIVE`` rather than silently weakened to a non-existent
    mode.
    """
    raw = data.get("review_policy_mode")
    if not raw:
        return ReviewPolicyMode.RISK_ADAPTIVE
    if not isinstance(raw, str):
        return ReviewPolicyMode.RISK_ADAPTIVE
    try:
        return ReviewPolicyMode(raw.strip().upper())
    except ValueError:
        return ReviewPolicyMode.RISK_ADAPTIVE


def freeze_owner_binding_registry_payload() -> dict[str, Any] | None:
    """Freeze the effective owner BindingRegistry as non-secret durable material.

    Ordinary exact-binding execution must interpret a candidate's frozen
    ``binding_id`` against the binding registry that was effective at Run
    admission, not against whatever the owner binding store happens to
    contain later.  This loads the canonical owner binding
    document once and renders it through the existing
    :meth:`BindingRegistry.to_dict`; no second binding-store format and no
    secret material are introduced.

    Returns ``None`` when the owner has no configured bindings (the
    historical static/legacy path must not require a registry) or when the
    store is unreadable/corrupt -- a binding-bearing candidate then fails
    closed with ``NO_REGISTRY`` rather than falling back to ambient state.
    """
    from saberops.control_plane.binding_store import load_owner_bindings

    try:
        owner = load_owner_bindings()
    except Exception:  # noqa: BLE001 - corrupt owner state must not launch
        return None
    if not tuple(owner.registry.list_bindings()):
        return None
    return owner.registry.to_dict()


def resolve_run_config(
    *,
    task: str,
    target_repo: Path | str | None = None,
    repo_path: Path | str | None = None,
    routing_mode: str = "auto",
    manual_tier: Tier | str | None = None,
    max_auto_tier: Tier | str | None = None,
    training_allowed: bool = True,
    provider_override: str | None = None,
    model_override: str | None = None,
    reserve_override: bool = False,
    required_capabilities: tuple[str | Capability, ...] = (),
    gate_enabled: bool = True,
    review_disabled: bool = False,
    workflow_mode: str = "custom",
    gate_command: str | None = None,
    max_attempts: int = 2,
    worker_timeout: float | None = None,
    gate_timeout: float | None = None,
    review_enabled: bool | None = None,
    review: bool | None = None,
    review_timeout: float | None = None,
    review_mode: str = "bounded",
    review_limit: int = DEFAULT_REVIEW_LIMIT,
    # Project-scoped risk-adaptive review policy mode, FROZEN at
    # run creation.  ``None`` falls back to the canonical default; a
    # malformed value raises.
    review_policy_mode: ReviewPolicyMode | str | None = None,
    publish_candidate: bool | None = None,
    tool_refs: tuple[str, ...] = (),
    # ``tier`` keeps legacy callers source-compatible while normalizing them.
    tier: Tier | str | None = None,
    gate_scratch_mode: GateScratchMode | str | None = None,
    # Frozen accept target branch.  The shared run-creation
    # authority always supplies an explicit value; direct low-level
    # callers may omit it (legacy "" = unknown, acceptance unchanged).
    accept_target_branch: str | None = None,
) -> RunConfig:
    """Normalize all public launch inputs into the one canonical policy object."""
    if type(gate_enabled) is not bool or type(review_disabled) is not bool:
        raise ValueError("Gate and review authorization must be boolean")
    target = target_repo if target_repo is not None else repo_path
    if target is None:
        raise ValueError("target repository is required")
    raw_mode = routing_mode.lower().strip()
    raw_review_mode = review_mode.lower().strip()
    if raw_review_mode not in _REVIEW_MODES:
        raise ValueError("review_mode must be 'bounded' or 'max'")
    if review_limit < 1:
        raise ValueError("review_limit must be >= 1")
    if review_policy_mode is None:
        resolved_review_policy_mode = ReviewPolicyMode.RISK_ADAPTIVE
    elif isinstance(review_policy_mode, ReviewPolicyMode):
        resolved_review_policy_mode = review_policy_mode
    else:
        try:
            resolved_review_policy_mode = ReviewPolicyMode(str(review_policy_mode).strip().upper())
        except ValueError as exc:
            known = ", ".join(mode.value for mode in ReviewPolicyMode)
            raise ValueError(
                f"review_policy_mode must be one of {known}; got {review_policy_mode!r}"
            ) from exc
    legacy_tier = _as_tier(tier)
    manual = _as_tier(manual_tier, default=legacy_tier)
    if raw_mode not in ("auto", "manual"):
        raise ValueError("routing_mode must be 'auto' or 'manual'")
    if (legacy_tier is not None or manual is not None) and routing_mode == "auto":
        raw_mode = "manual"
    if raw_mode == "manual" and manual is None:
        raise ValueError("manual routing requires a tier override")
    ceiling = _as_tier(max_auto_tier, default=Tier.T3)
    assert ceiling is not None
    task_clean = task.strip()
    if raw_mode == "manual":
        assert manual is not None
    explicit_worker = worker_timeout is not None
    enabled = review_enabled if review_enabled is not None else bool(review)
    normalized_tool_refs = tuple(sorted({validate_logical_ref(ref) for ref in tool_refs}))
    if gate_scratch_mode is None:
        resolved_gate_scratch_mode = GateScratchMode.DISK
    elif isinstance(gate_scratch_mode, GateScratchMode):
        resolved_gate_scratch_mode = gate_scratch_mode
    else:
        try:
            resolved_gate_scratch_mode = GateScratchMode(str(gate_scratch_mode).strip())
        except ValueError as exc:
            known = ", ".join(mode.value for mode in GateScratchMode)
            raise ValueError(
                f"gate_scratch_mode must be one of {known}; got {gate_scratch_mode!r}"
            ) from exc
    return RunConfig(
        task=task_clean,
        target_repo=str(Path(target).resolve()),
        routing_mode=raw_mode,  # type: ignore[arg-type]
        manual_tier=manual if raw_mode == "manual" else None,
        max_auto_tier=ceiling,
        training_allowed=training_allowed,
        provider_override=provider_override.strip() if provider_override else None,
        model_override=model_override.strip() if model_override else None,
        reserve_override=bool(reserve_override),
        required_capabilities=normalize_required_capabilities(required_capabilities),
        gate_enabled=gate_enabled,
        review_disabled=review_disabled,
        workflow_mode=workflow_mode,
        gate_command=(gate_command or "make gate").strip() if gate_enabled else "",
        max_attempts=max_attempts,
        worker_timeout=(float(worker_timeout) if worker_timeout is not None else None),
        worker_timeout_explicit=explicit_worker,
        gate_timeout=float(gate_timeout) if gate_timeout is not None else DEFAULT_GATE_TIMEOUT,
        review_enabled=enabled,
        review_timeout=(
            float(review_timeout) if review_timeout is not None else DEFAULT_REVIEW_TIMEOUT
        ),
        review_mode=raw_review_mode,  # type: ignore[arg-type]
        review_limit=review_limit,
        review_policy_mode=resolved_review_policy_mode,
        # Freeze the candidate-publication decision at run creation: an
        # explicit owner override wins; otherwise the persisted authority
        # policy decides.  Never re-derived later for an already-created run.
        publish_candidate=(
            bool(publish_candidate)
            if publish_candidate is not None
            else load_authority_policy().auto_publish_candidate
        ),
        tool_refs=normalized_tool_refs,
        gate_scratch_mode=resolved_gate_scratch_mode,
        accept_target_branch=(str(accept_target_branch).strip() if accept_target_branch else ""),
    )


def reconstruct_run_config(data: dict[str, Any]) -> RunConfig:
    """Reconstruct a normalized RunConfig from its persisted ``as_dict()`` payload.

    This is the inverse of :meth:`RunConfig.as_dict` and is used to faithfully
    restore routing policy on retry (routing mode, auto ceiling, provider/model
    overrides, attempt limits, and timeout policy). Missing keys fall back to the
    canonical defaults so legacy or partial payloads remain readable.
    """
    raw_mode = str(data.get("routing_mode", "auto")).lower().strip()
    if raw_mode not in ("auto", "manual"):
        raise ValueError("routing_mode must be 'auto' or 'manual'")
    raw_review_mode = str(data.get("review_mode", DEFAULT_REVIEW_MODE)).lower().strip()
    if raw_review_mode not in _REVIEW_MODES:
        raw_review_mode = DEFAULT_REVIEW_MODE
    raw_review_limit = int(data.get("review_limit", DEFAULT_REVIEW_LIMIT))
    if raw_review_limit < 1:
        raw_review_limit = DEFAULT_REVIEW_LIMIT
    manual_tier = _as_tier(data.get("manual_tier"))
    max_auto_tier = _as_tier(data.get("max_auto_tier"), default=Tier.T3)
    assert max_auto_tier is not None
    config = RunConfig(
        task=str(data.get("task", "")).strip(),
        target_repo=str(data.get("target_repo", "")),
        routing_mode=raw_mode,  # type: ignore[arg-type]
        manual_tier=manual_tier if raw_mode == "manual" else None,
        max_auto_tier=max_auto_tier,
        training_allowed=bool(data.get("training_allowed", True)),
        provider_override=data.get("provider_override"),
        model_override=data.get("model_override"),
        reserve_override=bool(data.get("reserve_override", False)),
        required_capabilities=normalize_required_capabilities(
            tuple(data.get("required_capabilities", ()))
        ),
        gate_enabled=_validation_bool(data, "gate_enabled", True),
        review_disabled=_validation_bool(data, "review_disabled", False),
        workflow_mode=str(data.get("workflow_mode", "custom")),
        gate_command=str(data.get("gate_command", "make gate")),
        max_attempts=int(data.get("max_attempts", 2)),
        worker_timeout=(
            float(data["worker_timeout"]) if data.get("worker_timeout") is not None else None
        ),
        worker_timeout_explicit=bool(data.get("worker_timeout_explicit", False)),
        worker_inactivity_timeout=float(
            data.get("worker_inactivity_timeout", WORKER_INACTIVITY_TIMEOUT)
        ),
        gate_timeout=float(data.get("gate_timeout", DEFAULT_GATE_TIMEOUT)),
        review_enabled=bool(data.get("review_enabled", False)),
        review_timeout=float(data.get("review_timeout", DEFAULT_REVIEW_TIMEOUT)),
        review_mode=raw_review_mode,  # type: ignore[arg-type]
        review_limit=raw_review_limit,
        # A missing / malformed ``review_policy_mode`` falls back
        # to the canonical RISK_ADAPTIVE default -- a legacy persisted
        # Run must never be re-classified as ``ALWAYS_REQUIRED`` by
        # reconstruction.  Reconstruction is also never downgraded to a
        # ``NEVER_REQUIRED`` mode (none exists), so a malformed value
        # becomes RISK_ADAPTIVE instead of silently weakening the policy.
        review_policy_mode=_reconstruct_review_policy_mode(data),
        # Legacy or partial payloads have no recorded decision: stay
        # conservative (no automatic remote mutation).
        publish_candidate=bool(data.get("publish_candidate", False)),
        tool_refs=tuple(
            sorted({validate_logical_ref(str(ref)) for ref in (data.get("tool_refs") or ())})
        ),
        autonomous_runtime=(
            dict(data["autonomous_runtime"])
            if isinstance(data.get("autonomous_runtime"), Mapping)
            else None
        ),
        # Reconstruct the run-frozen worker binding registry
        # verbatim from durable state.  A legacy/partial payload has no frozen
        # registry and stays ``None`` -- an already-admitted binding-bearing
        # Run must then fail closed rather than fabricate authority from the
        # current owner binding store.
        worker_binding_registry=(
            dict(data["worker_binding_registry"])
            if isinstance(data.get("worker_binding_registry"), Mapping)
            else None
        ),
        # Legacy/partial payloads have no frozen gate-scratch mode;
        # stay on the conservative DISK default.  The exact mode was
        # never recorded before this tranche -- replaying such a run with
        # the current RAM setting would silently change its semantics.
        gate_scratch_mode=_reconstruct_gate_scratch_mode(data),
        # Legacy payloads predate the frozen target branch; ""
        # preserves their acceptance behavior (live repo state).
        accept_target_branch=str(data.get("accept_target_branch", "") or ""),
        continuation=_reconstruct_continuation(data.get("continuation")),
        execution_mode=str(data.get("execution_mode", "single")),
        max_parallel_workers=data.get("max_parallel_workers", 3),
        parallel_plan=tuple(data.get("parallel_plan", ())),
        parallel_parent_run_id=data.get("parallel_parent_run_id"),
        parallel_task_index=data.get("parallel_task_index"),
    )
    from saberops.parallel_work import validate_work_config
    validate_work_config(config)
    return config


def _reconstruct_continuation(raw: object) -> dict[str, str] | None:
    if raw is None:
        return None
    if (not isinstance(raw, dict)
        or set(raw) != {"parent_run_id", "attempt_id", "candidate_sha", "base_sha"}
        or not all(isinstance(value, str) and value for value in raw.values())):
        raise ValueError("Invalid continuation reference")
    return dict(raw)


def _validation_bool(data: dict[str, Any], name: str, default: bool) -> bool:
    value = data.get(name, default)
    if type(value) is not bool:
        raise ValueError(f"Invalid frozen {name}")
    return value
