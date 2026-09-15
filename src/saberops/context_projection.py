"""C04 — bounded worker-context projection and surgical escalation capsule.

This module owns the deterministic context-projection contract that decides
what a worker actually sees for a given dispatch.  The contract is small,
typed, and cache-friendly; no model call is required.

Context modes
=============

* ``INITIAL``              — initial unsliced implementation; the full original
                              task is allowed once.
* ``PACKAGE``              — sliced implementation/repair of a single coherent
                              package; the raw full parent objective is NEVER
                              model-visible (it remains on the durable
                              ``WorkPackage`` row for audit/reconstruction).
* ``REPAIR``               — bounded corrective work on a committed candidate
                              (retry, repair, final-repair); the full original
                              task is NEVER model-visible.
* ``SURGICAL``             — surgical escalation capsule; a stronger/costlier
                              model receives ONLY the bounded residual defect,
                              not the original epic task.
* ``FRESH_REIMPLEMENTATION`` — exceptional explicit mode in which the full
                              original objective is shown again.  It must
                              represent an explicit, durable decision and must
                              never be selected silently by ordinary retry /
                              repair / escalation paths.

Worker-contract invariants
==========================

* Validation policy is appended exactly once and idempotently.
* Section ordering is stable across renders (cache-friendly prefix).
* No timestamps, UUIDs, or random attempt IDs appear in the prefix.
* The full original task never appears in REPAIR/SURGICAL/PACKAGE modes.

Determinism
===========

All renderers are pure functions of their inputs.  No model calls, no
network calls, no tokenizer dependencies, no embedding stores.
"""
# ruff: noqa: E501

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from saberops.models import DispatchRole
from saberops.tooling.contracts import FrozenToolContract

# ---------------------------------------------------------------------------
# Public configuration constants — small, explicit, deterministic budgets.
# ---------------------------------------------------------------------------

OBJECTIVE_SLUG_MAX_CHARS: int = 120

# Bounded objective digest kept small enough to never grow with conversation
# history.  Trimmed deterministically; no model summarization.
OBJECTIVE_DIGEST_MAX_CHARS: int = 2048

# Default truncation budgets for deterministic evidence.  These are
# implementation targets, not a budget framework.
EVIDENCE_BLOCK_MAX_CHARS: int = 2048  # ~2KB
REVIEW_BLOCK_MAX_CHARS: int = 3072  # ~3KB
STDERR_BLOCK_MAX_CHARS: int = 3072  # ~3KB
STDOUT_BLOCK_MAX_CHARS: int = 2048  # ~2KB

# Changed-path projection bound (typical small sliced/unsliced change set).
CHANGED_PATHS_MAX: int = 32

# Truncation marker shared by every deterministic prune call.
_TRUNCATION_MARKER = "\n...[truncated]...\n"


class DispatchContextMode(StrEnum):
    """Canonical C04/C05 context modes for a worker dispatch.

    The mode describes WHAT KIND of context the worker sees, not which tier /
    provider / model is selected.  Modes are explicitly durable and recorded
    on every dispatch (see ``ContextProjection.record_event_payload``).
    """

    INITIAL = "INITIAL"
    PACKAGE = "PACKAGE"
    REPAIR = "REPAIR"
    SURGICAL = "SURGICAL"
    CONTINUATION = "CONTINUATION"
    FRESH_REIMPLEMENTATION = "FRESH_REIMPLEMENTATION"


# Modes in which the raw full original task MUST NOT appear by default.
MODES_WITHOUT_FULL_TASK: frozenset[DispatchContextMode] = frozenset(
    {
        DispatchContextMode.PACKAGE,
        DispatchContextMode.REPAIR,
        DispatchContextMode.SURGICAL,
        DispatchContextMode.CONTINUATION,
    }
)


