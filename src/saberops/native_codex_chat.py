"""A private Codex app-server shared by the native TUI and the durable app inbox.

This client runs inside the native supervisor's fenced process group. It never
uses the user's global daemon, copies credentials, forks history, or retries a
turn whose delivery is uncertain. The execution owner remains separate.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from websockets.sync.client import unix_connect

from saberops.native_access import native_environment
from saberops.native_channel import INSTRUCTIONS
from saberops.native_shared_chat import NativeSharedChat
from saberops.session_control import ControlConflict, SessionControls


def server_argv(binary: str, descriptor: str, data: dict[str, Any], socket: str) -> list[str]:
    """Only native ChatGPT login; the channel has no API credentials."""
    config = {
        "forced_login_method": "chatgpt",
        "sandbox_mode": "read-only" if data.get("read_only") else "workspace-write",
        "approval_policy": "on-request",
        "model": data["model"],
        "mcp_servers.saberops.command": sys.executable,
        "mcp_servers.saberops.args": [
            "-I", str(Path(__file__).with_name("native_channel.py")), descriptor,
        ],
        "mcp_servers.saberops.required": True,
    }
    args = [binary, "app-server", "--listen", "unix://" + socket]
    for key, value in config.items():
        args += ["-c", key + "=" + json.dumps(value)]
    return args


class InboxConnection:
    """One subscribed RPC connection; TUI input remains provider-authored."""

    def __init__(self, socket: str, data: dict[str, Any], stop: threading.Event) -> None:
        self.socket, self.data, self.stop = socket, data, stop
        self.bridge = NativeSharedChat(SessionControls(Path(data["controls_root"])))
        self.sequence = 0
        self.busy = True
        self.current: str | None = None
        self.error = ""
        self.ready = threading.Event()

    def request(self, connection: Any, method: str, params: dict[str, Any]) -> int:
        self.sequence += 1
        connection.send(json.dumps({"id": self.sequence, "method": method, "params": params}))
        return self.sequence

    def handle(self, connection: Any, event: dict[str, Any]) -> None:
        # Approvals are answered by the native TUI. This observer never approves.
        method, params = event.get("method"), event.get("params") or {}
        if params.get("threadId") != self.data["native_id"]:
            return
        if method == "turn/started":
            self.busy = True
        elif method == "turn/completed":
            self.busy = False
            if self.current:
                channel = self.bridge.channel(self.data["id"])
                if channel:
                    with self.bridge.controls.connect() as db:
                        row = db.execute("SELECT state FROM native_chat_inbox WHERE id=?",
                                         (self.current,)).fetchone()
                    if row and row["state"] == "DELIVERED":
                        raise ControlConflict(
                            "Codex ended its turn without confirming the app reply. "
                            "The message is saved and was not resent."
                        )
                self.current = None

    def run(self) -> None:
        try:
            deadline = time.monotonic() + 25
            while not Path(self.socket).exists():
                if self.stop.wait(0.05) or time.monotonic() > deadline:
                    raise ControlConflict("The private Codex server did not start.")
            with unix_connect(self.socket, proxy=None, open_timeout=5, max_size=8_000_000) as ws:
                initialize = self.request(ws, "initialize", {
                    "clientInfo": {"name": "saberops", "version": "0.3.8"},
                    "capabilities": {},
                })
                resume: int | None = None
                while not self.stop.is_set():
                    try:
                        event = json.loads(ws.recv(timeout=0.1))
                    except TimeoutError:
                        event = {}
                    if not isinstance(event, dict):
                        raise ValueError("Invalid Codex server event")
                    if event.get("id") == initialize and "result" in event:
                        ws.send(json.dumps({"method": "initialized"}))
                        resume = self.request(ws, "thread/resume", {
                            "threadId": self.data["native_id"], "cwd": self.data["directory"],
                            "model": self.data["model"], "approvalPolicy": "on-request",
                            "sandbox": "read-only" if self.data.get("read_only")
                                       else "workspace-write",
                            "developerInstructions": INSTRUCTIONS +
                            "\nApp messages start with SABEROPS_APP_MESSAGE and their exact "
                            "message_id. Use mcp__saberops__reply for these. "
                            "Keep ordinary terminal replies natural. Never implement directly "
                            "in an orchestrator inspection session.",
                        })
                    elif resume is not None and event.get("id") == resume and "result" in event:
                        thread = event["result"]["thread"]
                        if thread["id"] != self.data["native_id"]:
                            raise ControlConflict("Codex resumed a different session.")
                        self.busy = thread.get("status", {}).get("type") == "active"
                        self.ready.set()
                    if "error" in event and "id" in event:
                        # Delivery can have reached the server. Fail closed, never replay it.
                        raise ControlConflict("Codex rejected the shared-session request: " +
                                              str(event["error"].get("message", "unknown error")))
                    self.handle(ws, event)
                    if self.ready.is_set() and not self.busy and not self.current:
                        message = self.bridge.next(self.data) if self.bridge.channel(
                            self.data["id"]) else None
                        if message:
                            text = message["text"]
                            if message["kind"] != "context":
                                text = "SABEROPS_APP_MESSAGE " + message["id"] + "\n" + text
                                self.current = message["id"]
                            else:
                                text = "SABEROPS_CONTEXT (no reply or work request):\n" + text
                            self.busy = True
                            self.request(ws, "turn/start", {
                                "threadId": self.data["native_id"],
                                "input": [{"type": "text", "text": text}],
                                "model": self.data["model"], "approvalPolicy": "on-request",
                            })
        except Exception as exc:
            self.error = str(exc)[:500]
            self.bridge.closed(self.data, self.error)
        finally:
            self.ready.set()


def main() -> int:
    descriptor = str(Path(sys.argv[1]).resolve())
    data = json.loads(Path(descriptor).read_text())
    binary = shutil.which("codex")
    if not binary:
        raise ControlConflict("Codex is not installed.")
    stop = threading.Event()
    with tempfile.TemporaryDirectory(prefix="saberops-codex-") as folder:
        Path(folder).chmod(0o700)
        socket = str(Path(folder) / "server.sock")
        with (Path(folder) / "server.log").open("wb") as log:
            server = subprocess.Popen(server_argv(binary, descriptor, data, socket),
                                      cwd=data["directory"], env=native_environment(data),
                                      stdin=subprocess.DEVNULL, stdout=log, stderr=log)
            relay = InboxConnection(socket, data, stop)
            thread = threading.Thread(target=relay.run, daemon=True)
            thread.start()
            try:
                if not relay.ready.wait(30) or relay.error:
                    raise ControlConflict(relay.error or "Codex shared connection timed out.")
                print("SaberOps · native Codex and app share this saved session.", flush=True)
                result = subprocess.run([
                    binary, "--remote", "unix://" + socket, "resume", data["native_id"],
                ], cwd=data["directory"], env=native_environment(data), check=False)
                return result.returncode
            finally:
                stop.set()
                server.terminate()
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()
                thread.join(timeout=2)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ControlConflict, OSError, ValueError) as exc:
        print("SaberOps: " + str(exc), file=sys.stderr)
        raise SystemExit(1) from exc
