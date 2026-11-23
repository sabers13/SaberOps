"""Focused review-requirement regression tests (phase 1.1 H4).

Hermetic and offline: no provider, model, credential, or network access.

* a malformed persisted ``ReviewDecision`` can never become a review skip;
* a legacy risk-adaptive skip record no longer counts as "review not
  required", because the classifier it came from is gone;
* acceptance requires review unless the owner's frozen manual switch says
  otherwise;
* bare prose approvals (``LGTM`` / ``APPROVED``) and malformed or
  contradictory output fail closed instead of establishing PASS;
* valid explicit-verdict behavior (including final-round semantics) stays
  deterministic.
"""

from __future__ import annotations

import json
from collections.abc import Generator
from pathlib import Path

import pytest

from saberops.accept import AcceptEngine
from saberops.config import RunConfig
from saberops.db import Database
from saberops.models import (
    ReviewVerdict,
    Run,
    RunStatus,
    Tier,
)
from saberops.orchestrator import Orchestrator
from saberops.review import parse_review_output
from saberops.review_adaptive import ReviewDecision, mandatory_review_decision


@pytest.fixture()
def _isolate_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Keep construction away from any real SaberOps state on the machine."""
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / f"hermetic-{variable}"))
    yield


def _make_run(
    db: Database, run_id: str, *, config_json: str | None = None
) -> Run:
    run = Run(
        id=run_id,
        task="Update docs",
        target_repo="/tmp/saberops-v031-target",
        base_commit="0" * 40,
        tier=Tier.T1,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.RUNNING,
        created_at="2026-01-01T00:00:00Z",
        config_json=config_json,
    )
    db.create_run(run)
    return run


def _resolve_with_persisted(
    db: Database,
    run_id: str,
    persisted: str,
) -> ReviewDecision:
    db.update_review_risk_decision(run_id, persisted)
    orch = Orchestrator(db=db, readiness_service=None)
    config = RunConfig(task="Update docs", target_repo="/tmp/saberops-v031-target")
    decision = orch._resolve_c13_f_review_decision(run_id=run_id, config=config)
    assert isinstance(decision, ReviewDecision)
    return decision


_LEGACY_SKIP = {
    "schema_version": "review-adaptive/1",
    "decision_id": "seed-skip",
    "review_policy_mode": "RISK_ADAPTIVE",
    "owner_explicit_review": False,
    "is_autonomous": False,
    "is_high_t3": False,
    "independent_review_required": False,
    "review_requirement_source": "CLASSIFIER_LOW_SKIPPED",
    "review_skip_reason": "DOCS_ONLY delta with risk_level=LOW",
    "classification": {
        "change_class": "DOCS_ONLY",
        "risk_level": "LOW",
        "changed_paths": ["README.md"],
        "crosses_ownership_boundary": False,
        "changes_hard_governance": False,
        "changes_security_boundary": False,
        "changes_acceptance_or_gate_authority": False,
        "changes_publication_authority": False,
        "changes_recovery_or_exactly_once": False,
        "deterministic_coverage_available": True,
        "signals": ["docs_only_paths"],
        "reason": "DOCS_ONLY (LOW; signals=docs_only_paths)",
    },
}


# ---------------------------------------------------------------------------
# Frozen-decision invariant: malformed or legacy state fails closed
# ---------------------------------------------------------------------------


def test_malformed_persisted_garbage_cannot_become_skip(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    """Corrupt persisted bytes must require review."""
    db = Database(tmp_path / "orch.db")
    _make_run(db, "run-malformed-garbage")
    decision = _resolve_with_persisted(
        db, "run-malformed-garbage", "{not valid review-decision json"
    )
    assert decision.independent_review_required is True
    # The corrupt bytes are preserved as forensic evidence, not healed
    # into a re-derived answer.
    assert db.get_review_risk_decision_json("run-malformed-garbage") == (
        "{not valid review-decision json"
    )


def test_legacy_risk_adaptive_skip_cannot_become_skip(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    """A pre-1.1 classifier skip record is read as review-required."""
    db = Database(tmp_path / "orch.db")
    _make_run(db, "run-legacy-skip")
    decision = _resolve_with_persisted(
        db, "run-legacy-skip", json.dumps(_LEGACY_SKIP, sort_keys=True)
    )
    assert decision.independent_review_required is True


def test_owner_disabled_decision_is_honoured_only_with_the_switch(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    """The one manual switch is honoured; without it the same record fails closed."""
    db = Database(tmp_path / "orch.db")
    _make_run(db, "run-owner-off")
    off = mandatory_review_decision(decision_id="probe", owner_disabled=True)
    db.update_review_risk_decision("run-owner-off", off.to_json())
    orch = Orchestrator(db=db, readiness_service=None)
    without_switch = orch._resolve_c13_f_review_decision(
        run_id="run-owner-off",
        config=RunConfig(task="t", target_repo="/tmp/x"),
    )
    assert without_switch.independent_review_required is True

    with_switch = orch._resolve_c13_f_review_decision(
        run_id="run-owner-off",
        config=RunConfig(task="t", target_repo="/tmp/x", review_disabled=True),
    )
    assert with_switch.independent_review_required is False


def test_missing_decision_defaults_to_required(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    db = Database(tmp_path / "orch.db")
    _make_run(db, "run-fresh")
    orch = Orchestrator(db=db, readiness_service=None)
    decision = orch._resolve_c13_f_review_decision(
        run_id="run-fresh",
        config=RunConfig(task="t", target_repo="/tmp/x"),
    )
    assert decision.independent_review_required is True


def test_accept_engine_treats_malformed_persisted_decision_as_required(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    """Accept-side read path stays fail-closed on corrupt persisted state."""
    db = Database(tmp_path / "orch.db")
    _make_run(db, "run-accept-malformed")
    db.update_review_risk_decision("run-accept-malformed", "{corrupt")
    engine = AcceptEngine(db=db)
    assert engine._resolve_review_required_from_persisted_run("run-accept-malformed") is True


def test_accept_requires_review_without_owner_switch(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    """A build run with no decision and no switch still requires review."""
    db = Database(tmp_path / "orch.db")
    _make_run(
        db,
        "run-accept-no-decision",
        config_json=json.dumps({"review_enabled": True, "review_disabled": False}),
    )
    engine = AcceptEngine(db=db)
    assert engine._resolve_review_required_from_persisted_run("run-accept-no-decision") is True


def test_accept_honours_owner_switch(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    db = Database(tmp_path / "orch.db")
    _make_run(
        db,
        "run-accept-switch",
        config_json=json.dumps({"review_enabled": False, "review_disabled": True}),
    )
    engine = AcceptEngine(db=db)
    assert engine._resolve_review_required_from_persisted_run("run-accept-switch") is False


# ---------------------------------------------------------------------------
# Live review parser: fail-closed protocol
# ---------------------------------------------------------------------------


def test_plain_lgtm_cannot_pass() -> None:
    for text in ("LGTM", "LGTM, looks good to me", "lgtm"):
        verdict, _, _ = parse_review_output(text)
        assert verdict is ReviewVerdict.BLOCK, text


def test_plain_approved_cannot_pass() -> None:
    for text in ("APPROVED", "Approved - ship it"):
        verdict, summary, _ = parse_review_output(text)
        assert verdict is ReviewVerdict.BLOCK, text
        assert "VERDICT" in summary


def test_malformed_output_fails_closed() -> None:
    for text in ("", "   ", "VERDICT: FROBNICATE", "VERDICT: PASS please?"):
        verdict, _, _ = parse_review_output(text)
        assert verdict is ReviewVerdict.BLOCK, repr(text)


def test_contradictory_verdict_lines_fail_closed() -> None:
    assert parse_review_output("VERDICT: PASS\nGood\nVERDICT: BLOCK")[0] is ReviewVerdict.BLOCK
    assert parse_review_output("VERDICT: BLOCK\nBad\nVERDICT: PASS")[0] is ReviewVerdict.BLOCK
    conflict_final = parse_review_output(
        "VERDICT: PASS\nGood\nVERDICT: FINAL_BLOCK", is_final=True
    )
    assert conflict_final[0] is ReviewVerdict.BLOCK


def test_valid_explicit_verdicts_stay_deterministic() -> None:
    assert parse_review_output("VERDICT: PASS\nAll good")[0] is ReviewVerdict.PASS
    assert parse_review_output("verdict: pass\nAll good")[0] is ReviewVerdict.PASS
    assert parse_review_output("VERDICT: BLOCK\nNeeds work")[0] is ReviewVerdict.BLOCK
    # Echoing the same verdict twice is not a contradiction.
    assert parse_review_output("VERDICT: PASS\nNote\nVERDICT: PASS")[0] is ReviewVerdict.PASS
    # Deterministic across repeated parses.
    first = parse_review_output("VERDICT: PASS\nAll good")
    second = parse_review_output("VERDICT: PASS\nAll good")
    assert first == second


def test_final_round_semantics_preserved() -> None:
    assert parse_review_output("VERDICT: FINAL_BLOCK", is_final=False)[0] is ReviewVerdict.BLOCK
    assert (
        parse_review_output("VERDICT: FINAL_BLOCK", is_final=True)[0] is ReviewVerdict.FINAL_BLOCK
    )
    assert (
        parse_review_output("VERDICT: PASS_WITH_BACKLOG", is_final=False)[0]
        is ReviewVerdict.BLOCK
    )
    assert (
        parse_review_output("VERDICT: PASS_WITH_BACKLOG", is_final=True)[0]
        is ReviewVerdict.PASS_WITH_BACKLOG
    )
