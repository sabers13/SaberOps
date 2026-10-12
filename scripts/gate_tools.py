#!/usr/bin/env python3
"""Resource ownership and evidence for development gates; no product imports."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

GB = 1024**3

HISTORY_DIRNAME = "saberops-gate-history"
HISTORY_FILENAME = "full.jsonl"
GATE_LABEL_ENV = "SABEROPS_GATE_LABEL"
DEFAULT_GATE_LABEL = "manual"


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


def test_directories(base: Path) -> list[Path]:
    directories: list[Path] = []
    for path in base.iterdir():
        if path.is_dir() and not path.is_symlink():
            directories.extend(path.iterdir() if path.name.startswith("popen-gw") else [path])
    return directories


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
    message: str = "waiting for full-gate slot: ",
    acquire_all: bool = False,
    slot_env: str = "SABEROPS_GATE_SLOTS",
) -> Iterator[None]:
    """flock releases on death; lane observers reserve all slots until entry.

    Acquisition order is always heavy -> light for the full gate, and a lane
    never waits for the heavy slot while holding a light one. Holders only
    wait for slots they do not hold, so there is no circular wait and no
    deadlock: lanes queue on heavy before taking light, and the full gate
    takes heavy before draining light.
    """
    if count < 1:
        raise ValueError(f"{slot_env} must be positive")
    root.mkdir(parents=True, exist_ok=True)
    handles: list[TextIO] = [(root / f"{i}.lock").open("a+") for i in range(count)]
    acquired: list[TextIO] = []
    started = time.monotonic()
    next_notice = started
    hold_all = acquire_all and not observe
    try:
        while True:
            for handle in handles:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    acquired.append(handle)
                    if not observe and not hold_all:
                        break
                except BlockingIOError:
                    pass
            if (observe and len(acquired) == count) or (not observe and acquired):
                if not observe and hold_all and len(acquired) < count:
                    for handle in acquired:
                        fcntl.flock(handle, fcntl.LOCK_UN)
                    acquired.clear()
                else:
                    break
            else:
                for handle in acquired:
                    fcntl.flock(handle, fcntl.LOCK_UN)
                acquired.clear()
            if time.monotonic() >= next_notice:
                holders = []
                for i in range(count):
                    meta = root / f"{i}.json"
                    if meta.exists():
                        try:
                            holders.append(meta.read_text())
                        except FileNotFoundError:
                            pass
                notify(message + "; ".join(holders))
                sys.stdout.flush()
                next_notice = time.monotonic() + 60
            if time.monotonic() - started >= wait:
                raise TimeoutError("SLOT_WAIT_TIMEOUT")
            time.sleep(min(0.1, max(0, wait - (time.monotonic() - started))))
        if not observe:
            for handle in acquired:
                index = handles.index(handle)
                (root / f"{index}.json").write_text(json.dumps(record))
        yield
    finally:
        if acquired and not observe:
            for handle in acquired:
                (root / f"{handles.index(handle)}.json").unlink(missing_ok=True)
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
    passed: set[str] = field(default_factory=set)
    count: int = 0


def junit(path: Path) -> Evidence:
    result = Evidence()
    if not path.exists():
        return result
    tree = ET.parse(path)
    for case in tree.iter("testcase"):
        if not case.get("classname") and case.get("name", "").endswith(".py"):
            node = case.get("name", "")
        else:
            node = ""
        filename = case.get("file") or case.get("classname", "").replace(".", "/") + ".py"
        node = node or filename + "::" + case.get("name", "")
        # xunit2 classname includes class names; split at the test module.
        if case.get("classname") and (
            ".py" not in filename or not Path(filename).name.startswith("test_")
        ):
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
        elif case.find("skipped") is None:
            result.passed.add(node)
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
    flaky = sorted(set(initial.failed) & rerun.passed) if rerun else []
    final = sorted(
        (set(initial.failed) - rerun.passed) | set(rerun.failed) if rerun else initial.failed
    )
    if 124 in codes or (
        final
        and any(
            "Timeout" in e.failed.get(n, "") or "timeout" in e.failed.get(n, "")
            for e in all_evidence
            for n in final
        )
    ):
        return "TIMEOUT", "", 1, final, flaky
    if selected and initial.count == 0 and all(c in (0, 5) for c in codes):
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


def history_path(common: Path) -> Path:
    """Full-gate history file shared by all worktrees of one checkout.

    Covers: ADR-0003 D5 (flaky tracking history format).
    """
    return common / HISTORY_DIRNAME / HISTORY_FILENAME


def append_history(path: Path, entry: dict[str, Any]) -> None:
    """Append one JSON line with an exclusive flock; warn, never raise.

    Covers: prompt section A (history append is best-effort).
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                handle.write(json.dumps(entry, sort_keys=True) + "\n")
                handle.flush()
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError as error:
        print(f"warning: could not write gate history: {error}", flush=True)


