"""Canonical routing configuration loader with strict validation and snapshots.

Schema 3 (the only writable schema): role chains for WORKER, SURGEON and
INTEGRATOR, each a list of fallback groups, each group an object with
exactly the key ``routes`` holding a non-empty list of candidate ids.
ORCHESTRATOR and REVIEWER keep their ordered lists where they live today
(``orch orchestrator show`` / ``orch reviewer show``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.jobs.models import JobRole
from saberops.models import WorkerCandidate

SCHEMA_VERSION = 3

#: The one role vocabulary (contract C3 from Wave 2). No second role enum.
ROUTED_ROLES: tuple[JobRole, ...] = (JobRole.WORKER, JobRole.SURGEON, JobRole.INTEGRATOR)

_ROUTED_ROLE_VALUES: tuple[str, ...] = ("WORKER", "SURGEON", "INTEGRATOR")

# Closed registry of allowed canonical ids.
#
# Antigravity is attended-only (ADR-0001 Amendment 1, item 3) and has
# no automatic routing candidate: only an explicit owner
# provider+model override may yield it, which never consults this
# registry.
ALLOWED_CANDIDATES: frozenset[str] = frozenset(
    {
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

# Legacy tier context keys (private tables kept for the 1→2 merge and for
# verifying persisted schema-1/2 snapshots; never public editor inputs).
_LEGACY_CONTEXT_KEYS: tuple[tuple[str, str], ...] = (
    ("T1", "default"),
    ("T2", "training_allowed"),
    ("T2", "training_denied_off_peak"),
    ("T2", "training_denied_peak"),
    ("T3", "off_peak"),
    ("T3", "peak"),
)

_LEGACY_V2_CONTEXT_KEYS: tuple[tuple[str, str], ...] = (
    ("T1", "default"),
    ("T2", "default"),
    ("T3", "default"),
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


def _normalize_role(role: JobRole | str) -> str:
    if isinstance(role, JobRole):
        return role.value
    cleaned = str(role).strip().upper()
    for value in _ROUTED_ROLE_VALUES:
        if cleaned == value:
            return value
    # ORCHESTRATOR / REVIEWER get their pointer message from the caller;
    # here just report the valid set.
    raise RoutingConfigError(
        f"unknown routing role {str(role)!r}: expected one of {sorted(_ROUTED_ROLE_VALUES)}"
    )


def _candidate_id_from_entry(entry: Any, ctx_key: str, source_label: str) -> str:
    if isinstance(entry, str):
        return entry
    if isinstance(entry, dict):
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
        return cid
    raise RoutingConfigError(
        f"Routing config at {source_label}: context {ctx_key} entry {entry!r} must be string or candidate object"  # noqa: E501
    )


def _validate_context_chain(
    chain: Any,
    ctx_key: str,
    source_label: str,
    approved: Mapping[str, DynamicCandidate] | None = None,
) -> list[str]:
    if not isinstance(chain, list):
        raise RoutingConfigError(
            f"Routing config at {source_label}: context {ctx_key} must be a list"
        )
    if len(chain) == 0:
        # An empty chain is a valid owner decision: no candidate may
        # dispatch in this context.  It terminates with structured
        # no-eligible-candidate evidence, never a substituted default.
        return []
    seen: set[str] = set()
    cids: list[str] = []
    for entry in chain:
        cid = _candidate_id_from_entry(entry, ctx_key, source_label)
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
        cids.append(cid)
    return cids


def _validate_legacy_structure(
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
    if type(sv) is not int or sv not in (1, 2):
        raise RoutingConfigError(
            f"Routing config at {source_label}: schema_version must be integer 2, got {sv!r}"
        )
    if approved is None:
        approved = _parse_approved_candidates(data, source_label)
    keys = _LEGACY_CONTEXT_KEYS if sv == 1 else _LEGACY_V2_CONTEXT_KEYS
    result: dict[str, list[str]] = {}
    for top, sub in keys:
        ctx_key = _canonical_chain_key(top, sub)
        top_val = data.get(top)
        if not isinstance(top_val, dict) or sub not in top_val:
            raise RoutingConfigError(f"Routing config at {source_label}: missing context {ctx_key}")
        unknown_subs = set(top_val) - {s for t, s in keys if t == top}
        if unknown_subs:
            raise RoutingConfigError(
                f"Routing config at {source_label}: unknown keys in {top}: {sorted(unknown_subs)}"
            )
        result[ctx_key] = _validate_context_chain(
            top_val[sub], ctx_key, source_label, approved=approved
        )
    return result


def _merge_legacy_to_v2(validated: dict[str, list[str]]) -> dict[str, list[str]]:
    """Merge validated legacy contexts into the three v2 default chains."""
    result: dict[str, list[str]] = {}
    for tier in ("T1", "T2", "T3"):
        merged: list[str] = []
        for key, chain in validated.items():
            if key.startswith(tier + "."):
                for cid in chain:
                    if cid not in merged:
                        merged.append(cid)
        result[f"{tier}.default"] = merged
    return result


def _migrate_v2_to_v3_roles(merged: dict[str, list[str]]) -> dict[str, list[list[str]]]:
    """Migrate merged v2 chains to schema-3 role chains (ADR-0002 §11 item 2)."""
    t1 = list(merged.get("T1.default", []))
    t2 = list(merged.get("T2.default", []))
    t3 = list(merged.get("T3.default", []))
    worker: list[str] = []
    for cid in (*t2, *t1, *t3):
        if cid not in worker:
            worker.append(cid)
    roles: dict[str, list[list[str]]] = {
        "WORKER": [[cid] for cid in worker],
        "SURGEON": [[cid] for cid in t3],
        "INTEGRATOR": [[t2[0]]] if t2 else [],
    }
    return roles


def _validate_role_groups(
    chain: Any,
    role: str,
    source_label: str,
    approved: Mapping[str, DynamicCandidate] | None = None,
) -> list[list[str]]:
    if not isinstance(chain, list):
        raise RoutingConfigError(
            f"Routing config at {source_label}: role {role} chain must be a list"
        )
    groups: list[list[str]] = []
    seen: set[str] = set()
    for index, group in enumerate(chain):
        ctx_key = f"{role}[{index + 1}]"
        if not isinstance(group, dict) or set(group.keys()) != {"routes"}:
            raise RoutingConfigError(
                f"Routing config at {source_label}: role {role} group {index + 1} must be an object with exactly the key 'routes'"  # noqa: E501
            )
        routes = group["routes"]
        if not isinstance(routes, list) or not routes:
            raise RoutingConfigError(
                f"Routing config at {source_label}: role {role} group {index + 1} 'routes' must be a non-empty list"  # noqa: E501
            )
        cids: list[str] = []
        for entry in routes:
            cid = _candidate_id_from_entry(entry, ctx_key, source_label)
            if cid in seen:
                raise RoutingConfigError(
                    f"Routing config at {source_label}: role {role} duplicate candidate {cid!r}"
                )
            seen.add(cid)
            approved_entry = approved.get(cid) if approved else None
            if cid not in ALLOWED_CANDIDATES and approved_entry is None:
                raise RoutingConfigError(
                    f"Routing config at {source_label}: role {role} unknown candidate id {cid!r}"
                )
            cids.append(cid)
        groups.append(cids)
    return groups


def _validate_v3_structure(
    data: Any, source_label: str, *, approved: Mapping[str, DynamicCandidate] | None = None
) -> dict[str, list[list[str]]]:
    if not isinstance(data, dict):
        raise RoutingConfigError(f"Routing config at {source_label}: top-level must be JSON object")
    allowed_top = {"schema_version", "roles", _APPROVED_TOP_KEY}
    unknown = set(data.keys()) - allowed_top
    if unknown:
        raise RoutingConfigError(
            f"Routing config at {source_label}: unknown top-level keys {sorted(unknown)}"
        )
    if "schema_version" not in data:
        raise RoutingConfigError(f"Routing config at {source_label}: missing schema_version")
    if "roles" not in data or not isinstance(data["roles"], dict):
        raise RoutingConfigError(
            f"Routing config at {source_label}: missing or invalid 'roles' object"
        )
    roles_obj = data["roles"]
    for key in roles_obj:
        if key in ("ORCHESTRATOR", "REVIEWER"):
            pointer = "orch orchestrator show" if key == "ORCHESTRATOR" else "orch reviewer show"
            raise RoutingConfigError(
                f"Routing config at {source_label}: role {key!r} is managed elsewhere; see '{pointer}'"  # noqa: E501
            )
    missing = [r for r in _ROUTED_ROLE_VALUES if r not in roles_obj]
    if missing:
        raise RoutingConfigError(
            f"Routing config at {source_label}: missing roles {missing}"
        )
    extra = [k for k in roles_obj if k not in _ROUTED_ROLE_VALUES]
    if extra:
        raise RoutingConfigError(
            f"Routing config at {source_label}: unknown roles {sorted(extra)}"
        )
    if approved is None:
        approved = _parse_approved_candidates(data, source_label)
    result: dict[str, list[list[str]]] = {}
    for role in _ROUTED_ROLE_VALUES:
        result[role] = _validate_role_groups(roles_obj[role], role, source_label, approved=approved)
    return result


def canonical_routing_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Return the canonical schema-3 document for any accepted input (1, 2, 3).

    Pure: the input is never mutated. Schema 1 merges exactly as before,
    then migrates 2 → 3 per ADR-0002 §11 item 2.
    """
    data: dict[str, Any] = json.loads(json.dumps(payload))
    if not isinstance(data, dict):
        raise RoutingConfigError("Routing config: top-level must be JSON object")
    if "schema_version" not in data:
        raise RoutingConfigError("Routing config: missing schema_version")
    sv = data["schema_version"]
    if type(sv) is not int or sv not in (1, 2, 3):
        raise RoutingConfigError(
            f"Routing config: schema_version must be integer 3, got {sv!r}"
        )
    if sv == 3:
        approved = _parse_approved_candidates(data, "packaged default")
        validated = _validate_v3_structure(data, "packaged default", approved=approved)
        result: dict[str, Any] = {
            "schema_version": 3,
            "roles": {
                role: [{"routes": list(group)} for group in groups]
                for role, groups in validated.items()
            },
        }
        if _APPROVED_TOP_KEY in data:
            result[_APPROVED_TOP_KEY] = data[_APPROVED_TOP_KEY]
        return result
    validated_legacy = _validate_legacy_structure(data, None)
    merged = _merge_legacy_to_v2(validated_legacy)
    roles = _migrate_v2_to_v3_roles(merged)
    result = {
        "schema_version": 3,
        "roles": {
            role: [{"routes": list(group)} for group in groups] for role, groups in roles.items()
        },
    }
    if _APPROVED_TOP_KEY in data:
        result[_APPROVED_TOP_KEY] = data[_APPROVED_TOP_KEY]
    return result


