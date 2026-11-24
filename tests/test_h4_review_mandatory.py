"""H4: review is mandatory, focused and able to read the candidate snapshot.

Hermetic: no real vendor CLI, no network, no owner store.  The tests cover

* a scheduler job's frozen run config requires review (the H0 regression:
  a docs-only delta under the default policy was accepted unreviewed);
* acceptance refuses a candidate with no candidate-linked review;
* the reviewer prompt carries ``review_focus`` and the worker's own report;
* the diff is bounded, the changed-path manifest stays complete, and a file
  omitted from the diff is still found by reading it from the snapshot;
* a reviewer write attempt is still detected and blocks.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops.accept import AcceptEngine
from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.db import Database
from saberops.jobs.models import Job, JobRequirements, JobState, WaitingReason
from saberops.jobs.service import job_dispatch_spec
from saberops.models import (
    Attempt,
    AttemptStatus,
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.review import REVIEW_DIFF_BUDGET_BYTES, ReviewEngine
from saberops.run_creation import resolve_effective_run_config
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

_CREATED = "2026-09-25T00:00:00+00:00"
_PROVIDER = "opencode"
_WORKER_MODEL = "h4-worker"
_REVIEWER_MODEL = "h4-reviewer"
_CONNECTION = "h4-conn"


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    monkeypatch.setenv("ORCH_BINDING_STORE", str(tmp_path / "bindings.json"))
    monkeypatch.setenv("ORCH_ACCESS_STORE", str(tmp_path / "access.json"))
    monkeypatch.setenv("ORCH_ACCESS_MODELS_STORE", str(tmp_path / "models.json"))
    monkeypatch.setenv("ORCH_READINESS_STORE", str(tmp_path / "readiness.json"))
    return tmp_path


def _seed_posture(config_home: Path) -> None:
    cfg_dir = config_home / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(UTC).date().isoformat()
    doc = {
        "version": 2,
        "providers": {_PROVIDER: {"state": "ALLOWED"}},
        "quota_pools": {},
        "postures": {
            _PROVIDER: {
                "class": "A",
                "policy_urls": ["https://example.com/h4-test-posture"],
                "ttl_days": 90,
                "verified_on": today,
                "note": "synthetic verified test posture",
            }
        },
    }
    (cfg_dir / "control_plane.json").write_text(json.dumps(doc), encoding="utf-8")


@pytest.fixture()
def posture(isolated: Path) -> Path:
    _seed_posture(isolated / "config")
    return isolated


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _repo_with_candidate(path: Path, *, big_file: bool) -> tuple[Path, str, str]:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.email", "h4@example.com")
    _git(path, "config", "user.name", "H4")
    (path / "Makefile").write_text("gate:\n\t@echo ok\n", encoding="utf-8")
    (path / "app.py").write_text("def greet() -> str:\n    return 'hello'\n", encoding="utf-8")
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "initial")
    base = _git(path, "rev-parse", "HEAD")
    (path / "app.py").write_text("def greet() -> str:\n    return 'hi'\n", encoding="utf-8")
    if big_file:
        # A second file whose diff does not fit the (test-shrunk) budget.
        (path / "omitted.py").write_text(
            "VALUE = 'BAD: this file hides a blocking change'\n", encoding="utf-8"
        )
    _git(path, "add", "-A")
    _git(path, "commit", "-m", "candidate")
    return path, base, _git(path, "rev-parse", "HEAD")


def _seed_run(
    db: Database,
    repo: Path,
    base: str,
    candidate: str,
    *,
    review_focus: str | None = None,
    worker_report: str = "",
    task: str = "h4 review prompt probe",
) -> str:
    from saberops.control_plane.policy import (
        freeze_control_plane_policy,
        load_control_plane_policy,
    )

    frozen = freeze_control_plane_policy(load_control_plane_policy())
    run_id = "h4-review"
    config_json: str | None = None
    if review_focus:
        config_json = json.dumps({"review_focus": review_focus, "task": "t"})
    db.create_run(
        Run(
            id=run_id,
            task=task,
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at=_CREATED,
            config_json=config_json,
            control_plane_snapshot_json=frozen.as_json(),
            control_plane_snapshot_digest=frozen.digest,
        )
    )
    db.create_attempt(
        Attempt(
            id=f"{run_id}-a1",
            run_id=run_id,
            attempt_number=1,
            provider=_PROVIDER,
            model=_WORKER_MODEL,
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            worker_stdout=worker_report,
            created_at=_CREATED,
        )
    )
    return run_id


def _reviewer_binding() -> tuple[str, OwnerBindings]:
    binding, profile, pool = materialize_discovered_binding(
        connection_id=_CONNECTION,
        provider=_PROVIDER,
        model=_REVIEWER_MODEL,
        binding_role=BindingRole.CHECK,
    )
    owner = upsert_owner_binding(OwnerBindings(), binding=binding, profile=profile, pool=pool)
    save_owner_bindings(owner)
    return binding.binding_id, owner


class _PromptInspectingAdapter(WorkerAdapter):
    """Reviewer stand-in that may read the snapshot and optionally write."""

    def __init__(self, *, write: bool = False) -> None:
        self.prompts: list[str] = []
        self.write = write

    @property
    def provider_name(self) -> str:
        return _PROVIDER

    def is_available(self) -> bool:
        return True

    def max_prompt_bytes(self) -> int | None:
        return None

    def run(self, request: WorkerRequest) -> WorkerResult:
        assert request.read_only is True
        self.prompts.append(request.task)
        if self.write:
            (request.worktree_path / "app.py").write_text("tampered\n", encoding="utf-8")
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.SUCCESS,
                summary="Wrote to the snapshot",
                stdout="VERDICT: PASS\n",
                stderr="",
                changed_paths=["app.py"],
                has_changes=True,
            )
        omitted = request.worktree_path / "omitted.py"
        if omitted.is_file() and "BAD" in omitted.read_text(encoding="utf-8"):
            return WorkerResult(
                provider=self.provider_name,
                model=request.model,
                status=AttemptStatus.SUCCESS,
                summary="Found a blocking change outside the diff",
                stdout="VERDICT: BLOCK\nSUMMARY: omitted.py hides a blocking change\n",
                stderr="",
                changed_paths=[],
                has_changes=False,
            )
        return WorkerResult(
            provider=self.provider_name,
            model=request.model,
            status=AttemptStatus.SUCCESS,
            summary="Reviewed",
            stdout="VERDICT: PASS\n",
            stderr="",
            changed_paths=[],
            has_changes=False,
        )


def _engine(db: Database, adapter: WorkerAdapter) -> ReviewEngine:
    return ReviewEngine(db=db, registry=AdapterRegistry([adapter]), readiness_service=None)


# ---------------------------------------------------------------------------
# Mandatory review on the scheduler path
# ---------------------------------------------------------------------------


def _scheduler_job(repo: Path) -> Job:
    return Job(
        job_id="h4-job",
        idempotency_key=None,
        caller_id="owner",
        repo=str(repo),
        base_ref="HEAD",
        base_sha="0" * 40,
        goal="update the docs",
        acceptance=None,
        requirements=JobRequirements(),
        state=JobState.QUEUED,
        waiting_reason=WaitingReason.NEW,
        created_at=_CREATED,
        updated_at=_CREATED,
        request_digest="h4-digest",
    )


def test_scheduler_job_dispatch_requires_review(
    isolated: Path,
) -> None:
    """The H0 regression: a docs-only scheduler job was accepted unreviewed."""
    from saberops.project_settings import set_project_gate_command

    repo, _base, _candidate = _repo_with_candidate(isolated / "sched", big_file=False)
    set_project_gate_command(repo, "true")
    spec = job_dispatch_spec(_scheduler_job(repo))
    assert spec.request.review_enabled is True
    config = resolve_effective_run_config(spec.request)
    assert config.review_enabled is True
    assert config.review_disabled is False


def test_docs_only_scheduler_candidate_needs_a_review_to_be_accepted(
    isolated: Path,
) -> None:
    """Without a candidate-linked review the default-policy job cannot be accepted."""
    repo, base, candidate = _repo_with_candidate(isolated / "docs", big_file=False)
    db = Database(isolated / "docs.db")
    db.create_run(
        Run(
            id="h4-docs",
            task="update the docs",
            target_repo=str(repo),
            base_commit=base,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.COMPLETED,
            created_at=_CREATED,
            completed_at=_CREATED,
            # The scheduler's frozen config before H4: no review bit at all.
            config_json=json.dumps({"review_enabled": False}),
        )
    )
    db.create_attempt(
        Attempt(
            id="h4-docs-a1",
            run_id="h4-docs",
            attempt_number=1,
            provider=_PROVIDER,
            model=_WORKER_MODEL,
            worktree_path=str(repo),
            branch_name="main",
            status=AttemptStatus.SUCCESS,
            commit_sha=candidate,
            created_at=_CREATED,
        )
    )
    assert AcceptEngine(db).check_accept_eligibility("h4-docs").eligible is False
    assert AcceptEngine(db)._resolve_review_required_from_persisted_run("h4-docs") is True


# ---------------------------------------------------------------------------
# Reviewer prompt: focus, worker report, bounded diff, snapshot access
# ---------------------------------------------------------------------------


def test_reviewer_prompt_carries_focus_report_and_complete_manifest(
    isolated: Path,
    posture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo, base, candidate = _repo_with_candidate(isolated / "prompt", big_file=True)
    db = Database(isolated / "prompt.db")
    binding_id, _owner = _reviewer_binding()
    run_id = _seed_run(
        db,
        repo,
        base,
        candidate,
        review_focus="check the new helper for broad exception handlers",
        worker_report=(
            "SKIPPED: nothing\nNOT CHECKED: database migration\nRISK: large file"
        ),
    )
    monkeypatch.setattr("saberops.review.REVIEW_DIFF_BUDGET_BYTES", 220)
    adapter = _PromptInspectingAdapter()
    result = _engine(db, adapter).review_run(
        run_id,
        reviewer_override=WorkerCandidate(
            provider=_PROVIDER, model=_REVIEWER_MODEL, binding_id=binding_id
        ),
        review_limit=1,
    )
    assert result.verdict is ReviewVerdict.BLOCK
    prompt = adapter.prompts[-1]
    # Read-only snapshot access is explicit, and the old prohibition is gone.
    assert "read-only snapshot" in prompt
    assert "You MAY read any file" in prompt
    assert "NO filesystem access" not in prompt
    # Planner focus and the worker's own report both reach the reviewer.
    assert "check the new helper for broad exception handlers" in prompt
    assert "SKIPPED: nothing" in prompt
    assert "RISK: large file" in prompt
    # The manifest is complete even when the diff body is bounded.
    assert "Changed paths (complete manifest; 2 file(s))" in prompt
    assert "- app.py" in prompt
    assert "- omitted.py (diff omitted below; read it from the snapshot)" in prompt
    assert "partial:" in prompt
    assert REVIEW_DIFF_BUDGET_BYTES > 220  # the production budget was not changed
    truncated = db.get_events_by_type(run_id, "review_diff_truncated")
    assert truncated
    payload = truncated[0].payload
    assert payload is not None and payload["omitted_paths"] == ["omitted.py"]


def test_reviewer_write_attempt_is_still_detected(
    isolated: Path,
    posture: Path,
) -> None:
    repo, base, candidate = _repo_with_candidate(isolated / "write", big_file=False)
    db = Database(isolated / "write.db")
    binding_id, _owner = _reviewer_binding()
    run_id = _seed_run(db, repo, base, candidate)
    adapter = _PromptInspectingAdapter(write=True)
    result = _engine(db, adapter).review_run(
        run_id,
        reviewer_override=WorkerCandidate(
            provider=_PROVIDER, model=_REVIEWER_MODEL, binding_id=binding_id
        ),
        review_limit=1,
    )
    assert result.verdict is ReviewVerdict.BLOCK
    assert "read-only policy" in result.summary
    assert db.get_events_by_type(run_id, "review_snapshot_mutated")


def test_bounded_diff_omits_whole_files_never_half_a_hunk() -> None:
    """The budget applies at file boundaries, not inside a hunk."""
    from saberops.review import _bounded_review_diff

    diff = (
        "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-x\n+y\n"
        "diff --git a/b.py b/b.py\n--- a/b.py\n+++ b/b.py\n@@ -1 +1 @@\n-x\n+z\n"
    )
    text, omitted = _bounded_review_diff(diff)
    assert omitted == ()
    assert text.count("diff --git") == 2


def test_review_decision_records_are_legacy_readable() -> None:
    from saberops.review_adaptive import ReviewDecision

    legacy = json.dumps(
        {
            "schema_version": "review-adaptive/1",
            "decision_id": "old",
            "review_policy_mode": "ALWAYS_REQUIRED",
            "owner_explicit_review": False,
            "is_autonomous": True,
            "is_high_t3": False,
            "independent_review_required": True,
            "review_requirement_source": "AUTONOMOUS_SAFETY",
            "review_skip_reason": "",
            "classification": {"change_class": "ORDINARY_PRODUCTION"},
        }
    )
    decision = ReviewDecision.from_json(legacy)
    assert decision.independent_review_required is True
    assert decision.review_requirement_source.value == "MANDATORY"


def test_job_review_focus_reaches_the_frozen_run(
    isolated: Path,
) -> None:
    from saberops.project_settings import set_project_gate_command

    repo, _base, _candidate = _repo_with_candidate(isolated / "focus-job", big_file=False)
    set_project_gate_command(repo, "true")
    job = replace(
        _scheduler_job(repo),
        requirements=JobRequirements(review_focus="  check the retry loop  "),
    )
    spec = job_dispatch_spec(job)
    assert spec.request.review_focus == "check the retry loop"
    config = resolve_effective_run_config(spec.request)
    assert config.review_focus == "check the retry loop"
    assert config.review_enabled is True


def test_plan_lane_review_focus_is_parsed_and_carried() -> None:
    from saberops.plan_file import parse_plan_file

    document = """# SaberOps plan: focus-probe

