"""Six project entry modes for starting and resuming SaberOps work (Step 026 / PM-1).

What this module is
-------------------
One deterministic authority for the question the Orchestrator asks before it
touches anything: *"which entry mode is this repository actually in, and is
that entry allowed right now?"* It implements the owner-approved v4 outcome
(roadmap proposal ``2026-10-18-v4-modes-autonomy-waves.md``, Step 026):

* the typed :class:`ProjectEntryMode` vocabulary (new project, develop an
  existing project, continue a SaberOps project, single task, resume a run,
  read-only inspect);
* :func:`extract_repository_facts` / :func:`extract_project_mode_facts`, which
  observe the repository and the owner state that already exist and never
  invent them;
* in-code narrowing of the allowed modes (:func:`allowed_modes`) plus the
  fail-closed refusal :func:`validate_recommendation`, so the Orchestrator
  picks *within* the fact-derived set and code validates its mode, confidence,
  evidence and runner-up;
* :func:`preflight`, which lists the blocking conditions for a mode together
  with the exact shell command that clears each one;
* :class:`ModeActivation` / :func:`check_activation`, the owner-confirmation
  and gate-policy boundary (a gate-disabled Task is owner-only and can never be
  chosen by the Orchestrator);
* the durable per-project :class:`ModeRecord` (owner-reclassifiable at accept);
* the repository ``ROADMAP.md`` export rendered from a
  :class:`~saberops.project_scope.plan_store.PlanRevision` (Step 015 / PL-1);
* the Continue summary that surfaces the startup digest, recent runs, the open
  backlog and the next roadmap item.

What this module is deliberately NOT
------------------------------------
It performs no scheduling, creates no run, launches no worker, executes no
gate and writes no candidate. It never chooses a paid provider and never calls
a model, so an Orchestrator recommendation is *validated input*, not an
authority: :func:`validate_recommendation` rejects a mode the facts do not
qualify, evidence that contradicts or is irrelevant to those facts, a
confidence above what the cited evidence supports, and an unqualified
runner-up. It fails closed; it never "repairs" a recommendation into
something plausible.

Authority boundaries kept intact:

* the gate-disabled workflow setting stays owned by
  :mod:`saberops.project_settings` (``gate_enabled``); this module only models
  the *per-Task* choice and refuses to let the Orchestrator take it. The
  warning it returns is a checklist projection for the existing accept flow;
  nothing is pushed, and the accept authority is untouched.
* readiness stays with :func:`saberops.ready_frontier.project_ready_frontier`.
  :func:`next_roadmap_item` is an *export ordering hint* for the ROADMAP.md
  rendering, not a launch decision.
* the plan store stays the only plan authority; the ROADMAP.md export is a
  deterministic rendering of one hash-verified
  :class:`~saberops.project_scope.plan_store.PlanRevision`.
* the startup digest belongs to Step 022 (OM-1). :class:`StartupDigest` is the
  structural subset this module consumes; it is never imported from a module
  that does not exist yet and never fabricated.

Mode lightness
--------------
Every mode has a rank, lightest (fewest writes) first. Ambiguous evidence is
resolved *towards* the lighter mode::

    inspect (0) < task (1) < resume_run (2) < develop_existing (3)
                < continue_saberops (4) < new_project (5)

The ordering is about authority footprint, not importance: ``inspect`` writes
nothing at all, while ``new_project`` creates history and a tracked roadmap.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Final

from saberops.process import run_process
from saberops.project import (
    ProjectRegistry,
    RegisteredProject,
    canonical_repo_root,
)
from saberops.project_scope.plan_store import PlanRevision, PlanStore, PlanTaskDef

__all__ = [
    "GATE_DISABLED_WARNING",
    "MODE_FORBIDDEN",
    "MODE_LABELS",
    "MODE_ORDER",
    "MODE_RECORD_FILENAME",
    "MODE_RECORD_SCHEMA_VERSION",
    "MODE_REQUIREMENTS",
    "MODE_WRITES_STATE",
    "ROADMAP_FILENAME",
    "ROADMAP_HEADER",
    "ActivationReport",
    "BlockerCode",
    "ContinueSummary",
    "EvidenceClaim",
    "FactKey",
    "ModeActivation",
    "ModeAuthorityError",
    "ModeChoice",
    "ModeConfirmationError",
    "ModeRecord",
    "ModeRecordError",
    "ModeRecommendation",
    "ModeSelector",
    "ModeValidationError",
    "OpenBacklogItem",
    "PreflightBlocker",
    "PreflightReport",
    "ProjectEntryMode",
    "ProjectModeError",
    "ProjectModeFacts",
    "RecentRun",
    "RepositoryFacts",
    "RoadmapItem",
    "SaberOpsStateFacts",
    "StartupDigest",
    "TaskGatePolicy",
    "allowed_modes",
    "build_continue_summary",
    "check_activation",
    "export_roadmap_markdown",
    "extract_project_mode_facts",
    "extract_repository_facts",
    "extract_saberops_state_facts",
    "load_mode_record",
    "mode_qualified",
    "mode_rank",
    "mode_requirements",
    "next_roadmap_item",
    "preflight",
    "reclassify_mode",
    "recommend_mode",
    "render_roadmap_markdown",
    "requires_owner_confirmation",
    "roadmap_export_matches",
    "save_mode_record",
    "validate_recommendation",
    "write_roadmap_export",
]

#: Repository-relative name of the exported roadmap document.
ROADMAP_FILENAME: Final[str] = "ROADMAP.md"

#: Directory holding repository-local SaberOps planning state.
ORCH_DIRNAME: Final[str] = ".orch"

#: Candidate gate entry points, in the order this module reports them.
GATE_SCRIPT_CANDIDATES: Final[tuple[str, ...]] = (
    "scripts/gate-full.sh",
    "scripts/gate-affected.sh",
    "scripts/lane-check.sh",
)

_MAKEFILE_NAMES: Final[tuple[str, ...]] = ("Makefile", "makefile", "GNUmakefile")
_GATE_TARGET_RE: Final = re.compile(r"(?m)^gate\s*:")

#: Confidence the pure recommender reports when it had to break a tie.
TIE_BREAK_CONFIDENCE: Final[float] = 0.5

#: Tolerance for the confidence <= evidence-coverage check.
_CONFIDENCE_EPSILON: Final[float] = 1e-9

MODE_RECORD_FILENAME: Final[str] = "project-mode.json"
MODE_RECORD_SCHEMA_VERSION: Final[int] = 1


class ProjectModeError(RuntimeError):
    """Base class for fail-closed project-mode refusals."""


class ModeValidationError(ProjectModeError):
    """A mode recommendation or qualification input is invalid."""


class ModeAuthorityError(ProjectModeError):
    """A caller tried to take an authority it does not hold."""


class ModeConfirmationError(ProjectModeError):
    """A state-writing mode was entered without explicit owner confirmation."""


class ModeRecordError(ProjectModeError):
    """The persisted per-project mode record is missing or malformed."""


class ProjectEntryMode(StrEnum):
    """The six ways a project can be entered.

    ``NEW_PROJECT``
        Fresh repository: ``git init``, an initial commit, an initial gate and
        an owner-approved ``ROADMAP.md`` in the repository.
    ``DEVELOP_EXISTING``
        Existing repository that SaberOps has no active plan for: a gate check
        plus a durable project map kept in SaberOps state per commit.
    ``CONTINUE_SABEROPS``
        A project SaberOps already develops: the OM-1 startup digest, recent
        runs, the open backlog and the next roadmap item.
    ``TASK``
        One task with a gate, or the existing owner-only gate-disabled setting
        applied to that Task (never chosen by the Orchestrator).
    ``RESUME_RUN``
        Resume a paused or interrupted run from its exact checkpoint.
    ``INSPECT``
        Read-only status and health report: zero candidates created, no gates
        run.
    """

    NEW_PROJECT = "new_project"
    DEVELOP_EXISTING = "develop_existing"
    CONTINUE_SABEROPS = "continue_saberops"
    TASK = "task"
    RESUME_RUN = "resume_run"
    INSPECT = "inspect"


#: Every mode, lightest first (see the module docstring).
MODE_ORDER: Final[tuple[ProjectEntryMode, ...]] = (
    ProjectEntryMode.INSPECT,
    ProjectEntryMode.TASK,
    ProjectEntryMode.RESUME_RUN,
    ProjectEntryMode.DEVELOP_EXISTING,
    ProjectEntryMode.CONTINUE_SABEROPS,
    ProjectEntryMode.NEW_PROJECT,
)

_MODE_RANK: Final[dict[ProjectEntryMode, int]] = {
    mode: index for index, mode in enumerate(MODE_ORDER)
}

#: One-line owner-facing label per mode.
MODE_LABELS: Final[dict[ProjectEntryMode, str]] = {
    ProjectEntryMode.NEW_PROJECT: "New project",
    ProjectEntryMode.DEVELOP_EXISTING: "Develop existing project",
    ProjectEntryMode.CONTINUE_SABEROPS: "Continue SaberOps project",
    ProjectEntryMode.TASK: "Task",
    ProjectEntryMode.RESUME_RUN: "Resume run",
    ProjectEntryMode.INSPECT: "Inspect (read-only)",
}

#: Modes that write roadmap or project state and therefore need owner consent.
MODE_WRITES_STATE: Final[frozenset[ProjectEntryMode]] = frozenset(
    {
        ProjectEntryMode.NEW_PROJECT,
        ProjectEntryMode.DEVELOP_EXISTING,
        ProjectEntryMode.CONTINUE_SABEROPS,
        ProjectEntryMode.TASK,
        ProjectEntryMode.RESUME_RUN,
    }
)


class FactKey(StrEnum):
    """The boolean repository/SaberOps facts modes are qualified against.

    The first six are the repository facts the owner asked to be detected;
    the remaining four are the owner-state facts that separate "a repository
    SaberOps already develops" from "a repository it merely can see".
    """

    GIT_REPOSITORY = "git_repository"
    COMMITS_PRESENT = "commits_present"
    GATE_SCRIPTS_PRESENT = "gate_scripts_present"
    ORCH_STATE_PRESENT = "orch_state_present"
    WORKING_TREE_DIRTY = "working_tree_dirty"
    GIT_HOOKS_PRESENT = "git_hooks_present"
    ROADMAP_PRESENT = "roadmap_present"
    REGISTERED_IN_SABEROPS = "registered_in_saberops"
    ACTIVE_PLAN_PRESENT = "active_plan_present"
    RESUMABLE_RUN_PRESENT = "resumable_run_present"


_REPOSITORY_FACT_KEYS: Final[frozenset[FactKey]] = frozenset(
    {
        FactKey.GIT_REPOSITORY,
        FactKey.COMMITS_PRESENT,
        FactKey.GATE_SCRIPTS_PRESENT,
        FactKey.ORCH_STATE_PRESENT,
        FactKey.WORKING_TREE_DIRTY,
        FactKey.GIT_HOOKS_PRESENT,
        FactKey.ROADMAP_PRESENT,
    }
)

#: Facts a mode must see satisfied (all of them) to be qualified.
MODE_REQUIREMENTS: Final[dict[ProjectEntryMode, tuple[FactKey, ...]]] = {
    ProjectEntryMode.INSPECT: (),
    ProjectEntryMode.NEW_PROJECT: (),
    ProjectEntryMode.TASK: (FactKey.GIT_REPOSITORY, FactKey.COMMITS_PRESENT),
    ProjectEntryMode.RESUME_RUN: (
        FactKey.GIT_REPOSITORY,
        FactKey.COMMITS_PRESENT,
        FactKey.RESUMABLE_RUN_PRESENT,
    ),
    ProjectEntryMode.DEVELOP_EXISTING: (
        FactKey.GIT_REPOSITORY,
        FactKey.COMMITS_PRESENT,
        FactKey.GATE_SCRIPTS_PRESENT,
    ),
    ProjectEntryMode.CONTINUE_SABEROPS: (
        FactKey.GIT_REPOSITORY,
        FactKey.COMMITS_PRESENT,
        FactKey.REGISTERED_IN_SABEROPS,
        FactKey.ACTIVE_PLAN_PRESENT,
    ),
}

#: Facts a mode must see *not* satisfied (all of them) to be qualified.
MODE_FORBIDDEN: Final[dict[ProjectEntryMode, tuple[FactKey, ...]]] = {
    ProjectEntryMode.INSPECT: (),
    ProjectEntryMode.NEW_PROJECT: (FactKey.COMMITS_PRESENT,),
    ProjectEntryMode.TASK: (),
    ProjectEntryMode.RESUME_RUN: (FactKey.WORKING_TREE_DIRTY,),
    ProjectEntryMode.DEVELOP_EXISTING: (FactKey.ACTIVE_PLAN_PRESENT,),
    ProjectEntryMode.CONTINUE_SABEROPS: (),
}


def mode_rank(mode: ProjectEntryMode) -> int:
    """Return the mode's lightness rank (0 is the lightest mode)."""
    return _MODE_RANK[mode]