# ---------------------------------------------------------------------------
# Compact projections
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextProjection:
    """Bounded worker-context projection for one dispatch.

    The projection carries only what the worker contract needs:
    * a stable invariant prefix (no timestamps / random IDs);
    * bounded dynamic evidence;
    * a single ``validation_policy`` block appended idempotently by the
      renderer.

    C04-R1: the new ``source_original_task_bytes`` field captures the byte
    size of the durable source objective so durable evidence can answer
    "size of source objective" independently of "was the full source
    objective model-visible".
    """

    mode: DispatchContextMode
    role: str
    parent_slug: str
    inherited_candidate_sha: str
    dispatch_reason: str
    responsibility: str  # bounded responsibility / goal for this dispatch
    acceptance_criteria: tuple[str, ...] = ()
    invariants: tuple[str, ...] = ()
    changed_paths: tuple[str, ...] = ()
    diff_ref: str | None = None
    failure_excerpt: str | None = None
    review_excerpt: str | None = None
    full_parent_included: bool = False
    original_task: str | None = None  # present only in INITIAL / FRESH_REIMPLEMENTATION
    # C04-R1: source byte size of the parent objective for this dispatch.
    # Independent of whether the full source objective was model-visible.
    source_original_task_bytes: int = 0
    focused_verification: str = "Run focused tests and nearby regressions; return the candidate."
    extra_sections: tuple[tuple[str, str], ...] = ()  # (title, body)
    package_id: str | None = None

    def project_size_bytes(self) -> int:
        """Return approximate byte size of the projected prompt body."""
        return len(self.render_body().encode("utf-8"))

    def render_body(self) -> str:
        """Render the projection body (without the validation policy block).

        The order of sections is stable across renders; high-entropy dynamic
        fields appear only after the stable prefix so the worker-contract
        prefix remains cache-friendly.
        """
        sections: list[str] = []
        sections.append(self._header_section())
        sections.append(self._target_section())
        if self.acceptance_criteria:
            sections.append(self._acceptance_section())
        if self.changed_paths or self.diff_ref:
            sections.append(self._changes_section())
        if self.failure_excerpt:
            sections.append(self._failure_section())
        if self.review_excerpt:
            sections.append(self._review_section())
        if self.invariants:
            sections.append(self._invariants_section())
        if self.full_parent_included and self.original_task is not None:
            sections.append(self._original_task_section())
        if self.extra_sections:
            for title, body in self.extra_sections:
                if title and body:
                    sections.append(f"[{title}]\n{body}")
        sections.append(self._focused_verification_section())
        # Body only — the validation policy is appended by the renderer.
        return "\n\n".join(s for s in sections if s)

    # -- section helpers (each returns a non-empty string) -----------------

    def _header_section(self) -> str:
        return (
            "ORCH WORKER CONTRACT v1\n"
            f"role: {self.role}\n"
            f"context_mode: {self.mode.value}\n"
            f"dispatch_reason: {self.dispatch_reason}"
        )

    def _target_section(self) -> str:
        lines = [
            "[TARGET]",
            f"parent_slug: {self.parent_slug}",
            f"inherited_candidate_sha: {self.inherited_candidate_sha}",
        ]
        if self.package_id:
            lines.append(f"package_id: {self.package_id}")
        lines.append(f"responsibility: {self.responsibility}")
        return "\n".join(lines)

    def _acceptance_section(self) -> str:
        body = "\n".join(f"- {item}" for item in self.acceptance_criteria)
        return f"[ACCEPTANCE CRITERIA]\n{body}"

    def _changes_section(self) -> str:
        lines = ["[RELEVANT CHANGES]"]
        if self.changed_paths:
            shown = self.changed_paths[:CHANGED_PATHS_MAX]
            lines.append("changed_paths:")
            for path in shown:
                lines.append(f"  - {path}")
            if len(self.changed_paths) > CHANGED_PATHS_MAX:
                lines.append(f"  ... ({len(self.changed_paths) - CHANGED_PATHS_MAX} more omitted)")
        if self.diff_ref:
            lines.append(f"diff_ref: {self.diff_ref}")
        return "\n".join(lines)

    def _failure_section(self) -> str:
        assert self.failure_excerpt is not None
        return f"[FAILURE / REVIEW EVIDENCE]\n{self.failure_excerpt}"

    def _review_section(self) -> str:
        assert self.review_excerpt is not None
        return f"[REVIEW EVIDENCE]\n{self.review_excerpt}"

    def _invariants_section(self) -> str:
        body = "\n".join(f"- {item}" for item in self.invariants)
        return f"[INVARIANTS TO PRESERVE]\n{body}"

    def _original_task_section(self) -> str:
        assert self.original_task is not None
        return f"[ORIGINAL TASK]\n{self.original_task.strip()}"

    def _focused_verification_section(self) -> str:
        return f"[FOCUSED VERIFICATION]\n{self.focused_verification}"

    def event_payload(self) -> dict[str, object]:
        """Return the compact durable evidence payload for this projection.

        C04-R1 semantics:

        * ``original_task_bytes`` is the byte size of the durable source
          parent objective for this dispatch.  It is non-zero even in
          REPAIR / SURGICAL / PACKAGE modes that do NOT model-expose the
          full parent.
        * ``projected_context_bytes`` is the byte size of the semantic
          projection body.  Actual dispatch evidence must use
          :meth:`RenderResult.event_payload`, which includes the appended
          validation policy in the final model-visible prompt size.
        * ``full_parent_included`` reports whether the source parent
          objective was actually included verbatim in the model-visible
          prompt body.
        """
        # Prefer the explicit source byte count; fall back to the embedded
        # original_task field only when the caller did not provide it
        # (legacy INITIAL/FRESH_REIMPLEMENTATION callers).
        if self.source_original_task_bytes > 0:
            source_bytes = self.source_original_task_bytes
        elif self.original_task is not None:
            source_bytes = len(self.original_task.encode("utf-8"))
        else:
            source_bytes = 0
        return {
            "context_mode": self.mode.value,
            "role": self.role,
            "inherited_candidate_sha": self.inherited_candidate_sha,
            "package_id": self.package_id,
            "original_task_bytes": source_bytes,
            "projected_context_bytes": self.project_size_bytes(),
            "full_parent_included": self.full_parent_included,
            "evidence_truncated": (
                self.failure_excerpt is not None
                and _TRUNCATION_MARKER.strip() in self.failure_excerpt
            )
            or (
                self.review_excerpt is not None
                and _TRUNCATION_MARKER.strip() in self.review_excerpt
            ),
        }


