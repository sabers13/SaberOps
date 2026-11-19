"""Project setup diagnostics shared by the CLI and MCP."""

from __future__ import annotations

import os
import shlex
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from saberops.vendor_cli_fixtures import Manifest


@dataclass(frozen=True)
class Finding:
    """One readiness result and the owner action required to fix it."""

    severity: str
    message: str
    remedy: str = ""


def disk_findings(target: Path, scratch: Path, state: Path | None = None) -> list[Finding]:
    """Show each filesystem independently using the engine's disk floor."""
    from saberops.storage_maintenance import _nearest_existing, _statvfs, current_disk_floor_bytes

    findings: list[Finding] = []
    paths = [("target/gate", target), ("scratch", scratch)]
    if state is not None:
        paths.append(("scheduler state", state))
    for label, path in paths:
        anchor = _nearest_existing(path)
        remedy = (
            f"orch gc --apply --repo {shlex.quote(str(target))}"
            if label != "scratch"
            else "export SABEROPS_GATE_TMP=/path/on/a/filesystem/with/free/space"
        )
        if anchor is None:
            findings.append(Finding("FAIL", f"{label}: {path}: filesystem unknown", remedy))
            continue
        try:
            free, total = _statvfs(anchor)
        except OSError:
            findings.append(Finding("FAIL", f"{label}: {path}: filesystem unavailable", remedy))
            continue
        try:
            gate_minimum = float(os.environ.get("SABEROPS_GATE_MIN_FREE_GB", "10"))
        except ValueError:
            gate_minimum = 10.0
        required = current_disk_floor_bytes(
            total, gate_minimum if label == "scratch" else max(10.0, gate_minimum)
        )
        mount = anchor.resolve()
        while mount.parent != mount and not os.path.ismount(mount):
            mount = mount.parent
        findings.append(
            Finding(
                "OK" if free >= required else "FAIL",
                f"{label}: {path} (filesystem mounted at {mount}, probe {anchor}): "
                f"{free} bytes available; "
                f"{required} bytes required",
                "" if free >= required else remedy,
            )
        )
    return findings


def sign_in_remedy(provider: str, reason: str, connection: str) -> str:
    """Ask for login only when a status probe explicitly established it is needed."""
    check = f"orch access test {shlex.quote(connection)}"
    login = {"codex": "codex login", "anthropic": "claude auth login"}.get(provider)
    if reason == "NOT_AUTHENTICATED" and login:
        return f"{login} && {check}"
    return check


def vendor_cli_version_findings(
    provider: str, executable: str, version: str, manifest: Manifest
) -> list[Finding]:
    """Report version drift and the allowlisted-help update-guard state.

    Drift stays the existing ``WARN``.  The guard runs the allowlisted
    ``--help`` capture only for a drifted CLI; an ``UNSUPPORTED`` result (a
    complete capture omits a flag the adapter pins) is the clear ``FAIL``
    unsupported-version state.  An unverified probe stays a ``WARN`` so no
    dispatch decision is ever made on a guess.
    """
    from saberops.vendor_cli_fixtures import (
        VersionSupport,
        check_version_support,
        detect_drift,
    )

    relevant = [
        item
        for item in detect_drift(manifest, {executable: version})
        if item.cli == executable
    ]
    if not relevant:
        return []
    findings = [Finding("WARN", f"{provider} CLI version differs from recorded fixtures")]
    check = check_version_support(manifest, executable, version)
    if check.state is VersionSupport.UNSUPPORTED:
        findings.append(
            Finding(
                "FAIL",
                f"{provider} CLI version unsupported: "
                f"{', '.join(check.missing)} missing from --help",
                f"python -m saberops.vendor_cli_fixtures capture "
                f"--vendor {relevant[0].vendor} --out <outside-repo>",
            )
        )
    elif check.state is VersionSupport.SUPPORTED:
        findings.append(
            Finding(
                "OK",
                f"{provider} CLI update guard: recorded adapter flags still documented",
            )
        )
    else:
        findings.append(
            Finding("WARN", f"{provider} CLI update guard: not verified ({check.detail})")
        )
    return findings


