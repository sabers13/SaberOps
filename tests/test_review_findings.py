"""Structured review findings: parsed, enforced, persisted and shown in the UI."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest

from saberops.config import resolve_run_config
from saberops.control_plane import (
    BindingRole,
    OwnerBindings,
    materialize_discovered_binding,
    save_owner_bindings,
    upsert_owner_binding,
)
from saberops.db import Database
from saberops.models import (
    AttemptStatus,
    ReviewVerdict,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator import Orchestrator
from saberops.review import render_findings, structured_review_outcome
from saberops.review_policy import (
    FINDINGS_PROMPT,
    ReviewPolicyVerdict,
    ReviewProtocolError,
    _parse_finding,
    _unique_finding_id,
    _unstructured_finding_note,
    compute_disposition,
    parse_review_findings,
    validate_finding_evidence,
)
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

PROVIDER = "opencode"


def _write_synthetic_verified_postures(config_home: Path) -> None:
    """Seed an isolated owner config with a verified posture for the synthetic
    ``opencode`` candidate used by this module.

    Covers: authorized setup-only fixture migration.
    Adds ONLY a synthetic provider/posture record needed to preserve the
    module's historical control-flow assertions now that unverified routes
    fail closed (F1); no assertion is changed and no real vendor receives an
    attestation.
    """
    cfg_dir = config_home / "saberops"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    today_iso = datetime.now(UTC).date().isoformat()
    cp_doc = {
        "version": 2,
        "providers": {"opencode": {"state": "ALLOWED"}},
        "quota_pools": {},
        "postures": {
            "opencode": {
                "class": "A",
                "policy_urls": ["https://example.com/opencode-policy"],
                "ttl_days": 90,
                "verified_on": today_iso,
                "note": "synthetic verified test posture for review findings",
            }
        },
    }
    (cfg_dir / "control_plane.json").write_text(json.dumps(cp_doc), encoding="utf-8")


def finding(code: str, **overrides: str) -> dict[str, Any]:
    base = {
        "id": "F1", "classification": code, "title": "Array body returns 500",
        "severity": "high", "contract_reference": "Brief: malformed requests return 400",
        "invariant_or_requirement": "Malformed bodies are rejected with 400",
        "code_location": "incident_desk/app.py:212",
        "failure_scenario": "PUT /api/issues/1 with [] crashes the handler",
        "evidence": "handler indexes body['title'] on a list",
        "minimum_correction": "Reject non-object bodies with 400",
        "repair_scope": "LOCAL_MECHANICAL",
    }
    base.update(overrides)
    return base


def output(verdict: str, findings: list[dict[str, Any]]) -> str:
    block = json.dumps({"findings": findings})
    return f"VERDICT: {verdict}\nLooks fine overall.\n\n```json\n{block}\n```\n"


def test_blocking_finding_turns_a_pass_line_into_block() -> None:
    verdict, findings, note = structured_review_outcome(
        output("PASS", [finding("B2")]), ReviewVerdict.PASS
    )
    assert verdict is ReviewVerdict.BLOCK and len(findings) == 1 and not note


def test_findings_never_relax_a_block_line() -> None:
    verdict, findings, _ = structured_review_outcome(output("BLOCK", []), ReviewVerdict.BLOCK)
    assert verdict is ReviewVerdict.BLOCK and findings == ()


def test_non_blocking_note_passes_and_becomes_backlog_on_final() -> None:
    note = finding("N1", evidence="Cosmetic and non-blocking; safe to defer")
    assert structured_review_outcome(output("PASS", [note]), ReviewVerdict.PASS)[0] is (
        ReviewVerdict.PASS
    )
    final = structured_review_outcome(output("PASS", [note]), ReviewVerdict.PASS, is_final=True)
    assert final[0] is ReviewVerdict.PASS_WITH_BACKLOG


def test_design_question_blocks_and_stops_a_final_review() -> None:
    d1 = finding("D1", invariant_or_requirement="Unresolved decision: soft or hard delete")
    assert structured_review_outcome(output("PASS", [d1]), ReviewVerdict.PASS)[0] is (
        ReviewVerdict.BLOCK
    )
    assert structured_review_outcome(
        output("PASS", [d1]), ReviewVerdict.PASS, is_final=True
    )[0] is ReviewVerdict.FINAL_BLOCK


@pytest.mark.parametrize(
    "text",
    [
        "VERDICT: PASS\nno block at all",
        "VERDICT: PASS\n```json\n{not json}\n```",
        output("PASS", [finding("N1", evidence="looks good")]),  # N1 without rationale
    ],
)
def test_invalid_findings_keep_the_line_verdict_and_say_why(text: str) -> None:
    verdict, findings, note = structured_review_outcome(text, ReviewVerdict.PASS)
    assert verdict is ReviewVerdict.PASS and findings == ()
    assert note.startswith("Structured findings not recorded:")


def test_duplicate_finding_ids_are_renumbered_and_both_kept() -> None:
    verdict, findings, note = structured_review_outcome(
        output("BLOCK", [finding("B1", title="First"), finding("B1", title="Second")]),
        ReviewVerdict.BLOCK,
    )
    assert verdict is ReviewVerdict.BLOCK
    assert [f.id for f in findings] == ["F1", "F1-2"]
    assert [f.title for f in findings] == ["First", "Second"]
    assert note == ""


def test_finding_missing_required_field_is_kept_as_unstructured_text() -> None:
    missing = finding("B1", title="Guard was removed")
    del missing["repair_scope"]
    verdict, findings, note = structured_review_outcome(
        output("BLOCK", [finding("B2", title="Real finding"), missing]),
        ReviewVerdict.BLOCK,
    )
    # The valid finding stays structured; the incomplete one is never guessed
    # a repair scope, and its raw text plus the reason survive in the detail.
    assert verdict is ReviewVerdict.BLOCK
    assert [f.title for f in findings] == ["Real finding"]
    assert "missing required field: repair_scope" in note
    assert "Guard was removed" in note
    assert "LOCAL_MECHANICAL" not in note


def test_missing_required_field_does_not_relax_a_block_verdict() -> None:
    missing = finding("B1", title="Only finding")
    del missing["repair_scope"]
    verdict, findings, note = structured_review_outcome(
        output("BLOCK", [missing]), ReviewVerdict.BLOCK
    )
    assert verdict is ReviewVerdict.BLOCK
    assert findings == ()
    assert note.startswith("Structured findings partly recorded as unstructured text:")
    assert "missing required field: repair_scope" in note


#: Exact finding blocks recovered from two recorded Codex-batch must-fail
#: reviews in the confirm-4 run (orchestrator.db reviews rev_b08da9ab9a and
#: rev_3279a3f832).  Replayed verbatim so the ingest repair is measured against
#: real reviewer output, not a hand-cleaned sample.
_REVIEW_FIXTURES = Path(__file__).parent / "fixtures" / "reviews"


def _fixture_review(name: str) -> str:
    return (_REVIEW_FIXTURES / name).read_text(encoding="utf-8")


def test_replayed_codex_reviews_keep_block_and_ingest_what_they_can() -> None:
    verdict, findings, note = structured_review_outcome(
        _fixture_review("codex_batch_duplicate_finding_id.txt"), ReviewVerdict.BLOCK
    )
    assert verdict is ReviewVerdict.BLOCK
    assert [f.id for f in findings] == ["B1", "B1-2"]
    assert note == ""

    verdict, findings, note = structured_review_outcome(
        _fixture_review("codex_batch_missing_repair_scope.txt"), ReviewVerdict.BLOCK
    )
    assert verdict is ReviewVerdict.BLOCK
    assert findings == ()
    assert "missing required field: repair_scope" in note
    assert "empty name does not raise ValueError" in note


def test_rendered_findings_reach_repair_context() -> None:
    _, findings, _ = structured_review_outcome(output("BLOCK", [finding("B2")]),
                                               ReviewVerdict.BLOCK)
    text = render_findings(findings)
    assert "B2 Correctness or safety" in text and "Reject non-object bodies" in text
    assert '"findings"' in FINDINGS_PROMPT


class _Adapter(WorkerAdapter):
    def __init__(self, prompts: list[str]) -> None:
        self.prompts = prompts

    @property
    def provider_name(self) -> str:
        return PROVIDER

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        if request.read_only:
            self.prompts.append(request.task)
            note = finding("N1", evidence="Minor and non-blocking; a follow-up is fine",
                           title="Delete skips the version check", severity="low")
            return WorkerResult(provider=PROVIDER, model=request.model,
                                status=AttemptStatus.SUCCESS, summary="ok",
                                stdout=output("PASS", [note]), stderr="",
                                changed_paths=[], has_changes=False)
        (request.worktree_path / "f.txt").write_text("x\n", encoding="utf-8")
        return WorkerResult(provider=PROVIDER, model=request.model,
                            status=AttemptStatus.SUCCESS, summary="wrote", stdout="",
                            stderr="", changed_paths=["f.txt"], has_changes=True)


def test_review_run_persists_structured_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, sub in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    _write_synthetic_verified_postures(tmp_path / "config")
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.email", "t@e.st"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True, capture_output=True)

    def _chain(**_: Any) -> list[WorkerCandidate]:
        return [WorkerCandidate(PROVIDER, "worker-model")]

    monkeypatch.setattr("saberops.orchestrator.resolve_candidate_chain", _chain)
    reviewer, profile, pool = materialize_discovered_binding(
        connection_id="findings-conn", provider=PROVIDER, model="reviewer-model",
        binding_role=BindingRole.CHECK,
    )
    owner = upsert_owner_binding(OwnerBindings(), binding=reviewer, profile=profile, pool=pool)
    save_owner_bindings(replace(owner, reviewer_binding_ids=(reviewer.binding_id,)))
    prompts: list[str] = []
    db = Database(tmp_path / "orch.db")
    orch = Orchestrator(db=db, registry=AdapterRegistry([_Adapter(prompts)]),
                        worktree_base=tmp_path / "wt")
    config = resolve_run_config(task="probe", target_repo=repo, gate_command="true",
                                review=True, publish_candidate=False)
    result = orch.run(config=config, is_peak=False)
    assert result.passed is True, result.summary
    assert prompts and '"findings"' in prompts[0]
    review = db.get_reviews_for_run(result.run_id)[-1]
    assert review.verdict is ReviewVerdict.PASS
    stored = db.get_findings_for_review(review.id)
    assert [f.classification for f in stored] == ["N1"] and not stored[0].blocking


# ---------------------------------------------------------------------------
# Mutation-testing gaps (H46): boundary paths the kept suite did not exercise.
# ---------------------------------------------------------------------------


def _parsed(code: str, **overrides: Any) -> Any:
    return _parse_finding(finding(code, **overrides), 0)


def test_b1_blank_contract_reference_is_a_missing_citation() -> None:
    """Covers: a whitespace B1 citation is refused, not accepted as a reference."""
    parsed = _parsed("B1", contract_reference="   ")
    assert validate_finding_evidence(parsed) == ["b1_contract_reference_missing"]


def test_b2_every_blank_required_field_is_reported() -> None:
    """Covers: the B2 empty-field loop checks each required field."""
    parsed = _parsed(
        "B2",
        invariant_or_requirement=" ",
        failure_scenario=" ",
        evidence=" ",
    )
    assert validate_finding_evidence(parsed) == [
        "b2_invariant_or_requirement_missing",
        "b2_failure_scenario_missing",
        "b2_evidence_missing",
    ]


def test_b2_concurrency_without_an_ordering_marker_is_vague() -> None:
    """Covers: vague concurrency wording needs a concrete ordering marker."""
    vague = _parsed(
        "B2",
        title="Handler may race under load",
        failure_scenario="It might race under load",
    )
    assert "b2_vague_concurrency_scenario_without_concrete_interleaving" in (
        validate_finding_evidence(vague)
    )
    concrete = _parsed(
        "B2",
        title="Handler may race under load",
        failure_scenario="Thread A writes the row before thread B reads it",
    )
    assert validate_finding_evidence(concrete) == []


def test_b3_evidence_must_name_the_missing_artifact() -> None:
    """Covers: B3 evidence names the missing artifact; the check is not inverted."""
    vague = _parsed(
        "B3",
        invariant_or_requirement="The run keeps a deploy log",
        evidence="The PR says nothing about it",
    )
    assert validate_finding_evidence(vague) == [
        "b3_evidence_must_name_required_missing_artifact"
    ]
    named = _parsed("B3", evidence="The deploy log is missing from the run")
    assert validate_finding_evidence(named) == []


def test_d1_must_name_the_unresolved_decision() -> None:
    """Covers: D1 requires an unresolved-decision marker, and accepts one."""
    undecided = _parsed("D1", invariant_or_requirement="Please look at this area")
    assert validate_finding_evidence(undecided) == ["d1_must_name_unresolved_decision"]
    decided = _parsed(
        "D1", invariant_or_requirement="Unresolved decision: keep or retire the flag"
    )
    assert validate_finding_evidence(decided) == []


def test_n1_evidence_must_explain_why_it_is_non_blocking() -> None:
    """Covers: N1 evidence explains non-blocking rationale, and accepts one."""
    vague = _parsed("N1", evidence="The name could be clearer")
    assert validate_finding_evidence(vague) == [
        "n1_evidence_must_explain_non_blocking_rationale"
    ]
    explained = _parsed("N1", evidence="Cosmetic and non-blocking; backlog it")
    assert validate_finding_evidence(explained) == []


@pytest.mark.parametrize("code", ["B1", "B2"])
def test_final_round_blocks_b1_and_b2(code: str) -> None:
    """Covers: a final-round B1/B2 finding maps BLOCK to FINAL_BLOCK."""
    parsed = _parsed(code)
    assert compute_disposition([parsed], is_final_round=False) is ReviewPolicyVerdict.BLOCK
    assert (
        compute_disposition([parsed], is_final_round=True)
        is ReviewPolicyVerdict.FINAL_BLOCK
    )


def test_malformed_finding_does_not_drop_a_later_valid_finding() -> None:
    """Covers: a skipped malformed finding does not stop the ingest loop."""
    missing = finding("B1", title="Incomplete first")
    del missing["repair_scope"]
    parsed = parse_review_findings(
        output("BLOCK", [missing, finding("B2", title="Valid second")])
    )
    assert [item.title for item in parsed.findings] == ["Valid second"]
    assert len(parsed.unstructured) == 1
    assert "Incomplete first" in parsed.unstructured[0]


def test_last_json_findings_block_wins() -> None:
    """Covers: only the last fenced json block is parsed."""
    first = output("PASS", [finding("N1", title="First", evidence="minor and non-blocking")])
    second = json.dumps(
        {"findings": [finding("N1", title="Second", evidence="minor and non-blocking")]}
    )
    parsed = parse_review_findings(f"{first}\n```json\n{second}\n```\n")
    assert [item.title for item in parsed.findings] == ["Second"]


def test_parse_finding_requires_every_required_field() -> None:
    """Covers: the required-field loop in the single-finding parser runs."""
    item = finding("B2")
    del item["evidence"]
    with pytest.raises(ReviewProtocolError, match="missing required field: evidence"):
        _parse_finding(item, 0)


def test_parse_finding_rejects_empty_and_non_string_ids() -> None:
    """Covers: an id must be a non-empty string, not just present."""
    with pytest.raises(ReviewProtocolError, match="non-empty string"):
        _parse_finding(finding("B2", id="   "), 0)
    with pytest.raises(ReviewProtocolError, match="non-empty string"):
        _parse_finding(finding("B2", id=cast("Any", 7)), 0)


def test_parse_finding_rejects_unknown_or_non_string_severity() -> None:
    """Covers: severity must be a known string, not just present."""
    with pytest.raises(ReviewProtocolError, match="invalid severity"):
        _parse_finding(finding("B2", severity="urgent"), 0)
    with pytest.raises(ReviewProtocolError, match="invalid severity"):
        _parse_finding(finding("B2", severity=cast("Any", 3)), 0)


def test_duplicate_id_renumbering_advances_past_an_existing_suffix() -> None:
    """Covers: a free suffix is chosen by advancing, not by a fixed guess."""
    assert _unique_finding_id("F1", {"F1-2"}) == "F1-3"


def test_unstructured_note_keeps_unicode_readable_and_sorts_keys() -> None:
    """Covers: the note keeps raw unicode and sorts keys deterministically."""
    note = _unstructured_finding_note({"b": "café", "a": 1}, 0, "wrong shape")
    assert '"b": "café"' in note
    assert note.index('"a"') < note.index('"b"')


def test_unstructured_note_falls_back_to_repr_for_unserialisable_items() -> None:
    """Covers: a value json cannot encode is rendered with repr, not raised."""

    class Strange:
        def __repr__(self) -> str:
            return "<strange>"

    note = _unstructured_finding_note(Strange(), 0, "wrong type")
    assert "<strange>" in note


def test_findings_block_must_be_an_object_with_a_findings_list() -> None:
    """Covers: a list or a missing findings key is a protocol error."""
    for body in ("[]", '{"summary": "no findings key"}'):
        with pytest.raises(ReviewProtocolError, match="object with a 'findings' list"):
            parse_review_findings(f"```json\n{body}\n```\n")
