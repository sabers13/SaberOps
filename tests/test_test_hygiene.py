"""Tests for scripts/test_hygiene.py (read-only hygiene candidates).

Every test builds a throwaway git repository with a tiny fake package plus a
synthetic coverage map (meta.json + shards in the exact build_test_map.py
format) and a small junit xml, so the real saberops package is never imported,
no test is ever executed, and no provider or network access happens.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
HYGIENE_SCRIPT = REPO_ROOT / "scripts" / "test_hygiene.py"

ALPHA_SRC = '''"""Fixture src module."""

__all__ = ["exported_helper"]


def only_tested() -> int:
    return 1


def shared() -> int:
    return 2


def exported_helper() -> int:
    return 3


def __dunder_thing__() -> int:
    return 4


def main() -> None:
    pass
'''

BETA_SRC = '''"""Another src module that uses shared."""

from saberops.alpha import shared


def use_shared() -> int:
    return shared()
'''

TEST_ALPHA = '''"""Fixture tests."""

from saberops.alpha import exported_helper, only_tested, shared


def test_only() -> None:
    """Covers: G-004 hygiene section A."""
    assert only_tested() == 1


def test_shared() -> None:
    """Covers: G-004 hygiene section A."""
    assert shared() == 2


def test_exported() -> None:
    """Covers: G-004 hygiene section A."""
    assert exported_helper() == 3
'''

BASE_FILES: dict[str, str] = {
    "src/saberops/__init__.py": "",
    "src/saberops/alpha.py": ALPHA_SRC,
    "src/saberops/beta.py": BETA_SRC,
    "tests/test_alpha.py": TEST_ALPHA,
}


def load_hygiene() -> ModuleType:
    """Covers: G-004 hygiene section D (regex unit checks need the constant)."""
    spec = importlib.util.spec_from_file_location("test_hygiene", HYGIENE_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["test_hygiene"] = module
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return completed.stdout.strip()


def write(base: Path, path: str, text: str) -> None:
    target = base / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    git(repo, "add", "-A")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "-m",
        message,
    )
    return git(repo, "rev-parse", "HEAD")


def seed_repo(root: Path, files: dict[str, str]) -> Path:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@localhost",
        "commit",
        "--allow-empty",
        "-m",
        "root",
    )
    for path, text in files.items():
        write(repo, path, text)
    commit(repo, "fixture")
    return repo


def write_map(
    root: Path,
    *,
    commit_sha: str,
    nodes: list[str],
    shards: dict[str, dict[str, list[int]]],
    unmatched: int = 0,
) -> Path:
    map_dir = root / "map"
    files_dir = map_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    by_test: dict[str, list[int]] = {}
    for position, node_id in enumerate(nodes):
        by_test.setdefault(node_id.split("::")[0], []).append(position)
    meta = {
        "version": 2,
        "commit": commit_sha,
        "built_at": "2026-10-12T00:00:00+00:00",
        "test_count": len(nodes),
        "context_count": len(nodes),
        "coverage_version": "test",
        "build_seconds": 1.0,
        "pytest_exit": 0,
        "subprocess_capture": False,
        "nodes": nodes,
        "by_test": by_test,
        "blind": [],
        "unmatched_nodes": unmatched,
    }
    (map_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    for rel, lines in shards.items():
        shard_path = files_dir / f"{rel}.json"
        shard_path.parent.mkdir(parents=True, exist_ok=True)
        shard_path.write_text(json.dumps(lines), encoding="utf-8")
    return map_dir


def write_junit(root: Path, cases: list[tuple[str, str, float]]) -> Path:
    suite = ET.Element("testsuite")
    for classname, name, elapsed in cases:
        ET.SubElement(
            suite,
            "testcase",
            {"classname": classname, "name": name, "time": str(elapsed)},
        )
    path = root / "junit.xml"
    ET.ElementTree(suite).write(path, encoding="utf-8", xml_declaration=True)
    return path


def run_hygiene(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    out = root / "out.md"
    payload = root / "out.json"
    return subprocess.run(
        [sys.executable, str(HYGIENE_SCRIPT), *args, "--out", str(out), "--json", str(payload)],
        check=False,
        capture_output=True,
        text=True,
    )


def read_outputs(root: Path) -> tuple[str, dict[str, object]]:
    markdown = (root / "out.md").read_text(encoding="utf-8")
    data = json.loads((root / "out.json").read_text(encoding="utf-8"))
    assert isinstance(data, dict)
    return markdown, data


def test_a_lists_test_only_symbols(tmp_path: Path) -> None:
    """Covers: G-004 hygiene section A (test-only src symbols)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    nodes = [
        "tests/test_alpha.py::test_only",
        "tests/test_alpha.py::test_shared",
        "tests/test_alpha.py::test_exported",
    ]
    map_dir = write_map(
        tmp_path,
        commit_sha=head,
        nodes=nodes,
        shards={"src/saberops/alpha.py": {"5": [0], "9": [1], "13": [2]}},
    )
    completed = run_hygiene(tmp_path, "--repo", str(repo), "--map", str(map_dir))
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    rows = data["a"]
    assert isinstance(rows, list)
    by_symbol = {row["symbol"]: row for row in rows}
    assert set(by_symbol) == {"only_tested", "exported_helper"}
    assert by_symbol["only_tested"]["file"] == "src/saberops/alpha.py"
    assert by_symbol["only_tested"]["exported"] is False
    assert by_symbol["only_tested"]["referenced_by"] == ["tests/test_alpha.py"]
    assert by_symbol["exported_helper"]["exported"] is True
    assert "src/saberops/alpha.py:6 only_tested — referenced by: tests/test_alpha.py" in markdown
    assert "exported_helper (exported)" in markdown