# ---------------------------------------------------------------------------
# Deterministic objective / digest / slug helpers
# ---------------------------------------------------------------------------


_WHITESPACE_RE = re.compile(r"\s+")
_VALIDATION_POLICY_MARKER_STRIP_RE = re.compile(r"VALIDATION POLICY:")


def _strip_validation_marker(task: str) -> str:
    """Strip any embedded ``VALIDATION POLICY:`` literal from the task text.

    Prevents the slug/digest from accidentally duplicating the marker that
    :func:`render_projection` appends idempotently.
    """
    return _VALIDATION_POLICY_MARKER_STRIP_RE.sub("", task)


def objective_slug(task: str) -> str:
    """Return a deterministic compact slug for a task (max 120 chars).

    Whitespace is normalized, surrounding whitespace stripped, and the result
    is bounded deterministically.  No model call is required.
    """
    if task is None:
        return ""
    normalized = _WHITESPACE_RE.sub(" ", _strip_validation_marker(task)).strip()
    if len(normalized) <= OBJECTIVE_SLUG_MAX_CHARS:
        return normalized
    # Bound deterministically: keep head + ellipsis-style tail truncation.
    return normalized[:OBJECTIVE_SLUG_MAX_CHARS].rstrip()


_NUMBERED_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+")


def objective_digest(task: str) -> str:
    """Return a deterministic bounded objective digest.

    The digest prefers explicit numbered/bulleted requirements (in source
    order) and falls back to a bounded prefix when none are present.  Repeated
    lines are deduplicated and the result is hard-stopped at
    ``OBJECTIVE_DIGEST_MAX_CHARS``.

    No model call is required.
    """
    if task is None:
        return ""
    raw = _strip_validation_marker(task).strip()
    if not raw:
        return ""

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    explicit: list[str] = []
    seen: set[str] = set()
    for line in lines:
        if _NUMBERED_RE.match(line):
            normalized = _NUMBERED_RE.sub("", line).strip()
            if normalized and normalized.lower() not in seen:
                seen.add(normalized.lower())
                explicit.append(normalized)

    if explicit:
        joined = "\n".join(f"- {item}" for item in explicit)
        if len(joined) <= OBJECTIVE_DIGEST_MAX_CHARS:
            return joined
        # Bound while preserving as much head as possible.
        truncated: list[str] = []
        used = 0
        for item in explicit:
            candidate = f"- {item}"
            if used + len(candidate) + 1 > OBJECTIVE_DIGEST_MAX_CHARS:
                break
            truncated.append(candidate)
            used += len(candidate) + 1
        return "\n".join(truncated)

    # Fallback: bounded prefix.
    return raw[:OBJECTIVE_DIGEST_MAX_CHARS].rstrip()


# ---------------------------------------------------------------------------
# Deterministic pruning utility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PruneResult:
    """Result of :func:`prune_evidence`."""

    text: str
    truncated: bool
    original_bytes: int
    kept_bytes: int


def prune_evidence(raw: str | None, limit: int = EVIDENCE_BLOCK_MAX_CHARS) -> PruneResult:
    """Return a deterministically bounded view of an evidence block.

    The function preserves a useful prefix and (when the input is long enough
    to warrant it) a useful suffix around a clear truncation marker.  No
    model call is required.  Returning a typed :class:`PruneResult` lets
    callers decide whether to surface ``truncated=True`` in durable evidence.
    """
    if not raw:
        return PruneResult(text="", truncated=False, original_bytes=0, kept_bytes=0)
    text = raw.strip()
    original_bytes = len(text.encode("utf-8"))
    if len(text) <= limit * 2:
        return PruneResult(
            text=text, truncated=False, original_bytes=original_bytes, kept_bytes=original_bytes
        )
    head = text[:limit]
    tail = text[-limit:]
    truncated_text = f"{head}{_TRUNCATION_MARKER}{tail}"
    return PruneResult(
        text=truncated_text,
        truncated=True,
        original_bytes=original_bytes,
        kept_bytes=len(truncated_text.encode("utf-8")),
    )