def load_history(path: Path) -> list[dict[str, Any]]:
    """Read full.jsonl oldest-first; missing file or bad lines yield [].

    Covers: prompt section B (flaky-report input).
    """
    if not path.exists():
        return []
    try:
        text = path.read_text()
    except OSError:
        return []
    entries: list[dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return entries


def read_quarantine(repo: Path) -> set[str]:
    """Exact quarantined node IDs from tests/quarantine.txt.

    Covers: prompt section B (quarantine marking is display-only).
    """
    quarantine_file = repo / "tests/quarantine.txt"
    if not quarantine_file.exists():
        return set()
    try:
        text = quarantine_file.read_text()
    except OSError:
        return set()
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def flaky_stats(
    entries: list[dict[str, Any]],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any] | None]:
    """Per-test flaky streak/total/last-seen plus the newest nightly entry.

    The streak counts consecutive non-INFRA full gates (newest first) in
    which the test was flaky; INFRA entries neither break nor extend it.
    Totals and last-seen likewise count non-INFRA gates only.

    Covers: prompt section B (streak counting newest-first, INFRA skipped).
    """
    per_test: dict[str, dict[str, Any]] = {}
    for entry in entries:
        if entry.get("outcome") == "INFRA":
            continue
        flaky = entry.get("flaky", [])
        if not isinstance(flaky, list):
            continue
        ts = str(entry.get("ts", ""))
        for node in flaky:
            if not isinstance(node, str):
                continue
            record = per_test.setdefault(node, {"total": 0, "last_seen": ""})
            record["total"] = int(record["total"]) + 1
            # Entries are oldest-first, so the newest flaky ts wins.
            record["last_seen"] = ts
    for node in per_test:
        streak = 0
        for entry in reversed(entries):
            if entry.get("outcome") == "INFRA":
                continue
            flaky = entry.get("flaky", [])
            if not isinstance(flaky, list):
                break
            if node in flaky:
                streak += 1
            else:
                break
        per_test[node]["streak"] = streak
    newest_nightly: dict[str, Any] | None = None
    for entry in reversed(entries):
        if entry.get("label") == "nightly":
            newest_nightly = entry
            break
    return per_test, newest_nightly