def test_b_lists_uncovered_and_marks_script_tests(tmp_path: Path) -> None:
    """Covers: G-004 hygiene section B (tests covering no src lines)."""
    files = dict(BASE_FILES)
    files["tests/test_beta.py"] = (
        '"""Script-loading fixture tests."""\n\nfrom scripts import select_tests\n\n\n'
        'def test_other() -> None:\n    """Covers: G-004 hygiene section B."""\n'
        "    assert select_tests is not None\n"
    )
    repo = seed_repo(tmp_path, files)
    head = git(repo, "rev-parse", "HEAD")
    nodes = [
        "tests/test_alpha.py::test_only",
        "tests/test_beta.py::test_other",
    ]
    map_dir = write_map(
        tmp_path,
        commit_sha=head,
        nodes=nodes,
        shards={"src/saberops/alpha.py": {"5": [0]}},
    )
    junit_path = write_junit(
        tmp_path,
        [
            ("tests.test_alpha", "test_only", 1.0),
            ("tests.test_beta", "test_other", 2.0),
        ],
    )
    completed = run_hygiene(
        tmp_path, "--repo", str(repo), "--map", str(map_dir), "--junit", str(junit_path)
    )
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    section_b = data["b"]
    assert isinstance(section_b, dict)
    assert "tests/test_alpha.py" not in section_b
    assert section_b["tests/test_beta.py"]["nodes"] == ["tests/test_beta.py::test_other"]
    assert section_b["tests/test_beta.py"]["script_test"] is True
    assert "tests/test_beta.py (script test):" in markdown


def test_b_lists_junit_id_absent_from_map(tmp_path: Path) -> None:
    """Covers: G-004 fix-up 1 section B (junit population beyond the map)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    nodes = ["tests/test_alpha.py::test_only"]
    map_dir = write_map(
        tmp_path,
        commit_sha=head,
        nodes=nodes,
        shards={"src/saberops/alpha.py": {"6": [0]}},
    )
    junit_path = write_junit(
        tmp_path,
        [
            ("tests.test_alpha", "test_only", 1.0),
            ("tests.test_ghost", "test_ghost", 2.0),
        ],
    )
    completed = run_hygiene(
        tmp_path, "--repo", str(repo), "--map", str(map_dir), "--junit", str(junit_path)
    )
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    section_b = data["b"]
    assert isinstance(section_b, dict)
    assert "tests/test_alpha.py" not in section_b
    assert section_b["tests/test_ghost.py"]["nodes"] == ["tests/test_ghost.py::test_ghost"]
    assert section_b["tests/test_ghost.py"]["script_test"] is False
    assert "- tests/test_ghost.py::test_ghost" in markdown
    assert "- id mismatch: map-only 0, junit-only 1" in markdown


def test_b_param_and_class_ids_match_map(tmp_path: Path) -> None:
    """Covers: G-004 fix-up 1 section B (parametrised and class ids match)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    nodes = ["tests/test_p.py::test_m[a]", "tests/test_c.py::TestC::test_m"]
    map_dir = write_map(
        tmp_path,
        commit_sha=head,
        nodes=nodes,
        shards={"src/saberops/alpha.py": {"6": [0, 1]}},
    )
    junit_path = write_junit(
        tmp_path,
        [("tests.test_p", "test_m[a]", 1.0), ("tests.test_c.TestC", "test_m", 2.0)],
    )
    completed = run_hygiene(
        tmp_path, "--repo", str(repo), "--map", str(map_dir), "--junit", str(junit_path)
    )
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    assert data["b"] == {}
    assert "- none found." in markdown.split("## B.")[1].split("## C.")[0]
    assert "- id mismatch: map-only 0, junit-only 0" in markdown


