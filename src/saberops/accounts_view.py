"""The Accounts page: how much of each subscription is left, and its billing.

Usage comes only from zero-inference readings of each CLI's own usage
metadata (no prompt is ever sent):

* Codex, Claude Code and OpenCode Go are the capacity harnesses the engine
  already probes; "Check now" forces that same probe
  (:func:`saberops.capacity.probes.run_due_probes`), so the reading also feeds
  capacity routing exactly as a scheduled probe would.
* Antigravity is deliberately not a registered capacity harness (owner
  initiated only). Its reading is cached in a small display-only file and
  never reaches routing or exclusions.

Billing rows are the owner's posture attestations from the plans registry,
shown in plain words; confirming again goes through the existing verify
route and authority.
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

__all__ = [
    "ACCOUNTS",
    "Account",
    "accounts_view",
    "check_usage",
]

#: Owner-facing names of the billing classes recorded by ``orch posture``.
BILLING_LABELS: dict[str, str] = {
    "A": "Subscription, may run on its own",
    "B": "Subscription, only while you watch",
    "C": "Pay per use, may run on its own",
    "D": "Pay per use, only while you watch",
    "X": "Not allowed",
}

_WINDOW_LABELS: dict[str, tuple[int, str]] = {
    "5h": (0, "5-hour window"),
    "daily": (1, "Today"),
    "weekly": (2, "This week"),
    "monthly": (3, "This month"),
    "ALL": (4, "Overall"),
}

_STALE_AFTER_MINUTES = 60

#: Posture statuses that mean the owner's confirmation is on record and current.
#: ATTENDED_ONLY and METERED are confirmed too: they only limit *when* the
#: account may run (while you watch / pay per use), not whether it is confirmed.
CONFIRMED_STATUSES = frozenset({"OK", "EXPIRING", "ATTENDED_ONLY", "METERED"})


@dataclass(frozen=True)
class Account:
    slug: str
    name: str
    provider: str
    harness: str  # capacity harness id, or "antigravity" (display-only cache)
    executable: str

    @property
    def probed_by_engine(self) -> bool:
        from saberops.capacity.probes import REGISTERED_HARNESSES

        return self.harness in REGISTERED_HARNESSES


ACCOUNTS: tuple[Account, ...] = (
    Account("codex", "Codex", "codex", "codex", "codex"),
    Account("claude", "Claude", "anthropic", "claude", "claude"),
    Account("opencode", "OpenCode Go", "opencode", "opencode-go", "opencode"),
    Account("antigravity", "Antigravity", "antigravity", "antigravity", "agy"),
)


def _cache_path() -> Path:
    override = os.environ.get("ORCH_ACCOUNT_USAGE_CACHE", "").strip()
    if override:
        return Path(override).expanduser()
    from saberops.project import owner_state_dir

    return owner_state_dir() / "account-usage-cache.json"


def _parse(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _ago(moment: datetime | None, now: datetime) -> str:
    if moment is None:
        return "never"
    minutes = int((now - moment).total_seconds() // 60)
    if minutes < 1:
        return "just now"
    if minutes < 60:
        return f"{minutes} min ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h ago"
    return f"{hours // 24} days ago"


def _until(moment: datetime | None, now: datetime) -> str | None:
    if moment is None:
        return None
    minutes = int((moment - now).total_seconds() // 60)
    if minutes <= 0:
        return "resetting now"
    if minutes < 60:
        return f"resets in {minutes} min"
    hours, rest = divmod(minutes, 60)
    if hours < 24:
        return f"resets in {hours} h {rest} min" if rest else f"resets in {hours} h"
    return f"resets in {hours // 24} d {hours % 24} h"


Reading = tuple[str, str, float, str | None, str]


def _window_rows(raw: list[Reading], now: datetime) -> list[dict[str, Any]]:
    """raw: (window_id, label, remaining_pct, reset_iso, observed_iso)."""
    rows: list[dict[str, Any]] = []
    for window_id, label, remaining, reset_iso, observed_iso in raw:
        order, plain = _WINDOW_LABELS.get(window_id, (5, window_id))
        pct = max(0.0, min(100.0, float(remaining)))
        reset = _parse(reset_iso)
        rows.append(
            {
                "window": window_id,
                "label": label or plain,
                "order": order,
                "remaining": round(pct),
                "used": round(100 - pct),
                "tone": "bad" if pct <= 5 else ("warn" if pct <= 20 else "ok"),
                "reset": _until(reset, now),
                "reset_at": reset.isoformat() if reset else None,
                "observed_at": observed_iso,
            }
        )
    rows.sort(key=lambda row: (row["order"], row["label"]))
    return rows


def _engine_readings(db: Any, harness: str) -> list[Reading]:
    """Latest capacity probe observation per window for one harness."""
    from saberops.capacity.store import ensure_tables, owner_connection

    ensure_tables(db)
    with owner_connection(db) as conn:
        rows = conn.execute(
            "SELECT window_id, remaining_pct, reset_at, observed_at FROM capacity_observations"
            " WHERE source = 'PROBE' AND legacy_provider = ?"
            " ORDER BY observed_at DESC",
            (harness,),
        ).fetchall()
    latest: dict[str, Reading] = {}
    for row in rows:
        window = str(row["window_id"])
        if window not in latest:
            latest[window] = (
                window, "", float(row["remaining_pct"]), row["reset_at"], str(row["observed_at"])
            )
    return list(latest.values())


def _cache_readings(slug: str) -> list[Reading]:
    try:
        data = json.loads(_cache_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    entry = data.get(slug) if isinstance(data, dict) else None
    if not isinstance(entry, dict):
        return []
    observed = str(entry.get("observed_at") or "")
    out: list[Reading] = []
    for row in entry.get("windows") or []:
        if not isinstance(row, dict):
            continue
        remaining = row.get("remaining")
        if not isinstance(remaining, (int, float)) or isinstance(remaining, bool):
            continue
        out.append(
            (str(row.get("window") or ""), str(row.get("label") or ""), float(remaining),
             row.get("reset_at") if isinstance(row.get("reset_at"), str) else None, observed)
        )
    return out


def _antigravity_label(label: str) -> str:
    group, _, kind = label.partition(" · ")
    when = "5-hour window" if "five" in kind.lower() else "This week"
    return f"{group.title()} models, {when.lower()}" if group else when


def check_usage(db: Any, slug: str, *, now: datetime | None = None) -> str:
    """Owner-initiated, zero-inference usage reading for one account.

    Returns a plain outcome: "ran", "no-executable", "failed" or
    "disabled-under-test". Never raises.
    """
    account = next((a for a in ACCOUNTS if a.slug == slug), None)
    if account is None:
        return "failed"
    moment = now or datetime.now(UTC)
    if account.probed_by_engine:
        from saberops.capacity.probes import run_due_probes

        outcomes = run_due_probes(db, now=moment, force_harnesses=(account.harness,))
        return outcomes.get(account.harness, "failed")
    if os.environ.get("PYTEST_CURRENT_TEST") is not None:
        return "disabled-under-test"
    executable = shutil.which(account.executable)
    if not executable:
        return "no-executable"
    try:
        from saberops.capacity.probes import duration_to_window
        from saberops.cli_usage import read_limits

        rows = read_limits(executable, account.provider)
    except Exception:
        return "failed"
    windows = [
        {
            "window": duration_to_window(row.get("duration")),
            "label": _antigravity_label(str(row.get("label") or "")),
            "remaining": row.get("remaining"),
            "reset_at": row.get("reset_at"),
        }
        for row in rows
        if isinstance(row.get("remaining"), (int, float))
    ]
    path = _cache_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (OSError, ValueError):
        data = {}
    data[slug] = {"observed_at": moment.isoformat(), "windows": windows}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return "failed"
    return "ran"


def _billing_rows(today: date) -> tuple[list[dict[str, Any]], str | None]:
    from saberops import plans_ops
    from saberops.control_plane.plans import load_effective_plan_set

    try:
        statuses = {row["selector"]: row for row in plans_ops.show_postures(today)["postures"]}
        plans, _ = load_effective_plan_set()
    except Exception as exc:
        return [], f"Billing confirmations could not be read: {exc}"
    rows: list[dict[str, Any]] = []
    for selector, status in statuses.items():
        record = plans.postures.get(selector)
        posture_class = str(status.get("class") or "X")
        state = str(status.get("status") or "UNVERIFIED")
        rows.append(
            {
                "selector": selector,
                "class": posture_class,
                "billing": BILLING_LABELS.get(posture_class, posture_class),
                "verified_on": status.get("verified_on"),
                "expires_on": status.get("expires_on"),
                "status": state,
                "ok": state in CONFIRMED_STATUSES,
                "urls": list(getattr(record, "policy_urls", ()) or ()),
                "note": getattr(record, "note", "") or "",
            }
        )
    return rows, None


def _belongs(selector: str, account: Account) -> bool:
    text = selector.strip().lower()
    if account.slug == "opencode":
        return text.startswith("opencode")
    return text == account.provider or text.startswith(account.provider + "/")


def accounts_view(db: Any, *, now: datetime | None = None) -> dict[str, Any]:
    """Everything the Accounts page shows (never raises)."""
    moment = now or datetime.now(UTC)
    billing, billing_error = _billing_rows(moment.date())
    used: set[str] = set()
    cards: list[dict[str, Any]] = []
    for account in ACCOUNTS:
        try:
            raw = (
                _engine_readings(db, account.harness)
                if account.probed_by_engine
                else _cache_readings(account.slug)
            )
        except Exception:
            raw = []
        windows = _window_rows(raw, moment)
        observed = max((_parse(w["observed_at"]) or moment for w in windows), default=None)
        mine = [row for row in billing if _belongs(row["selector"], account)]
        used.update(row["selector"] for row in mine)
        lowest = min(windows, key=lambda w: w["remaining"]) if windows else None
        cards.append(
            {
                "slug": account.slug,
                "name": account.name,
                "installed": shutil.which(account.executable) is not None,
                "windows": windows,
                "checked": _ago(observed, moment),
                "stale": observed is None
                or (moment - observed).total_seconds() > _STALE_AFTER_MINUTES * 60,
                "lowest": lowest,
                "billing": mine,
                "billing_ok": any(row["ok"] for row in mine),
                "display_only": not account.probed_by_engine,
            }
        )
    others = [row for row in billing if row["selector"] not in used]
    low = [
        (card["name"], card["lowest"])
        for card in cards
        if card["lowest"] is not None and card["lowest"]["remaining"] <= 20
    ]
    unconfirmed = [card["name"] for card in cards if card["billing"] and not card["billing_ok"]]
    return {
        "cards": cards,
        "others": others,
        "low": low,
        "unconfirmed": unconfirmed,
        "billing_error": billing_error,
        "labels": BILLING_LABELS,
    }
