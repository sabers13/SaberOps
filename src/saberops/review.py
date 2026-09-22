"""Independent code review engine with cross-provider diversity."""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path
from typing import Protocol

from saberops.authority import load_authority_settings
from saberops.config import DEFAULT_REVIEW_LIMIT, RunConfig, reconstruct_run_config
from saberops.control_plane import (
    legacy_frozen_control_plane,
    reconstruct_frozen_control_plane,
)
from saberops.db import Database, current_iso_timestamp
from saberops.dispatch import control_plane_decision
from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    DispatchEvidence,
    DispatchOutcome,
    DispatchReason,
    DispatchRole,
    DispatchStage,
    PhaseKind,
    ProviderReadiness,
    QuotaState,
    ReviewResult,
    ReviewVerdict,
    RoutingState,
    Run,
    RunStatus,
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.observability import new_dispatch_evidence, update_dispatch_from_result
from saberops.observability.phase_telemetry import measure_phase
from saberops.process import run_process
from saberops.provenance import ProvenanceError, assert_run_repository
from saberops.quota import CODEX_PROVIDER, CODEX_WEEKLY_POOL
from saberops.routing import (
    CANDIDATE_CODEX_SOL,
    CANDIDATE_CODEX_TERRA,
    CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
    CANDIDATE_OPENCODE_MUSE_SPARK,
    CANDIDATE_OPENCODE_OX_ALPHA,
    is_muse_model,
)
from saberops.routing_decision import TierDecision, choose_tier
from saberops.telemetry import persist_worker_usage
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

# Machine-readable pre-dispatch skip reasons are imported from dispatch for single source.


class _ReadinessAdmission(Protocol):
    """Neutral readiness input for one exact resolved connection.

    Structural (not imported) so review never depends on model_access:
    the composition layer injects the real
    :class:`saberops.model_access.ReadinessService`, which
    satisfies this shape.
    """

    @property
    def state(self) -> ProviderReadiness: ...

    @property
    def reason(self) -> str: ...

    @property
    def executable_available(self) -> bool: ...

    @property
    def connection_id(self) -> str | None: ...


class _ReadinessProvider(Protocol):
    """Composition-supplied readiness acquisition/projection seam."""

    def admission_for(
        self, *, provider: str, profile_id: str = "", backend_ref: str = ""
    ) -> _ReadinessAdmission: ...

    def record_success(self, connection_id: str) -> None: ...


def _normalized_model(model: str) -> str:
    return model.rsplit("/", 1)[-1].strip().lower()


def _bounded_tail(text: str | None, limit: int = 4000) -> str:
    """Return a bounded representation of reviewer output for diagnostics.

    Keeps the first and last ``limit`` characters of long output so both
    startup banners and terminal errors survive in persisted event payloads.
    """
    value = text or ""
    if len(value) <= 2 * limit:
        return value
    omitted = len(value) - 2 * limit
    return (
        f"{value[:limit]}\n"
        f"...[diagnostic tail truncated {omitted} middle characters]...\n"
        f"{value[-limit:]}"
    )


def _format_infra_details(notes: list[dict[str, str | None]]) -> str:
    """Render aggregated reviewer infrastructure failures into a readable block."""
    if not notes:
        return ""
    lines: list[str] = ["Reviewer infrastructure failures:"]
    for note in notes:
        provider = note.get("provider") or "?"
        model = note.get("model") or "?"
        status = note.get("status") or "?"
        error = note.get("error")
        lines.append(f"- {provider} / {model} (status={status}, error={error or 'n/a'})")
        stderr_tail = note.get("stderr_tail")
        if stderr_tail:
            lines.append(f"  stderr tail: {stderr_tail}")
        stdout_tail = note.get("stdout_tail")
        if stdout_tail:
            lines.append(f"  stdout tail: {stdout_tail}")
    return "\n".join(lines)


def _is_provider_quota_blocked(quota_states: list[QuotaState], provider: str) -> bool:
    """Return True when a provider is quota-blocked for review dispatch.

    Mirrors :func:`saberops.quota.apply_quota_policy`: only a BLOCKED
    state blocks a provider, and a Codex row for a non-``weekly`` pool never
    blocks Codex.
    """
    provider_norm = provider.strip().lower()
    for state in quota_states:
        if state.provider != provider_norm or state.routing_state != RoutingState.BLOCKED:
            continue
        if state.provider == CODEX_PROVIDER and state.pool != CODEX_WEEKLY_POOL:
            continue
        return True
    return False


def _pre_dispatch_skip_reason(
    *,
    candidate: WorkerCandidate,
    adapter: WorkerAdapter | None,
    training_allowed: bool,
    quota_states: list[QuotaState],
    prompt_bytes: int,
    policy: object | None = None,
    required_capabilities: tuple[str, ...] = (),
    control_plane_digest: str | None = None,
    readiness_state: ProviderReadiness | None = None,
    readiness_reason: str | None = None,
    readiness_executable_available: bool = True,
) -> str | None:
    """Determine a deterministic pre-dispatch skip reason, or None to launch.

    Delegates to the shared dispatch evaluator to ensure single-source policy.
    """
    from saberops.dispatch import evaluate_eligibility

    return evaluate_eligibility(
        candidate=candidate,
        adapter=adapter,
        training_allowed=training_allowed,
        quota_states=quota_states,
        prompt_bytes=prompt_bytes,
        explicit_override=False,
        policy=policy,  # type: ignore[arg-type]
        required_capabilities=required_capabilities,
        control_plane_digest=control_plane_digest,
        readiness_state=readiness_state,
        readiness_reason=readiness_reason,
        readiness_executable_available=readiness_executable_available,
    )


