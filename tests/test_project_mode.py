"""Hermetic tests for the six project entry modes (Step 026 / PM-1).

Every test builds its own temporary repository, plan store and registry. No
network, no provider call, no database, no clock-dependent assertion: the only
external tools are the local ``git`` executable and the filesystem under
``tmp_path``.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from saberops.project import ProjectRegistry
from saberops.project_mode import (
    GATE_DISABLED_WARNING,
    MODE_FORBIDDEN,
    MODE_REQUIREMENTS,
    ROADMAP_FILENAME,
    ROADMAP_HEADER,
    ActivationReport,
    BlockerCode,
    ContinueSummary,
    EvidenceClaim,
    FactKey,
    ModeActivation,
    ModeAuthorityError,
    ModeConfirmationError,
    ModeRecommendation,
    ModeRecord,
    ModeRecordError,
    ModeSelector,
    ModeValidationError,
    OpenBacklogItem,
    ProjectEntryMode,
    ProjectModeFacts,
    RecentRun,
    RepositoryFacts,
    StartupDigest,
    TaskGatePolicy,
    allowed_modes,
    build_continue_summary,
    check_activation,
    export_roadmap_markdown,
    extract_project_mode_facts,
    extract_repository_facts,
    load_mode_record,
    mode_qualified,
    mode_record_path,
    next_roadmap_item,
    preflight,
    reclassify_mode,
    recommend_mode,
    render_roadmap_markdown,
    roadmap_export_matches,
    save_mode_record,
    validate_recommendation,
    write_roadmap_export,
)
from saberops.project_scope.plan_store import PlanRevision, PlanStore, PlanTaskDef

CREATED_AT = "2026-10-18T00:00:00+00:00"


@pytest.fixture(autouse=True)
def _isolated_owner_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep every default owner-state path inside this test's ``tmp_path``."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "xdg-config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg-cache"))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)


def _git(repo: Path, *args: str) -> None:
    result = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed: {result.stderr}")


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "test@example.invalid")
    _git(path, "config", "user.name", "Test Owner")


def _commit_all(repo: Path, message: str = "init") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _add_gate_script(repo: Path) -> None:
    script = repo / "scripts" / "gate-full.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("#!/usr/bin/env bash\nset -euo pipefail\n", encoding="utf-8")
    script.chmod(0o755)


def _committed_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "README.md").write_text("# project\n", encoding="utf-8")
    _commit_all(repo)
    return repo


def _plan_store_with_active_plan(
    tmp_path: Path, project_id: str = "proj-a"
) -> tuple[PlanStore, PlanRevision]:
    store = PlanStore(tmp_path / "plan-store")
    revision = store.save_revision(
        project_id,
        [
            PlanTaskDef(task_id="t1", title="Set up the project", phase="1"),
            PlanTaskDef(
                task_id="t2",
                title="Build the feature",
                phase="2",
                start_dependencies=("t1",),
            ),
        ],
        version="v1",
        created_at=CREATED_AT,
    )
    store.set_active(project_id, "v1")
    return store, revision


def _registry_with_project(
    tmp_path: Path, repo: Path, project_id: str = "proj-a"
) -> ProjectRegistry:
    registry = ProjectRegistry(tmp_path / "registry.json")
    registry.register(
        project_id=project_id,
        display_name="Project A",
        repository_root=repo,
        state_root=tmp_path / "state",
    )
    return registry


# --------------------------------------------------------------------------- #
# Repository and state fact extraction
# --------------------------------------------------------------------------- #


def test_extract_repository_facts_reports_an_empty_directory(tmp_path: Path) -> None:
    """Covers: prompt 2 "Detect repository facts" on a directory with no history."""
    facts = extract_repository_facts(tmp_path)
    assert facts.is_git_repository is False
    assert facts.has_commits is False
    assert facts.head_sha is None
    assert facts.gate_scripts == ()
    assert facts.orch_state_present is False
    assert facts.working_tree_dirty is False
    assert facts.git_hooks == ()
    assert facts.roadmap_present is False


def test_extract_repository_facts_handles_a_missing_path(tmp_path: Path) -> None:
    """Covers: prompt 2 fact extraction fails closed on an absent path."""
    facts = extract_repository_facts(tmp_path / "does-not-exist")
    assert facts.is_git_repository is False
    assert facts.has_commits is False


def test_extract_repository_facts_observes_a_committed_repository(tmp_path: Path) -> None:
    """Covers: prompt 2 git repo, commits, gate scripts, .orch state, hooks, roadmap."""
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    (repo / ".orch").mkdir()
    (repo / ".orch" / ".gitkeep").write_text("", encoding="utf-8")
    (repo / "ROADMAP.md").write_text("# ROADMAP\n", encoding="utf-8")
    _commit_all(repo, "add gate, roadmap and orchestration state")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\n", encoding="utf-8")
    hook.chmod(0o755)

    facts = extract_repository_facts(repo)

    assert facts.is_git_repository is True
    assert facts.has_commits is True
    assert facts.head_sha is not None and len(facts.head_sha) == 40
    assert facts.gate_scripts == ("scripts/gate-full.sh",)
    assert facts.orch_state_present is True
    assert facts.working_tree_dirty is False
    assert facts.git_hooks == ("pre-commit",)
    assert facts.roadmap_present is True


def test_extract_repository_facts_detects_a_dirty_working_tree(tmp_path: Path) -> None:
    """Covers: prompt 2 dirty working tree detection."""
    repo = _committed_repo(tmp_path)
    (repo / "uncommitted.txt").write_text("draft\n", encoding="utf-8")
    facts = extract_repository_facts(repo)
    assert facts.working_tree_dirty is True


def test_extract_repository_facts_detects_a_makefile_gate_target(tmp_path: Path) -> None:
    """Covers: prompt 2 gate script presence includes a Makefile gate target."""
    repo = _committed_repo(tmp_path)
    (repo / "Makefile").write_text("gate:\n\tpytest -q\n", encoding="utf-8")
    _commit_all(repo, "add gate")
    facts = extract_repository_facts(repo)
    assert facts.gate_scripts == ("Makefile",)


def test_extract_project_mode_facts_merges_registry_and_plan_store(tmp_path: Path) -> None:
    """Covers: prompt 2 facts include the SaberOps state that separates Continue."""
    repo = _committed_repo(tmp_path)
    store, _ = _plan_store_with_active_plan(tmp_path)
    registry = _registry_with_project(tmp_path, repo)

    facts = extract_project_mode_facts(repo, plan_store=store, registry=registry)

    assert facts.repository.is_git_repository is True
    assert facts.state.registered_project_ids == ("proj-a",)
    assert facts.state.plan_versions == ("v1",)
    assert facts.state.active_plan_version == "v1"
    assert facts.claim(FactKey.REGISTERED_IN_SABEROPS) is True
    assert facts.claim(FactKey.ACTIVE_PLAN_PRESENT) is True


def test_extract_project_mode_facts_ignores_other_repositories(tmp_path: Path) -> None:
    """Covers: prompt 2 registration is matched by canonical repository path."""
    repo = _committed_repo(tmp_path)
    other = tmp_path / "other"
    _init_repo(other)
    (other / "README.md").write_text("# other\n", encoding="utf-8")
    _commit_all(other)
    registry = _registry_with_project(tmp_path, other, project_id="other-project")

    facts = extract_project_mode_facts(repo, registry=registry)

    assert facts.state.registered_project_ids == ()
    assert facts.state.active_plan_version is None


# --------------------------------------------------------------------------- #
# Mode qualification
# --------------------------------------------------------------------------- #


def test_new_project_is_the_only_writing_mode_for_an_empty_directory(
    tmp_path: Path,
) -> None:
    """Covers: prompt 1 NEW_PROJECT entered from a directory with no history."""
    facts = extract_project_mode_facts(tmp_path)
    modes = allowed_modes(facts)

    assert ProjectEntryMode.NEW_PROJECT in modes
    assert ProjectEntryMode.INSPECT in modes
    assert ProjectEntryMode.TASK not in modes
    assert ProjectEntryMode.DEVELOP_EXISTING not in modes
    assert ProjectEntryMode.CONTINUE_SABEROPS not in modes
    assert ProjectEntryMode.RESUME_RUN not in modes
    assert recommend_mode(facts).mode is ProjectEntryMode.NEW_PROJECT


def test_new_project_is_refused_once_commits_exist(tmp_path: Path) -> None:
    """Covers: prompt 1/2 NEW_PROJECT is disqualified by existing history."""
    repo = _committed_repo(tmp_path)
    facts = extract_project_mode_facts(repo)

    assert mode_qualified(facts, ProjectEntryMode.NEW_PROJECT) is False
    assert mode_qualified(facts, ProjectEntryMode.TASK) is True


def test_task_mode_is_entered_from_a_committed_repository(tmp_path: Path) -> None:
    """Covers: prompt 1 TASK reached from a repository with one commit."""
    repo = _committed_repo(tmp_path)
    facts = extract_project_mode_facts(repo)

    assert mode_qualified(facts, ProjectEntryMode.TASK) is True
    assert recommend_mode(facts).mode is ProjectEntryMode.TASK
    assert preflight(ProjectEntryMode.TASK, facts).ok is False
    report = preflight(ProjectEntryMode.TASK, facts, gate_required=False)
    assert report.ok is True


def test_develop_existing_requires_a_gate_and_wins_over_task(tmp_path: Path) -> None:
    """Covers: prompt 1 DEVELOP_EXISTING needs a gate check and a project map."""
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    _commit_all(repo, "add gate")
    facts = extract_project_mode_facts(repo)

    assert mode_qualified(facts, ProjectEntryMode.DEVELOP_EXISTING) is True
    report = preflight(ProjectEntryMode.DEVELOP_EXISTING, facts)
    assert report.ok is True
    recommendation = recommend_mode(facts)
    assert recommendation.mode is ProjectEntryMode.DEVELOP_EXISTING
    assert recommendation.runner_up is ProjectEntryMode.TASK


def test_continue_saberops_requires_registration_and_an_active_plan(
    tmp_path: Path,
) -> None:
    """Covers: prompt 1 CONTINUE_SABEROPS loaded from the owner state."""
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    _commit_all(repo, "add gate")
    store, revision = _plan_store_with_active_plan(tmp_path)
    registry = _registry_with_project(tmp_path, repo)

    facts = extract_project_mode_facts(repo, plan_store=store, registry=registry)
    recommendation = recommend_mode(facts)
    assert recommendation.mode is ProjectEntryMode.CONTINUE_SABEROPS
    assert preflight(ProjectEntryMode.CONTINUE_SABEROPS, facts).ok is True

    digest = StartupDigest(
        recent_runs=(RecentRun(run_id="run_1", status="completed", accepted=True),),
        open_backlog=(OpenBacklogItem(title="Improve the export", detail="docs"),),
    )
    summary = build_continue_summary(facts, revision, digest)
    assert summary.project_id == "proj-a"
    assert summary.plan_version == "v1"
    assert summary.next_item is not None and summary.next_item.task_id == "t1"
    rendered = summary.render()
    assert "Continue SaberOps project proj-a" in rendered
    assert "Next roadmap item: t1 [1] Set up the project" in rendered
    assert "- run_1: accepted" in rendered
    assert "- Improve the export — docs" in rendered


def test_continue_saberops_is_refused_without_registration(tmp_path: Path) -> None:
    """Covers: prompt 2 registration narrows CONTINUE_SABEROPS out of the set."""
    repo = _committed_repo(tmp_path)
    store, _ = _plan_store_with_active_plan(tmp_path)
    facts = extract_project_mode_facts(repo, plan_store=store)

    assert mode_qualified(facts, ProjectEntryMode.CONTINUE_SABEROPS) is False
    report = preflight(ProjectEntryMode.CONTINUE_SABEROPS, facts)
    codes = {blocker.code for blocker in report.blockers}
    assert BlockerCode.NOT_REGISTERED in codes
    assert BlockerCode.NO_ACTIVE_PLAN in codes


def test_resume_run_requires_a_resumable_run_and_a_clean_tree(tmp_path: Path) -> None:
    """Covers: prompt 1 RESUME_RUN needs an exact checkpoint and a clean tree."""
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    _commit_all(repo, "add gate")
    facts = extract_project_mode_facts(repo, resumable_run_ids=("run_7",))

    assert mode_qualified(facts, ProjectEntryMode.RESUME_RUN) is True
    assert preflight(ProjectEntryMode.RESUME_RUN, facts).ok is True

    (repo / "dirty.txt").write_text("draft\n", encoding="utf-8")
    dirty_facts = extract_project_mode_facts(repo, resumable_run_ids=("run_7",))
    assert mode_qualified(dirty_facts, ProjectEntryMode.RESUME_RUN) is False
    report = preflight(ProjectEntryMode.RESUME_RUN, dirty_facts)
    assert {blocker.code for blocker in report.blockers} == {
        BlockerCode.DIRTY_WORKING_TREE
    }


def test_an_unregistered_gate_repository_with_a_resumable_run_ties(
    tmp_path: Path,
) -> None:
    """Covers: prompt 2 ties go to the lighter mode (Resume over Develop)."""
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    _commit_all(repo, "add gate")
    facts = extract_project_mode_facts(repo, resumable_run_ids=("run_7",))

    recommendation = recommend_mode(facts)
    assert recommendation.mode is ProjectEntryMode.RESUME_RUN
    assert recommendation.runner_up is ProjectEntryMode.DEVELOP_EXISTING
    assert recommendation.confidence == pytest.approx(0.5)


def test_inspect_is_always_allowed_and_never_blocked(tmp_path: Path) -> None:
    """Covers: prompt 1 INSPECT is read-only: no candidate, no gate, no blocker."""
    for path in (tmp_path, tmp_path / "missing"):
        facts = extract_project_mode_facts(path)
        assert mode_qualified(facts, ProjectEntryMode.INSPECT) is True
        report = preflight(ProjectEntryMode.INSPECT, facts)
        assert report.ok is True
        assert report.blockers == ()


def test_mode_requirements_and_forbiddens_cover_every_mode() -> None:
    """Covers: prompt 1/2 the six-mode vocabulary is complete and explicit."""
    assert set(MODE_REQUIREMENTS) == set(ProjectEntryMode)
    assert set(MODE_FORBIDDEN) == set(ProjectEntryMode)
    assert len(ProjectEntryMode) == 6


# --------------------------------------------------------------------------- #
# Recommendation validation (fail-closed)
# --------------------------------------------------------------------------- #


def test_recommendation_rejects_a_mode_the_facts_do_not_allow(tmp_path: Path) -> None:
    """Covers: prompt 2 an invalid mode is rejected fail-closed."""
    facts = extract_project_mode_facts(tmp_path)
    recommendation = ModeRecommendation(
        mode=ProjectEntryMode.CONTINUE_SABEROPS,
        confidence=1.0,
        evidence=(),
    )
    with pytest.raises(ModeValidationError, match="not allowed by the observed"):
        validate_recommendation(recommendation, facts)


def test_recommendation_rejects_contradicted_evidence(tmp_path: Path) -> None:
    """Covers: prompt 2 evidence that mismatches the observed facts is rejected."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    recommendation = ModeRecommendation(
        mode=ProjectEntryMode.TASK,
        confidence=0.5,
        evidence=(
            EvidenceClaim(FactKey.GIT_REPOSITORY, True),
            EvidenceClaim(FactKey.COMMITS_PRESENT, False),
        ),
    )
    with pytest.raises(ModeValidationError, match="contradicts the repository"):
        validate_recommendation(recommendation, facts)


