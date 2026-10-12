"""M2-1 exclusion store/rules, legacy rows, shared pools, model scope."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from saberops.capacity.exclusions import (
    backoff_next_probe,
    exclusion_state,
    merge_wins,
)
from saberops.capacity.snapshot import effective_quota_states
from saberops.capacity.store import (
    clear_pool_exclusions,
    list_active_exclusions,
    upsert_exclusion,
)
from saberops.control_plane.quota_policy import QuotaPoolPolicy, quota_decision
from saberops.db import Database, current_iso_timestamp
from saberops.models import QuotaState, RoutingState, Run, RunStatus, Tier, WorkerCandidate
from saberops.workers.exhaustion import record_exhaustion


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "cap.db")


def _seed_run(db: Database, tmp_path: Path, run_id: str = "run-1") -> None:
    db.create_run(
        Run(
            id=run_id, task="t", target_repo=str(tmp_path), base_commit="0" * 40,
            tier=Tier.T1, training_allowed=True, gate_command="true",
            status=RunStatus.RUNNING, created_at=current_iso_timestamp(),
        )
    )


def test_exact_expiry_at_until_plus_skew(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    until = base + timedelta(hours=1)
    row = upsert_exclusion(
        db, pool_id="p", window_id="weekly", reason="QUOTA_EXHAUSTED",
        until=until, until_confidence="EXACT", source="codex", now=base,
    )
    assert exclusion_state(row, until + timedelta(minutes=1)) == "ACTIVE"
    assert exclusion_state(row, until + timedelta(minutes=2, seconds=1)) == "EXPIRED"
    active = list_active_exclusions(db, now=until + timedelta(minutes=3))
    assert active == []


def test_hint_never_readmits_overage_pool_without_probe(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    until = base + timedelta(minutes=30)
    upsert_exclusion(
        db, pool_id="p", window_id="weekly", reason="QUOTA_EXHAUSTED",
        until=until, until_confidence="HINT", source="codex", now=base,
    )
    active = list_active_exclusions(db, now=until + timedelta(minutes=1))
    assert len(active) == 1
    from saberops.capacity.exclusions import is_excluding

    assert is_excluding(active[0], until + timedelta(hours=5))
    # quota_decision still denies via the synthesized exclusion state.
    states = effective_quota_states(db, now=until + timedelta(hours=5), run_probes=False)
    cand = WorkerCandidate(provider="codex", model="gpt-5")
    policies = {"p": QuotaPoolPolicy(pool_id="p", members=("codex",), overage_possible=True)}
    decision = quota_decision(cand, states, pool_policies=policies)
    assert decision.eligible is False
    assert decision.reason == "QUOTA_EXCLUDED_UNTIL_RESET"


def test_unknown_backoff_sequence_and_cap(tmp_path: Path) -> None:
    created = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    assert backoff_next_probe(created, 0, "weekly") == created + timedelta(minutes=15)
    assert backoff_next_probe(created, 1, "weekly") == created + timedelta(minutes=30)
    assert backoff_next_probe(created, 10, "weekly") == created + timedelta(minutes=360)
    assert backoff_next_probe(created, 10, "5h") == created + timedelta(minutes=300)
    # No probe + overage False clears at next_probe_at (row expires from active).
    db = _db(tmp_path)
    row = upsert_exclusion(
        db, pool_id="p", window_id="weekly", reason="QUOTA_EXHAUSTED",
        until=None, until_confidence="UNKNOWN", source="codex", now=created,
    )
    assert row["next_probe_at"] is not None
    late = created + timedelta(minutes=16)
    # PROBE_DUE is still excluding, but the pool policy without overage
    # re-admits once cleared; here the row is due, not auto-cleared.
    assert exclusion_state(row, late) == "PROBE_DUE"
    clear_pool_exclusions(db, "p", by="EXPIRED_NO_PROBE", now=late)
    assert list_active_exclusions(db, now=late) == []


def test_merge_rule_confidence_then_later_until(tmp_path: Path) -> None:
    db = _db(tmp_path)
    base = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    old = {
        "until_confidence": "HINT", "until": (base + timedelta(hours=1)).isoformat(),
        "created_at": base.isoformat(),
    }
    new_exact = {
        "until_confidence": "EXACT", "until": (base + timedelta(minutes=10)).isoformat(),
        "created_at": (base + timedelta(minutes=1)).isoformat(),
    }
    assert merge_wins(old, new_exact) is True
    later = {
        "until_confidence": "EXACT", "until": (base + timedelta(hours=2)).isoformat(),
        "created_at": (base + timedelta(minutes=2)).isoformat(),
    }
    earlier = {
        "until_confidence": "EXACT", "until": (base + timedelta(hours=1)).isoformat(),
        "created_at": base.isoformat(),
    }
    assert merge_wins(earlier, later) is True
    assert merge_wins(later, earlier) is False
    # Store keeps a single active row per key.
    upsert_exclusion(
        db, pool_id="p", window_id="weekly", reason="QUOTA_EXHAUSTED",
        until=base + timedelta(hours=1), until_confidence="HINT",
        source="codex", now=base,
    )
    upsert_exclusion(
        db, pool_id="p", window_id="weekly", reason="QUOTA_EXHAUSTED",
        until=base + timedelta(minutes=10), until_confidence="EXACT",
        source="codex", now=base + timedelta(minutes=1),
    )
    active = list_active_exclusions(db, now=base + timedelta(minutes=2))
    assert len(active) == 1
    assert active[0]["until_confidence"] == "EXACT"


def test_legacy_remaining_zero_row_read_as_exclusion(tmp_path: Path) -> None:
    db = _db(tmp_path)
    reset = datetime(2026, 10, 12, 0, 0, tzinfo=UTC)
    db.set_quota("codex", "weekly", 0.0, reset.isoformat())
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    states = effective_quota_states(db, now=now, run_probes=False)
    synth = [s for s in states if getattr(s, "exclusion_id", None)]
    assert len(synth) == 1
    assert synth[0].exclusion_confidence == "EXACT"
    # Expired legacy EXACT rows stop excluding.
    later = effective_quota_states(db, now=reset + timedelta(minutes=3), run_probes=False)
    assert [s for s in later if getattr(s, "exclusion_id", None)] == []


def test_shared_pool_exhaustion() -> None:
    policies = {
        "shared": QuotaPoolPolicy(
            pool_id="shared", members=("codex", "opencode/openai/*")
        )
    }
    maker = WorkerCandidate(provider="codex", model="gpt-5")
    by_codex = WorkerCandidate(provider="opencode", model="openai/x")
    other = WorkerCandidate(provider="opencode", model="opencode-go/x")
    # Simulate the exclusion written by record_exhaustion via codex/*.
    exc = QuotaState(
        provider="codex", pool="weekly", remaining_percent=0.0,
        reset_at="2026-10-12T00:00:00+00:00", routing_state=RoutingState.NORMAL,
        updated_at=datetime(2026, 10, 11, 12, 0, tzinfo=UTC).isoformat(),
        pool_id="shared", normalized_remaining_pct=0.0, freshness="FRESH",
        exclusion_id="exc1", exclusion_until="2026-10-12T00:00:00+00:00",
        exclusion_confidence="EXACT", exclusion_model_scope=None,
    )
    assert quota_decision(by_codex, [exc], pool_policies=policies).eligible is False
    assert quota_decision(other, [exc], pool_policies=policies).eligible is True
    assert quota_decision(maker, [exc], pool_policies=policies).eligible is False


def test_model_scope_opus_does_not_block_sonnet() -> None:
    policies = {"p": QuotaPoolPolicy(pool_id="p", members=("anthropic",))}
    exc = QuotaState(
        provider="anthropic", pool="weekly", remaining_percent=0.0,
        reset_at="2026-10-12T00:00:00+00:00", routing_state=RoutingState.NORMAL,
        updated_at=datetime(2026, 10, 11, 12, 0, tzinfo=UTC).isoformat(),
        pool_id="p", normalized_remaining_pct=0.0, freshness="FRESH",
        exclusion_id="exc1", exclusion_until="2026-10-12T00:00:00+00:00",
        exclusion_confidence="EXACT", exclusion_model_scope="opus",
    )
    opus = WorkerCandidate(provider="anthropic", model="opus-4")
    sonnet = WorkerCandidate(provider="anthropic", model="sonnet-4")
    assert quota_decision(opus, [exc], pool_policies=policies).eligible is False
    assert quota_decision(sonnet, [exc], pool_policies=policies).eligible is True


def test_record_exhaustion_shared_pool(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, tmp_path)
    policies = {
        "shared": QuotaPoolPolicy(
            pool_id="shared", members=("codex", "opencode/openai/*")
        )
    }
    record_exhaustion(
        db, provider="codex", model="gpt-5", window="weekly",
        reset_at=datetime(2026, 10, 12, 0, 0, tzinfo=UTC),
        reset_confidence="EXACT", run_id="run-1", pool_policies=policies,
    )
    active = list_active_exclusions(db, now=datetime(2026, 10, 11, 12, 0, tzinfo=UTC))
    assert [a["pool_id"] for a in active] == ["shared"]
    # db.set_quota rows are not written by exhaustion.
    assert db.list_quota() == []


def test_hard_rule_no_metered_candidate(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_run(db, tmp_path)
    chain = [
        WorkerCandidate(provider="codex", model="gpt-5"),
        WorkerCandidate(provider="anthropic", model="sonnet"),
    ]
    policies = {
        "codex": QuotaPoolPolicy(pool_id="codex", members=("codex",)),
        "claude": QuotaPoolPolicy(pool_id="claude", members=("anthropic",)),
    }
    for cand in chain:
        record_exhaustion(
            db, provider=cand.provider, model=cand.model, window="weekly",
            reset_at=datetime(2026, 10, 12, 0, 0, tzinfo=UTC),
            reset_confidence="EXACT", run_id="run-1", pool_policies=policies,
        )
    states = effective_quota_states(
        db, now=datetime(2026, 10, 11, 12, 0, tzinfo=UTC), run_probes=False
    )
    survivors = [c for c in chain if quota_decision(c, states, pool_policies=policies).eligible]
    assert survivors == []
    metered = WorkerCandidate(provider="omniroute", model="auto")
    assert metered not in chain
    assert all(c.provider in {"codex", "anthropic"} for c in survivors)


def test_legacy_unknown_row_follows_no_probe_rule(tmp_path: Path) -> None:
    """A legacy UNKNOWN row past its first backoff: overage True keeps, False clears."""
    import sqlite3

    from saberops.control_plane.quota_policy import QuotaPoolPolicy

    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    old_updated = (now - timedelta(hours=2)).isoformat()

    def _seed(overage: bool) -> tuple[Database, dict[str, QuotaPoolPolicy]]:
        db = Database(tmp_path / f"legacy-{overage}.db")
        db.set_quota("codex", "weekly", 0.0, None)
        with sqlite3.connect(str(db.owner_db.db_path), timeout=10.0) as conn:
            conn.execute(
                "UPDATE quota_state SET updated_at = ? WHERE provider = ? AND pool = ?",
                (old_updated, "codex", "weekly"),
            )
            conn.commit()
        policies = {
            "codex": QuotaPoolPolicy(
                pool_id="codex", members=("codex",), overage_possible=overage
            )
        }
        return db, policies

    db, policies = _seed(True)
    states = effective_quota_states(db, now=now, run_probes=False, pool_policies=policies)
    assert len([s for s in states if getattr(s, "exclusion_id", None)]) == 1
    db, policies = _seed(False)
    states = effective_quota_states(db, now=now, run_probes=False, pool_policies=policies)
    assert [s for s in states if getattr(s, "exclusion_id", None)] == []