def _review_limit_from_run(run: Run) -> int:
    """Resolve the review budget from a run's persisted config, if present."""
    if run.config_json:
        try:
            data = json.loads(run.config_json)
            limit = int(data.get("review_limit", DEFAULT_REVIEW_LIMIT))
            if limit >= 1:
                return limit
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    return DEFAULT_REVIEW_LIMIT


def _review_tier_decision(run: Run) -> TierDecision:
    """Choose REVIEW's tier from persisted policy and the whole objective.

    Modern runs persist the complete normalized routing config.  Legacy rows
    do not, so retain their historical exact-tier behavior rather than
    guessing an automatic ceiling or manual policy that was never stored.
    """
    config: RunConfig | None = None
    if run.config_json:
        try:
            data = json.loads(run.config_json)
            if isinstance(data, dict):
                config = reconstruct_run_config(data)
        except (ValueError, TypeError, json.JSONDecodeError):
            pass
    if config is None:
        config = RunConfig(
            task=run.task,
            target_repo=run.target_repo,
            routing_mode="manual",
            manual_tier=run.tier,
            max_auto_tier=run.tier,
            training_allowed=run.training_allowed,
            gate_command=run.gate_command,
        )
    return choose_tier(
        config,
        stage=DispatchStage.REVIEW,
        requirements=run.task,
    )


def _ordered_reviewer_candidates(
    candidate_provider: str,
    candidate_model: str,
    tier: Tier,
) -> list[WorkerCandidate]:
    """Build the ordered reviewer ladder excluding only the implementer itself.

    Training-policy and provider-availability exclusions are intentionally NOT
    applied here: they are surfaced as pre-dispatch skip reasons by the dispatch
    loop so each skip can be recorded as a machine-readable event.
    """
    model_key = _normalized_model(candidate_model)
    if model_key in ("gpt-5.6-sol", "gpt-5.6-terra"):
        base = [
            CANDIDATE_OPENCODE_MUSE_SPARK,
            CANDIDATE_OPENCODE_OX_ALPHA,
            CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
        ]
    elif model_key == "deepseek-v4-pro":
        base = [
            CANDIDATE_OPENCODE_MUSE_SPARK,
            CANDIDATE_OPENCODE_OX_ALPHA,
            CANDIDATE_CODEX_TERRA,
        ]
    elif model_key == "muse-spark-1.2-contributor":
        base = [
            CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
            CANDIDATE_OPENCODE_OX_ALPHA,
            CANDIDATE_CODEX_TERRA,
        ]
    elif model_key == "ox-alpha-free":
        base = [
            CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
            CANDIDATE_OPENCODE_MUSE_SPARK,
            CANDIDATE_CODEX_TERRA,
        ]
    else:
        base = [
            CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO,
            CANDIDATE_OPENCODE_MUSE_SPARK,
            CANDIDATE_OPENCODE_OX_ALPHA,
            CANDIDATE_CODEX_TERRA,
        ]

    if tier == Tier.T3:
        if candidate_provider.lower() != "codex":
            base = [CANDIDATE_CODEX_SOL]
        else:
            base = [CANDIDATE_OPENCODE_GO_DEEPSEEK_V4_PRO]

    return [candidate for candidate in base if _normalized_model(candidate.model) != model_key]


def select_reviewer_ladder(
    candidate_provider: str,
    candidate_model: str,
    registry: AdapterRegistry,
    tier: Tier,
    training_allowed: bool,
) -> list[WorkerCandidate]:
    """Build an ordered fallback ladder of reviewer candidates.

    The ladder is keyed by the normalized implementer model basename (prefix- and
    case-insensitive) and the run tier. It never includes the implementer itself,
    drops Muse entries when training is not allowed, and only keeps candidates
    whose provider adapter is currently available.
    """
    ladder: list[WorkerCandidate] = []
    for candidate in _ordered_reviewer_candidates(candidate_provider, candidate_model, tier):
        if not training_allowed and is_muse_model(candidate.model):
            continue
        if not registry.is_available(candidate.provider):
            continue
        ladder.append(candidate)
    return ladder


def select_reviewer_ladder_unfiltered(
    candidate_provider: str,
    candidate_model: str,
    tier: Tier,
) -> list[WorkerCandidate]:
    """Build the ordered reviewer ladder including pre-dispatch-skippable entries.

    Unlike :func:`select_reviewer_ladder`, this returns every candidate except
    the implementer itself, leaving training-policy, availability, quota, and
    transport exclusions to the dispatch loop so each is recorded as a skip
    event with a machine-readable reason and none consumes a budget slot.
    """
    return _ordered_reviewer_candidates(candidate_provider, candidate_model, tier)


