"""F21: reliable family tokens for Claude, Codex and OpenCode Go.

Covers the closed vocabulary (``anthropic``, ``codex``, ``opencode-go``), the
submit-time refusal of an include no build-route candidate satisfies, the chosen
provider/model in the result projection, and the documentation itself.

Hermetic: temporary stores and an explicit routing snapshot; no vendor CLI,
model or network call.
"""

from __future__ import annotations

import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops.capacity.admission import matches_model_family
from saberops.db import Database
from saberops.jobs.feasibility import feasible
from saberops.jobs.models import (
    NO_ROUTE_FOR_REQUIREMENTS,
    Job,
    JobRequirements,
    JobState,
    WaitingReason,
)
from saberops.jobs.service import submit_job
from saberops.mcp.guide import (
    AMBIGUOUS_FAMILY_TOKENS,
    FAMILY_TOKENS,
    GUIDE,
)
from saberops.mcp.tools import _job_guidance, _job_result
from saberops.models import Attempt, AttemptStatus, Run, RunStatus, Tier, WorkerCandidate
from saberops.routing_config import RoutingSnapshot

_NOW = datetime(2026, 11, 19, 12, 0, 0, tzinfo=UTC)

CLAUDE = WorkerCandidate(provider="anthropic", model="claude-sonnet-4-6")
CODEX = WorkerCandidate(provider="codex", model="gpt-5.6-terra")
OPENCODE_GO = WorkerCandidate(provider="opencode", model="opencode-go/deepseek-v4-flash")
OPENCODE_CHATGPT = WorkerCandidate(
    provider="opencode", model="opencode/gpt-5.6-terra"
)
OPENCODE_GO_CLAUDE = WorkerCandidate(
    provider="opencode", model="opencode-go/claude-haiku-5-5"
)


