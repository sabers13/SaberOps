"""Self-healing after a vendor CLI update (F18, ADR-0031).

F5 (:mod:`saberops.vendor_cli_fixtures`) records the version each adapter was
verified against and can tell a version change apart from a removed flag
without sending a prompt.  This module acts on that evidence:

* **Detect** -- compare every installed vendor CLI with its last verified
  version on scheduler start and periodically.  A change marks the vendor
  ``unverified`` and parks its providers for new work.
* **Contain** -- unverified, failed and pin-mismatched vendors never receive a
  new job; queued work routes to another admitted candidate or waits.  Running
  jobs are never touched here.
* **Check** -- run the allowlisted ``--help`` guard, then one tiny smoke prompt
  through the vendor's own worker adapter (the normal isolated path) in a
  throwaway repository.  The smoke is on by default, runs at most once per
  installed version, and is skipped while the F17 work gate is closed.
* **Heal** -- when both checks pass, record the new version as verified and
  resume routing with one notice.  When a check fails, file exactly one repair
  job on the SaberOps repository with the evidence and keep the vendor parked
  until the owner accepts a repair and ``orch self-update`` re-verifies.

The durable state lives in the owner DB so every process (scheduler, CLI, MCP)
sees the same verdict.  A store that cannot be read never blocks dispatch: the
engine never decides on a guess (ADR-0018 rule 4).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from saberops.capacity.store import owner_connection
from saberops.notifications import notify_once
from saberops.vendor_cli_fixtures import (
    MISSING,
    REPO_ROOT,
    VENDOR_CLI,
    Manifest,
    VersionSupport,
    check_version_support,
    load_manifest,
    read_installed_version,
)

if TYPE_CHECKING:
    from saberops.db import Database
else:  # pragma: no cover - typing only
    Database = Any  # type: ignore[assignment,misc]

__all__ = [
    "CHECK_INTERVAL_ENV",
    "CHECKS_ENV",
    "FAMILY_BY_VENDOR",
    "PROVIDER_BY_VENDOR",
    "SMOKE_PROMPT",
    "VENDORS",
    "SmokeResult",
    "VendorChange",
    "VendorCheckResult",
    "VendorHealth",
    "VendorState",
    "blocked_providers",
    "clear_pin",
    "detect_changes",
    "ensure_tables",
    "is_repair_job",
    "load_health",
    "maybe_check",
    "pinned_version",
    "render_status",
    "run_checks",
    "run_smoke_prompt",
    "set_pin",
    "set_smoke_enabled",
    "smoke_enabled",
]

#: Vendors this module tracks.  Antigravity stays attended-only (ADR-0001).
VENDORS: tuple[str, ...] = ("codex", "claude", "opencode")

#: Vendor -> provider name used by routing, admission and the adapters.
PROVIDER_BY_VENDOR: Mapping[str, str] = {
    "codex": "codex",
    "claude": "anthropic",
    "opencode": "opencode",
}

#: Vendor -> build-route family token a repair job must exclude (ADR-0024).
FAMILY_BY_VENDOR: Mapping[str, str] = {
    "codex": "codex",
    "claude": "anthropic",
    "opencode": "opencode-go",
}

#: The one prompt the automatic check may send.  It is tiny and answer-only.
SMOKE_PROMPT: str = "Reply with the single word OK."

#: Environment switch: ``1`` disables the periodic detection step entirely.
CHECKS_ENV: str = "SABEROPS_VENDOR_CHECKS"

#: Environment switch: seconds between periodic detection passes.
CHECK_INTERVAL_ENV: str = "SABEROPS_VENDOR_CHECK_INTERVAL"
DEFAULT_CHECK_INTERVAL_SECONDS: float = 600.0

#: Environment switch: seconds the detached check subprocess may run.
CHECK_TIMEOUT_ENV: str = "SABEROPS_VENDOR_CHECK_TIMEOUT"

#: Environment switch: seconds one smoke prompt may run.
SMOKE_TIMEOUT_ENV: str = "SABEROPS_VENDOR_SMOKE_TIMEOUT"
DEFAULT_SMOKE_TIMEOUT_SECONDS: float = 180.0

#: Owner-directory body prefix that marks a repair job as an engine change
#: that waits for an owner accept (never auto-accepted).
REPAIR_GOAL_PREFIX: str = "[SELF-HEAL REPAIR]"
REPAIR_EVENT: str = "self_heal_repair"
REPAIR_CALLER: str = "vendor-health"


class VendorState(StrEnum):
    """Durable health verdict for one vendor CLI."""

    #: The installed version is the version the adapter was verified against.
    VERIFIED = "verified"
    #: A version change was detected and the automatic check is outstanding.
    UNVERIFIED = "unverified"
    #: A check failed; the vendor stays parked until a repair lands.
    FAILED = "failed"
    #: The owner pinned a version and the installed CLI does not match it.
    PINNED_MISMATCH = "pinned_mismatch"
    #: The CLI is not installed.  Nothing is parked on a missing binary.
    NOT_INSTALLED = "not_installed"


#: Only these states stop new work on the vendor's providers.
BLOCKING_STATES: frozenset[VendorState] = frozenset(
    {VendorState.UNVERIFIED, VendorState.FAILED, VendorState.PINNED_MISMATCH}
)


@dataclass(frozen=True)
class VendorHealth:
    """One vendor's durable health record."""

    vendor: str
    cli: str
    installed_version: str = ""
    verified_version: str = ""
    state: VendorState = VendorState.UNVERIFIED
    reason: str = ""
    detail: dict[str, Any] = field(default_factory=dict)
    repair_job_id: str | None = None
    smoke_version: str | None = None
    notified_version: str | None = None
    changed_at: str | None = None
    checked_at: str | None = None

    @property
    def blocks_dispatch(self) -> bool:
        return (
            self.state in BLOCKING_STATES
            and self.installed_version not in ("", MISSING)
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "cli": self.cli,
            "installed_version": self.installed_version,
            "verified_version": self.verified_version,
            "state": self.state.value,
            "reason": self.reason,
            "detail": dict(self.detail),
            "repair_job_id": self.repair_job_id,
            "smoke_version": self.smoke_version,
            "notified_version": self.notified_version,
            "changed_at": self.changed_at,
            "checked_at": self.checked_at,
            "blocks_dispatch": self.blocks_dispatch,
        }


