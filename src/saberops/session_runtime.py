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
    ProviderUsage,
    ReasoningEffort,
    WorkerInterruptionReason,
    WorkerRequest,
    WorkerResult,
)
from saberops.process import ProcessResult, launch_worker_process
from saberops.session_control import ControlConflict, SessionControls
from saberops.session_journal import SessionJournal
from saberops.workers.env import hermetic_env
from saberops.workers.opencode_isolation import (
    OpenCodeIsolationUnavailable,
    opencode_isolation,
)


class SessionRuntimeError(RuntimeError):
    pass


#: The OpenCode server surface this runtime speaks.  The installed v2.3.2
#: server exposes every route under ``/api/*`` and has no ``/global/health``
#: (the web handler answers SPA HTML there), so readiness is a real JSON
#: endpoint: ``GET /api/info`` returns the server identity and version.
#: Older ``/session`` + ``/global/health`` servers are refused with a clear
#: message; no committed fixture requires them.
API_INFO = "/api/info"
API_SESSION = "/api/session"
API_ACTIVE = "/api/session/active"
API_EVENT = "/api/event"

#: Lowest OpenCode server version that serves the required ``/api/*`` shape.
MINIMUM_SERVER_VERSION = (2, 3)

#: Routes the session runtime itself calls, used by the vendor live check to
#: pin the server API shape against the committed OpenAPI fixture.
REQUIRED_SERVER_ROUTES: tuple[str, ...] = (
    API_INFO,
    API_SESSION,
    "/api/session/{sessionID}",
    "/api/session/{sessionID}/message",
    "/api/session/{sessionID}/prompt",
    "/api/session/{sessionID}/interrupt",
    API_ACTIVE,
    API_EVENT,
)

_VERSION_RE = re.compile(r"(\d+)\.(\d+)(?:\.(\d+))?")


def server_version(version: object) -> tuple[int, ...] | None:
    """Parse an OpenCode server version string (``2.3.2`` or ``v2.3.2``)."""
    if not isinstance(version, str):
        return None
    match = _VERSION_RE.search(version)
    if match is None:
        return None
    return tuple(int(part) for part in match.groups() if part is not None)


def require_supported_server(version: object) -> tuple[int, ...]:
    """Refuse an OpenCode server that does not serve the supported API shape.

    Raises :class:`SessionRuntimeError` with a fixed, secret-free message;
    never guesses that an unknown surface behaves like v2.3.
    """
    parsed = server_version(version)
    minimum = MINIMUM_SERVER_VERSION
    if parsed is None or parsed[:2] < minimum:
        raise SessionRuntimeError(
            f"OpenCode server {version!r} is not supported: SaberOps requires "
            f"the v{minimum[0]}.{minimum[1]}+ /api/* server surface "
            "(GET /api/info, POST /api/session/{id}/prompt, "
            "POST /api/session/{id}/interrupt; no /global/health)"
        )
    return parsed