def render_flaky_report(
    history_file: Path,
    entries: list[dict[str, Any]],
    min_streak: int,
    quarantine: set[str],
    as_json: bool,
) -> str:
    """Render the human markdown report or the --json payload.

    Covers: prompt section B (quarantine proposals + nightly failures).
    """
    per_test, newest_nightly = flaky_stats(entries)
    proposals = sorted(
        (node for node, info in per_test.items() if int(info.get("streak", 0)) >= min_streak)
    )
    nightly_failed: list[str] = []
    if newest_nightly is not None:
        failed = newest_nightly.get("failed", [])
        if isinstance(failed, list):
            nightly_failed = sorted(n for n in failed if isinstance(n, str))
    if as_json:
        payload = {
            "history": str(history_file),
            "entries": len(entries),
            "min_streak": min_streak,
            "proposals": [
                {
                    "id": node,
                    "streak": int(per_test[node].get("streak", 0)),
                    "total": int(per_test[node].get("total", 0)),
                    "last_seen": str(per_test[node].get("last_seen", "")),
                    "quarantined": node in quarantine,
                }
                for node in proposals
            ],
            "nightly_failures": nightly_failed,
            "nightly": newest_nightly,
        }
        return json.dumps(payload, indent=2, sort_keys=True) + "\n"
    lines = ["# Flaky report", ""]
    lines.append(f"history: {history_file} ({len(entries)} full gates)")
    lines.append("")
    lines.append(f"## quarantine proposals (streak >= {min_streak})")
    if not proposals:
        lines.append("(none)")
    else:
        for node in proposals:
            info = per_test[node]
            suffix = " [in quarantine]" if node in quarantine else ""
            lines.append(
                f"- {node} (streak={info.get('streak')}, "
                f"total={info.get('total')}, last={info.get('last_seen')}){suffix}"
            )
    lines.append("")
    lines.append("## nightly failures")
    if newest_nightly is None:
        lines.append("(no nightly run yet)")
    elif not nightly_failed:
        failed_ts = newest_nightly.get("ts", "")
        failed_sha = newest_nightly.get("head_sha", "")
        lines.append(f"(none; newest nightly {failed_ts} {failed_sha})")
    else:
        lines.append(f"newest nightly {newest_nightly.get('ts', '')} "
                     f"{newest_nightly.get('head_sha', '')}:")
        for node in nightly_failed:
            lines.append(f"- {node}")
    lines.append("")
    return "\n".join(lines)


