"""Terminal-side supervisor for exclusive native resume and saved-session access."""

from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from saberops.native_access import claim_client, native_argv, native_environment, update_client
from saberops.ownership import ProcessIdentity, terminate_worker_group_fenced
from saberops.session_control import ControlConflict, SessionControls
from saberops.session_journal import SessionJournal


class HistoryMirror:
    """Mirror provider-authored messages only, never guesses from terminal keys."""

    def __init__(self, data: dict[str, Any], controls: SessionControls) -> None:
        self.data, self.controls = data, controls
        self.seen: set[str] = set()
        self.prompt: tuple[str, str] | None = None
        self.scan(baseline=True)

    def paths(self) -> list[Path]:
        sid, home = self.data["native_id"], Path.home()
        if self.data["provider"] in ("anthropic", "claude-code"):
            return list((home / ".claude/projects").glob("*/" + sid + ".jsonl"))
        if self.data["provider"] == "codex":
            return list((home / ".codex/sessions").glob("*/*/*/rollout-*" + sid + ".jsonl"))
        if self.data["provider"] == "copilot":
            return [home / ".copilot/session-state" / sid / "events.jsonl"]
        return []

    @staticmethod
    def text(value: object) -> str:
        if isinstance(value, str):
            return value
        if isinstance(value, list):
            return "\n".join(
                str(part.get("text", ""))
                for part in value
                if isinstance(part, dict)
                and part.get("type") in ("text", "input_text", "output_text")
            )
        return ""

    def scan(self, *, baseline: bool = False) -> None:
        import hashlib

        for path in self.paths():
            if not path.is_file() or path.stat().st_size > 16 * 1024 * 1024:
                continue
            for line in path.read_text(errors="replace").splitlines():
                key = hashlib.sha256(line.encode()).hexdigest()
                if key in self.seen:
                    continue
                self.seen.add(key)
                if baseline:
                    continue
                try:
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        continue
                    kind = event.get("type")
                    payload = (
                        event.get("payload") or event.get("data") or event.get("message") or {}
                    )
                    if not isinstance(payload, dict):
                        continue
                    role = kind if kind in ("user", "assistant") else payload.get("type") or kind
                    text = self.text(payload.get("content") or payload.get("message"))
                    if role in ("user", "user_message", "user.message") and text:
                        if (event.get("isMeta") or text.lstrip().startswith(
                                ("<channel ", "<command-name>", "<local-command-stdout>",
                                 "SABEROPS_APP_MESSAGE ", "SABEROPS_CONTEXT "))):
                            self.prompt = None
                            continue
                        self.prompt = ("native:" + self.data["token"] + ":" + key, text[:32000])
                        if self.data.get("shared_chat"):
                            from saberops.native_shared_chat import NativeSharedChat

                            NativeSharedChat(self.controls).track_terminal(
                                self.data, self.prompt[0], self.prompt[1])
                        self.controls.sync_chat(
                            {
                                "session_id": self.data["id"],
                                "id": self.prompt[0],
                                "text": self.prompt[1],
                            }
                        )
                    elif (
                        role in ("assistant", "agent_message", "assistant.message")
                        and text
                        and self.prompt
                    ):
                        from saberops.chat import ChatStore

                        control = self.controls.get(self.data["id"]) or {}
                        if control.get("chat_path") and control.get("chat_id"):
                            ChatStore(Path(control["chat_path"])).record_native_reply(
                                control["chat_id"],
                                self.data["token"] + ":" + key,
                                text,
                                role=control["role"],
                            )
                except (ValueError, OSError):
                    continue


