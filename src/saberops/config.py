"""One normalized run configuration shared by CLI and Web."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, Literal

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


# ---------------------------------------------------------------------------
# CFG-1: central workflow configuration, owner approval and the audit ledger
# ---------------------------------------------------------------------------
#
# Step 013 (``CFG-1``) centralizes workflow settings that used to live as
# scattered implicit constants: the 3x5 concurrency ceilings, the retention
# defaults, and the owner-editable gate-slot ceilings.  Every change is an
# explicit *proposal* that the owner *approves*; the approval is recorded in an
# append-only JSONL ledger where each entry links to the decision it
# *supersedes*.  The ledger is evidence: it is never rewritten, so the audit
# history cannot be altered by a later run.

#: Owner-declared 3x5 concurrency ceiling (ADR-0004 Amendment 7): at most three
#: concurrently active tasks and at most five worker sessions per project.  These
#: are CEILINGS, never utilization targets.
MAX_CONCURRENT_TASKS: Final[int] = 3
MAX_WORKER_SESSIONS_PER_PROJECT: Final[int] = 5

#: Gate-slot ceilings the central config validates the dynamic runtime against.
#: The *runtime* counts are computed from live memory (D-0076); these remain the
#: owner-editable upper bounds recorded in the ledger.
DEFAULT_GATE_HEAVY_SLOTS: Final[int] = 1
DEFAULT_MAX_LIGHT_SLOTS: Final[int] = 8

#: Retention defaults.  They mirror the accepted storage-retention policy in
#: :mod:`saberops.storage_lifecycle`; the ledger is where an owner override is
#: recorded so it is one audited decision instead of an implicit constant.
DEFAULT_EVIDENCE_ACCEPTED_RETENTION_DAYS: Final[int] = 90
DEFAULT_EVIDENCE_OTHER_RETENTION_DAYS: Final[int] = 30
DEFAULT_CHECKPOINT_RETENTION_DAYS: Final[int] = 7
DEFAULT_OWNER_DB_VACUUM_INTERVAL_DAYS: Final[int] = 7
DEFAULT_FAILED_SCRATCH_CAP_GIB: Final[float] = 2.0

CONFIG_LEDGER_SCHEMA_VERSION: Final[int] = 1
WORKFLOW_SETTINGS_VERSION: Final[int] = 1
_CONFIG_LEDGER_FILENAME: Final[str] = "config-decisions.jsonl"
_CONFIG_KIND_WORKFLOW: Final[str] = "workflow-settings"
_CLEANUP_KINDS: Final[frozenset[str]] = frozenset({"git", "storage"})


class ConfigError(ValueError):
    """Fail-closed error for invalid central configuration or audit state."""


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")


def _canonical_json(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _digest(payload: object) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _require_int(source: str, name: str, value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{source}: {name} must be an integer, got {type(value).__name__}")
    return value


def _require_float(source: str, name: str, value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{source}: {name} must be a number, got {type(value).__name__}")
    return float(value)


def _require_str(source: str, name: str, value: object) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{source}: {name} must be a string, got {type(value).__name__}")
    return value


@dataclass(frozen=True)
class WorkflowSettings:
    """Validated central workflow policy; safe to record verbatim in the ledger.

    The dataclass validates itself on construction, so an invalid ceiling can
    never be persisted: an over-limit concurrency, a non-positive retention, or
    a gate-slot budget that cannot host the configured worker concurrency all
    fail closed at :class:`ConfigError`.
    """

    max_concurrent_tasks: int = MAX_CONCURRENT_TASKS
    max_worker_sessions_per_project: int = MAX_WORKER_SESSIONS_PER_PROJECT
    gate_heavy_slots: int = DEFAULT_GATE_HEAVY_SLOTS
    gate_light_slots_max: int = DEFAULT_MAX_LIGHT_SLOTS
    evidence_accepted_retention_days: int = DEFAULT_EVIDENCE_ACCEPTED_RETENTION_DAYS
    evidence_other_retention_days: int = DEFAULT_EVIDENCE_OTHER_RETENTION_DAYS
    checkpoint_retention_days: int = DEFAULT_CHECKPOINT_RETENTION_DAYS
    owner_db_vacuum_interval_days: int = DEFAULT_OWNER_DB_VACUUM_INTERVAL_DAYS
    failed_scratch_cap_gib: float = DEFAULT_FAILED_SCRATCH_CAP_GIB

    def __post_init__(self) -> None:
        validate_workflow_settings(self)

    def as_dict(self) -> dict[str, object]:
        """Render the settings as a versioned JSON-compatible snapshot."""
        payload: dict[str, object] = {"version": WORKFLOW_SETTINGS_VERSION}
        for field_info in fields(self):
            payload[field_info.name] = getattr(self, field_info.name)
        return payload

    def frozen_hash(self) -> str:
        """Return the frozen content hash used for compatibility checks."""
        return _digest(self.as_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> WorkflowSettings:
        """Rebuild settings from a persisted snapshot, validating every field."""
        if not isinstance(data, Mapping):
            raise ConfigError("workflow settings snapshot must be a mapping")
        allowed = {field.name for field in fields(cls)} | {"version"}
        unknown = set(data) - allowed
        if unknown:
            raise ConfigError(f"workflow settings snapshot has unknown keys: {sorted(unknown)}")
        if "version" in data:
            version = _require_int("workflow settings", "version", data["version"])
            if version != WORKFLOW_SETTINGS_VERSION:
                raise ConfigError(
                    f"workflow settings version must be {WORKFLOW_SETTINGS_VERSION}, got {version}"
                )
        return cls(
            max_concurrent_tasks=_require_int(
                "workflow settings", "max_concurrent_tasks", data.get("max_concurrent_tasks")
            ),
            max_worker_sessions_per_project=_require_int(
                "workflow settings",
                "max_worker_sessions_per_project",
                data.get("max_worker_sessions_per_project"),
            ),
            gate_heavy_slots=_require_int(
                "workflow settings", "gate_heavy_slots", data.get("gate_heavy_slots")
            ),
            gate_light_slots_max=_require_int(
                "workflow settings", "gate_light_slots_max", data.get("gate_light_slots_max")
            ),
            evidence_accepted_retention_days=_require_int(
                "workflow settings",
                "evidence_accepted_retention_days",
                data.get("evidence_accepted_retention_days"),
            ),
            evidence_other_retention_days=_require_int(
                "workflow settings",
                "evidence_other_retention_days",
                data.get("evidence_other_retention_days"),
            ),
            checkpoint_retention_days=_require_int(
                "workflow settings",
                "checkpoint_retention_days",
                data.get("checkpoint_retention_days"),
            ),
            owner_db_vacuum_interval_days=_require_int(
                "workflow settings",
                "owner_db_vacuum_interval_days",
                data.get("owner_db_vacuum_interval_days"),
            ),
            failed_scratch_cap_gib=_require_float(
                "workflow settings", "failed_scratch_cap_gib", data.get("failed_scratch_cap_gib")
            ),
        )


def default_workflow_settings() -> WorkflowSettings:
    """Return the canonical default workflow settings."""
    return WorkflowSettings()


def validate_workflow_settings(settings: WorkflowSettings) -> None:
    """Fail closed on any configuration that violates the recorded policy.

    Rules (each one is a separate, testable invariant):

    * the 3x5 ceilings are *maximums* -- ``max_concurrent_tasks`` never exceeds
      :data:`MAX_CONCURRENT_TASKS` and ``max_worker_sessions_per_project`` never
      exceeds :data:`MAX_WORKER_SESSIONS_PER_PROJECT`;
    * the gate-slot budget must be able to host the configured worker
      concurrency (``gate_light_slots_max >= max_concurrent_tasks``) and at
      least one heavy slot exists;
    * retention windows are positive and the "other" window never outlives the
      "accepted" window;
    * the failed-scratch cap is non-negative.
    """
    if not 1 <= settings.max_concurrent_tasks <= MAX_CONCURRENT_TASKS:
        raise ConfigError(
            f"max_concurrent_tasks must be 1..{MAX_CONCURRENT_TASKS}, "
            f"got {settings.max_concurrent_tasks}"
        )
    if not 1 <= settings.max_worker_sessions_per_project <= MAX_WORKER_SESSIONS_PER_PROJECT:
        raise ConfigError(
            f"max_worker_sessions_per_project must be 1..{MAX_WORKER_SESSIONS_PER_PROJECT}, "
            f"got {settings.max_worker_sessions_per_project}"
        )
    if settings.gate_heavy_slots < 1:
        raise ConfigError(
            f"gate_heavy_slots must be >= 1, got {settings.gate_heavy_slots}"
        )
    if settings.gate_light_slots_max < 1:
        raise ConfigError(
            f"gate_light_slots_max must be >= 1, got {settings.gate_light_slots_max}"
        )
    if settings.gate_light_slots_max < settings.max_concurrent_tasks:
        raise ConfigError(
            "gate_light_slots_max must be >= max_concurrent_tasks "
            f"({settings.gate_light_slots_max} < {settings.max_concurrent_tasks})"
        )
    for name in (
        "evidence_accepted_retention_days",
        "evidence_other_retention_days",
        "checkpoint_retention_days",
        "owner_db_vacuum_interval_days",
    ):
        if getattr(settings, name) < 1:
            raise ConfigError(f"{name} must be >= 1, got {getattr(settings, name)}")
    if settings.evidence_other_retention_days > settings.evidence_accepted_retention_days:
        raise ConfigError(
            "evidence_other_retention_days must not exceed evidence_accepted_retention_days"
        )
    if settings.failed_scratch_cap_gib < 0:
        raise ConfigError(
            f"failed_scratch_cap_gib must be >= 0, got {settings.failed_scratch_cap_gib}"
        )


def verify_frozen_hash(settings: WorkflowSettings, expected_hash: str) -> bool:
    """True when ``settings`` still hashes to ``expected_hash``.

    Recording the hash alongside a decision makes an accidental edit of a
    persisted snapshot detectable instead of silently accepted.
    """
    return settings.frozen_hash() == expected_hash


@dataclass(frozen=True)
class ConfigProposal:
    """A proposed, *not yet approved* change to the workflow settings."""

    settings: WorkflowSettings
    changed: tuple[str, ...]
    rationale: str
    proposed_by: str
    frozen_hash: str

    def as_dict(self) -> dict[str, object]:
        return {
            "settings": self.settings.as_dict(),
            "changed": list(self.changed),
            "rationale": self.rationale,
            "proposed_by": self.proposed_by,
            "frozen_hash": self.frozen_hash,
        }


@dataclass(frozen=True)
class ConfigDecision:
    """One immutable, owner-approved entry in the configuration audit ledger.

    ``decision_id`` is the content hash of the entry body, so a replayed entry
    can be verified byte-for-byte.  ``supersedes`` links the entry to the one it
    replaces, giving the ledger a single linear audit chain per ``kind``.
    """

    created_at: str
    kind: str
    subject: str
    value: dict[str, Any]
    rationale: str
    approved_by: str
    supersedes: str | None = None
    frozen_hash: str = ""
    decision_id: str = ""

    def body(self) -> dict[str, Any]:
        return {
            "created_at": self.created_at,
            "kind": self.kind,
            "subject": self.subject,
            "value": self.value,
            "rationale": self.rationale,
            "approved_by": self.approved_by,
            "supersedes": self.supersedes,
            "frozen_hash": self.frozen_hash,
        }

    def expected_id(self) -> str:
        return _digest(self.body())

    def with_id(self) -> ConfigDecision:
        return replace(self, decision_id=self.expected_id())

    def as_dict(self) -> dict[str, Any]:
        payload = self.body()
        payload["decision_id"] = self.decision_id
        payload["version"] = CONFIG_LEDGER_SCHEMA_VERSION
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ConfigDecision:
        if not isinstance(data, Mapping):
            raise ConfigError("config decision entry must be a mapping")
        required = (
            "created_at",
            "kind",
            "subject",
            "value",
            "rationale",
            "approved_by",
            "decision_id",
        )
        missing = [name for name in required if name not in data]
        if missing:
            raise ConfigError(f"config decision entry missing keys: {missing}")
        value = data["value"]
        if not isinstance(value, dict):
            raise ConfigError("config decision 'value' must be a JSON object")
        supersedes = data.get("supersedes")
        if supersedes is not None and not isinstance(supersedes, str):
            raise ConfigError("config decision 'supersedes' must be a string or null")
        return cls(
            created_at=_require_str("config decision", "created_at", data["created_at"]),
            kind=_require_str("config decision", "kind", data["kind"]),
            subject=_require_str("config decision", "subject", data["subject"]),
            value=dict(value),
            rationale=_require_str("config decision", "rationale", data["rationale"]),
            approved_by=_require_str("config decision", "approved_by", data["approved_by"]),
            supersedes=supersedes,
            frozen_hash=str(data.get("frozen_hash", "")),
            decision_id=_require_str("config decision", "decision_id", data["decision_id"]),
        )


def config_ledger_path(state_dir: Path | str) -> Path:
    """Return the append-only configuration audit ledger path for a state dir."""
    return Path(state_dir) / _CONFIG_LEDGER_FILENAME


class ConfigLedger:
    """Append-only, flock-guarded JSONL ledger of configuration decisions.

    The ledger is immutable evidence: entries are only appended, every entry is
    verified against its own content hash on write, and the ``supersedes`` link
    must extend the single linear chain for its ``kind``.  A tampered or forked
    history raises :class:`ConfigError` instead of being accepted.
    """

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)

    def entries(self) -> tuple[ConfigDecision, ...]:
        """Read every entry oldest-first; a missing file is an empty history."""
        if not self.path.exists():
            return ()
        try:
            text = self.path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ConfigError(f"cannot read config ledger {self.path}: {exc}") from exc
        return self._parse(text)

    @staticmethod
    def _parse(text: str) -> tuple[ConfigDecision, ...]:
        decisions: list[ConfigDecision] = []
        seen: set[str] = set()
        for line_number, line in enumerate(text.splitlines(), start=1):
            raw = line.strip()
            if not raw:
                continue
            try:
                payload = json.loads(raw)
            except ValueError as exc:
                raise ConfigError(f"config ledger line {line_number} is not JSON: {exc}") from exc
            decision = ConfigDecision.from_dict(payload)
            if decision.decision_id != decision.expected_id():
                raise ConfigError(
                    f"config ledger line {line_number} fails its content-hash check"
                )
            if decision.decision_id in seen:
                raise ConfigError(
                    f"config ledger line {line_number} duplicates {decision.decision_id}"
                )
            seen.add(decision.decision_id)
            decisions.append(decision)
        return tuple(decisions)

    def superseded_ids(self) -> frozenset[str]:
        return frozenset(entry.supersedes for entry in self.entries() if entry.supersedes)

    def tip_for(self, kind: str) -> ConfigDecision | None:
        """Return the current (non-superseded) head of ``kind``'s chain."""
        superseded = self.superseded_ids()
        tips = [entry for entry in self.entries() if entry.decision_id not in superseded]
        matching = [entry for entry in tips if entry.kind == kind]
        if len(matching) > 1:
            raise ConfigError(f"config ledger has {len(matching)} heads for kind {kind!r}")
        return matching[0] if matching else None

    def append(self, decision: ConfigDecision) -> ConfigDecision:
        """Validate and append ``decision`` under an exclusive flock."""
        if decision.decision_id != decision.expected_id():
            raise ConfigError("config decision id does not match its immutable body")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.seek(0)
                existing = self._parse(handle.read())
                known = {entry.decision_id for entry in existing}
                if decision.decision_id in known:
                    raise ConfigError(f"config decision {decision.decision_id} already recorded")
                superseded = {entry.supersedes for entry in existing if entry.supersedes}
                tips = [entry for entry in existing if entry.decision_id not in superseded]
                if decision.supersedes is None:
                    if any(entry.kind == decision.kind for entry in tips):
                        raise ConfigError(
                            f"config ledger already has a head for kind {decision.kind!r}"
                        )
                else:
                    target = next(
                        (entry for entry in existing if entry.decision_id == decision.supersedes),
                        None,
                    )
                    if target is None:
                        raise ConfigError(
                            f"supersedes target {decision.supersedes} is not in the ledger"
                        )
                    if target.kind != decision.kind:
                        raise ConfigError("supersedes target has a different kind")
                handle.write(_canonical_json(decision.as_dict()) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
        return decision


def propose_config_change(
    base: WorkflowSettings,
    changes: Mapping[str, object],
    *,
    rationale: str,
    proposed_by: str,
) -> ConfigProposal:
    """Validate ``changes`` against ``base`` and return an approval-ready proposal.

    Nothing is persisted here: a proposal is only a decision request.  The owner
    (or a delegated authority) transforms it into a ledger entry through
    :func:`approve_config_proposal`.
    """
    validate_workflow_settings(base)
    known = {field.name for field in fields(WorkflowSettings)}
    unknown = set(changes) - known
    if unknown:
        raise ConfigError(f"unknown workflow setting(s): {sorted(unknown)}")
    payload: dict[str, object] = dict(base.as_dict())
    payload.pop("version", None)
    payload.update(dict(changes))
    candidate = WorkflowSettings.from_dict(payload)
    changed = tuple(
        sorted(
            name
            for name in changes
            if getattr(candidate, name) != getattr(base, name)
        )
    )
    if not changed:
        raise ConfigError("proposal does not change any workflow setting")
    return ConfigProposal(
        settings=candidate,
        changed=changed,
        rationale=rationale,
        proposed_by=proposed_by,
        frozen_hash=candidate.frozen_hash(),
    )


def approve_config_proposal(
    proposal: ConfigProposal,
    *,
    ledger: ConfigLedger,
    approved_by: str,
    created_at: str | None = None,
) -> ConfigDecision:
    """Record an owner approval of ``proposal`` as an immutable ledger entry."""
    if not proposal.frozen_hash:
        raise ConfigError("proposal is missing its frozen hash")
    if proposal.frozen_hash != proposal.settings.frozen_hash():
        raise ConfigError("proposal frozen hash does not match its settings")
    tip = ledger.tip_for("config")
    decision = ConfigDecision(
        created_at=created_at or _now_iso(),
        kind="config",
        subject=_CONFIG_KIND_WORKFLOW,
        value=proposal.settings.as_dict(),
        rationale=proposal.rationale,
        approved_by=approved_by,
        supersedes=tip.decision_id if tip is not None else None,
        frozen_hash=proposal.frozen_hash,
    ).with_id()
    return ledger.append(decision)


def record_decision(
    *,
    ledger: ConfigLedger,
    kind: str,
    subject: str,
    value: Mapping[str, Any],
    rationale: str,
    approved_by: str,
    created_at: str | None = None,
) -> ConfigDecision:
    """Record one generic owner-approved decision that supersedes its chain head."""
    if not kind:
        raise ConfigError("decision kind must be non-empty")
    if not subject:
        raise ConfigError("decision subject must be non-empty")
    if not approved_by:
        raise ConfigError("decision approved_by must be non-empty")
    tip = ledger.tip_for(kind)
    decision = ConfigDecision(
        created_at=created_at or _now_iso(),
        kind=kind,
        subject=subject,
        value=dict(value),
        rationale=rationale,
        approved_by=approved_by,
        supersedes=tip.decision_id if tip is not None else None,
        frozen_hash=_digest(dict(value)),
    ).with_id()
    return ledger.append(decision)


def validate_cleanup_policy(policy: Mapping[str, Any]) -> None:
    """Fail closed on a malformed owner-approved Git/storage cleanup policy.

    A cleanup policy names what may be deleted (``kind``: ``git`` or
    ``storage``), the retention window in days, and the concrete subjects.  It is
    validated before it is recorded so an owner cannot approve a policy the
    cleanup layer could not execute safely.
    """
    kind = policy.get("kind")
    if kind not in _CLEANUP_KINDS:
        raise ConfigError(
            f"cleanup policy 'kind' must be one of {sorted(_CLEANUP_KINDS)}, got {kind!r}"
        )
    ttl = policy.get("ttl_days")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or ttl < 0:
        raise ConfigError("cleanup policy 'ttl_days' must be a non-negative integer")
    subjects = policy.get("subjects")
    if not isinstance(subjects, Sequence) or isinstance(subjects, (str, bytes)):
        raise ConfigError("cleanup policy 'subjects' must be a list of strings")
    if not subjects or not all(isinstance(item, str) and item for item in subjects):
        raise ConfigError("cleanup policy 'subjects' must contain non-empty strings")


def record_cleanup_decision(
    *,
    ledger: ConfigLedger,
    policy: Mapping[str, Any],
    rationale: str,
    approved_by: str,
    created_at: str | None = None,
) -> ConfigDecision:
    """Record an owner-approved Git/storage cleanup policy in the ledger."""
    validate_cleanup_policy(policy)
    return record_decision(
        ledger=ledger,
        kind="cleanup",
        subject=str(policy["kind"]),
        value=dict(policy),
        rationale=rationale,
        approved_by=approved_by,
        created_at=created_at,
    )


def current_workflow_settings(ledger: ConfigLedger) -> WorkflowSettings:
    """Return the effective settings, verifying the head's frozen hash.

    With no recorded decision the canonical defaults apply.  A head whose
    snapshot does not hash to the value recorded alongside it is treated as
    tampered configuration and fails closed.
    """
    tip = ledger.tip_for("config")
    if tip is None:
        return default_workflow_settings()
    settings = WorkflowSettings.from_dict(tip.value)
    if not verify_frozen_hash(settings, tip.frozen_hash):
        raise ConfigError(
            f"recorded workflow settings at {tip.decision_id} fail their frozen-hash check"
        )
    return settings


__all__ = [
    "CONFIG_LEDGER_SCHEMA_VERSION",
    "DEFAULT_CHECKPOINT_RETENTION_DAYS",
    "DEFAULT_EVIDENCE_ACCEPTED_RETENTION_DAYS",
    "DEFAULT_EVIDENCE_OTHER_RETENTION_DAYS",
    "DEFAULT_FAILED_SCRATCH_CAP_GIB",
    "DEFAULT_GATE_HEAVY_SLOTS",
    "DEFAULT_MAX_LIGHT_SLOTS",
    "DEFAULT_OWNER_DB_VACUUM_INTERVAL_DAYS",
    "MAX_CONCURRENT_TASKS",
    "MAX_WORKER_SESSIONS_PER_PROJECT",
    "WORKFLOW_SETTINGS_VERSION",
    "ConfigDecision",
    "ConfigError",
    "ConfigLedger",
    "ConfigProposal",
    "WorkflowSettings",
    "approve_config_proposal",
    "config_ledger_path",
    "current_workflow_settings",
    "default_workflow_settings",
    "propose_config_change",
    "record_cleanup_decision",
    "record_decision",
    "validate_cleanup_policy",
    "validate_workflow_settings",
    "verify_frozen_hash",
]
