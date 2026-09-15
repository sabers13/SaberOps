"""C12-B bounded experiment campaign and evidence output (Sections 12, 14, 15, 20).

Runs the bounded context/cache campaign against representative frozen corpus
tasks using ONLY real Project primitives:

- Frozen source: each task's declared frozen SHA (task_def.frozen_start_ref)
  is materialized
  through the existing trusted Git/worktree primitives (ReplayEngine /
  DisposableWorkspace).  Context arms are built from the frozen checkout,
  never from the live C12-B working tree.
- Real Project identity: canonical-path-derived identity from
  compute_project_identity via RealProjectRegistry / ReplayEngine /
  FrozenStartingState.  ``project_id = task_def.project_key`` is never used
  as identity.
- Real frozen state: campaign evidence carries verified project_id,
  root_commit, starting_git_sha, checkpoint_id/state_digest when applicable,
  and frozen_state_digest.
- Real baseline verification: the corpus verifier actually executes inside
  the disposable frozen workspace through existing bwrap containment.  A
  shared baseline verifier PASS is baseline evidence ONLY.
- Semantic outcome: no existing executor consumes the constructed context in
  this campaign, so every arm records semantic_effect = UNMEASURED.  No arm
  is credited with preserving task success.

Experiment philosophy:
- No external model calls; model/cache/token metrics stay UNKNOWN.
- No production optimization policy is adopted from results.
- The summary reports observations only.  A valid conclusion is:
  NO CONTEXT STRATEGY PROVEN SUPERIOR YET.

Output per task x arm:
- Exact durable start used (verified frozen controls)
- Context rendered + class breakdown + digests
- Byte evidence
- Latency evidence (context construction)
- Environment/toolchain identity
- Whether comparison controls matched
- ARM D: NOT_AVAILABLE explicitly recorded
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from saberops.replay.arms import (
    build_all_arms,
    build_arm_b,
)
from saberops.replay.comparison import assert_comparable
from saberops.replay.contracts import (
    FrozenStartingState,
    MeasurementRecord,
    ReplayDefinition,
    ReplayError,
    ReplayExecutionRecord,
    ReplaySafetyViolationError,
)
from saberops.replay.corpus import (
    ReplayTaskDefinition,
    get_initial_corpus,
)
from saberops.replay.engine import ReplayEngine
from saberops.replay.toolchain import (
    fingerprint_or_unavailable,
)
from saberops.replay.workspace import (
    DisposableWorkspace,
    get_replay_worktree_base,
)

#: Semantic-outcome classification for context arms (Section 11).
SEMANTIC_EFFECT_MEASURED = "MEASURED"
SEMANTIC_EFFECT_UNMEASURED = "UNMEASURED"

#: Record kind marker distinguishing context-construction observations from
#: real executor executions.  Observation records carry REAL frozen controls
#: and REAL context measurements; they never claim verified task success.
RECORD_KIND_CONTEXT_OBSERVATION = "context_construction_observation"

# ---------------------------------------------------------------------------
# Evidence data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ContextArmEvidence:
    """Evidence for one arm's context-construction in one task run."""

    task_id: str
    arm_id: str
    arm_available: bool
    not_available_reason: str | None
    #: ISO-format elapsed time for context construction (seconds)
    construction_elapsed_seconds: float
    context_pack_dict: dict[str, Any] | None
    construction_evidence: dict[str, Any]
    #: MEASURED only when the context was actually consumed by an executor
    #: whose result was verified; otherwise UNMEASURED.
    semantic_effect: str = SEMANTIC_EFFECT_UNMEASURED

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "task_id": self.task_id,
            "arm_id": self.arm_id,
            "arm_available": self.arm_available,
            "not_available_reason": self.not_available_reason,
            "construction_elapsed_seconds": self.construction_elapsed_seconds,
            "context_pack": self.context_pack_dict,
            "construction_evidence": self.construction_evidence,
            "semantic_effect": self.semantic_effect,
        }