def collect_preflight(repo: Path, db_path: Path | None = None) -> list[Finding]:
    """Inspect current project setup without sending a model prompt."""
    from datetime import UTC, date, datetime
    from types import SimpleNamespace

    from saberops._gate_tools import gate_tools
    from saberops.capacity.admission import matches_model_family
    from saberops.capacity.freshness import format_age, freshness
    from saberops.capacity.loop import scheduler_alive
    from saberops.capacity.snapshot import effective_quota_states
    from saberops.control_plane.binding_store import load_owner_bindings
    from saberops.control_plane.bindings import BindingRole
    from saberops.control_plane.plans import effective_quota_pools
    from saberops.control_plane.policy import load_control_plane_policy
    from saberops.control_plane.posture import posture_decision
    from saberops.control_plane.quota_policy import pools_for, quota_decision
    from saberops.control_plane.reviewer_binding import select_reviewer_binding
    from saberops.db import Database, get_default_db_path
    from saberops.git import GitManager
    from saberops.model_access.connections import ACCOUNT_BACKENDS
    from saberops.model_access.readiness_service import ReadinessService
    from saberops.models import ProviderReadiness, WorkerCandidate
    from saberops.project import compute_project_identity, owner_state_dir, project_state_dir
    from saberops.project_settings import (
        detect_authoritative_gate,
        load_project_settings_for_state_dir,
    )
    from saberops.routing_config import load_effective_routing
    from saberops.vendor_cli_fixtures import (
        MISSING,
        load_manifest,
        read_installed_version,
    )
    from saberops.workers import AdapterRegistry

    repo = repo.expanduser().resolve()
    quoted_repo = shlex.quote(str(repo))
    results: list[Finding] = []
    if not GitManager.is_git_repo(repo):
        results.append(
            Finding(
                "FAIL", f"project is not a Git repository: {repo}", f"git -C {quoted_repo} init"
            )
        )
    else:
        identity = compute_project_identity(repo)
        settings = load_project_settings_for_state_dir(project_state_dir(identity))
        gate = settings.gate_command or detect_authoritative_gate(repo)
        restore_workflow = (
            f"orch project set-workflow custom --repo {quoted_repo} "
            "--gate-enabled --review always-required"
        )
        if not settings.gate_enabled:
            results.append(Finding("FAIL", "project gate is disabled", restore_workflow))
        if settings.review_disabled:
            results.append(Finding("FAIL", "project review is disabled", restore_workflow))
        results.append(
            Finding(
                "OK" if gate else "FAIL",
                f"check command: {gate or 'missing'}",
                "" if gate else f"orch project set-gate --repo {quoted_repo} '<check command>'",
            )
        )

    results.extend(disk_findings(repo, gate_tools.disk_scratch_base(), owner_state_dir()))
    routing = load_effective_routing()
    policy = load_control_plane_policy()
    readiness = ReadinessService.for_owner_state(owner_state_dir())
    workers = [candidate for group in routing.groups_for("WORKER") for candidate in group]
    workers.sort(
        key=lambda c: (
            not (
                readiness.admission_for(provider=c.provider).state is ProviderReadiness.READY
                and posture_decision(
                    c.provider, c.model, plans=policy.plans, today=date.today()
                ).admitted_unattended
            )
        )
    )
    workers = workers[:1]
    results.append(
        Finding(
            "OK" if workers else "FAIL",
            "build route: "
            + (f"{workers[0].provider}/{workers[0].model}" if workers else "missing"),
            ""
            if workers
            else (
                "orch access discover codex-chatgpt-account && "
                "orch routing accept-default --provider codex"
            ),
        )
    )
    registry = AdapterRegistry.default()
    owner = load_owner_bindings()
    selection = select_reviewer_binding(owner, adapter_registry=registry)
    reviewers = [
        b for b in owner.registry.list_bindings() if b.binding_role is BindingRole.CHECK
    ]
    suggested_reviewer = next(
        (b for b in reviewers if not workers or b.provider != workers[0].provider), None
    )
    reviewer = selection.binding if selection.status.value == "RESOLVED" else None
    results.append(
        Finding(
            "OK" if reviewer else "FAIL",
            "reviewer route: "
            + (f"{reviewer.provider}/{reviewer.model}" if reviewer else "missing or unresolved"),
            ""
            if reviewer
            else (
                f"orch reviewer select {shlex.quote(suggested_reviewer.binding_id)}"
                if suggested_reviewer
                else "orch access discover claude-code-subscription"
            ),
        )
    )
    if reviewer and workers:
        review_candidate = WorkerCandidate(reviewer.provider, reviewer.model)
        same_model = workers[0].model.lower() == reviewer.model.lower()
        same_family = any(
            matches_model_family(workers[0], family)
            and matches_model_family(review_candidate, family)
            for family in ("gpt-5", "gpt-4", "deepseek", "claude", "gemini", "muse", "ox")
        )
        if same_model or same_family:
            results.append(
                Finding(
                    "FAIL",
                    "reviewer is not independent of the build route",
                    "orch reviewer select '<independent-binding-id>'",
                )
            )
    routes = [(c.provider, c.model) for c in workers]
    if reviewer is not None:
        routes.append((reviewer.provider, reviewer.model))
    # On an empty setup, show the two independent subscription paths as examples.
    if not workers:
        routes.append(("codex", ""))
    if reviewer is None:
        routes.append(
            ("codex" if workers and workers[0].provider == "anthropic" else "anthropic", "")
        )
    providers = list(dict.fromkeys(provider for provider, _ in routes))
    manifest = None
    try:
        manifest = load_manifest()
    except (OSError, ValueError):
        results.append(Finding("WARN", "vendor CLI fixture manifest unavailable"))
    for provider in providers:
        descriptor = next((d for d in ACCOUNT_BACKENDS if d.provider == provider), None)
        if descriptor is None:
            results.append(
                Finding("FAIL", f"{provider}: unsupported CLI readiness", "orch access list")
            )
            continue
        version = read_installed_version(descriptor.executable)
        installed = shutil.which(descriptor.executable) is not None
        version_display = (
            version
            if version != MISSING
            else ("installed; version unavailable" if installed else "missing")
        )
        results.append(
            Finding(
                "OK" if installed else "FAIL",
                f"{provider} CLI: {version_display}",
                ""
                if installed
                else {
                    "codex": "npm install -g @openai/codex",
                    "anthropic": "npm install -g @anthropic-ai/claude-code",
                    "opencode": "npm install -g opencode-ai",
                    "antigravity": "agy --help",
                }.get(provider, f"{descriptor.executable} --help"),
            )
        )
        if installed and manifest is not None:
            results.extend(
                vendor_cli_version_findings(provider, descriptor.executable, version, manifest)
            )
        admission = readiness.admission_for(provider=provider)
        ready = admission.state is ProviderReadiness.READY
        results.append(
            Finding(
                "OK" if ready else "FAIL",
                f"{provider} sign-in: {admission.state.value} ({admission.reason})",
                ""
                if ready
                else sign_in_remedy(provider, admission.reason, descriptor.connection_id),
            )
        )
    for provider, model in routes:
        if policy.provider(provider).state == "DISABLED":
            results.append(
                Finding(
                    "FAIL",
                    f"{provider} provider is blocked",
                    f"orch provider allow {shlex.quote(provider)}",
                )
            )
        decision = posture_decision(provider, model, plans=policy.plans, today=date.today())
        results.append(
            Finding(
                "OK" if decision.admitted_unattended else "FAIL",
                f"{provider}/{model or '*'} billing: {decision.reason or 'confirmed'}",
                ""
                if decision.admitted_unattended
                else (
                    f"orch posture verify {shlex.quote(provider)} "
                    "--class '<owner-confirmed-class>' --url '<policy-url>'"
                ),
            )
        )
    path = db_path or get_default_db_path()
    db = Database(path, read_only=True) if path.exists() else None
    rows = effective_quota_states(db, run_probes=False) if db else []
    policies = effective_quota_pools(policy)
    now = datetime.now(UTC)
    quota_routes: list[tuple[WorkerCandidate, str | None]] = [
        (candidate, None) for candidate in workers
    ]
    if reviewer is not None:
        quota_routes.append(
            (WorkerCandidate(reviewer.provider, reviewer.model), reviewer.quota_pool_id)
        )
    for candidate, binding_pool in quota_routes:
        for pool in [binding_pool] if binding_pool else pools_for(candidate, policies):
            relevant_rows = [
                row
                for row in rows
                if row.provider == candidate.provider and (row.pool_id or row.pool) == pool
            ]
            current = next(
                (
                    row
                    for row in relevant_rows
                    if (row.freshness or freshness(row.updated_at, row.window_id, row.source, now))
                    == "FRESH"
                ),
                None,
            )
            quota_result = quota_decision(
                candidate, rows, pool_policies=policies, pool_id=pool, now=now
            )
            ok = current is not None and quota_result.eligible
            age = format_age(
                current.updated_at
                if current
                else (relevant_rows[0].updated_at if relevant_rows else None),
                now,
            )
            quota_status = "available" if ok else "missing, stale or blocked"
            results.append(
                Finding(
                    "OK" if ok else "FAIL",
                    f"{candidate.provider}/{pool} quota: {quota_status} (age {age})",
                    ""
                    if ok
                    else (
                        f"orch quota set {shlex.quote(candidate.provider)} "
                        f"{shlex.quote(pool)} --remaining '<percent>'"
                    ),
                )
            )
    if not quota_routes:
        results.append(
            Finding(
                "FAIL", "quota: no routes to inspect", "orch access discover codex-chatgpt-account"
            )
        )
    alive = scheduler_alive(SimpleNamespace(db_path=path))
    results.append(
        Finding(
            "OK" if alive else "FAIL",
            f"scheduler: {'alive' if alive else 'not running'}",
            "" if alive else "orch scheduler run",
        )
    )
    return results
