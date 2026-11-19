"""C8 tests: truthful capacity figures, packaged plans, and source/window freshness."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops.capacity.admission import admit
from saberops.capacity.freshness import freshness
from saberops.capacity.probes import handle_probe_result
from saberops.capacity.snapshot import effective_quota_states
from saberops.cli import main as cli_main
from saberops.control_plane.policy import load_control_plane_policy
from saberops.control_plane.quota_policy import QUOTA_EVIDENCE_REQUIRED
from saberops.db import Database
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer
from saberops.mcp.tools import ServerContext
from saberops.models import WorkerCandidate
from saberops.plans_ops import verify_posture


@pytest.fixture(autouse=True)
def isolated_xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, sub in (
        ("XDG_CONFIG_HOME", "xdg-config"),
        ("XDG_STATE_HOME", "xdg-state"),
        ("XDG_CACHE_HOME", "xdg-cache"),
        ("XDG_DATA_HOME", "xdg-data"),
    ):
        d = tmp_path / sub
        d.mkdir(parents=True, exist_ok=True)
        monkeypatch.setenv(name, str(d))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _fresh_db(tmp_path: Path, name: str = "fresh.db") -> Database:
    return Database(tmp_path / name)


def _mcp_call(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    raw = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert raw is not None
    result = raw["result"]
    assert not result.get("isError"), result
    structured: dict[str, Any] = result["structuredContent"]
    return structured


def test_six_hour_old_probe_does_not_admit_against_5h_window(tmp_path: Path) -> None:
    """Freshness follows real source and window; never stretches probes to 24h."""
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    six_hours_ago = now - timedelta(hours=6)

    # Unit check: unknown/missing/probe sources do not get 24h owner limit.
    assert freshness(six_hours_ago.isoformat(), "5h", "PROBE", now) == "STALE"
    assert freshness(six_hours_ago.isoformat(), "5h", None, now) == "STALE"
    assert freshness(six_hours_ago.isoformat(), "5h", "", now) == "STALE"
    assert freshness(six_hours_ago.isoformat(), "5h", "OWNER", now) == "FRESH"

    verify_posture("codex", (), None, None, None)
    policy = load_control_plane_policy()
    assert policy.plans.pool_plans == {}

    db = _fresh_db(tmp_path)
    handle_probe_result(
        db,
        harness="codex",
        windows=[
            ("5h", 80.0, now + timedelta(hours=2), "EXACT"),
            ("weekly", 75.0, now + timedelta(days=3), "EXACT"),
        ],
        now=six_hours_ago,
    )
    states = effective_quota_states(db, now=now, run_probes=False)
    by_win = {s.window_id: s for s in states if s.pool_id == "chatgpt-plus"}
    assert set(by_win) == {"5h", "weekly"}
    assert by_win["5h"].source == "PROBE"
    assert by_win["5h"].freshness == "STALE"

    decision = admit(
        WorkerCandidate(provider="codex", model="gpt-5.3-codex"),
        now=now,
        policy=policy,
        quota_states=states,
    )
    assert decision.admitted is False
    assert decision.reason == QUOTA_EVIDENCE_REQUIRED
    assert decision.binding_window == "5h"


def test_per_window_readings_are_not_collapsed_on_fresh_store(tmp_path: Path) -> None:
    """Probe windows keep independent percentages and ages rather than collapsing."""
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    one_hour_ago = now - timedelta(hours=1)
    verify_posture("codex", (), None, None, None)
    db = _fresh_db(tmp_path)

    # 1h old: STALE for 5h (20m limit) but FRESH for weekly (180m limit).
    handle_probe_result(
        db,
        harness="codex",
        windows=[
            ("5h", 85.0, now + timedelta(hours=3), "EXACT"),
            ("weekly", 60.0, now + timedelta(days=4), "EXACT"),
        ],
        now=one_hour_ago,
    )
    states = effective_quota_states(db, now=now, run_probes=False)
    by_win = {s.window_id: s for s in states if s.pool_id == "chatgpt-plus"}
    assert set(by_win) == {"5h", "weekly"}
    assert by_win["5h"].remaining_percent == 85.0
    assert by_win["5h"].freshness == "STALE"
    assert by_win["weekly"].remaining_percent == 60.0
    assert by_win["weekly"].freshness == "FRESH"


def test_packaged_concurrency_cap_enforced_on_fresh_store(tmp_path: Path) -> None:
    """Packaged max_concurrent applies automatically without owner pool_plans."""
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    verify_posture("codex", (), None, None, None)
    verify_posture("anthropic", (), None, None, None)
    policy = load_control_plane_policy()
    assert policy.plans.pool_plans == {}

    db = _fresh_db(tmp_path)
    handle_probe_result(
        db,
        harness="codex",
        windows=[
            ("5h", 80.0, now + timedelta(hours=3), "EXACT"),
            ("weekly", 80.0, now + timedelta(days=3), "EXACT"),
        ],
        now=now,
    )
    handle_probe_result(
        db,
        harness="claude",
        windows=[
            ("5h", 80.0, now + timedelta(hours=3), "EXACT"),
            ("weekly", 80.0, now + timedelta(days=3), "EXACT"),
        ],
        now=now,
    )
    states = effective_quota_states(db, now=now, run_probes=False)
    codex = WorkerCandidate(provider="codex", model="gpt-5.3-codex")
    claude = WorkerCandidate(provider="anthropic", model="claude-sonnet-4-5")

    ok = admit(
        codex,
        now=now,
        policy=policy,
        quota_states=states,
        running_counts={"chatgpt-plus": 1},
    )
    assert ok.admitted is True

    capped = admit(
        codex,
        now=now,
        policy=policy,
        quota_states=states,
        running_counts={"chatgpt-plus": 2},
    )
    assert capped.admitted is False
    assert capped.reason == "CONCURRENCY_LIMIT"
    assert capped.pool_id == "chatgpt-plus"
    assert capped.details == {"pool": "chatgpt-plus", "running": 2, "limit": 2}

    claude_capped = admit(
        claude,
        now=now,
        policy=policy,
        quota_states=states,
        running_counts={"claude-pro": 1},
    )
    assert claude_capped.admitted is False
    assert claude_capped.reason == "CONCURRENCY_LIMIT"
    assert claude_capped.pool_id == "claude-pro"
    assert claude_capped.details == {"pool": "claude-pro", "running": 1, "limit": 1}


def test_packaged_plan_windows_apply_on_fresh_store(tmp_path: Path) -> None:
    """Packaged weekly/monthly windows enforce red lines and evidence on fresh stores."""
    now = datetime(2026, 11, 19, 12, 0, tzinfo=UTC)
    verify_posture("codex", (), None, None, None)
    verify_posture("opencode/opencode-go/*", (), None, None, None)
    policy = load_control_plane_policy()
    assert policy.plans.pool_plans == {}

    db = _fresh_db(tmp_path)
    # chatgpt-plus: 5h healthy (90%), weekly below 15% red line (10%).
    handle_probe_result(
        db,
        harness="codex",
        windows=[
            ("5h", 90.0, now + timedelta(hours=3), "EXACT"),
            ("weekly", 10.0, now + timedelta(days=3), "EXACT"),
        ],
        now=now,
    )
    # opencode-go: missing monthly window -> must fail QUOTA_EVIDENCE_REQUIRED on monthly.
    handle_probe_result(
        db,
        harness="opencode-go",
        windows=[
            ("5h", 90.0, now + timedelta(hours=3), "EXACT"),
            ("weekly", 90.0, now + timedelta(days=3), "EXACT"),
        ],
        now=now,
    )
    states = effective_quota_states(db, now=now, run_probes=False)

    codex_dec = admit(
        WorkerCandidate(provider="codex", model="gpt-5.3-codex"),
        now=now,
        policy=policy,
        quota_states=states,
    )
    assert codex_dec.admitted is False
    assert codex_dec.reason == "RED_LINE_WEEKLY"
    assert codex_dec.binding_window == "weekly"

    go_missing = admit(
        WorkerCandidate(provider="opencode", model="opencode-go/model-x"),
        now=now,
        policy=policy,
        quota_states=states,
    )
    assert go_missing.admitted is False
    assert go_missing.reason == QUOTA_EVIDENCE_REQUIRED
    assert go_missing.binding_window == "monthly"

    # Supplying monthly completes packaged opencode-go windows and records all 3 draws.
    handle_probe_result(
        db,
        harness="opencode-go",
        windows=[("monthly", 90.0, now + timedelta(days=20), "EXACT")],
        now=now,
    )
    states_full = effective_quota_states(db, now=now, run_probes=False)
    go_ok = admit(
        WorkerCandidate(provider="opencode", model="opencode-go/model-x"),
        now=now,
        policy=policy,
        quota_states=states_full,
    )
    assert go_ok.admitted is True
    assert {(d.pool_id, d.window_id) for d in go_ok.draws} == {
        ("opencode-go", "5h"),
        ("opencode-go", "weekly"),
        ("opencode-go", "monthly"),
    }


def test_capacity_outputs_show_source_age_and_unknown_on_fresh_store(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """orch quota list, orch capacity show, and MCP capacity include source, age, unknown."""
    db_path = tmp_path / "cap.db"
    Database(db_path)

    # 1. Fresh empty store: all three outputs show source/age and "unknown".
    assert cli_main(["quota", "list", "--db", str(db_path)]) == 0
    quota_empty = capsys.readouterr().out
    assert "SOURCE" in quota_empty
    assert "AGE" in quota_empty
    assert "unknown" in quota_empty
    assert QUOTA_EVIDENCE_REQUIRED in quota_empty

    assert cli_main(["capacity", "show", "--db", str(db_path)]) == 0
    cap_empty = capsys.readouterr().out
    assert "Source" in cap_empty
    assert "Age" in cap_empty
    assert "unknown" in cap_empty

    assert cli_main(["capacity", "show", "--db", str(db_path), "--json"]) == 0
    cap_empty_json = json.loads(capsys.readouterr().out)
    assert cap_empty_json["pools"]
    assert all(
        p["source"] == "unknown" and p["age"] == "unknown" and p["remaining"] == "unknown"
        for p in cap_empty_json["pools"]
    )

    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    server = McpServer(
        ServerContext(
            db_path=db_path,
            questions_store=tmp_path / "questions.json",
            config_ledger_path=tmp_path / "config.jsonl",
            evidence=EvidenceRoots(roots=(orch,)),
            ledger=EventLedger(tmp_path / "events.jsonl"),
            lease=OrchestratorLease(tmp_path / "lease.json", holder="c8-test"),
        ),
        acquire_lease=False,
    )
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    mcp_empty = _mcp_call(server, "capacity", {})["capacity"]
    assert mcp_empty["pools"]
    assert all(
        p["source"] == "unknown" and p["age"] == "unknown" and p["remaining"] == "unknown"
        for p in mcp_empty["pools"]
    )

    # 2. Record a 2-hour-old PROBE for codex (5h + weekly); other packaged pools remain unknown.
    two_hours_ago = datetime.now(UTC) - timedelta(hours=2)
    db = Database(db_path)
    handle_probe_result(
        db,
        harness="codex",
        windows=[
            ("5h", 82.0, two_hours_ago + timedelta(hours=4), "EXACT"),
            ("weekly", 64.0, two_hours_ago + timedelta(days=4), "EXACT"),
        ],
        now=two_hours_ago,
    )

    assert cli_main(["quota", "list", "--db", str(db_path)]) == 0
    quota_out = capsys.readouterr().out
    assert "PROBE" in quota_out
    assert "2h" in quota_out
    assert "STALE" in quota_out
    assert "FRESH" in quota_out
    assert "unknown" in quota_out

    assert cli_main(["capacity", "show", "--db", str(db_path), "--json"]) == 0
    cap_json = json.loads(capsys.readouterr().out)
    by_key = {(p["pool_id"], p["window_id"]): p for p in cap_json["pools"]}
    assert ("chatgpt-plus", "ALL") not in by_key
    assert by_key[("chatgpt-plus", "5h")]["source"] == "PROBE"
    assert by_key[("chatgpt-plus", "5h")]["age"].startswith("2h")
    assert by_key[("chatgpt-plus", "5h")]["stale"] is True
    assert by_key[("chatgpt-plus", "weekly")]["source"] == "PROBE"
    assert by_key[("chatgpt-plus", "weekly")]["stale"] is False
    assert by_key[("claude-pro", "5h")]["source"] == "unknown"
    assert by_key[("claude-pro", "5h")]["age"] == "unknown"
    assert by_key[("claude-pro", "5h")]["remaining"] == "unknown"

    mcp_cap = _mcp_call(server, "capacity", {})["capacity"]
    mcp_by_key = {(p["pool_id"], p["window_id"]): p for p in mcp_cap["pools"]}
    assert mcp_by_key[("chatgpt-plus", "5h")]["source"] == "PROBE"
    assert mcp_by_key[("chatgpt-plus", "5h")]["age"].startswith("2h")
    assert isinstance(mcp_by_key[("chatgpt-plus", "5h")]["age_seconds"], int)
    assert mcp_by_key[("chatgpt-plus", "5h")]["age_seconds"] >= 7100
    assert mcp_by_key[("claude-pro", "5h")]["source"] == "unknown"
    assert mcp_by_key[("claude-pro", "5h")]["age"] == "unknown"
