"""Owner/XDG persistence for provider governance and quota pool policy."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from tempfile import NamedTemporaryFile

from saberops.control_plane.capabilities import normalize_required_capabilities
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
        """Return the canonical digest input without the self-referential digest."""
        return {
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

    def as_dict(self) -> dict[str, object]:
        """Return the durable snapshot, including its canonical digest."""
        return {**self.payload(), "control_plane_digest": self.digest}

    def as_json(self) -> str:
        """Return byte-stable JSON suitable for the run row."""
        return json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))


def get_control_plane_config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(xdg.strip()).expanduser() if xdg.strip() else Path.home() / ".config"
    return base / "orchestrator-v2" / "control_plane.json"


def _default() -> dict[str, object]:
    return {
        "version": 1,
        "providers": {
            provider: {"state": ProviderPolicyState.ALLOWED}
            for provider in ("opencode", "codex", "cline", "antigravity")
        },
        "quota_pools": {},
    }


def _policy_payload(policy: ControlPlanePolicy) -> dict[str, object]:
    """Return the canonical owner-policy payload used by its legacy digest."""
    return {
        "providers": {k: vars(v) for k, v in sorted(policy.provider_policies.items())},
        "quota_pools": {k: vars(v) for k, v in sorted(policy.quota_pools.items())},
    }


def _build(data: object, source: str) -> ControlPlanePolicy:
    if not isinstance(data, dict) or data.get("version") != 1:
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
    parsed_pools: dict[str, QuotaPoolPolicy] = {}
    for pool_id, raw in pools.items():
        if not isinstance(pool_id, str) or not isinstance(raw, dict):
            raise ValueError(f"Control-plane config at {source}: invalid quota pool")
        members = raw.get("members", [])
        if not isinstance(members, list) or not all(isinstance(m, str) for m in members):
            raise ValueError(f"Control-plane config at {source}: invalid quota pool members")
        parsed_pools[pool_id] = QuotaPoolPolicy(
            pool_id,
            QuotaPoolMode(str(raw.get("mode", QuotaPoolMode.STRICT))),
            float(raw.get("red_line_pct", 15.0)),
            tuple(members),
        )
    policy = ControlPlanePolicy(parsed, parsed_pools, "")
    canonical = json.dumps(
        _policy_payload(policy),
        sort_keys=True,
        default=lambda value: value.value if hasattr(value, "value") else value,
        separators=(",", ":"),
    )
    return ControlPlanePolicy(parsed, parsed_pools, hashlib.sha256(canonical.encode()).hexdigest())


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
    policy_data = {"version": 1, "providers": provider_data, "quota_pools": pools}
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
    """Provide a non-owner fallback for pre-R2 rows without a frozen contract."""
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


def set_provider_policy(provider: str, state: str, warning: str | None = None) -> Path:
    if state not in {
        ProviderPolicyState.ALLOWED,
        ProviderPolicyState.ALLOWED_WITH_WARNING,
        ProviderPolicyState.DISABLED,
    }:
        raise ValueError("state must be ALLOWED, ALLOWED_WITH_WARNING, or DISABLED")
    current = load_control_plane_policy()
    data = {
        "version": 1,
        "providers": {
            k: {"state": v.state, **({"warning": v.warning} if v.warning else {})}
            for k, v in current.provider_policies.items()
        },
        "quota_pools": {
            k: {"mode": v.mode.value, "red_line_pct": v.red_line_pct, "members": list(v.members)}
            for k, v in current.quota_pools.items()
        },
    }
    provider_data = data.setdefault("providers", {})
    assert isinstance(provider_data, dict)
    provider_data[provider.strip().lower()] = {
        "state": state,
        **({"warning": warning} if warning else {}),
    }
    return _write(data)


def set_quota_pool_policy(
    pool_id: str,
    *,
    mode: QuotaPoolMode = QuotaPoolMode.STRICT,
    red_line_pct: float = 15.0,
    members: tuple[str, ...] = (),
) -> Path:
    """Persist a shared pool contract in the same owner XDG config."""
    pool = QuotaPoolPolicy(pool_id, mode, red_line_pct, members)
    current = load_control_plane_policy()
    providers = {
        k: {"state": v.state, **({"warning": v.warning} if v.warning else {})}
        for k, v in current.provider_policies.items()
    }
    pools = {
        k: {
            "mode": v.mode.value,
            "red_line_pct": v.red_line_pct,
            "members": list(v.members),
        }
        for k, v in current.quota_pools.items()
    }
    pools[pool_id] = {
        "mode": pool.mode.value,
        "red_line_pct": pool.red_line_pct,
        "members": list(pool.members),
    }
    return _write({"version": 1, "providers": providers, "quota_pools": pools})
