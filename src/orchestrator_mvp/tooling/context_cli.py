"""Read-only bounded ``orch-context`` evidence surface."""

from __future__ import annotations

import json
from typing import Any

from orchestrator_mvp.db import Database

_REFS = ("continuation", "checkpoint", "handoff", "gate", "review", "failure")

# These are protocol limits, rather than limits derived from stored row size.
# The capsule limit matches the C05 resume-capsule ceiling.
CONTEXT_FIELD_MAX_BYTES = 4 * 1024
CONTEXT_CAPSULE_MAX_BYTES = 4 * 1024
CONTEXT_RESPONSE_MAX_BYTES = 16 * 1024
CHECKPOINT_CHANGED_PATHS_MAX = 32
CONTEXT_ITEMS_MAX = 32
_TRUNCATION_MARKER = "\n...[truncated]...\n"


def _byte_count(value: str | None) -> int:
    return len((value or "").encode("utf-8"))


def _bounded_text(value: str | None, limit: int = CONTEXT_FIELD_MAX_BYTES) -> tuple[str, bool]:
    """Return a valid UTF-8 prefix/suffix excerpt and its truncation state."""
    text = value or ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text, False
    marker = _TRUNCATION_MARKER.encode("utf-8")
    available = max(0, limit - len(marker))
    head_bytes = available // 2
    tail_bytes = available - head_bytes
    head = encoded[:head_bytes].decode("utf-8", errors="ignore")
    tail = encoded[-tail_bytes:].decode("utf-8", errors="ignore") if tail_bytes else ""
    return head + _TRUNCATION_MARKER + tail, True


def _text_evidence(value: str | None, *, limit: int = CONTEXT_FIELD_MAX_BYTES) -> dict[str, object]:
    excerpt, truncated = _bounded_text(value, limit)
    return {
        "excerpt": excerpt,
        "original_byte_count": _byte_count(value),
        "truncated": truncated,
    }


def _checkpoint_projection(item: Any) -> dict[str, object]:
    paths = tuple(str(path) for path in item.changed_paths)
    return {
        "id": item.id,
        "run_id": item.run_id,
        "source_attempt_id": item.source_attempt_id,
        "source_base_sha": item.source_base_sha,
        "checkpoint_sha": item.checkpoint_sha,
        "reason": item.reason,
        "changed_paths": list(paths[:CHECKPOINT_CHANGED_PATHS_MAX]),
        "changed_paths_truncated": len(paths) > CHECKPOINT_CHANGED_PATHS_MAX,
        "created_at": item.created_at,
        "handoff_id": item.handoff_id,
    }


def _handoff_projection(item: Any) -> dict[str, object]:
    content, truncated = _bounded_text(item.content, CONTEXT_CAPSULE_MAX_BYTES)
    return {
        "id": item.id,
        "kind": item.kind,
        "version": item.version,
        "created_at": item.created_at,
        "content": content,
        "original_byte_count": _byte_count(item.content),
        "truncated": truncated,
    }


def _gate_projection(item: Any) -> dict[str, object]:
    stdout = _text_evidence(item.stdout)
    stderr = _text_evidence(item.stderr)
    return {
        "id": item.id,
        "attempt_id": item.attempt_id,
        "gate_command": _bounded_text(item.gate_command)[0],
        "exit_code": item.exit_code,
        "passed": item.passed,
        "created_at": item.created_at,
        "stdout": stdout["excerpt"],
        "stderr": stderr["excerpt"],
        "original_stdout_byte_count": stdout["original_byte_count"],
        "original_stderr_byte_count": stderr["original_byte_count"],
        "stdout_truncated": stdout["truncated"],
        "stderr_truncated": stderr["truncated"],
    }


def _review_projection(item: Any) -> dict[str, object]:
    details = _text_evidence(item.details)
    return {
        "id": item.id,
        "provider": item.provider,
        "model": item.model,
        "verdict": item.verdict.value,
        "summary": _bounded_text(item.summary)[0],
        "details": details["excerpt"],
        "original_detail_byte_count": details["original_byte_count"],
        "details_truncated": details["truncated"],
        "created_at": item.created_at,
    }


