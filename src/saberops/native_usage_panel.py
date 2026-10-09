"""Read native /usage panels without submitting an inference prompt.

The terminal emulator is internal: only allowlisted numeric quota fields and
their displayed reset labels leave this module. Never return account identities,
terminal output, or credentials. No hooks or status-line configuration is installed.
"""

from __future__ import annotations

import os
import re
import signal
import subprocess
import tempfile
import time
from typing import Any

import pyte

PANEL_TIMEOUT = 20.0
MAX_PANEL_BYTES = 1_000_000


def parse_panel(provider: str, text: str) -> list[dict[str, Any]]:
    """Parse the displayed account sections, never session token/cost totals."""
    lines = [line.strip() for line in text.splitlines()]
    rows: list[dict[str, Any]] = []
    group = ""
    starts: list[tuple[int, str, float]] = []
    for index, line in enumerate(lines):
        if provider == "anthropic":
            if re.fullmatch(r"Current session|Current week(?: \([^\n]+\))?", line):
                starts.append((index, line, 300 if line == "Current session" else 10080))
        elif provider == "antigravity":
            if re.fullmatch(r"[A-Z][A-Z &]+ MODELS", line):
                group = line.removesuffix(" MODELS")
            if group and line in ("Weekly Limit Remaining", "Five Hour Limit Remaining"):
                starts.append(
                    (index, f"{group} · {line}", 10080 if line.startswith("Weekly") else 300)
                )
    for position, (index, label, duration) in enumerate(starts):
        end = starts[position + 1][0] if position + 1 < len(starts) else index + 6
        section = lines[index + 1 : min(end, index + 7)]
        # CLI local breakdowns/credits can also contain percentages. They are
        # separate sections and must never fill in missing account evidence.
        boundary = next(
            (
                i
                for i, line in enumerate(section)
                if line.startswith(
                    ("What's contributing", "Extra usage", "Prompt cache", "AI Credits")
                )
            ),
            len(section),
        )
        section = section[:boundary]
        pattern = (
            r"\b(\d+(?:\.\d+)?)%\s+used\b" if provider == "anthropic" else r"\b(\d+(?:\.\d+)?)%"
        )
        match = next((m for line in section if (m := re.search(pattern, line))), None)
        if match is None or not 0 <= float(match[1]) <= 100:
            continue
        value = float(match[1])
        reset_text = next(
            (
                line
                for line in section
                if re.fullmatch(
                    r"(?:Resets|Refreshes in) [\w :,.()/+−-]{1,100}|Quota available", line
                )
            ),
            None,
        )
        rows.append(
            {
                "label": label,
                "remaining": 100 - value if provider == "anthropic" else value,
                "used": value if provider == "anthropic" else None,
                "duration": duration,
                "reset_at": None,
                "reset_text": reset_text,
                "unlimited": False,
            }
        )
    return rows


def read_panel(executable: str, provider: str) -> list[dict[str, Any]]:
    """A bounded native CLI session; the only command submitted is /usage.

    Trust is accepted only for a fresh empty temporary directory created here,
    never for the user's repository. Unknown onboarding/login dialogs stop the
    query. CLI children are terminated together, including on parsing failure.
    """
    if os.name != "posix" or provider not in ("anthropic", "antigravity"):
        raise ValueError("Native usage panel is unavailable on this platform")
    import fcntl
    import pty
    import select
    import struct
    import termios

    argv = [executable]
    if provider == "anthropic":
        argv += [
            "--tools",
            "",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--settings",
            '{"disableAllHooks":true}',
        ]
    master, slave = pty.openpty()
    process: subprocess.Popen[bytes] | None = None
    try:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 80, 140, 0, 0))
        with tempfile.TemporaryDirectory(prefix="saberops-usage-panel-") as cwd:
            process = subprocess.Popen(
                argv,
                stdin=slave,
                stdout=slave,
                stderr=slave,
                cwd=cwd,
                env={**os.environ, "TERM": "xterm-256color"},
                start_new_session=True,
            )
            os.close(slave)
            slave = -1
            screen = pyte.Screen(140, 80)
            stream = pyte.ByteStream(screen)
            deadline = time.monotonic() + PANEL_TIMEOUT
            total = 0
            trusted = False
            submitted = False
            candidate: list[dict[str, Any]] = []
            stable_at = 0.0
            while time.monotonic() < deadline:
                if not select.select([master], [], [], 0.1)[0]:
                    if candidate and time.monotonic() - stable_at > 0.8:
                        return candidate
                    if process.poll() is not None:
                        break
                    continue
                try:
                    raw = os.read(master, 65536)
                except OSError:
                    break
                total += len(raw)
                if total > MAX_PANEL_BYTES:
                    raise ValueError("Native usage panel exceeds size limit")
                stream.feed(raw)
                text = "\n".join(screen.display)
                if not trusted and (
                    "Do you trust the contents of this project?" in text
                    or "Yes, I trust this folder" in text
                    and "Quick safety check:" in text
                ):
                    # Claude defaults to No; Antigravity defaults to Yes.
                    os.write(master, b"\x1b[B\r" if provider == "anthropic" else b"\r")
                    trusted = True
                    continue
                if not submitted:
                    prompt = (
                        "Claude Code" in text and bool(re.search(r"(?m)^❯[ \u00a0]+Try\b", text))
                        if provider == "anthropic"
                        else "? for shortcuts" in text and bool(re.search(r"(?m)^>\s*$", text))
                    )
                    if prompt:
                        os.write(master, b"/usage\r")
                        submitted = True
                    continue
                updated = parse_panel(provider, text)
                if updated and updated != candidate:
                    candidate = updated
                    stable_at = time.monotonic()
                elif candidate and time.monotonic() - stable_at > 0.8:
                    return candidate
            raise TimeoutError("Native /usage panel did not return account limits")
    finally:
        if process is not None:
            # Terminate children even if the main CLI has already exited.
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
        os.close(master)
        if slave >= 0:
            os.close(slave)
