"""Regression: the OpenCode session runtime speaks the v2.3 ``/api/*`` surface.

The v2.3.2 server has no ``/global/health`` route (the web handler answers SPA
HTML there).  Run ``run_70ead49549d5_71a86a5c40`` proved that polling it left
the worker unstarted until the 20 s deadline.  These tests drive the runtime
against a fake server that serves *only* the v2.3 ``/api/*`` routes taken from
the checked-in OpenAPI fixture, so the old readiness probe cannot pass here.
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from saberops.models import AttemptStatus, WorkerRequest
from saberops.session_control import SessionControls
from saberops.session_journal import SessionJournal
from saberops.session_runtime import (
    REQUIRED_SERVER_ROUTES,
    AgentSessionRuntime,
    SessionRuntimeError,
    require_supported_server,
)

FIXTURE = (
    Path(__file__).parent
    / "fixtures/vendor_cli/opencode/opencode-v2.3.2/openapi.json"
)
SPA_HTML = b"<!doctype html><html><body>OpenCode web</body></html>"


class FakeV232Server:
    """Minimal v2.3.2 HTTP server: only the routes the runtime requires."""

    def __init__(self) -> None:
        self.sessions: dict[str, dict[str, Any]] = {}
        self.messages: dict[str, list[dict[str, Any]]] = {}
        self.active: set[str] = set()
        self.requests: list[str] = []
        self.worktree: Path | None = None
        self.prompts: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return int(self.server.server_port)

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def reply(self, status: int, payload: object) -> None:
                raw = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                path = parsed.path
                query = urllib.parse.parse_qs(parsed.query)
                with fake.lock:
                    fake.requests.append(path + ("?" + parsed.query if parsed.query else ""))
                if path == "/api/info":
                    self.reply(
                        200,
                        {"version": "2.3.2", "pid": 1, "urls": [], "paths": {"tmp": "/tmp"}},
                    )
                    return
                if path == "/api/event":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    try:
                        while not fake.server._BaseServer__shutdown_request:  # type: ignore[attr-defined]
                            self.wfile.write(b": heartbeat\n\n")
                            self.wfile.flush()
                            threading.Event().wait(0.2)
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    return
                if path == "/api/session/active":
                    with fake.lock:
                        data = {sid: {"type": "running"} for sid in fake.active}
                    self.reply(200, {"data": data})
                    return
                if path == "/api/session":
                    with fake.lock:
                        rows = list(fake.sessions.values())
                    self.reply(200, {"data": rows, "cursor": {"previous": None, "next": None}})
                    return
                if path.startswith("/api/session/"):
                    rest = path[len("/api/session/") :]
                    session_id, _, tail = rest.partition("/")
                    with fake.lock:
                        session = fake.sessions.get(session_id)
                        messages = list(fake.messages.get(session_id, []))
                    if session is None:
                        self.reply(404, {"_tag": "session.not_found"})
                        return
                    if not tail:
                        self.reply(200, {"data": session})
                        return
                    if tail == "message":
                        # v2.3 returns a ``next`` anchor even on a complete
                        # page; following it past the end yields an empty
                        # page whose ``next`` is also an anchor.
                        if query.get("cursor"):
                            self.reply(
                                200,
                                {"data": [], "cursor": {"previous": None, "next": None}},
                            )
                        elif messages:
                            self.reply(
                                200,
                                {"data": messages, "cursor": {"previous": None,
                                                              "next": "next-anchor"}},
                            )
                        else:
                            self.reply(
                                200,
                                {"data": [], "cursor": {"previous": None, "next": None}},
                            )
                        return
                # The real v2.3 server answers anything else with SPA HTML.
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.send_header("Content-Length", str(len(SPA_HTML)))
                self.end_headers()
                self.wfile.write(SPA_HTML)

            def do_POST(self) -> None:
                path = urllib.parse.urlsplit(self.path).path
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length)) if length else {}
                with fake.lock:
                    fake.requests.append(path)
                if path == "/api/session":
                    session_id = "ses_fake0001"
                    worktree = body["location"]["directory"]
                    fake.worktree = Path(worktree)
                    session = {
                        "id": session_id,
                        "projectID": "proj_fake",
                        "agent": body.get("agent"),
                        "model": body.get("model"),
                        "cost": 0,
                        "tokens": {"input": 0, "output": 0, "reasoning": 0,
                                   "cache": {"read": 0, "write": 0}},
                        "time": {"created": 1, "updated": 1},
                        "title": body.get("title", ""),
                        "permissions": body.get("permissions", []),
                        "location": {"directory": worktree},
                    }
                    with fake.lock:
                        fake.sessions[session_id] = session
                        fake.messages[session_id] = []
                    self.reply(200, {"data": session})
                    return
                if path.startswith("/api/session/"):
                    rest = path[len("/api/session/") :]
                    session_id, _, tail = rest.partition("/")
                    if tail == "prompt":
                        with fake.lock:
                            fake.prompts.append(body)
                            fake.messages[session_id] = [
                                {
                                    "id": body["id"],
                                    "type": "user",
                                    "time": {"created": 2},
                                    "text": body["text"],
                                },
                                {
                                    "id": "msg_fake_reply",
                                    "type": "assistant",
                                    "time": {"created": 3, "completed": 4},
                                    "finish": "stop",
                                    "agent": "saberops",
                                    "model": {"id": "deepseek-v4.1-flash",
                                              "providerID": "opencode-go"},
                                    "content": [
                                        {"type": "text", "text": "Hello from the fake"},
                                        {"type": "tool", "id": "tool_1", "name": "write",
                                         "state": {"status": "completed"}, "time": {"created": 3}},
                                        {"type": "text", "text": "All done"},
                                    ],
                                    "tokens": {"input": 11, "output": 5, "reasoning": 0,
                                               "cache": {"read": 2, "write": 0}},
                                },
                            ]
                            fake.active.discard(session_id)
                            worktree = fake.worktree
                        if worktree is not None:
                            (worktree / "worker-output.txt").write_text("written\n")
                        self.reply(
                            200,
                            {
                                "data": {
                                    "id": body["id"],
                                    "sessionID": session_id,
                                    "time": {"created": 2},
                                    "type": "user",
                                    "payload": {"text": body["text"]},
                                    "delivery": "steer",
                                }
                            },
                        )
                        return
                    if tail == "interrupt":
                        self.reply(200, {"interrupted": False})
                        return
                self.reply(404, {"_tag": "not_found"})

        return Handler


def _fake_binary(path: Path, port: int) -> Path:
    script = path / "opencode"
    script.write_text(
        "#!/bin/sh\n"
        f'echo "server listening on http://127.0.0.1:{port}"\n'
        "exec sleep 60\n"
    )
    script.chmod(0o755)
    return script


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)


@pytest.fixture()
def fake(monkeypatch: pytest.MonkeyPatch) -> Any:
    server = FakeV232Server()
    yield server
    server.close()


def test_runtime_starts_and_finishes_on_a_v232_only_server(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake: Any
) -> None:
    worktree = tmp_path / "repo"
    worktree.mkdir()
    _git(worktree, "init", "-b", "main")
    (worktree / "README.md").write_text("base\n")
    _git(worktree, "add", ".")
    _git(worktree, "-c", "user.name=Test", "-c", "user.email=test@localhost",
         "commit", "-m", "base")

    binary = _fake_binary(tmp_path, fake.port)
    controls = SessionControls(tmp_path / "controls")
    controls.register("sid", "run", "{}", "worker")
    journal = SessionJournal(tmp_path / "controls")
    journal.register("sid", "run", "worker", "a1", "{}")
    published: list[dict[str, Any]] = []
    runtime = AgentSessionRuntime(
        tmp_path / "controls", "sid", controls, journal, published.append
    )
    monkeypatch.setenv("TMPDIR", "/var/tmp")
    request = WorkerRequest(
        "Write the file.",
        worktree,
        "opencode-go/deepseek-v4.1-flash",
        timeout_seconds=15,
        env={"PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "HOME": str(tmp_path)},
    )
    try:
        result = runtime.run_opencode(request, str(binary))
    finally:
        runtime.close()

    assert result.status == AttemptStatus.SUCCESS, result.error
    assert result.stdout == "Hello from the fake\nAll done"
    assert result.changed_paths == ["worker-output.txt"]
    assert result.usage and result.usage[0].input_tokens == 11
    assert result.usage[0].cache_read_tokens == 2
    assert result.actual_model == "deepseek-v4.1-flash"
    # The v2.3 readiness probe is /api/info; the retired path is never used.
    assert any(path.startswith("/api/info") for path in fake.requests)
    assert "/global/health" not in fake.requests
    assert all(path.startswith("/api/") or path == "/openapi.json"
               for path in fake.requests)
    # Pagination followed the anchor and never combined it with `order`.
    assert any("cursor=next-anchor" in path for path in fake.requests)
    assert not any("order=" in path and "cursor=" in path for path in fake.requests)
    # The prompt is text-only and carries the generated message identity.
    assert fake.prompts and fake.prompts[0]["text"] == "Write the file."
    assert re.fullmatch(r"msg_[A-Za-z0-9_-]+", fake.prompts[0]["id"])
    assert published[0]["runtime_version"] == "2.3.2"
    assert any(item.get("native_id") == "ses_fake0001" for item in published)


def test_v232_fixture_pins_every_route_the_runtime_calls() -> None:
    document = json.loads(FIXTURE.read_text())
    assert document["paths"]
    for route in REQUIRED_SERVER_ROUTES:
        assert route in document["paths"], route
    assert "/global/health" not in document["paths"]


def test_retired_health_probe_cannot_read_the_v232_server(fake: Any) -> None:
    """The pre-fix readiness poll fails on a real v2.3.2 server.

    ``/global/health`` is not an API route: the web handler answers SPA HTML,
    so the old ``_api("GET", "/global/health")`` JSON parse never succeeds.
    """
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{fake.port}/global/health") as response:
        assert response.headers["Content-Type"].startswith("text/html")
        with pytest.raises(ValueError):
            json.loads(response.read())


@pytest.mark.parametrize("version", ["2.0.29", "v2.0.29", "1.9.0", None, "unknown"])
def test_older_or_unknown_server_versions_are_refused_clearly(version: object) -> None:
    with pytest.raises(SessionRuntimeError, match=r"requires\s+the v2\.3\+ /api/\* server surface"):
        require_supported_server(version)


def test_v23_server_versions_are_supported() -> None:
    assert require_supported_server("2.3.2")[:2] == (2, 3)
    assert require_supported_server("v2.4.0")[:2] == (2, 4)


def test_message_pagination_never_combines_order_with_cursor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """v2.3 rejects ``cursor`` + ``order``; the anchor ends on an empty page."""
    runtime = AgentSessionRuntime(
        tmp_path, "sid", SessionControls(tmp_path), SessionJournal(tmp_path), lambda _: None
    )
    runtime.native_id = "ses_one"
    runtime.request = WorkerRequest("task", tmp_path, "provider/model")
    calls: list[str] = []

    def api(method: str, path: str, body: object = None, **kwargs: Any) -> Any:
        calls.append(path)
        if "cursor=c1" in path:
            return {"data": [{"id": "msg_2", "type": "user"}],
                    "cursor": {"previous": None, "next": "c2"}}
        if "cursor=c2" in path:
            return {"data": [], "cursor": {"previous": None, "next": "c3"}}
        return {"data": [{"id": "msg_1", "type": "user"}],
                "cursor": {"previous": None, "next": "c1"}}

    monkeypatch.setattr(runtime, "api", api)
    rows = runtime._messages("/api/session/ses_one")
    assert [row["id"] for row in rows] == ["msg_1", "msg_2"]
    assert not any("order=" in path and "cursor=" in path for path in calls)
    assert len(calls) == 3  # first page, one continuation, then the empty end
