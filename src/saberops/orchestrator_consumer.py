"""Production consumer for the selected Orchestrator-model binding.

C15-XEC (Orchestrator Execution Consumer): the bounded seam that lets the
selected Orchestrator binding drive a real model invocation, instead of
remaining a stored configuration only.

The consumer is the *production* path for "the owner chose this binding for
the Orchestrator-model slot; actually use it."  It is intentionally narrow:

* It loads the persisted :class:`OwnerBindings` from disk (the same
  :func:`~saberops.control_plane.binding_store.load_owner_bindings`
  the Web ``/orchestrator`` picker writes through) and resolves the exact
  binding via :func:`select_orch_binding`.  No caller-supplied
  provider/model string is ever honored; a raw override never
  synthesizes an Orchestrator identity.
* It then resolves the executable runtime value via
  :func:`resolve_execution_binding`, the canonical C11-D resolver the
  autonomous lifecycle already uses for worker dispatch.  The same
  ``ORCH_EXECUTION_*`` env overlay (carrying the exact
  ``ORCH_EXECUTION_BINDING_ID`` / ``ORCH_EXECUTION_PROFILE_ID`` /
  ``ORCH_EXECUTION_POOL_ID`` / ``ORCH_EXECUTION_BACKEND`` identity) is
  passed through, so the durable evidence the consumer records is the
  exact identity the invocation observes.
* Effort resolution is delegated to
  :func:`~saberops.control_plane.effort.resolve_effort` over the
  canonical :data:`DEFAULT_ORCH_EFFORT_POLICY` -- ULTRA never silently
  degrades, an out-of-bounds tier fails closed, and an unsupported
  binding tier is typed ``UNSUPPORTED`` (never silently coerced).
  The bounded consumer-side correction recognises the
  ``no explicit effort + no known controllable tiers`` case
  (caller request is ``None``, owner policy has not selected a tier,
  binding declares an empty ``reasoning_effort_capabilities``) and
  resolves ``effort = None`` so the adapter / provider may use its
  normal default reasoning behaviour -- never a fabricated
  LOW/MEDIUM/HIGH/ULTRA claim, never a sibling substitution.
* The actual model call goes through the existing
  :class:`~saberops.workers.base.WorkerAdapter` the binding's
  provider resolves to.  Reusing the same adapter registry / env
  projection is deliberate: there is no second adapter surface, and the
  exact binding identity observed by the adapter is the same identity
  recorded as durable evidence.

C15 invariants preserved:

* **WORKER / ORCHESTRATOR separation (C15-XB-03):** the consumer only
  resolves bindings whose :class:`BindingRole` is
  :class:`~saberops.control_plane.bindings.BindingRole.ORCHESTRATOR`.
  A WORKER-role binding selected on the Web /orchestrator page is a UI
  bug; the consumer refuses such a binding closed, never silently
  swaps it for a sibling.
* **Exact identity (C15-XB-01):** two bindings with the same
  provider/model on distinct connections remain distinct; resolution
  is always through the exact :attr:`ExecutionBinding.binding_id`
  resolved by the persisted owner policy, never through
  provider/model first match.
* **Capability truthfulness (C15-XC-01):** effort / capability /
  profile decisions never fabricate a tier.  A binding whose
  :attr:`~ExecutionBinding.reasoning_effort_capabilities` does not
  list a tier returns typed ``UNSUPPORTED`` *only when the caller
  or owner policy explicitly asserts a tier*; an empty capability
  set combined with no explicit request / policy-selected tier
  resolves to ``effort = None`` (provider-default), preserving
  capability truthfulness -- the binding's capability set is never
  mutated, no fabricated tier is persisted to evidence.  A binding
  whose profile is ``enabled=False`` returns typed
  ``PROFILE_DISABLED``; a binding whose backend has no verified
  runtime seam (``DIRECT_API``) returns typed ``UNSUPPORTED_BACKEND``.
* **OpenCode compatibility evidence (C15-GC-03):** when the persisted
  binding routes through OpenCode, the consumer consults the same
  :mod:`~saberops.workers.instruction_compat` evidence the governed
  worker dispatch already enforces.  Compatibility is reported but
  never used to silently swap providers: ``INCOMPATIBLE`` and
  ``UNKNOWN`` each fail closed with a typed reason.
* **Secret / credential boundary:** the consumer never reads
  ``os.environ`` itself; the resolved binding carries the
  ``ORCH_EXECUTION_PROFILE_ACCOUNT`` /
  ``ORCH_EXECUTION_PROFILE_BACKEND`` selectors that the adapter's
  own secret-resolution layers consume, and the consumer never
  persists the resolved values.  Raw API keys / tokens /
  account-session material never enter this module.
* **Fail closed:** every step below has a typed refusal.  An absent
  Orchestrator selection, a pinned binding that resolves to no
  executable, an unsupported effort, or a transport refusal all
  produce a typed :class:`OrchestratorModelInvocationError` with a
  stable reason and **zero** adapter invocation.  The consumer never
  silently picks a sibling binding because the chosen one is
  unavailable.

The consumer is **not** the place where the Orchestrator-loop decides
*whether* to invoke a model.  It is only the seam that, when invoked,
turns a persisted owner selection into a real model call.  Decision
policy (when to think, what to ask) is owned by the caller; the
consumer owns execution semantics.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from saberops.control_plane.binding_store import (
    BindingStoreError,
    OwnerBindings,
    load_owner_bindings,
)
from saberops.control_plane.bindings import (
    BindingResolutionError,
    BindingRole,
    ExecutionBinding,
    ResolvedExecutionBinding,
    resolve_execution_binding,
)
from saberops.control_plane.effort import (
    DEFAULT_ORCH_EFFORT_POLICY,
    EFFORT_BELOW_MIN,
    EFFORT_ULTRA_DISABLED,
    EffortDecision,
    EffortDecisionStatus,
    EffortPolicy,
    resolve_effort,
)
from saberops.control_plane.orch_binding import (
    ORCH_BINDING_AUTO_NONE,
    ORCH_BINDING_EMPTY_PREFERRED,
    ORCH_BINDING_NO_ELIGIBLE,
    ORCH_BINDING_NOT_FOUND,
    ORCH_BINDING_PINNED_CAPABILITY,
    ORCH_BINDING_PINNED_DISABLED,
    ORCH_BINDING_PINNED_QUOTA,
    ORCH_BINDING_PINNED_RISK,
    ORCH_BINDING_PINNED_TRAINING,
    ORCH_BINDING_PINNED_UNAVAILABLE,
    ORCH_BINDING_PROFILE_DISABLED,
    OrchSelectionStatus,
    select_orch_binding,
)
from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    AttemptStatus,
    ReasoningEffort,
    Run,
    RunStatus,
    Tier,
    WorkerRequest,
    WorkerResult,
)
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

# Stable reason vocabulary for Orchestrator-model invocation refusals.
# These strings appear verbatim in durable evidence; new values are
# additive only.
REASON_NO_POLICY = "ORCH_MODEL_NO_POLICY"
# Distinct from ``REASON_NO_POLICY``: the persisted owner bindings
# document exists but is unreadable / malformed / invalid (parse
# failure, schema mismatch, registry invalid).  An owner document
# that loads successfully and carries ``policy is None`` is
# ``REASON_NO_POLICY``; a document that fails to load is
# ``REASON_BINDING_STORE_UNREADABLE`` -- the consumer must never
# collapse a corrupt configuration into a non-request.
REASON_BINDING_STORE_UNREADABLE = "ORCH_MODEL_BINDING_STORE_UNREADABLE"
REASON_SELECTION_UNRESOLVED = "ORCH_MODEL_SELECTION_UNRESOLVED"
REASON_NOT_ORCHESTRATOR_ROLE = "ORCH_MODEL_NOT_ORCHESTRATOR_ROLE"
REASON_NO_ADAPTER = "ORCH_MODEL_NO_ADAPTER"
REASON_RESOLUTION_FAILED = "ORCH_MODEL_RESOLUTION_FAILED"
REASON_EFFORT_UNSUPPORTED = "ORCH_MODEL_EFFORT_UNSUPPORTED"
REASON_EFFORT_UNAUTHORIZED = "ORCH_MODEL_EFFORT_UNAUTHORIZED"
REASON_INSTRUCTION_INCOMPATIBLE = "ORCH_MODEL_INSTRUCTION_INCOMPATIBLE"
REASON_INSTRUCTION_UNKNOWN = "ORCH_MODEL_INSTRUCTION_UNKNOWN"
REASON_INVOCATION_REFUSED = "ORCH_MODEL_INVOCATION_REFUSED"
REASON_INVOCATION_FAILED = "ORCH_MODEL_INVOCATION_FAILED"
REASON_INVOCATION_TIMEOUT = "ORCH_MODEL_INVOCATION_TIMEOUT"


# Mapping from canonical Orch-binding refusal vocabulary to the typed
# Orchestrator-model invocation refusal vocabulary.  Centralised here so
# a call to :func:`select_orch_binding` that returns ``UNRESOLVED``
# always surfaces a stable reason string downstream and the consumer
# does not silently reinterpret the policy failure.
_ORCH_SELECTION_REASON_MAP: Mapping[str, str] = {
    ORCH_BINDING_NOT_FOUND: "ORCH_MODEL_NOT_FOUND",
    ORCH_BINDING_PROFILE_DISABLED: "ORCH_MODEL_PROFILE_DISABLED",
    ORCH_BINDING_PINNED_DISABLED: "ORCH_MODEL_PINNED_DISABLED",
    ORCH_BINDING_PINNED_TRAINING: "ORCH_MODEL_PINNED_TRAINING",
    ORCH_BINDING_PINNED_CAPABILITY: "ORCH_MODEL_PINNED_CAPABILITY",
    ORCH_BINDING_PINNED_QUOTA: "ORCH_MODEL_PINNED_QUOTA",
    ORCH_BINDING_PINNED_RISK: "ORCH_MODEL_PINNED_RISK",
    ORCH_BINDING_PINNED_UNAVAILABLE: "ORCH_MODEL_PINNED_UNAVAILABLE",
    ORCH_BINDING_NO_ELIGIBLE: "ORCH_MODEL_NO_ELIGIBLE",
    ORCH_BINDING_AUTO_NONE: "ORCH_MODEL_AUTO_NONE",
    ORCH_BINDING_EMPTY_PREFERRED: "ORCH_MODEL_EMPTY_PREFERRED",
}


# Mapping from canonical C11-D binding-resolution refusal vocabulary
# to the typed Orchestrator-model invocation refusal vocabulary.
_RESOLUTION_REASON_MAP: Mapping[str, str] = {
    "NO_REGISTRY": "ORCH_MODEL_NO_REGISTRY",
    "UNKNOWN_BINDING": "ORCH_MODEL_UNKNOWN_BINDING",
    "PROFILE_UNKNOWN": "ORCH_MODEL_PROFILE_UNKNOWN",
    "PROFILE_DISABLED": "ORCH_MODEL_PROFILE_DISABLED",
    "POOL_UNKNOWN": "ORCH_MODEL_POOL_UNKNOWN",
    "UNSUPPORTED_BACKEND": "ORCH_MODEL_UNSUPPORTED_BACKEND",
    "NO_ADAPTER": "ORCH_MODEL_NO_ADAPTER",
    "EFFORT_NOT_IN_BINDING": "ORCH_MODEL_EFFORT_NOT_IN_BINDING",
    "UNMAPPABLE_PROFILE": "ORCH_MODEL_UNMAPPABLE_PROFILE",
}


@dataclass(frozen=True)
class OrchestratorModelRequest:
    """A single Orchestrator-model invocation request.

    The request carries only the inputs the consumer itself owns: the
    system/user prompts, the optional effort tier, and optional
    scratch / run / correlation hints.  Provider, model, binding
    identity and transport are NOT part of the request -- those come
    exclusively from the persisted owner selection, exactly to
    prevent a caller from synthesizing a different Orchestrator
    identity than the owner chose.
    """

    system_prompt: str = ""
    user_prompt: str = ""
    reasoning_effort: ReasoningEffort | None = None
    scratch_path: Path | None = None
    timeout_seconds: float = 60.0
    run_id: str | None = None
    correlation_id: str | None = None


@dataclass(frozen=True)
class OrchestratorModelResult:
    """The typed result of one Orchestrator-model invocation.

    Carries the exact identity the consumer resolved AND invoked with
    (binding_id / profile_id / quota_pool_id / provider / model /
    backend / effort), the typed status, and the durable evidence
    ids the consumer recorded.  A result whose ``status`` is
    ``REFUSED`` / ``FAILED`` / ``TIMEOUT`` carries a typed reason
    and ``output_text == ""`` -- the consumer never invents output
    for a refused launch.
    """

    status: str
    binding_id: str
    profile_id: str
    quota_pool_id: str
    provider: str
    model: str
    backend: str
    reasoning_effort: ReasoningEffort | None
    output_text: str
    summary: str
    error: str | None
    prompt_bytes: int
    duration_seconds: float
    correlation_id: str | None
    evidence_run_id: str
    evidence_event_id: int
    reason: str | None = None
    capability_descriptor: dict[str, Any] = field(default_factory=dict)


class OrchestratorModelInvocationError(ValueError):
    """Typed refusal to launch an Orchestrator-model invocation.

    Raised before any adapter invocation.  Carries a stable
    :attr:`reason` drawn from the documented refusal vocabulary so
    the durable evidence the consumer records identifies the exact
    step that closed.
    """

    def __init__(
        self,
        reason: str,
        *,
        message: str = "",
        detail: Mapping[str, Any] | None = None,
    ) -> None:
        self.reason = reason
        self.detail: dict[str, Any] = dict(detail) if detail else {}
        super().__init__(message or reason)


def _load_bindings_safely(
    path: Path | str | None,
) -> OwnerBindings:
    """Load :class:`OwnerBindings` from ``path`` (or the default path).

    An unreadable / invalid document raises
    :class:`BindingStoreError` which the consumer maps to
    :class:`OrchestratorModelInvocationError` with reason
    ``ORCH_MODEL_BINDING_STORE_UNREADABLE`` -- a corrupt owner
    document is a typed REFUSAL distinct from
    ``ORCH_MODEL_NO_POLICY`` (which means the document loaded
    successfully but carries no policy).  The bounded guidance
    helper maps this reason to ``REFUSED``; collapsing it into
    ``NOT_REQUESTED`` would silently misclassify a malformed
    configuration as "no Orchestrator was requested".
    """
    try:
        return load_owner_bindings(path)
    except BindingStoreError as exc:
        raise OrchestratorModelInvocationError(
            REASON_BINDING_STORE_UNREADABLE,
            message=f"owner bindings store unreadable: {exc}",
        ) from exc


_SYNTHETIC_EVENT_COUNTER = 0


def _emit_event(
    db: Database | None,
    run_id: str,
    event_type: str,
    *,
    payload: Mapping[str, Any],
    require_existing_run: bool = False,
) -> int:
    """Record one durable evidence event for the consumer's invocation.

    When ``db`` is ``None`` (testing) the consumer synthesises a
    monotonic counter so the durable-evidence contract remains
    observable without an actual database roundtrip.  When ``db``
    is supplied the run row is auto-created (idempotent insert) so
    the events foreign-key constraint never rejects the durable
    evidence -- EXCEPT when ``require_existing_run`` is True, in
    which case the consumer requires the parent run to already exist
    and refuses to create a synthetic run row.  A missing parent run
    is then a visible failure, never a silent launch.
    """
    global _SYNTHETIC_EVENT_COUNTER
    if db is None:
        _SYNTHETIC_EVENT_COUNTER += 1
        return _SYNTHETIC_EVENT_COUNTER
    if require_existing_run:
        if db.get_run(run_id) is None:
            raise RuntimeError(
                f"orchestrator consumer requires existing run {run_id!r}; "
                f"refusing to create a synthetic run row"
            )
    else:
        _ensure_run_row(db, run_id)
    result = db.record_event(run_id, event_type, payload=dict(payload))
    return int(result)


def _ensure_run_row(db: Database, run_id: str) -> None:
    """Idempotently create a minimal run row for evidence attribution.

    The consumer's evidence lives on the ``events`` table, which has
    a foreign key to ``runs``.  Real run rows carry far more state;
    we only insert a minimal row so the FK never rejects the
    consumer's evidence.  This must never overwrite an existing run
    row.
    """
    if db.get_run(run_id) is not None:
        return
    db.create_run(
        Run(
            id=run_id,
            task="orchestrator_model_consumer",
            target_repo="",
            base_commit="",
            tier=Tier.T1,
            training_allowed=False,
            gate_command="",
            status=RunStatus.PENDING,
            created_at=current_iso_timestamp(),
        )
    )


def _truncate(value: str | None, *, limit: int = 300) -> str:
    """Return ``value`` truncated to ``limit`` characters (safe for evidence)."""
    if not value:
        return ""
    cleaned = value.replace("\n", " ").replace("\r", " ").strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1] + "\u2026"


# Identity fields that may travel through ``OrchestratorModelInvocationError``.
# Anything outside this allowlist is stripped before the bounded
# guidance helper projects the detail onto ``orchestrator_guidance_result``,
# so raw credentials / cookies / session material / arbitrary adapter
# environment / arbitrary exception payloads can never leak into evidence.
_SAFE_IDENTITY_DETAIL_KEYS: frozenset[str] = frozenset(
    {
        "binding_id",
        "provider",
        "model",
        "profile_id",
        "quota_pool_id",
        "backend",
        "effort",
    }
)


def _safe_selected_identity(
    *,
    binding: ExecutionBinding,
    resolved: ResolvedExecutionBinding | None = None,
    resolved_effort: ReasoningEffort | None = None,
) -> dict[str, str]:
    """Return the durable, non-secret selected-binding identity for evidence.

    The consumer records this on a typed refusal (via
    ``OrchestratorModelInvocationError.detail``) so the bounded
    guidance helper can preserve the safe exact identity on the
    ``orchestrator_guidance_result`` event even when invocation did
    not occur.  Only non-secret identity fields are returned.

    When ``resolved`` has been computed, ``profile_id`` and
    ``quota_pool_id`` come from the resolved binding (these equal the
    binding's declared references, but the resolver is the
    authoritative source post-step-5).  When ``resolved`` is ``None``
    the binding's declared references are returned instead.
    ``effort`` is included only when the effort has already been
    resolved.
    """
    identity: dict[str, str] = {
        "binding_id": binding.binding_id,
        "provider": binding.provider,
        "model": binding.model,
        "backend": binding.backend.value,
        "profile_id": (
            resolved.profile_id
            if resolved is not None
            else binding.profile_id
        ),
        "quota_pool_id": (
            resolved.quota_pool_id
            if resolved is not None
            else binding.quota_pool_id
        ),
    }
    if resolved_effort is not None:
        identity["effort"] = resolved_effort.value
    return identity


def _scratch_dir_for(request: OrchestratorModelRequest) -> Path:
    """Return the ephemeral scratch directory used for the invocation.

    The consumer only consumes ``request.scratch_path`` when the
    caller supplies one; otherwise it synthesises a fresh
    sub-directory under a parent that the caller has not constrained.
    The scratch dir is for adapter transport bookkeeping (e.g. ``--dir``
    argv); it is never authoritative for any execution identity.
    """
    if request.scratch_path is not None:
        return Path(request.scratch_path)
    parent = Path("/tmp") / f"orch-model-{uuid.uuid4().hex[:8]}"
    parent.mkdir(parents=True, exist_ok=True)
    return parent


class OrchestratorModelConsumer:
    """Production consumer for the persisted Orchestrator-model binding.

    Construction parameters are explicit, dependency-injected, and
    overridable for tests.  In production the constructor reads from
    the canonical owner-state file.  The consumer is **read-only**
    against owner state: selecting a binding here does NOT mutate the
    persisted document.
    """

    def __init__(
        self,
        *,
        db: Database | None = None,
        registry: AdapterRegistry | None = None,
        owner_bindings: OwnerBindings | None = None,
        binding_store_path: Path | str | None = None,
        effort_policy: EffortPolicy | None = None,
        now_iso: Callable[[], str] | None = None,
        require_existing_run: bool = False,
    ) -> None:
        self._db = db
        self._registry = registry or AdapterRegistry.default()
        self._owner_bindings = owner_bindings
        self._binding_store_path = binding_store_path
        self._effort_policy_override = effort_policy
        self._now_iso = now_iso or current_iso_timestamp
        # C15 evidence correction: when True, the consumer refuses to
        # auto-create a synthetic run row in ``_emit_event``.  The
        # bounded initial-guidance lifecycle helper sets this so the
        # Orchestrator evidence is durably attributed to the existing
        # parent run -- never to a synthetic ``orch_initial_guidance_*``
        # / ``orchestrator_model_consumer`` Run row.  Default False
        # preserves the existing C15-XEC evidence stream for direct
        # callers (services, tests) that already operate on a parent run.
        self._require_existing_run = require_existing_run

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    def load_owner_bindings(self) -> OwnerBindings:
        """Return the effective :class:`OwnerBindings` for this consumer.

        When ``owner_bindings`` was injected at construction, return
        it verbatim; otherwise load the owner document from disk and
        raise :class:`OrchestratorModelInvocationError`
        (``ORCH_MODEL_NO_POLICY``) if the document is unreadable.  An
        empty owner document is a typed refusal, not a silent launch
        -- the consumer only runs when the owner has explicitly
        selected an Orchestrator binding.
        """
        if self._owner_bindings is not None:
            return self._owner_bindings
        return _load_bindings_safely(self._binding_store_path)

    def _resolve_effort(
        self,
        binding: ExecutionBinding,
        requested: ReasoningEffort | None,
    ) -> tuple[ReasoningEffort | None, str | None]:
        """Apply owner effort policy + binding capabilities to ``requested``.

        Returns the resolved tier and ``None`` on success; returns
        ``(None, reason)`` with a stable refusal reason on a typed
        refusal.  A ``None`` request delegates to AUTO selection
        within the canonical Orchestrator effort bounds.

        C15 provider-default correction: an empty
        ``binding.reasoning_effort_capabilities`` does NOT prove the
        model cannot execute -- it means SaberOps has no authoritative
        evidence of a controllable tier for this binding.  When
        NEITHER the caller nor the effective owner effort policy
        asserts a tier, the consumer recognises the
        ``no explicit effort + no known controllable tiers`` case and
        resolves to ``None`` (provider-default / uncontrolled
        reasoning effort) instead of fabricating a
        LOW/MEDIUM/HIGH/ULTRA claim or refusing closed.  An explicit
        caller request or an explicit owner policy-selected tier
        continues to delegate to :func:`resolve_effort` so all
        existing capability / authorisation / ULTRA gates remain
        authoritative.
        """
        policy = self._effort_policy_override or DEFAULT_ORCH_EFFORT_POLICY

        # Provider-default / uncontrolled reasoning effort path.
        # The early-return below is the bounded C15 correction: the
        # discovered binding declares no authoritative controllable
        # tiers, and no caller / policy has asserted a specific
        # tier -- so the consumer MUST NOT return
        # ``ORCH_MODEL_EFFORT_UNSUPPORTED`` and MUST NOT fabricate a
        # LOW/MEDIUM/HIGH/ULTRA claim.  The adapter receives
        # ``reasoning_effort=None`` and the provider/model uses its
        # normal default reasoning behaviour.  The binding's
        # ``reasoning_effort_capabilities`` set is never mutated;
        # no fabricated tier is persisted to durable evidence.
        if (
            requested is None
            and policy.selected_effort is None
            and not binding.reasoning_effort_capabilities
        ):
            return None, None

        decision: EffortDecision = resolve_effort(
            binding, policy, requested_effort=requested
        )
        if decision.status is EffortDecisionStatus.RESOLVED:
            return decision.resolved_effort, None
        if decision.status is EffortDecisionStatus.UNAUTHORIZED:
            reason_map = {
                EFFORT_ULTRA_DISABLED: REASON_EFFORT_UNAUTHORIZED,
                EFFORT_BELOW_MIN: REASON_EFFORT_UNAUTHORIZED,
                "EFFORT_ABOVE_MAX": REASON_EFFORT_UNAUTHORIZED,
            }
            return None, reason_map.get(decision.reason, REASON_EFFORT_UNAUTHORIZED)
        return None, REASON_EFFORT_UNSUPPORTED

    def _record_invocation_event(
        self,
        *,
        run_id: str,
        request: OrchestratorModelRequest,
        binding: ExecutionBinding,
        profile_id: str,
        quota_pool_id: str,
        resolved_effort: ReasoningEffort | None,
        capability_descriptor: Mapping[str, Any],
        adapter_provider: str,
    ) -> int:
        """Record the durable evidence that the consumer resolved a binding.

        Emitted BEFORE the adapter runs so the audit trail captures
        the identity that was about to be invoked, not just the
        identity that was invoked (a refused launch still carries
        this event).  The payload contains the exact binding_id /
        profile_id / pool_id / backend / effort / capability
        descriptor the adapter will observe through the env overlay.
        No secret material is included.
        """
        payload = {
            "binding_id": binding.binding_id,
            "profile_id": profile_id,
            "quota_pool_id": quota_pool_id,
            "provider": binding.provider,
            "model": binding.model,
            "backend": binding.backend.value,
            "binding_role": binding.binding_role.value,
            "adapter_provider": adapter_provider,
            "requested_effort": (
                None
                if request.reasoning_effort is None
                else request.reasoning_effort.value
            ),
            "resolved_effort": (
                None if resolved_effort is None else resolved_effort.value
            ),
            "prompt_bytes": len(request.user_prompt.encode("utf-8")),
            "system_prompt_bytes": len(request.system_prompt.encode("utf-8")),
            "capability_descriptor": dict(capability_descriptor),
            "correlation_id": request.correlation_id,
            "invoked_at": self._now_iso(),
        }
        return _emit_event(
            self._db,
            run_id,
            "orchestrator_model_resolved",
            payload=payload,
            require_existing_run=self._require_existing_run,
        )

    def _record_outcome_event(
        self,
        *,
        run_id: str,
        status: str,
        binding: ExecutionBinding,
        profile_id: str,
        quota_pool_id: str,
        resolved_effort: ReasoningEffort | None,
        summary: str,
        output_text: str,
        error: str | None,
        duration_seconds: float,
        reason: str | None,
        correlation_id: str | None,
    ) -> int:
        payload = {
            "binding_id": binding.binding_id,
            "profile_id": profile_id,
            "quota_pool_id": quota_pool_id,
            "provider": binding.provider,
            "model": binding.model,
            "backend": binding.backend.value,
            "resolved_effort": (
                None if resolved_effort is None else resolved_effort.value
            ),
            "status": status,
            "summary": _truncate(summary),
            "output_bytes": len(output_text.encode("utf-8")),
            "duration_seconds": duration_seconds,
            "error": _truncate(error, limit=300) if error else None,
            "reason": reason,
            "correlation_id": correlation_id,
            "recorded_at": self._now_iso(),
        }
        return _emit_event(
            self._db,
            run_id,
            "orchestrator_model_invoked",
            payload=payload,
            require_existing_run=self._require_existing_run,
        )

    def _record_refusal_event(
        self,
        *,
        run_id: str,
        reason: str,
        detail: Mapping[str, Any] | None,
        correlation_id: str | None,
    ) -> int:
        payload: dict[str, Any] = {
            "reason": reason,
            "detail": dict(detail) if detail else {},
            "correlation_id": correlation_id,
            "refused_at": self._now_iso(),
        }
        return _emit_event(
            self._db,
            run_id,
            "orchestrator_model_refused",
            payload=payload,
            require_existing_run=self._require_existing_run,
        )

    def _resolve_executable(
        self,
        *,
        binding: ExecutionBinding,
        adapter: WorkerAdapter | None,
        effort: ReasoningEffort | None,
        registry: Any = None,
    ) -> ResolvedExecutionBinding:
        """Run the canonical C11-D binding resolver over the selected binding.

        ``registry`` is the optional override used by tests; in
        production the resolver reads from the same owner registry
        the consumer already consulted in :meth:`invoke`.  When
        ``registry`` is ``None`` the canonical
        :func:`resolve_execution_binding` falls back to the
        resolver's own lookup against the binding id alone -- but
        the consumer always passes the owner registry here so the
        exact identity the resolver returns equals the identity the
        consumer recorded.
        """
        if adapter is None:
            raise OrchestratorModelInvocationError(
                REASON_NO_ADAPTER,
                message=f"no adapter registered for provider {binding.provider!r}",
                detail={
                    "binding_id": binding.binding_id,
                    "provider": binding.provider,
                },
            )
        try:
            return resolve_execution_binding(
                registry=registry,
                binding_id=binding.binding_id,
                adapter=adapter,
                reasoning_effort=effort,
            )
        except BindingResolutionError as exc:
            mapped = _RESOLUTION_REASON_MAP.get(
                exc.reason, REASON_RESOLUTION_FAILED
            )
            raise OrchestratorModelInvocationError(
                mapped,
                message=(
                    "exact Orchestrator binding does not resolve to executable "
                    f"runtime ({exc.reason}); zero launch"
                ),
                detail={"binding_id": binding.binding_id, "reason": exc.reason},
            ) from exc
        except ValueError as exc:
            raise OrchestratorModelInvocationError(
                REASON_NO_ADAPTER,
                message=f"adapter lookup refused: {exc}",
                detail={"binding_id": binding.binding_id},
            ) from exc

    def _check_instruction_compatibility(
        self,
        *,
        binding: ExecutionBinding,
        adapter: WorkerAdapter | None,
    ) -> str | None:
        """Return a typed refusal reason if the binding's provider carries
        an instruction-compatibility veto that must be honored.

        Only the OpenCode adapter currently opts into this contract;
        for every other adapter ``instruction_compatibility`` is
        ``None`` and the consumer treats the binding as
        compatible-by-default (matching the existing autonomous
        dispatch behaviour).
        """
        if adapter is None:
            return None
        hook = getattr(adapter, "instruction_compatibility", None)
        if not callable(hook):
            return None
        try:
            compat = hook(binding.model)
        except Exception:
            return REASON_INSTRUCTION_UNKNOWN
        from saberops.workers.instruction_compat import InstructionCompatibility

        if compat is InstructionCompatibility.INCOMPATIBLE:
            return REASON_INSTRUCTION_INCOMPATIBLE
        if compat is InstructionCompatibility.UNKNOWN:
            return REASON_INSTRUCTION_UNKNOWN
        return None

    def invoke(
        self,
        request: OrchestratorModelRequest,
    ) -> OrchestratorModelResult:
        """Resolve the persisted binding and invoke it through the adapter.

        Every step below has a typed refusal path; the consumer NEVER
        silently picks a sibling binding because the chosen one is
        unavailable.  When the owner document carries no
        Orchestrator policy, the consumer refuses closed with
        ``ORCH_MODEL_NO_POLICY``; when the persisted binding is not
        an ORCHESTRATOR-role binding, the consumer refuses closed
        with ``ORCH_MODEL_NOT_ORCHESTRATOR_ROLE``.
        """
        correlation_id = request.correlation_id
        evidence_run_id = request.run_id or f"orch_model_{uuid.uuid4().hex[:12]}"

        # 1. Load owner document.
        try:
            owner = self.load_owner_bindings()
        except OrchestratorModelInvocationError as exc:
            self._record_refusal_event(
                run_id=evidence_run_id,
                reason=exc.reason,
                detail=exc.detail,
                correlation_id=correlation_id,
            )
            raise

        if owner.policy is None:
            event_id = self._record_refusal_event(
                run_id=evidence_run_id,
                reason=REASON_NO_POLICY,
                detail={
                    "binding_count": len(tuple(owner.registry.list_bindings()))
                },
                correlation_id=correlation_id,
            )
            raise OrchestratorModelInvocationError(
                REASON_NO_POLICY,
                message=(
                    "no Orchestrator binding policy configured: owner must "
                    "select an Orchestrator binding on /orchestrator before "
                    "the consumer can invoke"
                ),
                detail={"evidence_event_id": event_id},
            )

        # 2. Resolve the exact Orchestrator binding.
        selection = select_orch_binding(owner.policy, owner.registry)
        if selection.status is not OrchSelectionStatus.RESOLVED or selection.binding is None:
            mapped = _ORCH_SELECTION_REASON_MAP.get(
                selection.reason or "", REASON_SELECTION_UNRESOLVED
            )
            event_id = self._record_refusal_event(
                run_id=evidence_run_id,
                reason=mapped,
                detail={
                    "policy_mode": owner.policy.mode.value,
                    "pinned_binding_id": owner.policy.pinned_binding_id,
                    "candidate_pool": list(selection.candidate_pool),
                    "selection_status": selection.status.value,
                    "selection_reason": selection.reason,
                },
                correlation_id=correlation_id,
            )
            # ``select_orch_binding`` may still surface a non-``None``
            # ``binding`` even when the policy refuses (e.g. PINNED +
            # disabled profile: the binding was identified, the
            # profile then failed eligibility).  When that happens,
            # the safe selected-binding identity is already
            # authoritative -- project it onto the typed refusal so
            # the bounded guidance evidence does not lose it.
            pre_selection_identity: dict[str, str] = (
                _safe_selected_identity(binding=selection.binding)
                if selection.binding is not None
                else {}
            )
            raise OrchestratorModelInvocationError(
                mapped,
                message=(
                    "Orchestrator binding selection refused: "
                    f"{selection.reason or 'no eligible binding'}"
                ),
                detail={
                    "evidence_event_id": event_id,
                    **pre_selection_identity,
                },
            )

        binding = selection.binding
        # C15-XB-03 invariant: the Orchestrator consumer never honors
        # a WORKER-role binding; the persisted owner policy must
        # already be ORCHESTRATOR-only by construction, but the
        # consumer re-checks at the execution seam.
        if binding.binding_role is not BindingRole.ORCHESTRATOR:
            event_id = self._record_refusal_event(
                run_id=evidence_run_id,
                reason=REASON_NOT_ORCHESTRATOR_ROLE,
                detail={
                    "binding_id": binding.binding_id,
                    "binding_role": binding.binding_role.value,
                },
                correlation_id=correlation_id,
            )
            raise OrchestratorModelInvocationError(
                REASON_NOT_ORCHESTRATOR_ROLE,
                message=(
                    f"binding {binding.binding_id!r} has role "
                    f"{binding.binding_role.value!r}; Orchestrator consumer "
                    "only honors BindingRole.ORCHESTRATOR"
                ),
                detail={
                    "evidence_event_id": event_id,
                    **_safe_selected_identity(binding=binding),
                },
            )

        # C15-XB-01 invariant: identity is the binding's exact id;
        # we never reach for a provider/model first-match.
        # Validate that the binding's profile and pool references
        # exist in the owner registry before resolution -- the
        # canonical C11-D resolver below re-reads them from the
        # owner registry, but a pre-check here keeps the consumer's
        # evidence consistent with what the resolver will see.
        try:
            owner.registry.profile_of(binding)
            owner.registry.pool_of(binding)
        except KeyError as exc:
            raise OrchestratorModelInvocationError(
                REASON_RESOLUTION_FAILED,
                message=(
                    "selected Orchestrator binding references unknown "
                    f"profile or pool: {exc}"
                ),
                detail={
                    **_safe_selected_identity(binding=binding),
                },
            ) from exc

        # 3. Resolve effort against the binding's declared
        # capabilities and the canonical Orchestrator effort policy.
        # A typed refusal here is the only path: ULTRA without owner
        # enablement, an out-of-bounds tier, or a binding whose
        # declared capabilities do not include the requested tier
        # all fail closed.
        resolved_effort, effort_refusal = self._resolve_effort(
            binding,
            request.reasoning_effort,
        )
        if effort_refusal is not None:
            event_id = self._record_refusal_event(
                run_id=evidence_run_id,
                reason=effort_refusal,
                detail={
                    "binding_id": binding.binding_id,
                    "requested_effort": (
                        None
                        if request.reasoning_effort is None
                        else request.reasoning_effort.value
                    ),
                    "binding_supports_effort": sorted(
                        e.value for e in binding.reasoning_effort_capabilities
                    ),
                },
                correlation_id=correlation_id,
            )
            raise OrchestratorModelInvocationError(
                effort_refusal,
                message=(
                    f"effort resolution refused for binding {binding.binding_id!r}"
                ),
                detail={
                    "evidence_event_id": event_id,
                    **_safe_selected_identity(binding=binding),
                },
            )

        # 4. Resolve the adapter for the binding's provider.
        adapter = self._registry.get(binding.provider)

        # 5. Resolve the executable binding (exact identity + env
        # overlay).  This is the canonical C11-D resolver the
        # autonomous lifecycle already uses; we deliberately reuse
        # it so the binding identity the consumer records equals the
        # binding identity the adapter invocation observes.
        capability_descriptor = binding.capability_profile_descriptor()
        try:
            resolved = self._resolve_executable(
                binding=binding,
                adapter=adapter,
                effort=resolved_effort,
                registry=owner.registry,
            )
        except OrchestratorModelInvocationError as exc:
            self._record_refusal_event(
                run_id=evidence_run_id,
                reason=exc.reason,
                detail={
                    **exc.detail,
                    "binding_id": binding.binding_id,
                    "provider": binding.provider,
                },
                correlation_id=correlation_id,
            )
            # Preserve the safe selected identity on the raised
            # exception so the bounded guidance helper can project it
            # onto ``orchestrator_guidance_result``.  The inner
            # ``_resolve_executable`` may already carry some identity
            # fields, but they are inconsistent across refusal paths;
            # we set the authoritative ones from the binding here.
            for key, value in _safe_selected_identity(binding=binding).items():
                exc.detail.setdefault(key, value)
            raise

        # 6. OpenCode instruction-compatibility veto.  Only the
        # OpenCode adapter opts in; for every other adapter the
        # hook is absent and the consumer treats the binding as
        # compatible-by-default, matching the existing autonomous
        # dispatch behaviour.
        compat_refusal = self._check_instruction_compatibility(
            binding=binding, adapter=adapter
        )
        if compat_refusal is not None:
            self._record_refusal_event(
                run_id=evidence_run_id,
                reason=compat_refusal,
                detail={
                    "binding_id": binding.binding_id,
                    "provider": binding.provider,
                    "model": binding.model,
                },
                correlation_id=correlation_id,
            )
            raise OrchestratorModelInvocationError(
                compat_refusal,
                message=(
                    f"provider instruction-compatibility refused for "
                    f"{binding.provider}/{binding.model}"
                ),
                detail={
                    **_safe_selected_identity(
                        binding=binding,
                        resolved=resolved,
                        resolved_effort=resolved_effort,
                    ),
                },
            )

        # 7. Record durable pre-invocation evidence.
        self._record_invocation_event(
            run_id=evidence_run_id,
            request=request,
            binding=binding,
            profile_id=resolved.profile_id,
            quota_pool_id=resolved.quota_pool_id,
            resolved_effort=resolved_effort,
            capability_descriptor=capability_descriptor,
            adapter_provider=str(getattr(adapter, "provider_name", binding.provider)),
        )

        # 8. Build the adapter request.  The env overlay comes
        # exclusively from the resolved binding; the consumer adds
        # no secret material, only the canonical non-secret
        # identity markers.
        scratch = _scratch_dir_for(request)
        scratch.mkdir(parents=True, exist_ok=True)
        prompt_text = self._compose_prompt(request)
        prompt_bytes = len(prompt_text.encode("utf-8"))
        worker_request = WorkerRequest(
            task=prompt_text,
            worktree_path=scratch,
            model=binding.model,
            timeout_seconds=request.timeout_seconds,
            env=dict(resolved.overlay),
            read_only=True,
            reasoning_effort=resolved_effort,
        )

        # 9. Run through the resolved adapter.  The consumer treats
        # any pre-Invocation refusal by the adapter (no live
        # binary, runtime error) as a typed
        # ``ORCH_MODEL_INVOCATION_REFUSED``; a Timeout becomes a
        # typed ``ORCH_MODEL_INVOCATION_TIMEOUT``; any other failure
        # becomes ``ORCH_MODEL_INVOCATION_FAILED``.
        assert adapter is not None
        if not adapter.is_available():
            outcome_event_id = self._record_outcome_event(
                run_id=evidence_run_id,
                status="REFUSED",
                binding=binding,
                profile_id=resolved.profile_id,
                quota_pool_id=resolved.quota_pool_id,
                resolved_effort=resolved_effort,
                summary="adapter unavailable",
                output_text="",
                error=f"adapter {binding.provider!r} is not available",
                duration_seconds=0.0,
                reason=REASON_INVOCATION_REFUSED,
                correlation_id=correlation_id,
            )
            raise OrchestratorModelInvocationError(
                REASON_INVOCATION_REFUSED,
                message=f"adapter {binding.provider!r} is not available",
                detail={
                    "evidence_event_id": outcome_event_id,
                    **_safe_selected_identity(
                        binding=binding,
                        resolved=resolved,
                        resolved_effort=resolved_effort,
                    ),
                },
            )

        try:
            worker_result: WorkerResult = adapter.run(worker_request)
        except Exception as exc:
            outcome_event_id = self._record_outcome_event(
                run_id=evidence_run_id,
                status="FAILED",
                binding=binding,
                profile_id=resolved.profile_id,
                quota_pool_id=resolved.quota_pool_id,
                resolved_effort=resolved_effort,
                summary="adapter raised",
                output_text="",
                error=str(exc),
                duration_seconds=0.0,
                reason=REASON_INVOCATION_FAILED,
                correlation_id=correlation_id,
            )
            raise OrchestratorModelInvocationError(
                REASON_INVOCATION_FAILED,
                message=f"adapter raised during Orchestrator invocation: {exc}",
                detail={
                    "evidence_event_id": outcome_event_id,
                    **_safe_selected_identity(
                        binding=binding,
                        resolved=resolved,
                        resolved_effort=resolved_effort,
                    ),
                },
            ) from exc

        status, reason = self._classify_worker_result(worker_result)
        outcome_duration = worker_result.duration_seconds or 0.0
        outcome_event_id = self._record_outcome_event(
            run_id=evidence_run_id,
            status=status,
            binding=binding,
            profile_id=resolved.profile_id,
            quota_pool_id=resolved.quota_pool_id,
            resolved_effort=resolved_effort,
            summary=worker_result.summary or "",
            output_text=worker_result.stdout or "",
            error=worker_result.error,
            duration_seconds=outcome_duration,
            reason=reason,
            correlation_id=correlation_id,
        )

        return OrchestratorModelResult(
            status=status,
            binding_id=binding.binding_id,
            profile_id=resolved.profile_id,
            quota_pool_id=resolved.quota_pool_id,
            provider=binding.provider,
            model=binding.model,
            backend=binding.backend.value,
            reasoning_effort=resolved_effort,
            output_text=worker_result.stdout or "",
            summary=worker_result.summary or "",
            error=worker_result.error,
            prompt_bytes=prompt_bytes,
            duration_seconds=outcome_duration,
            correlation_id=correlation_id,
            evidence_run_id=evidence_run_id,
            evidence_event_id=outcome_event_id,
            reason=reason,
            capability_descriptor=dict(capability_descriptor),
        )

    @staticmethod
    def _compose_prompt(request: OrchestratorModelRequest) -> str:
        """Render the canonical Orchestrator-model prompt text.

        The consumer never invents an Orchestrator-system identity;
        the system prompt is purely the owner's verbatim
        instruction, and the user prompt is the verbatim task.  The
        composed text is what the adapter sees, byte-for-byte, and
        is what the durable evidence records (prompt_bytes).
        """
        system = (request.system_prompt or "").strip()
        user = (request.user_prompt or "").strip()
        if system and user:
            return f"{system}\n\n{user}"
        return system or user

    @staticmethod
    def _classify_worker_result(
        worker_result: WorkerResult,
    ) -> tuple[str, str | None]:
        """Translate a :class:`WorkerResult` into consumer-level status/reason.

        Maps the proven worker-dispatch status vocabulary onto the
        consumer surface so the durable evidence is stable and
        comparable across worker and Orchestrator invocations.
        """
        if worker_result.status is AttemptStatus.SUCCESS:
            return "SUCCESS", None
        if worker_result.status is AttemptStatus.TIMEOUT:
            return "TIMEOUT", REASON_INVOCATION_TIMEOUT
        return "FAILED", REASON_INVOCATION_FAILED


# Deterministic bounds for the initial-guidance lifecycle call.
# The first semantic role the bounded Orchestrator model plays in a
# normal run is "initial objective interpretation / execution guidance"
# -- a single bounded invocation that produces a short advisory text
# the first worker sees as ``extra_sections``.  These constants
# enforce the "maximum Orchestrator semantic calls per normal run = 1"
# / "deterministic maximum returned text size" / "timeout" rules.
INITIAL_GUIDANCE_MAX_PROMPT_BYTES = 4096
INITIAL_GUIDANCE_MAX_OUTPUT_CHARS = 600
INITIAL_GUIDANCE_TIMEOUT_SECONDS = 30.0
INITIAL_GUIDANCE_RUN_ID_PREFIX = "orch_initial_guidance_"


def fetch_initial_orchestrator_guidance(
    *,
    service: Any,
    original_task: str,
    run_id: str,
    correlation_id: str | None = None,
) -> tuple[str, str | None]:
    """Invoke the bounded initial-orchestrator-guidance seam once.

    This is the ONE production caller the bounded Orchestrator model
    has in a normal run today.  It is intended to be called from the
    workflow before the FIRST implementation-worker dispatch; the
    first worker then receives the returned text as a bounded
    ``extra_sections`` advisory.

    The function is deliberately fail-closed: every documented refusal
    path returns ``("", reason)`` so the caller can record
    non-secret evidence without ever blocking the deterministic
    lifecycle.  Successful invocations return
    ``(truncated_text, None)``.

    C15 evidence correction: this helper now attributes the durable
    Orchestrator evidence to the existing parent run (``run_id``)
    rather than to a synthetic ``orch_initial_guidance_<run_id>`` /
    ``orchestrator_model_consumer`` run row.  The consumer is
    invoked with the real ``service.db`` and the parent ``run_id``
    under the narrow ``require_existing_run=True`` evidence seam;
    a missing parent run fails visibly rather than silently
    synthesising a run row.  Exactly ONE ``orchestrator_guidance_result``
    event is recorded against the parent run for each attempted
    invocation (caching callers consult the same result, so multiple
    cache reads emit zero additional events).

    The function is also a single-shot bound: it performs ONE bounded
    invocation per call and never recursively asks the Orchestrator
    model.  Multiple calls per run are caller-supplied and out of
    scope here.
    """
    from saberops.orchestrator_consumer import (
        OrchestratorModelConsumer,
        OrchestratorModelInvocationError,
        OrchestratorModelRequest,
    )

    bounded_task = (original_task or "").strip()
    bounded_task_bytes = bounded_task.encode("utf-8")
    if len(bounded_task_bytes) > INITIAL_GUIDANCE_MAX_PROMPT_BYTES:
        bounded_task = bounded_task_bytes[
            :INITIAL_GUIDANCE_MAX_PROMPT_BYTES
        ].decode("utf-8", errors="replace")

    service_db = getattr(service, "db", None)

    consumer = OrchestratorModelConsumer(
        db=service_db,
        registry=getattr(service, "registry", None),
        binding_store_path=getattr(service, "binding_store_path", None),
        owner_bindings=getattr(service, "owner_bindings", None),
        require_existing_run=True,
    )
    request = OrchestratorModelRequest(
        system_prompt=(
            "You are the bounded advisory guidance component of a "
            "deterministic project control plane.  Produce a short, "
            "bounded-text interpretation of the user's objective that "
            "calls out intent, important constraints, a reasonable "
            "implementation focus, ambiguity that flags, and validation "
            "concerns.  Do NOT invent provider / model / binding / "
            "shell / git / acceptance decisions -- those are owned "
            "deterministically by the control plane.  Return plain "
            "bounded text only; no lists, no JSON, no command lines."
        ),
        user_prompt=(
            "User objective (verbatim):\n"
            f"{bounded_task}\n\n"
            "Return a bounded advisory guidance text the first "
            "implementation worker will read as additional context."
        ),
        timeout_seconds=INITIAL_GUIDANCE_TIMEOUT_SECONDS,
        run_id=run_id,
        correlation_id=correlation_id,
    )

    outcome_status: str
    outcome_reason: str | None
    outcome_binding_id: str | None = None
    outcome_provider: str | None = None
    outcome_model: str | None = None
    outcome_profile_id: str | None = None
    outcome_quota_pool_id: str | None = None
    outcome_backend: str | None = None
    outcome_effort: ReasoningEffort | None = None
    outcome_output_bytes: int = 0
    outcome_output_digest: str | None = None

    result: OrchestratorModelResult | None = None
    try:
        result = consumer.invoke(request)
    except OrchestratorModelInvocationError as exc:
        # Distinguish ``NOT_REQUESTED`` (no Orchestrator model was ever
        # requested: ``REASON_NO_POLICY``) from ``FAILED`` (adapter
        # was reached but the invocation itself failed:
        # ``REASON_INVOCATION_FAILED``) from ``REFUSED`` (pre-invocation
        # typed refusal where a model/binding was requested but
        # deterministic policy / resolution / compatibility /
        # readiness refused execution).  The consumer raises the same
        # exception class for all refusal paths; the typed reason and
        # the presence of selected-binding identity on the exception
        # detail are the discriminators.
        exc_detail = getattr(exc, "detail", None) or {}
        if not isinstance(exc_detail, dict):
            exc_detail = {}
        if exc.reason == REASON_NO_POLICY:
            outcome_status = "NOT_REQUESTED"
            outcome_binding_id = None
            outcome_provider = None
            outcome_model = None
            outcome_profile_id = None
            outcome_quota_pool_id = None
            outcome_backend = None
            outcome_effort = None
        else:
            if exc.reason == REASON_INVOCATION_FAILED:
                outcome_status = "FAILED"
            else:
                outcome_status = "REFUSED"
            # The consumer enriches the exception detail with the
            # safe, non-secret selected-binding identity once that
            # identity is known.  Project the already-authoritative
            # safe fields onto the bounded result event so the durable
            # evidence carries the exact identity the consumer
            # observed, never a re-derived heuristic.  Pre-selection
            # refusals carry no identity and remain ``None``.
            safe_binding_id = exc_detail.get("binding_id")
            safe_provider = exc_detail.get("provider")
            safe_model = exc_detail.get("model")
            safe_profile_id = exc_detail.get("profile_id")
            safe_quota_pool_id = exc_detail.get("quota_pool_id")
            safe_backend = exc_detail.get("backend")
            safe_effort_str = exc_detail.get("effort")
            outcome_binding_id = (
                str(safe_binding_id)
                if isinstance(safe_binding_id, str) and safe_binding_id
                else None
            )
            outcome_provider = (
                str(safe_provider)
                if isinstance(safe_provider, str) and safe_provider
                else None
            )
            outcome_model = (
                str(safe_model)
                if isinstance(safe_model, str) and safe_model
                else None
            )
            outcome_profile_id = (
                str(safe_profile_id)
                if isinstance(safe_profile_id, str) and safe_profile_id
                else None
            )
            outcome_quota_pool_id = (
                str(safe_quota_pool_id)
                if isinstance(safe_quota_pool_id, str) and safe_quota_pool_id
                else None
            )
            outcome_backend = (
                str(safe_backend)
                if isinstance(safe_backend, str) and safe_backend
                else None
            )
            if (
                isinstance(safe_effort_str, str)
                and safe_effort_str
                and outcome_effort is None
            ):
                try:
                    outcome_effort = ReasoningEffort(safe_effort_str)
                except ValueError:
                    outcome_effort = None
        outcome_reason = exc.reason
    else:
        # Normalize the consumer's worker-aligned status vocabulary
        # (``SUCCESS`` / ``TIMEOUT`` / ``FAILED``) onto the bounded
        # result-event vocabulary (``SUCCEEDED`` / ``FAILED``).  The
        # consumer-level status remains authoritative for its own
        # evidence stream; this mapping is local to the bounded
        # guidance-result evidence contract.
        outcome_status = (
            "SUCCEEDED" if result.status == "SUCCESS" else "FAILED"
        )
        outcome_reason = result.reason
        outcome_binding_id = result.binding_id
        outcome_provider = result.provider
        outcome_model = result.model
        outcome_profile_id = result.profile_id
        outcome_quota_pool_id = result.quota_pool_id
        outcome_backend = result.backend
        outcome_effort = result.reasoning_effort
        outcome_output_bytes = len((result.output_text or "").encode("utf-8"))
        if result.output_text:
            outcome_output_digest = hashlib.sha256(
                result.output_text.encode("utf-8")
            ).hexdigest()

    # Record ONE canonical, sanitized result event against the parent run.
    # This is the bounded durable evidence the C15-XEC correction
    # requires: request-attempted + outcome, attributed to the
    # existing parent run only (no synthetic rows).  A ``db=None``
    # service is treated as a test seam and emits no DB event.
    _record_guidance_result_event(
        db=service_db,
        run_id=run_id,
        status=outcome_status,
        reason=outcome_reason,
        binding_id=outcome_binding_id,
        provider=outcome_provider,
        model=outcome_model,
        profile_id=outcome_profile_id,
        quota_pool_id=outcome_quota_pool_id,
        backend=outcome_backend,
        effort=outcome_effort,
        output_bytes=outcome_output_bytes,
        output_digest=outcome_output_digest,
        correlation_id=correlation_id,
    )

    if result is None or outcome_status != "SUCCEEDED":
        return "", outcome_reason or "ORCH_MODEL_NON_SUCCESS"

    output = (result.output_text or "").strip()
    if len(output) > INITIAL_GUIDANCE_MAX_OUTPUT_CHARS:
        output = output[:INITIAL_GUIDANCE_MAX_OUTPUT_CHARS].rstrip()
    if not output:
        return "", "ORCH_MODEL_EMPTY_OUTPUT"
    return output, None


def _record_guidance_result_event(
    *,
    db: Database | None,
    run_id: str,
    status: str,
    reason: str | None,
    binding_id: str | None,
    provider: str | None,
    model: str | None,
    profile_id: str | None,
    quota_pool_id: str | None,
    backend: str | None,
    effort: ReasoningEffort | None,
    output_bytes: int,
    output_digest: str | None,
    correlation_id: str | None,
) -> int | None:
    """Record one canonical ``orchestrator_guidance_result`` event.

    Stable status vocabulary: ``SUCCEEDED``, ``REFUSED``, ``FAILED``.
    Carries only safe metadata (exact identity, effort, sanitized
    typed reason, output length and deterministic digest).  No secret
    material; no full model output.  A ``db=None`` service seam
    (hermetic tests) is a no-op: the helper still returns its
    bounded text/reason to the caller.
    """
    if db is None:
        return None
    payload: dict[str, Any] = {
        "status": status,
        "reason": reason,
        "binding_id": binding_id,
        "provider": provider,
        "model": model,
        "profile_id": profile_id,
        "quota_pool_id": quota_pool_id,
        "backend": backend,
        "effort": effort.value if effort is not None else None,
        "output_bytes": output_bytes,
        "output_digest": output_digest,
        "correlation_id": correlation_id,
        "recorded_at": current_iso_timestamp(),
    }
    return int(db.record_event(run_id, "orchestrator_guidance_result", payload=payload))


__all__ = [
    "INITIAL_GUIDANCE_MAX_OUTPUT_CHARS",
    "INITIAL_GUIDANCE_MAX_PROMPT_BYTES",
    "INITIAL_GUIDANCE_RUN_ID_PREFIX",
    "INITIAL_GUIDANCE_TIMEOUT_SECONDS",
    "OrchestratorModelConsumer",
    "OrchestratorModelRequest",
    "OrchestratorModelResult",
    "OrchestratorModelInvocationError",
    "REASON_NO_POLICY",
    "REASON_BINDING_STORE_UNREADABLE",
    "REASON_SELECTION_UNRESOLVED",
    "REASON_NOT_ORCHESTRATOR_ROLE",
    "REASON_NO_ADAPTER",
    "REASON_RESOLUTION_FAILED",
    "REASON_EFFORT_UNSUPPORTED",
    "REASON_EFFORT_UNAUTHORIZED",
    "REASON_INSTRUCTION_INCOMPATIBLE",
    "REASON_INSTRUCTION_UNKNOWN",
    "REASON_INVOCATION_REFUSED",
    "REASON_INVOCATION_FAILED",
    "REASON_INVOCATION_TIMEOUT",
    "fetch_initial_orchestrator_guidance",
]
