"""Owner quota observation vocabulary.

Normalization only: one raw observation becomes a remaining percentage.
Routing policy lives in ``control_plane/quota_policy.py`` and
``capacity/``. The legacy provider-keyed NORMAL/DEMOTED/BLOCKED policy
(25 % threshold) was deleted in M2-4a; ``orch quota set|list|enable``
are owner commands mapped onto the capacity ledger.
"""

from __future__ import annotations

import math
from typing import Any, cast

from saberops.models import QuotaRawSemantics


def normalize_quota_observation(
    raw_value: object,
    raw_semantics: QuotaRawSemantics | str,
    raw_limit: object = None,
) -> float | None:
    """Normalize one raw observation without applying routing policy.

    This lives with the quota observation vocabulary so the database write
    boundary can derive the canonical value without importing observability.
    The persisted meaning is always *remaining* percentage: 100 is unused and
    0 is exhausted.
    """
    try:
        value = float(cast(Any, raw_value)) if raw_value is not None else math.nan
        semantics = QuotaRawSemantics(str(raw_semantics))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(value) or value < 0:
        return None
    if semantics in (QuotaRawSemantics.REMAINING_PERCENT, QuotaRawSemantics.USED_PERCENT):
        if value > 100:
            return None
        result = (
            value
            if semantics == QuotaRawSemantics.REMAINING_PERCENT
            else 100.0 - value
        )
    else:
        try:
            limit = float(cast(Any, raw_limit)) if raw_limit is not None else math.nan
        except (TypeError, ValueError):
            return None
        if not math.isfinite(limit) or limit <= 0 or value > limit:
            return None
        result = (
            100.0 - (value / limit * 100.0)
            if semantics == QuotaRawSemantics.USED_AND_LIMIT
            else value / limit * 100.0
        )
    if not math.isfinite(result) or result < 0 or result > 100:
        return None
    return round(result, 10)