def mode_requirements(mode: ProjectEntryMode) -> tuple[FactKey, ...]:
    """Return the facts ``mode`` requires to be satisfied."""
    return MODE_REQUIREMENTS[mode]


def _lighter(first: ProjectEntryMode, second: ProjectEntryMode) -> ProjectEntryMode:
    return first if mode_rank(first) <= mode_rank(second) else second


# --------------------------------------------------------------------------- #
# Repository fact extraction
# --------------------------------------------------------------------------- #


def _git_lines(args: list[str], cwd: Path) -> tuple[str, ...]:
    """Run one git command and return non-empty stdout lines (never raising)."""
    result = run_process(["git", *args], cwd=cwd)
    if not result.passed:
        return ()
    return tuple(line for line in result.stdout.splitlines() if line.strip())


def _detect_gate_scripts(root: Path) -> tuple[str, ...]:
    """Return the repository-relative gate entry points that exist."""
    found: list[str] = []
    for relative in GATE_SCRIPT_CANDIDATES:
        candidate = root / relative
        if candidate.is_file():
            found.append(relative)
    for name in _MAKEFILE_NAMES:
        makefile = root / name
        try:
            text = makefile.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _GATE_TARGET_RE.search(text):
            found.append(name)
            break
    return tuple(found)


def _detect_git_hooks(root: Path) -> tuple[str, ...]:
    """Return installed (non-sample) git hook names, sorted and deduplicated."""
    common = _git_lines(["rev-parse", "--git-common-dir"], root)
    if not common:
        return ()
    git_dir = Path(common[0])
    if not git_dir.is_absolute():
        git_dir = root / git_dir
    hooks_dir = git_dir / "hooks"
    if not hooks_dir.is_dir():
        return ()
    names = {
        entry.name
        for entry in hooks_dir.iterdir()
        if entry.is_file() and not entry.name.endswith(".sample")
    }
    return tuple(sorted(names))


