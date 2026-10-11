"""Plan exhaustion -> QUOTA_EXHAUSTED with reset time (T-004).

Classifier fixtures are taken from ``.orch/R-001/findings.md``: verbatim
where R-001 marks the signal VERIFIED, explicitly marked SYNTHETIC where
R-001 marks it INFERRED/UNKNOWN (Cline + Antigravity failure text, auth
messages, trap shapes).  No provider/LLM is called; all fixtures are
static strings fed to the pure classifiers or to adapters behind fake
runners.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from saberops.control_plane.quota_policy import quota_decision
from saberops.db import Database, current_iso_timestamp
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    QuotaState,
    RoutingState,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerInterruptionReason,
    WorkerRequest,
    WorkerResult,
)
from saberops.process import ProcessResult
from saberops.quota import apply_quota_policy
from saberops.workers.exhaustion import (
    PROVISIONAL,
    VERIFIED,
    ExhaustionVerdict,
    build_quota_observation,
    classify_antigravity,
    classify_claude,
    classify_cline,
    classify_codex,
    classify_opencode,
    parse_antigravity_usage,
    record_exhaustion,
    reset_to_iso,
)


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


# ---------------------------------------------------------------------------
# Codex fixtures (R-001: VERBATIM event shapes + message texts).
# ---------------------------------------------------------------------------

# VERBATIM plan-limit text (harness-cli-normalizer #325), dual events.
CODEX_QUOTA_TEXT = (
    "You've hit your usage limit. Visit "
    "https://chatgpt.com/codex/settings/usage to purchase more credits "
    "or try again at Oct 3rd, 2026 11:58 PM."
)
CODEX_QUOTA_STDOUT = (
    _line({"type": "error", "message": CODEX_QUOTA_TEXT})
    + "\n"
    + _line({"type": "turn.failed", "error": {"message": CODEX_QUOTA_TEXT}})
)

# VERBATIM API rate-limit text (codex #246, #88).
CODEX_RATE_TEXT = (
    "Rate limit reached for o4-mini in organization org-abc on tokens "
    "per min (TPM): Limit 200000, Used 162582, Requested 45297. "
    "Please try again in 2.363s. ..."
)
CODEX_RATE_STDOUT = _line({"type": "turn.failed", "error": {"message": CODEX_RATE_TEXT}})

# VERBATIM capacity text (codex #43337), dual events.
CODEX_CAPACITY_TEXT = "Selected model is at capacity. Please try a different model."
CODEX_CAPACITY_STDOUT = (
    _line({"type": "error", "message": CODEX_CAPACITY_TEXT})
    + "\n"
    + _line({"type": "turn.failed", "error": {"message": CODEX_CAPACITY_TEXT}})
)

# SYNTHETIC auth failure (no verified Codex auth text in R-001; shape only).
CODEX_AUTH_STDOUT = _line(
    {"type": "turn.failed", "error": {"message": "Not authenticated. Please run `codex login`."}}
)

# SYNTHETIC Interrupted trap: stream ends with no terminal turn event (#50585).
CODEX_INTERRUPTED_STDOUT = (
    _line({"type": "thread.started", "thread_id": "thread-1"})
    + "\n"
    + _line({"type": "turn.started"})
)

# SYNTHETIC success shape.
CODEX_SUCCESS_STDOUT = _line(
    {"type": "turn.completed", "usage": {"input_tokens": 10, "output_tokens": 5}}
)


def test_codex_quota_verbatim() -> None:
    from datetime import timezone
    result = classify_codex(CODEX_QUOTA_STDOUT, tz=timezone.utc)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.confidence == VERIFIED
    # M2-1: prose date without timezone is a HINT in host-local time.
    assert result.reset_confidence == "HINT"
    assert result.reset_at == datetime(2026, 10, 3, 23, 58, tzinfo=UTC)
    assert result.window is None
    assert result.model_scope is None


def test_codex_rate_limit_verbatim() -> None:
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    result = classify_codex(CODEX_RATE_STDOUT, now=now)
    assert result.verdict is ExhaustionVerdict.RATE_LIMITED
    assert result.reset_at is not None
    assert abs((result.reset_at - now).total_seconds() - 2.363) < 0.01


def test_codex_capacity_is_other() -> None:
    result = classify_codex(CODEX_CAPACITY_STDOUT)
    assert result.verdict is ExhaustionVerdict.OTHER


def test_codex_auth_is_never_quota() -> None:
    result = classify_codex(CODEX_AUTH_STDOUT)
    assert result.verdict is ExhaustionVerdict.OTHER


def test_codex_interrupted_stream_end_is_other() -> None:
    # Trap: Interrupted turns emit NO terminal event; stream end alone
    # must not classify as a limit.
    assert classify_codex(CODEX_INTERRUPTED_STDOUT).verdict is ExhaustionVerdict.OTHER
    assert classify_codex("").verdict is ExhaustionVerdict.OTHER
    assert classify_codex(CODEX_SUCCESS_STDOUT).verdict is ExhaustionVerdict.OTHER


# ---------------------------------------------------------------------------
# Claude fixtures (R-001: VERBATIM captures).
# ---------------------------------------------------------------------------

# VERBATIM limit prose (rhei #451) with the success/is_error trap.
CLAUDE_SESSION_STDOUT = (
    _line({"type": "system", "subtype": "init", "model": "claude-opus-4-6"})
    + "\n"
    + _line(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "text",
                        "text": (
                        "You've hit your session limit \u00b7 "
                        "resets 10:20pm (Europe/Zurich)"
                    ),
                    }
                ],
            },
            "error": "rate_limit",
        }
    )
    + "\n"
    + _line(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "result": "You've hit your session limit \u00b7 resets 10:20pm (Europe/Zurich)",
        }
    )
)

# VERBATIM structured companions (thirdshift #587): epoch resetsAt plus
# weekly prose with an explicit UTC zone.
CLAUDE_WEEKLY_STDOUT = (
    _line(
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": []},
            "api_error": {
                "api_error": "usage_limit_reached",
                "rateLimitType": "seven_day",
                "resetsAt": 1791597600,
                "overageStatus": "rejected",
            },
        }
    )
    + "\n"
    + _line(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 429,
            "result": (
                "You've hit your monthly spend limit \u00b7 raise it at "
                "https://claude.ai/settings/usage?from=cc_cli_limit_message "
                "\u00b7 your weekly limit resets Oct 10, 2am (UTC)"
            ),
        }
    )
)

# SYNTHETIC rate surface (VERBATIM text "Request rejected (429)").
CLAUDE_RATE_STDOUT = _line(
    {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "Request rejected (429)",
        "errors": ["Request rejected (429)"],
    }
)

# VERBATIM throttle text -> OTHER.
CLAUDE_THROTTLE_STDOUT = _line(
    {
        "type": "result",
        "subtype": "error_during_execution",
        "is_error": True,
        "result": "Server is temporarily limiting requests",
    }
)

# VERBATIM spend-cap marker -> OTHER (needs human action, no reset).
CLAUDE_SPEND_CAP_STDOUT = _line(
    {
        "type": "assistant",
        "message": {"role": "assistant", "content": []},
        "error": {"details": {"error_code": "enforced_spend_limit_reached"}},
    }
)

# SYNTHETIC auth failure.
CLAUDE_AUTH_STDOUT = _line(
    {"type": "result", "subtype": "success", "is_error": True, "result": "Not authenticated"}
)

# SYNTHETIC clean success.
CLAUDE_SUCCESS_STDOUT = _line(
    {"type": "result", "subtype": "success", "is_error": False, "result": "Done"}
)


def test_claude_success_trap_session_limit() -> None:
    # The trap: subtype "success" must NOT read as success.
    from datetime import timezone
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    result = classify_claude(CLAUDE_SESSION_STDOUT, now=now, tz=timezone.utc)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.confidence == VERIFIED
    # M2-1: bare clock prose is a HINT (next host-local occurrence).
    assert result.reset_confidence == "HINT"
    assert result.reset_at == datetime(2026, 10, 11, 22, 20, tzinfo=UTC)
    assert result.window == "5h"
    assert result.model_scope is None


def test_claude_structured_epoch_reset() -> None:
    result = classify_claude(CLAUDE_WEEKLY_STDOUT)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.reset_at == datetime.fromtimestamp(1791597600, tz=UTC)


def test_claude_zoned_dated_prose_reset() -> None:
    now = datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    stdout = _line(
        {
            "type": "result",
            "subtype": "success",
            "is_error": True,
            "api_error_status": 429,
            "result": "You've hit your weekly limit \u00b7 resets Oct 10, 2am (UTC)",
        }
    )
    result = classify_claude(stdout, now=now)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.reset_at == datetime(2026, 10, 10, 2, 0, tzinfo=UTC)


def test_claude_rate_limit() -> None:
    assert classify_claude(CLAUDE_RATE_STDOUT).verdict is ExhaustionVerdict.RATE_LIMITED


def test_claude_throttle_and_spend_cap_are_other() -> None:
    assert classify_claude(CLAUDE_THROTTLE_STDOUT).verdict is ExhaustionVerdict.OTHER
    assert classify_claude(CLAUDE_SPEND_CAP_STDOUT).verdict is ExhaustionVerdict.OTHER


def test_claude_auth_and_success_are_other() -> None:
    assert classify_claude(CLAUDE_AUTH_STDOUT).verdict is ExhaustionVerdict.OTHER
    assert classify_claude(CLAUDE_SUCCESS_STDOUT).verdict is ExhaustionVerdict.OTHER


# ---------------------------------------------------------------------------
# OpenCode fixtures (R-001: VERBATIM live envelopes).
# ---------------------------------------------------------------------------

# VERBATIM Zen pool exhaustion (coforge #749): quota despite rate text.
OPENCODE_ZEN_QUOTA_STDOUT = _line(
    {
        "type": "error",
        "timestamp": "2026-09-23T10:00:00Z",
        "sessionID": "ses_abc",
        "error": {
            "type": "provider.quota",
            "message": "Rate limit exceeded. Please try again later.",
            "status": 429,
        },
    }
)

# VERBATIM ClinePass weekly limit through opencode (relevo #616).
OPENCODE_CLINEPASS_STDOUT = _line(
    {
        "type": "error",
        "timestamp": "2026-09-23T10:00:00Z",
        "sessionID": "ses_abc",
        "error": {
            "type": "provider.rate-limit",
            "message": (
                "Error 429: You have reached your weekly Clinepass limit. "
                "The limit resets in 23m, please try again later."
            ),
            "status": 429,
        },
    }
)

# VERBATIM Zen free-tier envelope (sst/opencode #33495).
OPENCODE_FREE_TIER_STDOUT = _line(
    {
        "type": "error",
        "error": {
            "type": "FreeUsageLimitError",
            "message": "Rate limit exceeded. Please try again later.",
        },
        "statusCode": 429,
    }
)

# VERBATIM cheatsheet burst-rate shape -> RATE_LIMITED.
OPENCODE_RATE_STDOUT = _line(
    {
        "type": "error",
        "timestamp": "2026-09-23T10:00:00Z",
        "sessionID": "ses_abc",
        "error": {
            "name": "APIError",
            "data": {"message": "Rate limit exceeded", "statusCode": 429, "isRetryable": True},
        },
    }
)

# SYNTHETIC trap: step_finish stream with no error event -> OTHER.
OPENCODE_NO_ERROR_STDOUT = (
    _line({"type": "step_start"})
    + "\n"
    + _line({"type": "step_finish", "part": {"type": "step-finish", "reason": "stop"}})
)

# SYNTHETIC auth failure (VERBATIM AUTH_REQUIRED lead from OpenMausBot).
OPENCODE_AUTH_STDOUT = _line(
    {
        "type": "error",
        "error": {
            "name": "APIError",
            "data": {"message": "AUTH_REQUIRED: not authenticated", "statusCode": 401},
        },
    }
)


def test_opencode_zen_quota_despite_rate_text() -> None:
    result = classify_opencode(OPENCODE_ZEN_QUOTA_STDOUT)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.confidence == VERIFIED


def test_opencode_clinepass_window_with_relative_reset() -> None:
    now = datetime(2026, 10, 11, 12, 0, tzinfo=UTC)
    result = classify_opencode(OPENCODE_CLINEPASS_STDOUT, now=now)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.reset_at is not None
    assert abs((result.reset_at - now).total_seconds() - 23 * 60) < 1.0


def test_opencode_free_tier_quota() -> None:
    assert classify_opencode(OPENCODE_FREE_TIER_STDOUT).verdict is ExhaustionVerdict.QUOTA_EXHAUSTED


def test_opencode_burst_rate_is_rate_limited() -> None:
    assert classify_opencode(OPENCODE_RATE_STDOUT).verdict is ExhaustionVerdict.RATE_LIMITED


def test_opencode_missing_step_finish_is_other() -> None:
    # Trap: missing step_finish must never imply a limit.
    assert classify_opencode(OPENCODE_NO_ERROR_STDOUT).verdict is ExhaustionVerdict.OTHER


def test_opencode_auth_is_never_quota() -> None:
    assert classify_opencode(OPENCODE_AUTH_STDOUT).verdict is ExhaustionVerdict.OTHER


# ---------------------------------------------------------------------------
# Cline fixtures (ALL SYNTHETIC: R-001 UNKNOWN exact signal; PROVISIONAL).
# ---------------------------------------------------------------------------

CLINE_QUOTA_STDERR = _line(
    {
        "type": "error",
        "message": "ClinePassLimitError: You have reached your weekly Clinepass limit",
    }
)
CLINE_RATE_STDERR = _line(
    {"type": "error", "message": "Rate limit exceeded, too many requests (429)"}
)
CLINE_AUTH_STDERR = _line({"type": "error", "message": "Not authenticated: sign in to Cline"})
CLINE_COMPLETED_STDOUT = _line(
    {
        "type": "run_result",
        "ts": "2026-10-11T10:00:00Z",
        "finishReason": "completed",
        "text": "Done",
    }
)


def test_cline_provisional_quota_rule() -> None:
    result = classify_cline("", CLINE_QUOTA_STDERR)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.confidence == PROVISIONAL
    # Reset format UNKNOWN: never parsed.
    assert result.reset_at is None


def test_cline_rate_auth_and_completed() -> None:
    assert classify_cline("", CLINE_RATE_STDERR).verdict is ExhaustionVerdict.RATE_LIMITED
    assert classify_cline("", CLINE_AUTH_STDERR).verdict is ExhaustionVerdict.OTHER
    assert classify_cline(CLINE_COMPLETED_STDOUT, "").verdict is ExhaustionVerdict.OTHER
    assert classify_cline("", "").verdict is ExhaustionVerdict.OTHER


# ---------------------------------------------------------------------------
# Antigravity fixtures (failure text SYNTHETIC/INFERRED; /usage VERIFIED).
# ---------------------------------------------------------------------------

AGY_QUOTA_STDOUT = _line(
    {
        "event": "result",
        "result": {
            "status": "ERROR",
            "error": "RESOURCE_EXHAUSTED: quota exhausted for gemini-weekly pool",
            "usage": {},
        },
    }
)
AGY_RATE_STDOUT = _line(
    {
        "event": "result",
        "result": {"status": "ERROR", "error": "Rate limit exceeded, retry shortly"},
    }
)
AGY_SUCCESS_STDOUT = _line(
    {"event": "result", "result": {"status": "SUCCESS", "response": "Done"}}
)

# VERBATIM /usage shape (ai-limits-gnome #3, agy 1.3.1).
AGY_USAGE_JSON = json.dumps(
    {
        "status": "SUCCESS",
        "command": {
            "name": "usage",
            "data": {
                "groups": [
                    {
                        "name": "Gemini Models",
                        "buckets": [
                            {
                                "id": "gemini-weekly",
                                "window": "weekly",
                                "remaining_fraction": 0.0,
                                "reset_time": "2026-10-13T09:15:45Z",
                            }
                        ],
                    },
                    {
                        "name": "Claude and GPT models",
                        "buckets": [
                            {
                                "id": "3p-weekly",
                                "window": "weekly",
                                "remaining_fraction": 1.0,
                                "reset_time": "2026-10-15T13:32:48Z",
                            }
                        ],
                    },
                ]
            },
        },
    }
)


def test_antigravity_provisional_quota_with_usage_reset() -> None:
    result = classify_antigravity(AGY_QUOTA_STDOUT, usage_json=AGY_USAGE_JSON)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.confidence == PROVISIONAL
    # Most-exhausted bucket (remaining 0) selects the reset clock.
    assert result.reset_at == datetime(2026, 10, 13, 9, 15, 45, tzinfo=UTC)


def test_antigravity_quota_without_usage_has_no_reset() -> None:
    result = classify_antigravity(AGY_QUOTA_STDOUT)
    assert result.verdict is ExhaustionVerdict.QUOTA_EXHAUSTED
    assert result.reset_at is None


def test_antigravity_rate_success_and_auth() -> None:
    assert classify_antigravity(AGY_RATE_STDOUT).verdict is ExhaustionVerdict.RATE_LIMITED
    assert classify_antigravity(AGY_SUCCESS_STDOUT).verdict is ExhaustionVerdict.OTHER
    assert (
        classify_antigravity("", "AGY_ERROR: Not authenticated").verdict is ExhaustionVerdict.OTHER
    )
    assert parse_antigravity_usage("not json") is None
    assert parse_antigravity_usage(json.dumps({"status": "SUCCESS"})) is None


# ---------------------------------------------------------------------------
# Adapter mapping: QUOTA_EXHAUSTED -> interruption; RATE_LIMITED unchanged.
# ---------------------------------------------------------------------------


@pytest.fixture()
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for module in (
        "saberops.workers.codex",
        "saberops.workers.claude",
        "saberops.workers.opencode",
        "saberops.workers.cline",
        "saberops.workers.antigravity",
    ):
        monkeypatch.setattr(f"{module}.GitManager.get_changed_paths", lambda path: [])
    return tmp_path


def _require_quota_interruption(result: WorkerResult, provider: str) -> None:
    assert result.provider == provider
    assert result.status is AttemptStatus.FAILED
    assert result.interruption_reason is WorkerInterruptionReason.QUOTA_EXHAUSTED
    # The orchestrator's continuation-after-quota path consumes the
    # interruption; the outcome must never be capability evidence.
    assert result.outcome is DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE
    assert "quota exhausted" in (result.error or "").lower()


def test_codex_adapter_maps_quota_to_interruption(worktree: Path) -> None:
    from saberops.workers.codex import CodexAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 1, CODEX_QUOTA_STDOUT, "", 0.1)

    result = CodexAdapter(runner=runner).run(WorkerRequest("Do work", worktree, "gpt-5"))
    _require_quota_interruption(result, "codex")
    # M2-1: prose reset without timezone is a HINT clock.
    assert result.quota_reset_at is not None
    assert result.quota_reset_confidence == "HINT"


def test_codex_adapter_rate_limit_keeps_retry_behaviour(worktree: Path) -> None:
    from saberops.workers.codex import CodexAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 1, CODEX_RATE_STDOUT, "", 0.1)

    result = CodexAdapter(runner=runner).run(WorkerRequest("Do work", worktree, "gpt-5"))
    assert result.status is AttemptStatus.FAILED
    assert result.interruption_reason is None
    assert result.outcome is DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE


def _claude_auth() -> str:
    return json.dumps({"loggedIn": True, "authMethod": "claude.ai", "apiProvider": "firstParty"})


def test_claude_adapter_maps_success_trap_to_interruption(worktree: Path) -> None:
    from saberops.workers.claude import ClaudeCodeAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        if "auth" in argv:
            return ProcessResult(argv, 0, _claude_auth(), "", 0.1)
        return ProcessResult(argv, 1, CLAUDE_SESSION_STDOUT, "", 0.1)

    result = ClaudeCodeAdapter(runner=runner).run(WorkerRequest("Do work", worktree, "sonnet"))
    _require_quota_interruption(result, "anthropic")


def test_claude_adapter_rate_limit_keeps_retry_behaviour(worktree: Path) -> None:
    from saberops.workers.claude import ClaudeCodeAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        if "auth" in argv:
            return ProcessResult(argv, 0, _claude_auth(), "", 0.1)
        return ProcessResult(argv, 1, CLAUDE_RATE_STDOUT, "", 0.1)

    result = ClaudeCodeAdapter(runner=runner).run(WorkerRequest("Do work", worktree, "sonnet"))
    assert result.status is AttemptStatus.FAILED
    assert result.interruption_reason is None


def test_opencode_adapter_maps_quota_to_interruption(worktree: Path) -> None:
    from saberops.workers.opencode import OpenCodeAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 1, OPENCODE_ZEN_QUOTA_STDOUT, "", 0.1)

    result = OpenCodeAdapter(runner=runner).run(
        WorkerRequest("Do work", worktree, "opencode/claude-sonnet")
    )
    _require_quota_interruption(result, "opencode")


def test_opencode_adapter_passes_relative_reset_through(worktree: Path) -> None:
    from saberops.workers.opencode import OpenCodeAdapter

    before = datetime.now(UTC)

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 1, OPENCODE_CLINEPASS_STDOUT, "", 0.1)

    result = OpenCodeAdapter(runner=runner).run(
        WorkerRequest("Do work", worktree, "opencode/claude-sonnet")
    )
    _require_quota_interruption(result, "opencode")
    assert result.quota_reset_at is not None
    reset = datetime.fromisoformat(result.quota_reset_at)
    assert 22 * 60 <= (reset - before).total_seconds() <= 24 * 60


def test_opencode_adapter_rate_limit_keeps_retry_behaviour(worktree: Path) -> None:
    from saberops.workers.opencode import OpenCodeAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 1, OPENCODE_RATE_STDOUT, "", 0.1)

    result = OpenCodeAdapter(runner=runner).run(
        WorkerRequest("Do work", worktree, "opencode/claude-sonnet")
    )
    assert result.status is AttemptStatus.FAILED
    assert result.interruption_reason is None


def test_cline_adapter_maps_provisional_quota_to_interruption(worktree: Path) -> None:
    from saberops.workers.cline import ClineAdapter

    stdout = (
        _line(
            {
                "type": "agent_event",
                "ts": "2026-10-11T10:00:00Z",
                "event": {"type": "error", "error": {"message": "ClinePassLimitError: weekly"}},
            }
        )
        + "\n"
        + _line(
            {
                "type": "run_result",
                "ts": "2026-10-11T10:00:01Z",
                "finishReason": "error",
                "text": "",
            }
        )
    )

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 0, stdout, CLINE_QUOTA_STDERR, 0.1)

    result = ClineAdapter(runner=runner).run(WorkerRequest("Do work", worktree, "cline-pass/m"))
    _require_quota_interruption(result, "cline")


def test_antigravity_adapter_maps_quota_to_interruption(worktree: Path) -> None:
    from saberops.workers.antigravity import AntigravityAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 1, AGY_QUOTA_STDOUT, "", 0.1)

    result = AntigravityAdapter(runner=runner).run(WorkerRequest("Do work", worktree, "m-high"))
    _require_quota_interruption(result, "antigravity")


def test_antigravity_adapter_rate_limit_keeps_retry_behaviour(worktree: Path) -> None:
    from saberops.workers.antigravity import AntigravityAdapter

    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        return ProcessResult(argv, 1, AGY_RATE_STDOUT, "", 0.1)

    result = AntigravityAdapter(runner=runner).run(WorkerRequest("Do work", worktree, "m-high"))
    assert result.status is AttemptStatus.FAILED
    assert result.interruption_reason is None


# ---------------------------------------------------------------------------
# Quota evidence: exhaustion may only EXCLUDE; never metered failover.
# ---------------------------------------------------------------------------


def _seed_run(db: Database, tmp_path: Path, run_id: str) -> None:
    db.create_run(
        Run(
            id=run_id,
            task="plan-exhaustion probe",
            target_repo=str(tmp_path),
            base_commit="0" * 40,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=current_iso_timestamp(),
        )
    )


def test_record_exhaustion_writes_fresh_zero_evidence(tmp_path: Path) -> None:
    db = Database(tmp_path / "exh.db")
    _seed_run(db, tmp_path, "run-1")
    reset = datetime(2026, 10, 13, 9, 15, 45, tzinfo=UTC)
    state = record_exhaustion(db, provider="codex", run_id="run-1", reset_at=reset)
    assert state.provider == "codex"
    assert state.remaining_percent == 0.0
    # M2-1: no sticky BLOCK; exclusion via the seam (routing NORMAL).
    assert state.routing_state is RoutingState.NORMAL
    assert state.reset_at == reset_to_iso(reset)
    assert state.exclusion_id
    assert db.list_quota() == []
    observations = db.get_quota_observations_for_run("run-1")
    assert len(observations) == 1
    observation = observations[0]
    assert observation.freshness == "FRESH"
    assert observation.raw_value == 0.0
    assert observation.normalized_remaining_pct == 0.0
    assert observation.reset_at == reset_to_iso(reset)


def test_exhaustion_evidence_only_excludes_never_enables(tmp_path: Path) -> None:
    db = Database(tmp_path / "exh.db")
    _seed_run(db, tmp_path, "run-1")
    chain = [
        WorkerCandidate(provider="codex", model="gpt-5"),
        WorkerCandidate(provider="opencode", model="opencode/m"),
        WorkerCandidate(provider="anthropic", model="sonnet"),
    ]
    states: list[QuotaState] = [
        record_exhaustion(db, provider="codex", run_id="run-1"),
        record_exhaustion(db, provider="opencode", run_id="run-1"),
    ]
    # M2-1: legacy apply_quota_policy sees NORMAL (sticky block removed);
    # exclusion denies via quota_decision through the seam instead.
    filtered = apply_quota_policy(chain, states)
    assert [c.provider for c in filtered] == ["codex", "opencode", "anthropic"]
    # Canonical control-plane admission: exhausted pools are ineligible.
    assert quota_decision(chain[0], states).eligible is False
    assert quota_decision(chain[1], states).eligible is False
    assert quota_decision(chain[2], states).eligible is True


def test_exhaustion_never_fails_over_to_metered_route(tmp_path: Path) -> None:
    """Hard rule: exhaustion must never synthesize an API-key route.

    When every chain member is exhausted, routing ends with an empty
    chain -- never with an injected OmniRoute / direct_api candidate.
    The adapters likewise preserve the subscription provider identity
    instead of rewriting the route.
    """
    db = Database(tmp_path / "exh.db")
    _seed_run(db, tmp_path, "run-1")
    chain = [
        WorkerCandidate(provider="codex", model="gpt-5"),
        WorkerCandidate(provider="anthropic", model="sonnet"),
    ]
    states: list[QuotaState] = [
        record_exhaustion(db, provider="codex", run_id="run-1"),
        record_exhaustion(db, provider="anthropic", run_id="run-1"),
    ]
    survivors = [c for c in chain if quota_decision(c, states).eligible]
    assert survivors == []
    # No metered provider is admitted by exhaustion evidence: a provider
    # with no observation (e.g. omniroute) gains nothing and, crucially,
    # nothing in this module ever constructs a candidate for one.
    metered = WorkerCandidate(provider="omniroute", model="auto")
    assert quota_decision(metered, states).eligible is True  # no evidence against it...
    assert metered not in chain  # ...but exhaustion never adds it to the chain
    assert all(c.provider in {"codex", "anthropic"} for c in survivors)


def test_build_quota_observation_defaults() -> None:
    observation = build_quota_observation(provider="ANTHROPIC")
    assert observation.provider == "anthropic"
    assert observation.pool == "weekly"
    assert observation.freshness == "FRESH"
    assert observation.normalized_remaining_pct == 0.0
    assert observation.reset_at is None