def test_recommendation_rejects_invented_and_irrelevant_evidence(tmp_path: Path) -> None:
    """Covers: prompt 2 invented evidence is rejected fail-closed."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    invented = ModeRecommendation(
        mode=ProjectEntryMode.TASK,
        confidence=1.0,
        evidence=(
            EvidenceClaim(FactKey.GIT_REPOSITORY, True),
            EvidenceClaim(FactKey.COMMITS_PRESENT, True),
            EvidenceClaim(FactKey.ROADMAP_PRESENT, True),
        ),
    )
    with pytest.raises(ModeValidationError, match="does not qualify mode"):
        validate_recommendation(invented, facts)


def test_recommendation_rejects_missing_or_duplicated_evidence(tmp_path: Path) -> None:
    """Covers: prompt 2 evidence must cite each fact at most once and not be empty."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    missing = ModeRecommendation(mode=ProjectEntryMode.TASK, confidence=0.0)
    with pytest.raises(ModeValidationError, match="needs evidence"):
        validate_recommendation(missing, facts)
    duplicated = ModeRecommendation(
        mode=ProjectEntryMode.TASK,
        confidence=1.0,
        evidence=(
            EvidenceClaim(FactKey.GIT_REPOSITORY, True),
            EvidenceClaim(FactKey.GIT_REPOSITORY, True),
            EvidenceClaim(FactKey.COMMITS_PRESENT, True),
        ),
    )
    with pytest.raises(ModeValidationError, match="duplicate evidence"):
        validate_recommendation(duplicated, facts)


