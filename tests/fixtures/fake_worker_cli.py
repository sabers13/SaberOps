#!/usr/bin/env python3
"""Deterministic fake worker and reviewer CLI for SaberOps lifecycle tests.

Emulates the local CLI wire contracts of ``codex``, ``opencode``, and ``claude``
without contacting any vendor endpoint.  Supports failure switches for
hanging without output, crashing mid-task (non-zero exit), and emitting
quota-exhaustion signals recognized by ``saberops.workers.exhaustion``.
"""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

_CHALLENGE_RE = re.compile(r"SABEROPS_ACCESS_[0-9a-f]+")


def _pop_flag_value(argv: list[str], flag: str) -> str | None:
    """Remove ``--flag value`` or ``--flag=value`` from *argv* and return value."""
    for idx, token in enumerate(argv):
        if token == flag and idx + 1 < len(argv):
            val = argv[idx + 1]
            del argv[idx : idx + 2]
            return val
        if token.startswith(f"{flag}="):
            val = token.split("=", 1)[1]
            del argv[idx]
            return val
    return None


def _get_arg_value(argv: SequenceLike, flag: str) -> str | None:
    for idx, token in enumerate(argv):
        if token == flag and idx + 1 < len(argv):
            return argv[idx + 1]
        if token.startswith(f"{flag}="):
            return token.split("=", 1)[1]
    return None


SequenceLike = list[str] | tuple[str, ...]


def _read_control_json(path_str: str | None) -> dict[str, Any]:
    if not path_str:
        return {}
    path = Path(path_str)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _reported_identity(harness: str) -> tuple[str, str] | None:
    """Return the provider/model identity the fake CLI should report, if any."""
    return _identity_from(_read_control_json(_worker_control_path()), harness)


def _identity_from(cfg: dict[str, Any], harness: str) -> tuple[str, str] | None:
    raw_model = cfg.get("report_model")
    if not (isinstance(raw_model, str) and raw_model.strip()):
        return None
    raw_provider = cfg.get("report_provider")
    provider = (
        raw_provider.strip()
        if isinstance(raw_provider, str) and raw_provider.strip()
        else f"{harness}-fake"
    )
    return provider, raw_model.strip()


def _control_turn(index: int) -> dict[str, Any]:
    """Return per-turn control overrides for the long-lived ``serve`` mode.

    ``turns`` is a list applied in order to each prompt the server receives,
    so one test run can drive several attempts with distinct behaviour even
    though it shares one server process.
    """
    cfg = _read_control_json(_worker_control_path())
    turns = cfg.get("turns")
    if isinstance(turns, list) and index < len(turns) and isinstance(turns[index], dict):
        return dict(turns[index])
    return {}


def _next_turn_index() -> int:
    """Return this prompt's ordinal, persisted beside the control file.

    The OpenCode server may be restarted between attempts, so the ordinal
    must survive the process; a small counter file does that.
    """
    control = _worker_control_path()
    if control is None:
        return 0
    path = Path(control).with_name("fake-worker-turns")
    try:
        index = int(path.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        index = 0
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(index + 1), encoding="utf-8")
    except OSError:
        pass
    return index


def _worker_control_path() -> str | None:
    control_path = os.environ.get("ORCH_FAKE_WORKER_CONTROL_FILE")
    if control_path:
        return control_path
    state_home = os.environ.get("XDG_STATE_HOME")
    if state_home:
        return str(Path(state_home) / "saberops" / "fake-worker-mode.json")
    return None


