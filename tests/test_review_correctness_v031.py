"""Focused v0.3.1 review-correctness regression tests.

Hermetic and offline: no provider, model, credential, or network access.
Covers the three corrected review-classification defects plus the
fail-closed live review parser:

* a malformed persisted ``ReviewDecision`` can never be re-derived into
  a review skip (frozen-decision invariant);
* real normalized ``src/saberops/...`` paths classify on the correct
  authority surfaces (acceptance/gate, security, publication,
  recovery, planning, ordinary production);
* same-module ``src/...`` files do not falsely cross ownership
  boundaries, while true cross-owner changes still do;
* bare prose approvals (``LGTM`` / ``APPROVED``) and malformed or
  contradictory output fail closed instead of establishing PASS;
* valid explicit-verdict behavior (including final-round semantics)
  and the structured review machinery stay deterministic.
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
    ReviewPolicyMode,
    ReviewVerdict,
    RiskLevel,
    Run,
    RunStatus,
    Tier,
)
from saberops.orchestrator import Orchestrator
from saberops.process import run_process
from saberops.review import parse_review_output
from saberops.review_adaptive import (
    ChangeClass,
    ChangeClassification,
    ReviewDecision,
    ReviewRequirementSource,
    classify_change,
    decide_review,
)
from saberops.review_policy import ReviewProtocolError, parse_structured_review


@pytest.fixture()
def _isolate_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Generator[None, None, None]:
    """Keep construction away from any real SaberOps state on the machine."""
    for variable in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_CACHE_HOME"):
        monkeypatch.setenv(variable, str(tmp_path / f"hermetic-{variable}"))
    yield


def _make_run(db: Database, run_id: str, tier: Tier = Tier.T1) -> Run:
    run = Run(
        id=run_id,
        task="Update docs",
        target_repo="/tmp/saberops-v031-target",
        base_commit="0" * 40,
        tier=tier,
        training_allowed=True,
        gate_command="true",
        status=RunStatus.RUNNING,
        created_at="2026-01-01T00:00:00Z",
    )
    db.create_run(run)
    return run


def _init_docs_only_repo(path: Path) -> tuple[str, str]:
    """Return ``(base_commit, candidate_commit)`` with a docs-only delta."""
    path.mkdir(parents=True, exist_ok=True)
    run_process(["git", "init", "-b", "main"], cwd=path)
    run_process(["git", "config", "user.name", "Test Runner"], cwd=path)
    run_process(["git", "config", "user.email", "test@localhost"], cwd=path)
    (path / "README.md").write_text("# Base\n")
    run_process(["git", "add", "."], cwd=path)
    run_process(["git", "commit", "-m", "init"], cwd=path)
    base = run_process(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
    (path / "README.md").write_text("# Base\n\nMore docs.\n")
    run_process(["git", "add", "."], cwd=path)
    run_process(["git", "commit", "-m", "docs"], cwd=path)
    candidate = run_process(["git", "rev-parse", "HEAD"], cwd=path).stdout.strip()
    assert base != candidate
    return base, candidate


def _resolve_with_persisted(
    db: Database,
    run_id: str,
    persisted: str,
    *,
    base_commit: str,
    candidate_sha: str,
    target_repo: Path,
) -> ReviewDecision:
    db.update_review_risk_decision(run_id, persisted)
    orch = Orchestrator(db=db, readiness_service=None)
    config = RunConfig(task="Update docs", target_repo=str(target_repo))
    decision = orch._resolve_c13_f_review_decision(
        run_id=run_id,
        config=config,
        target_repo=target_repo,
        base_commit=base_commit,
        candidate_sha=candidate_sha,
    )
    assert isinstance(decision, ReviewDecision)
    return decision


# ---------------------------------------------------------------------------
# Frozen-decision invariant: malformed persisted state fails closed
# ---------------------------------------------------------------------------


def test_malformed_persisted_garbage_cannot_become_skip(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    """Corrupt persisted bytes must require review, even for a LOW diff.

    The docs-only delta below would classify LOW on re-derivation; the
    resolver must not reclassify it into a skip.
    """
    db = Database(tmp_path / "orch.db")
    repo = tmp_path / "repo"
    base, candidate = _init_docs_only_repo(repo)
    _make_run(db, "run-malformed-garbage")
    decision = _resolve_with_persisted(
        db,
        "run-malformed-garbage",
        "{not valid review-decision json",
        base_commit=base,
        candidate_sha=candidate,
        target_repo=repo,
    )
    assert decision.independent_review_required is True
    # The corrupt bytes are preserved as forensic evidence, not healed
    # into a re-derived answer.
    assert db.get_review_risk_decision_json("run-malformed-garbage") == (
        "{not valid review-decision json"
    )


def test_invariant_violating_persisted_skip_cannot_become_skip(
    tmp_path: Path, _isolate_xdg: Generator[None, None, None]
) -> None:
    """A persisted skip that contradicts its classification fails closed."""
    db = Database(tmp_path / "orch.db")
    repo = tmp_path / "repo"
    base, candidate = _init_docs_only_repo(repo)
    _make_run(db, "run-malformed-skip")
    low = classify_change(
        task_text="Update docs",
        changed_paths=("README.md",),
        tier=Tier.T1,
        autonomous=False,
    )
    skip = decide_review(
        classification=low,
        review_policy_mode=ReviewPolicyMode.RISK_ADAPTIVE,
        owner_explicit_review=False,
        is_autonomous=False,
        decision_id="seed-skip",
    )
    assert skip.independent_review_required is False
    payload = json.loads(skip.to_json())
    # Tamper: HIGH_T3 classification paired with "review not required".
    payload["classification"]["change_class"] = ChangeClass.HIGH_T3.value
    tampered = json.dumps(payload, sort_keys=True)
    decision = _resolve_with_persisted(
        db,
        "run-malformed-skip",
        tampered,
        base_commit=base,
        candidate_sha=candidate,
        target_repo=repo,
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


# ---------------------------------------------------------------------------
# Real normalized path classification (src/saberops/...)
# ---------------------------------------------------------------------------


def _classify(paths: tuple[str, ...]) -> ChangeClassification:
    return classify_change(
        task_text="Change",
        changed_paths=paths,
        tier=Tier.T1,
        autonomous=False,
    )


def _decide(classification: ChangeClassification) -> ReviewDecision:
    return decide_review(
        classification=classification,
        review_policy_mode=ReviewPolicyMode.RISK_ADAPTIVE,
        owner_explicit_review=False,
        is_autonomous=False,
        decision_id="probe",
    )


def test_acceptance_gate_authority_recognized() -> None:
    for path in ("src/saberops/accept.py", "saberops/accept.py"):
        classification = _classify((path,))
        assert classification.changes_acceptance_or_gate_authority is True
        assert classification.change_class is ChangeClass.HIGH_T3
        assert classification.risk_level is RiskLevel.HIGH
        decision = _decide(classification)
        assert decision.independent_review_required is True
        assert decision.review_requirement_source is ReviewRequirementSource.CLASSIFIER_HIGH_T3


def test_gate_runner_recognized_as_acceptance_authority() -> None:
    classification = _classify(("src/saberops/gate_runner.py",))
    assert classification.changes_acceptance_or_gate_authority is True
    assert _decide(classification).independent_review_required is True


def test_security_surface_recognized() -> None:
    for path in (
        "src/saberops/sandbox/policy.py",
        "src/saberops/control_plane/policy.py",
        "src/saberops/authority.py",
        "src/saberops/provenance.py",
    ):
        classification = _classify((path,))
        assert classification.changes_security_boundary is True, path
        assert classification.change_class is ChangeClass.HIGH_T3, path
        assert _decide(classification).independent_review_required is True, path


def test_publication_surface_recognized() -> None:
    for path in ("src/saberops/gateway/bridge.py", "src/saberops/publishing.py"):
        classification = _classify((path,))
        assert classification.changes_publication_authority is True, path
        assert _decide(classification).independent_review_required is True, path


def test_recovery_surface_recognized() -> None:
    for path in ("src/saberops/replay/engine.py", "src/saberops/db.py"):
        classification = _classify((path,))
        assert classification.changes_recovery_or_exactly_once is True, path
        assert _decide(classification).independent_review_required is True, path


def test_planning_surface_stays_low() -> None:
    for paths in (
        ("src/saberops/planning.py",),
        ("src/saberops/project_scope/graph.py",),
    ):
        classification = _classify(paths)
        assert classification.change_class is ChangeClass.PLANNING_DOCS
        assert classification.risk_level is RiskLevel.LOW
        decision = _decide(classification)
        assert decision.independent_review_required is False
        assert decision.review_requirement_source is ReviewRequirementSource.CLASSIFIER_LOW_SKIPPED


def test_ordinary_production_surface_stays_low() -> None:
    for paths in (
        ("src/saberops/telemetry.py",),
        ("src/saberops/observability/trace.py",),
    ):
        classification = _classify(paths)
        assert classification.change_class is ChangeClass.ORDINARY_PRODUCTION
        assert classification.risk_level is RiskLevel.LOW
        assert _decide(classification).independent_review_required is False


# ---------------------------------------------------------------------------
# Ownership-boundary classification
# ---------------------------------------------------------------------------


def test_same_module_files_do_not_cross_ownership() -> None:
    classification = _classify(
        ("src/saberops/gateway/bridge.py", "src/saberops/gateway/client.py")
    )
    assert classification.crosses_ownership_boundary is False
    assert "ownership_boundary_crossed" not in classification.signals


def test_true_cross_owner_change_still_crosses() -> None:
    classification = _classify(
        ("src/saberops/gateway/bridge.py", "src/saberops/replay/engine.py")
    )
    assert classification.crosses_ownership_boundary is True


def test_cross_owner_ordinary_files_escalate() -> None:
    classification = _classify(("src/saberops/telemetry.py", "src/saberops/classifier.py"))
    assert classification.crosses_ownership_boundary is True
    assert classification.change_class is ChangeClass.LOAD_BEARING_AUTHORITY
    assert _decide(classification).independent_review_required is True


def test_bare_package_paths_keep_ownership_semantics() -> None:
    same = _classify(("saberops/gateway/a.py", "saberops/gateway/b.py"))
    assert same.crosses_ownership_boundary is False
    crossed = _classify(("saberops/gateway/a.py", "saberops/replay/b.py"))
    assert crossed.crosses_ownership_boundary is True


def test_review_module_family_does_not_cross_ownership() -> None:
    classification = _classify(("src/saberops/review.py", "src/saberops/review_policy.py"))
    assert classification.crosses_ownership_boundary is False
    assert "ownership_boundary_crossed" not in classification.signals


def test_review_adaptive_module_family_does_not_cross_ownership() -> None:
    classification = _classify(("src/saberops/review.py", "src/saberops/review_adaptive.py"))
    assert classification.crosses_ownership_boundary is False
    assert "ownership_boundary_crossed" not in classification.signals


def test_related_top_level_families_share_one_domain() -> None:
    for paths in (
        ("src/saberops/routing.py", "src/saberops/routing_config.py"),
        ("src/saberops/routing_config.py", "src/saberops/routing_decision.py"),
        ("src/saberops/authority.py", "src/saberops/authority_sync.py"),
        ("src/saberops/orchestrator.py", "src/saberops/orchestrator_consumer.py"),
    ):
        classification = _classify(paths)
        assert classification.crosses_ownership_boundary is False, paths


def test_unrelated_top_level_modules_still_cross_ownership() -> None:
    for paths in (
        ("src/saberops/accept.py", "src/saberops/telemetry.py"),
        ("src/saberops/review.py", "src/saberops/telemetry.py"),
    ):
        classification = _classify(paths)
        assert classification.crosses_ownership_boundary is True, paths


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


def test_structured_review_machinery_stays_deterministic() -> None:
    approval = json.dumps({"verdict": "PASS", "summary": "clean", "findings": []})
    assert parse_structured_review(approval).verdict.value == "PASS"
    blocker = json.dumps(
        {
            "verdict": "BLOCK",
            "summary": "one defect",
            "findings": [
                {
                    "id": "F1",
                    "classification": "B2",
                    "title": "Off-by-one in retry bound",
                    "severity": "high",
                    "invariant_or_requirement": "retry count stays within bound",
                    "failure_scenario": "exhaustion then an extra attempt fires",
                    "evidence": "loop at worker.py:120 runs bound+1 times",
                    "minimum_correction": "clamp the loop to bound",
                    "repair_scope": "LOCAL_MECHANICAL",
                }
            ],
        }
    )
    assert parse_structured_review(blocker).verdict.value == "BLOCK"
    with pytest.raises(ReviewProtocolError):
        parse_structured_review("LGTM")
