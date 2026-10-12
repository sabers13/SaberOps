"""Zero-inference usage probes with persisted rate limiting (M2-1 C)."""

from __future__ import annotations

import os
import shutil
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Protocol

from saberops.capacity.exclusions import exclusion_state, parse_iso

HARNESS_PROVIDER: dict[str, str] = {
    "codex": "codex",
    "claude": "anthropic",
    "opencode-go": "opencode",
}

REGISTERED_HARNESSES: tuple[str, ...] = ("codex", "claude", "opencode-go")

#: Antigravity is intentionally not registered (owner-initiated only).
#: Cline has no probe.


class ProbeWindow(tuple):  # type: ignore[type-arg]
    """(window_id, remaining_pct, reset_at, reset_confidence)."""


class Probe(Protocol):
    harness: str

    def read(self, now: datetime) -> list[tuple[str, float, datetime | None, str]]:
        ...


def duration_to_window(duration: Any) -> str:
    """Map a duration in minutes to a window id."""
    try:
        minutes = float(duration) if duration is not None else None
    except (TypeError, ValueError):
        minutes = None
    if minutes is None:
        return "monthly"
    if abs(minutes - 300) < 1:
        return "5h"
    if abs(minutes - 1440) < 1:
        return "daily"
    if abs(minutes - 10080) < 1:
        return "weekly"
    if minutes >= 40000:
        return "monthly"
    return "ALL"


