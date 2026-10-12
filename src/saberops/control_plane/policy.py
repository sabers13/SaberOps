"""Owner/XDG persistence for provider governance and quota pool policy."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile

from saberops.control_plane.capabilities import normalize_required_capabilities
from saberops.control_plane.plans import PlanSet, validate_plan_sections
from saberops.control_plane.quota_policy import QuotaPoolMode, QuotaPoolPolicy


class ProviderPolicyState(StrEnum):
    ALLOWED = "ALLOWED"
    ALLOWED_WITH_WARNING = "ALLOWED_WITH_WARNING"
    DISABLED = "DISABLED"


@dataclass(frozen=True)
class ProviderPolicy:
    provider: str
    state: str = ProviderPolicyState.ALLOWED
    warning: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {
            ProviderPolicyState.ALLOWED,
            ProviderPolicyState.ALLOWED_WITH_WARNING,
            ProviderPolicyState.DISABLED,
        }:
            raise ValueError(f"unknown provider policy state: {self.state}")
        object.__setattr__(self, "state", ProviderPolicyState(self.state))


@dataclass(frozen=True)
class ControlPlanePolicy:
    provider_policies: dict[str, ProviderPolicy]
    quota_pools: dict[str, QuotaPoolPolicy]
    content_hash: str
    plans: PlanSet = field(default_factory=PlanSet.empty)

    def provider(self, name: str) -> ProviderPolicy:
        return self.provider_policies.get(
            name.strip().lower(),
            ProviderPolicy(
                name.strip().lower(), ProviderPolicyState.ALLOWED_WITH_WARNING, "unknown provider"
            ),
        )


@dataclass(frozen=True)
class FrozenControlPlane:
    """Immutable, non-secret control-plane contract captured for one run."""

    policy: ControlPlanePolicy
    required_capabilities: tuple[str, ...]
    reserve_override: bool
    host_access: str
    digest: str

    def payload(self) -> dict[str, object]:
        """Return the canonical digest input without the self-referential digest.

        Plan sections are covered by the digest exactly when present: an
        empty plan set contributes no keys, so snapshots frozen before
        plans existed keep their historical digest byte-for-byte.
        """
        payload: dict[str, object] = {
            "schema_version": 1,
            "provider_policies": {
                name: {
                    "state": provider.state,
                    **({"warning": provider.warning} if provider.warning else {}),
                }
                for name, provider in sorted(self.policy.provider_policies.items())
            },
            "quota_pools": {
                name: {
                    "mode": pool.mode.value,
                    "red_line_pct": pool.red_line_pct,
                    "members": list(pool.members),
                }
                for name, pool in sorted(self.policy.quota_pools.items())
            },
            "required_capabilities": list(self.required_capabilities),
            "reserve_override": self.reserve_override,
            "host_access": self.host_access,
        }
        plans_payload = _plans_digest_payload(self.policy.plans)
        payload.update(plans_payload)
        return payload

    def as_dict(self) -> dict[str, object]:
        """Return the durable snapshot, including its canonical digest."""
        return {**self.payload(), "control_plane_digest": self.digest}

    def as_json(self) -> str:
        """Return byte-stable JSON suitable for the run row."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def get_control_plane_config_path() -> Path:
    """Return the canonical public owner control-plane-config path."""
    from saberops.paths import get_control_plane_config_path as _canonical

    return _canonical()


def _default() -> dict[str, object]:
    # Automatic provider governance.  Antigravity is attended-only
    # (ADR-0001 Amendment 1, item 3): it is not an automatic routing
    # provider, so it carries no default policy entry (an explicit
    # owner override still dispatches; unknown providers evaluate with
    # a warning, never a silent allow).
    return {
        "version": 1,
        "providers": {
            provider: {"state": ProviderPolicyState.ALLOWED}
            for provider in ("opencode", "codex", "cline")
        },
        "quota_pools": {},
    }


