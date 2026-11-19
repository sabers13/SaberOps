"""Phase 1 F1: one owner mode that narrows authority and never widens it.

Covers the typed decision function, its persisted document, the owner-only
switch surfaces, and the two integration points that had no mode gate: the
autonomous supervisor and the scheduler dispatch decision.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.authority import (
    ADVISORY_POLICY_FIELDS,
    ENFORCED_POLICY_FIELDS,
    load_authority_policy,
    set_authority_mode,
)
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
    ENFORCED_ACTIONS,
    ModeAction,
    OwnerMode,
    OwnerModeError,
    clear_owner_mode,
    decide_owner_action,
    effective_authority_policy,
    load_effective_owner_mode,
    load_owner_mode_settings,
    mode_view,
    normalize_until,
    parse_owner_mode,
    set_owner_mode,
)
from saberops.paths import get_owner_mode_path, owner_state_dir

pytestmark = pytest.mark.packaged_routing_default


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
            {"version": 1, "mode": "ask-me", "until": "2999-01-01T00:00:00Z"}
        ),
        encoding="utf-8",
    )
    with pytest.raises(OwnerModeError, match="only valid with away"):
        load_owner_mode_settings()

    path.write_text(json.dumps({"version": 1, "mode": "someday"}), encoding="utf-8")
    with pytest.raises(OwnerModeError, match="Unknown owner mode"):
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


# --------------------------------------------------------------------------- #
# The narrowing decision
# --------------------------------------------------------------------------- #


def test_ask_me_parks_recovery_a_permissive_preset_allows(isolated: Path) -> None:
    """F1 done-when (a): ask-me parks retry/repair/escalation under full autonomy."""
    set_authority_mode("full_autonomy")
    set_owner_mode("ask-me")
    preset = load_authority_policy()
    assert preset.auto_retry and preset.auto_repair
    assert preset.auto_escalate_when_policy_justifies

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

    effective = effective_authority_policy(preset, OwnerMode.ASK_ME)
    assert effective.auto_retry is False
    assert effective.auto_repair is False
    assert effective.auto_escalate_when_policy_justifies is False
    # The mode never narrows the attended start gate.
    assert effective.auto_start == preset.auto_start


def test_away_never_exceeds_the_authority_preset(isolated: Path) -> None:
    """F1 done-when (b): away is the preset, never more."""
    set_owner_mode("away", max_jobs=10)
    expected = {
        "ask_before_actions": (False, False, False, False),
        "supervised": (False, False, False, False),
        "autonomous": (True, True, True, False),
        "full_autonomy": (True, True, True, True),
        "full_autonomy_publish": (True, True, True, True),
    }
    for authority, (retry, repair, escalation, accept) in expected.items():
        set_authority_mode(authority)
        assert decide_owner_action(ModeAction.RETRY).allowed is retry, authority
        assert decide_owner_action(ModeAction.REPAIR).allowed is repair, authority
        assert decide_owner_action(ModeAction.ESCALATION).allowed is escalation, authority
        assert decide_owner_action(ModeAction.ACCEPT).allowed is accept, authority

    # The one dispatch that away may refuse: a preset without auto_start.
    set_authority_mode("ask_before_actions")
    assert decide_owner_action(ModeAction.DISPATCH).allowed is False
    set_owner_mode("ask-me")
    assert decide_owner_action(ModeAction.DISPATCH).allowed is False
    set_owner_mode("away", max_jobs=10)
    assert decide_owner_action(ModeAction.DISPATCH).allowed is False
    set_authority_mode("supervised")
    assert decide_owner_action(ModeAction.DISPATCH).allowed is True


def test_full_autonomy_plus_ask_me_never_auto_accepts(isolated: Path) -> None:
    """F1 done-when (c): the permissive preset cannot leak through ask-me."""
    set_authority_mode("full_autonomy")
    set_owner_mode("ask-me")
    assert decide_owner_action(ModeAction.ACCEPT).allowed is False
    assert effective_authority_policy(
        load_authority_policy(), OwnerMode.ASK_ME
    ).auto_accept is False
    # Away is where the preset's acceptance grant may later be wired (F15).
    set_owner_mode("away", max_jobs=10)
    assert decide_owner_action(ModeAction.ACCEPT).allowed is True


def test_expired_away_behaves_as_ask_me(isolated: Path) -> None:
    set_authority_mode("full_autonomy")
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


def test_decision_view_labels_unenforced_actions_advisory(isolated: Path) -> None:
    """F1 done-when (e) at the projection level: no live promise for unread work."""
    set_authority_mode("full_autonomy")
    set_owner_mode("away", max_jobs=10)
    view = mode_view()
    assert view["decisions"]["accept"]["allowed"] is True
    assert view["decisions"]["accept"]["enforced"] is False
    for action in ENFORCED_ACTIONS:
        assert view["decisions"][action.value]["enforced"] is True
    assert "auto_accept" in ADVISORY_POLICY_FIELDS
    assert "auto_accept" not in ENFORCED_POLICY_FIELDS


# --------------------------------------------------------------------------- #
# Decision points: autonomous supervisor and scheduler dispatch
# --------------------------------------------------------------------------- #


def test_autonomous_supervisor_uses_the_effective_mode(
    rig: RigEnvironment, isolated: Path
) -> None:
    set_authority_mode("full_autonomy")
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


def test_away_parks_dispatch_the_preset_forbids(
    rig: RigEnvironment, isolated: Path
) -> None:
    """F1 dispatch decision: away may not start what ask_before_actions forbids."""
    set_authority_mode("ask_before_actions")
    set_owner_mode("away", max_jobs=10)
    db = rig.db()
    job = submit_job(db, repo=rig.repo, goal="parked by the owner mode").job

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
    set_authority_mode("supervised")
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


def test_cli_mode_show_set_and_clear(
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
    assert view["decisions"]["retry"]["allowed"] is False  # supervised preset

    assert main(["authority", "set", "full_autonomy"]) == 0
    capsys.readouterr()
    assert main(["mode", "show", "--json"]) == 0
    view = json.loads(capsys.readouterr().out)
    assert view["decisions"]["retry"]["allowed"] is True

    assert main(["mode", "set", "ask-me", "--json"]) == 0
    capsys.readouterr()
    assert main(["authority", "show", "--json"]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert "auto_accept" in shown["advisory_fields"]
    assert "auto_retry" in shown["enforced_fields"]

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