@dataclass(frozen=True)
class RepositoryFacts:
    """Observed facts about one repository working tree (no inference)."""

    repo_path: str
    is_git_repository: bool
    has_commits: bool
    head_sha: str | None = None
    gate_scripts: tuple[str, ...] = ()
    orch_state_present: bool = False
    working_tree_dirty: bool = False
    git_hooks: tuple[str, ...] = ()
    roadmap_present: bool = False

    def claim(self, key: FactKey) -> bool:
        """Return the observed boolean value of one repository fact."""
        if key is FactKey.GIT_REPOSITORY:
            return self.is_git_repository
        if key is FactKey.COMMITS_PRESENT:
            return self.has_commits
        if key is FactKey.GATE_SCRIPTS_PRESENT:
            return bool(self.gate_scripts)
        if key is FactKey.ORCH_STATE_PRESENT:
            return self.orch_state_present
        if key is FactKey.WORKING_TREE_DIRTY:
            return self.working_tree_dirty
        if key is FactKey.GIT_HOOKS_PRESENT:
            return bool(self.git_hooks)
        if key is FactKey.ROADMAP_PRESENT:
            return self.roadmap_present
        raise ModeValidationError(f"{key!r} is not a repository fact")

    def as_dict(self) -> dict[str, object]:
        """Serialize deterministically for evidence and reports."""
        return {
            "repo_path": self.repo_path,
            "is_git_repository": self.is_git_repository,
            "has_commits": self.has_commits,
            "head_sha": self.head_sha,
            "gate_scripts": list(self.gate_scripts),
            "orch_state_present": self.orch_state_present,
            "working_tree_dirty": self.working_tree_dirty,
            "git_hooks": list(self.git_hooks),
            "roadmap_present": self.roadmap_present,
        }


def extract_repository_facts(repo_path: Path | str) -> RepositoryFacts:
    """Observe the repository facts a mode qualification is allowed to use.

    A missing path, a non-repository directory and a repository without
    commits are all *observed states* here, never errors: narrowing a mode
    because the facts say so is the point. No network, no model, no clock.
    """
    path = Path(repo_path).expanduser()
    if not path.is_dir():
        return RepositoryFacts(
            repo_path=str(path),
            is_git_repository=False,
            has_commits=False,
        )
    root = canonical_repo_root(path)
    if root is None:
        return RepositoryFacts(
            repo_path=str(path),
            is_git_repository=False,
            has_commits=False,
            gate_scripts=_detect_gate_scripts(path),
            orch_state_present=(path / ORCH_DIRNAME).is_dir(),
            roadmap_present=(path / ROADMAP_FILENAME).is_file(),
        )
    head = _git_lines(["rev-parse", "HEAD"], root)
    head_sha = head[0] if head else None
    return RepositoryFacts(
        repo_path=str(root),
        is_git_repository=True,
        has_commits=head_sha is not None,
        head_sha=head_sha,
        gate_scripts=_detect_gate_scripts(root),
        orch_state_present=(root / ORCH_DIRNAME).is_dir(),
        working_tree_dirty=bool(_git_lines(["status", "--porcelain"], root)),
        git_hooks=_detect_git_hooks(root),
        roadmap_present=(root / ROADMAP_FILENAME).is_file(),
    )


@dataclass(frozen=True)
class SaberOpsStateFacts:
    """Observed owner-state facts for the project behind a repository.

    ``plan_versions`` / ``active_plan_version`` come from the Step 015 plan
    store and ``registered_project_ids`` from the Step 015 registry.
    ``resumable_run_ids`` is supplied by the caller (Step 011 / WX-2 owns the
    run inventory); this module never queries a database, so an unavailable
    inventory narrows the modes instead of guessing that a run is resumable.
    """

    registered_project_ids: tuple[str, ...] = ()
    plan_versions: tuple[str, ...] = ()
    active_plan_version: str | None = None
    resumable_run_ids: tuple[str, ...] = ()

    @property
    def registered(self) -> bool:
        """Whether any registered project points at this repository."""
        return bool(self.registered_project_ids)

    @property
    def active_plan_present(self) -> bool:
        """Whether SaberOps holds an active plan revision for this project."""
        return self.active_plan_version is not None

    @property
    def resumable_run_present(self) -> bool:
        """Whether the caller supplied at least one resumable run."""
        return bool(self.resumable_run_ids)

    def claim(self, key: FactKey) -> bool:
        """Return the observed boolean value of one owner-state fact."""
        if key is FactKey.REGISTERED_IN_SABEROPS:
            return self.registered
        if key is FactKey.ACTIVE_PLAN_PRESENT:
            return self.active_plan_present
        if key is FactKey.RESUMABLE_RUN_PRESENT:
            return self.resumable_run_present
        raise ModeValidationError(f"{key!r} is not a SaberOps state fact")

    def as_dict(self) -> dict[str, object]:
        """Serialize deterministically for evidence and reports."""
        return {
            "registered_project_ids": list(self.registered_project_ids),
            "plan_versions": list(self.plan_versions),
            "active_plan_version": self.active_plan_version,
            "resumable_run_ids": list(self.resumable_run_ids),
        }


def _matching_registered_projects(
    registry: ProjectRegistry, repo_root: Path
) -> tuple[RegisteredProject, ...]:
    """Return registry entries whose repository root is ``repo_root``."""
    target = os.path.realpath(repo_root)
    matches: list[RegisteredProject] = []
    for project in registry.list_projects():
        if os.path.realpath(str(Path(project.repository_root).expanduser())) == target:
            matches.append(project)
    return tuple(sorted(matches, key=lambda project: project.project_id))


def extract_saberops_state_facts(
    repo_path: Path | str,
    *,
    plan_store: PlanStore | None = None,
    registry: ProjectRegistry | None = None,
    resumable_run_ids: tuple[str, ...] = (),
) -> SaberOpsStateFacts:
    """Observe the owner-state facts for the project behind ``repo_path``.

    A repository that is not registered, or registered without an active plan
    revision, yields an honest empty/partial observation; nothing is created.
    """
    path = Path(repo_path).expanduser()
    root = canonical_repo_root(path) if path.is_dir() else None
    if root is None:
        return SaberOpsStateFacts(resumable_run_ids=resumable_run_ids)
    active_registry = registry if registry is not None else ProjectRegistry()
    matches = _matching_registered_projects(active_registry, root)
    if not matches:
        return SaberOpsStateFacts(resumable_run_ids=resumable_run_ids)
    store = plan_store if plan_store is not None else PlanStore()
    versions: list[str] = []
    active_version: str | None = None
    for project in matches:
        for version in store.list_revisions(project.project_id):
            if version not in versions:
                versions.append(version)
        if active_version is None:
            active_version = store.active_version(project.project_id)
    return SaberOpsStateFacts(
        registered_project_ids=tuple(project.project_id for project in matches),
        plan_versions=tuple(versions),
        active_plan_version=active_version,
        resumable_run_ids=resumable_run_ids,
    )


@dataclass(frozen=True)
class ProjectModeFacts:
    """The complete fact snapshot one mode decision is made against."""

    repository: RepositoryFacts
    state: SaberOpsStateFacts = field(default_factory=SaberOpsStateFacts)

    def claim(self, key: FactKey) -> bool:
        """Return the observed boolean value of one fact, from either half."""
        if key in _REPOSITORY_FACT_KEYS:
            return self.repository.claim(key)
        return self.state.claim(key)

    def as_dict(self) -> dict[str, object]:
        """Serialize both fact halves deterministically."""
        return {
            "repository": self.repository.as_dict(),
            "state": self.state.as_dict(),
        }


