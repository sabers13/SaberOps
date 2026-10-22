"""Behavioral-policy injection into all implementation worker context modes."""

from __future__ import annotations

import pytest

from saberops.context_projection import ContextProjection, DispatchContextMode, render_projection
from saberops.validation_policy import VALIDATION_POLICY_MARKER, VALIDATION_POLICY_TEXT
from saberops.worker_engineering_policy import (
    WORKER_ENGINEERING_POLICY_MARKER,
    WORKER_ENGINEERING_POLICY_TEXT,
)


@pytest.mark.parametrize("mode", list(DispatchContextMode))
def test_worker_discipline_is_stable_and_precedes_frozen_validation(
    mode: DispatchContextMode,
) -> None:
    projection = ContextProjection(
        mode=mode,
        role="REPAIR" if mode is DispatchContextMode.REPAIR else "BULK",
        parent_slug="fix-example",
        inherited_candidate_sha="abc123",
        dispatch_reason="test",
        responsibility="Implement an authorized correction.",
        acceptance_criteria=("The relevant test passes.",),
    )
    kwargs = dict(
        validation_policy_text=VALIDATION_POLICY_TEXT,
        validation_policy_marker=VALIDATION_POLICY_MARKER,
        project_scope_text="[PROJECT SCOPE]\nOnly change src/example.py.",
        run_verification_text="[RUN VERIFICATION]\nUse focused checks.",
    )
    first = render_projection(projection, **kwargs)
    second = render_projection(projection, **kwargs)

    assert first.prompt == second.prompt
    assert first.prompt.count(WORKER_ENGINEERING_POLICY_TEXT) == 1
    assert first.prompt.count(WORKER_ENGINEERING_POLICY_MARKER) == 1
    assert first.prompt.count(VALIDATION_POLICY_MARKER) == 1
    assert first.validation_policy_marker_count == 1
    assert first.prompt.index(WORKER_ENGINEERING_POLICY_MARKER) < first.prompt.index(
        "[PROJECT SCOPE]"
    )
    assert first.prompt.index("[RUN VERIFICATION]") < first.prompt.index(
        VALIDATION_POLICY_MARKER
    )
    assert first.projected_prompt_bytes == len(first.prompt.encode("utf-8"))


def test_policy_does_not_drop_safeguards_or_create_new_review_authority() -> None:
    assert "error handling" in WORKER_ENGINEERING_POLICY_TEXT
    assert "concurrency safety" in WORKER_ENGINEERING_POLICY_TEXT
    assert "Do not touch unauthorized paths" in WORKER_ENGINEERING_POLICY_TEXT
    assert "do not create extra gates, reviews, or phases" in WORKER_ENGINEERING_POLICY_TEXT
    assert "Only the engine initiates mandatory independent reviews" in (
        WORKER_ENGINEERING_POLICY_TEXT
    )