def _plans_digest_payload(plans: PlanSet) -> dict[str, object]:
    """Return the canonical plan-section digest input (empty when no plans)."""
    if plans.is_empty():
        return {}
    payload: dict[str, object] = {}
    if plans.accounts:
        payload["accounts"] = {
            account_id: {
                "vendor": account.vendor,
                "plan": account.plan,
                "required_toggles": list(account.required_toggles),
                **(
                    {"toggles_attested_on": account.toggles_attested_on}
                    if account.toggles_attested_on is not None
                    else {}
                ),
                "notes": account.notes,
            }
            for account_id, account in sorted(plans.accounts.items())
        }
    if plans.pool_plans:
        payload["pool_plans"] = {
            pool_id: {
                **({"account": plan.account} if plan.account is not None else {}),
                "windows": [
                    {
                        "id": window.id,
                        "kind": window.kind,
                        "duration_min": window.duration_min,
                        "unit": window.unit,
                    }
                    for window in plan.windows
                ],
                "overage_possible": plan.overage_possible,
                **(
                    {"max_concurrent": plan.max_concurrent}
                    if plan.max_concurrent is not None
                    else {}
                ),
                "members": [
                    {"route": member.route, "multiplier": member.multiplier}
                    for member in plan.members
                ],
                "mode": plan.mode,
                "red_line_pct": plan.red_line_pct,
            }
            for pool_id, plan in sorted(plans.pool_plans.items())
        }
    if plans.postures:
        payload["postures"] = {
            selector: {
                "class": record.posture_class,
                "policy_urls": list(record.policy_urls),
                **({"ttl_days": record.ttl_days} if record.ttl_days is not None else {}),
                **(
                    {"verified_on": record.verified_on}
                    if record.verified_on is not None
                    else {}
                ),
                **({"account": record.account} if record.account is not None else {}),
                "note": record.note,
            }
            for selector, record in sorted(plans.postures.items())
        }
    if plans.reserves:
        payload["reserves"] = [
            {
                "pool": reserve.pool,
                "window": reserve.window,
                "pct": reserve.pct,
                "hours": reserve.hours,
                "days": list(reserve.days),
                "tz": reserve.tz,
            }
            for reserve in sorted(
                plans.reserves, key=lambda item: (item.pool, item.window, item.hours)
            )
        ]
    return payload


def _policy_payload(policy: ControlPlanePolicy) -> dict[str, object]:
    """Return the canonical owner-policy payload used by its legacy digest.

    Plan sections join the digest exactly when present, so a v1 document
    without plan sections hashes exactly as it did before plans existed.
    """
    payload: dict[str, object] = {
        "providers": {k: vars(v) for k, v in sorted(policy.provider_policies.items())},
        "quota_pools": {k: vars(v) for k, v in sorted(policy.quota_pools.items())},
    }
    payload.update(_plans_digest_payload(policy.plans))
    return payload


