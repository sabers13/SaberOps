"""Bounded observational transcripts for individual agent invocations.

Live calls can be held by their execution owner for synchronized manual turns.
Provider sessions persist; frozen permissions and the original deadline still apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any

from saberops.models import AttemptStatus, WorkerInterruptionReason, WorkerRequest, WorkerResult
from saberops.ownership import Liveness, ProcessIdentity, probe_liveness
from saberops.session_control import SessionControls
from saberops.session_journal import SessionJournal
from saberops.session_runtime import AgentSessionRuntime
from saberops.verification_snapshot import source_tree

LOG_MAX_BYTES = 4 * 1024 * 1024
VIEW_MAX_BYTES = 16000
SESSIONS_MAX = 24


def owner_directory(root: Path, owner: str) -> Path:
    return root / hashlib.sha256(owner.encode()).hexdigest()


def _write_json(path: Path, data: dict[str, Any]) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    temporary.chmod(0o600)
    temporary.replace(path)


def observe_invocation(
    invoke: Callable[[WorkerRequest], WorkerResult],
    request: WorkerRequest,
    *,
    root: Path | None,
    owner: str,
    role: str,
    provider: str,
    association: str,
    allow_shared_runtime: bool = True,
) -> WorkerResult:
    """Capture output and serialize manual turns before the caller proceeds."""
    if root is None:
        return invoke(request)
    controls = SessionControls(root)
    journal = SessionJournal(root)
    native_provider = "claude-code" if provider == "anthropic" else provider
    if (request.env or {}).get("ORCH_COPILOT_PROFILE_ID"):
        native_provider = "copilot"
    supported = native_provider in (
        "codex", "opencode", "claude-code", "antigravity", "cline", "copilot"
    )
    binding = (request.env or {}).get("ORCH_EXECUTION_BINDING_ID")
    resume_id = request.native_resume_id
    if supported and role == "orchestrator" and not resume_id:
        previous = read_sessions(root, owner)
        from saberops.native_access import BUSY
        if any((row.get("native_client") or {}).get("state") in BUSY for row in previous):
            return WorkerResult(
                provider, request.model, AttemptStatus.FAILED,
                "Close the orchestrator's native CLI before sending an app turn.", "", ""
            )
        resume_id = next(
            (
                str(row["native_id"])
                for row in previous
                if row.get("native_id")
                and row.get("state") == "SUCCESS"
                and row.get("control_mode") == "DONE"
                and row.get("provider") == provider
                and row.get("model") == request.model
                and row.get("binding") == binding
                and row.get("read_only") == request.read_only
            ),
            None,
        )
    try:
        folder = owner_directory(root, owner)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        if native_provider == "opencode" and role == "orchestrator" and request.read_only:
            # Native OpenCode sessions bind to a directory. Keep the conversation
            # workspace stable across turns while preserving read-only tools.
            scratch = folder / "orchestrator-workspace"
            scratch.mkdir(exist_ok=True, mode=0o700)
            request = replace(request, worktree_path=scratch)
        session_id = uuid.uuid4().hex
        metadata_path = folder / f"{session_id}.json"
        log_path = folder / f"{session_id}.log"
        log_path.touch(mode=0o600)
        identity = ProcessIdentity.for_pid(os.getpid())
        metadata: dict[str, Any] = {
            "id": session_id,
            "role": role,
            "association": association,
            "provider": provider,
            "native_provider": native_provider,
            "model": request.model,
            "state": "ACTIVE",
            "created": time.time(),
            "owner_identity": json.dumps(identity.to_dict()) if identity else None,
            "actual_provider": None,
            "actual_model": None,
            "truncated": False,
            "native_id": resume_id,
            "binding": binding,
            "read_only": request.read_only,
            "native_handoff_allowed": allow_shared_runtime,
            "worktree": str(request.worktree_path),
            "selectors": {key: val for key, val in (request.env or {}).items()
                          if key in ("ORCH_EXECUTION_BINDING_ID", "ORCH_EXECUTION_PROFILE_ID",
                                     "ORCH_EXECUTION_PROFILE_ACCOUNT",
                                     "ORCH_EXECUTION_PROFILE_BACKEND",
                                     "ORCH_COPILOT_PROFILE_ID")},
            "git_common_dir": _git_common_dir(request.worktree_path),
            "control_available": supported,
            "source": "Provider output · durable session journal",
            "native_access": "saved-session" if supported else "monitor",
            "native_attach_ready": False,
            "native_cli_reason": (
                "Waiting for a verified native session."
                if supported
                else "This provider does not support synchronized manual control."
            ),
        }
        _write_json(metadata_path, metadata)
        journal.register(session_id, owner, role, association, metadata["owner_identity"])
        journal.append(session_id, "assignment.started", {"provider": provider,
            "model": request.model, "role": role, "association": association,
            "read_only": request.read_only, "source_tree": source_tree(request.worktree_path)})
        if supported:
            controls.register(session_id, owner, metadata["owner_identity"], role)
            if resume_id:
                controls.native_id(session_id, resume_id)
    except (OSError, sqlite3.Error):
        return invoke(request)
    lock = threading.RLock()
    buffers: dict[str, str] = {"stdout": "", "stderr": ""}
    evidence_error: str | None = None
    runtime: AgentSessionRuntime | None = None

    def publish(values: dict[str, Any]) -> None:
        with lock:
            metadata.update(values)
            if values.get("native_id"):
                controls.native_id(session_id, str(values["native_id"]))
            _write_json(metadata_path, metadata)

    runtime = AgentSessionRuntime(folder, session_id, controls, journal, publish)
    size = 0
    streamed = False
    started = time.monotonic()
    anchor = (
        (
            "read-only-scratch"
            if role == "orchestrator" and request.read_only
            else _worktree_head(request.worktree_path)
        )
        if supported
        else None
    )

    def append(text: str) -> None:
        nonlocal size
        try:
            with lock:
                data = text.encode("utf-8", errors="replace")
                kept = data[: max(0, LOG_MAX_BYTES - size)]
                if len(kept) < len(data):
                    metadata["truncated"] = True
                if kept:
                    with log_path.open("ab") as output:
                        output.write(kept)
                    size += len(kept)
                if supported:
                    native = _native_id(text, native_provider)
                    if native and metadata.get("native_id") != native:
                        metadata["native_id"] = native
                        metadata["native_cli_reason"] = (
                            "Shared provider session; use synchronized CLI."
                        )
                        controls.native_id(session_id, native)
                        _write_json(metadata_path, metadata)
        except OSError:
            pass

    def callback(previous: Callable[[str], None] | None, channel: str) -> Callable[[str], None]:
        def consume(text: str) -> None:
            nonlocal streamed, evidence_error
            streamed = True
            try:
                journal.append(session_id, "output." + channel, {"text": text})
                try:
                    whole = json.loads(text)
                except ValueError:
                    whole = None
                if isinstance(whole, dict):
                    buffers[channel] = ""
                    lines = [text]
                else:
                    buffers[channel] += text
                    lines = buffers[channel].split("\n")
                    buffers[channel] = lines.pop()[-65536:]
                for line in lines:
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if isinstance(event, dict):
                        journal.append(session_id, "provider.structured", event)
                        native = _native_id(line, native_provider)
                        if native:
                            publish({"native_id": native})
            except (OSError, sqlite3.Error) as exc:
                evidence_error = str(exc)
            append(("\n[stderr] " if channel == "stderr" else "") + text)
            if previous is not None:
                previous(text)

        return consume

    def interrupted() -> bool:
        return bool(evidence_error) or controls.should_interrupt(session_id) or bool(
            request.control_interrupt and request.control_interrupt())

    def process_started(identity: dict[str, Any]) -> None:
        nonlocal evidence_error
        try:
            journal.process(session_id, identity)
        except (OSError, sqlite3.Error) as exc:
            evidence_error = str(exc)
        if request.on_process_start:
            request.on_process_start(identity)

    def turn(req: WorkerRequest, kind: str) -> WorkerResult:
        journal.append(session_id, "turn.started", {"kind": kind})
        res = invoke(req)
        journal.append(session_id, "turn.completed", {"kind": kind, "status": res.status.value,
            "provider_terminal_status": res.provider_terminal_status,
            "exit_code": res.provider_exit_code, "response": res.stdout, "error": res.error,
            "source_tree": source_tree(request.worktree_path)})
        return res

    observed = replace(
        request,
        on_stdout=callback(request.on_stdout, "stdout"),
        on_stderr=callback(request.on_stderr, "stderr"),
        persist_native_session=supported or request.persist_native_session,
        native_resume_id=resume_id,
        on_process_start=process_started,
        session_runtime=runtime if allow_shared_runtime else None,
        control_interrupt=interrupted if supported else request.control_interrupt,
    )

    try:
        current = observed
        previous_usage = []
        finished = False
        while not finished:
            from saberops.native_access import BUSY, client_state, session_key
            if current.native_resume_id and (
                client_state(controls, session_key(metadata)) or {}
            ).get("state") in BUSY:
                result = WorkerResult(provider, request.model, AttemptStatus.FAILED,
                                      "The native CLI owns this session. Close it before resuming.",
                                      "", "", manual_control_stopped=True)
                controls.finish(session_id, "FAILED", result.summary)
                break
            result = turn(current, "automatic")
            previous_usage.extend(result.usage)
            if not supported or controls.complete_if_active(session_id):
                break
            if controls.mode(session_id) != "REQUESTED" or (
                result.status != AttemptStatus.SUCCESS
                and result.interruption_reason != WorkerInterruptionReason.ABORTED
            ):
                controls.finish(
                    session_id, "FAILED", "The provider could not be safely interrupted."
                )
                break
            paused_anchor = (
                "read-only-scratch"
                if anchor == "read-only-scratch"
                else _worktree_head(request.worktree_path)
            )
            if (
                not anchor
                or not paused_anchor
                or anchor.splitlines()[-1] != paused_anchor.splitlines()[-1]
            ):
                result = WorkerResult(
                    provider=provider,
                    model=request.model,
                    status=AttemptStatus.FAILED,
                    summary="Worktree cannot be verified for takeover",
                    stdout="",
                    stderr="",
                    has_changes=False,
                )
                controls.finish(session_id, "FAILED", result.summary)
                break
            anchor = paused_anchor
            controls.finish(session_id, "MANUAL")
            journal.append(session_id, "control.manual",
                           {"source_tree": source_tree(request.worktree_path)})
            metadata["state"] = "MANUAL"
            _write_json(metadata_path, metadata)
            while True:
                remaining = request.timeout_seconds - (time.monotonic() - started)
                if remaining <= 0:
                    result = WorkerResult(
                        provider=provider,
                        model=request.model,
                        status=AttemptStatus.TIMEOUT,
                        summary="Manual control reached the deadline",
                        stdout="",
                        stderr="",
                        has_changes=False,
                    )
                    controls.finish(session_id, "EXPIRED", "Original invocation deadline expired.")
                    finished = True
                    break
                if controls.mode(session_id) == "RESUMING":
                    if (
                        anchor != "read-only-scratch"
                        and _worktree_head(request.worktree_path) != anchor
                    ):
                        result = WorkerResult(
                            provider=provider,
                            model=request.model,
                            status=AttemptStatus.FAILED,
                            summary="Worktree reconciliation failed",
                            stdout="",
                            stderr="",
                            has_changes=False,
                            error="Worktree revision or branch changed during manual control.",
                        )
                        controls.finish(session_id, "FAILED", result.error or "")
                        finished = True
                        break
                    # Manual reviewer replies are never substituted for a formal verdict.
                    journal.append(session_id, "control.reconciled",
                                   {"source_tree": source_tree(request.worktree_path)})
                    current = replace(
                        observed,
                        task=request.task + "\n\nResume the original automated assignment. "
                        "Inspect the current worktree and validate any manual changes.",
                        timeout_seconds=remaining,
                        native_resume_id=metadata.get("native_id") if role != "reviewer" else None,
                    )
                    controls.finish(session_id, "ACTIVE")
                    metadata["state"] = "ACTIVE"
                    _write_json(metadata_path, metadata)
                    break
                message = controls.next_message(session_id)
                if message:
                    controls.sync_chat(message)
                    manual = turn(
                        replace(
                            observed,
                            task=message["text"],
                            timeout_seconds=remaining,
                            native_resume_id=metadata.get("native_id"),
                            native_message_id=message["id"].partition(":")[2],
                        ), "manual"
                    )
                    previous_usage.extend(manual.usage)
                    response = manual.stdout or manual.error or manual.summary
                    if manual.status != AttemptStatus.SUCCESS:
                        prefix = (
                            "Reply interrupted."
                            if manual.interruption_reason == WorkerInterruptionReason.ABORTED
                            else "Manual reply failed."
                        )
                        response = prefix + "\n\n" + response
                    controls.reply(message, response, failed=manual.status != AttemptStatus.SUCCESS)
                    if (
                        manual.status != AttemptStatus.SUCCESS
                        and manual.interruption_reason != WorkerInterruptionReason.ABORTED
                    ):
                        result = manual
                        controls.finish(
                            session_id, "FAILED", "Manual provider turn failed; automation stopped."
                        )
                        finished = True
                        break
                else:
                    time.sleep(0.1)
        result.usage = previous_usage
        if supported and controls.mode(session_id) in ("FAILED", "EXPIRED"):
            result.manual_control_stopped = True
    except BaseException:
        metadata["state"] = "INTERRUPTED"
        if supported:
            controls.finish(session_id, "FAILED", "The execution owner was interrupted.")
        raise
    else:
        if not streamed:
            append(result.stdout)
            if result.stderr:
                append("\n[stderr]\n" + result.stderr)
        # The parsed final response is retained independently from native JSONL.
        metadata.update(
            state=result.status.value,
            actual_provider=result.actual_provider,
            actual_model=result.actual_model,
            summary=result.summary[:4000],
            response=result.stdout[-VIEW_MAX_BYTES:],
        )
        if supported and not metadata.get("native_id"):
            metadata["native_cli_reason"] = (
                "The CLI did not report a resumable session; this call is read-only."
            )
        return result
    finally:
        runtime_safe = runtime.close() if runtime else True
        from saberops.native_access import wait_for_client_shutdown
        if not wait_for_client_shutdown(controls, session_id):
            runtime_safe = False
        quiescent = (
            False if not runtime_safe
            else runtime_safe if runtime and runtime.thread is not None
            else result.execution_quiescent if metadata["state"] != "INTERRUPTED"
            else None
        )
        if metadata["state"] != "INTERRUPTED":
            if quiescent is False or evidence_error:
                result.status = AttemptStatus.FAILED
                result.error = (evidence_error
                                or "Provider server termination could not be confirmed")
                result.summary = result.error
                result.manual_control_stopped = True
                controls.finish(session_id, "FAILED", result.error)
                metadata["state"] = "FAILED"
            try:
                journal.complete(session_id, {"status": result.status.value,
                    "source": "execution-owner", "exit_code": result.provider_exit_code,
                    "provider_terminal_status": result.provider_terminal_status
                        or (runtime.terminal_status if runtime else None),
                    "native_id": metadata.get("native_id"),
                    "source_tree": source_tree(request.worktree_path),
                    "quiescent": quiescent, "error": result.error,
                    "duration_seconds": result.duration_seconds,
                    "actual_provider": result.actual_provider, "actual_model": result.actual_model})
            except (OSError, sqlite3.Error) as exc:
                result.status = AttemptStatus.FAILED
                result.error = "Completion evidence could not be saved: " + str(exc)
                result.manual_control_stopped = True
                metadata["state"] = "FAILED"
        else:
            journal.complete(session_id, {"status": "INTERRUPTED", "source": "execution-owner",
                                          "quiescent": quiescent})
        metadata["completed"] = time.time()
        try:
            _write_json(metadata_path, metadata)
        except OSError:
            pass


def read_sessions(root: Path, owner: str) -> list[dict[str, Any]]:
    """Read-only, bounded output. Restarted sessions never masquerade as active."""
    folder = owner_directory(root, owner)
    if not folder.is_dir():
        return []
    paths = sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    sessions: list[dict[str, Any]] = []
    for path in paths[:SESSIONS_MAX]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or not re.fullmatch(
                r"[0-9a-f]{32}", value.get("id", "")
            ):
                continue
            log_path = folder / f"{value['id']}.log"
            with log_path.open("rb") as log:
                length = log.seek(0, 2)
                log.seek(max(0, length - VIEW_MAX_BYTES))
                raw = log.read(VIEW_MAX_BYTES).decode("utf-8", errors="replace")
            value["output"] = raw
            value["view_truncated"] = length > VIEW_MAX_BYTES
            if value.get("state") == "ACTIVE":
                identity = ProcessIdentity.from_json(value.get("owner_identity"))
                if probe_liveness(identity) == Liveness.DEAD:
                    value["state"] = "INTERRUPTED"
                elif probe_liveness(identity) == Liveness.UNKNOWN:
                    value["state"] = "UNKNOWN"
            controls = SessionControls(root).get(value["id"])
            if controls:
                value["control"] = SessionControls.public(controls)
                value["control_mode"] = controls["mode"]
                value["can_takeover"] = controls["mode"] == "ACTIVE" and bool(controls["native_id"])
                if controls["mode"] in (
                    "MANUAL",
                    "REQUESTED",
                    "RESUMING",
                    "INTERRUPTED",
                    "UNKNOWN",
                ):
                    value["state"] = controls["mode"]
            journal = SessionJournal(root)
            latest = journal.tail(value["id"], max_bytes=VIEW_MAX_BYTES)
            if latest:
                value["output"] = latest
            value["completion"] = journal.view(value["id"], limit=1)["outcome"]
            if value["completion"]:
                value["state"] = value["completion"]["status"]
                value["native_attach_ready"] = False
                value["can_takeover"] = False
                value["native_cli_reason"] = (
                    "This invocation has a recorded outcome. Read its final response and "
                    "check the run's gate and review results separately."
                )
                if not value.get("native_id"):
                    value["native_cli_reason"] += " The CLI did not report a resumable session."
            value.pop("owner_identity", None)
            from saberops.native_access import capability, client_state, session_key
            value.update(capability(value))
            value["native_client"] = client_state(SessionControls(root), session_key(value))
            client_log = folder / (value["id"] + ".native.log")
            if client_log.is_file():
                with client_log.open("rb") as log:
                    length = log.seek(0, 2)
                    log.seek(max(0, length - VIEW_MAX_BYTES))
                    value["native_client_output"] = log.read(VIEW_MAX_BYTES).decode(
                        "utf-8", errors="replace"
                    )
            sessions.append(value)
        except (OSError, ValueError, TypeError, sqlite3.Error):
            continue
    return sessions


def legacy_session(
    session_id: str,
    role: str,
    association: str,
    provider: str,
    model: str,
    state: str,
    output: str,
    created: str | float | None = None,
) -> dict[str, Any]:
    try:
        timestamp = (
            datetime.fromisoformat(created).timestamp()
            if isinstance(created, str)
            else float(created or 0)
        )
    except ValueError:
        timestamp = 0.0
    return {
        "id": session_id,
        "role": role,
        "association": association,
        "provider": provider,
        "model": model,
        "state": state,
        "output": output[-VIEW_MAX_BYTES:],
        "response": "",
        "created": timestamp,
        "view_truncated": len(output) > VIEW_MAX_BYTES,
        "source": "Recorded result · live transcript was not captured",
        "native_cli_reason": "No verified native session attachment was recorded for this call.",
    }


def _native_id(text: str, provider: str) -> str | None:
    for line in text.splitlines():
        try:
            event = json.loads(line)
            if not isinstance(event, dict):
                continue
            if provider == "codex" and event.get("type") != "thread.started":
                continue
            if provider == "claude-code" and event.get("type") not in ("system", "result"):
                continue
            value = (
                event.get("thread_id")
                if provider == "codex"
                else event.get("session_id")
                if provider == "claude-code"
                else event.get("conversation_id")
                or (event.get("result") or {}).get("conversation_id")
                if provider == "antigravity"
                else event.get("session_id") or event.get("sessionId") or event.get("taskId")
                or (event.get("data") or {}).get("sessionId")
                or event.get("sessionID")
                if provider in ("cline", "copilot")
                else event.get("sessionID") or (event.get("part") or {}).get("sessionID")
            )
            if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{7,127}", value):
                return value
        except (ValueError, AttributeError):
            pass
    return None


def _git_common_dir(path: Path) -> str | None:
    try:
        result = subprocess.run(["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
                                cwd=path, capture_output=True, text=True, timeout=5, check=False)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _worktree_head(path: Path) -> str | None:
    # A Git metadata read, never a semantic worker launch.
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD", "--symbolic-full-name", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip()
    return (
        value
        if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}\n(?:refs/[^\n]+|HEAD)", value)
        else None
    )