def test_recommendation_rejects_confidence_above_its_evidence(tmp_path: Path) -> None:
    """Covers: prompt 2 code validates the stated confidence against the evidence."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    overconfident = ModeRecommendation(
        mode=ProjectEntryMode.TASK,
        confidence=0.9,
        evidence=(EvidenceClaim(FactKey.GIT_REPOSITORY, True),),
    )
    with pytest.raises(ModeValidationError, match="exceeds the evidence coverage"):
        validate_recommendation(overconfident, facts)

    malformed = ModeRecommendation(
        mode=ProjectEntryMode.TASK,
        confidence=2.0,
        evidence=(
            EvidenceClaim(FactKey.GIT_REPOSITORY, True),
            EvidenceClaim(FactKey.COMMITS_PRESENT, True),
        ),
    )
    with pytest.raises(ModeValidationError, match="float in"):
        validate_recommendation(malformed, facts)


def test_recommendation_rejects_an_unqualified_or_self_runner_up(tmp_path: Path) -> None:
    """Covers: prompt 2 code validates the runner-up as well as the mode."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    evidence = (
        EvidenceClaim(FactKey.GIT_REPOSITORY, True),
        EvidenceClaim(FactKey.COMMITS_PRESENT, True),
    )
    unqualified = ModeRecommendation(
        mode=ProjectEntryMode.TASK,
        confidence=1.0,
        evidence=evidence,
        runner_up=ProjectEntryMode.DEVELOP_EXISTING,
    )
    with pytest.raises(ModeValidationError, match="runner_up .* is not allowed"):
        validate_recommendation(unqualified, facts)
    self_referential = ModeRecommendation(
        mode=ProjectEntryMode.TASK,
        confidence=1.0,
        evidence=evidence,
        runner_up=ProjectEntryMode.TASK,
    )
    with pytest.raises(ModeValidationError, match="runner_up must differ"):
        validate_recommendation(self_referential, facts)


