"""Frozen replay corpus definitions for real-project replay.

Strictly adheres to Section 7:
- Describes tasks, does NOT execute optimization experiments yet.
- Distinguishes replay definition from replay execution from replay measurement.
- No development-project tasks ship as built-in defaults. Owners supply
  replay task definitions explicitly; the default bundled corpus is
  intentionally empty.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from orchestrator_mvp.replay.contracts import (
    DeterministicVerifier,
    ReplayDefinition,
    ReplayError,
    digest_of,
)


@dataclass(frozen=True)
class ReplayTaskDefinition:
    """Specification of one frozen replay task in the corpus."""

    task_id: str
    project_key: str
    frozen_start_ref: str
    description: str
    objective: str
    expected_scope: tuple[str, ...]
    verifier: DeterministicVerifier
    permitted_strategies: tuple[str, ...]
    expected_artifact_shape: tuple[str, ...]
    requires_external_model: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Canonical dictionary representation."""
        return {
            "task_id": self.task_id,
            "project_key": self.project_key,
            "frozen_start_ref": self.frozen_start_ref,
            "description": self.description,
            "objective": self.objective,
            "expected_scope": list(self.expected_scope),
            "verifier": self.verifier.to_dict(),
            "permitted_strategies": list(self.permitted_strategies),
            "expected_artifact_shape": list(self.expected_artifact_shape),
            "requires_external_model": self.requires_external_model,
        }

    @property
    def content_digest(self) -> str:
        """Deterministic content identity of this task definition.

        Two definitions sharing a task_id but differing in ANY semantic field
        (description, objective, expected_scope, verifier, permitted
        strategies, artifact shape, model requirement, or frozen baseline)
        MUST produce different digests.  Only the exact same semantic content
        produces the same digest.
        """
        return digest_of(self.to_dict())

    def instantiate_definition(
        self,
        strategy_id: str,
        strategy_params: dict[str, Any] | None = None,
    ) -> ReplayDefinition:
        """Create a deterministic ReplayDefinition for a specific strategy arm."""
        if strategy_id not in self.permitted_strategies:
            raise ReplayError(
                f"Strategy '{strategy_id}' is not permitted for task '{self.task_id}'. "
                f"Permitted strategies: {', '.join(self.permitted_strategies)}"
            )

        return ReplayDefinition(
            task_id=self.task_id,
            project_key=self.project_key,
            frozen_start_sha=self.frozen_start_ref,
            strategy_id=strategy_id,
            strategy_params=dict(sorted((strategy_params or {}).items())),
            verifier=self.verifier,
            expected_artifacts=self.expected_artifact_shape,
            requires_external_model=self.requires_external_model,
            task_definition_digest=self.content_digest,
        )


#: Default bundled replay task corpus. Intentionally empty: no
#: development-project tasks ship as built-in product data. Owners supply
#: replay task definitions explicitly; nothing here invents or bundles
#: task provenance.
INITIAL_ORCHESTRATOR_CORPUS: tuple[ReplayTaskDefinition, ...] = ()


def get_initial_corpus() -> tuple[ReplayTaskDefinition, ...]:
    """Return bundled replay tasks (none ship by default)."""
    return INITIAL_ORCHESTRATOR_CORPUS


def get_corpus_task(task_id: str) -> ReplayTaskDefinition | None:
    """Retrieve a specific replay task definition from the bundled corpus."""
    for task in INITIAL_ORCHESTRATOR_CORPUS:
        if task.task_id == task_id:
            return task
    return None