# ---------------------------------------------------------------------------
# Surgical Escalation Capsule
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SurgicalCapsule:
    """First-class surgical escalation capsule.

    A surgical capsule carries ONLY the bounded residual defect; the raw full
    original task, full repository diff, and complete review history are
    intentionally absent.  The dispatcher is responsible for selecting SURGEON
    role only when genuine capability-bearing tier escalation is required.
    """

    parent_slug: str
    inherited_candidate_sha: str
    dispatch_reason: str
    bounded_defect: str
    failing_check_command: str | None = None
    failing_exit_code: int | None = None
    bounded_stdout_excerpt: str | None = None
    bounded_stderr_excerpt: str | None = None
    blocking_review_finding: str | None = None
    changed_paths: tuple[str, ...] = ()
    diff_ref: str | None = None
    invariants_to_preserve: tuple[str, ...] = ()
    # C04-R1: source parent objective byte size for accurate evidence.
    source_original_task_bytes: int = 0
    focused_verification: str = (
        "Apply the bounded fix only; verify with focused tests and return the candidate."
    )

    def to_projection(self, role: str = "SURGEON") -> ContextProjection:
        """Return the :class:`ContextProjection` for this capsule."""
        change_section: list[tuple[str, str]] = []
        if self.bounded_stdout_excerpt:
            change_section.append(("STDOUT EXCERPT", self.bounded_stdout_excerpt))
        if self.bounded_stderr_excerpt:
            change_section.append(("STDERR EXCERPT", self.bounded_stderr_excerpt))
        if self.failing_check_command or self.failing_exit_code is not None:
            details = []
            if self.failing_check_command:
                details.append(f"command: {self.failing_check_command}")
            if self.failing_exit_code is not None:
                details.append(f"exit_code: {self.failing_exit_code}")
            change_section.append(("FAILING CHECK", "\n".join(details)))
        return ContextProjection(
            mode=DispatchContextMode.SURGICAL,
            role=role,
            parent_slug=self.parent_slug,
            inherited_candidate_sha=self.inherited_candidate_sha,
            dispatch_reason=self.dispatch_reason,
            responsibility=self.bounded_defect,
            invariants=self.invariants_to_preserve,
            changed_paths=self.changed_paths,
            diff_ref=self.diff_ref,
            failure_excerpt=(
                self.blocking_review_finding if self.blocking_review_finding else None
            ),
            full_parent_included=False,
            source_original_task_bytes=self.source_original_task_bytes,
            focused_verification=self.focused_verification,
            extra_sections=tuple(change_section),
        )


# ---------------------------------------------------------------------------
# Convenience builders used by Orchestrator to construct projections.
# ---------------------------------------------------------------------------


def build_retry_projection(
    *,
    original_task: str,
    attempt_idx: int,
    prior_attempt: object,
    prior_check: object | None,
    inherited_base_sha: str | None,
    role: str = "REPAIR",
    invariants: tuple[str, ...] = (),
) -> ContextProjection:
    """Build a :class:`ContextProjection` for an ordinary retry.

    The raw full ``original_task`` is intentionally NOT included; only the
    bounded objective digest, inherited candidate SHA, and exact failure
    evidence are projected.  ``original_task`` is still consulted by the
    durable digest helper so the worker can see what it is fixing without
    rediscovering the whole task.
    """
    failure_parts: list[str] = []
    prior_worker_error = getattr(prior_attempt, "worker_error", None)
    if prior_worker_error:
        failure_parts.append(f"Prior worker error: {prior_worker_error}")
    if prior_check is not None:
        failure_parts.append(
            f"Prior gate '{getattr(prior_check, 'gate_command', '?')}' failed "
            f"with exit code {getattr(prior_check, 'exit_code', '?')}."
        )
        stderr = getattr(prior_check, "stderr", None) or ""
        stdout = getattr(prior_check, "stdout", None) or ""
        if stderr.strip():
            excerpt = prune_evidence(stderr.strip(), limit=EVIDENCE_BLOCK_MAX_CHARS)
            failure_parts.append(f"Gate stderr excerpt:\n{excerpt.text}")
            if excerpt.truncated:
                failure_parts.append("[truncated]")
        elif stdout.strip():
            excerpt = prune_evidence(stdout.strip(), limit=EVIDENCE_BLOCK_MAX_CHARS)
            failure_parts.append(f"Gate stdout excerpt:\n{excerpt.text}")
            if excerpt.truncated:
                failure_parts.append("[truncated]")
    failure_excerpt = "\n".join(failure_parts) if failure_parts else None
    slug = objective_slug(original_task)
    digest = objective_digest(original_task)
    responsibility = (
        f"Repair the failure introduced by attempt #{getattr(prior_attempt, 'attempt_number', '?')}; "
        "preserve the inherited candidate commit and produce a single coherent fix."
    )
    invariants = (*invariants, "Original task context (bounded digest): " + digest)
    return ContextProjection(
        mode=DispatchContextMode.REPAIR,
        role=role,
        parent_slug=slug,
        inherited_candidate_sha=(
            inherited_base_sha or getattr(prior_attempt, "base_sha", None) or "UNKNOWN"
        ),
        dispatch_reason=(
            f"retry attempt #{attempt_idx} after prior "
            f"({getattr(prior_attempt, 'provider', '?')}/"
            f"{getattr(prior_attempt, 'model', '?')}) failure"
        ),
        responsibility=responsibility,
        failure_excerpt=failure_excerpt,
        invariants=invariants,
        source_original_task_bytes=len(original_task.encode("utf-8")),
        focused_verification=(
            "Apply focused tests near the change and any nearby regressions; "
            "return the next candidate to Orch."
        ),
    )


