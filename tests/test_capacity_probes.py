"""M2-1 probe tests: fake CLIs, hermetic env, rate limit, zero/clear."""

from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from saberops.capacity.probes import (
    handle_probe_result,
    run_due_probes,
)
from saberops.capacity.store import list_active_exclusions
from saberops.control_plane.quota_policy import QuotaPoolPolicy
from saberops.db import Database


def _db(tmp_path: Path) -> Database:
    return Database(tmp_path / "probes.db")


def _policies() -> dict[str, QuotaPoolPolicy]:
    return {
        "codex-pool": QuotaPoolPolicy(pool_id="codex-pool", members=("codex",)),
        "claude-pool": QuotaPoolPolicy(pool_id="claude-pool", members=("anthropic",)),
        "go-pool": QuotaPoolPolicy(pool_id="go-pool", members=("opencode",)),
    }


class _FakeProbe:
    def __init__(
        self, harness: str, windows: list[tuple[str, float, object, str]],
        record: dict[str, object],
    ) -> None:
        self.harness = harness
        self._windows = windows
        self._record = record

    def read(self, now: datetime) -> list[tuple[str, float, object, str]]:
        self._record["called"] = True
        return self._windows


def test_probe_no_prompt_in_argv_or_stdin() -> None:
    # Contract: Codex probe uses app-server + account/rateLimits/read;
    # Claude probe only submits /usage; Go probe uses the plugin path and
    # `models` enumeration (no inference session).
    import inspect

    import saberops.cli_usage as cli_usage
    import saberops.native_usage_panel as panel
    import saberops.opencode_go_usage as go

    codex_src = inspect.getsource(cli_usage._native_limits)
    assert "app-server" in codex_src
    assert "account/rateLimits/read" in inspect.getsource(cli_usage)
    assert '"models"' not in codex_src  # never a model dispatch
    panel_src = inspect.getsource(panel.read_panel)
    assert "/usage" in panel_src
    go_src = inspect.getsource(go.read_go_limits)
    assert "plugin" in go_src
    assert '"models"' in go_src or "'models'" in go_src or "models" in go_src


def test_child_env_is_hermetic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Fake codex binary records its environment; parent secrets must not arrive.
    bindir = tmp_path / "bin"
    bindir.mkdir()
    record = bindir / "env.json"
    argv_record = bindir / "argv.json"
    fake = bindir / "codex"
    fake.write_text(
        "#!/bin/sh\n"
        f'echo "$0 $@" > "{argv_record}"\n'
        f'python3 -c "import json,os; json.dump(dict(os.environ), open(\'{record}\', \'w\'))"\n'
        'echo \'{"jsonrpc":"2.0","id":1,"result":{}}\'\n'
        'echo \'{"jsonrpc":"2.0","id":2,"result":{"rateLimits":{}}}\'\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("ANTHROPIC_API_KEY", "parent-secret-anthropic")
    monkeypatch.setenv("OPENAI_API_KEY", "parent-secret-openai")
    from saberops.cli_usage import read_limits

    try:
        read_limits(str(fake), "codex")
    except Exception:
        pass
    assert record.is_file()
    child = json.loads(record.read_text())
    assert "ANTHROPIC_API_KEY" not in child
    assert "OPENAI_API_KEY" not in child
    argv = argv_record.read_text()
    assert "app-server" in argv
    assert "Do work" not in argv


def test_rate_limit_persists_across_store_instances(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    record: dict[str, Any] = {}
    fake = _FakeProbe("codex", [("weekly", 50.0, None, "EXACT")], record)
    first = run_due_probes(
        db, now=now, probes={"codex": fake}, pool_policies=_policies()
    )
    assert first["codex"] == "ran"
    record.clear()
    # Second run 1 min later with a fresh Database handle on the same file.
    db2 = Database(tmp_path / "probes.db")
    second = run_due_probes(
        db2, now=now + timedelta(minutes=1),
        probes={"codex": _FakeProbe("codex", [("weekly", 50.0, None, "EXACT")], record)},
        pool_policies=_policies(),
    )
    assert second["codex"] == "rate-limited"
    assert "called" not in record


def test_probe_failure_no_clear_no_raise(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    from saberops.capacity.store import upsert_exclusion

    upsert_exclusion(
        db, pool_id="codex-pool", window_id="weekly", reason="QUOTA_EXHAUSTED",
        until=now + timedelta(hours=1), until_confidence="EXACT",
        source="codex", now=now,
    )

    class _Fail:
        harness = "codex"

        def read(self, now: datetime) -> list[Any]:
            raise RuntimeError("boom")

    outcomes = run_due_probes(
        db, now=now + timedelta(minutes=11), probes={"codex": _Fail()},
        pool_policies=_policies(),
    )
    assert outcomes["codex"] == "failed"
    active = list_active_exclusions(db, now=now + timedelta(minutes=11))
    assert len(active) == 1


def test_zero_creates_exclusion_positive_clears(tmp_path: Path) -> None:
    db = _db(tmp_path)
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    reset = now + timedelta(hours=1)
    handle_probe_result(
        db, harness="codex", windows=[("weekly", 0.0, reset, "EXACT")],
        now=now, pool_policies=_policies(),
    )
    active = list_active_exclusions(db, now=now)
    assert len(active) == 1
    assert active[0]["reason"] == "PROBE_ZERO"
    assert active[0]["pool_id"] == "codex-pool"
    # Positive reading clears that window.
    handle_probe_result(
        db, harness="codex", windows=[("weekly", 80.0, None, "EXACT")],
        now=now + timedelta(minutes=11), pool_policies=_policies(),
    )
    assert list_active_exclusions(db, now=now + timedelta(minutes=11)) == []
    # Probe observations + red-line rows are written.
    assert db.list_quota() != []


def test_no_policies_claude_zero_denies_anthropic(tmp_path: Path) -> None:
    """No policies: a Claude 0% probe must exclude provider anthropic."""
    from saberops.capacity.snapshot import effective_quota_states
    from saberops.control_plane.quota_policy import quota_decision
    from saberops.models import WorkerCandidate

    db = _db(tmp_path)
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    handle_probe_result(
        db, harness="claude", windows=[("weekly", 0.0, now + timedelta(hours=1), "EXACT")],
        now=now, pool_policies=None,
    )
    states = effective_quota_states(db, now=now, run_probes=False)
    cand = WorkerCandidate(provider="anthropic", model="sonnet-4")
    decision = quota_decision(cand, states)
    assert decision.eligible is False
    assert decision.reason == "QUOTA_EXCLUDED_UNTIL_RESET"


def test_no_policies_go_zero_denies_opencode(tmp_path: Path) -> None:
    """No policies: an opencode-go 0% probe must exclude provider opencode."""
    from saberops.capacity.snapshot import effective_quota_states
    from saberops.control_plane.quota_policy import quota_decision
    from saberops.models import WorkerCandidate

    db = _db(tmp_path)
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    handle_probe_result(
        db, harness="opencode-go",
        windows=[("weekly", 0.0, now + timedelta(hours=1), "EXACT")],
        now=now, pool_policies=None,
    )
    states = effective_quota_states(db, now=now, run_probes=False)
    cand = WorkerCandidate(provider="opencode", model="opencode-go/model-x")
    decision = quota_decision(cand, states)
    assert decision.eligible is False
    assert decision.reason == "QUOTA_EXCLUDED_UNTIL_RESET"