def _content_hash(
    validated: Any,
    schema_version: int,
    approved: Mapping[str, DynamicCandidate] | None = None,
) -> str:
    if schema_version == 3:
        body: dict[str, Any] = {
            "schema_version": 3,
            "roles": {
                role: [list(group) for group in groups] for role, groups in validated.items()
            },
        }
        if approved:
            body[_APPROVED_TOP_KEY] = {
                cid: entry.to_dict() for cid, entry in sorted(approved.items())
            }
        canonical = json.dumps(body, sort_keys=True)
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    legacy_body: dict[str, Any] = {"schema_version": schema_version, "chains": validated}
    # Only owner-approved dynamic candidates contribute; an empty approval set
    # hashes exactly as before, so existing static snapshots stay byte-stable.
    if approved:
        legacy_body[_APPROVED_TOP_KEY] = {
            cid: entry.to_dict() for cid, entry in sorted(approved.items())
        }
    canonical = json.dumps(legacy_body, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class RoutingSnapshot:
    """Frozen snapshot of effective routing configuration (schema 3)."""

    schema_version: int
    source: str  # "user" or "default"
    source_path: str | None
    content_hash: str
    # Immutable mapping ROLE -> tuple of fallback groups, each a tuple of candidates.
    role_chains: tuple[tuple[str, tuple[tuple[WorkerCandidate, ...], ...]], ...] = ()
    # The original tier chains of a persisted schema-1/2 snapshot, else None.
    # Each entry is (context key, tuple of candidates).
    legacy_chains: tuple[tuple[str, tuple[WorkerCandidate, ...]], ...] | None = None
    #: Owner-approved, discovery-evidenced executable candidates outside the
    #: closed static registry.  Chains still carry the canonical
    #: ``provider/model`` identity; this is the evidence binding that lets the
    #: validated loader accept that identity.  Empty for static-only configs.
    approved_candidates: tuple[DynamicCandidate, ...] = ()
    #: Schema the effective document was migrated from in memory (1 | 2 | None).
    migrated_from: int | None = None

    def _role_key(self, role: JobRole | str) -> str:
        if isinstance(role, JobRole):
            return role.value
        return str(role).strip().upper()

    def groups_for(self, role: JobRole | str) -> tuple[tuple[WorkerCandidate, ...], ...]:
        """Return the fallback groups for ``role`` in listed order."""
        key = self._role_key(role)
        for stored_role, groups in self.role_chains:
            if stored_role == key:
                return groups
        raise RoutingConfigError(f"unknown routing role {key!r}")

    def chain_for_role(self, role: JobRole | str) -> list[WorkerCandidate]:
        """Return the flattened candidate chain for ``role`` (group order)."""
        flattened: list[WorkerCandidate] = []
        for group in self.groups_for(role):
            flattened.extend(group)
        return flattened


def snapshot_references_exact_bindings(snapshot: RoutingSnapshot | None) -> bool:
    """True iff the frozen routing snapshot can produce an exact-binding candidate.

    A routing chain candidate gains its exact ``binding_id`` only from an
    owner approval record (``approved_candidates``).  When at least one
    approval names a non-empty ``binding_id``, an ordinary Run created from
    this snapshot may dispatch an exact binding and must therefore freeze the
    effective owner BindingRegistry alongside the snapshot
Static-only snapshots return ``False`` so the
    historical provider/model path neither loads nor requires owner bindings.
    """
    if snapshot is None:
        return False
    return any(
        str(getattr(entry, "binding_id", "") or "").strip()
        for entry in getattr(snapshot, "approved_candidates", ())
    )


def _snapshot_from_v3(
    validated: dict[str, list[list[str]]],
    *,
    source: str,
    source_path: Path | None,
    approved: Mapping[str, DynamicCandidate] | None = None,
    migrated_from: int | None = None,
) -> RoutingSnapshot:
    ch = _content_hash(validated, 3, approved)

    def to_group(cids: list[str]) -> tuple[WorkerCandidate, ...]:
        return tuple(_parse_candidate_id(cid) for cid in cids)

    role_chains = tuple(
        (role, tuple(to_group(group) for group in groups)) for role, groups in validated.items()
    )
    return RoutingSnapshot(
        schema_version=3,
        source=source,
        source_path=str(source_path) if source_path else None,
        content_hash=ch,
        role_chains=role_chains,
        legacy_chains=None,
        approved_candidates=tuple(entry for _, entry in sorted((approved or {}).items())),
        migrated_from=migrated_from,
    )


def load_packaged_default() -> RoutingSnapshot:
    """Load and validate the packaged default routing.json (schema 3)."""
    path = _packaged_default_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise RoutingConfigError(f"Packaged default routing.json not found at {path}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RoutingConfigError(f"Packaged default routing.json invalid JSON: {exc}") from exc
    canonical = canonical_routing_payload(data)
    validated = _validate_v3_structure(
        canonical, str(path), approved=_parse_approved_candidates(canonical, str(path))
    )
    approved = _parse_approved_candidates(canonical, str(path))
    return _snapshot_from_v3(validated, source="default", source_path=None, approved=approved)


def load_effective_routing() -> RoutingSnapshot:
    """Load effective routing: user file if present, else packaged default.

    A schema-1/2 user file loads as schema 3 in memory; the file is never
    rewritten by a load. The snapshot records ``migrated_from``.

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
        if not isinstance(data, dict):
            raise RoutingConfigError(
                f"Routing config at {user_path}: top-level must be JSON object"
            )
        raw_sv = data.get("schema_version")
        migrated_from = raw_sv if type(raw_sv) is int and raw_sv in (1, 2) else None
        canonical = canonical_routing_payload(data)
        approved = _parse_approved_candidates(canonical, str(user_path))
        validated = _validate_v3_structure(canonical, str(user_path), approved=approved)
        return _snapshot_from_v3(
            validated, source="user", source_path=user_path, approved=approved,
            migrated_from=migrated_from,
        )
    return load_packaged_default()


def snapshot_to_payload(snapshot: RoutingSnapshot, created_at: str) -> dict[str, Any]:
    """Serialize snapshot to DB payload shape."""
    if snapshot.legacy_chains is not None:
        # A persisted schema-1/2 snapshot re-emits its legacy payload shape
        # byte-identically (original keys, original digest).
        chains: dict[str, list[dict[str, str]]] = {}
        for ctx_key, candidates in snapshot.legacy_chains:
            chains[ctx_key] = [
                {"provider": c.provider, "model": c.model} for c in candidates
            ]
        payload: dict[str, Any] = {
            "schema_version": snapshot.schema_version,
            "source": snapshot.source,
            "source_path": snapshot.source_path,
            "content_hash": snapshot.content_hash,
            "chains": chains,
            "created_at": created_at,
        }
        if snapshot.approved_candidates:
            payload[_APPROVED_TOP_KEY] = {
                entry.candidate_id: entry.to_dict() for entry in snapshot.approved_candidates
            }
        return payload
    roles: dict[str, list[dict[str, list[dict[str, str]]]]] = {}
    for role, groups in snapshot.role_chains:
        roles[role] = [
            {"routes": [{"provider": c.provider, "model": c.model} for c in group]}
            for group in groups
        ]
    payload = {
        "schema_version": 3,
        "source": snapshot.source,
        "source_path": snapshot.source_path,
        "content_hash": snapshot.content_hash,
        "roles": roles,
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
    if type(sv) is not int or sv not in (1, 2, 3):
        raise RoutingConfigError(
            f"Routing config at {source_label}: schema_version must be integer {SCHEMA_VERSION}, got {sv!r}"  # noqa: E501
        )
    approved = _parse_approved_candidates(payload, source_label)
    if sv in (1, 2):
        chains_obj = payload.get("chains")
        if not isinstance(chains_obj, dict):
            raise RoutingConfigError(
                f"Routing config at {source_label}: missing or invalid 'chains' object"
            )
        keys = _LEGACY_CONTEXT_KEYS if sv == 1 else _LEGACY_V2_CONTEXT_KEYS
        expected_contexts = {_canonical_chain_key(top, sub) for top, sub in keys}
        unknown_contexts = set(chains_obj.keys()) - expected_contexts
        if unknown_contexts:
            raise RoutingConfigError(
                f"Routing config at {source_label}: unknown context keys in chains: {sorted(unknown_contexts)}"  # noqa: E501
            )
        validated: dict[str, list[str]] = {}
        for top, sub in keys:
            ctx_key = _canonical_chain_key(top, sub)
            if ctx_key not in chains_obj:
                raise RoutingConfigError(
                    f"Routing config at {source_label}: missing context {ctx_key!r}"
                )
            chain = chains_obj[ctx_key]
            validated[ctx_key] = _validate_context_chain(
                chain, ctx_key, source_label, approved=approved
            )

        # A persisted snapshot is self-consistent only when its stored
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

        def to_candidates(k: str) -> tuple[WorkerCandidate, ...]:
            return tuple(_parse_candidate_id(cid) for cid in validated[k])

        legacy_chains = tuple((ctx_key, to_candidates(ctx_key)) for ctx_key in validated)
        # Derive the role chains with the migration (ADR-0002 §11 item 2).
        merged = _merge_legacy_to_v2(validated)
        migrated_roles = _migrate_v2_to_v3_roles(merged)
        role_chains = tuple(
            (role, tuple(tuple(_parse_candidate_id(cid) for cid in group) for group in groups))
            for role, groups in migrated_roles.items()
        )
        return RoutingSnapshot(
            schema_version=sv,
            source=str(payload.get("source", "persisted")),
            source_path=payload.get("source_path")
            if isinstance(payload.get("source_path"), str)
            else None,
            content_hash=str(ch),
            role_chains=role_chains,
            legacy_chains=legacy_chains,
            approved_candidates=tuple(entry for _, entry in sorted(approved.items())),
        )
    # Schema 3 persisted payload.
    roles_obj = payload.get("roles")
    if not isinstance(roles_obj, dict):
        raise RoutingConfigError(
            f"Routing config at {source_label}: missing or invalid 'roles' object"
        )
    for key in roles_obj:
        if key in ("ORCHESTRATOR", "REVIEWER"):
            pointer = "orch orchestrator show" if key == "ORCHESTRATOR" else "orch reviewer show"
            raise RoutingConfigError(
                f"Routing config at {source_label}: role {key!r} is managed elsewhere; see '{pointer}'"  # noqa: E501
            )
    validated_v3: dict[str, list[list[str]]] = {}
    for role in _ROUTED_ROLE_VALUES:
        if role not in roles_obj:
            raise RoutingConfigError(
                f"Routing config at {source_label}: missing role {role!r}"
            )
        raw_groups = roles_obj[role]
        if not isinstance(raw_groups, list):
            raise RoutingConfigError(
                f"Routing config at {source_label}: role {role} chain must be a list"
            )
        groups: list[list[str]] = []
        seen: set[str] = set()
        for index, group in enumerate(raw_groups):
            ctx_key = f"{role}[{index + 1}]"
            if not isinstance(group, dict) or set(group.keys()) != {"routes"}:
                raise RoutingConfigError(
                    f"Routing config at {source_label}: role {role} group {index + 1} must be an object with exactly the key 'routes'"  # noqa: E501
                )
            routes = group["routes"]
            if not isinstance(routes, list) or not routes:
                raise RoutingConfigError(
                    f"Routing config at {source_label}: role {role} group {index + 1} 'routes' must be a non-empty list"  # noqa: E501
                )
            cids: list[str] = []
            for entry in routes:
                cid = _candidate_id_from_entry(entry, ctx_key, source_label)
                if cid in seen:
                    raise RoutingConfigError(
                        f"Routing config at {source_label}: role {role} duplicate candidate {cid!r}"
                    )
                seen.add(cid)
                approved_entry = approved.get(cid)
                if cid not in ALLOWED_CANDIDATES and approved_entry is None:
                    raise RoutingConfigError(
                        f"Routing config at {source_label}: role {role} "
                        f"unknown candidate id {cid!r}"
                    )
                cids.append(cid)
            groups.append(cids)
        validated_v3[role] = groups
    extra_roles = [k for k in roles_obj if k not in _ROUTED_ROLE_VALUES]
    if extra_roles:
        raise RoutingConfigError(
            f"Routing config at {source_label}: unknown roles {sorted(extra_roles)}"
        )
    canonical_hash_v3 = _content_hash(validated_v3, 3, approved)
    if "content_hash" not in payload or payload.get("content_hash") is None:
        ch3 = canonical_hash_v3
    else:
        stored3 = payload.get("content_hash")
        if not isinstance(stored3, str) or not stored3:
            raise RoutingConfigError(
                f"Routing config at {source_label}: content_hash must be "
                f"a non-empty string digest, got {stored3!r}"
            )
        if stored3 != canonical_hash_v3:
            raise RoutingConfigError(
                f"Routing config at {source_label}: content_hash mismatch: "
                f"persisted snapshot content does not match its stored digest "
                f"(fail closed; refusing to route under a stale digest)"
            )
        ch3 = stored3
    role_chains_v3 = tuple(
        (role, tuple(tuple(_parse_candidate_id(cid) for cid in group) for group in groups))
        for role, groups in validated_v3.items()
    )
    return RoutingSnapshot(
        schema_version=3,
        source=str(payload.get("source", "persisted")),
        source_path=payload.get("source_path")
        if isinstance(payload.get("source_path"), str)
        else None,
        content_hash=str(ch3),
        role_chains=role_chains_v3,
        legacy_chains=None,
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
# Canonical routing-configuration mutation seam.
#
# Routing configuration remains exclusively owned by this module.  The
# helpers below let operator surfaces reuse the *existing* validated
# persistence path -- there is deliberately no second routing document,
# no shadow chain store and no alternate candidate vocabulary.  Every
# mutation is expressed as a full document and re-validated by the same
# ``_validate_v3_structure`` used by the loader, so role semantics,
# ordering and training-denial rules stay authoritative.  Candidate identity is
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

    The record also carries ``binding_id``: the *exact* binding layer
    :class:`~saberops.control_plane.bindings.ExecutionBinding`
    identity this candidate executes through.  A raw ``provider/model``
    string never substitutes for it; the approval is only meaningful when
    the referenced binding exists and matches provider/model exactly.  The
    routing document keeps the canonical ``provider/model`` identity for
    ordering and quota policy, while real execution recovers ``binding_id`` from the
    canonical registry.

    It carries no provider credential, no endpoint and no secret material.
    Eligibility in a given context is still decided by the existing quota
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
                "(a dynamic candidate must reference an exact execution binding)"
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
            "capabilities": [{"name": name, "state": state} for name, state in self.capabilities],
            "approved_at": self.approved_at,
        }

    @classmethod
    def from_mapping(cls, candidate_id: str, payload: Mapping[str, Any]) -> DynamicCandidate:
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
    that want to mutate routing start from exactly this value. The
    returned document is always the canonical schema-3 shape.
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
        if not isinstance(data, dict):
            raise RoutingConfigError(
                f"Routing config at {user_path}: top-level must be JSON object"
            )
        return canonical_routing_payload(data)
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
    return canonical_routing_payload(data)


def _require_role_payload(
    payload: Mapping[str, Any], role: JobRole | str
) -> tuple[dict[str, Any], str]:
    data = canonical_routing_payload(payload)
    key = _normalize_role(role)
    roles = data.get("roles")
    if not isinstance(roles, dict) or key not in roles:
        raise RoutingConfigError(f"unknown routing role {key!r}")
    return data, key


def _revalidate_v3(data: dict[str, Any]) -> dict[str, Any]:
    approved = _parse_approved_candidates(data, "routing edit")
    _validate_v3_structure(data, "routing edit", approved=approved)
    return data


def add_candidate_to_chain(
    payload: Mapping[str, Any],
    *,
    role: JobRole | str,
    candidate_id: str,
    group: int | None = None,
) -> dict[str, Any]:
    """Return a validated copy of ``payload`` with ``candidate_id`` added to ``role``.

    Default: a new single-route group at the end. ``group`` (1-based)
    appends into that existing group instead. Adding an already-present
    candidate of that role is a no-op. The result is re-validated, so
    unknown candidate ids fail closed.
    """
    data, key = _require_role_payload(payload, role)
    cleaned = candidate_id.strip()
    if not cleaned:
        raise RoutingConfigError("candidate id must not be empty")
    chain = data["roles"][key]
    for existing in chain:
        if cleaned in existing.get("routes", []):
            return _revalidate_v3(data)
    if group is None:
        chain.append({"routes": [cleaned]})
    else:
        if type(group) is not int or group < 1 or group > len(chain):
            raise RoutingConfigError(f"unknown group {group!r} for role {key}")
        chain[group - 1]["routes"].append(cleaned)
    return _revalidate_v3(data)


def add_dynamic_candidate_to_chain(
    payload: Mapping[str, Any],
    *,
    role: JobRole | str,
    candidate: DynamicCandidate,
    group: int | None = None,
) -> dict[str, Any]:
    """Register an owner approval and add its candidate to ``role``.

    This is the dynamic-candidate analogue of
    :func:`add_candidate_to_chain`: it records the non-secret approval
    evidence under ``approved_candidates`` in the *same* canonical routing
    document and adds the canonical ``provider/model`` identity.  The
    whole document is then re-validated, so role semantics, ordering,
    identity validity and training-denial rules stay authoritative.
    Adding is idempotent; ordering is never rewritten.
    """
    data, key = _require_role_payload(payload, role)
    approved = data.get(_APPROVED_TOP_KEY)
    if not isinstance(approved, dict):
        approved = {}
        data[_APPROVED_TOP_KEY] = approved
    approved[candidate.candidate_id] = candidate.to_dict()
    chain = data["roles"][key]
    for existing in chain:
        if candidate.candidate_id in existing.get("routes", []):
            return _revalidate_v3(data)
    if group is None:
        chain.append({"routes": [candidate.candidate_id]})
    else:
        if type(group) is not int or group < 1 or group > len(chain):
            raise RoutingConfigError(f"unknown group {group!r} for role {key}")
        chain[group - 1]["routes"].append(candidate.candidate_id)
    return _revalidate_v3(data)


def remove_candidate_from_chain(
    payload: Mapping[str, Any],
    *,
    role: JobRole | str,
    candidate_id: str,
) -> dict[str, Any]:
    """Return a validated copy of ``payload`` with ``candidate_id`` removed from ``role``.

    Removing a candidate not present is a no-op. An emptied group is
    dropped; removing the final route leaves a valid empty chain: no
    default is substituted and dispatch in that role terminates with
    structured no-eligible-candidate evidence. The owner approval record
    (if any) is left in place: removing a candidate from a chain is an
    ordering decision, not a revocation of discovery evidence, and an
    approval that is no longer referenced is inert.
    """
    data, key = _require_role_payload(payload, role)
    cleaned = candidate_id.strip()
    if not cleaned:
        raise RoutingConfigError("candidate id must not be empty")
    chain = data["roles"][key]
    rebuilt: list[dict[str, list[str]]] = []
    for existing in chain:
        routes = [cid for cid in existing.get("routes", []) if cid != cleaned]
        if routes:
            rebuilt.append({"routes": routes})
    data["roles"][key] = rebuilt
    return _revalidate_v3(data)


def move_group_in_chain(
    payload: Mapping[str, Any],
    *,
    role: JobRole | str,
    group: int,
    direction: str,
) -> dict[str, Any]:
    """Return a validated copy of ``payload`` with group ``group`` moved one place.

    ``group`` is 1-based; ``direction`` is ``"up"`` (one position
    earlier) or ``"down"`` (one position later). Moving the first group
    up or the last group down is a no-op. No route is added, removed or
    substituted -- this helper only reorders groups.
    """
    data, key = _require_role_payload(payload, role)
    cleaned_direction = direction.strip().lower()
    if cleaned_direction not in ("up", "down"):
        raise RoutingConfigError(f"invalid move direction {direction!r}: expected 'up' or 'down'")
    chain = data["roles"][key]
    if type(group) is not int or group < 1 or group > len(chain):
        raise RoutingConfigError(f"unknown group {group!r} for role {key}")
    index = group - 1
    target = index - 1 if cleaned_direction == "up" else index + 1
    if 0 <= target < len(chain):
        chain[index], chain[target] = chain[target], chain[index]
    return _revalidate_v3(data)


def reorder_chain_groups(
    payload: Mapping[str, Any], *, role: JobRole | str, order: list[int]
) -> dict[str, Any]:
    """Reorder a role's groups without changing membership or approvals.

    ``order`` is a permutation of the 1-based group numbers; anything
    else fails closed.
    """
    data, key = _require_role_payload(payload, role)
    chain = data["roles"][key]
    if (
        not all(type(entry) is int for entry in order)
        or len(order) != len(chain)
        or set(order) != set(range(1, len(chain) + 1))
    ):
        raise RoutingConfigError("reordering must preserve every existing group exactly once")
    data["roles"][key] = [chain[number - 1] for number in order]
    return _revalidate_v3(data)


def save_user_routing(payload: Mapping[str, Any]) -> Path:
    """Persist the canonical user routing document (validated, atomic).

    The document is validated with the same fail-closed validator the
    loader uses and written to the canonical user path atomically.  This
    is the only routing writer; no other routing document is created.
    """
    if not isinstance(payload, Mapping):
        raise RoutingConfigError("routing payload must be a mapping")
    data = canonical_routing_payload(payload)
    approved = _parse_approved_candidates(data, "routing edit")
    _validate_v3_structure(data, str(get_user_routing_path()), approved=approved)
    user_path = get_user_routing_path()
    try:
        user_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = user_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(user_path)
    except OSError as exc:
        raise RoutingConfigError(f"Routing config at {user_path}: cannot write: {exc}") from exc
    return user_path