def test_evidence_free_recommendation_keeps_the_lighter_tie(tmp_path: Path) -> None:
    """Covers: prompt 2 tie-breaking: unresolved common evidence yields the lighter mode."""
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    _commit_all(repo, "add gate")
    facts = extract_project_mode_facts(repo, resumable_run_ids=("run_7",))
    tied = ModeRecommendation(
        mode=ProjectEntryMode.DEVELOP_EXISTING,
        confidence=0.6,
        evidence=(
            EvidenceClaim(FactKey.GIT_REPOSITORY, True),
            EvidenceClaim(FactKey.COMMITS_PRESENT, True),
        ),
    )
    choice = validate_recommendation(tied, facts)
    assert choice.mode is ProjectEntryMode.RESUME_RUN
    assert choice.requested_mode is ProjectEntryMode.DEVELOP_EXISTING
    assert choice.tie_broken is True
    assert choice.runner_up is ProjectEntryMode.DEVELOP_EXISTING
    assert choice.as_dict()["mode"] == "resume_run"


def test_distinguishing_evidence_keeps_the_requested_mode(tmp_path: Path) -> None:
    """Covers: prompt 2 a cited distinguishing fact resolves the tie upward safely."""
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    _commit_all(repo, "add gate")
    facts = extract_project_mode_facts(repo, resumable_run_ids=("run_7",))
    distinguished = ModeRecommendation(
        mode=ProjectEntryMode.DEVELOP_EXISTING,
        confidence=1.0,
        evidence=(
            EvidenceClaim(FactKey.GIT_REPOSITORY, True),
            EvidenceClaim(FactKey.COMMITS_PRESENT, True),
            EvidenceClaim(FactKey.GATE_SCRIPTS_PRESENT, True),
        ),
    )
    choice = validate_recommendation(distinguished, facts)
    assert choice.mode is ProjectEntryMode.DEVELOP_EXISTING
    assert choice.tie_broken is False


