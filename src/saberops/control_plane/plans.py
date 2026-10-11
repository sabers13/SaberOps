"""Plan registry types and fail-closed validation (M2-2, control-plane config v2).

This module owns the *data* side of the capacity model: accounts, pool
plans (windows, overage, concurrency, member multipliers), billing
postures and time-of-day reserves.  It deliberately wires nothing into
admission (M2-4b) or quota enforcement (I-003); it only parses,
validates and carries the data.

Selector grammar (shared with M2-1's matcher)::

    provider                e.g. ``codex``
    provider/model          e.g. ``codex/gpt-5.6-terra``
    provider/<prefix>/*     e.g. ``opencode/opencode-go/*`` (trailing glob only)

Posture selectors may additionally be ``backend:DIRECT_API``.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

WINDOW_IDS: frozenset[str] = frozenset(
    {"5h", "daily", "weekly", "monthly", "session", "app-weekly"}
)
WINDOW_KINDS: frozenset[str] = frozenset({"ROLLING", "CALENDAR", "UNKNOWN"})
WINDOW_UNITS: frozenset[str] = frozenset({"PERCENT", "USD", "CREDITS"})
POSTURE_CLASSES: frozenset[str] = frozenset({"A", "B", "C", "D", "X"})
RESERVE_DAYS: frozenset[str] = frozenset(
    {"Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"}
)
_HOURS_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d-([01]\d|2[0-3]):[0-5]\d$")
_PROVIDER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*")


@dataclass(frozen=True)
class Account:
    account_id: str
    vendor: str = ""
    plan: str = ""
    required_toggles: tuple[str, ...] = ()
    toggles_attested_on: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class Window:
    id: str
    kind: str = "UNKNOWN"
    duration_min: int = 0
    unit: str = "PERCENT"


@dataclass(frozen=True)
class PoolMember:
    route: str
    multiplier: float = 1.0


@dataclass(frozen=True)
class PoolPlan:
    pool_id: str
    account: str | None = None
    windows: tuple[Window, ...] = ()
    overage_possible: bool = False
    max_concurrent: int | None = None
    members: tuple[PoolMember, ...] = ()
    mode: str = "STRICT"
    red_line_pct: float = 15.0


@dataclass(frozen=True)
class PostureRecord:
    selector: str
    posture_class: str = "X"
    policy_urls: tuple[str, ...] = ()
    ttl_days: int | None = None
    verified_on: str | None = None
    account: str | None = None
    note: str = ""


@dataclass(frozen=True)
class Reserve:
    pool: str
    window: str
    pct: float = 0.0
    hours: str = ""
    days: tuple[str, ...] = ()
    tz: str = "UTC"


@dataclass(frozen=True)
class PlanSet:
    accounts: dict[str, Account] = field(default_factory=dict)
    pool_plans: dict[str, PoolPlan] = field(default_factory=dict)
    postures: dict[str, PostureRecord] = field(default_factory=dict)
    reserves: tuple[Reserve, ...] = ()

    @classmethod
    def empty(cls) -> PlanSet:
        return cls({}, {}, {}, ())

    def is_empty(self) -> bool:
        return not self.accounts and not self.pool_plans and not self.postures and not self.reserves


def _fail(path: str, message: str) -> ValueError:
    return ValueError(f"{path}: {message}")


def selector_kind(selector: str) -> str:
    """Return the selector kind: provider | route | prefix | backend.

    Raises ValueError on malformed selectors.
    """
    text = selector.strip()
    if text.startswith("backend:"):
        if text == "backend:DIRECT_API":
            return "backend"
        raise ValueError(f"unknown backend selector: {selector!r}")
    if not text or "*" in text.strip("*") or text.count("*") > 1:
        raise ValueError(f"invalid selector (only a trailing /* glob is allowed): {selector!r}")
    if text.endswith("/*"):
        head = text[:-2]
        if "/" not in head or head.endswith("/") or head.startswith("/"):
            raise ValueError(f"invalid prefix selector: {selector!r}")
        provider, _, prefix = head.partition("/")
        if not _PROVIDER_RE.fullmatch(provider) or not prefix or "*" in prefix:
            raise ValueError(f"invalid prefix selector: {selector!r}")
        return "prefix"
    if "/" in text:
        provider, _, model = text.partition("/")
        if (
            not _PROVIDER_RE.fullmatch(provider)
            or not model
            or model.startswith("/")
            or "*" in model
        ):
            raise ValueError(f"invalid route selector: {selector!r}")
        return "route"
    if not _PROVIDER_RE.fullmatch(text):
        raise ValueError(f"invalid provider selector: {selector!r}")
    return "provider"


def validate_selector(selector: str, path: str, *, allow_backend: bool = False) -> str:
    """Validate one selector string, returning its normalized form."""
    if not isinstance(selector, str) or not selector.strip():
        raise _fail(path, "selector must be a non-empty string")
    text = selector.strip()
    try:
        kind = selector_kind(text)
    except ValueError as exc:
        raise _fail(path, str(exc)) from exc
    if kind == "backend" and not allow_backend:
        raise _fail(path, "backend: selectors are only allowed in postures")
    return text


def match_route_selector(
    selector: str, provider: str, model: str, *, backend: str | None = None
) -> bool:
    """Return True when a route (provider/model, optional backend) draws a pool.

    Matching is case-insensitive on provider and model; backend selectors
    match only when the caller supplies the same backend name.
    """
    kind = selector_kind(selector.strip())
    want_provider = provider.strip().lower()
    want_model = model.strip().lower()
    if kind == "backend":
        return backend is not None and backend.strip() == selector.strip().partition(":")[2]
    if kind == "provider":
        return selector.strip().lower() == want_provider
    sel_provider, _, rest = selector.strip().partition("/")
    if sel_provider.lower() != want_provider:
        return False
    if kind == "route":
        return rest.lower() == want_model
    # prefix: provider/<prefix>/*
    prefix = rest[:-2].lower()
    return want_model == prefix or want_model.startswith(prefix + "/")


def _validate_window_id(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, "window id must be a non-empty string")
    if value in WINDOW_IDS:
        return value
    if value.startswith("model:") and len(value) > len("model:"):
        return value
    raise _fail(
        path,
        "window id must be one of 5h|daily|weekly|monthly|session|app-weekly|model:<name>",
    )


def _validate_windows(raw: Any, path: str) -> tuple[Window, ...]:
    if not isinstance(raw, list):
        raise _fail(path, "windows must be a list")
    seen: set[str] = set()
    windows: list[Window] = []
    for index, item in enumerate(raw):
        base = f"{path}[{index}]"
        if not isinstance(item, dict):
            raise _fail(base, "window must be an object")
        wid = _validate_window_id(item.get("id"), f"{base}.id")
        if wid in seen:
            raise _fail(f"{base}.id", f"duplicate window id {wid!r} in pool")
        seen.add(wid)
        kind = item.get("kind", "UNKNOWN")
        if kind not in WINDOW_KINDS:
            raise _fail(f"{base}.kind", f"kind must be one of {sorted(WINDOW_KINDS)}")
        duration = item.get("duration_min")
        if isinstance(duration, bool) or not isinstance(duration, (int, float)):
            raise _fail(f"{base}.duration_min", "duration_min must be a number > 0")
        if not duration > 0:
            raise _fail(f"{base}.duration_min", "duration_min must be > 0")
        unit = item.get("unit", "PERCENT")
        if unit not in WINDOW_UNITS:
            raise _fail(f"{base}.unit", f"unit must be one of {sorted(WINDOW_UNITS)}")
        windows.append(
            Window(
                id=wid,
                kind=str(kind),
                duration_min=int(duration),
                unit=str(unit),
            )
        )
    return tuple(windows)


def _validate_members(raw: Any, path: str) -> tuple[PoolMember, ...]:
    if not isinstance(raw, list):
        raise _fail(path, "members must be a list")
    members: list[PoolMember] = []
    for index, item in enumerate(raw):
        base = f"{path}[{index}]"
        if isinstance(item, str):
            route = validate_selector(item, base)
            members.append(PoolMember(route=route, multiplier=1.0))
        elif isinstance(item, dict):
            route_raw = item.get("route")
            route = validate_selector(
                route_raw if isinstance(route_raw, str) else "", base + ".route"
            )
            multiplier = item.get("multiplier", 1.0)
            if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)):
                raise _fail(base + ".multiplier", "multiplier must be a number > 0")
            if not multiplier > 0:
                raise _fail(base + ".multiplier", "multiplier must be > 0")
            members.append(PoolMember(route=route, multiplier=float(multiplier)))
        else:
            raise _fail(base, "member must be a string or {route, multiplier} object")
    return tuple(members)


def _validate_iso_date(value: Any, path: str) -> str:
    if not isinstance(value, str) or not value:
        raise _fail(path, "date must be an ISO YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise _fail(path, f"invalid ISO date {value!r}") from exc
    if parsed > date.today():
        raise _fail(path, f"date {value!r} must not be in the future")
    return value


def validate_plan_sections(
    data: dict[str, Any], source: str, *, packaged: bool = False
) -> PlanSet:
    """Validate the plan sections of a control-plane document (fail closed).

    ``packaged`` marks the shipped default: any non-null ``verified_on``
    or ``toggles_attested_on`` there is a validation error, because
    verification is the owner's attestation.  Every failure raises
    ValueError whose message starts with the exact error path.
    """
    accounts_raw = data.get("accounts", {})
    pools_raw = data.get("quota_pools", {})
    postures_raw = data.get("postures", {})
    reserves_raw = data.get("reserves", [])
    if not isinstance(accounts_raw, dict):
        raise _fail("accounts", f"config at {source}: accounts must be an object")
    if not isinstance(pools_raw, dict):
        raise _fail("quota_pools", f"config at {source}: quota_pools must be an object")
    if not isinstance(postures_raw, dict):
        raise _fail("postures", f"config at {source}: postures must be an object")
    if not isinstance(reserves_raw, list):
        raise _fail("reserves", f"config at {source}: reserves must be a list")

    accounts: dict[str, Account] = {}
    for account_id, raw in accounts_raw.items():
        base = f"accounts.{account_id}"
        if not isinstance(account_id, str) or not account_id.strip():
            raise _fail("accounts", "account id must be a non-empty string")
        if not isinstance(raw, dict):
            raise _fail(base, "account must be an object")
        vendor = raw.get("vendor", "")
        plan = raw.get("plan", "")
        if not isinstance(vendor, str) or not isinstance(plan, str):
            raise _fail(base, "vendor/plan must be strings")
        toggles = raw.get("required_toggles", [])
        if not isinstance(toggles, list) or not all(isinstance(t, str) for t in toggles):
            raise _fail(f"{base}.required_toggles", "required_toggles must be a list of strings")
        attested = raw.get("toggles_attested_on")
        if attested is not None:
            if packaged:
                raise _fail(
                    f"{base}.toggles_attested_on",
                    "packaged defaults must not attest toggles (owner attestation only)",
                )
            attested = _validate_iso_date(attested, f"{base}.toggles_attested_on")
        notes = raw.get("notes", "")
        if not isinstance(notes, str):
            raise _fail(f"{base}.notes", "notes must be a string")
        accounts[account_id] = Account(
            account_id=account_id,
            vendor=vendor,
            plan=plan,
            required_toggles=tuple(toggles),
            toggles_attested_on=attested,
            notes=notes,
        )

    pool_plans: dict[str, PoolPlan] = {}
    for pool_id, raw in pools_raw.items():
        base = f"quota_pools.{pool_id}"
        if not isinstance(pool_id, str) or not pool_id.strip():
            raise _fail("quota_pools", "pool id must be a non-empty string")
        if not isinstance(raw, dict):
            raise _fail(base, "pool must be an object")
        account = raw.get("account")
        if account is not None:
            if not isinstance(account, str) or account not in accounts:
                raise _fail(f"{base}.account", f"unknown account {account!r}")
        overage = raw.get("overage_possible", False)
        if not isinstance(overage, bool):
            raise _fail(f"{base}.overage_possible", "overage_possible must be a bool")
        max_concurrent = raw.get("max_concurrent")
        if max_concurrent is not None:
            if (
                isinstance(max_concurrent, bool)
                or not isinstance(max_concurrent, int)
                or max_concurrent < 1
            ):
                raise _fail(f"{base}.max_concurrent", "max_concurrent must be an int >= 1 or null")
        windows = _validate_windows(raw.get("windows", []), f"{base}.windows")
        members = _validate_members(raw.get("members", []), f"{base}.members")
        mode = str(raw.get("mode", "STRICT"))
        red_line = raw.get("red_line_pct", 15.0)
        if isinstance(red_line, bool) or not isinstance(red_line, (int, float)):
            raise _fail(f"{base}.red_line_pct", "red_line_pct must be a number")
        pool_plans[pool_id] = PoolPlan(
            pool_id=pool_id,
            account=account,
            windows=windows,
            overage_possible=overage,
            max_concurrent=max_concurrent,
            members=members,
            mode=mode,
            red_line_pct=float(red_line),
        )

    postures: dict[str, PostureRecord] = {}
    for selector, raw in postures_raw.items():
        base = f"postures.{selector}"
        validate_selector(selector, base + ".selector", allow_backend=True)
        if not isinstance(raw, dict):
            raise _fail(base, "posture must be an object")
        cls = raw.get("class")
        if cls not in POSTURE_CLASSES:
            raise _fail(f"{base}.class", f"class must be one of {sorted(POSTURE_CLASSES)}")
        urls = raw.get("policy_urls", [])
        if not isinstance(urls, list) or not all(isinstance(u, str) for u in urls):
            raise _fail(f"{base}.policy_urls", "policy_urls must be a list of strings")
        for url in urls:
            if not url.startswith("https://"):
                raise _fail(f"{base}.policy_urls", f"policy url must be https: {url!r}")
        if cls in {"A", "B", "C"} and not urls:
            raise _fail(f"{base}.policy_urls", f"class {cls} requires at least one policy url")
        ttl = raw.get("ttl_days")
        if cls == "X":
            if ttl is not None:
                raise _fail(f"{base}.ttl_days", "class X must not set ttl_days")
        else:
            if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 90:
                raise _fail(f"{base}.ttl_days", "ttl_days must be an int in 1..90")
        verified = raw.get("verified_on")
        if verified is not None:
            if packaged:
                raise _fail(
                    f"{base}.verified_on",
                    "packaged defaults must not verify postures (owner attestation only)",
                )
            verified = _validate_iso_date(verified, f"{base}.verified_on")
        account = raw.get("account")
        if account is not None:
            if not isinstance(account, str) or account not in accounts:
                raise _fail(f"{base}.account", f"unknown account {account!r}")
        note = raw.get("note", "")
        if not isinstance(note, str):
            raise _fail(f"{base}.note", "note must be a string")
        postures[selector] = PostureRecord(
            selector=selector,
            posture_class=str(cls),
            policy_urls=tuple(urls),
            ttl_days=ttl,
            verified_on=verified,
            account=account,
            note=note,
        )

    window_index: dict[str, set[str]] = {
        pool_id: {window.id for window in plan.windows} for pool_id, plan in pool_plans.items()
    }
    reserves: list[Reserve] = []
    for index, raw in enumerate(reserves_raw):
        base = f"reserves[{index}]"
        if not isinstance(raw, dict):
            raise _fail(base, "reserve must be an object")
        pool = raw.get("pool")
        window = raw.get("window")
        if not isinstance(pool, str) or pool not in pool_plans:
            raise _fail(f"{base}.pool", f"unknown pool {pool!r}")
        if not isinstance(window, str) or window not in window_index.get(pool, set()):
            raise _fail(f"{base}.window", f"unknown window {window!r} for pool {pool!r}")
        pct = raw.get("pct")
        if isinstance(pct, bool) or not isinstance(pct, (int, float)) or not 0 <= pct <= 100:
            raise _fail(f"{base}.pct", "pct must be a number in 0..100")
        hours = raw.get("hours")
        if not isinstance(hours, str) or not _HOURS_RE.fullmatch(hours):
            raise _fail(f"{base}.hours", "hours must be HH:MM-HH:MM")
        days = raw.get("days", [])
        if not isinstance(days, list) or not all(isinstance(d, str) for d in days):
            raise _fail(f"{base}.days", "days must be a list of Mon..Sun")
        for day in days:
            if day not in RESERVE_DAYS:
                raise _fail(f"{base}.days", f"unknown day {day!r}")
        tz = raw.get("tz")
        if not isinstance(tz, str) or not tz:
            raise _fail(f"{base}.tz", "tz must be an IANA zone name")
        try:
            ZoneInfo(tz)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise _fail(f"{base}.tz", f"unknown IANA zone {tz!r}") from exc
        reserves.append(
            Reserve(
                pool=pool,
                window=str(window),
                pct=float(pct),
                hours=hours,
                days=tuple(days),
                tz=tz,
            )
        )

    return PlanSet(
        accounts=accounts,
        pool_plans=pool_plans,
        postures=postures,
        reserves=tuple(reserves),
    )


# ---------------------------------------------------------------------------
# Packaged defaults and owner overlay (step D)
# ---------------------------------------------------------------------------

ACCOUNT_FIELDS: tuple[str, ...] = (
    "vendor",
    "plan",
    "required_toggles",
    "toggles_attested_on",
    "notes",
)
POOL_FIELDS: tuple[str, ...] = (
    "mode",
    "red_line_pct",
    "members",
    "account",
    "windows",
    "overage_possible",
    "max_concurrent",
)
POSTURE_FIELDS: tuple[str, ...] = (
    "class",
    "policy_urls",
    "ttl_days",
    "verified_on",
    "account",
    "note",
)


def packaged_plans_path() -> Path:
    """Return the shipped default plan registry document."""
    return Path(__file__).resolve().parent.parent / "defaults" / "plans.json"


def load_packaged_plans_raw() -> dict[str, Any]:
    """Read and strictly validate the packaged default (owner attestation absent)."""
    path = packaged_plans_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Packaged plans at {path}: cannot read/parse") from exc
    if not isinstance(data, dict):
        raise ValueError(f"Packaged plans at {path}: top-level must be an object")
    try:
        validate_plan_sections(data, str(path), packaged=True)
    except ValueError as exc:
        raise ValueError(f"Packaged plans at {path}: {exc}") from exc
    return data


def overlay_plan_docs(
    packaged: dict[str, Any], owner: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge the owner document over the packaged default, field by field.

    The owner wins per field: a field present in the owner's entry for an
    account/pool/posture replaces the packaged value, while absent fields
    keep the packaged one.  ``reserves`` is wholesale: the owner's list
    replaces the packaged list only when the owner document carries the
    key.  Returns (merged_doc, sources) where sources marks every field
    ``packaged`` or ``owner``.
    """
    merged: dict[str, Any] = {"version": 2}
    sources: dict[str, Any] = {}

    def _merge_section(
        section: str, fields: tuple[str, ...]
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        packaged_section = packaged.get(section, {})
        owner_section = owner.get(section, {})
        if not isinstance(packaged_section, dict):
            packaged_section = {}
        if not isinstance(owner_section, dict):
            owner_section = {}
        merged_section: dict[str, Any] = {}
        section_sources: dict[str, Any] = {}
        for key in (*packaged_section, *[k for k in owner_section if k not in packaged_section]):
            packaged_entry = packaged_section.get(key)
            owner_entry = owner_section.get(key)
            if not isinstance(owner_entry, dict):
                merged_section[key] = packaged_entry
                if isinstance(packaged_entry, dict):
                    section_sources[key] = {
                        item: "packaged" for item in fields if item in packaged_entry
                    }
                continue
            if not isinstance(packaged_entry, dict):
                merged_section[key] = dict(owner_entry)
                section_sources[key] = {
                    item: "owner" for item in fields if item in owner_entry
                }
                continue
            entry: dict[str, Any] = {}
            entry_sources: dict[str, Any] = {}
            for item in fields:
                if item in owner_entry:
                    entry[item] = owner_entry[item]
                    entry_sources[item] = "owner"
                elif item in packaged_entry:
                    entry[item] = packaged_entry[item]
                    entry_sources[item] = "packaged"
            merged_section[key] = entry
            section_sources[key] = entry_sources
        return merged_section, section_sources

    merged["accounts"], sources["accounts"] = _merge_section("accounts", ACCOUNT_FIELDS)
    merged["quota_pools"], sources["quota_pools"] = _merge_section(
        "quota_pools", POOL_FIELDS
    )
    merged["postures"], sources["postures"] = _merge_section("postures", POSTURE_FIELDS)
    if "reserves" in owner and isinstance(owner["reserves"], list):
        merged["reserves"] = owner["reserves"]
        sources["reserves"] = "owner"
    else:
        merged["reserves"] = packaged.get("reserves", [])
        sources["reserves"] = "packaged"
    return merged, sources


def load_effective_plan_set() -> tuple[PlanSet, dict[str, Any]]:
    """Return the effective (packaged overlaid by owner) plan set plus sources.

    ``verified_on`` and ``toggles_attested_on`` can only come from the
    owner document: a packaged non-null value is a validation error, and
    the merge never takes those fields from the packaged side.
    """
    packaged = load_packaged_plans_raw()
    from saberops.control_plane.policy import _read_raw_doc

    owner = _read_raw_doc()
    merged, sources = overlay_plan_docs(packaged, owner)
    try:
        plans = validate_plan_sections(merged, "effective plans")
    except ValueError as exc:
        raise ValueError(f"Effective plans: {exc}") from exc
    return plans, sources