FINAL_CONVERGENCE_NOTICE = (
    "FINAL CONVERGENCE REVIEW - This is the final review attempt. "
    "There is no further review cycle; return PASS, PASS_WITH_BACKLOG, or FINAL_BLOCK. "
    "FINAL_BLOCK is reserved for true release blockers (security-critical defects, "
    "data corruption/loss, fundamentally incorrect or materially non-working behavior). "
    "Minor, stylistic, optional, deferred or compromisable issues must NOT produce "
    "FINAL_BLOCK - they belong in a backlog list returned with the verdict."
)


def parse_review_output(output_text: str, is_final: bool = False) -> tuple[ReviewVerdict, str, str]:
    """Parse reviewer output to determine verdict, summary, and details.

    ``is_final`` gates final-only verdicts: ``FINAL_BLOCK`` and
    ``PASS_WITH_BACKLOG`` are only valid on a final review. On a non-final
    review they deterministically map to substantive ``BLOCK`` so a repair
    cycle proceeds and backlog collection is deferred to the final review.

    Fail-closed protocol (mirrors the structured review machinery in
    :mod:`saberops.review_policy`, whose disposition is computed, never
    trusted from prose): only a single unambiguous explicit ``VERDICT:``
    line can establish ``PASS``.  Bare prose approvals (``LGTM`` /
    ``APPROVED``) without that line, conflicting explicit verdict lines,
    and unknown or ambiguous verdict strings all map to ``BLOCK``.
    """
    text = output_text.strip()
    if not text:
        return ReviewVerdict.BLOCK, "Empty review output", ""

    lines = [line.strip() for line in text.splitlines() if line.strip()]
    verdict = ReviewVerdict.BLOCK
    summary = "Review completed"

    # Step 1: Scan line-by-line for explicit verdict lines.
    # Anchored as their own line with case-insensitivity and surrounding whitespace allowed.
    explicit_verdicts: list[tuple[int, ReviewVerdict]] = []
    for idx, raw_line in enumerate(text.splitlines()):
        stripped = raw_line.strip()
        m = re.match(
            r"^VERDICT:\s*(PASS_WITH_BACKLOG|FINAL_BLOCK|PASS|BLOCK)$",
            stripped,
            re.IGNORECASE,
        )
        if m:
            token = m.group(1).upper()
            if token == "PASS":
                v = ReviewVerdict.PASS
            elif token == "PASS_WITH_BACKLOG":
                v = ReviewVerdict.PASS_WITH_BACKLOG
            elif token == "FINAL_BLOCK":
                v = ReviewVerdict.FINAL_BLOCK
            else:
                v = ReviewVerdict.BLOCK
            explicit_verdicts.append((idx, v))

    if explicit_verdicts:
        # Gate final-only verdicts behind is_final: on non-final reviews they
        # map deterministically to BLOCK (repair proceeds).
        gated: list[tuple[int, ReviewVerdict]] = []
        for idx, raw_verdict in explicit_verdicts:
            if not is_final and raw_verdict in (
                ReviewVerdict.PASS_WITH_BACKLOG,
                ReviewVerdict.FINAL_BLOCK,
            ):
                raw_verdict = ReviewVerdict.BLOCK
            gated.append((idx, raw_verdict))
        if len({verdict for _, verdict in gated}) > 1:
            # Conflicting explicit verdict lines are malformed reviewer
            # output: fail closed as BLOCK, never approve on contradiction.
            return (
                ReviewVerdict.BLOCK,
                "Conflicting explicit verdict lines without a single "
                "disposition (blocked)",
                text,
            )
        last_idx, last_verdict = gated[-1]
        verdict = last_verdict
        if verdict in (ReviewVerdict.PASS, ReviewVerdict.PASS_WITH_BACKLOG):
            if verdict == ReviewVerdict.PASS_WITH_BACKLOG:
                # Include backlog details if present as summary tail
                following_lines = [
                    line.strip()
                    for line in text.splitlines()[last_idx + 1 :]
                    if line.strip() and not line.strip().upper().startswith("VERDICT:")
                ]
                if following_lines:
                    summary = following_lines[0][:300]
                else:
                    summary = "Code changes approved with backlog"
            else:
                summary = "Code changes approved by reviewer"
        else:
            following_lines = [
                line.strip()
                for line in text.splitlines()[last_idx + 1 :]
                if line.strip() and not line.strip().upper().startswith("VERDICT:")
            ]
            if following_lines:
                summary = following_lines[0][:300]
            else:
                blocker_lines = [line for line in lines if not line.upper().startswith("VERDICT:")]
                summary = blocker_lines[0][:300] if blocker_lines else "Reviewer blocked changes"
        return verdict, summary, text

    # Step 2: Fall back to existing keyword heuristics when no explicit verdict line exists.
    upper_text = text.upper()
    if "VERDICT:" in upper_text:
        return (
            ReviewVerdict.BLOCK,
            "Ambiguous or echoed verdict markers without explicit verdict line",
            text,
        )

    # Only texts containing no explicit verdict line reach here.  A bare
    # prose approval (``LGTM`` / ``APPROVED``) without the required
    # explicit ``VERDICT:`` line is NOT an authoritative PASS: the
    # fail-closed review protocol only honors the explicit verdict line,
    # so unstructured approval fails closed as BLOCK with a truthful
    # summary.  The verdict vocabulary itself is unchanged (no new
    # protocol); only the approval condition is tightened.
    if "LGTM" in upper_text or "APPROVED" in upper_text:
        return (
            ReviewVerdict.BLOCK,
            "Unstructured approval without explicit VERDICT line "
            f"(blocked): {lines[0][:200]}",
            text,
        )
    return ReviewVerdict.BLOCK, lines[0][:300], text