def opencode_serve_config(model: str, permissions: dict[str, str]) -> dict[str, Any]:
    """Return the pinned inline server config for one OpenCode assignment.

    The config always starts from ``{}`` -- never from ``request.env`` or
    the parent shell -- so ambient ``OPENCODE_CONFIG_CONTENT`` cannot change
    provider selection.  The v2.3 keys are used directly: ``agents`` (not the
    legacy ``agent``), ``permissions`` (a :class:`Permission.Ruleset`, not the
    legacy name->effect map) and ``enabled_providers`` allowlisting exactly
    the selected model's provider (inline config outranks project and global
    config files).  Raises :class:`SessionRuntimeError` when ``model`` has no
    ``<provider>/<model>`` prefix, since billing cannot be pinned then.
    """
    provider, separator, identifier = model.partition("/")
    if not separator or not provider or not identifier:
        raise SessionRuntimeError("OpenCode requires a provider/model identity")
    rules = permission_rules(permissions)
    return {
        "permissions": rules,
        "model": model,
        "default_agent": "saberops",
        "agents": {
            "saberops": {
                "description": "Frozen SaberOps assignment",
                "mode": "primary",
                "model": {"providerID": provider, "model": identifier},
                "permissions": rules,
            }
        },
        "enabled_providers": [provider],
    }


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
        self.isolation: Any = None
        self.last_message: dict[str, Any] = {}
        self.has_turn = False
        self.turn_lock = threading.Lock()
        self.terminal_status: str | None = None
        self.native_parts: dict[str, list[dict[str, str]]] = {}
        self.server_version: str | None = None
        #: True once the process monitor has begun terminating the owned
        #: server.  On the idle-tree cut the turn's HTTP call can fail before
        #: ``launch_worker_process`` publishes its typed result, so the failure
        #: path waits for that result instead of reporting a provider symptom.
        self.monitor_terminating = False

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
        try:
            with opener.open(req, timeout=timeout) as response:
                raw = response.read()
        except urllib.error.HTTPError as exc:
            # Surface the server's own error tag/message (never a secret):
            # a bare "HTTP Error 400" hides which request was refused.
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:500]
            except OSError:
                pass
            raise SessionRuntimeError(
                f"Provider API {method} {path.split('?')[0]} failed: "
                f"HTTP {exc.code}{': ' + detail if detail else ''}"
            ) from None
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
        config = opencode_serve_config(request.model, permissions)
        env = dict(request.env or {})
        env.update(
            OPENCODE_SERVER_PASSWORD=self.password,
            OPENCODE_SERVER_USERNAME="opencode",
        )
        base_env = hermetic_env("opencode", env)
        # Config/plugin isolation for the server's lifetime: a private
        # config/home root (project/ancestor discovery disabled, ambient
        # config selectors removed, inline deny policies).  The worktree is
        # still the child ``cwd``.  Refuse before launch when isolation cannot
        # be established; billing/permission pinning alone is not enough.
        try:
            isolation = opencode_isolation(base_env)
        except OpenCodeIsolationUnavailable as exc:
            raise SessionRuntimeError(str(exc)) from exc
        child_env = isolation.env_for(base_env, config=isolation.config(config))
        self.isolation = isolation
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
                # No ``--pure``: the installed opencode v2.3.2 ``serve --help``
                # (committed at
                # tests/fixtures/vendor_cli/opencode/opencode-v2.3.2/config_help.txt)
                # documents no such flag.
                # Config/plugin isolation comes from ``child_env`` (a private
                # config/home root owned until the server stops); the worktree
                # is the child ``cwd`` and the per-request ``?directory=``
                # query parameter (see ``api``).
                self.process_result = launch_worker_process(
                    [binary, "serve", "--hostname", "127.0.0.1", "--port", "0"],
                    cwd=request.worktree_path,
                    # Hermetic child environment: only safe runtime variables
                    # plus the explicit request overlay (including the injected
                    # OPENCODE_SERVER_* values) reach the OpenCode server.
                    env=child_env,
                    inherit_env=False,
                    inherit_env_exclude=request.inherit_env_exclude,
                    timeout=request.timeout_seconds,
                    control_interrupt=self.stop.is_set,
                    on_stdout=output,
                    on_stderr=output,
                    on_process_start=request.on_process_start,
                    on_monitor_transition=self.monitor_transition,
                    on_process_sample=request.on_process_sample,
                )
            except BaseException as exc:
                self.failure = exc
            finally:
                self.ready.set()

        context = copy_context()  # Preserve autonomous authority on the launch thread.
        self.thread = threading.Thread(target=lambda: context.run(serve), daemon=True)
        self.thread.start()
        try:
            while not self.ready.wait(0.05):
                if time.monotonic() - started >= min(20, request.timeout_seconds):
                    raise SessionRuntimeError("Provider server startup timed out")
        except BaseException:
            self.close()
            raise
        if not self.endpoint or self.failure or not self.thread.is_alive():
            self.close()
            raise SessionRuntimeError("Provider server could not start")
        deadline = started + min(20, request.timeout_seconds)
        info: dict[str, Any] | None = None
        while True:
            try:
                payload = self.api("GET", API_INFO, timeout=1)
                if not isinstance(payload, dict):
                    raise SessionRuntimeError("Provider server info endpoint is malformed")
                version = payload.get("version")
                require_supported_server(version)
                info = payload
                self.server_version = str(version)
                break
            except SessionRuntimeError:
                # An answered-but-unsupported server is a contract failure:
                # retrying cannot change the shipped API surface.
                self.close()
                raise
            except (OSError, ValueError):
                if time.monotonic() >= deadline:
                    self.close()
                    raise SessionRuntimeError(
                        "Provider server did not become ready: GET /api/info did "
                        "not return JSON (an older server without the v2.3+ "
                        "/api/* surface is not supported)"
                    ) from None
                time.sleep(0.05)
        assert info is not None
        if request.native_resume_id:
            session = self.api("GET", API_SESSION + "/" + request.native_resume_id)
            info_payload = session["data"] if isinstance(session, dict) else session
            directory = (info_payload.get("location") or {}).get("directory")
            if directory is None or Path(directory).resolve() != request.worktree_path.resolve():
                raise SessionRuntimeError("Saved session belongs to a different worktree")
            self.native_id = request.native_resume_id
        else:
            session = self.api(
                "POST",
                API_SESSION,
                {
                    "title": "SaberOps assignment",
                    "agent": "saberops",
                    "model": native_model(request.model, request.reasoning_effort),
                    "permissions": permission_rules(permissions),
                    "location": {"directory": str(request.worktree_path)},
                },
            )
            self.native_id = session["data"]["id"]
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
                "runtime_version": self.server_version,
                "native_cli_reason": "Native OpenCode interface; input uses controlled takeover.",
            }
        )
        self.events_thread = threading.Thread(target=self._events, daemon=True)
        self.events_thread.start()

    def _messages(self, prefix: str) -> list[dict[str, Any]]:
        """Return every projected message for the session, oldest first.

        The v2.3 list endpoint is cursor-paginated (``data`` + ``cursor.next``);
        callers must not assume a bare list.
        """
        rows: list[dict[str, Any]] = []
        cursor: str | None = None
        while True:
            # ``order`` and ``cursor`` are mutually exclusive on v2.3; the
            # first page fixes the order and cursors continue it.  ``next``
            # is an anchor, present even on a complete page, so an empty page
            # is the end of the list.
            query: dict[str, str] = {"limit": "200"}
            if cursor is None:
                query["order"] = "asc"
            else:
                query["cursor"] = cursor
            payload = self.api("GET", prefix + "/message?" + urllib.parse.urlencode(query))
            page = payload.get("data") if isinstance(payload, dict) else payload
            if not isinstance(page, list):
                raise SessionRuntimeError("Provider message list is malformed")
            if not page:
                return rows
            rows.extend(page)
            next_cursor = (payload.get("cursor") or {}).get("next") if isinstance(
                payload, dict
            ) else None
            if not next_cursor:
                return rows
            if next_cursor == cursor or len(rows) > 10000:
                raise SessionRuntimeError("Provider message pagination did not advance")
            cursor = str(next_cursor)

    def _active(self) -> bool:
        """Return whether this OpenCode process currently executes our session."""
        payload = self.api("GET", API_ACTIVE)
        data = payload.get("data") if isinstance(payload, dict) else payload
        return isinstance(data, dict) and self.native_id in data

    def _events(self) -> None:
        assert self.request is not None
        headers = {
            "Authorization": "Basic "
            + base64.b64encode(("opencode:" + self.password).encode()).decode()
        }
        url = (
            self.endpoint
            + API_EVENT
            + "?"
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
                        if not isinstance(event, dict):
                            continue
                        if event_session_id(event) not in (None, self.native_id):
                            continue
                        self.capture_event(event)
            except (OSError, ValueError):
                if not self.stop.is_set():
                    self.stop.wait(0.2)

    def capture_event(self, event: dict[str, Any]) -> None:
        """Late deltas cannot overwrite a message already confirmed complete by the API.

        v2.3 event frames are ``{"id", "created", "type", "location", "data"}``
        envelopes; the message being edited is named by ``assistantMessageID``
        or ``messageID`` inside ``data``.
        """
        with self.event_lock:
            message_id = event_message_id(event)
            event_type = event.get("type")
            if (
                self.closing.is_set()
                or (message_id is not None and message_id in self.sealed_messages)
                or (
                    self.idle_confirmed
                    and event_type
                    in (
                        "session.execution.started",
                        "session.execution.succeeded",
                        "session.execution.failed",
                        "session.execution.interrupted",
                        "session.status",
                        "session.idle",
                    )
                )
            ):
                return
            if self.request and self.request.on_stdout:
                self.request.on_stdout(json.dumps(event) + "\n")

    def confirm_snapshot(
        self, messages: list[dict[str, Any]], *, outcome: str = "succeeded"
    ) -> None:
        """Persist API-confirmed state independently of a delayed provider event stream."""
        with self.event_lock:
            self.sealed_messages.update(
                str(row["id"]) for row in messages if isinstance(row.get("id"), str)
            )
            self.idle_confirmed = True
            self.final_cursor = self.journal.append(
                self.sid,
                "provider.snapshot",
                {
                    "source": "provider-api",
                    "session_id": self.native_id,
                    "messages": messages,
                    "outcome": outcome,
                    "status": {"type": "idle"},
                    "directory": str(self.request.worktree_path) if self.request else "",
                },
            )

    @staticmethod
    def snapshot_events(payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Render one provider snapshot as a v2.3 event frame.

        The native client re-fetches message content from the API, so the
        snapshot only has to publish the terminal execution state truthfully:
        an ``execution.succeeded|failed|interrupted`` frame with our session
        id.  Replaying old deltas would risk putting a finished session back
        into a busy state.
        """
        outcome = payload.get("outcome", "succeeded")
        event_type = {
            "succeeded": "session.execution.succeeded",
            "failed": "session.execution.failed",
            "interrupted": "session.execution.interrupted",
        }.get(outcome, "session.execution.succeeded")
        data: dict[str, Any] = {"sessionID": payload.get("session_id")}
        if event_type == "session.execution.failed" and payload.get("error"):
            data["error"] = payload["error"]
        if event_type == "session.execution.interrupted":
            data["reason"] = "interrupted"
        return [
            {
                "id": "evt_saberops_" + uuid.uuid4().hex,
                "created": int(time.time() * 1000),
                "type": event_type,
                "location": {"directory": payload.get("directory", "")},
                "data": data,
            }
        ]

    def monitor_transition(self, payload: dict[str, Any]) -> None:
        """Keep the engine's termination signal and forward the durable hook.

        The turn's HTTP call can fail while the owned server is being cut, so
        the failure path needs its own view of "the engine is terminating this
        process" rather than a provider error string.
        """
        state = str(payload.get("state") or "")
        if state in {"TERMINATING", "TERMINAL"}:
            self.monitor_terminating = True
        request = self.request
        callback = request.on_monitor_transition if request is not None else None
        if callback is not None:
            callback(payload)

    def failure_process_result(self, timeout: float = 5.0) -> ProcessResult | None:
        """Return the owned server's result even when a failure races a cut.

        ``launch_worker_process`` publishes the typed ``termination_reason``
        only when it returns.  An idle-tree cut kills the server while a turn's
        HTTP call is in flight, so that call fails first with a provider
        symptom (HTTP 503 / connection refused).  When the monitor has begun
        terminating the process -- or the launch thread has already finished
        -- wait briefly for the result so the durable attempt names the cut
        instead of the symptom.
        """
        if self.process_result is not None:
            return self.process_result
        thread = self.thread
        if thread is None:
            return None
        if self.monitor_terminating or not thread.is_alive():
            thread.join(timeout)
        return self.process_result

    def run_opencode(self, request: WorkerRequest, binary: str) -> WorkerResult:
        started = time.monotonic()
        provider, separator, identifier = request.model.partition("/")
        if not separator or not provider or not identifier:
            detail = (
                "OpenCode requires a provider/model identity "
                f"(got {request.model!r}); without a provider prefix SaberOps "
                "cannot pin billing through enabled_providers"
            )
            return WorkerResult(
                provider="opencode",
                model=request.model,
                status=AttemptStatus.FAILED,
                summary=detail,
                stdout="",
                stderr=detail,
                error=detail,
                has_changes=False,
                outcome=DispatchOutcome.ELIGIBILITY_SKIP,
                duration_seconds=time.monotonic() - started,
            )
        try:
            self.ensure_started(request, binary)
            with self.turn_lock:
                return self._turn(request, started)
        except (OSError, ValueError, KeyError, SessionRuntimeError) as exc:
            process = self.failure_process_result()
            termination_reason = (
                process.termination_reason if process is not None else None
            )
            termination_note = (
                process.termination_note if process is not None else None
            )
            detail = termination_note or str(exc)
            return WorkerResult(
                provider="opencode",
                model=request.model,
                status=AttemptStatus.FAILED,
                summary=termination_note or "Provider session failed",
                stdout="",
                stderr=detail,
                error=detail,
                has_changes=False,
                outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                duration_seconds=time.monotonic() - started,
                termination_reason=termination_reason,
                termination_note=termination_note,
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
                API_SESSION,
                {
                    "title": "SaberOps formal review",
                    "agent": "saberops",
                    "model": native_model(request.model, request.reasoning_effort),
                    "permissions": permission_rules(permissions),
                    "location": {"directory": str(request.worktree_path)},
                },
            )
            self.native_id = session["data"]["id"]
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
        prefix = API_SESSION + "/" + self.native_id
        before = {str(row["id"]) for row in self._messages(prefix) if "id" in row}
        if request.on_stdout:
            request.on_stdout(json.dumps({"sessionID": self.native_id}) + "\n")
        provider, separator, model = request.model.partition("/")
        if not separator or not provider or not model:
            raise SessionRuntimeError("OpenCode requires a provider/model identity")
        message_id = request.native_message_id
        if not message_id or not re.fullmatch(r"msg_[A-Za-z0-9_-]{1,120}", message_id):
            message_id = "msg_" + uuid.uuid4().hex
        parts = self.native_parts.pop(message_id, None) or [{"type": "text", "text": request.task}]
        text = "\n".join(str(part.get("text", "")) for part in parts)
        self.has_turn = True
        with self.event_lock:
            self.idle_confirmed = False
        # v2.3 admits one proxied prompt into the manual queue; model and agent
        # are frozen on the session, so the prompt carries text only.
        self.api(
            "POST",
            prefix + "/prompt",
            {"id": message_id, "text": text},
        )
        self.terminal_status = None
        interrupted = timed_out = False
        completed: dict[str, Any] | None = None
        own_messages: list[dict[str, Any]] = []
        while True:
            timed_out = time.monotonic() - started >= request.timeout_seconds
            interrupted = bool(request.control_interrupt and request.control_interrupt())
            if timed_out or interrupted:
                self.api("POST", prefix + "/interrupt")
                # Abort is a request; do not acknowledge takeover until provider reports idle.
                deadline = time.monotonic() + 10
                while self._active():
                    if time.monotonic() >= deadline:
                        raise SessionRuntimeError("Provider did not confirm cancellation")
                    time.sleep(0.05)
                break
            if self.thread is None or not self.thread.is_alive():
                raise SessionRuntimeError("Provider server disappeared during a turn")
            rows = self._messages(prefix)
            foreign = [
                row
                for row in rows
                if row.get("type") == "user"
                and row.get("id") not in before
                and row.get("id") != message_id
            ]
            if foreign:
                self.journal.append(self.sid, "native.foreign_input", foreign)
                self.controls.hold(self.sid, "Unexpected native input detected; automation paused.")
                continue
            for row in rows:
                if row.get("id") in before or row.get("type") != "assistant":
                    continue
                raw_time = row.get("time")
                time_info: dict[str, Any] = raw_time if isinstance(raw_time, dict) else {}
                if row.get("error") or (
                    time_info.get("completed") and row.get("finish") != "tool-calls"
                ):
                    completed = row
            own_messages = [
                row
                for row in rows
                if row.get("type") == "assistant" and row.get("id") not in before
            ]
            if completed and not self._active():
                break
            time.sleep(0.1)
        self.last_message = completed or {}
        if not own_messages and (interrupted or timed_out):
            own_messages = [
                row
                for row in self._messages(prefix)
                if row.get("type") == "assistant" and row.get("id") not in before
            ]
        text = assistant_text(self.last_message)
        usage = session_usage(self.last_message)
        error = self.last_message.get("error")
        success = bool(completed and not error and not interrupted and not timed_out)
        outcome = "interrupted" if interrupted else "succeeded" if success else "failed"
        self.confirm_snapshot(own_messages, outcome=outcome)
        self.terminal_status = "SUCCESS" if success else "INTERRUPTED" if interrupted else "FAILED"
        changes = GitManager.get_changed_paths(request.worktree_path)
        model_ref = self.last_message.get("model")
        model_ref = model_ref if isinstance(model_ref, dict) else {}
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
            actual_model=model_ref.get("id"),
            actual_provider=model_ref.get("providerID"),
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
                target = urllib.parse.urlsplit(self.path)
                path = target.path
                if path.startswith(API_SESSION + "/"):
                    session_id = path[len(API_SESSION) + 1 :].split("/")[0]
                    if session_id != "active" and session_id != runtime.native_id:
                        self.respond(403, {"error": "This terminal is scoped to one session"})
                        return
                allowed = (
                    "/api/event",
                    "/api/info",
                    "/api/config",
                    "/api/provider",
                    "/api/agent",
                    "/api/model",
                    "/api/command",
                    "/api/project",
                    "/api/location",
                    "/api/vcs",
                    "/api/fs/find",
                    "/api/fs/list",
                    "/api/permission/request",
                    "/api/permission/saved",
                    "/api/mcp",
                    "/api/skill",
                    "/api/plugin",
                    "/api/integration",
                    "/api/form",
                    "/api/reference",
                )
                if path != API_SESSION and not path.startswith(API_SESSION + "/"):
                    if not any(path == p or path.startswith(p + "/") for p in allowed):
                        self.respond(403, {"error": "Endpoint unavailable in managed terminal"})
                        return
                try:
                    if path == API_EVENT:
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        # Native clients bootstrap history with the message API.
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
                                            ("data: " + json.dumps(frame) + "\n\n").encode()
                                        )
                                self.wfile.write(b": heartbeat\n\n")
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
                    data = runtime.api("GET", path + _pinned_query(target.query))
                    if path == API_SESSION:
                        rows = data.get("data") if isinstance(data, dict) else data
                        if isinstance(rows, list):
                            scoped = [row for row in rows if row.get("id") == runtime.native_id]
                            data = (
                                {**data, "data": scoped} if isinstance(data, dict) else scoped
                            )
                    elif path == API_ACTIVE:
                        active = data.get("data") if isinstance(data, dict) else data
                        if isinstance(active, dict):
                            data = {"data": {k: v for k, v in active.items()
                                             if k == runtime.native_id}}
                    self.respond(200, data)
                except (OSError, ValueError):
                    self.respond(503, {"error": "Provider session unavailable"})

            def do_POST(self) -> None:
                if not self.authenticated():
                    self.respond(401, {"error": "Native client authentication required"})
                    return
                path = urllib.parse.urlsplit(self.path).path
                prefix = API_SESSION + "/" + str(runtime.native_id)
                try:
                    if path == prefix + "/interrupt":
                        state = runtime.controls.get(runtime.sid)
                        active = bool(state and state["mode"] in ("ACTIVE", "REQUESTED"))
                        if active:
                            runtime.controls.hold(
                                runtime.sid, "Native terminal requested interruption."
                            )
                        else:
                            runtime.controls.interrupt(runtime.sid)
                        self.respond(200, {"interrupted": active})
                        return
                    if path != prefix + "/prompt":
                        raise ControlConflict("This operation is outside the frozen assignment")
                    length = int(self.headers.get("Content-Length", "0"))
                    if length <= 0 or length > 128000:
                        raise ControlConflict("Native message is too large or missing")
                    body = json.loads(self.rfile.read(length))
                    if body.get("files") or body.get("agents") or body.get("skills"):
                        raise ControlConflict("Managed native messages currently support text only")
                    text = body.get("text")
                    if not isinstance(text, str) or not text.strip():
                        raise ControlConflict("Managed native messages currently support text only")
                    request_id = body.get("id") or "msg_" + uuid.uuid4().hex
                    native_parts = [{"type": "text", "text": text}]
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
                    self.respond(
                        200,
                        {
                            "data": {
                                "id": str(request_id),
                                "sessionID": runtime.native_id,
                                "time": {"created": int(time.time() * 1000)},
                                "type": "user",
                                "payload": {"text": text},
                                "delivery": "steer",
                            }
                        },
                    )
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
        # The owned config/home root lives through the whole server lifetime
        # and is removed on shutdown (normal, error, cancellation or close).
        owned, self.isolation = self.isolation, None
        if owned is not None:
            owned.close()
        self.publish({"native_attach_ready": False})
        return safe


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self, req: urllib.request.Request, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def permission_rules(permissions: dict[str, str]) -> list[dict[str, str]]:
    """Project the frozen permission map onto the v2.3 ``Permission.Ruleset``.

    The owner policy maps a permission name (``"*"``, ``"external_directory"``)
    to an effect; every rule applies to every resource.
    """
    return [
        {"action": action, "resource": "*", "effect": effect}
        for action, effect in permissions.items()
    ]


def native_model(
    model: str, reasoning_effort: ReasoningEffort | None = None
) -> dict[str, str]:
    """Return the v2.3 ``Model.Ref`` for a ``provider/model`` identity."""
    provider, separator, identifier = model.partition("/")
    if not separator or not identifier:
        raise SessionRuntimeError("OpenCode requires a provider/model identity")
    ref = {"providerID": provider, "id": identifier}
    if reasoning_effort is not None:
        ref["variant"] = reasoning_effort.value.lower()
    return ref


def event_session_id(event: dict[str, Any]) -> str | None:
    """Return the v2.3 event's owning session id, when it names one."""
    data = event.get("data")
    if isinstance(data, dict):
        for key in ("sessionID", "sessionId"):
            value = data.get(key)
            if isinstance(value, str):
                return value
        info = data.get("session")
        if isinstance(info, dict):
            value = info.get("id")
            if isinstance(value, str):
                return value
    return None


def event_message_id(event: dict[str, Any]) -> str | None:
    """Return the message an event edits (``assistantMessageID``/``messageID``)."""
    data = event.get("data")
    if not isinstance(data, dict):
        return None
    for key in ("assistantMessageID", "messageID", "messageId"):
        value = data.get(key)
        if isinstance(value, str):
            return value
    message = data.get("message")
    if isinstance(message, dict):
        value = message.get("id")
        if isinstance(value, str):
            return value
    return None


def assistant_text(message: dict[str, Any]) -> str:
    """Join the assistant message's text content (v2.3 ``content`` array)."""
    content = message.get("content")
    if not isinstance(content, list):
        return ""
    return "\n".join(
        str(part["text"])
        for part in content
        if isinstance(part, dict)
        and part.get("type") == "text"
        and isinstance(part.get("text"), str)
    )


def session_usage(message: dict[str, Any]) -> list[ProviderUsage]:
    """Return exact usage from the v2.3 assistant message token counters."""
    tokens = message.get("tokens")
    if not isinstance(tokens, dict):
        return [ProviderUsage(usage_source="opencode_session_message_without_usage")]
    cache = tokens.get("cache")
    cache_dict = cache if isinstance(cache, dict) else {}
    cache_read = _integer(cache_dict.get("read"))
    return [
        ProviderUsage(
            input_tokens=_integer(tokens.get("input")),
            output_tokens=_integer(tokens.get("output")),
            cached_tokens=cache_read,
            cache_read_tokens=cache_read,
            cache_write_tokens=_integer(cache_dict.get("write")),
            reasoning_tokens=_integer(tokens.get("reasoning")),
            usage_source="opencode_session_message_tokens",
            raw_payload=json.dumps({"tokens": tokens}),
        )
    ]


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _pinned_query(query: str) -> str:
    """Keep client query parameters but never let a client choose the location."""
    kept = [
        (key, value)
        for key, value in urllib.parse.parse_qsl(query, keep_blank_values=True)
        if key not in ("directory", "location[directory]", "location")
    ]
    encoded = urllib.parse.urlencode(kept)
    return "?" + encoded if encoded else ""
