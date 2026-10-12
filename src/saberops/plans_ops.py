"""Owner plan-registry and billing-posture operations (service layer).

Single service-layer authority for the ``orch plans`` and ``orch
posture`` commands, and the seam the future MCP server will call.
Every function here is the ONE service-layer function for its
operation; the CLI is a thin wrapper that only parses arguments and
renders output.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any

from saberops.control_plane.plans import (
    ACCOUNT_FIELDS,
    POOL_FIELDS,
    POSTURE_FIELDS,
    load_effective_plan_set,
    load_packaged_plans_raw,
    overlay_plan_docs,
    validate_iso_date,
    validate_plan_sections,
    validate_selector,
)
from saberops.control_plane.posture import posture_decision, posture_status


class PlansOperationError(ValueError):
    """Fail-closed refusal of a plan/posture operation."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


def show_plans() -> dict[str, Any]:
    """Return the effective plan set with each field's source marked.

    Every account/pool/posture entry carries its values plus a
    ``sources`` mapping of field -> ``packaged`` | ``owner``.
    """
    packaged = load_packaged_plans_raw()
    try:
        plans, sources = load_effective_plan_set()
    except ValueError as exc:
        raise PlansOperationError("PLANS_UNREADABLE", str(exc)) from exc
    merged, _ = overlay_plan_docs(packaged, _owner_raw())

    def _section(
        section: str, fields: tuple[str, ...]
    ) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for key, entry in merged.get(section, {}).items():
            if not isinstance(entry, dict):
                continue
            out[key] = {
                field: entry.get(field) for field in fields if field in entry
            }
            out[key]["sources"] = {
                field: sources.get(section, {}).get(key, {}).get(field, "packaged")
                for field in fields
                if field in entry
            }
        return out

    _ = plans  # validated authority; rendering comes from the merged raw doc.
    return {
        "accounts": _section("accounts", ACCOUNT_FIELDS),
        "quota_pools": _section("quota_pools", POOL_FIELDS),
        "postures": _section("postures", POSTURE_FIELDS),
        "reserves": merged.get("reserves", []),
        "reserves_source": sources.get("reserves", "packaged"),
    }


def _owner_raw() -> dict[str, Any]:
    from saberops.control_plane.policy import _read_raw_doc

    return _read_raw_doc()


def validate_plans(path: Path | None) -> dict[str, Any]:
    """Validate a control-plane document, or the effective plans by default.

    Returns ``{"ok", "errors", "path"}``.  With ``path`` the file is
    validated as a full control-plane document (version 1 or 2);
    without it the effective (packaged overlaid by owner) plan set is
    validated.
    """
    if path is not None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"ok": False, "errors": [f"{path}: cannot read/parse: {exc}"], "path": str(path)}
        if not isinstance(data, dict):
            return {
                "ok": False,
                "errors": [f"{path}: top-level must be an object"],
                "path": str(path),
            }
        try:
            validate_plan_sections(data, str(path))
        except ValueError as exc:
            return {"ok": False, "errors": [str(exc)], "path": str(path)}
        return {"ok": True, "errors": [], "path": str(path)}
    try:
        load_effective_plan_set()
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "path": "effective plans"}
    # The owner document itself must also load standalone (mode strings,
    # provider states and quota core are enforced by the document loader).
    try:
        from saberops.control_plane.policy import load_control_plane_policy

        load_control_plane_policy()
    except ValueError as exc:
        return {"ok": False, "errors": [str(exc)], "path": "effective plans"}
    return {"ok": True, "errors": [], "path": "effective plans"}


def show_postures(today: date) -> dict[str, Any]:
    """Return per-selector posture state: class, dates and status."""
    try:
        plans, _ = load_effective_plan_set()
    except ValueError as exc:
        raise PlansOperationError("PLANS_UNREADABLE", str(exc)) from exc
    rows: list[dict[str, Any]] = []
    for selector in sorted(plans.postures):
        provider, model, backend = _representative_route(selector)
        decision = posture_decision(
            provider, model, backend=backend, plans=plans, today=today
        )
        record = plans.postures[selector]
        rows.append(
            {
                "selector": selector,
                "class": record.posture_class,
                "verified_on": record.verified_on,
                "expires_on": decision.expires_on,
                "status": posture_status(decision),
            }
        )
    return {"postures": rows, "today": today.isoformat()}


def _representative_route(selector: str) -> tuple[str, str, str | None]:
    """Return a route exercising a selector for status display."""
    from saberops.control_plane.plans import selector_kind

    kind = selector_kind(selector.strip())
    if kind == "backend":
        return ("-", "-", selector.strip().partition(":")[2])
    if kind == "provider":
        return (selector.strip(), "__owner_verify__", None)
    provider, _, rest = selector.strip().partition("/")
    if kind == "route":
        return (provider, rest, None)
    return (provider, f"{rest[:-2]}/__owner_verify__", None)