def main() -> int:
    import fcntl
    import pty
    import struct
    import termios
    import tty

    data: dict[str, Any] = json.loads(Path(sys.argv[1]).read_text())
    data["descriptor"] = sys.argv[1]
    controls = SessionControls(Path(data["controls_root"]))
    journal = SessionJournal(controls.path.parent)
    child_identity: ProcessIdentity | None = None
    pid: int | None = None
    fd: int | None = None
    settings: list[Any] | None = None
    code, error = 1, ""
    identity = ProcessIdentity.for_pid(os.getpid())
    if identity is None:
        print("Terminal process identity could not be confirmed.", flush=True)
        return 1
    try:
        claim_client(controls, data, identity)
    except ControlConflict as exc:
        print(str(exc), flush=True)
        return 1
    try:
        print("SaberOps · opening the exact saved provider session.", flush=True)
        if data["access_mode"] == "handoff":
            print(
                "Waiting for the execution owner to pause safely. "
                "Automation remains held until you return it in the app.",
                flush=True,
            )
            deadline = time.monotonic() + 30
            while True:
                control = controls.get(data["id"]) or {}
                if control.get("mode") == "MANUAL":
                    break
                if control.get("mode") != "REQUESTED" or time.monotonic() > deadline:
                    raise ControlConflict(
                        "The owner did not confirm a safe handoff. Refresh SaberOps."
                    )
                time.sleep(0.1)
        argv, env = native_argv(data), native_environment(data)
        mirror = HistoryMirror(data, controls)
        terminal = sys.stdin.isatty()
        initial_size = (
            fcntl.ioctl(sys.stdin.fileno(), termios.TIOCGWINSZ, bytes(8))
            if terminal else struct.pack("HHHH", 24, 100, 0, 0)
        )
        read_end, write_end = os.pipe()
        pid, fd = pty.fork()
        if pid == 0:
            os.close(write_end)
            # No provider execution until the supervisor has persisted child
            # identity. Parent death before publication closes the pipe.
            if os.read(read_end, 1) != b"x":
                os._exit(1)
            os.close(read_end)
            os.chdir(data["directory"])
            os.execvpe(argv[0], argv, env)
        os.close(read_end)
        # Set dimensions while the child is still fenced. TUIs read their size
        # during startup, before they can handle later resize notifications.
        fcntl.ioctl(fd, termios.TIOCSWINSZ, initial_size)
        child_identity = ProcessIdentity.for_pid(pid)
        if not child_identity:
            raise ControlConflict("Native process identity could not be confirmed.")
        update_client(controls, data, "ACTIVE", child=child_identity)
        if (
            data["access_mode"] == "handoff"
            and (controls.get(data["id"]) or {}).get("mode") != "MANUAL"
        ):
            os.close(write_end)
            raise ControlConflict("Manual control ended before the native CLI could start.")
        os.write(write_end, b"x")
        os.close(write_end)
        journal.append(
            data["id"],
            "native.client.opened",
            {
                "access_mode": data["access_mode"],
                "provider": data["provider"],
                "native_id": data["native_id"],
            },
        )
        def resize(*_: object) -> None:
            if terminal and fd is not None:
                size = fcntl.ioctl(
                    sys.stdin.fileno(), termios.TIOCGWINSZ, struct.pack("HHHH", 24, 100, 0, 0)
                )
                fcntl.ioctl(fd, termios.TIOCSWINSZ, size)

        resize()
        signal.signal(signal.SIGWINCH, resize)
        if terminal:
            settings = termios.tcgetattr(sys.stdin.fileno())
            tty.setraw(sys.stdin.fileno())

        def stopped(*_: object) -> None:
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, stopped)
        signal.signal(signal.SIGHUP, stopped)
        log = Path(data["log"])
        with log.open("ab") as output:
            last_scan = 0.0
            while True:
                if (
                    data["access_mode"] == "handoff"
                    and (controls.get(data["id"]) or {}).get("mode") != "MANUAL"
                ):
                    error = "Manual control ended; this native client has been stopped."
                    break
                ready, _, _ = select.select(
                    [fd, *([sys.stdin.fileno()] if terminal else [])], [], [], 0.2
                )
                if fd in ready:
                    try:
                        chunk = os.read(fd, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    os.write(sys.stdout.fileno(), chunk)
                    output.write(chunk)
                    output.flush()
                    journal.append(
                        data["id"],
                        "native.client.output",
                        {"text": chunk.decode("utf-8", errors="replace"), "token": data["token"]},
                    )
                if terminal and sys.stdin.fileno() in ready:
                    chunk = os.read(sys.stdin.fileno(), 65536)
                    if not chunk:
                        break
                    os.write(fd, chunk)
                if time.monotonic() - last_scan > 1:
                    mirror.scan()
                    last_scan = time.monotonic()
        mirror.scan()
        waited, status = os.waitpid(pid, os.WNOHANG)
        deadline = time.monotonic() + 1
        while not waited and not error and time.monotonic() < deadline:
            time.sleep(0.01)
            waited, status = os.waitpid(pid, os.WNOHANG)
        code = os.waitstatus_to_exitcode(status) if waited else 1
        if code and not error:
            error = f"Provider CLI exited with code {code}."
    except KeyboardInterrupt:
        code, error = 130, "Terminal client was closed or interrupted."
    except (OSError, ValueError) as exc:
        error = str(exc)
    finally:
        safe = (
            terminate_worker_group_fenced(json.dumps(child_identity.to_dict()))[0]
            if child_identity
            else pid is None
        )
        if pid:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass
        if fd is not None:
            os.close(fd)
        if settings is not None:
            termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, settings)
        if sys.stdout.isatty():
            print("\033[0m\033[?25h\033[?1049l", end="", flush=True)
        if data.get("shared_chat"):
            from saberops.native_shared_chat import NativeSharedChat

            NativeSharedChat(controls).closed(
                data, "The native CLI closed before its reply was confirmed. "
                      "Your message is saved; it was not automatically resent.")
        update_client(
            controls,
            data,
            "DONE" if safe and not error and code == 0 else "FAILED" if safe else "ACTIVE",
            error=error or ("Native process shutdown could not be confirmed." if not safe else ""),
        )
        print(
            "\n" + (error or "Native client closed. The recorded run result is unchanged."),
            flush=True,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