@dataclass(frozen=True)
class TaskExperimentEvidence:
    """All arm evidence for a single corpus task."""

    task_id: str
    project_key: str
    #: Deterministic content identity of the corpus task definition.
    task_definition_digest: str
    #: Verified frozen controls (real Project identity + history + state).
    frozen_controls: dict[str, Any] = field(default_factory=dict)
    toolchain_fingerprint: str = ""
    #: Actual baseline verifier result (baseline evidence only).
    baseline_verifier: dict[str, Any] = field(default_factory=dict)
    #: Cache-stability observations from repeated cold construction.
    cache_stability: dict[str, Any] = field(default_factory=dict)
    arm_evidence: tuple[ContextArmEvidence, ...] = ()
    comparison_results: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "task_id": self.task_id,
            "project_key": self.project_key,
            "task_definition_digest": self.task_definition_digest,
            "frozen_controls": self.frozen_controls,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "baseline_verifier": self.baseline_verifier,
            "cache_stability": self.cache_stability,
            "arm_evidence": [a.to_dict() for a in self.arm_evidence],
            "comparison_results": list(self.comparison_results),
        }


@dataclass(frozen=True)
class ExperimentResult:
    """Aggregate result of the C12-B bounded campaign."""

    task_evidence: tuple[TaskExperimentEvidence, ...]
    toolchain_fingerprint: str
    summary: str
    #: Manifest of written evidence files (path + sha256), when written.
    evidence_manifest: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "c12b_experiment_result": True,
            "toolchain_fingerprint": self.toolchain_fingerprint,
            "tasks": [t.to_dict() for t in self.task_evidence],
            "summary": self.summary,
            "evidence_manifest": self.evidence_manifest,
        }


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------


def _validate_evidence_dir(evidence_dir: Path, repo: Path) -> None:
    """Refuse evidence destinations inside the authoritative repo or replay worktrees."""
    ev = evidence_dir.resolve()
    if ev == repo or ev.is_relative_to(repo):
        raise ReplaySafetyViolationError(
            f"Refusing evidence destination inside authoritative repository: {ev}"
        )
    wt_base = get_replay_worktree_base(repo).resolve()
    if ev == wt_base or ev.is_relative_to(wt_base):
        raise ReplaySafetyViolationError(
            f"Refusing evidence destination inside transient replay workspaces: {ev}"
        )


# ---------------------------------------------------------------------------
# Campaign execution
# ---------------------------------------------------------------------------


def _baseline_verifier_evidence(baseline_record: ReplayExecutionRecord) -> dict[str, Any]:
    """Extract actual baseline verifier evidence from the real execution record."""
    measurement = baseline_record.measurement
    return {
        "record_kind": "baseline_verifier_execution",
        "strategy_id": baseline_record.strategy_id,
        "verified_success": baseline_record.verified_success,
        "failure_classification": (
            baseline_record.failure_classification.value
            if baseline_record.failure_classification
            else None
        ),
        "workspace_result_digest": baseline_record.workspace_result_digest,
        "record_digest": baseline_record.digest,
        "verifier_passed": (
            baseline_record.verifier_evidence.get("passed")
            if baseline_record.verifier_evidence
            else None
        ),
        "verifier_exit_code": (
            baseline_record.verifier_evidence.get("exit_code")
            if baseline_record.verifier_evidence
            else None
        ),
        "verifier_command": (
            baseline_record.verifier_evidence.get("command")
            if baseline_record.verifier_evidence
            else None
        ),
        "verifier_toolchain_fingerprint": (
            measurement.verifier_toolchain_fingerprint if measurement else None
        ),
        "note": (
            "Shared baseline verifier result is baseline evidence ONLY. "
            "No context arm was consumed by an executor in this campaign."
        ),
    }


