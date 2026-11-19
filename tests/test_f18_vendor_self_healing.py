"""Phase 1 F18: self-healing after a vendor CLI update (ADR-0031).

A version change is detected without a prompt, parks the vendor's providers,
runs the allowlisted ``--help`` guard and one tiny smoke prompt, and either
re-verifies the vendor or files exactly one repair job with the evidence.  No
test sends a prompt to a real vendor: versions, help captures and smoke results
are injected, and one smoke goes through the fake worker CLI only.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from saberops import vendor_health
from saberops.capacity.scheduler import DispatchDecision, WaitDecision, schedule
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
from saberops.jobs.models import JobRequirements, JobRole
from saberops.models import QuotaState, RoutingState, WorkerCandidate
from saberops.routing_config import RoutingSnapshot
from saberops.vendor_cli_fixtures import Fixture, Manifest

OLD_VERSION = "codex-cli 1.0.0"
NEW_VERSION = "codex-cli 1.1.0"
FLAGS = ["--format", "exec", "--model"]


def _fixture(
    cli: str = "codex",
    *,
    version: str = OLD_VERSION,
    vendor: str | None = None,
    flags: Sequence[str] = FLAGS,
) -> Fixture:
    return Fixture(
        vendor=vendor or cli,
        cli=cli,
        cli_version=version,
        captured_on="2026-11-19",
        kind="config_help",
        source="real-capture",
        redacted=True,
        file=f"{cli}/x/config_help.txt",
        consumer=f"saberops.workers.{cli}",
        expect={"flags": list(flags)},
    )


def _manifest(*fixtures: Fixture) -> Manifest:
    return Manifest(
        fixtures=fixtures or (_fixture(),),
        fake_only=(),
    )


def _help_run(fail: bool = False, text: str | None = None) -> Any:
    def run(command: Sequence[str], _timeout: float) -> tuple[int, str, str]:
        if fail:
            return 1, "", "boom"
        return 0, text if text is not None else "Usage: codex exec --format --model\n", ""

    return run


def _passing_smoke(calls: list[tuple[str, str | None]]) -> vendor_health.SmokeRunner:
    def smoke(vendor: str, model: str | None) -> vendor_health.SmokeResult:
        calls.append((vendor, model))
        return vendor_health.SmokeResult(
            passed=True,
            detail="exit=0 status=success usage_rows=1 known_rows=1",
            provider="codex",
            model=model or "gpt-5.6-terra",
            usage_rows=1,
        )

    return smoke


def _failing_smoke(calls: list[tuple[str, str | None]]) -> vendor_health.SmokeRunner:
    def smoke(vendor: str, model: str | None) -> vendor_health.SmokeResult:
        calls.append((vendor, model))
        return vendor_health.SmokeResult(
            passed=False,
            detail="exit=1 status=failed usage_rows=0 known_rows=0",
            provider="codex",
            model=model or "gpt-5.6-terra",
        )

    return smoke


@pytest.fixture()
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Database]:
    for name in ("state", "config", "cache", "data", "home"):
        (tmp_path / name).mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    yield Database(tmp_path / "owner.db")


@pytest.fixture()
def engine_repo(tmp_path: Path) -> Path:
    import subprocess

    repo = tmp_path / "engine"
    repo.mkdir()
    env = {
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@localhost",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@localhost",
        "PATH": "/usr/bin:/bin",
    }
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True, env=env)
    (repo / "README.md").write_text("engine\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True, env=env)
    subprocess.run(["git", "commit", "-q", "-m", "engine"], cwd=repo, check=True, env=env)
    return repo


def _script(
    db: Database,
    monkeypatch: pytest.MonkeyPatch,
    *,
    installed: str,
    help_run: Any = None,
    smoke: vendor_health.SmokeRunner | None = None,
    work_allowed: bool = True,
    manifest: Manifest | None = None,
) -> list[tuple[str, str, str]]:
    notices: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once",
        lambda _ledger, key, title, body: notices.append((key, title, body)),
    )
    assert not vendor_health.detect_changes(
        db,
        manifest=manifest or _manifest(),
        installed={"codex": OLD_VERSION},
        now=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
    )
    changes = vendor_health.detect_changes(
        db,
        manifest=manifest or _manifest(),
        installed={"codex": installed},
        now=datetime(2026, 11, 19, 12, 5, tzinfo=UTC),
    )
    assert [change.vendor for change in changes] == ["codex"]
    vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=manifest or _manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=help_run if help_run is not None else _help_run(),
        smoke=smoke,
        work_allowed=lambda: work_allowed,
        now=datetime(2026, 11, 19, 12, 6, tzinfo=UTC),
    )
    return notices


# ---------------------------------------------------------------------------
# Detection and containment
# ---------------------------------------------------------------------------


def test_first_pass_seeds_the_manifest_version_without_parking(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    changes = vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    assert changes == []
    health = vendor_health.load_health(db)["codex"]
    assert health.state is vendor_health.VendorState.VERIFIED
    assert health.verified_version == OLD_VERSION
    assert vendor_health.blocked_providers(db) == {}


def test_a_version_change_marks_the_vendor_unverified_and_parks_it(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    notices = _script(db, monkeypatch, installed=NEW_VERSION, work_allowed=False)
    health = vendor_health.load_health(db)["codex"]
    assert health.state is vendor_health.VendorState.UNVERIFIED
    assert health.installed_version == NEW_VERSION
    assert health.verified_version == OLD_VERSION
    assert "unverified" in vendor_health.blocked_providers(db)["codex"]
    assert vendor_health.blocked_providers(db) == {
        "codex": f"codex {NEW_VERSION} is unverified: {health.reason}"
    }
    unverified = [notice for notice in notices if "parked" in notice[1].lower()]
    assert len(unverified) == 1
    assert NEW_VERSION in unverified[0][2]


def test_unchanged_interface_is_reverified_and_routing_resumes(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []
    notices = _script(db, monkeypatch, installed=NEW_VERSION, smoke=_passing_smoke(calls))
    health = vendor_health.load_health(db)["codex"]
    assert health.state is vendor_health.VendorState.VERIFIED
    assert health.verified_version == NEW_VERSION
    assert health.smoke_version == NEW_VERSION
    assert health.repair_job_id is None
    assert vendor_health.blocked_providers(db) == {}
    assert len(calls) == 1
    healed = [notice for notice in notices if "resumed" in notice[1].lower()]
    assert len(healed) == 1
    # The next interface change is compared with the *new* verified version.
    changes = vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": NEW_VERSION}
    )
    assert changes == []


def test_removed_flag_parks_the_vendor_and_files_exactly_one_repair_job(
    db: Database, monkeypatch: pytest.MonkeyPatch, engine_repo: Path
) -> None:
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": NEW_VERSION}
    )
    first = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(text="Usage: codex exec --model\n"),
        smoke=_passing_smoke(calls),
        engine_repo=engine_repo,
        work_allowed=lambda: True,
    )
    assert first[0].state is vendor_health.VendorState.FAILED
    assert first[0].repair_job_id
    assert calls == []  # the smoke never runs after a failed interface check
    health = vendor_health.load_health(db)["codex"]
    assert health.state is vendor_health.VendorState.FAILED
    assert health.detail["missing_flags"] == ["--format"]
    assert vendor_health.blocked_providers(db)["codex"].startswith("codex")

    from saberops.jobs import store
    from saberops.jobs.service import get_job

    jobs = store.list_jobs_tolerant(db, limit=1000)[0]
    repair = [job for job in jobs if job.job_id == first[0].repair_job_id]
    assert len(repair) == 1
    job = get_job(db, repair[0].job_id)
    assert job is not None
    assert str(engine_repo) == job.repo
    assert "--format" in job.goal
    assert NEW_VERSION in job.goal
    assert "requires the owner" in job.goal or "owner accepts" in job.goal
    assert job.requirements.model_family_exclude == ("codex",)
    assert vendor_health.is_repair_job(db, job.job_id)

    # A repeated check never files a second job for the same version.
    second = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(text="Usage: codex exec --model\n"),
        smoke=_passing_smoke(calls),
        engine_repo=engine_repo,
        work_allowed=lambda: True,
    )
    assert second[0].repair_job_id == first[0].repair_job_id
    jobs = store.list_jobs_tolerant(db, limit=1000)[0]
    assert len([item for item in jobs if item.job_id == first[0].repair_job_id]) == 1
    assert len(jobs) == 1


def test_unreadable_help_never_files_a_repair_job(
    db: Database, monkeypatch: pytest.MonkeyPatch, engine_repo: Path
) -> None:
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": NEW_VERSION}
    )
    result = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(fail=True),
        engine_repo=engine_repo,
    )
    # An unreadable help capture is never evidence of a removed flag.
    assert result[0].state is vendor_health.VendorState.UNVERIFIED
    assert result[0].repair_job_id is None
    from saberops.jobs import store

    assert store.list_jobs_tolerant(db, limit=100)[0] == []


def test_smoke_runs_once_per_version(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []
    _script(db, monkeypatch, installed=NEW_VERSION, smoke=_passing_smoke(calls))
    for _ in range(3):
        vendor_health.detect_changes(
            db, manifest=_manifest(), installed={"codex": NEW_VERSION}
        )
        vendor_health.run_checks(
            db,
            vendors=("codex",),
            manifest=_manifest(),
            which=lambda _cli: "/usr/bin/codex",
            help_run=_help_run(),
            smoke=_passing_smoke(calls),
        )
    assert len(calls) == 1


def test_smoke_is_off_when_the_owner_turns_it_off(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []
    vendor_health.set_smoke_enabled(db, False)
    assert vendor_health.smoke_enabled(db) is False
    _script(
        db,
        monkeypatch,
        installed=NEW_VERSION,
        smoke=_failing_smoke(calls),
        work_allowed=False,
    )
    health = vendor_health.load_health(db)["codex"]
    assert health.state is vendor_health.VendorState.VERIFIED
    assert calls == []
    assert health.detail["smoke"] == "disabled"


def test_a_failing_smoke_parks_the_vendor_and_files_the_evidence(
    db: Database, monkeypatch: pytest.MonkeyPatch, engine_repo: Path
) -> None:
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": NEW_VERSION}
    )
    result = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(),
        smoke=_failing_smoke(calls),
        engine_repo=engine_repo,
    )
    assert result[0].state is vendor_health.VendorState.FAILED
    health = vendor_health.load_health(db)["codex"]
    assert health.state is vendor_health.VendorState.FAILED
    assert "smoke prompt failed" in health.reason
    assert health.detail["smoke"]["passed"] is False
    assert len(calls) == 1

    from saberops.jobs.service import get_job

    job = get_job(db, health.repair_job_id or "")
    assert job is not None
    assert "usage_rows=0" in job.goal


def test_gate_closed_defers_the_smoke_until_the_gate_opens(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []
    _script(
        db,
        monkeypatch,
        installed=NEW_VERSION,
        smoke=_passing_smoke(calls),
        work_allowed=False,
    )
    health = vendor_health.load_health(db)["codex"]
    assert health.state is vendor_health.VendorState.UNVERIFIED
    assert "work gate" in health.reason
    assert calls == []
    result = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(),
        smoke=_passing_smoke(calls),
        work_allowed=lambda: True,
    )
    assert result[0].state is vendor_health.VendorState.VERIFIED
    assert len(calls) == 1


def test_pin_mismatch_warns_instead_of_adapting(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[str, str | None]] = []
    notices: list[tuple[str, str, str]] = []
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once",
        lambda _ledger, key, title, body: notices.append((key, title, body)),
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    vendor_health.set_pin(db, "codex", OLD_VERSION)
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": NEW_VERSION}
    )
    result = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(),
        smoke=_passing_smoke(calls),
    )
    assert result[0].state is vendor_health.VendorState.PINNED_MISMATCH
    assert calls == []
    assert vendor_health.blocked_providers(db)["codex"].startswith("codex")
    assert any("pinned version changed" in notice[1].lower() for notice in notices)
    assert vendor_health.load_health(db)["codex"].state is (
        vendor_health.VendorState.PINNED_MISMATCH
    )


# ---------------------------------------------------------------------------
# Containment in the pacing scheduler
# ---------------------------------------------------------------------------


def _routes() -> RoutingSnapshot:
    return RoutingSnapshot(
        schema_version=3,
        source="test",
        source_path=None,
        content_hash="test",
        role_chains=(
            (
                "BUILD",
                (
                    (WorkerCandidate("codex", "gpt-5.6-terra"),),
                    (WorkerCandidate("opencode", "opencode-go/deepseek-v4-flash"),),
                ),
            ),
        ),
    )


def _policy() -> ControlPlanePolicy:
    from datetime import timedelta

    today = (datetime(2026, 11, 19, tzinfo=UTC).date() - timedelta(days=5)).isoformat()
    return ControlPlanePolicy(
        provider_policies={
            name: ProviderPolicy(name, ProviderPolicyState.ALLOWED)
            for name in ("codex", "opencode", "anthropic")
        },
        quota_pools={},
        content_hash="test-policy",
        plans=PlanSet(
            accounts={},
            pool_plans={
                "chatgpt-plus": PoolPlan(
                    pool_id="chatgpt-plus",
                    members=(PoolMember(route="codex", multiplier=1.0),),
                    windows=(Window(id="5h", duration_min=300),),
                    mode="STRICT",
                    red_line_pct=10.0,
                    overage_possible=False,
                ),
                "opencode-go": PoolPlan(
                    pool_id="opencode-go",
                    members=(PoolMember(route="opencode", multiplier=1.0),),
                    windows=(Window(id="5h", duration_min=300),),
                    mode="STRICT",
                    red_line_pct=10.0,
                    overage_possible=False,
                ),
            },
            postures={
                "codex": PostureRecord(
                    "codex", "A", ttl_days=30, verified_on=today
                ),
                "opencode": PostureRecord(
                    "opencode", "A", ttl_days=30, verified_on=today
                ),
            },
        ),
    )


def _quota() -> list[QuotaState]:
    from datetime import timedelta

    reset = (datetime(2026, 11, 19, 12, 0, tzinfo=UTC) + timedelta(hours=3)).isoformat()
    return [
        QuotaState(
            provider="codex",
            pool="chatgpt-plus",
            remaining_percent=80.0,
            reset_at=reset,
            routing_state=RoutingState.NORMAL,
            updated_at=datetime(2026, 11, 19, 11, 0, tzinfo=UTC).isoformat(),
        ),
        QuotaState(
            provider="opencode",
            pool="opencode-go",
            remaining_percent=80.0,
            reset_at=reset,
            routing_state=RoutingState.NORMAL,
            updated_at=datetime(2026, 11, 19, 11, 0, tzinfo=UTC).isoformat(),
        ),
    ]


def _job(job_id: str = "job_1") -> Any:
    from saberops.jobs.models import Job, JobState, Priority, WaitingReason

    return Job(
        job_id=job_id,
        idempotency_key=None,
        caller_id="test",
        repo="/repo",
        base_ref="main",
        base_sha="abc1234",
        goal="work",
        acceptance=None,
        requirements=JobRequirements(role=JobRole.BUILD),
        priority=Priority.NORMAL,
        deadline=None,
        not_before=None,
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        failure_reason=None,
        eta=None,
        eta_confidence=None,
        current_run_id=None,
        run_ids=(),
        created_at="2026-11-19T10:00:00+00:00",
        updated_at="2026-11-19T10:00:00+00:00",
        request_digest="digest",
    )


def test_schedule_routes_work_to_the_other_vendor_when_one_is_parked() -> None:
    decisions = schedule(
        now=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
        queued=[_job()],
        routing_snapshot=_routes(),
        policy=_policy(),
        quota_states=_quota(),
        blocked_providers={"codex": "codex 1.1.0 is unverified: version changed"},
    )
    dispatch = [item for item in decisions if isinstance(item, DispatchDecision)]
    assert len(dispatch) == 1
    assert dispatch[0].candidate.provider == "opencode"


def test_schedule_waits_and_names_the_vendor_when_every_route_is_parked() -> None:
    decisions = schedule(
        now=datetime(2026, 11, 19, 12, 0, tzinfo=UTC),
        queued=[_job()],
        routing_snapshot=_routes(),
        policy=_policy(),
        quota_states=_quota(),
        blocked_providers={
            "codex": "codex 1.1.0 is unverified: version changed",
            "opencode": "opencode x is unverified: version changed",
        },
    )
    waits = [item for item in decisions if isinstance(item, WaitDecision)]
    assert len(waits) == 1
    wait = waits[0]
    assert isinstance(wait, WaitDecision)
    assert wait.details["diagnostic"] == "VENDOR_CLI_UNVERIFIED"
    assert set(wait.details["vendor_unverified"]) == {"codex", "opencode"}


def test_scheduler_tick_reports_and_respects_a_parked_vendor(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The tick reads durable health and never dispatches to a parked provider."""
    from saberops.capacity import loop as loop_module

    captured: dict[str, object] = {}

    def fake_schedule(**kwargs: object) -> list[object]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(loop_module, "schedule", fake_schedule)
    monkeypatch.setattr(loop_module, "deliver_pending", lambda _db: None, raising=False)
    vendor_health.save_health(
        db,
        vendor_health.VendorHealth(
            vendor="codex",
            cli="codex",
            installed_version=NEW_VERSION,
            verified_version=OLD_VERSION,
            state=vendor_health.VendorState.UNVERIFIED,
            reason="version changed",
        ),
    )
    # `maybe_check` is disabled in tests; the durable verdict alone must apply.
    from saberops.capacity.loop import acquire_lease, scheduler_tick

    lease = acquire_lease(db, "test_holder", ttl_seconds=30.0)
    assert lease is not None
    result = scheduler_tick(db, lease, now=datetime(2026, 11, 19, 12, 0, tzinfo=UTC))
    assert result.ran is True
    assert result.vendor_unverified == ("codex",)
    assert captured["blocked_providers"] == {
        "codex": "codex codex-cli 1.1.0 is unverified: version changed"
    }


