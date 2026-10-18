"""`orch questions ...` CLI entry point for the owner question queue (Q-1).

Hermetic: every test points the XDG bases at ``tmp_path`` and removes
``ORCH_QUESTIONS_STORE``/``SABEROPS_PROFILE``, so the owner's real question
document is never read or written and no provider is ever called.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from saberops.cli import main

_OWNER_ONLY_ASK = (
    "questions",
    "ask",
    "--key",
    "run_abc/gate-block",
    "--subject",
    "May the lane touch the protected test?",
    "--kind",
    "gate-block",
    "--decision-class",
    "TEST_CHANGE_AGAINST_ADR",
    "--role",
    "WORKER",
    "--by",
    "worker:run_abc",
    "--run-id",
    "run_abc",
    "--blocks",
    "wave3/lane-a",
    "--requirement",
    "The lane cannot pass the gate without editing a protected test",
    "--expansion",
    "Allow editing tests/test_gate_scope.py in this lane",
    "--smallest",
    "Add one assertion to the existing protected test",
    "--consequences",
    "An accepted ADR's test contract changes",
    "--recommendation",
    "Owner decides; the Orchestrator cannot",
)


@pytest.fixture(autouse=True)
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate the owner state for *every* test in this module.

    Autouse on purpose: a CLI test that forgets the fixture must never be able
    to read or write the owner's real question document.
    """
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("ORCH_QUESTIONS_STORE", raising=False)
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _store_path(isolated: Path) -> Path:
    return isolated / "state" / "saberops" / "questions.json"


def _ask(
    *extra: str, capsys: pytest.CaptureFixture[str], json_out: bool = False
) -> tuple[int, str, str]:
    argv = [*_OWNER_ONLY_ASK, *extra, *(["--json"] if json_out else [])]
    return _cli(*argv, capsys=capsys)


def _answer(
    ref: str,
    words: str,
    *extra: str,
    capsys: pytest.CaptureFixture[str],
) -> tuple[int, str, str]:
    return _cli(
        "questions", "answer", ref, "--words", words, *extra, "--json", capsys=capsys
    )


# ---------------------------------------------------------------------------
# ask / list / show
# ---------------------------------------------------------------------------


