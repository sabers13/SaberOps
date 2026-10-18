"""Adversarial tests for the host-wide adaptive storage budget.

Covers: ADR-0003 Amendment 2 (concurrent-run adaptive storage admission).

Every test here is hermetic: a fake filesystem probe supplies capacities, no
large file is written, no provider/model is reachable, and no file is deleted.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

import saberops.storage_budget as sb
from saberops import paths as saberops_paths
from saberops.ownership import Liveness, ProcessIdentity, current_boot_id
from saberops.ownership import probe_liveness as owner_probe_liveness
from saberops.storage_budget import (
    AdmissionOutcome,
    Attribution,
    AttributionUnavailableError,
    EstimateComponent,
    EstimateSource,
    EstimateUnavailableError,
    FilesystemIdentity,
    FilesystemIdentityError,
    LedgerConfig,
    LedgerCorruptError,
    LedgerSecurityError,
    MarginPolicy,
    OwnerToken,
    PolicyUnavailableError,
    ReservationKey,
    StorageEstimate,
    StorageLedger,
    compute_required_free,
    load_margin_policy,
    real_filesystem_identity,
)

GB = 1024**3
MIB = 1024**2


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


class FakeFilesystem:
    """A deterministic filesystem probe keyed by real path prefix.

    Aliases that resolve to the same real directory report the same identity,
    and distinct roots can be placed on distinct fake devices.
    """

    def __init__(self, mapping: dict[Path, tuple[str, int, int]]) -> None:
        self._mapping = [
            (os.path.realpath(str(root)), key, total, free)
            for root, (key, total, free) in mapping.items()
        ]

    def __call__(self, path: Path) -> FilesystemIdentity:
        real = os.path.realpath(str(path))
        for root, key, total, free in self._mapping:
            if real == root or real.startswith(root + os.sep):
                return FilesystemIdentity(
                    key=key,
                    device=abs(hash(key)) % 4096,
                    total_bytes=total,
                    free_bytes=free,
                )
        raise FilesystemIdentityError(f"unknown path {path}")


def write_policy(tmp_path: Path, **overrides: object) -> Path:
    payload: dict[str, object] = {
        "approved": True,
        "fixed_bytes": GB,
        "min_bytes": 64 * MIB,
        "max_bytes": 2 * GB,
        "source": "test-owner",
        "version": "1",
    }
    payload.update(overrides)
    path = tmp_path / "policy.json"
    path.write_text(json.dumps(payload))
    return path


def make_config(tmp_path: Path, *, lease_seconds: float = 900.0, **policy: object) -> LedgerConfig:
    return LedgerConfig(
        root=tmp_path / "ledger",
        policy_path=write_policy(tmp_path, **policy),
        allow_isolated=True,
        lease_seconds=lease_seconds,
        lock_timeout_seconds=5.0,
    )


def make_key(
    session: str = "s1",
    attempt: str = "a1",
    phase: str = "gate",
    installation: str = "proj",
) -> ReservationKey:
    return ReservationKey(
        installation_id=installation,
        session_id=session,
        attempt_id=attempt,
        phase_id=phase,
    )


def make_estimate(
    path: Path,
    amount: int = GB,
    *,
    uncertainty: int = 0,
    source: EstimateSource = EstimateSource.OWNER_HINT,
    sample_count: int = 0,
    confidence: float = 0.5,
    attribution: Attribution = Attribution.OWNED,
    estimate_version: int = 1,
    conservative_fallback: bool = False,
) -> StorageEstimate:
    return StorageEstimate(
        components=(
            EstimateComponent(
                path=str(path),
                remaining_growth_bytes=amount,
                uncertainty_bytes=uncertainty,
            ),
        ),
        source=source,
        sample_count=sample_count,
        confidence=confidence,
        attribution=attribution,
        estimate_version=estimate_version,
        conservative_fallback=conservative_fallback,
    )


def dead_identity() -> ProcessIdentity:
    """A fabricated identity that is provably dead on this host."""
    return ProcessIdentity(
        host=socket.gethostname(),
        boot_id=current_boot_id(),
        pid=2_147_483_646,
        start_ticks=1,
        session_id=1,
        process_group_id=1,
    )


def tree_snapshot(root: Path) -> set[str]:
    return {str(path.relative_to(root)) for path in root.rglob("*")}


# --------------------------------------------------------------------------
# arithmetic that four sessions must add up
# --------------------------------------------------------------------------


def test_four_sessions_sum_independently(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (four estimates are added once, not N-way)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        totals: list[int] = []
        for index in range(4):
            decision = ledger.try_reserve(
                make_key(session=f"s{index}"), OwnerToken(f"o{index}", 1),
                make_estimate(tmp_path),
            )
            assert decision.outcome is AdmissionOutcome.ADMITTED, decision.reason
            assert decision.reserved
            assert decision.total_active_remaining is not None
            totals.append(decision.total_active_remaining)
        assert totals == [0, GB, 2 * GB, 3 * GB]
        final = ledger.assess(make_estimate(tmp_path))
        assert final.total_active_remaining == 4 * GB
        assert final.candidate_bound == GB
        assert final.emergency_margin == GB
        assert final.free_bytes == 8 * GB
        assert final.required_free == 6 * GB
        assert final.available_for_new_admissions == 3 * GB
        # 8 GiB free, 1 GiB margin, 4 GiB already admitted: only 3 GiB left.
        over = ledger.try_reserve(
            make_key(session="s9"), OwnerToken("o9", 1), make_estimate(tmp_path, 4 * GB)
        )
        assert over.outcome is AdmissionOutcome.EXCEEDED
        assert over.reason_code == sb.REASON_EXCEEDED
        assert not over.reserved
        assert len(ledger.active_reservations()) == 4
        sessions = [c.reservation.key.session_id for c in final.filesystems[0].contributions]
        assert sessions == ["s0", "s1", "s2", "s3"]
        assert [c.remaining_growth_bytes for c in final.filesystems[0].contributions] == [
            GB, GB, GB, GB
        ]


def test_candidate_joining_three_live_sessions_makes_four_reservations(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (one reservation per live session plus one)."""
    fs = FakeFilesystem({tmp_path: ("fs", 32 * GB, 16 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        for index in range(3):
            assert ledger.try_reserve(
                make_key(session=f"live{index}"), OwnerToken(f"o{index}", 1),
                make_estimate(tmp_path, 2 * GB),
            ).admitted
        before = ledger.assess(make_estimate(tmp_path, 2 * GB))
        assert before.total_active_remaining == 6 * GB
        assert ledger.try_reserve(
            make_key(session="candidate"), OwnerToken("oc", 1), make_estimate(tmp_path, 2 * GB)
        ).admitted
        assert len(ledger.active_reservations()) == 4
        after = ledger.assess(make_estimate(tmp_path, 2 * GB))
        assert after.total_active_remaining == 8 * GB


@pytest.mark.parametrize("sessions", [1, 2, 4, 15])
def test_one_two_four_fifteen_sessions_across_projects(tmp_path: Path, sessions: int) -> None:
    """Covers: ADR-0003 Amendment 2 (1/2/4/15 sessions and multiple projects)."""
    fs = FakeFilesystem({tmp_path: ("fs", 64 * GB, 32 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        for index in range(sessions):
            key = make_key(session=f"s{index}", installation=f"proj{index % 3}")
            assert ledger.try_reserve(
                key, OwnerToken(f"o{index}", 1), make_estimate(tmp_path, GB)
            ).admitted
        report = ledger.assess(make_estimate(tmp_path, GB))
        assert report.total_active_remaining == sessions * GB
        installations = {r.key.installation_id for r in ledger.active_reservations()}
        assert installations == {f"proj{index % 3}" for index in range(sessions)}


def test_shared_parent_wave_budget_is_counted_once(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (a common parent wave is not counted four times)."""
    fs = FakeFilesystem({tmp_path: ("fs", 32 * GB, 16 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        for index in range(4):
            assert ledger.try_reserve(
                make_key(session=f"m{index}"), OwnerToken(f"o{index}", 1),
                make_estimate(tmp_path, GB),
            ).admitted
        shared = make_key(session="wave-42", attempt="attempt-1", phase="shared-gate")
        owner = OwnerToken("orchestrator", 1)
        for _ in range(4):
            assert ledger.try_reserve(shared, owner, make_estimate(tmp_path, 2 * GB)).admitted
        assert len(ledger.active_reservations()) == 5
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 6 * GB


# --------------------------------------------------------------------------
# filesystem identity
# --------------------------------------------------------------------------


def test_same_filesystem_path_alias_counts_once(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (same-filesystem alias must not double count)."""
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "alias"
    link.symlink_to(real)
    fs = FakeFilesystem({real: ("fs", 16 * GB, 8 * GB)})
    estimate = StorageEstimate(
        components=(
            EstimateComponent(path=str(real), remaining_growth_bytes=2 * GB),
            EstimateComponent(path=str(link), remaining_growth_bytes=2 * GB),
            EstimateComponent(path=str(real / "."), remaining_growth_bytes=GB),
        ),
        source=EstimateSource.OWNER_HINT,
        sample_count=0,
        confidence=0.5,
    )
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        decision = ledger.try_reserve(make_key(), OwnerToken("o", 1), estimate)
        assert decision.outcome is AdmissionOutcome.ADMITTED
        assert decision.candidate_bound == 2 * GB  # alias and "." collapse onto one path


def test_distinct_paths_on_one_filesystem_add_up(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (distinct growth locations on one device sum)."""
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    estimate = StorageEstimate(
        components=(
            EstimateComponent(path=str(first), remaining_growth_bytes=GB),
            EstimateComponent(path=str(second), remaining_growth_bytes=2 * GB),
        ),
        source=EstimateSource.OWNER_HINT,
        sample_count=0,
        confidence=0.5,
    )
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        decision = ledger.try_reserve(make_key(), OwnerToken("o", 1), estimate)
        assert decision.candidate_bound == 3 * GB


def test_different_filesystems_are_independent_and_ordered(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (distinct devices independent, deterministic order)."""
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    home.mkdir()
    scratch.mkdir()
    fs = FakeFilesystem({
        home: ("fs-b", 16 * GB, 4 * GB),
        scratch: ("fs-a", 128 * GB, 64 * GB),
    })
    estimate = StorageEstimate(
        components=(
            EstimateComponent(path=str(scratch), remaining_growth_bytes=8 * GB),
            EstimateComponent(path=str(home), remaining_growth_bytes=4 * GB),
        ),
        source=EstimateSource.OWNER_HINT,
        sample_count=0,
        confidence=0.5,
    )
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        decision = ledger.try_reserve(make_key(), OwnerToken("o", 1), estimate)
        # home cannot cover 4 GiB against 4 GiB free minus the 1 GiB margin.
        assert decision.outcome is AdmissionOutcome.EXCEEDED
        assert [b.fs_key for b in decision.filesystems] == ["fs-a", "fs-b"]
        assert decision.primary is not None and decision.primary.fs_key == "fs-b"
        assert not decision.reserved
        assert ledger.active_reservations() == ()
        # Admission is all-or-nothing: nothing was left on the roomy device.
        assert decision.filesystems[0].fits and not decision.filesystems[1].fits


def test_multi_filesystem_order_is_declaration_independent(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (deterministic multi-filesystem lock order)."""
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    home.mkdir()
    scratch.mkdir()
    fs = FakeFilesystem({
        home: ("fs-z-home", 32 * GB, 16 * GB),
        scratch: ("fs-a-scratch", 32 * GB, 16 * GB),
    })
    components = (
        EstimateComponent(path=str(scratch), remaining_growth_bytes=GB),
        EstimateComponent(path=str(home), remaining_growth_bytes=2 * GB),
    )
    reversed_components = tuple(reversed(components))
    reports = []
    for index, ordered in enumerate((components, reversed_components)):
        estimate = StorageEstimate(
            components=ordered,
            source=EstimateSource.OWNER_HINT,
            sample_count=0,
            confidence=0.5,
        )
        with StorageLedger(make_config(tmp_path), fs) as ledger:
            decision = ledger.try_reserve(
                make_key(session=f"order{index}"), OwnerToken("o", 1), estimate
            )
            assert decision.admitted
            reports.append(
                [(b.fs_key, b.candidate_bound, b.free_bytes) for b in decision.filesystems]
            )
    assert reports[0] == reports[1]
    assert [row[0] for row in reports[0]] == ["fs-a-scratch", "fs-z-home"]


def test_missing_filesystem_fails_closed(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (misidentified filesystem fails closed)."""
    watched = tmp_path / "watched"
    watched.mkdir()
    fs = FakeFilesystem({watched: ("fs", 16 * GB, 8 * GB)})
    elsewhere = tmp_path / "not-watched"
    elsewhere.mkdir()
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        decision = ledger.try_reserve(make_key(), OwnerToken("o", 1), make_estimate(elsewhere))
        assert decision.outcome is AdmissionOutcome.UNAVAILABLE
        assert decision.reason_code == FilesystemIdentityError.reason_code
        assert ledger.active_reservations() == ()
    with pytest.raises(FilesystemIdentityError):
        real_filesystem_identity(tmp_path / "absent")


def test_real_filesystem_identity_measures_this_host(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (mount/device identity, not just a path)."""
    identity = real_filesystem_identity(tmp_path)
    assert identity.key.startswith(socket.gethostname())
    assert identity.total_bytes > 0
    assert identity.free_bytes >= 0
    assert real_filesystem_identity(tmp_path / ".").key == identity.key
    a_file = tmp_path / "a-file"
    a_file.write_text("not a directory")
    with pytest.raises(FilesystemIdentityError):
        real_filesystem_identity(a_file)


# --------------------------------------------------------------------------
# lifecycle and lifecycle-adjacent accounting
# --------------------------------------------------------------------------


def test_active_vs_waiting_jobs_and_release_frees_capacity(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (a waiting job holds no storage liability)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 10 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        keys = [make_key(session=f"s{index}") for index in range(4)]
        for index, key in enumerate(keys):
            assert ledger.try_reserve(
                key, OwnerToken(f"o{index}", 1), make_estimate(tmp_path, 2 * GB)
            ).admitted
        blocked = ledger.try_reserve(
            make_key(session="waiting"), OwnerToken("w", 1), make_estimate(tmp_path, 2 * GB)
        )
        assert blocked.outcome is AdmissionOutcome.EXCEEDED
        # A waiting job never entered the ledger.
        assert len(ledger.active_reservations()) == 4
        released = ledger.release(keys[0], OwnerToken("o0", 1), detail="superseded")
        assert released.outcome is AdmissionOutcome.ADMITTED
        assert released.reserved is False
        now_admitted = ledger.try_reserve(
            make_key(session="waiting"), OwnerToken("w", 1), make_estimate(tmp_path, 2 * GB)
        )
        assert now_admitted.admitted


def test_retained_preempted_wip_keeps_a_reduced_liability(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (preempted WIP keeps liability, no executing slot)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        owner = OwnerToken("preempted", 1)
        key = make_key(session="p1", phase="worktree")
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, 4 * GB)).admitted
        # Preemption counts only the measured WIP checkpoint, not the peak plan.
        renewed = ledger.renew(key, owner, make_estimate(tmp_path, 512 * MIB))
        assert renewed.admitted
        reservations = ledger.active_reservations()
        assert len(reservations) == 1
        assert reservations[0].filesystems[0].bound_bytes == 512 * MIB
        report = ledger.assess(make_estimate(tmp_path, GB))
        assert report.total_active_remaining == 512 * MIB


def test_duplicate_and_changed_session_key(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (idempotent key, stale generation fenced)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        key = make_key()
        owner = OwnerToken("owner", 1)
        first = ledger.try_reserve(key, owner, make_estimate(tmp_path, 2 * GB))
        duplicate = ledger.try_reserve(key, owner, make_estimate(tmp_path, 2 * GB))
        assert first.admitted and duplicate.admitted
        assert len(ledger.active_reservations()) == 1
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB
        lowered = ledger.try_reserve(key, owner, make_estimate(tmp_path, GB))
        assert lowered.admitted  # same incarnation: a lower re-declaration is safe
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == GB
        replayed = ledger.try_reserve(key, OwnerToken("other", 1), make_estimate(tmp_path, GB))
        assert replayed.outcome is AdmissionOutcome.UNAVAILABLE
        assert replayed.reason_code == sb.FencingError.reason_code
        assert len(ledger.active_reservations()) == 1


def test_killed_before_and_after_reserve_replay(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (crash before/after booking never double books)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        key = make_key()
        assert ledger.try_reserve(
            key, OwnerToken("first", 1, dead_identity()), make_estimate(tmp_path, 2 * GB), now=base
        ).admitted
        # Crash AFTER the durable commit and before the child started: the next
        # attempt adopts the same row once the dead owner's lease has lapsed.
        adopted = ledger.try_reserve(
            key,
            OwnerToken("second", 2, ProcessIdentity.for_self()),
            make_estimate(tmp_path, 2 * GB),
            now=base + timedelta(seconds=120),
        )
        assert adopted.admitted
        assert len(ledger.active_reservations()) == 1
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB
        # A live predecessor is never replaced, even with a newer generation.
        refused = ledger.try_reserve(
            key,
            OwnerToken("third", 3),
            make_estimate(tmp_path, 2 * GB),
            now=base + timedelta(seconds=180),
        )
        assert refused.outcome is AdmissionOutcome.UNAVAILABLE
        assert refused.reason_code == sb.FencingError.reason_code
        assert len(ledger.active_reservations()) == 1


def test_tight_headroom_replay_does_not_double_count(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (a replay never charges the row to itself)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 4 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        key = make_key()
        first = ledger.try_reserve(
            key, OwnerToken("first", 1, dead_identity()), make_estimate(tmp_path, 3 * GB),
            now=base,
        )
        # 4 GiB free, 1 GiB margin, 3 GiB bound: exactly the whole headroom.
        assert first.admitted
        assert first.required_free == 4 * GB
        # Crash AFTER commit: the replay must see 3 GiB (not 6) and still admit.
        replay = ledger.try_reserve(
            key, OwnerToken("second", 2, ProcessIdentity.for_self()),
            make_estimate(tmp_path, 3 * GB), now=base + timedelta(seconds=120),
        )
        assert replay.admitted, replay.reason
        assert len(ledger.active_reservations()) == 1
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 3 * GB
        # A genuine growth delta is still checked atomically: 4 GiB no longer fits.
        grown = ledger.try_reserve(
            key, OwnerToken("second", 2, ProcessIdentity.for_self()),
            make_estimate(tmp_path, 4 * GB), now=base + timedelta(seconds=120),
        )
        assert grown.outcome is AdmissionOutcome.EXCEEDED
        assert len(ledger.active_reservations()) == 1
        assert ledger.active_reservations()[0].filesystems[0].bound_bytes == 3 * GB


def test_idempotent_settle_and_release_refusal(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (settlement is idempotent and fenced)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        key = make_key()
        owner = OwnerToken("owner", 1)
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, 2 * GB)).admitted
        first = ledger.settle(key, owner)
        second = ledger.settle(key, owner)
        assert first.outcome is AdmissionOutcome.ADMITTED
        assert second.reason == "already SETTLED"
        assert ledger.active_reservations() == ()
        wrong = ledger.settle(key, OwnerToken("other", 2))
        assert wrong.outcome is AdmissionOutcome.UNAVAILABLE
        assert wrong.reason_code == sb.FencingError.reason_code
        reopened = ledger.try_reserve(key, OwnerToken("owner", 2), make_estimate(tmp_path, GB))
        assert reopened.admitted
        assert [event.event for event in ledger.history(key)] == [
            "RESERVED", "SETTLED", "RESERVED",
        ]
        assert ledger.release(key, OwnerToken("owner", 2)).outcome is AdmissionOutcome.ADMITTED
        assert ledger.active_reservations() == ()


def test_settle_requires_the_owner_generation(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (fenced single-writer settlement)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        key = make_key()
        ledger.try_reserve(key, OwnerToken("owner", 3), make_estimate(tmp_path, GB))
        stale = ledger.settle(key, OwnerToken("owner", 2))
        assert stale.reason_code == sb.FencingError.reason_code
        assert ledger.active_reservations() != ()


def test_settle_and_release_reject_a_foreign_incarnation(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (a copied token cannot close another incarnation)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        recorded = ProcessIdentity.for_self()
        # PID reuse: the same pid later, a different start time.  The pair
        # (owner_id, generation) is identical, so only the incarnation can
        # distinguish the stale process from the one that booked the row.
        forged = dataclasses.replace(recorded, start_ticks=(recorded.start_ticks or 0) + 1)
        assert forged != recorded

        settle_key = make_key(session="settle")
        owner = OwnerToken("owner", 1, recorded)
        assert ledger.try_reserve(settle_key, owner, make_estimate(tmp_path, 2 * GB)).admitted
        bogus = ledger.settle(settle_key, OwnerToken("owner", 1, forged))
        assert bogus.outcome is AdmissionOutcome.UNAVAILABLE
        assert bogus.reason_code == sb.FencingError.reason_code
        # The live row, its charge and its audit trail are untouched.
        assert [row.state for row in ledger.active_reservations()] == [
            sb.ReservationState.RESERVED
        ]
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB
        assert [event.event for event in ledger.history(settle_key)] == ["RESERVED"]

        release_key = make_key(session="release")
        assert ledger.try_reserve(
            release_key, OwnerToken("owner", 1, recorded), make_estimate(tmp_path, 2 * GB)
        ).admitted
        bogus_release = ledger.release(release_key, OwnerToken("owner", 1, forged))
        assert bogus_release.outcome is AdmissionOutcome.UNAVAILABLE
        assert bogus_release.reason_code == sb.FencingError.reason_code
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 4 * GB

        # The recorded incarnation closes normally; an idempotent replay and a
        # token that omits the identity both resolve to this same live process,
        # so they are the recorded owner rather than a delegated foreign caller.
        assert ledger.settle(settle_key, owner).reason == "SETTLED"
        assert ledger.settle(settle_key, owner).reason == "already SETTLED"
        assert (
            ledger.release(release_key, OwnerToken("owner", 1)).outcome
            is AdmissionOutcome.ADMITTED
        )
        assert ledger.active_reservations() == ()


def test_omitted_identity_resolves_to_the_actual_process(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (an omitted identity is the live caller).

    Policy correction (owner-approved): the former "no-identity delegation"
    doctrine is revoked.  A token that omits its identity is resolved to the
    live invoking process, so it is authorised exactly when this process *is*
    the recorded incarnation — never because omission grants another live
    process authority over the shared ``(owner_id, generation)`` pair.
    """
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        key = make_key()
        recorded = ProcessIdentity.for_self()
        # The owner records its own real incarnation explicitly.
        assert ledger.try_reserve(
            key, OwnerToken("owner", 1, recorded), make_estimate(tmp_path, 2 * GB)
        ).admitted
        # An omitted identity resolves to this live process, which is the
        # recorded incarnation; settlement and the in-process next generation
        # therefore keep working on actual-process identity, not delegation.
        assert OwnerToken("owner", 1).resolved_identity() == recorded
        assert ledger.settle(key, OwnerToken("owner", 1)).reason == "SETTLED"
        assert ledger.settle(key, OwnerToken("owner", 1)).reason == "already SETTLED"
        reopened = ledger.try_reserve(
            key, OwnerToken("owner", 2), make_estimate(tmp_path, 2 * GB)
        )
        assert reopened.admitted, reopened.reason
        assert [event.event for event in ledger.history(key)] == [
            "RESERVED", "SETTLED", "RESERVED",
        ]


def test_reopen_refuses_a_stale_generation_replay(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (no stale resurrection of a settled key)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        key = make_key()
        assert ledger.try_reserve(
            key, OwnerToken("owner", 3), make_estimate(tmp_path, 2 * GB)
        ).admitted
        assert ledger.settle(key, OwnerToken("owner", 3)).reason == "SETTLED"
        stale = ledger.try_reserve(key, OwnerToken("owner", 2), make_estimate(tmp_path, 2 * GB))
        assert stale.outcome is AdmissionOutcome.UNAVAILABLE
        assert stale.reason_code == sb.FencingError.reason_code
        assert ledger.active_reservations() == ()
        # The same generation cannot reopen either: monotonic strictly upward.
        duplicate = ledger.try_reserve(
            key, OwnerToken("owner", 3), make_estimate(tmp_path, 2 * GB)
        )
        assert duplicate.reason_code == sb.FencingError.reason_code
        assert ledger.active_reservations() == ()
        assert [event.event for event in ledger.history(key)] == ["RESERVED", "SETTLED"]


def test_reopen_refuses_a_foreign_owner_while_the_predecessor_lives(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0003 Amendment 2 (a live predecessor blocks foreign reopen)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        key = make_key()
        assert ledger.try_reserve(
            key, OwnerToken("owner", 1, ProcessIdentity.for_self()), make_estimate(tmp_path, GB)
        ).admitted
        assert ledger.settle(key, OwnerToken("owner", 1)).reason == "SETTLED"
        intruder = ledger.try_reserve(key, OwnerToken("intruder", 2), make_estimate(tmp_path, GB))
        assert intruder.outcome is AdmissionOutcome.UNAVAILABLE
        assert intruder.reason_code == sb.FencingError.reason_code
        assert ledger.active_reservations() == ()
        assert [event.event for event in ledger.history(key)] == ["RESERVED", "SETTLED"]


def test_reopen_by_an_authorised_successor_preserves_audit_timing(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (crash-after-settle reopen keeps created_at)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        key = make_key()
        assert ledger.try_reserve(
            key, OwnerToken("owner", 1, dead_identity()), make_estimate(tmp_path, 2 * GB),
            now=base,
        ).admitted
        created_at = ledger.active_reservations()[0].created_at
        # Policy correction: closing a row owned by the fabricated dead identity
        # now requires that recorded identity, not an omitted (delegated) one.
        assert (
            ledger.settle(key, OwnerToken("owner", 1, dead_identity()), now=base).reason
            == "SETTLED"
        )
        # Crash after settle: a dead predecessor with a lapsed lease is replaced
        # by a strictly newer generation, even from a different owner id.
        reopened = ledger.try_reserve(
            key, OwnerToken("recovery", 2, ProcessIdentity.for_self()),
            make_estimate(tmp_path, 2 * GB), now=base + timedelta(seconds=120),
        )
        assert reopened.admitted, reopened.reason
        rows = ledger.active_reservations()
        assert [row.owner_id for row in rows] == ["recovery"]
        assert rows[0].created_at == created_at  # original timing is preserved
        assert rows[0].owner_generation == 2
        assert [event.event for event in ledger.history(key)] == [
            "RESERVED", "SETTLED", "TAKEOVER", "RESERVED",
        ]
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB


def test_reopen_refuses_a_foreign_live_incarnation_sharing_the_owner_id(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0003 Amendment 2 (a live foreign incarnation cannot reopen)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    child = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        foreign: ProcessIdentity | None = None
        for _ in range(400):
            foreign = ProcessIdentity.for_pid(child.pid)
            if foreign is not None:
                break
            time.sleep(0.005)
        assert foreign is not None, "could not read the child process identity"
        assert owner_probe_liveness(foreign) is Liveness.LIVE
        recorded = ProcessIdentity.for_self()
        assert foreign != recorded
        with StorageLedger(make_config(tmp_path), fs) as ledger:
            key = make_key()
            assert ledger.try_reserve(
                key, OwnerToken("shared", 1, recorded), make_estimate(tmp_path, 2 * GB)
            ).admitted
            assert ledger.settle(key, OwnerToken("shared", 1)).reason == "SETTLED"
            # A different live process that copied the owner_id string and merely
            # raised the generation is not the recorded incarnation: the settled
            # key stays closed while its predecessor is provably alive.
            intruder = ledger.try_reserve(
                key, OwnerToken("shared", 2, foreign), make_estimate(tmp_path, 2 * GB)
            )
            assert intruder.outcome is AdmissionOutcome.UNAVAILABLE
            assert intruder.reason_code == sb.FencingError.reason_code
            assert ledger.active_reservations() == ()
            assert [event.event for event in ledger.history(key)] == ["RESERVED", "SETTLED"]
    finally:
        child.terminate()
        child.wait(timeout=10)


def test_reopen_by_a_distinct_incarnation_after_proven_death(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (recovery survives a legitimate identity change)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        key = make_key()
        assert ledger.try_reserve(
            key, OwnerToken("shared", 1, dead_identity()), make_estimate(tmp_path, 2 * GB),
            now=base,
        ).admitted
        created_at = ledger.active_reservations()[0].created_at
        # Policy correction: the recorded dead incarnation, not an omitted one,
        # must settle the row before the distinct successor takes over.
        assert (
            ledger.settle(key, OwnerToken("shared", 1, dead_identity()), now=base).reason
            == "SETTLED"
        )
        # The recorded predecessor is proven dead *and* its lease has lapsed, so
        # a distinct incarnation may take the next generation under the same
        # owner id: genuine crash recovery is not broken by the incarnation fence.
        successor = ProcessIdentity.for_self()
        assert successor != dead_identity()
        reopened = ledger.try_reserve(
            key, OwnerToken("shared", 2, successor), make_estimate(tmp_path, 2 * GB),
            now=base + timedelta(seconds=120),
        )
        assert reopened.admitted, reopened.reason
        rows = ledger.active_reservations()
        assert [row.owner_id for row in rows] == ["shared"]
        assert rows[0].owner_generation == 2
        assert rows[0].created_at == created_at
        assert [event.event for event in ledger.history(key)] == [
            "RESERVED", "SETTLED", "TAKEOVER", "RESERVED",
        ]
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB


def test_reopen_of_a_dead_predecessor_still_needs_a_lapsed_lease(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (proven death alone does not release the lease)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        key = make_key()
        assert ledger.try_reserve(
            key, OwnerToken("shared", 1, dead_identity()), make_estimate(tmp_path, 2 * GB),
            now=base,
        ).admitted
        # Policy correction: settle as the recorded dead incarnation; an omitted
        # identity is no longer cross-process authority.
        assert (
            ledger.settle(key, OwnerToken("shared", 1, dead_identity()), now=base).reason
            == "SETTLED"
        )
        early = ledger.try_reserve(
            key, OwnerToken("shared", 2, ProcessIdentity.for_self()),
            make_estimate(tmp_path, 2 * GB), now=base + timedelta(seconds=30),
        )
        assert early.outcome is AdmissionOutcome.UNAVAILABLE
        assert early.reason_code == sb.FencingError.reason_code
        assert ledger.active_reservations() == ()
        assert [event.event for event in ledger.history(key)] == ["RESERVED", "SETTLED"]


def test_omitted_identity_takeover_requires_proven_death_and_lapsed_lease(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0003 Amendment 2 (takeover: proven death + lapsed lease + new gen)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        key = make_key()
        assert ledger.try_reserve(
            key, OwnerToken("shared", 1, dead_identity()), make_estimate(tmp_path, 2 * GB),
            now=base,
        ).admitted
        created_at = ledger.active_reservations()[0].created_at
        # A live incarnation presenting *no* identity must still prove the
        # predecessor dead *and* the lease lapsed before it may take over.
        early = ledger.try_reserve(
            key, OwnerToken("shared", 2), make_estimate(tmp_path, 2 * GB),
            now=base + timedelta(seconds=30),
        )
        assert early.outcome is AdmissionOutcome.UNAVAILABLE
        assert early.reason_code == sb.FencingError.reason_code
        # Proven-dead predecessor with a lapsed lease: the strictly newer
        # generation is admitted and the original audit timing is retained.
        takeover = ledger.try_reserve(
            key, OwnerToken("shared", 2), make_estimate(tmp_path, 2 * GB),
            now=base + timedelta(seconds=120),
        )
        assert takeover.admitted, takeover.reason
        rows = ledger.active_reservations()
        assert [row.owner_id for row in rows] == ["shared"]
        assert rows[0].owner_generation == 2
        assert rows[0].created_at == created_at
        assert [event.event for event in ledger.history(key)] == [
            "RESERVED", "TAKEOVER", "RENEWED",
        ]
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB


def _corrupt_owner_identity(config: LedgerConfig, value: str) -> None:
    conn = sqlite3.connect(config.database_path)
    conn.execute("UPDATE reservations SET owner_identity = ?", (value,))
    conn.commit()
    conn.close()


@pytest.mark.parametrize("corrupt", ["{not json", "", "null", "[]", '{"host": "x"}'])
def test_corrupt_recorded_identity_denies_every_mutation_and_stays_charged(
    tmp_path: Path, corrupt: str
) -> None:
    """Covers: ADR-0003 Amendment 2 (unreadable recorded identity fails closed)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    key = make_key()
    owner = OwnerToken("owner", 1, ProcessIdentity.for_self())
    with StorageLedger(config, fs) as ledger:
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, 2 * GB)).admitted
    _corrupt_owner_identity(config, corrupt)
    with StorageLedger(config, fs) as ledger:
        same = OwnerToken("owner", 1)
        reserve = ledger.try_reserve(key, same, make_estimate(tmp_path, 2 * GB))
        assert reserve.outcome is AdmissionOutcome.UNAVAILABLE
        assert reserve.reason_code == LedgerCorruptError.reason_code
        renew = ledger.renew(key, same, make_estimate(tmp_path, 2 * GB))
        assert renew.outcome is AdmissionOutcome.UNAVAILABLE
        assert renew.reason_code == LedgerCorruptError.reason_code
        with pytest.raises(LedgerCorruptError):
            ledger.observe(key, same, tmp_path, 1024)
        assert ledger.settle(key, same).reason_code == LedgerCorruptError.reason_code
        assert ledger.release(key, same).reason_code == LedgerCorruptError.reason_code
        # A token carrying the exact recorded-looking identity cannot help: with
        # no readable recorded incarnation there is nothing to match against.
        assert ledger.settle(key, owner).reason_code == LedgerCorruptError.reason_code
        # Conservative: nothing was mutated and the capacity stays fully charged.
        assert ledger.reclaim_stale() == ()
        active = ledger.active_reservations()
        assert [row.state for row in active] == [sb.ReservationState.RESERVED]
        assert sum(sample.bound_bytes for sample in active[0].filesystems) == 2 * GB
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB
        assert [event.event for event in ledger.history(key)] == ["RESERVED"]


def test_corrupt_recorded_identity_denies_terminal_reopen(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (a corrupt terminal row is not resurrectable)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    key = make_key()
    owner = OwnerToken("owner", 1, ProcessIdentity.for_self())
    with StorageLedger(config, fs) as ledger:
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, 2 * GB)).admitted
        assert ledger.settle(key, owner).reason == "SETTLED"
    _corrupt_owner_identity(config, "{not json")
    with StorageLedger(config, fs) as ledger:
        reopened = ledger.try_reserve(
            key, OwnerToken("owner", 2), make_estimate(tmp_path, 2 * GB)
        )
        assert reopened.outcome is AdmissionOutcome.UNAVAILABLE
        assert reopened.reason_code == LedgerCorruptError.reason_code
        assert reopened.reserved is False
        # Even a matching explicit incarnation and a newer generation cannot
        # fabricate ownership over an unprovable predecessor.
        again = ledger.try_reserve(
            key, OwnerToken("owner", 2, ProcessIdentity.for_self()),
            make_estimate(tmp_path, 2 * GB),
        )
        assert again.reason_code == LedgerCorruptError.reason_code
        assert ledger.active_reservations() == ()
        assert [event.event for event in ledger.history(key)] == ["RESERVED", "SETTLED"]


def test_unknown_reservation_mutations_are_typed_refusals(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (an unknown key is a typed refusal, never a write)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        absent = make_key(session="absent")
        assert (
            ledger.settle(absent, OwnerToken("o", 1)).reason_code
            == sb.FencingError.reason_code
        )
        assert (
            ledger.release(absent, OwnerToken("o", 1)).reason_code
            == sb.FencingError.reason_code
        )
        renew = ledger.renew(absent, OwnerToken("o", 1), make_estimate(tmp_path, GB))
        assert renew.reason_code == sb.FencingError.reason_code
        with pytest.raises(sb.FencingError):
            ledger.observe(absent, OwnerToken("o", 1), tmp_path, 1024)
        assert ledger.active_reservations() == ()


_FOREIGN_OMITTED_IDENTITY_SOURCE = '''
import json
import sys
from pathlib import Path

from saberops.storage_budget import (
    EstimateComponent,
    EstimateSource,
    FilesystemIdentity,
    LedgerConfig,
    OwnerToken,
    ReservationKey,
    StorageEstimate,
    StorageLedger,
)

payload = json.loads(Path(sys.argv[1]).read_text())


def probe(path: Path) -> FilesystemIdentity:
    return FilesystemIdentity(
        key=payload["fs_key"],
        device=11,
        total_bytes=payload["total"],
        free_bytes=payload["free"],
    )


def summarise(decision):
    return {
        "outcome": str(decision.outcome),
        "reason": decision.reason_code,
        "reserved": decision.reserved,
    }


config = LedgerConfig(
    root=Path(payload["root"]),
    policy_path=Path(payload["policy"]),
    allow_isolated=True,
    lock_timeout_seconds=60.0,
)
estimate = StorageEstimate(
    components=(
        EstimateComponent(path=payload["path"], remaining_growth_bytes=payload["bytes"]),
    ),
    source=EstimateSource.OWNER_HINT,
    sample_count=0,
    confidence=0.5,
)
key = ReservationKey(
    installation_id=payload["installation"],
    session_id=payload["session"],
    attempt_id=payload["attempt"],
    phase_id=payload["phase"],
)
owner_id = payload["owner_id"]
generation = int(payload["generation"])

results = {}
with StorageLedger(config, probe) as ledger:
    # Every token below omits its identity: the child resolves to its own live
    # process and must be refused because it is not the recorded owner.
    omitted = OwnerToken(owner_id, generation)
    results["try_reserve"] = summarise(ledger.try_reserve(key, omitted, estimate))
    results["renew"] = summarise(ledger.renew(key, omitted, estimate))
    results["settle"] = summarise(ledger.settle(key, omitted))
    results["release"] = summarise(ledger.release(key, omitted))
    try:
        ledger.observe(key, omitted, Path(payload["path"]), 1024)
        results["observe"] = "NO_REFUSAL"
    except Exception as exc:
        results["observe"] = type(exc).__name__
    results["reopen"] = summarise(
        ledger.try_reserve(key, OwnerToken(owner_id, generation + 1), estimate)
    )
print(json.dumps(results))
'''


def test_foreign_process_omitted_identity_cannot_mutate_a_live_reservation(
    tmp_path: Path,
) -> None:
    """Covers: ADR-0003 Amendment 2 (omitting identity is not cross-process authority)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    script = tmp_path / "foreign_omitted.py"
    script.write_text(_FOREIGN_OMITTED_IDENTITY_SOURCE)
    key = make_key()
    owner = OwnerToken("shared", 1, ProcessIdentity.for_self())
    with StorageLedger(config, fs) as ledger:
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, 2 * GB)).admitted
    payload = {
        "fs_key": "fs",
        "total": 16 * GB,
        "free": 8 * GB,
        "root": str(config.root),
        "policy": str(config.policy_path),
        "path": str(tmp_path),
        "bytes": 2 * GB,
        "installation": key.installation_id,
        "session": key.session_id,
        "attempt": key.attempt_id,
        "phase": key.phase_id,
        "owner_id": "shared",
        "generation": 1,
    }
    payload_path = tmp_path / "foreign-payload.json"
    payload_path.write_text(json.dumps(payload))
    src = str(Path(sb.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
    completed = subprocess.run(
        [sys.executable, str(script), str(payload_path)],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert lines, completed.stderr
    results = lines[0]
    for name in ("try_reserve", "renew", "settle", "release", "reopen"):
        assert results[name]["outcome"] == "UNAVAILABLE", (name, results[name])
        assert results[name]["reason"] == sb.FencingError.reason_code, (name, results[name])
        assert results[name]["reserved"] is False, (name, results[name])
    assert results["observe"] == "FencingError", results
    # The original reservation is untouched and still fully charged: the
    # foreign no-identity attempts caused no capacity undercount.
    with StorageLedger(config, fs) as ledger:
        active = ledger.active_reservations()
        assert len(active) == 1
        assert active[0].owner_identity == owner.identity
        assert sum(sample.bound_bytes for sample in active[0].filesystems) == 2 * GB
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB
        assert [event.event for event in ledger.history(key)] == ["RESERVED"]
        # The genuine same-process owner (omitted identity) still operates.
        assert ledger.settle(key, OwnerToken("shared", 1)).reason == "SETTLED"
        assert ledger.try_reserve(
            key, OwnerToken("shared", 2), make_estimate(tmp_path, 2 * GB)
        ).admitted
        assert [event.event for event in ledger.history(key)] == [
            "RESERVED", "SETTLED", "RESERVED",
        ]


def test_state_restart_keeps_the_same_reservation(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (restart/durability, no missing-ledger all-clear)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    with StorageLedger(config, fs) as ledger:
        assert ledger.try_reserve(
            make_key(session="s1"), OwnerToken("o1", 1), make_estimate(tmp_path, 2 * GB)
        ).admitted
    with StorageLedger(config, fs) as reopened:
        assert len(reopened.active_reservations()) == 1
        report = reopened.assess(make_estimate(tmp_path))
        assert report.total_active_remaining == 2 * GB
        assert report.required_free == GB + 2 * GB + GB  # margin + active + candidate
        again = reopened.try_reserve(
            make_key(session="s1"), OwnerToken("o1", 1), make_estimate(tmp_path, 2 * GB)
        )
        assert again.admitted
        assert len(reopened.active_reservations()) == 1
        assert again.outcome is AdmissionOutcome.ADMITTED
        assert again.estimate_version == 1
        assert again.confidence == 0.5


# --------------------------------------------------------------------------
# staleness, leases and PID reuse
# --------------------------------------------------------------------------


def test_lease_expiry_and_pid_reuse(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (stale lease/PID reuse, no premature reclaim)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        # A dead owner with an unexpired lease is reported stale but kept.
        assert ledger.try_reserve(
            make_key(session="dead"), OwnerToken("d", 1, dead_identity()),
            make_estimate(tmp_path, GB), now=base,
        ).admitted
        assert ledger.reclaim_stale(now=base + timedelta(seconds=30)) == ()
        # A live owner is never reclaimed, even after the lease lapses.
        live = ProcessIdentity.for_self()
        assert ledger.try_reserve(
            make_key(session="live"), OwnerToken("l", 1, live),
            make_estimate(tmp_path, GB), now=base,
        ).admitted
        reclaimed = ledger.reclaim_stale(now=base + timedelta(seconds=120))
        assert [key.session_id for key in reclaimed] == ["dead"]
        assert [r.key.session_id for r in ledger.active_reservations()] == ["live"]
        # PID reuse: the same pid with a different start_ticks is a dead owner.
        reused = dataclasses.replace(live, start_ticks=(live.start_ticks or 0) + 1)
        assert ledger.try_reserve(
            make_key(session="reused"), OwnerToken("r", 1, reused),
            make_estimate(tmp_path, GB), now=base,
        ).admitted
        assert [key.session_id for key in ledger.reclaim_stale(
            now=base + timedelta(seconds=200))] == ["reused"]
        dead = [r for r in ledger.active_reservations() if r.key.session_id == "dead"]
        assert dead == []


def test_owner_token_rejects_empty_id_and_nonpositive_generation(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (an unvalidated fencing token is refused)."""
    for owner_id, generation in (("", 1), ("   ", 1), ("o", 0), ("o", -3)):
        with pytest.raises(sb.FencingError):
            OwnerToken(owner_id, generation)
    assert OwnerToken("o", 1).generation == 1
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        with pytest.raises(sb.FencingError):
            ledger.settle(make_key(), OwnerToken("o", 0))
        assert ledger.active_reservations() == ()


def test_same_generation_from_another_incarnation_is_fenced(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (PID-reuse replay is not the recorded owner)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        key = make_key()
        incarnation = ProcessIdentity.for_self()
        assert ledger.try_reserve(
            key, OwnerToken("owner", 1, incarnation), make_estimate(tmp_path, GB)
        ).admitted
        reused = dataclasses.replace(
            incarnation, start_ticks=(incarnation.start_ticks or 0) + 1
        )
        replay = ledger.try_reserve(
            key, OwnerToken("owner", 1, reused), make_estimate(tmp_path, GB)
        )
        assert replay.outcome is AdmissionOutcome.UNAVAILABLE
        assert replay.reason_code == sb.FencingError.reason_code
        assert len(ledger.active_reservations()) == 1
        with pytest.raises(sb.FencingError):
            ledger.observe(key, OwnerToken("owner", 1, reused), tmp_path, 1024)
        # A caller that presents no identity still rides the recorded incarnation.
        assert ledger.try_reserve(
            key, OwnerToken("owner", 1), make_estimate(tmp_path, GB)
        ).admitted
        assert len(ledger.active_reservations()) == 1


def test_corrupt_reservation_row_fails_closed(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (a tampered row is corruption, not a crash)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    with StorageLedger(config, fs) as ledger:
        assert ledger.try_reserve(
            make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB)
        ).admitted
    conn = sqlite3.connect(config.database_path)
    conn.execute("UPDATE reservations SET state = ?", ("NOT_A_STATE",))
    conn.commit()
    conn.close()
    with StorageLedger(config, fs) as ledger:
        decision = ledger.try_reserve(
            make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB)
        )
        assert decision.outcome is AdmissionOutcome.UNAVAILABLE
        assert decision.reason_code == sb.LedgerCorruptError.reason_code
        assert decision.reserved is False


def test_corrupt_owner_identity_stays_charged(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (unreadable owner state fails closed, never 0)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    config = make_config(tmp_path, lease_seconds=60.0)
    with StorageLedger(config, fs) as ledger:
        assert ledger.try_reserve(
            make_key(session="corrupt"), OwnerToken("c", 1), make_estimate(tmp_path, 2 * GB),
            now=base,
        ).admitted
    conn = sqlite3.connect(config.database_path)
    conn.execute("UPDATE reservations SET owner_identity = ?", ("{not json",))
    conn.commit()
    conn.close()
    with StorageLedger(config, fs) as ledger:
        assert ledger.reclaim_stale(now=base + timedelta(seconds=600)) == ()
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == 2 * GB


def test_assessment_does_not_probe_live_leases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 Amendment 2 (bounded owner scan, unexpired lease charged)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    calls: list[int] = []

    def counted(identity: ProcessIdentity | None) -> Liveness:
        calls.append(1)
        return owner_probe_liveness(identity)

    # Patched through the module namespace: the probe is bound at import time.
    monkeypatch.setattr(sb, "probe_liveness", counted)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        for index in range(5):
            assert ledger.try_reserve(
                make_key(session=f"s{index}"), OwnerToken(f"o{index}", 1, dead_identity()),
                make_estimate(tmp_path, GB), now=base,
            ).admitted
        report = ledger.assess(make_estimate(tmp_path), now=base + timedelta(seconds=30))
        assert report.total_active_remaining == 5 * GB  # unexpired leases stay charged
        assert calls == []  # no /proc read while the lease is valid
        assert not any(item.stale for item in report.filesystems[0].contributions)
        assert ledger.reclaim_stale(now=base + timedelta(seconds=30)) == ()
        lapsed = ledger.assess(make_estimate(tmp_path), now=base + timedelta(seconds=120))
        assert len(calls) == 5
        assert all(item.stale for item in lapsed.filesystems[0].contributions)
        assert lapsed.total_active_remaining == 5 * GB


def test_stale_reservations_stay_fully_charged(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (an unproven owner never shrinks a reservation)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    with StorageLedger(make_config(tmp_path, lease_seconds=60.0), fs) as ledger:
        ledger.try_reserve(
            make_key(session="dead"), OwnerToken("d", 1, dead_identity()),
            make_estimate(tmp_path, 2 * GB), now=base,
        )
        report = ledger.assess(make_estimate(tmp_path, GB), now=base + timedelta(seconds=300))
        assert report.total_active_remaining == 2 * GB
        contribution = report.filesystems[0].contributions[0]
        assert contribution.stale
        assert contribution.stale_reason == "owner_dead_lease_expired"


# --------------------------------------------------------------------------
# observed bytes, backpressure and bounds
# --------------------------------------------------------------------------


def test_observed_vs_remaining_byte_accounting(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (observed owned consumption, conservative shared)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        owner = OwnerToken("owner", 1)
        key = make_key()
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, 2 * GB)).admitted
        remaining = ledger.observe(key, owner, tmp_path, 500 * MIB)
        assert remaining == 2 * GB - 500 * MIB
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == (2 * GB - 500 * MIB)
        # Observed bytes are monotonic: a lower reading never credits space back.
        assert ledger.observe(key, owner, tmp_path, 100 * MIB) == 2 * GB - 500 * MIB
        # A shared, uncertain cache keeps its full conservative bound.
        shared_key = make_key(session="s2", phase="shared-cache")
        shared = make_estimate(tmp_path, GB, attribution=Attribution.SHARED_UNCERTAIN)
        assert ledger.try_reserve(shared_key, owner, shared).admitted
        with pytest.raises(AttributionUnavailableError):
            ledger.observe(shared_key, owner, tmp_path, 100 * MIB)
        assert ledger.assess(make_estimate(tmp_path)).total_active_remaining == (
            3 * GB - 500 * MIB
        )


def test_observed_overshoot_pauses_new_starts_only(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (unknown spike pauses new starts, never kills)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        owner = OwnerToken("owner", 1)
        key = make_key()
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, GB)).admitted
        assert ledger.observe(key, owner, tmp_path, 3 * GB) == 0  # never negative
        report = ledger.assess(make_estimate(tmp_path, GB))
        assert report.outcome is AdmissionOutcome.UNAVAILABLE
        assert report.reason_code == "BACKPRESSURE"
        assert report.backpressure
        assert report.total_active_remaining == 0
        # The running reservation is untouched and still holds a row.
        assert len(ledger.active_reservations()) == 1
        # A recalculation (new estimate version) clears the backpressure.
        cleared = ledger.renew(
            key, owner, make_estimate(tmp_path, 2 * GB, estimate_version=2)
        )
        assert cleared.admitted
        assert ledger.assess(make_estimate(tmp_path, GB)).outcome is AdmissionOutcome.ADMITTED


def test_observe_rejects_foreign_filesystem_and_bad_owner(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (attribution only where it is authoritative)."""
    home = tmp_path / "home"
    scratch = tmp_path / "scratch"
    home.mkdir()
    scratch.mkdir()
    fs = FakeFilesystem({
        home: ("fs-home", 16 * GB, 8 * GB),
        scratch: ("fs-scratch", 16 * GB, 8 * GB),
    })
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        owner = OwnerToken("owner", 1)
        key = make_key()
        assert ledger.try_reserve(key, owner, make_estimate(home, GB)).admitted
        with pytest.raises(EstimateUnavailableError):
            ledger.observe(key, owner, scratch, 1024)
        with pytest.raises(sb.FencingError):
            ledger.observe(key, OwnerToken("owner", 2), home, 1024)
        with pytest.raises(EstimateUnavailableError):
            ledger.observe(key, owner, home, -1)


def test_renewal_is_bounded_and_fit_checked(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (bounded renewal, dynamic lower-only admission)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        owner = OwnerToken("owner", 1)
        key = make_key()
        assert ledger.try_reserve(key, owner, make_estimate(tmp_path, GB)).admitted
        assert ledger.renew(key, owner, make_estimate(tmp_path, int(1.5 * GB))).admitted
        unbounded = ledger.renew(key, owner, make_estimate(tmp_path, 3 * GB))
        assert unbounded.outcome is AdmissionOutcome.UNAVAILABLE
        assert unbounded.reason_code == sb.RenewalBoundError.reason_code
        assert ledger.active_reservations()[0].filesystems[0].bound_bytes == int(1.5 * GB)
        assert ledger.try_reserve(
            make_key(session="s2"), OwnerToken("o2", 1),
            make_estimate(tmp_path, int(5.5 * GB)),
        ).admitted
        refused = ledger.renew(key, owner, make_estimate(tmp_path, 2 * GB))
        assert refused.outcome is AdmissionOutcome.EXCEEDED
        assert refused.reason_code == sb.REASON_EXCEEDED
        # The old bound stands: capacity may be over-reserved, never overcommitted.
        assert ledger.active_reservations()[0].filesystems[0].bound_bytes == int(1.5 * GB)


def test_requirement_arithmetic_rejects_negatives(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (nonnegativity and bounds)."""
    assert compute_required_free(GB, 2 * GB, 3 * GB) == 6 * GB
    with pytest.raises(EstimateUnavailableError):
        compute_required_free(-1, 0, 0)
    with pytest.raises(EstimateUnavailableError):
        compute_required_free(0, -1, 0)
    sample = sb.FsSample(
        fs_key="fs", bound_bytes=GB, original_bound_bytes=GB, observed_bytes=4 * GB,
        overshoot_bytes=3 * GB, attribution=Attribution.OWNED,
    )
    assert sample.remaining_bytes == 0
    shared = sb.FsSample(
        fs_key="fs", bound_bytes=GB, original_bound_bytes=GB, observed_bytes=4 * GB,
        overshoot_bytes=3 * GB, attribution=Attribution.SHARED_UNCERTAIN,
    )
    assert shared.remaining_bytes == GB


# --------------------------------------------------------------------------
# estimates, policy and failure classification
# --------------------------------------------------------------------------


def test_unknown_historical_samples_fail_closed(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (unknown samples are never read as zero)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        owner = OwnerToken("o", 1)
        cases = [
            make_estimate(
                tmp_path, GB, source=EstimateSource.HISTORICAL_UPPER_TAIL, sample_count=0
            ),
            make_estimate(tmp_path, GB, source=EstimateSource.MEASURED, sample_count=0,
                          confidence=0.9),
            make_estimate(tmp_path, GB, source=EstimateSource.MEASURED, sample_count=3,
                          confidence=0.0),
            make_estimate(tmp_path, GB, source=EstimateSource.CONSERVATIVE_FALLBACK),
            make_estimate(tmp_path, 0),
            StorageEstimate(components=(), source=EstimateSource.OWNER_HINT, sample_count=0,
                            confidence=0.5),
            StorageEstimate(
                components=(EstimateComponent(path=str(tmp_path), remaining_growth_bytes=GB),),
                source=EstimateSource.OWNER_HINT, sample_count=0, confidence=2.0,
                estimate_version=0,
            ),
            StorageEstimate(
                components=(EstimateComponent(path=str(tmp_path), remaining_growth_bytes=GB),),
                source=EstimateSource.OWNER_HINT, sample_count=-1, confidence=1.0,
            ),
        ]
        for index, estimate in enumerate(cases):
            decision = ledger.try_reserve(make_key(session=f"s{index}"), owner, estimate)
            assert decision.outcome is AdmissionOutcome.UNAVAILABLE, estimate
            assert decision.reason_code == EstimateUnavailableError.reason_code
        assert ledger.active_reservations() == ()
        assert ledger.try_reserve(
            make_key(session="fallback"), owner,
            make_estimate(tmp_path, GB, source=EstimateSource.CONSERVATIVE_FALLBACK,
                          conservative_fallback=True),
        ).admitted


def test_invalid_margins_and_missing_policy_fail_closed(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (margin calibration is an owner matter)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    bad: list[dict[str, object]] = [
        {"approved": "yes"},
        {"approved": True, "fixed_bytes": GB, "fraction": 0.1},
        {"approved": True, "fixed_bytes": 0, "fraction": None},
        {"approved": True, "fixed_bytes": None, "fraction": 0.0},
        {"approved": True, "fixed_bytes": None, "fraction": 1.0},
        {"approved": True, "fixed_bytes": GB, "min_bytes": 0},
        {"approved": True, "fixed_bytes": GB, "min_bytes": 2 * GB, "max_bytes": GB},
        {"approved": True, "fixed_bytes": GB, "min_bytes": True},
        {"approved": True, "fixed_bytes": True},
        {"approved": True, "fixed_bytes": -1},
    ]
    for index, override in enumerate(bad):
        directory = tmp_path / f"case{index}"
        directory.mkdir()
        path = write_policy(directory, **override)
        with pytest.raises(PolicyUnavailableError):
            load_margin_policy(path)
    malformed = tmp_path / "malformed.json"
    malformed.write_text("{not json")
    with pytest.raises(PolicyUnavailableError):
        load_margin_policy(malformed)
    assert load_margin_policy(tmp_path / "absent.json").approved is False
    assert load_margin_policy(None).approved is False
    unapproved = MarginPolicy(False, None, None, 0, 0, "none", "absent")
    with pytest.raises(PolicyUnavailableError):
        unapproved.margin_for(GB, 16 * GB)
    # An unapproved policy yields a labelled shadow estimate, never an admission.
    config = LedgerConfig(
        root=tmp_path / "ledger", policy_path=tmp_path / "absent.json", allow_isolated=True
    )
    with StorageLedger(config, fs) as ledger:
        decision = ledger.try_reserve(make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB))
        assert decision.outcome is AdmissionOutcome.UNAVAILABLE
        assert decision.reason_code == PolicyUnavailableError.reason_code
        assert decision.margin_authorised is False
        assert decision.margin_source == "shadow-placeholder-unapproved"
        assert decision.emergency_margin == sb.SHADOW_MARGIN_BYTES != 0
        assert decision.emergency_margin < 10 * GB  # smaller than the retired static floor
        assert ledger.active_reservations() == ()


def test_fractional_margin_is_bounded(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (bounded, non-zero, owner-calibrated margin)."""
    path = write_policy(tmp_path, fixed_bytes=None, fraction=0.2, min_bytes=GB, max_bytes=2 * GB)
    policy = load_margin_policy(path)
    assert policy.approved and policy.fixed_bytes is None and policy.fraction == 0.2
    assert policy.margin_for(0, 16 * GB) == 2 * GB  # capped at max_bytes
    assert policy.margin_for(0, 8 * GB) == int(8 * GB * 0.2)
    assert policy.margin_for(0, 0) == GB  # floored at min_bytes


# --------------------------------------------------------------------------
# ledger integrity, sharing and security
# --------------------------------------------------------------------------


def test_corrupt_ledger_fails_closed(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (corrupt ledger is never an all-clear)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    with StorageLedger(config, fs):
        pass
    for suffix in ("-wal", "-shm"):
        Path(str(config.database_path) + suffix).unlink(missing_ok=True)
    config.database_path.write_bytes(b"not a database at all" * 32)
    ledger = StorageLedger(config, fs)
    with pytest.raises((LedgerCorruptError, sb.LedgerUnavailableError)):
        ledger.open()
    decision = ledger.try_reserve(make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB))
    assert decision.outcome is AdmissionOutcome.UNAVAILABLE
    assert decision.reserved is False
    with pytest.raises((LedgerCorruptError, sb.LedgerUnavailableError)):
        ledger.active_reservations()


def test_missing_schema_version_is_corruption(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (durable schema versioning is enforced)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    with StorageLedger(config, fs):
        pass
    conn = sqlite3.connect(config.database_path)
    conn.execute("DELETE FROM ledger_meta WHERE name = 'schema_version'")
    conn.commit()
    conn.close()
    with pytest.raises(LedgerCorruptError):
        StorageLedger(config, fs).open()


def test_ledger_scope_rejects_path_redirection(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (verified ledger identity, no redirection)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    original = LedgerConfig(
        root=tmp_path / "one", policy_path=write_policy(tmp_path), allow_isolated=True
    )
    with StorageLedger(original, fs) as ledger:
        assert ledger.try_reserve(make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB)
                                  ).admitted
    moved = tmp_path / "two"
    moved.mkdir()
    (moved / sb.LEDGER_FILENAME).write_bytes(
        (tmp_path / "one" / sb.LEDGER_FILENAME).read_bytes()
    )
    with pytest.raises(LedgerSecurityError):
        StorageLedger(
            LedgerConfig(root=moved, policy_path=write_policy(tmp_path), allow_isolated=True), fs
        ).open()


def test_default_root_must_be_the_shared_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 Amendment 2 (reject silently independent ledgers)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    canonical = sb.canonical_ledger_root()
    assert canonical == tmp_path / "state" / "saberops" / sb.LEDGER_DIRNAME
    assert LedgerConfig().database_path == canonical / sb.LEDGER_FILENAME
    assert LedgerConfig().policy_path == tmp_path / "config" / "saberops" / sb.POLICY_FILENAME
    guarded = LedgerConfig(root=tmp_path / "elsewhere")
    with pytest.raises(LedgerSecurityError):
        StorageLedger(guarded).open()


def test_canonical_root_and_policy_ignore_the_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 Amendment 2 (one ledger/policy, never per profile)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    resolved: set[Path] = set()
    for profile in ("alpha", "beta"):
        monkeypatch.setenv("SABEROPS_PROFILE", profile)
        resolved.add(sb.canonical_ledger_root())
        resolved.add(sb.default_policy_path())
        # The surrounding application state *is* still profile-scoped, so the
        # assertion below is not vacuous.
        assert saberops_paths.owner_state_dir() == (
            tmp_path / "state" / "saberops" / "profiles" / profile
        )
    monkeypatch.delenv("SABEROPS_PROFILE")
    resolved.add(sb.canonical_ledger_root())
    resolved.add(sb.default_policy_path())
    assert resolved == {
        tmp_path / "state" / "saberops" / sb.LEDGER_DIRNAME,
        tmp_path / "config" / "saberops" / sb.POLICY_FILENAME,
    }


def test_two_profiles_share_one_ledger_and_accounting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 Amendment 2 (profiles cannot each book free-minus-margin)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("SABEROPS_PROFILE", "alpha")
    alpha = LedgerConfig(lease_seconds=900.0)
    policy_path = alpha.policy_path
    assert policy_path is not None
    assert policy_path == sb.default_policy_path()
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(json.dumps({
        "approved": True, "fixed_bytes": GB, "min_bytes": 64 * MIB,
        "max_bytes": 2 * GB, "source": "test-owner", "version": "1",
    }))
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 4 * GB)})
    with StorageLedger(alpha, fs) as ledger:
        assert ledger.try_reserve(
            make_key(session="alpha"), OwnerToken("a", 1), make_estimate(tmp_path, 3 * GB)
        ).admitted
    monkeypatch.setenv("SABEROPS_PROFILE", "beta")
    beta = LedgerConfig(lease_seconds=900.0)
    assert beta.root == alpha.root == sb.canonical_ledger_root()
    with StorageLedger(beta, fs) as ledger:
        # beta sees alpha's row and cannot independently book the same capacity.
        assert [row.key.session_id for row in ledger.active_reservations()] == ["alpha"]
        overbook = ledger.try_reserve(
            make_key(session="beta"), OwnerToken("b", 1), make_estimate(tmp_path, 3 * GB)
        )
        assert overbook.outcome is AdmissionOutcome.EXCEEDED
        assert overbook.total_active_remaining == 3 * GB
        assert [row.key.session_id for row in ledger.active_reservations()] == ["alpha"]


def test_conflicting_margin_policy_is_rejected_on_the_shared_ledger(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0003 Amendment 2 (one authoritative margin, no per-profile policy)."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    rogue = write_policy(tmp_path, fixed_bytes=64 * MIB, min_bytes=32 * MIB, max_bytes=GB)
    with pytest.raises(LedgerSecurityError):
        StorageLedger(LedgerConfig(policy_path=rogue)).open()
    canonical = LedgerConfig()
    canonical_policy = canonical.policy_path
    assert canonical_policy is not None
    canonical_policy.parent.mkdir(parents=True, exist_ok=True)
    canonical_policy.write_text(json.dumps({
        "approved": True, "fixed_bytes": GB, "min_bytes": 64 * MIB,
        "max_bytes": 2 * GB, "source": "test-owner", "version": "1",
    }))
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(canonical, fs) as ledger:
        assert ledger.try_reserve(
            make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB)
        ).admitted


_CROSS_PROFILE_CHILD = '''
import json
import sys
from pathlib import Path

from saberops.storage_budget import (
    EstimateComponent,
    EstimateSource,
    FilesystemIdentity,
    LedgerConfig,
    OwnerToken,
    ReservationKey,
    StorageEstimate,
    StorageLedger,
)

payload = json.loads(Path(sys.argv[1]).read_text())
index = int(sys.argv[2])


def probe(path: Path) -> FilesystemIdentity:
    return FilesystemIdentity(
        key="fs",
        device=11,
        total_bytes=payload["total"],
        free_bytes=payload["free"],
    )


config = LedgerConfig(lease_seconds=900.0)
estimate = StorageEstimate(
    components=(
        EstimateComponent(path=payload["path"], remaining_growth_bytes=payload["bytes"]),
    ),
    source=EstimateSource.OWNER_HINT,
    sample_count=0,
    confidence=0.5,
)
key = ReservationKey(
    installation_id="shared", session_id=f"s{index}", attempt_id="a1", phase_id="gate"
)
with StorageLedger(config, probe) as ledger:
    decision = ledger.try_reserve(key, OwnerToken(f"o{index}", 1), estimate)
    print(json.dumps({"index": index, "outcome": str(decision.outcome),
                      "reason": decision.reason_code, "root": str(config.root)}))
'''


def test_cross_profile_processes_share_one_ledger(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (two profiles, one host ledger: no overbook)."""
    state = tmp_path / "state"
    config_dir = tmp_path / "config"
    policy = config_dir / "saberops" / sb.POLICY_FILENAME
    policy.parent.mkdir(parents=True)
    policy.write_text(json.dumps({
        "approved": True, "fixed_bytes": GB, "min_bytes": 64 * MIB,
        "max_bytes": 2 * GB, "source": "test-owner", "version": "1",
    }))
    script = tmp_path / "profile_child.py"
    script.write_text(_CROSS_PROFILE_CHILD)
    payload = {"total": 16 * GB, "free": 4 * GB, "path": str(tmp_path), "bytes": 3 * GB}
    payload_path = tmp_path / "profile-payload.json"
    payload_path.write_text(json.dumps(payload))
    src = str(Path(sb.__file__).resolve().parents[1])
    expected_root = str(state / "saberops" / sb.LEDGER_DIRNAME)
    results: list[dict[str, object]] = []
    for index, profile in enumerate(("alpha", "beta")):
        env = {
            **os.environ,
            "PYTHONPATH": src,
            "PYTHONDONTWRITEBYTECODE": "1",
            "XDG_STATE_HOME": str(state),
            "XDG_CONFIG_HOME": str(config_dir),
            "SABEROPS_PROFILE": profile,
        }
        completed = subprocess.run(
            [sys.executable, str(script), str(payload_path), str(index)],
            capture_output=True,
            text=True,
            env=env,
            timeout=120,
        )
        assert completed.returncode == 0, completed.stderr
        lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
        assert lines, completed.stderr
        results.append(lines[0])
    # Both profiles resolved the identical unprofiled ledger root...
    assert {row["root"] for row in results} == {expected_root}
    # ...so 4 GiB free, 1 GiB margin, 3 GiB each admits only one of them.
    assert [row["outcome"] for row in results] == ["ADMITTED", "EXCEEDED"], results


def test_ledger_is_owner_only_and_symlinks_are_refused(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (owner-only persistence, no symlink redirection)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    with StorageLedger(config, fs):
        pass
    assert (config.root.stat().st_mode & 0o077) == 0
    assert (config.database_path.stat().st_mode & 0o077) == 0
    loosened = tmp_path / "loose"
    loosened.mkdir(mode=0o755)
    with StorageLedger(
        LedgerConfig(root=loosened, policy_path=write_policy(tmp_path), allow_isolated=True), fs
    ):
        pass
    assert (loosened.stat().st_mode & 0o077) == 0

    target = tmp_path / "target-db"
    target.write_bytes(b"")
    linked = tmp_path / "linked"
    linked.mkdir()
    (linked / sb.LEDGER_FILENAME).symlink_to(target)
    with pytest.raises(LedgerSecurityError):
        StorageLedger(
            LedgerConfig(root=linked, policy_path=write_policy(tmp_path), allow_isolated=True), fs
        ).open()


def test_lock_contention_is_bounded_and_typed(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (bounded locks; contention is a typed INFRA)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = dataclasses.replace(make_config(tmp_path), lock_timeout_seconds=0.2)
    holder = StorageLedger(config, fs)
    holder.open()
    fd = holder._acquire_lock()  # deliberate cross-instance contention
    try:
        started = time.monotonic()
        decision = StorageLedger(config, fs).try_reserve(
            make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB)
        )
        assert time.monotonic() - started < 5
        assert decision.outcome is AdmissionOutcome.UNAVAILABLE
        assert decision.reason_code == sb.LockContentionError.reason_code
    finally:
        holder._release_lock(fd)


def test_report_shape_and_determinism(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (deterministic reasoned per-FS report)."""
    fs = FakeFilesystem({tmp_path: ("fs-2", 16 * GB, 8 * GB)})
    decision = StorageLedger(make_config(tmp_path), fs).try_reserve(
        make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB, uncertainty=256 * MIB)
    )
    assert decision.admitted
    payload = decision.as_dict()
    for field_name in (
        "free_bytes",
        "emergency_margin",
        "total_active_remaining",
        "candidate_bound",
        "required_free",
        "available_for_new_admissions",
        "estimate_version",
        "confidence",
        "filesystems",
    ):
        assert field_name in payload, field_name
    assert payload["candidate_bound"] == GB + 256 * MIB
    assert payload["required_free"] == GB + GB + 256 * MIB
    assert json.loads(json.dumps(payload)) == payload
    assert decision.model_calls == 0
    contributions = payload["filesystems"]
    assert isinstance(contributions, list) and contributions[0]["contributions"] == []
    again = StorageLedger(make_config(tmp_path), fs).assess(
        make_estimate(tmp_path, GB, uncertainty=256 * MIB)
    )
    assert again.free_bytes == decision.free_bytes
    assert again.total_active_remaining == decision.candidate_bound
    assert len(again.filesystems[0].contributions) == 1


# --------------------------------------------------------------------------
# no model calls, no deletion
# --------------------------------------------------------------------------


def test_module_has_no_model_or_network_dependency() -> None:
    """Covers: ADR-0003 Amendment 2 (deterministic, zero-token decision path)."""
    source = Path(sb.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module.split(".")[0])
    assert imported <= {
        "__future__", "contextlib", "fcntl", "hashlib", "json", "os", "re", "socket",
        "sqlite3", "stat", "time", "collections", "dataclasses", "datetime", "enum",
        "pathlib", "typing", "saberops",
    }, imported
    assert sb.MODEL_CALLS == 0


def test_operations_never_delete_owner_files(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (no arbitrary deletion of user data)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    owner_data = tmp_path / "owner-data"
    owner_data.mkdir()
    (owner_data / "precious.txt").write_text("do not touch")
    base = datetime(2026, 10, 15, 12, 0, 0, tzinfo=UTC)
    config = make_config(tmp_path, lease_seconds=60.0)
    before = tree_snapshot(owner_data)
    with StorageLedger(config, fs) as ledger:
        owner = OwnerToken("owner", 1)
        key = make_key()
        decision = ledger.try_reserve(key, owner, make_estimate(tmp_path, GB), now=base)
        assert decision.model_calls == 0
        assert ledger.observe(key, owner, tmp_path, 100 * MIB) == GB - 100 * MIB
        assert ledger.settle(key, owner).reason == "SETTLED"
        dead = make_key(session="dead")
        assert ledger.try_reserve(
            dead, OwnerToken("d", 1, dead_identity()), make_estimate(tmp_path, GB), now=base
        ).admitted
        assert ledger.reclaim_stale(now=base + timedelta(seconds=120)) == (dead,)
        assert ledger.history(key) != ()
    assert (owner_data / "precious.txt").read_text() == "do not touch"
    assert tree_snapshot(owner_data) == before


# --------------------------------------------------------------------------
# cross-process contention: exactly the number that fits is admitted
# --------------------------------------------------------------------------

_CHILD_SOURCE = '''
import json
import sys
import time
from pathlib import Path

from saberops.storage_budget import (
    EstimateComponent,
    EstimateSource,
    FilesystemIdentity,
    LedgerConfig,
    OwnerToken,
    ReservationKey,
    StorageEstimate,
    StorageLedger,
)

payload = json.loads(Path(sys.argv[1]).read_text())
index = int(sys.argv[2])


def probe(path: Path) -> FilesystemIdentity:
    return FilesystemIdentity(
        key=payload["fs_key"],
        device=11,
        total_bytes=payload["total"],
        free_bytes=payload["free"],
    )


config = LedgerConfig(
    root=Path(payload["root"]),
    policy_path=Path(payload["policy"]),
    allow_isolated=True,
    lock_timeout_seconds=60.0,
)
estimate = StorageEstimate(
    components=(
        EstimateComponent(path=payload["path"], remaining_growth_bytes=payload["bytes"]),
    ),
    source=EstimateSource.OWNER_HINT,
    sample_count=0,
    confidence=0.5,
)
key = ReservationKey(
    installation_id="race",
    session_id=f"s{index}",
    attempt_id="a1",
    phase_id="gate",
)

gate = Path(payload["gate"])
while not gate.exists():
    time.sleep(0.002)

with StorageLedger(config, probe) as ledger:
    decision = ledger.try_reserve(key, OwnerToken(f"owner-{index}", 1), estimate)
    print(json.dumps({"index": index, "outcome": str(decision.outcome),
                      "reason": decision.reason_code}))
'''


def test_cross_process_race_admits_only_what_fits(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (four competing processes cannot overbook)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)
    script = tmp_path / "race_child.py"
    script.write_text(_CHILD_SOURCE)
    payload = {
        "fs_key": "fs",
        "device": 11,
        "total": 16 * GB,
        "free": 8 * GB,
        "root": str(config.root),
        "policy": str(config.policy_path),
        "path": str(tmp_path),
        "bytes": 3 * GB,
        "gate": str(tmp_path / "go"),
    }
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(json.dumps(payload))
    src = str(Path(sb.__file__).resolve().parents[1])
    env = {**os.environ, "PYTHONPATH": src, "PYTHONDONTWRITEBYTECODE": "1"}
    processes = [
        subprocess.Popen(
            [sys.executable, str(script), str(payload_path), str(index)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        for index in range(5)
    ]
    (tmp_path / "go").write_text("go")
    results: list[dict[str, object]] = []
    for process in processes:
        out, err = process.communicate(timeout=120)
        assert process.returncode == 0, err
        lines = [json.loads(line) for line in out.splitlines() if line.strip()]
        assert lines, err
        results.append(lines[0])
    admitted = [line for line in results if line["outcome"] == "ADMITTED"]
    # 8 GiB free, 1 GiB margin, 3 GiB each: two fit and the rest are refused.
    assert len(admitted) == 2, results
    with StorageLedger(config, fs) as ledger:
        active = ledger.active_reservations()
        assert len(active) == 2
        total = sum(sample.bound_bytes for row in active for sample in row.filesystems)
        assert total == 6 * GB
        assert total + GB <= 8 * GB  # never knowingly overbooked
        assert len({row.key.session_id for row in active}) == 2


def test_concurrent_threads_do_not_overbook(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (competing in-process writers, one winner set)."""
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    config = make_config(tmp_path)

    def slow_probe(path: Path) -> FilesystemIdentity:
        time.sleep(0.02)  # widen the critical section so writers really overlap
        return fs(path)

    outcomes: list[str] = []
    lock = threading.Lock()

    def worker(index: int) -> None:
        decision = StorageLedger(config, slow_probe).try_reserve(
            make_key(session=f"t{index}"), OwnerToken(f"o{index}", 1),
            make_estimate(tmp_path, 3 * GB),
        )
        with lock:
            outcomes.append(str(decision.outcome))

    threads = [threading.Thread(target=worker, args=(index,)) for index in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert outcomes.count("ADMITTED") == 2, outcomes
    with StorageLedger(config, fs) as ledger:
        assert len(ledger.active_reservations()) == 2


# --------------------------------------------------------------------------
# integration-visible surface
# --------------------------------------------------------------------------


def test_unavailable_decisions_expose_the_typed_reason(tmp_path: Path) -> None:
    """Covers: ADR-0003 Amendment 2 (typed INFRA vocabulary for callers)."""
    assert sb.StorageBudgetError.reason_code == sb.REASON_UNAVAILABLE
    assert sb.LockContentionError.reason_code == "LOCK_CONTENTION"
    assert sb.LedgerCorruptError.reason_code == "LEDGER_CORRUPT"
    assert sb.LedgerSecurityError.reason_code == "LEDGER_INSECURE"
    assert sb.EstimateUnavailableError.reason_code == "ESTIMATE_UNAVAILABLE"
    assert sb.PolicyUnavailableError.reason_code == "MARGIN_POLICY_UNAVAILABLE"
    assert sb.FilesystemIdentityError.reason_code == "FILESYSTEM_MISIDENTIFIED"
    assert sb.FencingError.reason_code == "FENCING_REJECTED"
    assert AdmissionOutcome.ADMITTED.value == "ADMITTED"
    assert ReservationKey("a", "b", "c", "d").render() == "a/b/c/d"
    assert ReservationKey("a", "b", "c", "d").reservation_id == ReservationKey(
        "a", "b", "c", "d"
    ).reservation_id
    with pytest.raises(sb.FencingError):
        ReservationKey("a", "b", "c", "d/e")
    fs = FakeFilesystem({tmp_path: ("fs", 16 * GB, 8 * GB)})
    with StorageLedger(make_config(tmp_path), fs) as ledger:
        decision = ledger.renew(make_key(), OwnerToken("o", 1), make_estimate(tmp_path, GB))
        assert decision.outcome is AdmissionOutcome.UNAVAILABLE
        assert decision.reason_code == sb.FencingError.reason_code
        assert decision.filesystems == ()
        assert decision.primary is None
        assert decision.free_bytes is None
        assert decision.required_free is None
        assert not decision.admitted
