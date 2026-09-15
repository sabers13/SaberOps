"""Command-line interface for the multi-provider AI coding orchestrator."""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import sys
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from orchestrator_mvp.authority import (
    AUTHORITY_ORDER,
    AuthorityConfigError,
    HostAccessMode,
    label_for,
    list_presets,
    load_authority_settings,
    parse_authority_mode,
    set_authority_mode,
    set_host_access,
)
from orchestrator_mvp.authority_sync import (
    DEFAULT_AGENT_PATH,
    AgentSyncError,
    default_allowed_directories,
    default_orch_executable,
    sync_agent_file,
)
from orchestrator_mvp.config import reconstruct_run_config, resolve_run_config
from orchestrator_mvp.db import Database, get_default_db_path
from orchestrator_mvp.dispatch import READINESS_AUTHORITY_UNAVAILABLE
from orchestrator_mvp.gate_runner import (
    GateScratchPolicyError,
    load_project_gate_scratch_policy,
)
from orchestrator_mvp.git import GitManager
from orchestrator_mvp.model_access import ReadinessService
from orchestrator_mvp.models import (
    TERMINAL_RUN_STATUSES,
    AttemptStatus,
    GateScratchMode,
    ProviderReadiness,
    QuotaState,
    ReviewPolicyMode,
    RoutingState,
    RunStatus,
)
from orchestrator_mvp.observability import build_run_observability_snapshot, canonical_snapshot_json
from orchestrator_mvp.ownership import (
    OWNER_ENV_ID,
    Liveness,
    OwnershipError,
    ProcessIdentity,
    parse_owner_reservation,
)
from orchestrator_mvp.project import (
    ProjectIdentityError,
    ProjectStateConflictError,
    owner_state_dir,
)
from orchestrator_mvp.review_adaptive import (
    ReviewPolicyError,
    load_project_review_policy,
    resolve_effective_review_policy_mode,
)
from orchestrator_mvp.routing import (
    is_peak_hours,
)
from orchestrator_mvp.routing_config import (
    RoutingConfigError,
    get_user_routing_path,
    init_user_config,
    load_effective_routing,
)
from orchestrator_mvp.runtime import db_path_for_run, project_db_path_for_repo
from orchestrator_mvp.service import OrchestratorService
from orchestrator_mvp.supervisor import RunSupervisor
from orchestrator_mvp.tooling.context_cli import context_json, list_context_refs
from orchestrator_mvp.tooling.contracts import FrozenToolContract, ToolManifestError
from orchestrator_mvp.tooling.invocation import ToolInvoker
from orchestrator_mvp.tooling.registry import ToolRegistry
from orchestrator_mvp.wake import subscribe_run
from orchestrator_mvp.workers.registry import AdapterRegistry

_BIN_ATTR: dict[str, str] = {
    "opencode": "opencode_bin",
    "codex": "codex_bin",
    "antigravity": "agy_bin",
    "cline": "cline_bin",
}

_DOCTOR_PROVIDERS: tuple[str, ...] = ("opencode", "codex", "antigravity", "cline")

_CORE_MODULES: tuple[str, ...] = ("fastapi", "uvicorn", "jinja2", "httpx", "pytest")

_SPECIFIER_PATTERN = re.compile(r"^(>=|<=|==|!=|>|<|~=)\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?$")


@dataclass(frozen=True)
class DoctorFinding:
    """A single read-only doctor check outcome."""

    severity: str
    message: str


def _cli_review_policy_mode(args: object) -> ReviewPolicyMode:
    """Translate the CLI ``--review-policy`` choice into the canonical enum.

    Centralised so every ``resolve_run_config`` call site uses one
    deterministic mapping.  Unrecognized values (forward-compat) raise
    so the call site never silently downgrades to RISK_ADAPTIVE.
    """
    raw = getattr(args, "review_policy", None)
    if raw is None or not str(raw).strip():
        return ReviewPolicyMode.RISK_ADAPTIVE
    token = str(raw).strip().lower()
    if token == "risk-adaptive":
        return ReviewPolicyMode.RISK_ADAPTIVE
    if token == "always-required":
        return ReviewPolicyMode.ALWAYS_REQUIRED
    raise ValueError(
        f"unknown --review-policy value {raw!r}; expected 'risk-adaptive' or 'always-required'"
    )


def _orch_project_root() -> Path:
    """Return the orchestrator-mvp project root (contains pyproject.toml and .venv)."""
    return Path(__file__).resolve().parents[2]


def _version_clause_satisfied(
    current: tuple[int, int, int], op: str, reference: tuple[int, int, int], components: int
) -> bool:
    if op == ">=":
        return current >= reference
    if op == "<=":
        return current <= reference
    if op == ">":
        return current > reference
    if op == "<":
        return current < reference
    if op == "==":
        return current == reference
    if op == "!=":
        return current != reference
    upper = (reference[0] + 1, 0, 0) if components == 2 else (reference[0], reference[1] + 1, 0)
    return reference <= current < upper


def _requires_python_satisfied(specifier: str, current: tuple[int, int, int]) -> bool | None:
    """Evaluate a PEP 508 version specifier against the running interpreter.

    Returns None when any clause cannot be parsed (unknown syntax).
    """
    for clause in specifier.split(","):
        match = _SPECIFIER_PATTERN.match(clause.strip())
        if match is None:
            return None
        op, major_s, minor_s, patch_s = match.groups()
        reference = (
            int(major_s),
            int(minor_s) if minor_s is not None else 0,
            int(patch_s) if patch_s is not None else 0,
        )
        components = sum(part is not None for part in (minor_s, patch_s)) + 1
        if op == "~=" and components < 2:
            return None
        if not _version_clause_satisfied(current, op, reference, components):
            return False
    return True


def _check_python_requires(project_root: Path) -> DoctorFinding:
    pyproject = project_root / "pyproject.toml"
    if not pyproject.is_file():
        return DoctorFinding(
            "WARN", f"pyproject.toml not found at {pyproject}; cannot verify requires-python"
        )
    try:
        with pyproject.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        return DoctorFinding("WARN", f"could not parse {pyproject}: {exc}")
    project_section = data.get("project")
    raw = project_section.get("requires-python") if isinstance(project_section, dict) else None
    if not isinstance(raw, str) or not raw.strip():
        return DoctorFinding(
            "WARN", f"pyproject.toml at {project_root} has no requires-python constraint"
        )
    current_version = (sys.version_info[0], sys.version_info[1], sys.version_info[2])
    verdict = _requires_python_satisfied(raw.strip(), current_version)
    current_str = ".".join(str(part) for part in current_version)
    if verdict is None:
        return DoctorFinding(
            "WARN",
            f"could not evaluate requires-python '{raw}' against python {current_str}",
        )
    if verdict:
        return DoctorFinding("OK", f"python {current_str} satisfies requires-python '{raw}'")
    return DoctorFinding("FAIL", f"python {current_str} does NOT satisfy requires-python '{raw}'")


def _check_venv_tools(project_root: Path) -> DoctorFinding:
    required = ("ruff", "mypy", "pytest")
    # Candidate worktrees intentionally do not contain their own environment.
    # Prefer a colocated environment, but accept the active interpreter's
    # environment when it provides the exact developer tools.  This remains
    # read-only and makes ``orch doctor`` useful from isolated worktrees.
    # Do not resolve ``sys.executable``: venv's python is commonly a symlink
    # to the system interpreter, while its sibling development tools live in
    # the original ``.venv/bin`` directory.
    candidates = (project_root / ".venv" / "bin", Path(sys.executable).parent)
    venv_bin = next(
        (
            candidate
            for candidate in candidates
            if candidate.is_dir() and all((candidate / tool).is_file() for tool in required)
        ),
        None,
    )
    if venv_bin is not None:
        return DoctorFinding("OK", f".venv bootstrap tools present ({', '.join(required)})")
    expected = project_root / ".venv" / "bin"
    if not expected.is_dir():
        return DoctorFinding(
            "FAIL",
            f".venv not found at {project_root / '.venv'} "
            "(bootstrap with: python3 -m venv .venv && .venv/bin/pip install -e '.[dev]')",
        )
    missing = [tool for tool in required if not (expected / tool).is_file()]
    if missing:
        return DoctorFinding(
            "FAIL",
            f".venv bootstrap tools missing: {', '.join(missing)} "
            f"(expected under {expected}; repair with: .venv/bin/pip install -e '.[dev]')",
        )
    return DoctorFinding("OK", f".venv bootstrap tools present ({', '.join(required)})")


def _check_core_imports() -> DoctorFinding:
    missing = [name for name in _CORE_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        return DoctorFinding("FAIL", f"core modules unresolvable: {', '.join(missing)}")
    return DoctorFinding("OK", f"core modules resolvable: {', '.join(_CORE_MODULES)}")


def _check_git_binary() -> DoctorFinding:
    git_path = shutil.which("git")
    if git_path is None:
        return DoctorFinding("FAIL", "git binary not found on PATH")
    return DoctorFinding("OK", f"git binary available: {git_path}")


def _quota_rows_readonly(resolved_db_path: Path) -> list[QuotaState]:
    """Read quota_state rows through a strictly read-only SQLite connection."""
    conn = sqlite3.connect(f"{resolved_db_path.as_uri()}?mode=ro", uri=True, timeout=5.0)
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM quota_state ORDER BY provider ASC, pool ASC").fetchall()
    finally:
        conn.close()
    return [
        QuotaState(
            provider=str(row["provider"]),
            pool=str(row["pool"]),
            remaining_percent=float(row["remaining_percent"]),
            reset_at=str(row["reset_at"]) if row["reset_at"] else None,
            routing_state=RoutingState(str(row["routing_state"])),
            updated_at=str(row["updated_at"]),
        )
        for row in rows
    ]


def _check_database(db_path: Path) -> tuple[DoctorFinding, list[QuotaState]]:
    resolved = db_path.resolve()
    if not resolved.exists():
        return (
            DoctorFinding(
                "WARN",
                f"database not initialized yet (expected at {resolved}); "
                "it will be created on first orchestrator write command",
            ),
            [],
        )
    try:
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0)
        try:
            row = conn.execute("SELECT COUNT(*) FROM runs").fetchone()
            count = int(row[0]) if row is not None else 0
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return (
            DoctorFinding("FAIL", f"database unreadable/corrupt at {resolved}: {exc}"),
            [],
        )
    quota_rows: list[QuotaState]
    try:
        quota_rows = _quota_rows_readonly(resolved)
    except sqlite3.Error as exc:
        return (
            DoctorFinding("FAIL", f"database quota table unreadable at {resolved}: {exc}"),
            [],
        )
    return (
        DoctorFinding("OK", f"database readable (read-only) at {resolved} ({count} runs)"),
        quota_rows,
    )