def test_ask_creates_one_keyed_question_in_owner_state(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out, err = _ask(capsys=capsys, json_out=True)
    assert (code, err) == (0, "")
    view = json.loads(out)
    assert view["status"] == "OPEN"
    assert view["key"] == "run_abc/gate-block"
    assert view["decision_class"] == "TEST_CHANGE_AGAINST_ADR"
    assert view["answerable_by"] == "OWNER"
    assert view["run_id"] == "run_abc"
    assert view["blocks"] == ["wave3/lane-a"]
    assert view["escalation"]["recommendation"] == "Owner decides; the Orchestrator cannot"

    document = json.loads(_store_path(isolated).read_text(encoding="utf-8"))
    assert len(document["questions"]) == 1

    # A retried ask is idempotent: still exactly one question.
    code, out, _ = _ask(capsys=capsys, json_out=True)
    assert code == 0
    assert json.loads(out)["question_id"] == view["question_id"]
    document = json.loads(_store_path(isolated).read_text(encoding="utf-8"))
    assert len(document["questions"]) == 1


def test_ask_requires_all_five_escalation_parts(capsys: pytest.CaptureFixture[str]) -> None:
    argv: list[str] = []
    skip_next = False
    for item in _OWNER_ONLY_ASK:
        if skip_next:
            skip_next = False
            continue
        if item == "--requirement":
            skip_next = True
            continue
        argv.append(item)
    with pytest.raises(SystemExit) as excinfo:
        main([*argv, "--store", "/tmp/never-written.json"])
    assert excinfo.value.code == 2
    assert "--requirement" in capsys.readouterr().err


def test_list_and_show_render_the_queue(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    code, out, err = _cli("questions", "list", "--json", capsys=capsys)
    assert (code, err) == (0, "")
    payload = json.loads(out)
    assert payload["pending"] == [payload["questions"][0]["question_id"]]
    assert payload["questions"][0]["status"] == "OPEN"

    code, out, err = _cli(
        "questions", "list", "--status", "ANSWERED", "--json", capsys=capsys
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["questions"] == []

    code, out, err = _cli("questions", "show", "run_abc/gate-block", capsys=capsys)
    assert (code, err) == (0, "")
    assert "run_abc/gate-block" in out


def test_show_unknown_question_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    code, out, err = _cli("questions", "show", "nope", capsys=capsys)
    assert code == 1
    assert out == ""
    assert "unknown question" in err


# ---------------------------------------------------------------------------
# answer: verbatim owner words and authority criteria
# ---------------------------------------------------------------------------


def test_the_answer_round_trip_stores_the_owners_exact_words(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    words = "  yes — allow exactly one new assertion,\n  nothing else.  "
    code, out, err = _answer("run_abc/gate-block", words, capsys=capsys)
    assert (code, err) == (0, "")
    view = json.loads(out)
    assert view["status"] == "ANSWERED"
    assert view["closed_by"] == "answer"
    assert view["answer"]["words"] == words
    assert view["answer"]["authority"] == "OWNER"

    code, out, _ = _cli(
        "questions", "show", "run_abc/gate-block", "--json", capsys=capsys
    )
    assert code == 0
    assert json.loads(out)["answer"]["words"] == words


def test_an_orchestrator_cannot_answer_an_owner_only_question(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    code, out, err = _cli(
        "questions",
        "answer",
        "run_abc/gate-block",
        "--authority",
        "orchestrator",
        "--by",
        "orchestrator:session-1",
        "--words",
        "allow it",
        "--evidence",
        "e",
        "--rejected-alternative",
        "a",
        capsys=capsys,
    )
    assert code == 1
    assert out == ""
    assert "owner-only" in err


def test_an_implementer_role_can_never_answer(capsys: pytest.CaptureFixture[str]) -> None:
    assert _ask(capsys=capsys)[0] == 0
    code, _, err = _answer(
        "run_abc/gate-block", "we allow it", "--role", "WORKER", capsys=capsys
    )
    assert code == 1
    assert "implementers never answer" in err


def test_an_orchestrator_alone_answer_requires_evidence_and_alternative(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert (
        _cli(
            "questions",
            "ask",
            "--key",
            "review/finding-f3",
            "--subject",
            "Classify review finding F3",
            "--kind",
            "review-finding",
            "--decision-class",
            "REVIEW_FINDING_CLASSIFICATION",
            "--role",
            "REVIEWER",
            "--by",
            "reviewer:run_abc",
            "--requirement",
            "The finding blocks the candidate",
            "--expansion",
            "Classify the finding as accept",
            "--smallest",
            "Accept the finding",
            "--consequences",
            "One fix round is added",
            "--recommendation",
            "Accept the finding",
            capsys=capsys,
        )[0]
        == 0
    )
    code, _, err = _cli(
        "questions",
        "answer",
        "review/finding-f3",
        "--authority",
        "orchestrator",
        "--by",
        "orchestrator:session-1",
        "--words",
        "accept the finding",
        capsys=capsys,
    )
    assert code == 1
    assert "evidence" in err

    code, out, err = _cli(
        "questions",
        "answer",
        "review/finding-f3",
        "--authority",
        "orchestrator",
        "--by",
        "orchestrator:session-1",
        "--words",
        "accept the finding",
        "--evidence",
        "review finding F3 quotes the failing assertion",
        "--rejected-alternative",
        "reject the finding",
        "--json",
        capsys=capsys,
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["answer"]["authority"] == "ORCHESTRATOR"

    code, out, err = _cli("questions", "audit", "--json", capsys=capsys)
    assert (code, err) == (0, "")
    audit = json.loads(out)
    assert len(audit) == 1
    assert audit[0]["decision_class"] == "REVIEW_FINDING_CLASSIFICATION"
    assert audit[0]["evidence"] == "review finding F3 quotes the failing assertion"
    assert audit[0]["rejected_alternative"] == "reject the finding"

    code, out, err = _cli("questions", "audit", capsys=capsys)
    assert (code, err) == (0, "")
    assert "rejected" in out


# ---------------------------------------------------------------------------
# divergence, defer, moot
# ---------------------------------------------------------------------------


def test_a_conflicting_answer_is_reported_and_never_auto_closed(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    assert _answer("run_abc/gate-block", "yes, allow it", capsys=capsys)[0] == 0
    code, out, err = _answer(
        "run_abc/gate-block", "no, keep the test frozen", capsys=capsys
    )
    assert (code, err) == (0, "")
    view = json.loads(out)
    assert view["status"] == "ANSWERED"
    assert view["divergence_count"] == 1
    assert view["answer"]["words"] == "yes, allow it"

    code, out, err = _cli("questions", "divergences", "--json", capsys=capsys)
    assert (code, err) == (0, "")
    reports = json.loads(out)
    assert len(reports) == 1
    assert reports[0]["status"] == "ANSWERED"
    assert reports[0]["record"]["reason"] == "DIFFERENT_WORDS"
    assert reports[0]["record"]["words"] == "no, keep the test frozen"


def test_a_deferred_question_resurfaces_on_its_date(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    code, out, err = _cli(
        "questions",
        "defer",
        "run_abc/gate-block",
        "--until",
        "2026-11-01",
        "--json",
        capsys=capsys,
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["status"] == "DEFERRED"

    code, out, err = _cli(
        "questions", "list", "--as-of", "2026-10-31", "--json", capsys=capsys
    )
    assert (code, err) == (0, "")
    before = json.loads(out)
    assert before["questions"][0]["due"] is False
    assert before["pending"] == []

    code, out, err = _cli(
        "questions", "list", "--as-of", "2026-11-01", "--json", capsys=capsys
    )
    assert (code, err) == (0, "")
    after = json.loads(out)
    assert after["questions"][0]["resurfaced"] is True
    assert after["pending"] == [after["questions"][0]["question_id"]]


def test_moot_requires_evidence_and_closes_on_evidence(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    with pytest.raises(SystemExit) as excinfo:
        main(["questions", "moot", "run_abc/gate-block"])
    assert excinfo.value.code == 2
    assert "--evidence" in capsys.readouterr().err

    code, out, err = _cli(
        "questions",
        "moot",
        "run_abc/gate-block",
        "--evidence",
        "the protected test left the accepted ADR contract in commit abc",
        "--by",
        "engine",
        "--role",
        "ENGINE",
        "--json",
        capsys=capsys,
    )
    assert (code, err) == (0, "")
    view = json.loads(out)
    assert view["status"] == "MOOT"
    assert view["closed_by"] == "mark_moot"
    assert "commit abc" in view["moot_evidence"]


def test_the_cli_cannot_defer_as_an_implementer(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    with pytest.raises(SystemExit) as excinfo:
        main(
            [
                "questions",
                "defer",
                "run_abc/gate-block",
                "--until",
                "2026-11-01",
                "--role",
                "WORKER",
            ]
        )
    assert excinfo.value.code == 2
    assert "WORKER" in capsys.readouterr().err
    code, out, _ = _cli("questions", "show", "run_abc/gate-block", "--json", capsys=capsys)
    assert code == 0
    assert json.loads(out)["status"] == "OPEN"


def test_diverge_records_a_conflict_without_closing(
    capsys: pytest.CaptureFixture[str]
) -> None:
    assert _ask(capsys=capsys)[0] == 0
    assert _answer("run_abc/gate-block", "yes", capsys=capsys)[0] == 0
    code, out, err = _cli(
        "questions",
        "diverge",
        "run_abc/gate-block",
        "--reason",
        "DIFFERENT_WORDS",
        "--words",
        "no",
        "--by",
        "owner",
        "--role",
        "OWNER",
        "--json",
        capsys=capsys,
    )
    assert (code, err) == (0, "")
    view = json.loads(out)
    assert view["status"] == "ANSWERED"
    assert view["divergence_count"] == 1


# ---------------------------------------------------------------------------
# standing rules for CFG-1
# ---------------------------------------------------------------------------


def _three_same_kind_answers(capsys: pytest.CaptureFixture[str]) -> None:
    for index in (1, 2, 3):
        key = f"run_{index}/gate-block"
        argv = [
            item.replace("run_abc/gate-block", key).replace("run_abc", f"run_{index}")
            for item in _OWNER_ONLY_ASK
        ]
        assert _cli(*argv, capsys=capsys)[0] == 0
        assert _answer(key, "yes, allow exactly one assertion", capsys=capsys)[0] == 0


def test_repeated_same_kind_answers_become_a_standing_rule_proposal(
    capsys: pytest.CaptureFixture[str]
) -> None:
    _three_same_kind_answers(capsys)
    code, out, err = _cli("questions", "standing-rules", "--json", capsys=capsys)
    assert (code, err) == (0, "")
    payload = json.loads(out)
    assert payload["threshold"] == 3
    proposals = payload["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["target"] == "CFG-1"
    assert proposals[0]["kind"] == "gate-block"
    assert proposals[0]["occurrences"] == 3
    assert proposals[0]["owner_words"] == ["yes, allow exactly one assertion"] * 3

    code, out, err = _cli("questions", "standing-rules", capsys=capsys)
    assert (code, err) == (0, "")
    assert "proposals only" in out


def test_no_standing_rule_below_the_threshold(capsys: pytest.CaptureFixture[str]) -> None:
    _three_same_kind_answers(capsys)
    code, out, err = _cli(
        "questions", "standing-rules", "--threshold", "4", "--json", capsys=capsys
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["proposals"] == []


# ---------------------------------------------------------------------------
# fail-closed storage
# ---------------------------------------------------------------------------


def test_a_corrupt_document_fails_closed_without_a_traceback(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = _store_path(isolated)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    code, out, err = _cli("questions", "list", capsys=capsys)
    assert code == 1
    assert out == ""
    assert err.startswith("[!]")
    assert "Traceback" not in err
    assert path.read_text(encoding="utf-8") == "{not json"


def test_the_store_argument_pins_an_explicit_document(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pinned = isolated / "pinned.json"
    code, out, err = _cli(
        "questions",
        "list",
        "--store",
        str(pinned),
        "--json",
        capsys=capsys,
    )
    assert (code, err) == (0, "")
    assert json.loads(out)["questions"] == []
    assert not pinned.exists()


def test_questions_command_is_reachable_from_the_parser() -> None:
    from saberops.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["questions", "list", "--json"])
    assert args.command == "questions"
    assert args.questions_command == "list"
    assert "questions" in parser.format_help()