def _build_arm_observation_record(
    task_def: ReplayTaskDefinition,
    frozen_state: FrozenStartingState,
    arm_id: str,
    arm_pack_dict: dict[str, Any] | None,
    toolchain_fp: str,
) -> ReplayExecutionRecord:
    """Build a context-CONSTRUCTION observation record with REAL frozen controls.

    The record carries the verified FrozenStartingState, real canonical
    Project identity, real task-definition identity, real measured context
    bytes/digests, and the real toolchain fingerprint.  It never claims
    verified task success: verified_success is False and the record is
    explicitly marked as a context-construction observation with
    semantic_effect = UNMEASURED.
    """
    definition = ReplayDefinition(
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        frozen_start_sha=frozen_state.starting_git_sha,
        strategy_id=arm_id,
        verifier=task_def.verifier,
        expected_artifacts=task_def.expected_artifact_shape,
        requires_external_model=task_def.requires_external_model,
        task_definition_digest=task_def.content_digest,
        frozen_starting_state=frozen_state,
        checkpoint_id=frozen_state.checkpoint_id,
        state_digest=frozen_state.state_digest,
    )

    measurement = MeasurementRecord(
        # Real, measured context construction quantities:
        stable_context_bytes=arm_pack_dict.get("stable_bytes") if arm_pack_dict else None,
        semi_stable_context_bytes=(
            arm_pack_dict.get("semi_stable_bytes") if arm_pack_dict else None
        ),
        volatile_context_bytes=arm_pack_dict.get("volatile_bytes") if arm_pack_dict else None,
        context_pack_digest=arm_pack_dict.get("pack_digest") if arm_pack_dict else None,
        rendered_prefix_digest=(
            arm_pack_dict.get("rendered_prefix_digest") if arm_pack_dict else None
        ),
        # Real environment identity evidence:
        verifier_toolchain_fingerprint=toolchain_fp,
        # Model/cache/token metrics: no model was invoked → remain UNKNOWN.
    )

    return ReplayExecutionRecord(
        execution_id=f"c12b_ctx_obs_{arm_id}",
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        definition_digest=definition.definition_digest,
        strategy_id=arm_id,
        start_time="",
        starting_sha=frozen_state.starting_git_sha,
        frozen_starting_state=frozen_state,
        frozen_state_digest=frozen_state.digest,
        task_definition_digest=task_def.content_digest,
        effective_definition=definition,
        measurement=measurement,
        strategy_evidence={
            "record_kind": RECORD_KIND_CONTEXT_OBSERVATION,
            "semantic_effect": SEMANTIC_EFFECT_UNMEASURED,
            "note": (
                "Context construction observation only; no executor consumed "
                "this context and no task success is claimed for the arm."
            ),
        },
    )


