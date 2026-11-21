"""F2: per-attempt and per-subscription usage in the job surfaces.

A job that ran two attempts on two providers must show both usages in
``get_job``, ``catch_up`` and the shared return summary, and an attempt whose
provider reported no counters must say ``unknown`` instead of zero.

Hermetic: a fresh rig store, the fake worker CLI for both providers, and no
vendor CLI, model or network call.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import pytest
from lifecycle_rig import build_lifecycle_rig

from saberops.db import Database, current_iso_timestamp
from saberops.jobs.models import Job, JobRequirements, JobState, WaitingReason
from saberops.jobs.store import insert_job, mark_dispatched, reconcile_transition
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer
from saberops.mcp.tools import ServerContext
from saberops.models import (
    Attempt,
    AttemptStatus,
    DispatchStage,
    Run,
    RunStatus,
    Tier,
    WorkerRequest,
)
from saberops.telemetry import persist_worker_usage
from saberops.workers.codex import CodexAdapter
from saberops.workers.opencode import OpenCodeAdapter

pytestmark = pytest.mark.packaged_routing_default

CODEX_MODEL = "gpt-5.6-terra"
OPENCODE_MODEL = "opencode-go/deepseek-v4-flash"


def _server(rig: Any) -> McpServer:
    state = rig.state_home / "saberops"
    context = ServerContext(
        db_path=rig.db().db_path,
        questions_store=state / "questions.json",
        config_ledger_path=state / "config.jsonl",
        evidence=EvidenceRoots(roots=(rig.root / ".orch",)),
        ledger=EventLedger(state / "events.jsonl"),
        lease=OrchestratorLease(state / "lease.json", holder="f2-test", pid=os.getpid()),
    )
    server = McpServer(context)
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def _payload(server: McpServer, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert response is not None
    result = cast(dict[str, Any], response["result"])
    assert result["isError"] is False, result
    return cast(dict[str, Any], result["structuredContent"])


def _seed_job(db: Database, rig: Any, *, job_id: str, run_id: str) -> None:
    now = current_iso_timestamp()
    db.create_run(
        Run(
            id=run_id,
            task="Two attempts on two providers",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command=rig.gate_command,
            status=RunStatus.COMPLETED,
            created_at=now,
            completed_at=now,
        )
    )
    insert_job(
        db,
        Job(
            job_id=job_id,
            idempotency_key=None,
            caller_id="planner",
            repo=str(rig.repo),
            base_ref="HEAD",
            base_sha=rig.base_sha,
            goal="Two attempts on two providers",
            acceptance="usage is visible",
            requirements=JobRequirements(),
            state=JobState.QUEUED,
            waiting_reason=WaitingReason.NEW,
            created_at=now,
            updated_at=now,
            request_digest="f2-digest",
        ),
    )
    mark_dispatched(db, job_id, run_id)
    reconcile_transition(db, job_id, to=JobState.SUCCEEDED)


def _attempt(
    db: Database,
    *,
    run_id: str,
    attempt_id: str,
    attempt_number: int,
    provider: str,
    model: str,
    result: Any,
    rig: Any,
) -> Attempt:
    now = current_iso_timestamp()
    attempt = Attempt(
        id=attempt_id,
        run_id=run_id,
        attempt_number=attempt_number,
        provider=provider,
        model=model,
        worktree_path=str(rig.repo),
        branch_name=f"orch/{run_id}/a{attempt_number}",
        status=result.status,
        worker_stdout=result.stdout,
        worker_stderr=result.stderr,
        worker_error=result.error,
        commit_sha=result.commit_sha,
        created_at=now,
        completed_at=now,
    )
    db.create_attempt(attempt)
    persist_worker_usage(
        db,
        run_id=run_id,
        association_id=attempt_id,
        attempt_id=attempt_id,
        review_id=None,
        stage=DispatchStage.IMPLEMENTATION,
        tier=Tier.T1,
        provider=provider,
        model=model,
        prompt_bytes=result.prompt_bytes or 0,
        transport=result.prompt_transport_used or "stdin",
        result=result,
    )
    return attempt


def test_job_with_two_attempts_on_two_providers_shows_both_usages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    job_id = "job-f2-two-attempts"
    run_id = "run-f2-two-attempts"
    _seed_job(db, rig, job_id=job_id, run_id=run_id)

    codex_result = CodexAdapter().run(
        WorkerRequest(
            task="first attempt on the codex subscription",
            worktree_path=rig.repo,
            model=CODEX_MODEL,
            timeout_seconds=30.0,
        )
    )
    assert codex_result.status is AttemptStatus.SUCCESS, codex_result
    assert codex_result.usage, codex_result
    _attempt(
        db,
        run_id=run_id,
        attempt_id=f"{run_id}-a1",
        attempt_number=1,
        provider="codex",
        model=CODEX_MODEL,
        result=codex_result,
        rig=rig,
    )

    opencode_result = OpenCodeAdapter().run(
        WorkerRequest(
            task="second attempt on the opencode subscription",
            worktree_path=rig.repo,
            model=OPENCODE_MODEL,
            timeout_seconds=30.0,
        )
    )
    assert opencode_result.status is AttemptStatus.SUCCESS, opencode_result
    assert opencode_result.usage, opencode_result
    _attempt(
        db,
        run_id=run_id,
        attempt_id=f"{run_id}-a2",
        attempt_number=2,
        provider="opencode",
        model=OPENCODE_MODEL,
        result=opencode_result,
        rig=rig,
    )

    server = _server(rig)
    got = _payload(server, "get_job", {"job_id": job_id})
    caught = _payload(server, "catch_up", {"job_id": job_id})

    attempts = got["usage"]["attempts"]
    assert [entry["attempt_id"] for entry in attempts] == [
        f"{run_id}-a1",
        f"{run_id}-a2",
    ]
    assert [entry["subscription"] for entry in attempts] == ["codex", "opencode"]
    assert [entry["provider"] for entry in attempts] == ["codex", "opencode"]
    assert attempts[0]["usage"]["reported"] is True
    assert attempts[1]["usage"]["reported"] is True
    assert attempts[0]["usage"]["input_tokens"] == 12
    assert attempts[0]["usage"]["output_tokens"] == 8
    assert attempts[1]["usage"]["input_tokens"] == 12
    assert attempts[1]["usage"]["output_tokens"] == 8
    assert attempts[0]["usage"]["usage_sources"] == ["codex_turn_completed_json"]
    assert attempts[1]["usage"]["usage_sources"] == ["opencode_step_finish_json"]
    assert got["usage"]["coverage_complete"] is True
    assert [
        entry["subscription"] for entry in got["usage"]["subscriptions"]
    ] == ["codex", "opencode"]

    # catch_up carries the same projection at its top level and in the
    # shared return summary; get_job's result usage is the same object.
    assert caught["usage"] == got["usage"]
    assert caught["result"]["usage"] == got["usage"]
    assert got["result"]["usage"] == got["usage"]


def test_attempt_without_provider_counters_reports_unknown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    job_id = "job-f2-unknown-usage"
    run_id = "run-f2-unknown-usage"
    _seed_job(db, rig, job_id=job_id, run_id=run_id)
    now = current_iso_timestamp()
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model=CODEX_MODEL,
            worktree_path=str(rig.repo),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            created_at=now,
            completed_at=now,
        )
    )

    server = _server(rig)
    got = _payload(server, "get_job", {"job_id": job_id})
    usage = got["usage"]
    entry = usage["attempts"][0]
    assert entry["subscription"] == "codex"
    assert entry["usage"]["reported"] is False
    assert entry["usage"]["input_tokens"] == "unknown"
    assert entry["usage"]["output_tokens"] == "unknown"
    assert entry["usage"]["total_tokens"] == "unknown"
    assert entry["usage"]["provider_usage_event_count"] == 0
    assert usage["subscriptions"][0]["usage"]["input_tokens"] == "unknown"
    assert usage["total"]["input_tokens"] == "unknown"
    assert usage["coverage_complete"] is False


def test_review_usage_belongs_to_the_reviewer_subscription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """B3 D8: a review dispatch's tokens are not dropped from the job view.

    The Codex batch showed only codex calls because ``job_usage_view`` walked
    attempts only; review usage rows are a separate dispatch (no attempt id).
    """
    from saberops.jobs.service import get_job, job_usage_view
    from saberops.models import ProviderUsage, WorkerResult

    rig = build_lifecycle_rig(tmp_path, monkeypatch)
    db = rig.db()
    job_id = "job-f2-review-usage"
    run_id = "run-f2-review-usage"
    _seed_job(db, rig, job_id=job_id, run_id=run_id)
    now = current_iso_timestamp()
    attempt_id = f"{run_id}-a1"
    db.create_attempt(
        Attempt(
            id=attempt_id,
            run_id=run_id,
            attempt_number=1,
            provider="codex",
            model=CODEX_MODEL,
            worktree_path=str(rig.repo),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.SUCCESS,
            created_at=now,
            completed_at=now,
        )
    )
    persist_worker_usage(
        db,
        run_id=run_id,
        association_id=attempt_id,
        attempt_id=attempt_id,
        review_id=None,
        stage=DispatchStage.IMPLEMENTATION,
        tier=Tier.T1,
        provider="codex",
        model=CODEX_MODEL,
        prompt_bytes=10,
        transport="stdin",
        result=WorkerResult(
            provider="codex",
            model=CODEX_MODEL,
            status=AttemptStatus.SUCCESS,
            summary="done",
            stdout="",
            stderr="",
            usage=[
                ProviderUsage(
                    input_tokens=12,
                    output_tokens=8,
                    total_tokens=20,
                    usage_source="codex_turn_completed_json",
                )
            ],
        ),
    )
    persist_worker_usage(
        db,
        run_id=run_id,
        association_id="rev-review-1",
        attempt_id=None,
        review_id="rev-review-1",
        stage=DispatchStage.REVIEW,
        tier=Tier.T1,
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        prompt_bytes=10,
        transport="stdin",
        result=WorkerResult(
            provider="anthropic",
            model="claude-haiku-4-5-20251001",
            status=AttemptStatus.SUCCESS,
            summary="VERDICT: PASS",
            stdout="VERDICT: PASS",
            stderr="",
            usage=[
                ProviderUsage(
                    input_tokens=100,
                    output_tokens=20,
                    total_tokens=120,
                    cache_read_tokens=5,
                    cache_write_tokens=7,
                    usage_source="claude_result_json",
                )
            ],
        ),
    )

    job = get_job(db, job_id)
    assert job is not None
    usage = job_usage_view(db, job)
    assert [entry["subscription"] for entry in usage["subscriptions"]] == [
        "claude",
        "codex",
    ]
    reviewer = usage["subscriptions"][0]
    assert reviewer["attempt_ids"] == []
    assert reviewer["review_ids"] == ["rev-review-1"]
    assert reviewer["usage"]["input_tokens"] == 100
    assert reviewer["usage"]["output_tokens"] == 20
    assert reviewer["usage"]["cache_read_tokens"] == 5
    assert reviewer["usage"]["cache_write_tokens"] == 7
    assert usage["reviews"][0]["review_id"] == "rev-review-1"
    assert usage["coverage_complete"] is True

    from saberops.benchmark import aggregate_subscription_usage

    rows = aggregate_subscription_usage(db, [job_id])
    by_subscription = {row.subscription: row for row in rows}
    assert set(by_subscription) == {"claude", "codex"}
    assert by_subscription["claude"].input_tokens == 100
    assert by_subscription["claude"].output_tokens == 20
    assert by_subscription["claude"].reported is True
