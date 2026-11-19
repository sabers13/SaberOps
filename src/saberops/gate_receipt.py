"""Receipt-bound duplicate-gate skip for acceptance (ADR-0010).

Acceptance runs the configured gate after fast-forwarding the target.  When the
target moves to exactly the candidate commit that already passed the same gate
for the same base and the same gate configuration, the second run is redundant.
ADR-0010 permits skipping it only when a durable receipt proves every
condition:

* the receipt is for the exact candidate SHA and the exact base SHA;
* the receipt is for the exact gate command and gate configuration;
* the receipt records a passing gate;
* the receipt is less than 24 hours old at acceptance.

Any mismatch, a merge/refresh that creates a new SHA, a changed command or
configuration, or an expired receipt means the full gate runs as before.  This
module only *reports* eligibility; acceptance still performs its own CAS and
post-CAS verification.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from saberops.config import RunConfig, reconstruct_run_config
from saberops.db import Database
from saberops.models import Run

#: Durable run-event type carrying one full-gate receipt.
FULL_GATE_RECEIPT_EVENT = "full_gate_receipt"

#: Receipt body schema version.  A receipt with another version is ignored.
RECEIPT_SCHEMA_VERSION = 1

#: ADR-0010 condition 5: a receipt is valid for less than 24 hours.
RECEIPT_MAX_AGE = timedelta(hours=24)


class GateReceiptError(ValueError):
    """A persisted receipt is malformed and cannot authorize a skip."""


@dataclass(frozen=True)
class GateConfiguration:
    """The gate-affecting fields of a frozen run configuration."""

    gate_enabled: bool
    gate_command: str
    gate_timeout: float
    gate_scratch_mode: str

    @classmethod
    def from_run_config(cls, config: RunConfig) -> GateConfiguration:
        return cls(
            gate_enabled=bool(config.gate_enabled),
            gate_command=str(config.gate_command),
            gate_timeout=float(config.gate_timeout),
            gate_scratch_mode=config.gate_scratch_mode.value,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "gate_enabled": self.gate_enabled,
            "gate_command": self.gate_command,
            "gate_timeout": self.gate_timeout,
            "gate_scratch_mode": self.gate_scratch_mode,
        }

    def digest(self) -> str:
        canonical = json.dumps(self.as_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def run_config_for_receipt(run: Run) -> RunConfig:
    """Return the frozen run configuration used to build and check receipts.

    ``Run.config_json`` is the persisted normalized payload.  A legacy or empty
    payload is reconstructed conservatively from the run row so a missing
    field can never make two different configurations share a digest.
    """
    data: dict[str, Any] = {}
    if run.config_json:
        try:
            parsed = json.loads(run.config_json)
        except (TypeError, ValueError) as exc:
            raise GateReceiptError(
                f"run {run.id} configuration JSON is malformed: {exc}"
            ) from exc
        if not isinstance(parsed, dict):
            raise GateReceiptError(
                f"run {run.id} configuration JSON is not an object"
            )
        data = parsed
    data.setdefault("task", run.task)
    data.setdefault("target_repo", run.target_repo)
    data.setdefault("gate_command", run.gate_command)
    return reconstruct_run_config(data)


def gate_configuration_digest(config: RunConfig) -> str:
    return GateConfiguration.from_run_config(config).digest()


def _canonical_digest(body: dict[str, Any]) -> str:
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_receipt(
    *,
    config: RunConfig,
    base_sha: str,
    candidate_sha: str,
    started_at: str,
    finished_at: str,
    passed: bool,
) -> dict[str, Any]:
    """Build the canonical receipt body, including its self-digest."""
    if not base_sha or not candidate_sha:
        raise GateReceiptError("a receipt needs both a base SHA and a candidate SHA")
    gate = GateConfiguration.from_run_config(config)
    body: dict[str, Any] = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "base_sha": base_sha,
        "candidate_sha": candidate_sha,
        "gate_command": gate.gate_command,
        "config_digest": gate.digest(),
        "gate_configuration": gate.as_dict(),
        "passed": bool(passed),
        "started_at": started_at,
        "finished_at": finished_at,
    }
    body["receipt_digest"] = _canonical_digest(body)
    return body


def record_receipt(
    db: Database,
    *,
    run_id: str,
    attempt_id: str | None,
    config: RunConfig,
    base_sha: str,
    candidate_sha: str,
    started_at: str,
    finished_at: str,
    passed: bool,
) -> dict[str, Any]:
    """Persist one receipt on the run's durable event stream and return it."""
    receipt = build_receipt(
        config=config,
        base_sha=base_sha,
        candidate_sha=candidate_sha,
        started_at=started_at,
        finished_at=finished_at,
        passed=passed,
    )
    db.record_event(
        run_id,
        FULL_GATE_RECEIPT_EVENT,
        attempt_id=attempt_id,
        payload=receipt,
    )
    return receipt