def build_repair_projection(
    *,
    original_task: str,
    attempt_idx: int,
    from_sha: str,
    review: object,
    role: str = "REPAIR",
    invariants: tuple[str, ...] = (),
) -> ContextProjection:
    """Build a :class:`ContextProjection` for an ordinary review repair.

    As in :func:`build_retry_projection`, the raw full original task is NOT
    included; only the bounded objective digest and bounded review evidence.
    """
    summary = getattr(review, "summary", "") or ""
    details = getattr(review, "details", "") or ""
    bounded_summary = prune_evidence(summary, limit=REVIEW_BLOCK_MAX_CHARS)
    bounded_details = prune_evidence(details, limit=REVIEW_BLOCK_MAX_CHARS)
    parts: list[str] = []
    if summary:
        parts.append(f"Summary: {bounded_summary.text}")
    if details:
        parts.append(f"Details:\n{bounded_details.text}")
    review_excerpt = "\n".join(parts) if parts else ""
    truncated = bounded_summary.truncated or bounded_details.truncated
    slug = objective_slug(original_task)
    digest = objective_digest(original_task)
    invariants = (
        *invariants,
        "Original task context (bounded digest): " + digest,
        "Acceptance criteria must remain satisfied.",
    )
    return ContextProjection(
        mode=DispatchContextMode.REPAIR,
        role=role,
        parent_slug=slug,
        inherited_candidate_sha=from_sha,
        dispatch_reason=(
            f"review repair attempt #{attempt_idx} after blocking review by "
            f"{getattr(review, 'provider', '?')}/{getattr(review, 'model', '?')}"
        ),
        responsibility=(
            "Address the blocking review findings without introducing regressions; "
            "preserve the inherited candidate commit."
        ),
        review_excerpt=review_excerpt,
        invariants=invariants,
        source_original_task_bytes=len(original_task.encode("utf-8")),
        focused_verification=(
            "Re-run the focused tests that originally surfaced the gap; "
            "return the next candidate to Orch."
        ),
        extra_sections=(
            (("REVIEW TRUNCATION", "Some review evidence was deterministically bounded."),)
            if truncated
            else ()
        ),
    )


def build_final_repair_projection(
    *,
    original_task: str,
    attempt_idx: int,
    from_sha: str,
    reviews: Iterable[object],
    role: str = "REPAIR",
    invariants: tuple[str, ...] = (),
) -> ContextProjection:
    """Build a :class:`ContextProjection` for final-convergence repair.

    Aggregates UNRESOLVED review findings, deterministically bounds each
    detail, deduplicates already-resolved findings, and never re-introduces
    the raw full original task.
    """
    parts: list[str] = []
    truncated_any = False
    seen_keys: set[str] = set()
    for idx, review in enumerate(reviews, start=1):
        provider = getattr(review, "provider", "?")
        model = getattr(review, "model", "?")
        summary = (getattr(review, "summary", "") or "").strip()
        details = (getattr(review, "details", "") or "").strip()
        key = f"{summary.lower()}|{details.lower()}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        bounded_summary = prune_evidence(summary, limit=REVIEW_BLOCK_MAX_CHARS)
        bounded_details = prune_evidence(details, limit=REVIEW_BLOCK_MAX_CHARS)
        truncated_any = truncated_any or bounded_summary.truncated or bounded_details.truncated
        parts.append(
            f"--- Finding #{idx} ({provider}/{model}) ---\nSummary: {bounded_summary.text}"
        )
        if details:
            parts.append(f"Details:\n{bounded_details.text}")
    review_excerpt = "\n".join(parts) if parts else ""
    slug = objective_slug(original_task)
    digest = objective_digest(original_task)
    invariants = (
        *invariants,
        "Original task context (bounded digest): " + digest,
        "Acceptance criteria must remain satisfied.",
    )
    return ContextProjection(
        mode=DispatchContextMode.REPAIR,
        role=role,
        parent_slug=slug,
        inherited_candidate_sha=from_sha,
        dispatch_reason=f"final convergence repair attempt #{attempt_idx}",
        responsibility=(
            "Address ALL outstanding findings in a single pass without "
            "introducing regressions; preserve the inherited candidate commit."
        ),
        review_excerpt=review_excerpt,
        invariants=invariants,
        source_original_task_bytes=len(original_task.encode("utf-8")),
        focused_verification=(
            "Re-run the focused tests associated with each outstanding finding; "
            "return the next candidate to Orch."
        ),
        extra_sections=(
            (
                (
                    "REVIEW TRUNCATION",
                    "Some review evidence was deterministically bounded.",
                ),
            )
            if truncated_any
            else ()
        ),
    )