```plan
{"format": "saberops-plan/1", "plan_id": "focus-probe"}
```

## Wave 0

### P1. Focus probe

- **Why:** probe.
- **Do:** change one file.
- **Out of scope:** none.
- **Done when:**
  - the change is reviewed
- **Needs:** none

```plan
{"id": "P1", "wave": "Wave 0", "depends_on": [], "owns": [], "run": "job",
 "review_focus": "check the migration guard"}
```
"""
    parsed = parse_plan_file(document)
    assert parsed.lanes[0].review_focus == "check the migration guard"
    task = parsed.to_tasks()[0]
    assert task.review_focus == "check the migration guard"
    from saberops.plan_runner import _lane_requirements as lane_requirements

    assert lane_requirements(task).review_focus == "check the migration guard"


def test_unavailable_clause_evidence_blocks_the_review(
    isolated: Path,
    posture: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clause review without engine evidence must block, never pass."""
    repo, base, candidate = _repo_with_candidate(isolated / "evidence", big_file=False)
    db = Database(isolated / "evidence.db")
    binding_id, _owner = _reviewer_binding()
    run_id = _seed_run(
        db,
        repo,
        base,
        candidate,
        task="Add a flag.\n\nAcceptance:\nthe flag defaults to off.",
    )

    def _broken_evidence(**_: object) -> str:
        raise RuntimeError("git grep failed")

    monkeypatch.setattr("saberops.review.build_review_evidence", _broken_evidence)
    adapter = _PromptInspectingAdapter()
    with pytest.raises(ValueError, match="review evidence unavailable"):
        _engine(db, adapter).review_run(
            run_id,
            reviewer_override=WorkerCandidate(
                provider=_PROVIDER, model=_REVIEWER_MODEL, binding_id=binding_id
            ),
            review_limit=1,
        )
    assert adapter.prompts == []