@dataclass(frozen=True)
class VendorChange:
    """A detected change, before any expensive check runs."""

    vendor: str
    cli: str
    previous_version: str
    installed_version: str
    state: VendorState

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "cli": self.cli,
            "previous_version": self.previous_version,
            "installed_version": self.installed_version,
            "state": self.state.value,
        }


@dataclass(frozen=True)
class SmokeResult:
    """Outcome of the one tiny prompt (or the reason it was skipped)."""

    passed: bool
    detail: str
    provider: str = ""
    model: str = ""
    usage_rows: int = 0
    reported_model: str | None = None
    skipped: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "skipped": self.skipped,
            "detail": self.detail,
            "provider": self.provider,
            "model": self.model,
            "usage_rows": self.usage_rows,
            "reported_model": self.reported_model,
        }


@dataclass(frozen=True)
class VendorCheckResult:
    """Outcome of checking one vendor."""

    vendor: str
    state: VendorState
    detail: str
    repair_job_id: str | None = None

    @property
    def healed(self) -> bool:
        return self.state is VendorState.VERIFIED

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor,
            "state": self.state.value,
            "detail": self.detail,
            "repair_job_id": self.repair_job_id,
        }


SmokeRunner = Callable[[str, str | None], SmokeResult]


# ---------------------------------------------------------------------------
# Durable store
# ---------------------------------------------------------------------------