# --------------------------------------------------------------------------- #
# Preflight blockers
# --------------------------------------------------------------------------- #


def test_new_project_preflight_lists_every_blocker_with_a_command(
    tmp_path: Path,
) -> None:
    """Covers: prompt 2 preflight lists blockers with exact fix commands."""
    facts = extract_project_mode_facts(tmp_path)
    report = preflight(ProjectEntryMode.NEW_PROJECT, facts)
    codes = [blocker.code for blocker in report.blockers]
    assert codes == [
        BlockerCode.NOT_A_GIT_REPOSITORY,
        BlockerCode.NO_COMMITS,
        BlockerCode.NO_GATE_SCRIPTS,
        BlockerCode.MISSING_ROADMAP,
    ]
    commands = [blocker.fix_command for blocker in report.blockers]
    assert commands[0] == "git init"
    assert commands[1].startswith("git add -A && git commit")
    assert commands[2].startswith("mkdir -p scripts && printf")
    assert commands[3].startswith("printf '# ROADMAP")
    rendered = report.render()
    assert "ok: no" in rendered
    assert "fix: git init" in rendered


def test_new_project_preflight_reports_existing_history(tmp_path: Path) -> None:
    """Covers: prompt 2 a forbidden fact is reported, not swallowed."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    report = preflight(ProjectEntryMode.NEW_PROJECT, facts)
    assert BlockerCode.REPOSITORY_HAS_COMMITS in {
        blocker.code for blocker in report.blockers
    }


def test_develop_existing_preflight_reports_a_missing_gate(tmp_path: Path) -> None:
    """Covers: prompt 2 DEVELOP_EXISTING preflight names the missing gate."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    report = preflight(ProjectEntryMode.DEVELOP_EXISTING, facts)
    assert {blocker.code for blocker in report.blockers} == {
        BlockerCode.NO_GATE_SCRIPTS
    }


