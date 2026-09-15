"""C12-B context strategy arms (Section 9).

Implements four experimentable context-construction arms:

ARM A — AGGRESSIVE MINIMIZATION
    Smallest dependency-closed task context: only VOLATILE.

ARM B — STABLE PREFIX + SMALL DELTA
    Large deterministic STABLE prefix + bounded SEMI_STABLE + small VOLATILE.

ARM C — FULL DETERMINISTIC RECONSTRUCTION
    Full context reconstructed from authoritative durable state each time.

ARM D — NATIVE CONTINUATION
    Only if an existing provider exposes a safe measurable mechanism.
    Currently recorded as NOT_AVAILABLE; no provider-specific architecture added.

Invariants:
- No arm may modify authoritative Project Ledger state.
- Context caches are disposable; cold reconstruction must work correctly.
- No timestamps, session IDs, or execution IDs embed in context identity.
- ARM D absence is recorded explicitly, not fabricated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from saberops.replay.context_pack import (
    ContextClass,
    ContextPack,
    ContextSection,
    build_context_pack,
)
from saberops.replay.corpus import ReplayTaskDefinition

#: Canonical arm identifiers.
ARM_A_ID = "arm_a_aggressive_minimization"
ARM_B_ID = "arm_b_stable_prefix_delta"
ARM_C_ID = "arm_c_full_reconstruction"
ARM_D_ID = "arm_d_native_continuation"

#: Sentinel for when ARM D is not available.
ARM_D_NOT_AVAILABLE = "NOT_AVAILABLE"


@dataclass(frozen=True)
class ArmResult:
    """Result of building one context arm for an experiment run.

    Attributes:
        arm_id: Canonical arm identifier.
        context_pack: Built ContextPack (or None if arm is not available).
        available: False only for ARM D when no native continuation exists.
        not_available_reason: Human-readable reason when available=False.
        construction_evidence: Dict of additional context-construction metadata.
    """

    arm_id: str
    context_pack: ContextPack | None
    available: bool = True
    not_available_reason: str | None = None
    construction_evidence: dict[str, Any] = ()  # type: ignore[assignment]

    def __post_init__(self) -> None:
        # Coerce mutable default-like dict
        if self.construction_evidence == ():  # type: ignore[comparison-overlap]
            object.__setattr__(self, "construction_evidence", {})

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "arm_id": self.arm_id,
            "available": self.available,
            "not_available_reason": self.not_available_reason,
            "context_pack": self.context_pack.to_dict() if self.context_pack is not None else None,
            "construction_evidence": self.construction_evidence,
        }


# ---------------------------------------------------------------------------
# Helpers for reading durable sources deterministically
# ---------------------------------------------------------------------------

def _read_file_safe(path: Path) -> str:
    """Read file content or return empty string with a note."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _collect_stable_governance(repo_path: Path) -> list[ContextSection]:
    """Collect STABLE governance / architectural law context from durable sources."""
    sections: list[ContextSection] = []

    # Module ownership contracts (architecture/modules/*.toml)
    modules_dir = repo_path / "architecture" / "modules"
    if modules_dir.is_dir():
        toml_files = sorted(modules_dir.glob("*.toml"))
        combined = "\n\n".join(
            f"# {f.name}\n{_read_file_safe(f)}" for f in toml_files
        )
        if combined.strip():
            sections.append(ContextSection(
                class_=ContextClass.STABLE,
                label="module_ownership_contracts",
                content=combined,
                sources=tuple(str(f.relative_to(repo_path)) for f in toml_files),
            ))

    # AGENTS.md governance rules
    agents_md = repo_path / "AGENTS.md"
    if agents_md.exists():
        sections.append(ContextSection(
            class_=ContextClass.STABLE,
            label="governance_rules",
            content=_read_file_safe(agents_md),
            sources=("AGENTS.md",),
        ))

    # ROADMAP.md milestones and constraints
    roadmap_md = repo_path / "ROADMAP.md"
    if roadmap_md.exists():
        sections.append(ContextSection(
            class_=ContextClass.STABLE,
            label="roadmap_milestones",
            content=_read_file_safe(roadmap_md),
            sources=("ROADMAP.md",),
        ))

    return sections


