"""Native account panels: numeric projection, safe commands and bounded cleanup."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from saberops.native_usage_panel import parse_panel, read_panel

CLAUDE = """Session
Total cost: $0.0000
Usage: 0 input, 0 output
Current session
0% used
Resets 8:40pm (Europe/Berlin)
Current week (all models)
███████████ 22% used
Resets Oct 9, 9am (Europe/Berlin)
What's contributing to your limits usage?
Approximate, based on local sessions
99% used
"""
AGY = """Models & Quota
Account: never-display@example.com
GEMINI MODELS
Models within this group: Gemini Flash, Gemini Pro
Weekly Limit Remaining
[████████] 99.41%
Refreshes in 91h 3m
Five Hour Limit Remaining
[████████] 100.00%
Quota available
CLAUDE AND GPT MODELS
Models within this group: Claude Opus, Claude Sonnet, GPT-OSS
Weekly Limit Remaining
[████████] 100.00%
Quota available
Five Hour Limit Remaining
[████████] 100.00%
Quota available
"""


def test_native_account_sections_preserve_precision_and_reset_labels() -> None:
    claude = parse_panel("anthropic", CLAUDE)
    assert [w["remaining"] for w in claude] == [100, 78]
    assert claude[1]["used"] == 22
    assert claude[0]["reset_text"] == "Resets 8:40pm (Europe/Berlin)"
    agy = parse_panel("antigravity", AGY)
    assert [w["remaining"] for w in agy] == [99.41, 100, 100, 100]
    assert agy[0]["reset_text"] == "Refreshes in 91h 3m"
    assert "never-display" not in json.dumps(agy)
    assert parse_panel("anthropic", CLAUDE.replace("22% used", "122% used"))[0]["remaining"] == 100
    assert parse_panel("anthropic", "Session\n0% used\nUsage: 0 input") == []
    assert len(parse_panel("anthropic", CLAUDE.replace("22% used", "Loading…"))) == 1
    assert parse_panel("antigravity", "Weekly Limit Remaining\n100%") == []


@pytest.mark.skipif(os.name != "posix", reason="Native PTY panels require POSIX")
@pytest.mark.parametrize("provider", ["anthropic", "antigravity"])
def test_native_panel_only_submits_usage_then_terminates(tmp_path: Path, provider: str) -> None:
    exe = tmp_path / "fake-cli"
    trace = tmp_path / "trace"
    pid = tmp_path / "pid"
    output = CLAUDE if provider == "anthropic" else AGY
    prompt = "Claude Code\n❯ Try a prompt" if provider == "anthropic" else "? for shortcuts\n> "
    exe.write_text(
        "#!/usr/bin/env python3\nimport os,sys,time\n"
        f"open({str(pid)!r},'w').write(str(os.getpid()))\n"
        f"print({prompt!r},flush=True)\n"
        "command=sys.stdin.readline()\n"
        f"open({str(trace)!r},'w').write(command)\n"
        "assert command.strip()=='/usage'\n"
        f"print('\\x1b[2J\\x1b[H'+{output!r},flush=True)\n"
        "time.sleep(60)\n"
    )
    exe.chmod(0o755)
    rows = read_panel(str(exe), provider)
    assert len(rows) == (2 if provider == "anthropic" else 4)
    assert trace.read_text().strip() == "/usage"
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


@pytest.mark.skipif(os.name != "posix", reason="Native PTY panels require POSIX")
def test_unknown_login_dialog_does_not_receive_prompt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("saberops.native_usage_panel.PANEL_TIMEOUT", 0.4)
    exe = tmp_path / "fake-cli"
    trace = tmp_path / "trace"
    pid = tmp_path / "pid"
    exe.write_text(
        "#!/usr/bin/env python3\nimport os,sys\n"
        f"open({str(pid)!r},'w').write(str(os.getpid()))\n"
        "print('Claude Code\\nChoose a login method:\\n❯ Sign in',flush=True)\n"
        "command=sys.stdin.readline()\n"
        f"open({str(trace)!r},'w').write(command)\n"
    )
    exe.chmod(0o755)
    with pytest.raises(TimeoutError):
        read_panel(str(exe), "anthropic")
    assert not trace.exists()
    with pytest.raises(ProcessLookupError):
        os.kill(int(pid.read_text()), 0)


