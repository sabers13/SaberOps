"""The bounded bootstrap projection.

A bootstrap is what a fresh Orch reads instead of its predecessor's
transcript.  It is a *cache*: derived entirely from durable Project state,
regenerable at any time, and never itself authoritative.  Deleting every
bootstrap ever produced must cost the Project exactly nothing.

Boundedness is deliberate.  Continuity is not solved by pasting the Project
database into a prompt, so each typed category is selected by a deterministic
relevance order and capped by :class:`BootstrapBudget`.  Whatever the budget
excludes stays in the ledger, still authoritative, still reachable, and the
projection says exactly how much it left behind.  A later tranche can make the
budget token-aware without touching a durable schema.
"""

from __future__ import annotations

from collections.abc import Sequence

from orchestrator_mvp.db import Database
from orchestrator_mvp.project_ledger.checkpoint import (
    VerifiedCheckpoint,
    latest_verified_checkpoint,
)
from orchestrator_mvp.project_ledger.continuity import runtime_composition
from orchestrator_mvp.project_ledger.contracts import (
    BOOTSTRAP_SCHEMA_VERSION,
    BootstrapBudget,
    FactCategory,
    InFlightWork,
    OrchBootstrap,
    ProjectCheckpoint,
    ProjectDecision,
    ProjectFact,
    ProjectState,
    RoadmapStatus,
)
from orchestrator_mvp.project_ledger.store import ProjectLedger, load_project

#: Fact categories a bootstrap surfaces as goals, in that order.
_GOAL_CATEGORIES = (FactCategory.GOAL, FactCategory.CONSTRAINT)

#: Fact categories carried as bounded authoritative evidence references.
_EVIDENCE_CATEGORIES = (
    FactCategory.ARCHITECTURE,
    FactCategory.ENVIRONMENT,
    FactCategory.EVIDENCE,
    FactCategory.MILESTONE,
    FactCategory.OBSERVATION,
)


def _bounded[RecordT](records: Sequence[RecordT], limit: int) -> tuple[tuple[RecordT, ...], int]:
    """Take at most ``limit`` records, reporting how many were left behind."""
    if limit < 0:
        raise ValueError("bootstrap budget limits must not be negative")
    selected = tuple(records[:limit])
    return selected, max(len(records) - limit, 0)


def build_bootstrap(
    ledger: ProjectLedger,
    checkpoint: ProjectCheckpoint,
    *,
    budget: BootstrapBudget | None = None,
) -> OrchBootstrap:
    """Project durable Project state into a bounded continuation cache.

    Selection is deterministic: newest-first within each typed category, ties
    broken by identity, so the same state and budget always project to the
    same bootstrap.  No model is consulted about what matters.
    """
    resolved_budget = budget if budget is not None else BootstrapBudget()
    state = ledger.state
    omitted: dict[str, int] = {}

    goals, omitted["goals"] = _bounded(_goals(state), resolved_budget.max_facts)
    non_goals, omitted["non_goals"] = _bounded(
        state.accepted_facts(FactCategory.NON_GOAL), resolved_budget.max_non_goals
    )
    decisions, omitted["decisions"] = _bounded(
        _newest_first(state.accepted_decisions()), resolved_budget.max_decisions
    )
    roadmap, omitted["roadmap"] = _bounded(
        state.active_roadmap(), resolved_budget.max_roadmap_items
    )
    questions, omitted["open_questions"] = _bounded(
        state.open_question_records(), resolved_budget.max_open_questions
    )
    evidence, omitted["evidence"] = _bounded(_evidence(state), resolved_budget.max_facts)
    provisional, omitted["provisional_assumptions"] = _bounded(
        state.provisional_facts(), resolved_budget.max_provisional
    )
    intake, omitted["pending_intake"] = _bounded(
        state.pending_intake(), resolved_budget.max_intake
    )

    in_flight = tuple(
        InFlightWork(
            item=item,
            runtime=(
                None if not item.run_ref else runtime_composition(ledger.db, item.run_ref)
            ),
        )
        for item in state.in_flight_roadmap()
    )

    return OrchBootstrap(
        schema_version=BOOTSTRAP_SCHEMA_VERSION,
        project_id=state.project_id,
        canonical_path=state.identity.canonical_path,
        root_commit=state.identity.root_commit,
        project_version=state.project_version,
        checkpoint_id=checkpoint.checkpoint_id,
        checkpoint_digest=checkpoint.digest,
        state_digest=checkpoint.state_digest,
        goals=goals,
        non_goals=non_goals,
        decisions=decisions,
        roadmap=roadmap,
        open_questions=questions,
        in_flight=in_flight,
        evidence=evidence,
        provisional_assumptions=provisional,
        pending_intake=intake,
        continuation_instructions=_continuation_instructions(state, checkpoint, in_flight),
        budget=resolved_budget,
        omitted={key: value for key, value in omitted.items() if value},
    )


