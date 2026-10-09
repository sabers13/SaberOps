"""Execution-owner hosted OpenCode server with a guarded native-client endpoint.

The provider server is launched through the existing containment/process seam.
Native clients never own it. All client prompts join the durable manual queue;
only the owner sends provider turns, with frozen model and tool permissions.
"""

from __future__ import annotations

import base64
import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from contextvars import copy_context
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    WorkerInterruptionReason,
    WorkerRequest,
    WorkerResult,
)
from saberops.process import ProcessResult, launch_worker_process
from saberops.session_control import ControlConflict, SessionControls
from saberops.session_journal import SessionJournal
from saberops.telemetry import parse_opencode_jsonl


class SessionRuntimeError(RuntimeError):
    pass


class AgentSessionRuntime:
    def __init__(
        self,
        folder: Path,
        sid: str,
        controls: SessionControls,
        journal: SessionJournal,
        publish: Any,
    ) -> None:
        self.folder, self.sid, self.controls, self.journal = folder, sid, controls, journal
        self.publish = publish
        self.stop = threading.Event()
        self.closing = threading.Event()
        self.event_lock = threading.RLock()
        self.stream_condition = threading.Condition()
        self.stream_cursors: dict[str, int] = {}
        self.final_cursor = 0
        self.sealed_messages: set[str] = set()
        self.idle_confirmed = False
        self.ready = threading.Event()
        self.thread: threading.Thread | None = None
        self.events_thread: threading.Thread | None = None
        self.proxy: ThreadingHTTPServer | None = None
        self.process_result: ProcessResult | None = None
        self.failure: BaseException | None = None
        self.endpoint = ""
        self.password = secrets.token_urlsafe(32)
        self.client_password = secrets.token_urlsafe(32)
        self.native_id: str | None = None
        self.request: WorkerRequest | None = None
        self.last_message: dict[str, Any] = {}
        self.has_turn = False
        self.turn_lock = threading.Lock()
        self.terminal_status: str | None = None
        self.native_parts: dict[str, list[dict[str, str]]] = {}

    def api(self, method: str, path: str, body: object = None, *, timeout: float = 5) -> Any:
        if not self.endpoint:
            raise SessionRuntimeError("Provider server has not started")
        assert self.request is not None
        separator = "&" if "?" in path else "?"
        path += separator + urllib.parse.urlencode({"directory": str(self.request.worktree_path)})
        headers = {
            "Authorization": "Basic "
            + base64.b64encode(("opencode:" + self.password).encode()).decode(),
            "Content-Type": "application/json",
        }
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.endpoint + path, data=data, headers=headers, method=method
        )
        # Never follow redirects or ambient HTTP proxies to another authority.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(req, timeout=timeout) as response:
            raw = response.read()
        return json.loads(raw) if raw else None

    def ensure_started(self, request: WorkerRequest, binary: str) -> None:
        if self.thread is not None:
            if self.failure or not self.thread.is_alive():
                raise SessionRuntimeError("Provider server is no longer available")
            return
        self.request = replace(request, session_runtime=None)
        permissions = {"*": "allow", "external_directory": "deny", "doom_loop": "deny"}
        if request.read_only:
            permissions = {
                "*": "deny",
                "read": "allow",
                "glob": "allow",
                "grep": "allow",
                "list": "allow",
            }
        config = json.loads((request.env or {}).get("OPENCODE_CONFIG_CONTENT", "{}"))
        if not isinstance(config, dict):
            raise SessionRuntimeError("Selected OpenCode configuration must be an object")
        config.update(
            {
                "permission": permissions,
                "model": request.model,
                "default_agent": "saberops",
                "agent": {
                    "saberops": {
                        "description": "Frozen SaberOps assignment",
                        "mode": "primary",
                        "model": request.model,
                        "permission": permissions,
                    }
                },
            }
        )
        env = dict(request.env or {})
        env.update(
            OPENCODE_SERVER_PASSWORD=self.password,
            OPENCODE_SERVER_USERNAME="opencode",
            OPENCODE_CONFIG_CONTENT=json.dumps(config),
        )
        # Native CLI remains responsible for credential selection; no provider tokens copied.
        started = time.monotonic()
        startup_buffer = ""

        def output(text: str) -> None:
            nonlocal startup_buffer
            startup_buffer = (startup_buffer + text)[-8000:]
            match = re.search(r"http://127\.0\.0\.1:(\d+)", startup_buffer)
            if match and not self.endpoint:
                self.endpoint = match.group(0)
                self.ready.set()
            if request.on_stderr:
                request.on_stderr(text)

        def serve() -> None:
            try:
                self.process_result = launch_worker_process(
                    [binary, "serve", "--pure", "--hostname", "127.0.0.1", "--port", "0"],
                    cwd=request.worktree_path,
                    env=env,
                    inherit_env_exclude=request.inherit_env_exclude,
                    timeout=request.timeout_seconds,
                    control_interrupt=self.stop.is_set,
                    on_stdout=output,
                    on_stderr=output,
                    on_process_start=request.on_process_start,
                    on_monitor_transition=request.on_monitor_transition,
                )
            except BaseException as exc:
                self.failure = exc
            finally:
                self.ready.set()

        context = copy_context()  # Preserve autonomous authority on the launch thread.
        self.thread = threading.Thread(target=lambda: context.run(serve), daemon=True)
        self.thread.start()
        while not self.ready.wait(0.05):
            if time.monotonic() - started >= min(20, request.timeout_seconds):
                raise SessionRuntimeError("Provider server startup timed out")
        if not self.endpoint or self.failure or not self.thread.is_alive():
            raise SessionRuntimeError("Provider server could not start")
        deadline = started + min(20, request.timeout_seconds)
        while True:
            try:
                health = self.api("GET", "/global/health", timeout=1)
                if not health.get("healthy"):
                    raise SessionRuntimeError("Provider server health check failed")
                break
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    raise SessionRuntimeError("Provider server did not become ready") from None
                time.sleep(0.05)
        if request.native_resume_id:
            session = self.api("GET", "/session/" + request.native_resume_id)
            # Never reuse another assignment's directory or permission scope.
            if Path(session["directory"]).resolve() != request.worktree_path.resolve():
                raise SessionRuntimeError("Saved session belongs to a different worktree")
            self.native_id = request.native_resume_id
        else:
            session = self.api(
                "POST",
                "/session",
                {
                    "title": "SaberOps assignment",
                    "agent": "saberops",
                    "model": native_model(request.model),
                    "permission": permission_rules(permissions),
                },
            )
            self.native_id = session["id"]
        if not isinstance(self.native_id, str) or not re.fullmatch(
            r"ses_[A-Za-z0-9]+", self.native_id
        ):
            raise SessionRuntimeError("Provider did not return a valid session identity")
        self._start_proxy(binary)
        self.publish(
            {
                "native_id": self.native_id,
                "native_access": "shared-server",
                "native_attach_ready": True,
                "runtime_version": health.get("version"),
                "native_cli_reason": "Native OpenCode interface; input uses controlled takeover.",
            }
        )
        self.events_thread = threading.Thread(target=self._events, daemon=True)
        self.events_thread.start()

    def _events(self) -> None:
        assert self.request is not None
        headers = {
            "Authorization": "Basic "
            + base64.b64encode(("opencode:" + self.password).encode()).decode()
        }
        url = (
            self.endpoint
            + "/event?"
            + urllib.parse.urlencode({"directory": str(self.request.worktree_path)})
        )
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        while not self.stop.is_set():
            try:
                with opener.open(urllib.request.Request(url, headers=headers), timeout=2) as stream:
                    for raw in stream:
                        if self.stop.is_set():
                            return
                        if not raw.startswith(b"data:"):
                            continue
                        event = json.loads(raw[5:])
                        properties = event.get("properties", {})
                        info = properties.get("info") or properties.get("part") or properties
                        if info.get("sessionID") not in (None, self.native_id):
                            continue
                        self.capture_event(event)
            except (OSError, ValueError):
                if not self.stop.is_set():
                    self.stop.wait(0.2)

    def capture_event(self, event: dict[str, Any]) -> None:
        """Late deltas cannot overwrite a message already confirmed complete by the API."""
        with self.event_lock:
            properties = event.get("properties", {})
            info = properties.get("info") or properties.get("part") or properties
            message_id = info.get("messageID") or properties.get("messageID")
            if event.get("type") == "message.updated":
                message_id = info.get("id")
            if (
                self.closing.is_set()
                or message_id in self.sealed_messages
                or (self.idle_confirmed and event.get("type") in ("session.status", "session.idle"))
            ):
                return
            if self.request and self.request.on_stdout:
                self.request.on_stdout(json.dumps(event) + "\n")

    def confirm_snapshot(self, messages: list[dict[str, Any]]) -> None:
        """Persist API-confirmed state independently of a delayed provider event stream."""
        with self.event_lock:
            self.sealed_messages.update(row["info"]["id"] for row in messages)
            self.idle_confirmed = True
            self.final_cursor = self.journal.append(
                self.sid,
                "provider.snapshot",
                {
                    "source": "provider-api",
                    "session_id": self.native_id,
                    "messages": messages,
                    "status": {"type": "idle"},
                },
            )

    @staticmethod
    def snapshot_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        for row in payload["messages"]:
            events.append({"type": "message.updated", "properties": {"info": row["info"]}})
            for part in row.get("parts", []):
                events.append({"type": "message.part.updated", "properties": {"part": part}})
        events.append(
            {
                "type": "session.status",
                "properties": {"sessionID": payload["session_id"], "status": payload["status"]},
            }
        )
        return events

    def run_opencode(self, request: WorkerRequest, binary: str) -> WorkerResult:
        started = time.monotonic()
        try:
            self.ensure_started(request, binary)
            with self.turn_lock:
                return self._turn(request, started)
        except (OSError, ValueError, KeyError, SessionRuntimeError) as exc:
            return WorkerResult(
                provider="opencode",
                model=request.model,
                status=AttemptStatus.FAILED,
                summary="Provider session failed",
                stdout="",
                stderr=str(exc),
                error=str(exc),
                has_changes=False,
                outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                duration_seconds=time.monotonic() - started,
            )

    def _turn(self, request: WorkerRequest, started: float) -> WorkerResult:
        assert self.native_id
        # A fresh formal review cannot reuse the manually steered context.
        if request.native_resume_id is None and self.has_turn and request.read_only:
            assert self.request
            permissions = {
                "*": "deny",
                "read": "allow",
                "glob": "allow",
                "grep": "allow",
                "list": "allow",
            }
            session = self.api(
                "POST",
                "/session",
                {
                    "title": "SaberOps formal review",
                    "agent": "saberops",
                    "model": native_model(request.model),
                    "permission": permission_rules(permissions),
                },
            )
            self.native_id = session["id"]
            descriptor = self.folder / (self.sid + ".native")
            if descriptor.exists():
                data = json.loads(descriptor.read_text())
                data["session"] = self.native_id
                temporary = descriptor.with_suffix(".native.tmp")
                with os.fdopen(
                    os.open(temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w"
                ) as file:
                    json.dump(data, file)
                temporary.replace(descriptor)
            self.publish({"native_id": self.native_id})
        prefix = "/session/" + self.native_id
        before = {row["info"]["id"] for row in self.api("GET", prefix + "/message")}
        if request.on_stdout:
            request.on_stdout(json.dumps({"sessionID": self.native_id}) + "\n")
        provider, separator, model = request.model.partition("/")
        if not separator:
            raise SessionRuntimeError("OpenCode requires a provider/model identity")
        message_id = request.native_message_id
        if not message_id or not re.fullmatch(r"msg_[A-Za-z0-9_-]{1,120}", message_id):
            message_id = "msg_" + uuid.uuid4().hex
        parts = self.native_parts.pop(message_id, None) or [{"type": "text", "text": request.task}]
        self.has_turn = True
        with self.event_lock:
            self.idle_confirmed = False
        self.api(
            "POST",
            prefix + "/prompt_async",
            {
                "messageID": message_id,
                "model": {"providerID": provider, "modelID": model},
                "agent": "saberops",
                "parts": parts,
                **(
                    {"variant": request.reasoning_effort.value.lower()}
                    if request.reasoning_effort is not None
                    else {}
                ),
            },
        )
        self.terminal_status = None
        interrupted = timed_out = False
        completed: dict[str, Any] | None = None
        own_messages: list[dict[str, Any]] = []
        while True:
            timed_out = time.monotonic() - started >= request.timeout_seconds
            interrupted = bool(request.control_interrupt and request.control_interrupt())
            if timed_out or interrupted:
                self.api("POST", prefix + "/abort")
                # Abort is a request; do not acknowledge takeover until provider reports idle.
                deadline = time.monotonic() + 10
                while (
                    self.api("GET", "/session/status").get(self.native_id, {}).get("type", "idle")
                    != "idle"
                ):
                    if time.monotonic() >= deadline:
                        raise SessionRuntimeError("Provider did not confirm cancellation")
                    time.sleep(0.05)
                break
            if self.thread is None or not self.thread.is_alive():
                raise SessionRuntimeError("Provider server disappeared during a turn")
            rows = self.api("GET", prefix + "/message")
            foreign = [
                row
                for row in rows
                if row["info"].get("role") == "user"
                and row["info"]["id"] not in before
                and row["info"]["id"] != message_id
            ]
            if foreign:
                self.journal.append(self.sid, "native.foreign_input", foreign)
                self.controls.hold(self.sid, "Unexpected native input detected; automation paused.")
                continue
            for row in rows:
                info = row["info"]
                if (
                    info["id"] not in before
                    and info.get("role") == "assistant"
                    and info.get("parentID") == message_id
                ):
                    if info.get("error") or (
                        info.get("time", {}).get("completed") and info.get("finish") != "tool-calls"
                    ):
                        completed = row
            own_messages = [
                row
                for row in rows
                if row["info"].get("role") == "assistant"
                and row["info"].get("parentID") == message_id
            ]
            status = self.api("GET", "/session/status").get(self.native_id, {}).get("type", "idle")
            if completed and status == "idle":
                break
            time.sleep(0.1)
        self.last_message = completed or {}
        if not own_messages and (interrupted or timed_out):
            own_messages = [
                row
                for row in self.api("GET", prefix + "/message")
                if row["info"].get("role") == "assistant"
                and row["info"].get("parentID") == message_id
            ]
        self.confirm_snapshot(own_messages)
        text = "\n".join(
            p["text"]
            for p in self.last_message.get("parts", [])
            if p.get("type") == "text" and isinstance(p.get("text"), str)
        )
        raw_usage = "\n".join(
            json.dumps({"type": "step_finish", "part": p})
            for row in own_messages
            for p in row.get("parts", [])
            if p.get("type") == "step-finish"
        )
        _, usage = parse_opencode_jsonl(raw_usage)
        error = self.last_message.get("info", {}).get("error")
        success = bool(completed and not error and not interrupted and not timed_out)
        self.terminal_status = "SUCCESS" if success else "INTERRUPTED" if interrupted else "FAILED"
        changes = GitManager.get_changed_paths(request.worktree_path)
        return WorkerResult(
            provider="opencode",
            model=request.model,
            status=AttemptStatus.SUCCESS
            if success
            else AttemptStatus.TIMEOUT
            if timed_out
            else AttemptStatus.FAILED,
            summary=text[:300]
            if success
            else "Provider turn interrupted"
            if interrupted
            else "Provider turn did not succeed",
            stdout=text,
            stderr=json.dumps(error) if error else "",
            error=json.dumps(error) if error else None,
            changed_paths=changes,
            has_changes=bool(changes),
            usage=usage,
            actual_model=self.last_message.get("info", {}).get("modelID"),
            actual_provider=self.last_message.get("info", {}).get("providerID"),
            prompt_bytes=len(request.task.encode()),
            prompt_transport_used="session-protocol",
            duration_seconds=time.monotonic() - started,
            interruption_reason=WorkerInterruptionReason.ABORTED if interrupted else None,
            outcome=None
            if success
            else DispatchOutcome.TIMEOUT
            if timed_out
            else DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
        )

    def _start_proxy(self, binary: str) -> None:
        runtime = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                pass

            def respond(self, status: int, payload: object) -> None:
                raw = b"" if status == 204 else json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                try:
                    self.end_headers()
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # A closed native client does not affect the execution owner.

            def authenticated(self) -> bool:
                expected = (
                    "Basic "
                    + base64.b64encode(("opencode:" + runtime.client_password).encode()).decode()
                )
                return secrets.compare_digest(self.headers.get("Authorization", ""), expected)

            def do_GET(self) -> None:
                if not self.authenticated():
                    self.respond(401, {"error": "Native client authentication required"})
                    return
                path = urllib.parse.urlsplit(self.path).path
                if path.startswith("/session/") and path != "/session/status":
                    if path.split("/")[2] != runtime.native_id:
                        self.respond(403, {"error": "This terminal is scoped to one session"})
                        return
                allowed = (
                    "/event",
                    "/global/health",
                    "/global/event",
                    "/project",
                    "/path",
                    "/vcs",
                    "/config",
                    "/provider",
                    "/session",
                    "/agent",
                    "/command",
                    "/permission",
                    "/question",
                    "/file",
                    "/find",
                    "/lsp",
                    "/formatter",
                    "/mcp",
                    "/doc",
                )
                if not any(path == p or path.startswith(p + "/") for p in allowed):
                    self.respond(403, {"error": "Endpoint unavailable in managed terminal"})
                    return
                try:
                    if path in ("/event", "/global/event"):
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        # Native clients bootstrap history with GET /message.
                        # Old deltas can put a completed session back into busy state.
                        cursor = runtime.journal.latest_sequence(runtime.sid)
                        stream_id = uuid.uuid4().hex
                        with runtime.stream_condition:
                            runtime.stream_cursors[stream_id] = cursor
                        try:
                            while not runtime.stop.is_set():
                                view = runtime.journal.view(runtime.sid, after=cursor, limit=200)
                                cursor = view["cursor"]
                                for event in view["events"]:
                                    frames = (
                                        runtime.snapshot_events(event["payload"])
                                        if event["kind"] == "provider.snapshot"
                                        else [event["payload"]]
                                        if event["kind"] == "provider.structured"
                                        and event["payload"].get("type")
                                        else []
                                    )
                                    for frame in frames:
                                        self.wfile.write(
                                            (
                                                "data: "
                                                + json.dumps(
                                                    {
                                                        "directory": str(
                                                            runtime.request.worktree_path
                                                        )
                                                        if runtime.request
                                                        else "",
                                                        "payload": frame,
                                                    }
                                                    if path == "/global/event"
                                                    else frame
                                                )
                                                + "\n\n"
                                            ).encode()
                                        )
                                self.wfile.write(b": keepalive\n\n")
                                self.wfile.flush()
                                with runtime.stream_condition:
                                    runtime.stream_cursors[stream_id] = cursor
                                    runtime.stream_condition.notify_all()
                                runtime.stop.wait(0.05)
                        finally:
                            with runtime.stream_condition:
                                runtime.stream_cursors.pop(stream_id, None)
                                runtime.stream_condition.notify_all()
                        return
                    data = runtime.api("GET", path)
                    if path == "/session":
                        data = [row for row in data if row.get("id") == runtime.native_id]
                    elif path == "/session/status":
                        data = {k: v for k, v in data.items() if k == runtime.native_id}
                    self.respond(200, data)
                except (OSError, ValueError):
                    self.respond(503, {"error": "Provider session unavailable"})

            def do_POST(self) -> None:
                if not self.authenticated():
                    self.respond(401, {"error": "Native client authentication required"})
                    return
                path = urllib.parse.urlsplit(self.path).path
                try:
                    if path == "/session/" + str(runtime.native_id) + "/abort":
                        state = runtime.controls.get(runtime.sid)
                        if state and state["mode"] in ("ACTIVE", "REQUESTED"):
                            runtime.controls.hold(
                                runtime.sid, "Native terminal requested interruption."
                            )
                        else:
                            runtime.controls.interrupt(runtime.sid)
                        self.respond(200, True)
                        return
                    if path not in (
                        "/session/" + str(runtime.native_id) + "/message",
                        "/session/" + str(runtime.native_id) + "/prompt_async",
                    ):
                        raise ControlConflict("This operation is outside the frozen assignment")
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 128000:
                        raise ControlConflict("Native message is too large or missing")
                    body = json.loads(self.rfile.read(length))
                    parts = body.get("parts", [])
                    if not parts or any(p.get("type") != "text" for p in parts):
                        raise ControlConflict("Managed native messages currently support text only")
                    text = "\n".join(str(p.get("text", "")) for p in parts)
                    request_id = body.get("messageID") or uuid.uuid4().hex
                    native_parts = [
                        {
                            "type": "text",
                            "text": str(p.get("text", "")),
                            **({"id": p["id"]} if isinstance(p.get("id"), str) else {}),
                        }
                        for p in parts
                    ]
                    had_parts = str(request_id) in runtime.native_parts
                    runtime.native_parts.setdefault(str(request_id), native_parts)
                    try:
                        runtime.controls.submit_native(runtime.sid, text, str(request_id))
                    except ControlConflict:
                        if not had_parts:
                            runtime.native_parts.pop(str(request_id), None)
                        raise
                    runtime.journal.append(
                        runtime.sid,
                        "native.message.queued",
                        {"request_id": request_id, "parts": native_parts},
                    )
                    if path.endswith("/prompt_async"):
                        self.respond(204, None)
                        return
                    # Blocking native clients see the same response as the app.
                    while not runtime.stop.wait(0.1):
                        data = runtime.controls.get(runtime.sid, messages=True) or {}
                        row = next(
                            (
                                m
                                for m in data.get("messages", [])
                                if m["id"] == runtime.sid + ":" + str(request_id)
                            ),
                            None,
                        )
                        if row and row["state"] in ("DONE", "FAILED"):
                            self.respond(
                                200 if row["state"] == "DONE" else 409,
                                runtime.last_message or {"error": row["response"]},
                            )
                            return
                    raise ControlConflict("Session closed before the reply")
                except (ControlConflict, ValueError, TypeError, AttributeError) as exc:
                    self.respond(409, {"error": str(exc)})

            def do_PATCH(self) -> None:
                self.respond(403, {"error": "Frozen session settings cannot be changed"})

            do_DELETE = do_PATCH
            do_PUT = do_PATCH

        self.proxy = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.proxy.daemon_threads = True
        threading.Thread(target=self.proxy.serve_forever, daemon=True).start()
        assert self.request
        data = {
            "provider": "opencode",
            "binary": binary,
            "url": f"http://127.0.0.1:{self.proxy.server_port}",
            "password": self.client_password,
            "session": self.native_id,
            "directory": str(self.request.worktree_path),
            "id": self.sid,
            "controls_root": str(self.controls.path.parent),
            "run_db_path": (self.request.env or {}).get("ORCH_DB_PATH"),
            "run_id": (self.request.env or {}).get("ORCH_RUN_ID"),
        }
        path = self.folder / (self.sid + ".native")
        with os.fdopen(os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600), "w") as file:
            json.dump(data, file)

    def close(self) -> bool:
        self.closing.set()
        # Bounded delivery fence; a slow/disconnected client never owns execution lifetime.
        deadline = time.monotonic() + 2
        with self.stream_condition:
            while (
                self.final_cursor
                and any(cursor < self.final_cursor for cursor in self.stream_cursors.values())
                and time.monotonic() < deadline
            ):
                self.stream_condition.wait(min(0.1, max(0, deadline - time.monotonic())))
        self.stop.set()
        if self.proxy:
            self.proxy.shutdown()
            self.proxy.server_close()
        if self.thread:
            self.thread.join(15)
            safe = bool(
                not self.thread.is_alive()
                and self.process_result
                and not self.process_result.termination_incomplete
            )
        else:
            safe = True
        if self.events_thread:
            self.events_thread.join(3)
        self.publish({"native_attach_ready": False})
        return safe


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def permission_rules(permissions: dict[str, str]) -> list[dict[str, str]]:
    return [
        {"permission": name, "pattern": "*", "action": action}
        for name, action in permissions.items()
    ]


def native_model(model: str) -> dict[str, str]:
    provider, separator, identifier = model.partition("/")
    if not separator or not identifier:
        raise SessionRuntimeError("OpenCode requires a provider/model identity")
    return {"providerID": provider, "id": identifier}