def _collect_semi_stable_context(
    task_def: ReplayTaskDefinition,
    repo_path: Path,
) -> list[ContextSection]:
    """Collect SEMI_STABLE module/scope context for the task's expected_scope."""
    sections: list[ContextSection] = []

    # Architecture context docs for affected modules
    context_dir = repo_path / "architecture" / "context"
    affected_modules: set[str] = set()
    for scope_path in task_def.expected_scope:
        # Infer module name from scope path (e.g., "src/saberops/replay/")
        parts = Path(scope_path).parts
        for part in parts:
            if part not in ("src", "saberops", "tests", "architecture", "modules"):
                affected_modules.add(part.rstrip("/"))

    context_parts: list[str] = []
    sources: list[str] = []
    for module in sorted(affected_modules):
        ctx_file = context_dir / f"{module}.md"
        if ctx_file.exists():
            content = _read_file_safe(ctx_file)
            if content.strip():
                context_parts.append(f"## {module}\n{content}")
                sources.append(str(ctx_file.relative_to(repo_path)))

    if context_parts:
        sections.append(ContextSection(
            class_=ContextClass.SEMI_STABLE,
            label="module_context_closure",
            content="\n\n".join(context_parts),
            sources=tuple(sources),
        ))

    # Accepted milestone state (minimal)
    state_md = repo_path / "STATE.md"
    if state_md.exists():
        sections.append(ContextSection(
            class_=ContextClass.SEMI_STABLE,
            label="accepted_milestone_state",
            content=_read_file_safe(state_md),
            sources=("STATE.md",),
        ))

    return sections


def _build_volatile_task(task_def: ReplayTaskDefinition) -> list[ContextSection]:
    """Build VOLATILE context for the specific task objective."""
    task_payload = (
        f"Task ID: {task_def.task_id}\n"
        f"Project: {task_def.project_key}\n"
        f"Frozen Start: {task_def.frozen_start_ref}\n\n"
        f"Description:\n{task_def.description}\n\n"
        f"Objective:\n{task_def.objective}\n\n"
        f"Expected Scope:\n" + "\n".join(f"  - {s}" for s in task_def.expected_scope) + "\n\n"
        f"Verifier: {task_def.verifier.verifier_type}\n"
        f"Command: {' '.join(task_def.verifier.command)}\n"
    )
    return [
        ContextSection(
            class_=ContextClass.VOLATILE,
            label="task_objective",
            content=task_payload,
            sources=(f"corpus::{task_def.task_id}",),
        )
    ]


# ---------------------------------------------------------------------------
# ARM A: AGGRESSIVE MINIMIZATION
# ---------------------------------------------------------------------------

def build_arm_a(task_def: ReplayTaskDefinition, repo_path: Path) -> ArmResult:
    """ARM A — Aggressive Minimization.

    Smallest dependency-closed task context: VOLATILE only.
    STABLE = empty, SEMI_STABLE = empty.
    Tests whether minimal context is sufficient for verified task success.
    """
    sections = _build_volatile_task(task_def)
    pack = build_context_pack(sections)
    return ArmResult(
        arm_id=ARM_A_ID,
        context_pack=pack,
        available=True,
        construction_evidence={
            "strategy": "aggressive_minimization",
            "stable_sections": 0,
            "semi_stable_sections": 0,
            "volatile_sections": len(sections),
        },
    )


# ---------------------------------------------------------------------------
# ARM B: STABLE PREFIX + SMALL DELTA
# ---------------------------------------------------------------------------

def build_arm_b(task_def: ReplayTaskDefinition, repo_path: Path) -> ArmResult:
    """ARM B — Stable Prefix + Small Delta.

    Large deterministic STABLE prefix (governance + module ownership)
      + bounded SEMI_STABLE (module context closure)
      + small VOLATILE task delta.

    The stable prefix should be byte-identical across tasks with the same
    durable governance state (Section 16 cache-stability test).
    """
    stable_sections = _collect_stable_governance(repo_path)
    semi_stable_sections = _collect_semi_stable_context(task_def, repo_path)
    volatile_sections = _build_volatile_task(task_def)

    all_sections = stable_sections + semi_stable_sections + volatile_sections
    pack = build_context_pack(all_sections)

    return ArmResult(
        arm_id=ARM_B_ID,
        context_pack=pack,
        available=True,
        construction_evidence={
            "strategy": "stable_prefix_delta",
            "stable_sections": len(stable_sections),
            "semi_stable_sections": len(semi_stable_sections),
            "volatile_sections": len(volatile_sections),
            "rendered_prefix_digest": pack.rendered_prefix_digest,
            "stable_bytes": pack.stable_bytes,
            "semi_stable_bytes": pack.semi_stable_bytes,
            "volatile_bytes": pack.volatile_bytes,
        },
    )


# ---------------------------------------------------------------------------
# ARM C: FULL DETERMINISTIC RECONSTRUCTION
# ---------------------------------------------------------------------------