def build_initial_projection(
    *,
    original_task: str,
    role: str = "BULK",
    invariants: tuple[str, ...] = (),
    package_id: str | None = None,
) -> ContextProjection:
    """Build a :class:`ContextProjection` for the initial unsliced dispatch.

    C04-R1: the raw full original task is model-visible EXACTLY ONCE, in
    the ``[ORIGINAL TASK]`` section.  The ``responsibility`` line uses a
    compact static placeholder so the same source text never appears
    twice in the rendered body.
    """
    return ContextProjection(
        mode=DispatchContextMode.INITIAL,
        role=role,
        parent_slug=objective_slug(original_task),
        inherited_candidate_sha="UNKNOWN",
        dispatch_reason="initial implementation",
        # Compact static placeholder; the full task is emitted once
        # further down in [ORIGINAL TASK] when full_parent_included=True.
        responsibility="Implement the original objective below as one coherent initial candidate.",
        invariants=invariants,
        full_parent_included=True,
        original_task=original_task,
        source_original_task_bytes=len(original_task.encode("utf-8")),
        package_id=package_id,
        focused_verification=(
            "Run focused tests and nearby regressions; return the candidate to Orch."
        ),
    )


def build_package_initial_projection(
    *,
    original_task: str,
    package: object,
    completed_summaries: tuple[str, ...] = (),
    inherited_base_sha: str,
    role: str | None = None,
) -> ContextProjection:
    """Build a single :class:`ContextProjection` for an INITIAL sliced
    package dispatch.

    C04-R1: this is the ONLY worker contract for an initial package
    dispatch.  It does NOT concatenate with an INITIAL projection.  The
    raw full original task is NEVER model-visible in PACKAGE mode.
    """
    pkg_goal = getattr(package, "goal", "") or ""
    pkg_criteria = tuple(str(item) for item in getattr(package, "acceptance_criteria", ()) or ())
    pkg_relevant_paths = tuple(getattr(package, "relevant_paths", ()) or ())
    pkg_prereq_summaries = tuple(
        completed_summaries or getattr(package, "prerequisite_summaries", ()) or ()
    )
    pkg_id = getattr(package, "id", None)
    pkg_base_candidate_sha = getattr(package, "base_candidate_sha", None) or "UNKNOWN"
    slug = objective_slug(original_task)
    digest = objective_digest(original_task)
    invariants = (
        "Implement ONLY this package's responsibility; do not pre-implement "
        "downstream packages merely because they exist.",
        "Inspect the isolated worktree for source context; preserve completed prerequisite work.",
        "Original task context (bounded digest): " + digest,
    )
    prereqs_section = (
        ("PREREQUISITES", "\n".join(str(item) for item in pkg_prereq_summaries) or "(none yet)"),
    )
    return ContextProjection(
        mode=DispatchContextMode.PACKAGE,
        role=(role or DispatchRole.BULK.value),
        parent_slug=slug,
        inherited_candidate_sha=inherited_base_sha,
        dispatch_reason="package implementation",
        responsibility=pkg_goal.strip() or "Implement this package's responsibility.",
        acceptance_criteria=pkg_criteria,
        invariants=invariants,
        changed_paths=pkg_relevant_paths,
        diff_ref=f"{pkg_base_candidate_sha}..{inherited_base_sha}",
        package_id=pkg_id,
        source_original_task_bytes=len(original_task.encode("utf-8")),
        extra_sections=prereqs_section,
        focused_verification=(
            "Run focused tests and nearby regressions for this package only; "
            "return the candidate to Orch."
        ),
    )


