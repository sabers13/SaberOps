#!/usr/bin/env python3
"""Resource ownership and evidence for development gates; no product imports."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import shutil
import subprocess
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

GB = 1024**3


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=repo, text=True).strip()


def size(path: Path) -> int:
    total = 0
    for child in path.rglob("*"):
        try:
            if child.is_file() and not child.is_symlink():
                total += child.stat().st_size
        except FileNotFoundError:
            pass
    return total


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def owner(path: Path) -> dict[str, Any]:
    try:
        return dict(json.loads((path / "owner.json").read_text()))
    except (OSError, ValueError, TypeError):
        return {}


def remove(path: Path) -> None:
    print(f"remove {path}: {size(path)} bytes", flush=True)
    shutil.rmtree(path)


def sweep(root: Path, legacy: Path | None = None, now: float | None = None) -> None:
    now = time.time() if now is None else now
    for path in root.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        record = owner(path)
        # Only owned run directories are ours to remove.
        if record and (not alive(int(record["pid"])) or now - path.stat().st_mtime > 86400):
            remove(path)
    if legacy and legacy.is_dir():
        for path in legacy.glob("pytest-*"):
            if path.is_dir() and not path.is_symlink() and now - path.stat().st_mtime > 86400:
                remove(path)


def required_free(total: int, minimum_gb: float) -> float:
    return max(minimum_gb * GB, total * 0.1)


def disk_ok(paths: list[Path], minimum_gb: float) -> bool:
    okay = True
    for path in paths:
        usage = shutil.disk_usage(path)
        required = required_free(usage.total, minimum_gb)
        print(f"disk {path}: free={usage.free} required={int(required)} bytes", flush=True)
        okay &= usage.free >= required
    return okay


def keep_failed(root: Path, worktree: str, cap: int) -> None:
    failed = [p for p in root.iterdir() if p.is_dir() and owner(p).get("failed")]
    failed.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    seen: set[str] = set()
    kept: list[Path] = []
    for path in failed:
        name = str(owner(path).get("worktree"))
        if name in seen:
            remove(path)
        else:
            seen.add(name)
            kept.append(path)
    total = sum(size(p) for p in kept)
    for path in reversed(kept):
        if total <= cap:
            break
        total -= size(path)
        remove(path)


@contextlib.contextmanager
def slot(
    root: Path,
    record: dict[str, Any],
    count: int = 1,
    wait: float = 3600,
    observe: bool = False,
    notify: Callable[[str], None] = print,
) -> Iterator[None]:
    """flock releases on death; lane observers reserve all slots until entry."""
    if count < 1:
        raise ValueError("SABEROPS_GATE_SLOTS must be positive")
    root.mkdir(parents=True, exist_ok=True)
    handles: list[TextIO] = [(root / f"{i}.lock").open("a+") for i in range(count)]
    acquired: list[TextIO] = []
    started = time.monotonic()
    next_notice = started
    try:
        while True:
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired.append(handle)
                    if not observe:
                        break
                except BlockingIOError:
                    pass
            if (observe and len(acquired) == count) or (not observe and acquired):
                break
            for handle in acquired:
                fcntl.flock(handle, fcntl.LOCK_UN)
            acquired.clear()
            if time.monotonic() >= next_notice:
                holders = []
                for i in range(count):
                    meta = root / f"{i}.json"
                    if meta.exists():
                        holders.append(meta.read_text())
                notify("waiting for full-gate slot: " + "; ".join(holders))
                next_notice = time.monotonic() + 60
            if time.monotonic() - started >= wait:
                raise TimeoutError("SLOT_WAIT_TIMEOUT")
            time.sleep(min(0.1, max(0, wait - (time.monotonic() - started))))
        if not observe:
            index = handles.index(acquired[0])
            (root / f"{index}.json").write_text(json.dumps(record))
        yield
    finally:
        if acquired and not observe:
            (root / f"{handles.index(acquired[0])}.json").unlink(missing_ok=True)
        for handle in handles:
            handle.close()


def lane_targets(repo: Path, changed: list[str], also: list[str]) -> list[str]:
    targets = set(also)
    for name in changed:
        path = Path(name)
        if name.startswith("tests/") and path.name.startswith("test_") and path.suffix == ".py":
            if (repo / path).is_file():
                targets.add(name)
        elif name.startswith("src/saberops/") and path.suffix == ".py":
            test = f"tests/test_{path.stem}.py"
            if (repo / test).is_file():
                targets.add(test)
    return sorted(targets)


@dataclass
class Evidence:
    codes: list[int] = field(default_factory=list)
    log: str = ""
    cases: dict[str, str] = field(default_factory=dict)
    failed: dict[str, str] = field(default_factory=dict)
    count: int = 0


def junit(path: Path) -> Evidence:
    result = Evidence()
    if not path.exists():
        return result
    tree = ET.parse(path)
    for case in tree.iter("testcase"):
        filename = case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
        node = filename + "::" + case.get("name", "")
        # xunit2 classname includes class names; split at the test module.
        if ".py" not in filename or not Path(filename).name.startswith("test_"):
            parts = case.get("classname", "").split(".")
            module = next((i for i, p in enumerate(parts) if p.startswith("test_")), len(parts) - 1)
            node = "/".join(parts[: module + 1]) + ".py::"
            node += "::".join([*parts[module + 1 :], case.get("name", "")])
        error = case.find("failure")
        if error is None:
            error = case.find("error")
        text = "" if error is None else "".join(error.itertext())
        result.cases[node] = text
        if error is not None:
            result.failed[node] = text
        result.count += 1
    return result


def evaluate(
    initial: Evidence,
    rerun: Evidence | None = None,
    quarantine: set[str] | None = None,
    checks_ok: bool = True,
    selected: bool = True,
) -> tuple[str, str, int, list[str], list[str]]:
    all_evidence = [initial] + ([rerun] if rerun else [])
    logs = "\n".join(e.log for e in all_evidence)
    codes = [c for e in all_evidence for c in e.codes]
    if "No space left on device" in logs or "Errno 28" in logs:
        return "INFRA", "ENOSPC", 3, sorted(initial.failed), []
    if 3 in codes or "INTERNALERROR" in logs:
        return "INFRA", "PYTEST_INTERNAL", 3, sorted(initial.failed), []
    if any(c < 0 or c in (130, 143) for c in codes):
        return "INFRA", "INTERRUPTED", 3, sorted(initial.failed), []
    flaky = sorted(set(initial.failed) - set(rerun.failed)) if rerun else []
    final = sorted(rerun.failed if rerun else initial.failed)
    if 124 in codes or (
        final
        and any(
            "Timeout" in e.failed.get(n, "") or "timeout" in e.failed.get(n, "")
            for e in all_evidence
            for n in final
        )
    ):
        return "TIMEOUT", "", 1, final, flaky
    if selected and initial.count == 0 and 5 in codes:
        return "FAIL", "SELECTED_BUT_ZERO_RAN", 5, final, flaky
    unquarantined = sorted(set(flaky) - (quarantine or set()))
    bad_initial = any(c not in (0, 1, 5) for c in initial.codes)
    bad_rerun = rerun is not None and any(c != 0 for c in rerun.codes)
    if not checks_ok or final or unquarantined or bad_initial or bad_rerun:
        reason = "unquarantined: " + ", ".join(unquarantined) if unquarantined else ""
        return "FAIL", reason, 1, final, flaky
    if any(c == 1 for c in initial.codes) and not initial.failed:
        return "FAIL", "pytest failed without rerunnable node IDs", 1, final, flaky
    return ("FLAKY_PASS" if flaky else "PASS"), "", 0, final, flaky