def _parse_timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def receipt_matches(
    receipt: object,
    *,
    base_sha: str,
    candidate_sha: str,
    gate_command: str,
    config_digest: str,
    now: datetime,
) -> bool:
    """Return True when ``receipt`` satisfies every ADR-0010 condition."""
    if not isinstance(receipt, dict):
        return False
    if receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        return False
    if receipt.get("passed") is not True:
        return False
    if receipt.get("base_sha") != base_sha:
        return False
    if receipt.get("candidate_sha") != candidate_sha:
        return False
    if receipt.get("gate_command") != gate_command:
        return False
    if receipt.get("config_digest") != config_digest:
        return False
    body = {key: value for key, value in receipt.items() if key != "receipt_digest"}
    if receipt.get("receipt_digest") != _canonical_digest(body):
        return False
    finished = _parse_timestamp(receipt.get("finished_at"))
    started = _parse_timestamp(receipt.get("started_at"))
    if finished is None or started is None:
        return False
    if finished < started:
        return False
    age = now - finished
    if age < -timedelta(minutes=5):
        # A receipt from the future is not trustworthy evidence.
        return False
    return age < RECEIPT_MAX_AGE


def find_valid_receipt(
    db: Database,
    run: Run,
    *,
    candidate_sha: str,
    now: datetime | None = None,
) -> dict[str, Any] | None:
    """Return the newest receipt that authorizes a duplicate-gate skip, or None.

    Every condition is re-checked here at acceptance time; a receipt is never
    trusted because it was accepted earlier or because acceptance is otherwise
    eligible.
    """
    config = run_config_for_receipt(run)
    gate = GateConfiguration.from_run_config(config)
    if not gate.gate_enabled:
        return None
    config_digest = gate.digest()
    current = now or datetime.now(UTC)
    valid: list[dict[str, Any]] = []
    for event in db.get_events_by_type(run.id, FULL_GATE_RECEIPT_EVENT):
        payload = event.payload
        if receipt_matches(
            payload,
            base_sha=run.base_commit,
            candidate_sha=candidate_sha,
            gate_command=run.gate_command,
            config_digest=config_digest,
            now=current,
        ):
            assert isinstance(payload, dict)  # receipt_matches proves this
            valid.append(payload)
    if not valid:
        return None
    return max(valid, key=lambda item: str(item.get("finished_at", "")))


def receipt_summary(receipt: dict[str, Any]) -> dict[str, Any]:
    """Project the skip evidence recorded with an acceptance."""
    return {
        "reason": "receipt-bound-fast-forward",
        "candidate_sha": receipt.get("candidate_sha"),
        "base_sha": receipt.get("base_sha"),
        "gate_command": receipt.get("gate_command"),
        "config_digest": receipt.get("config_digest"),
        "receipt_digest": receipt.get("receipt_digest"),
        "finished_at": receipt.get("finished_at"),
    }


__all__ = [
    "FULL_GATE_RECEIPT_EVENT",
    "GateConfiguration",
    "GateReceiptError",
    "RECEIPT_MAX_AGE",
    "RECEIPT_SCHEMA_VERSION",
    "build_receipt",
    "find_valid_receipt",
    "gate_configuration_digest",
    "receipt_summary",
    "record_receipt",
    "run_config_for_receipt",
]