def _build(data: object, source: str) -> ControlPlanePolicy:
    if not isinstance(data, dict) or data.get("version") not in (1, 2):
        raise ValueError(f"Control-plane config at {source}: invalid version/document")
    providers = data.get("providers", {})
    pools = data.get("quota_pools", {})
    if not isinstance(providers, dict) or not isinstance(pools, dict):
        raise ValueError(f"Control-plane config at {source}: providers/quota_pools must be objects")
    parsed: dict[str, ProviderPolicy] = {}
    for name, raw in providers.items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError(f"Control-plane config at {source}: invalid provider policy")
        state = raw.get("state", ProviderPolicyState.ALLOWED)
        warning = raw.get("warning")
        if not isinstance(state, str) or (warning is not None and not isinstance(warning, str)):
            raise ValueError(f"Control-plane config at {source}: invalid provider policy")
        parsed[name.lower()] = ProviderPolicy(name.lower(), state, warning)
    version = data.get("version")
    if version == 1:
        # A v1 document loads with empty plan sections: its digest must
        # stay byte-identical to the pre-plans code path.
        plans = PlanSet.empty()
    else:
        try:
            plans = validate_plan_sections(data, source)
        except ValueError as exc:
            raise ValueError(f"Control-plane config at {source}: {exc}") from exc
    parsed_pools: dict[str, QuotaPoolPolicy] = {}
    for pool_id, raw in pools.items():
        if not isinstance(pool_id, str) or not isinstance(raw, dict):
            raise ValueError(f"Control-plane config at {source}: invalid quota pool")
        if version == 1:
            members = raw.get("members", [])
            if not isinstance(members, list) or not all(
                isinstance(m, str) for m in members
            ):
                raise ValueError(
                    f"Control-plane config at {source}: invalid quota pool members"
                )
            member_routes = tuple(members)
        else:
            plan = plans.pool_plans.get(pool_id)
            member_routes = (
                tuple(member.route for member in plan.members)
                if plan is not None
                else ()
            )
        try:
            mode = QuotaPoolMode(str(raw.get("mode", QuotaPoolMode.STRICT)))
        except ValueError as exc:
            raise ValueError(
                f"Control-plane config at {source}: "
                f"quota_pools.{pool_id}.mode: unknown mode {raw.get('mode')!r}"
            ) from exc
        parsed_pools[pool_id] = QuotaPoolPolicy(
            pool_id,
            mode,
            float(raw.get("red_line_pct", 15.0)),
            member_routes,
        )
    policy = ControlPlanePolicy(parsed, parsed_pools, "", plans)
    canonical = json.dumps(
        _policy_payload(policy),
        sort_keys=True,
        default=lambda value: value.value if hasattr(value, "value") else value,
        separators=(",", ":"),
    )
    return ControlPlanePolicy(
        parsed, parsed_pools, hashlib.sha256(canonical.encode()).hexdigest(), plans
    )


def _merge_snapshot_pools(pools: object, extras: object) -> dict[str, object]:
    """Fold frozen ``pool_plans`` extras back into their quota-pool entries."""
    merged: dict[str, object] = {}
    entries = pools if isinstance(pools, dict) else {}
    extra_map = extras if isinstance(extras, dict) else {}
    for pool_id, entry in entries.items():
        merged_entry: dict[str, object] = (
            dict(entry) if isinstance(entry, dict) else {"members": entry}
        )
        extra_entry = extra_map.get(pool_id)
        if isinstance(extra_entry, dict):
            for extra_key in (
                "account",
                "windows",
                "overage_possible",
                "max_concurrent",
                "members",
            ):
                if extra_key in extra_entry:
                    merged_entry[extra_key] = extra_entry[extra_key]
        merged[pool_id] = merged_entry
    return merged


def packaged_control_plane_policy() -> ControlPlanePolicy:
    """Return the packaged default without consulting owner configuration."""
    return _build(_default(), "packaged default")


def freeze_control_plane_policy(
    policy: ControlPlanePolicy,
    *,
    required_capabilities: tuple[str, ...] = (),
    reserve_override: bool = False,
    host_access: str = "governed",
) -> FrozenControlPlane:
    """Capture policy and run-level control inputs into one canonical contract."""
    normalized = normalize_required_capabilities(required_capabilities)
    host = str(host_access).strip().lower()
    if host not in {"governed", "unrestricted"}:
        raise ValueError(f"unknown host access mode: {host_access}")
    provisional = FrozenControlPlane(policy, normalized, bool(reserve_override), host, "")
    canonical = json.dumps(
        provisional.payload(), sort_keys=True, separators=(",", ":")
    ).encode()
    return FrozenControlPlane(
        policy,
        normalized,
        bool(reserve_override),
        host,
        hashlib.sha256(canonical).hexdigest(),
    )