def _provider_status_line(
    registry: AdapterRegistry, provider: str, quota_rows: list[QuotaState]
) -> str:
    adapter = registry.get(provider)
    if adapter is None:
        return "NOT INSTALLED"
    bin_attr = _BIN_ATTR.get(provider)
    configured = getattr(adapter, bin_attr, None) if bin_attr else None
    resolved = shutil.which(configured) if configured else None
    if resolved is None:
        return "NOT INSTALLED"
    for row in quota_rows:
        if row.provider == provider and row.routing_state == RoutingState.BLOCKED:
            return "BLOCKED BY QUOTA"
    # C15-RDY-02: append the durable readiness projection.  The leading
    # "AVAILABLE (<path>)" wording is preserved for existing operator
    # tooling; the suffix distinguishes READY / NOT READY /
    # READINESS UNKNOWN.  Strictly read-only: never mutates state.
    # C15-RDY-02A: a lookup failure is itself UNKNOWN -- never plain
    # AVAILABLE -- with the fixed sanitized reason.  No exception text
    # (paths, secrets) is rendered.
    try:
        readiness = ReadinessService.for_owner_state(
            owner_state_dir()
        ).admission_for(provider=provider)
    except Exception:
        return (
            f"AVAILABLE ({resolved}) / READINESS UNKNOWN "
            f"({READINESS_AUTHORITY_UNAVAILABLE})"
        )
    if readiness.state is ProviderReadiness.READY:
        return f"AVAILABLE ({resolved}) / READY"
    if readiness.state is ProviderReadiness.NOT_READY:
        return f"AVAILABLE ({resolved}) / NOT READY ({readiness.reason})"
    return f"AVAILABLE ({resolved}) / READINESS UNKNOWN ({readiness.reason})"


def _check_providers(quota_rows: list[QuotaState]) -> list[DoctorFinding]:
    registry = AdapterRegistry.default()
    findings: list[DoctorFinding] = []
    any_available = False
    for provider in _DOCTOR_PROVIDERS:
        status = _provider_status_line(registry, provider, quota_rows)
        if status.startswith("AVAILABLE"):
            any_available = True
            findings.append(DoctorFinding("OK", f"provider '{provider}': {status}"))
        else:
            findings.append(DoctorFinding("WARN", f"provider '{provider}': {status}"))
    if not any_available:
        findings.append(
            DoctorFinding(
                "FAIL",
                "no worker provider is AVAILABLE "
                f"(install at least one of: {', '.join(_DOCTOR_PROVIDERS)})",
            )
        )
    return findings


