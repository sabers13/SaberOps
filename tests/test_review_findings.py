"""Structured review findings: parsed, enforced, persisted and shown in the UI."""

from __future__ import annotations

import json
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

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
    Tier,
    WorkerCandidate,
    WorkerRequest,
    WorkerResult,
)
from saberops.orchestrator import Orchestrator
from saberops.review import render_findings, structured_review_outcome
from saberops.review_policy import FINDINGS_PROMPT
from saberops.web import create_app
from saberops.workers.base import WorkerAdapter
from saberops.workers.registry import AdapterRegistry

PROVIDER = "opencode"


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


def test_review_run_persists_findings_and_run_page_shows_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name, sub in (("XDG_STATE_HOME", "state"), ("XDG_CONFIG_HOME", "config"),
                      ("XDG_CACHE_HOME", "cache")):
        monkeypatch.setenv(name, str(tmp_path / sub))
    monkeypatch.delenv("ORCH_BINDING_STORE", raising=False)
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    for cmd in (["git", "init", "-b", "main"], ["git", "config", "user.email", "t@e.st"],
                ["git", "config", "user.name", "T"]):
        subprocess.run(cmd, cwd=repo, check=True, capture_output=True)
    (repo / "README.md").write_text("# t\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-m", "i"], cwd=repo, check=True, capture_output=True)

    def _chain(*, tier: Tier, **_: Any) -> list[WorkerCandidate]:
        return [WorkerCandidate(PROVIDER, "worker-model")]

    monkeypatch.setattr("saberops.orchestrator.resolve_candidate_chain", _chain)
    reviewer, profile, pool = materialize_discovered_binding(
        connection_id="findings-conn", provider=PROVIDER, model="reviewer-model",
        binding_role=BindingRole.REVIEWER,
    )
    owner = upsert_owner_binding(OwnerBindings(), binding=reviewer, profile=profile, pool=pool)
    save_owner_bindings(replace(owner, reviewer_binding_id=reviewer.binding_id))
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
    page = TestClient(create_app(db_path=tmp_path / "orch.db")).get(f"/runs/{result.run_id}")
    assert "Structured findings" in page.text and "Delete skips the version check" in page.text
    assert "N1 Non-blocking note" in page.text and "0 blocking" in page.text
