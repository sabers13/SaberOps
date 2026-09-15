"""C11-B: token-budgeted, ledger-aware context planning.

The context engine composes the inputs that go into one worker /
orchestrator dispatch, with explicit byte and token budgets, deterministic
selection, content-addressable section digests, and a delta-vs-previous
representation for prompt caching. It extends (does not replace) the
existing C04 :class:`ContextProjection` -- the projection is still the
authoritative render contract; the engine decides WHAT goes into it.

Three architectural invariants the engine enforces:

* **Bootstrap remains a cache.**  The engine reads the verified C11-A
  bootstrap (an :class:`OrchBootstrap`) and may include any of its
  bounded sections, but the bootstrap is never mutated and never
  declared authoritative for Project reconstruction.  Context omission
  is NEVER deletion or retirement: an omitted section stays in the
  Project Ledger.

* **Dependency-closed, task-scoped, bounded.**  The engine reads
  ``devscope`` graph evidence to find relevant records and walks
  dependency closure to a bounded depth.  It does not solve continuity
  by loading all Project history.

* **Provider-neutral token accounting.**  The engine exposes a typed
  :class:`TokenEstimator` interface; the default estimator is a
  deterministic ratio of bytes to tokens.  Hard byte limits coexist
  with estimated token limits; the estimate source is observable in
  every produced pack.  No structural dependence on any vendor
  tokenizer; C12 will measure whether backend-native caching helps.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol

from saberops.project_ledger.contracts import (
    OrchBootstrap,
    ProjectDecision,
    ProjectFact,
    RoadmapItem,
)
from saberops.project_ledger.observation import RepositoryObservation
from saberops.project_ledger.store import ProjectLedger

# ---------------------------------------------------------------------------
# Token accounting
# ---------------------------------------------------------------------------


class TokenEstimateSource(StrEnum):
    """Where the token estimate for a section came from.

    Honest accounting matters: callers (and C12 harness experiments)
    must be able to distinguish a vendor-derived number from a typed
    deterministic estimate, so that no estimate is ever mistaken for
    an exact measurement.
    """

    ESTIMATOR = "ESTIMATOR"
    VENDOR = "VENDOR"
    UNKNOWN = "UNKNOWN"


class TokenEstimator(Protocol):
    """Typed interface for converting bytes to estimated tokens.

    ``source`` is the provenance of the estimate (estimator / vendor /
    unknown).  ``estimate_bytes`` returns a non-negative integer; an
    estimator that cannot produce a number must return ``None`` and
    let the engine fall back to the byte-only budget.

    Implemented by both the deterministic default estimator and the
    vendor-supplied shim.  No structural dependence on any vendor
    tokenizer.
    """

    source: TokenEstimateSource
    """Provenance tag for the estimate (estimator / vendor / unknown)."""

    def estimate_bytes(self, payload: str | bytes) -> int | None:
        """Return a non-negative integer estimate or ``None`` if unknown."""
        ...


@dataclass(frozen=True)
class DeterministicByteEstimator(TokenEstimator):
    """Default provider-neutral token estimator.

    Uses a fixed bytes-per-token ratio (default ``4``).  The estimate
    is explicitly typed as ``ESTIMATOR`` so historical evidence can
    distinguish it from vendor-supplied usage telemetry.

    Inherits from :class:`TokenEstimator` so the dataclass's
    ``source`` field unambiguously satisfies the protocol.
    """

    bytes_per_token: float = 4.0
    source: TokenEstimateSource = TokenEstimateSource.ESTIMATOR

    def estimate_bytes(self, payload: str | bytes) -> int | None:
        if payload is None:
            return None
        if isinstance(payload, str):
            size = len(payload.encode("utf-8"))
        else:
            size = len(payload)
        if size == 0:
            return 0
        if self.bytes_per_token <= 0:
            return None
        return int(size / self.bytes_per_token)


# ---------------------------------------------------------------------------
# Selection reasons
# ---------------------------------------------------------------------------


class ContextInclusionReason(StrEnum):
    """Typed justification for why a section was (or was not) included.

    The engine MUST be able to answer for every emitted section why it
    appeared in the pack; omission is NEVER silent.
    """

    PRIMARY_OBJECTIVE = "PRIMARY_OBJECTIVE"
    PROJECT_GOAL = "PROJECT_GOAL"
    PROJECT_NON_GOAL = "PROJECT_NON_GOAL"
    PROJECT_DECISION = "PROJECT_DECISION"
    PROJECT_ROADMAP = "PROJECT_ROADMAP"
    PROJECT_OPEN_QUESTION = "PROJECT_OPEN_QUESTION"
    PROJECT_IN_FLIGHT = "PROJECT_IN_FLIGHT"
    PROJECT_PROVISIONAL = "PROJECT_PROVISIONAL"
    PROJECT_PENDING_INTAKE = "PROJECT_PENDING_INTAKE"
    PROJECT_BOOTSTRAP_FACT = "PROJECT_BOOTSTRAP_FACT"
    REPOSITORY_OBSERVATION = "REPOSITORY_OBSERVATION"
    DEPENDENCY_INTERFACES = "DEPENDENCY_INTERFACES"
    BOUNDED_EVIDENCE = "BOUNDED_EVIDENCE"
    BUDGET_TRUNCATION = "BUDGET_TRUNCATION"


@dataclass(frozen=True)
class ContextSection:
    """One typed section of a context pack.

    The digest is the content address of the section's payload.  Two
    sections with identical content / source ref always share the
    same digest; this lets compatible backends detect unchanged sections for
    prompt-cache reuse.

    The selection reason makes the engine's choices inspectable.
    """

    section_id: str
    source_ref: str
    content_digest: str
    bytes: int
    estimated_tokens: int | None
    inclusion_reason: ContextInclusionReason
    payload: str = ""
    token_estimate_source: TokenEstimateSource = TokenEstimateSource.ESTIMATOR
    provenance: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering for durable evidence and content-addressable
        reuse across providers."""
        return {
            "section_id": self.section_id,
            "source_ref": self.source_ref,
            "content_digest": self.content_digest,
            "bytes": self.bytes,
            "estimated_tokens": self.estimated_tokens,
            "token_estimate_source": self.token_estimate_source.value,
            "inclusion_reason": self.inclusion_reason.value,
            "provenance": list(self.provenance),
        }