def _check_target_repo(repo_path: Path) -> DoctorFinding:
    repo = repo_path.resolve()
    if not GitManager.is_git_repo(repo):
        return DoctorFinding("FAIL", f"target repo '{repo}' is not a valid git repository")
    if not GitManager.is_clean(repo):
        return DoctorFinding("WARN", f"target repo '{repo}' has uncommitted changes")
    return DoctorFinding("OK", f"target repo '{repo}' is a clean git repository")


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="orch",
        description=(
            "Minimal reliable multi-provider AI coding orchestrator with Git worktree isolation "
            "and deterministic gate verification."
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # orch tools: deterministic logical tool inspection and invocation.
    tools_parser = subparsers.add_parser(
        "tools", help="Inspect or invoke registered logical tools."
    )
    tools_parser.add_argument("--registry", type=str, default=None, help="Tool registry JSON path.")
    tools_parser.add_argument("--db", type=str, default=None)
    tools_parser.add_argument("--run-id", type=str, default=None)
    tools_subparsers = tools_parser.add_subparsers(dest="tools_command", required=True)
    tools_list = tools_subparsers.add_parser("list", help="List compact logical tool refs.")
    tools_list.add_argument("--db", type=str, default=argparse.SUPPRESS)
    tools_list.add_argument("--run-id", type=str, default=argparse.SUPPRESS)
    tools_list.add_argument("--json", action="store_true", help="Emit JSON.")
    tools_describe = tools_subparsers.add_parser("describe", help="Describe one logical tool.")
    tools_describe.add_argument("logical_ref", type=str)
    tools_describe.add_argument("--db", type=str, default=argparse.SUPPRESS)
    tools_describe.add_argument("--run-id", type=str, default=argparse.SUPPRESS)
    tools_describe.add_argument("--json", action="store_true", help="Emit JSON.")
    tools_exec = tools_subparsers.add_parser("exec", help="Invoke one logical tool.")
    tools_exec.add_argument("logical_ref", type=str)
    tools_exec.add_argument("--db", type=str, default=argparse.SUPPRESS)
    tools_exec.add_argument("--run-id", type=str, default=argparse.SUPPRESS)
    tools_exec.add_argument("--attempt-id", type=str, default=None)
    tools_exec.add_argument("--operation-id", type=str, default=None)
    tools_exec.add_argument("tool_args", nargs=argparse.REMAINDER)

    # orch scope: deterministic project-scope inspection and expansion (C09).
    scope_parser = subparsers.add_parser(
        "scope", help="Inspect or expand deterministic project scope."
    )
    scope_parser.add_argument("--db", type=str, default=None)
    scope_parser.add_argument("--run-id", type=str, default=None)
    scope_subparsers = scope_parser.add_subparsers(dest="scope_command", required=True)
    scope_graph = scope_subparsers.add_parser(
        "graph", help="Build and inspect a project graph (read-only)."
    )
    scope_graph.add_argument("--repo", type=str, default=None)
    scope_graph.add_argument("--json", action="store_true", help="Emit JSON.")
    scope_show = scope_subparsers.add_parser(
        "show", help="Resolve and render a bounded scope for a query."
    )
    scope_show.add_argument("query", type=str)
    scope_show.add_argument("--repo", type=str, default=None)
    scope_show.add_argument("--json", action="store_true", help="Emit JSON.")
    scope_expand = scope_subparsers.add_parser(
        "expand", help="Expand the run scope with an explicit reason."
    )
    scope_expand.add_argument("--path", type=str, default=None)
    scope_expand.add_argument("--symbol", type=str, default=None)
    scope_expand.add_argument("--reason", type=str, required=True)
    scope_expand.add_argument("--db", type=str, default=None)
    scope_expand.add_argument("--run-id", type=str, default=None)
    scope_expand.add_argument("--json", action="store_true", help="Emit JSON.")
    scope_affected = scope_subparsers.add_parser(
        "affected", help="Shadow affected-verification planning."
    )
    scope_affected.add_argument("--repo", type=str, default=None)
    scope_affected.add_argument("--base", type=str, default=None, help="Git base revision.")
    scope_affected.add_argument("--paths", nargs="*", default=None, help="Explicit paths.")
    scope_affected.add_argument("--json", action="store_true", help="Emit JSON.")

    context_parser = subparsers.add_parser(
        "orch-context", help="Read one bounded durable run-evidence reference."
    )
    context_parser.add_argument("run_id", type=str)
    context_parser.add_argument("ref", nargs="?", default=None)
    context_parser.add_argument("--db", type=str, default=None)
    context_parser.add_argument("--json", action="store_true", help="Emit JSON (default).")

    # orch run
    run_parser = subparsers.add_parser(
        "run",
        help="Execute a task end-to-end on a target repository.",
    )
    run_parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="Path to target Git repository (default: current directory).",
    )
    run_parser.add_argument(
        "--task",
        type=str,
        required=False,
        default=None,
        help="Task description for the worker.",
    )
    run_parser.add_argument(
        "--run-id",
        type=str,
        default=None,
        help="Adopt an existing PENDING run by id instead of creating a new one.",
    )
    run_parser.add_argument(
        "--gate",
        type=str,
        default="make gate",
        help="Deterministic gate command to verify changes (default: 'make gate').",
    )
    run_parser.add_argument(
        "--tier",
        type=str,
        choices=["auto", "T1", "T2", "T3"],
        default="auto",
        help="Routing mode: auto (default) or a manual tier override.",
    )
    run_parser.add_argument(
        "--max-tier",
        choices=["T1", "T2", "T3"],
        default="T3",
        help="Maximum automatic routing tier (default: T3).",
    )
    run_parser.add_argument(
        "--provider",
        type=str,
        default=None,
        help="Explicit worker provider override (e.g. 'codex', 'opencode', 'antigravity').",
    )
    run_parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Explicit model override (e.g. 'gpt-5.6-terra', 'opencode-go/deepseek-v4-flash').",
    )
    run_parser.add_argument(
        "--reserve",
        dest="reserve_override",
        action="store_true",
        default=False,
        help="Explicit owner emergency override for RESERVE quota pools only.",
    )
    run_parser.add_argument(
        "--require-capability",
        dest="required_capabilities",
        action="append",
        default=[],
        metavar="CAPABILITY",
        help="Require a factual provider capability; repeat for multiple requirements.",
    )
    run_parser.add_argument(
        "--tool",
        dest="tool_refs",
        action="append",
        default=[],
        metavar="LOGICAL_REF",
        help="Expose a registered logical tool; repeat for multiple tools.",
    )
    training_group = run_parser.add_mutually_exclusive_group()
    training_group.add_argument(
        "--training-allowed",
        action="store_true",
        default=True,
        help="Permit models that may use task data for training (e.g. Muse); the default.",
    )
    training_group.add_argument(
        "--training-denied",
        action="store_false",
        dest="training_allowed",
        help="Deny training-eligible models (explicit opt-out).",
    )
    run_parser.add_argument(
        "--max-attempts",
        type=int,
        default=2,
        help="Maximum worker retry attempts (default: 2).",
    )
    run_parser.add_argument(
        "--review",
        action="store_true",
        default=False,
        help="Run independent code review on successful candidate.",
    )
    run_parser.add_argument(
        "--review-policy",
        choices=["risk-adaptive", "always-required"],
        default="risk-adaptive",
        dest="review_policy",
        help=(
            "C13-F risk-adaptive review policy: 'risk-adaptive' (default; review "
            "intensity follows the classifier), or 'always-required' (project "
            "tightening override -- always launch an independent reviewer)."
        ),
    )
    run_parser.add_argument(
        "--review-mode",
        choices=["bounded", "max"],
        default="bounded",
        help=("Review mode: 'bounded' (fixed limit) or 'max' (autonomous loop until convergence)."),
    )
    run_parser.add_argument(
        "--review-limit",
        type=int,
        default=3,
        help="Maximum review rounds when --review-mode bounded (default: 3).",
    )
    run_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help=(
            "Path to SQLite database (default: "
            "$XDG_STATE_HOME/orchestrator-mvp/orchestrator.db or "
            "~/.local/state/orchestrator-mvp/orchestrator.db)."
        ),
    )
    run_parser.add_argument(
        "--worker-timeout",
        type=float,
        default=None,
        help="Worker timeout (default: tier-aware T1=600, T2=1200, T3=1800 seconds).",
    )
    run_parser.add_argument(
        "--gate-timeout",
        type=float,
        default=300.0,
        help="Gate timeout in seconds (default: 300).",
    )
    run_parser.add_argument(
        "--review-timeout",
        type=float,
        default=1200.0,
        help="Review timeout in seconds (default: 1200).",
    )
    run_parser.add_argument(
        "--detach",
        action="store_true",
        default=False,
        help=(
            "Start the detached execution owner and return immediately instead of "
            "following the run. The run continues either way if this shell exits."
        ),
    )
    publish_group = run_parser.add_mutually_exclusive_group()
    publish_group.add_argument(
        "--publish-candidate",
        dest="publish_candidate",
        action="store_true",
        default=None,
        help=(
            "Explicitly enable candidate publication for this run: every "
            "gate-passed candidate is pushed (never force) to the candidate "
            "branch on origin for external review. Overrides authority mode."
        ),
    )
    publish_group.add_argument(
        "--no-publish-candidate",
        dest="publish_candidate",
        action="store_false",
        help=(
            "Explicitly disable candidate publication for this run, even in "
            "high-autonomy authority modes. Overrides authority mode."
        ),
    )

    # orch review <run-id>
    review_parser = subparsers.add_parser(
        "review",
        help="Run independent read-only code review on a completed run.",
    )
    review_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    review_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )
    review_parser.add_argument(
        "--timeout",
        type=float,
        default=300.0,
        help="Reviewer timeout in seconds (default: 300).",
    )

    # orch accept <run-id>
    accept_parser = subparsers.add_parser(
        "accept",
        help="Safely fast-forward integrate a verified candidate run into the source repository.",
    )
    accept_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    accept_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )
    accept_parser.add_argument(
        "--gate-timeout",
        type=float,
        default=300.0,
        help="Post-integration gate timeout in seconds (default: 300).",
    )

    # orch cleanup <run-id>
    cleanup_parser = subparsers.add_parser(
        "cleanup",
        help="Safely remove clean attempt worktrees for a completed or failed run.",
    )
    cleanup_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    cleanup_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    # orch status <run-id>
    status_parser = subparsers.add_parser(
        "status",
        help="Show details, checks, and reviews for a specific run.",
    )
    status_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    status_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    plan_parser = subparsers.add_parser("plan", help="Show persisted execution plan as JSON.")
    plan_parser.add_argument("run_id", type=str)
    plan_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    handoff_parser = subparsers.add_parser(
        "handoff", help="Show latest immutable handoff or resume capsule."
    )
    handoff_parser.add_argument("run_id", type=str)
    handoff_parser.add_argument(
        "--capsule", action="store_true", help="Show compact resume capsule."
    )
    handoff_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    wait_parser = subparsers.add_parser(
        "wait", aliases=["watch"], help="Block until a persisted meaningful state change."
    )
    wait_parser.add_argument("run_id", type=str)
    wait_parser.add_argument("--after", type=int, default=0, help="Last event id already observed.")
    wait_parser.add_argument("--timeout", type=float, default=300.0, help="Maximum wait seconds.")
    wait_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    reconcile_parser = subparsers.add_parser(
        "reconcile", help="Reconcile persisted run state with supervisor evidence."
    )
    reconcile_parser.add_argument("run_id", type=str)
    reconcile_parser.add_argument(
        "--dry-run", action="store_true", help="Report orphan action without changing state."
    )
    reconcile_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")

    # orch recover <run-id>: dedicated C05 cold-takeover child invocation.
    # Reached only from the supervisor's fenced-recovery launch path (see
    # ``RunSupervisor.ensure_recovery_running``); ``--db`` is the only knob
    # a human could conceivably pass, and even that is primarily for tests.
    recover_parser = subparsers.add_parser(
        "recover",
        help=(
            "Dedicated C05 cold-takeover execution owner.  Internal: invoked "
            "by the supervisor with a fenced execution-owner reservation.  "
            "Requires ORCH_EXECUTION_OWNER_GENERATION to match the durable "
            "record; otherwise fails closed before any mutation."
        ),
    )
    recover_parser.add_argument("run_id", type=str)
    recover_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")

    cancel_parser = subparsers.add_parser(
        "cancel", help="Explicitly cancel a run and terminate its execution owner."
    )
    cancel_parser.add_argument("run_id", type=str)
    cancel_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")

    # orch events <run-id>
    events_parser = subparsers.add_parser(
        "events",
        help="Show diagnostic event log for a specific run in chronological order.",
    )
    events_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    events_parser.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum events to display (default: 200).",
    )
    events_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    # orch observability <run-id>
    observability_parser = subparsers.add_parser(
        "observability",
        help="Show deterministic, read-only reconstructability evidence for a run.",
    )
    observability_parser.add_argument("run_id", type=str)
    observability_parser.add_argument(
        "--json", action="store_true", help="Emit canonical JSON (the default)."
    )
    observability_parser.add_argument(
        "--db", type=str, default=None, help="Path to SQLite database."
    )

    # orch list
    list_parser = subparsers.add_parser("list", help="List recent orchestrator runs.")
    list_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Maximum runs to display (default: 20).",
    )
    list_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )
    list_parser.add_argument(
        "--repo",
        type=str,
        default=None,
        help="Project whose runs to list (default: current directory).",
    )

    # orch models
    models_parser = subparsers.add_parser(
        "models",
        help="List configured tiers, routing chains, and provider availability.",
    )
    models_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    # orch doctor
    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Strictly read-only preflight diagnostics for local orchestration readiness.",
    )
    doctor_parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="Path to target Git repository to validate (default: current directory).",
    )
    doctor_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help=(
            "Path to SQLite database (default: "
            "$XDG_STATE_HOME/orchestrator-mvp/orchestrator.db or "
            "~/.local/state/orchestrator-mvp/orchestrator.db)."
        ),
    )

    # orch ui
    ui_parser = subparsers.add_parser(
        "ui",
        help="Start the local web dashboard.",
    )
    ui_parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Bind host for the dashboard (default: 127.0.0.1, localhost-only).",
    )
    ui_parser.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Port to listen on (default: 8765).",
    )
    ui_parser.add_argument(
        "--repo",
        type=str,
        default=".",
        help="Target repository the dashboard dispatches runs against (default: '.').",
    )
    ui_parser.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    # orch quota set|list|enable
    quota_parser = subparsers.add_parser(
        "quota",
        help="Manage owner-supplied quota state for quota-aware routing.",
    )
    quota_sub = quota_parser.add_subparsers(dest="quota_command", required=True)

    quota_set = quota_sub.add_parser("set", help="Set quota state for a provider pool.")
    quota_set.add_argument("provider", type=str, help="Provider name (e.g. 'codex').")
    quota_set.add_argument("pool", type=str, help="Pool name (e.g. 'weekly').")
    quota_set.add_argument(
        "--remaining",
        type=float,
        required=True,
        help="Remaining quota percentage (0-100).",
    )
    quota_set.add_argument(
        "--reset-at",
        type=str,
        default=None,
        help="Optional quota reset timestamp.",
    )
    quota_set.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    quota_list = quota_sub.add_parser("list", help="List quota state.")
    quota_list.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    quota_enable = quota_sub.add_parser(
        "enable",
        help="Explicitly re-enable automatic routing for a provider pool.",
    )
    quota_enable.add_argument("provider", type=str, help="Provider name (e.g. 'codex').")
    quota_enable.add_argument("pool", type=str, help="Pool name (e.g. 'weekly').")
    quota_enable.add_argument(
        "--db",
        type=str,
        default=None,
        help="Path to SQLite database.",
    )

    # orch routing show|validate|init
    routing_parser = subparsers.add_parser(
        "routing",
        help="Manage routing configuration (show/validate/init).",
    )
    routing_sub = routing_parser.add_subparsers(dest="routing_command", required=True)
    routing_show = routing_sub.add_parser("show", help="Show effective routing.")
    routing_show.add_argument("--json", action="store_true", help="JSON output")
    routing_sub.add_parser("validate", help="Validate routing config.")
    routing_init = routing_sub.add_parser("init", help="Initialize user routing config.")
    routing_init.add_argument("--force", action="store_true", help="Overwrite existing file.")

    # orch authority show|list|set|sync-opencode
    authority_parser = subparsers.add_parser(
        "authority",
        help="Manage the owner authority preset (show/list/set/sync-opencode).",
    )
    authority_sub = authority_parser.add_subparsers(dest="authority_command", required=True)

    authority_show = authority_sub.add_parser(
        "show", help="Show the effective authority mode, label, and policy."
    )
    authority_show.add_argument("--json", action="store_true", help="JSON output")

    authority_list = authority_sub.add_parser(
        "list", help="List all authority presets in stable product order."
    )
    authority_list.add_argument("--json", action="store_true", help="JSON output")

    authority_set = authority_sub.add_parser("set", help="Persist the owner authority mode.")
    authority_set.add_argument(
        "mode",
        type=str,
        help="Exact authority mode id: " + ", ".join(m.value for m in AUTHORITY_ORDER) + ".",
    )
    authority_set.add_argument("--json", action="store_true", help="JSON output")

    authority_host = authority_sub.add_parser(
        "set-host-access", help="Persist governed or unrestricted technical host access."
    )
    authority_host.add_argument("mode", choices=[mode.value for mode in HostAccessMode], type=str)
    authority_host.add_argument("--json", action="store_true", help="JSON output")

    authority_sync = authority_sub.add_parser(
        "sync-opencode",
        help="Synchronize the managed authority regions of the OpenCode ORX agent.",
    )
    authority_sync.add_argument(
        "--agent",
        type=str,
        default=DEFAULT_AGENT_PATH,
        help=f"Path to the OpenCode agent file (default: {DEFAULT_AGENT_PATH}).",
    )
    authority_sync.add_argument(
        "--orch-executable",
        type=str,
        default=None,
        help="Exact Orch executable ORX may run (default: alongside this interpreter).",
    )
    authority_sync.add_argument(
        "--allow-directory",
        action="append",
        default=None,
        metavar="GLOB",
        help="External-directory allow glob; repeatable. Replaces the derived default.",
    )
    authority_sync.add_argument("--json", action="store_true", help="JSON output")

    # orch project gate-scratch show|set
    #
    # ``--repo`` is registered ONLY on the leaf subparsers
    # (``orch project gate-scratch show --repo X`` /
    # ``orch project gate-scratch set --repo X MODE``).  Registering
    # the same ``dest`` on multiple ancestor parsers shadows the
    # ancestor value at parse time and the dispatcher loses the
    # earlier parse, so the supported syntax is the leaf-position
    # form.  The accepted CLI syntax is documented accordingly.
    project_parser = subparsers.add_parser(
        "project",
        help="Project-scoped settings (currently: gate-scratch).",
    )
    project_sub = project_parser.add_subparsers(dest="project_command", required=True)
    project_gate_scratch = project_sub.add_parser(
        "gate-scratch",
        help="Project gate-scratch mode (DISK / RAM_PREFERRED / RAM_REQUIRED).",
    )
    project_gate_scratch_sub = project_gate_scratch.add_subparsers(
        dest="project_gate_scratch_command", required=True
    )
    project_gate_scratch_show = project_gate_scratch_sub.add_parser(
        "show",
        help="Show the project's persisted gate-scratch mode.",
    )
    project_gate_scratch_show.add_argument(
        "--repo",
        dest="project_repo",
        type=str,
        default=None,
        help="Absolute path to the target Git repository.",
    )
    project_gate_scratch_show.add_argument("--json", action="store_true", help="JSON output")
    project_gate_scratch_set = project_gate_scratch_sub.add_parser(
        "set",
        help="Persist the project's gate-scratch mode.",
    )
    project_gate_scratch_set.add_argument(
        "--repo",
        dest="project_repo",
        type=str,
        default=None,
        help="Absolute path to the target Git repository.",
    )
    project_gate_scratch_set.add_argument(
        "mode",
        type=str,
        choices=[mode.value for mode in GateScratchMode],
        help="Exact mode id: " + ", ".join(mode.value for mode in GateScratchMode) + ".",
    )

    return parser