def _resolve_worker_mode(
    cli_mode: str | None, harness: str
) -> tuple[str, float, int, str, str | None]:
    cfg = _read_control_json(_worker_control_path())

    # ``per_harness`` lets one lane's CLI fail while another keeps working, so a
    # failover test can exhaust a single subscription.  It outranks the generic
    # environment/control mode; an explicit ``--fake-mode`` still wins.
    per_harness = cfg.get("per_harness")
    harness_mode = None
    if isinstance(per_harness, dict):
        raw = per_harness.get(harness)
        if raw is not None:
            harness_mode = str(raw)

    mode = (
        cli_mode
        or harness_mode
        or os.environ.get("ORCH_FAKE_WORKER_MODE")
        or str(cfg.get("mode", "commit"))
    ).strip().lower()
    hang_seconds = float(
        os.environ.get("ORCH_FAKE_WORKER_HANG_SECONDS")
        or cfg.get("hang_seconds", 60.0)
    )
    exit_code = int(
        os.environ.get("ORCH_FAKE_WORKER_EXIT_CODE")
        or cfg.get("exit_code", 2)
    )
    target_file = str(
        os.environ.get("ORCH_FAKE_WORKER_FILE")
        or cfg.get("target_file", "fake_worker_output.txt")
    )
    content = os.environ.get("ORCH_FAKE_WORKER_CONTENT") or (
        str(cfg["content"]) if "content" in cfg and cfg["content"] is not None else None
    )
    return mode, hang_seconds, exit_code, target_file, content


def _resolve_reviewer_verdict(cli_verdict: str | None) -> str:
    control_path = os.environ.get("ORCH_FAKE_REVIEWER_CONTROL_FILE")
    if not control_path:
        state_home = os.environ.get("XDG_STATE_HOME")
        if state_home:
            control_path = str(Path(state_home) / "saberops" / "fake-reviewer-mode.json")
    cfg = _read_control_json(control_path)
    raw = (
        cli_verdict
        or os.environ.get("ORCH_FAKE_REVIEWER_VERDICT")
        or str(cfg.get("verdict", "PASS"))
    ).strip().upper()
    if raw in {"REJECT", "BLOCK"}:
        return "BLOCK"
    return "PASS"


def _codex_prompt_arg(args: list[str]) -> str:
    """Return the positional prompt Codex was given (``""`` when it is stdin)."""
    flag_values = {"--model", "--cd", "--sandbox", "--image", "-c", "--config"}
    try:
        index = args.index("exec") + 1
    except ValueError:
        return ""
    if index < len(args) and args[index] == "resume":
        index += 1
        if index < len(args) and not args[index].startswith("-"):
            index += 1
    while index < len(args):
        token = args[index]
        if token == "-":
            return ""
        if token in flag_values:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return ""


def _opencode_prompt_arg(args: list[str]) -> str:
    """Return the positional prompt OpenCode was given (``""`` when it is stdin)."""
    flag_values = {"--model", "--format", "--session", "--dir", "--agent"}
    index = 0
    while index < len(args):
        token = args[index]
        if token == "run":
            index += 1
            continue
        if token in flag_values:
            index += 2
            continue
        if token.startswith("-"):
            index += 1
            continue
        return token
    return ""


def _prompt_log_append(harness: str, prompt: str) -> None:
    """Append one received prompt to the test log named by the rig, if any."""
    cfg = _read_control_json(_worker_control_path())
    path_str = os.environ.get("ORCH_FAKE_WORKER_PROMPT_LOG") or str(
        cfg.get("prompt_log") or ""
    )
    if not path_str:
        return
    path = Path(path_str)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as sink:
            sink.write(
                json.dumps(
                    {"harness": harness, "pid": os.getpid(), "prompt": prompt},
                    sort_keys=True,
                )
                + "\n"
            )
    except OSError:
        pass


def _wait_for_control_file() -> None:
    """Block a worker run until the rig's marker file exists (bounded)."""
    cfg = _read_control_json(_worker_control_path())
    path_str = os.environ.get("ORCH_FAKE_WORKER_WAIT_FILE") or str(
        cfg.get("wait_file") or ""
    )
    if not path_str:
        return
    timeout = float(
        os.environ.get("ORCH_FAKE_WORKER_WAIT_TIMEOUT")
        or cfg.get("wait_timeout_seconds", 30)
    )
    path = Path(path_str)
    deadline = time.monotonic() + timeout
    while not path.exists() and time.monotonic() < deadline:
        time.sleep(0.02)