def test_continue_preflight_names_registration_and_plan_fixes(tmp_path: Path) -> None:
    """Covers: prompt 1/2 Continue blockers carry real fix commands."""
    repo = _committed_repo(tmp_path)
    facts = extract_project_mode_facts(repo)
    report = preflight(ProjectEntryMode.CONTINUE_SABEROPS, facts)
    fixes = {blocker.code: blocker.fix_command for blocker in report.blockers}
    assert fixes[BlockerCode.NOT_REGISTERED].startswith("orch project register")
    assert fixes[BlockerCode.NO_ACTIVE_PLAN].startswith(
        "python -c \"from saberops.project_scope.plan_store import PlanStore"
    )


def test_resume_preflight_names_the_resume_command(tmp_path: Path) -> None:
    """Covers: prompt 2 resume blocker carries the run inventory command."""
    facts = extract_project_mode_facts(_committed_repo(tmp_path))
    report = preflight(ProjectEntryMode.RESUME_RUN, facts)
    assert {blocker.code for blocker in report.blockers} == {
        BlockerCode.NO_RESUMABLE_RUN
    }
    assert report.blockers[0].fix_command == f"orch list --repo {facts.repository.repo_path}"


# --------------------------------------------------------------------------- #
# Owner confirmation, gate policy and activation
# --------------------------------------------------------------------------- #


def _gate_repo_facts(tmp_path: Path) -> ProjectModeFacts:
    repo = _committed_repo(tmp_path)
    _add_gate_script(repo)
    _commit_all(repo, "add gate")
    return extract_project_mode_facts(repo)


def test_state_writing_modes_require_explicit_owner_confirmation(
    tmp_path: Path,
) -> None:
    """Covers: prompt 3 every roadmap/state-writing mode needs owner confirmation."""
    facts = _gate_repo_facts(tmp_path)
    with pytest.raises(ModeConfirmationError, match="requires explicit owner confirmation"):
        check_activation(ModeActivation(mode=ProjectEntryMode.TASK), facts)
    confirmed = check_activation(
        ModeActivation(mode=ProjectEntryMode.TASK, confirmed_by_owner=True), facts
    )
    assert isinstance(confirmed, ActivationReport)
    assert confirmed.allowed is True


def test_inspect_needs_no_owner_confirmation(tmp_path: Path) -> None:
    """Covers: prompt 1 INSPECT creates no candidate, so it needs no confirmation."""
    facts = extract_project_mode_facts(tmp_path)
    report = check_activation(ModeActivation(mode=ProjectEntryMode.INSPECT), facts)
    assert report.allowed is True


def test_gate_disabled_task_is_owner_only(tmp_path: Path) -> None:
    """Covers: prompt 1/3 the Orchestrator can never choose a gate-disabled Task."""
    facts = _gate_repo_facts(tmp_path)
    with pytest.raises(ModeAuthorityError, match="never choose a gate-disabled Task"):
        check_activation(
            ModeActivation(
                mode=ProjectEntryMode.TASK,
                selected_by=ModeSelector.ORCHESTRATOR,
                confirmed_by_owner=True,
                gate_policy=TaskGatePolicy.OWNER_DISABLED,
            ),
            facts,
        )
    with pytest.raises(ModeAuthorityError, match="applies only to a Task"):
        check_activation(
            ModeActivation(
                mode=ProjectEntryMode.DEVELOP_EXISTING,
                selected_by=ModeSelector.OWNER,
                confirmed_by_owner=True,
                gate_policy=TaskGatePolicy.OWNER_DISABLED,
            ),
            facts,
        )


def test_owner_disabled_gate_is_allowed_with_a_warning_and_no_gate_blocker(
    tmp_path: Path,
) -> None:
    """Covers: prompt 1/3 a no-gate Task is only by explicit owner choice."""
    repo = _committed_repo(tmp_path)
    facts = extract_project_mode_facts(repo)
    with pytest.raises(ModeConfirmationError):
        check_activation(
            ModeActivation(
                mode=ProjectEntryMode.TASK,
                gate_policy=TaskGatePolicy.OWNER_DISABLED,
            ),
            facts,
        )
    report = check_activation(
        ModeActivation(
            mode=ProjectEntryMode.TASK,
            selected_by=ModeSelector.OWNER,
            confirmed_by_owner=True,
            gate_policy=TaskGatePolicy.OWNER_DISABLED,
        ),
        facts,
    )
    assert report.allowed is True
    assert report.accept_checklist == (GATE_DISABLED_WARNING,)
    assert GATE_DISABLED_WARNING in report.warnings
    assert "no gate evidence" in GATE_DISABLED_WARNING
    assert "never pushed" in GATE_DISABLED_WARNING