def _explicit_db(args: argparse.Namespace) -> str | None:
    """Return an explicitly requested database path, if the caller gave one."""
    raw = getattr(args, "db", None)
    return str(raw) if raw else None


def db_for_repo(args: argparse.Namespace, repo_path: Path | str) -> Database:
    """Open the runtime state store for a repository.

    An explicit ``--db`` always wins: it is a request for one specific store.
    Otherwise runtime state is project-local, so two repositories never share
    runs, transcripts, or supervision records.
    """
    explicit = _explicit_db(args)
    if explicit is not None:
        return Database(explicit)
    return Database(project_db_path_for_repo(repo_path), owner_db=Database(get_default_db_path()))


def db_for_run(args: argparse.Namespace, run_id: str) -> Database:
    """Open the runtime state store that owns an existing run.

    The store is derived from the run ID's own project segment, never from a
    current working directory or a selected project.  Only genuinely legacy
    run IDs (no project segment) resolve to the preserved global store; a
    project-scoped ID whose project state is missing raises
    :class:`ProjectStateUnavailableError` rather than being retried there.
    """
    explicit = _explicit_db(args)
    if explicit is not None:
        return Database(explicit)
    resolved = db_path_for_run(run_id)
    if resolved is None:
        return Database(get_default_db_path())
    return Database(resolved, owner_db=Database(get_default_db_path()))


def handle_run(args: argparse.Namespace) -> int:
    """Handle `orch run` command."""
    # Adoption path: --run-id adopts an EXISTING PENDING run atomically.
    run_id_arg = getattr(args, "run_id", None)
    if run_id_arg is not None:
        run_id_clean = str(run_id_arg).strip()
        if run_id_clean:
            db = db_for_run(args, run_id_clean)
            service = OrchestratorService(db=db)
            existing = db.get_run(run_id_clean)
            if existing is None:
                print(
                    f"[!] Cannot adopt run '{run_id_clean}': run not found",
                    file=sys.stderr,
                )
                return 1
            if existing.status != RunStatus.PENDING:
                print(
                    f"[!] Cannot adopt run '{run_id_clean}': "
                    f"status is {existing.status.value}, expected PENDING",
                    file=sys.stderr,
                )
                return 1
            # Use the persisted config for the PENDING run when available
            try:
                if existing.config_json:
                    cfg_data = json.loads(existing.config_json)
                    config = reconstruct_run_config(cfg_data)
                else:
                    if not args.task or not str(args.task).strip():
                        print("[!] Task is required", file=sys.stderr)
                        return 1
                    try:
                        resolved_mode = load_project_gate_scratch_policy(args.repo)
                    except GateScratchPolicyError as exc:
                        print(
                            f"[!] Project gate-scratch policy is invalid: {exc}",
                            file=sys.stderr,
                        )
                        return 1
                    try:
                        project_review_mode = load_project_review_policy(args.repo)
                    except ReviewPolicyError as exc:
                        print(
                            f"[!] Project review policy is invalid: {exc}",
                            file=sys.stderr,
                        )
                        return 1
                    effective_review_mode = resolve_effective_review_policy_mode(
                        project_mode=project_review_mode,
                        explicit_run_mode=_cli_review_policy_mode(args),
                    )
                    config = resolve_run_config(
                        task=args.task,
                        repo_path=args.repo,
                        routing_mode="auto" if args.tier == "auto" else "manual",
                        manual_tier=None if args.tier == "auto" else args.tier,
                        max_auto_tier=args.max_tier,
                        training_allowed=args.training_allowed,
                        provider_override=args.provider,
                        model_override=args.model,
                        reserve_override=args.reserve_override,
                        required_capabilities=tuple(args.required_capabilities),
                        gate_command=args.gate,
                        max_attempts=args.max_attempts,
                        worker_timeout=args.worker_timeout,
                        gate_timeout=args.gate_timeout,
                        review=args.review,
                        review_timeout=args.review_timeout,
                        review_mode=args.review_mode,
                        review_limit=args.review_limit,
                        review_policy_mode=effective_review_mode,
                        publish_candidate=getattr(args, "publish_candidate", None),
                        tool_refs=tuple(args.tool_refs),
                        gate_scratch_mode=resolved_mode,
                    )
            except Exception as exc:
                print(f"[!] Error during run execution: {exc}", file=sys.stderr)
                return 1
            print(f"[*] Adopting PENDING run : {run_id_clean}")
            print(f"[*] Target repository : {config.target_repo}")
            print(f"[*] Task              : {config.task}")
            print(f"[*] Gate command      : {config.gate_command}")
            print(f"[*] Routing           : {config.routing_mode} ({config.initial_tier.value})")
            print(f"[*] Training allowed  : {config.training_allowed}")
            print(
                f"[*] Candidate publish : {'enabled' if config.publish_candidate else 'disabled'}"
            )
            if config.provider_override:
                print(f"[*] Provider override : {config.provider_override}")
            if config.model_override:
                print(f"[*] Model override    : {config.model_override}")
            print("─" * 60)
            # This process IS the durable execution owner from here on.  The
            # claim is atomic and rejects a second live owner before any worker
            # is launched; the fencing token adopts a reservation made for us.
            reservation = parse_owner_reservation()
            if reservation is None and (os.environ.get(OWNER_ENV_ID) or "").strip():
                # A reservation id without a usable generation is a half token.
                # It adopts nothing; fencing is never weakened silently.
                db.record_event(
                    run_id_clean,
                    "execution_owner_token_rejected",
                    payload={"reason": "missing_or_malformed_generation"},
                )
            try:
                owner = db.claim_execution_owner(
                    run_id_clean,
                    identity=ProcessIdentity.for_self(),
                    expect_owner_id=reservation.owner_id if reservation else None,
                    expect_generation=reservation.generation if reservation else None,
                )
            except OwnershipError as exc:
                print(f"[!] Cannot execute run '{run_id_clean}': {exc}", file=sys.stderr)
                return 1
            try:
                result = service.run_sync(
                    task=config.task,
                    repo_path=config.target_repo,
                    config=config,
                    run_id=run_id_clean,
                )
            except Exception as exc:
                db.release_execution_owner(
                    run_id_clean,
                    owner_id=owner.owner_id,
                    generation=owner.generation,
                    reason="execution_error",
                )
                print(f"[!] Error during run execution: {exc}", file=sys.stderr)
                return 1
            db.release_execution_owner(
                run_id_clean,
                owner_id=owner.owner_id,
                generation=owner.generation,
                reason="execution_finished",
            )
            print("─" * 60)
            print(f"Run ID      : {result.run_id}")
            print(f"Status      : {result.status.value}")
            print(f"Gate Passed : {'YES' if result.passed else 'NO'}")
            print(f"Summary     : {result.summary}")
            if result.branch_name:
                print(f"Branch      : {result.branch_name}")
            if result.commit_sha:
                print(f"Commit SHA  : {result.commit_sha}")
            if result.worktree_path:
                print(f"Worktree    : {result.worktree_path}")
            if result.candidate_published:
                print(
                    f"Candidate   : PUBLISHED to '{result.candidate_remote}' as "
                    f"{result.candidate_remote_branch}"
                )
                print(f"Verified    : remote SHA {result.candidate_verified_remote_sha}")
            elif config.publish_candidate:
                print("Candidate   : not published")
            if result.reviews:
                for rev in result.reviews:
                    rev_summary = (
                        f"Review      : [{rev.provider}/{rev.model}] -> {rev.verdict.value}"  # noqa: E501
                    )
                    print(f"{rev_summary} ({rev.summary})")
            return 0 if result.passed else 1
    if not args.task or not str(args.task).strip():
        print("[!] Task is required", file=sys.stderr)
        return 1
    try:
        service = OrchestratorService(db=db_for_repo(args, args.repo))
    except ProjectIdentityError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    try:
        resolved_gate_scratch_mode = load_project_gate_scratch_policy(args.repo)
    except GateScratchPolicyError as exc:
        print(f"[!] Project gate-scratch policy is invalid: {exc}", file=sys.stderr)
        return 1
    try:
        project_review_mode = load_project_review_policy(args.repo)
    except ReviewPolicyError as exc:
        print(f"[!] Project review policy is invalid: {exc}", file=sys.stderr)
        return 1
    effective_review_mode = resolve_effective_review_policy_mode(
        project_mode=project_review_mode,
        explicit_run_mode=_cli_review_policy_mode(args),
    )
    config = resolve_run_config(
        task=args.task,
        repo_path=args.repo,
        routing_mode="auto" if args.tier == "auto" else "manual",
        manual_tier=None if args.tier == "auto" else args.tier,
        max_auto_tier=args.max_tier,
        training_allowed=args.training_allowed,
        provider_override=args.provider,
        model_override=args.model,
        reserve_override=args.reserve_override,
        required_capabilities=tuple(args.required_capabilities),
        gate_command=args.gate,
        max_attempts=args.max_attempts,
        worker_timeout=args.worker_timeout,
        gate_timeout=args.gate_timeout,
        review=args.review,
        review_timeout=args.review_timeout,
        review_mode=args.review_mode,
        review_limit=args.review_limit,
        review_policy_mode=effective_review_mode,
        publish_candidate=getattr(args, "publish_candidate", None),
        tool_refs=tuple(args.tool_refs),
        gate_scratch_mode=resolved_gate_scratch_mode,
    )
    print(f"[*] Target repository : {config.target_repo}")
    print(f"[*] Task              : {config.task}")
    print(f"[*] Gate command      : {config.gate_command}")
    print(f"[*] Routing           : {config.routing_mode} ({config.initial_tier.value})")
    print(f"[*] Training allowed  : {config.training_allowed}")
    print(f"[*] Candidate publish : {'enabled' if config.publish_candidate else 'disabled'}")
    if args.provider:
        print(f"[*] Provider override : {args.provider}")
    if args.model:
        print(f"[*] Model override    : {args.model}")
    print("─" * 60)

    # Execution is delegated to a detached per-run owner in its own session, so
    # losing this shell (hangup, Ctrl-C, terminal close) never stops the run.
    supervisor = RunSupervisor(db=service.db)
    try:
        started = supervisor.start_supervised_run(config)
    except Exception as exc:
        print(f"[!] Error during run execution: {exc}", file=sys.stderr)
        return 1
    run_id = started.run_id
    spawned_owner = service.db.get_execution_owner(run_id)
    print(f"[*] Run ID            : {run_id}")
    if spawned_owner is None or not spawned_owner.is_active:
        launched = service.db.get_run(run_id)
        summary = launched.result_summary if launched is not None else "owner was not claimed"
        print(f"[!] Execution owner was not started: {summary}", file=sys.stderr)
        return 1
    print(f"[*] Execution owner   : pid {spawned_owner.identity.pid} (detached session)")
    if args.detach:
        print("[*] Detached. The run continues independently of this shell.")
        print(f"    orch wait {run_id} | orch status {run_id} | orch cancel {run_id}")
        return 0
    return _follow_run(service.db, run_id)


# Lifecycle edges worth echoing while following a detached run.
_FOLLOW_EVENT_TYPES: frozenset[str] = frozenset(
    {
        "run_started",
        "worker_started",
        "worker_completed",
        "worker_failed",
        "gate_passed",
        "gate_failed",
        "gate_completed",
        "review_completed",
        "plan_step_completed",
        "plan_step_blocked",
        "plan_step_failed",
        "orphan_detected",
        "run_cancelled",
        "run_completed",
        "run_failed",
    }
)