def test_worker_instructions_end_with_the_lines_the_reviewer_reads(
    isolated: Path,
    posture: Path,
) -> None:
    """H12: the worker report's last lines are the reviewer's risk evidence."""
    from saberops.validation_policy import WORKER_INSTRUCTIONS

    repo, base, candidate = _repo_with_candidate(isolated / "report", big_file=False)
    db = Database(isolated / "report.db")
    binding_id, _owner = _reviewer_binding()
    run_id = _seed_run(
        db,
        repo,
        base,
        candidate,
        worker_report=(
            "Implemented the change.\n"
            "SKIPPED: nothing\n"
            "NOT CHECKED: the database migration\n"
            "RISKS: broad exception handler at app.py:12"
        ),
    )
    adapter = _PromptInspectingAdapter()
    _engine(db, adapter).review_run(
        run_id,
        reviewer_override=WorkerCandidate(
            provider=_PROVIDER, model=_REVIEWER_MODEL, binding_id=binding_id
        ),
        review_limit=1,
    )
    prompt = adapter.prompts[-1]
    for line in (
        "SKIPPED: nothing",
        "NOT CHECKED: the database migration",
        "RISKS: broad exception handler at app.py:12",
    ):
        assert line in prompt, line
    flat = " ".join(WORKER_INSTRUCTIONS.split())
    assert "what you skipped" in flat
    assert "what you did not check" in flat
    assert "the risks" in flat
