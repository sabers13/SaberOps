"""Command-line interface for SaberOps (`orch`)."""
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
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from saberops.certification import CertificationResult

from saberops.authority import (
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
from saberops.config import RunConfig, reconstruct_run_config
from saberops.db import Database, get_default_db_path
from saberops.dispatch import READINESS_AUTHORITY_UNAVAILABLE
from saberops.gate_runner import GateScratchPolicyError
from saberops.git import GitManager
from saberops.model_access import ReadinessService
from saberops.models import (
    TERMINAL_RUN_STATUSES,
    AttemptStatus,
    GateScratchMode,
    ProviderReadiness,
    QuotaState,
    ReviewPolicyMode,
    RoutingState,
    RunStatus,
)
from saberops.observability import build_run_observability_snapshot, canonical_snapshot_json
from saberops.observability.run_report import (
    build_run_report,
    canonical_report_json,
    is_terminal_report,
    render_monitor_frame,
    render_report_text,
)
from saberops.ownership import (
    OWNER_ENV_ID,
    Liveness,
    OwnershipError,
    ProcessIdentity,
    parse_owner_reservation,
)
from saberops.project import (
    ProjectIdentityError,
    ProjectRegistry,
    ProjectStateConflictError,
    RegisteredProject,
    owner_state_dir,
)
from saberops.project_scope.plan_store import PlanStore
from saberops.questions import (
    STANDING_RULE_THRESHOLD,
    AnswerAuthority,
    AskRole,
    DecisionClass,
    Escalation,
    OwnerQuestion,
    OwnerQuestionQueue,
    QuestionError,
    QuestionRequest,
    QuestionStatus,
    question_view,
)
from saberops.review_adaptive import ReviewPolicyError
from saberops.routing_config import (
    RoutingConfigError,
    get_user_routing_path,
    init_user_config,
    load_effective_routing,
)
from saberops.run_creation import (
    RunCreationError,
    RunRequest,
    effective_policy_summary,
    resolve_effective_run_config,
)
from saberops.runtime import db_path_for_run, project_db_path_for_repo
from saberops.service import OrchestratorService
from saberops.storage_gc import (
    GcPolicyError,
    GcPreview,
    apply_gc,
    default_gc_policy_path,
    load_gc_policy,
    preview_gc,
)
from saberops.storage_inventory import ArtifactClass, Inventory, build_inventory
from saberops.storage_maintenance import database_audit_sink
from saberops.supervisor import RunSupervisor
from saberops.tooling.context_cli import context_json, list_context_refs
from saberops.tooling.contracts import FrozenToolContract, ToolManifestError
from saberops.tooling.invocation import ToolInvoker
from saberops.tooling.registry import ToolRegistry
from saberops.wake import subscribe_run
from saberops.workers.registry import AdapterRegistry

_BIN_ATTR: dict[str, str] = {
    "opencode": "opencode_bin",
    "codex": "codex_bin",
    "antigravity": "agy_bin",
    "cline": "cline_bin",
}

_DOCTOR_PROVIDERS: tuple[str, ...] = ("opencode", "codex", "antigravity", "cline")

#: Runtime imports necessary to use SaberOps (from ``pyproject.toml``
#: ``dependencies``).  Missing entries are a genuine readiness failure.
_RUNTIME_MODULES: tuple[str, ...] = ("fastapi", "uvicorn", "jinja2", "multipart", "websockets")

#: Development-only imports relevant to a source checkout.  Never a
#: runtime readiness blocker for an installed package.
_DEV_MODULES: tuple[str, ...] = ("httpx", "pytest")

#: Development-only executables relevant to a source checkout.
_DEV_TOOLS: tuple[str, ...] = ("ruff", "mypy", "pytest")

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


def _cli_explicit_review_policy(args: argparse.Namespace) -> ReviewPolicyMode | None:
    """Return the per-run review-policy override, or None to follow project.

    The CLI default (``risk-adaptive``) is intentionally mapped to None:
    the tightening-only merge treats an unset per-run value and an
    explicit RISK_ADAPTIVE identically, so the project setting drives.
    Only an explicit ``always-required`` tightens this run.
    """
    raw = str(getattr(args, "review_policy", "") or "").strip().lower()
    if raw in ("", "risk-adaptive"):
        return None
    return _cli_review_policy_mode(args)


def _resolve_cli_run_config(args: argparse.Namespace, repo: Path | str) -> RunConfig:
    """Resolve one frozen RunConfig through the shared project-settings authority.

    This is the CLI's only run-creation path: Web's ``POST /runs``
    calls the same :func:`resolve_effective_run_config` with the same
    precedence, so identical project settings plus equivalent run
    requests freeze identical policy.  Raises :class:`RunCreationError`
    (fail-closed, before any worker/model execution) when the project
    has no usable gate or the policy is invalid.
    """
    tier_raw = getattr(args, "tier", "auto")
    max_tier_raw = getattr(args, "max_tier", "T3")
    full = RunRequest(
        task=str(args.task),
        repo_path=repo,
        gate_command=getattr(args, "gate", None),
        training_allowed=getattr(args, "training_allowed", None),
        review_policy=_cli_explicit_review_policy(args),
        routing_mode="auto" if tier_raw == "auto" else "manual",
        manual_tier=None if tier_raw == "auto" else tier_raw,
        max_auto_tier=max_tier_raw,
        provider_override=args.provider,
        model_override=args.model,
        reserve_override=args.reserve_override,
        required_capabilities=tuple(args.required_capabilities),
        max_attempts=args.max_attempts,
        worker_timeout=args.worker_timeout,
        gate_timeout=args.gate_timeout,
        review=args.review,
        review_timeout=args.review_timeout,
        review_mode=args.review_mode,
        review_limit=args.review_limit,
        publish_candidate=getattr(args, "publish_candidate", None),
        tool_refs=tuple(args.tool_refs),
    )
    config: RunConfig = resolve_effective_run_config(full)
    return config


def _warn_deprecated_routing_flags(args: argparse.Namespace) -> None:
    """Print one deprecation line per explicitly given routing flag (ADR-0002 §11)."""
    if hasattr(args, "tier"):
        print(
            "[!] --tier is deprecated (ADR-0002 §11): "
            "--tier T1|T2 runs the WORKER chain without escalation, "
            "--tier T3 starts on the SURGEON chain.",
            file=sys.stderr,
        )
    if hasattr(args, "max_tier"):
        print(
            "[!] --max-tier is deprecated (ADR-0002 §11): "
            "--max-tier T1|T2 disables escalation to SURGEON, "
            "--max-tier T3 (default) allows it.",
            file=sys.stderr,
        )


def _orch_project_root() -> Path:
    """Return the candidate SaberOps project root for a source checkout."""
    return Path(__file__).resolve().parents[2]


def _is_source_checkout(project_root: Path) -> bool:
    """Return True only for a developer source checkout layout.

    An installed (non-editable) package lives under site-packages and has
    no ``pyproject.toml`` / ``src/saberops`` siblings; development
    diagnostics apply only to a real checkout.
    """
    return (project_root / "pyproject.toml").is_file() and (
        project_root / "src" / "saberops"
    ).is_dir()


def _installed_package_metadata() -> tuple[str | None, str | None]:
    """Return ``(version, requires_python)`` from installed metadata, if any."""
    try:
        from importlib.metadata import metadata as _metadata
        from importlib.metadata import version as _version
    except Exception:
        return None, None
    try:
        dist_version = _version("saberops")
    except Exception:
        return None, None
    try:
        requires_python = _metadata("saberops").get("Requires-Python")
    except Exception:
        requires_python = None
    return dist_version, str(requires_python) if requires_python else None


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
    """Check the running interpreter against the declared requires-python.

    Uses installed package metadata when available (the installed-package
    path) and falls back to the checkout ``pyproject.toml`` only for a
    source checkout without installed metadata.
    """
    current_version = (sys.version_info[0], sys.version_info[1], sys.version_info[2])
    current_str = ".".join(str(part) for part in current_version)
    dist_version, requires_python = _installed_package_metadata()
    if requires_python:
        verdict = _requires_python_satisfied(requires_python.strip(), current_version)
        if verdict is None:
            return DoctorFinding(
                "WARN",
                f"saberops {dist_version}: could not evaluate "
                f"requires-python '{requires_python}' against python {current_str}",
            )
        if verdict:
            return DoctorFinding(
                "OK",
                f"saberops {dist_version}: python {current_str} satisfies "
                f"requires-python '{requires_python}'",
            )
        return DoctorFinding(
            "FAIL",
            f"saberops {dist_version}: python {current_str} does NOT satisfy "
            f"requires-python '{requires_python}'",
        )
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
    verdict = _requires_python_satisfied(raw.strip(), current_version)
    if verdict is None:
        return DoctorFinding(
            "WARN",
            f"could not evaluate requires-python '{raw}' against python {current_str}",
        )
    if verdict:
        return DoctorFinding("OK", f"python {current_str} satisfies requires-python '{raw}'")
    return DoctorFinding("FAIL", f"python {current_str} does NOT satisfy requires-python '{raw}'")


def _check_venv_tools(project_root: Path) -> DoctorFinding:
    """Check developer bootstrap tools; informational for installed packages.

    A normal installed package must never fail readiness merely because
    development-only executables (ruff/mypy/pytest) are absent.  This check
    fails only inside a source checkout where the developer environment is
    genuinely expected.
    """
    required = _DEV_TOOLS
    if not _is_source_checkout(project_root):
        return DoctorFinding(
            "OK",
            "installed package: developer-tool check skipped "
            f"({', '.join(required)} not required at runtime)",
        )
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
    """Check runtime imports necessary to use SaberOps (genuine blockers)."""
    missing = [name for name in _RUNTIME_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        return DoctorFinding("FAIL", f"runtime modules unresolvable: {', '.join(missing)}")
    return DoctorFinding("OK", f"runtime modules resolvable: {', '.join(_RUNTIME_MODULES)}")


def _check_dev_imports(project_root: Path) -> DoctorFinding:
    """Report development-only imports without ever blocking readiness."""
    if not _is_source_checkout(project_root):
        return DoctorFinding(
            "OK",
            "installed package: development-only modules "
            f"({', '.join(_DEV_MODULES)}) not required at runtime",
        )
    missing = [name for name in _DEV_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        return DoctorFinding(
            "WARN",
            f"development-only modules unresolvable: {', '.join(missing)} "
            "(source checkout; not a runtime blocker)",
        )
    return DoctorFinding("OK", f"development-only modules resolvable: {', '.join(_DEV_MODULES)}")


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
    try:
        version_row_conn = sqlite3.connect(
            f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0
        )
        try:
            version_row = version_row_conn.execute("PRAGMA user_version").fetchone()
            schema_version = int(version_row[0]) if version_row is not None else 0
        finally:
            version_row_conn.close()
    except sqlite3.Error as exc:
        return (
            DoctorFinding("FAIL", f"database schema unreadable at {resolved}: {exc}"),
            [],
        )
    from saberops.db import DB_SCHEMA_VERSION

    if schema_version != DB_SCHEMA_VERSION:
        return (
            DoctorFinding(
                "FAIL",
                f"database schema unsupported at {resolved}: "
                f"user_version={schema_version}, expected {DB_SCHEMA_VERSION}",
            ),
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
        DoctorFinding(
            "OK",
            f"database readable (read-only) at {resolved} "
            f"({count} runs, schema v{schema_version})",
        ),
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
    # Append the durable readiness projection.  The leading
    # "AVAILABLE (<path>)" wording is preserved for existing operator
    # tooling; the suffix distinguishes READY / NOT READY /
    # READINESS UNKNOWN.  Strictly read-only: never mutates state.
    # A lookup failure is itself UNKNOWN -- never plain
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


def _check_legacy_manager_state() -> DoctorFinding | None:
    """Report a leftover manager-session dir as legacy/unused state, if present.

    Strictly read-only: never creates, mutates, or deletes owner files.
    """
    from saberops.paths import saberops_state_dir

    legacy_dir = saberops_state_dir() / "manager-session"
    try:
        if legacy_dir.is_dir():
            return DoctorFinding(
                "WARN",
                f"legacy unused state at {legacy_dir} "
                "(removed Manager capability; left untouched)",
            )
    except OSError:
        return None
    return None


def _positive_int_arg(value: str) -> int:
    """argparse type for a strictly positive integer (used by --max-concurrent)."""
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError(f"invalid integer value: {value!r}") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer (>= 1)")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    """Build CLI argument parser."""
    parser = argparse.ArgumentParser(
        prog="orch",
        description=(
            "Local control plane for supervised multi-model AI engineering workflows with "
            "Git worktree isolation and deterministic gate verification."
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

    # orch scope: deterministic project-scope inspection and expansion.
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
        default=None,
        help=(
            "Explicit gate command for this run (default: follow the project "
            "gate setting; run creation fails before dispatch when the "
            "project has no gate configured or detected)."
        ),
    )
    run_parser.add_argument(
        "--tier",
        type=str,
        choices=["auto", "T1", "T2", "T3"],
        default=argparse.SUPPRESS,
        help="Deprecated (ADR-0002 §11): routing mode auto (default) or a manual tier override.",
    )
    run_parser.add_argument(
        "--max-tier",
        choices=["T1", "T2", "T3"],
        default=argparse.SUPPRESS,
        help="Deprecated (ADR-0002 §11): maximum automatic routing tier (default: T3).",
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
        default=None,
        dest="training_allowed",
        help=argparse.SUPPRESS,
    )
    training_group.add_argument(
        "--training-denied",
        action="store_false",
        dest="training_allowed",
        help=argparse.SUPPRESS,
    )
    # No implicit default.  ``None`` means "follow the persisted
    # project data policy" (unset project policy resolves conservatively
    # to Denied); the shared run-creation authority owns the default.
    run_parser.set_defaults(training_allowed=None)
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
            "Risk-adaptive review policy: 'risk-adaptive' (default; review "
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
            "$XDG_STATE_HOME/saberops/orchestrator.db or "
            "~/.local/state/saberops/orchestrator.db)."
        ),
    )
    run_parser.add_argument(
        "--worker-timeout",
        type=float,
        default=None,
        help="Worker timeout in seconds (default: role default WORKER/INTEGRATOR=1200, SURGEON=1800).",
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
    review_parser.add_argument(
        "--action-id",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
    )
    review_parser.add_argument(
        "--expected-candidate",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
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
    accept_parser.add_argument(
        "--action-id",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
    )
    accept_parser.add_argument(
        "--expected-candidate",
        type=str,
        default=None,
        help=argparse.SUPPRESS,
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

    # orch gc [--dry-run|--apply] [--class ...] [--json]
    gc_parser = subparsers.add_parser(
        "gc",
        help="Inventory owned storage artifacts and preview/apply the exact scoped GC.",
    )
    gc_mode = gc_parser.add_mutually_exclusive_group()
    gc_mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview only: no schema creation, no write, no deletion (default).",
    )
    gc_mode.add_argument(
        "--apply",
        action="store_true",
        help="Execute the exact previewed scope; requires a validated standing owner policy.",
    )
    gc_parser.add_argument(
        "--class",
        dest="artifact_class",
        action="append",
        default=None,
        choices=[cls.value for cls in ArtifactClass],
        help="Restrict to an artifact class (repeatable).",
    )
    gc_parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON.")
    gc_parser.add_argument(
        "--repo",
        dest="project_repo",
        type=str,
        default=None,
        help="Repository root to inventory (defaults to the current directory).",
    )
    gc_parser.add_argument(
        "--policy",
        type=str,
        default=None,
        help="Path to the standing GC policy file (defaults to the owner settings path).",
    )
    gc_parser.add_argument(
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
    plan_store_parser = subparsers.add_parser(
        "plan-store",
        help="Show or validate a stored versioned plan revision (Step 015 plan store).",
    )
    plan_store_sub = plan_store_parser.add_subparsers(dest="plan_store_command", required=True)
    plan_show = plan_store_sub.add_parser(
        "show", help="Show a stored versioned plan revision (Step 015 plan store)."
    )
    plan_show.add_argument("--project", type=str, required=True, help="Stable project ID.")
    plan_show.add_argument("--version", type=str, default=None, help="Plan version (default: active).")
    plan_show.add_argument("--store", type=str, default=None, help="Plan-store root directory.")
    plan_show.add_argument("--json", action="store_true", help="JSON output")
    plan_validate = plan_store_sub.add_parser(
        "validate", help="Validate a stored plan revision (Step 015 plan store)."
    )
    plan_validate.add_argument("--project", type=str, default=None, help="Stable project ID.")
    plan_validate.add_argument(
        "--all", action="store_true", help="Validate every project with an active revision."
    )
    plan_validate.add_argument("--version", type=str, default=None, help="Plan version (default: active).")
    plan_validate.add_argument("--store", type=str, default=None, help="Plan-store root directory.")
    plan_validate.add_argument("--json", action="store_true", help="JSON output")
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

    # orch recover <run-id>: dedicated supervisor cold-takeover child invocation.
    # Reached only from the supervisor's fenced-recovery launch path (see
    # ``RunSupervisor.ensure_recovery_running``); ``--db`` is the only knob
    # a human could conceivably pass, and even that is primarily for tests.
    recover_parser = subparsers.add_parser(
        "recover",
        help=(
            "Dedicated cold-takeover execution owner.  Internal: invoked "
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

    # orch report <run-id> [--json]
    report_parser = subparsers.add_parser(
        "report",
        help="Show the read-only run report (same evidence as observability).",
    )
    report_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    report_parser.add_argument(
        "--json",
        action="store_true",
        help="Emit canonical deterministic JSON instead of human-readable text.",
    )
    report_parser.add_argument(
        "--db", type=str, default=None, help="Path to SQLite database."
    )

    # orch monitor <run-id>
    monitor_parser = subparsers.add_parser(
        "monitor",
        help="Follow a run live using the run-report projection (read-only).",
    )
    monitor_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    monitor_parser.add_argument(
        "--db", type=str, default=None, help="Path to SQLite database."
    )
    monitor_parser.add_argument(
        "--interval",
        type=float,
        default=2.0,
        help="Refresh interval in seconds (default: 2.0).",
    )
    monitor_parser.add_argument(
        "--once",
        action="store_true",
        help="Render a single frame and exit instead of following the run.",
    )
    monitor_parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="Maximum follow time in seconds (default: 0 = no limit).",
    )

    # orch list
    list_parser = subparsers.add_parser("list", help="List recent SaberOps runs.")
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
        help="Strictly read-only preflight diagnostics for SaberOps readiness.",
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
            "$XDG_STATE_HOME/saberops/orchestrator.db or "
            "~/.local/state/saberops/orchestrator.db)."
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
        default=None,
        help="Optional initial target repository. Omit for an empty first-run UI.",
    )
    ui_parser.add_argument(
        "--profile",
        type=str,
        default=None,
        help=(
            "Optional isolated SaberOps owner profile. The profile gets its own "
            "state/config/cache namespace; project and model setup then happens in the UI."
        ),
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

    # orch routing show|validate|init|migrate|add|remove|move|reorder
    routing_parser = subparsers.add_parser(
        "routing",
        help="Manage routing configuration (show/validate/init/migrate/add/remove/move/reorder).",
    )
    routing_sub = routing_parser.add_subparsers(dest="routing_command", required=True)
    routing_show = routing_sub.add_parser("show", help="Show effective routing.")
    routing_show.add_argument("--json", action="store_true", help="JSON output")
    routing_sub.add_parser("validate", help="Validate routing config.")
    routing_init = routing_sub.add_parser("init", help="Initialize user routing config.")
    routing_init.add_argument("--force", action="store_true", help="Overwrite existing file.")
    routing_migrate = routing_sub.add_parser(
        "migrate", help="Migrate a schema-1/2 user routing file to schema 3."
    )
    routing_migrate.add_argument(
        "--write", action="store_true", help="Write the migrated schema-3 file (with backup)."
    )
    routing_add = routing_sub.add_parser("add", help="Add a candidate to a role chain.")
    routing_add.add_argument("role", type=str, help="Role: worker | surgeon | integrator.")
    routing_add.add_argument("candidate_id", type=str, help="Candidate id (provider/model).")
    routing_add.add_argument(
        "--group", type=int, default=None, help="1-based group to append into."
    )
    routing_remove = routing_sub.add_parser(
        "remove", help="Remove a candidate from a role chain."
    )
    routing_remove.add_argument("role", type=str, help="Role: worker | surgeon | integrator.")
    routing_remove.add_argument("candidate_id", type=str, help="Candidate id (provider/model).")
    routing_move = routing_sub.add_parser("move", help="Move a fallback group one place.")
    routing_move.add_argument("role", type=str, help="Role: worker | surgeon | integrator.")
    routing_move.add_argument("group", type=int, help="1-based group number.")
    routing_move.add_argument("direction", type=str, choices=["up", "down"])
    routing_reorder = routing_sub.add_parser(
        "reorder", help="Reorder a role's fallback groups (a permutation)."
    )
    routing_reorder.add_argument("role", type=str, help="Role: worker | surgeon | integrator.")
    routing_reorder.add_argument("order", type=int, nargs="+", help="1-based group numbers.")

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

    # orch reviewer show|select|clear
    reviewer_parser = subparsers.add_parser(
        "reviewer",
        help="Manage the owner-selected exact reviewer binding (show/select/clear).",
    )
    reviewer_sub = reviewer_parser.add_subparsers(dest="reviewer_command", required=True)
    reviewer_show = reviewer_sub.add_parser(
        "show", help="Show the reviewer selection and the eligible bindings."
    )
    reviewer_show.add_argument("--json", action="store_true", help="JSON output")
    reviewer_select = reviewer_sub.add_parser(
        "select", help="Persist the owner's exact reviewer binding choice."
    )
    reviewer_select.add_argument(
        "binding_id",
        type=str,
        help="Exact REVIEWER-role binding id from 'orch reviewer show'.",
    )
    reviewer_select.add_argument("--json", action="store_true", help="JSON output")
    reviewer_clear = reviewer_sub.add_parser(
        "clear", help="Remove the reviewer selection (legacy review ladder applies)."
    )
    reviewer_clear.add_argument("--json", action="store_true", help="JSON output")

    # orch orchestrator show|select|clear
    orch_parser = subparsers.add_parser(
        "orchestrator",
        help="Manage the owner-selected orchestrator binding (show/select/clear).",
    )
    orch_sub = orch_parser.add_subparsers(dest="orchestrator_command", required=True)
    orch_show = orch_sub.add_parser(
        "show", help="Show the orchestrator policy and the eligible bindings."
    )
    orch_show.add_argument("--json", action="store_true", help="JSON output")
    orch_select = orch_sub.add_parser(
        "select", help="Persist an orchestrator binding policy over exact binding identity."
    )
    orch_mode = orch_select.add_mutually_exclusive_group(required=True)
    orch_mode.add_argument(
        "--binding",
        type=str,
        default=None,
        help="Exact ORCHESTRATOR-role binding id (PINNED mode).",
    )
    orch_mode.add_argument(
        "--auto",
        action="store_true",
        help="Resolve through the existing routing authority (AUTO mode).",
    )
    orch_select.add_argument("--json", action="store_true", help="JSON output")
    orch_clear = orch_sub.add_parser(
        "clear", help="Remove the orchestrator policy (no pinned selection)."
    )
    orch_clear.add_argument("--json", action="store_true", help="JSON output")

    # orch access list|show|add|test|discover|verify-model
    access_parser = subparsers.add_parser(
        "access",
        help="Manage model-access connections (list/show/add/test/discover/verify-model).",
    )
    access_sub = access_parser.add_subparsers(dest="access_command", required=True)
    access_list = access_sub.add_parser("list", help="List connections with readiness.")
    access_list.add_argument("--json", action="store_true", help="JSON output")
    access_show = access_sub.add_parser("show", help="Show one connection and its evidence.")
    access_show.add_argument("connection_id", type=str, help="Connection id.")
    access_show.add_argument("--json", action="store_true", help="JSON output")
    access_add = access_sub.add_parser(
        "add", help="Save a user-owned API connection (credential reference only, no secret)."
    )
    access_add.add_argument("--id", dest="connection_id", type=str, required=True)
    access_add.add_argument(
        "--preset",
        type=str,
        default="custom-openai-compatible",
        help="Provider preset id (default: custom-openai-compatible).",
    )
    access_add.add_argument("--protocol", type=str, default="OPENAI_COMPATIBLE")
    access_add.add_argument("--endpoint", type=str, default="")
    access_add.add_argument(
        "--credential-ref",
        type=str,
        default="",
        help="Credential handle (env:/profile:/store:), never a secret value.",
    )
    access_add.add_argument("--auth-header-name", type=str, default="")
    access_add.add_argument("--model", type=str, default="")
    access_add.add_argument(
        "--public-header",
        dest="public_headers",
        action="append",
        default=[],
        metavar="NAME: value",
        help="Literal public metadata header; repeat for multiple headers.",
    )
    access_add.add_argument("--json", action="store_true", help="JSON output")
    access_test = access_sub.add_parser(
        "test", help="Verify one connection (non-inference status check only)."
    )
    access_test.add_argument("connection_id", type=str, help="Connection id.")
    access_test.add_argument("--json", action="store_true", help="JSON output")
    access_discover = access_sub.add_parser(
        "discover", help="Discover the models one connection enumerates (evidence only)."
    )
    access_discover.add_argument("connection_id", type=str, help="Connection id.")
    access_discover.add_argument("--json", action="store_true", help="JSON output")
    access_verify = access_sub.add_parser(
        "verify-model",
        help="Explicit bounded model check for one account connection (owner-authorized).",
    )
    access_verify.add_argument("connection_id", type=str, help="Connection id.")
    access_verify.add_argument(
        "--model", type=str, required=True, help="Exact current catalog model id."
    )
    access_verify.add_argument("--json", action="store_true", help="JSON output")

    # orch plans show|validate + orch posture show|verify
    plans_parser = subparsers.add_parser(
        "plans",
        help="Show or validate the plan registry (accounts, pools, postures).",
    )
    plans_sub = plans_parser.add_subparsers(dest="plans_command", required=True)
    plans_show = plans_sub.add_parser("show", help="Show effective plans with sources.")
    plans_show.add_argument("--json", action="store_true", help="JSON output")
    plans_validate = plans_sub.add_parser("validate", help="Validate plans.")
    plans_validate.add_argument(
        "--file", type=str, default=None, help="Control-plane document to validate."
    )
    plans_validate.add_argument("--json", action="store_true", help="JSON output")

    posture_parser = subparsers.add_parser(
        "posture",
        help="Show or verify billing posture (show/verify).",
    )
    posture_sub = posture_parser.add_subparsers(dest="posture_command", required=True)
    posture_show = posture_sub.add_parser(
        "show", help="Show per-selector posture state."
    )
    posture_show.add_argument("--json", action="store_true", help="JSON output")
    posture_verify = posture_sub.add_parser(
        "verify", help="Record the owner's re-verification of one selector."
    )
    posture_verify.add_argument("selector", type=str, help="Posture selector.")
    posture_verify.add_argument(
        "--url",
        dest="urls",
        action="append",
        default=[],
        metavar="URL",
        help="Policy URL; repeat for multiple URLs.",
    )
    posture_verify.add_argument("--note", type=str, default=None, help="Owner note.")
    posture_verify.add_argument(
        "--class",
        dest="posture_class",
        type=str,
        choices=["A", "B", "C", "D", "X"],
        default=None,
        help="Posture class (required for a new selector or a class change).",
    )
    posture_verify.add_argument(
        "--date",
        type=str,
        default=None,
        help="Verification date YYYY-MM-DD (default: today, local).",
    )
    posture_verify.add_argument(
        "--ttl-days",
        dest="ttl_days",
        type=int,
        default=None,
        help="Make this confirmation expire after N days (1-90). Default: never expires.",
    )
    posture_verify.add_argument("--json", action="store_true", help="JSON output")

    # orch questions ask|list|show|answer|defer|moot|diverge|divergences|
    # standing-rules|audit (Q-1 owner question queue).
    questions_parser = subparsers.add_parser(
        "questions",
        help="Owner question queue: ask, answer (verbatim), defer, moot and report (Q-1).",
    )
    questions_sub = questions_parser.add_subparsers(dest="questions_command", required=True)

    def _add_store_arg(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--store",
            type=str,
            default=None,
            help=(
                "Path to the question document (default: "
                "$XDG_STATE_HOME/saberops/questions.json)."
            ),
        )

    questions_ask = questions_sub.add_parser(
        "ask",
        help="Raise one keyed question with its five-part escalation.",
    )
    questions_ask.add_argument("--key", type=str, required=True, help="Idempotency key.")
    questions_ask.add_argument("--subject", type=str, required=True, help="The question.")
    questions_ask.add_argument(
        "--kind", type=str, required=True, help="Question kind (groups standing rules)."
    )
    questions_ask.add_argument(
        "--decision-class",
        dest="decision_class",
        type=str,
        required=True,
        choices=[item.value for item in DecisionClass],
        help="Decision class; the answerable authority is derived from it.",
    )
    questions_ask.add_argument(
        "--role",
        type=str,
        required=True,
        choices=[item.value for item in AskRole],
        help="Role raising the question.",
    )
    questions_ask.add_argument(
        "--by", dest="raised_by", type=str, required=True, help="Actor raising it."
    )
    questions_ask.add_argument("--scope", type=str, default=None, help="Scope it belongs to.")
    questions_ask.add_argument("--run-id", dest="run_id", type=str, default=None)
    questions_ask.add_argument(
        "--blocks",
        action="append",
        default=[],
        metavar="SCOPE",
        help="What the question blocks; repeat for multiple.",
    )
    questions_ask.add_argument(
        "--option",
        dest="options",
        action="append",
        default=[],
        metavar="OPTION",
        help="An allowed answer option; repeat for multiple.",
    )
    questions_ask.add_argument("--default-option", dest="default_option", type=str, default=None)
    questions_ask.add_argument(
        "--supersedes", type=str, default=None, help="Question id/key this one supersedes."
    )
    questions_ask.add_argument(
        "--requirement", type=str, required=True, help="Escalation 1/5: forcing requirement."
    )
    questions_ask.add_argument(
        "--expansion",
        dest="proposed_expansion",
        type=str,
        required=True,
        help="Escalation 2/5: proposed expansion.",
    )
    questions_ask.add_argument(
        "--smallest",
        dest="smallest_compliant",
        type=str,
        required=True,
        help="Escalation 3/5: smallest compliant option.",
    )
    questions_ask.add_argument(
        "--consequences",
        type=str,
        required=True,
        help="Escalation 4/5: concrete consequences.",
    )
    questions_ask.add_argument(
        "--recommendation",
        type=str,
        required=True,
        help="Escalation 5/5: Orchestrator recommendation.",
    )
    questions_ask.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_ask)

    questions_list = questions_sub.add_parser("list", help="List questions with their status.")
    questions_list.add_argument(
        "--status",
        type=str,
        default=None,
        choices=[item.value for item in QuestionStatus],
        help="Restrict to one status.",
    )
    questions_list.add_argument(
        "--as-of",
        dest="as_of",
        type=str,
        default=None,
        help="Reference date YYYY-MM-DD (default: today) for deferred resurfacing.",
    )
    questions_list.add_argument("--run-id", dest="run_id", type=str, default=None)
    questions_list.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_list)

    questions_show = questions_sub.add_parser("show", help="Show one question in full.")
    questions_show.add_argument("ref", type=str, help="Question key or id.")
    questions_show.add_argument(
        "--as-of", dest="as_of", type=str, default=None, help="Reference date YYYY-MM-DD."
    )
    questions_show.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_show)

    questions_answer = questions_sub.add_parser(
        "answer", help="Answer a question, storing the owner's exact words."
    )
    questions_answer.add_argument("ref", type=str, help="Question key or id.")
    questions_answer.add_argument(
        "--words", type=str, required=True, help="The owner's exact words (stored verbatim)."
    )
    questions_answer.add_argument(
        "--authority",
        type=str,
        default="owner",
        choices=["owner", "orchestrator"],
        help="Answer authority (default: owner).",
    )
    questions_answer.add_argument("--by", dest="answered_by", type=str, default=None)
    questions_answer.add_argument(
        "--role", type=str, default=None, choices=[item.value for item in AskRole]
    )
    questions_answer.add_argument("--chosen-option", dest="chosen_option", type=str, default=None)
    questions_answer.add_argument(
        "--evidence", type=str, default=None, help="Required for an Orchestrator-alone answer."
    )
    questions_answer.add_argument(
        "--rejected-alternative",
        dest="rejected_alternative",
        type=str,
        default=None,
        help="Required for an Orchestrator-alone answer.",
    )
    questions_answer.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_answer)

    questions_defer = questions_sub.add_parser(
        "defer", help="Defer a question until a date; it resurfaces then."
    )
    questions_defer.add_argument("ref", type=str, help="Question key or id.")
    questions_defer.add_argument("--until", type=str, required=True, help="Resurface date YYYY-MM-DD.")
    questions_defer.add_argument("--by", dest="deferred_by", type=str, default="owner")
    questions_defer.add_argument(
        "--role",
        type=str,
        default="OWNER",
        choices=[AskRole.OWNER.value, AskRole.ORCHESTRATOR.value],
    )
    questions_defer.add_argument("--note", type=str, default="")
    questions_defer.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_defer)

    questions_moot = questions_sub.add_parser(
        "moot", help="Close a question as moot on explicit evidence."
    )
    questions_moot.add_argument("ref", type=str, help="Question key or id.")
    questions_moot.add_argument(
        "--evidence", type=str, required=True, help="Evidence proving the question is moot."
    )
    questions_moot.add_argument("--by", dest="closed_by", type=str, default="owner")
    questions_moot.add_argument(
        "--role",
        type=str,
        default=AskRole.OWNER.value,
        choices=[
            AskRole.OWNER.value,
            AskRole.ORCHESTRATOR.value,
            AskRole.ENGINE.value,
        ],
    )
    questions_moot.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_moot)

    questions_diverge = questions_sub.add_parser(
        "diverge", help="Record a conflicting later record (never auto-closes)."
    )
    questions_diverge.add_argument("ref", type=str, help="Question key or id.")
    questions_diverge.add_argument(
        "--reason",
        type=str,
        required=True,
        choices=[
            "DIFFERENT_WORDS",
            "DIFFERENT_OPTION",
            "ANSWER_AFTER_MOOT",
            "MOOT_AFTER_ANSWER",
            "DIFFERENT_EVIDENCE",
        ],
    )
    questions_diverge.add_argument("--words", type=str, required=True)
    questions_diverge.add_argument("--by", dest="recorded_by", type=str, required=True)
    questions_diverge.add_argument(
        "--role", type=str, required=True, choices=[item.value for item in AskRole]
    )
    questions_diverge.add_argument("--chosen-option", dest="chosen_option", type=str, default=None)
    questions_diverge.add_argument("--note", type=str, default="")
    questions_diverge.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_diverge)

    questions_divergences = questions_sub.add_parser(
        "divergences", help="Report every divergent record."
    )
    questions_divergences.add_argument("ref", type=str, nargs="?", default=None)
    questions_divergences.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_divergences)

    questions_standing = questions_sub.add_parser(
        "standing-rules",
        help="Propose CFG-1 standing rules from repeated same-kind owner answers.",
    )
    questions_standing.add_argument(
        "--threshold", type=int, default=STANDING_RULE_THRESHOLD, help="Repeat count (default: 3)."
    )
    questions_standing.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_standing)

    questions_audit = questions_sub.add_parser(
        "audit", help="Audit list of every Orchestrator-alone decision."
    )
    questions_audit.add_argument("--json", action="store_true", help="JSON output")
    _add_store_arg(questions_audit)

    # orch job submit|get|list|cancel (M2-3 job queue; each command calls
    # exactly one service function in saberops.jobs.service).
    job_parser = subparsers.add_parser(
        "job",
        help="Submit and inspect durable jobs (queue until the scheduler lands).",
    )
    job_sub = job_parser.add_subparsers(dest="job_command", required=True)
    job_submit = job_sub.add_parser("submit", help="Submit one job.")
    job_submit.add_argument("--repo", type=str, required=True, help="Path to git work tree.")
    job_submit.add_argument("--goal", type=str, required=True, help="Work to be done.")
    job_submit.add_argument("--acceptance", type=str, default=None, help="Acceptance text.")
    job_submit.add_argument(
        "--base-ref", type=str, default="HEAD", help="Base ref resolved to a commit."
    )
    job_submit.add_argument(
        "--role",
        type=str,
        default="WORKER",
        choices=["ORCHESTRATOR", "WORKER", "INTEGRATOR", "SURGEON", "REVIEWER"],
        help="Job role (only WORKER is dispatchable until R-002).",
    )
    job_submit.add_argument(
        "--effort", type=str, default=None, choices=["low", "medium", "high"]
    )
    job_submit.add_argument(
        "--capability",
        dest="capabilities",
        action="append",
        default=[],
        metavar="NAME",
        help="Required capability; repeat for multiple.",
    )
    job_submit.add_argument("--read-only", action="store_true", help="Require read-only.")
    job_submit.add_argument(
        "--family-include",
        dest="family_include",
        action="append",
        default=[],
        metavar="F",
        help="Model family allowlist entry; repeat for multiple.",
    )
    job_submit.add_argument(
        "--family-exclude",
        dest="family_exclude",
        action="append",
        default=[],
        metavar="F",
        help="Model family denylist entry; repeat for multiple.",
    )
    job_submit.add_argument("--size", type=str, default=None, choices=["S", "M", "L"])
    job_submit.add_argument(
        "--priority",
        type=str,
        default="NORMAL",
        choices=["URGENT", "NORMAL", "BACKGROUND"],
    )
    job_submit.add_argument("--deadline", type=str, default=None, help="Aware ISO-8601.")
    job_submit.add_argument("--not-before", type=str, default=None, help="Aware ISO-8601.")
    job_submit.add_argument("--idempotency-key", type=str, default=None)
    job_submit.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    job_submit.add_argument("--json", action="store_true", help="JSON output")
    job_get = job_sub.add_parser("get", help="Show one job.")
    job_get.add_argument("job_id", type=str, help="Job identifier.")
    job_get.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    job_get.add_argument("--json", action="store_true", help="JSON output")
    job_list = job_sub.add_parser("list", help="List jobs in queue order.")
    job_list.add_argument(
        "--state",
        type=str,
        default=None,
        choices=["QUEUED", "RUNNING", "SUCCEEDED", "FAILED", "CANCELLED"],
    )
    job_list.add_argument("--limit", type=int, default=50, help="Max jobs (default: 50).")
    job_list.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    job_list.add_argument("--json", action="store_true", help="JSON output")
    job_cancel = job_sub.add_parser("cancel", help="Cancel one job.")
    job_cancel.add_argument("job_id", type=str, help="Job identifier.")
    job_cancel.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    job_cancel.add_argument("--json", action="store_true", help="JSON output")

    # orch scheduler run [--once] [--interval-seconds INT]
    scheduler_parser = subparsers.add_parser(
        "scheduler",
        help="Run the pacing scheduler loop (ADR-0002 D7).",
    )
    scheduler_sub = scheduler_parser.add_subparsers(
        dest="scheduler_command", required=True
    )
    scheduler_run = scheduler_sub.add_parser("run", help="Run the pacing scheduler loop.")
    scheduler_run.add_argument(
        "--once", action="store_true", help="Run one scheduler tick and exit."
    )
    scheduler_run.add_argument(
        "--interval-seconds",
        type=float,
        default=60.0,
        help="Seconds between ticks (default: 60.0).",
    )
    scheduler_run.add_argument(
        "--max-concurrent",
        type=_positive_int_arg,
        default=None,
        metavar="N",
        help=(
            "Lower the effective owner global ceiling on concurrently RUNNING"
            " top-level jobs for this run (aggregate admission only; cannot"
            " raise the owner-configured ceiling)."
        ),
    )
    scheduler_run.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    scheduler_run.add_argument("--json", action="store_true", help="JSON output")

    # orch capacity show [--json]
    capacity_parser = subparsers.add_parser(
        "capacity",
        help="Inspect ledger capacity pools and estimates (read-only).",
    )
    capacity_sub = capacity_parser.add_subparsers(
        dest="capacity_command", required=True
    )
    capacity_show = capacity_sub.add_parser(
        "show", help="Show capacity pools and ledger estimates."
    )
    capacity_show.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    capacity_show.add_argument("--json", action="store_true", help="JSON output")

    # orch retry|continue|try-worker
    retry_parser = subparsers.add_parser(
        "retry", help="Start a fresh attempt of a run's frozen task and policy."
    )
    retry_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    retry_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    retry_parser.add_argument("--json", action="store_true", help="JSON output")
    continue_parser = subparsers.add_parser(
        "continue", help="Continue a failed run from its immutable saved candidate."
    )
    continue_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    continue_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    continue_parser.add_argument("--json", action="store_true", help="JSON output")
    try_worker_parser = subparsers.add_parser(
        "try-worker",
        help="Explicitly retry a failed run with one blocked UNKNOWN worker.",
    )
    try_worker_parser.add_argument("run_id", type=str, help="Run identifier (e.g. run_abc123).")
    try_worker_parser.add_argument(
        "--binding", type=str, required=True, help="Exact blocked worker binding id."
    )
    try_worker_parser.add_argument("--db", type=str, default=None, help="Path to SQLite database.")
    try_worker_parser.add_argument("--json", action="store_true", help="JSON output")

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
        help="Project-scoped settings (gate, data policy, review policy, target branch, gate-scratch).",
    )
    project_sub = project_parser.add_subparsers(dest="project_command", required=True)

    def _add_repo_arg(parser: argparse.ArgumentParser) -> None:
        parser.add_argument(
            "--repo",
            dest="project_repo",
            type=str,
            default=None,
            help="Absolute path to the target Git repository.",
        )

    def _add_json_arg(parser: argparse.ArgumentParser) -> None:
        parser.add_argument("--json", action="store_true", help="JSON output")

    project_show = project_sub.add_parser(
        "show",
        help="Show the effective project settings (gate, data policy, review policy, target branch, gate storage).",
    )
    _add_repo_arg(project_show)
    _add_json_arg(project_show)

    project_list = project_sub.add_parser(
        "list",
        help="List explicitly registered stable projects (Step 015 registry).",
    )
    project_list.add_argument(
        "--registry", type=str, default=None, help="Registry file path."
    )
    _add_json_arg(project_list)

    project_register = project_sub.add_parser(
        "register",
        help="Register one stable project with explicit identity (Step 015 registry).",
    )
    project_register.add_argument("--id", type=str, required=True, help="Stable project slug.")
    project_register.add_argument("--name", type=str, required=True, help="Display name.")
    project_register.add_argument("--repo", type=str, required=True, help="Repository root path.")
    project_register.add_argument("--state-dir", type=str, required=True, help="State root path.")
    project_register.add_argument(
        "--active", action="store_true", help="Activate immediately (enforces the 3-project cap)."
    )
    project_register.add_argument(
        "--registry", type=str, default=None, help="Registry file path."
    )
    _add_json_arg(project_register)

    project_active = project_sub.add_parser(
        "active",
        help="List currently active registered projects (Step 015 registry).",
    )
    project_active.add_argument(
        "--registry", type=str, default=None, help="Registry file path."
    )
    _add_json_arg(project_active)

    project_activate = project_sub.add_parser(
        "activate",
        help="Mark a registered project active (enforces the 3-project cap).",
    )
    project_activate.add_argument("--id", type=str, required=True, help="Stable project slug.")
    project_activate.add_argument(
        "--registry", type=str, default=None, help="Registry file path."
    )
    _add_json_arg(project_activate)

    project_deactivate = project_sub.add_parser(
        "deactivate",
        help="Mark a registered project inactive.",
    )
    project_deactivate.add_argument("--id", type=str, required=True, help="Stable project slug.")
    project_deactivate.add_argument(
        "--registry", type=str, default=None, help="Registry file path."
    )
    _add_json_arg(project_deactivate)

    project_set_gate = project_sub.add_parser(
        "set-gate",
        help="Persist the project's gate command (explicit owner decision).",
    )
    _add_repo_arg(project_set_gate)
    project_set_gate.add_argument(
        "command",
        type=str,
        help="Exact gate command to freeze into future runs (e.g. 'make gate').",
    )

    project_clear_gate = project_sub.add_parser(
        "clear-gate",
        help="Remove the project's explicit gate command (returns to unset).",
    )
    _add_repo_arg(project_clear_gate)

    project_set_data = project_sub.add_parser(
        "set-data-policy",
        help="Persist the project's training/data-use policy (allowed|denied).",
    )
    _add_repo_arg(project_set_data)
    project_set_data.add_argument(
        "policy",
        type=str,
        choices=["allowed", "denied"],
        help="Owner decision for training/data use in future runs.",
    )

    project_set_review = project_sub.add_parser(
        "set-review-policy",
        help="Persist the project's review policy (risk-adaptive|always-required).",
    )
    _add_repo_arg(project_set_review)
    project_set_review.add_argument(
        "policy",
        type=str,
        choices=["risk-adaptive", "always-required"],
        help="Tightening-only review policy for future runs.",
    )

    project_set_branch = project_sub.add_parser(
        "set-target-branch",
        help="Persist the owner-selectable accept target branch.",
    )
    _add_repo_arg(project_set_branch)
    project_set_branch.add_argument(
        "branch",
        type=str,
        help="Branch name frozen as the accept target of future runs.",
    )

    project_clear_branch = project_sub.add_parser(
        "clear-target-branch",
        help="Remove the explicit accept target branch (returns to unset).",
    )
    _add_repo_arg(project_clear_branch)

    project_set_workflow = project_sub.add_parser(
        "set-workflow",
        help="Persist a workflow preset (quick|balanced|thorough|max) or custom settings.",
    )
    _add_repo_arg(project_set_workflow)
    project_set_workflow.add_argument(
        "preset",
        type=str,
        choices=["quick", "balanced", "thorough", "max", "custom"],
        help="Workflow preset, or 'custom' to persist the explicit flags.",
    )
    project_set_workflow.add_argument(
        "--gate-command",
        type=str,
        default=None,
        help="Gate command to persist first (required for non-quick presets without a gate).",
    )
    gate_group = project_set_workflow.add_mutually_exclusive_group()
    gate_group.add_argument(
        "--gate-enabled", dest="gate_enabled", action="store_true", default=None
    )
    gate_group.add_argument(
        "--gate-disabled", dest="gate_enabled", action="store_false", default=None
    )
    project_set_workflow.add_argument(
        "--review",
        type=str,
        choices=["disabled", "risk-adaptive", "always-required"],
        default=None,
    )
    project_set_workflow.add_argument(
        "--mode", type=str, choices=["bounded", "max"], default=None
    )
    project_set_workflow.add_argument("--limit", type=int, default=None)
    _add_json_arg(project_set_workflow)
    project_gate_scratch = project_sub.add_parser(
        "gate-scratch",
        help="Project gate temporary storage (DISK / RAM_PREFERRED / RAM_REQUIRED).",
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

    # orch mcp [serve|tools] --transport stdio
    mcp_parser = subparsers.add_parser(
        "mcp",
        help="Expose the Orchestrator contract tools over MCP (stdio transport).",
        description=(
            "Serve the SaberOps Orchestrator contract tools (submit_job, get_job, "
            "list_jobs, cancel_job, capacity, submit_wave, wave_status, read_evidence, "
            "list_questions, answer_question, propose_config_change, list_proposals) "
            "over the Model Context Protocol. Owner-only operations are never exposed."
        ),
    )
    mcp_parser.add_argument(
        "mcp_action",
        nargs="?",
        default="serve",
        choices=["serve", "tools", "on", "off", "status"],
        help=(
            "serve the MCP server (default), list the exposed tools, or switch agent "
            "access on/off (off keeps every agent registered but SaberOps offers nothing)."
        ),
    )
    mcp_parser.add_argument(
        "--profile", type=str, default=None, help="SaberOps profile whose switch/lease to use."
    )
    mcp_parser.add_argument(
        "--active",
        action="store_true",
        help="Start the session with SaberOps on (default: off until the user turns it on).",
    )
    mcp_parser.add_argument(
        "--transport",
        type=str,
        default="stdio",
        choices=["stdio"],
        help="Transport to serve (only stdio is implemented).",
    )
    mcp_parser.add_argument("--db", type=str, default=None, help="Owner database path.")
    mcp_parser.add_argument(
        "--lease", type=str, default=None, help="Orchestrator lease file path."
    )
    mcp_parser.add_argument(
        "--ledger", type=str, default=None, help="Append-only event ledger path."
    )
    mcp_parser.add_argument(
        "--questions-store", dest="questions_store", type=str, default=None
    )
    mcp_parser.add_argument(
        "--config-ledger", dest="config_ledger", type=str, default=None
    )
    mcp_parser.add_argument(
        "--cwd",
        type=str,
        default=None,
        help="Base directory used to discover the .orch/.saberops evidence roots.",
    )
    mcp_parser.add_argument("--json", action="store_true", help="JSON output (tools).")

    # orch connect [agent ...] (register SaberOps as an MCP server in agent CLIs).
    connect_parser = subparsers.add_parser(
        "connect",
        help="Connect SaberOps to coding-agent CLIs (Claude Code, Codex, ...) over MCP.",
        description=(
            "Register `orch mcp serve` with each agent's own MCP settings. With no agent "
            "named, lists the agents found on this computer and whether each is connected."
        ),
    )
    connect_parser.add_argument(
        "agents", nargs="*", help="claude, codex, antigravity, opencode, cursor, or all."
    )
    connect_parser.add_argument(
        "--remove", action="store_true", help="Disconnect instead of connect."
    )
    connect_parser.add_argument(
        "--project",
        nargs="?",
        const=".",
        default=None,
        help="Connect only for this project folder (default: current folder).",
    )
    connect_parser.add_argument(
        "--dry-run", action="store_true", help="Show what would change; change nothing."
    )
    connect_parser.add_argument(
        "--profile", type=str, default=None, help="SaberOps profile the agents should use."
    )
    connect_parser.add_argument("--json", action="store_true", help="JSON output.")

    # orch certify check|run|report|approve (END production certification).
    certify_parser = subparsers.add_parser(
        "certify",
        help="Evaluate END production certification for self-dogfooding.",
    )
    certify_sub = certify_parser.add_subparsers(dest="certify_command", required=True)
    certify_check = certify_sub.add_parser(
        "check", help="Read-only evaluation of the certification checklist and Q4."
    )
    certify_check.add_argument(
        "--evidence", type=str, default=None, help="JSON evidence file (default: none)."
    )
    certify_check.add_argument("--json", action="store_true", help="JSON output")
    certify_run = certify_sub.add_parser(
        "run", help="Execute the certification suite and scenario verifications."
    )
    certify_run.add_argument(
        "--evidence", type=str, default=None, help="JSON evidence file (default: none)."
    )
    certify_run.add_argument(
        "--scenario",
        type=str,
        default=None,
        choices=[
            "mid-turn-kill",
            "context-overflow",
            "second-session",
            "dead-worker",
            "gate-protection",
            "standing-scope",
            "morning-report",
            "stall-alarm",
            "idea-intake",
        ],
        help="Restrict output to one resilience scenario.",
    )
    certify_run.add_argument("--json", action="store_true", help="JSON output")
    certify_report = certify_sub.add_parser(
        "report", help="Generate a human-readable markdown certification report."
    )
    certify_report.add_argument(
        "--evidence", type=str, default=None, help="JSON evidence file (default: none)."
    )
    certify_report.add_argument("--out", type=str, default=None, help="Output file.")
    certify_approve = certify_sub.add_parser(
        "approve", help="Owner-only authorization to start self-dogfooding."
    )
    certify_approve.add_argument(
        "--evidence", type=str, default=None, help="JSON evidence file (default: none)."
    )
    certify_approve.add_argument(
        "--token", type=str, default=None, help="Explicit owner approval token."
    )
    certify_approve.add_argument(
        "--record", type=str, default=None, help="Approval record file (JSONL)."
    )
    certify_approve.add_argument("--note", type=str, default="", help="Optional note.")

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
    _warn_deprecated_routing_flags(args)
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
                        config = _resolve_cli_run_config(args, args.repo)
                    except RunCreationError as exc:
                        print(f"[!] {exc}", file=sys.stderr)
                        return 1
            except RunCreationError as exc:
                print(f"[!] {exc}", file=sys.stderr)
                return 1
            except Exception as exc:
                print(f"[!] Error during run execution: {exc}", file=sys.stderr)
                return 1
            print(f"[*] Adopting PENDING run : {run_id_clean}")
            print(f"[*] Target repository : {config.target_repo}")
            print(f"[*] Task              : {config.task}")
            print(f"[*] Gate command      : {config.gate_command}")
            print(f"[*] Routing           : {config.routing_mode} ({config.initial_tier.value})")
            print(f"[*] Review policy     : {config.review_policy_mode.value}")
            print(f"[*] Gate storage      : {config.gate_scratch_mode.value}")
            print(
                f"[*] Target branch     : {config.accept_target_branch or '(unknown legacy)'}"
            )
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
    # The CLI resolves run policy exclusively through the shared
    # run-creation authority (same precedence as Web's POST /runs).
    try:
        config = _resolve_cli_run_config(args, args.repo)
    except RunCreationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"[*] Target repository : {config.target_repo}")
    print(f"[*] Task              : {config.task}")
    print(f"[*] Gate command      : {config.gate_command}")
    print(f"[*] Routing           : {config.routing_mode} ({config.initial_tier.value})")
    print(f"[*] Review policy     : {config.review_policy_mode.value}")
    print(f"[*] Gate storage      : {config.gate_scratch_mode.value}")
    print(f"[*] Target branch     : {config.accept_target_branch or '(unknown legacy)' }")
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
    action_id = getattr(args, "action_id", None)
    expected_candidate = getattr(args, "expected_candidate", None)
    if action_id:
        # Detached owner-action child: fence on the exact requested
        # candidate, then delegate to the existing service authority.
        # The explicit --db (handed by the launcher) always wins so the
        # child can never be retargeted to another project store.
        from saberops.owner_actions import run_review_action_child

        return run_review_action_child(
            db=db_for_run(args, args.run_id),
            action_id=str(action_id),
            expected_candidate_sha=str(expected_candidate or ""),
            run_id=str(args.run_id),
            timeout=args.timeout,
        )
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
    action_id = getattr(args, "action_id", None)
    expected_candidate = getattr(args, "expected_candidate", None)
    if action_id:
        # Detached owner-action child: fence on the exact requested
        # candidate, then delegate to the existing service authority.
        # The AcceptEngine re-checks every canonical condition immediately
        # before mutation; the Web pre-check stays advisory only.
        from saberops.owner_actions import run_accept_action_child

        return run_accept_action_child(
            db=db_for_run(args, args.run_id),
            action_id=str(action_id),
            expected_candidate_sha=str(expected_candidate or ""),
            run_id=str(args.run_id),
            gate_timeout=args.gate_timeout,
        )
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
    if not getattr(args, "run_id", None):
        print("[!] orch plan requires a run_id (or the show|validate subcommand)", file=sys.stderr)
        return 2
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


def _plan_store_for(args: argparse.Namespace) -> PlanStore:
    """Resolve the Step 015 plan store for CLI args."""
    raw = str(getattr(args, "store", "") or "").strip()
    if raw:
        return PlanStore(Path(raw).expanduser())
    return PlanStore()


def handle_plan_show(args: argparse.Namespace) -> int:
    """Handle ``orch plan show`` (stored versioned plan revision)."""
    from saberops.project_scope.plan_store import PlanNotFoundError, PlanTamperError

    try:
        store = _plan_store_for(args)
        project_id = str(args.project)
        version = str(args.version) if getattr(args, "version", None) else None
        revision = (
            store.load_active(project_id) if version is None else store.get_revision(project_id, version)
        )
    except (PlanNotFoundError, PlanTamperError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(revision.as_dict(), indent=2, sort_keys=True))
        return 0
    print(f"project: {revision.project_id}  version: {revision.version}")
    print(f"created: {revision.created_at}  sha256: {revision.sha256[:16]}...")
    if not revision.tasks:
        print("  (no tasks)")
        return 0
    for task in revision.tasks:
        print(f"  {task.task_id} [{task.phase}] {task.title}")
        for dep in task.start_dependencies:
            print(f"    needs: {dep}")
        for prereq in task.integration_prerequisites:
            print(f"    integrates-after: {prereq}")
    return 0


def handle_plan_validate(args: argparse.Namespace) -> int:
    """Handle ``orch plan validate`` (exit 2 on validation errors)."""
    from saberops.project_scope.plan_store import (
        PlanNotFoundError,
        PlanStoreError,
        PlanTamperError,
        PlanValidationError,
        plan_revisions_to_snapshot,
    )
    from saberops.ready_frontier import FrontierRequest, project_ready_frontier

    try:
        store = _plan_store_for(args)
        if getattr(args, "all", False):
            root_entries = (
                sorted(entry.name for entry in store.root.iterdir() if entry.is_dir())
                if store.root.is_dir()
                else []
            )
            targets = [
                project_id
                for project_id in root_entries
                if store.active_version(project_id) is not None
            ]
            if not targets:
                raise PlanNotFoundError("no project has an active plan revision")
            revisions = [store.load_active(project_id) for project_id in targets]
            snapshot = plan_revisions_to_snapshot(revisions, owner_id="cli")
            projection = project_ready_frontier(FrontierRequest(snapshot=snapshot))
            ok = projection.projected
            errors = [] if ok else list(projection.reason_codes())
        else:
            project_id = str(getattr(args, "project", "") or "").strip()
            if not project_id:
                raise PlanValidationError("--project is required (or pass --all)")
            version = str(args.version) if getattr(args, "version", None) else None
            revision = (
                store.load_active(project_id)
                if version is None
                else store.get_revision(project_id, version)
            )
            snapshot = plan_revisions_to_snapshot([revision], owner_id="cli")
            projection = project_ready_frontier(FrontierRequest(snapshot=snapshot))
            ok = revision.verify()
            errors = (
                []
                if ok and projection.projected
                else ([f"hash mismatch for {revision.version!r}"] if not ok else [])
                + ([] if projection.projected else list(projection.reason_codes()))
            )
    except (PlanNotFoundError, PlanTamperError, PlanValidationError, PlanStoreError) as exc:
        if getattr(args, "json", False):
            print(json.dumps({"ok": False, "errors": [str(exc)]}, indent=2, sort_keys=True))
        else:
            print(f"[!] {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps({"ok": ok, "errors": errors}, indent=2, sort_keys=True))
        return 0 if ok else 2
    if ok:
        print("Plan valid.")
        return 0
    for error in errors:
        print(f"[!] {error}", file=sys.stderr)
    return 2


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
    from saberops.supervisor import RunSupervisor

    db = db_for_run(args, args.run_id)
    try:
        result = RunSupervisor(db=db).reconcile(args.run_id, dry_run=args.dry_run)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


def handle_recover(args: argparse.Namespace) -> int:
    """Handle ``orch recover`` -- the dedicated supervisor cold-takeover owner.

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
    print(f"Role             : {service.run_routing_role(run.id)}")
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
        if supervision.manager_state.value == "ATTENTION_REQUIRED":
            print("Needs attention : yes — this run needs owner review")

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
    """Handle the strictly read-only observability snapshot command."""
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


def _open_read_only_report_db(args: argparse.Namespace) -> Database | None:
    """Open the run's store read-only for the report/monitor surface.

    Returns ``None`` (after printing a diagnostic) when no store exists.
    The projection never mutates the run: refreshes re-open the store
    read-only on every iteration.
    """
    explicit = _explicit_db(args)
    path = Path(explicit) if explicit else (db_path_for_run(args.run_id) or get_default_db_path())
    resolved = path.resolve()
    if not resolved.is_file():
        print(f"Error: Database not found at {resolved}", file=sys.stderr)
        return None
    return Database(resolved, read_only=True)


def handle_report(args: argparse.Namespace) -> int:
    """Handle the strictly read-only canonical run report command."""
    db = _open_read_only_report_db(args)
    if db is None:
        return 1
    try:
        report = build_run_report(db, args.run_id)
    except (OSError, sqlite3.Error, ValueError) as exc:
        print(f"Error: Could not read report for '{args.run_id}': {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        sys.stdout.buffer.write(canonical_report_json(report) + b"\n")
    else:
        print(render_report_text(report))
    return 0


def handle_monitor(args: argparse.Namespace) -> int:
    """Follow a run live with the canonical projection (read-only polling).

    Each refresh re-opens the store read-only and rebuilds the same
    canonical report that ``orch report`` emits, so the live view and
    the post-run report can never disagree on semantics.  Terminal
    states exit 0 after rendering the final frame; ``--timeout``
    exits 2 while the run is still active; ``--once`` renders one
    frame and exits 0.
    """
    interval = max(0.5, float(getattr(args, "interval", 2.0) or 2.0))
    timeout = max(0.0, float(getattr(args, "timeout", 0.0) or 0.0))
    once = bool(getattr(args, "once", False))
    deadline = time.monotonic() + timeout if timeout > 0 else None
    try:
        while True:
            db = _open_read_only_report_db(args)
            if db is None:
                return 1
            try:
                report = build_run_report(db, args.run_id)
            except (OSError, sqlite3.Error, ValueError) as exc:
                print(
                    f"Error: Could not read monitor for '{args.run_id}': {exc}",
                    file=sys.stderr,
                )
                return 1
            print(render_monitor_frame(report))
            print("", flush=True)
            # ADR-0002 D8 (M2-5) readout-only: a PREEMPTED run is terminal
            # for the run, so the monitor must stop here.  The canonical
            # set is authoritative; is_terminal_report() still enumerates
            # the pre-D8 statuses (see the Step 001 report for the narrow
            # unification proposal), so PREEMPTED is recognized from the
            # report's own lifecycle status without duplicating any list.
            lifecycle = report.get("lifecycle")
            lifecycle_status = (
                lifecycle.get("status") if isinstance(lifecycle, dict) else None
            )
            if (
                once
                or is_terminal_report(report)
                or (
                    isinstance(lifecycle_status, str)
                    and lifecycle_status in {s.value for s in TERMINAL_RUN_STATUSES}
                )
            ):
                return 0
            if deadline is not None and time.monotonic() >= deadline:
                print(
                    f"Monitor timeout after {timeout:.0f}s; run '{args.run_id}' "
                    "is still active.",
                    file=sys.stderr,
                )
                return 2
            sleep_for = interval
            if deadline is not None:
                sleep_for = max(0.0, min(interval, deadline - time.monotonic()))
            time.sleep(sleep_for)
    except KeyboardInterrupt:
        print("Monitor interrupted; run is untouched.", file=sys.stderr)
        return 130


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

    header = f"{'RUN ID':<16} {'STATUS':<12} {'ROLE':<10} {'CREATED AT':<24} {'TASK'}"
    print(header)
    print("─" * len(header))
    for r in runs:
        task_snippet = (r.task[:40] + "...") if len(r.task) > 40 else r.task
        print(
            f"{r.id:<16} {r.status.value:<12} {service.run_routing_role(r.id):<10} "
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


def _gc_read_only_db(args: argparse.Namespace, repo: Path) -> Database | None:
    """Open the most specific existing store read-only, or None when absent.

    A dry run must not create a database, WAL file or schema, so a missing
    store yields ``None`` and the inventory reports every Git artifact as
    UNKNOWN/KEEP rather than adopting it.
    """
    explicit = _explicit_db(args)
    candidates = (
        [Path(explicit)]
        if explicit is not None
        else [Path(project_db_path_for_repo(repo)), Path(get_default_db_path())]
    )
    for candidate in candidates:
        if candidate.exists():
            return Database(candidate, read_only=True)
    return None


def _gc_inventory_payload(inventory: Inventory, preview: GcPreview) -> dict[str, Any]:
    return {
        "repo_root": inventory.repo_root,
        "project_id": inventory.project_id,
        "generated_at": inventory.generated_at.isoformat(),
        "warnings": list(inventory.warnings),
        "policy_ref": preview.policy_ref,
        "approved": preview.approved,
        "preview_id": preview.preview_id,
        "candidates": [
            {
                "artifact_class": c.artifact_class.value,
                "action": c.action.value,
                "reference": c.reference,
                "identity": c.identity,
                "owner": c.owner,
                "reason": c.reason,
                "estimated_bytes": c.estimated_bytes,
            }
            for c in preview.candidates
        ],
        "exclusions": [
            {
                "artifact_class": e.artifact_class.value,
                "reference": e.reference,
                "reason": e.reason,
            }
            for e in preview.exclusions
        ],
        "estimated_reclaimable_bytes": preview.estimated_reclaimable_bytes,
        "inventory": [
            {
                "artifact_class": item.artifact_class.value,
                "reference": item.reference,
                "identity": item.identity,
                "owner": item.owner,
                "ownership": item.ownership.value,
                "reason": item.reason,
                "size_bytes": item.size_bytes,
                "reclaimable_bytes": item.reclaimable_bytes,
            }
            for item in inventory.items
        ],
    }


def _print_gc_report(inventory: Inventory, preview: GcPreview) -> None:
    print(f"[*] Storage inventory for {inventory.repo_root} ({len(inventory.items)} artifacts)")
    for item in inventory.items:
        size = "?" if item.size_bytes is None else str(item.size_bytes)
        print(
            f"  {item.artifact_class.value:<16} {item.ownership.value:<14} "
            f"{size:>12}B  {item.reference}  [{item.reason}]"
        )
    for warning in inventory.warnings:
        print(f"  ! {warning}")
    print("─" * 60)
    if preview.policy_ref is None:
        print("[*] No validated standing owner policy: inventory/report only (nothing eligible).")
    else:
        print(f"[*] Standing policy: {preview.policy_ref}")
    if preview.candidates:
        print(f"[*] Eligible candidates ({len(preview.candidates)}):")
        for candidate in preview.candidates:
            size = "unknown" if candidate.estimated_bytes is None else str(candidate.estimated_bytes)
            print(
                f"  → {candidate.action.value}: {candidate.reference} "
                f"[{candidate.reason}] est={size}B"
            )
    if preview.exclusions:
        print(f"[*] Kept ({len(preview.exclusions)}), reasons:")
        reasons: dict[str, int] = {}
        for exclusion in preview.exclusions:
            reasons[exclusion.reason] = reasons.get(exclusion.reason, 0) + 1
        for reason, count in sorted(reasons.items()):
            print(f"  · {reason}: {count}")


def handle_gc(args: argparse.Namespace) -> int:
    """Handle `orch gc [--dry-run|--apply] [--class ...] [--json]`."""
    repo = (
        Path(args.project_repo).resolve() if args.project_repo else Path.cwd().resolve()
    )
    if not repo.exists() or not GitManager.is_git_repo(repo):
        print(f"[!] Not a Git repository: {repo}", file=sys.stderr)
        return 1
    classes: tuple[ArtifactClass, ...] | None = (
        tuple(ArtifactClass(value) for value in args.artifact_class)
        if args.artifact_class
        else None
    )
    policy_path = Path(args.policy) if args.policy else default_gc_policy_path()
    try:
        policy = load_gc_policy(policy_path)
    except GcPolicyError as exc:
        print(f"[!] Invalid GC policy: {exc}", file=sys.stderr)
        return 1
    effective_policy_path = policy_path if policy_path.exists() else None

    if not args.apply:
        db = _gc_read_only_db(args, repo)
        inventory = build_inventory(db, repo_root=repo)
        preview = preview_gc(
            inventory,
            policy=policy,
            policy_path=effective_policy_path,
            classes=classes,
        )
        if args.json:
            print(json.dumps(_gc_inventory_payload(inventory, preview), indent=2, sort_keys=True))
        else:
            _print_gc_report(inventory, preview)
        return 0

    if not policy.approved:
        print(
            "[!] Refusing --apply: no validated standing owner policy authorizes "
            "destructive GC.",
            file=sys.stderr,
        )
        return 1
    db = db_for_repo(args, repo)
    inventory = build_inventory(db, repo_root=repo)
    preview = preview_gc(
        inventory,
        policy=policy,
        policy_path=effective_policy_path,
        classes=classes,
    )
    result = apply_gc(
        preview,
        preview.candidates,
        db=db,
        repo_root=repo,
        audit=database_audit_sink(db),
    )
    payload = _gc_inventory_payload(inventory, preview)
    payload["apply"] = {
        "applied": [
            {
                "reference": outcome.candidate.reference,
                "action": outcome.candidate.action.value,
                "reason": outcome.reason,
                "actual_bytes": outcome.actual_bytes,
            }
            for outcome in result.applied
        ],
        "refused": [
            {
                "reference": outcome.candidate.reference,
                "action": outcome.candidate.action.value,
                "reason": outcome.reason,
            }
            for outcome in result.refused
        ],
        "audit_uncertain": result.audit_uncertain,
    }
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        if result.audit_uncertain:
            return 1
    else:
        _print_gc_report(inventory, preview)
        print("─" * 60)
        print(f"[*] Applied {len(result.applied)} action(s); refused {len(result.refused)}.")
        for outcome in result.applied:
            if outcome.reason == "audit-uncertain":
                print(
                    f"  ! {outcome.candidate.action.value}: "
                    f"{outcome.candidate.reference} (audit uncertain)"
                )
            else:
                print(f"  ✓ {outcome.candidate.action.value}: {outcome.candidate.reference}")
        for outcome in result.refused:
            print(f"  ! kept {outcome.candidate.reference} [{outcome.reason}]")
        if result.audit_uncertain:
            print("[!] Audit persistence failed after a completed action; reconcile required.")
            return 1
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
    try:
        snap = load_effective_routing()
    except RoutingConfigError as exc:
        print(f"[!] Routing config invalid: {exc}", file=sys.stderr)
        return 1
    print("Configured Multi-Provider Execution Roles & Routing Chains:\n")
    print(f"Routing Source: {snap.source} {snap.source_path or '(packaged default)'}")
    print(f"Content Hash: {snap.content_hash}\n")
    for role in ("WORKER", "SURGEON", "INTEGRATOR"):
        print(f"{role}:")
        for number, group in enumerate(snap.groups_for(role), start=1):
            for c in group:
                avail = "✓ available" if registry.is_available(c.provider) else "✗ unavailable"
                print(f"  {number}. → [{c.provider}] {c.model} ({avail})")

    print("\nLocal Provider Status:")
    quota_rows = service.list_quota()
    for prov in _DOCTOR_PROVIDERS:
        status = _provider_status_line(registry, prov, quota_rows)
        print(f"  • {prov:<12}: {status}")

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
            "migrated_from": snap.migrated_from,
            "roles": {
                role: [
                    [f"{c.provider}/{c.model}" for c in group]
                    for group in snap.groups_for(role)
                ]
                for role in ("WORKER", "SURGEON", "INTEGRATOR")
            },
            "managed_elsewhere": {
                "ORCHESTRATOR": "orch orchestrator show",
                "REVIEWER": "orch reviewer show",
            },
        }
        print(json.dumps(payload, indent=2))
        return 0
    print(f"Source: {snap.source} {snap.source_path or '(packaged default)'}")
    print(f"Hash: {snap.content_hash}")
    print(f"Schema: {snap.schema_version}")
    if snap.migrated_from is not None:
        print(
            f"Note: migrated in memory from schema {snap.migrated_from}; "
            "run `orch routing migrate --write` to persist"
        )
    for role in ("WORKER", "SURGEON", "INTEGRATOR"):
        print(f"{role}:")
        for number, group in enumerate(snap.groups_for(role), start=1):
            print(f"  {number}.", [f"{c.provider}/{c.model}" for c in group])
    print("ORCHESTRATOR: see 'orch orchestrator show'")
    print("REVIEWER: see 'orch reviewer show'")
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


def _routing_role_or_refuse(role_raw: str) -> str | None:
    """Return the normalized routed role, or print a pointer and return None."""
    cleaned = str(role_raw).strip().upper()
    if cleaned in ("ORCHESTRATOR", "REVIEWER"):
        pointer = "orch orchestrator show" if cleaned == "ORCHESTRATOR" else "orch reviewer show"
        print(f"[!] Role {cleaned!r} is managed elsewhere; see '{pointer}'", file=sys.stderr)
        return None
    if cleaned not in ("WORKER", "SURGEON", "INTEGRATOR"):
        print(f"[!] Unknown routing role {role_raw!r}: expected worker, surgeon or integrator",
              file=sys.stderr)
        return None
    return cleaned


def _read_raw_user_routing() -> tuple[Path | None, dict[str, Any] | None, str | None]:
    """Return (path, raw document, error) for the user routing file.

    The third element is an error string when the file exists but cannot
    be read as a JSON object; the document is None when no user file exists.
    """
    from saberops.routing_config import get_user_routing_path as _user_path

    user_path = _user_path()
    if not user_path.is_file():
        return None, None, None
    try:
        raw_text = user_path.read_text(encoding="utf-8")
    except OSError as exc:
        return user_path, None, f"Routing config at {user_path}: cannot read: {exc}"
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        return user_path, None, f"Routing config at {user_path}: invalid JSON: {exc}"
    if not isinstance(data, dict):
        return user_path, None, f"Routing config at {user_path}: top-level must be JSON object"
    return user_path, data, None


def handle_routing_migrate(args: argparse.Namespace) -> int:
    from saberops.routing_config import canonical_routing_payload, save_user_routing

    user_path, data, error = _read_raw_user_routing()
    if error is not None:
        print(f"[!] {error}", file=sys.stderr)
        return 1
    if data is None:
        print("Packaged default is schema 3; no user routing file to migrate.")
        return 0
    raw_version = data.get("schema_version")
    if raw_version == 3:
        print(f"User routing config at {user_path} is already schema 3; nothing to do.")
        return 0
    if raw_version not in (1, 2):
        print(f"[!] Routing config at {user_path}: unsupported schema_version {raw_version!r}",
              file=sys.stderr)
        return 1
    try:
        migrated = canonical_routing_payload(data)
    except RoutingConfigError as exc:
        print(f"[!] Routing config invalid: {exc}", file=sys.stderr)
        return 1
    print(f"User routing config at {user_path} is schema {raw_version}; migrated preview:")
    for tier in ("T1", "T2", "T3"):
        section = data.get(tier)
        if isinstance(section, dict):
            for sub, chain in section.items():
                print(f"  old {tier}.{sub}:", list(chain) if isinstance(chain, list) else chain)
    for role, groups in migrated["roles"].items():
        print(f"  new {role}:")
        for number, group in enumerate(groups, start=1):
            print(f"    {number}.", list(group["routes"]))
    if not bool(getattr(args, "write", False)):
        print("Dry run: nothing written. Re-run with --write to persist (with backup).")
        return 0
    from datetime import datetime

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    assert user_path is not None
    backup = user_path.with_name(f"{user_path.name}.bak-{stamp}")
    counter = 0
    while backup.exists():
        counter += 1
        backup = user_path.with_name(f"{user_path.name}.bak-{stamp}.{counter}")
    try:
        backup.write_bytes(user_path.read_bytes())
    except OSError as exc:
        print(f"[!] Cannot back up {user_path}: {exc}", file=sys.stderr)
        return 1
    try:
        dest = save_user_routing(migrated)
    except RoutingConfigError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Backed up {user_path} to {backup}")
    print(f"Wrote schema-3 routing config to {dest}")
    return 0


def _edit_role_chain(args: argparse.Namespace, op: str) -> int:
    from saberops.routing_config import (
        add_candidate_to_chain,
        canonical_routing_payload,
        load_effective_routing_payload,
        move_group_in_chain,
        remove_candidate_from_chain,
        reorder_chain_groups,
        save_user_routing,
    )

    role = _routing_role_or_refuse(str(getattr(args, "role", "")))
    if role is None:
        return 1
    user_path, data, error = _read_raw_user_routing()
    if error is not None:
        print(f"[!] {error}", file=sys.stderr)
        return 1
    if data is not None and data.get("schema_version") in (1, 2):
        print("[!] User routing file is schema 1/2; run `orch routing migrate` first",
              file=sys.stderr)
        return 1
    try:
        payload = load_effective_routing_payload()
        if op == "add":
            payload = add_candidate_to_chain(
                payload,
                role=role,
                candidate_id=str(getattr(args, "candidate_id", "")),
                group=getattr(args, "group", None),
            )
        elif op == "remove":
            payload = remove_candidate_from_chain(
                payload,
                role=role,
                candidate_id=str(getattr(args, "candidate_id", "")),
            )
        elif op == "move":
            payload = move_group_in_chain(
                payload,
                role=role,
                group=int(args.group),
                direction=str(getattr(args, "direction", "")),
            )
        elif op == "reorder":
            payload = reorder_chain_groups(
                payload,
                role=role,
                order=[int(entry) for entry in args.order],
            )
        else:
            print(f"[!] Unknown routing edit {op!r}", file=sys.stderr)
            return 1
        canonical_routing_payload(payload)
        dest = save_user_routing(payload)
    except RoutingConfigError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Updated {role} chain in {dest}:")
    for number, group in enumerate(payload["roles"][role], start=1):
        print(f"  {number}.", list(group["routes"]))
    return 0


def handle_routing_add(args: argparse.Namespace) -> int:
    return _edit_role_chain(args, "add")


def handle_routing_remove(args: argparse.Namespace) -> int:
    return _edit_role_chain(args, "remove")


def handle_routing_move(args: argparse.Namespace) -> int:
    return _edit_role_chain(args, "move")


def handle_routing_reorder(args: argparse.Namespace) -> int:
    return _edit_role_chain(args, "reorder")


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


def handle_project_show(args: argparse.Namespace) -> int:
    """Handle ``orch project show`` (effective owner-facing settings)."""
    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    summary = effective_policy_summary(repo)
    if "error" in summary:
        print(f"[!] {summary['error']}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2, sort_keys=True))
        return 0
    print(f"Gate            : {summary['gate_effective'] or '(not configured)'}")
    print(f"  configured    : {'yes' if summary['gate_configured'] else 'no'}")
    print(f"Data policy     : {summary['data_policy_label']}")
    print(f"  explicit      : {'yes' if summary['data_policy_explicit'] else 'no (Denied default)'}")
    print(f"Review policy   : {summary['review_policy_mode']}")
    print(f"  explicit      : {'yes' if summary['review_policy_explicit'] else 'no'}")
    print(f"Gate storage    : {summary['gate_scratch_mode']}")
    print(f"Target branch   : {summary['accept_target_branch_effective'] or '(unknown)'}")
    print(
        "  explicit      : "
        f"{'yes' if summary['accept_target_branch_explicit'] else 'no (live branch)'}"
    )
    return 0


def _project_registry_for(args: argparse.Namespace) -> ProjectRegistry:
    """Resolve the Step 015 stable project registry for CLI args."""
    raw = str(getattr(args, "registry", "") or "").strip()
    if raw:
        return ProjectRegistry(Path(raw).expanduser())
    return ProjectRegistry()


def _print_registered_projects(projects: tuple[RegisteredProject, ...], as_json: bool) -> int:
    """Render registered projects as JSON or human text."""
    records = [item.as_dict() for item in projects]
    if as_json:
        print(json.dumps({"projects": records}, indent=2, sort_keys=True))
        return 0
    if not records:
        print("(no registered projects)")
        return 0
    for record in records:
        flag = "active" if record["active"] else "inactive"
        print(f"{record['project_id']} [{flag}] {record['display_name']}")
        print(f"  repo : {record['repository_root']}")
        print(f"  state: {record['state_root']}")
    return 0


def handle_project_list(args: argparse.Namespace) -> int:
    """Handle ``orch project list`` (Step 015 stable registry)."""
    from saberops.project import ProjectRegistrationError

    try:
        registry = _project_registry_for(args)
        projects = registry.list_projects()
    except ProjectRegistrationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    return _print_registered_projects(projects, bool(getattr(args, "json", False)))


def handle_project_register(args: argparse.Namespace) -> int:
    """Handle ``orch project register`` (explicit identity only)."""
    from saberops.project import ProjectCapError, ProjectRegistrationError

    try:
        registry = _project_registry_for(args)
        project = registry.register(
            str(args.id),
            str(args.name),
            str(args.repo),
            str(args.state_dir),
            active=bool(getattr(args, "active", False)),
        )
    except (ProjectRegistrationError, ProjectCapError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(project.as_dict(), indent=2, sort_keys=True))
        return 0
    print(f"registered {project.project_id} ({project.display_name})")
    return 0


def handle_project_active(args: argparse.Namespace) -> int:
    """Handle ``orch project active`` (currently active registrations)."""
    from saberops.project import ProjectRegistrationError

    try:
        registry = _project_registry_for(args)
        projects = registry.active_projects()
    except ProjectRegistrationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    return _print_registered_projects(projects, bool(getattr(args, "json", False)))


def handle_project_activate(args: argparse.Namespace) -> int:
    """Handle ``orch project activate`` (enforces the 3-project cap)."""
    from saberops.project import ProjectCapError, ProjectRegistrationError

    try:
        registry = _project_registry_for(args)
        project = registry.activate(str(args.id))
    except (ProjectRegistrationError, ProjectCapError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(project.as_dict(), indent=2, sort_keys=True))
        return 0
    print(f"activated {project.project_id}")
    return 0


def handle_project_deactivate(args: argparse.Namespace) -> int:
    """Handle ``orch project deactivate``."""
    from saberops.project import ProjectRegistrationError

    try:
        registry = _project_registry_for(args)
        project = registry.deactivate(str(args.id))
    except ProjectRegistrationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(project.as_dict(), indent=2, sort_keys=True))
        return 0
    print(f"deactivated {project.project_id}")
    return 0


def _certify_result_for(args: argparse.Namespace) -> CertificationResult:
    """Resolve the certification result from --evidence or an empty evaluation."""
    from saberops import certification as _cert

    evidence = getattr(args, "evidence", None)
    if evidence:
        return _cert.load_evidence_file(evidence)
    return _cert.default_empty_evaluation()


def handle_certify_check(args: argparse.Namespace) -> int:
    """Handle ``orch certify check`` (read-only checklist + Q4 evaluation)."""
    from saberops import certification as _cert

    try:
        result = _certify_result_for(args)
    except _cert.CertificationError as exc:
        print(f"[!] Invalid evidence: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(_cert.render_markdown_report(result))
    return 0 if result.passed else 1


def handle_certify_run(args: argparse.Namespace) -> int:
    """Handle ``orch certify run`` (execute suite + scenario verifications)."""
    from saberops import certification as _cert

    try:
        result = _certify_result_for(args)
    except _cert.CertificationError as exc:
        print(f"[!] Invalid evidence: {exc}", file=sys.stderr)
        return 1
    scenario = getattr(args, "scenario", None)
    if scenario:
        wanted = {
            "mid-turn-kill": "S1",
            "context-overflow": "S2",
            "second-session": "S3",
            "dead-worker": "S4",
            "gate-protection": "S5",
            "standing-scope": "S6",
            "morning-report": "S7",
            "stall-alarm": "S8",
            "idea-intake": "S9",
        }[scenario]
        filtered = tuple(c for c in result.checks if c.name.startswith(wanted + " "))
        if getattr(args, "json", False):
            print(json.dumps([c.to_dict() for c in filtered], indent=2, sort_keys=True))
        else:
            for check in filtered:
                print(f"[{'PASS' if check.passed else 'FAIL'}] {check.name}: {check.evidence}")
        return 0 if all(c.passed for c in filtered) else 1
    if getattr(args, "json", False):
        print(json.dumps(result.to_dict(), indent=2, sort_keys=True))
    else:
        print(_cert.render_markdown_report(result))
    return 0 if result.passed else 1


def handle_certify_report(args: argparse.Namespace) -> int:
    """Handle ``orch certify report`` (markdown report to stdout or file)."""
    from saberops import certification as _cert

    try:
        result = _certify_result_for(args)
    except _cert.CertificationError as exc:
        print(f"[!] Invalid evidence: {exc}", file=sys.stderr)
        return 1
    text = _cert.render_markdown_report(result)
    out = getattr(args, "out", None)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        print(f"Wrote certification report to {out}")
    else:
        print(text)
    return 0 if result.passed else 1


def handle_certify_approve(args: argparse.Namespace) -> int:
    """Handle ``orch certify approve`` (owner-only self-dogfooding grant)."""
    import os

    from saberops import certification as _cert

    try:
        result = _certify_result_for(args)
    except _cert.CertificationError as exc:
        print(f"[!] Invalid evidence: {exc}", file=sys.stderr)
        return 1
    token = getattr(args, "token", None) or os.environ.get("SABEROPS_OWNER_TOKEN", "")
    record_path = getattr(args, "record", None) or os.environ.get(
        "SABEROPS_APPROVAL_RECORD", "approval-record.jsonl"
    )
    try:
        record = _cert.approve_self_dogfooding(
            result, owner_token=token, record_path=record_path, note=str(getattr(args, "note", ""))
        )
    except _cert.CertificationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Recorded self-dogfooding approval (digest {record.certification_digest})")
    return 0


def handle_project_set_gate(args: argparse.Namespace) -> int:
    """Handle ``orch project set-gate <command>``."""
    from saberops.project_settings import ProjectSettingsError, set_project_gate_command

    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        path = set_project_gate_command(repo, str(args.command))
    except ProjectSettingsError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Project gate set to '{str(args.command).strip()}' at {path}")
    return 0


def handle_project_clear_gate(args: argparse.Namespace) -> int:
    """Handle ``orch project clear-gate``."""
    from saberops.project_settings import ProjectSettingsError, clear_project_gate_command

    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        path = clear_project_gate_command(repo)
    except ProjectSettingsError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Project gate cleared (unset) at {path}")
    return 0


def handle_project_set_data_policy(args: argparse.Namespace) -> int:
    """Handle ``orch project set-data-policy <allowed|denied>``."""
    from saberops.project_settings import ProjectSettingsError, set_project_data_policy

    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        path = set_project_data_policy(repo, str(args.policy))
    except ProjectSettingsError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Project data policy set to '{str(args.policy).strip().upper()}' at {path}")
    return 0


def handle_project_set_review_policy(args: argparse.Namespace) -> int:
    """Handle ``orch project set-review-policy`` via the canonical store."""
    from saberops.project_settings import ProjectSettingsError, set_project_review_policy

    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    raw = str(args.policy).strip().lower()
    mode = (
        ReviewPolicyMode.ALWAYS_REQUIRED
        if raw == "always-required"
        else ReviewPolicyMode.RISK_ADAPTIVE
    )
    try:
        path = set_project_review_policy(repo, mode)
    except (ProjectSettingsError, ReviewPolicyError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Project review policy set to '{mode.value}' at {path}")
    return 0


def handle_project_set_target_branch(args: argparse.Namespace) -> int:
    """Handle ``orch project set-target-branch <branch>``."""
    from saberops.project_settings import (
        ProjectSettingsError,
        set_project_accept_target_branch,
    )

    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        path = set_project_accept_target_branch(repo, str(args.branch))
    except ProjectSettingsError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Project target branch set to '{str(args.branch).strip()}' at {path}")
    return 0


def handle_project_clear_target_branch(args: argparse.Namespace) -> int:
    """Handle ``orch project clear-target-branch``."""
    from saberops.project_settings import (
        ProjectSettingsError,
        clear_project_accept_target_branch,
    )

    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        path = clear_project_accept_target_branch(repo)
    except ProjectSettingsError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    print(f"Project target branch cleared (unset) at {path}")
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


def _render_bindings_human(title: str, bindings: list[dict[str, Any]]) -> None:
    """Print one eligible-binding row per line (human-readable selection help)."""
    if not bindings:
        print(f"{title}: (none eligible)")
        return
    print(f"{title}:")
    for binding in bindings:
        print(
            f"  {binding.get('binding_id')} "
            f"[{binding.get('provider')}/{binding.get('model')}] "
            f"role={binding.get('binding_role')}"
        )


def handle_reviewer_show(args: argparse.Namespace) -> int:
    """Handle `orch reviewer show`."""
    from saberops.service import OwnerSelectionError

    service = OrchestratorService()
    try:
        status = service.get_reviewer_status()
    except OwnerSelectionError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    selected = status["selected_binding_id"] or "(none: legacy review ladder applies)"
    print(f"Reviewer selection: {selected}")
    print(f"Status            : {status['status']}")
    if status["reason"]:
        print(f"Reason            : {status['reason']}")
    _render_bindings_human("Eligible REVIEWER bindings", status["eligible"])
    return 0


def handle_reviewer_select(args: argparse.Namespace) -> int:
    """Handle `orch reviewer select <binding-id>`."""
    from saberops.service import OwnerSelectionError

    service = OrchestratorService()
    try:
        result = service.set_reviewer_binding(str(args.binding_id))
    except (OwnerSelectionError, ValueError) as exc:
        print(f"[!] Reviewer selection refused: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(
        f"Reviewer selection saved: {result['selected_binding_id']} "
        f"[{result['provider']}/{result['model']}]"
    )
    return 0


def handle_reviewer_clear(args: argparse.Namespace) -> int:
    """Handle `orch reviewer clear`."""
    from saberops.service import OwnerSelectionError

    service = OrchestratorService()
    try:
        result = service.clear_reviewer_binding()
    except (OwnerSelectionError, ValueError) as exc:
        print(f"[!] Reviewer selection not cleared: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("Reviewer selection cleared (legacy review ladder applies).")
    return 0


def handle_orchestrator_show(args: argparse.Namespace) -> int:
    """Handle `orch orchestrator show`."""
    from saberops.service import OwnerSelectionError

    service = OrchestratorService()
    try:
        status = service.get_orchestrator_status()
    except OwnerSelectionError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(status, indent=2, sort_keys=True))
        return 0
    policy = status["policy"]
    print(f"Orchestrator policy: {policy['mode'] if policy else '(none)'}")
    print(f"Status             : {status['status']}")
    if status["reason"]:
        print(f"Reason             : {status['reason']}")
    resolved = status["resolved_binding"]
    if resolved is not None:
        print(f"Resolved           : {resolved['binding_id']} [{resolved['provider']}/{resolved['model']}]")
    _render_bindings_human("Eligible ORCHESTRATOR bindings", status["eligible"])
    return 0


def handle_orchestrator_select(args: argparse.Namespace) -> int:
    """Handle `orch orchestrator select --binding ID | --auto`."""
    from saberops.service import OwnerSelectionError

    service = OrchestratorService()
    try:
        if bool(getattr(args, "auto", False)):
            result = service.set_orchestrator_binding(mode="auto")
        else:
            result = service.set_orchestrator_binding(str(args.binding), mode="pinned")
    except (OwnerSelectionError, ValueError) as exc:
        print(f"[!] Orchestrator selection refused: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(
        f"Orchestrator selection saved: mode={result['mode']} "
        f"binding={result['selected_binding_id']}"
    )
    return 0


def handle_orchestrator_clear(args: argparse.Namespace) -> int:
    """Handle `orch orchestrator clear`."""
    from saberops.service import OwnerSelectionError

    service = OrchestratorService()
    try:
        result = service.clear_orchestrator_binding()
    except (OwnerSelectionError, ValueError) as exc:
        print(f"[!] Orchestrator policy not cleared: {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print("Orchestrator policy cleared (no pinned selection).")
    return 0


def handle_access_list(args: argparse.Namespace) -> int:
    """Handle `orch access list`."""
    from saberops.access_ops import AccessOperationError, list_access_connections

    try:
        payload = list_access_connections()
    except AccessOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    rows = payload["connections"]
    if not rows:
        print("No model-access connections configured.")
        return 0
    header = f"{'CONNECTION':<28} {'PROVIDER':<12} {'BACKEND':<16} {'READINESS':<10} {'REASON'}"
    print(header)
    print("─" * len(header))
    for row in rows:
        print(
            f"{row['connection_id']:<28} {row['provider']:<12} {row['backend']:<16} "
            f"{row['readiness_state']:<10} {row['readiness_reason']}"
        )
    return 0


def handle_access_show(args: argparse.Namespace) -> int:
    """Handle `orch access show <connection-id>`."""
    from saberops.access_ops import AccessOperationError, show_access_connection

    try:
        payload = show_access_connection(str(args.connection_id))
    except AccessOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Connection  : {payload['connection_id']}")
    print(f"Provider    : {payload['provider']} (backend {payload['backend']})")
    print(f"Protocol    : {payload['protocol']} (auth {payload['auth_mode']})")
    print(f"Credential  : {payload['credential_display'] or '(none)'}")
    print(f"Readiness   : {payload['readiness_state']} ({payload['readiness_reason']})")
    if payload["observed_at"]:
        print(f"Observed at : {payload['observed_at']}")
    print(f"Catalog     : {len(payload['catalog_models'])} model(s) last enumerated")
    return 0


def handle_access_add(args: argparse.Namespace) -> int:
    """Handle `orch access add`."""
    from saberops.access_ops import AccessOperationError, add_access_connection

    try:
        result = add_access_connection(
            connection_id=str(args.connection_id),
            preset=str(args.preset),
            protocol=str(args.protocol),
            endpoint=str(args.endpoint),
            credential_ref=str(args.credential_ref),
            auth_header_name=str(args.auth_header_name),
            model=str(args.model),
            public_headers="\n".join(str(item) for item in (args.public_headers or [])),
        )
    except AccessOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"Connection saved: {result['connection_id']} (credential reference only; no secret stored).")
    discovery = result["discovery"]
    if discovery.get("attempted") and discovery.get("ok"):
        print(f"Bounded discovery found {discovery['models_found']} model(s); nothing was placed into routing.")
    elif discovery.get("attempted"):
        print("Automatic discovery could not enumerate models; use 'orch access discover' to retry.")
    return 0


def handle_access_test(args: argparse.Namespace) -> int:
    """Handle `orch access test <connection-id>`."""
    from saberops.access_ops import AccessOperationError, test_access_connection

    try:
        probe = test_access_connection(str(args.connection_id))
    except AccessOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(probe, indent=2, sort_keys=True))
        return 0
    print(f"Connection: {probe['connection_id']}")
    print(f"Reachable : {probe['ok']}")
    print(f"Reason    : {probe['reason']}")
    print(f"Detail    : {probe['detail']}")
    return 0


def handle_access_discover(args: argparse.Namespace) -> int:
    """Handle `orch access discover <connection-id>`."""
    from saberops.access_ops import AccessOperationError, discover_access_models

    try:
        result = discover_access_models(str(args.connection_id))
    except AccessOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"Connection: {result['connection_id']}")
    print(f"Enumerated: {'yes' if result['ok'] else 'no'} ({result['reason']})")
    print(f"Detail    : {result.get('detail', '')}")
    print(f"Models    : {len(result.get('models', []))}")
    return 0


def handle_access_verify_model(args: argparse.Namespace) -> int:
    """Handle `orch access verify-model <connection-id> --model <model>`."""
    from saberops.access_ops import AccessOperationError, verify_access_model

    try:
        probe = verify_access_model(str(args.connection_id), str(args.model))
    except AccessOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(probe, indent=2, sort_keys=True))
        return 0
    print(f"Connection: {probe['connection_id']}")
    print(f"Model     : {probe['model_id']}")
    print(f"Access    : {probe['ok']}")
    print(f"Reason    : {probe['reason']}")
    print(f"Detail    : {probe['detail']}")
    return 0


def handle_plans_show(args: argparse.Namespace) -> int:
    """Handle `orch plans show`."""
    from saberops.plans_ops import PlansOperationError, show_plans

    try:
        payload = show_plans()
    except PlansOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    for section in ("accounts", "quota_pools", "postures"):
        print(f"[{section}]")
        entries = payload[section]
        if not entries:
            print("  (none)")
            continue
        for key in sorted(entries):
            entry = entries[key]
            sources = entry.pop("sources", {})
            for field in sorted(entry):
                source = sources.get(field, "packaged")
                print(f"  {key}.{field} = {entry[field]!r} ({source})")
            entry["sources"] = sources
    print(f"[reserves] ({payload['reserves_source']})")
    for reserve in payload["reserves"]:
        print(f"  {reserve}")
    return 0


def handle_plans_validate(args: argparse.Namespace) -> int:
    """Handle `orch plans validate` (exit 2 on validation errors)."""
    from pathlib import Path as _Path

    from saberops.plans_ops import validate_plans

    raw_file = getattr(args, "file", None)
    result = validate_plans(_Path(str(raw_file)) if raw_file else None)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result["ok"] else 2
    if result["ok"]:
        print(f"Plans valid ({result['path']}).")
        return 0
    for error in result["errors"]:
        print(f"[!] {error}", file=sys.stderr)
    return 2


def handle_posture_show(args: argparse.Namespace) -> int:
    """Handle `orch posture show`."""
    from datetime import date as _date

    from saberops.plans_ops import PlansOperationError, show_postures

    try:
        payload = show_postures(_date.today())
    except PlansOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    header = f"{'SELECTOR':<32} {'CLASS':<6} {'VERIFIED':<12} {'EXPIRES':<12} {'STATUS'}"
    print(header)
    print("─" * len(header))
    for row in payload["postures"]:
        print(
            f"{row['selector']:<32} {row['class']:<6} "
            f"{row['verified_on'] or '-':<12} {row['expires_on'] or '-':<12} "
            f"{row['status']}"
        )
    return 0


def handle_posture_verify(args: argparse.Namespace) -> int:
    """Handle `orch posture verify SELECTOR ...`."""
    from saberops.plans_ops import PlansOperationError, verify_posture

    try:
        result = verify_posture(
            str(args.selector),
            tuple(str(url) for url in (args.urls or [])),
            str(args.note) if args.note is not None else None,
            str(args.posture_class) if args.posture_class is not None else None,
            str(args.date) if args.date is not None else None,
            ttl_days=getattr(args, "ttl_days", None),
        )
    except PlansOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(
        f"Posture verified: {result['selector']} "
        f"(class {result['class']}, verified {result['verified_on']}, "
        f"expires {result['expires_on'] or 'never'}, status {result['status']})"
    )
    return 0


def _questions_queue(args: argparse.Namespace) -> OwnerQuestionQueue:
    """Resolve the question document the command should act on."""
    return OwnerQuestionQueue(getattr(args, "store", None))


def _questions_fail(exc: Exception) -> int:
    print(f"[!] {exc}", file=sys.stderr)
    return 1


def _questions_reference_date(raw: str | None) -> date:
    """Resolve the reference date for deferred resurfacing (default: today)."""
    if raw is None or not str(raw).strip():
        return datetime.now(UTC).date()
    return date.fromisoformat(str(raw).strip())


def _print_question(question: OwnerQuestion, *, as_json: bool) -> int:
    view = question_view(question)
    if as_json:
        print(json.dumps(view, sort_keys=True))
        return 0
    print(f"Question : {view['question_id']} ({view['key']}) [{view['status']}]")
    print(f"Subject  : {view['subject']}")
    print(f"Class    : {view['decision_class']} — answerable by {view['answerable_by']}")
    print(f"Raised   : {view['raised_by_role']} {view['raised_by']} at {view['raised_at']}")
    if view["scope"] or view["run_id"]:
        print(f"Scope    : {view['scope'] or '-'} run {view['run_id'] or '-'}")
    if view["blocks"]:
        print(f"Blocks   : {', '.join(view['blocks'])}")
    if view["defer_until"]:
        print(f"Deferred : until {view['defer_until']}")
    if view["answer"] is not None:
        answer = view["answer"]
        print(f"Answer   : {answer['words']}")
        print(f"Authority: {answer['authority']} ({answer['answered_by']})")
    if view["moot_evidence"]:
        print(f"Moot     : {view['moot_evidence']}")
    if view["divergence_count"]:
        print(f"Divergent: {view['divergence_count']} record(s) — reported, not auto-closed")
    return 0


def handle_questions_ask(args: argparse.Namespace) -> int:
    """Handle `orch questions ask` (raises one keyed escalatable question)."""
    try:
        request = QuestionRequest(
            key=str(args.key),
            subject=str(args.subject),
            kind=str(args.kind),
            decision_class=DecisionClass(str(args.decision_class)),
            raised_by_role=AskRole(str(args.role)),
            raised_by=str(args.raised_by),
            escalation=Escalation(
                requirement=str(args.requirement),
                proposed_expansion=str(args.proposed_expansion),
                smallest_compliant=str(args.smallest_compliant),
                consequences=str(args.consequences),
                recommendation=str(args.recommendation),
            ),
            scope=args.scope,
            run_id=args.run_id,
            blocks=tuple(str(item) for item in args.blocks),
            options=tuple(str(item) for item in args.options),
            default_option=args.default_option,
            supersedes=args.supersedes,
        )
        question = _questions_queue(args).ask(request)
    except QuestionError as exc:
        return _questions_fail(exc)
    return _print_question(question, as_json=bool(args.json))


def handle_questions_list(args: argparse.Namespace) -> int:
    """Handle `orch questions list`."""
    try:
        as_of = _questions_reference_date(args.as_of)
        queue = _questions_queue(args)
        questions = queue.questions(status=args.status, run_id=args.run_id)
        views = [question_view(question, as_of) for question in questions]
    except (QuestionError, ValueError) as exc:
        return _questions_fail(exc)
    if args.json:
        print(
            json.dumps(
                {
                    "as_of": as_of.isoformat(),
                    "questions": views,
                    "pending": [
                        view["question_id"]
                        for view in views
                        if view["status"] == QuestionStatus.OPEN.value or view["due"]
                    ],
                },
                sort_keys=True,
            )
        )
        return 0
    if not views:
        print("No questions.")
        return 0
    for view in views:
        marker = "DUE" if view["due"] else view["status"]
        print(
            f"{view['question_id']}  {marker:<9} {view['decision_class']:<32} "
            f"{view['key']}  {view['subject']}"
        )
    return 0


def handle_questions_show(args: argparse.Namespace) -> int:
    """Handle `orch questions show REF`."""
    try:
        as_of = _questions_reference_date(args.as_of)
        question = _questions_queue(args).get(str(args.ref))
    except (QuestionError, ValueError) as exc:
        return _questions_fail(exc)
    if question is None:
        print(f"[!] unknown question: {args.ref!r}", file=sys.stderr)
        return 1
    if args.as_of and not args.json:
        print(f"Reference date: {as_of.isoformat()}")
    return _print_question(question, as_json=bool(args.json))


def handle_questions_answer(args: argparse.Namespace) -> int:
    """Handle `orch questions answer REF --words ...` (verbatim owner words)."""
    authority = AnswerAuthority(str(args.authority).strip().upper())
    role_raw = getattr(args, "role", None)
    role = (
        AskRole(str(role_raw))
        if role_raw
        else (
            AskRole.OWNER
            if authority is AnswerAuthority.OWNER
            else AskRole.ORCHESTRATOR
        )
    )
    actor = str(args.answered_by) if args.answered_by else (
        "owner" if authority is AnswerAuthority.OWNER else ""
    )
    if not actor:
        print(
            "[!] --by is required for an Orchestrator answer (the actor is audited)",
            file=sys.stderr,
        )
        return 1
    try:
        question = _questions_queue(args).answer(
            str(args.ref),
            words=str(args.words),
            authority=authority,
            answered_by=actor,
            answered_by_role=role,
            chosen_option=args.chosen_option,
            evidence=args.evidence,
            rejected_alternative=args.rejected_alternative,
        )
    except QuestionError as exc:
        return _questions_fail(exc)
    return _print_question(question, as_json=bool(args.json))


def handle_questions_defer(args: argparse.Namespace) -> int:
    """Handle `orch questions defer REF --until DATE`."""
    try:
        question = _questions_queue(args).defer(
            str(args.ref),
            until=str(args.until),
            deferred_by=str(args.deferred_by),
            deferred_by_role=AskRole(str(args.role)),
            note=str(args.note or ""),
        )
    except QuestionError as exc:
        return _questions_fail(exc)
    return _print_question(question, as_json=bool(args.json))


def handle_questions_moot(args: argparse.Namespace) -> int:
    """Handle `orch questions moot REF --evidence ...` (evidence-backed close)."""
    try:
        question = _questions_queue(args).mark_moot(
            str(args.ref),
            evidence=str(args.evidence),
            closed_by=str(args.closed_by),
            closed_by_role=AskRole(str(args.role)),
        )
    except QuestionError as exc:
        return _questions_fail(exc)
    return _print_question(question, as_json=bool(args.json))


def handle_questions_diverge(args: argparse.Namespace) -> int:
    """Handle `orch questions diverge REF ...` (records a conflict)."""
    from saberops.questions import DivergenceReason

    try:
        question = _questions_queue(args).record_divergence(
            str(args.ref),
            reason=DivergenceReason(str(args.reason)),
            words=str(args.words),
            recorded_by=str(args.recorded_by),
            recorded_by_role=AskRole(str(args.role)),
            chosen_option=args.chosen_option,
            note=str(args.note or ""),
        )
    except QuestionError as exc:
        return _questions_fail(exc)
    return _print_question(question, as_json=bool(args.json))


def handle_questions_divergences(args: argparse.Namespace) -> int:
    """Handle `orch questions divergences [REF]` (reported, never auto-closed)."""
    try:
        reports = _questions_queue(args).divergences(args.ref)
    except QuestionError as exc:
        return _questions_fail(exc)
    if args.json:
        print(json.dumps([report.to_mapping() for report in reports], sort_keys=True))
        return 0
    if not reports:
        print("No divergent records.")
        return 0
    for report in reports:
        record = report.record
        print(
            f"{record.recorded_at}  {report.question_id} [{report.status.value}]  "
            f"{record.reason.value}  {record.words}"
        )
    return 0


def handle_questions_standing_rules(args: argparse.Namespace) -> int:
    """Handle `orch questions standing-rules` (proposals for CFG-1)."""
    try:
        proposals = _questions_queue(args).standing_rule_proposals(int(args.threshold))
    except QuestionError as exc:
        return _questions_fail(exc)
    if args.json:
        print(
            json.dumps(
                {"threshold": int(args.threshold), "proposals": [p.to_mapping() for p in proposals]},
                sort_keys=True,
            )
        )
        return 0
    if not proposals:
        print("No standing-rule proposal yet.")
        return 0
    print("Standing-rule proposals for CFG-1 (proposals only; nothing is applied):")
    for proposal in proposals:
        print(
            f"  {proposal.kind} x{proposal.occurrences} -> {proposal.chosen_answer} "
            f"({', '.join(proposal.question_ids)})"
        )
    return 0


def handle_questions_audit(args: argparse.Namespace) -> int:
    """Handle `orch questions audit` (every Orchestrator-alone decision)."""
    try:
        decisions = _questions_queue(args).orchestrator_alone_decisions()
    except QuestionError as exc:
        return _questions_fail(exc)
    if args.json:
        print(json.dumps([item.to_mapping() for item in decisions], sort_keys=True))
        return 0
    if not decisions:
        print("No Orchestrator-alone decision recorded.")
        return 0
    for item in decisions:
        print(
            f"{item.answered_at}  {item.question_id}  {item.decision_class.value}  "
            f"{item.words}"
        )
        print(f"    evidence : {item.evidence}")
        print(f"    rejected : {item.rejected_alternative}")
    return 0


def _job_payload(job: object) -> dict[str, Any]:
    from saberops.jobs.models import Job as _Job

    assert isinstance(job, _Job)
    return job.to_dict()


def handle_job_submit(args: argparse.Namespace) -> int:
    """Handle `orch job submit` (exit 3 when stored FAILED at submit)."""
    from saberops.db import Database as _Database
    from saberops.jobs.models import Effort as _Effort
    from saberops.jobs.models import JobRequirements as _Requirements
    from saberops.jobs.models import JobRole as _JobRole
    from saberops.jobs.models import Priority as _Priority
    from saberops.jobs.service import (
        IdempotencyConflictError as _IdempotencyConflict,
    )
    from saberops.jobs.service import (
        submit_job as _submit,
    )

    try:
        requirements = _Requirements(
            role=_JobRole(str(args.role)),
            effort=_Effort(str(args.effort).lower()) if args.effort else None,
            required_capabilities=tuple(args.capabilities or ()),
            read_only=bool(args.read_only),
            model_family_include=tuple(args.family_include or ()),
            model_family_exclude=tuple(args.family_exclude or ()),
            size_hint=args.size,
        )
    except ValueError as exc:
        print(f"[!] Invalid job requirements: {exc}", file=sys.stderr)
        return 2
    try:
        result = _submit(
            _Database(args.db),
            repo=str(args.repo),
            goal=str(args.goal),
            acceptance=args.acceptance,
            base_ref=str(args.base_ref),
            requirements=requirements,
            priority=_Priority(str(args.priority)),
            deadline=args.deadline,
            not_before=args.not_before,
            idempotency_key=args.idempotency_key,
            routing=None,
        )
    except ValueError as exc:
        print(f"[!] Invalid job: {exc}", file=sys.stderr)
        return 2
    except _IdempotencyConflict as exc:
        print(f"[!] Idempotency conflict: {exc}", file=sys.stderr)
        return 1
    payload = {"job": _job_payload(result.job), "created": result.created}
    failed = result.job.state.value == "FAILED"
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 3 if failed else 0
    print(f"Job        : {result.job.job_id}")
    print(f"State      : {result.job.state.value}")
    print(f"Created    : {'yes' if result.created else 'no (idempotent replay)'}")
    if failed:
        print(f"Reason     : {result.job.failure_reason}")
        return 3
    return 0


def handle_job_get(args: argparse.Namespace) -> int:
    """Handle `orch job get`."""
    from saberops.db import Database as _Database
    from saberops.jobs.service import get_job as _get

    try:
        job = _get(_Database(args.db), str(args.job_id))
    except ValueError as exc:
        print(f"[!] Invalid job get: {exc}", file=sys.stderr)
        return 2
    if job is None:
        print(f"Error: unknown job: {args.job_id}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(_job_payload(job), indent=2, sort_keys=True))
        return 0
    print(f"Job        : {job.job_id}")
    print(f"State      : {job.state.value}")
    print(f"Goal       : {job.goal}")
    print(f"Repo       : {job.repo} @ {job.base_ref} ({job.base_sha})")
    return 0


def handle_job_list(args: argparse.Namespace) -> int:
    """Handle `orch job list`."""
    from saberops.db import Database as _Database
    from saberops.jobs.service import list_jobs as _list

    try:
        jobs = _list(_Database(args.db), state=args.state, limit=int(args.limit))
    except ValueError as exc:
        print(f"[!] Invalid job list: {exc}", file=sys.stderr)
        return 2
    if getattr(args, "json", False):
        print(json.dumps({"jobs": [_job_payload(job) for job in jobs]}, indent=2))
        return 0
    if not jobs:
        print("No jobs.")
        return 0
    for job in jobs:
        print(f"{job.job_id} {job.state.value:<9} {job.priority.value:<10} {job.goal}")
    return 0


def handle_job_cancel(args: argparse.Namespace) -> int:
    """Handle `orch job cancel` (RUNNING cancels the live run first)."""
    from saberops.db import Database as _Database
    from saberops.jobs.service import JobNotFoundError as _NotFound
    from saberops.jobs.service import cancel_job as _cancel

    def _run_canceller(run_id: str) -> bool:
        return RunSupervisor(db=db_for_run(args, run_id)).cancel(run_id)

    try:
        result = _cancel(
            _Database(args.db), str(args.job_id), run_canceller=_run_canceller
        )
    except _NotFound:
        print(f"Error: unknown job: {args.job_id}", file=sys.stderr)
        return 1
    except ValueError as exc:
        print(f"[!] Invalid job cancel: {exc}", file=sys.stderr)
        return 2
    payload = {"job": _job_payload(result.job), "cancelled": result.cancelled}
    if getattr(args, "json", False):
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    print(f"Job        : {result.job.job_id}")
    print(f"State      : {result.job.state.value}")
    print(f"Cancelled  : {'yes' if result.cancelled else 'no'}")
    return 0


def _scheduler_storage_callbacks(
    db: Database,
) -> tuple[Any, Any]:
    """Build the GS-1 (maintenance, launch-guard) callbacks for the scheduler.

    On construction failure neither callback is ``None`` silently: the
    maintenance callback degrades to a typed reason and the guard degrades to
    the same, so mandatory storage checks are never quietly disabled.
    """
    try:
        from saberops.service import OrchestratorService
        from saberops.storage_maintenance import MaintenanceTrigger, StorageMaintenance

        service: StorageMaintenance = OrchestratorService(db=db).storage_maintenance(
            repo_root=Path.cwd().resolve()
        )
    except Exception:
        def _degraded(_trigger: str) -> str | None:
            return "STORAGE_MAINTENANCE_UNAVAILABLE"

        def _guarded() -> str | None:
            return "STORAGE_MAINTENANCE_UNAVAILABLE"

        return _degraded, _guarded

    def _maintenance(trigger: str) -> str | None:
        return service.run(MaintenanceTrigger(trigger)).infra_reason

    def _guard() -> str | None:
        return service.launch_guard_reason()

    return _maintenance, _guard


def handle_scheduler_run(args: argparse.Namespace) -> int:
    """Handle `orch scheduler run [--once] [--interval-seconds INT]`."""
    from saberops.capacity.loop import run_scheduler_loop
    from saberops.db import Database as _Database

    db_path = getattr(args, "db", None)
    db = _Database(db_path) if db_path else _Database()
    run_once = bool(getattr(args, "once", False))
    interval = float(getattr(args, "interval_seconds", 60.0) or 60.0)
    max_concurrent = getattr(args, "max_concurrent", None)

    readiness_svc: Any = None
    try:
        from saberops.model_access.readiness_service import ReadinessService
        from saberops.project import owner_state_dir

        readiness_svc = ReadinessService.for_owner_state(owner_state_dir())
    except Exception:
        readiness_svc = None

    try:
        storage_maintenance_cb, launch_guard_cb = _scheduler_storage_callbacks(db)
        result = run_scheduler_loop(
            db,
            tick_interval=interval,
            once=run_once,
            readiness_service=readiness_svc,
            max_concurrent_override=max_concurrent,
            storage_maintenance=storage_maintenance_cb,
            launch_guard=launch_guard_cb,
        )
        if result is None or (not result.ran and result.reason in ("lease_held_by_other", "lost_lease")):
            reason = result.reason if result is not None else "lease_held_by_other"
            print(f"[!] Scheduler lease held by another instance ({reason})", file=sys.stderr)
            return 2
        if not result.ran and result.reason == "invalid_global_concurrency":
            print(
                "[!] Scheduler aborted: invalid global concurrency configuration",
                file=sys.stderr,
            )
            return 2

        if result.storage_infra_reason is not None:
            # Typed storage/SQLite INFRA: park, never a code failure, never a pass.
            print(
                f"[!] Storage infrastructure park: {result.storage_infra_reason} "
                "(no worker or gate launched)",
                file=sys.stderr,
            )
            return 3

        if getattr(args, "json", False):
            payload: list[dict[str, Any]] = []
            if result is not None:
                for d in result.decisions:
                    job = getattr(d, "job", None)
                    entry: dict[str, Any] = {
                        "type": type(d).__name__,
                        "job_id": job.job_id if job is not None else None,
                    }
                    pool_id = getattr(d, "pool_id", None)
                    if pool_id is not None:
                        entry["pool_id"] = str(pool_id)
                    cand = getattr(d, "candidate", None)
                    if cand is not None:
                        entry["candidate"] = f"{cand.provider}/{cand.model}"
                    reason = getattr(d, "reason", None)
                    if reason is not None:
                        entry["reason"] = str(reason)
                    payload.append(entry)
            print(json.dumps(payload, indent=2))
        return 0
    except Exception as exc:
        print(f"[!] Scheduler loop failed: {exc}", file=sys.stderr)
        return 1


def handle_capacity_show(args: argparse.Namespace) -> int:
    """Handle `orch capacity show [--json]` (read-only)."""
    from dataclasses import asdict

    from saberops.db import Database as _Database
    from saberops.jobs.service import capacity as _capacity

    db_path = getattr(args, "db", None)
    # Open read-only to honour D5 lifecycle rule: create nothing, write nothing on read
    db = _Database(db_path, read_only=True) if db_path else _Database(read_only=True)

    try:
        cap_view = _capacity(db)
        if getattr(args, "json", False):
            print(json.dumps(asdict(cap_view), indent=2))
            return 0
        print(f"Generated at : {cap_view.generated_at}")
        print(f"Ledger wired : {cap_view.ledger_wired}")
        if not cap_view.pools:
            print("No capacity pools recorded.")
            return 0
        header = f"{'Pool':<18} {'Window':<8} {'Remaining':<12} {'Uncertainty':<12} {'Source':<10} {'Stale':<6} {'Excluded Until'}"
        print(header)
        print("-" * len(header))
        for p in cap_view.pools:
            rem = f"{p.remaining_est:.1f}%" if p.remaining_est is not None else "None"
            unc = f"±{p.uncertainty:.1f}%" if p.uncertainty is not None else "0.0%"
            src = p.source or "-"
            stl = "yes" if p.stale else "no"
            exc = p.excluded_until or "-"
            print(
                f"{p.pool_id:<18} {p.window_id:<8} {rem:<12} {unc:<12} {src:<10} {stl:<6} {exc}"
            )
        return 0
    except Exception as exc:
        print(f"[!] Capacity inspection failed: {exc}", file=sys.stderr)
        return 1


def handle_mcp(args: argparse.Namespace) -> int:
    """Handle `orch mcp [serve|tools]` (stdio MCP server for the D7 tools)."""
    from saberops.mcp.protocol import McpServer
    from saberops.mcp.stdio import build_server_context, serve_stdio

    action = str(getattr(args, "mcp_action", "serve") or "serve")
    if action in ("on", "off", "status"):
        return _handle_mcp_switch(args, action)
    transport = str(getattr(args, "transport", "stdio") or "stdio")
    if transport != "stdio":
        print(f"[!] unsupported MCP transport: {transport}", file=sys.stderr)
        return 2
    context = build_server_context(
        db_path=getattr(args, "db", None),
        lease_path=getattr(args, "lease", None),
        ledger_path=getattr(args, "ledger", None),
        questions_store=getattr(args, "questions_store", None),
        config_ledger=getattr(args, "config_ledger", None),
        cwd=getattr(args, "cwd", None),
    )
    if action == "tools":
        # Read-only inspection: never acquires the writer lease.
        server = McpServer(context, acquire_lease=False)
        descriptors = server.registry.descriptors()
        if getattr(args, "json", False):
            print(json.dumps({"tools": descriptors}, indent=2, sort_keys=True))
            return 0
        for descriptor in descriptors:
            print(f"{descriptor['name']:<22} {descriptor['description']}")
        return 0
    # Agent sessions start with SaberOps off; the user turns it on in the agent
    # ("turn on saberops" -> saberops_on). --active starts it on.
    server = McpServer(context, start_active=bool(getattr(args, "active", False)))
    if not server.active:
        print(
            "[i] SaberOps MCP ready and OFF for this session; ask the agent to turn it on.",
            file=sys.stderr,
        )
    elif not server.read_only:
        print(
            "[i] MCP writer lease acquired; owner-only operations stay unavailable.",
            file=sys.stderr,
        )
    else:
        print(
            "[i] MCP read-only session: another Orchestrator holds the writer lease.",
            file=sys.stderr,
        )
    return serve_stdio(server)


def _handle_mcp_switch(args: argparse.Namespace, action: str) -> int:
    """`orch mcp on|off|status`: the owner's agent-access switch."""
    from saberops.mcp.lease import OrchestratorLease, pid_is_alive
    from saberops.mcp.switch import mcp_enabled, set_mcp_enabled

    lease_path = Path(args.lease).expanduser() if getattr(args, "lease", None) else None
    if action in ("on", "off"):
        set_mcp_enabled(action == "on", lease_path)
    enabled = mcp_enabled(lease_path)
    lease = OrchestratorLease(lease_path) if lease_path else OrchestratorLease()
    record = lease.read()
    planner = (
        record.client or record.holder
        if enabled and record is not None and pid_is_alive(record.pid)
        else None
    )
    if getattr(args, "json", False):
        print(json.dumps({"enabled": enabled, "planner": planner}, indent=2, sort_keys=True))
        return 0
    if enabled:
        print("SaberOps agent access: ON")
        print(f"Planning agent       : {planner or 'none connected'}")
    else:
        print("SaberOps agent access: OFF (agents stay registered but see no SaberOps tools)")
        print("Turn it back on with : orch mcp on")
    return 0


def handle_connect(args: argparse.Namespace) -> int:
    """`orch connect [agent ...] [--remove] [--project [DIR]] [--dry-run]`."""
    from saberops.agent_connect import AGENTS, agent_rows, connect, disconnect

    wanted = [a.strip().lower() for a in (getattr(args, "agents", None) or []) if a.strip()]
    project = Path(args.project).expanduser().resolve() if args.project else None
    if not wanted:
        rows = agent_rows()
        if args.json:
            print(json.dumps({"agents": rows}, indent=2, sort_keys=True))
            return 0
        for row in rows:
            state = (
                "connected" if row["connected"]
                else "not connected" if row["installed"] else "not installed"
            )
            print(f"{row['name']:<12} {state}")
        print("\nConnect one with:  orch connect claude   (or: orch connect all)")
        return 0
    if wanted == ["all"]:
        wanted = [row["slug"] for row in agent_rows() if row["installed"]]
    unknown = [slug for slug in wanted if slug not in {a.slug for a in AGENTS}]
    if unknown:
        print(f"[!] Unknown agent(s): {', '.join(unknown)}", file=sys.stderr)
        return 2
    results = [
        disconnect(slug, project=project) if args.remove
        else connect(slug, project=project, dry_run=args.dry_run)
        for slug in wanted
    ]
    if args.json:
        print(json.dumps({"results": [r.__dict__ for r in results]}, indent=2, sort_keys=True))
    else:
        for result in results:
            print(("✓ " if result.ok else "✗ ") + result.message)
            for command in result.commands:
                print(f"    $ {command}")
            if result.snippet and not result.ok:
                print("    Add this by hand:")
                print("    " + result.snippet.replace("\n", "\n    "))
        if any(r.ok for r in results) and not args.remove and not args.dry_run:
            print("\nStart a new session in that agent; it becomes the agent that plans the work.")
    return 0 if all(r.ok for r in results) else 1


def handle_project_set_workflow(args: argparse.Namespace) -> int:
    """Handle `orch project set-workflow <preset> [--repo X]`."""
    from saberops.service import RunOperationError

    try:
        repo = _resolve_repo_for_project_settings(args)
    except SystemExit:
        return 2
    try:
        service = OrchestratorService()
        result = service.set_project_workflow(
            repo,
            str(args.preset),
            gate_enabled=True if args.gate_enabled is None else bool(args.gate_enabled),
            review=str(args.review) if args.review is not None else "risk-adaptive",
            mode=str(args.mode) if args.mode is not None else "bounded",
            limit=int(args.limit) if args.limit is not None else 3,
            gate_command=str(args.gate_command) if args.gate_command is not None else None,
        )
    except RunOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    print(f"Project workflow set to '{result['preset']}'")
    print(f"  gate   : {'enabled' if result['gate_enabled'] else 'disabled'} "
          f"({result['gate_effective'] or 'no gate'})")
    print(f"  review : {'disabled' if result['review_disabled'] else result['review_mode'] + ' x' + str(result['review_limit'])}")
    return 0


def _handle_run_operation(
    args: argparse.Namespace, operation: str, binding_id: str | None = None
) -> int:
    """Shared thin wrapper for `orch retry|continue|try-worker`."""
    from saberops.service import RunOperationError

    db = db_for_run(args, args.run_id)
    service = OrchestratorService(db=db)
    try:
        if operation == "retry":
            result = service.retry_run(str(args.run_id))
        elif operation == "continue":
            result = service.continue_run(str(args.run_id))
        else:
            result = service.try_worker(str(args.run_id), str(binding_id or ""))
    except RunOperationError as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 1
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    if result.get("already_continued"):
        print(f"Run {args.run_id} already has a continuation: {result['run_id']}")
        return 0
    print(f"Started {operation}: {result['run_id']} (from {result['source_run_id']})")
    print(f"    orch wait {result['run_id']} | orch status {result['run_id']}")
    return 0


def handle_retry(args: argparse.Namespace) -> int:
    """Handle `orch retry <run-id>`."""
    return _handle_run_operation(args, "retry")


def handle_continue(args: argparse.Namespace) -> int:
    """Handle `orch continue <run-id>`."""
    return _handle_run_operation(args, "continue")


def handle_try_worker(args: argparse.Namespace) -> int:
    """Handle `orch try-worker <run-id> --binding <id>`."""
    return _handle_run_operation(args, "try-worker", binding_id=str(args.binding))


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

    from saberops.cli_environment import prepare_cli_environment

    prepare_cli_environment()

    # Profile selection must happen before any SaberOps path/store is resolved.
    # It is a launch-time namespace choice only; every substantive owner choice
    # (providers, models, routing, Orchestrator, project and policy) stays in UI.
    profile = (getattr(args, "profile", None) or "").strip()
    if profile:
        os.environ["SABEROPS_PROFILE"] = profile
        from saberops.paths import active_profile

        active_profile()  # fail closed before constructing any state path

    from saberops.db import Database
    from saberops.model_access import AccessStoreError, ReadinessStoreError
    from saberops.web import create_app, initialize_account_readiness

    try:
        initialize_account_readiness()
    except (AccessStoreError, ReadinessStoreError):
        # Preserve unreadable owner state for repair; the Access page explains
        # unavailable evidence. Never reset it or block the dashboard from opening.
        print("Provider discovery could not read or save owner state; see Access.")
    app = create_app(db_path=args.db, repo_path=args.repo, seed_cwd_if_empty=False)
    startup_db = Database(args.db) if args.db else Database(get_default_db_path())
    url = f"http://{args.host}:{args.port}"
    from saberops.observability.run_report import saberops_version

    print("─" * 60)
    print(f"SaberOps {saberops_version()}")
    print(f"  URL      : {url}")
    print(f"  Database : {startup_db.db_path}")
    print(f"  Profile  : {profile or 'default'}")
    print(f"  Target   : {Path(args.repo).resolve() if args.repo else 'none (select in UI)'}")
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


def collect_doctor_findings(repo: Path, db_path: Path) -> list[DoctorFinding]:
    """Run every read-only doctor check (shared by the CLI and the web page)."""
    project_root = _orch_project_root()
    findings: list[DoctorFinding] = []

    findings.append(_check_python_requires(project_root))
    findings.append(_check_core_imports())
    findings.append(_check_dev_imports(project_root))
    findings.append(_check_venv_tools(project_root))
    findings.append(_check_git_binary())
    db_finding, quota_rows = _check_database(db_path)
    findings.append(db_finding)
    findings.extend(_check_providers(quota_rows))
    findings.append(_check_target_repo(repo))
    legacy_manager = _check_legacy_manager_state()
    if legacy_manager is not None:
        findings.append(legacy_manager)
    return findings


def handle_doctor(args: argparse.Namespace) -> int:
    """Handle `orch doctor` command. Strictly read-only: never mutates state."""
    db_arg = getattr(args, "db", None)
    db_path = Path(db_arg) if db_arg else get_default_db_path()
    findings = collect_doctor_findings(Path(getattr(args, "repo", ".")), db_path)

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
    normalized = list(sys.argv[1:] if argv is None else argv)
    # `orch plan show|validate ...` addresses the Step 015 plan store, while
    # `orch plan <run_id>` keeps its legacy run-plan contract. argparse cannot
    # mix an optional positional with subparsers (a run ID would be mistaken
    # for a subcommand), so the store verbs live on the `plan-store`
    # group and this shim rewrites only the exact `plan show|validate` prefix.
    # `orch plan --db X show` still means run ID "show" (escape hatch).
    if len(normalized) >= 2 and normalized[0] == "plan" and normalized[1] in ("show", "validate"):
        normalized = ["plan-store", *normalized[1:]]
    args = parser.parse_args(normalized)
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
        from saberops.project_scope.cli import handle_scope

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
    elif args.command == "gc":
        return handle_gc(args)
    elif args.command == "status":
        return handle_status(args)
    elif args.command == "plan":
        return handle_plan(args)
    elif args.command == "plan-store":
        if args.plan_store_command == "show":
            return handle_plan_show(args)
        elif args.plan_store_command == "validate":
            return handle_plan_validate(args)
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
    elif args.command == "report":
        return handle_report(args)
    elif args.command == "monitor":
        return handle_monitor(args)
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
    elif args.command == "reviewer":
        if args.reviewer_command == "show":
            return handle_reviewer_show(args)
        elif args.reviewer_command == "select":
            return handle_reviewer_select(args)
        elif args.reviewer_command == "clear":
            return handle_reviewer_clear(args)
    elif args.command == "orchestrator":
        if args.orchestrator_command == "show":
            return handle_orchestrator_show(args)
        elif args.orchestrator_command == "select":
            return handle_orchestrator_select(args)
        elif args.orchestrator_command == "clear":
            return handle_orchestrator_clear(args)
    elif args.command == "access":
        if args.access_command == "list":
            return handle_access_list(args)
        elif args.access_command == "show":
            return handle_access_show(args)
        elif args.access_command == "add":
            return handle_access_add(args)
        elif args.access_command == "test":
            return handle_access_test(args)
        elif args.access_command == "discover":
            return handle_access_discover(args)
        elif args.access_command == "verify-model":
            return handle_access_verify_model(args)
    elif args.command == "plans":
        if args.plans_command == "show":
            return handle_plans_show(args)
        elif args.plans_command == "validate":
            return handle_plans_validate(args)
    elif args.command == "posture":
        if args.posture_command == "show":
            return handle_posture_show(args)
        elif args.posture_command == "verify":
            return handle_posture_verify(args)
    elif args.command == "questions":
        if args.questions_command == "ask":
            return handle_questions_ask(args)
        elif args.questions_command == "list":
            return handle_questions_list(args)
        elif args.questions_command == "show":
            return handle_questions_show(args)
        elif args.questions_command == "answer":
            return handle_questions_answer(args)
        elif args.questions_command == "defer":
            return handle_questions_defer(args)
        elif args.questions_command == "moot":
            return handle_questions_moot(args)
        elif args.questions_command == "diverge":
            return handle_questions_diverge(args)
        elif args.questions_command == "divergences":
            return handle_questions_divergences(args)
        elif args.questions_command == "standing-rules":
            return handle_questions_standing_rules(args)
        elif args.questions_command == "audit":
            return handle_questions_audit(args)
    elif args.command == "job":
        if args.job_command == "submit":
            return handle_job_submit(args)
        elif args.job_command == "get":
            return handle_job_get(args)
        elif args.job_command == "list":
            return handle_job_list(args)
        elif args.job_command == "cancel":
            return handle_job_cancel(args)
    elif args.command == "scheduler":
        if args.scheduler_command == "run":
            return handle_scheduler_run(args)
    elif args.command == "capacity":
        if args.capacity_command == "show":
            return handle_capacity_show(args)
    elif args.command == "mcp":
        return handle_mcp(args)
    elif args.command == "connect":
        return handle_connect(args)
    elif args.command == "retry":
        return handle_retry(args)
    elif args.command == "continue":
        return handle_continue(args)
    elif args.command == "try-worker":
        return handle_try_worker(args)
    elif args.command == "routing":
        if args.routing_command == "show":
            return handle_routing_show(args)
        elif args.routing_command == "validate":
            return handle_routing_validate(args)
        elif args.routing_command == "init":
            return handle_routing_init(args)
        elif args.routing_command == "migrate":
            return handle_routing_migrate(args)
        elif args.routing_command == "add":
            return handle_routing_add(args)
        elif args.routing_command == "remove":
            return handle_routing_remove(args)
        elif args.routing_command == "move":
            return handle_routing_move(args)
        elif args.routing_command == "reorder":
            return handle_routing_reorder(args)
    elif args.command == "authority":
        if args.authority_command == "show":
            return handle_authority_show(args)
        elif args.authority_command == "list":
            return handle_authority_list(args)
        elif args.authority_command == "set":
            return handle_authority_set(args)
        elif args.authority_command == "set-host-access":
            return handle_authority_set_host_access(args)
    elif args.command == "project":
        if args.project_command == "gate-scratch":
            if args.project_gate_scratch_command == "show":
                return handle_project_gate_scratch_show(args)
            elif args.project_gate_scratch_command == "set":
                return handle_project_gate_scratch_set(args)
        elif args.project_command == "show":
            return handle_project_show(args)
        elif args.project_command == "set-gate":
            return handle_project_set_gate(args)
        elif args.project_command == "clear-gate":
            return handle_project_clear_gate(args)
        elif args.project_command == "set-data-policy":
            return handle_project_set_data_policy(args)
        elif args.project_command == "set-review-policy":
            return handle_project_set_review_policy(args)
        elif args.project_command == "set-target-branch":
            return handle_project_set_target_branch(args)
        elif args.project_command == "clear-target-branch":
            return handle_project_clear_target_branch(args)
        elif args.project_command == "set-workflow":
            return handle_project_set_workflow(args)
        elif args.project_command == "list":
            return handle_project_list(args)
        elif args.project_command == "register":
            return handle_project_register(args)
        elif args.project_command == "active":
            return handle_project_active(args)
        elif args.project_command == "activate":
            return handle_project_activate(args)
        elif args.project_command == "deactivate":
            return handle_project_deactivate(args)
    elif args.command == "certify":
        if args.certify_command == "check":
            return handle_certify_check(args)
        elif args.certify_command == "run":
            return handle_certify_run(args)
        elif args.certify_command == "report":
            return handle_certify_report(args)
        elif args.certify_command == "approve":
            return handle_certify_approve(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
