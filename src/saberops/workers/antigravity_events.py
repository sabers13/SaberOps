"""Native Antigravity terminal evidence; soft-denied tools prevent success."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from saberops.models import ProviderUsage


@dataclass
class AntigravityEvents:
    text: str = ""
    terminal_status: str | None = None
    denied: bool = False
    usage: list[ProviderUsage] = field(default_factory=list)


def parse_events(raw: str) -> AntigravityEvents:
    parsed = AntigravityEvents()
    fragments: list[str] = []
    for line in raw.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if event.get("event") == "step_update":
            step = event.get("step_update") or {}
            if isinstance(step.get("text_delta"), str):
                fragments.append(step["text_delta"])
            tool = step.get("tool_info") or {}
            error = tool.get("error") or {}
            denied_types = {
                "PERMISSION_DENIED",
                "SOFT_DENIED",
                "DENIED",
                "permission_denied",
                "PermissionDenied",
                "PermissionDeniedError",
                "soft_denied",
            }
            parsed.denied |= (
                step.get("state") in denied_types
                or error.get("type") in denied_types
                or step.get("permission_denied") is True
            )
        if event.get("event") == "result":
            result: dict[str, Any] = event.get("result") or {}
            parsed.terminal_status = result.get("status")
            parsed.text = result.get("response", "")
            parsed.denied |= bool(result.get("permission_denials") or result.get("denied_tools"))
            usage = result.get("usage")
            if isinstance(usage, dict):

                def integer(key: str, values: dict[str, Any] = usage) -> int | None:
                    v = values.get(key)
                    return v if type(v) is int and v >= 0 else None

                parsed.usage = [
                    ProviderUsage(
                        input_tokens=integer("input_tokens"),
                        output_tokens=integer("output_tokens"),
                        total_tokens=integer("total_tokens"),
                        cache_read_tokens=integer("cache_read_tokens"),
                        cached_tokens=integer("cache_read_tokens"),
                        cache_write_tokens=integer("cache_write_tokens"),
                        reasoning_tokens=integer("thinking_tokens"),
                        usage_source="antigravity_terminal_result",
                        raw_payload=json.dumps(usage),
                    )
                ]
    if not parsed.text:
        parsed.text = "".join(fragments)
    return parsed
