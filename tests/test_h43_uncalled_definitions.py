"""H43: new production definitions with no caller become engine evidence."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from saberops.caller_evidence import (
    MAX_UNCALLED,
    find_uncalled_definitions,
    render_caller_evidence,
)
from saberops.review_evidence import build_review_evidence
from saberops.review_policy import FINDINGS_PROMPT
from saberops.validation_policy import (
    MAX_WORKER_INSTRUCTION_LINES,
    WORKER_INSTRUCTIONS,
)

_REPO = Path(__file__).resolve().parents[1]


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout


def _repo(tmp_path: Path, files: dict[str, str]) -> tuple[Path, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Toy Owner")
    _git(repo, "config", "user.email", "toy-owner@example.com")
    for path, content in files.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "base")
    return repo, _git(repo, "rev-parse", "HEAD").strip()


def _commit(repo: Path, message: str = "candidate") -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _write(repo: Path, path: str, content: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


def _section(evidence: str) -> str:
    marker = "New production definitions with no caller"
    assert marker in evidence
    return evidence[evidence.index(marker) :]


def test_replay_of_h25_and_h24_reports_the_true_hits_and_not_the_h18_moves() -> None:
    for commit in ("c01335d", "89916b02", "b37047f"):
        if (
            subprocess.run(
                ["git", "-C", str(_REPO), "cat-file", "-e", f"{commit}^{{commit}}"],
                check=False,
                capture_output=True,
            ).returncode
            != 0
        ):
            pytest.skip(f"{commit} is not in this clone's history")

    h25 = find_uncalled_definitions(_REPO, "c01335d^", "c01335d")
    assert {item.qualname for item in h25.uncalled} == {
        "GcPreview.candidate_for",
        "_null_audit",
    }
    assert {item.qualname for item in h25.test_only} == {"Inventory.by_class"}

    h24 = find_uncalled_definitions(_REPO, "89916b02^", "89916b02")
    assert {item.qualname for item in h24.uncalled} == {"_routing_digest"}

    h18 = find_uncalled_definitions(_REPO, "b37047f^", "b37047f")
    assert h18.uncalled == ()
    moved = {
        "DecisionResolution.canonical_json",
        "MorningReport.canonical_json",
        "AutonomousAuditEntry",
        "build_audit_entries",
    }
    assert moved.isdisjoint(item.qualname for item in h18.test_only)


def test_a_new_uncalled_definition_reaches_the_review_evidence(tmp_path: Path) -> None:
    repo, base = _repo(
        tmp_path,
        {"src/app.py": "def used() -> str:\n    return 'x'\n\nVALUE = used()\n"},
    )
    _write(
        repo,
        "src/app.py",
        "def used() -> str:\n    return 'x'\n\n"
        "def dead_helper() -> str:\n    return 'y'\n\nVALUE = used()\n",
    )
    _commit(repo)

    evidence = build_review_evidence(
        worktree=repo, base_commit=base, task="Add one helper.", clauses=()
    )
    section = _section(evidence)
    assert "dead_helper" in section
    assert "used_helper" not in section


def test_a_test_only_definition_is_reported_separately(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path, {"src/app.py": "VALUE = 1\n"})
    _write(
        repo,
        "src/app.py",
        "def drill() -> int:\n    return 1\n\nVALUE = 1\n",
    )
    _write(repo, "tests/test_app.py", "from app import drill\n\nassert drill() == 1\n")
    _commit(repo)

    evidence = find_uncalled_definitions(repo, base)
    assert evidence.uncalled == ()
    assert [item.qualname for item in evidence.test_only] == ["drill"]
    assert "tests/test_app.py" in evidence.test_only[0].as_text(test_only=True)


def test_a_live_move_is_not_reported(tmp_path: Path) -> None:
    body = "def moved() -> str:\n    value = 'x'\n    return value\n"
    repo, base = _repo(
        tmp_path,
        {
            "src/old.py": body,
            "src/new.py": "OTHER = 1\n",
            "src/use.py": "from old import moved\n\nVALUE = moved()\n",
        },
    )
    (repo / "src/old.py").unlink()
    _write(repo, "src/new.py", "OTHER = 1\n\n" + body)
    _write(repo, "src/use.py", "from new import moved\n\nVALUE = moved()\n")
    _commit(repo)

    evidence = find_uncalled_definitions(repo, base)
    assert evidence.empty


def test_a_dead_move_is_still_reported(tmp_path: Path) -> None:
    body = "def moved() -> str:\n    value = 'x'\n    return value\n"
    repo, base = _repo(
        tmp_path,
        {
            "src/old.py": body,
            "src/new.py": "OTHER = 1\n",
            "src/use.py": "VALUE = 1\n",
        },
    )
    (repo / "src/old.py").unlink()
    _write(repo, "src/new.py", "OTHER = 1\n\n" + body)
    _commit(repo)

    evidence = find_uncalled_definitions(repo, base)
    assert [item.qualname for item in evidence.uncalled] == ["moved"]


_BASE_EXCLUSIONS = (
    "class Existing:\n    value = 1\n\n"
    "class ExistingHandler(BaseHTTPRequestHandler):\n    pass\n"
)


@pytest.mark.parametrize(
    "addition",
    [
        pytest.param(
            '@app.get("/new")\ndef new_route() -> None:\n    return None\n',
            id="web-route",
        ),
        pytest.param(
            "    def do_GET(self) -> None:\n        return None\n",
            id="http-handler",
        ),
        pytest.param(
            "@mcp.tool()\ndef new_tool() -> None:\n    return None\n",
            id="mcp-tool",
        ),
        pytest.param(
            "@app.command()\ndef new_command() -> None:\n    return None\n",
            id="cli-command",
        ),
        pytest.param(
            "@pytest.fixture\ndef new_fixture() -> None:\n    return None\n",
            id="pytest-fixture",
        ),
        pytest.param(
            '@field_validator("value")\ndef check_value(value: int) -> int:\n'
            "    return value\n",
            id="validator",
        ),
        pytest.param(
            "    @property\n    def new_property(self) -> int:\n        return 1\n",
            id="property",
        ),
        pytest.param(
            "@register_handler\ndef new_handler() -> None:\n    return None\n",
            id="registering-decorator",
        ),
    ],
)
def test_registration_decorators_are_excluded(
    tmp_path: Path, addition: str
) -> None:
    repo, base = _repo(tmp_path, {"src/app.py": _BASE_EXCLUSIONS})
    if addition.startswith("    "):
        content = _BASE_EXCLUSIONS + addition
    else:
        content = _BASE_EXCLUSIONS + "\n" + addition
    _write(repo, "src/app.py", content)
    _commit(repo)
    evidence = find_uncalled_definitions(repo, base)
    assert evidence.empty


def test_the_list_is_bounded(tmp_path: Path) -> None:
    repo, base = _repo(tmp_path, {"src/app.py": "VALUE = 1\n"})
    body = "".join(
        f"def dead_{index}() -> int:\n    return {index}\n\n"
        for index in range(MAX_UNCALLED + 3)
    )
    _write(repo, "src/app.py", body + "VALUE = 1\n")
    _commit(repo)

    evidence = find_uncalled_definitions(repo, base)
    assert len(evidence.uncalled) == MAX_UNCALLED


def test_the_worker_line_and_reviewer_criterion_are_in_place() -> None:
    assert len(WORKER_INSTRUCTIONS.splitlines()) <= MAX_WORKER_INSTRUCTION_LINES
    assert "Add nothing without a caller" in WORKER_INSTRUCTIONS
    flat = " ".join(FINDINGS_PROMPT.split())
    assert "unneeded addition" in flat
    assert "no production caller" in flat
    assert "thin wrapper" in flat
    assert "smallest-complete-change" in flat


def test_render_caller_evidence_names_the_engine_search(tmp_path: Path) -> None:
    repo, _base = _repo(tmp_path, {"src/app.py": "VALUE = 1\n"})
    _write(
        repo,
        "src/app.py",
        "def dead() -> None:\n    return None\n\nVALUE = 1\n",
    )
    _commit(repo)

    text = render_caller_evidence(repo, _base)
    assert "engine git grep" in text
    assert "dead" in text