def _failure_projection(item: Any) -> dict[str, object]:
    worker_error = _text_evidence(item.worker_error)
    stderr = _text_evidence(item.worker_stderr)
    return {
        "attempt_id": item.id,
        "provider": item.provider,
        "model": item.model,
        "status": item.status.value,
        "worker_error": worker_error["excerpt"],
        "stderr": stderr["excerpt"],
        "original_worker_error_byte_count": worker_error["original_byte_count"],
        "original_stderr_byte_count": stderr["original_byte_count"],
        "worker_error_truncated": worker_error["truncated"],
        "stderr_truncated": stderr["truncated"],
        "candidate_sha": item.commit_sha,
        "base_sha": item.base_sha,
        "created_at": item.created_at,
    }


def list_context_refs(db: Database, run_id: str) -> tuple[str, ...]:
    """Return only bounded, named evidence refs; never filesystem paths."""
    if db.get_run(run_id) is None:
        raise ValueError(f"unknown run: {run_id}")
    return _REFS


def get_context_ref(db: Database, run_id: str, ref: str) -> dict[str, Any]:
    if ref not in _REFS:
        raise ValueError(f"unsupported context ref: {ref}")
    run = db.get_run(run_id)
    if run is None:
        raise ValueError(f"unknown run: {run_id}")
    if ref == "checkpoint":
        checkpoint_items = db.get_worker_checkpoints_for_run(run_id)
        value: object = [
            _checkpoint_projection(item) for item in checkpoint_items[:CONTEXT_ITEMS_MAX]
        ]
        truncated = len(checkpoint_items) > CONTEXT_ITEMS_MAX
    elif ref == "handoff":
        latest = db.get_latest_handoff(run_id, "handoff_resume_capsule")
        value = {"latest": _handoff_projection(latest) if latest else None}
        truncated = False
    elif ref == "gate":
        check_items = db.get_checks_for_run(run_id)
        value = [_gate_projection(item) for item in check_items[:CONTEXT_ITEMS_MAX]]
        truncated = len(check_items) > CONTEXT_ITEMS_MAX
    elif ref == "review":
        review_items = db.get_reviews_for_run(run_id)
        value = [_review_projection(item) for item in review_items[:CONTEXT_ITEMS_MAX]]
        truncated = len(review_items) > CONTEXT_ITEMS_MAX
    elif ref == "failure":
        failure_items = [item for item in db.get_attempts_for_run(run_id) if item.worker_error]
        value = [_failure_projection(item) for item in failure_items[:CONTEXT_ITEMS_MAX]]
        truncated = len(failure_items) > CONTEXT_ITEMS_MAX
    else:
        latest = db.get_latest_handoff(run_id, "handoff_resume_capsule")
        value = {"latest": _handoff_projection(latest) if latest else None}
        truncated = False
    return {"run_id": run_id, "ref": ref, "value": value, "truncated": truncated}


def _dump(payload: object) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _fit_response(payload: dict[str, Any]) -> str:
    """Keep the complete response bounded while retaining valid JSON."""
    rendered = _dump(payload)
    if len(rendered.encode("utf-8")) <= CONTEXT_RESPONSE_MAX_BYTES:
        return rendered
    value = payload.get("value")
    if isinstance(value, list):
        payload["truncated"] = True
        while value and len(_dump(payload).encode("utf-8")) > CONTEXT_RESPONSE_MAX_BYTES:
            value.pop()
        return _dump(payload)
    if isinstance(value, dict):
        payload["truncated"] = True
        for key, item in tuple(value.items()):
            if isinstance(item, str):
                value[key] = _bounded_text(item, 1024)[0]
        rendered = _dump(payload)
        if len(rendered.encode("utf-8")) <= CONTEXT_RESPONSE_MAX_BYTES:
            return rendered
    # The selected projections above are already small, but keep a final
    # deterministic valid-JSON fallback for pathological durable rows.
    return _dump(
        {
            "run_id": payload.get("run_id"),
            "ref": payload.get("ref"),
            "value": [],
            "truncated": True,
        }
    )


def context_json(db: Database, run_id: str, ref: str | None = None) -> str:
    payload: object = (
        list_context_refs(db, run_id) if ref is None else get_context_ref(db, run_id, ref)
    )
    if ref is None:
        return _dump(payload)
    return _fit_response(payload)  # type: ignore[arg-type]