def test_b_without_junit_prints_needs_line(tmp_path: Path) -> None:
    """Covers: G-004 fix-up 1 section B (needs-junit line with unmatched count)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    map_dir = write_map(
        tmp_path,
        commit_sha=head,
        nodes=["tests/test_alpha.py::test_only"],
        shards={"src/saberops/alpha.py": {"6": [0]}},
        unmatched=7,
    )
    completed = run_hygiene(tmp_path, "--repo", str(repo), "--map", str(map_dir))
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    expected = (
        "B needs --junit: the map lists only tests that executed src code "
        "(7 tests not in the map)."
    )
    assert f"- {expected}" in markdown
    assert data["b"] == {"needs_junit": expected}
    assert "## E." not in markdown


def test_c_groups_identical_coverage_across_files(tmp_path: Path) -> None:
    """Covers: G-004 hygiene section C (identical-coverage groups)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    nodes = [
        "tests/test_a.py::test_1",
        "tests/test_a.py::test_2",
        "tests/test_b.py::test_1",
        "tests/test_c.py::test_1",
        "tests/test_c.py::test_2",
        "tests/test_d.py::test_x",
    ]
    map_dir = write_map(
        tmp_path,
        commit_sha=head,
        nodes=nodes,
        shards={
            "src/saberops/alpha.py": {"5": [0, 1, 2], "9": [0, 1, 2]},
            "src/saberops/beta.py": {"4": [3, 4]},
        },
    )
    completed = run_hygiene(tmp_path, "--repo", str(repo), "--map", str(map_dir))
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    section_c = data["c"]
    assert isinstance(section_c, dict)
    groups = section_c["groups"]
    assert isinstance(groups, list)
    assert len(groups) == 1
    assert groups[0]["nodes"] == [
        "tests/test_a.py::test_1",
        "tests/test_a.py::test_2",
        "tests/test_b.py::test_1",
    ]
    assert groups[0]["lines"] == 2
    assert section_c["same_file_groups"] == 1
    assert "tests/test_d.py::test_x" not in markdown.split("## C.")[1].split("## D.")[0]
    assert "counted, not listed: 1" in markdown


@pytest.mark.parametrize(
    ("stem", "expected"),
    [
        ("test_r3e12_reviewer_resource_admission", True),
        ("test_c16b2_owner_workflow", True),
        ("test_h3_gate_misconfiguration", True),
        ("test_i004_glue", True),
        ("test_quota_exclusions", False),
        ("test_capacity_ledger", False),
    ],
)
def test_d_matches_task_named_stems(
    tmp_path: Path, stem: str, expected: bool
) -> None:
    """Covers: G-004 hygiene section D (task-named test files)."""
    module = load_hygiene()
    assert bool(module.TASK_NAMED_RE.match(stem)) is expected
    repo = seed_repo(tmp_path, BASE_FILES)
    write(
        repo,
        f"tests/{stem}.py",
        "def test_probe() -> None:\n"
        '    """Covers: G-004 hygiene section D."""\n',
    )
    commit(repo, "add probe file")
    head = git(repo, "rev-parse", "HEAD")
    nodes = [f"tests/{stem}.py::test_probe"]
    map_dir = write_map(tmp_path, commit_sha=head, nodes=nodes, shards={})
    completed = run_hygiene(tmp_path, "--repo", str(repo), "--map", str(map_dir))
    assert completed.returncode == 0, completed.stderr
    _, data = read_outputs(tmp_path)
    rows_d = data["d"]
    assert isinstance(rows_d, list)
    files = {row["file"]: row["tests"] for row in rows_d}
    if expected:
        assert files.get(f"tests/{stem}.py") == 1
    else:
        assert f"tests/{stem}.py" not in files