def reconstruct_frozen_control_plane(
    snapshot_json: str,
    expected_digest: str | None = None,
) -> FrozenControlPlane:
    """Reconstruct and integrity-check a persisted run control-plane contract."""
    try:
        raw = json.loads(snapshot_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("frozen control-plane snapshot is not valid JSON") from exc
    if not isinstance(raw, dict):
        raise ValueError("frozen control-plane snapshot must be an object")
    if raw.get("schema_version") != 1:
        raise ValueError("frozen control-plane snapshot has an unsupported schema")
    providers = raw.get("provider_policies")
    pools = raw.get("quota_pools")
    required = raw.get("required_capabilities", [])
    reserve = raw.get("reserve_override", False)
    host = raw.get("host_access", "governed")
    stored_digest = raw.get("control_plane_digest")
    if not isinstance(providers, dict) or not isinstance(pools, dict):
        raise ValueError("frozen control-plane snapshot lacks policy mappings")
    if not isinstance(required, list) or not all(isinstance(item, str) for item in required):
        raise ValueError("frozen control-plane required_capabilities is invalid")
    if not isinstance(reserve, bool) or not isinstance(host, str):
        raise ValueError("frozen control-plane run inputs are invalid")
    provider_data = {
        name: value for name, value in providers.items() if isinstance(name, str)
    }
    if len(provider_data) != len(providers):
        raise ValueError("frozen control-plane provider mapping is invalid")
    has_plans = any(
        key in raw for key in ("accounts", "postures", "reserves", "pool_plans")
    )
    policy_data: dict[str, object] = {
        # Snapshots frozen before plans existed rebuild as v1 with empty
        # plan sections, so their digest is unaffected.
        "version": 2 if has_plans else 1,
        "providers": provider_data,
        "quota_pools": _merge_snapshot_pools(pools, raw.get("pool_plans")),
    }
    for key in ("accounts", "postures", "reserves"):
        if key in raw:
            policy_data[key] = raw[key]
    policy = _build(policy_data, "frozen run snapshot")
    frozen = freeze_control_plane_policy(
        policy,
        required_capabilities=tuple(required),
        reserve_override=reserve,
        host_access=host,
    )
    if stored_digest != frozen.digest or (
        expected_digest is not None and expected_digest != frozen.digest
    ):
        raise ValueError("frozen control-plane snapshot digest mismatch")
    return frozen


def legacy_frozen_control_plane(
    *,
    required_capabilities: tuple[str, ...] = (),
    reserve_override: bool = False,
    host_access: str = "governed",
) -> FrozenControlPlane:
    """Provide a non-owner fallback for older rows without a frozen contract."""
    return freeze_control_plane_policy(
        packaged_control_plane_policy(),
        required_capabilities=required_capabilities,
        reserve_override=reserve_override,
        host_access=host_access,
    )


def load_control_plane_policy() -> ControlPlanePolicy:
    path = get_control_plane_config_path()
    if not path.is_file():
        return _build(_default(), "packaged default")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Control-plane config at {path}: cannot read/parse") from exc
    return _build(data, str(path))


def _write(data: dict[str, object]) -> Path:
    path = get_control_plane_config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        tmp = Path(handle.name)
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    return path


def _read_raw_doc() -> dict[str, object]:
    """Return the raw owner document (unvalidated), or {} when absent."""
    path = get_control_plane_config_path()
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _dump_v2(policy: ControlPlanePolicy, raw: dict[str, object]) -> dict[str, object]:
    """Render the full version-2 document, preserving every plan section.

    Provider and quota-pool *core* fields come from the validated policy;
    per-pool plan extras (account, windows, overage_possible,
    max_concurrent, object members) and the accounts/postures/reserves
    sections round-trip verbatim from the raw owner document so a
    provider/quota write can never drop them.
    """
    raw_pools = raw.get("quota_pools")
    raw_pool_map = raw_pools if isinstance(raw_pools, dict) else {}
    pools: dict[str, object] = {}
    for pool_id, pool in policy.quota_pools.items():
        entry: dict[str, object] = {
            "mode": pool.mode.value,
            "red_line_pct": pool.red_line_pct,
            "members": list(pool.members),
        }
        raw_entry = raw_pool_map.get(pool_id)
        if isinstance(raw_entry, dict):
            plan = policy.plans.pool_plans.get(pool_id)
            if plan is not None and any(
                member.multiplier != 1.0 for member in plan.members
            ):
                entry["members"] = raw_entry.get("members", entry["members"])
            for extra_key in (
                "account",
                "windows",
                "overage_possible",
                "max_concurrent",
            ):
                if extra_key in raw_entry:
                    entry[extra_key] = raw_entry[extra_key]
        pools[pool_id] = entry
    data: dict[str, object] = {
        "version": 2,
        "providers": {
            k: {"state": v.state, **({"warning": v.warning} if v.warning else {})}
            for k, v in policy.provider_policies.items()
        },
        "quota_pools": pools,
    }
    for key in ("accounts", "postures", "reserves"):
        if key in raw:
            data[key] = raw[key]
    return data


def _write_policy(policy: ControlPlanePolicy, raw: dict[str, object]) -> Path:
    """Write the version-2 document and fail closed when it stops validating."""
    path = _write(_dump_v2(policy, raw))
    load_control_plane_policy()
    return path


def save_owner_control_plane(data: dict[str, object]) -> Path:
    """Atomically persist a full owner control-plane document (validated).

    This is the writer service layers (e.g. posture verification) use to
    store owner attestations.  The document is validated after the write
    and any failure raises; nothing is left half-written (atomic replace).
    """
    if not isinstance(data, dict):
        raise ValueError("owner control-plane document must be an object")
    path = _write(data)
    load_control_plane_policy()
    return path


def set_provider_policy(provider: str, state: str, warning: str | None = None) -> Path:
    if state not in {
        ProviderPolicyState.ALLOWED,
        ProviderPolicyState.ALLOWED_WITH_WARNING,
        ProviderPolicyState.DISABLED,
    }:
        raise ValueError("state must be ALLOWED, ALLOWED_WITH_WARNING, or DISABLED")
    raw = _read_raw_doc()
    current = load_control_plane_policy()
    providers = dict(current.provider_policies)
    providers[provider.strip().lower()] = ProviderPolicy(
        provider.strip().lower(), state, warning
    )
    updated = ControlPlanePolicy(
        providers, dict(current.quota_pools), current.content_hash, current.plans
    )
    return _write_policy(updated, raw)


def set_quota_pool_policy(
    pool_id: str,
    *,
    mode: QuotaPoolMode = QuotaPoolMode.STRICT,
    red_line_pct: float = 15.0,
    members: tuple[str, ...] = (),
) -> Path:
    """Persist a shared pool contract in the same owner XDG config."""
    pool = QuotaPoolPolicy(pool_id, mode, red_line_pct, members)
    raw = _read_raw_doc()
    current = load_control_plane_policy()
    pools = dict(current.quota_pools)
    pools[pool_id] = pool
    updated = ControlPlanePolicy(
        dict(current.provider_policies), pools, current.content_hash, current.plans
    )
    # The caller replaces this pool's member list outright (plain strings);
    # every other section and this pool's non-member extras are preserved.
    raw_pools = raw.get("quota_pools")
    if isinstance(raw_pools, dict):
        existing = raw_pools.get(pool_id)
        base_entry: dict[str, object] = dict(existing) if isinstance(existing, dict) else {}
        raw = {
            **raw,
            "quota_pools": {
                **raw_pools,
                pool_id: {
                    **base_entry,
                    "mode": pool.mode.value,
                    "red_line_pct": pool.red_line_pct,
                    "members": list(pool.members),
                },
            },
        }
    return _write_policy(updated, raw)