def test_maybe_check_spawns_one_detached_check_per_change(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("SABEROPS_VENDOR_CHECKS", raising=False)
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "saberops.vendor_health.read_installed_version",
        lambda cli, which=None: NEW_VERSION,
    )
    spawns: list[int] = []

    def spawn(_db: object) -> bool:
        spawns.append(1)
        return True

    changes = vendor_health.maybe_check(db, background=True, spawn=spawn)
    assert "codex" in {change.vendor for change in changes}
    vendor_health.maybe_check(db, background=True, spawn=spawn)
    assert len(spawns) == 1


# ---------------------------------------------------------------------------
# The smoke prompt through the fake worker CLI (no real vendor)
# ---------------------------------------------------------------------------


def test_smoke_prompt_runs_through_the_adapter_in_a_throwaway_repository(
    tmp_path: Path,
) -> None:
    """One fake-worker prompt: the adapter parses usage and exit, nothing else."""
    from saberops.workers.codex import CodexAdapter

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "codex"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "import json, sys\n"
        "print(json.dumps({'type': 'turn.completed', 'usage': "
        "{'input_tokens': 12, 'output_tokens': 2, 'total_tokens': 14}}))\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    result = vendor_health.run_smoke_prompt(
        "codex",
        "gpt-5.6-terra",
        worktree_root=tmp_path / "smoke",
        timeout_seconds=30.0,
        adapter=CodexAdapter(codex_bin=str(shim)),
    )
    assert result.passed is True
    assert result.usage_rows == 1
    assert "exit=0" in result.detail