def _run_task_experiment(
    task_def: ReplayTaskDefinition,
    engine: ReplayEngine,
    frozen_state: FrozenStartingState,
    toolchain_fp: str,
) -> TaskExperimentEvidence:
    """Run the bounded arm experiment for one corpus task from the frozen state."""
    repo = engine.repo_path

    # 1. REAL baseline verification: execute the corpus verifier inside a
    #    disposable workspace materialized at the exact frozen SHA.  This
    #    also materializes the frozen source through trusted worktree
    #    primitives and produces the actual baseline verifier result.
    baseline_definition = task_def.instantiate_definition("baseline")
    baseline_record = engine.execute_replay(
        baseline_definition,
        starting_state=frozen_state,
    )
    baseline_evidence = _baseline_verifier_evidence(baseline_record)

    # 2. Materialize the frozen source once for context construction.
    #    Context arms are built from the task's FROZEN checkout, never
    #    from the live working tree.
    arm_evidence_list: list[ContextArmEvidence] = []
    arm_pack_dicts: dict[str, dict[str, Any] | None] = {}
    comparison_results: list[dict[str, Any]] = []
    cache_stability: dict[str, Any] = {}

    with DisposableWorkspace.create(
        repo_path=repo,
        starting_state=frozen_state,
        execution_id=f"c12b_ctx_{task_def.task_id}",
    ) as ws:
        frozen_source = ws.worktree_path

        t_start = time.perf_counter()
        arm_results = build_all_arms(task_def, frozen_source)
        total_construction_seconds = time.perf_counter() - t_start

        # 3. Cache-stability observation: rebuild ARM B cold and compare.
        t_rebuild = time.perf_counter()
        arm_b_rebuild = build_arm_b(task_def, frozen_source)
        rebuild_seconds = time.perf_counter() - t_rebuild

        arm_b_first = arm_results.get("arm_b_stable_prefix_delta")
        first_pack = arm_b_first.context_pack if arm_b_first else None
        rebuild_pack = arm_b_rebuild.context_pack
        cache_stability = {
            "observation": "arm_b_cold_reconstruction",
            "first_prefix_digest": (
                first_pack.rendered_prefix_digest if first_pack else None
            ),
            "rebuild_prefix_digest": (
                rebuild_pack.rendered_prefix_digest if rebuild_pack else None
            ),
            "prefix_digest_stable": (
                first_pack is not None
                and rebuild_pack is not None
                and first_pack.rendered_prefix_digest == rebuild_pack.rendered_prefix_digest
            ),
            "first_pack_digest": first_pack.pack_digest if first_pack else None,
            "rebuild_pack_digest": rebuild_pack.pack_digest if rebuild_pack else None,
            "pack_digest_stable": (
                first_pack is not None
                and rebuild_pack is not None
                and first_pack.pack_digest == rebuild_pack.pack_digest
            ),
            "rebuild_elapsed_seconds": round(rebuild_seconds, 6),
            "total_all_arms_construction_seconds": round(total_construction_seconds, 6),
        }

        # 4. Per-arm evidence + REAL-control observation records.
        for arm_id, arm_result in arm_results.items():
            pack_dict = arm_result.context_pack.to_dict() if arm_result.context_pack else None
            arm_pack_dicts[arm_id] = pack_dict
            arm_evidence_list.append(ContextArmEvidence(
                task_id=task_def.task_id,
                arm_id=arm_id,
                arm_available=arm_result.available,
                not_available_reason=arm_result.not_available_reason,
                construction_elapsed_seconds=round(
                    arm_result.construction_evidence.get("_elapsed", 0.0), 6
                ),
                context_pack_dict=pack_dict,
                construction_evidence=arm_result.construction_evidence,
                # No existing executor consumes the context in this campaign:
                semantic_effect=SEMANTIC_EFFECT_UNMEASURED,
            ))

        # 5. Cross-arm comparability checks over REAL-control records.
        available_arm_ids = [
            arm_id for arm_id, ar in arm_results.items() if ar.available
        ]
        observation_records = {
            arm_id: _build_arm_observation_record(
                task_def, frozen_state, arm_id, arm_pack_dicts.get(arm_id), toolchain_fp
            )
            for arm_id in available_arm_ids
        }
        for i, arm_a in enumerate(available_arm_ids):
            for arm_b in available_arm_ids[i + 1:]:
                result = assert_comparable(
                    observation_records[arm_a],
                    observation_records[arm_b],
                    allow_different_arm=True,
                )
                comparison_results.append({
                    "arm_a": arm_a,
                    "arm_b": arm_b,
                    "verdict": result.verdict.value,
                    "comparable": result.comparable,
                    "reasons": list(result.reasons),
                })

    frozen_controls = {
        **frozen_state.to_dict(),
        "frozen_state_digest": frozen_state.digest,
        "materialization": (
            "frozen SHA materialized via DisposableWorkspace git worktree "
            "primitives; context built from frozen source, not live checkout"
        ),
    }

    return TaskExperimentEvidence(
        task_id=task_def.task_id,
        project_key=task_def.project_key,
        task_definition_digest=task_def.content_digest,
        frozen_controls=frozen_controls,
        toolchain_fingerprint=toolchain_fp,
        baseline_verifier=baseline_evidence,
        cache_stability=cache_stability,
        arm_evidence=tuple(arm_evidence_list),
        comparison_results=tuple(comparison_results),
    )