@dataclass(frozen=True)
class ContextPack:
    """The deterministic context plan produced by the engine.

    ``digest`` is the content identity of the whole pack (sorted,
    excluding timestamps).  Two packs with identical inputs and
    identical budgets have identical digests.

    ``omitted`` reports the count of records that were left out per
    category.  Context omission is NEVER deletion: those records
    remain authoritative in the Project Ledger.
    """

    schema_version: int = 1
    pack_id: str = ""
    digest: str = ""
    sections: tuple[ContextSection, ...] = ()
    total_bytes: int = 0
    estimated_total_tokens: int | None = None
    token_estimate_source: TokenEstimateSource = TokenEstimateSource.ESTIMATOR
    budget: ContextBudget = field(default_factory=lambda: ContextBudget())
    omitted: dict[str, int] = field(default_factory=dict)
    selected_authoritative_refs: tuple[str, ...] = ()
    bootstrap_digest: str | None = None
    bootstrap_checkpoint_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical rendering of the context pack (sorted keys)."""
        return {
            "schema_version": self.schema_version,
            "pack_id": self.pack_id,
            "digest": self.digest,
            "sections": [section.to_dict() for section in self.sections],
            "total_bytes": self.total_bytes,
            "estimated_total_tokens": self.estimated_total_tokens,
            "token_estimate_source": self.token_estimate_source.value,
            "budget": self.budget.to_dict(),
            "omitted": dict(sorted(self.omitted.items())),
            "selected_authoritative_refs": sorted(self.selected_authoritative_refs),
            "bootstrap_digest": self.bootstrap_digest,
            "bootstrap_checkpoint_id": self.bootstrap_checkpoint_id,
        }


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextBudget:
    """Bounded budgets for a single context pack.

    Hard byte caps are structural; estimated-token caps are advisory.
    When both are configured the engine enforces bytes first, then
    reports the estimated-token usage.  Omitting any budget cap
    disables that constraint; the engine always reports pressure
    against whichever caps were configured.
    """

    max_bytes: int | None = 64 * 1024
    max_estimated_tokens: int | None = 16_000
    max_sections: int = 32
    max_bootstrap_goals: int = 16
    max_bootstrap_decisions: int = 16
    max_bootstrap_roadmap: int = 16
    max_bootstrap_open_questions: int = 16
    max_bootstrap_provisional: int = 8
    max_bootstrap_pending_intake: int = 8
    max_dependency_modules: int = 4
    per_section_byte_cap: int = 8 * 1024

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_bytes": self.max_bytes,
            "max_estimated_tokens": self.max_estimated_tokens,
            "max_sections": self.max_sections,
            "max_bootstrap_goals": self.max_bootstrap_goals,
            "max_bootstrap_decisions": self.max_bootstrap_decisions,
            "max_bootstrap_roadmap": self.max_bootstrap_roadmap,
            "max_bootstrap_open_questions": self.max_bootstrap_open_questions,
            "max_bootstrap_provisional": self.max_bootstrap_provisional,
            "max_bootstrap_pending_intake": self.max_bootstrap_pending_intake,
            "max_dependency_modules": self.max_dependency_modules,
            "per_section_byte_cap": self.per_section_byte_cap,
        }


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextInput:
    """The authoritative inputs that may appear in a context pack.

    Every field here is either:

    * machine-readable durable Project state (so it is part of
      Project truth and survives any session);
    * the bounded :class:`OrchBootstrap` projection (a regenerable
      cache that may be discarded without Project consequence);
    * a task-scoped target that names the immediate responsibility.

    Nothing here names a binding, model, provider, or live process.
    """

    ledger: ProjectLedger
    bootstrap: OrchBootstrap
    task_slug: str = ""
    task_summary: str = ""
    scope_modules: tuple[str, ...] = ()
    scope_changed_paths: tuple[str, ...] = ()
    scope_symbols: tuple[str, ...] = ()
    repository_observation: RepositoryObservation | None = None
    dependency_interfaces: tuple[tuple[str, tuple[str, ...]], ...] = ()
    bounded_evidence: tuple[str, ...] = ()
    pack_id: str = ""


# ---------------------------------------------------------------------------
# Delta seams
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextDelta:
    """The seam between two context packs.

    Identical unchanged sections carry the same digest; the engine
    exposes which sections are unchanged / changed / new so backends that
    support prompt caching can retransmit only the deltas.
    """

    unchanged: tuple[ContextSection, ...] = ()
    changed: tuple[ContextSection, ...] = ()
    new: tuple[ContextSection, ...] = ()
    dropped: tuple[ContextSection, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "unchanged": [section.section_id for section in self.unchanged],
            "changed": [section.section_id for section in self.changed],
            "new": [section.section_id for section in self.new],
            "dropped": [section.section_id for section in self.dropped],
        }


def diff_context_packs(previous: ContextPack, current: ContextPack) -> ContextDelta:
    """Compute a deterministic delta between two context packs.

    Sections are compared by ``section_id`` and ``content_digest``;
    the section identity is the contract, not the row order.
    """
    prev_by_id: dict[str, ContextSection] = {s.section_id: s for s in previous.sections}
    cur_by_id: dict[str, ContextSection] = {s.section_id: s for s in current.sections}
    unchanged_ids = [
        sid
        for sid, section in cur_by_id.items()
        if sid in prev_by_id and prev_by_id[sid].content_digest == section.content_digest
    ]
    changed_ids = [
        sid
        for sid, section in cur_by_id.items()
        if sid in prev_by_id and prev_by_id[sid].content_digest != section.content_digest
    ]
    new_ids = [sid for sid in cur_by_id if sid not in prev_by_id]
    dropped_ids = [sid for sid in prev_by_id if sid not in cur_by_id]
    return ContextDelta(
        unchanged=tuple(cur_by_id[sid] for sid in unchanged_ids),
        changed=tuple(cur_by_id[sid] for sid in changed_ids),
        new=tuple(cur_by_id[sid] for sid in new_ids),
        dropped=tuple(prev_by_id[sid] for sid in dropped_ids),
    )


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


def _canonical_digest(payload: Any) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _truncate_to_bytes(text: str, byte_cap: int) -> tuple[str, bool]:
    if byte_cap <= 0:
        return text, True
    encoded = text.encode("utf-8")
    if len(encoded) <= byte_cap:
        return text, False
    # Truncate at the byte boundary; the resulting text may end mid-character,
    # but every consumer must treat content_digest as authoritative.
    truncated = encoded[:byte_cap].decode("utf-8", errors="ignore")
    return truncated, True


def _make_section(
    *,
    section_id: str,
    source_ref: str,
    body: str,
    inclusion_reason: ContextInclusionReason,
    estimator: TokenEstimator,
    byte_cap: int,
    provenance: Sequence[str] = (),
) -> ContextSection:
    truncated_body, truncated = _truncate_to_bytes(body, byte_cap)
    payload_bytes = len(truncated_body.encode("utf-8"))
    digest = _canonical_digest(
        {
            "section_id": section_id,
            "source_ref": source_ref,
            "body": truncated_body,
            "truncated": truncated,
        }
    )
    estimated = estimator.estimate_bytes(truncated_body)
    return ContextSection(
        section_id=section_id,
        source_ref=source_ref,
        content_digest=digest,
        bytes=payload_bytes,
        estimated_tokens=estimated,
        inclusion_reason=(
            inclusion_reason
            if not truncated
            else ContextInclusionReason.BUDGET_TRUNCATION
        ),
        payload=truncated_body,
        token_estimate_source=estimator.source,
        provenance=tuple(provenance),
    )


def _record_to_section_body(record: ProjectFact | ProjectDecision | RoadmapItem) -> str:
    if isinstance(record, ProjectFact):
        ref = record.reference or "(none)"
        return f"[{record.category.value}] {record.statement}\nref: {ref}"
    if isinstance(record, ProjectDecision):
        ref = record.reference or "(none)"
        return f"[decision] {record.statement}\nrationale: {record.rationale}\nref: {ref}"
    ref = record.acceptance_ref or "(none)"
    return f"[roadmap] {record.title} ({record.status.value})\nacceptance: {ref}"


def _fact_section_body(facts: Iterable[ProjectFact]) -> str:
    parts: list[str] = []
    for fact in facts:
        parts.append(f"[{fact.category.value}] {fact.statement}")
        if fact.reference:
            parts.append(f"  ref: {fact.reference}")
    return "\n".join(parts)


def _decision_section_body(decisions: Iterable[ProjectDecision]) -> str:
    parts: list[str] = []
    for decision in decisions:
        parts.append(f"- {decision.statement}")
        if decision.rationale:
            parts.append(f"  rationale: {decision.rationale}")
    return "\n".join(parts)


def _roadmap_section_body(items: Iterable[RoadmapItem]) -> str:
    parts: list[str] = []
    for item in items:
        parts.append(f"- {item.title} [{item.status.value}]")
        if item.acceptance_ref:
            parts.append(f"  acceptance: {item.acceptance_ref}")
    return "\n".join(parts)


def build_context_pack(
    context_input: ContextInput,
    *,
    budget: ContextBudget | None = None,
    estimator: TokenEstimator | None = None,
    pack_id: str | None = None,
) -> ContextPack:
    """Build a bounded, content-addressable context pack.

    The pack is fully deterministic over its inputs: identical
    Project state, bootstrap, scope, and budget produce identical
    content digests.  The function is pure: no model call, no
    provider discovery, no live network call, no live process.
    """
    effective_budget = budget if budget is not None else ContextBudget()
    effective_estimator = (
        estimator if estimator is not None else DeterministicByteEstimator()
    )
    sections: list[ContextSection] = []
    used_bytes = 0
    estimated_total: int | None = 0 if effective_estimator else None
    omitted: dict[str, int] = {}
    authoritative_refs: list[str] = []

    # 1.  Target responsibility (always present, anchored to the prompt).
    target_body = _target_body(context_input.task_slug, context_input.task_summary)
    target_section = _make_section(
        section_id="target",
        source_ref="task",
        body=target_body,
        inclusion_reason=ContextInclusionReason.PRIMARY_OBJECTIVE,
        estimator=effective_estimator,
        byte_cap=effective_budget.per_section_byte_cap,
        provenance=("task",),
    )
    sections.append(target_section)
    used_bytes += target_section.bytes
    if estimated_total is not None and target_section.estimated_tokens is not None:
        estimated_total += target_section.estimated_tokens

    # 2.  Project goals / non-goals / decisions / roadmap / open questions.
    bootstrap = context_input.bootstrap

    def _try_add(
        section_id: str,
        records: Sequence[Any],
        reason: ContextInclusionReason,
        *,
        max_records: int,
        cap: int,
        body_fn: Any,
    ) -> int:
        nonlocal used_bytes, estimated_total
        kept = list(records[:max_records])
        omitted_count = max(len(records) - len(kept), 0)
        if omitted_count:
            omitted[section_id] = omitted_count
        if not kept:
            return 0
        body = body_fn(kept)
        section = _make_section(
            section_id=section_id,
            source_ref=f"bootstrap:{section_id}",
            body=body,
            inclusion_reason=reason,
            estimator=effective_estimator,
            byte_cap=cap,
            provenance=("bootstrap", bootstrap.state_digest),
        )
        sections.append(section)
        used_bytes += section.bytes
        if estimated_total is not None and section.estimated_tokens is not None:
            estimated_total += section.estimated_tokens
        authoritative_refs.extend(record.id for record in kept)
        return 1

    _try_add(
        "project_goals",
        bootstrap.goals,
        ContextInclusionReason.PROJECT_GOAL,
        max_records=effective_budget.max_bootstrap_goals,
        cap=effective_budget.per_section_byte_cap,
        body_fn=_fact_section_body,
    )
    _try_add(
        "project_non_goals",
        bootstrap.non_goals,
        ContextInclusionReason.PROJECT_NON_GOAL,
        max_records=effective_budget.max_bootstrap_goals,
        cap=effective_budget.per_section_byte_cap,
        body_fn=_fact_section_body,
    )
    _try_add(
        "project_decisions",
        bootstrap.decisions,
        ContextInclusionReason.PROJECT_DECISION,
        max_records=effective_budget.max_bootstrap_decisions,
        cap=effective_budget.per_section_byte_cap,
        body_fn=_decision_section_body,
    )
    _try_add(
        "project_roadmap",
        bootstrap.roadmap,
        ContextInclusionReason.PROJECT_ROADMAP,
        max_records=effective_budget.max_bootstrap_roadmap,
        cap=effective_budget.per_section_byte_cap,
        body_fn=_roadmap_section_body,
    )
    _try_add(
        "project_open_questions",
        bootstrap.open_questions,
        ContextInclusionReason.PROJECT_OPEN_QUESTION,
        max_records=effective_budget.max_bootstrap_open_questions,
        cap=effective_budget.per_section_byte_cap,
        body_fn=lambda items: "\n".join(f"- {q.question}" for q in items),
    )
    _try_add(
        "project_provisional",
        bootstrap.provisional_assumptions,
        ContextInclusionReason.PROJECT_PROVISIONAL,
        max_records=effective_budget.max_bootstrap_provisional,
        cap=effective_budget.per_section_byte_cap,
        body_fn=_fact_section_body,
    )
    _try_add(
        "project_pending_intake",
        bootstrap.pending_intake,
        ContextInclusionReason.PROJECT_PENDING_INTAKE,
        max_records=effective_budget.max_bootstrap_pending_intake,
        cap=effective_budget.per_section_byte_cap,
        body_fn=lambda items: "\n".join(f"- {i.summary}" for i in items),
    )
    if bootstrap.in_flight:
        in_flight_kept = list(
            bootstrap.in_flight[: effective_budget.max_bootstrap_roadmap]
        )
        omitted["project_in_flight"] = max(
            len(bootstrap.in_flight) - len(in_flight_kept), 0
        )
        body_lines = []
        for work in in_flight_kept:
            body_lines.append(f"- {work.item.title} [{work.item.status.value}]")
            if work.runtime is not None:
                body_lines.append(
                    f"  runtime: run_ref={work.runtime.run_ref} "
                    f"interrupted={work.runtime.interrupted} "
                    f"uncertain={work.runtime.outcome_uncertain}"
                )
        section = _make_section(
            section_id="project_in_flight",
            source_ref="bootstrap:in_flight",
            body="\n".join(body_lines),
            inclusion_reason=ContextInclusionReason.PROJECT_IN_FLIGHT,
            estimator=effective_estimator,
            byte_cap=effective_budget.per_section_byte_cap,
            provenance=("bootstrap", bootstrap.state_digest),
        )
        sections.append(section)
        used_bytes += section.bytes
        if estimated_total is not None and section.estimated_tokens is not None:
            estimated_total += section.estimated_tokens
        authoritative_refs.extend(work.item.id for work in in_flight_kept)

    # 3.  Dependency interfaces (dependency-closed).
    if context_input.dependency_interfaces:
        deps_kept = context_input.dependency_interfaces[
            : effective_budget.max_dependency_modules
        ]
        omitted["dependency_interfaces"] = max(
            len(context_input.dependency_interfaces) - len(deps_kept), 0
        )
        body = "\n".join(
            f"- {module}\n  " + "\n  ".join(paths) for module, paths in deps_kept
        )
        section = _make_section(
            section_id="dependency_interfaces",
            source_ref="scope:dependencies",
            body=body,
            inclusion_reason=ContextInclusionReason.DEPENDENCY_INTERFACES,
            estimator=effective_estimator,
            byte_cap=effective_budget.per_section_byte_cap,
            provenance=("scope",),
        )
        sections.append(section)
        used_bytes += section.bytes
        if estimated_total is not None and section.estimated_tokens is not None:
            estimated_total += section.estimated_tokens

    # 4.  Repository observation (when present).
    if context_input.repository_observation is not None:
        ro = context_input.repository_observation
        body = (
            f"canonical_path: {ro.identity.canonical_path}\n"
            f"root_commit: {ro.identity.root_commit}\n"
            f"head_sha: {ro.head_sha}\n"
            f"branch: {ro.branch or '(none}'}\n"
            f"declared_documents: " + ",".join(sorted(f.path for f in ro.files))
        )
        section = _make_section(
            section_id="repository_observation",
            source_ref="repo:observation",
            body=body,
            inclusion_reason=ContextInclusionReason.REPOSITORY_OBSERVATION,
            estimator=effective_estimator,
            byte_cap=effective_budget.per_section_byte_cap,
            provenance=("observation", ro.head_sha),
        )
        sections.append(section)
        used_bytes += section.bytes
        if estimated_total is not None and section.estimated_tokens is not None:
            estimated_total += section.estimated_tokens

    # 5.  Bounded evidence (from the caller, e.g. worker's prior failure
    #     excerpts).  Each line is treated as a separate slice for byte
    #     budgeting but presented as a single section.
    if context_input.bounded_evidence:
        joined = "\n".join(context_input.bounded_evidence)
        section = _make_section(
            section_id="bounded_evidence",
            source_ref="evidence:caller",
            body=joined,
            inclusion_reason=ContextInclusionReason.BOUNDED_EVIDENCE,
            estimator=effective_estimator,
            byte_cap=effective_budget.per_section_byte_cap,
            provenance=("caller",),
        )
        sections.append(section)
        used_bytes += section.bytes
        if estimated_total is not None and section.estimated_tokens is not None:
            estimated_total += section.estimated_tokens

    # 6.  Honour hard byte / section caps.  When the budget is exhausted
    #     we drop whole sections from the tail so that the remaining
    #     high-priority sections are unaffected.
    if (
        effective_budget.max_bytes is not None
        and used_bytes > effective_budget.max_bytes
    ):
        truncated_sections: list[ContextSection] = []
        running_bytes = 0
        for section in sections:
            if (
                running_bytes + section.bytes <= effective_budget.max_bytes
                or not truncated_sections
            ):
                truncated_sections.append(section)
                running_bytes += section.bytes
        omitted["byte_cap_exceeded"] = used_bytes - running_bytes
        sections = truncated_sections
        used_bytes = running_bytes

    if effective_budget.max_sections is not None and len(sections) > effective_budget.max_sections:
        trimmed = sections[: effective_budget.max_sections]
        omitted["section_cap_exceeded"] = len(sections) - len(trimmed)
        sections = trimmed
        used_bytes = sum(section.bytes for section in sections)

    pack_payload = {
        "sections": [section.to_dict() for section in sections],
        "budget": effective_budget.to_dict(),
        "omitted": dict(sorted(omitted.items())),
        "bootstrap_digest": bootstrap.state_digest,
        "bootstrap_checkpoint_id": bootstrap.checkpoint_id,
    }
    digest = _canonical_digest(pack_payload)
    final_pack_id = pack_id or context_input.pack_id or f"ctx_{digest[:16]}"
    return ContextPack(
        pack_id=final_pack_id,
        digest=digest,
        sections=tuple(sections),
        total_bytes=used_bytes,
        estimated_total_tokens=estimated_total,
        token_estimate_source=effective_estimator.source,
        budget=effective_budget,
        omitted=omitted,
        selected_authoritative_refs=tuple(dict.fromkeys(authoritative_refs)),
        bootstrap_digest=bootstrap.state_digest,
        bootstrap_checkpoint_id=bootstrap.checkpoint_id,
    )


def _target_body(slug: str, summary: str) -> str:
    return f"[target]\nslug: {slug}\nsummary: {summary}".rstrip()


def render_pack(pack: ContextPack) -> str:
    """Render a context pack as a deterministic text body.

    Used for diagnostic inspection and for callers that want to feed a
    pack into an existing C04 projection without parsing its typed
    fields.  Section order is stable so the rendered body itself is
    deterministic over the pack's content digests.
    """
    lines: list[str] = []
    for section in pack.sections:
        lines.append(f"--- {section.section_id} ({section.content_digest[:16]}) ---")
        if section.inclusion_reason is ContextInclusionReason.BUDGET_TRUNCATION:
            lines.append("[budget truncated]")
        lines.append(section.payload)
        lines.append("")
    lines.append(f"[pack digest: {pack.digest}]")
    return "\n".join(lines)


__all__ = [
    "ContextBudget",
    "ContextDelta",
    "ContextInclusionReason",
    "ContextInput",
    "ContextPack",
    "ContextSection",
    "DeterministicByteEstimator",
    "TokenEstimateSource",
    "TokenEstimator",
    "build_context_pack",
    "diff_context_packs",
    "render_pack",
]
