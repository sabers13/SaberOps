"""Offline numeric usage projections and bounded native metadata queries."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saberops.cli_usage import parse_limits, read_limits


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