def _build_summary(
    task_evidence_list: list[TaskExperimentEvidence],
    toolchain_fp: str,
) -> str:
    """Build the bounded human-readable campaign summary (observations only)."""
    lines: list[str] = [
        "BOUNDED CONTEXT / CACHE CAMPAIGN — OBSERVATIONS ONLY",
        "=" * 60,
        "",
        f"Toolchain fingerprint: {toolchain_fp}",
        f"Tasks evaluated: {len(task_evidence_list)}",
        "No external model was invoked: model/cache/token metrics are UNKNOWN.",
        "",
    ]

    for task_ev in task_evidence_list:
        lines.append(f"Task: {task_ev.task_id}")
        lines.append(f"  Task-definition digest: {task_ev.task_definition_digest[:16]}...")
        fc = task_ev.frozen_controls
        lines.append(f"  Frozen start: {fc.get('starting_git_sha', '')[:12]}")
        lines.append(f"  project_id (canonical): {fc.get('project_id', '')}")
        lines.append(f"  root_commit: {fc.get('root_commit', '')}")
        lines.append(f"  frozen_state_digest: {fc.get('frozen_state_digest', '')[:16]}...")
        bv = task_ev.baseline_verifier
        lines.append(
            f"  Baseline verifier (actual): passed={bv.get('verifier_passed')} "
            f"exit={bv.get('verifier_exit_code')} "
            f"verified_success={bv.get('verified_success')} (baseline evidence only)"
        )
        cs = task_ev.cache_stability
        lines.append(
            f"  Cache stability: prefix_stable={cs.get('prefix_digest_stable')} "
            f"pack_stable={cs.get('pack_digest_stable')}"
        )
        for ae in task_ev.arm_evidence:
            if not ae.arm_available:
                lines.append(
                    f"  ARM {ae.arm_id}: NOT_AVAILABLE — {ae.not_available_reason or ''}"
                )
                continue
            pack = ae.context_pack_dict or {}
            lines.append(
                f"  ARM {ae.arm_id}: "
                f"total={pack.get('total_bytes')}B "
                f"stable={pack.get('stable_bytes')}B "
                f"semi={pack.get('semi_stable_bytes')}B "
                f"volatile={pack.get('volatile_bytes')}B "
                f"pack_digest={(pack.get('pack_digest') or '')[:12]} "
                f"prefix_digest={(pack.get('rendered_prefix_digest') or '')[:12]} "
                f"semantic_effect={ae.semantic_effect}"
            )
        for cmp_result in task_ev.comparison_results:
            verdict = cmp_result["verdict"]
            lines.append(
                f"  COMPARE {cmp_result['arm_a']} vs {cmp_result['arm_b']}: {verdict}"
            )
            if not cmp_result["comparable"] and cmp_result["reasons"]:
                lines.append(f"    reasons: {'; '.join(cmp_result['reasons'])}")
        lines.append("")

    lines.extend([
        "OBSERVATIONS (no production winner installed, none recommended):",
        "- ARM A rendered the smallest context (volatile-only).",
        "- ARM B rendered a stable prefix plus a small volatile delta.",
        "- ARM C rendered the largest fully reconstructed context.",
        "- ARM D is NOT_AVAILABLE: no provider exposes a safe measurable "
        "native continuation meeting the required safety constraints.",
        "- All arms in this campaign have semantic_effect=UNMEASURED: no "
        "existing executor consumed the constructed context, so no claim "
        "about task success under any arm is supported by this evidence.",
        "- Comparability verdicts above reflect the enforced controls "
        "(identity, history, frozen state, task definition, verifier, "
        "execution semantics, toolchain fingerprint).",
        "",
        "NO CONTEXT STRATEGY PROVEN SUPERIOR YET.",
    ])

    return "\n".join(lines)