def _newest_first(records: Sequence[ProjectDecision]) -> tuple[ProjectDecision, ...]:
    return tuple(sorted(records, key=lambda r: (-r.introduced_version, r.id)))


def _goals(state: ProjectState) -> tuple[ProjectFact, ...]:
    selected = [fact for category in _GOAL_CATEGORIES for fact in state.accepted_facts(category)]
    return tuple(sorted(selected, key=lambda r: (-r.introduced_version, r.id)))


def _evidence(state: ProjectState) -> tuple[ProjectFact, ...]:
    selected = [
        fact for category in _EVIDENCE_CATEGORIES for fact in state.accepted_facts(category)
    ]
    return tuple(sorted(selected, key=lambda r: (-r.introduced_version, r.id)))


def _continuation_instructions(
    state: ProjectState,
    checkpoint: ProjectCheckpoint,
    in_flight: Sequence[InFlightWork],
) -> tuple[str, ...]:
    """Deterministic continuation guidance derived from durable state alone."""
    lines = [
        "Durable Project state is authoritative; this bootstrap is a regenerable "
        "projection of it and may be discarded at any time.",
        f"Continue project {state.project_id} from checkpoint "
        f"{checkpoint.checkpoint_id} at version {checkpoint.project_version}; "
        "verify its digest before acting on it.",
    ]
    for work in in_flight:
        if work.item.status is RoadmapStatus.INTERRUPTED:
            lines.append(
                f"Roadmap item {work.item.id} is INTERRUPTED: confirm the real "
                "repository and runtime state before claiming any part of it is done."
            )
        elif work.item.status is RoadmapStatus.OUTCOME_UNCERTAIN:
            lines.append(
                f"Roadmap item {work.item.id} has an UNCERTAIN outcome: its side effect "
                "was never confirmed, so treat it as neither completed nor failed."
            )
        else:
            lines.append(
                f"Roadmap item {work.item.id} is IN_PROGRESS: it was not observed to "
                "finish, so verify before continuing it."
            )
        if work.runtime is not None and work.runtime.outcome_uncertain:
            lines.append(
                f"Run {work.runtime.run_ref} has unconfirmed runtime effects; C05 "
                "run recovery owns that determination, not the Project Ledger."
            )
    provisional = state.provisional_facts()
    if provisional:
        lines.append(
            f"{len(provisional)} provisional assumption(s) are recorded; they are not "
            "Project truth and must be explicitly accepted before being relied upon."
        )
    pending = state.pending_intake()
    if pending:
        lines.append(
            f"{len(pending)} captured change(s) await disposition; capture is not "
            "acceptance and none of them is yet a requirement."
        )
    return tuple(lines)


def resume_project(
    db: Database,
    project_id: str,
    *,
    budget: BootstrapBudget | None = None,
) -> tuple[ProjectLedger, VerifiedCheckpoint, OrchBootstrap]:
    """Reconstruct a Project from durable state alone.

    This is the whole continuation seam: given only a store and a project
    identity, load the ledger, verify the newest checkpoint against durable
    state, and project a bounded bootstrap.  No predecessor session, process,
    transcript, provider or model participates.
    """
    ledger = load_project(db, project_id)
    verified = latest_verified_checkpoint(db, project_id)
    bootstrap = build_bootstrap(ledger, verified.checkpoint, budget=budget)
    return ledger, verified, bootstrap


__all__ = [
    "build_bootstrap",
    "resume_project",
]