def extract_project_mode_facts(
    repo_path: Path | str,
    *,
    plan_store: PlanStore | None = None,
    registry: ProjectRegistry | None = None,
    resumable_run_ids: tuple[str, ...] = (),
) -> ProjectModeFacts:
    """Observe both halves of the mode facts for one repository."""
    return ProjectModeFacts(
        repository=extract_repository_facts(repo_path),
        state=extract_saberops_state_facts(
            repo_path,
            plan_store=plan_store,
            registry=registry,
            resumable_run_ids=resumable_run_ids,
        ),
    )


# --------------------------------------------------------------------------- #
# Mode qualification
# --------------------------------------------------------------------------- #


def mode_qualified(facts: ProjectModeFacts, mode: ProjectEntryMode) -> bool:
    """Return whether ``facts`` qualify ``mode`` (requirements and forbiddens)."""
    if not all(facts.claim(key) for key in MODE_REQUIREMENTS[mode]):
        return False
    return not any(facts.claim(key) for key in MODE_FORBIDDEN[mode])


def allowed_modes(facts: ProjectModeFacts) -> tuple[ProjectEntryMode, ...]:
    """Return the modes the observed facts allow, lightest first."""
    return tuple(mode for mode in MODE_ORDER if mode_qualified(facts, mode))


def _specificity(mode: ProjectEntryMode) -> int:
    """Return how many facts ``mode`` requires (more facts, more specific)."""
    return len(MODE_REQUIREMENTS[mode])


def _candidate_modes(facts: ProjectModeFacts) -> tuple[ProjectEntryMode, ...]:
    """Qualified modes excluding ``INSPECT`` (never a default recommendation).

    ``INSPECT`` is always available but is an explicit read-only request, not
    a default entry; recommending it would hide the actionable mode.
    """
    return tuple(
        mode for mode in allowed_modes(facts) if mode is not ProjectEntryMode.INSPECT
    )


# --------------------------------------------------------------------------- #
# Recommendation and its fail-closed validation
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class EvidenceClaim:
    """One fact assertion in a mode recommendation.

    ``expected`` is the value the Orchestrator asserts the fact has. The
    validator compares it against the observed snapshot, so a claim that
    contradicts the repository (or a fact the mode does not use) is refused
    instead of being silently dropped.
    """

    fact: FactKey
    expected: bool = True


@dataclass(frozen=True)
class ModeRecommendation:
    """A typed entry-mode suggestion with its evidence and runner-up."""

    mode: ProjectEntryMode
    confidence: float
    evidence: tuple[EvidenceClaim, ...] = ()
    runner_up: ProjectEntryMode | None = None


@dataclass(frozen=True)
class ModeChoice:
    """The validated result of a recommendation, after tie-breaking."""

    mode: ProjectEntryMode
    requested_mode: ProjectEntryMode
    confidence: float
    evidence: tuple[EvidenceClaim, ...]
    runner_up: ProjectEntryMode | None
    tie_broken: bool

    def as_dict(self) -> dict[str, object]:
        """Serialize deterministically for evidence records."""
        return {
            "mode": self.mode.value,
            "requested_mode": self.requested_mode.value,
            "confidence": self.confidence,
            "evidence": [
                {"fact": claim.fact.value, "expected": claim.expected}
                for claim in self.evidence
            ],
            "runner_up": self.runner_up.value if self.runner_up is not None else None,
            "tie_broken": self.tie_broken,
        }


def recommend_mode(facts: ProjectModeFacts) -> ModeRecommendation:
    """Recommend the most specific qualified mode from the facts alone.

    The most specific qualified mode is the one whose qualification uses the
    most observed facts, because extra facts are what distinguish "a
    repository SaberOps develops" from "any repository with a commit". Two
    modes with the same specificity are a genuine tie and the lighter one
    wins (with a reduced :data:`TIE_BREAK_CONFIDENCE`); when every mode is
    disqualified, ``INSPECT`` is the honest answer.
    """
    candidates = _candidate_modes(facts)
    if not candidates:
        return ModeRecommendation(
            mode=ProjectEntryMode.INSPECT,
            confidence=1.0,
            evidence=(),
            runner_up=None,
        )
    best_specificity = max(_specificity(mode) for mode in candidates)
    top = tuple(mode for mode in candidates if _specificity(mode) == best_specificity)
    chosen = top[0]
    for mode in top[1:]:
        chosen = _lighter(chosen, mode)
    tied = tuple(mode for mode in top if mode is not chosen)
    runner_up: ProjectEntryMode | None = tied[0] if tied else None
    if runner_up is None:
        others = sorted(
            (mode for mode in candidates if mode is not chosen),
            key=lambda mode: (-_specificity(mode), mode_rank(mode)),
        )
        runner_up = others[0] if others else None
    evidence = tuple(
        EvidenceClaim(fact=key, expected=True) for key in MODE_REQUIREMENTS[chosen]
    )
    confidence = TIE_BREAK_CONFIDENCE if tied else 1.0
    return ModeRecommendation(
        mode=chosen,
        confidence=confidence,
        evidence=evidence,
        runner_up=runner_up,
    )


def _coverage(mode: ProjectEntryMode, evidence: tuple[EvidenceClaim, ...]) -> float:
    """Return the fraction of ``mode``'s required facts the evidence covers."""
    required = MODE_REQUIREMENTS[mode]
    if not required:
        return 1.0
    cited = {claim.fact for claim in evidence}
    return sum(1 for key in required if key in cited) / float(len(required))


def _validated_evidence(
    mode: ProjectEntryMode, evidence: tuple[EvidenceClaim, ...], facts: ProjectModeFacts
) -> None:
    """Refuse evidence that is invented, duplicated or contradicts the facts."""
    seen: set[FactKey] = set()
    required = set(MODE_REQUIREMENTS[mode])
    for claim in evidence:
        if claim.fact in seen:
            raise ModeValidationError(
                f"duplicate evidence for fact {claim.fact.value!r} in mode {mode.value!r}"
            )
        seen.add(claim.fact)
        if claim.fact not in required:
            raise ModeValidationError(
                f"evidence fact {claim.fact.value!r} does not qualify mode "
                f"{mode.value!r}: only required facts may be cited"
            )
        observed = facts.claim(claim.fact)
        if observed is not claim.expected:
            raise ModeValidationError(
                f"evidence for {mode.value!r} contradicts the repository: fact "
                f"{claim.fact.value!r} is {observed}, evidence claims {claim.expected}"
            )
    if required and not evidence:
        raise ModeValidationError(
            f"mode {mode.value!r} needs evidence for its facts, none was cited"
        )


def _tie_is_resolved(mode: ProjectEntryMode, evidence: tuple[EvidenceClaim, ...]) -> bool:
    """Whether the evidence cites a fact only this mode of the tie requires."""
    cited = {claim.fact for claim in evidence}
    own = set(MODE_REQUIREMENTS[mode])
    for other in MODE_ORDER:
        if other is mode or _specificity(other) != _specificity(mode):
            continue
        if cited & (own - set(MODE_REQUIREMENTS[other])):
            return True
    return False