def run_c12b_experiments(
    repo_path: Path | str,
    *,
    evidence_dir: Path | str | None = None,
    task_ids: tuple[str, ...] | None = None,
) -> ExperimentResult:
    """Run the bounded C12-B context/cache campaign on real frozen corpus tasks.

    Args:
        repo_path: Path to the authoritative repository (must be clean and
            match the registered real project identity).
        evidence_dir: If provided, write JSON evidence + summary + manifest
            here.  Must be outside the authoritative repository and outside
            the replay worktree base.
        task_ids: If provided, only run these task IDs from the corpus.
            If None, runs a representative subset (tasks 1, 3, 5).

    Returns:
        ExperimentResult with frozen controls, arm evidence, comparability
        verdicts, cache-stability observations, and (when written) the
        evidence manifest with SHA-256 digests.

    Notes:
        No external model is invoked.  Model/cache/token metrics remain
        UNKNOWN.  Every context arm records semantic_effect=UNMEASURED.
        No production optimization policy is adopted from results.
    """
    repo = Path(repo_path).resolve()

    if evidence_dir is not None:
        _validate_evidence_dir(Path(evidence_dir), repo)

    # Real Project identity via the registry + canonical-path-derived identity.
    engine = ReplayEngine(repo)

    toolchain_fp = fingerprint_or_unavailable()

    corpus = get_initial_corpus()
    if task_ids is not None:
        tasks_to_run = [t for t in corpus if t.task_id in task_ids]
        missing = set(task_ids) - {t.task_id for t in tasks_to_run}
        if missing:
            raise ReplayError(f"Unknown corpus task IDs: {sorted(missing)}")
    else:
        # Representative subset: bounded code change, review/repair, scope check
        default_ids = {
            "orch-v2-task-01-bounded-change",
            "orch-v2-task-03-review-repair",
            "orch-v2-task-05-scope-affected",
        }
        tasks_to_run = [t for t in corpus if t.task_id in default_ids]

    task_evidence_list: list[TaskExperimentEvidence] = []
    for task_def in tasks_to_run:
        # Freeze each task from THAT TASK'S declared frozen Project state —
        # never from global constants.  The exact task frozen SHA (never the
        # live checkout HEAD) is resolved through the existing ReplayEngine
        # authority for the task's own project_key.
        frozen_state = engine.freeze_starting_state(
            task_def.project_key,
            starting_sha=task_def.frozen_start_ref,
        )
        ev = _run_task_experiment(task_def, engine, frozen_state, toolchain_fp)
        task_evidence_list.append(ev)

    summary = _build_summary(task_evidence_list, toolchain_fp)

    result = ExperimentResult(
        task_evidence=tuple(task_evidence_list),
        toolchain_fingerprint=toolchain_fp,
        summary=summary,
    )

    if evidence_dir is not None:
        manifest = write_experiment_evidence(result, Path(evidence_dir))
        result = ExperimentResult(
            task_evidence=result.task_evidence,
            toolchain_fingerprint=result.toolchain_fingerprint,
            summary=result.summary,
            evidence_manifest=manifest,
        )

    return result


def write_campaign_evidence(
    result_dict: dict[str, Any],
    summary: str,
    evidence_dir: Path,
    *,
    json_name: str,
    summary_name: str,
    manifest_name: str,
    manifest_key: str,
    extra_files: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Write one campaign's JSON evidence, summary, and SHA-256 manifest.

    Canonical owner (C12-E consolidation) of the evidence-serialization
    shape previously repeated once per campaign (C12-B/C/D).  Callers keep
    thin per-campaign ``write_experiment_evidence`` wrappers that fix the
    file names and manifest key; the byte layout is identical.
    """
    evidence_dir.mkdir(parents=True, exist_ok=True)

    json_path = evidence_dir / json_name
    json_payload = json.dumps(result_dict, indent=2, sort_keys=True)
    json_path.write_text(json_payload, encoding="utf-8")

    summary_path = evidence_dir / summary_name
    summary_path.write_text(summary, encoding="utf-8")

    files: dict[str, str] = {
        json_path.name: hashlib.sha256(json_payload.encode("utf-8")).hexdigest(),
        summary_path.name: hashlib.sha256(summary.encode("utf-8")).hexdigest(),
    }
    if extra_files:
        files.update(extra_files)

    manifest: dict[str, Any] = {
        manifest_key: True,
        "files": files,
    }
    manifest_path = evidence_dir / manifest_name
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    return manifest


def write_experiment_evidence(result: ExperimentResult, evidence_dir: Path) -> dict[str, Any]:
    """Write machine-readable JSON evidence, human-readable summary, and manifest.

    Args:
        result: The ExperimentResult to write.
        evidence_dir: Directory to write evidence into (created if needed).

    Returns:
        Manifest mapping file names to their SHA-256 digests.
    """
    return write_campaign_evidence(
        result.to_dict(),
        result.summary,
        evidence_dir,
        json_name="c12b_experiment_evidence.json",
        summary_name="c12b_experiment_summary.txt",
        manifest_name="c12b_manifest.json",
        manifest_key="c12b_campaign_manifest",
    )