def _commit_change(workdir: Path, harness: str, target_file: str, content: str | None) -> None:
    target = workdir / target_file
    target.parent.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex[:8]
    line = content if content is not None else f"{harness}:{nonce}:worktree-change\n"
    if not line.endswith("\n"):
        line += "\n"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.write_text(existing + line, encoding="utf-8")
    subprocess.run(["git", "-C", str(workdir), "add", target_file], check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(workdir),
            "-c",
            "user.name=Fake Worker",
            "-c",
            "user.email=fake-worker@example.com",
            "commit",
            "-m",
            f"fake {harness} worker commit ({nonce})",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _write_partial_change(
    workdir: Path, harness: str, target_file: str, content: str | None
) -> None:
    """Leave an uncommitted worktree change, the way a worker interrupted mid-task would."""
    target = workdir / target_file
    target.parent.mkdir(parents=True, exist_ok=True)
    nonce = uuid.uuid4().hex[:8]
    line = content if content is not None else f"{harness}:{nonce}:partial-work\n"
    if not line.endswith("\n"):
        line += "\n"
    existing = target.read_text(encoding="utf-8") if target.is_file() else ""
    target.write_text(existing + line, encoding="utf-8")


def _run_failure_switch(
    harness: str,
    mode: str,
    hang_seconds: float,
    exit_code: int,
    *,
    workdir: Path | None = None,
    target_file: str = "fake_worker_output.txt",
    content: str | None = None,
) -> int | None:
    if mode == "live":
        # Emit line-oriented output while the process is still running so a
        # second session can tail the run transcript mid-flight.  Execution
        # continues normally afterwards (the usual commit walk).
        lines = int(os.environ.get("ORCH_FAKE_WORKER_LIVE_LINES") or 0)
        interval = float(os.environ.get("ORCH_FAKE_WORKER_LIVE_INTERVAL") or 0.2)
        for index in range(lines):
            print(
                f"fake {harness} live line {index + 1}/{lines}",
                flush=True,
            )
            if interval > 0:
                time.sleep(interval)
        return None
    if mode == "hang":
        time.sleep(hang_seconds)
        return 0
    if mode == "crash":
        sys.stderr.write(f"fake {harness} worker crashed mid-task\n")
        sys.stderr.flush()
        return exit_code if exit_code != 0 else 2
    if mode in {"quota_exhausted", "partial_quota"}:
        if mode == "partial_quota" and workdir is not None:
            # A real quota exhaustion arrives mid-turn: the worker has already
            # edited files but never committed them.
            _write_partial_change(workdir, harness, target_file, content)
        if harness == "codex":
            print(
                json.dumps(
                    {
                        "type": "turn.failed",
                        "error": {
                            "message": (
                                "You've hit your usage limit. "
                                "Please try again at Nov 20, 2026 12:00 PM."
                            )
                        },
                    }
                ),
                flush=True,
            )
        elif harness == "opencode":
            print(
                json.dumps(
                    {
                        "type": "error",
                        "error": {
                            "type": "provider.quota",
                            "message": "Monthly usage limit reached. Resets in 60 minutes",
                        },
                    }
                ),
                flush=True,
            )
        else:
            print(
                json.dumps(
                    {
                        "type": "result",
                        "subtype": "success",
                        "is_error": True,
                        "api_error_status": 429,
                        "result": "You've hit your session limit · resets 10:00pm (UTC)",
                    }
                ),
                flush=True,
            )
        return 1
    return None


def _handle_codex(
    args: list[str],
    *,
    mode: str,
    hang_seconds: float,
    exit_code: int,
    target_file: str,
    content: str | None,
) -> int:
    if args[:2] == ["doctor", "--json"]:
        print(
            json.dumps(
                {
                    "schemaVersion": 1,
                    "overallStatus": "ok",
                    "checks": {"auth.credentials": {"status": "ok"}},
                }
            )
        )
        return 0
    if args[:2] == ["debug", "models"]:
        print(json.dumps({"models": [{"id": "gpt-5.6-terra"}, {"id": "gpt-5.6-sol"}]}))
        return 0

    cd_val = _get_arg_value(args, "--cd")
    workdir = Path(cd_val) if cd_val else Path.cwd()
    prompt_text = ""
    if "-" in args and not sys.stdin.isatty():
        prompt_text = sys.stdin.read()
    else:
        prompt_text = _codex_prompt_arg(args)
    _prompt_log_append("codex", prompt_text)
    _wait_for_control_file()
    switched = _run_failure_switch(
        "codex",
        mode,
        hang_seconds,
        exit_code,
        workdir=workdir,
        target_file=target_file,
        content=content,
    )
    if switched is not None:
        return switched

    sandbox = _get_arg_value(args, "--sandbox")
    control = _read_control_json(_worker_control_path())
    ignore_read_only = bool(control.get("ignore_read_only"))
    if sandbox != "read-only" or ignore_read_only:
        _commit_change(workdir, "codex", target_file, content)
    report_text = control.get("report_text")
    message_text = (
        report_text if isinstance(report_text, str) and report_text.strip()
        else "Fake codex worker completed."
    )
    print(json.dumps({"type": "turn.started"}))
    print(
        json.dumps(
            {
                "type": "item.completed",
                "item": {"type": "agent_message", "text": message_text},
            }
        )
    )
    print(json.dumps({"type": "turn.completed", "usage": {"input_tokens": 12, "output_tokens": 8}}))
    return 0


def _serve_opencode(
    *,
    mode: str,
    hang_seconds: float,
    exit_code: int,
    target_file: str,
    content: str | None,
) -> int:
    """Minimal HTTP server emulating ``opencode serve`` for ``OpenCodeServerSession``."""
    session_id = "ses_fakeopencode01"
    workdir = Path.cwd()
    messages: list[dict[str, Any]] = []
    lock = threading.Lock()
    state = {"status": "idle"}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            pass

        def _send_json(self, code: int, payload: object) -> None:
            raw = b"" if code == 204 else json.dumps(payload).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            if raw:
                self.wfile.write(raw)

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/global/health":
                self._send_json(200, {"healthy": True, "version": "2.0.29-fake"})
                return
            if path == "/session/status":
                with lock:
                    current = state["status"]
                self._send_json(200, {session_id: {"type": current}})
                return
            if path == f"/session/{session_id}":
                self._send_json(200, {"id": session_id, "directory": str(workdir)})
                return
            if path == f"/session/{session_id}/message":
                with lock:
                    snapshot = list(messages)
                self._send_json(200, snapshot)
                return
            if path == "/event":
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                event = json.dumps(
                    {
                        "type": "session.status",
                        "properties": {"sessionID": session_id, "status": {"type": "idle"}},
                    }
                )
                try:
                    self.wfile.write(f"data: {event}\n\n".encode())
                    self.wfile.flush()
                except OSError:
                    pass
                return
            self._send_json(404, {"error": f"not found: {path}"})

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw_body = self.rfile.read(length) if length > 0 else b"{}"
            try:
                body = json.loads(raw_body.decode("utf-8")) if raw_body else {}
            except ValueError:
                body = {}

            if path == "/session":
                self._send_json(200, {"id": session_id})
                return
            if path == f"/session/{session_id}/abort":
                with lock:
                    state["status"] = "idle"
                self._send_json(200, {})
                return
            if path == f"/session/{session_id}/prompt_async":
                message_id = str(body.get("messageID") or f"msg_{uuid.uuid4().hex}")
                turn_cfg = _control_turn(_next_turn_index())
                turn_mode = str(turn_cfg.get("mode", mode)).strip().lower()
                if turn_mode == "hang":
                    with lock:
                        state["status"] = "busy"
                    self._send_json(204, None)
                    time.sleep(hang_seconds)
                    return
                if turn_mode == "crash":
                    self._send_json(500, {"error": f"Fake opencode crash (code {exit_code})"})
                    return
                if turn_mode in {"quota_exhausted", "partial_quota"}:
                    if turn_mode == "partial_quota":
                        # Interrupted mid-turn: uncommitted work, then the limit.
                        try:
                            _write_partial_change(workdir, "opencode", target_file, content)
                        except OSError:
                            pass
                    with lock:
                        messages.append(
                            {
                                "info": {
                                    "id": f"msg_err_{len(messages) + 1}",
                                    "role": "assistant",
                                    "parentID": message_id,
                                    "time": {"completed": 1},
                                    "finish": "error",
                                    "error": {
                                        "name": "APIError",
                                        "data": {
                                            "message": "429 quota exceeded / rate limit reached"
                                        },
                                    },
                                },
                                "parts": [
                                    {
                                        "type": "text",
                                        "text": "429 quota exceeded / rate limit reached",
                                    }
                                ],
                            }
                        )
                        state["status"] = "idle"
                    self._send_json(204, None)
                    return

                _commit_change(workdir, "opencode", target_file, content)
                reported = _identity_from(turn_cfg, "opencode") or _reported_identity(
                    "opencode"
                )
                info: dict[str, Any] = {
                    "id": f"msg_resp_{len(messages) + 1}",
                    "role": "assistant",
                    "parentID": message_id,
                    "time": {"completed": 1},
                    "finish": "stop",
                }
                if reported is not None:
                    info["providerID"], info["modelID"] = reported
                with lock:
                    messages.append(
                        {
                            "info": info,
                            "parts": [
                                {"type": "text", "text": "Fake opencode completed."},
                            ],
                        }
                    )
                    state["status"] = "idle"
                self._send_json(204, None)
                return

            self._send_json(404, {"error": f"not found: {path}"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    port = server.server_address[1]
    stop_event = threading.Event()

    def _shutdown(*_args: object) -> None:
        stop_event.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"opencode server listening on http://127.0.0.1:{port}", flush=True)
    try:
        server.serve_forever(poll_interval=0.05)
    finally:
        server.server_close()
    return 0


def _handle_opencode(
    args: list[str],
    *,
    mode: str,
    hang_seconds: float,
    exit_code: int,
    target_file: str,
    content: str | None,
) -> int:
    if args and args[0] == "models":
        print("opencode-go/deepseek-v4-flash")
        print("opencode-go/mimo-v2.5")
        return 0

    if args and args[0] == "serve":
        return _serve_opencode(
            mode=mode,
            hang_seconds=hang_seconds,
            exit_code=exit_code,
            target_file=target_file,
            content=content,
        )

    agent = _get_arg_value(args, "--agent")
    if agent == "saberops-access-check":
        joined = " ".join(args)
        match = _CHALLENGE_RE.search(joined)
        challenge = match.group(0) if match else "OK"
        print(json.dumps({"type": "step_start", "part": {"type": "step-start"}}))
        print(json.dumps({"type": "text", "part": {"type": "text", "text": challenge}}))
        print(
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "type": "step-finish",
                        "reason": "stop",
                        "tokens": {"input": 4, "output": 2, "total": 6},
                    },
                }
            )
        )
        return 0

    switched = _run_failure_switch(
        "opencode",
        mode,
        hang_seconds,
        exit_code,
        workdir=Path.cwd(),
        target_file=target_file,
        content=content,
    )

    stdin_text = ""
    if not sys.stdin.isatty():
        stdin_text = sys.stdin.read()
    _prompt_log_append("opencode", _opencode_prompt_arg(args) or stdin_text)
    _wait_for_control_file()
    if switched is not None:
        return switched
    _commit_change(Path.cwd(), "opencode", target_file, content)
    start_part: dict[str, Any] = {"type": "step-start"}
    identity = _reported_identity("opencode")
    if identity is not None:
        provider, model = identity
        start_part.update({"providerID": provider, "modelID": model})
    print(json.dumps({"type": "step_start", "part": start_part}))
    print(
        json.dumps({"type": "text", "part": {"type": "text", "text": "Fake opencode completed."}})
    )
    print(
        json.dumps(
            {
                "type": "step_finish",
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {"input": 12, "output": 8, "total": 20},
                },
            }
        )
    )
    return 0


