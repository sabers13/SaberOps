"""Provider-native token parsing, normalized persistence, and aggregation."""

from __future__ import annotations

import json
import uuid
from collections import Counter
from dataclasses import dataclass

from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    DispatchStage,
    ModelCallUsage,
    ProviderUsage,
    Tier,
    WorkerResult,
)

_RAW_USAGE_LIMIT = 4000


def _integer(value: object) -> int | None:
    return value if isinstance(value, int) and value >= 0 else None


def _bounded_json(payload: object) -> str:
    value = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return value[:_RAW_USAGE_LIMIT]


def _json_lines(stdout: str) -> list[dict[str, object]]:
    events: list[dict[str, object]] = []
    for line in stdout.splitlines():
        try:
            payload = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(payload, dict):
            events.append(payload)
    return events


def parse_opencode_jsonl(stdout: str) -> tuple[str, list[ProviderUsage]]:
    """Parse exact usage from installed OpenCode ``step_finish`` events."""
    events = _json_lines(stdout)
    if not events:
        return stdout, []
    text_parts: list[str] = []
    usages: list[ProviderUsage] = []
    starts = 0
    finishes = 0
    for event in events:
        event_type = event.get("type")
        part = event.get("part")
        part_dict = part if isinstance(part, dict) else {}
        part_type = part_dict.get("type")
        if event_type == "step_start" or part_type == "step-start":
            starts += 1
        if event_type == "text" and isinstance(part_dict.get("text"), str):
            text_parts.append(str(part_dict["text"]))
        if event_type != "step_finish" and part_type != "step-finish":
            continue
        finishes += 1
        tokens = part_dict.get("tokens")
        if not isinstance(tokens, dict):
            usages.append(
                ProviderUsage(
                    usage_source="opencode_step_finish_without_usage",
                    raw_payload=_bounded_json(event),
                )
            )
            continue
        cache = tokens.get("cache")
        cache_dict = cache if isinstance(cache, dict) else {}
        cache_read = _integer(cache_dict.get("read"))
        cache_write = _integer(cache_dict.get("write"))
        usages.append(
            ProviderUsage(
                input_tokens=_integer(tokens.get("input")),
                output_tokens=_integer(tokens.get("output")),
                cached_tokens=cache_read,
                total_tokens=_integer(tokens.get("total")),
                cache_read_tokens=cache_read,
                cache_write_tokens=cache_write,
                reasoning_tokens=_integer(tokens.get("reasoning")),
                usage_source="opencode_step_finish_json",
                raw_payload=_bounded_json({"type": event_type, "part": {"tokens": tokens}}),
            )
        )
    for _ in range(max(0, starts - finishes)):
        usages.append(ProviderUsage(usage_source="opencode_step_finish_missing"))
    return "\n".join(text_parts) if text_parts else stdout, usages


def parse_codex_jsonl(stdout: str) -> tuple[str, list[ProviderUsage]]:
    """Parse exact usage from installed Codex ``turn.completed`` events."""
    events = _json_lines(stdout)
    if not events:
        return stdout, []
    text_parts: list[str] = []
    usages: list[ProviderUsage] = []
    starts = 0
    finishes = 0
    for event in events:
        event_type = event.get("type")
        if event_type == "turn.started":
            starts += 1
        item = event.get("item")
        if (
            event_type == "item.completed"
            and isinstance(item, dict)
            and item.get("type") == "agent_message"
            and isinstance(item.get("text"), str)
        ):
            text_parts.append(str(item["text"]))
        if event_type != "turn.completed":
            continue
        finishes += 1
        usage = event.get("usage")
        if not isinstance(usage, dict):
            usages.append(
                ProviderUsage(
                    usage_source="codex_turn_completed_without_usage",
                    raw_payload=_bounded_json(event),
                )
            )
            continue
        input_tokens = _integer(usage.get("input_tokens"))
        output_tokens = _integer(usage.get("output_tokens"))
        # Codex's provider event reports cached input as a subset of input.
        # input + output is therefore an exact arithmetic normalization, not
        # an estimate based on bytes or a tokenizer.
        total_tokens = (
            input_tokens + output_tokens
            if input_tokens is not None and output_tokens is not None
            else None
        )
        cache_read = _integer(usage.get("cached_input_tokens"))
        usages.append(
            ProviderUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_tokens=cache_read,
                total_tokens=total_tokens,
                cache_read_tokens=cache_read,
                cache_write_tokens=_integer(usage.get("cache_write_input_tokens")),
                reasoning_tokens=_integer(usage.get("reasoning_output_tokens")),
                usage_source="codex_turn_completed_json",
                raw_payload=_bounded_json({"type": event_type, "usage": usage}),
            )
        )
    for _ in range(max(0, starts - finishes)):
        usages.append(ProviderUsage(usage_source="codex_turn_completed_missing"))
    return "\n".join(text_parts) if text_parts else stdout, usages