def _utcnow(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(UTC) if now.tzinfo else now.replace(tzinfo=UTC)
    return datetime.now(UTC)


def read_codex_windows(
    executable: str, *, now: datetime | None = None
) -> list[tuple[str, float, datetime | None, str]]:
    """Read Codex app-server rate limits (structured resets -> EXACT)."""
    from saberops.cli_usage import read_limits

    _ = now
    rows = read_limits(executable, "codex")
    out: list[tuple[str, float, datetime | None, str]] = []
    for row in rows:
        raw_remaining = row.get("remaining")
        if not isinstance(raw_remaining, (int, float)) or isinstance(raw_remaining, bool):
            continue
        remaining = float(raw_remaining)
        window = duration_to_window(row.get("duration"))
        reset_raw = row.get("reset_at")
        reset = parse_iso(reset_raw) if isinstance(reset_raw, str) else None
        out.append((window, remaining, reset, "EXACT"))
    return out


def read_claude_windows(
    executable: str, *, now: datetime | None = None, tz: Any | None = None
) -> list[tuple[str, float, datetime | None, str]]:
    """Read Claude /usage panel rows; reset_text parsed (zoned PARSED else HINT)."""
    from saberops.native_usage_panel import read_panel

    moment = _utcnow(now)
    rows = read_panel(executable, "anthropic")
    out: list[tuple[str, float, datetime | None, str]] = []
    for row in rows:
        raw_remaining = row.get("remaining")
        if not isinstance(raw_remaining, (int, float)) or isinstance(raw_remaining, bool):
            continue
        remaining = float(raw_remaining)
        label = str(row.get("label") or "")
        low = label.lower()
        if "session" in low:
            window = "5h"
        elif "week" in low:
            window = "weekly"
        else:
            window = duration_to_window(row.get("duration"))
        reset_text = row.get("reset_text")
        reset: datetime | None = None
        confidence = "UNKNOWN"
        if isinstance(reset_text, str) and reset_text.strip():
            parsed, conf = parse_claude_reset_text(reset_text, now=moment, tz=tz)
            reset, confidence = parsed, conf
        out.append((window, remaining, reset, confidence))
    return out


def parse_claude_reset_text(
    text: str, *, now: datetime | None = None, tz: Any | None = None
) -> tuple[datetime | None, str]:
    """Parse Claude panel reset_text with B's parser rules.

    Zoned or UTC-dated prose -> PARSED; bare clock -> HINT (next occurrence
    in host-local time, injectable tz for tests); else UNKNOWN.
    """
    import re
    from zoneinfo import ZoneInfo

    from saberops.workers.exhaustion import _CLOCK_RE, _MONTH_NAME_TO_NUM, _MONTH_RE

    moment = _utcnow(now)
    zone_match = re.search(r"\((?P<zone>[A-Za-z_][A-Za-z0-9_+\-/]*)\)", text)
    month_match = _MONTH_RE.search(text)
    clock_match = _CLOCK_RE.search(text)
    if zone_match is not None and month_match is not None and clock_match is not None:
        zone = zone_match.group("zone")
        try:
            zoneinfo = ZoneInfo("UTC" if zone.upper() == "UTC" else zone)
        except Exception:
            return None, "UNKNOWN"
        month_token = month_match.group(0)
        month_num = _MONTH_NAME_TO_NUM[month_token[:3].lower()]
        day_match = re.search(r"\d{1,2}", month_token[3:] or month_token)
        if day_match is None:
            return None, "UNKNOWN"
        day = int(day_match.group(0))
        year_match = re.search(r"\d{4}", month_token)
        year = int(year_match.group(0)) if year_match else moment.astimezone(zoneinfo).year
        hour = int(clock_match.group(1)) % 12
        if clock_match.group(3).lower() == "pm":
            hour += 12
        minute = int(clock_match.group(2)) if clock_match.group(2) else 0
        try:
            reset = datetime(year, month_num, day, hour, minute, tzinfo=zoneinfo).astimezone(UTC)
        except ValueError:
            return None, "UNKNOWN"
        return reset, "PARSED"
    if clock_match is not None:
        # HINT: next occurrence in host-local time (injectable tz).
        try:
            local_tz = tz if tz is not None else datetime.now().astimezone().tzinfo
            assert local_tz is not None
            local_now = moment.astimezone(local_tz)
        except Exception:
            return None, "UNKNOWN"
        hour = int(clock_match.group(1)) % 12
        if clock_match.group(3).lower() == "pm":
            hour += 12
        minute = int(clock_match.group(2)) if clock_match.group(2) else 0
        candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if candidate <= local_now:
            from datetime import timedelta

            candidate = candidate + timedelta(days=1)
        try:
            return candidate.astimezone(UTC), "HINT"
        except Exception:
            return None, "UNKNOWN"
    return None, "UNKNOWN"


def read_go_windows(
    executable: str, *, now: datetime | None = None
) -> list[tuple[str, float, datetime | None, str]]:
    """Read OpenCode Go plugin limits (structured resets -> EXACT)."""
    from saberops.opencode_go_usage import read_go_limits

    _ = now
    rows = read_go_limits(executable)
    out: list[tuple[str, float, datetime | None, str]] = []
    for row in rows:
        raw_remaining = row.get("remaining")
        if not isinstance(raw_remaining, (int, float)) or isinstance(raw_remaining, bool):
            continue
        remaining = float(raw_remaining)
        label = str(row.get("label") or "").lower()
        if "5 hour" in label:
            window = "5h"
        elif "week" in label:
            window = "weekly"
        elif "month" in label:
            window = "monthly"
        else:
            window = duration_to_window(row.get("duration"))
        reset_raw = row.get("reset_at")
        reset = parse_iso(reset_raw) if isinstance(reset_raw, str) else None
        out.append((window, remaining, reset, "EXACT"))
    return out


_READERS = {
    "codex": read_codex_windows,
    "claude": read_claude_windows,
    "opencode-go": read_go_windows,
}


def pools_for_probe(
    harness: str, pool_policies: dict[str, Any] | None
) -> list[str]:
    """Pools whose members match the harness's routes, else [provider].

    The fallback is the harness's provider (``HARNESS_PROVIDER``), matching
    :func:`saberops.control_plane.quota_policy.pools_for`, which resolves
    candidates to ``[provider]`` when no policy matches. Falling back to the
    raw harness id (e.g. ``claude``) would write exclusions no candidate ever
    checks (candidates use provider names like ``anthropic``). The exclusion
    ``source`` stays the harness id.
    """
    provider = HARNESS_PROVIDER.get(harness, harness)
    if not pool_policies:
        return [provider]
    matched: list[str] = []
    for pool_id, policy in pool_policies.items():
        members = getattr(policy, "members", ()) or ()
        for member in members:
            sel = member.get("route") if isinstance(member, dict) else member
            if not isinstance(sel, str):
                continue
            text = sel.strip().lower()
            prov = provider.lower()
            if harness == "opencode-go":
                # Go usage covers Go routes, not every OpenCode upstream.
                prefix = prov + "/opencode-go"
                hit = (
                    text == prov or text == prefix or text == prefix + "/*"
                    or text.startswith(prefix + "/")
                )
            else:
                hit = text == prov or text.startswith(prov + "/")
            if hit:
                matched.append(pool_id)
                break
    return matched or [provider]


def _load_pool_policies() -> dict[str, Any]:
    try:
        from saberops.control_plane.plans import effective_quota_pools
        from saberops.control_plane.policy import load_control_plane_policy

        return effective_quota_pools(load_control_plane_policy())
    except Exception:
        return {}


def handle_probe_result(
    db: Any,
    *,
    harness: str,
    windows: list[tuple[str, float, datetime | None, str]],
    now: datetime | None = None,
    pool_policies: dict[str, Any] | None = None,
    run_id: str | None = None,
) -> None:
    """Apply one probe reading: exclusions, observations, red-line rows."""
    from saberops.capacity.store import clear_pool_exclusions, upsert_exclusion
    from saberops.db import current_iso_timestamp
    from saberops.models import QuotaObservation, QuotaRawSemantics

    moment = _utcnow(now)
    policies = pool_policies if pool_policies is not None else _load_pool_policies()
    pools = pools_for_probe(harness, policies)
    provider = HARNESS_PROVIDER.get(harness, harness)
    now_iso = moment.isoformat()
    for window_id, remaining, reset, confidence in windows:
        # Observation audit (source PROBE).
        try:
            obs = QuotaObservation(
                id=f"probe_{harness}_{window_id}_{moment.strftime('%Y%m%d%H%M%S%f')}",
                provider=provider,
                account_ref=None,
                pool=window_id,
                window_type=window_id,
                raw_value=float(remaining),
                raw_limit=100.0,
                raw_semantics=QuotaRawSemantics.REMAINING_PERCENT.value,
                normalized_remaining_pct=float(remaining),
                reset_at=reset.isoformat() if reset else None,
                observed_at=now_iso or current_iso_timestamp(),
                source=f"PROBE:{harness}",
                freshness="FRESH",
                confidence="VERIFIED",
                run_id=run_id,
            )
            try:
                db.create_quota_observation(obs)
            except Exception:
                pass
        except Exception:
            pass
        if remaining <= 0:
            for pool_id in pools:
                try:
                    upsert_exclusion(
                        db, pool_id=pool_id, window_id=window_id,
                        reason="PROBE_ZERO", until=reset,
                        until_confidence=confidence, source=harness,
                        run_id=run_id, now=moment,
                    )
                except Exception:
                    pass
        else:
            for pool_id in pools:
                if window_id == "ALL":
                    continue
                try:
                    clear_pool_exclusions(
                        db, pool_id, window_id=window_id, by="PROBE", now=moment
                    )
                except Exception:
                    pass
    # Red-line path visibility: one legacy row per pool with the minimum
    # remaining across the probed windows (pool-scoped, not provider+window).
    # Per-window detail lives only in the quota observations above.
    if windows:
        try:
            min_window = min(windows, key=lambda w: float(w[1]))
            min_remaining = float(min_window[1])
            min_reset = min_window[2]
            min_reset_iso = min_reset.isoformat() if min_reset else None
            for pool_id in pools:
                try:
                    db.set_quota(provider, pool_id, min_remaining, min_reset_iso)
                except Exception:
                    pass
        except Exception:
            pass
    # ALL-window exclusions clear only when every probed window is > 0.
    try:
        if windows and all(w[1] > 0 for w in windows):
            for pool_id in pools:
                try:
                    clear_pool_exclusions(
                        db, pool_id, window_id="ALL", by="PROBE", now=moment
                    )
                except Exception:
                    pass
    except Exception:
        pass


def run_due_probes(
    db: Any,
    *,
    now: datetime | None = None,
    probes: dict[str, Any] | None = None,
    pool_policies: dict[str, Any] | None = None,
    deadline: float | None = None,
    force_harnesses: Iterable[str] = (),
) -> dict[str, str]:
    """Run probes only for harnesses with a PROBE_DUE exclusion; never raises.

    A harness is considered only when at least one exclusion is PROBE_DUE now
    and maps to it (``source == harness`` or its pool is in
    ``pools_for_probe``); otherwise no executable is looked up and the outcome
    is ``not-due``. ``force_harnesses`` runs named harnesses regardless (for
    future callers; unused now). Under pytest without an injected probe for
    the harness, no real executable is ever resolved or launched
    (``disabled-under-test``): tests must never depend on owner logins.
    Returns {harness: outcome}; a failing probe never clears and never raises.
    """
    from saberops.capacity.store import (
        ensure_tables,
        list_active_exclusions,
        probe_allowed,
        record_probe_run,
    )

    moment = _utcnow(now)
    ensure_tables(db)
    try:
        active = list_active_exclusions(db, now=moment)
    except Exception:
        active = []
    try:
        policies = pool_policies if pool_policies is not None else _load_pool_policies()
    except Exception:
        policies = {}
    due_harness: set[str] = set()
    hint_due_harness: set[str] = set()
    try:
        for exc in active:
            if exclusion_state(exc, moment) != "PROBE_DUE":
                continue
            for harness in REGISTERED_HARNESSES:
                if _exclusion_maps_to_harness(exc, harness, policies):
                    due_harness.add(harness)
            if str(exc.get("until_confidence") or "").upper() == "HINT":
                until = exc.get("until")
                parsed = until if isinstance(until, datetime) else parse_iso(
                    until if isinstance(until, str) else None
                )
                if parsed is not None and moment >= parsed:
                    for harness in REGISTERED_HARNESSES:
                        if _exclusion_maps_to_harness(exc, harness, policies):
                            hint_due_harness.add(harness)
    except Exception:
        pass
    forced = {h.strip() for h in force_harnesses if isinstance(h, str) and h.strip()}
    under_test = os.environ.get("PYTEST_CURRENT_TEST") is not None
    outcomes: dict[str, str] = {}
    for harness in REGISTERED_HARNESSES:
        if deadline is not None and time.monotonic() >= deadline:
            break
        if harness not in forced and harness not in due_harness:
            outcomes[harness] = "not-due"
            continue
        if harness not in forced:
            try:
                allowed = probe_allowed(
                    db, harness, now=moment, hint_due=(harness in hint_due_harness)
                )
            except Exception:
                allowed = True
            if not allowed:
                outcomes[harness] = "rate-limited"
                continue
        # Custom injected probe (tests) takes precedence.
        custom = (probes or {}).get(harness) if probes else None
        if custom is None and under_test:
            # Under pytest, real CLIs must never launch: tests would depend
            # on owner logins and account state.
            outcomes[harness] = "disabled-under-test"
            continue
        try:
            if custom is not None:
                if callable(getattr(custom, "read", None)):
                    windows = custom.read(moment)
                elif callable(custom):
                    windows = custom(moment)
                else:
                    windows = list(custom)
            else:
                # Default: real executables; missing binary is not a failure.
                exe_name = {
                    "codex": "codex", "claude": "claude", "opencode-go": "opencode",
                }[harness]
                executable = shutil.which(exe_name)
                if not executable:
                    outcomes[harness] = "no-executable"
                    continue
                reader = _READERS[harness]
                windows = reader(executable, now=moment)
            handle_probe_result(
                db, harness=harness, windows=list(windows),
                now=moment, pool_policies=pool_policies,
            )
            try:
                record_probe_run(db, harness=harness, ok=True, now=moment)
            except Exception:
                pass
            outcomes[harness] = "ran"
        except Exception:
            try:
                record_probe_run(
                    db, harness=harness, ok=False, error_code="PROBE_FAILED", now=moment
                )
            except Exception:
                pass
            outcomes[harness] = "failed"
    return outcomes


def _exclusion_maps_to_harness(
    exc: dict[str, Any], harness: str, policies: dict[str, Any] | None
) -> bool:
    """True when an exclusion is probed by ``harness`` (source or pool)."""
    if str(exc.get("source") or "") == harness:
        return True
    pool_id = exc.get("pool_id")
    if isinstance(pool_id, str) and pool_id:
        try:
            return pool_id in pools_for_probe(harness, policies)
        except Exception:
            return False
    return False


def probe_available_for(
    exc: dict[str, Any],
    *,
    policies: dict[str, Any] | None,
    probes: dict[str, Any] | None,
    now: datetime | None = None,
) -> bool:
    """True when some registered harness could probe ``exc`` (no launch).

    Used by the seam for the no-probe path: an injected probe counts; outside
    pytest a present executable counts; under pytest without an injected probe
    nothing counts. Never launches anything.
    """
    _ = now
    under_test = os.environ.get("PYTEST_CURRENT_TEST") is not None
    for harness in REGISTERED_HARNESSES:
        try:
            if not _exclusion_maps_to_harness(exc, harness, policies):
                continue
        except Exception:
            continue
        if probes and harness in probes:
            return True
        if under_test:
            continue
        exe_name = {
            "codex": "codex", "claude": "claude", "opencode-go": "opencode",
        }[harness]
        try:
            if shutil.which(exe_name):
                return True
        except Exception:
            continue
    return False


__all__ = [
    "HARNESS_PROVIDER",
    "REGISTERED_HARNESSES",
    "Probe",
    "duration_to_window",
    "read_codex_windows",
    "read_claude_windows",
    "read_go_windows",
    "parse_claude_reset_text",
    "pools_for_probe",
    "probe_available_for",
    "handle_probe_result",
    "run_due_probes",
]
