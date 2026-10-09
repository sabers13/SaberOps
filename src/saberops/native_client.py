"""Launch only a native client for an owner-hosted, guarded provider session."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    # Desktop/tmux servers may retain an older PYTHONPATH or installed release.
    # Use the same package tree as the execution owner's UI process.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saberops.native_access import interactive_environment
from saberops.native_console import display
from saberops.session_control import SessionControls
from saberops.session_journal import SessionJournal

LIVE_MODES = ("ACTIVE", "REQUESTED", "MANUAL", "RESUMING")


def show_finished(data: dict[str, object], controls: SessionControls) -> int:
    sid = str(data["id"])
    journal = SessionJournal(controls.path.parent)
    outcome = journal.view(sid, limit=1)["outcome"] or {}
    session = controls.get(sid) or {}
    status = outcome.get("status", "UNKNOWN")
    role = str(session.get("role", "agent")).capitalize()
    labels = {
        "SUCCESS": "finished successfully",
        "FAILED": "failed",
        "TIMEOUT": "timed out",
        "INTERRUPTED": "was interrupted",
        "UNCERTAIN": "has an uncertain result",
        "UNKNOWN": "has no confirmed final result",
    }
    print(f"\nSaberOps · {role} {labels.get(status, status)} ({status})", flush=True)
    turn = journal.last_turn(sid)
    response = turn.get("response")
    if not response:
        metadata = Path(str(data.get("descriptor", ""))).with_suffix(".json")
        if metadata.is_file():
            response = json.loads(metadata.read_text()).get("response")
    if response:
        print("\n" + display(str(response)), flush=True)
    run_id = data.get("run_id") or session.get("owner")
    db_path = Path(str(data.get("run_db_path") or controls.path.parent.parent / "orchestrator.db"))
    if db_path.is_file() and run_id:
        with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as conn:
            row = conn.execute(
                "SELECT status,result_summary FROM runs WHERE id=?", (run_id,)
            ).fetchone()
        if row:
            print("\nSaberOps run at the time of this summary: " + display(row[0]), flush=True)
            if row[1]:
                print(display(row[1]), flush=True)
    if outcome.get("quiescent"):
        print(
            "\nProvider execution has stopped. Gate and review results belong to the SaberOps run.",
            flush=True,
        )
    else:
        print(
            "\nThis terminal client is disconnected. Check SaberOps for execution status.",
            flush=True,
        )
    if sys.stdin.isatty():
        try:
            input("Press Enter to close this finished session… ")
        except (EOFError, KeyboardInterrupt):
            pass
    return 0 if status == "SUCCESS" else 1


def stop_client(client: subprocess.Popen[bytes]) -> None:
    """Only the client we launched is stopped; never the provider server or owner."""
    if client.poll() is None:
        client.terminate()
        try:
            client.wait(timeout=3)
        except subprocess.TimeoutExpired:
            client.kill()
            client.wait(timeout=3)


def main() -> int:
    data = json.loads(Path(sys.argv[1]).read_text())
    data["descriptor"] = sys.argv[1]
    controls = SessionControls(Path(data["controls_root"]))
    session = controls.get(data["id"])
    if session is None or session["mode"] not in LIVE_MODES:
        print("This session is no longer live. Open its saved output in SaberOps.")
        try:
            return show_finished(data, controls)
        except (OSError, sqlite3.Error, ValueError):
            print("Final evidence could not be read. Check SaberOps for execution status.")
            return 1
    if data["provider"] != "opencode":
        print("Native attachment is not supported for this session.")
        return 1
    env = interactive_environment(dict(
        os.environ, OPENCODE_SERVER_PASSWORD=data["password"], OPENCODE_SERVER_USERNAME="opencode"
    ))
    print(
        "SaberOps native session. Sending a message requests takeover and holds automation.\n"
        "Closing this terminal leaves the agent and its execution owner running."
    )
    terminal_settings = None
    if sys.stdin.isatty() and os.name == "posix":
        import termios

        terminal_settings = termios.tcgetattr(sys.stdin.fileno())
    client = subprocess.Popen(
        [
            data["binary"],
            "attach",
            data["url"],
            "--pure",
            "--session",
            data["session"],
            "--dir",
            data["directory"],
        ],
        env=env,
    )
    journal = SessionJournal(controls.path.parent)
    ended_at: float | None = None
    ended = False
    try:
        while True:
            session = controls.get(data["id"])
            outcome = journal.view(data["id"], limit=1)["outcome"]
            if outcome:
                ended = True
                break
            if session is None or session["mode"] not in LIVE_MODES:
                ended_at = ended_at or time.monotonic()
                # DONE precedes shutdown and its durable outcome. Do not infer success early.
                if (
                    session is None
                    or session["mode"] in ("UNKNOWN", "INTERRUPTED")
                    or time.monotonic() - ended_at > 22
                ):
                    ended = True
                    break
            if client.poll() is not None:
                if ended_at is not None:
                    time.sleep(0.1)
                    continue
                return client.returncode or 0
            time.sleep(0.2)
    except (OSError, sqlite3.Error):
        ended = True
    except KeyboardInterrupt:
        return 130
    finally:
        stop_client(client)
        if terminal_settings is not None:
            import termios

            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, terminal_settings)
        if sys.stdout.isatty():
            # Restore the shell screen/cursor even when the vendor client was unresponsive.
            print("\033[0m\033[?25h\033[?1049l", end="", flush=True)
    if ended:
        try:
            return show_finished(data, controls)
        except (OSError, sqlite3.Error, ValueError):
            print("Session ended; final evidence could not be read. Check SaberOps.")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