def test_e_orders_slowest_and_skips_without_junit(tmp_path: Path) -> None:
    """Covers: G-004 hygiene section E (slowest tests from junit)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    map_dir = write_map(tmp_path, commit_sha=head, nodes=[], shards={})
    junit_path = write_junit(
        tmp_path,
        [
            ("tests.test_a", "test_fast", 1.0),
            ("tests.test_a", "test_slow", 5.0),
            ("tests.test_b", "test_mid", 3.0),
        ],
    )
    completed = run_hygiene(
        tmp_path, "--repo", str(repo), "--map", str(map_dir), "--junit", str(junit_path)
    )
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    slowest = data["e"]
    assert isinstance(slowest, list)
    assert [entry["node"] for entry in slowest] == [
        "tests/test_a.py::test_slow",
        "tests/test_b.py::test_mid",
        "tests/test_a.py::test_fast",
    ]
    assert slowest[0]["time"] == pytest.approx(5.0)
    assert "5.000s tests/test_a.py::test_slow" in markdown

    plain = tmp_path / "plain"
    plain.mkdir()
    completed = subprocess.run(
        [sys.executable, str(HYGIENE_SCRIPT), "--repo", str(repo), "--map", str(map_dir)],
        check=False,
        capture_output=True,
        text=True,
        cwd=str(plain),
    )
    assert completed.returncode == 0, completed.stderr
    assert "## E." not in completed.stdout


def test_f_lists_new_tests_missing_covers(tmp_path: Path) -> None:
    """Covers: G-004 hygiene section F (new tests without Covers:)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    write(
        repo,
        "tests/test_f.py",
        '"""Fixture F tests."""\n\n\ndef test_old() -> None:\n    assert True\n\n\n'
        "def test_covered() -> None:\n"
        '    """Covers: G-004 hygiene section F."""\n    assert True\n',
    )
    commit(repo, "base tests")
    base = git(repo, "rev-parse", "HEAD")
    write(
        repo,
        "tests/test_f.py",
        '"""Fixture F tests."""\n\n\ndef test_old() -> None:\n    assert True\n\n\n'
        "def test_covered() -> None:\n"
        '    """Covers: G-004 hygiene section F."""\n    assert True\n\n\n'
        "def test_new() -> None:\n    assert True\n\n\n"
        "def test_new_covered() -> None:\n"
        '    """Covers: G-004 hygiene section F."""\n    assert True\n',
    )
    commit(repo, "new tests")
    head = git(repo, "rev-parse", "HEAD")
    map_dir = write_map(tmp_path, commit_sha=head, nodes=[], shards={})
    completed = run_hygiene(
        tmp_path, "--repo", str(repo), "--map", str(map_dir), "--base", base
    )
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    assert data["f"] == ["tests/test_f.py::test_new"]
    assert "- tests/test_f.py::test_new\n" in markdown
    assert "test_old" not in markdown.split("## F.")[1]


def test_stale_map_skips_b_and_c(tmp_path: Path) -> None:
    """Covers: G-004 map freshness (stale map skips sections B and C)."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    map_dir = write_map(
        tmp_path,
        commit_sha="0" * 40,
        nodes=["tests/test_alpha.py::test_only"],
        shards={"src/saberops/alpha.py": {"5": [0]}},
    )
    junit_path = write_junit(tmp_path, [("tests.test_alpha", "test_only", 2.0)])
    write(
        repo,
        "tests/test_extra.py",
        "def test_added() -> None:\n    assert True\n",
    )
    commit(repo, "extra test")
    new_head = git(repo, "rev-parse", "HEAD")
    assert new_head != head
    completed = run_hygiene(
        tmp_path,
        "--repo",
        str(repo),
        "--map",
        str(map_dir),
        "--junit",
        str(junit_path),
        "--base",
        head,
    )
    assert completed.returncode == 0, completed.stderr
    markdown, data = read_outputs(tmp_path)
    assert f"map is stale ({'0' * 40} vs {new_head})" in markdown
    assert "## B." not in markdown
    assert "## C." not in markdown
    assert "## A." in markdown
    assert "## D." in markdown
    assert "## E." in markdown
    assert "## F." in markdown
    assert data["b"] is None
    assert data["c"] is None
    assert isinstance(data["a"], list)
    assert isinstance(data["d"], list)
    assert isinstance(data["e"], list)
    assert isinstance(data["f"], list)


def test_run_is_readonly_and_validates_inputs(tmp_path: Path) -> None:
    """Covers: G-004 read-only run, exit 0 with candidates, exit 2 on bad input."""
    repo = seed_repo(tmp_path, BASE_FILES)
    head = git(repo, "rev-parse", "HEAD")
    nodes = ["tests/test_alpha.py::test_only"]
    map_dir = write_map(
        tmp_path, commit_sha=head, nodes=nodes, shards={"src/saberops/alpha.py": {"5": [0]}}
    )
    before_status = git(repo, "status", "--porcelain")
    before_mtimes = {
        path.name: os.stat(path).st_mtime_ns for path in map_dir.rglob("*") if path.is_file()
    }
    completed = run_hygiene(tmp_path, "--repo", str(repo), "--map", str(map_dir))
    assert completed.returncode == 0, completed.stderr
    assert git(repo, "status", "--porcelain") == before_status
    after_mtimes = {
        path.name: os.stat(path).st_mtime_ns for path in map_dir.rglob("*") if path.is_file()
    }
    assert after_mtimes == before_mtimes

    bad_junit = subprocess.run(
        [
            sys.executable,
            str(HYGIENE_SCRIPT),
            "--repo",
            str(repo),
            "--junit",
            str(tmp_path / "missing.xml"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert bad_junit.returncode == 2
    assert git(repo, "status", "--porcelain") == before_status
