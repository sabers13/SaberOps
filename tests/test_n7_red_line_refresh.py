"""N7: a red line tipped by synthetic charges must not survive fresh evidence.

Each admitted task posts an estimated charge after the last vendor observation.
After about nine tasks those charges alone cross the 15 % red line while the
vendor still reports the pool at 100 %.  The dispatch boundary must re-probe a
safe harness before it refuses, and expose the charge ledger terms that produced
the decision.  A genuinely low fresh pool still refuses.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops.capacity.ledger import record_owner_quota
from saberops.capacity.store import owner_connection
from saberops.control_plane.plans import (
    PlanSet,
    PoolMember,
    PoolPlan,
    PostureRecord,
    Window,
)
from saberops.control_plane.policy import (
    ControlPlanePolicy,
    ProviderPolicy,
    ProviderPolicyState,
)
from saberops.db import Database
from saberops.dispatch import control_plane_decision, refresh_quota_evidence
from saberops.models import WorkerCandidate

_NOW = datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC)


class _Adapter:
    def max_prompt_bytes(self) -> int:
        return 200_000

    def prompt_transport_for(self, prompt_bytes: int) -> str:
        return "stdin"

    def instruction_compatibility(self, model: str) -> str:
        return "FULL"

    def is_available(self) -> bool:
        return True


class _Probe:
    """Deterministic zero-inference probe used by the tests."""

    harness = "codex"

    def __init__(self, remaining: float) -> None:
        self._remaining = remaining

    def read(self, now: datetime) -> list[tuple[str, float, datetime | None, str]]:
        return [("5h", self._remaining, _NOW + timedelta(hours=3), "EXACT")]


def _policy() -> ControlPlanePolicy:
    return ControlPlanePolicy(
        provider_policies={
            "codex": ProviderPolicy("codex", ProviderPolicyState.ALLOWED),
        },
        quota_pools={},
        content_hash="n7-policy",
        plans=PlanSet(
            accounts={},
            postures={
                "codex": PostureRecord(
                    "codex", "A", ttl_days=30, verified_on=_NOW.date().isoformat()
                )
            },
            pool_plans={
                "chatgpt-plus": PoolPlan(
                    pool_id="chatgpt-plus",
                    members=(PoolMember(route="codex", multiplier=1.0),),
                    windows=(Window(id="5h", duration_min=300),),
                    mode="STRICT",
                    red_line_pct=15.0,
                    overage_possible=False,
                )
            },
        ),
    )


def _seed_tipped_pool(db: Database, *, charges: int = 10) -> None:
    """Owner observation at 100 %, then ten 8 % synthetic charges after it.

    Nine settled charges leave 28 %; the next dispatch's own 8 % projected
    cost is what crosses the 15 % line, which is exactly the report's shape.
    """
    observed_at = _NOW - timedelta(minutes=5)
    record_owner_quota(
        db,
        "codex",
        "chatgpt-plus",
        100.0,
        (_NOW + timedelta(hours=3)).isoformat(),
        source="OWNER",
        now=observed_at,
    )
    with owner_connection(db) as conn:
        for index in range(charges):
            conn.execute(
                "INSERT INTO capacity_reservations (id, run_id, job_id, route,"
                " pool_id, window_id, multiplier, size_bucket, cost_pct, spread_pct,"
                " state, total_tokens, created_at, settled_at) VALUES"
                " (?, ?, ?, 'codex/gpt-5', 'chatgpt-plus', '5h', 1.0, 'M',"
                " 8.0, 4.0, 'SETTLED', NULL, ?, ?)",
                (
                    f"res_n7_{index}",
                    f"run_n7_{index}",
                    f"job_n7_{index}",
                    (_NOW - timedelta(minutes=4, seconds=index)).isoformat(),
                    (_NOW - timedelta(minutes=4, seconds=index)).isoformat(),
                ),
            )
        conn.commit()


def _decide(db: Database, policy: ControlPlanePolicy) -> Any:
    from saberops.capacity.snapshot import effective_quota_states

    return control_plane_decision(
        candidate=WorkerCandidate("codex", "gpt-5"),
        adapter=_Adapter(),  # type: ignore[arg-type]
        training_allowed=True,
        quota_states=effective_quota_states(db, now=_NOW),
        prompt_bytes=100,
        policy=policy,
        db=db,
        now=_NOW,
    )


def test_red_line_tipped_by_charges_exposes_ledger_and_offers_probe(
    tmp_path: Path,
) -> None:
    """The refusal says 100 % because it omits the charges; N7 exposes them."""
    db = Database(tmp_path / "n7.db")
    policy = _policy()
    _seed_tipped_pool(db)

    decision = _decide(db, policy)

    assert decision.eligible is False
    assert decision.exclusion_reason == "RED_LINE_5H"
    assert decision.capacity is not None
    assert decision.capacity["observed_remaining_pct"] == pytest.approx(100.0)
    assert decision.capacity["prior_charges_pct"] == pytest.approx(80.0)
    assert decision.capacity["remaining_after_charges_pct"] == pytest.approx(20.0)
    assert decision.capacity["projected_cost_pct"] == pytest.approx(8.0)
    assert decision.capacity["reserve_pct"] == pytest.approx(0.0)
    assert decision.capacity["needs_probe"] is True
    payload = decision.as_payload()
    assert payload["capacity"] == decision.capacity


def test_fresh_probe_clears_charge_only_red_line(tmp_path: Path) -> None:
    """A fresh 100 % reading resets the post-observation charges and admits."""
    db = Database(tmp_path / "n7.db")
    policy = _policy()
    _seed_tipped_pool(db)
    refused = _decide(db, policy)
    assert refused.eligible is False

    outcome = refresh_quota_evidence(
        db,
        provider="codex",
        probes={"codex": _Probe(100.0)},
        policy=policy,
        now=_NOW,
    )
    assert outcome == "ran"

    admitted = _decide(db, policy)
    assert admitted.eligible is True
    assert admitted.capacity is not None
    assert admitted.capacity["prior_charges_pct"] == pytest.approx(0.0)
    assert admitted.capacity["remaining_after_charges_pct"] == pytest.approx(
        100.0
    )
    assert admitted.capacity["projected_cost_pct"] == pytest.approx(8.0)


def test_fresh_low_probe_keeps_the_red_line_refusal(tmp_path: Path) -> None:
    """Fresh evidence below the line still refuses: the fix is not a bypass."""
    db = Database(tmp_path / "n7.db")
    policy = _policy()
    _seed_tipped_pool(db)

    outcome = refresh_quota_evidence(
        db,
        provider="codex",
        probes={"codex": _Probe(5.0)},
        policy=policy,
        now=_NOW,
    )
    assert outcome == "ran"

    refused = _decide(db, policy)
    assert refused.eligible is False
    assert refused.exclusion_reason == "RED_LINE_5H"
    # A genuinely low fresh reading is refused by the quota authority itself,
    # before the charge ledger is consulted, so no probe is requested.
    assert refused.capacity is None
    assert refused.as_payload()["quota"]["normalized_remaining_pct"] == pytest.approx(
        5.0
    )


def test_no_probe_keeps_conservative_refusal(tmp_path: Path) -> None:
    """Without an available probe the original refusal stands unchanged."""
    db = Database(tmp_path / "n7.db")
    policy = _policy()
    _seed_tipped_pool(db)
    _decide(db, policy)

    outcome = refresh_quota_evidence(
        db, provider="codex", probes={}, policy=policy, now=_NOW
    )
    assert outcome == "disabled-under-test"

    still_refused = _decide(db, policy)
    assert still_refused.eligible is False
    assert still_refused.exclusion_reason == "RED_LINE_5H"
    assert still_refused.capacity is not None
    assert still_refused.capacity["prior_charges_pct"] == pytest.approx(80.0)


def test_tipped_red_line_probe_payload_is_durable_evidence(tmp_path: Path) -> None:
    """N7-observability: the pre-probe charge terms survive the refresh."""
    from saberops.dispatch import capacity_probe_event_payload

    db = Database(tmp_path / "n7.db")
    policy = _policy()
    _seed_tipped_pool(db)
    refused = _decide(db, policy)

    payload = capacity_probe_event_payload(refused, "ran")

    assert payload["provider"] == "codex"
    assert payload["model"] == "gpt-5"
    assert payload["reason"] == "RED_LINE_5H"
    assert payload["probe_outcome"] == "ran"
    capacity = payload["capacity"]
    assert isinstance(capacity, dict)
    assert capacity["prior_charges_pct"] == pytest.approx(80.0)
    assert capacity["remaining_after_charges_pct"] == pytest.approx(20.0)
    assert capacity["projected_cost_pct"] == pytest.approx(8.0)
    assert capacity["red_line_pct"] == pytest.approx(15.0)
    assert payload["quota"]["pool_id"] == "chatgpt-plus"


def test_stale_evidence_probe_payload_names_the_exclusion() -> None:
    """Item 5: a stale-evidence probe is named instead of staying invisible."""
    from saberops.dispatch import capacity_probe_event_payload

    class _Decision:
        exclusion_reason = "QUOTA_EVIDENCE_REQUIRED"
        capacity = {"needs_probe": True}

        def as_payload(self) -> dict[str, Any]:
            return {
                "provider": "codex",
                "model": "gpt-5",
                "reason": self.exclusion_reason,
                "capacity": self.capacity,
            }

    payload = capacity_probe_event_payload(_Decision(), "no-executable")
    assert payload["evidence_state"] == "stale"
    assert payload["probe_outcome"] == "no-executable"
    assert payload["reason"] == "QUOTA_EVIDENCE_REQUIRED"
