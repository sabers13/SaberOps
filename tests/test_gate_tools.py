"""Synthetic resource and evidence checks; no product/provider execution."""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest


def load_tools() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "gate_tools", Path(__file__).resolve().parents[1] / "scripts/gate_tools.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


g = load_tools()


def owned(root: Path, name: str, pid: int, failed: bool = False) -> Path:
    path = root / name
    path.mkdir()
    (path / "owner.json").write_text(json.dumps({"pid": pid, "worktree": name, "failed": failed}))
    return path


def test_sweep(tmp_path: Path) -> None:
    dead = owned(tmp_path, "dead", 99999999)
    live = owned(tmp_path, "live", os.getpid())
    old = owned(tmp_path, "old", os.getpid())
    os.utime(old, (time.time() - 90000, time.time() - 90000))
    g.sweep(tmp_path)
    assert not dead.exists() and live.exists() and not old.exists()


def test_floor() -> None:
    assert g.required_free(100 * g.GB, 5) == 10 * g.GB
    assert g.required_free(10 * g.GB, 5) == 5 * g.GB


def test_keep_failed_cap(tmp_path: Path) -> None:
    old = owned(tmp_path, "old", os.getpid(), True)
    new = owned(tmp_path, "new", os.getpid(), True)
    (old / "data").write_bytes(b"x" * 100)
    (new / "data").write_bytes(b"x" * 100)
    os.utime(old, (1, 1))
    g.keep_failed(tmp_path, "new", g.size(new))
    assert not old.exists() and new.exists()
    g.keep_failed(tmp_path, "new", 0)
    assert not new.exists()


def test_slot_waits(tmp_path: Path) -> None:
    notices: list[str] = []
    started = time.monotonic()
    with g.slot(tmp_path, {"pid": os.getpid()}):
        with pytest.raises(TimeoutError):
            with g.slot(tmp_path, {}, wait=0.2, notify=notices.append):
                pytest.fail("second slot acquired")
    assert 0.15 <= time.monotonic() - started < 2
    assert notices and "waiting" in notices[0]
    with g.slot(tmp_path, {}, wait=0):
        pass


def test_lane_targets(tmp_path: Path) -> None:
    (tmp_path / "tests").mkdir()
    for name in ("test_new.py", "test_modified.py", "test_core.py"):
        (tmp_path / "tests" / name).touch()
    assert g.lane_targets(
        tmp_path,
        [
            "tests/test_new.py",
            "tests/test_modified.py",
            "src/saberops/sub/core.py",
            "tests/test_deleted.py",
            "docs/gate.md",
        ],
        ["tests/test_core.py::test_one"],
    ) == [
        "tests/test_core.py",
        "tests/test_core.py::test_one",
        "tests/test_modified.py",
        "tests/test_new.py",
    ]
    assert g.lane_targets(tmp_path, ["docs/gate.md"], []) == []


@pytest.mark.parametrize(
    ("code", "log", "expected"),
    [
        (0, "", ("PASS", "", 0)),
        (1, "No space left on device", ("INFRA", "ENOSPC", 3)),
        (1, "Errno 28", ("INFRA", "ENOSPC", 3)),
        (3, "", ("INFRA", "PYTEST_INTERNAL", 3)),
        (1, "INTERNALERROR", ("INFRA", "PYTEST_INTERNAL", 3)),
        (2, "ImportError", ("FAIL", "", 1)),
        (124, "", ("TIMEOUT", "", 1)),
        (-15, "", ("INFRA", "INTERRUPTED", 3)),
    ],
)
def test_outcomes(code: int, log: str, expected: tuple[str, str, int]) -> None:
    assert g.evaluate(g.Evidence(codes=[code], log=log, count=1))[:3] == expected


def test_junit_and_rerun(tmp_path: Path) -> None:
    xml = tmp_path / "first.xml"
    xml.write_text(
        '<testsuites><testsuite><testcase classname="tests.test_a.TestA" '
        'name="test_one"><failure>AssertionError: broken</failure></testcase>'
        "</testsuite></testsuites>"
    )
    first = g.junit(xml)
    node = "tests/test_a.py::TestA::test_one"
    assert list(first.failed) == [node]
    first.codes = [1]
    assert g.evaluate(first)[0] == "FAIL"
    rerun = g.Evidence(codes=[0], count=1, passed={node})
    assert g.evaluate(first, rerun, {node})[0] == "FLAKY_PASS"
    assert g.evaluate(first, rerun)[0] == "FAIL"
    first.failed[node] = "Failed: Timeout (>180.0s)"
    assert g.evaluate(first)[0] == "TIMEOUT"
    assert g.evaluate(g.Evidence(codes=[5]), selected=True)[2] == 5


def test_skipped_rerun_is_not_flaky_pass() -> None:
    first = g.Evidence(codes=[1], count=1, failed={"tests/test_a.py::test_a": "broken"})
    rerun = g.Evidence(codes=[0], count=1)
    assert g.evaluate(first, rerun, {"tests/test_a.py::test_a"})[0] == "FAIL"


def test_keep_only_newest_failure_per_worktree(tmp_path: Path) -> None:
    old = owned(tmp_path, "old", os.getpid(), True)
    new = owned(tmp_path, "new", os.getpid(), True)
    record = json.loads((old / "owner.json").read_text())
    record["worktree"] = "new"
    (old / "owner.json").write_text(json.dumps(record))
    os.utime(old, (1, 1))
    g.keep_failed(tmp_path, "new", g.GB)
    assert not old.exists() and new.exists()


def test_collection_junit_keeps_file(tmp_path: Path) -> None:
    xml = tmp_path / "collection.xml"
    xml.write_text(
        '<testsuites><testsuite><testcase classname="" '
        'name="tests/test_broken.py"><error>ImportError: broken</error>'
        "</testcase></testsuite></testsuites>"
    )
    evidence = g.junit(xml)
    evidence.codes = [2]
    assert list(evidence.failed) == ["tests/test_broken.py"]
    assert g.evaluate(evidence)[0] == "FAIL"
