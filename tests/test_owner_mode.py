"""Phase 1.1 H22: one owner mode carries the authority presets (ADR-0082).

Covers the typed decision function, the v2 persisted document, the legacy
migration (the exact saved permission, never wider), the owner-only switch
surfaces, and the two integration points that consult the mode: the autonomous
supervisor and the scheduler dispatch decision.

The frozen table below is the pre-1.1 ``preset x mode`` effective permission,
captured from the old reader before it was removed.  Every migrated store must
land at or below its row.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.autonomous_runtime import AutonomousSupervisor
from saberops.capacity.loop import run_scheduler_loop
from saberops.capacity.scheduler import DispatchDecision
from saberops.cli import main
from saberops.jobs.models import JobState, WaitingReason
from saberops.jobs.service import get_job, submit_job
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer
from saberops.mcp.tools import (
    MUTATING_TOOLS,
    READ_TOOLS,
    TOOL_NAMES,
    ServerContext,
)
from saberops.model_access.readiness_service import ReadinessService
from saberops.owner_mode import (
    ADVISORY_POLICY_FIELDS,
    ENFORCED_ACTIONS,
    ENFORCED_POLICY_FIELDS,
    MODE_CEILINGS,
    HostAccessMode,
    ModeAction,
    ModePermissions,
    OwnerMode,
    OwnerModeError,
    clear_owner_mode,
    decide_owner_action,
    effective_owner_mode,
    load_effective_owner_mode,
    load_owner_mode_settings,
    mode_permissions,
    mode_view,
    normalize_until,
    parse_owner_mode,
    set_host_access,
    set_owner_mode,
)
from saberops.paths import get_owner_mode_path, owner_state_dir

pytestmark = pytest.mark.packaged_routing_default

#: Permission fields in :class:`ModePermissions` order.
_FIELDS: tuple[str, ...] = tuple(ModePermissions.__dataclass_fields__)

#: Pre-1.1 effective permission for every ``(preset, mode)`` pair, as a bit
#: string in ``_FIELDS`` order.  Frozen from the old reader (authority.py).
_LEGACY_EFFECTIVE: dict[tuple[str, str], str] = {
    ("ask_before_actions", "ask-me"): "00000000001",
    ("ask_before_actions", "critical-only"): "00000000001",
    ("ask_before_actions", "away"): "00000000001",
    ("supervised", "ask-me"): "10001100001",
    ("supervised", "critical-only"): "10001100001",
    ("supervised", "away"): "10001100001",
    ("autonomous", "ask-me"): "10001100000",
    ("autonomous", "critical-only"): "11111101010",
    ("autonomous", "away"): "11111101010",
    ("full_autonomy", "ask-me"): "10001100000",
    ("full_autonomy", "critical-only"): "11111111010",
    ("full_autonomy", "away"): "11111111010",
    ("full_autonomy_publish", "ask-me"): "10001100000",
    ("full_autonomy_publish", "critical-only"): "11111111110",
    ("full_autonomy_publish", "away"): "11111111110",
}

_LEGACY_PRESETS = tuple(sorted({preset for preset, _mode in _LEGACY_EFFECTIVE}))


def _expected(preset: str, mode: str) -> dict[str, bool]:
    bits = _LEGACY_EFFECTIVE[(preset, mode)]
    return {field: bit == "1" for field, bit in zip(_FIELDS, bits, strict=True)}


def _legacy_store(
    root: Path,
    preset: str,
    mode: str,
    *,
    until: str | None = None,
    max_jobs: int | None = None,
) -> None:
    """Write the two pre-1.1 documents the removed readers left behind."""
    config = root / "config" / "saberops"
    config.mkdir(parents=True, exist_ok=True)
    (config / "authority.json").write_text(
        json.dumps({"version": 1, "mode": preset, "host_access": "governed"}),
        encoding="utf-8",
    )
    document: dict[str, object] = {"version": 1, "mode": mode}
    if until is not None:
        document["until"] = until
    if max_jobs is not None:
        document["max_jobs"] = max_jobs
    (config / "mode.json").write_text(json.dumps(document), encoding="utf-8")


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    state.mkdir(parents=True, exist_ok=True)
    config.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def test_conftest_guard_keeps_every_test_off_the_real_owner_store() -> None:
    """H22 postmortem: the autouse guard must isolate the owner XDG bases.

    This host leaves ``XDG_CONFIG_HOME`` unset, so the pre-1.1 fallback resolved
    ``~/.config/saberops`` and the migration wrote the owner's real store.  The
    guard in ``tests/conftest.py`` fills the XDG bases per test; this asserts it
    stayed in force.
    """
    from saberops.paths import saberops_config_dir

    assert os.environ.get("XDG_CONFIG_HOME"), "the autouse guard did not set XDG_CONFIG_HOME"
    resolved = saberops_config_dir().resolve()
    real = (Path.home() / ".config" / "saberops").resolve()
    assert resolved != real
    assert real not in resolved.parents


# --------------------------------------------------------------------------- #
# No widening: every legacy preset x mode pair
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(("preset", "mode"), sorted(_LEGACY_EFFECTIVE))
def test_every_legacy_pair_migrates_to_an_equal_or_narrower_grant(
    isolated: Path, preset: str, mode: str
) -> None:
    """H22 done-when: the effective permission before equals the new one."""
    _legacy_store(isolated, preset, mode)
    settings = load_owner_mode_settings()
    assert settings.mode.value == mode
    assert settings.notice is not None and "ADR-0082" in settings.notice

    got = settings.permissions.as_dict()
    expected = _expected(preset, mode)
    # Nothing widens, ever.
    for field in _FIELDS:
        assert not (got[field] and not expected[field]), (preset, mode, field)
    # Everything is preserved exactly except the two publication grants: a
    # mode never pushes to a remote (ADR-0082); a run opts in explicitly.
    for field in _FIELDS:
        if field in {"auto_publish", "auto_publish_candidate"}:
            continue
        assert got[field] == expected[field], (preset, mode, field)
    assert got["auto_publish"] is False
    assert got["auto_publish_candidate"] is False

    for action in ENFORCED_ACTIONS:
        assert decide_owner_action(action).allowed is _expected_action(
            got, mode, action
        ), (preset, mode, action)


def _expected_action(
    expected: dict[str, bool], mode: str, action: ModeAction
) -> bool:
    if action is ModeAction.DISPATCH:
        return expected["auto_start"]
    if mode == "ask-me":
        return False
    field = {
        ModeAction.AUTONOMOUS_START: "auto_start",
        ModeAction.RETRY: "auto_retry",
        ModeAction.REPAIR: "auto_repair",
        ModeAction.ESCALATION: "auto_escalate_when_policy_justifies",
        ModeAction.ACCEPT: "auto_accept",
        ModeAction.PUBLISH: "auto_publish",
        ModeAction.PUBLISH_CANDIDATE: "auto_publish_candidate",
        ModeAction.SAFE_CLEANUP: "auto_safe_cleanup",
    }[action]
    return expected[field]


def test_mode_ceiling_never_exceeds_the_widest_legacy_pair() -> None:
    """No mode can permit more than the widest old store already could."""
    for mode, ceiling in MODE_CEILINGS.items():
        got = ceiling.as_dict()
        for field in _FIELDS:
            widest = any(
                _expected(preset, mode.value)[field] for preset in _LEGACY_PRESETS
            )
            assert not (got[field] and not widest), (mode, field)


def test_a_stored_grant_can_only_narrow_the_ceiling() -> None:
    crafted = replace(MODE_CEILINGS[OwnerMode.ASK_ME], auto_accept=True, auto_retry=True)
    narrowed = mode_permissions(OwnerMode.ASK_ME, crafted)
    assert narrowed.auto_accept is False
    assert narrowed.auto_retry is False
    assert narrowed.auto_start is True


# --------------------------------------------------------------------------- #
# The migration
# --------------------------------------------------------------------------- #


def test_migration_writes_one_v2_document_and_retires_the_old_one(
    isolated: Path,
) -> None:
    until = "2999-01-01T00:00:00Z"
    _legacy_store(isolated, "full_autonomy", "away", until=until)
    config = isolated / "config" / "saberops"

    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.AWAY
    assert settings.until == until
    assert settings.host_access is HostAccessMode.GOVERNED
    assert settings.permissions.auto_accept is True
    assert settings.notice is not None

    document = json.loads(get_owner_mode_path().read_text(encoding="utf-8"))
    assert document["version"] == 2
    assert document["mode"] == "away"
    assert document["permissions"]["auto_accept"] is True
    assert not (config / "authority.json").exists()
    assert (config / "authority.json.migrated").is_file()

    again = load_owner_mode_settings()
    assert again.notice is None
    assert again.permissions == settings.permissions


def test_authority_only_store_migrates_into_ask_me(isolated: Path) -> None:
    """An owner who only ever picked a preset had no declared mode: it is ask-me."""
    _legacy_store(isolated, "full_autonomy", "ask-me")
    get_owner_mode_path().unlink()
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.ASK_ME
    assert settings.permissions.as_dict() == _expected("full_autonomy", "ask-me")
    assert settings.notice is not None


def test_mode_only_store_keeps_the_packaged_preset(isolated: Path) -> None:
    (isolated / "config" / "saberops").mkdir(parents=True, exist_ok=True)
    get_owner_mode_path().write_text(
        json.dumps({"version": 1, "mode": "away", "max_jobs": 2}), encoding="utf-8"
    )
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.AWAY
    assert settings.max_jobs == 2
    assert settings.permissions.as_dict() == _expected("supervised", "away")


def test_a_corrupt_legacy_authority_document_fails_closed(isolated: Path) -> None:
    config = isolated / "config" / "saberops"
    config.mkdir(parents=True, exist_ok=True)
    (config / "authority.json").write_text(
        json.dumps({"version": 1, "mode": "god_mode"}), encoding="utf-8"
    )
    with pytest.raises(OwnerModeError, match="unknown authority mode"):
        load_owner_mode_settings()


# --------------------------------------------------------------------------- #
# The persisted document
# --------------------------------------------------------------------------- #


def test_missing_document_is_ask_me_and_dispatch_stays_attended(isolated: Path) -> None:
    """A fresh install asks the owner; absence is never 'away'."""
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.ASK_ME
    assert settings.source == "package_default"
    assert load_effective_owner_mode() is OwnerMode.ASK_ME

    dispatch = decide_owner_action(ModeAction.DISPATCH)
    assert dispatch.allowed is True
    assert dispatch.mode is OwnerMode.ASK_ME
    retry = decide_owner_action(ModeAction.RETRY)
    assert retry.allowed is False
    assert "leaves retry decisions to the owner" in retry.reason


def test_mode_document_round_trips_and_fails_closed(isolated: Path) -> None:
    """The versioned document persists, and malformed input never selects a mode."""
    until = "2999-01-01T00:00:00Z"
    path = set_owner_mode(OwnerMode.AWAY, until=until, set_by="owner")
    assert path == get_owner_mode_path()
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.AWAY
    assert settings.until == until
    assert settings.set_by == "owner"

    path.write_text(json.dumps({"mode": "away"}), encoding="utf-8")
    with pytest.raises(OwnerModeError, match="missing 'version'"):
        load_owner_mode_settings()

    path.write_text(
        json.dumps(
            {"version": 2, "mode": "ask-me", "until": "2999-01-01T00:00:00Z"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(OwnerModeError, match="only valid with away"):
        load_owner_mode_settings()

    path.write_text(json.dumps({"version": 2, "mode": "someday"}), encoding="utf-8")
    with pytest.raises(OwnerModeError, match="Unknown owner mode"):
        load_owner_mode_settings()

    path.write_text(
        json.dumps(
            {
                "version": 2,
                "mode": "ask-me",
                "permissions": {"auto_start": True},
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(OwnerModeError, match="missing permission fields"):
        load_owner_mode_settings()


def test_set_until_requires_away_and_a_timezone(isolated: Path) -> None:
    with pytest.raises(OwnerModeError, match="only valid with away"):
        set_owner_mode("critical-only", until="2999-01-01T00:00:00Z")
    with pytest.raises(OwnerModeError, match="timezone"):
        set_owner_mode("away", until="2999-01-01T00:00:00")
    with pytest.raises(OwnerModeError, match="Unknown owner mode"):
        parse_owner_mode("afk")
    assert normalize_until("2999-01-01T00:00:00+02:00") == "2998-12-31T22:00:00Z"


def test_clear_removes_the_document(isolated: Path) -> None:
    set_owner_mode("away", max_jobs=10)
    assert clear_owner_mode() == get_owner_mode_path()
    assert clear_owner_mode() is None
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


def test_host_access_lives_in_the_one_document(isolated: Path) -> None:
    set_owner_mode("away", max_jobs=2)
    set_host_access(HostAccessMode.UNRESTRICTED)
    settings = load_owner_mode_settings()
    assert settings.mode is OwnerMode.AWAY
    assert settings.max_jobs == 2
    assert settings.host_access is HostAccessMode.UNRESTRICTED
    document = json.loads(get_owner_mode_path().read_text(encoding="utf-8"))
    assert document["host_access"] == "unrestricted"

    set_host_access("governed")
    assert load_owner_mode_settings().host_access is HostAccessMode.GOVERNED

    # Host access alone on a fresh store creates the one document, still ask-me.
    clear_owner_mode()
    set_host_access("governed")
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


# --------------------------------------------------------------------------- #
# The narrowing decision
# --------------------------------------------------------------------------- #


def test_ask_me_parks_every_autonomous_action(isolated: Path) -> None:
    """Ask-me keeps every acceptance and recovery decision for the owner."""
    set_owner_mode("ask-me")
    for action in (
        ModeAction.RETRY,
        ModeAction.REPAIR,
        ModeAction.ESCALATION,
        ModeAction.ACCEPT,
        ModeAction.PUBLISH,
        ModeAction.PUBLISH_CANDIDATE,
        ModeAction.SAFE_CLEANUP,
    ):
        decision = decide_owner_action(action)
        assert decision.allowed is False, action
        assert decision.mode is OwnerMode.ASK_ME

    policy = load_owner_mode_settings().permissions
    assert policy.auto_retry is False
    assert policy.auto_repair is False
    assert policy.auto_escalate_when_policy_justifies is False
    # The attended start gate stays open: an explicit submission is not a
    # judgment about whether to act.
    assert policy.auto_start is True


def test_away_and_critical_only_converge_and_accept(isolated: Path) -> None:
    """F1 done-when (b): the unattended modes grant the recorded repair set."""
    for mode in (OwnerMode.CRITICAL_ONLY, OwnerMode.AWAY):
        if mode is OwnerMode.AWAY:
            set_owner_mode(mode, max_jobs=10)
        else:
            set_owner_mode(mode)
        for action in (
            ModeAction.RETRY,
            ModeAction.REPAIR,
            ModeAction.ESCALATION,
            ModeAction.ACCEPT,
            ModeAction.DISPATCH,
        ):
            assert decide_owner_action(action).allowed is True, (mode, action)
        # Publication is never a mode grant (ADR-0082).
        assert decide_owner_action(ModeAction.PUBLISH).allowed is False
        assert decide_owner_action(ModeAction.PUBLISH_CANDIDATE).allowed is False


def test_ask_me_never_auto_accepts(isolated: Path) -> None:
    """F1 done-when (c): acceptance is manual while the owner is present."""
    set_owner_mode("ask-me")
    assert decide_owner_action(ModeAction.ACCEPT).allowed is False
    assert load_owner_mode_settings().permissions.auto_accept is False
    set_owner_mode("away", max_jobs=10)
    assert decide_owner_action(ModeAction.ACCEPT).allowed is True


def test_expired_away_behaves_as_ask_me(isolated: Path) -> None:
    set_owner_mode("away", until="2026-01-01T00:00:00Z")
    later = datetime(2030, 1, 1, tzinfo=UTC)
    decision = decide_owner_action(ModeAction.RETRY, now=later)
    assert decision.expired is True
    assert decision.mode is OwnerMode.ASK_ME
    assert decision.allowed is False
    view = mode_view(now=later)
    assert view["declared_mode"] == "away"
    assert view["mode"] == "ask-me"
    assert view["expired"] is True
    settings = load_owner_mode_settings()
    assert effective_owner_mode(settings, now=later) == (OwnerMode.ASK_ME, True)


def test_decision_view_labels_unenforced_actions_advisory(isolated: Path) -> None:
    """F1 done-when (e) at the projection level: no live promise for unread work."""
    set_owner_mode("away", max_jobs=10)
    view = mode_view()
    assert view["decisions"]["accept"]["allowed"] is True
    assert view["decisions"]["accept"]["enforced"] is True
    for action in ENFORCED_ACTIONS:
        assert view["decisions"][action.value]["enforced"] is True
    assert "auto_accept" not in ADVISORY_POLICY_FIELDS
    assert "auto_accept" in ENFORCED_POLICY_FIELDS


# --------------------------------------------------------------------------- #
# Decision points: autonomous supervisor and scheduler dispatch
# --------------------------------------------------------------------------- #


def test_autonomous_supervisor_uses_the_effective_mode(
    rig: RigEnvironment, isolated: Path
) -> None:
    set_owner_mode("ask-me")
    supervisor = AutonomousSupervisor(db=rig.db())
    assert supervisor.allows_retry is False
    assert supervisor.allows_repair is False
    assert supervisor.allows_escalation is False

    set_owner_mode("away", max_jobs=10)
    supervisor = AutonomousSupervisor(db=rig.db())
    assert supervisor.allows_retry is True
    assert supervisor.allows_repair is True
    assert supervisor.allows_escalation is True


def test_a_migrated_narrow_store_parks_dispatch_it_never_earned(
    rig: RigEnvironment, isolated: Path
) -> None:
    """A store saved with no auto_start keeps parking dispatch in away mode."""
    _legacy_store(isolated, "ask_before_actions", "away", max_jobs=10)
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="parked by the migrated grant").job

    tick = run_scheduler_loop(
        db,
        once=True,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
        run_launcher=lambda decision_job, dispatch, run_id: run_id,
    )
    assert tick is not None and tick.ran is True, tick
    assert tick.dispatched == 0
    parked = get_job(db, job.job_id)
    assert parked is not None
    assert parked.state is JobState.QUEUED
    assert parked.waiting_reason is WaitingReason.MODE_RESTRICTED


def test_ask_me_keeps_owner_submitted_dispatch_working(
    rig: RigEnvironment, isolated: Path
) -> None:
    """The attended mode does not turn an explicit submission into inert work."""
    set_owner_mode("ask-me")
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="run while the owner is present").job
    tick = run_scheduler_loop(
        db,
        once=True,
        readiness_service=ReadinessService.for_owner_state(owner_state_dir()),
        run_launcher=lambda decision_job, dispatch, run_id: run_id,
    )
    assert tick is not None and tick.ran is True, tick
    dispatched = [
        decision.job.job_id
        for decision in tick.decisions
        if isinstance(decision, DispatchDecision)
    ]
    assert dispatched == [job.job_id]


# --------------------------------------------------------------------------- #
# Owner-only surfaces
# --------------------------------------------------------------------------- #


def test_cli_mode_show_set_clear_and_host_access(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = main(["mode", "set", "away", "--until", "2999-01-01T00:00:00Z", "--json"])
    assert code == 0, capsys.readouterr().err
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "away"
    assert payload["until"] == "2999-01-01T00:00:00Z"

    assert main(["mode", "show", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["mode"] == "away"
    assert view["decisions"]["retry"]["allowed"] is True
    assert "authority_mode" not in view
    assert "auto_accept" in view["enforced_fields"]
    assert "auto_retry" in view["enforced_fields"]

    assert main(["mode", "set", "ask-me", "--json"]) == 0
    capsys.readouterr()
    assert main(["mode", "show", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["decisions"]["retry"]["allowed"] is False

    assert main(["mode", "set-host-access", "unrestricted", "--json"]) == 0
    capsys.readouterr()
    assert load_owner_mode_settings().host_access is HostAccessMode.UNRESTRICTED
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME

    assert main(["mode", "clear"]) == 0
    assert "ask-me" in capsys.readouterr().out
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME


def _context(tmp_path: Path) -> ServerContext:
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    return ServerContext(
        db_path=tmp_path / "mcp.db",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(orch,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=OrchestratorLease(tmp_path / "lease.json", holder="mode-test", pid=os.getpid()),
    )


def _server(tmp_path: Path) -> McpServer:
    server = McpServer(_context(tmp_path))
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def _payload(server: McpServer, name: str, arguments: dict[str, object]) -> dict[str, Any]:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None and "result" in response, response
    result = response["result"]
    assert result["isError"] is False, result
    payload: dict[str, Any] = result["structuredContent"]
    return payload


def test_mcp_reads_mode_and_cannot_change_it(isolated: Path, tmp_path: Path) -> None:
    """F1 done-when (d): proposal raises an owner question, never a mode change."""
    set_owner_mode("ask-me")
    assert "get_mode" in READ_TOOLS and "get_mode" in TOOL_NAMES
    assert "propose_mode_change" in MUTATING_TOOLS
    assert "propose_mode_change" in TOOL_NAMES
    assert "set_mode" not in TOOL_NAMES
    assert "mode_set" not in TOOL_NAMES

    server = _server(tmp_path)
    current = _payload(server, "get_mode", {})
    assert current["mode"] == "ask-me"
    assert "authority_mode" not in current

    proposed = _payload(
        server,
        "propose_mode_change",
        {
            "mode": "away",
            "until": "2999-01-01T00:00:00Z",
            "rationale": "The owner asked for an unattended window.",
            "proposed_by": "planner-1",
        },
    )
    assert proposed["applied"] is False
    assert proposed["proposed_mode"] == "away"
    question = proposed["question"]
    assert question["answerable_by"] == "OWNER"
    assert question["decision_class"] == "OTHER"
    assert load_owner_mode_settings().mode is OwnerMode.ASK_ME

    after = _payload(server, "get_mode", {})
    assert after["mode"] == "ask-me"
