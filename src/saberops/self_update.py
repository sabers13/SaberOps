"""``orch self-update``: reinstall the engine only from an accepted green commit.

After the owner accepts a repair (or any engine change) on the SaberOps
repository, the engine must be able to move its own daily install to that
commit.  This module is the guarded path for that:

* the commit must be **accepted** -- reachable from the protected target branch
  (the owner merges into that branch; the engine never moves it);
* the commit must be **green** -- the shared engine gate ledger holds a passing
  ``full`` profile entry for the exact SHA (``G3``, ``main-gate`` or ``manual``);
* the checkout must be **at that commit and clean**, so the reinstall can only
  build committed code;
* every RUNNING worker is parked at a safe point through the F17 pause path
  before the reinstall and released afterwards, so nothing is discarded;
* the new install is recorded in the restart marker and ``orch --version`` is
  reported before and after.

It never pushes to a remote and never decides acceptance itself.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from saberops.gate_history import GateKind, history_path, read_entries
from saberops.paths import saberops_state_dir
from saberops.runtime_gate import PauseReason
from saberops.vendor_cli_fixtures import REPO_ROOT

__all__ = [
    "RESTART_MARKER_FILENAME",
    "SelfUpdateResult",
    "engine_marker_path",
    "engine_revision",
    "marker_revision",
    "perform_self_update",
    "read_restart_marker",
    "restart_requested",
    "write_restart_marker",
]

RESTART_MARKER_FILENAME: str = "engine-restart.json"

#: Gate lifecycle points whose profile is the authoritative full gate.
FULL_GATE_KINDS: frozenset[str] = frozenset(
    {GateKind.G3.value, GateKind.MAIN_GATE.value, GateKind.MANUAL.value}
)

#: Only an exact passing word proves a green gate.
PASSING_OUTCOMES: frozenset[str] = frozenset({"PASS", "FLAKY_PASS"})

#: Bound on the reinstall command.
INSTALL_TIMEOUT_SECONDS: float = 900.0


@dataclass(frozen=True)
class SelfUpdateResult:
    """Outcome of one ``orch self-update`` attempt."""

    ok: bool
    code: str
    summary: str
    commit: str = ""
    target_ref: str = ""
    old_version: str = ""
    new_version: str = ""
    paused_jobs: tuple[str, ...] = ()
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "code": self.code,
            "summary": self.summary,
            "commit": self.commit,
            "target_ref": self.target_ref,
            "old_version": self.old_version,
            "new_version": self.new_version,
            "paused_jobs": list(self.paused_jobs),
            "evidence": dict(self.evidence),
        }


def _git(repo: Path, *args: str) -> tuple[int, str]:
    try:
        process = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=30.0,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 127, str(exc)
    return process.returncode, (process.stdout or process.stderr).strip()


def repo_root(path: str | Path | None = None) -> Path:
    """Resolve the repository root (defaults to the installed source tree)."""
    start = Path(path) if path is not None else REPO_ROOT
    code, out = _git(start, "rev-parse", "--show-toplevel")
    if code != 0 or not out:
        raise ValueError(f"{start} is not inside a git repository")
    return Path(out).resolve()


def engine_revision(repo: str | Path | None = None) -> str:
    """Return ``<sha>`` plus ``-dirty`` when the checkout has local changes."""
    root = repo_root(repo)
    code, sha = _git(root, "rev-parse", "HEAD")
    if code != 0 or not sha:
        return ""
    code, status = _git(root, "status", "--porcelain")
    if code == 0 and status:
        return f"{sha}-dirty"
    return sha


def engine_marker_path() -> Path:
    return saberops_state_dir() / RESTART_MARKER_FILENAME


def read_restart_marker() -> dict[str, Any]:
    """Return the durable restart marker, or an empty mapping."""
    path = engine_marker_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def write_restart_marker(repo: Path, *, previous: str = "") -> dict[str, Any]:
    """Record the engine revision a running process should restart into."""
    payload = {
        "revision": engine_revision(repo),
        "previous_revision": previous,
        "version": installed_version(repo),
    }
    path = engine_marker_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp, path)
    return payload


def restart_requested(started_revision: str) -> bool:
    """True when a newer engine revision was installed after this process started."""
    marker = read_restart_marker()
    revision = str(marker.get("revision") or "")
    return bool(revision) and revision != started_revision


def marker_revision() -> str:
    """The revision currently recorded for running processes to restart into."""
    return str(read_restart_marker().get("revision") or "")


def full_gate_evidence(repo: Path, commit: str) -> dict[str, Any] | None:
    """Return the passing full-gate history entry for ``commit``, if any."""
    code, common = _git(repo, "rev-parse", "--git-common-dir")
    if code != 0 or not common:
        return None
    common_dir = Path(common)
    if not common_dir.is_absolute():
        common_dir = (repo / common_dir).resolve()
    for entry in reversed(read_entries(history_path(common_dir))):
        if str(entry.get("head_sha") or "") != commit:
            continue
        if str(entry.get("outcome") or "") not in PASSING_OUTCOMES:
            continue
        if str(entry.get("profile") or "") != "full":
            continue
        if str(entry.get("kind") or "") not in FULL_GATE_KINDS:
            continue
        return dict(entry)
    return None


def installed_version(repo: Path | None = None) -> str:
    """Run ``orch --version`` through the current interpreter."""
    import saberops

    if repo is None:
        return f"orch {saberops.__version__}"
    env = dict(os.environ)
    source = repo / "src"
    env["PYTHONPATH"] = os.pathsep.join((str(source), env.get("PYTHONPATH", "")))
    try:
        process = subprocess.run(
            [sys.executable, "-m", "saberops.cli", "--version"],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60.0,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return f"orch {saberops.__version__}"
    return (process.stdout or process.stderr).strip() or f"orch {saberops.__version__}"


def _default_install(repo: Path) -> tuple[bool, str]:
    """Reinstall the daily install from the checked-out commit."""
    env = dict(os.environ)
    env.setdefault("PIP_DISABLE_PIP_VERSION_CHECK", "1")
    command = [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--no-deps",
        "--force-reinstall",
        "-e",
        str(repo),
    ]
    try:
        process = subprocess.run(
            command,
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=INSTALL_TIMEOUT_SECONDS,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"{type(exc).__name__}: {exc}"
    output = (process.stdout or "") + (process.stderr or "")
    return process.returncode == 0, output[-4000:]


def _refuse(code: str, summary: str, **kwargs: Any) -> SelfUpdateResult:
    return SelfUpdateResult(ok=False, code=code, summary=summary, **kwargs)


def perform_self_update(
    db: Any,
    *,
    repo: str | Path | None = None,
    target_ref: str = "main",
    commit: str | None = None,
    install: Callable[[Path], tuple[bool, str]] | None = None,
    version_probe: Callable[[Path], str] | None = None,
    recheck: Callable[[], Any] | None = None,
    pause: Callable[..., Any] | None = None,
    resume: Callable[..., Any] | None = None,
) -> SelfUpdateResult:
    """Reinstall the engine from an accepted, green commit and restart safely.

    The refusal reasons are stable codes: ``not_a_repository``,
    ``unknown_ref``, ``commit_not_accepted``, ``checkout_mismatch``,
    ``dirty_worktree``, ``no_full_gate``, ``pause_failed`` and
    ``install_failed``.  A refusal never touches the install, the gate or a
    running worker.
    """
    try:
        root = repo_root(repo)
    except ValueError as exc:
        return _refuse("not_a_repository", str(exc))
    code, target_sha = _git(root, "rev-parse", "--verify", f"{target_ref}^{{commit}}")
    if code != 0 or not target_sha:
        return _refuse("unknown_ref", f"target ref {target_ref!r} cannot be resolved")
    resolved_commit = commit or target_sha
    code, commit_sha = _git(root, "rev-parse", "--verify", f"{resolved_commit}^{{commit}}")
    if code != 0 or not commit_sha:
        return _refuse("unknown_commit", f"commit {resolved_commit!r} cannot be resolved")

    # Acceptance: the commit is already on the protected target branch.
    code, _ = _git(root, "merge-base", "--is-ancestor", commit_sha, target_sha)
    if code != 0:
        return _refuse(
            "commit_not_accepted",
            f"{commit_sha[:12]} is not on {target_ref}; only an accepted commit may be installed",
            commit=commit_sha,
            target_ref=target_ref,
        )

    # Green: the shared ledger holds a passing full-gate entry for this SHA.
    evidence = full_gate_evidence(root, commit_sha)
    if evidence is None:
        return _refuse(
            "no_full_gate",
            f"no passing full gate is recorded for {commit_sha[:12]}",
            commit=commit_sha,
            target_ref=target_ref,
        )

    # Checked out and clean: a reinstall may only build committed code.
    code, head = _git(root, "rev-parse", "HEAD")
    if code != 0 or head != commit_sha:
        return _refuse(
            "checkout_mismatch",
            f"checkout is at {head[:12] or 'unknown'}, not the accepted {commit_sha[:12]}",
            commit=commit_sha,
            target_ref=target_ref,
        )
    code, status = _git(root, "status", "--porcelain")
    if code != 0 or status:
        return _refuse(
            "dirty_worktree",
            "the SaberOps checkout has uncommitted changes; commit or stash them first",
            commit=commit_sha,
            target_ref=target_ref,
        )

    probe = version_probe or installed_version
    old_version = probe(root)
    previous_revision = engine_revision(root)

    pause_fn = pause
    if pause_fn is None:
        from saberops.pause_work import pause_now

        pause_fn = pause_now
    resume_fn = resume
    if resume_fn is None:
        from saberops.pause_work import resume_work

        resume_fn = resume_work
    try:
        state = pause_fn(db, reason=PauseReason.SELF_UPDATE, by="self-update")
    except Exception as exc:  # noqa: BLE001 - never install under a live worker
        return _refuse(
            "pause_failed",
            f"could not park running workers: {type(exc).__name__}: {exc}",
            commit=commit_sha,
            target_ref=target_ref,
            old_version=old_version,
        )
    paused_jobs = tuple(str(job) for job in getattr(state, "paused_jobs", ()) or ())

    install_fn = install or _default_install
    install_ok, install_output = install_fn(root)
    if not install_ok:
        try:
            resume_fn(db, by="self-update-failed")
        except Exception:  # noqa: BLE001 - the failure is already reported
            pass
        return _refuse(
            "install_failed",
            "the reinstall command failed; the previous install was left in place",
            commit=commit_sha,
            target_ref=target_ref,
            old_version=old_version,
            paused_jobs=paused_jobs,
            evidence={"install_output": install_output[-2000:]},
        )

    new_version = probe(root)
    marker = write_restart_marker(root, previous=previous_revision)
    try:
        resume_fn(db, by="self-update")
    except Exception:  # noqa: BLE001 - parked work stays visible for the next resume
        pass
    checks: Any = None
    if recheck is not None:
        # Run after the gate opens so a pending smoke is allowed; a vendor that
        # still fails stays parked by its own durable verdict.
        try:
            checks = recheck()
        except Exception as exc:  # noqa: BLE001 - the update itself still happened
            checks = {"error": f"{type(exc).__name__}: {exc}"}

    return SelfUpdateResult(
        ok=True,
        code="updated",
        summary=(
            f"installed {new_version or 'the new build'} from {commit_sha[:12]}; "
            f"workers resumed through the work gate"
        ),
        commit=commit_sha,
        target_ref=target_ref,
        old_version=old_version,
        new_version=new_version,
        paused_jobs=paused_jobs,
        evidence={
            "gate": {
                "kind": evidence.get("kind"),
                "profile": evidence.get("profile"),
                "outcome": evidence.get("outcome"),
                "ts": evidence.get("ts"),
            },
            "revision": marker.get("revision"),
            "vendor_checks": checks,
        },
    )