@dataclass(frozen=True)
class ClineStructuredRun:
    """Defensive parse of one Cline ``--json`` NDJSON event stream.

    ``text`` is the final ``run_result.text`` when present, else the raw
    stream.  ``usage`` carries one :class:`ProviderUsage` per ``run_result``
    line (the run-level snapshot Cline reports at termination -- it is never
    summed with other events).  ``finish_reason``/``actual_model``/
    ``actual_provider`` preserve the provider-reported terminal identity.
    ``saw_run_result`` is the only success signal: a stream without a final
    ``run_result`` must never be interpreted as a completed run.
    """

    text: str
    usage: list[ProviderUsage]
    saw_run_result: bool = False
    finish_reason: str | None = None
    actual_model: str | None = None
    actual_provider: str | None = None
    error_messages: tuple[str, ...] = ()
    duration_ms: int | None = None
    iterations: int | None = None


def _cline_usage_from_dict(usage: dict[str, object]) -> ProviderUsage:
    """Map one Cline usage snapshot with unknown-is-None (never zero) semantics."""
    cache_read = _integer(usage.get("cacheReadTokens"))
    return ProviderUsage(
        input_tokens=_integer(usage.get("inputTokens")),
        output_tokens=_integer(usage.get("outputTokens")),
        cached_tokens=cache_read,
        total_tokens=None,
        cache_read_tokens=cache_read,
        cache_write_tokens=_integer(usage.get("cacheWriteTokens")),
        reasoning_tokens=None,
        usage_source="cline_run_result_json",
        raw_payload=_bounded_json({"type": "run_result", "usage": usage}),
    )


def parse_cline_jsonl(stdout: str) -> ClineStructuredRun:
    """Parse the installed Cline CLI (3.0.60) ``--json`` NDJSON stream.

    The stream is a sequence of JSON objects with top-level ``type`` values
    including ``agent_event`` (nested ``event`` objects: iteration starts,
    tool activity, errors), ``hook_event``, ``run_result``, and ``error``.
    Parsing is defensive: malformed lines, unknown top-level types, and
    unknown future event shapes are skipped without failing; usage is taken
    only from the terminal ``run_result`` snapshot (never aggregated with
    anything else); a missing or truncated stream yields
    ``saw_run_result=False`` so callers can fail closed.
    """
    events = _json_lines(stdout)
    if not events:
        return ClineStructuredRun(text=stdout, usage=[])
    error_messages: list[str] = []
    usages: list[ProviderUsage] = []
    saw_run_result = False
    finish_reason: str | None = None
    actual_model: str | None = None
    actual_provider: str | None = None
    duration_ms: int | None = None
    iterations: int | None = None
    final_text: str | None = None
    for event in events:
        event_type = event.get("type")
        if event_type == "error":
            message = event.get("message")
            if isinstance(message, str) and message:
                error_messages.append(message)
            continue
        if event_type == "agent_event":
            nested = event.get("event")
            if isinstance(nested, dict) and nested.get("type") == "error":
                error = nested.get("error")
                message = error.get("message") if isinstance(error, dict) else None
                if isinstance(message, str) and message:
                    error_messages.append(message)
            continue
        if event_type != "run_result":
            # hook_event and unknown future top-level types are tolerated.
            continue
        saw_run_result = True
        reason = event.get("finishReason")
        if isinstance(reason, str) and reason:
            finish_reason = reason
        model = event.get("model")
        if isinstance(model, dict):
            model_id = model.get("id")
            model_provider = model.get("provider")
            if isinstance(model_id, str) and model_id:
                actual_model = model_id
            if isinstance(model_provider, str) and model_provider:
                actual_provider = model_provider
        raw_duration = event.get("durationMs")
        if isinstance(raw_duration, int) and raw_duration >= 0:
            duration_ms = raw_duration
        raw_iterations = event.get("iterations")
        if isinstance(raw_iterations, int) and raw_iterations >= 0:
            iterations = raw_iterations
        raw_text = event.get("text")
        if isinstance(raw_text, str) and raw_text:
            final_text = raw_text
        usage = event.get("usage")
        if not isinstance(usage, dict):
            # Fall back to the run-level aggregate snapshot when the primary
            # usage object is absent; otherwise record explicit unknowns.
            usage = event.get("aggregateUsage")
        if isinstance(usage, dict):
            usages.append(_cline_usage_from_dict(usage))
        else:
            usages.append(
                ProviderUsage(
                    usage_source="cline_run_result_without_usage",
                    raw_payload=_bounded_json(event),
                )
            )
    return ClineStructuredRun(
        text=final_text if final_text is not None else stdout,
        usage=usages,
        saw_run_result=saw_run_result,
        finish_reason=finish_reason,
        actual_model=actual_model,
        actual_provider=actual_provider,
        error_messages=tuple(error_messages),
        duration_ms=duration_ms,
        iterations=iterations,
    )





