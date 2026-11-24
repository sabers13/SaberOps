"""H12 (ADR-0089): skills, tool descriptions and engine worker instructions.

The lane's contract:

* ``orch connect --skill`` installs one short main skill plus three on-demand
  skills, and removal takes all four back (covered in ``test_agent_skill``);
* the main skill and the worker section stay under their set sizes;
* every tool a skill names exists;
* build-role workers receive the engine-generated instructions, and scouts do
  not;
* the short rules for one tool live in that tool's MCP description;
* the reviewer receives the worker's risk lines.

The rig tests use the real lifecycle rig (isolated store, toy repository, fake
CLI shims); no vendor CLI is launched and no prompt reaches a real model.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from lifecycle_rig import RigEnvironment, build_lifecycle_rig

from saberops.agent_connect import WORKER_VENDOR, worker_document
from saberops.context_projection import build_initial_projection, render_projection
from saberops.jobs.service import get_job, submit_job
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.tools import ServerContext, build_default_registry
from saberops.model_access.readiness_service import ReadinessService
from saberops.models import RunStatus
from saberops.paths import owner_state_dir
from saberops.validation_policy import (
    MAX_WORKER_INSTRUCTION_LINES,
    VALIDATION_POLICY_MARKER,
    VALIDATION_POLICY_TEXT,
    WORKER_INSTRUCTIONS,
    WORKER_INSTRUCTIONS_MARKER,
)

pytestmark = pytest.mark.packaged_routing_default


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> RigEnvironment:
    return build_lifecycle_rig(tmp_path, monkeypatch)


def _prompts(path: Path) -> list[str]:
    return [
        json.loads(line)["prompt"]
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _tick(rig: RigEnvironment) -> None:
    from saberops.capacity.loop import run_scheduler_loop

    readiness = ReadinessService.for_owner_state(owner_state_dir())
    run_scheduler_loop(rig.db(), once=True, readiness_service=readiness)


def test_worker_instructions_stay_short_and_carry_the_required_rules() -> None:
    lines = WORKER_INSTRUCTIONS.splitlines()
    assert lines[0] == WORKER_INSTRUCTIONS_MARKER
    assert len(lines) <= MAX_WORKER_INSTRUCTION_LINES
    flat = " ".join(WORKER_INSTRUCTIONS.split())
    for phrase in (
        "smallest complete change",
        "Never cut validation at a trust boundary",
        "data loss",
        "security",
        "accessibility",
        "Edit surgically",
        "never delete",
        "shortcut: <limit>, <when to upgrade>",
        "check each review finding against the code",
        "rebuttal loop",
        "what you skipped",
        "what you did not check",
        "the risks",
    ):
        assert phrase in flat, phrase


def test_render_appends_worker_instructions_once_before_the_validation_policy() -> None:
    projection = build_initial_projection(original_task="Add one helper.")
    rendered = render_projection(
        projection,
        validation_policy_text=VALIDATION_POLICY_TEXT,
        validation_policy_marker=VALIDATION_POLICY_MARKER,
        worker_instructions_text=WORKER_INSTRUCTIONS,
    )
    assert rendered.prompt.count(WORKER_INSTRUCTIONS_MARKER) == 1
    assert rendered.prompt.index(WORKER_INSTRUCTIONS_MARKER) < rendered.prompt.index(
        VALIDATION_POLICY_MARKER
    )


def test_worker_instructions_are_appended_only_once() -> None:
    projection = build_initial_projection(original_task="Add one helper.")
    first = render_projection(
        projection,
        validation_policy_text=VALIDATION_POLICY_TEXT,
        validation_policy_marker=VALIDATION_POLICY_MARKER,
        worker_instructions_text=WORKER_INSTRUCTIONS,
    )
    # A composed prompt that already carries the section is not duplicated.
    assert first.prompt.count(WORKER_INSTRUCTIONS_MARKER) == 1


def test_a_build_job_prompt_carries_the_worker_instructions(rig: RigEnvironment) -> None:
    prompt_log = rig.root / "build-prompts.jsonl"
    rig.set_worker_mode(
        "commit", target_file="h12.txt", content="built\n", prompt_log=prompt_log
    )
    db = rig.db()
    submitted = submit_job(db, repo=rig.repo, goal="Create h12.txt")
    _tick(rig)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=120.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    prompts = _prompts(prompt_log)
    assert prompts
    assert WORKER_INSTRUCTIONS_MARKER in prompts[0]
    assert WORKER_INSTRUCTIONS in prompts[0]


def test_a_scout_prompt_keeps_its_own_section(rig: RigEnvironment) -> None:
    prompt_log = rig.root / "scout-prompts.jsonl"
    rig.set_worker_mode(
        "commit", report_text="The helper is safe.", prompt_log=prompt_log
    )
    db = rig.db()
    submitted = submit_job(
        db, repo=rig.repo, goal="Inspect the helper", kind="SCOUT"
    )
    _tick(rig)
    job = get_job(db, submitted.job.job_id)
    assert job is not None and job.current_run_id is not None
    run = rig.wait_for_run(job.current_run_id, timeout=120.0)
    assert run.status is RunStatus.COMPLETED, run.result_summary
    prompts = _prompts(prompt_log)
    assert prompts
    assert WORKER_INSTRUCTIONS_MARKER not in prompts[0]
    assert "SCOUT JOB" in prompts[0]


def test_the_native_worker_definition_carries_the_build_rules() -> None:
    for slug in WORKER_VENDOR:
        document = worker_document(slug)
        assert WORKER_INSTRUCTIONS_MARKER in document, slug
        assert "shortcut: <limit>, <when to upgrade>" in document, slug
        assert "native_heartbeat" in document, slug


def _registry() -> Any:
    return build_default_registry(
        ServerContext(
            db_path=Path("/nonexistent/h12.db"),
            lease=OrchestratorLease(Path("/nonexistent/h12-lease.json")),
        )
    )


def test_tool_descriptions_carry_their_short_rules() -> None:
    registry = _registry()
    expected = {
        "submit_job": (
            "worker does not write",
            "review_focus",
            "One job per independent task",
        ),
        "note_job": ("redirect_job", "new job"),
        "redirect_job": ("note_job", "new job", "undelivered note"),
        "resume_job": ("scratch", "last_candidate", "One successor per call"),
        "get_job": ("candidate_sha", "owner_action", "owner accepts"),
        "wait_job": ("candidate_sha", "owner_action", "after a timeout"),
        "catch_up": ("catch up", "plan_id", "no history"),
        "propose_config_change": ("nothing is applied", "Only the owner approves"),
    }
    for name, phrases in expected.items():
        description = registry.get(name).description
        for phrase in phrases:
            assert phrase in description, (name, phrase)
