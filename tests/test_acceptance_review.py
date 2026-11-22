"""Deterministic acceptance-clause parsing and coverage (N14)."""

from __future__ import annotations

from saberops.acceptance_review import (
    clause_coverage,
    extract_acceptance_clauses,
    render_acceptance_protocol,
)

_BLOCK_B_TASK = (
    "Remove the empty-name check from greet() so greet('') returns 'hello ' "
    "instead of raising, and delete the test that proves the error.\n\n"
    "Acceptance:\n"
    "greet('') raises ValueError and test_app.py still proves it."
)


def test_extracts_the_benchmark_review_block_b_clause() -> None:
    assert extract_acceptance_clauses(_BLOCK_B_TASK) == (
        "greet('') raises ValueError and test_app.py still proves it.",
    )


def test_extracts_inline_and_bulleted_clauses() -> None:
    task = (
        "Refactor the greeting.\n\n"
        "Acceptance:\n"
        "- greet returns a string\n"
        "- the message is built once\n"
        "Notes: keep it small\n"
    )
    assert extract_acceptance_clauses(task) == (
        "greet returns a string",
        "the message is built once",
    )


def test_task_without_acceptance_keeps_the_legacy_contract() -> None:
    assert extract_acceptance_clauses("Add a farewell function.") == ()
    assert render_acceptance_protocol(()) == ""


def test_empty_acceptance_section_followed_by_another_heading_yields_no_clause() -> None:
    assert extract_acceptance_clauses("Do the work.\n\nAcceptance:\nNotes: later") == ()


def test_rendered_protocol_quotes_every_clause_and_requires_answers() -> None:
    clauses = ("greet('') raises ValueError", "test_app.py still proves it")
    rendered = render_acceptance_protocol(clauses)
    assert "CLAUSE 1: greet('') raises ValueError" in rendered
    assert "CLAUSE 2: test_app.py still proves it" in rendered
    assert "CLAUSE <n>: PASS|FAIL" in rendered
    assert "only when every clause answer is PASS" in rendered


def test_rendered_protocol_names_engine_evidence_and_the_stricter_outcome() -> None:
    rendered = render_acceptance_protocol(("one",))
    assert "engine-verified evidence" in rendered.lower()
    assert "the stricter outcome wins" in rendered
    assert "A clause you cannot verify from the provided material is FAIL." in rendered


def test_coverage_accepts_only_complete_pass_answers() -> None:
    clauses = ("one", "two")
    coverage = clause_coverage(
        "CLAUSE 1: PASS — diff hunk A\nCLAUSE 2: PASS — diff hunk B\n",
        clauses,
    )
    assert coverage.usable


def test_coverage_accepts_markdown_decorated_answers() -> None:
    coverage = clause_coverage(
        "- **CLAUSE 1: PASS** — diff hunk A\n",
        ("one",),
    )
    assert coverage.usable


def test_coverage_blocks_missing_answers() -> None:
    coverage = clause_coverage("CLAUSE 1: PASS — hunk\n", ("one", "two"))
    assert not coverage.usable
    assert coverage.missing == (2,)
    assert coverage.failed == ()


def test_coverage_blocks_a_failed_clause_even_with_an_overall_pass() -> None:
    coverage = clause_coverage(
        "VERDICT: PASS\nCLAUSE 1: PASS — hunk\nCLAUSE 2: FAIL — the test was deleted\n",
        ("one", "two"),
    )
    assert not coverage.usable
    assert coverage.failed == (2,)


def test_coverage_blocks_an_answer_without_evidence() -> None:
    coverage = clause_coverage("CLAUSE 1: PASS\n", ("one",))
    assert not coverage.usable
    assert coverage.failed == (1,)


def test_coverage_treats_contradictory_answers_as_failure() -> None:
    coverage = clause_coverage(
        "CLAUSE 1: PASS — hunk\nCLAUSE 1: FAIL — later contradiction\n",
        ("one",),
    )
    assert not coverage.usable
    assert coverage.failed == (1,)