def persist_worker_usage(
    db: Database,
    *,
    run_id: str,
    association_id: str,
    attempt_id: str | None,
    review_id: str | None,
    stage: DispatchStage,
    tier: Tier,
    provider: str,
    model: str,
    prompt_bytes: int,
    transport: str,
    result: WorkerResult,
) -> list[ModelCallUsage]:
    """Persist one row per provider event, or one explicit unknown row."""
    provider_rows = result.usage or [ProviderUsage(usage_source="unavailable")]
    created: list[ModelCallUsage] = []
    for provider_usage in provider_rows:
        row = ModelCallUsage(
            id=f"usage_{uuid.uuid4().hex}",
            run_id=run_id,
            association_id=association_id,
            attempt_id=attempt_id,
            review_id=review_id,
            stage=stage,
            tier=tier,
            provider=provider,
            model=model,
            prompt_bytes=prompt_bytes,
            transport=transport,
            input_tokens=provider_usage.input_tokens,
            output_tokens=provider_usage.output_tokens,
            cached_tokens=provider_usage.cached_tokens,
            total_tokens=provider_usage.total_tokens,
            cache_read_tokens=provider_usage.cache_read_tokens,
            cache_write_tokens=provider_usage.cache_write_tokens,
            reasoning_tokens=provider_usage.reasoning_tokens,
            usage_source=provider_usage.usage_source,
            raw_usage_json=provider_usage.raw_payload,
            created_at=current_iso_timestamp(),
        )
        db.create_model_call_usage(row)
        created.append(row)
    return created


def aggregate_usage(rows: list[ModelCallUsage]) -> dict[str, object]:
    """Deterministically aggregate exact known fields and explicit coverage."""
    known = [row for row in rows if row.input_tokens is not None or row.output_tokens is not None]
    unknown = [row for row in rows if row.input_tokens is None and row.output_tokens is None]

    def sum_known(field: str) -> int | None:
        values = [getattr(row, field) for row in rows if getattr(row, field) is not None]
        return sum(values) if values else None

    by_stage = Counter(row.stage.value for row in rows)
    by_tier = Counter(row.tier.value for row in rows)
    by_model = Counter(f"{row.provider}/{row.model}" for row in rows)
    return {
        "known_input_tokens": sum_known("input_tokens"),
        "known_output_tokens": sum_known("output_tokens"),
        "known_cached_tokens": sum_known("cached_tokens"),
        "known_total_tokens": sum_known("total_tokens"),
        "calls_with_known_usage": len(known),
        "calls_with_unknown_usage": len(unknown),
        "coverage_complete": not unknown
        and all(
            row.input_tokens is not None and row.output_tokens is not None for row in rows
        ),
        "calls_by_stage": dict(sorted(by_stage.items())),
        "calls_by_tier": {tier.value: by_tier.get(tier.value, 0) for tier in Tier},
        "calls_by_provider_model": dict(sorted(by_model.items())),
        "t3_call_count": by_tier.get(Tier.T3.value, 0),
        "call_count": len(rows),
    }