def test_activation_reports_environment_blockers(tmp_path: Path) -> None:
    """Covers: prompt 2 activation surfaces preflight blockers instead of raising."""
    facts = extract_project_mode_facts(tmp_path)
    report = check_activation(
        ModeActivation(mode=ProjectEntryMode.NEW_PROJECT, confirmed_by_owner=True),
        facts,
    )
    assert report.allowed is False
    assert {blocker.code for blocker in report.preflight.blockers} == {
        BlockerCode.NOT_A_GIT_REPOSITORY,
        BlockerCode.NO_COMMITS,
        BlockerCode.NO_GATE_SCRIPTS,
        BlockerCode.MISSING_ROADMAP,
    }


# --------------------------------------------------------------------------- #
# Durable mode record and owner reclassification
# --------------------------------------------------------------------------- #


def test_mode_record_round_trips_and_requires_confirmation(tmp_path: Path) -> None:
    """Covers: prompt 3 the mode is recorded per project."""
    state_dir = tmp_path / "state"
    record = ModeRecord(
        project_id="proj-a",
        mode=ProjectEntryMode.TASK,
        recorded_at=CREATED_AT,
        confirmed_by_owner=True,
    )
    path = save_mode_record(state_dir, record)
    assert path == mode_record_path(state_dir)
    loaded = load_mode_record(state_dir)
    assert loaded == record
    assert loaded is not None
    assert loaded.as_dict()["mode"] == "task"

    unconfirmed = ModeRecord(
        project_id="proj-a",
        mode=ProjectEntryMode.DEVELOP_EXISTING,
        recorded_at=CREATED_AT,
    )
    with pytest.raises(ModeConfirmationError):
        save_mode_record(state_dir, unconfirmed)


def test_mode_record_allows_an_unconfirmed_read_only_record(tmp_path: Path) -> None:
    """Covers: prompt 1 INSPECT writes no state, so its record needs no confirmation."""
    state_dir = tmp_path / "state"
    record = ModeRecord(
        project_id="proj-a",
        mode=ProjectEntryMode.INSPECT,
        recorded_at=CREATED_AT,
    )
    save_mode_record(state_dir, record)
    assert load_mode_record(state_dir) == record


def test_mode_is_owner_reclassifiable_at_accept(tmp_path: Path) -> None:
    """Covers: prompt 3 the mode is owner-reclassifiable at accept."""
    state_dir = tmp_path / "state"
    save_mode_record(
        state_dir,
        ModeRecord(
            project_id="proj-a",
            mode=ProjectEntryMode.TASK,
            recorded_at=CREATED_AT,
            confirmed_by_owner=True,
        ),
    )
    with pytest.raises(ModeConfirmationError):
        reclassify_mode(
            state_dir,
            mode=ProjectEntryMode.DEVELOP_EXISTING,
            confirmed_by_owner=False,
            recorded_at="2026-10-18T01:00:00+00:00",
        )
    updated = reclassify_mode(
        state_dir,
        mode=ProjectEntryMode.DEVELOP_EXISTING,
        confirmed_by_owner=True,
        recorded_at="2026-10-18T01:00:00+00:00",
    )
    assert updated.mode is ProjectEntryMode.DEVELOP_EXISTING
    assert updated.revision == 2
    assert updated.reclassified_by_owner is True
    assert load_mode_record(state_dir) == updated


def test_mode_record_fails_closed_on_missing_or_corrupt_state(tmp_path: Path) -> None:
    """Covers: prompt 3 record persistence fails closed."""
    state_dir = tmp_path / "state"
    assert load_mode_record(state_dir) is None
    with pytest.raises(ModeRecordError):
        reclassify_mode(
            state_dir,
            mode=ProjectEntryMode.TASK,
            confirmed_by_owner=True,
            recorded_at=CREATED_AT,
        )
    path = mode_record_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ModeRecordError, match="unreadable"):
        load_mode_record(state_dir)
    path.write_text(json.dumps({"project_id": "proj-a", "mode": "bogus"}), encoding="utf-8")
    with pytest.raises(ModeRecordError):
        load_mode_record(state_dir)


# --------------------------------------------------------------------------- #
# ROADMAP.md export
# --------------------------------------------------------------------------- #