def flaky_report_cli(argv: list[str]) -> int:
    """Entry point for `gate_tools.py flaky-report [--min-streak N] [--json]`.

    Covers: prompt section B (flaky-report CLI).
    """
    parser = argparse.ArgumentParser(prog="gate_tools.py flaky-report")
    parser.add_argument("--min-streak", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    parsed = parser.parse_args(argv)
    if parsed.min_streak < 1:
        parser.error("--min-streak must be >= 1")
    repo = Path.cwd()
    try:
        common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
        history_file = history_path(common)
    except subprocess.CalledProcessError:
        history_file = Path.cwd() / HISTORY_DIRNAME / HISTORY_FILENAME
    entries = load_history(history_file)
    quarantine = read_quarantine(repo)
    report = render_flaky_report(history_file, entries, parsed.min_streak, quarantine, parsed.json)
    print(report, end="")
    return 0


def exclusive_cli(argv: list[str]) -> int:
    """Run CMD while holding the heavy slot and all light slots.

    Uses exactly the full gate's acquisition order, waits, messages and
    SABEROPS_GATE_SLOT_WAIT timeout via slot(); no second implementation.

    Covers: prompt section C (exclusive map-rebuild slot).
    """
    cmd: list[str] = list(argv[1:]) if argv[:1] == ["--"] else list(argv)
    if not cmd or cmd == ["--"]:
        print("usage: gate_tools.py exclusive -- CMD [ARGS...]", file=sys.stderr)
        return 2
    if cmd and cmd[0] == "--":
        cmd = cmd[1:]
    if not cmd:
        print("usage: gate_tools.py exclusive -- CMD [ARGS...]", file=sys.stderr)
        return 2
    repo = Path.cwd()
    try:
        common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
    except subprocess.CalledProcessError:
        print("INFRA SLOT_WAIT_TIMEOUT (not a git repository)", flush=True)
        return 3
    heavy_root = common / "saberops-gate-slots"
    light_root = common / "saberops-gate-light-slots"
    try:
        heavy_count = int(os.environ.get("SABEROPS_GATE_SLOTS", "1"))
        light_count = int(os.environ.get("SABEROPS_LANE_SLOTS", "2"))
        wait = float(os.environ.get("SABEROPS_GATE_SLOT_WAIT", "3600"))
    except ValueError as error:
        print(f"INFRA SLOT_WAIT_TIMEOUT ({error})", flush=True)
        return 3
    record: dict[str, Any] = {
        "pid": os.getpid(),
        "worktree": str(repo),
        "started_at": datetime.now(UTC).isoformat(),
        "kind": "exclusive",
    }
    try:
        with slot(
            heavy_root,
            record,
            heavy_count,
            wait,
            observe=False,
            message="waiting for full-gate slot: ",
        ):
            with slot(
                light_root,
                record,
                light_count,
                wait,
                acquire_all=True,
                message="waiting for running lane checks: ",
                slot_env="SABEROPS_LANE_SLOTS",
            ):
                completed = subprocess.run(cmd, check=False)
                return completed.returncode
    except TimeoutError:
        print("INFRA SLOT_WAIT_TIMEOUT", flush=True)
        return 3


class Interrupted(Exception):
    """A gate signal, after the child process group is stopped."""


class Runner:
    def __init__(self, repo: Path, kind: str, result_dir: Path | None = None) -> None:
        self.repo = repo
        self.kind = kind
        self.started = time.monotonic()
        self.common = (repo / git(repo, "rev-parse", "--git-common-dir")).resolve()
        user = os.environ.get("USER", str(os.getuid()))
        self.root = Path(
            os.environ.get(
                "SABEROPS_GATE_TMP", f"{os.environ.get('TMPDIR', '/tmp')}/saberops-gate-{user}"
            )
        )
        self.root.mkdir(parents=True, exist_ok=True)
        sweep(self.root, Path(f"/tmp/pytest-of-{user}"))
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%f")
        self.run = self.root / f"{repo.name}-{stamp}-{os.getpid()}"
        self.run.mkdir()
        self.record = {
            "pid": os.getpid(),
            "worktree": str(repo),
            "started_at": datetime.now(UTC).isoformat(),
            "kind": kind,
        }
        (self.run / "owner.json").write_text(json.dumps(self.record))
        self.result = result_dir or self.run / "result"
        self.result.mkdir(parents=True, exist_ok=True)
        self.python = (
            str(repo / ".venv/bin/python")
            if (repo / ".venv/bin/python").exists()
            else sys.executable
        )
        self.child: subprocess.Popen[str] | None = None
        self.peak = 0
        self.test_count = 0
        self.heavy: list[tuple[int, str]] = []
        self.stop_monitor = threading.Event()
        self.monitor = threading.Thread(target=self.measure_loop, daemon=True)
        self.monitor.start()
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            signal.signal(sig, self.interrupt)

    def interrupt(self, signum: int, frame: Any) -> None:
        if self.child is not None and self.child.poll() is None:
            os.killpg(self.child.pid, signal.SIGTERM)
            try:
                self.child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(self.child.pid, signal.SIGKILL)
                self.child.wait()
        raise Interrupted()

    def measure_loop(self) -> None:
        while not self.stop_monitor.wait(0.5):
            try:
                self.measure()
            except OSError:
                pass  # pytest removes successful tmp_path directories concurrently

    def measure(self) -> None:
        value = size(self.run)
        if value >= self.peak:
            self.peak = value
            self.heavy = sorted(
                [
                    (size(p), str(p.relative_to(self.run)))
                    for base in self.run.glob("pytest-*")
                    for p in test_directories(base)
                    if p.is_dir() and not p.is_symlink()
                ],
                reverse=True,
            )[:10]

    def command(self, args: list[str], name: str, timeout: float | None = None) -> tuple[int, str]:
        print("+ " + " ".join(args), flush=True)
        logfile = self.result / f"{name}.log"
        # A file avoids pipe deadlocks and survives interruptions.
        with logfile.open("w") as output:
            self.child = subprocess.Popen(
                args,
                cwd=self.repo,
                stdout=output,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            started = time.monotonic()
            timed_out = False
            with logfile.open() as reader:
                while self.child.poll() is None:
                    text = reader.read()
                    if text:
                        print(text, end="", flush=True)
                    if timeout and time.monotonic() - started >= timeout:
                        timed_out = True
                        os.killpg(self.child.pid, signal.SIGTERM)
                        try:
                            self.child.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            os.killpg(self.child.pid, signal.SIGKILL)
                            self.child.wait()
                    time.sleep(0.05)
                print(reader.read(), end="", flush=True)
            code = 124 if timed_out else self.child.returncode
        self.child = None
        return code, logfile.read_text(errors="replace")

    def pytest(self, name: str, args: list[str], budget: float | None = None) -> Evidence:
        path = self.result / f"{name}.xml"
        argv = [
            self.python,
            "-m",
            "pytest",
            "-q",
            "--durations=20",
            "--junitxml",
            str(path),
            "--basetemp",
            str(self.run / f"pytest-{name}"),
            *args,
        ]
        if budget is not None:
            argv = ["timeout", "--kill-after=5", str(budget), *argv]
        code, log = self.command(argv, name, budget + 10 if budget else None)
        if budget and code == 137:
            code = 124
        try:
            evidence = junit(path)
        except ET.ParseError:
            evidence = Evidence()
            if code in (0, 1, 5):
                code = 3
        evidence.codes = [code]
        evidence.log = log
        return evidence

    def finish(self, verdict: tuple[str, str, int, list[str], list[str]], count: int) -> int:
        outcome, reason, code, failed, flaky = verdict
        self.stop_monitor.set()
        self.monitor.join()
        self.measure()
        wall = round(time.monotonic() - self.started, 2)
        print(f"run dir {self.run}: {size(self.run)} bytes; peak {self.peak} bytes")
        print("10 largest test dirs at peak:")
        for amount, name in self.heavy:
            print(f"  {amount} {name}")
        (self.result / "failed.txt").write_text("".join(n + "\n" for n in failed))
        (self.result / "flaky.txt").write_text("".join(n + "\n" for n in flaky))
        payload = {
            "outcome": outcome,
            "reason": reason,
            "counts": {"tests": count, "failed": len(failed), "flaky": len(flaky)},
            "wall_time": wall,
            "head_sha": git(self.repo, "rev-parse", "HEAD"),
            "peak_bytes": self.peak,
            "heavy_dirs": self.heavy,
        }
        (self.result / "outcome.json").write_text(json.dumps(payload, indent=2) + "\n")
        if self.kind == "full":
            entry: dict[str, Any] = {
                "ts": datetime.now(UTC).isoformat(timespec="seconds"),
                "head_sha": payload["head_sha"],
                "worktree": str(self.repo),
                "label": os.environ.get(GATE_LABEL_ENV, DEFAULT_GATE_LABEL),
                "outcome": outcome,
                "reason": reason,
                "tests": count,
                "failed": sorted(failed),
                "flaky": sorted(flaky),
                "wall_time": wall,
                "peak_bytes": self.peak,
            }
            append_history(history_path(self.common), entry)
        if not (self.result / "full.xml").exists():
            ET.ElementTree(ET.Element("testsuites")).write(self.result / "full.xml")
        # Publish evidence before cleaning the private scratch directory.
        if self.kind == "full":
            last = self.common / "saberops-gate-last"
            last.mkdir(parents=True, exist_ok=True)
            for old in [] if last.resolve() == self.result.resolve() else last.iterdir():
                if old.is_file():
                    old.unlink()
            for path in [] if last.resolve() == self.result.resolve() else self.result.iterdir():
                if path.is_file():
                    shutil.copy2(path, last / path.name)
        if os.environ.get("SABEROPS_GATE_KEEP") != "1":
            if outcome in ("FAIL", "TIMEOUT"):
                self.record["failed"] = True
                (self.run / "owner.json").write_text(json.dumps(self.record))
                keep_failed(
                    self.root,
                    str(self.repo),
                    int(float(os.environ.get("SABEROPS_GATE_KEEP_FAILED_GB", "2")) * GB),
                )
            else:
                remove(self.run)
        label = "GATE" if self.kind == "full" else "LANE CHECK"
        suffix = f" ({reason})" if reason else ""
        print(f"{count} tests; {len(failed)} failed; {len(flaky)} flaky; wall {wall}s")
        print(f"{label} OUTCOME: {outcome}{suffix}", flush=True)
        return code

    def execute(self, base: str, also: list[str]) -> int:
        verdict: tuple[str, str, int, list[str], list[str]] = ("INFRA", "INTERRUPTED", 3, [], [])
        try:
            minimum = float(os.environ.get("SABEROPS_GATE_MIN_FREE_GB", "10"))
            if not disk_ok([self.root, self.repo], minimum):
                return self.finish(("INFRA", "DISK_LOW", 3, [], []), 0)
            heavy_root = self.common / "saberops-gate-slots"
            light_root = self.common / "saberops-gate-light-slots"
            heavy_count = int(os.environ.get("SABEROPS_GATE_SLOTS", "1"))
            light_count = int(os.environ.get("SABEROPS_LANE_SLOTS", "2"))
            wait = float(os.environ.get("SABEROPS_GATE_SLOT_WAIT", "3600"))
            if self.kind == "full":
                with slot(
                    heavy_root,
                    self.record,
                    heavy_count,
                    wait,
                    observe=False,
                    message="waiting for full-gate slot: ",
                ):
                    with slot(
                        light_root,
                        self.record,
                        light_count,
                        wait,
                        acquire_all=True,
                        message="waiting for running lane checks: ",
                        slot_env="SABEROPS_LANE_SLOTS",
                    ):
                        return self.run_checks(base, also)
            with slot(
                heavy_root,
                self.record,
                heavy_count,
                wait,
                observe=True,
                message="waiting for full-gate slot: ",
            ):
                pass
            # Lane observer lock is released before taking a light slot below.
            with slot(
                light_root,
                self.record,
                light_count,
                wait,
                observe=False,
                message="waiting for lane slot: ",
                slot_env="SABEROPS_LANE_SLOTS",
            ):
                return self.run_checks(base, also)
        except TimeoutError:
            verdict = ("INFRA", "SLOT_WAIT_TIMEOUT", 3, [], [])
        except Interrupted:
            pass
        except OSError as error:
            verdict = ("INFRA", "ENOSPC" if error.errno == 28 else "PYTEST_INTERNAL", 3, [], [])
            print(str(error), flush=True)
        return self.finish(verdict, self.test_count)

    def run_checks(self, base: str, also: list[str]) -> int:
        targets: list[str] = []
        if self.kind == "lane":
            changed = set(git(self.repo, "diff", "--name-only", f"{base}..HEAD").splitlines())
            for git_args in [
                ("diff", "--name-only"),
                ("diff", "--cached", "--name-only"),
                ("ls-files", "--others", "--exclude-standard"),
            ]:
                changed.update(git(self.repo, *git_args).splitlines())
            print("changed files:\n" + "\n".join(sorted(changed)))
            targets = lane_targets(self.repo, sorted(changed), also)
            print(f"lane targets ({len(targets)}):\n" + "\n".join(targets))
        lint, _ = self.command([self.python, "-m", "ruff", "check", "."], "ruff")
        cache = os.environ.get("MYPY_CACHE_DIR", str(self.common / "saberops-mypy-cache"))
        types, _ = self.command(
            [self.python, "-m", "mypy", "--strict", "--cache-dir", cache, "src", "tests"], "mypy"
        )
        initial = Evidence()
        rerun = None
        if self.kind == "full":
            for name, args in [
                (
                    "parallel",
                    [
                        "-m",
                        "not serial",
                        "-n",
                        os.environ.get("SABEROPS_GATE_WORKERS", "auto"),
                        "--dist",
                        "worksteal",
                    ],
                ),
                ("serial", ["-m", "serial", "-p", "no:xdist"]),
            ]:
                part = self.pytest(name, args)
                initial.codes += part.codes
                initial.log += part.log
                initial.failed.update(part.failed)
                initial.cases.update(part.cases)
                initial.passed.update(part.passed)
                initial.count += part.count
                self.test_count = initial.count
                if evaluate(part)[0] == "INFRA":
                    break
            if (
                initial.failed
                and all(c in (0, 1, 5) for c in initial.codes)
                and evaluate(initial)[0] != "INFRA"
            ):
                rerun = self.pytest("rerun", ["-p", "no:xdist", *sorted(initial.failed)])
        elif targets:
            initial = self.pytest(
                "lane",
                [*(["-n", "4"] if len(targets) > 150 else ["-p", "no:xdist"]), *targets],
                float(os.environ.get("SABEROPS_LANE_TIMEOUT", "1200")),
            )
        self.test_count = initial.count
        quarantine_file = self.repo / "tests/quarantine.txt"
        quarantine = (
            {
                line.strip()
                for line in quarantine_file.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
            if quarantine_file.exists()
            else set()
        )
        verdict = evaluate(
            initial,
            rerun,
            quarantine,
            lint == 0 and types == 0,
            self.kind == "full" or bool(targets),
        )
        merged = ET.Element("testsuites")
        for path in self.result.glob("*.xml"):
            if path.name != "full.xml":
                try:
                    tree = ET.parse(path).getroot()
                    merged.extend(list(tree) if tree.tag == "testsuites" else [tree])
                except ET.ParseError:
                    pass
        ET.ElementTree(merged).write(self.result / "full.xml", encoding="utf-8")
        return self.finish(verdict, initial.count)


def scratch_cli() -> int:
    """Shared UI scratch adapter; the shell retains its existing check scope."""
    if sys.argv[1] == "scratch-start":
        manifest = Path(sys.argv[2])
        runner = Runner(Path.cwd(), "ui")
        runner.stop_monitor.set()
        runner.monitor.join()
        runner.record["pid"] = int(sys.argv[3])
        (runner.run / "owner.json").write_text(json.dumps(runner.record))
        manifest.write_text(str(runner.run))
        if not disk_ok(
            [runner.root, runner.repo], float(os.environ.get("SABEROPS_GATE_MIN_FREE_GB", "10"))
        ):
            remove(runner.run)
            return 3
        return 0
    path = Path(sys.argv[2])
    code = int(sys.argv[3])
    print(f"run dir {path}: {size(path)} bytes")
    for amount, name in sorted(
        [
            (size(p), str(p))
            for base in path.glob("pytest-*")
            for p in base.iterdir()
            if p.is_dir() and not p.is_symlink()
        ],
        reverse=True,
    )[:10]:
        print(f"  {amount} {name}")
    if os.environ.get("SABEROPS_GATE_KEEP") != "1":
        if code in (0, 3, 130, 143):
            remove(path)
        else:
            record = owner(path)
            record["failed"] = True
            (path / "owner.json").write_text(json.dumps(record))
            keep_failed(
                path.parent,
                str(record["worktree"]),
                int(float(os.environ.get("SABEROPS_GATE_KEEP_FAILED_GB", "2")) * GB),
            )
    return 0


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == "flaky-report":
        return flaky_report_cli(sys.argv[2:])
    if len(sys.argv) > 1 and sys.argv[1] == "exclusive":
        return exclusive_cli(sys.argv[2:])
    parser = argparse.ArgumentParser()
    parser.add_argument("kind", choices=["full", "lane"])
    parser.add_argument("--base")
    parser.add_argument("--also", nargs="+", action="extend", default=[])
    parser.add_argument("--out", type=Path)
    parser.add_argument("--result-dir", type=Path)
    args = parser.parse_args()
    if args.kind == "lane" and args.base is None:
        parser.error("lane check requires --base REF")
    args.base = args.base or "HEAD"
    if args.out:
        # tee the entire gate including the outcome without buffering a whole run.
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w") as output:
            child = subprocess.Popen(
                [
                    sys.executable,
                    __file__,
                    args.kind,
                    "--base",
                    args.base,
                    *(["--also", *args.also] if args.also else []),
                    *(["--result-dir", str(args.result_dir)] if args.result_dir else []),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                signal.signal(sig, lambda signum, frame: child.send_signal(signum))
            assert child.stdout
            for line in child.stdout:
                print(line, end="", flush=True)
                output.write(line)
                output.flush()
            return child.wait()
    return Runner(Path.cwd(), args.kind, args.result_dir).execute(args.base, args.also)


if __name__ == "__main__":
    raise SystemExit(
        scratch_cli() if len(sys.argv) > 1 and sys.argv[1].startswith("scratch-") else main()
    )
