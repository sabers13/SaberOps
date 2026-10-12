"""Offline numeric usage projections and bounded native metadata queries."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from saberops.cli_usage import CliUsageService, _log_activity, parse_limits, read_limits
from saberops.db import Database
from saberops.models import DispatchStage, ModelCallUsage, Run, RunStatus, Tier


def test_limit_shapes_preserve_unknown_and_unlimited() -> None:
    payload = {
        "rateLimits": {"primary": {"usedPercent": 99}},
        "rateLimitsByLimitId": {
            "codex": {
                "primary": {"usedPercent": 35, "windowDurationMins": 300, "resetsAt": 2000000000},
                "secondary": {"usedPercent": None},
            }
        },
        "token": "never-display",
    }
    rows = parse_limits("codex", payload)
    assert len(rows) == 1 and rows[0]["remaining"] == 65
    assert "never-display" not in json.dumps(rows)
    assert parse_limits("codex", {"rateLimits": {"primary": {"usedPercent": 101}}}) == []
    assert parse_limits("codex", {"rateLimits": {"primary": {"usedPercent": False}}}) == []


def test_claude_stream_revisions_and_duplicate_forks_do_not_double_count(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()

    def event(output: int) -> dict[str, Any]:
        return {
            "type": "assistant",
            "message": {
                "id": "message-1",
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": output,
                    "cache_read_input_tokens": 50,
                },
            },
            "secret": "not-returned",
        }

    text = "\n".join(json.dumps(event(x)) for x in [2, 20])
    (root / "first.jsonl").write_text(text)
    (root / "fork.jsonl").write_text(text)
    result = _log_activity(root, "anthropic")
    assert result is not None
    assert result["input_tokens"] == 10 and result["output_tokens"] == 20
    assert "not-returned" not in json.dumps(result)


def test_codex_cumulative_session_totals_are_counted_once(tmp_path: Path) -> None:
    entries = [
        {
            "payload": {
                "type": "token_count",
                "info": {
                    "total_token_usage": {
                        "input_tokens": n,
                        "output_tokens": n // 2,
                        "cached_input_tokens": 0,
                    }
                },
            }
        }
        for n in (10, 100)
    ]
    (tmp_path / "session.jsonl").write_text("\n".join(json.dumps(d) for d in entries))
    result = _log_activity(tmp_path, "codex")
    assert result is not None and result["input_tokens"] == 100
    assert result["output_tokens"] == 50


def test_scan_limit_reports_partial_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("saberops.cli_usage.MAX_LOG_FILES", 1)
    for name in ("a", "b"):
        (tmp_path / f"{name}.jsonl").write_text(
            json.dumps(
                {
                    "payload": {
                        "type": "token_count",
                        "info": {"total_token_usage": {"input_tokens": 1, "output_tokens": 1}},
                    }
                }
            )
        )
    result = _log_activity(tmp_path, "codex")
    assert result is not None and "scan limit" in result["coverage"]


def test_usage_cache_retains_last_good_limit_with_error_timestamp(tmp_path: Path) -> None:
    clock = [1000.0]
    calls: list[str] = []

    def limits(exe: str, provider: str) -> list[dict[str, Any]]:
        calls.append(provider)
        if len(calls) > 1:
            raise TimeoutError
        return [{"label": "weekly", "remaining": 44}]

    service = CliUsageService(
        home=tmp_path,
        which=lambda exe: exe if exe == "codex" else None,
        limits=limits,
        clock=lambda: clock[0],
    )
    initial = service.snapshot()
    assert initial[0]["windows"] == [] and calls == []
    refreshed = service.snapshot(refresh=True)
    assert refreshed[0]["windows"][0]["remaining"] == 44
    stamp = refreshed[0]["checked_at"]
    service.snapshot(refresh=True)
    assert len(calls) == 1
    clock[0] += 61
    failed = service.snapshot(refresh=True)
    assert failed[0]["checked_at"] == stamp
    assert failed[0]["windows"][0]["remaining"] == 44
    assert failed[0]["limits_error"]


def test_read_only_databases_without_mutation(tmp_path: Path) -> None:
    service = CliUsageService(home=tmp_path, which=lambda exe: exe)
    cards = service.snapshot()
    assert all(not c["windows"] for c in cards)
    assert len(cards) == 6
    path = tmp_path / ".cline/data/db/sessions.db"
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sessions (metadata_json TEXT)")
        db.execute(
            "INSERT INTO sessions VALUES (?)",
            (
                json.dumps(
                    {
                        "usage": {"inputTokens": 10},
                        "aggregateUsage": {
                            "inputTokens": 15,
                            "outputTokens": 5,
                            "cacheReadTokens": 2,
                        },
                    }
                ),
            ),
        )
    before = path.read_bytes()
    cline = next(c for c in service.snapshot(force=True) if c["provider"] == "cline")
    assert cline["activity"]["input_tokens"] == 15  # no usage + aggregate double count
    assert path.read_bytes() == before


def test_native_rpc_is_metadata_only_and_closes_process(tmp_path: Path) -> None:
    provider = "codex"
    exe = tmp_path / "fake-cli"
    trace = tmp_path / "trace.jsonl"
    script = """#!/usr/bin/env python3
