"""Composition-layer acquisition and projection of provider readiness."""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from saberops.model_access.connections import (
    ACCOUNT_BACKENDS,
    AccessRegistry,
    AccessResolutionError,
    AccessStore,
    ConnectionTestReason,
    ConnectionTestResult,
    DiscoveryState,
    ProviderConnection,
    TriState,
    resolve_access,
)
from saberops.model_access.native_auth import native_authentication
from saberops.model_access.readiness import (
    ProviderReadinessEvidence,
    ReadinessEvidenceSource,
    ReadinessStore,
    derive_dispatch_readiness,
)
from saberops.models import ProviderReadiness

DEFAULT_READINESS_MAX_AGE = timedelta(hours=24)
AUTH_STATUS_TIMEOUT_SECONDS = 8


@dataclass(frozen=True)
class ReadinessAdmission:
    """Neutral input consumed by common dispatch admission."""

    state: ProviderReadiness
    reason: str
    executable_available: bool
    connection_id: str | None = None
    observed_at: str | None = None


def _now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def _claude_authentication(payload: object) -> bool | None:
    """Project ``claude auth status --json`` to an authentication verdict.

    Verified shape (Claude Code CLI, ``--json`` is the default)::

        {"loggedIn": true, "authMethod": "...", ...}

    Only the ``loggedIn`` boolean is read.  Anything else -- including a
    successful command with an unrecognized shape -- stays ``None``
    (UNKNOWN); only an explicit ``false`` proves NOT_READY.
    """
    if not isinstance(payload, Mapping):
        return None
    value = payload.get("loggedIn")
    return value if isinstance(value, bool) else None


def _codex_authentication(payload: object) -> bool | None:
    """Project ``codex doctor --json`` to an authentication verdict.

    Verified shape (Codex CLI 0.147.0, redacted machine-readable report)::

        {"schemaVersion": 1, "overallStatus": "...",
         "checks": {"auth.credentials": {"status": "ok", ...}, ...}}

    Only the ``auth.credentials`` check ``status`` is read: ``"ok"``
    proves a supported session; an explicit error status proves
    NOT_READY.  ``"warning"``, missing keys, or any unrecognized shape
    stays ``None`` (UNKNOWN) -- never a fabricated verdict.
    """
    if not isinstance(payload, Mapping):
        return None
    checks = payload.get("checks")
    if not isinstance(checks, Mapping):
        return None
    auth_check = checks.get("auth.credentials")
    if not isinstance(auth_check, Mapping):
        return None
    status = auth_check.get("status")
    if status == "ok":
        return True
    if status in ("error", "fail", "failed"):
        return False
    return None


def _status_authentication(backend: str, payload: object) -> bool | None:
    """Parse only the verified authentication verdict for supported CLIs."""
    if backend == "claude-code":
        return _claude_authentication(payload)
    if backend == "codex":
        return _codex_authentication(payload)
    return None