def validate_recommendation(
    recommendation: ModeRecommendation, facts: ProjectModeFacts
) -> ModeChoice:
    """Validate an Orchestrator recommendation, fail-closed, and tie-break it.

    Refusals: a mode the facts do not allow, an unqualified or self-referential
    runner-up, a malformed confidence, confidence above the cited evidence's
    coverage, missing evidence for a mode that has required facts, and
    evidence that is irrelevant to the mode or contradicts the observation.
    A genuine tie (equal specificity, no distinguishing cited fact) resolves
    to the lighter mode instead of failing; the result records that it did.
    """
    mode = recommendation.mode
    if not isinstance(mode, ProjectEntryMode):
        raise ModeValidationError(f"unknown entry mode {mode!r}")
    if not mode_qualified(facts, mode):
        raise ModeValidationError(
            f"mode {mode.value!r} is not allowed by the observed repository facts; "
            f"allowed: {[entry.value for entry in allowed_modes(facts)]}"
        )
    confidence = recommendation.confidence
    if not isinstance(confidence, float) or not (0.0 <= confidence <= 1.0):
        raise ModeValidationError(
            f"confidence must be a float in [0, 1], got {confidence!r}"
        )
    evidence = tuple(recommendation.evidence)
    _validated_evidence(mode, evidence, facts)
    coverage = _coverage(mode, evidence)
    if confidence > coverage + _CONFIDENCE_EPSILON:
        raise ModeValidationError(
            f"confidence {confidence} exceeds the evidence coverage {coverage} for "
            f"mode {mode.value!r}"
        )
    runner_up = recommendation.runner_up
    if runner_up is not None:
        if not isinstance(runner_up, ProjectEntryMode):
            raise ModeValidationError(f"unknown runner-up mode {runner_up!r}")
        if runner_up is mode:
            raise ModeValidationError("runner_up must differ from the recommended mode")
        if not mode_qualified(facts, runner_up):
            raise ModeValidationError(
                f"runner_up {runner_up.value!r} is not allowed by the observed facts"
            )
    tied: tuple[ProjectEntryMode, ...] = ()
    if mode is not ProjectEntryMode.INSPECT and not _tie_is_resolved(mode, evidence):
        tied = tuple(
            other
            for other in allowed_modes(facts)
            if other is not mode
            and other is not ProjectEntryMode.INSPECT
            and _specificity(other) == _specificity(mode)
        )
    if not tied:
        return ModeChoice(
            mode=mode,
            requested_mode=mode,
            confidence=confidence,
            evidence=evidence,
            runner_up=runner_up,
            tie_broken=False,
        )
    effective = mode
    for other in tied:
        effective = _lighter(effective, other)
    if runner_up is None:
        runner_up = mode if effective is not mode else tied[0]
    return ModeChoice(
        mode=effective,
        requested_mode=mode,
        confidence=confidence,
        evidence=evidence,
        runner_up=runner_up,
        tie_broken=True,
    )


# --------------------------------------------------------------------------- #
# Preflight blockers
# --------------------------------------------------------------------------- #


class BlockerCode(StrEnum):
    """Machine-readable preflight blocker vocabulary."""

    NOT_A_GIT_REPOSITORY = "not_a_git_repository"
    NO_COMMITS = "no_commits"
    NO_GATE_SCRIPTS = "no_gate_scripts"
    NOT_REGISTERED = "not_registered"
    NO_ACTIVE_PLAN = "no_active_plan"
    NO_RESUMABLE_RUN = "no_resumable_run"
    DIRTY_WORKING_TREE = "dirty_working_tree"
    MISSING_ROADMAP = "missing_roadmap"
    REPOSITORY_HAS_COMMITS = "repository_has_commits"
    ACTIVE_PLAN_EXISTS = "active_plan_exists"


@dataclass(frozen=True)
class PreflightBlocker:
    """One blocking condition with the exact shell command that clears it."""

    code: BlockerCode
    summary: str
    fix_command: str


@dataclass(frozen=True)
class PreflightReport:
    """The blockers and non-blocking warnings for entering one mode."""

    mode: ProjectEntryMode
    blockers: tuple[PreflightBlocker, ...] = ()
    warnings: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        """Whether the mode can be entered with no blocking condition."""
        return not self.blockers

    def render(self) -> str:
        """Render the report as deterministic plain text."""
        lines = [f"mode: {self.mode.value}", f"ok: {'yes' if self.ok else 'no'}"]
        for blocker in self.blockers:
            lines.append(f"blocker: {blocker.code.value}: {blocker.summary}")
            lines.append(f"  fix: {blocker.fix_command}")
        for warning in self.warnings:
            lines.append(f"warning: {warning}")
        return "\n".join(lines)


_MISSING_FACT_BLOCKERS: Final[dict[FactKey, BlockerCode]] = {
    FactKey.GIT_REPOSITORY: BlockerCode.NOT_A_GIT_REPOSITORY,
    FactKey.COMMITS_PRESENT: BlockerCode.NO_COMMITS,
    FactKey.GATE_SCRIPTS_PRESENT: BlockerCode.NO_GATE_SCRIPTS,
    FactKey.REGISTERED_IN_SABEROPS: BlockerCode.NOT_REGISTERED,
    FactKey.ACTIVE_PLAN_PRESENT: BlockerCode.NO_ACTIVE_PLAN,
    FactKey.RESUMABLE_RUN_PRESENT: BlockerCode.NO_RESUMABLE_RUN,
}

_MISSING_FACT_SUMMARIES: Final[dict[FactKey, str]] = {
    FactKey.GIT_REPOSITORY: "the target is not a git repository",
    FactKey.COMMITS_PRESENT: "the repository has no commits yet",
    FactKey.GATE_SCRIPTS_PRESENT: (
        "no gate entry point (scripts/gate-*.sh or a Makefile gate target) exists"
    ),
    FactKey.REGISTERED_IN_SABEROPS: "no registered SaberOps project points at this repository",
    FactKey.ACTIVE_PLAN_PRESENT: "the project has no active plan revision to continue",
    FactKey.RESUMABLE_RUN_PRESENT: "no paused or interrupted run is available to resume",
}

_VIOLATED_FORBIDDEN_BLOCKERS: Final[dict[FactKey, BlockerCode]] = {
    FactKey.COMMITS_PRESENT: BlockerCode.REPOSITORY_HAS_COMMITS,
    FactKey.WORKING_TREE_DIRTY: BlockerCode.DIRTY_WORKING_TREE,
    FactKey.ACTIVE_PLAN_PRESENT: BlockerCode.ACTIVE_PLAN_EXISTS,
}

_VIOLATED_FORBIDDEN_SUMMARIES: Final[dict[FactKey, str]] = {
    FactKey.COMMITS_PRESENT: (
        "the repository already has commits, so it cannot be a new project"
    ),
    FactKey.WORKING_TREE_DIRTY: (
        "the working tree has uncommitted changes, so a resume cannot start from "
        "the exact checkpoint"
    ),
    FactKey.ACTIVE_PLAN_PRESENT: (
        "the project already has an active plan revision; continue it instead of "
        "developing it from scratch"
    ),
}

_GATE_SCRIPT_FIX: Final[str] = (
    "mkdir -p scripts && printf '#!/usr/bin/env bash\\nset -euo pipefail\\n"
    "# <project test command>\\n' > scripts/gate-full.sh && chmod +x scripts/gate-full.sh"
)