def ensure_tables(db: Database) -> None:
    """Create the health and settings tables idempotently."""
    with owner_connection(db) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_cli_health (
                vendor TEXT PRIMARY KEY,
                cli TEXT NOT NULL,
                installed_version TEXT NOT NULL DEFAULT '',
                verified_version TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                reason TEXT NOT NULL DEFAULT '',
                detail_json TEXT NOT NULL DEFAULT '{}',
                repair_job_id TEXT,
                smoke_version TEXT,
                notified_version TEXT,
                changed_at TEXT,
                checked_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_cli_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS vendor_cli_check_claim (
                claim TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                started_at TEXT NOT NULL
            )
            """
        )


def _now_iso(now: datetime | None = None) -> str:
    moment = now.astimezone(UTC) if now is not None else datetime.now(UTC)
    return moment.isoformat(timespec="seconds")


def _row_health(row: Any) -> VendorHealth:
    try:
        detail = json.loads(row["detail_json"] or "{}")
    except ValueError:
        detail = {}
    return VendorHealth(
        vendor=str(row["vendor"]),
        cli=str(row["cli"]),
        installed_version=str(row["installed_version"] or ""),
        verified_version=str(row["verified_version"] or ""),
        state=VendorState(str(row["state"])),
        reason=str(row["reason"] or ""),
        detail=detail if isinstance(detail, dict) else {},
        repair_job_id=row["repair_job_id"],
        smoke_version=row["smoke_version"],
        notified_version=row["notified_version"],
        changed_at=row["changed_at"],
        checked_at=row["checked_at"],
    )


def load_health(db: Database) -> dict[str, VendorHealth]:
    """Read every durable vendor record (empty when the tables are absent)."""
    try:
        ensure_tables(db)
        with owner_connection(db) as conn:
            rows = conn.execute("SELECT * FROM vendor_cli_health").fetchall()
    except Exception:  # noqa: BLE001 - an unreadable store never blocks work
        logging.getLogger(__name__).exception("could not read vendor CLI health")
        return {}
    return {_row_health(row).vendor: _row_health(row) for row in rows}


def save_health(db: Database, health: VendorHealth) -> None:
    """Upsert one vendor record."""
    ensure_tables(db)
    with owner_connection(db) as conn:
        conn.execute(
            """
            INSERT INTO vendor_cli_health (
                vendor, cli, installed_version, verified_version, state, reason,
                detail_json, repair_job_id, smoke_version, notified_version,
                changed_at, checked_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(vendor) DO UPDATE SET
                cli = excluded.cli,
                installed_version = excluded.installed_version,
                verified_version = excluded.verified_version,
                state = excluded.state,
                reason = excluded.reason,
                detail_json = excluded.detail_json,
                repair_job_id = excluded.repair_job_id,
                smoke_version = excluded.smoke_version,
                notified_version = excluded.notified_version,
                changed_at = excluded.changed_at,
                checked_at = excluded.checked_at
            """,
            (
                health.vendor,
                health.cli,
                health.installed_version,
                health.verified_version,
                health.state.value,
                health.reason,
                json.dumps(health.detail, sort_keys=True),
                health.repair_job_id,
                health.smoke_version,
                health.notified_version,
                health.changed_at,
                health.checked_at,
            ),
        )


def _setting(db: Database, key: str) -> str | None:
    try:
        ensure_tables(db)
        with owner_connection(db) as conn:
            row = conn.execute(
                "SELECT value FROM vendor_cli_settings WHERE key = ?", (key,)
            ).fetchone()
    except Exception:  # noqa: BLE001 - settings fall back to their default
        logging.getLogger(__name__).exception("could not read vendor CLI setting %s", key)
        return None
    return None if row is None else str(row["value"])


def _set_setting(db: Database, key: str, value: str | None) -> None:
    ensure_tables(db)
    with owner_connection(db) as conn:
        if value is None:
            conn.execute("DELETE FROM vendor_cli_settings WHERE key = ?", (key,))
        else:
            conn.execute(
                "INSERT INTO vendor_cli_settings (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (key, value),
            )


def smoke_enabled(db: Database) -> bool:
    """Whether the automatic smoke prompt is on (it is on by default)."""
    raw = _setting(db, "smoke_enabled")
    return raw is None or raw != "0"


def set_smoke_enabled(db: Database, enabled: bool) -> None:
    _set_setting(db, "smoke_enabled", "1" if enabled else "0")


def pinned_version(db: Database, vendor: str) -> str | None:
    raw = _setting(db, f"pin:{vendor}")
    return raw or None


def set_pin(db: Database, vendor: str, version: str) -> None:
    if vendor not in VENDORS:
        raise ValueError(f"unknown vendor {vendor!r}")
    if not version.strip():
        raise ValueError("pinned version must be a non-empty string")
    _set_setting(db, f"pin:{vendor}", version.strip())


def clear_pin(db: Database, vendor: str) -> None:
    _set_setting(db, f"pin:{vendor}", None)


def smoke_model(db: Database, vendor: str) -> str | None:
    raw = _setting(db, f"smoke_model:{vendor}")
    return raw or None


def set_smoke_model(db: Database, vendor: str, model: str | None) -> None:
    if vendor not in VENDORS:
        raise ValueError(f"unknown vendor {vendor!r}")
    _set_setting(db, f"smoke_model:{vendor}", (model or "").strip() or None)


def blocked_providers(db: Database) -> dict[str, str]:
    """Provider -> one-line reason for every provider new work must avoid.

    Returns an empty mapping when the store is unreadable: containment never
    blocks dispatch on a guess (ADR-0018 rule 4).
    """
    blocked: dict[str, str] = {}
    for health in load_health(db).values():
        if not health.blocks_dispatch:
            continue
        provider = PROVIDER_BY_VENDOR.get(health.vendor)
        if provider is None:
            continue
        detail = health.reason or health.state.value
        blocked[provider] = (
            f"{health.cli} {health.installed_version} is {health.state.value}: {detail}"
        )
    return blocked


# ---------------------------------------------------------------------------
# Notices
# ---------------------------------------------------------------------------


def _notice_ledger(db: Database) -> Path:
    owner = getattr(db, "owner_db", db)
    path = Path(owner.db_path)
    return path.with_name(path.name + ".notifications.sqlite3")


def _notify(db: Database, key: str, title: str, body: str) -> None:
    try:
        notify_once(_notice_ledger(db), key, title, body)
    except Exception:  # noqa: BLE001 - a notice never breaks the check
        logging.getLogger(__name__).exception("vendor CLI notice failed")


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def _expected_version(manifest: Manifest, cli: str) -> str | None:
    versions = {
        fixture.cli_version
        for fixture in manifest.fixtures
        if fixture.cli == cli and fixture.cli_version != "unknown"
    }
    return sorted(versions)[-1] if versions else None


def _vendor_for_cli(manifest: Manifest, cli: str) -> str:
    for fixture in manifest.fixtures:
        if fixture.cli == cli:
            return fixture.vendor
    return cli


def detect_changes(
    db: Database,
    *,
    manifest: Manifest | None = None,
    installed: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    now: datetime | None = None,
) -> list[VendorChange]:
    """Compare installed versions with the last verified ones (no prompts).

    The first pass seeds each vendor from the checked-in F5 manifest, so the
    feature starts from the interface the adapters were verified against.  A
    change marks the vendor unverified (or pin-mismatched) and sends exactly
    one notice per new version.  A rollout back to the verified version heals
    the vendor immediately without any check.
    """
    loaded = manifest if manifest is not None else load_manifest()
    health = load_health(db)
    observed = dict(installed) if installed is not None else {
        cli: read_installed_version(cli, which=which) for cli in loaded.checked_clis()
    }
    changes: list[VendorChange] = []
    changed_at = _now_iso(now)
    for cli in loaded.checked_clis():
        vendor = _vendor_for_cli(loaded, cli)
        if vendor not in VENDORS:
            continue
        expected = _expected_version(loaded, cli) or ""
        installed_version = observed.get(cli, MISSING)
        is_new = vendor not in health
        previous = health.get(vendor) or VendorHealth(
            vendor=vendor,
            cli=cli,
            verified_version=expected,
        )
        if installed_version == MISSING:
            updated = replace(
                previous,
                installed_version=MISSING,
                state=VendorState.NOT_INSTALLED,
                reason="CLI is not installed",
                detail={},
                smoke_version=None,
            )
        elif pinned := pinned_version(db, vendor):
            if installed_version == pinned:
                updated = replace(
                    previous,
                    installed_version=installed_version,
                    verified_version=pinned,
                    state=VendorState.VERIFIED,
                    reason="installed version matches the pinned version",
                    detail={},
                )
            else:
                updated = replace(
                    previous,
                    installed_version=installed_version,
                    state=VendorState.PINNED_MISMATCH,
                    reason=(
                        f"pinned {pinned}, installed {installed_version}"
                    ),
                    detail={"pinned": pinned},
                )
        elif installed_version == previous.verified_version:
            updated = replace(
                previous,
                installed_version=installed_version,
                state=VendorState.VERIFIED,
                reason="installed version matches the last verified version",
                detail={},
            )
        elif installed_version == previous.installed_version:
            # Same version as the previous pass: keep its verdict untouched.
            updated = previous
        else:
            updated = replace(
                previous,
                installed_version=installed_version,
                state=VendorState.UNVERIFIED,
                reason=(
                    "version changed from "
                    f"{previous.installed_version or previous.verified_version or 'unknown'} "
                    f"to {installed_version}"
                ),
                detail={
                    "previous_version": previous.installed_version
                    or previous.verified_version
                },
                smoke_version=None,
            )
        if (
            updated.state != previous.state
            or updated.installed_version != previous.installed_version
        ) and not (
            is_new
            and updated.state is VendorState.VERIFIED
            and installed_version == expected
        ):
            updated = replace(updated, changed_at=changed_at)
            changes.append(
                VendorChange(
                    vendor=vendor,
                    cli=cli,
                    previous_version=previous.installed_version or previous.verified_version,
                    installed_version=installed_version,
                    state=updated.state,
                )
            )
        if updated.state is VendorState.UNVERIFIED:
            if updated.notified_version != installed_version:
                _notify(
                    db,
                    f"vendor:{vendor}:unverified:{installed_version}",
                    f"SaberOps parked {vendor}: CLI update needs a check",
                    (
                        f"{cli} reports {installed_version} (last verified "
                        f"{previous.verified_version}). New work on {vendor} waits "
                        "until the interface and one smoke prompt pass."
                    ),
                )
                updated = replace(updated, notified_version=installed_version)
        elif updated.state is VendorState.PINNED_MISMATCH:
            if updated.notified_version != installed_version:
                _notify(
                    db,
                    f"vendor:{vendor}:pin:{installed_version}",
                    f"SaberOps parked {vendor}: pinned version changed",
                    (
                        "The owner pinned "
                        f"{pinned_version(db, vendor) or 'a version'} but the CLI reports "
                        f"{installed_version}. Update the pin or reinstall the pinned CLI."
                    ),
                )
                updated = replace(updated, notified_version=installed_version)
        elif (
            not is_new
            and updated.state is VendorState.VERIFIED
            and previous.state in BLOCKING_STATES
        ):
            if updated.notified_version != installed_version:
                _notify(
                    db,
                    f"vendor:{vendor}:verified:{installed_version}",
                    f"SaberOps resumed {vendor}",
                    f"{cli} {installed_version} is the verified interface again.",
                )
                updated = replace(updated, notified_version=installed_version)
        save_health(db, updated)
        health[vendor] = updated
    return changes


# ---------------------------------------------------------------------------
# Smoke prompt
# ---------------------------------------------------------------------------


def _smoke_repository(root: Path, vendor: str) -> Path:
    """Create a throwaway git repository the adapter can work in."""
    path = root / f"smoke-{vendor}-{os.getpid()}-{int(time.time() * 1000)}"
    path.mkdir(parents=True, exist_ok=True)
    (path / "README.md").write_text("SaberOps vendor smoke\n", encoding="utf-8")
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "SaberOps",
        "GIT_AUTHOR_EMAIL": "saberops@localhost",
        "GIT_COMMITTER_NAME": "SaberOps",
        "GIT_COMMITTER_EMAIL": "saberops@localhost",
    }
    subprocess.run(
        ["git", "init", "-q"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    subprocess.run(
        ["git", "add", "README.md"], cwd=path, check=True, capture_output=True, text=True, env=env
    )
    subprocess.run(
        ["git", "commit", "-q", "-m", "smoke"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
        env=env,
    )
    return path


def run_smoke_prompt(
    vendor: str,
    model: str | None,
    *,
    worktree_root: Path | str | None = None,
    timeout_seconds: float | None = None,
    adapter: Any = None,
) -> SmokeResult:
    """Send ONE tiny prompt through the vendor's own adapter and check its shape.

    This is the normal isolated worker path: the adapter applies its hermetic
    environment and vendor isolation, and the run happens in a throwaway
    repository that is deleted afterwards.  Success needs a successful exit
    with at least one parsed usage row carrying token counts; identity is
    recorded but may legitimately be ``unreported`` (OpenCode 2.3.2).
    """
    provider = PROVIDER_BY_VENDOR.get(vendor, vendor)
    from saberops.models import AttemptStatus, WorkerRequest
    from saberops.workers.registry import AdapterRegistry

    if adapter is None:
        adapter = AdapterRegistry.default().get(provider)
    if adapter is None:
        return SmokeResult(
            passed=False,
            detail=f"no worker adapter for provider {provider!r}",
            provider=provider,
        )
    resolved_model = model
    if not resolved_model:
        from saberops.routing_config import load_effective_routing

        try:
            resolved_model = cheapest_plan_model(load_effective_routing(), vendor)
        except Exception:  # noqa: BLE001 - routing errors are reported, not raised
            resolved_model = None
    if not resolved_model:
        return SmokeResult(
            passed=True,
            skipped=True,
            detail=(
                f"no plan model is routed for {vendor}; interface check only "
                "(one smoke prompt is still spent for this version)"
            ),
            provider=provider,
        )
    timeout = timeout_seconds or _smoke_timeout()
    root = Path(worktree_root) if worktree_root is not None else _smoke_root()
    root.mkdir(parents=True, exist_ok=True)
    worktree: Path | None = None
    try:
        worktree = _smoke_repository(root, vendor)
        request = WorkerRequest(
            task=SMOKE_PROMPT,
            worktree_path=worktree,
            model=resolved_model,
            timeout_seconds=timeout,
            persist_native_session=False,
        )
        result = adapter.run(request)
    except Exception as exc:  # noqa: BLE001 - evidence, never a crash
        return SmokeResult(
            passed=False,
            detail=f"smoke run raised {type(exc).__name__}: {exc}",
            provider=provider,
            model=resolved_model,
        )
    finally:
        if worktree is not None:
            shutil.rmtree(worktree, ignore_errors=True)
    usage_rows = len(result.usage)
    known = [
        row
        for row in result.usage
        if row.input_tokens is not None or row.output_tokens is not None
    ]
    detail = (
        f"exit={result.provider_exit_code} status={result.status.value} "
        f"usage_rows={usage_rows} known_rows={len(known)} "
        f"reported_model={result.actual_model or 'unreported'}"
    )
    passed = result.status is AttemptStatus.SUCCESS and bool(known)
    if not passed and result.error:
        detail = f"{detail}; error={result.error}"
    return SmokeResult(
        passed=passed,
        detail=detail,
        provider=provider,
        model=resolved_model,
        usage_rows=usage_rows,
        reported_model=result.actual_model,
    )


def _smoke_root() -> Path:
    from saberops.paths import saberops_state_dir

    return saberops_state_dir() / "vendor-health" / "smoke"


def _smoke_timeout() -> float:
    raw = os.environ.get(SMOKE_TIMEOUT_ENV)
    if raw:
        try:
            return max(10.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_SMOKE_TIMEOUT_SECONDS


def cheapest_plan_model(snapshot: Any, vendor: str) -> str | None:
    """Return the vendor's least-preferred routed plan model.

    Route chains list the owner's preferred worker first, so the last
    candidate for a vendor is the cheapest one the owner admitted.  An owner
    can name an exact smoke model with ``orch vendor smoke-model``.
    """
    provider = PROVIDER_BY_VENDOR.get(vendor, vendor)
    candidates = [
        candidate
        for candidate in snapshot.chain_for_role("BUILD")
        if candidate.provider == provider
    ]
    if not candidates:
        return None
    model: str = candidates[-1].model
    return model


# ---------------------------------------------------------------------------
# Checks, healing and repair
# ---------------------------------------------------------------------------


def _repair_goal(health: VendorHealth, evidence: Mapping[str, Any]) -> str:
    lines = [
        f"{REPAIR_GOAL_PREFIX} Adapt SaberOps to {health.cli} {health.installed_version}",
        "",
        f"The installed {health.cli} reports {health.installed_version}; the last "
        f"verified version was {health.verified_version}.",
        f"Failing check: {health.reason}",
        "",
        "Evidence:",
        json.dumps(evidence, indent=2, sort_keys=True),
        "",
        "Restore the pinned interface or adapt the adapter and its parsers, keep the "
        "full gate green, and add a regression test. This changes the engine, so the "
        "owner accepts it explicitly; it is never auto-accepted.",
    ]
    return "\n".join(lines)


def _repair_acceptance(health: VendorHealth) -> str:
    return (
        f"SaberOps adapts to {health.cli} {health.installed_version}: the recorded "
        "interface check and the smoke prompt pass against the new version, the full "
        "gate is green, and a regression test covers the change."
    )


def _file_repair_job(
    db: Database,
    health: VendorHealth,
    evidence: Mapping[str, Any],
    *,
    engine_repo: Path | str | None,
) -> str | None:
    from saberops.jobs import store
    from saberops.jobs.models import JobRequirements
    from saberops.jobs.service import SubmitResult, submit_job

    repo = Path(engine_repo) if engine_repo is not None else REPO_ROOT
    try:
        result: SubmitResult = submit_job(
            db,
            repo=repo,
            goal=_repair_goal(health, evidence),
            acceptance=_repair_acceptance(health),
            requirements=JobRequirements(
                model_family_exclude=(FAMILY_BY_VENDOR.get(health.vendor, health.vendor),),
                size_hint="M",
            ),
            idempotency_key=f"vendor-repair:{health.vendor}:{health.installed_version}",
            caller_id=REPAIR_CALLER,
        )
    except Exception:  # noqa: BLE001 - a repair failure is retried next check
        logging.getLogger(__name__).exception("could not file the vendor repair job")
        return None
    job = result.job
    if result.created:
        try:
            store.append_event(
                db,
                job.job_id,
                REPAIR_EVENT,
                {
                    "vendor": health.vendor,
                    "installed_version": health.installed_version,
                    "verified_version": health.verified_version,
                    "requires_owner_accept": True,
                },
            )
        except Exception:  # noqa: BLE001 - the job still exists
            logging.getLogger(__name__).exception("could not record the repair marker")
    return job.job_id


def is_repair_job(db: Database, job_id: str) -> bool:
    """True when ``job_id`` is a self-heal engine repair awaiting owner review."""
    from saberops.jobs import store

    try:
        events = store.list_events(db, job_id)
    except Exception:  # noqa: BLE001 - unknown means "not proven"
        return False
    return any(event.get("kind") == REPAIR_EVENT for event in events)


def _fail(
    db: Database,
    health: VendorHealth,
    *,
    reason: str,
    evidence: Mapping[str, Any],
    engine_repo: Path | str | None,
    now: datetime | None,
    smoke_ran: bool = False,
) -> VendorCheckResult:
    job_id = health.repair_job_id
    if job_id is None:
        job_id = _file_repair_job(db, health, evidence, engine_repo=engine_repo)
    updated = replace(
        health,
        state=VendorState.FAILED,
        reason=reason,
        detail=dict(evidence),
        repair_job_id=job_id,
        checked_at=_now_iso(now),
        # One smoke prompt per installed version, even when it failed: the
        # post-repair re-check must not spend a second one.
        smoke_version=health.installed_version if smoke_ran else health.smoke_version,
    )
    save_health(db, updated)
    if health.notified_version != f"failed:{health.installed_version}":
        body = (
            f"{health.cli} {health.installed_version}: {reason}. New work on "
            f"{health.vendor} stays parked until the repair is accepted."
        )
        if job_id:
            body += f"\nRepair job: {job_id}"
        _notify(
            db,
            f"vendor:{health.vendor}:failed:{health.installed_version}",
            f"SaberOps could not adapt to {health.vendor}",
            body,
        )
        updated = replace(updated, notified_version=f"failed:{health.installed_version}")
        save_health(db, updated)
    return VendorCheckResult(
        vendor=health.vendor,
        state=VendorState.FAILED,
        detail=reason,
        repair_job_id=job_id,
    )


def _heal(
    db: Database,
    health: VendorHealth,
    *,
    reason: str,
    evidence: Mapping[str, Any],
    now: datetime | None,
) -> VendorCheckResult:
    """Record the new version as verified (the runtime fixture record)."""
    updated = replace(
        health,
        state=VendorState.VERIFIED,
        verified_version=health.installed_version,
        reason=reason,
        detail=dict(evidence),
        repair_job_id=None,
        smoke_version=health.installed_version,
        notified_version=f"verified:{health.installed_version}",
        checked_at=_now_iso(now),
    )
    save_health(db, updated)
    _notify(
        db,
        f"vendor:{health.vendor}:healed:{health.installed_version}",
        f"SaberOps resumed {health.vendor}",
        (
            f"{health.cli} {health.installed_version} passed the interface check and "
            "the smoke prompt. Routing resumed."
        ),
    )
    return VendorCheckResult(
        vendor=health.vendor,
        state=VendorState.VERIFIED,
        detail=reason,
    )


def run_checks(
    db: Database,
    *,
    vendors: Sequence[str] | None = None,
    manifest: Manifest | None = None,
    which: Callable[[str], str | None] = shutil.which,
    help_run: Any = None,
    smoke: SmokeRunner | None = None,
    engine_repo: Path | str | None = None,
    work_allowed: Callable[[], bool] | None = None,
    now: datetime | None = None,
    force: bool = False,
) -> list[VendorCheckResult]:
    """Check every unverified vendor and heal or park it.

    The allowlisted ``--help`` guard runs first.  Only a complete capture that
    omits an adapter-pinned flag is a failure; an unreadable probe stays
    unverified and parks the vendor without filing a repair job.  When the
    help guard passes, one smoke prompt runs (at most once per installed
    version, skipped while the F17 gate is closed or when the owner turned the
    smoke off).
    """
    loaded = manifest if manifest is not None else load_manifest()
    health = load_health(db)
    allowed = work_allowed if work_allowed is not None else _gate_allows_work(db)
    results: list[VendorCheckResult] = []
    for vendor in (vendors or tuple(VENDORS)):
        if vendor not in VENDORS:
            raise ValueError(f"unknown vendor {vendor!r}")
        record = health.get(vendor)
        if record is None:
            continue
        if record.state in (VendorState.VERIFIED, VendorState.NOT_INSTALLED):
            if not (
                force
                and record.state is VendorState.VERIFIED
                and record.installed_version not in ("", MISSING)
            ):
                continue
        if record.state is VendorState.PINNED_MISMATCH:
            results.append(
                VendorCheckResult(
                    vendor=vendor,
                    state=record.state,
                    detail=record.reason,
                    repair_job_id=record.repair_job_id,
                )
            )
            continue
        check = check_version_support(
            loaded, record.cli, record.installed_version, which=which, run=help_run
        )
        if check.state is VersionSupport.UNSUPPORTED:
            evidence = {
                "version": record.installed_version,
                "verified_version": record.verified_version,
                "check": "allowlisted --help",
                "missing_flags": list(check.missing),
                "commands": list(check.commands),
                "detail": check.detail,
            }
            results.append(
                _fail(
                    db,
                    record,
                    reason=(
                        "adapter flags missing from --help: " + ", ".join(check.missing)
                    ),
                    evidence=evidence,
                    engine_repo=engine_repo,
                    now=now,
                )
            )
            continue
        if check.state is VersionSupport.UNVERIFIED:
            updated = replace(
                record,
                state=VendorState.UNVERIFIED,
                reason=check.detail or "interface could not be verified",
                detail={
                    "version": record.installed_version,
                    "check": "allowlisted --help",
                    "detail": check.detail,
                    "commands": list(check.commands),
                },
                checked_at=_now_iso(now),
            )
            save_health(db, updated)
            results.append(
                VendorCheckResult(
                    vendor=vendor,
                    state=VendorState.UNVERIFIED,
                    detail=updated.reason,
                )
            )
            continue
        if not smoke_enabled(db):
            results.append(
                _heal(
                    db,
                    record,
                    reason="interface check passed; smoke prompt is turned off",
                    evidence={
                        "version": record.installed_version,
                        "check": "allowlisted --help",
                        "smoke": "disabled",
                    },
                    now=now,
                )
            )
            continue
        if record.smoke_version == record.installed_version:
            results.append(
                _heal(
                    db,
                    record,
                    reason=(
                        "interface check passed; the one smoke prompt for this "
                        "version was already spent"
                    ),
                    evidence={
                        "version": record.installed_version,
                        "check": "allowlisted --help",
                        "smoke": record.detail.get("smoke")
                        or "already spent for this version",
                    },
                    now=now,
                )
            )
            continue
        if not allowed():
            updated = replace(
                record,
                state=VendorState.UNVERIFIED,
                reason="interface check passed; waiting for the work gate to open",
                detail={
                    "version": record.installed_version,
                    "check": "allowlisted --help",
                    "smoke": "deferred by the F17 work gate",
                },
                checked_at=_now_iso(now),
            )
            save_health(db, updated)
            results.append(
                VendorCheckResult(
                    vendor=vendor,
                    state=VendorState.UNVERIFIED,
                    detail=updated.reason,
                )
            )
            continue
        smoke_result = _run_smoke(db, record, smoke)
        if smoke_result.skipped:
            results.append(
                _heal(
                    db,
                    record,
                    reason="interface check passed; no plan model is routed for this vendor",
                    evidence={
                        "version": record.installed_version,
                        "check": "allowlisted --help",
                        "smoke": smoke_result.to_dict(),
                    },
                    now=now,
                )
            )
        elif smoke_result.passed:
            results.append(
                _heal(
                    db,
                    record,
                    reason="interface check and smoke prompt passed",
                    evidence={
                        "version": record.installed_version,
                        "check": "allowlisted --help",
                        "smoke": smoke_result.to_dict(),
                    },
                    now=now,
                )
            )
        else:
            results.append(
                _fail(
                    db,
                    record,
                    reason=f"smoke prompt failed: {smoke_result.detail}",
                    evidence={
                        "version": record.installed_version,
                        "verified_version": record.verified_version,
                        "check": "smoke prompt",
                        "smoke": smoke_result.to_dict(),
                    },
                    engine_repo=engine_repo,
                    now=now,
                    smoke_ran=True,
                )
            )
    return results


def _run_smoke(
    db: Database,
    record: VendorHealth,
    smoke: SmokeRunner | None,
) -> SmokeResult:
    runner = smoke or (
        lambda vendor, model: run_smoke_prompt(
            vendor, model or smoke_model(db, vendor)
        )
    )
    try:
        return runner(record.vendor, smoke_model(db, record.vendor))
    except Exception as exc:  # noqa: BLE001 - evidence, never a crash
        return SmokeResult(passed=False, detail=f"smoke raised {type(exc).__name__}: {exc}")


def _gate_allows_work(db: Database) -> Callable[[], bool]:
    def allowed() -> bool:
        try:
            from saberops.pause_work import work_allowed

            return work_allowed(db)[0]
        except Exception:  # noqa: BLE001 - an unreadable gate fails closed
            logging.getLogger(__name__).exception("could not read the work gate")
            return False

    return allowed


# ---------------------------------------------------------------------------
# Scheduler start / periodic step
# ---------------------------------------------------------------------------


def _check_interval() -> float:
    raw = os.environ.get(CHECK_INTERVAL_ENV)
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            pass
    return DEFAULT_CHECK_INTERVAL_SECONDS


def _claim(db: Database, now: datetime | None = None) -> bool:
    """Take the single check claim, or return False when a check is running."""
    moment = now or datetime.now(UTC)
    ensure_tables(db)
    try:
        with owner_connection(db) as conn:
            row = conn.execute(
                "SELECT started_at FROM vendor_cli_check_claim WHERE claim = 'check'"
            ).fetchone()
            if row is not None:
                from saberops.capacity.exclusions import parse_iso

                started = parse_iso(str(row["started_at"]))
                if started is not None and (
                    moment - started
                ).total_seconds() < _check_timeout() * 2:
                    return False
            conn.execute(
                "INSERT INTO vendor_cli_check_claim (claim, owner, started_at) "
                "VALUES ('check', ?, ?) ON CONFLICT(claim) DO UPDATE SET "
                "owner = excluded.owner, started_at = excluded.started_at",
                (str(os.getpid()), _now_iso(moment)),
            )
    except Exception:  # noqa: BLE001 - a claim error must not break the tick
        logging.getLogger(__name__).exception("could not claim the vendor CLI check")
        return False
    return True


def _release_claim(db: Database) -> None:
    try:
        with owner_connection(db) as conn:
            conn.execute("DELETE FROM vendor_cli_check_claim WHERE claim = 'check'")
    except Exception:  # noqa: BLE001 - an expired claim self-heals
        pass


def _check_timeout() -> float:
    raw = os.environ.get(CHECK_TIMEOUT_ENV)
    if raw:
        try:
            return max(30.0, float(raw))
        except ValueError:
            pass
    return 900.0


def _spawn_check(db: Database) -> bool:
    """Start one detached check process (never sends a prompt in this process)."""
    owner = getattr(db, "owner_db", db)
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        (str(Path(__file__).resolve().parents[1]), env.get("PYTHONPATH", ""))
    )
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "saberops.cli",
                "vendor",
                "check",
                "--db",
                str(owner.db_path),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
        )
    except OSError:
        logging.getLogger(__name__).exception("could not start the vendor CLI check")
        return False
    return True


def maybe_check(
    db: Database,
    *,
    now: datetime | None = None,
    background: bool = True,
    spawn: Callable[[Database], bool] | None = None,
) -> list[VendorChange]:
    """Detect version changes and start one check when one is due.

    Called on scheduler start (the first tick) and periodically.  Detection
    runs here; the allowlisted help capture and the smoke prompt run in the
    detached check process (``background=True``) or inline (``background=False``)
    under the single claim, so a repeated tick never sends a second prompt.
    """
    if os.environ.get(CHECKS_ENV) == "0":
        return []
    moment = now or datetime.now(UTC)
    interval = _check_interval()
    last_raw = _setting(db, "last_detected_at")
    changes: list[VendorChange] = []
    if last_raw:
        from saberops.capacity.exclusions import parse_iso

        last = parse_iso(last_raw)
        if last is not None and (moment - last).total_seconds() < interval:
            return []
    changes = detect_changes(db, now=moment)
    _set_setting(db, "last_detected_at", _now_iso(moment))
    health = load_health(db)
    due = [
        record
        for record in health.values()
        if record.state in (VendorState.UNVERIFIED, VendorState.FAILED)
        and record.installed_version not in ("", MISSING)
    ]
    if not due:
        return changes
    if not _claim(db, now):
        return changes
    if background:
        started = (spawn or _spawn_check)(db)
        if not started:
            _release_claim(db)
        return changes
    try:
        run_checks(db, now=now)
    finally:
        _release_claim(db)
    return changes


# ---------------------------------------------------------------------------
# Status rendering
# ---------------------------------------------------------------------------


def render_status(db: Database) -> str:
    """One deterministic status table for ``orch vendor status``."""
    health = load_health(db)
    lines = [
        f"{'vendor':<8}  {'cli':<8}  {'installed':<24}  {'verified':<24}  "
        f"{'state':<15}  smoke",
        f"{'-' * 8}  {'-' * 8}  {'-' * 24}  {'-' * 24}  {'-' * 15}  {'-' * 5}",
    ]
    for vendor in VENDORS:
        record = health.get(vendor)
        if record is None:
            lines.append(
                f"{vendor:<8}  {VENDOR_CLI[vendor]:<8}  {'-':<24}  {'-':<24}  {'-':<15}  -"
            )
            continue
        lines.append(
            f"{record.vendor:<8}  {record.cli:<8}  {record.installed_version:<24.24}  "
            f"{record.verified_version:<24.24}  {record.state.value:<15}  "
            f"{record.smoke_version or '-'}"
        )
    for vendor in VENDORS:
        pin = pinned_version(db, vendor)
        if pin:
            lines.append(f"pin {vendor}: {pin}")
    if not smoke_enabled(db):
        lines.append("automatic smoke prompt: off")
    return "\n".join(lines)