def build_package_retry_projection(
    *,
    original_task: str,
    package: object,
    inherited_base_sha: str,
    prior_check: object | None,
    review_excerpt: str | None = None,
    failure_excerpt: str | None = None,
    evidence_truncated: bool = False,
    role: str | None = None,
) -> ContextProjection:
    """Build a single :class:`ContextProjection` for a PACKAGE retry
    dispatch (either a gate-failure retry or a review-repair).

    C04-R1: this is the ONLY worker contract for a package retry
    dispatch.  It does NOT concatenate with a generic REPAIR projection.
    The raw full original task is NEVER model-visible in PACKAGE mode.
    """
    pkg_goal = getattr(package, "goal", "") or ""
    pkg_criteria = tuple(str(item) for item in getattr(package, "acceptance_criteria", ()) or ())
    pkg_relevant_paths = tuple(getattr(package, "relevant_paths", ()) or ())
    pkg_id = getattr(package, "id", None)
    pkg_base_candidate_sha = getattr(package, "base_candidate_sha", None) or "UNKNOWN"
    slug = objective_slug(original_task)
    digest = objective_digest(original_task)
    invariants = (
        "Implement ONLY this package's responsibility; do not pre-implement "
        "downstream packages merely because they exist.",
        "Inspect the isolated worktree for source context; preserve completed prerequisite work.",
        "Acceptance criteria of the inherited candidate must remain satisfied.",
        "Original task context (bounded digest): " + digest,
    )
    extra_sections: tuple[tuple[str, str], ...] = ()
    if evidence_truncated:
        extra_sections = (
            ("EVIDENCE TRUNCATION", "Some bounded evidence was deterministically pruned."),
        )
    return ContextProjection(
        mode=DispatchContextMode.PACKAGE,
        role=(role or DispatchRole.REPAIR.value),
        parent_slug=slug,
        inherited_candidate_sha=inherited_base_sha,
        dispatch_reason="package retry",
        responsibility=pkg_goal.strip()
        or "Repair the package failure on the inherited candidate commit.",
        acceptance_criteria=pkg_criteria,
        invariants=invariants,
        changed_paths=pkg_relevant_paths,
        diff_ref=f"{pkg_base_candidate_sha}..{inherited_base_sha}",
        failure_excerpt=failure_excerpt,
        review_excerpt=review_excerpt,
        package_id=pkg_id,
        source_original_task_bytes=len(original_task.encode("utf-8")),
        extra_sections=extra_sections,
        focused_verification=(
            "Re-run the focused tests associated with this package's bounded "
            "evidence; return the candidate to Orch."
        ),
    )


def build_continuation_projection(
    *,
    original_task: str,
    run_id: str,
    attempt_idx: int,
    checkpoint_sha: str,
    last_verified_candidate_sha: str,
    interruption_reason: str,
    previous_provider: str,
    previous_model: str,
    changed_paths: tuple[str, ...] = (),
    diff_summary: str | None = None,
    failure_excerpt: str | None = None,
    package: object | None = None,
    acceptance_criteria: tuple[str, ...] = (),
    invariants: tuple[str, ...] = (),
    side_effect_summary: str | None = None,
    role: str = "REPAIR",
) -> ContextProjection:
    """Build a bounded, provider-neutral :class:`ContextProjection` for a continuation dispatch.

    The raw full parent objective is NEVER model-visible.
    Inherits the durable checkpoint commit SHA directly.
    """
    slug = objective_slug(original_task)
    digest = objective_digest(original_task)
    pkg_id = getattr(package, "id", None) if package else None
    pkg_goal = getattr(package, "goal", "") if package else ""
    pkg_criteria = (
        tuple(str(c) for c in getattr(package, "acceptance_criteria", ()) or ()) if package else ()
    )
    all_criteria = tuple(dict.fromkeys((*acceptance_criteria, *pkg_criteria)))

    responsibility = (
        f"Continue implementation from partial checkpoint {checkpoint_sha[:8]}; "
        f"previous worker ({previous_provider}/{previous_model}) interrupted due to {interruption_reason}."
    )
    if pkg_goal.strip():
        responsibility += f" Package goal: {pkg_goal.strip()}"

    all_invariants = (
        f"Continue from partial checkpoint {checkpoint_sha}; do not reset to base.",
        "Preserve existing valid edits and produce a complete candidate.",
        "Original task context (bounded digest): " + digest,
        *invariants,
    )

    extra_sections: list[tuple[str, str]] = [
        (
            "CONTINUATION METADATA",
            "\n".join(
                [
                    f"run_id: {run_id}",
                    f"source_interruption_reason: {interruption_reason}",
                    f"previous_worker: {previous_provider}/{previous_model}",
                    f"last_verified_candidate_sha: {last_verified_candidate_sha}",
                    f"inherited_checkpoint_sha: {checkpoint_sha}",
                ]
            ),
        )
    ]
    if side_effect_summary:
        extra_sections.append(("SIDE EFFECT JOURNAL", side_effect_summary))
    if diff_summary:
        bounded_diff = prune_evidence(diff_summary, limit=EVIDENCE_BLOCK_MAX_CHARS)
        extra_sections.append(("CHECKPOINT DIFF SUMMARY", bounded_diff.text))

    return ContextProjection(
        mode=DispatchContextMode.CONTINUATION,
        role=role,
        parent_slug=slug,
        inherited_candidate_sha=checkpoint_sha,
        dispatch_reason=f"continuation attempt #{attempt_idx} after {interruption_reason}",
        responsibility=responsibility,
        acceptance_criteria=all_criteria,
        invariants=all_invariants,
        changed_paths=changed_paths,
        diff_ref=f"{last_verified_candidate_sha}..{checkpoint_sha}",
        failure_excerpt=failure_excerpt,
        full_parent_included=False,
        package_id=pkg_id,
        source_original_task_bytes=len(original_task.encode("utf-8")),
        extra_sections=tuple(extra_sections),
        focused_verification=(
            "Apply focused tests to verify both inherited and new edits; "
            "return the next candidate to Orch."
        ),
    )


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RenderResult:
    """Result of rendering a :class:`ContextProjection` to a worker prompt."""

    prompt: str
    projection: ContextProjection
    full_task_included: bool
    original_task_bytes: int
    projected_prompt_bytes: int
    validation_policy_marker_count: int

    def event_payload(self) -> dict[str, object]:
        """Return authoritative evidence for this exact rendered contract."""
        payload = self.projection.event_payload()
        payload["projected_context_bytes"] = self.projected_prompt_bytes
        return payload