def _snapshot(*candidates: WorkerCandidate) -> RoutingSnapshot:
    return RoutingSnapshot(
        schema_version=3,
        source="test",
        source_path=None,
        content_hash="f21",
        role_chains=(("BUILD", (tuple(candidates),)),),
    )


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "f21@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "F21"], cwd=path, check=True)
    (path / "README.md").write_text("# f21\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


# ---------------------------------------------------------------------------
# the vocabulary
# ---------------------------------------------------------------------------


def test_supported_tokens_are_exactly_the_three_documented_ones() -> None:
    assert FAMILY_TOKENS == ("anthropic", "codex", "opencode-go")
    assert AMBIGUOUS_FAMILY_TOKENS == ("claude", "opencode")


@pytest.mark.parametrize(
    ("family", "candidate", "expected"),
    [
        ("anthropic", CLAUDE, True),
        ("codex", CODEX, True),
        ("opencode-go", OPENCODE_GO, True),
        # The two traps: `claude` also selects a Go-hosted Claude model and
        # `opencode` selects a ChatGPT-plan OpenCode model.
        ("claude", CLAUDE, True),
        ("claude", OPENCODE_GO_CLAUDE, True),
        ("opencode", OPENCODE_CHATGPT, True),
        ("opencode-go", OPENCODE_CHATGPT, False),
        ("anthropic", OPENCODE_GO_CLAUDE, False),
    ],
)
def test_matcher_semantics_are_unchanged(
    family: str, candidate: WorkerCandidate, expected: bool
) -> None:
    """F21 reuses the matcher; it does not change what a token matches."""
    assert matches_model_family(candidate, family) is expected


# ---------------------------------------------------------------------------
# submit-time feasibility
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("family", FAMILY_TOKENS)
def test_supported_family_include_is_feasible(family: str) -> None:
    snapshot = _snapshot(CLAUDE, CODEX, OPENCODE_GO)
    ok, reason = feasible(JobRequirements(model_family_include=(family,)), snapshot)
    assert ok is True
    assert reason is None


def test_include_matching_nothing_is_not_feasible() -> None:
    snapshot = _snapshot(CLAUDE, CODEX)
    ok, reason = feasible(JobRequirements(model_family_include=("gemini",)), snapshot)
    assert ok is False
    assert reason == NO_ROUTE_FOR_REQUIREMENTS


def test_opencode_go_include_never_satisfied_by_chatgpt_plan_model() -> None:
    snapshot = _snapshot(OPENCODE_CHATGPT)
    ok, reason = feasible(
        JobRequirements(model_family_include=("opencode-go",)), snapshot
    )
    assert ok is False
    assert reason == NO_ROUTE_FOR_REQUIREMENTS


def test_include_and_capability_must_be_satisfied_together() -> None:
    """A matching family without the capability cannot make the include feasible."""
    snapshot = _snapshot(CLAUDE)
    ok, reason = feasible(
        JobRequirements(
            model_family_include=("anthropic",),
            required_capabilities=("tool_restriction",),
        ),
        snapshot,
    )
    assert ok is False
    assert reason == NO_ROUTE_FOR_REQUIREMENTS


def test_submit_refuses_unsatisfiable_include_with_supported_tokens(
    tmp_path: Path,
) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    result = submit_job(
        db,
        repo=repo,
        goal="run this on a family nothing provides",
        requirements=JobRequirements(model_family_include=("gemini",)),
        routing=_snapshot(CLAUDE, CODEX),
        now=_NOW,
    )
    assert result.job.state == JobState.FAILED
    assert result.job.failure_reason == NO_ROUTE_FOR_REQUIREMENTS
    guidance = _job_guidance(result.job)
    assert guidance is not None
    for token in FAMILY_TOKENS:
        assert f"`{token}`" in guidance
    assert "`claude`" in guidance and "not supported tokens" in guidance


def test_submit_accepts_supported_include_on_the_build_route(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    result = submit_job(
        db,
        repo=repo,
        goal="run this on Claude",
        requirements=JobRequirements(model_family_include=("anthropic",)),
        routing=_snapshot(CLAUDE, CODEX),
        now=_NOW,
    )
    assert result.job.state == JobState.QUEUED
    assert result.job.waiting_reason == WaitingReason.NEW
    assert result.job.failure_reason is None


# ---------------------------------------------------------------------------
# the result names the chosen worker
# ---------------------------------------------------------------------------


def test_job_result_names_the_chosen_provider_and_model(tmp_path: Path) -> None:
    db = Database(tmp_path / "jobs.db")
    repo = _git_repo(tmp_path / "repo")
    submitted = submit_job(
        db,
        repo=repo,
        goal="run this on Claude",
        requirements=JobRequirements(model_family_include=("anthropic",)),
        routing=_snapshot(CLAUDE, CODEX),
        now=_NOW,
    )
    run_id = "f21-run"
    db.create_run(
        Run(
            id=run_id,
            task=submitted.job.goal,
            target_repo=str(repo),
            base_commit=submitted.job.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="pytest -q",
            status=RunStatus.RUNNING,
            created_at="2026-11-19T12:00:00+00:00",
        )
    )
    db.create_attempt(
        Attempt(
            id="f21-attempt",
            run_id=run_id,
            attempt_number=1,
            provider="anthropic",
            model="claude-sonnet-4-6",
            worktree_path=str(tmp_path / "worktree"),
            branch_name=f"orch/{run_id}/a1",
            status=AttemptStatus.RUNNING,
            created_at="2026-11-19T12:01:00+00:00",
        )
    )
    job = Job(
        job_id=submitted.job.job_id,
        idempotency_key=None,
        caller_id="orchestrator",
        repo=submitted.job.repo,
        base_ref=submitted.job.base_ref,
        base_sha=submitted.job.base_sha,
        goal=submitted.job.goal,
        acceptance=None,
        requirements=submitted.job.requirements,
        state=JobState.RUNNING,
        current_run_id=run_id,
        run_ids=(run_id,),
        created_at="2026-11-19T12:00:00+00:00",
        updated_at="2026-11-19T12:01:00+00:00",
        request_digest="digest",
    )

    result = _job_result(db, job)
    assert result["worker"] == {
        "provider": "anthropic",
        "model": "claude-sonnet-4-6",
        "route": "anthropic/claude-sonnet-4-6",
    }


# ---------------------------------------------------------------------------
# the documentation stays honest
# ---------------------------------------------------------------------------


def _supported_paragraph(path: Path, anchor: str) -> str:
    text = path.read_text(encoding="utf-8")
    marker = text.index(anchor)
    end = text.find("\n\n", marker)
    return text[marker : end if end != -1 else len(text)]


def test_docs_list_only_the_three_supported_tokens() -> None:
    root = Path(__file__).resolve().parents[1]
    cases = {
        root / "docs" / "mcp.md": "Three family tokens are supported",
        root / "docs" / "operations.md": "Three tokens are supported",
    }
    for path, anchor in cases.items():
        paragraph = _supported_paragraph(path, anchor)
        listed = set()
        for token in FAMILY_TOKENS + AMBIGUOUS_FAMILY_TOKENS:
            if f"`{token}`" in paragraph:
                listed.add(token)
        assert listed == set(FAMILY_TOKENS), (path.name, listed)


def test_guide_names_supported_and_ambiguous_tokens() -> None:
    for token in FAMILY_TOKENS:
        assert f"`{token}`" in GUIDE
    flat = " ".join(GUIDE.split())
    assert "`claude` and `opencode` are ambiguous and are not supported tokens" in flat
    assert "NO_ROUTE_FOR_REQUIREMENTS" in flat
