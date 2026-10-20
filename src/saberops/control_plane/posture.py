"""Pure billing-posture decision (M2-2, step E).

Selects the most specific posture record for a route and decides
whether unattended execution is admitted.  This module performs no
I/O and is not wired into preflight or routing (that is M2-4b).

TTL boundary: ``expires_on = verified_on + ttl_days`` and the record
is valid *through* ``expires_on`` (expired when
``verified_on + ttl_days < today``).  Both sides of the boundary are
tested in ``tests/test_posture.py``.  A record with ``ttl_days`` unset
never expires: the owner's confirmation holds until they change it.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from saberops.control_plane.plans import PlanSet, PostureRecord, selector_kind

POSTURE_UNVERIFIED = "POSTURE_UNVERIFIED"
POSTURE_EXPIRED = "POSTURE_EXPIRED"
POSTURE_FORBIDDEN = "POSTURE_FORBIDDEN"
POSTURE_ATTENDED_ONLY = "POSTURE_ATTENDED_ONLY"
POSTURE_METERED = "POSTURE_METERED"

#: A route is this many days from expiry when it counts as expiring soon.
EXPIRING_SOON_DAYS = 3


@dataclass(frozen=True)
class PostureDecision:
    posture_class: str | None
    selector: str | None
    admitted_unattended: bool
    reason: str | None
    verified_on: str | None
    expires_on: str | None
    expiring_soon: bool


def _specificity(selector: str) -> tuple[int, int]:
    """Order key: exact route > longest prefix > provider > backend."""
    kind = selector_kind(selector)
    if kind == "route":
        return (3, len(selector))
    if kind == "prefix":
        return (2, len(selector))
    if kind == "provider":
        return (1, len(selector))
    return (0, 0)


def _matches(record: PostureRecord, provider: str, model: str, backend: str | None) -> bool:
    kind = selector_kind(record.selector)
    want_provider = provider.strip().lower()
    want_model = model.strip().lower()
    if kind == "backend":
        return backend is not None and backend.strip() == record.selector.partition(":")[2]
    if kind == "provider":
        return record.selector.lower() == want_provider
    sel_provider, _, rest = record.selector.partition("/")
    if sel_provider.lower() != want_provider:
        return False
    if kind == "route":
        return rest.lower() == want_model
    prefix = rest[:-2].lower()
    return want_model == prefix or want_model.startswith(prefix + "/")


def _parse_verified(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def posture_decision(
    provider: str,
    model: str,
    *,
    backend: str | None = None,
    plans: PlanSet,
    today: date,
) -> PostureDecision:
    """Decide the billing posture for one route (pure, deterministic)."""
    candidates = [
        record
        for record in plans.postures.values()
        if _matches(record, provider, model, backend)
    ]
    if not candidates:
        return PostureDecision(
            posture_class=None,
            selector=None,
            admitted_unattended=False,
            reason=POSTURE_UNVERIFIED,
            verified_on=None,
            expires_on=None,
            expiring_soon=False,
        )
    record = max(candidates, key=lambda item: _specificity(item.selector))
    cls = record.posture_class
    if cls == "X":
        return PostureDecision(
            posture_class=cls,
            selector=record.selector,
            admitted_unattended=False,
            reason=POSTURE_FORBIDDEN,
            verified_on=record.verified_on,
            expires_on=None,
            expiring_soon=False,
        )
    verified = _parse_verified(record.verified_on)
    if verified is None:
        return PostureDecision(
            posture_class=cls,
            selector=record.selector,
            admitted_unattended=False,
            reason=POSTURE_UNVERIFIED,
            verified_on=record.verified_on,
            expires_on=None,
            expiring_soon=False,
        )
    if record.ttl_days is None:
        # No expiry: the confirmation stands until the owner changes it.
        # The billing class is still enforced exactly as below.
        return PostureDecision(
            posture_class=cls,
            selector=record.selector,
            admitted_unattended=cls not in ("B", "D"),
            reason={"B": POSTURE_ATTENDED_ONLY, "D": POSTURE_METERED}.get(cls),
            verified_on=record.verified_on,
            expires_on=None,
            expiring_soon=False,
        )
    expires = verified + timedelta(days=record.ttl_days)
    if expires < today:
        return PostureDecision(
            posture_class=cls,
            selector=record.selector,
            admitted_unattended=False,
            reason=POSTURE_EXPIRED,
            verified_on=record.verified_on,
            expires_on=expires.isoformat(),
            expiring_soon=False,
        )
    if cls == "B":
        return PostureDecision(
            posture_class=cls,
            selector=record.selector,
            admitted_unattended=False,
            reason=POSTURE_ATTENDED_ONLY,
            verified_on=record.verified_on,
            expires_on=expires.isoformat(),
            expiring_soon=(expires - today).days <= EXPIRING_SOON_DAYS,
        )
    if cls == "D":
        return PostureDecision(
            posture_class=cls,
            selector=record.selector,
            admitted_unattended=False,
            reason=POSTURE_METERED,
            verified_on=record.verified_on,
            expires_on=expires.isoformat(),
            expiring_soon=(expires - today).days <= EXPIRING_SOON_DAYS,
        )
    return PostureDecision(
        posture_class=cls,
        selector=record.selector,
        admitted_unattended=True,
        reason=None,
        verified_on=record.verified_on,
        expires_on=expires.isoformat(),
        expiring_soon=(expires - today).days <= EXPIRING_SOON_DAYS,
    )


def posture_status(decision: PostureDecision) -> str:
    """Render the per-selector status for ``orch posture show``."""
    if decision.reason == POSTURE_UNVERIFIED:
        return "UNVERIFIED"
    if decision.reason == POSTURE_EXPIRED:
        return "EXPIRED"
    if decision.reason == POSTURE_FORBIDDEN:
        return "FORBIDDEN"
    if decision.reason == POSTURE_ATTENDED_ONLY:
        return "ATTENDED_ONLY"
    if decision.reason == POSTURE_METERED:
        return "METERED"
    if decision.admitted_unattended and decision.expiring_soon:
        return "EXPIRING"
    return "OK"
