#!/usr/bin/env python3
"""Backend scenario matrix against scripted fake providers (no accounts, no network).

Each case builds a fresh target repository and fresh owner state, then runs one
``orch run`` through the real orchestrator with ``codex`` replaced by
``fakecli.py`` (see its docstring for the ``[fake:...]`` tags).  The output is one
line per case: expected-vs-actual status, exit code, duration and the run summary.

    .venv/bin/python scripts/scenarios/matrix.py            # every case
    .venv/bin/python scripts/scenarios/matrix.py happy gate_fail

Expectation ``"?"`` means the status is recorded, not asserted: those cases document
current behaviour that is a judgement call rather than a defect.  The process exits
non-zero when any asserted case differs, so this can run unattended.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOT = Path(os.environ.get("SCENARIO_ROOT", REPO / ".scenario-runs"))
ORCH = os.environ.get("ORCH_BIN", str(REPO / ".venv" / "bin" / "orch"))
FAKE_BIN = ROOT / "fake-bin"
GATE = f"{sys.executable} -m pytest -q"


@dataclass(frozen=True)
class Case:
    name: str
    task: str
    expect: str
    args: tuple[str, ...] = ()
    env: dict[str, str] = field(default_factory=dict)


CASES = (
    Case("happy", "Add subtract", "COMPLETED"),
    Case("worker_fail", "Add subtract [fake:fail]", "FAILED"),
    Case("quota", "Add subtract [fake:quota]", "FAILED"),
    Case("malformed", "Add subtract [fake:malformed]", "?"),
    Case("noop", "Add subtract [fake:noop]", "?"),
    Case("crashmid", "Add subtract [fake:crashmid]", "FAILED"),
    Case(
        "hang_timeout",
        "Add subtract [fake:hang]",
        "FAILED",
        ("--worker-timeout", "3", "--max-attempts", "1"),
    ),
    Case("gate_fail", "Add subtract", "FAILED", ("--gate", "false")),
    Case("gate_missing", "Add subtract", "FAILED", ("--gate", "definitely-not-a-command")),
    Case(
        "gate_timeout",
        "Add subtract",
        "FAILED",
        ("--gate", "sleep 30", "--gate-timeout", "2", "--max-attempts", "1"),
    ),
    Case("logged_out", "Add subtract", "?", env={"FAKE_LOGGED_OUT": "1"}),
    Case("big_prompt", "Add subtract " + "x" * 110_000, "COMPLETED"),
    Case("review_clean", "Add subtract [fake:review-clean]", "?", ("--review",)),
    Case("unicode_task", 'Füge Subtraktion hinzu — 日本語 ✓ "quotes" $HOME `ls`', "COMPLETED"),
)


def ensure_fake_bin() -> None:
    FAKE_BIN.mkdir(parents=True, exist_ok=True)
    for name in ("codex", "claude"):
        link = FAKE_BIN / name
        if not link.is_symlink():
            link.symlink_to(Path(__file__).resolve().with_name("fakecli.py"))


def git(path: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=path, check=True, capture_output=True)


def fresh_target(name: str) -> Path:
    case_dir = ROOT / "cases" / name
    shutil.rmtree(case_dir, ignore_errors=True)
    target = case_dir / "target"
    target.mkdir(parents=True)
    git(target, "init", "-q", "-b", "main")
    git(target, "config", "user.email", "scenario@localhost")
    git(target, "config", "user.name", "scenario")
    (target / "calc.py").write_text("def add(a, b):\n    return a + b\n")
    (target / "test_calc.py").write_text(
        "from calc import add\n\n\ndef test_add():\n    assert add(1, 2) == 3\n"
    )
    (target / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n")
    (target / "Makefile").write_text(f".PHONY: gate\ngate:\n\t{GATE}\n")
    git(target, "add", "-A")
    git(target, "commit", "-qm", "init")
    return target


def environment(case_dir: Path, extra: dict[str, str]) -> dict[str, str]:
    state = case_dir / "state"
    env = dict(os.environ)
    env.update(
        PATH=f"{FAKE_BIN}:{Path(ORCH).parent}:{env['PATH']}",
        XDG_STATE_HOME=str(state / "s"),
        XDG_CONFIG_HOME=str(state / "c"),
        XDG_CACHE_HOME=str(state / "k"),
        XDG_DATA_HOME=str(state / "d"),
        FAKE_LOG=str(case_dir / "fake.log"),
    )
    env.update(extra)
    return env


def run_case(case: Case) -> bool:
    target = fresh_target(case.name)
    case_dir = target.parent
    command = [
        ORCH,
        "run",
        "--repo",
        str(target),
        "--task",
        case.task,
        *(() if "--gate" in case.args else ("--gate", GATE)),
        "--provider",
        "codex",
        "--model",
        "gpt-5.6-terra",
        *case.args,
    ]
    started = time.time()
    try:
        done = subprocess.run(
            command,
            env=environment(case_dir, case.env),
            capture_output=True,
            text=True,
            timeout=240,
        )
        output, code = done.stdout + done.stderr, str(done.returncode)
    except subprocess.TimeoutExpired:
        output, code = "", "TIMEOUT"
    (case_dir / "out.txt").write_text(output)
    status_match = re.search(r"^Status\s*:\s*(\S+)", output, re.M)
    summary_match = re.search(r"^Summary\s*:\s*(.*)$", output, re.M)
    status = status_match.group(1) if status_match else "NO-STATUS"
    ok = case.expect in ("?", status)
    summary = (summary_match.group(1) if summary_match else output.strip()[-160:])[:130]
    print(
        f"{'ok ' if ok else 'BAD'} {case.name:14s} rc={code:7s} status={status:10s} "
        f"{time.time() - started:5.1f}s  {summary}",
        flush=True,
    )
    return ok


def main(selected: set[str]) -> int:
    ensure_fake_bin()
    results = [run_case(case) for case in CASES if not selected or case.name in selected]
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main(set(sys.argv[1:])))