def _fix_command(code: BlockerCode, facts: ProjectModeFacts) -> str:
    """Return the exact shell command that clears one blocker."""
    repo = facts.repository.repo_path or "."
    if code is BlockerCode.NOT_A_GIT_REPOSITORY:
        return "git init"
    if code is BlockerCode.NO_COMMITS:
        return 'git add -A && git commit -m "chore: initial commit"'
    if code is BlockerCode.NO_GATE_SCRIPTS:
        return _GATE_SCRIPT_FIX
    if code is BlockerCode.NOT_REGISTERED:
        return (
            f"orch project register --id <project-id> --name <display-name> "
            f"--repo {repo} --state-dir <state-root>"
        )
    if code is BlockerCode.NO_ACTIVE_PLAN:
        return (
            "python -c \"from saberops.project_scope.plan_store import PlanStore, "
            "PlanTaskDef; s = PlanStore(); s.set_active('<project-id>', "
            "s.save_revision('<project-id>', [PlanTaskDef(task_id='t1', "
            "title='<first task>')]).version)\""
        )
    if code is BlockerCode.NO_RESUMABLE_RUN:
        return f"orch list --repo {repo}"
    if code is BlockerCode.DIRTY_WORKING_TREE:
        return 'git stash push --include-untracked -m "saberops: before resume"'
    if code is BlockerCode.MISSING_ROADMAP:
        return (
            "printf '# ROADMAP\\n\\nOwner-approved roadmap.\\n' > ROADMAP.md && "
            'git add ROADMAP.md && git commit -m "docs: add owner-approved ROADMAP.md"'
        )
    if code is BlockerCode.REPOSITORY_HAS_COMMITS:
        return f'orch run --repo {repo} --task "<task>"'
    if code is BlockerCode.ACTIVE_PLAN_EXISTS:
        return "orch plan-store show --project <project-id>"
    raise ModeValidationError(f"no fix command is defined for blocker {code!r}")


def _preflight_warnings(mode: ProjectEntryMode, facts: ProjectModeFacts) -> tuple[str, ...]:
    """Return non-blocking observations worth showing with the report."""
    warnings: list[str] = []
    repository = facts.repository
    if not Path(repository.repo_path).is_dir():
        warnings.append(f"path {repository.repo_path!r} does not exist yet")
    if mode in (
        ProjectEntryMode.NEW_PROJECT,
        ProjectEntryMode.DEVELOP_EXISTING,
        ProjectEntryMode.TASK,
    ):
        if not facts.claim(FactKey.GIT_HOOKS_PRESENT):
            warnings.append(
                "no git hooks are installed; a pre-push gate hook is recommended"
            )
    if mode is ProjectEntryMode.NEW_PROJECT and repository.has_commits:
        warnings.append("an existing history will not be rewritten")
    if (
        mode is ProjectEntryMode.CONTINUE_SABEROPS
        and not facts.claim(FactKey.ORCH_STATE_PRESENT)
    ):
        warnings.append(
            "the repository has no .orch state directory; the plan lives only in "
            "SaberOps owner state"
        )
    if (
        mode
        in (
            ProjectEntryMode.DEVELOP_EXISTING,
            ProjectEntryMode.TASK,
        )
        and repository.working_tree_dirty
    ):
        warnings.append(
            "uncommitted changes are not part of the durable project map until committed"
        )
    if mode is ProjectEntryMode.RESUME_RUN and repository.working_tree_dirty:
        warnings.append("the dirty working tree must be resolved before resuming")
    return tuple(warnings)


def preflight(
    mode: ProjectEntryMode,
    facts: ProjectModeFacts,
    *,
    gate_required: bool = True,
) -> PreflightReport:
    """List the blocking conditions for entering ``mode`` under ``facts``.

    Blockers come from three sources: an unsatisfied required fact, a violated
    forbidden fact, and a mode-specific operational requirement. ``NEW_PROJECT``
    is special because entering it *creates* those facts, so its preflight lists
    ``git init``, the initial commit, a gate entry point and the committed
    ``ROADMAP.md`` in the order they must be done. ``INSPECT`` is read-only and
    is never blocked. Each blocker carries an exact shell command that clears
    it; ``<project-id>``-style placeholders are values only the owner can
    choose.
    """
    if mode is ProjectEntryMode.INSPECT:
        return PreflightReport(
            mode=mode, blockers=(), warnings=_preflight_warnings(mode, facts)
        )
    blockers: list[PreflightBlocker] = []

    def add_fact(fact: FactKey) -> None:
        code = _MISSING_FACT_BLOCKERS[fact]
        blockers.append(
            PreflightBlocker(
                code=code,
                summary=_MISSING_FACT_SUMMARIES[fact],
                fix_command=_fix_command(code, facts),
            )
        )

    def add_forbidden(fact: FactKey) -> None:
        code = _VIOLATED_FORBIDDEN_BLOCKERS[fact]
        blockers.append(
            PreflightBlocker(
                code=code,
                summary=_VIOLATED_FORBIDDEN_SUMMARIES[fact],
                fix_command=_fix_command(code, facts),
            )
        )

    requirements = MODE_REQUIREMENTS[mode]
    if mode is ProjectEntryMode.NEW_PROJECT:
        # A new project is *entered* by creating what it needs, so the same
        # facts that make it the allowed mode are also its preflight steps.
        for fact in (
            FactKey.GIT_REPOSITORY,
            FactKey.COMMITS_PRESENT,
            FactKey.GATE_SCRIPTS_PRESENT,
        ):
            if not facts.claim(fact):
                add_fact(fact)
        if not facts.claim(FactKey.ROADMAP_PRESENT):
            blockers.append(
                PreflightBlocker(
                    code=BlockerCode.MISSING_ROADMAP,
                    summary="no owner-approved ROADMAP.md is committed in the repository",
                    fix_command=_fix_command(BlockerCode.MISSING_ROADMAP, facts),
                )
            )
    else:
        for fact in requirements:
            if not facts.claim(fact):
                add_fact(fact)
        if (
            mode is ProjectEntryMode.TASK
            and gate_required
            and not facts.claim(FactKey.GATE_SCRIPTS_PRESENT)
        ):
            add_fact(FactKey.GATE_SCRIPTS_PRESENT)
    for fact in MODE_FORBIDDEN[mode]:
        if facts.claim(fact):
            add_forbidden(fact)
    return PreflightReport(
        mode=mode,
        blockers=tuple(blockers),
        warnings=_preflight_warnings(mode, facts),
    )


# --------------------------------------------------------------------------- #
# Owner confirmation, gate policy and durable mode record
# --------------------------------------------------------------------------- #


class ModeSelector(StrEnum):
    """Who proposes the mode."""

    OWNER = "owner"
    ORCHESTRATOR = "orchestrator"


class TaskGatePolicy(StrEnum):
    """The per-Task gate policy for one entry."""

    REQUIRED = "required"
    OWNER_DISABLED = "owner_disabled"


#: Warning shown on the accept checklist when a Task runs without a gate.
GATE_DISABLED_WARNING: Final[str] = (
    "this Task runs with the owner-disabled gate setting: no gate evidence will be "
    "produced, the candidate is never pushed, and the accept checklist must show "
    "this warning"
)

_ORCHESTRATOR_STATE_WRITE_HINT: Final[str] = (
    "state-writing mode {mode} requires explicit owner confirmation"
)


def requires_owner_confirmation(mode: ProjectEntryMode) -> bool:
    """Whether entering ``mode`` needs explicit owner confirmation."""
    return mode in MODE_WRITES_STATE


@dataclass(frozen=True)
class ModeActivation:
    """One proposed entry into a mode, with its authority inputs."""

    mode: ProjectEntryMode
    selected_by: ModeSelector = ModeSelector.OWNER
    confirmed_by_owner: bool = False
    gate_policy: TaskGatePolicy = TaskGatePolicy.REQUIRED