class ReviewEngine:
    """Performs read-only independent review of completed candidate runs."""

    def __init__(
        self,
        db: Database | None = None,
        registry: AdapterRegistry | None = None,
        readiness_service: _ReadinessProvider | None = None,
    ) -> None:
        self.db = db or Database()
        self.registry = registry or AdapterRegistry.default()
        # C15-RDY-02A: ``None`` is an explicit test/compatibility-only
        # disable of readiness admission.  Production construction never
        # passes ``None``: the ``OrchestratorService`` composition seam
        # always injects a provider (owner-state service or the
        # fail-closed unavailable fallback, whose UNKNOWN makes
        # installed reviewers skip).  A missing/broken authority must
        # never resolve to this disabled state.
        self.readiness_service = readiness_service

    def review_run(
        self,
        run_id: str,
        reviewer_override: WorkerCandidate | None = None,
        timeout: float = 300.0,
        review_limit: int | None = None,
        is_final: bool = False,
        final_attempt: int | None = None,
        final_limit: int | None = None,
    ) -> ReviewResult:
        """Review a completed run using a diverse reviewer model.

        ``review_limit`` is the bounded-mode budget: the maximum number of
        reviewer model calls actually launched. When omitted it is resolved from
        the run's persisted config, falling back to the canonical default.

        ``is_final`` marks the bounded final-convergence attempt. When true the
        FINAL CONVERGENCE notice is injected and a ``final_review_started``
        event is recorded.
        """
        if is_final:
            payload: dict[str, object] = {}
            if final_attempt is not None:
                payload["attempt"] = final_attempt
            if final_limit is not None:
                payload["limit"] = final_limit
            # Always record, even if values missing
            if not payload:
                payload = {"attempt": 0, "limit": 0}
            self.db.record_event(
                run_id,
                "final_review_started",
                payload=payload,
            )
        self.db.record_event(
            run_id,
            "review_started",
            payload={"run_id": run_id},
        )
        with measure_phase(
            self.db,
            run_id=run_id,
            phase=PhaseKind.REVIEW,
            source_kind="review.ReviewEngine.review_run",
            boundary_kind="review_execution",
            notes="C14-C: full review cycle wall-clock for this run "
            "(covers every reviewer candidate actually launched until "
            "a verdict is recorded).",
        ):
            try:
                return self._review_run_inner(
                    run_id=run_id,
                    reviewer_override=reviewer_override,
                    timeout=timeout,
                    review_limit=review_limit,
                    is_final=is_final,
                )
            except Exception as exc:
                self.db.record_event(
                    run_id,
                    "review_execution_failed",
                    payload={"error": str(exc)},
                )
                self.db.record_event(
                    run_id,
                    "review_failed",
                    payload={"error": str(exc)},
                )
                raise

    def _review_run_inner(
        self,
        run_id: str,
        reviewer_override: WorkerCandidate | None = None,
        timeout: float = 300.0,
        review_limit: int | None = None,
        is_final: bool = False,
    ) -> ReviewResult:
        run = self.db.get_run(run_id)
        if not run:
            raise ValueError(f"Run '{run_id}' not found")
        # A reviewable candidate is the latest SUCCESSFUL attempt's worktree,
        # which exists while the run is still RUNNING (candidate-ready is
        # only persisted after required review passes).  PENDING / FAILED /
        # ORPHANED runs have no reviewable candidate and fail closed here.
        if run.status not in (RunStatus.COMPLETED, RunStatus.RUNNING):
            raise ValueError(
                f"Cannot review run '{run_id}': status is {run.status.value}, "
                "expected COMPLETED or RUNNING"
            )

        # Reviewing an existing run must act on the repository that run was
        # created for, regardless of which project the UI currently shows.
        try:
            assert_run_repository(run)
        except ProvenanceError as exc:
            raise ValueError(f"Cannot review run '{run_id}': {exc}") from exc

        attempts = self.db.get_attempts_for_run(run_id)
        successful_attempts = [a for a in attempts if a.status == AttemptStatus.SUCCESS]
        if not successful_attempts:
            raise ValueError(f"Run '{run_id}' has no successful attempts to review")

        final_attempt = successful_attempts[-1]
        wt_path = Path(final_attempt.worktree_path)
        if not wt_path.exists():
            raise FileNotFoundError(f"Worktree path does not exist: {wt_path}")

        review_tier_decision = _review_tier_decision(run)
        review_tier = review_tier_decision.effective_tier
        self.db.record_event(
            run_id,
            "tier_decision",
            payload=review_tier_decision.as_payload(),
        )

        # Choose reviewer candidate ladder. Unfiltered ordering is used so that
        # training-policy, availability, quota, and transport exclusions are
        # surfaced as recorded skip events instead of being silently dropped.
        if reviewer_override is not None:
            ladder = [reviewer_override]
        else:
            ladder = select_reviewer_ladder_unfiltered(
                candidate_provider=final_attempt.provider,
                candidate_model=final_attempt.model,
                tier=review_tier,
            )

        # Gather evidence
        diff_text = GitManager.get_diff(wt_path, base_commit=run.base_commit)
        checks = self.db.get_checks_for_attempt(final_attempt.id)
        gate_info = (
            f"Gate command: '{checks[-1].gate_command}' (exit code: {checks[-1].exit_code})"
            if checks
            else "No gate check recorded"
        )

        prior_reviews = self.db.get_reviews_for_run(run_id)
        prior_review_context = ""
        if prior_reviews:
            prior_blocks = [r for r in prior_reviews if r.verdict == ReviewVerdict.BLOCK]
            if prior_blocks:
                # Only the MOST RECENT block is embedded: the candidate was
                # repaired specifically to address it, so it is the exact
                # blocking evidence this review must verify. Earlier rounds'
                # findings would otherwise be re-embedded, verbatim, into
                # every subsequent review prompt for the rest of the run --
                # duplicate reviewer prose that inflates the call without
                # adding information the repair loop has not already acted
                # on. Bounded deterministically (no model call) against a
                # single reviewer writing an unusually long finding.
                latest_block = prior_blocks[-1]
                raw_detail = (
                    latest_block.details.strip() if latest_block.details else latest_block.summary
                )
                detail = _bounded_tail(raw_detail)
                prior_review_context = (
                    "\n\nPrior Review Feedback (most recent blocking finding):\n"
                    f"--- {latest_block.provider}/{latest_block.model} ---\n"
                    f"Summary: {latest_block.summary}\n"
                    f"Feedback:\n{detail}"
                    + (
                        "\n\nNote: The candidate has been repaired to address prior feedback. "
                        "Verify that prior issues are resolved and no regressions were introduced."
                    )
                )

        if is_final:
            final_notice_block = f"\n\n{FINAL_CONVERGENCE_NOTICE}\n"
            final_instructions = (
                "This is the FINAL review. There is no further review cycle. "
                "You must return exactly one of:\n"
                "VERDICT: PASS\n"
                "VERDICT: PASS_WITH_BACKLOG\n"
                "VERDICT: FINAL_BLOCK\n"
                "Followed by concise feedback and, for PASS_WITH_BACKLOG, a backlog list."
            )
        else:
            final_notice_block = ""
            final_instructions = (
                "Review the diff for correctness, adherence to task, and regressions.\n"
                "Format your answer starting with:\n"
                "VERDICT: PASS\n"
                "or\n"
                "VERDICT: BLOCK\n"
                "Followed by concise feedback."
            )

        review_prompt = (
            "IMPORTANT: You have NO filesystem access to the repository under review. The complete "  # noqa: E501
            "candidate diff and task context are embedded in this prompt. Base your review SOLELY on the "  # noqa: E501
            "provided material - do not search the filesystem. Your response MUST begin with a line that "  # noqa: E501
            "is exactly VERDICT: PASS or VERDICT: BLOCK, followed by concise feedback.\n\n"  # noqa: E501
            "You are an independent, read-only code reviewer.\n"
            f"Task:\n{run.task}\n\n"
            f"Base commit: {run.base_commit}\n"
            f"Candidate commit: {final_attempt.commit_sha or 'HEAD'}\n"
            f"Gate verification: {gate_info}"
            f"{prior_review_context}"
            f"{final_notice_block}\n\n"
            f"Candidate diff:\n```diff\n{diff_text}\n```\n\n"
            "Instructions:\n"
            f"{final_instructions}"
        )

        import tempfile

        if review_limit is not None:
            limit = review_limit
        else:
            total_limit = _review_limit_from_run(run)
            launched_so_far = self.db.count_launched_reviews(run_id)
            limit = max(0, total_limit - launched_so_far)
        prompt_bytes = len(review_prompt.encode("utf-8"))
        quota_states = self.db.list_quota()
        review_config = None
        if run.config_json:
            raw_config: object = None
            try:
                raw_config = json.loads(run.config_json)
                if isinstance(raw_config, dict):
                    review_config = reconstruct_run_config(raw_config)
            except (ValueError, TypeError, json.JSONDecodeError) as exc:
                if isinstance(raw_config, dict) and "required_capabilities" in raw_config:
                    raise ValueError("run config contains invalid required_capabilities") from exc
                review_config = None
        if run.control_plane_snapshot_json is not None:
            frozen_control_plane = reconstruct_frozen_control_plane(
                run.control_plane_snapshot_json,
                run.control_plane_snapshot_digest,
            )
        elif run.control_plane_snapshot_digest is not None:
            raise ValueError("run has a control-plane digest without its snapshot")
        else:
            frozen_control_plane = legacy_frozen_control_plane(
                required_capabilities=(
                    review_config.required_capabilities if review_config is not None else ()
                ),
                reserve_override=(
                    review_config.reserve_override if review_config is not None else False
                ),
            )
        control_plane_policy = frozen_control_plane.policy

        # Bounded budget accounting: only ACTUALLY LAUNCHED reviewer model calls
        # consume a slot. Deterministic pre-dispatch skips do not.
        skipped_count = 0
        tried_count = 0
        unusable_count = 0
        unusable_notes: list[dict[str, str | None]] = []
        review_dispatches: list[DispatchEvidence] = []
        for candidate in ladder:
            if tried_count >= limit:
                break
            cand_adapter = self.registry.get(candidate.provider)
            if self.readiness_service is not None:
                readiness = self.readiness_service.admission_for(
                    provider=candidate.provider
                )
            else:
                readiness = None
            skip_reason = _pre_dispatch_skip_reason(
                candidate=candidate,
                adapter=cand_adapter,
                training_allowed=run.training_allowed,
                quota_states=quota_states,
                prompt_bytes=prompt_bytes,
                policy=control_plane_policy,
                required_capabilities=frozen_control_plane.required_capabilities,
                control_plane_digest=frozen_control_plane.digest,
                readiness_state=readiness.state if readiness is not None else None,
                readiness_reason=readiness.reason if readiness is not None else None,
                readiness_executable_available=(
                    readiness.executable_available if readiness is not None else True
                ),
            )
            decision = control_plane_decision(
                candidate=candidate,
                adapter=cand_adapter,
                training_allowed=run.training_allowed,
                quota_states=quota_states,
                prompt_bytes=prompt_bytes,
                policy=control_plane_policy,
                required_capabilities=frozen_control_plane.required_capabilities,
                control_plane_digest=frozen_control_plane.digest,
                readiness_state=readiness.state if readiness is not None else None,
                readiness_reason=readiness.reason if readiness is not None else None,
                readiness_executable_available=(
                    readiness.executable_available if readiness is not None else True
                ),
            )
            self.db.record_event(
                run_id,
                "control_plane_decision",
                payload=decision.as_payload(),
            )
            if skip_reason is not None:
                # Build shared payload and merge for backward compatibility
                from saberops.dispatch import build_skip_payload as _build_review_skip

                try:
                    max_b = cand_adapter.max_prompt_bytes() if cand_adapter else None
                except Exception:
                    max_b = None
                transport_v = (
                    cand_adapter.prompt_transport_for(prompt_bytes) if cand_adapter else "unknown"
                )
                shared = _build_review_skip(
                    role=DispatchRole.REVIEW,
                    candidate=candidate,
                    reason=skip_reason,
                    prompt_bytes=prompt_bytes,
                    max_prompt_bytes=max_b,
                    transport=transport_v,
                    explicit_override=False,
                )
                # Preserve legacy keys at top level for compatibility
                payload_merged = {
                    "provider": candidate.provider,
                    "model": candidate.model,
                    "reason": skip_reason,
                    **shared,
                }
                self.db.record_event(
                    run_id,
                    "review_candidate_skipped",
                    payload=payload_merged,
                )
                skipped_count += 1
                continue

            assert cand_adapter is not None
            tried_count += 1
            reviewer = candidate
            adapter = cand_adapter
            review_association_id = f"rev_{uuid.uuid4().hex[:10]}"
            self.db.record_event(
                run_id,
                "review_dispatch_started",
                payload={
                    "review_id": review_association_id,
                    "provider": reviewer.provider,
                    "model": reviewer.model,
                    "stage": DispatchStage.REVIEW.value,
                    "tier": review_tier.value,
                    "tier_reason": review_tier_decision.source,
                    "prompt_bytes": prompt_bytes,
                    "transport": adapter.prompt_transport_for(prompt_bytes),
                },
            )

            # Snapshot before review to ensure read-only invariant
            sha_before = GitManager.get_head_commit(wt_path)
            status_before = run_process(["git", "status", "--porcelain=v1"], cwd=wt_path).stdout
            diff_before = run_process(["git", "diff", "HEAD"], cwd=wt_path).stdout

            review_dispatch_evidence: DispatchEvidence | None = None
            try:
                with tempfile.TemporaryDirectory(prefix=f"orch_rev_{run_id}_") as rev_dir_str:
                    rev_dir = Path(rev_dir_str)
                    req = WorkerRequest(
                        task=review_prompt,
                        worktree_path=rev_dir,
                        model=reviewer.model,
                        timeout_seconds=timeout,
                        read_only=True,
                    )
                    routing_digest = None
                    if run.routing_snapshot_json:
                        try:
                            snapshot_payload = json.loads(run.routing_snapshot_json)
                            if isinstance(snapshot_payload, dict):
                                raw_digest = snapshot_payload.get("content_hash")
                                routing_digest = str(raw_digest) if raw_digest else None
                        except json.JSONDecodeError:
                            routing_digest = None
                    review_dispatch_evidence = new_dispatch_evidence(
                        run_id=run_id,
                        association_id=review_association_id,
                        review_id=None,
                        stage=DispatchStage.REVIEW,
                        role=DispatchRole.REVIEW,
                        tier=review_tier,
                        dispatch_reason=DispatchReason.INDEPENDENT_REVIEW,
                        requested_provider=reviewer.provider,
                        requested_model=reviewer.model,
                        task=req.task,
                        context_mode="REVIEW",
                        raw_parent_included=True,
                        prompt_transport=adapter.prompt_transport_for(prompt_bytes),
                        authority_mode=load_authority_settings().mode.value,
                        routing_snapshot_digest=routing_digest,
                        control_plane_digest=frozen_control_plane.digest,
                    )
                    self.db.create_dispatch_evidence(review_dispatch_evidence)
                    review_dispatches.append(review_dispatch_evidence)
                    worker_res = adapter.run(req)
            except Exception as exc:
                worker_res = WorkerResult(
                    provider=reviewer.provider,
                    model=reviewer.model,
                    status=AttemptStatus.FAILED,
                    summary="Reviewer adapter raised during dispatch",
                    stdout="",
                    stderr=str(exc),
                    error=str(exc),
                    outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
                )
                if review_dispatch_evidence is not None:
                    update_dispatch_from_result(review_dispatch_evidence, worker_res)
                    self.db.update_dispatch_evidence(review_dispatch_evidence)
            else:
                assert review_dispatch_evidence is not None
                update_dispatch_from_result(review_dispatch_evidence, worker_res)
                review_dispatch_evidence.candidate_sha = final_attempt.commit_sha
                self.db.update_dispatch_evidence(review_dispatch_evidence)

            if (
                worker_res.status is AttemptStatus.SUCCESS
                and self.readiness_service is not None
                and readiness is not None
                and readiness.connection_id is not None
            ):
                # Successful reviewer execution is direct evidence
                # for the exact connection used; failures record
                # nothing and never fabricate NOT_READY.
                self.readiness_service.record_success(readiness.connection_id)

            review_prompt_bytes = (
                worker_res.prompt_bytes if worker_res.prompt_bytes is not None else prompt_bytes
            )
            review_transport = worker_res.prompt_transport_used or adapter.prompt_transport_for(
                prompt_bytes
            )
            persist_worker_usage(
                self.db,
                run_id=run_id,
                association_id=review_association_id,
                attempt_id=None,
                review_id=None,
                stage=DispatchStage.REVIEW,
                tier=review_tier,
                provider=reviewer.provider,
                model=reviewer.model,
                prompt_bytes=review_prompt_bytes,
                transport=review_transport,
                result=worker_res,
            )

            # Invariant check: candidate worktree must remain unmutated
            sha_after = GitManager.get_head_commit(wt_path)
            status_after = run_process(["git", "status", "--porcelain=v1"], cwd=wt_path).stdout
            diff_after = run_process(["git", "diff", "HEAD"], cwd=wt_path).stdout

            has_mutated = (
                sha_before != sha_after
                or status_before != status_after
                or diff_before != diff_after
            )

            raw = (worker_res.stdout or "") + "\n" + (worker_res.stderr or "")
            raw_lower = raw.lower()
            is_usable = "verdict:" in raw_lower or "lgtm" in raw_lower or "approved" in raw_lower

            infrastructure_failure = worker_res.status != AttemptStatus.SUCCESS or not is_usable

            if infrastructure_failure:
                # Provider down, transport error, reviewer process timeout, or
                # empty/unusable output: never a substantive BLOCK.
                unusable_count += 1
                note: dict[str, str | None] = {
                    "provider": reviewer.provider,
                    "model": reviewer.model,
                    "status": worker_res.status.value,
                    "error": worker_res.error,
                    "stdout_tail": _bounded_tail(worker_res.stdout),
                    "stderr_tail": _bounded_tail(worker_res.stderr),
                }
                unusable_notes.append(note)
                self.db.record_event(
                    run_id,
                    "review_candidate_unusable",
                    payload=dict(note),
                )
                self.db.record_event(
                    run_id,
                    "review_execution_failed",
                    payload=dict(note),
                )
                if reviewer_override is not None or tried_count >= limit:
                    break
                else:
                    continue

            # Usable output from a successful reviewer: proceed to mutation
            # detection then parse into a substantive verdict.
            if has_mutated:
                # Do NOT destructive-reset or clean. Preserve mutated  # noqa: E501
                # worktree for forensic inspection.  # noqa: E501
                verdict = ReviewVerdict.BLOCK  # noqa: E501
                summary = (  # noqa: E501
                    "Reviewer violated read-only policy by mutating "  # noqa: E501
                    "candidate worktree (blocked)"  # noqa: E501
                )
                evidence_lines: list[str] = [
                    "Reviewer violated read-only policy by mutating candidate worktree (BLOCKED).",
                    f"Worktree path: {wt_path}",
                ]
                if sha_before != sha_after:
                    evidence_lines.append(f"HEAD changed: {sha_before} -> {sha_after}")
                if status_before != status_after:
                    status_b = status_before or "<clean>"
                    status_a = status_after or "<clean>"
                    evidence_lines.append(  # noqa: E501
                        f"Status changed:\nBefore:\n{status_b}\nAfter:\n{status_a}"  # noqa: E501
                    )
                if diff_before != diff_after:
                    diff_b = diff_before or "<empty>"
                    diff_a = diff_after or "<empty>"
                    evidence_lines.append(  # noqa: E501
                        f"Diff changed:\nBefore:\n{diff_b}\nAfter:\n{diff_a}"  # noqa: E501
                    )
                if worker_res.stdout or worker_res.stderr:
                    evidence_lines.append(  # noqa: E501
                        f"Reviewer output:\n{worker_res.stdout or worker_res.stderr}"  # noqa: E501
                    )
                details = "\n\n".join(evidence_lines)
            else:
                verdict, summary, details = parse_review_output(raw, is_final=is_final)

            review_id = review_association_id
            review = ReviewResult(
                id=review_id,
                run_id=run_id,
                provider=reviewer.provider,
                model=reviewer.model,
                verdict=verdict,
                summary=summary,
                details=details,
                created_at=current_iso_timestamp(),
            )
            self.db.create_review(review)
            self.db.link_model_calls_to_review(
                run_id,
                [review_dispatch_evidence.association_id]
                if review_dispatch_evidence is not None
                else [],
                review_id,
            )
            self.db.record_event(
                run_id,
                "review_completed",
                payload={
                    "review_id": review_id,
                    "provider": reviewer.provider,
                    "model": reviewer.model,
                    "verdict": verdict.value,
                    "summary": summary,
                    # Execution telemetry (never token telemetry): actual
                    # transport used for this dispatch when the adapter
                    # reports it, falling back to the adapter's static
                    # capability descriptor.
                    "prompt_bytes": review_prompt_bytes,
                    "transport": review_transport,
                    "stage": "review",
                    "tier": review_tier.value,
                    # Deterministic launch ordinal within this review_run
                    # call: tried_count was incremented immediately before
                    # THIS candidate was dispatched (bounded-budget
                    # accounting above), so it is already this launch's
                    # 1-indexed ordinal among actually-launched reviewer
                    # calls (pre-dispatch skips never increment it).
                    "attempt": tried_count,
                },
            )
            if verdict == ReviewVerdict.PASS_WITH_BACKLOG:
                self.db.record_event(
                    run_id,
                    "backlog_recorded",
                    payload={
                        "review_id": review_id,
                        "provider": reviewer.provider,
                        "model": reviewer.model,
                        "summary": summary,
                    },
                )
            if verdict == ReviewVerdict.REVIEW_EXECUTION_FAILED:
                self.db.record_event(
                    run_id,
                    "review_execution_failed",
                    payload={
                        "review_id": review_id,
                        "provider": reviewer.provider,
                        "model": reviewer.model,
                        "error": summary,
                    },
                )
            return review

        # Ladder exhausted without a substantive verdict (or the bounded budget
        # is spent). Infrastructure failures never surface as BLOCK; map them to
        # the explicit states.
        self.db.record_event(
            run_id,
            "review_ladder_exhausted",
            payload={
                "candidates_total": len(ladder),
                "candidates_tried": tried_count,
                "candidates_skipped": skipped_count,
                "candidates_unusable": unusable_count,
                "review_limit": limit,
            },
        )
        review_id = f"rev_{uuid.uuid4().hex[:10]}"
        if tried_count == 0:
            self.db.record_event(
                run_id,
                "no_eligible_candidate",
                payload={
                    "outcome": "NO_ELIGIBLE_CANDIDATE",
                    "stage": DispatchStage.REVIEW.value,
                    "candidates": [
                        {"provider": c.provider, "model": c.model} for c in ladder
                    ],
                },
            )
            verdict = ReviewVerdict.REVIEW_UNAVAILABLE
            summary = "No reviewer available"
            details = ""
        else:
            verdict = ReviewVerdict.REVIEW_EXECUTION_FAILED
            summary = "Reviewer execution failed (no usable verdict produced)"
            details = _format_infra_details(unusable_notes)
        review = ReviewResult(
            id=review_id,
            run_id=run_id,
            provider="",
            model="",
            verdict=verdict,
            summary=summary,
            details=details,
            created_at=current_iso_timestamp(),
        )
        self.db.create_review(review)
        self.db.link_model_calls_to_review(
            run_id,
            [dispatch.association_id for dispatch in review_dispatches],
            review_id,
        )
        self.db.record_event(
            run_id,
            "review_completed",
            payload={
                "review_id": review_id,
                "verdict": verdict.value,
                "summary": summary,
                "stage": DispatchStage.REVIEW.value,
                "tier": review_tier.value,
            },
        )
        if verdict == ReviewVerdict.REVIEW_EXECUTION_FAILED:
            self.db.record_event(
                run_id,
                "review_execution_failed",
                payload={
                    "review_id": review_id,
                    "verdict": verdict.value,
                    "summary": summary,
                },
            )
        if verdict == ReviewVerdict.PASS_WITH_BACKLOG:
            self.db.record_event(
                run_id,
                "backlog_recorded",
                payload={
                    "review_id": review_id,
                    "verdict": verdict.value,
                    "summary": summary,
                },
            )
        return review