# Coarse safety fallback only.  The wake FIFO, not this timeout, is the normal
# mechanism by which a follower learns that something happened.
FOLLOW_FALLBACK_SECONDS: float = 30.0


def _follow_run(db: Database, run_id: str) -> int:
    """Print lifecycle edges until the run is terminal; never polls a model.

    The follower blocks on the run's cross-process wake channel and re-reads the
    durable event log when woken.  Interrupting it detaches the follower only:
    the execution owner keeps running, because caller loss is not cancellation.
    """
    after = 0
    supervisor = RunSupervisor(db=db)
    try:
        with subscribe_run(db.db_path, run_id) as wake:
            while True:
                for event in db.get_events(run_id, after_id=after, limit=200):
                    after = event.id
                    if event.event_type in _FOLLOW_EVENT_TYPES:
                        print(f"[{event.event_type}]")
                run = db.get_run(run_id)
                if run is not None and run.status in TERMINAL_RUN_STATUSES:
                    break
                if not wake.wait(FOLLOW_FALLBACK_SECONDS):
                    # Fallback tick: only used to notice an owner that died
                    # without being able to record its own terminal event.
                    if supervisor.execution_liveness(run_id) is Liveness.DEAD:
                        print(
                            f"[!] Execution owner for {run_id} is no longer alive; "
                            f"run 'orch reconcile {run_id}' to recover.",
                            file=sys.stderr,
                        )
                        return 1
    except KeyboardInterrupt:
        print(
            f"\n[*] Detached from {run_id}; the execution owner keeps running.\n"
            f"    orch wait {run_id} | orch status {run_id} | orch cancel {run_id}"
        )
        return 0

    final = db.get_run(run_id)
    print("─" * 60)
    print(f"Run ID      : {run_id}")
    if final is None:
        return 1
    print(f"Status      : {final.status.value}")
    print(f"Gate Passed : {'YES' if final.status == RunStatus.COMPLETED else 'NO'}")
    print(f"Summary     : {final.result_summary or ''}")
    attempts = db.get_attempts_for_run(run_id)
    if attempts:
        last = attempts[-1]
        print(f"Branch      : {last.branch_name}")
        if last.commit_sha:
            print(f"Commit SHA  : {last.commit_sha}")
        print(f"Worktree    : {last.worktree_path}")
    for rev in db.get_reviews_for_run(run_id):
        print(f"Review      : [{rev.provider}/{rev.model}] -> {rev.verdict.value} ({rev.summary})")
    return 0 if final.status == RunStatus.COMPLETED else 1


def handle_cancel(args: argparse.Namespace) -> int:
    """Handle `orch cancel <run-id>`: the sole deliberate stop authority."""
    db = db_for_run(args, args.run_id)
    run = db.get_run(args.run_id)
    if run is None:
        print(f"Error: unknown run: {args.run_id}", file=sys.stderr)
        return 1
    cancelled = RunSupervisor(db=db).cancel(args.run_id)
    after = db.get_run(args.run_id)
    owner = db.get_execution_owner(args.run_id)
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "cancelled": cancelled,
                "status": after.status.value if after is not None else None,
                "owner_state": owner.state if owner is not None else None,
            },
            sort_keys=True,
        )
    )
    return 0


def handle_review(args: argparse.Namespace) -> int:
    """Handle `orch review <run-id>` command."""
    service = OrchestratorService(db=db_for_run(args, args.run_id))

    print(f"[*] Initiating independent review for run '{args.run_id}'...")
    try:
        review = service.review_run(run_id=args.run_id, timeout=args.timeout)
    except Exception as exc:
        print(f"[!] Review failed: {exc}", file=sys.stderr)
        return 1

    print("─" * 60)
    print(f"Review ID : {review.id}")
    print(f"Run ID    : {review.run_id}")
    print(f"Reviewer  : {review.provider} / {review.model}")
    print(f"Verdict   : {review.verdict.value}")
    print(f"Summary   : {review.summary}")
    if review.details:
        print(f"\nFeedback:\n{review.details}")

    return 0 if review.verdict.value == "PASS" else 1


def handle_accept(args: argparse.Namespace) -> int:
    """Handle `orch accept <run-id>` command."""
    service = OrchestratorService(db=db_for_run(args, args.run_id))

    print(f"[*] Integrating candidate from run '{args.run_id}'...")
    try:
        result = service.accept_run(run_id=args.run_id, gate_timeout=args.gate_timeout)
    except Exception as exc:
        print(f"[!] Integration failed: {exc}", file=sys.stderr)
        return 1

    print("─" * 60)
    print("INTEGRATION SUCCESSFUL")
    print(f"Target Repo  : {result.target_repo}")
    print(f"Final Branch : {result.final_branch}")
    print(f"Final SHA    : {result.final_sha}")
    print(f"Summary      : {result.summary}")
    return 0


def _format_output_tail(
    raw: str,
    max_lines: int = 15,
    max_line_length: int = 500,
) -> list[str]:
    """Extract a bounded tail of non-empty lines from stored output."""
    if not raw or not raw.strip():
        return []
    lines = [line.rstrip() for line in raw.splitlines()]
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines:
        return []
    tail = lines[-max_lines:]
    return [
        (line[:max_line_length] + "..." if len(line) > max_line_length else line) for line in tail
    ]


def handle_plan(args: argparse.Namespace) -> int:
    """Handle ``orch plan`` using only persisted normalized rows."""
    db = db_for_run(args, args.run_id)
    plan = db.get_run_plan(args.run_id)
    if plan is None:
        print(json.dumps({"run_id": args.run_id, "plan": None, "legacy": True}))
        return 0
    packages = {package.id: package for package in db.get_work_packages(args.run_id)}
    steps = []
    for step in db.get_plan_steps(args.run_id):
        package = packages.get(step.work_package_id or "")
        steps.append(
            {
                "id": step.id,
                "kind": step.kind,
                "title": step.title,
                "goal": step.goal,
                "status": step.status.value,
                "ordering": step.ordering,
                "dependency_ids": list(step.dependency_ids),
                "work_package_id": step.work_package_id,
                "attempt_id": step.attempt_id,
                "package": {
                    "base_candidate_sha": package.base_candidate_sha,
                    "long_running": package.long_running,
                    "long_running_reason": package.long_running_reason,
                }
                if package
                else None,
            }
        )
    print(
        json.dumps(
            {
                "run_id": args.run_id,
                "plan": {"id": plan.id, "status": plan.status.value, "version": plan.version},
                "steps": steps,
            },
            sort_keys=True,
        )
    )
    return 0


def handle_handoff(args: argparse.Namespace) -> int:
    """Handle immutable handoff inspection."""
    kind = "handoff_resume_capsule" if args.capsule else "handoff_full"
    handoff = db_for_run(args, args.run_id).get_latest_handoff(args.run_id, kind)
    if handoff is None:
        print(json.dumps({"run_id": args.run_id, "kind": kind, "handoff": None}))
        return 0
    print(handoff.content)
    return 0


def handle_wait(args: argparse.Namespace) -> int:
    """Block on the durable event log; no model/status polling is involved."""
    db = db_for_run(args, args.run_id)
    deadline = time.monotonic() + max(0.0, args.timeout)
    after = max(0, args.after)
    meaningful = {
        "worker_completed",
        "worker_failed",
        "gate_passed",
        "gate_failed",
        "gate_completed",
        "review_completed",
        "manager_suspended",
        "manager_wake_requested",
        "manager_resumed",
        "manager_attention_required",
        "plan_step_ready",
        "plan_step_started",
        "plan_step_completed",
        "plan_step_blocked",
        "plan_step_failed",
        "orphan_detected",
        "run_cancelled",
        "run_completed",
        "run_failed",
        "run_ready",
    }
    # Subscribe before the first read so an event recorded by the detached
    # execution owner between the read and the wait cannot be missed.
    with subscribe_run(db.db_path, args.run_id) as wake:
        while True:
            events = db.get_events(args.run_id, after_id=after, limit=100)
            for event in events:
                after = event.id
                if event.event_type in meaningful:
                    print(
                        json.dumps(
                            {
                                "timeout": False,
                                "event": {
                                    "id": event.id,
                                    "type": event.event_type,
                                    "run_id": event.run_id,
                                    "attempt_id": event.attempt_id,
                                    "payload": event.payload,
                                    "created_at": event.created_at,
                                },
                            },
                            sort_keys=True,
                        )
                    )
                    return 0
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print(
                    json.dumps(
                        {"timeout": True, "run_id": args.run_id, "after": after}, sort_keys=True
                    )
                )
                return 0
            # Blocks on the run's cross-process wake channel; the only timeout
            # here is the caller's own --timeout budget.
            wake.wait(remaining)