class ReadinessService:
    """Refresh and project secret-free readiness for declared connections only."""

    def __init__(
        self,
        registry: AccessRegistry,
        store: ReadinessStore,
        *,
        which: Callable[[str], str | None] = shutil.which,
        run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        now: Callable[[], str] = _now,
        max_age: timedelta = DEFAULT_READINESS_MAX_AGE,
    ) -> None:
        self.registry = registry
        self.store = store
        self.which = which
        self.run = run
        self.now = now
        self.max_age = max_age

    @classmethod
    def for_owner_state(cls, root: Path | str) -> ReadinessService:
        """Build the service over an explicit owner state directory.

        The caller supplies the root (normally
        :func:`saberops.project.owner_state_dir`); this module
        never resolves ambient state itself, keeping the model_access
        -> vcs dependency edge out of the architecture.
        """
        directory = Path(root)
        return cls(
            AccessStore(directory / AccessStore.FILENAME).load(),
            ReadinessStore(directory / ReadinessStore.FILENAME),
        )

    def refresh(self, connection: ProviderConnection) -> ProviderReadinessEvidence:
        """Execute at most the declared non-inference status command and persist its projection."""
        if not connection.is_account_backed:
            from saberops.model_access.api_verification import verify_api
            evidence = verify_api(connection)
            self.store.replace(evidence)
            return evidence
        descriptor = next(
            (
                item
                for item in ACCOUNT_BACKENDS
                if item.connection_id == connection.connection_id
            ),
            None,
        )
        installed = bool(descriptor and self.which(descriptor.executable))
        observed_at = self.now()
        if not installed:
            evidence = derive_dispatch_readiness(
                connection,
                discovery=DiscoveryState(connection.connection_id, installed=TriState.FALSE),
                test_result=ConnectionTestResult(
                    connection.connection_id, False, ConnectionTestReason.BACKEND_NOT_INSTALLED
                ),
                observed_at=observed_at,
            )
        elif descriptor is None or not descriptor.has_auth_probe:
            # Installation/catalog listings cannot supersede actual execution
            # evidence. Freshness is still enforced when it is consumed.
            previous = self.store.load().try_get(connection.connection_id)
            if (
                connection.enabled
                and connection.is_configured
                and previous is not None
                and previous.source in (ReadinessEvidenceSource.SUCCESSFUL_EXECUTION,
                                        ReadinessEvidenceSource.MODEL_ACCESS_TEST)
                and previous.state == ProviderReadiness.READY
                and previous.provider == connection.provider
                and previous.backend == connection.backend
            ):
                return previous
            evidence = derive_dispatch_readiness(
                connection,
                discovery=DiscoveryState(connection.connection_id, installed=TriState.TRUE),
                observed_at=observed_at,
            )
        else:
            authenticated: bool | None = None
            try:
                if descriptor.auth_status_protocol:
                    authenticated = native_authentication(
                        self.which(descriptor.executable) or descriptor.executable,
                        descriptor.auth_status_protocol, timeout=AUTH_STATUS_TIMEOUT_SECONDS,
                    )
                else:
                    result = self.run(
                        [descriptor.executable, *descriptor.auth_status_argv],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=AUTH_STATUS_TIMEOUT_SECONDS,
                    )
                    # Nonzero never proves positive readiness; only an
                    # explicit negative verdict can prove NOT_READY.
                    parsed = _status_authentication(
                        connection.backend, json.loads(result.stdout)
                    )
                    if result.returncode == 0:
                        authenticated = parsed
                    elif parsed is False:
                        authenticated = False
            except (OSError, ValueError, subprocess.SubprocessError):
                authenticated = None
            state = TriState.UNKNOWN if authenticated is None else (
                TriState.TRUE if authenticated else TriState.FALSE
            )
            evidence = derive_dispatch_readiness(
                connection,
                discovery=DiscoveryState(
                    connection.connection_id,
                    installed=TriState.TRUE,
                    authenticated=state,
                    reachable=TriState.TRUE if authenticated else TriState.UNKNOWN,
                ),
                test_result=(
                    ConnectionTestResult(connection.connection_id, True, ConnectionTestReason.OK)
                    if authenticated
                    else ConnectionTestResult(
                        connection.connection_id,
                        False,
                        ConnectionTestReason.NOT_AUTHENTICATED
                        if authenticated is False
                        else ConnectionTestReason.CAPABILITY_UNKNOWN,
                    )
                ),
                observed_at=observed_at,
            )
        self.store.replace(evidence)
        return evidence

    def verify_model(self, connection: ProviderConnection, model: str) -> ProviderReadinessEvidence:
        """Owner-requested first-use test. Callers validate the exact catalog model.

        This is separate from refresh: startup and status checks never infer.
        The persisted receipt carries the tested model; readiness remains the
        existing connection-level transport observation, not entitlement for
        every model in a multi-provider catalog.
        """
        from saberops.model_access.model_verification import verify_native_model
        descriptor = next((d for d in ACCOUNT_BACKENDS
                           if d.connection_id == connection.connection_id), None)
        if self.registry.try_get(connection.connection_id) != connection:
            raise ValueError("connection identity does not match the access registry")
        if (descriptor is None or descriptor.provider != connection.provider
                or descriptor.backend != connection.backend):
            raise ValueError("connection does not match its declared native backend")
        if not connection.enabled or not connection.is_configured:
            return self.refresh(connection)
        executable = self.which(descriptor.executable) if descriptor else None
        reason = (verify_native_model(executable, connection.backend, model)
                  if executable else "BACKEND_NOT_INSTALLED")
        state = (ProviderReadiness.READY if reason == "READY" else
                 ProviderReadiness.NOT_READY if reason == "BACKEND_NOT_INSTALLED" else
                 ProviderReadiness.UNKNOWN)
        evidence = ProviderReadinessEvidence(
            connection.connection_id, connection.provider, connection.backend,
            state, reason, self.now(), ReadinessEvidenceSource.MODEL_ACCESS_TEST, model,
        )
        # A model-specific failure doesn't disprove access through another
        # previously successful model on the same connection.
        previous = self.store.load().try_get(connection.connection_id)
        if (state == ProviderReadiness.READY or previous is None
                or previous.state != ProviderReadiness.READY
                or reason == "BACKEND_NOT_INSTALLED"):
            self.store.replace(evidence)
        return evidence

    def refresh_accounts_on_startup(self) -> None:
        """Detect declared CLIs and run only their supported login-status commands.

        Called explicitly by the interactive UI launcher, never by page rendering
        or application construction. API connections are excluded; no model
        prompt, credentials, owner bindings or routing configuration are used.
        """
        for descriptor in ACCOUNT_BACKENDS:
            connection = self.registry.try_get(descriptor.connection_id)
            if connection is not None:
                self.refresh(connection)

    def admission_for(
        self, *, provider: str, profile_id: str = "", backend_ref: str = ""
    ) -> ReadinessAdmission:
        """Resolve one exact connection and project durable evidence for admission."""
        try:
            resolved = resolve_access(
                registry=self.registry,
                provider=provider,
                profile_id=profile_id,
                backend_ref=backend_ref,
            )
            connection = resolved.connection
        except AccessResolutionError:
            # Legacy static candidates have no binding profile. They can only
            # use this compatibility path when exactly one declared connection
            # has the requested provider, preserving connection identity.
            # Anything else (no row, ambiguous rows) is UNKNOWN with no
            # executable verdict: absence of evidence must never hard-fail
            # an explicit owner bootstrap, while automatic selection still
            # fails closed on UNKNOWN.
            matches = self.registry.for_provider(provider)
            if profile_id or backend_ref or len(matches) != 1:
                return ReadinessAdmission(
                    ProviderReadiness.UNKNOWN, "INSUFFICIENT_EVIDENCE", True
                )
            connection = matches[0]
        if not connection.is_account_backed:
            from saberops.workers.direct_api import credential_value, supports_connection

            if not supports_connection(connection):
                return ReadinessAdmission(
                    ProviderReadiness.NOT_READY, "PROTOCOL_UNSUPPORTED", False,
                    connection.connection_id,
                )
            try:
                credential_value(connection)
            except ValueError:
                return ReadinessAdmission(
                    ProviderReadiness.NOT_READY, "CREDENTIAL_MISSING", False,
                    connection.connection_id,
                )
        descriptor = next(
            (
                item
                for item in ACCOUNT_BACKENDS
                if item.connection_id == connection.connection_id
            ),
            None,
        )
        installed = bool(descriptor is None or self.which(descriptor.executable))
        if not installed:
            return ReadinessAdmission(
                ProviderReadiness.NOT_READY,
                ConnectionTestReason.BACKEND_NOT_INSTALLED.value,
                False,
                connection.connection_id,
            )
        try:
            evidence = self.store.load().try_get(connection.connection_id)
        except (OSError, ValueError):
            # C15-RDY-02A: a malformed/unreadable readiness store is an
            # authority failure, never a pass.  Fail closed to UNKNOWN
            # with the fixed sanitized reason (no exception text, path,
            # or secret leaves this seam): automatic dispatch skips,
            # while an explicit owner exact-UNKNOWN bootstrap remains
            # possible.  The resolved connection identity is genuine
            # (from the live registry, not fabricated) so a later
            # successful exact execution can still be attributed.
            return ReadinessAdmission(
                ProviderReadiness.UNKNOWN,
                "READINESS_AUTHORITY_UNAVAILABLE",
                True,
                connection.connection_id,
            )
        if evidence is None:
            return ReadinessAdmission(
                ProviderReadiness.UNKNOWN, "INSUFFICIENT_EVIDENCE", True, connection.connection_id
            )
        fresh = evidence.freshness(as_of=datetime.now(UTC), max_age=self.max_age)
        return ReadinessAdmission(
            fresh.state, fresh.reason, True, fresh.connection_id, evidence.observed_at
        )

    def record_success(self, connection_id: str) -> None:
        """Persist direct evidence only for the exact connection that executed."""
        connection = self.registry.try_get(connection_id)
        if connection is None:
            return
        try:
            self.store.replace(
                ProviderReadinessEvidence(
                    connection_id=connection.connection_id,
                    provider=connection.provider,
                    backend=connection.backend,
                    state=ProviderReadiness.READY,
                    reason="READY",
                    observed_at=self.now(),
                    source=ReadinessEvidenceSource.SUCCESSFUL_EXECUTION,
                )
            )
        except (OSError, ValueError):
            # C15-RDY-02A: success-evidence persistence is best-effort.
            # A malformed/unreadable store must neither crash run
            # accounting nor leak store content into persisted
            # diagnostics; the successful execution result itself is
            # unaffected.
            return None