def test_smoke_prompt_fails_closed_when_the_output_shape_is_gone(
    tmp_path: Path,
) -> None:
    from saberops.workers.codex import CodexAdapter

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "codex"
    shim.write_text(
        "#!/usr/bin/env python3\n"
        "print('this is not the json stream the adapter parses')\n",
        encoding="utf-8",
    )
    shim.chmod(0o755)
    result = vendor_health.run_smoke_prompt(
        "codex",
        "gpt-5.6-terra",
        worktree_root=tmp_path / "smoke",
        timeout_seconds=30.0,
        adapter=CodexAdapter(codex_bin=str(shim)),
    )
    assert result.passed is False


def test_a_failed_smoke_is_not_resent_and_the_next_check_can_heal(
    db: Database, monkeypatch: pytest.MonkeyPatch, engine_repo: Path
) -> None:
    """At most one smoke per installed version, including a failed one."""
    calls: list[tuple[str, str | None]] = []
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": NEW_VERSION}
    )
    first = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(),
        smoke=_failing_smoke(calls),
        engine_repo=engine_repo,
    )
    assert first[0].state is vendor_health.VendorState.FAILED
    assert len(calls) == 1
    # The accepted repair updates the engine; the next check re-verifies the
    # interface without spending a second prompt for the same version.
    second = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(),
        smoke=_passing_smoke(calls),
        engine_repo=engine_repo,
    )
    assert second[0].state is vendor_health.VendorState.VERIFIED
    assert second[0].repair_job_id is None
    assert len(calls) == 1
    assert vendor_health.blocked_providers(db) == {}