def handle_reconcile(args: argparse.Namespace) -> int:
    """Handle explicit owner-invoked orphan reconciliation."""
    from orchestrator_mvp.supervisor import RunSupervisor

    db = db_for_run(args, args.run_id)
    try:
        result = RunSupervisor(db=db).reconcile(args.run_id, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def handle_recover(args: argparse.Namespace) -> int:
    """Handle ``orch recover`` -- the dedicated C05 cold-takeover owner.

    This CLI is intentionally distinct from ``orch run --run-id``: ordinary
    PENDING adoption stays PENDING-only.  Cold takeover of a RUNNING run
    requires a fenced execution-owner reservation handed down from the
    supervisor that pre-claimed the new generation.  Lacking that
    reservation (no env token, stale token, or no matching durable record)
    this entry fails closed before any checkpoint or attempt mutation.

    The :class:`OrchestratorService.recover_sync` invocation reads the
    environment reservation; the underlying orchestrator re-verifies the
    fenced generation against the durable execution owner row and confirms
    the predecessor is provably DEAD.
    """
    run_id = str(args.run_id).strip()
    if not run_id:
        print("[!] Run id is required", file=sys.stderr)
        return 1
    db = db_for_run(args, run_id)
    reservation = parse_owner_reservation()
    child_identity = ProcessIdentity.for_self()
    if reservation is None:
        db.record_event(
            run_id,
            "recovery_refused_no_authority",
            payload={"reason": "missing_complete_owner_reservation"},
        )
        print("[!] Recovery refused: missing complete owner reservation", file=sys.stderr)
        return 1
    try:
        # This is an atomic adoption, not an advisory identity update.  It
        # replaces the launcher's ProcessIdentity only when *both* halves of
        # the reservation still name the durable claim.
        db.claim_execution_owner(
            run_id,
            identity=child_identity,
            expect_owner_id=reservation.owner_id,
            expect_generation=reservation.generation,
        )
    except OwnershipError as exc:
        db.record_event(
            run_id,
            "recovery_refused_no_authority",
            payload={"reason": "reservation_adoption_failed", "error": str(exc)},
        )
        print(f"[!] Recovery refused: {exc}", file=sys.stderr)
        return 1
    service = OrchestratorService(db=db)
    try:
        result = service.recover_sync(run_id=run_id)
    except OwnershipError as exc:
        db.record_event(
            run_id,
            "recovery_refused_no_authority",
            payload={
                "reason": "fenced_authority_missing",
                "error": str(exc),
            },
        )
        print(f"[!] Recovery refused: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        db.record_event(
            run_id,
            "recovery_failed",
            payload={"error": str(exc)},
        )
        print(f"[!] Recovery failed: {exc}", file=sys.stderr)
        return 1
    finally:
        # Release this child process's owner claim so the orchestrator may
        # release the reservation made on its behalf by the supervisor.
        owner = db.get_execution_owner(run_id)
        if (
            owner is not None
            and owner.is_active
            and owner.owner_id == reservation.owner_id
            and owner.generation == reservation.generation
            and owner.identity == child_identity
        ):
            try:
                db.release_execution_owner(
                    run_id,
                    owner_id=reservation.owner_id,
                    generation=reservation.generation,
                    reason="recovery_finished",
                )
            except Exception:
                pass
    print("─" * 60)
    print(f"Run ID      : {result.run_id}")
    print(f"Status      : {result.status.value}")
    print(f"Gate Passed : {'YES' if result.passed else 'NO'}")
    print(f"Summary     : {result.summary}")
    if result.branch_name:
        print(f"Branch      : {result.branch_name}")
    if result.commit_sha:
        print(f"Commit SHA  : {result.commit_sha}")
    if result.worktree_path:
        print(f"Worktree    : {result.worktree_path}")
    return 0 if result.passed else 1


def handle_status(args: argparse.Namespace) -> int:
    """Handle `orch status <run-id>` command."""
    service = OrchestratorService(db=db_for_run(args, args.run_id))
    run = service.get_run(args.run_id)
    if not run:
        print(f"Error: Run '{args.run_id}' not found in {service.db.db_path}", file=sys.stderr)
        return 1

    print(f"Run ID           : {run.id}")
    print(f"Task             : {run.task}")
    print(f"Target Repo      : {run.target_repo}")
    print(f"Base Commit      : {run.base_commit}")
    print(f"Tier             : {run.tier.value}")
    print(f"Training Allowed : {run.training_allowed}")
    print(f"Gate Command     : {run.gate_command}")
    print(f"Status           : {run.status.value}")
    print(f"Created At       : {run.created_at}")
    print(f"Completed At     : {run.completed_at or 'N/A'}")
    print(f"Summary          : {run.result_summary or 'N/A'}")
    supervision = service.db.get_supervision(run.id)
    if supervision is not None:
        print(f"Supervisor       : {supervision.supervisor_state or 'UNKNOWN'}")
        print(f"Worker PID       : {supervision.worker_pid or 'UNKNOWN'}")
        print(
            f"Worker Health    : {supervision.health_state.value} ({supervision.health_reason or 'UNKNOWN'})"
        )
        print(f"Manager State    : {supervision.manager_state.value}")

    attempts = service.db.get_attempts_for_run(run.id)
    # Candidate-publication evidence comes from the durable event log so the
    # status surface stays consistent with what actually happened.
    publication_events = [
        event
        for event in service.get_events(run.id, limit=1000)
        if event.event_type == "candidate_published"
    ]
    if publication_events:
        latest = publication_events[-1]
        payload = latest.payload or {}
        print("\nCandidate Publication:")
        print("  Published : yes")
        print(f"  Remote    : {payload.get('remote_name', 'origin')}")
        print(f"  Branch    : {payload.get('remote_branch', payload.get('candidate_branch', ''))}")
        print(f"  Candidate : {payload.get('candidate_sha', '')}")
        print(f"  Verified  : {payload.get('verified_remote_sha', '')}")
    else:
        cfg_data: dict[str, object] = {}
        if run.config_json:
            try:
                loaded = json.loads(run.config_json)
                if isinstance(loaded, dict):
                    cfg_data = loaded
            except json.JSONDecodeError:
                pass
        if cfg_data.get("publish_candidate"):
            print("\nCandidate Publication: enabled, but no candidate was published")

    print(f"\nAttempts ({len(attempts)}):")

    for att in attempts:
        print(f"  Attempt #{att.attempt_number} [{att.provider} / {att.model}]")
        print(f"    Status   : {att.status.value}")
        print(f"    Branch   : {att.branch_name}")
        print(f"    Commit   : {att.commit_sha or 'N/A'}")
        print(f"    Worktree : {att.worktree_path}")
        if att.worker_error:
            print(f"    Error    : {att.worker_error}")

        if att.status != AttemptStatus.SUCCESS or att.worker_error:
            stderr_tail = _format_output_tail(att.worker_stderr)
            if stderr_tail:
                print("    Stderr tail:")
                for line in stderr_tail:
                    print(f"      {line}")
            else:
                stdout_tail = _format_output_tail(att.worker_stdout)
                if stdout_tail:
                    print("    Stdout tail:")
                    for line in stdout_tail:
                        print(f"      {line}")

        checks = service.db.get_checks_for_attempt(att.id)
        for chk in checks:
            pass_str = "PASS" if chk.passed else "FAIL"
            print(f"    Check    : Gate '{chk.gate_command}' -> {pass_str} (exit {chk.exit_code})")

    reviews = service.db.get_reviews_for_run(run.id)
    if reviews:
        print(f"\nReviews ({len(reviews)}):")
        for rev in reviews:
            print(f"  Review [{rev.provider} / {rev.model}] -> {rev.verdict.value}")
            print(f"    Summary  : {rev.summary}")
            details_tail = _format_output_tail(rev.details, max_lines=200, max_line_length=1000)
            if details_tail:
                print("    Feedback:")
                for line in details_tail:
                    print(f"      {line}")

    return 0


def _bound_payload_strings(data: Any, max_length: int = 8000) -> Any:
    """Recursively bound all string values in data to their last max_length characters."""
    if isinstance(data, str):
        return data[-max_length:] if len(data) > max_length else data
    if isinstance(data, dict):
        return {k: _bound_payload_strings(v, max_length) for k, v in data.items()}
    if isinstance(data, list):
        return [_bound_payload_strings(item, max_length) for item in data]
    return data


def _format_event_payload(payload_raw: str | None, max_string_len: int = 8000) -> str:
    """Format and bound event JSON payload for display."""
    if payload_raw is None or not payload_raw.strip():
        return "{}"
    try:
        parsed = json.loads(payload_raw)
        bounded = _bound_payload_strings(parsed, max_length=max_string_len)
        return json.dumps(bounded, indent=2)
    except Exception:
        return payload_raw[-max_string_len:] if len(payload_raw) > max_string_len else payload_raw


def handle_events(args: argparse.Namespace) -> int:
    """Handle `orch events <run-id>` command. Strictly read-only."""
    db_arg = getattr(args, "db", None)
    if db_arg:
        db_path = Path(db_arg)
    else:
        # A project-scoped run ID raises here rather than silently reading the
        # preserved global store.
        db_path = db_path_for_run(args.run_id) or get_default_db_path()
    resolved = db_path.resolve()

    if not resolved.exists():
        print(f"Error: Database not found at {resolved}", file=sys.stderr)
        return 1

    try:
        conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        print(f"Error: Could not open database at {resolved}: {exc}", file=sys.stderr)
        return 1

    try:
        conn.row_factory = sqlite3.Row
        run_row = conn.execute("SELECT id FROM runs WHERE id = ?", (args.run_id,)).fetchone()
        if run_row is None:
            print(f"Error: Run '{args.run_id}' not found in {resolved}", file=sys.stderr)
            return 1

        limit = max(1, args.limit) if args.limit is not None and args.limit > 0 else 200
        rows = conn.execute(
            """
            SELECT id, attempt_id, event_type, payload_json, created_at
            FROM (
                SELECT id, attempt_id, event_type, payload_json, created_at
                FROM events
                WHERE run_id = ?
                ORDER BY id DESC
                LIMIT ?
            )
            ORDER BY id ASC
            """,
            (args.run_id, limit),
        ).fetchall()

        if not rows:
            print(f"No events found for run '{args.run_id}'.")
            return 0

        for i, row in enumerate(rows):
            if i > 0:
                print()
            event_id = row["id"]
            created_at = row["created_at"]
            attempt_id = row["attempt_id"] if row["attempt_id"] else "-"
            event_type = row["event_type"]
            payload_str = _format_event_payload(row["payload_json"])

            print(f"Event ID   : {event_id}")
            print(f"Created At : {created_at}")
            print(f"Attempt ID : {attempt_id}")
            print(f"Event Type : {event_type}")
            print(f"Payload    :\n{payload_str}")

        return 0
    except sqlite3.Error as exc:
        print(f"Error: Database query failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()


def handle_observability(args: argparse.Namespace) -> int:
    """Handle the strictly read-only C06 observability snapshot command."""
    explicit = _explicit_db(args)
    path = Path(explicit) if explicit else (db_path_for_run(args.run_id) or get_default_db_path())
    resolved = path.resolve()
    if not resolved.is_file():
        print(f"Error: Database not found at {resolved}", file=sys.stderr)
        return 1
    try:
        db = Database(resolved, read_only=True)
        snapshot = build_run_observability_snapshot(db, args.run_id)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: Could not read observability for '{args.run_id}': {exc}", file=sys.stderr)
        return 1
    # Canonical JSON is always used so repeated reads have byte-identical output.
    sys.stdout.buffer.write(canonical_snapshot_json(snapshot) + b"\n")
    return 0


def handle_list(args: argparse.Namespace) -> int:
    """Handle `orch list` command.

    Without an explicit ``--db`` the listing is project-local so runs from
    unrelated repositories are never presented together.
    """
    repo_arg = getattr(args, "repo", None) or Path.cwd()
    if _explicit_db(args) is not None:
        db = Database(_explicit_db(args))
    else:
        try:
            db = db_for_repo(args, repo_arg)
        except ProjectStateConflictError:
            raise
        except ProjectIdentityError:
            # Not a repository at all: show the preserved global store.
            db = Database(get_default_db_path())
    service = OrchestratorService(db=db)
    runs = service.list_runs(limit=args.limit)
    if not runs:
        print(f"No runs found in database {service.db.db_path}.")
        return 0

    header = f"{'RUN ID':<16} {'STATUS':<12} {'TIER':<6} {'CREATED AT':<24} {'TASK'}"
    print(header)
    print("─" * len(header))
    for r in runs:
        task_snippet = (r.task[:40] + "...") if len(r.task) > 40 else r.task
        print(
            f"{r.id:<16} {r.status.value:<12} {r.tier.value:<6} "
            f"{r.created_at[:19]:<24} {task_snippet}"
        )
    return 0


def handle_cleanup(args: argparse.Namespace) -> int:
    """Handle `orch cleanup <run-id>` command."""
    service = OrchestratorService(db=db_for_run(args, args.run_id))

    print(f"[*] Cleaning up attempt worktrees for run '{args.run_id}'...")
    try:
        res = service.cleanup_run(args.run_id)
    except Exception as exc:
        print(f"[!] Cleanup failed: {exc}", file=sys.stderr)
        return 1

    print("─" * 60)
    if res["removed"]:
        print(f"Removed clean worktree(s) ({len(res['removed'])}):")
        for path in res["removed"]:
            print(f"  ✓ {path}")

    if res["skipped_dirty"]:
        print(f"\nSkipped dirty worktree(s) for inspection ({len(res['skipped_dirty'])}):")
        for path in res["skipped_dirty"]:
            print(f"  ! {path}")

    if res["skipped_missing"]:
        print(f"\nAlready removed / missing ({len(res['skipped_missing'])}):")
        for path in res["skipped_missing"]:
            print(f"  - {path}")

    return 0


def handle_models(
    args: argparse.Namespace,
    *,
    registry: AdapterRegistry | None = None,
    db: Database | None = None,
) -> int:
    """Handle `orch models` command."""
    if registry is None:
        registry = AdapterRegistry.default()
    if db is None:
        db = Database(getattr(args, "db", None))
    service = OrchestratorService(db=db, registry=registry)
    peak = is_peak_hours()
    try:
        snap = load_effective_routing()
    except RoutingConfigError as exc:
        print(f"[!] Routing config invalid: {exc}", file=sys.stderr)
        return 1
    print("Configured Multi-Provider Execution Tiers & Routing Chains:\n")
    print(f"Current Schedule State: {'PEAK' if peak else 'OFF-PEAK'}")
    print(f"Routing Source: {snap.source} {snap.source_path or '(packaged default)'}")
    print(f"Content Hash: {snap.content_hash}\n")

    print("T1 (Standard / Fast Tier):")
    for c in snap.t1_default:
        avail = "✓ available" if registry.is_available(c.provider) else "✗ unavailable"
        print(f"  → [{c.provider}] {c.model} ({avail})")

    print("\nT2 (Advanced Tier - Training Denied):")
    print("  [OFF-PEAK ORDER]:")
    for c in snap.t2_training_denied_off_peak:
        avail = "✓ available" if registry.is_available(c.provider) else "✗ unavailable"
        print(f"    → [{c.provider}] {c.model} ({avail})")
    print("  [PEAK ORDER]:")
    for c in snap.t2_training_denied_peak:
        avail = "✓ available" if registry.is_available(c.provider) else "✗ unavailable"
        print(f"    → [{c.provider}] {c.model} ({avail})")

    print("\nT2 (Advanced Tier - Training Allowed):")
    for c in snap.t2_training_allowed:
        avail = "✓ available" if registry.is_available(c.provider) else "✗ unavailable"
        print(f"  → [{c.provider}] {c.model} ({avail})")

    print("\nT3 (High Performance Tier):")
    print("  [OFF-PEAK ORDER]:")
    for c in snap.t3_off_peak:
        avail = "✓ available" if registry.is_available(c.provider) else "✗ unavailable"
        print(f"    → [{c.provider}] {c.model} ({avail})")
    print("  [PEAK ORDER]:")
    for c in snap.t3_peak:
        avail = "✓ available" if registry.is_available(c.provider) else "✗ unavailable"
        print(f"    → [{c.provider}] {c.model} ({avail})")

    print("\nLocal Provider Status:")
    quota_rows = service.list_quota()
    for prov in _DOCTOR_PROVIDERS:
        status = _provider_status_line(registry, prov, quota_rows)
        print(f"  • {prov:<12}: {status}")

    print(
        "\nPolicy Note: Muse models are included by default; pass "
        "--training-denied to exclude them."
    )
    return 0


def handle_routing_show(args: argparse.Namespace) -> int:
    try:
        snap = load_effective_routing()
    except RoutingConfigError as exc:
        print(f"[!] Routing config invalid: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        payload = {
            "source": snap.source,
            "source_path": snap.source_path,
            "content_hash": snap.content_hash,
            "schema_version": snap.schema_version,
            "chains": {
                "T1.default": [f"{c.provider}/{c.model}" for c in snap.t1_default],
                "T2.training_allowed": [
                    f"{c.provider}/{c.model}" for c in snap.t2_training_allowed
                ],  # noqa: E501
                "T2.training_denied_off_peak": [
                    f"{c.provider}/{c.model}" for c in snap.t2_training_denied_off_peak
                ],  # noqa: E501
                "T2.training_denied_peak": [
                    f"{c.provider}/{c.model}" for c in snap.t2_training_denied_peak
                ],  # noqa: E501
                "T3.off_peak": [f"{c.provider}/{c.model}" for c in snap.t3_off_peak],
                "T3.peak": [f"{c.provider}/{c.model}" for c in snap.t3_peak],
            },
        }
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Source: {snap.source} {snap.source_path or '(packaged default)'}")
    print(f"Hash: {snap.content_hash}")
    print(f"Schema: {snap.schema_version}")
    print("T1.default:", [f"{c.provider}/{c.model}" for c in snap.t1_default])
    print("T2.training_allowed:", [f"{c.provider}/{c.model}" for c in snap.t2_training_allowed])  # noqa: E501
    print(
        "T2.training_denied_off_peak:",
        [f"{c.provider}/{c.model}" for c in snap.t2_training_denied_off_peak],
    )  # noqa: E501
    print(
        "T2.training_denied_peak:",
        [f"{c.provider}/{c.model}" for c in snap.t2_training_denied_peak],
    )  # noqa: E501
    print("T3.off_peak:", [f"{c.provider}/{c.model}" for c in snap.t3_off_peak])
    print("T3.peak:", [f"{c.provider}/{c.model}" for c in snap.t3_peak])
    return 0


def handle_routing_validate(args: argparse.Namespace) -> int:  # noqa: ARG001
    user_path = get_user_routing_path()
    if not user_path.is_file():
        print(f"No user routing config at {user_path}; packaged defaults are effective.")
        try:
            snap = load_effective_routing()
            print(f"Packaged default valid (hash {snap.content_hash})")
        except RoutingConfigError as exc:
            print(f"[!] Packaged default invalid: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        snap = load_effective_routing()
        print(f"Routing config valid at {snap.source_path} (hash {snap.content_hash})")
        return 0
    except RoutingConfigError as exc:
        print(f"[!] Routing config invalid: {exc}", file=sys.stderr)
        return 1


def handle_routing_init(args: argparse.Namespace) -> int:
    try:
        dest = init_user_config(force=bool(getattr(args, "force", False)))
        print(f"Routing config initialized at {dest}")
        return 0
    except RoutingConfigError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1


def _resolve_repo_for_project_settings(args: argparse.Namespace) -> Path:
    """Return the explicit ``--repo`` arg as a resolved Path.

    Project-scoped settings always need an explicit repository; the
    existing orch surfaces already converge on a ``--repo`` flag for
    this purpose.  The argument is registered with
    ``dest='project_repo'`` on both the parent and the leaf subparsers
    so it can appear before or after the leaf command.
    """
    raw = str(getattr(args, "project_repo", "") or "").strip()
    if not raw:
        print("[!] --repo is required for project-scoped settings", file=sys.stderr)
        raise SystemExit(2)
    return Path(raw).expanduser().resolve()


def handle_project_gate_scratch_show(args: argparse.Namespace) -> int:
    """Handle ``orch project gate-scratch show``."""
    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        payload = OrchestratorService.get_project_gate_scratch_policy(repo)
    except GateScratchPolicyError as exc:
        print(f"[!] Project gate-scratch policy is invalid: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Mode          : {payload['mode']}")
    print(f"Label         : {payload['label']}")
    print(f"Default (DISK): {'yes' if payload['is_default'] else 'no'}")
    return 0


def handle_project_gate_scratch_set(args: argparse.Namespace) -> int:
    """Handle ``orch project gate-scratch set <mode>``."""
    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        path = OrchestratorService.set_project_gate_scratch_policy(repo, str(args.mode))
    except GateScratchPolicyError as exc:
        print(f"[!] Project gate-scratch policy is invalid: {exc}", file=sys.stderr)
        return 1
    print(f"Project gate-scratch mode set to '{args.mode}' at {path}")
    return 0


def handle_authority_show(args: argparse.Namespace) -> int:
    """Handle `orch authority show`."""
    try:
        settings = load_authority_settings()
    except AuthorityConfigError as exc:
        print(f"[!] Authority config invalid: {exc}", file=sys.stderr)
        return 1
    payload = settings.as_dict()
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Mode          : {settings.mode.value}")
    print(f"Label         : {settings.label}")
    print(f"Source        : {settings.source} {settings.source_path or '(packaged default)'}")
    print(f"Host access   : {settings.host_access.value}")
    print(f"Config path   : {payload['config_path']}")
    print(f"Package default: {payload['package_default_mode']}")
    print("Policy:")
    policy = settings.policy.as_dict()
    for key in policy:
        print(f"  {key}: {'true' if policy[key] else 'false'}")
    return 0


def handle_authority_list(args: argparse.Namespace) -> int:
    """Handle `orch authority list`."""
    presets = list_presets()
    if getattr(args, "json", False):
        print(json.dumps({"presets": presets}, indent=2))
        return 0
    for index, preset in enumerate(presets, start=1):
        print(f"{index}. {preset['label']} ({preset['mode']})")
    return 0


def handle_authority_set(args: argparse.Namespace) -> int:
    """Handle `orch authority set <mode>`."""
    try:
        mode = parse_authority_mode(args.mode)
    except AuthorityConfigError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    try:
        path = set_authority_mode(mode)
    except OSError as exc:
        print(f"[!] Could not persist authority mode: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(
            json.dumps(
                {"mode": mode.value, "label": label_for(mode), "config_path": str(path)},
                indent=2,
            )
        )
        return 0
    print(f"Authority mode set to '{mode.value}' ({label_for(mode)}) at {path}")
    return 0


def handle_authority_set_host_access(args: argparse.Namespace) -> int:
    """Handle `orch authority set-host-access <mode>`."""
    try:
        mode = HostAccessMode(str(args.mode))
        path = set_host_access(mode)
    except (ValueError, OSError, AuthorityConfigError) as exc:
        print(f"[!] Could not persist host access: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps({"host_access": mode.value, "config_path": str(path)}, indent=2))
    else:
        print(f"Host access set to '{mode.value}' at {path}")
    return 0


def handle_authority_sync_opencode(args: argparse.Namespace) -> int:
    """Handle `orch authority sync-opencode`."""
    try:
        settings = load_authority_settings()
    except AuthorityConfigError as exc:
        print(f"[!] Authority config invalid: {exc}", file=sys.stderr)
        return 1
    agent_path = Path(str(args.agent)).expanduser()
    raw_executable = getattr(args, "orch_executable", None)
    executable = (
        Path(str(raw_executable)).expanduser() if raw_executable else default_orch_executable()
    )
    raw_directories = getattr(args, "allow_directory", None)
    directories = (
        tuple(str(entry) for entry in raw_directories)
        if raw_directories
        else default_allowed_directories(executable)
    )
    try:
        result = sync_agent_file(
            agent_path,
            settings.mode,
            orch_executable=executable,
            allowed_directories=directories,
        )
    except AgentSyncError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result.as_dict(), indent=2))
        return 0
    if not result.changed:
        print(f"Agent {result.path} already synchronized with '{result.mode.value}'.")
        return 0
    print(f"Agent {result.path} synchronized with '{result.mode.value}'.")
    print(f"Backup: {result.backup_path}")
    return 0


def handle_quota_set(args: argparse.Namespace) -> int:
    """Handle `orch quota set` command."""
    service = OrchestratorService(db=Database(args.db))
    try:
        state = service.set_quota(
            provider=args.provider,
            pool=args.pool,
            remaining_percent=args.remaining,
            reset_at=args.reset_at,
        )
    except ValueError as exc:
        print(f"[!] Invalid quota: {exc}", file=sys.stderr)
        return 1

    reset_str = state.reset_at or "N/A"
    print(f"Quota set: {state.provider}/{state.pool}")
    print(f"  remaining     : {state.remaining_percent:g}%")
    print(f"  reset         : {reset_str}")
    print(f"  routing state : {state.routing_state.value}")
    return 0


def handle_quota_list(args: argparse.Namespace) -> int:
    """Handle `orch quota list` command."""
    service = OrchestratorService(db=Database(args.db))
    states = service.list_quota()
    if not states:
        print("No quota state configured. Providers without quota state are treated as NORMAL.")
        return 0

    header = f"{'PROVIDER':<12} {'POOL':<12} {'REMAINING':<11} {'RESET':<26} {'ROUTING STATE'}"
    print(header)
    print("─" * len(header))
    for state in states:
        reset_str = state.reset_at or "N/A"
        print(
            f"{state.provider:<12} {state.pool:<12} {state.remaining_percent:<11g} "
            f"{reset_str:<26} {state.routing_state.value}"
        )
    return 0


def handle_ui(args: argparse.Namespace) -> int:
    """Handle `orch ui` command."""
    import uvicorn

    from orchestrator_mvp.db import Database
    from orchestrator_mvp.web import create_app

    app = create_app(db_path=args.db, repo_path=args.repo)
    startup_db = Database(args.db) if args.db else db_for_repo(args, args.repo)
    url = f"http://{args.host}:{args.port}"
    print("─" * 60)
    print("Orchestrator Dashboard")
    print(f"  URL      : {url}")
    print(f"  Database : {startup_db.db_path}")
    print(f"  Target   : {Path(args.repo).resolve()}")
    print("─" * 60)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def handle_quota_enable(args: argparse.Namespace) -> int:
    """Handle `orch quota enable` command."""
    service = OrchestratorService(db=Database(args.db))
    state = service.enable_quota(provider=args.provider, pool=args.pool)
    print(
        f"Quota enabled: {state.provider}/{state.pool} "
        f"(remaining={state.remaining_percent:g}%, routing state=NORMAL)"
    )
    return 0


def handle_doctor(args: argparse.Namespace) -> int:
    """Handle `orch doctor` command. Strictly read-only: never mutates state."""
    project_root = _orch_project_root()
    findings: list[DoctorFinding] = []

    findings.append(_check_python_requires(project_root))
    findings.append(_check_venv_tools(project_root))
    findings.append(_check_core_imports())
    findings.append(_check_git_binary())

    db_arg = getattr(args, "db", None)
    db_path = Path(db_arg) if db_arg else get_default_db_path()
    db_finding, quota_rows = _check_database(db_path)
    findings.append(db_finding)

    findings.extend(_check_providers(quota_rows))
    findings.append(_check_target_repo(Path(getattr(args, "repo", "."))))

    ok_count = sum(1 for f in findings if f.severity == "OK")
    warn_count = sum(1 for f in findings if f.severity == "WARN")
    fail_count = sum(1 for f in findings if f.severity == "FAIL")
    verdict = "READY" if fail_count == 0 else "NOT READY"

    for finding in findings:
        print(f"[{finding.severity}] {finding.message}")
    print(f"Doctor summary: {ok_count} OK, {warn_count} WARN, {fail_count} FAIL - {verdict}")
    return 0 if fail_count == 0 else 1


def main(argv: list[str] | None = None) -> int:
    """Main CLI entrypoint."""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return _dispatch(args)
    except ProjectIdentityError as exc:
        # Project identity/state problems fail closed with a plain message
        # rather than a traceback or a silent fallback to another project.
        print(f"Error: {exc}", file=sys.stderr)
        return 1


def handle_tools(args: argparse.Namespace) -> int:
    """Handle the deterministic logical-tool surface."""
    try:
        raw_args = list(getattr(args, "tool_args", ()))
        explicit_db = getattr(args, "db", None)
        explicit_run_id = getattr(args, "run_id", None)
        explicit_attempt_id = getattr(args, "attempt_id", None)
        explicit_operation_id = getattr(args, "operation_id", None)
        if args.tools_command == "exec" and raw_args:
            delimiter = raw_args.index("--") if "--" in raw_args else len(raw_args)
            control_args = raw_args[:delimiter]
            tool_payload = raw_args[delimiter + 1 :] if delimiter < len(raw_args) else []
            parsed_control: list[str] = []
            index = 0
            flag_destinations = {
                "--db": "db",
                "--run-id": "run_id",
                "--attempt-id": "attempt_id",
                "--operation-id": "operation_id",
            }
            while index < len(control_args):
                destination = flag_destinations.get(control_args[index])
                if destination is None:
                    parsed_control.append(control_args[index])
                    index += 1
                    continue
                if index + 1 >= len(control_args):
                    raise ToolManifestError(f"{control_args[index]} requires a value")
                value = control_args[index + 1]
                if destination == "db" and explicit_db is None:
                    explicit_db = value
                elif destination == "run_id" and explicit_run_id is None:
                    explicit_run_id = value
                elif destination == "attempt_id" and explicit_attempt_id is None:
                    explicit_attempt_id = value
                elif destination == "operation_id" and explicit_operation_id is None:
                    explicit_operation_id = value
                else:
                    parsed_control.extend((control_args[index], value))
                index += 2
            raw_args = [*parsed_control, *tool_payload]
        db_path = explicit_db or os.environ.get("ORCH_DB_PATH", "").strip() or None
        run_id = explicit_run_id or os.environ.get("ORCH_RUN_ID", "").strip() or None
        if (db_path is None) != (run_id is None):
            raise ToolManifestError(
                "run-scoped tool access requires both --db/ORCH_DB_PATH and "
                "--run-id/ORCH_RUN_ID"
            )

        db: Database | None = None
        contract: FrozenToolContract | None = None
        if db_path is not None and run_id is not None:
            # Inspection is strictly read-only. Invocation uses the writable
            # handle only because the side-effect journal is durable evidence.
            db = Database(db_path, read_only=args.tools_command != "exec")
            run = db.get_run(run_id)
            if run is None:
                raise ToolManifestError(f"unknown run: {run_id}")
            if not run.tool_contract_json or not run.tool_contract_digest:
                raise ToolManifestError("run has no immutable frozen tool contract")
            contract = FrozenToolContract.from_json(run.tool_contract_json)
            if run.tool_contract_digest != contract.digest:
                raise ToolManifestError(f"stored tool contract digest mismatch for run: {run_id}")
            expected_digest = os.environ.get("ORCH_TOOL_CONTRACT_DIGEST", "").strip()
            if expected_digest and expected_digest != contract.digest:
                raise ToolManifestError("ORCH_TOOL_CONTRACT_DIGEST does not match the run contract")
            # A frozen contract is self-contained. An empty registry makes it
            # impossible for this path to accidentally consult mutable owner
            # state.
            registry = ToolRegistry()
        else:
            registry = ToolRegistry.from_path(args.registry)

        if args.tools_command == "list":
            specs = (
                contract.bindings
                if contract is not None
                else registry.list_refs()
            )
            list_payload = [{"ref": spec.ref, "hint": spec.hint} for spec in specs]
            if args.json:
                print(json.dumps(list_payload, sort_keys=True, separators=(",", ":")))
            else:
                for item in list_payload:
                    print(f"{item['ref']} — {item['hint']}")
            return 0
        if args.tools_command == "describe":
            describe_payload = ToolInvoker(
                registry, contract=contract
            ).describe(args.logical_ref, run_id=run_id)
            print(
                json.dumps(describe_payload, sort_keys=True)
                if args.json
                else "\n".join(f"{key}: {value}" for key, value in describe_payload.items())
            )
            return 0
        result = ToolInvoker(
            registry,
            contract=contract,
            journal=db if run_id else None,
        ).invoke(
            args.logical_ref,
            raw_args,
            run_id=run_id,
            attempt_id=explicit_attempt_id,
            operation_id=explicit_operation_id,
        )
        if result.stdout:
            print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, end="", file=sys.stderr)
        return result.exit_code
    except (ToolManifestError, OSError, ValueError) as exc:
        print(f"[!] Tool operation failed: {exc}", file=sys.stderr)
        return 1


def handle_orch_context(args: argparse.Namespace) -> int:
    """Handle read-only bounded durable evidence retrieval."""
    try:
        db = Database(args.db, read_only=True)
        if args.ref is None:
            print(json.dumps(list_context_refs(db, args.run_id), separators=(",", ":")))
        else:
            print(context_json(db, args.run_id, args.ref))
        return 0
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"[!] Context operation failed: {exc}", file=sys.stderr)
        return 1