def test_roadmap_export_matches_the_active_plan_revision(tmp_path: Path) -> None:
    """Covers: prompt 3 ROADMAP.md export matches the plan store."""
    store, revision = _plan_store_with_active_plan(tmp_path)
    rendered = export_roadmap_markdown(store, "proj-a")
    assert rendered == render_roadmap_markdown(revision)
    assert rendered.startswith(ROADMAP_HEADER)
    assert "Project: `proj-a`" in rendered
    assert "Plan revision: `v1`" in rendered
    assert "## Phase 1" in rendered
    assert "| `t1` | Set up the project | — |" in rendered
    assert "| `t2` | Build the feature | `t1` |" in rendered
    assert "2026-10-18T00:00:00+00:00" in rendered

    destination = tmp_path / "repo" / ROADMAP_FILENAME
    written = write_roadmap_export(tmp_path / "repo", revision)
    assert written == destination
    assert destination.read_text(encoding="utf-8") == rendered
    assert roadmap_export_matches(tmp_path / "repo", revision) is True

    newer = store.save_revision(
        "proj-a",
        [PlanTaskDef(task_id="t1", title="Renamed", phase="1")],
        version="v2",
        created_at="2026-10-19T00:00:00+00:00",
    )
    assert roadmap_export_matches(tmp_path / "repo", newer) is False
    assert roadmap_export_matches(tmp_path / "missing", revision) is False


def test_roadmap_export_is_deterministic_and_escapes_cells(tmp_path: Path) -> None:
    """Covers: prompt 3 the export is a deterministic rendering, not a clock read."""
    store = PlanStore(tmp_path / "plan-store")
    revision = store.save_revision(
        "proj-b",
        [
            PlanTaskDef(task_id="t1", title="A | B", phase="phase | one"),
            PlanTaskDef(task_id="t2", title="", phase=""),
        ],
        version="v1",
        created_at=CREATED_AT,
    )
    first = render_roadmap_markdown(revision)
    second = render_roadmap_markdown(revision)
    assert first == second
    assert "A \\| B" in first
    assert "## Phase phase \\| one" in first
    assert "## Phase unassigned" in first
    assert first.endswith("\n")
    empty = store.save_revision("proj-c", [], version="v1", created_at=CREATED_AT)
    assert "_No tasks in the active plan revision._" in render_roadmap_markdown(empty)


def test_next_roadmap_item_prefers_a_dependency_free_task(tmp_path: Path) -> None:
    """Covers: prompt 1 Continue shows the next roadmap item."""
    store = PlanStore(tmp_path / "plan-store")
    revision = store.save_revision(
        "proj-a",
        [
            PlanTaskDef(task_id="t2", title="second", phase="2", start_dependencies=("t1",)),
            PlanTaskDef(task_id="t1", title="first", phase="1"),
            PlanTaskDef(task_id="t3", title="third", phase="3"),
        ],
        version="v1",
        created_at=CREATED_AT,
    )
    item = next_roadmap_item(revision)
    assert item is not None
    assert item.task_id == "t1"
    assert item.title == "first"
    assert next_roadmap_item(
        store.save_revision("proj-b", [], version="v1", created_at=CREATED_AT)
    ) is None


def test_continue_summary_requires_continue_mode(tmp_path: Path) -> None:
    """Covers: prompt 1 Continue is only built from a Continue-qualified project."""
    repo = _committed_repo(tmp_path)
    store, revision = _plan_store_with_active_plan(tmp_path)
    facts = extract_project_mode_facts(repo, plan_store=store)
    with pytest.raises(ModeValidationError, match="not allowed by the observed facts"):
        build_continue_summary(facts, revision, StartupDigest())


def test_fact_claims_refuse_foreign_keys(tmp_path: Path) -> None:
    """Covers: prompt 2 the two fact halves never silently accept each other's keys."""
    facts = extract_project_mode_facts(tmp_path)
    assert facts.claim(FactKey.GIT_REPOSITORY) is False
    with pytest.raises(ModeValidationError, match="not a SaberOps state fact"):
        facts.state.claim(FactKey.GIT_REPOSITORY)
    with pytest.raises(ModeValidationError, match="not a repository fact"):
        RepositoryFacts(
            repo_path=str(tmp_path), is_git_repository=False, has_commits=False
        ).claim(FactKey.REGISTERED_IN_SABEROPS)


def test_continue_summary_type_annotations_are_exposed() -> None:
    """Covers: prompt 1 the Continue summary is a typed, inspectable value."""
    summary = ContinueSummary(
        project_id="proj-a",
        plan_version="v1",
        next_item=None,
        recent_runs=(),
        open_backlog=(),
    )
    assert "none (the active plan has no tasks)" in summary.render()
