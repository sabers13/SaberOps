"""Provider-native token parsing, normalized persistence, and aggregation."""

from __future__ import annotations

import json
import uuid
from collections import Counter

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


def extract_opencode_identity(stdout: str) -> tuple[str | None, str | None]:
    """Return ``(reported_model, reported_variant)`` from OpenCode JSONL events."""
    events = _json_lines(stdout)
    reported_model: str | None = None
    reported_variant: str | None = None
    for event in events:
        part = event.get("part")
        part_dict = part if isinstance(part, dict) else {}
        for container in (part_dict, event):
            raw_model = container.get("modelID") or container.get("model")
            raw_provider = container.get("providerID") or container.get("provider")
            if isinstance(raw_model, str) and raw_model.strip():
                model_str = raw_model.strip()
                if (
                    isinstance(raw_provider, str)
                    and raw_provider.strip()
                    and "/" not in model_str
                ):
                    reported_model = f"{raw_provider.strip()}/{model_str}"
                else:
                    reported_model = model_str
            raw_variant = container.get("variant")
            if isinstance(raw_variant, str) and raw_variant.strip():
                reported_variant = raw_variant.strip()
    return reported_model, reported_variant


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