def _dispatch(args: argparse.Namespace) -> int:
    """Route a parsed command to its handler."""
    if args.command == "tools":
        return handle_tools(args)
    elif args.command == "scope":
        from orchestrator_mvp.project_scope.cli import handle_scope

        return handle_scope(args)
    elif args.command == "orch-context":
        return handle_orch_context(args)
    elif args.command == "run":
        return handle_run(args)
    elif args.command == "review":
        return handle_review(args)
    elif args.command == "accept":
        return handle_accept(args)
    elif args.command == "cleanup":
        return handle_cleanup(args)
    elif args.command == "status":
        return handle_status(args)
    elif args.command == "plan":
        return handle_plan(args)
    elif args.command == "handoff":
        return handle_handoff(args)
    elif args.command in ("wait", "watch"):
        return handle_wait(args)
    elif args.command == "reconcile":
        return handle_reconcile(args)
    elif args.command == "recover":
        return handle_recover(args)
    elif args.command == "cancel":
        return handle_cancel(args)
    elif args.command == "events":
        return handle_events(args)
    elif args.command == "observability":
        return handle_observability(args)
    elif args.command == "list":
        return handle_list(args)
    elif args.command == "models":
        return handle_models(args)
    elif args.command == "doctor":
        return handle_doctor(args)
    elif args.command == "ui":
        return handle_ui(args)
    elif args.command == "quota":
        if args.quota_command == "set":
            return handle_quota_set(args)
        elif args.quota_command == "list":
            return handle_quota_list(args)
        elif args.quota_command == "enable":
            return handle_quota_enable(args)
    elif args.command == "routing":
        if args.routing_command == "show":
            return handle_routing_show(args)
        elif args.routing_command == "validate":
            return handle_routing_validate(args)
        elif args.routing_command == "init":
            return handle_routing_init(args)
    elif args.command == "authority":
        if args.authority_command == "show":
            return handle_authority_show(args)
        elif args.authority_command == "list":
            return handle_authority_list(args)
        elif args.authority_command == "set":
            return handle_authority_set(args)
        elif args.authority_command == "set-host-access":
            return handle_authority_set_host_access(args)
        elif args.authority_command == "sync-opencode":
            return handle_authority_sync_opencode(args)
    elif args.command == "project":
        if args.project_command == "gate-scratch":
            if args.project_gate_scratch_command == "show":
                return handle_project_gate_scratch_show(args)
            elif args.project_gate_scratch_command == "set":
                return handle_project_gate_scratch_set(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