@dataclass(frozen=True)
class ActivationReport:
    """The outcome of checking one activation: allowed, blockers, warnings."""

    activation: ModeActivation
    preflight: PreflightReport
    warnings: tuple[str, ...]
    accept_checklist: tuple[str, ...]

    @property
    def allowed(self) -> bool:
        """Whether every blocking condition is cleared."""
        return self.preflight.ok


def _check_authority(activation: ModeActivation) -> None:
    """Refuse authority the caller does not hold, before any state is touched."""
    if activation.gate_policy is TaskGatePolicy.OWNER_DISABLED:
        if activation.mode is not ProjectEntryMode.TASK:
            raise ModeAuthorityError(
                "the gate-disabled setting applies only to a Task, not to "
                f"{activation.mode.value}"
            )
        if activation.selected_by is not ModeSelector.OWNER:
            raise ModeAuthorityError(
                "the Orchestrator can never choose a gate-disabled Task; only the "
                "owner may disable the gate"
            )
    if requires_owner_confirmation(activation.mode) and not activation.confirmed_by_owner:
        raise ModeConfirmationError(
            _ORCHESTRATOR_STATE_WRITE_HINT.format(mode=activation.mode.value)
        )


def _accept_checklist(activation: ModeActivation) -> tuple[str, ...]:
    """Return the accept-checklist lines this activation adds."""
    if activation.gate_policy is TaskGatePolicy.OWNER_DISABLED:
        return (GATE_DISABLED_WARNING,)
    return ()


def check_activation(
    activation: ModeActivation,
    facts: ProjectModeFacts,
    *,
    gate_required: bool = True,
) -> ActivationReport:
    """Check one activation: authority first, then the mode's preflight.

    Authority violations (a state-writing mode without owner confirmation, a
    gate-disabled Task chosen by the Orchestrator, a gate-disabled non-Task)
    raise; they are never reported as a soft blocker. Environment conditions
    are reported as blockers so the caller can show the exact fix command.
    """
    _check_authority(activation)
    effective_gate = (
        gate_required and activation.gate_policy is not TaskGatePolicy.OWNER_DISABLED
    )
    report = preflight(activation.mode, facts, gate_required=effective_gate)
    warnings = list(report.warnings)
    if activation.gate_policy is TaskGatePolicy.OWNER_DISABLED:
        warnings.append(GATE_DISABLED_WARNING)
    return ActivationReport(
        activation=activation,
        preflight=report,
        warnings=tuple(warnings),
        accept_checklist=_accept_checklist(activation),
    )


@dataclass(frozen=True)
class ModeRecord:
    """The durable per-project mode decision (owner-reclassifiable at accept)."""

    project_id: str
    mode: ProjectEntryMode
    recorded_at: str
    confirmed_by_owner: bool = False
    gate_policy: TaskGatePolicy = TaskGatePolicy.REQUIRED
    revision: int = 1
    reclassified_by_owner: bool = False

    def as_dict(self) -> dict[str, object]:
        """Serialize deterministically for durable storage."""
        return {
            "schema_version": MODE_RECORD_SCHEMA_VERSION,
            "project_id": self.project_id,
            "mode": self.mode.value,
            "recorded_at": self.recorded_at,
            "confirmed_by_owner": self.confirmed_by_owner,
            "gate_policy": self.gate_policy.value,
            "revision": self.revision,
            "reclassified_by_owner": self.reclassified_by_owner,
        }

    @classmethod
    def from_dict(cls, data: object) -> ModeRecord:
        """Reconstruct from storage, validating every field fail-closed."""
        if not isinstance(data, dict):
            raise ModeRecordError("stored mode record must be a mapping")
        schema_version = data.get("schema_version", MODE_RECORD_SCHEMA_VERSION)
        if schema_version != MODE_RECORD_SCHEMA_VERSION:
            raise ModeRecordError(
                f"unsupported mode record schema version {schema_version!r}"
            )
        project_id = data.get("project_id")
        recorded_at = data.get("recorded_at")
        if not isinstance(project_id, str) or not project_id.strip():
            raise ModeRecordError("stored mode record has no project_id")
        if not isinstance(recorded_at, str) or not recorded_at.strip():
            raise ModeRecordError("stored mode record has no recorded_at stamp")
        try:
            mode = ProjectEntryMode(str(data.get("mode")))
            gate_policy = TaskGatePolicy(str(data.get("gate_policy", TaskGatePolicy.REQUIRED)))
        except ValueError as exc:
            raise ModeRecordError(f"stored mode record is invalid: {exc}") from exc
        revision = data.get("revision", 1)
        if type(revision) is not int or revision < 1:
            raise ModeRecordError("stored mode record revision must be a positive integer")
        confirmed = data.get("confirmed_by_owner", False)
        reclassified = data.get("reclassified_by_owner", False)
        if type(confirmed) is not bool or type(reclassified) is not bool:
            raise ModeRecordError("stored mode record flags must be booleans")
        return cls(
            project_id=project_id.strip(),
            mode=mode,
            recorded_at=recorded_at.strip(),
            confirmed_by_owner=confirmed,
            gate_policy=gate_policy,
            revision=revision,
            reclassified_by_owner=reclassified,
        )


def mode_record_path(state_dir: Path | str) -> Path:
    """Return the on-disk path of the per-project mode record."""
    return Path(state_dir) / MODE_RECORD_FILENAME


def save_mode_record(state_dir: Path | str, record: ModeRecord) -> Path:
    """Atomically persist a mode record, refusing unconfirmed state writes."""
    if requires_owner_confirmation(record.mode) and not record.confirmed_by_owner:
        raise ModeConfirmationError(
            _ORCHESTRATOR_STATE_WRITE_HINT.format(mode=record.mode.value)
        )
    path = mode_record_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(json.dumps(record.as_dict(), indent=2, sort_keys=True) + "\n", "utf-8")
    os.replace(tmp_path, path)
    return path


def load_mode_record(state_dir: Path | str) -> ModeRecord | None:
    """Load the persisted mode record; ``None`` when none was ever written."""
    path = mode_record_path(state_dir)
    if not path.is_file():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ModeRecordError(f"unreadable mode record {path}: {exc}") from exc
    return ModeRecord.from_dict(data)


def reclassify_mode(
    state_dir: Path | str,
    *,
    mode: ProjectEntryMode,
    confirmed_by_owner: bool,
    recorded_at: str,
    gate_policy: TaskGatePolicy | None = None,
) -> ModeRecord:
    """Reclassify the recorded mode, owner-confirmed (e.g. at accept time).

    The previous record must exist; reclassification never invents history.
    The owner-confirmation requirement is enforced here as well as in
    :func:`save_mode_record`, so the accept-time reclassification cannot be
    performed silently. An omitted ``gate_policy`` keeps the recorded policy.
    """
    if not confirmed_by_owner:
        raise ModeConfirmationError(
            "reclassifying a project mode at accept requires explicit owner confirmation"
        )
    current = load_mode_record(state_dir)
    if current is None:
        raise ModeRecordError(
            "no mode record exists to reclassify; record the initial entry mode first"
        )
    updated = ModeRecord(
        project_id=current.project_id,
        mode=mode,
        recorded_at=recorded_at,
        confirmed_by_owner=True,
        gate_policy=gate_policy if gate_policy is not None else current.gate_policy,
        revision=current.revision + 1,
        reclassified_by_owner=True,
    )
    save_mode_record(state_dir, updated)
    return updated


# --------------------------------------------------------------------------- #
# ROADMAP.md export (an export of the Step 015 plan store)
# --------------------------------------------------------------------------- #

ROADMAP_HEADER: Final[str] = (
    "<!-- generated by SaberOps from the plan store; do not hand-edit -->"
)


