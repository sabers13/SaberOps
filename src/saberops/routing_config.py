"""Canonical routing configuration loader with strict validation and snapshots."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.models import WorkerCandidate

SCHEMA_VERSION = 1

# Closed registry of allowed canonical ids
ALLOWED_CANDIDATES: frozenset[str] = frozenset(
    {
        "antigravity/gemini-3.7-flash",
        "opencode/opencode-go/mimo-v2.5",
        "opencode/opencode-go/deepseek-v4-flash",
        "opencode/opencode-go/deepseek-v4-pro",
        "opencode/opencode-go/muse-spark-1.2-contributor",
        "opencode/opencode-go/ox-alpha-free",
        "codex/gpt-5.6-terra",
        "codex/gpt-5.6-sol",
        "codex/gpt-5.6-luna",
        "cline/cline-pass/glm-5.3-flash",
        "cline/cline-pass/glm-5.3",
        "cline/cline-pass/mimo-v2.5",
        "cline/cline-pass/deepseek-v4-flash",
        "cline/cline-pass/deepseek-v4-pro",
        "cline/cline-pass/minimax-m3",
        "opencode/opencode/big-pickle",
        "opencode/opencode/mimo-v2.5-free",
        "opencode/opencode/hy3-free",
        "opencode/opencode/nemotron-3-ultra-free",
        "opencode/opencode/nemotron-3.5-lightning-free",
        "opencode/opencode/muse-spark-1.2-contributor-free",
        "opencode/opencode/deepseek-v4-flash",
        "opencode/opencode/minimax-m3",
        "opencode/opencode/qwen3.7-plus",
        "opencode/opencode/deepseek-v4-pro",
    }
)

#: Additive canonical top-level key recording owner-approved, discovery-evidenced
#: executable candidates that are not part of the closed static registry.  It is
#: part of the *same* canonical routing document (never a second routing file).
_APPROVED_TOP_KEY = "approved_candidates"

# Context keys mapping: (top_key, sub_key)
CONTEXT_KEYS: tuple[tuple[str, str], ...] = (
    ("T1", "default"),
    ("T2", "training_allowed"),
    ("T2", "training_denied_off_peak"),
    ("T2", "training_denied_peak"),
    ("T3", "off_peak"),
    ("T3", "peak"),
)


class RoutingConfigError(ValueError):
    """Fail-closed error for invalid routing configuration."""


def _canonical_chain_key(top: str, sub: str) -> str:
    return f"{top}.{sub}"


# Candidate ids requiring training permission (training_allowed == True)
TRAINING_REQUIRED_CANDIDATES: frozenset[str] = frozenset(
    {
        "opencode/opencode-go/muse-spark-1.2-contributor",
        "opencode/opencode/big-pickle",
        "opencode/opencode/mimo-v2.5-free",
        "opencode/opencode/hy3-free",
        "opencode/opencode/nemotron-3-ultra-free",
        "opencode/opencode/nemotron-3.5-lightning-free",
        "opencode/opencode/muse-spark-1.2-contributor-free",
    }
)


def requires_training_permission(candidate_id: str) -> bool:
    """Check if candidate ID requires training permission (training_allowed == True)."""
    norm = candidate_id.strip().lower()
    return norm in TRAINING_REQUIRED_CANDIDATES or "muse" in norm


def _is_muse_id(cid: str) -> bool:
    return requires_training_permission(cid)


def get_user_routing_path() -> Path:
    """Return the canonical public owner routing-config path."""
    from saberops.paths import get_user_routing_path as _canonical

    return _canonical()


def _packaged_default_path() -> Path:
    return Path(__file__).resolve().parent / "defaults" / "routing.json"


def _parse_candidate_id(cid: str) -> WorkerCandidate:
    # Format: provider/model ; provider may contain /? but allowed set is known
    # We split on first "/"
    if "/" not in cid:
        raise RoutingConfigError(f"Invalid candidate id '{cid}': missing provider prefix")
    provider, model = cid.split("/", 1)
    # For opencode-go prefix, provider is opencode, model is opencode-go/...
    # But our provider is extracted as per canonical id: split on first slash
    # For consistency, use first slash as provider delimiter
    # e.g. "opencode/opencode-go/deepseek..." -> provider opencode, model opencode-go/...
    return WorkerCandidate(provider=provider, model=model)


def _validate_context_chain(
    chain: Any,
    ctx_key: str,
    source_label: str,
    approved: Mapping[str, DynamicCandidate] | None = None,
) -> list[str]:
    if not isinstance(chain, list) or len(chain) == 0:
        raise RoutingConfigError(
            f"Routing config at {source_label}: context {ctx_key} must be non-empty list"
        )
    seen: set[str] = set()
    cids: list[str] = []
    for entry in chain:
        if isinstance(entry, str):
            cid = entry
        elif isinstance(entry, dict):
            provider = entry.get("provider")
            model = entry.get("model")
            if not isinstance(provider, str) or not isinstance(model, str):
                raise RoutingConfigError(
                    f"Routing config at {source_label}: context {ctx_key} entry {entry!r} missing or invalid 'provider'/'model' string fields"  # noqa: E501
                )
            cid = f"{provider}/{model}"
            try:
                parsed = _parse_candidate_id(cid)
            except RoutingConfigError as exc:
                raise RoutingConfigError(
                    f"Routing config at {source_label}: context {ctx_key} invalid candidate object {entry!r}: {exc}"  # noqa: E501
                ) from exc
            if parsed.provider != provider or parsed.model != model:
                raise RoutingConfigError(
                    f"Routing config at {source_label}: context {ctx_key} entry provider/model inconsistency {entry!r}"  # noqa: E501
                )
        else:
            raise RoutingConfigError(
                f"Routing config at {source_label}: context {ctx_key} entry {entry!r} must be string or candidate object"  # noqa: E501
            )
        if cid in seen:
            raise RoutingConfigError(
                f"Routing config at {source_label}: context {ctx_key} duplicate candidate {cid!r}"
            )
        seen.add(cid)
        approved_entry = approved.get(cid) if approved else None
        # Candidate *identity validity* and candidate *eligibility* are distinct:
        # a candidate is identity-valid when it is either in the closed static
        # registry or carries an owner approval record with discovery + execution
        # evidence.  Arbitrary unvalidated strings still fail closed here.
        if cid not in ALLOWED_CANDIDATES and approved_entry is None:
            raise RoutingConfigError(
                f"Routing config at {source_label}: context {ctx_key} unknown candidate id {cid!r}"
            )
        if "training_denied" in ctx_key:
            if approved_entry is not None:
                # A dynamic candidate's training requirement is classified from
                # evidence.  UNKNOWN fails closed here: it is never inferred to be
                # "training allowed".
                if approved_entry.training_required != "FALSE":
                    raise RoutingConfigError(
                        f"Routing config at {source_label}: context {ctx_key} candidate {cid!r} "
                        f"training requirement is {approved_entry.training_required} "
                        f"(fail closed in {ctx_key})"
                    )
            elif requires_training_permission(cid):
                raise RoutingConfigError(
                    f"Routing config at {source_label}: context {ctx_key} candidate {cid!r} "
                    f"requires training permission (forbidden in {ctx_key})"
                )
        cids.append(cid)
    return cids


def _validate_structure(
    data: Any, source_path: Path | None, *, approved: Mapping[str, DynamicCandidate] | None = None
) -> dict[str, list[str]]:
    source_label = str(source_path) if source_path else "packaged default"
    if not isinstance(data, dict):
        raise RoutingConfigError(f"Routing config at {source_label}: top-level must be JSON object")
    # Check unknown top-level keys
    allowed_top = {"schema_version", "T1", "T2", "T3", _APPROVED_TOP_KEY}
    unknown = set(data.keys()) - allowed_top
    if unknown:
        raise RoutingConfigError(
            f"Routing config at {source_label}: unknown top-level keys {sorted(unknown)}"
        )
    # schema_version
    if "schema_version" not in data:
        raise RoutingConfigError(f"Routing config at {source_label}: missing schema_version")
    sv = data["schema_version"]
    if type(sv) is not int or sv != SCHEMA_VERSION:
        raise RoutingConfigError(
            f"Routing config at {source_label}: schema_version must be integer {SCHEMA_VERSION}, got {sv!r}"  # noqa: E501
        )
    if approved is None:
        approved = _parse_approved_candidates(data, source_label)
    # Check each context present and validate
    result: dict[str, list[str]] = {}
    for top, sub in CONTEXT_KEYS:
        ctx_key = _canonical_chain_key(top, sub)
        top_val = data.get(top)
        if not isinstance(top_val, dict):
            raise RoutingConfigError(
                f"Routing config at {source_label}: missing or invalid section {top!r} for context {ctx_key}"  # noqa: E501
            )
        if sub not in top_val:
            raise RoutingConfigError(f"Routing config at {source_label}: missing context {ctx_key}")
        chain = top_val[sub]
        result[ctx_key] = _validate_context_chain(
            chain, ctx_key, source_label, approved=approved
        )
        # Check unknown sub-keys within this top
        allowed_subs_for_top = {s for t, s in CONTEXT_KEYS if t == top}
        unknown_subs = set(top_val.keys()) - allowed_subs_for_top
        if unknown_subs:
            raise RoutingConfigError(
                f"Routing config at {source_label}: unknown keys in {top!r}: {sorted(unknown_subs)}"
            )
    return result


def _content_hash(
    validated: dict[str, list[str]],
    schema_version: int,
    approved: Mapping[str, DynamicCandidate] | None = None,
) -> str:
    body: dict[str, Any] = {"schema_version": schema_version, "chains": validated}
    # Only owner-approved dynamic candidates contribute; an empty approval set
    # hashes exactly as before, so existing static snapshots stay byte-stable.
    if approved:
        body[_APPROVED_TOP_KEY] = {
            cid: entry.to_dict() for cid, entry in sorted(approved.items())
        }
    canonical = json.dumps(body, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoutingSnapshot:
    """Frozen snapshot of effective routing configuration."""

    schema_version: int
    source: str  # "user" or "default"
    source_path: str | None
    content_hash: str
    # Six ordered lists as WorkerCandidate
    t1_default: list[WorkerCandidate]
    t2_training_allowed: list[WorkerCandidate]
    t2_training_denied_off_peak: list[WorkerCandidate]
    t2_training_denied_peak: list[WorkerCandidate]
    t3_off_peak: list[WorkerCandidate]
    t3_peak: list[WorkerCandidate]
    #: Owner-approved, discovery-evidenced executable candidates outside the
    #: closed static registry.  Chains still carry the canonical
    #: ``provider/model`` identity; this is the evidence binding that lets the
    #: validated loader accept that identity.  Empty for static-only configs.
    approved_candidates: tuple[DynamicCandidate, ...] = ()

    def chain_for(self, tier: str, training_allowed: bool, is_peak: bool) -> list[WorkerCandidate]:
        if tier == "T1":
            return list(self.t1_default)
        if tier == "T2":
            if training_allowed:
                return list(self.t2_training_allowed)
            return list(
                self.t2_training_denied_peak if is_peak else self.t2_training_denied_off_peak
            )
        if tier == "T3":
            return list(self.t3_peak if is_peak else self.t3_off_peak)
        return list(self.t1_default)


def snapshot_references_exact_bindings(snapshot: RoutingSnapshot | None) -> bool:
    """True iff the frozen routing snapshot can produce an exact-binding candidate.

    A routing chain candidate gains its exact ``binding_id`` only from an
    owner approval record (``approved_candidates``).  When at least one
    approval names a non-empty ``binding_id``, an ordinary Run created from
    this snapshot may dispatch an exact binding and must therefore freeze the
    effective owner BindingRegistry alongside the snapshot
    (C15-XB-FREEZE-01).  Static-only snapshots return ``False`` so the
    historical provider/model path neither loads nor requires owner bindings.
    """
    if snapshot is None:
        return False
    return any(
        str(getattr(entry, "binding_id", "") or "").strip()
        for entry in getattr(snapshot, "approved_candidates", ())
    )


def _build_snapshot(
    validated: dict[str, list[str]],
    schema_version: int,
    source: str,
    source_path: Path | None,
    approved: Mapping[str, DynamicCandidate] | None = None,
) -> RoutingSnapshot:
    ch = _content_hash(validated, schema_version, approved)

    def to_candidates(key: str) -> list[WorkerCandidate]:
        return [_parse_candidate_id(cid) for cid in validated[key]]

    return RoutingSnapshot(
        schema_version=schema_version,
        source=source,
        source_path=str(source_path) if source_path else None,
        content_hash=ch,
        t1_default=to_candidates("T1.default"),
        t2_training_allowed=to_candidates("T2.training_allowed"),
        t2_training_denied_off_peak=to_candidates("T2.training_denied_off_peak"),
        t2_training_denied_peak=to_candidates("T2.training_denied_peak"),
        t3_off_peak=to_candidates("T3.off_peak"),
        t3_peak=to_candidates("T3.peak"),
        approved_candidates=tuple(
            entry for _, entry in sorted((approved or {}).items())
        ),
    )


def load_packaged_default() -> RoutingSnapshot:
    """Load and validate the packaged default routing.json."""
    path = _packaged_default_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RoutingConfigError(f"Packaged default routing.json not found at {path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RoutingConfigError(f"Packaged default routing.json invalid JSON: {exc}") from exc
    validated = _validate_structure(data, path)
    approved = _parse_approved_candidates(data, str(path))
    return _build_snapshot(validated, SCHEMA_VERSION, "default", None, approved=approved)


def load_effective_routing() -> RoutingSnapshot:
    """Load effective routing: user file if present, else packaged default.

    Raises RoutingConfigError on invalid user file (fail-closed).
    """
    user_path = get_user_routing_path()
    if user_path.is_file():
        try:
            raw = user_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RoutingConfigError(f"Routing config at {user_path}: cannot read: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RoutingConfigError(f"Routing config at {user_path}: invalid JSON: {exc}") from exc
        validated = _validate_structure(data, user_path)
        approved = _parse_approved_candidates(data, str(user_path))
        return _build_snapshot(validated, SCHEMA_VERSION, "user", user_path, approved=approved)
    return load_packaged_default()


def snapshot_to_payload(snapshot: RoutingSnapshot, created_at: str) -> dict[str, Any]:
    """Serialize snapshot to DB payload shape."""
    payload: dict[str, Any] = {
        "schema_version": snapshot.schema_version,
        "source": snapshot.source,
        "source_path": snapshot.source_path,
        "content_hash": snapshot.content_hash,
        "chains": {
            "T1.default": [{"provider": c.provider, "model": c.model} for c in snapshot.t1_default],
            "T2.training_allowed": [
                {"provider": c.provider, "model": c.model} for c in snapshot.t2_training_allowed
            ],
            "T2.training_denied_off_peak": [
                {"provider": c.provider, "model": c.model}
                for c in snapshot.t2_training_denied_off_peak
            ],
            "T2.training_denied_peak": [
                {"provider": c.provider, "model": c.model} for c in snapshot.t2_training_denied_peak
            ],
            "T3.off_peak": [
                {"provider": c.provider, "model": c.model} for c in snapshot.t3_off_peak
            ],
            "T3.peak": [{"provider": c.provider, "model": c.model} for c in snapshot.t3_peak],
        },
        "created_at": created_at,
    }
    if snapshot.approved_candidates:
        payload[_APPROVED_TOP_KEY] = {
            entry.candidate_id: entry.to_dict() for entry in snapshot.approved_candidates
        }
    return payload


def _parse_approved_candidates(
    payload: Mapping[str, Any], source_label: str
) -> dict[str, DynamicCandidate]:
    raw = payload.get(_APPROVED_TOP_KEY)
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise RoutingConfigError(
            f"Routing config at {source_label}: '{_APPROVED_TOP_KEY}' must be a JSON object"
        )
    approved: dict[str, DynamicCandidate] = {}
    for key, value in raw.items():
        candidate_id = str(key).strip()
        if not candidate_id:
            raise RoutingConfigError(
                f"Routing config at {source_label}: approved candidate id must not be empty"
            )
        if not isinstance(value, Mapping):
            raise RoutingConfigError(
                f"Routing config at {source_label}: approved candidate "
                f"{candidate_id!r} must be an object"
            )
        try:
            approved[candidate_id] = DynamicCandidate.from_mapping(candidate_id, value)
        except (TypeError, ValueError) as exc:
            raise RoutingConfigError(
                f"Routing config at {source_label}: invalid approved candidate "
                f"{candidate_id!r}: {exc}"
            ) from exc
    return approved


def payload_to_snapshot(payload: Any, run_id: str | None = None) -> RoutingSnapshot:
    """Reconstruct snapshot from persisted payload with strict fail-closed validation."""
    source_label = f"persisted snapshot for run '{run_id}'" if run_id else "persisted snapshot"
    if not isinstance(payload, dict):
        raise RoutingConfigError(f"Routing config at {source_label}: payload must be JSON object")
    if "schema_version" not in payload:
        raise RoutingConfigError(f"Routing config at {source_label}: missing schema_version")
    sv = payload["schema_version"]
    if type(sv) is not int or sv != SCHEMA_VERSION:
        raise RoutingConfigError(
            f"Routing config at {source_label}: schema_version must be integer {SCHEMA_VERSION}, got {sv!r}"  # noqa: E501
        )
    chains_obj = payload.get("chains")
    if not isinstance(chains_obj, dict):
        raise RoutingConfigError(
            f"Routing config at {source_label}: missing or invalid 'chains' object"
        )
    approved = _parse_approved_candidates(payload, source_label)
    expected_contexts = {_canonical_chain_key(top, sub) for top, sub in CONTEXT_KEYS}
    unknown_contexts = set(chains_obj.keys()) - expected_contexts
    if unknown_contexts:
        raise RoutingConfigError(
            f"Routing config at {source_label}: unknown context keys in chains: {sorted(unknown_contexts)}"  # noqa: E501
        )
    validated: dict[str, list[str]] = {}
    for top, sub in CONTEXT_KEYS:
        ctx_key = _canonical_chain_key(top, sub)
        if ctx_key not in chains_obj:
            raise RoutingConfigError(
                f"Routing config at {source_label}: missing context {ctx_key!r}"
            )
        chain = chains_obj[ctx_key]
        validated[ctx_key] = _validate_context_chain(
            chain, ctx_key, source_label, approved=approved
        )

    # C15-XR-01: a persisted snapshot is self-consistent only when its stored
    # digest equals the canonical recomputation over the validated content.
    # A present digest is never trusted without verification: it must be a
    # non-empty string and must match exactly, otherwise reconstruction fails
    # closed (routing under digest B while executing content A is forbidden,
    # as is silently replacing a mismatched digest and continuing).
    # Only a genuinely absent legacy payload (key missing, or explicit JSON
    # null) may have its canonical digest reconstructed.
    canonical_hash = _content_hash(validated, sv, approved)
    if "content_hash" not in payload or payload.get("content_hash") is None:
        ch = canonical_hash
    else:
        stored = payload.get("content_hash")
        if not isinstance(stored, str) or not stored:
            raise RoutingConfigError(
                f"Routing config at {source_label}: content_hash must be "
                f"a non-empty string digest, got {stored!r}"
            )
        if stored != canonical_hash:
            raise RoutingConfigError(
                f"Routing config at {source_label}: content_hash mismatch: "
                f"persisted snapshot content does not match its stored digest "
                f"(fail closed; refusing to route under a stale digest)"
            )
        ch = stored

    def to_candidates(k: str) -> list[WorkerCandidate]:
        return [_parse_candidate_id(cid) for cid in validated[k]]

    return RoutingSnapshot(
        schema_version=sv,
        source=str(payload.get("source", "persisted")),
        source_path=payload.get("source_path")
        if isinstance(payload.get("source_path"), str)
        else None,
        content_hash=str(ch),
        t1_default=to_candidates("T1.default"),
        t2_training_allowed=to_candidates("T2.training_allowed"),
        t2_training_denied_off_peak=to_candidates("T2.training_denied_off_peak"),
        t2_training_denied_peak=to_candidates("T2.training_denied_peak"),
        t3_off_peak=to_candidates("T3.off_peak"),
        t3_peak=to_candidates("T3.peak"),
        approved_candidates=tuple(entry for _, entry in sorted(approved.items())),
    )


def init_user_config(force: bool = False) -> Path:
    """Copy packaged default to user path, refusing to overwrite unless force=True."""
    user_path = get_user_routing_path()
    if user_path.exists() and not force:
        raise RoutingConfigError(
            f"User routing config already exists at {user_path}; use --force to overwrite"
        )
    user_path.parent.mkdir(parents=True, exist_ok=True)
    src = _packaged_default_path()
    data = src.read_bytes()
    user_path.write_bytes(data)
    return user_path


# ---------------------------------------------------------------------------
# C15: canonical routing-configuration mutation seam.
#
# Routing configuration remains exclusively owned by this module.  The
# helpers below let operator surfaces reuse the *existing* validated
# persistence path -- there is deliberately no second routing document,
# no shadow chain store and no alternate candidate vocabulary.  Every
# mutation is expressed as a full document and re-validated by the same
# ``_validate_structure`` used by the loader, so tier semantics, ordering
# and training-denial rules stay authoritative.  Candidate identity is
# validated against the closed static registry OR against an owner approval
# record (``approved_candidates``) carrying discovery + execution evidence;
# arbitrary unvalidated strings are still refused rather than persisted.
# ---------------------------------------------------------------------------


#: Tri-state vocabulary shared by routing-config approval evidence.  Kept as
#: plain strings so ``control_plane`` never imports ``model_access`` (discovery
#: evidence must not become policy authority).
_APPROVAL_TRISTATE: frozenset[str] = frozenset({"TRUE", "FALSE", "UNKNOWN"})


@dataclass(frozen=True)
class DynamicCandidate:
    """Owner-approved, discovery-evidenced executable routing candidate.

    This is the dynamic candidate *contract* that lets an owner add a
    newly-discovered, executable provider model to routing without every
    future model id having to be compiled into the static
    :data:`ALLOWED_CANDIDATES` catalog.  It deliberately keeps three facts
    separate:

    * **identity validity** -- ``candidate_id`` must round-trip exactly
      through :func:`format_candidate_id` / :func:`_parse_candidate_id`;
    * **discovery evidence** -- ``discovery_status`` must be ``TRUE``: the
      provider/backend actually enumerated this exact id (a manual,
      owner-typed id is never discovery-verified);
    * **execution evidence** -- ``execution_support`` must be ``SUPPORTED``:
      a real executable transport exists for this connection/backend/model.

    The record also carries ``binding_id``: the *exact* C11-B
    :class:`~saberops.control_plane.bindings.ExecutionBinding`
    identity this candidate executes through.  A raw ``provider/model``
    string never substitutes for it; the approval is only meaningful when
    the referenced binding exists and matches provider/model exactly.  The
    routing document keeps the canonical ``provider/model`` identity for
    ordering and C07, while real execution recovers ``binding_id`` from the
    canonical registry.

    It carries no provider credential, no endpoint and no secret material.
    Eligibility in a given context is still decided by the existing C07
    routing authority: this record only makes the identity admissible.
    ``training_required`` is tri-state; ``UNKNOWN`` fails closed in
    training-denied contexts and is never inferred to mean "allowed".
    """

    candidate_id: str
    provider: str
    model: str
    connection_id: str
    backend: str
    binding_id: str
    discovery_status: str = "TRUE"
    execution_support: str = "SUPPORTED"
    training_required: str = "UNKNOWN"
    capabilities: tuple[tuple[str, str], ...] = ()
    approved_at: str = ""

    def __post_init__(self) -> None:
        candidate_id = self.candidate_id.strip()
        if not candidate_id:
            raise RoutingConfigError("dynamic candidate id must not be empty")
        provider = self.provider.strip()
        model = self.model.strip()
        if not provider or not model:
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: provider and model must not be empty"
            )
        expected = format_candidate_id(provider, model)
        if candidate_id != expected:
            raise RoutingConfigError(
                f"dynamic candidate identity mismatch: {candidate_id!r} != {expected!r}"
            )
        parsed = _parse_candidate_id(candidate_id)
        if parsed.provider != provider or parsed.model != model:
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: provider/model fields are inconsistent"
            )
        if not self.connection_id.strip():
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: connection_id must not be empty"
            )
        if not self.backend.strip():
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: backend must not be empty"
            )
        binding_id = self.binding_id.strip()
        if not binding_id:
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: binding_id must not be empty "
                "(a dynamic candidate must reference an exact C11-B execution binding)"
            )
        discovery_status = self.discovery_status.strip().upper()
        if discovery_status != "TRUE":
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: discovery_status must be TRUE, "
                f"got {self.discovery_status!r}"
            )
        execution_support = self.execution_support.strip().upper()
        if execution_support != "SUPPORTED":
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: execution_support must be SUPPORTED, "
                f"got {self.execution_support!r}"
            )
        training_required = self.training_required.strip().upper()
        if training_required not in _APPROVAL_TRISTATE:
            raise RoutingConfigError(
                f"dynamic candidate {candidate_id!r}: training_required must be one of "
                f"{sorted(_APPROVAL_TRISTATE)}, got {self.training_required!r}"
            )
        cleaned_caps: list[tuple[str, str]] = []
        for name, state in self.capabilities:
            key = str(name).strip().lower()
            value = str(state).strip().upper()
            if not key:
                raise RoutingConfigError(
                    f"dynamic candidate {candidate_id!r}: capability name must not be empty"
                )
            if value not in _APPROVAL_TRISTATE:
                raise RoutingConfigError(
                    f"dynamic candidate {candidate_id!r}: capability {key!r} state must be "
                    f"one of {sorted(_APPROVAL_TRISTATE)}, got {state!r}"
                )
            cleaned_caps.append((key, value))
        object.__setattr__(self, "candidate_id", candidate_id)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "model", model)
        object.__setattr__(self, "connection_id", self.connection_id.strip())
        object.__setattr__(self, "backend", self.backend.strip().lower())
        object.__setattr__(self, "binding_id", binding_id)
        object.__setattr__(self, "discovery_status", discovery_status)
        object.__setattr__(self, "execution_support", execution_support)
        object.__setattr__(self, "training_required", training_required)
        object.__setattr__(self, "capabilities", tuple(sorted(cleaned_caps)))
        object.__setattr__(self, "approved_at", str(self.approved_at))

    def to_dict(self) -> dict[str, Any]:
        """Canonical non-secret rendering for the routing document."""
        return {
            "provider": self.provider,
            "model": self.model,
            "connection_id": self.connection_id,
            "backend": self.backend,
            "binding_id": self.binding_id,
            "discovery_status": self.discovery_status,
            "execution_support": self.execution_support,
            "training_required": self.training_required,
            "capabilities": [
                {"name": name, "state": state} for name, state in self.capabilities
            ],
            "approved_at": self.approved_at,
        }

    @classmethod
    def from_mapping(
        cls, candidate_id: str, payload: Mapping[str, Any]
    ) -> DynamicCandidate:
        """Reconstruct an approval record from its durable mapping."""
        if not isinstance(payload, Mapping):
            raise RoutingConfigError("approved candidate mapping must be a mapping")
        raw_caps = payload.get("capabilities", ())
        capabilities: list[tuple[str, str]] = []
        if isinstance(raw_caps, Mapping):
            capabilities = [(str(name), str(state)) for name, state in raw_caps.items()]
        elif isinstance(raw_caps, list):
            for item in raw_caps:
                if not isinstance(item, Mapping) or "name" not in item:
                    raise RoutingConfigError("invalid capability entry")
                capabilities.append((str(item["name"]), str(item.get("state", "UNKNOWN"))))
        elif raw_caps not in (None, ()):
            raise RoutingConfigError("capabilities must be a mapping or a list")
        return cls(
            candidate_id=str(candidate_id),
            provider=str(payload.get("provider", "")),
            model=str(payload.get("model", "")),
            connection_id=str(payload.get("connection_id", "")),
            backend=str(payload.get("backend", "")),
            binding_id=str(payload.get("binding_id", "")),
            discovery_status=str(payload.get("discovery_status", "UNKNOWN")),
            execution_support=str(payload.get("execution_support", "UNKNOWN")),
            training_required=str(payload.get("training_required", "UNKNOWN")),
            capabilities=tuple(capabilities),
            approved_at=str(payload.get("approved_at", "")),
        )


def format_candidate_id(provider: str, model: str) -> str:
    """Return the canonical ``provider/model`` candidate id.

    The provider is the part before the first ``/`` and the model keeps
    any remaining slashes (e.g. ``opencode`` + ``opencode-go/x`` ->
    ``opencode/opencode-go/x``).  This mirrors :func:`_parse_candidate_id`
    exactly and performs no aliasing.
    """
    cleaned_provider = provider.strip()
    cleaned_model = model.strip()
    if not cleaned_provider or "/" in cleaned_provider:
        raise RoutingConfigError(f"invalid provider for candidate id: {provider!r}")
    if not cleaned_model:
        raise RoutingConfigError("invalid model for candidate id: must not be empty")
    return f"{cleaned_provider}/{cleaned_model}"


def load_effective_routing_payload() -> dict[str, Any]:
    """Return the raw effective routing document (user file, else packaged default).

    This is the canonical document the loader itself consumes; callers
    that want to mutate routing start from exactly this value.
    """
    user_path = get_user_routing_path()
    if user_path.is_file():
        try:
            raw = user_path.read_text(encoding="utf-8")
        except OSError as exc:
            raise RoutingConfigError(
                f"Routing config at {user_path}: cannot read: {exc}"
            ) from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RoutingConfigError(
                f"Routing config at {user_path}: invalid JSON: {exc}"
            ) from exc
        if not isinstance(data, dict):
            raise RoutingConfigError(
                f"Routing config at {user_path}: top-level must be JSON object"
            )
        return data
    path = _packaged_default_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RoutingConfigError(f"Packaged default routing.json not found at {path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RoutingConfigError(f"Packaged default routing.json invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RoutingConfigError("Packaged default routing.json top-level must be JSON object")
    return data


def add_candidate_to_chain(
    payload: Mapping[str, Any],
    *,
    top: str,
    sub: str,
    candidate_id: str,
) -> dict[str, Any]:
    """Return a validated copy of ``payload`` with ``candidate_id`` appended.

    The candidate is appended at the end of the named tier/context chain,
    which preserves every existing ordering decision: this helper never
    reorders, ranks or substitutes.  Adding an already-present candidate
    is idempotent.  The result is re-validated by the canonical
    structure validator, so unknown candidate ids, training-denied
    placements and unknown contexts fail closed.
    """
    context = _canonical_chain_key(top, sub)
    if context not in {_canonical_chain_key(t, s) for t, s in CONTEXT_KEYS}:
        raise RoutingConfigError(f"unknown routing context {context!r}")
    cleaned = candidate_id.strip()
    if not cleaned:
        raise RoutingConfigError("candidate id must not be empty")
    data: dict[str, Any] = json.loads(json.dumps(payload))
    section = data.get(top)
    if not isinstance(section, dict):
        raise RoutingConfigError(f"routing section {top!r} is missing or invalid")
    chain = section.get(sub)
    if not isinstance(chain, list):
        raise RoutingConfigError(f"routing context {context!r} is missing or invalid")
    if cleaned not in chain:
        chain.append(cleaned)
    _validate_structure(data, None)
    return data


def add_dynamic_candidate_to_chain(
    payload: Mapping[str, Any],
    *,
    top: str,
    sub: str,
    candidate: DynamicCandidate,
) -> dict[str, Any]:
    """Register an owner approval and append its candidate to a chain.

    This is the dynamic-candidate analogue of
    :func:`add_candidate_to_chain`: it records the non-secret approval
    evidence under ``approved_candidates`` in the *same* canonical routing
    document and appends the canonical ``provider/model`` identity.  The
    whole document is then re-validated by :func:`_validate_structure`, so
    tier semantics, ordering, identity validity and training-denial rules
    stay authoritative.  Appending is idempotent; ordering is never
    rewritten.
    """
    context = _canonical_chain_key(top, sub)
    if context not in {_canonical_chain_key(t, s) for t, s in CONTEXT_KEYS}:
        raise RoutingConfigError(f"unknown routing context {context!r}")
    data: dict[str, Any] = json.loads(json.dumps(payload))
    approved = data.get(_APPROVED_TOP_KEY)
    if not isinstance(approved, dict):
        approved = {}
        data[_APPROVED_TOP_KEY] = approved
    approved[candidate.candidate_id] = candidate.to_dict()
    section = data.get(top)
    if not isinstance(section, dict):
        raise RoutingConfigError(f"routing section {top!r} is missing or invalid")
    chain = section.get(sub)
    if not isinstance(chain, list):
        raise RoutingConfigError(f"routing context {context!r} is missing or invalid")
    if candidate.candidate_id not in chain:
        chain.append(candidate.candidate_id)
    _validate_structure(data, None)
    return data


def remove_candidate_from_chain(
    payload: Mapping[str, Any],
    *,
    top: str,
    sub: str,
    candidate_id: str,
) -> dict[str, Any]:
    """Return a validated copy of ``payload`` with ``candidate_id`` removed.

    Removing a candidate not present is a no-op.  The failure modes are
    covered by the same canonical validator as :func:`add_candidate_to_chain`.
    The owner approval record (if any) is left in place: removing a candidate
    from a chain is an ordering decision, not a revocation of discovery
    evidence, and an approval that is no longer referenced is inert.
    """
    context = _canonical_chain_key(top, sub)
    if context not in {_canonical_chain_key(t, s) for t, s in CONTEXT_KEYS}:
        raise RoutingConfigError(f"unknown routing context {context!r}")
    cleaned = candidate_id.strip()
    if not cleaned:
        raise RoutingConfigError("candidate id must not be empty")
    data: dict[str, Any] = json.loads(json.dumps(payload))
    section = data.get(top)
    if not isinstance(section, dict):
        raise RoutingConfigError(f"routing section {top!r} is missing or invalid")
    chain = section.get(sub)
    if not isinstance(chain, list):
        raise RoutingConfigError(f"routing context {context!r} is missing or invalid")
    data[top][sub] = [cid for cid in chain if cid != cleaned]
    _validate_structure(data, None)
    return data


def move_candidate_in_chain(
    payload: Mapping[str, Any],
    *,
    top: str,
    sub: str,
    candidate_id: str,
    direction: str,
) -> dict[str, Any]:
    """Return a validated copy of ``payload`` with ``candidate_id`` reordered.

    ``direction`` is ``"up"`` (one position earlier) or ``"down"`` (one
    position later) within the named tier/context chain.  Moving the first
    entry up or the last entry down is a no-op; moving an absent candidate
    is also a no-op.  No other entry is added, removed or substituted --
    this helper only reorders.  The result is re-validated by the canonical
    structure validator, so unknown contexts fail closed.
    """
    context = _canonical_chain_key(top, sub)
    if context not in {_canonical_chain_key(t, s) for t, s in CONTEXT_KEYS}:
        raise RoutingConfigError(f"unknown routing context {context!r}")
    cleaned = candidate_id.strip()
    if not cleaned:
        raise RoutingConfigError("candidate id must not be empty")
    cleaned_direction = direction.strip().lower()
    if cleaned_direction not in ("up", "down"):
        raise RoutingConfigError(
            f"invalid move direction {direction!r}: expected 'up' or 'down'"
        )
    data: dict[str, Any] = json.loads(json.dumps(payload))
    section = data.get(top)
    if not isinstance(section, dict):
        raise RoutingConfigError(f"routing section {top!r} is missing or invalid")
    chain = section.get(sub)
    if not isinstance(chain, list):
        raise RoutingConfigError(f"routing context {context!r} is missing or invalid")
    if cleaned in chain:
        index = chain.index(cleaned)
        target = index - 1 if cleaned_direction == "up" else index + 1
        if 0 <= target < len(chain):
            chain[index], chain[target] = chain[target], chain[index]
    _validate_structure(data, None)
    return data


def save_user_routing(payload: Mapping[str, Any]) -> Path:
    """Persist the canonical user routing document (validated, atomic).

    The document is validated with the same fail-closed validator the
    loader uses and written to the canonical user path atomically.  This
    is the only routing writer; no other routing document is created.
    """
    if not isinstance(payload, Mapping):
        raise RoutingConfigError("routing payload must be a mapping")
    data: dict[str, Any] = json.loads(json.dumps(payload))
    _validate_structure(data, get_user_routing_path())
    user_path = get_user_routing_path()
    try:
        user_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = user_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(user_path)
    except OSError as exc:
        raise RoutingConfigError(f"Routing config at {user_path}: cannot write: {exc}") from exc
    return user_path