def test_cli_surface_is_wired(
    db: Database, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import json as _json

    from saberops.cli import main

    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        vendor_health,
        "read_installed_version",
        lambda cli, which=None: OLD_VERSION if cli == "codex" else "missing",
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    assert main(["vendor", "status", "--db", str(db.db_path), "--json"]) == 0
    payload = _json.loads(capsys.readouterr().out)
    assert payload["vendors"]["codex"]["state"] == "verified"
    assert payload["smoke_enabled"] is True

    assert main(["vendor", "smoke", "off", "--db", str(db.db_path)]) == 0
    assert vendor_health.smoke_enabled(db) is False
    assert main(
        ["vendor", "pin", "codex", "codex-cli 1.0.0", "--db", str(db.db_path)]
    ) == 0
    assert vendor_health.pinned_version(db, "codex") == "codex-cli 1.0.0"
    assert main(["vendor", "unpin", "codex", "--db", str(db.db_path)]) == 0
    assert vendor_health.pinned_version(db, "codex") is None
    assert main(["vendor", "status", "--db", str(db.db_path)]) == 0
    assert "codex" in capsys.readouterr().out


def test_a_vendor_with_no_routed_plan_model_heals_on_the_interface_check(
    db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "saberops.vendor_health.notify_once", lambda *args, **kwargs: None
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": OLD_VERSION}
    )
    vendor_health.detect_changes(
        db, manifest=_manifest(), installed={"codex": NEW_VERSION}
    )

    def skipped(_vendor: str, _model: str | None) -> vendor_health.SmokeResult:
        return vendor_health.SmokeResult(
            passed=True, skipped=True, detail="no plan model is routed"
        )

    result = vendor_health.run_checks(
        db,
        vendors=("codex",),
        manifest=_manifest(),
        which=lambda _cli: "/usr/bin/codex",
        help_run=_help_run(),
        smoke=skipped,
    )
    assert result[0].state is vendor_health.VendorState.VERIFIED
    assert vendor_health.blocked_providers(db) == {}


def test_status_renders_each_vendor_and_the_smoke_setting(db: Database) -> None:
    vendor_health.detect_changes(db, manifest=_manifest(), installed={"codex": OLD_VERSION})
    text = vendor_health.render_status(db)
    assert "codex" in text
    assert "verified" in text
    vendor_health.set_smoke_enabled(db, False)
    assert "automatic smoke prompt: off" in vendor_health.render_status(db)


def test_store_is_readable_across_processes(db: Database, tmp_path: Path) -> None:
    """The verdict lives in the owner DB, not in process memory."""
    vendor_health.detect_changes(db, manifest=_manifest(), installed={"codex": OLD_VERSION})
    with sqlite3.connect(str(db.db_path)) as conn:
        row = conn.execute(
            "SELECT state FROM vendor_cli_health WHERE vendor = 'codex'"
        ).fetchone()
    assert row is not None and row[0] == "verified"