def _escape_cell(value: str) -> str:
    return value.replace("|", "\\|").replace("\n", " ").strip()


def _dependency_cell(task: PlanTaskDef) -> str:
    if not task.start_dependencies:
        return "—"
    return ", ".join(f"`{_escape_cell(edge)}`" for edge in task.start_dependencies)


def render_roadmap_markdown(
    revision: PlanRevision,
    *,
    title: str | None = None,
) -> str:
    """Render one hash-verified plan revision as the repository ``ROADMAP.md``.

    Deterministic: the only stamp is the revision's own ``created_at``, never
    the current clock. Phases appear in the order they first appear in the
    revision; tasks keep their stored order, so the export round-trips.
    """
    heading = (title or "ROADMAP").strip() or "ROADMAP"
    lines = [
        ROADMAP_HEADER,
        f"# {heading}",
        "",
        f"Project: `{revision.project_id}`",
        f"Plan revision: `{revision.version}` (sha256 `{revision.sha256[:12]}`)",
        f"Plan created: {revision.created_at}",
        "",
        "This file is an export of the SaberOps plan store (Step 015 / PL-1). "
        "Edit the plan revision, not this file.",
        "",
    ]
    phase_order: list[str] = []
    by_phase: dict[str, list[PlanTaskDef]] = {}
    for task in revision.tasks:
        phase = task.phase.strip() or "unassigned"
        if phase not in by_phase:
            by_phase[phase] = []
            phase_order.append(phase)
        by_phase[phase].append(task)
    if not phase_order:
        lines.append("_No tasks in the active plan revision._")
        return "\n".join(lines) + "\n"
    for phase in phase_order:
        lines.append(f"## Phase {_escape_cell(phase)}")
        lines.append("")
        lines.append("| Task | Title | Depends on |")
        lines.append("| --- | --- | --- |")
        for task in by_phase[phase]:
            task_id = _escape_cell(task.task_id)
            task_title = _escape_cell(task.title) or "—"
            lines.append(
                f"| `{task_id}` | {task_title} | {_dependency_cell(task)} |"
            )
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def export_roadmap_markdown(store: PlanStore, project_id: str) -> str:
    """Render the project's active revision as ROADMAP.md content."""
    revision = store.load_active(project_id)
    return render_roadmap_markdown(revision)


def _roadmap_destination(destination: Path | str) -> Path:
    """Resolve a directory-or-file roadmap destination to its file path."""
    path = Path(destination)
    if path.is_dir() or (not path.suffix and path.name != ROADMAP_FILENAME):
        return path / ROADMAP_FILENAME
    return path


def write_roadmap_export(destination: Path | str, revision: PlanRevision) -> Path:
    """Write the rendered roadmap atomically to ``destination``.

    ``destination`` is either the directory that will contain ``ROADMAP.md``
    or an explicit file path.
    """
    path = _roadmap_destination(destination)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    tmp_path.write_text(render_roadmap_markdown(revision), "utf-8")
    os.replace(tmp_path, path)
    return path


def roadmap_export_matches(destination: Path | str, revision: PlanRevision) -> bool:
    """Whether the file at ``destination`` is byte-identical to the export."""
    path = _roadmap_destination(destination)
    try:
        current = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return current == render_roadmap_markdown(revision)


# --------------------------------------------------------------------------- #
# Continue summary (consumes the OM-1 startup digest when it exists)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class RecentRun:
    """One recent run row of the startup digest."""

    run_id: str
    status: str
    accepted: bool = False


@dataclass(frozen=True)
class OpenBacklogItem:
    """One open backlog entry of the startup digest."""

    title: str
    detail: str = ""


@dataclass(frozen=True)
class StartupDigest:
    """The structural subset of the OM-1 startup digest this module consumes.

    Step 022 (OM-1) owns the digest; declaring the shape here keeps Continue
    mode implementable and testable today without importing or guessing at a
    module that does not exist yet.
    """

    recent_runs: tuple[RecentRun, ...] = ()
    open_backlog: tuple[OpenBacklogItem, ...] = ()


@dataclass(frozen=True)
class RoadmapItem:
    """One roadmap item projected from the active plan revision for display."""

    task_id: str
    title: str
    phase: str
    start_dependencies: tuple[str, ...] = ()


def next_roadmap_item(revision: PlanRevision) -> RoadmapItem | None:
    """Return the export ordering hint for the next roadmap item.

    Dependency-free tasks (no same-project hard START edge) come first, then
    the remaining tasks; within each group the smallest ``(phase, task_id)``
    wins. This is a deterministic display order for the roadmap export only —
    readiness stays with :func:`saberops.ready_frontier.project_ready_frontier`.
    """
    if not revision.tasks:
        return None

    def to_item(task: PlanTaskDef) -> RoadmapItem:
        return RoadmapItem(
            task_id=task.task_id,
            title=task.title,
            phase=task.phase,
            start_dependencies=tuple(task.start_dependencies),
        )

    def key(task: PlanTaskDef) -> tuple[int, str, str]:
        local_edges = tuple(edge for edge in task.start_dependencies if "/" not in edge)
        return (1 if local_edges else 0, task.phase, task.task_id)

    return to_item(min(revision.tasks, key=key))


@dataclass(frozen=True)
class ContinueSummary:
    """What Continue mode shows: digest, backlog and the next roadmap item."""

    project_id: str
    plan_version: str
    next_item: RoadmapItem | None
    recent_runs: tuple[RecentRun, ...]
    open_backlog: tuple[OpenBacklogItem, ...]

    def render(self) -> str:
        """Render the summary as deterministic plain text."""
        lines = [
            f"Continue SaberOps project {self.project_id}",
            f"Active plan revision: {self.plan_version}",
        ]
        if self.next_item is None:
            lines.append("Next roadmap item: none (the active plan has no tasks)")
        else:
            lines.append(
                f"Next roadmap item: {self.next_item.task_id} "
                f"[{self.next_item.phase or 'unassigned'}] {self.next_item.title}"
            )
        lines.append(f"Recent runs: {len(self.recent_runs)}")
        for run in self.recent_runs:
            marker = "accepted" if run.accepted else run.status
            lines.append(f"  - {run.run_id}: {marker}")
        lines.append(f"Open backlog: {len(self.open_backlog)}")
        for item in self.open_backlog:
            suffix = f" — {item.detail}" if item.detail else ""
            lines.append(f"  - {item.title}{suffix}")
        return "\n".join(lines)


def build_continue_summary(
    facts: ProjectModeFacts,
    revision: PlanRevision,
    digest: StartupDigest,
) -> ContinueSummary:
    """Build the Continue summary; require Continue mode to actually be allowed."""
    if not mode_qualified(facts, ProjectEntryMode.CONTINUE_SABEROPS):
        raise ModeValidationError(
            "Continue SaberOps mode is not allowed by the observed facts: "
            f"allowed: {[entry.value for entry in allowed_modes(facts)]}"
        )
    if not facts.state.registered_project_ids:
        raise ModeValidationError("Continue SaberOps mode needs a registered project")
    if len(facts.state.registered_project_ids) > 1:
        raise ModeValidationError(
            "Continue SaberOps mode is ambiguous: "
            f"{len(facts.state.registered_project_ids)} projects claim this repository"
        )
    return ContinueSummary(
        project_id=facts.state.registered_project_ids[0],
        plan_version=revision.version,
        next_item=next_roadmap_item(revision),
        recent_runs=tuple(digest.recent_runs),
        open_backlog=tuple(digest.open_backlog),
    )