import sys,json
for _ in range(4):
 raw=sys.stdin.buffer.readline()
 if not raw:break
 d=json.loads(raw)
 with open(TRACE,'a') as f:f.write(json.dumps(d)+'\\n')
 if 'id' not in d:continue
 r={'id':d['id'],'result':{}}
 if d['id']==2:
  r['result']={'rateLimits':{'primary':{'usedPercent':45}}}
 body=json.dumps(r).encode()
 sys.stdout.buffer.write(body+b'\\n')
 sys.stdout.buffer.flush()
"""
    exe.write_text(script.replace("TRACE", repr(str(trace))))
    exe.chmod(0o755)
    limits = read_limits(str(exe), provider)
    assert limits[0]["remaining"] == 55
    methods = [json.loads(line)["method"] for line in trace.read_text().splitlines()]
    assert methods == ["initialize", "initialized", "account/rateLimits/read"]


def test_usage_refresh_fragment_and_manual_quota_authority(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from saberops.service import OrchestratorService

    for env in ("XDG_STATE_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(env, str(tmp_path / env))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    service = CliUsageService(
        home=tmp_path,
        which=lambda exe: exe if exe == "codex" else None,
        limits=lambda exe, provider: [
            {
                "label": "weekly",
                "remaining": 70,
                "duration": 10080,
                "reset_at": None,
                "unlimited": False,
            }
        ],
    )
    cards = service.snapshot(refresh=True, force=True)
    codex = next(card for card in cards if card["provider"] == "codex")
    assert codex["windows"][0]["remaining"] == 70
    db_path = tmp_path / "app.db"
    OrchestratorService(db=Database(db_path)).set_quota(
        provider="codex", pool="weekly", remaining_percent=10, reset_at=None
    )
    state = Database(db_path).list_quota()
    assert state[0].remaining_percent == 10
    assert state[0].routing_state.value == "NORMAL"


def test_recorded_provider_totals_keep_missing_tokens_unknown(tmp_path: Path) -> None:
    db = Database(tmp_path / "app.db")
    db.create_run(
        Run(
            id="run1",
            task="usage test",
            target_repo=str(tmp_path),
            base_commit="abc",
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.PENDING,
            created_at="2026-10-05T00:00:00Z",
        )
    )
    db.create_model_call_usage(
        ModelCallUsage(
            id="call1",
            run_id="run1",
            association_id="association1",
            attempt_id=None,
            review_id=None,
            stage=DispatchStage.IMPLEMENTATION,
            tier=Tier.T1,
            provider="antigravity",
            model="gemini",
            prompt_bytes=0,
            transport="DIRECT_CLI",
            input_tokens=None,
            output_tokens=None,
            cached_tokens=None,
            total_tokens=None,
            cache_read_tokens=None,
            cache_write_tokens=None,
            reasoning_tokens=None,
            usage_source="unknown",
            raw_usage_json=None,
            created_at="2026-10-05T00:00:00Z",
        )
    )
    totals = db.aggregate_provider_usage()
    assert totals[0]["calls"] == 1 and totals[0]["unknown_calls"] == 1
    assert totals[0]["input_tokens"] is None


def test_native_timeout_terminates_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os
    import time

    monkeypatch.setattr("saberops.cli_usage.RPC_TIMEOUT", 0.5)
    exe = tmp_path / "hanging-cli"
    pid_path = tmp_path / "pid"
    exe.write_text(
        "#!/usr/bin/env python3\nimport os,time\n"
        f"open({str(pid_path)!r},'w').write(str(os.getpid()))\n"
        "time.sleep(60)\n"
    )
    exe.chmod(0o755)
    started = time.monotonic()
    with pytest.raises(TimeoutError):
        read_limits(str(exe), "codex")
    assert time.monotonic() - started < 3
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid_path.read_text()), 0)