def build_arm_c(task_def: ReplayTaskDefinition, repo_path: Path) -> ArmResult:
    """ARM C — Full Deterministic Reconstruction.

    Full context reconstructed from authoritative durable state each time.
    All three classes populated from filesystem sources at call time.
    Baseline for comparison: what does full reconstruction cost vs. prefixed?
    """
    stable_sections = _collect_stable_governance(repo_path)
    semi_stable_sections = _collect_semi_stable_context(task_def, repo_path)
    volatile_sections = _build_volatile_task(task_def)

    # ARM C also reads pyproject.toml and all architecture context docs (fuller reconstruction)
    extra_stable: list[ContextSection] = []
    pyproject = repo_path / "pyproject.toml"
    if pyproject.exists():
        extra_stable.append(ContextSection(
            class_=ContextClass.STABLE,
            label="project_configuration",
            content=_read_file_safe(pyproject),
            sources=("pyproject.toml",),
        ))

    context_dir = repo_path / "architecture" / "context"
    if context_dir.is_dir():
        all_ctx_parts: list[str] = []
        all_ctx_sources: list[str] = []
        for ctx_file in sorted(context_dir.glob("*.md")):
            content = _read_file_safe(ctx_file)
            if content.strip():
                all_ctx_parts.append(f"## {ctx_file.stem}\n{content}")
                all_ctx_sources.append(str(ctx_file.relative_to(repo_path)))
        if all_ctx_parts:
            extra_stable.append(ContextSection(
                class_=ContextClass.STABLE,
                label="all_module_context",
                content="\n\n".join(all_ctx_parts),
                sources=tuple(all_ctx_sources),
            ))

    all_sections = stable_sections + extra_stable + semi_stable_sections + volatile_sections
    pack = build_context_pack(all_sections)

    return ArmResult(
        arm_id=ARM_C_ID,
        context_pack=pack,
        available=True,
        construction_evidence={
            "strategy": "full_reconstruction",
            "stable_sections": len(stable_sections) + len(extra_stable),
            "semi_stable_sections": len(semi_stable_sections),
            "volatile_sections": len(volatile_sections),
            "stable_bytes": pack.stable_bytes,
            "semi_stable_bytes": pack.semi_stable_bytes,
            "volatile_bytes": pack.volatile_bytes,
        },
    )


# ---------------------------------------------------------------------------
# ARM D: NATIVE CONTINUATION
# ---------------------------------------------------------------------------

def build_arm_d(
    task_def: ReplayTaskDefinition,
    repo_path: Path,
) -> ArmResult:
    """ARM D — Native Continuation.

    Only if an existing execution provider/backend exposes a safe measurable
    continuation mechanism without making session state authoritative.

    Currently: NOT_AVAILABLE — no provider exposes such a mechanism that
    meets the safety requirements.  Recorded explicitly, not fabricated.

    Returns an ArmResult with available=False and NOT_AVAILABLE evidence.
    """
    return ArmResult(
        arm_id=ARM_D_ID,
        context_pack=None,
        available=False,
        not_available_reason=(
            "No execution provider/backend currently exposes a safe, measurable "
            "native continuation mechanism that meets C12-B safety requirements "
            "(session state must not become Project authority; continuation must "
            "be verifiable without making provider session state required for "
            "Project reconstruction).  ARM D is recorded as NOT_AVAILABLE per "
            "Section 9 specification."
        ),
        construction_evidence={
            "strategy": "native_continuation",
            "availability": ARM_D_NOT_AVAILABLE,
            "reason": "no_safe_measurable_provider_continuation",
        },
    )


# ---------------------------------------------------------------------------
# Strategy registry
# ---------------------------------------------------------------------------

_ARM_BUILDERS = {
    ARM_A_ID: build_arm_a,
    ARM_B_ID: build_arm_b,
    ARM_C_ID: build_arm_c,
    ARM_D_ID: build_arm_d,
}

ALL_ARM_IDS: tuple[str, ...] = (ARM_A_ID, ARM_B_ID, ARM_C_ID, ARM_D_ID)


def build_all_arms(
    task_def: ReplayTaskDefinition,
    repo_path: Path,
) -> dict[str, ArmResult]:
    """Build all context strategy arms for the given task.

    Returns a mapping from arm_id → ArmResult.
    ARM D will always be present with available=False if no native
    continuation is available.

    Each arm's construction_evidence records the measured wall-clock
    construction latency under the ``_elapsed`` key (latency evidence only;
    it never contributes to context content identity).
    """
    results: dict[str, ArmResult] = {}
    for arm_id, builder in _ARM_BUILDERS.items():
        t0 = time.perf_counter()
        result = builder(task_def, repo_path)
        elapsed = time.perf_counter() - t0
        evidence = dict(result.construction_evidence)
        evidence["_elapsed"] = elapsed
        results[arm_id] = ArmResult(
            arm_id=result.arm_id,
            context_pack=result.context_pack,
            available=result.available,
            not_available_reason=result.not_available_reason,
            construction_evidence=evidence,
        )
    return results