def verify_posture(
    selector: str,
    urls: tuple[str, ...],
    note: str | None,
    posture_class: str | None,
    day: str | None,
) -> dict[str, Any]:
    """Record the owner's re-verification of one posture selector.

    Writes only the owner document, atomically, through policy.py's
    writer, and only the attestation fields (plus ``class`` for a new
    selector or an explicit class change).  All other fields keep coming
    from the packaged default via the overlay.
    """
    from saberops.control_plane.policy import save_owner_control_plane

    text = selector.strip()
    if not text:
        raise PlansOperationError("SELECTOR_REQUIRED", "A posture selector is required.")
    try:
        validate_selector(text, f"postures.{text}.selector", allow_backend=True)
    except ValueError as exc:
        raise PlansOperationError("INVALID_SELECTOR", str(exc)) from exc
    if posture_class is not None and posture_class not in {"A", "B", "C", "D", "X"}:
        raise PlansOperationError(
            "INVALID_CLASS", f"class must be one of A|B|C|D|X, got {posture_class!r}"
        )
    verified_on = day.strip() if isinstance(day, str) and day.strip() else date.today().isoformat()
    try:
        validate_iso_date(verified_on, f"postures.{text}.verified_on")
    except ValueError as exc:
        raise PlansOperationError("INVALID_DATE", str(exc)) from exc

    packaged = load_packaged_plans_raw()
    owner = _owner_raw()
    merged, _ = overlay_plan_docs(packaged, owner)
    try:
        effective = validate_plan_sections(merged, "effective plans")
    except ValueError as exc:
        raise PlansOperationError("PLANS_UNREADABLE", str(exc)) from exc

    existing = effective.postures.get(text)
    if existing is None and posture_class is None:
        raise PlansOperationError(
            "UNKNOWN_SELECTOR",
            f"Unknown posture selector {text!r}: pass --class to record a new one.",
        )
    new_class = posture_class if posture_class is not None else (
        existing.posture_class if existing is not None else "X"
    )
    provided = [url for url in urls if url.strip()]
    inherited = list(existing.policy_urls) if existing is not None else []
    final_urls = provided if provided else inherited
    if new_class in {"A", "B", "C"} and not final_urls:
        raise PlansOperationError(
            "URLS_REQUIRED",
            f"class {new_class} requires at least one https --url.",
        )
    for url in final_urls:
        if not url.startswith("https://"):
            raise PlansOperationError(
                "INVALID_URL", f"policy url must be https: {url!r}"
            )
    if existing is not None:
        new_ttl = existing.ttl_days
        new_account = existing.account
        new_note = note if note is not None else existing.note
    else:
        # A new selector has no packaged record to inherit: TTL defaults to
        # the packaged convention (30 days, X has none).
        new_ttl = 30 if new_class != "X" else None
        new_account = None
        new_note = note if note is not None else ""

    # The owner document must stay standalone-valid, so the entry is
    # stored complete (class, urls, ttl, attestation, account, note);
    # every other section of the owner document is left untouched.
    entry: dict[str, Any] = {
        "class": new_class,
        "policy_urls": final_urls,
        "verified_on": verified_on,
        "note": new_note,
    }
    if new_ttl is not None:
        entry["ttl_days"] = new_ttl
    if new_account is not None:
        entry["account"] = new_account
    owner_postures = dict(owner.get("postures", {})) if isinstance(
        owner.get("postures", {}), dict
    ) else {}
    owner_postures[text] = entry
    new_owner = dict(owner)
    new_owner["postures"] = owner_postures
    if "version" not in new_owner:
        new_owner["version"] = 2
    if new_account is not None:
        owner_accounts = new_owner.get("accounts", {})
        if not isinstance(owner_accounts, dict):
            owner_accounts = {}
        else:
            owner_accounts = dict(owner_accounts)
        if new_account not in owner_accounts:
            # The owner document stays standalone-valid: a posture that
            # pins a packaged-only account carries that account's
            # packaged definition with it. Nothing else is copied.
            packaged_accounts = packaged.get("accounts", {})
            if isinstance(packaged_accounts, dict) and isinstance(
                packaged_accounts.get(new_account), dict
            ):
                owner_accounts[new_account] = packaged_accounts[new_account]
        new_owner["accounts"] = owner_accounts

    # Validate the merged result before writing anything.
    new_merged, _ = overlay_plan_docs(packaged, new_owner)
    try:
        new_effective = validate_plan_sections(new_merged, "effective plans")
    except ValueError as exc:
        raise PlansOperationError("POSTURE_NOT_SAVED", str(exc)) from exc
    try:
        save_owner_control_plane(new_owner)
    except ValueError as exc:
        raise PlansOperationError("POSTURE_NOT_SAVED", str(exc)) from exc

    provider, model, backend = _representative_route(text)
    decision = posture_decision(
        provider,
        model,
        backend=backend,
        plans=new_effective,
        today=date.fromisoformat(verified_on),
    )
    return {
        "selector": text,
        "class": new_class,
        "verified_on": verified_on,
        "expires_on": decision.expires_on,
        "status": posture_status(decision),
    }