def _review_text(verdict: str) -> str:
    if verdict == "BLOCK":
        findings_block = {
            "summary": "Fake reviewer rejected candidate.",
            "findings": [
                {
                    "id": "F1",
                    "classification": "B1",
                    "title": "Fake reviewer rejection requested",
                    "severity": "high",
                    "contract_reference": "fake-reviewer-instruction",
                    "invariant_or_requirement": "Candidate must pass fake reviewer policy",
                    "code_location": None,
                    "failure_scenario": "Candidate was instructed to be rejected by fake reviewer.",
                    "evidence": "Fake reviewer control instructed rejection.",
                    "minimum_correction": "Update candidate and re-submit for review.",
                    "repair_scope": "LOCAL_MECHANICAL",
                }
            ],
        }
        return (
            "VERDICT: BLOCK\n"
            "Fake reviewer rejected candidate.\n\n"
            f"```json\n{json.dumps(findings_block)}\n```\n"
        )
    findings_block = {"summary": "Fake reviewer approved candidate.", "findings": []}
    return (
        "VERDICT: PASS\n"
        "Fake reviewer approved candidate.\n\n"
        f"```json\n{json.dumps(findings_block)}\n```\n"
    )


def _handle_claude(
    args: list[str],
    *,
    mode: str,
    hang_seconds: float,
    exit_code: int,
    review_verdict: str,
) -> int:
    if "auth" in args and "status" in args:
        print(
            json.dumps(
                {
                    "loggedIn": True,
                    "authMethod": "claude.ai",
                    "apiProvider": "firstParty",
                }
            )
        )
        return 0

    input_fmt = _get_arg_value(args, "--input-format")
    if input_fmt == "stream-json" and _get_arg_value(args, "--tools") == "":
        raw_line = sys.stdin.readline()
        req_id = "saberops-models"
        if raw_line:
            try:
                msg = json.loads(raw_line)
                if isinstance(msg, dict) and isinstance(msg.get("request_id"), str):
                    req_id = msg["request_id"]
            except ValueError:
                pass
        print(
            json.dumps(
                {
                    "type": "control_response",
                    "response": {
                        "subtype": "success",
                        "request_id": req_id,
                        "response": {"models": [{"value": "claude-sonnet-4-6"}]},
                    },
                }
            ),
            flush=True,
        )
        return 0

    switched = _run_failure_switch("claude", mode, hang_seconds, exit_code)
    if switched is not None:
        return switched

    prompt_text = ""
    if not sys.stdin.isatty():
        prompt_text = sys.stdin.read()
    # A prompt-level instruction lets a multi-job test mix passing and failing
    # reviews in one batch (the control file is global to the rig).  The
    # control file still wins for every prompt that does not carry it.
    if "FAKE-REVIEW: BLOCK" in prompt_text:
        review_verdict = "BLOCK"
    model = _get_arg_value(args, "--model") or "claude-sonnet-4-6"
    body = _review_text(review_verdict)
    print(json.dumps({"type": "system", "subtype": "init", "model": model}))
    print(
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": body}]}})
    )
    print(
        json.dumps(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "permission_denials": [],
                "result": body,
            }
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    cli_harness = _pop_flag_value(args, "--fake-harness")
    cli_mode = _pop_flag_value(args, "--fake-mode")
    cli_verdict = _pop_flag_value(args, "--fake-review-verdict")

    harness = (
        cli_harness
        or os.environ.get("ORCH_FAKE_HARNESS")
        or Path(sys.argv[0]).name
    ).strip().lower()
    if "--version" in args:
        # SaberOps probes `<cli> --version` before dispatch.  A version read
        # must never edit or commit work in the caller's working directory.
        print(f"{harness} 0.0.0-fake")
        return 0
    mode, hang_seconds, exit_code, target_file, content = _resolve_worker_mode(
        cli_mode, harness
    )
    review_verdict = _resolve_reviewer_verdict(cli_verdict)

    if harness == "codex":
        return _handle_codex(
            args,
            mode=mode,
            hang_seconds=hang_seconds,
            exit_code=exit_code,
            target_file=target_file,
            content=content,
        )
    if harness == "opencode":
        return _handle_opencode(
            args,
            mode=mode,
            hang_seconds=hang_seconds,
            exit_code=exit_code,
            target_file=target_file,
            content=content,
        )
    if harness in {"claude", "anthropic"}:
        return _handle_claude(
            args,
            mode=mode,
            hang_seconds=hang_seconds,
            exit_code=exit_code,
            review_verdict=review_verdict,
        )
    sys.stderr.write(f"Unknown fake harness: {harness!r}\n")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