def render_projection(
    projection: ContextProjection,
    *,
    validation_policy_text: str,
    validation_policy_marker: str,
    tool_contract: FrozenToolContract | None = None,
    project_scope_text: str | None = None,
) -> RenderResult:
    """Render a deterministic worker prompt for ``projection``.

    The validation policy is appended exactly once and idempotently.  The
    invariant contract prefix is byte-identical for identical
    (role, mode, parent_slug, package_id, dispatch_reason) tuples.
    C09: an optional bounded ``[PROJECT SCOPE]`` section is appended exactly
    once, before the validation policy.
    """
    if projection.mode in MODES_WITHOUT_FULL_TASK:
        # Defense in depth: refuse to model-expose the full task in
        # bounded modes.  Callers may still persist ``original_task`` on
        # durable rows for audit, but it never reaches the worker.
        assert not projection.full_parent_included, (
            f"full_parent_included=True not allowed in {projection.mode.value}"
        )
        assert projection.original_task is None, (
            f"original_task must be None in {projection.mode.value}"
        )

    body = projection.render_body()
    if tool_contract is not None and tool_contract.bindings:
        tools_section = "[AVAILABLE TOOLS]\n"
        tools_section += "\n".join(
            f"- {binding.ref} — {binding.hint}" for binding in tool_contract.bindings
        )
        tools_section += "\n\nInspect one tool lazily with:\n  orch tools describe <logical-ref>"
        tools_section += "\nInvoke one tool lazily with:\n  orch tools exec <logical-ref> -- <args>"
        body = body.rstrip() + "\n\n" + tools_section
    stripped = body.rstrip()
    if project_scope_text is not None and "[PROJECT SCOPE]" not in stripped:
        stripped = stripped + "\n\n" + project_scope_text.rstrip()
    if validation_policy_marker in stripped:
        final = stripped
    else:
        final = stripped + "\n\n" + validation_policy_text.rstrip()
    return RenderResult(
        prompt=final,
        projection=projection,
        full_task_included=projection.full_parent_included,
        original_task_bytes=(
            len(projection.original_task.encode("utf-8")) if projection.original_task else 0
        ),
        projected_prompt_bytes=len(final.encode("utf-8")),
        validation_policy_marker_count=final.count(validation_policy_marker),
    )


__all__ = [
    "CHANGED_PATHS_MAX",
    "ContextProjection",
    "DispatchContextMode",
    "EVIDENCE_BLOCK_MAX_CHARS",
    "MODES_WITHOUT_FULL_TASK",
    "OBJECTIVE_DIGEST_MAX_CHARS",
    "OBJECTIVE_SLUG_MAX_CHARS",
    "PruneResult",
    "REVIEW_BLOCK_MAX_CHARS",
    "RenderResult",
    "STDERR_BLOCK_MAX_CHARS",
    "STDOUT_BLOCK_MAX_CHARS",
    "SurgicalCapsule",
    "build_continuation_projection",
    "build_final_repair_projection",
    "build_initial_projection",
    "build_package_initial_projection",
    "build_package_retry_projection",
    "build_repair_projection",
    "build_retry_projection",
    "objective_digest",
    "objective_slug",
    "prune_evidence",
    "render_projection",
]
