"""Deterministic subprocess runner with explicit boundaries and timeout control."""

from __future__ import annotations

import errno
import os
import shlex
import shutil
import subprocess
import threading
import time
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

if TYPE_CHECKING:
    from orchestrator_mvp.models import WorkerRequest, WorkerResult


@dataclass(frozen=True)
class ProcessResult:
    """Result of a subprocess execution."""

    args: list[str]
    exit_code: int
    stdout: str
    stderr: str
    duration_seconds: float
    timed_out: bool = False
    # True when the worker process group could not be confirmed extinct
    # before returning.  Such a result is a deterministic execution failure:
    # the worktree may still be mutated by surviving descendants, so callers
    # must never treat it as safely quiescent (no commit, gate, or retry on
    # this worktree).
    termination_incomplete: bool = False
    # True when subprocess creation/bootstrap failed before the command
    # actually executed (e.g. missing executable, OSError at Popen / run).
    # When ``launch_failed`` is True the result carries no meaningful
    # ``exit_code`` (defaults to -1) and the typed outcome is an
    # INFRASTRUCTURE_FAILURE, never a VALIDATION_FAILURE.
    launch_failed: bool = False

    @property
    def passed(self) -> bool:
        """True if process completed with exit code 0 and did not time out."""
        return (
            self.exit_code == 0
            and not self.timed_out
            and not self.termination_incomplete
            and not self.launch_failed
        )


class LeaderExitStatus(StrEnum):
    """Tri-state result after provider-leader exit.

    LIVE    — one or more live non-zombie group members were observed.
    EXTINCT — complete /proc evidence confirms zero live members.
    UNKNOWN — extinction cannot be proven because evidence remains incomplete.
    """

    LIVE = "LIVE"
    EXTINCT = "EXTINCT"
    UNKNOWN = "UNKNOWN"


DEFAULT_PROMPT_ARG_LIMIT: int = 32768
ORCH_PROMPT_ARG_LIMIT: int = DEFAULT_PROMPT_ARG_LIMIT

__all__ = [
    "AUTONOMOUS_CONTROL_ENV_ALLOWLIST",
    "AUTONOMOUS_RUNTIME_ENV_ALLOWLIST",
    "AutonomousDispatchConfig",
    "AutonomousEvidenceError",
    "DEFAULT_PROMPT_ARG_LIMIT",
    "EVIDENCE_UNCERTAIN_MARKER",
    "LeaderExitStatus",
    "ORCH_PROMPT_ARG_LIMIT",
    "ProcessResult",
    "SandboxedProcessError",
    "get_prompt_arg_limit",
    "is_evidence_uncertain_error",
    "launch_worker_process",
    "persist_autonomous_trace",
    "project_autonomous_child_env",
    "run_autonomous_adapter",
    "run_process",
    "run_sandboxed_process",
    "sandboxed_child_write_text",
    "split_command",
]


def get_prompt_arg_limit() -> int:
    """Return the threshold in bytes above which prompt is passed via stdin."""
    raw = os.environ.get("ORCH_PROMPT_ARG_LIMIT")
    if raw is not None:
        try:
            return int(raw.strip())
        except ValueError:
            pass
    return DEFAULT_PROMPT_ARG_LIMIT


def split_command(command: str | list[str]) -> list[str]:
    """Parse a command string or list safely into an argv list."""
    if isinstance(command, list):
        return [str(arg) for arg in command]
    return shlex.split(command)


class SandboxedProcessError(RuntimeError):
    """Fail-closed refusal to launch a sandboxed child process.

    Raised *before* semantic execution when the effective
    :class:`SandboxPolicy` / :class:`ToolGrant` does not authorise the
    requested child.  Never raised after the child has started.
    """


class AutonomousEvidenceError(SandboxedProcessError):
    """Typed failure: durable trace evidence missing after execution.

    Raised when semantic execution MAY have produced effects but
    durable ``AttemptTrace`` / tool-effect persistence failed.  Unlike
    a pre-launch :class:`SandboxedProcessError` (zero effects, clean),
    this error preserves effect uncertainty: the Attempt must NOT be
    reported as an ordinary successful autonomous result, gated,
    committed, or accepted, and must NOT fail over as though no
    effects happened.  Existing lifecycle/failover logic treats this
    conservatively because the durable trace it requires is absent.
    """


#: Stable marker carried in the ``WorkerResult.error`` of an
#: evidence-uncertain autonomous result (see
#: :func:`is_evidence_uncertain_error`).  The Orchestrator dispatch
#: loop keys its terminal failover block on this marker -- never on
#: the ``DispatchOutcome`` value alone -- so evidence uncertainty can
#: never be mistaken for an ordinary provider/runtime failure.
EVIDENCE_UNCERTAIN_MARKER = "effects uncertain"


def is_evidence_uncertain_error(error: str | None) -> bool:
    """Return True iff ``error`` carries autonomous evidence uncertainty.

    True means semantic execution MAY have produced effects while
    authoritative trace/effect evidence is incomplete.  The caller
    must terminalize the dispatch loop (no blind candidate rotation,
    no tier escalation, no continuation/replay) and let
    :class:`FailoverEngine` block.  ``False`` preserves ordinary
    pre-launch/provider failure routing.
    """
    return error is not None and EVIDENCE_UNCERTAIN_MARKER in error


# ---------------------------------------------------------------------------
# Generic autonomous execution seam.
#
# This is the ONE place where autonomous semantic execution meets the
# process boundary.  Every worker adapter -- OpenCode, Codex, Cline,
# Antigravity, Copilot, and any test-local backend -- launches its
# child through :func:`launch_worker_process`, which routes to the
# proven :func:`run_sandboxed_process` containment seam whenever an
# autonomous boundary is installed, and refuses plain ``run_process``
# launches while a boundary is active.  No adapter carries its own
# copy of this logic; provider-specific sandbox branches are
# forbidden here.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AutonomousDispatchConfig:
    """Explicit per-run autonomous containment contract inputs.

    The Orchestrator builds one of these when autonomous dispatch is
    requested (never by default) and hands it to
    :func:`run_autonomous_adapter` per Attempt.  ``readable_paths``
    defaults to mirroring ``writable_paths``; ``deny_paths`` defaults
    to the sandbox default deny (``.git/**``, ``.github/**``).
    ``writable_paths`` may be empty for a read-only autonomous
    attempt -- but then ``readable_paths`` must be given explicitly:
    an empty grantable surface fails closed (a readable scope is a
    deliberate decision, never an implicit whole-repo grant).
    Network is always ``DENY``: any backend that needs
    egress is ineligible under containment (see ``requires_provider_network``
    on the adapter).
    """

    writable_paths: tuple[str, ...] = ()
    readable_paths: tuple[str, ...] | None = None
    deny_paths: tuple[str, ...] | None = None
    credential_refs: tuple[str, ...] = ()
    approved_executables: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "writable_paths", tuple(self.writable_paths))
        if self.readable_paths is not None:
            object.__setattr__(self, "readable_paths", tuple(self.readable_paths))
        if self.deny_paths is not None:
            object.__setattr__(self, "deny_paths", tuple(self.deny_paths))
        object.__setattr__(self, "credential_refs", tuple(self.credential_refs))
        object.__setattr__(self, "approved_executables", tuple(self.approved_executables))

    def as_dict(self) -> dict[str, object]:
        """Render the non-secret autonomous containment contract."""
        return {
            "writable_paths": list(self.writable_paths),
            "readable_paths": (
                None if self.readable_paths is None else list(self.readable_paths)
            ),
            "deny_paths": None if self.deny_paths is None else list(self.deny_paths),
            "credential_refs": list(self.credential_refs),
            "approved_executables": list(self.approved_executables),
        }

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> AutonomousDispatchConfig:
        """Reconstruct a contract from its durable non-secret mapping."""
        if not isinstance(payload, Mapping):
            raise ValueError("autonomous dispatch contract must be a mapping")

        def _strings(key: str) -> tuple[str, ...]:
            raw = payload.get(key, ())
            if raw is None:
                return ()
            if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
                raise ValueError(f"autonomous dispatch contract '{key}' must be a list")
            if any(not isinstance(item, str) for item in raw):
                raise ValueError(f"autonomous dispatch contract '{key}' entries must be strings")
            return tuple(raw)

        def _optional_strings(key: str) -> tuple[str, ...] | None:
            if key not in payload or payload[key] is None:
                return None
            return _strings(key)

        return cls(
            writable_paths=_strings("writable_paths"),
            readable_paths=_optional_strings("readable_paths"),
            deny_paths=_optional_strings("deny_paths"),
            credential_refs=_strings("credential_refs"),
            approved_executables=_strings("approved_executables"),
        )


@dataclass
class _AutonomousBoundary:
    """The installed per-Attempt autonomous execution boundary.

    Carried on a :class:`ContextVar` (never globals, never ambient
    env) so the boundary is scoped to the exact dynamic extent of one
    adapter invocation: :func:`run_process` consults it on every
    launch, and :func:`launch_worker_process` routes through
    containment from it.
    """

    policy: Any
    grant: Any
    trace_mechanism: Any
    attempt_id: str
    run_id: str


_autonomous_boundary: ContextVar[_AutonomousBoundary | None] = ContextVar(
    "orch_autonomous_boundary", default=None
)
_contained_launch: ContextVar[bool] = ContextVar(
    "orch_contained_launch", default=False
)


# ---------------------------------------------------------------------------
# Generic autonomous credential projection.
#
# The generic autonomous boundary is authoritative for what environment
# reaches an autonomous child.  Projection starts from an explicit
# allowlist -- never a blacklist -- so an ambient or request-smuggled
# credential can never enter the child unless the effective
# SandboxPolicy explicitly authorises it via ``credential_refs``.
# Secret VALUES are never persisted in policy, trace, ledger, events,
# or diagnostics by this projection (it returns the child mapping
# only; callers must not log it).
# ---------------------------------------------------------------------------

#: Non-secret runtime variables an autonomous child may need to execute.
#: ``LC_*`` is matched by prefix (``LC_ALL``, ``LC_CTYPE``, ...); every
#: other entry is an exact name.
AUTONOMOUS_RUNTIME_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LANGUAGE",
    "TZ",
    "TERM",
)

#: Bounded non-secret Orch execution identity/control variables the
#: existing runtime requires inside the child: run/attempt association,
#: the run database path, verified tool-contract metadata, the C10
#: gateway pinning the gateway boundary injects for gateway dispatches
#: (non-secret names/values only -- the real OmniRoute token is
#: resolved at forwarding time, never from this mapping), the exact
#: C11-D execution binding identity the canonical lifecycle resolved
#: (binding/profile/pool/backend references only, never secrets), and
#: the Copilot profile selector honoured by the Copilot adapter's
#: request-env overlay, and the C11-D executable-profile selectors
#: (``ORCH_EXECUTION_PROFILE_ACCOUNT`` / ``ORCH_EXECUTION_PROFILE_BACKEND``):
#: non-secret account/backend references the canonical lifecycle
#: populated ONLY from a verified adapter ``execution_profile_overlay``
#: hook, observed by the adapter's actual invocation.  Anything not
#: listed here is absent.
AUTONOMOUS_CONTROL_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "ORCH_RUN_ID",
        "ORCH_ATTEMPT_ID",
        "ORCH_ASSOCIATION_ID",
        "ORCH_DB_PATH",
        "ORCH_TOOL_TELEMETRY_SINK",
        "ORCH_TOOL_CONTRACT_DIGEST",
        "ORCH_COPILOT_PROFILE_ID",
        "ORCH_EXECUTION_BINDING_ID",
        "ORCH_EXECUTION_PROFILE_ID",
        "ORCH_EXECUTION_POOL_ID",
        "ORCH_EXECUTION_BACKEND",
        "ORCH_EXECUTION_PROFILE_ACCOUNT",
        "ORCH_EXECUTION_PROFILE_BACKEND",
        "ORCH_GATEWAY_ENDPOINT",
        "ORCH_GATEWAY_CONNECTION",
        "ORCH_GATEWAY_AUTHORIZATION_ENV",
        "ORCH_GATEWAY_FROZEN_DIGEST",
        "ORCH_GATEWAY_SELECTION_REASON",
        "ORCH_GATEWAY_PROVIDER_ID",
        "ORCH_GATEWAY_LOOPBACK_URL",
        "_ORCH_BRIDGE_AUTH_SENTINEL",
    }
)


def _is_runtime_allowlisted(key: str) -> bool:
    if key in AUTONOMOUS_RUNTIME_ENV_ALLOWLIST:
        return True
    if key == "LC_ALL" or key.startswith("LC_"):
        return key[3:].isupper() and key[3:].replace("_", "").isalpha()
    return False


def project_autonomous_child_env(
    request_env: dict[str, str] | None,
    *,
    policy: Any,
) -> dict[str, str]:
    """Return the hermetic child environment for one autonomous dispatch.

    Deterministic allowlist projection over ``request_env`` (never
    ``os.environ``: the only ambient channel is what the caller already
    placed in the request):

    * non-secret runtime variables (see
      :data:`AUTONOMOUS_RUNTIME_ENV_ALLOWLIST`, ``LC_*`` by prefix);
    * bounded non-secret Orch control variables (see
      :data:`AUTONOMOUS_CONTROL_ENV_ALLOWLIST`);
    * credential material ONLY for ``env:<NAME>`` refs authorised by
      ``policy.credential_refs`` and present in ``request_env`` --
      exactly that variable, never any other.

    ``profile:`` / ``store:`` refs have no generic resolver at this
    seam, and an ``env:`` ref naming a variable absent from
    ``request_env``, both fail closed with :class:`SandboxedProcessError`
    before any semantic child exists -- rather than copying broad
    environment state.  Copilot's profile-specific
    ``build_attempt_env`` semantics are preserved: this projection only
    narrows the request overlay Copilot also reads and never adds a
    credential Copilot would have dropped.
    """
    source = dict(request_env) if request_env is not None else {}
    child: dict[str, str] = {}
    for key, value in source.items():
        if _is_runtime_allowlisted(key) or key in AUTONOMOUS_CONTROL_ENV_ALLOWLIST:
            child[key] = value
    credential_refs = tuple(getattr(policy, "credential_refs", None) or ())
    for ref in credential_refs:
        if not ref.startswith("env:"):
            raise SandboxedProcessError(
                f"refusing autonomous dispatch: credential ref {ref!r} has no "
                "supported resolver at the generic execution seam "
                "(only env:<NAME> refs are resolvable here)"
            )
        name = ref[len("env:") :]
        if not name or name not in source:
            raise SandboxedProcessError(
                f"refusing autonomous dispatch: credential ref {ref!r} cannot "
                "be resolved safely from the request environment"
            )
        child[name] = source[name]
    return child


def _autonomous_policy_for(config: AutonomousDispatchConfig, repo_root: str) -> Any:
    """Build the effective per-Attempt SandboxPolicy for ``repo_root``."""
    from orchestrator_mvp.sandbox.policy import (
        GIT_INTERNAL_DENY,
        GITHUB_DENY,
        NetworkPolicy,
        ProcessPolicy,
        SandboxPolicy,
    )

    readable = (
        tuple(sorted(config.readable_paths))
        if config.readable_paths is not None
        else tuple(sorted(config.writable_paths))
    )
    return SandboxPolicy(
        repo_root=repo_root,
        readable_paths=readable,
        writable_paths=tuple(sorted(config.writable_paths)),
        deny_paths=(
            tuple(sorted(config.deny_paths))
            if config.deny_paths is not None
            else (GIT_INTERNAL_DENY + GITHUB_DENY)
        ),
        network=NetworkPolicy(mode="DENY"),
        credential_refs=tuple(sorted(config.credential_refs)),
        process=ProcessPolicy(
            allow_host_shell=False,
            sandboxed_shell=True,
            approved_executables=tuple(sorted(config.approved_executables)),
        ),
    )


def run_sandboxed_process(
    args: list[str],
    *,
    policy: Any,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    input_text: str | None = None,
    inherit_env: bool = False,
    trace_mechanism: Any | None = None,
    attempt_id: str | None = None,
    run_id: str | None = None,
    **kwargs: Any,
) -> ProcessResult:
    """Launch a child through the REAL sandboxed process boundary.

    Enforcement, in order, before any semantic execution:

    1. ``argv[0]`` passes :func:`preflight_executable` against the
       effective policy (host shell is never used -- ``shell=False``
       on every path -- and a non-empty ``approved_executables``
       allowlist is enforced, so ``authorize_sandboxed_shell() ==
       True`` alone never grants an unrestricted host command path).
       A symlinked ``argv[0]`` that resolves outside both the
       repository and the runtime binds refuses: execution escapes
       are not laundered through path text.
    2. An explicit ``cwd`` must resolve inside ``policy.repo_root``
       (symlink-anchored); otherwise the child could be rooted outside
       its scope.
    3. The effective network mode must be ``DENY``: no backend on
       this host can enforce selective egress, so any other mode
       fails closed instead of pretending the policy is enforced.
    4. When the policy authorises filesystem mutation (any writable
       scope), the launch additionally requires BOTH:

        a. a trace mechanism INSTALLED on this execution path (an
           installed :class:`SandboxEffectInterceptor` or an
           installed verified native harness source) -- a directly
           constructed dataclass/token derives ``NONE`` and fails
           closed before semantic launch; and
        b. real filesystem containment (``bwrap`` on PATH) plus an
           EXACTLY representable writable scope: the child is
           launched inside a bubblewrap user namespace with only the
           runtime filesystem plus the authorised repository scope
           visible, denied paths overlaid empty and read-only, and
           ``--unshare-net`` so even host loopback is unreachable.
           Unrepresentable writable scopes fail closed instead of
           widening a directory.

        When containment is unavailable the launch fails closed with
        ``FILESYSTEM CONTAINMENT UNAVAILABLE \u2014 AUTONOMOUS MUTATION
        BLOCKED`` instead of running unsandboxed.
    5. The child environment defaults to hermetic
       (``inherit_env=False``): only the explicit ``env`` mapping is
       visible.  Callers that pass ``inherit_env=True`` do so
       explicitly and own the ambient-credential consequences.

    Non-mutating launches (no writable scope) are contained with the
    authorised readable scope whenever ``bwrap`` is available.  When
    ``bwrap`` is unavailable the launch behaviour depends on context:
    an ordinary non-autonomous call falls back to a direct launch
    (nothing is authorised for mutation either way -- legacy behaviour
    preserved), but an autonomous semantic launch -- one made while an
    autonomous execution boundary is installed -- fails closed with a
    typed containment-unavailable reason instead: readable scope is
    itself a security boundary, so no autonomous semantic child may
    run uncontained merely because ``writable_paths`` is empty.

    All remaining ``kwargs`` are forwarded to :func:`run_process`.
    """
    from orchestrator_mvp.models import TraceCapability
    from orchestrator_mvp.sandbox.enforcement import (
        FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE,
        READONLY_CONTAINMENT_UNAVAILABLE_MESSAGE,
        SandboxEnforcementError,
        bwrap_available,
        policy_is_mutating,
        preflight_executable,
        require_exact_writable_scope,
        require_installed_trace_mechanism,
        resolve_within_repo,
    )

    try:
        preflight_executable(policy, list(args))
    except SandboxEnforcementError as exc:
        raise SandboxedProcessError(str(exc)) from exc
    if cwd is not None:
        resolved_cwd = Path(os.path.realpath(str(cwd)))
        root_real = Path(os.path.realpath(policy.repo_root))
        if resolved_cwd != root_real and root_real not in resolved_cwd.parents:
            raise SandboxedProcessError(
                f"refusing sandboxed launch with cwd outside the repository: {cwd!r}"
            )
        # Anchor check only; run_process resolves its own cwd.
        _ = resolve_within_repo(policy.repo_root, ".")
    if policy.network.mode != "DENY":
        raise SandboxedProcessError(
            f"refusing sandboxed launch with network mode {policy.network.mode!r}: "
            "this backend cannot enforce selective egress"
        )
    # The mechanism is resolved against THIS launch: the caller passes
    # the effective grant via the ``sandbox_grant`` kwarg (popped here,
    # never forwarded to run_process) so interception stays bound to
    # this attempt's boundary.
    grant = kwargs.pop("sandbox_grant", None)
    mutating = bool(policy_is_mutating(policy))
    if mutating:
        if trace_mechanism is None or grant is None:
            raise SandboxedProcessError(
                "refusing autonomous mutating launch: no verified trace "
                "mechanism for this execution path (trace NONE fails closed)"
            )
        try:
            resolved_class = require_installed_trace_mechanism(
                trace_mechanism,
                policy=policy,
                grant=grant,
                attempt_id=attempt_id,
                run_id=run_id,
            )
        except SandboxEnforcementError as exc:
            raise SandboxedProcessError(
                f"refusing autonomous mutating launch: {exc}"
            ) from exc
        if resolved_class is TraceCapability.NONE:  # pragma: no cover - choke raises first
            raise SandboxedProcessError(
                "refusing autonomous mutating launch: no verified trace "
                "mechanism for this execution path (trace NONE fails closed)"
            )
        try:
            require_exact_writable_scope(policy)
        except SandboxEnforcementError as exc:
            raise SandboxedProcessError(str(exc)) from exc
        if not bwrap_available():
            raise SandboxedProcessError(
                FILESYSTEM_CONTAINMENT_UNAVAILABLE_MESSAGE
            )
        try:
            launch_args = _bwrap_prefix_for_policy(policy) + _contained_argv(
                policy, list(args)
            )
        except SandboxEnforcementError as exc:
            raise SandboxedProcessError(
                f"refusing autonomous mutating launch: {exc}"
            ) from exc
        return _run_contained(
            launch_args,
            cwd=cwd,
            env=env,
            timeout=timeout,
            inherit_env=inherit_env,
            input_text=input_text,
            **kwargs,
        )
    if bwrap_available():
        try:
            launch_args = _bwrap_prefix_for_policy(policy) + _contained_argv(
                policy, list(args)
            )
        except SandboxEnforcementError as exc:
            raise SandboxedProcessError(
                f"refusing sandboxed launch: {exc}"
            ) from exc
        return _run_contained(
            launch_args,
            cwd=cwd,
            env=env,
            timeout=timeout,
            inherit_env=inherit_env,
            input_text=input_text,
            **kwargs,
        )
    if _autonomous_boundary.get() is not None:
        # Autonomous semantic execution without a proven containment
        # path: readable scope is itself a security boundary, so a
        # zero-write autonomous child may never fall through to an
        # ordinary uncontained launch.  Fail CLOSED before semantic
        # execution.  (Non-autonomous callers keep the legacy direct
        # path below unchanged.)
        raise SandboxedProcessError(READONLY_CONTAINMENT_UNAVAILABLE_MESSAGE)
    return run_process(
        list(args),
        cwd=cwd,
        env=env,
        timeout=timeout,
        inherit_env=inherit_env,
        input_text=input_text,
        **kwargs,
    )


def _bwrap_prefix_for_policy(policy: Any) -> list[str]:
    """Return the ``bwrap`` argv prefix enforcing ``policy`` at the kernel."""
    from orchestrator_mvp.sandbox.enforcement import (
        bwrap_mount_args_for_policy,
        ensure_sandbox_bind_dirs,
    )

    ensure_sandbox_bind_dirs(policy)
    return ["bwrap", *bwrap_mount_args_for_policy(policy), "--"]


def _contained_argv(policy: Any, argv: list[str]) -> list[str]:
    """Resolve ``argv[0]`` to its real host binary for contained launch.

    The contained filesystem exposes only the runtime binds plus the
    authorised repository scope: an ``argv[0]`` that is a host-side
    symlink into those binds (e.g. a virtualenv ``python`` pointing
    at ``/usr/bin/python3``) would not exist in-namespace and must be
    rewritten to the resolved path it names.  A symlink that resolves
    outside BOTH the repository and the runtime binds refuses: an
    execution escape must never be laundered through path text.
    Unresolvable names are left untouched so the launch fails loudly
    (``ENOENT``) instead of silently running something else.
    """
    from orchestrator_mvp.sandbox.enforcement import (
        SandboxEnforcementError,
        runtime_readonly_binds,
    )

    if not argv or not argv[0]:
        raise SandboxEnforcementError("refusing to launch an empty child argv")
    first = argv[0]
    resolved: str | None = None
    if os.path.isabs(first) and os.path.lexists(first):
        resolved = os.path.realpath(first)
    elif "/" not in first and "\\" not in first:
        found = shutil.which(first)
        if found is not None:
            resolved = os.path.realpath(found)
    if resolved is None or resolved == first:
        return list(argv)
    root_real = os.path.realpath(policy.repo_root)
    allowed_roots = [root_real, *runtime_readonly_binds()]
    if any(resolved == root or resolved.startswith(root + os.sep) for root in allowed_roots):
        return [resolved, *argv[1:]]
    raise SandboxEnforcementError(
        f"refusing contained launch of {first!r}: resolves outside the "
        "repository and the runtime binds"
    )


def _run_contained(
    args: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    inherit_env: bool = False,
    input_text: str | None = None,
    **kwargs: Any,
) -> ProcessResult:
    """Run the already-prefixed contained argv through the real seam.

    Sets the contained-launch token so the autonomous-boundary guard
    in :func:`run_process` admits this ONE launch (the kernel-enforced
    one) while still refusing any plain ``run_process`` call made
    under an active boundary.
    """
    token = _contained_launch.set(True)
    try:
        return run_process(
            args,
            cwd=cwd,
            env=env,
            timeout=timeout,
            inherit_env=inherit_env,
            input_text=input_text,
            **kwargs,
        )
    finally:
        _contained_launch.reset(token)


def launch_worker_process(
    argv: list[str],
    *,
    runner: Callable[..., ProcessResult] | None = None,
    **kwargs: Any,
) -> ProcessResult:
    """Launch one worker child through the generic execution seam.

    Every worker adapter calls this instead of ``run_process``
    directly.  Without an installed autonomous boundary it behaves
    exactly like the legacy direct launch (``runner or run_process``).
    With an active boundary the launch MUST go through the proven
    :func:`run_sandboxed_process` containment seam: an injected test
    runner cannot satisfy containment and is refused with a typed
    error instead of silently bypassing the boundary.  The contained
    child defaults to hermetic (``inherit_env=False``) unless the
    adapter explicitly opts into ambient inheritance.
    """
    boundary = _autonomous_boundary.get()
    if boundary is None:
        effective = runner if runner is not None else run_process
        return effective(list(argv), **kwargs)
    if runner is not None and runner is not run_process:
        raise SandboxedProcessError(
            "refusing autonomous worker launch through an injected runner: "
            "only the proven containment seam may execute semantic children"
        )
    sandbox_grant = kwargs.pop("sandbox_grant", None)
    if sandbox_grant is not None and sandbox_grant is not boundary.grant:
        raise SandboxedProcessError(
            "refusing autonomous worker launch with a foreign grant: "
            "the grant must be the boundary's effective grant"
        )
    return run_sandboxed_process(
        list(argv),
        policy=boundary.policy,
        cwd=kwargs.pop("cwd", None),
        env=kwargs.pop("env", None),
        timeout=kwargs.pop("timeout", None),
        input_text=kwargs.pop("input_text", None),
        inherit_env=kwargs.pop("inherit_env", False),
        trace_mechanism=boundary.trace_mechanism,
        sandbox_grant=boundary.grant,
        attempt_id=boundary.attempt_id,
        run_id=boundary.run_id,
        **kwargs,
    )


def run_autonomous_adapter(
    adapter: Any,
    request: WorkerRequest,
    *,
    autonomous: AutonomousDispatchConfig,
    attempt_id: str,
    run_id: str,
    repo_root: str,
    db: Any | None = None,
) -> WorkerResult:
    """Run one adapter dispatch inside the proven enforcement boundary.

    The generic production path for autonomous semantic execution:

    1. build the effective per-Attempt ``SandboxPolicy`` / ``ToolGrant``
       for ``repo_root`` from ``autonomous``;
    2. project the request environment through the generic
       allowlist (:func:`project_autonomous_child_env`): the child
       receives only non-secret runtime/control values plus
       credential material explicitly authorised by
       ``credential_refs``; unresolvable refs fail closed here;
    3. install the per-Attempt trace mechanism on the boundary
       (installed sandbox interceptor for every adapter, plus the
       verified native harness source when the adapter's provider
       owns one);
    4. run the deterministic pre-launch gate
       (:func:`preflight_autonomous_dispatch`): missing trace
       mechanism, unpinned identity, unenforceable network policy,
       provider-network need, missing containment (mutating OR
       read-only), or an unrepresentable writable scope all fail
       CLOSED here with a typed reason, before any semantic child
       exists;
    5. install the boundary and invoke ``adapter.run(request)``: the
       adapter's ordinary :func:`launch_worker_process` call is
       routed to kernel containment; any plain ``run_process``
       bypass attempt is refused;
    6. record the adapter's reported ``changed_paths`` as observed
       effects on the installed boundary (post/in-flight evidence;
       zero effects is valid);
    7. persist the observed evidence durably.  Persistence failure
       after execution raises :class:`AutonomousEvidenceError` -- the
       Attempt is never reported as an ordinary success;
    8. retire the Attempt's in-memory installation so stale
       eligibility material can never authorise a later Attempt.

    Returns the adapter's result unchanged.
    """
    from dataclasses import replace as _replace_request

    from orchestrator_mvp.sandbox.enforcement import (
        NATIVE_STRUCTURED_ADAPTER_SOURCES,
        SandboxEnforcementBackend,
        SandboxEnforcementError,
        VerifiedTraceMechanism,
        attach_sandbox_interceptor,
        preflight_autonomous_dispatch,
        record_intercepted_effect,
        retire_boundary_installation,
        verify_native_trace_source,
    )
    from orchestrator_mvp.sandbox.policy import grant_for_sandbox

    try:
        policy = _autonomous_policy_for(autonomous, repo_root)
        grant = grant_for_sandbox(policy)
    except SandboxEnforcementError as exc:
        raise SandboxedProcessError(
            f"refusing autonomous dispatch: no grantable resource surface: {exc}"
        ) from exc
    try:
        projected_request = _replace_request(
            request,
            env=project_autonomous_child_env(request.env, policy=policy),
        )
    except SandboxedProcessError:
        raise
    except Exception as exc:
        raise SandboxedProcessError(
            f"refusing autonomous dispatch: credential projection failed: {exc}"
        ) from exc
    provider = str(getattr(adapter, "provider_name", "unknown"))
    interceptor = attach_sandbox_interceptor(
        policy=policy, grant=grant, attempt_id=attempt_id, run_id=run_id
    )
    try:
        mechanism = VerifiedTraceMechanism(interceptor=interceptor)
        native_sources = sorted(NATIVE_STRUCTURED_ADAPTER_SOURCES.get(provider, frozenset()))
        if native_sources:
            try:
                native = verify_native_trace_source(
                    adapter_provider=provider,
                    source_kind=native_sources[0],
                    policy=policy,
                    grant=grant,
                    attempt_id=attempt_id,
                    run_id=run_id,
                )
                mechanism = VerifiedTraceMechanism(
                    native_source_kind=native.native_source_kind,
                    native_verified_by=native.native_verified_by,
                    interceptor=interceptor,
                )
            except SandboxEnforcementError:
                mechanism = VerifiedTraceMechanism(interceptor=interceptor)
        requires_provider_network = bool(getattr(adapter, "requires_provider_network", True))
        try:
            preflight_autonomous_dispatch(
                policy=policy,
                grant=grant,
                exposure=None,
                trace_mechanism=mechanism,
                backend=SandboxEnforcementBackend.BWRAP,
                requires_provider_network=requires_provider_network,
                attempt_id=attempt_id,
                run_id=run_id,
            )
        except SandboxEnforcementError as exc:
            raise SandboxedProcessError(str(exc)) from exc
        boundary = _AutonomousBoundary(
            policy=policy,
            grant=grant,
            trace_mechanism=mechanism,
            attempt_id=attempt_id,
            run_id=run_id,
        )
        token = _autonomous_boundary.set(boundary)
        try:
            result: WorkerResult = adapter.run(projected_request)
        finally:
            _autonomous_boundary.reset(token)
        for changed in getattr(result, "changed_paths", None) or ():
            try:
                record_intercepted_effect(
                    policy=policy,
                    grant=grant,
                    attempt_id=attempt_id,
                    run_id=run_id,
                    operation="FILE_WRITE",
                    resource_ref=str(changed),
                )
            except SandboxEnforcementError as exc:
                # Post-execution evidence loss is itself evidence
                # uncertainty: the runtime claims an observed effect
                # that cannot be authoritatively recorded, so the
                # Attempt must never be reported as an ordinary
                # result.  Never swallowed.
                raise AutonomousEvidenceError(
                    "autonomous effect evidence recording failed after "
                    f"semantic execution for attempt {attempt_id!r}: effects "
                    "are uncertain and the attempt must not be treated as "
                    "successful; missing durable trace blocks "
                    f"failover/replay: {exc}"
                ) from exc
        if db is not None:
            persisted = persist_autonomous_trace(
                db, policy=policy, grant=grant, attempt_id=attempt_id, run_id=run_id
            )
            if persisted is None:
                # The boundary was installed and semantic execution
                # occurred, yet no boundary evidence could be
                # persisted: returning worker success here would
                # erase effect uncertainty.  Fail closed instead.
                raise AutonomousEvidenceError(
                    "autonomous trace evidence missing after semantic "
                    f"execution for attempt {attempt_id!r}: effects are "
                    "uncertain and the attempt must not be treated as "
                    "successful; missing durable trace blocks "
                    "failover/replay: the installed boundary produced "
                    "no durable evidence"
                )
        return result
    finally:
        retire_boundary_installation(
            policy=policy, grant=grant, attempt_id=attempt_id, run_id=run_id
        )


def persist_autonomous_trace(
    db: Any,
    *,
    policy: Any,
    grant: Any,
    attempt_id: str,
    run_id: str,
) -> Any:
    """Persist the installed boundary's observed evidence, if any.

    Reads ONLY what the enforcement layer recorded for this attempt
    (see :func:`boundary_evidence_for`) into an
    :class:`AttemptTraceRecorder` row plus one tool-effect row per
    observed mutation.  Returns the finalised ``AttemptTrace`` or
    ``None`` when no boundary was installed.

    Never swallows persistence failure: when a boundary WAS installed
    (semantic execution may have produced effects) and durable
    persistence fails, raises :class:`AutonomousEvidenceError` so the
    Attempt is never reported as an ordinary successful autonomous
    result.  Evidence persistence failure after execution is
    load-bearing and is NOT equivalent to a pre-launch failure with
    zero effects.
    """
    from orchestrator_mvp.models import EffectStatus, ReconciliationStatus
    from orchestrator_mvp.observability.trace import AttemptTraceRecorder
    from orchestrator_mvp.sandbox import SideEffectSemantics
    from orchestrator_mvp.sandbox.enforcement import boundary_evidence_for

    evidence = boundary_evidence_for(
        attempt_id=attempt_id, run_id=run_id, policy=policy, grant=grant
    )
    if evidence is None:
        return None
    try:
        recorder = AttemptTraceRecorder(
            db=db,
            attempt_id=attempt_id,
            run_id=run_id,
            trace_class=evidence.trace_class,
            source_kind=evidence.source_kind,
        )
        for ref in evidence.effect_refs:
            recorder.record_tool_effect(
                operation="FILE_WRITE",
                capability=None,
                resource_ref=ref,
                side_effect_class=SideEffectSemantics.LOCAL_REPLAY_SAFE,
                status=EffectStatus.COMPLETED,
                reconciliation_status=ReconciliationStatus.LOCAL_REPLAY_SAFE,
            )
        return recorder.finalise()
    except Exception as exc:
        raise AutonomousEvidenceError(
            "autonomous trace evidence persistence failed after semantic "
            f"execution for attempt {attempt_id!r}: effects are uncertain "
            "and the attempt must not be treated as successful; "
            f"missing durable trace blocks failover/replay: {exc}"
        ) from exc


def sandboxed_child_write_text(
    policy: Any,
    grant: Any,
    repo_relative_path: str,
    content: str,
    *,
    timeout: float | None = 30.0,
    trace_mechanism: Any | None = None,
    attempt_id: str | None = None,
    run_id: str | None = None,
) -> ProcessResult:
    """Perform one policy-scoped file write via a REAL child process.

    Preflight (:func:`preflight_write`: policy AND grant,
    symlink-anchored) runs first and fails closed -- a denied write
    never spawns a child.  An allowed write is executed by a hermetic
    ``sys.executable`` child (empty inherited environment, content on
    stdin so no payload ever enters argv) writing exactly the resolved
    path that preflight authorised -- launched through the SAME
    :func:`run_sandboxed_process` containment seam production
    autonomous children use, so kernel containment (and the verified
    trace mechanism for mutating launches) applies here too.

    Mutating launches require the Attempt-pinned ``attempt_id`` /
    ``run_id`` the trace mechanism was installed for; without them the
    launch fails closed.
    """
    import sys

    from orchestrator_mvp.sandbox.enforcement import preflight_write

    resolved = preflight_write(policy, grant, repo_relative_path)
    script = (
        "import sys\n"
        "path = sys.argv[1]\n"
        "data = sys.stdin.read()\n"
        "with open(path, 'w', encoding='utf-8') as fh:\n"
        "    fh.write(data)\n"
    )
    return run_sandboxed_process(
        [sys.executable, "-c", script, str(resolved)],
        policy=policy,
        cwd=policy.repo_root,
        env={},
        timeout=timeout,
        inherit_env=False,
        input_text=content,
        trace_mechanism=trace_mechanism,
        sandbox_grant=grant,
        attempt_id=attempt_id,
        run_id=run_id,
    )


def _drain_stream(
    stream: IO[str],
    chunks: list[str],
    callback: Callable[[str], None] | None,
    errors: list[str] | None = None,
    stream_name: str = "stream",
    tracker: _ActivityTracker | None = None,
    monitor_touch: Callable[[], None] | None = None,
) -> None:
    """Drain a text stream line-by-line, capturing and optionally streaming.

    Any exception from reading or from the callback is recorded into *errors*
    instead of being silently swallowed, so the caller can surface it in the
    ProcessResult.  Callback errors do not truncate already-captured bytes;
    draining continues for remaining lines.  When *tracker* is provided, every
    captured line touches it so the owner thread can evaluate inactivity.
    When *monitor_touch* is provided, it is called for every captured line to
    feed the WorkerMonitor its output-presence evidence.
    """
    try:
        while True:
            try:
                line = stream.readline()
            except Exception as exc:
                if errors is not None:
                    errors.append(f"[Stream drain error ({stream_name}): {exc}]")
                break
            if line == "":
                break
            chunks.append(line)
            if tracker is not None:
                tracker.touch()
            if monitor_touch is not None:
                monitor_touch()
            if callback is not None:
                if line.endswith("\r\n"):
                    stripped = line[:-2]
                elif line.endswith("\n") or line.endswith("\r"):
                    stripped = line[:-1]
                else:
                    stripped = line
                try:
                    callback(stripped)
                except Exception as exc:
                    if errors is not None:
                        errors.append(f"[Callback error ({stream_name}): {exc}]")
                    # Continue draining remaining lines; do not truncate.
    except Exception as exc:
        if errors is not None:
            errors.append(f"[Stream drain error ({stream_name}): {exc}]")


def _write_stdin(stream: IO[str], text: str) -> None:
    """Write input_text to child's stdin and close to signal EOF."""
    try:
        stream.write(text)
        stream.flush()
    except (BrokenPipeError, OSError):
        pass
    finally:
        try:
            stream.close()
        except Exception:
            pass


class _ActivityTracker:
    """Thread-safe last-output tracker shared by drain threads and the wait loop.

    Drain threads call :meth:`touch` for every captured line; the owner thread
    reads :meth:`quiet_seconds` to evaluate the soft inactivity threshold.
    """

    __slots__ = ("_lock", "_last_output")

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_output = time.monotonic()

    def touch(self) -> None:
        with self._lock:
            self._last_output = time.monotonic()

    def quiet_seconds(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_output


def _fire_callback(
    callback: Callable[..., None],
    args: tuple[Any, ...],
    errors: list[str] | None,
    stream_name: str,
) -> None:
    """Invoke a callback, recording any exception instead of propagating it."""
    try:
        callback(*args)
    except Exception as exc:
        if errors is not None:
            errors.append(f"[Callback error ({stream_name}): {exc}]")


def _terminate_group_and_reap(
    proc: subprocess.Popen[Any],
    grace_seconds: float,
) -> tuple[bool, str]:
    """Stop the worker's entire process group via the ONE canonical primitive.

    Because ``run_process`` creates the subprocess with ``start_new_session=True``,
    the child's PID *is* its process group ID, so the canonical
    :func:`orchestrator_mvp.monitor.terminate_group_confirmed` covers the whole
    descendant tree: SIGTERM, bounded grace, SIGKILL if required, and confirmed
    /proc extinction.  The direct child is reaped afterwards so it cannot
    linger as a zombie.

    Returns ``(confirmed_extinct, reason)``.  Callers MUST inspect the result:
    ``confirmed_extinct`` is True only when every live non-zombie member of
    the worker PGID is provably gone.
    """
    from orchestrator_mvp.monitor import terminate_group_confirmed

    ok, reason = terminate_group_confirmed(
        proc.pid,
        grace_seconds=max(0.0, grace_seconds),
        kill_wait_seconds=5.0,
        poll_interval=0.05,
    )
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        pass
    return ok, reason


def _check_leader_exit_liveness(pgid: int) -> LeaderExitStatus:
    """Tri-state check after provider-leader exit.

    Never collapses UNKNOWN into EXTINCT: incomplete /proc evidence cannot
    become proof of extinction, no matter how many times it is retried.

    Returns:
        LIVE    — one or more live non-zombie group members observed.
        EXTINCT — complete evidence confirms zero live members.
        UNKNOWN — extinction cannot be proven (evidence remains incomplete).
    """
    from orchestrator_mvp.monitor import snapshot_process_group

    for _ in range(3):
        snap = snapshot_process_group(pgid)
        if snap.has_live_members:
            return LeaderExitStatus.LIVE
        if not snap.evidence_incomplete:
            return LeaderExitStatus.EXTINCT
        time.sleep(0.05)
    return LeaderExitStatus.UNKNOWN


_WAIT_SLICE_SECONDS: float = 0.2
_DEFAULT_TERMINATION_GRACE_SECONDS: float = 5.0

# Stream wrappers deliberately kept referenced when a surviving orphan
# descendant holds a pipe end and a drain thread remains blocked in readline.
# Letting them be garbage-collected would run BufferedReader.close() on the
# lock the blocked drain thread holds, hanging run_process's return.  This is
# a rare, bounded leak on the unconfirmed-termination failure path only.
_ORPHANED_PIPE_STREAMS: list[Any] = []


def run_process(
    args: list[str],
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    inherit_env: bool = True,
    inherit_env_exclude: tuple[str, ...] = (),
    input_text: str | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    on_process_start: Callable[[dict[str, Any]], None] | None = None,
    inactivity_timeout: float | None = None,
    stall_confirmation: float | None = None,
    on_stall: Callable[[float], None] | None = None,
    on_resume: Callable[[], None] | None = None,
    termination_grace_seconds: float = _DEFAULT_TERMINATION_GRACE_SECONDS,
    worker_monitor: Any | None = None,
    on_monitor_transition: Callable[[dict[str, Any]], None] | None = None,
) -> ProcessResult:
    """Execute a subprocess deterministically without shell=True.

    Args:
        args: Command and arguments as an argv list.
        cwd: Working directory for the process.
        env: Explicit environment variables.
        timeout: Explicit absolute runtime budget in seconds.  When set (and
            positive), the provider process is terminated if it exceeds this
            duration.  When ``None``, no hard deadline is enforced; the process
            runs until completion or external cancellation.
        inherit_env: If True, merges env with current process environment.
        inherit_env_exclude: Names removed from the inherited parent
            environment before explicit ``env`` values are overlaid.
        input_text: Optional text to feed to stdin of the subprocess.
        on_stdout: Optional callback invoked for each stdout line (without trailing
            newline). Enables live streaming mode when provided.
        on_stderr: Optional callback invoked for each stderr line (without trailing
            newline). Enables live streaming mode when provided.
        on_process_start: Optional callback invoked once immediately after the
            subprocess starts.  Receives a dict with the real provider process
            identity (pid, process_group_id, start_ticks, host, boot_id) so the
            caller can fence later destructive operations against PID reuse.
        inactivity_timeout: Soft inactivity threshold in seconds.  Exceeding it
            never terminates the process; it only reports a stall via *on_stall*
            (and recovery via *on_resume* once output arrives again).  Requires
            the streaming path, which is selected automatically.
        stall_confirmation: Whole-tree inactivity duration (measured from the
            last meaningful activity) that the internal WorkerMonitor requires
            before confirming STALLED and authorising termination.  Defaults
            to ``monitor.STALL_CONFIRMATION_SECONDS`` (1800s).
        on_stall: Optional callback invoked with the quiet duration in seconds
            when the inactivity threshold is first crossed.
        on_resume: Optional callback invoked when output resumes after a stall.
        termination_grace_seconds: Bounded grace period between SIGTERM and
            SIGKILL on hard-deadline expiry.
        worker_monitor: Optional :class:`orchestrator_mvp.monitor.WorkerMonitor`
            instance.  When provided, its ``touch_stdout`` / ``touch_stderr``
            methods are called from the drain threads (no lock needed — they
            are thread-safe); ``sample()`` is called once per wait-loop
            iteration; and a STALLED verdict authorises graceful tree
            termination without a hard deadline.
        on_monitor_transition: Optional callback invoked with a dict payload
            ONLY when the WorkerMonitor's :class:`MonitorState` *changes*
            (never per sample).  Payload keys: ``state``, ``reason``,
            ``pgid``, ``member_count``, ``evidence_incomplete``,
            ``timestamp``.  Callback errors never abort execution.

    Returns:
        ProcessResult with exit code, stdout, stderr, duration, and timeout
        status.  ``termination_incomplete`` is True when the worker process
        group could not be confirmed extinct before returning (provider
        leader exited with surviving descendants whose termination could not
        be confirmed); such a result is a deterministic execution failure.
    """
    if not args:
        raise ValueError("Cannot execute empty command args")

    # Autonomous fail-closed boundary: while an autonomous execution
    # boundary is installed, ordinary ``run_process`` launches are
    # refused -- semantic children may only start through the proven
    # :func:`run_sandboxed_process` containment seam (which sets the
    # contained-launch token for its one kernel-enforced launch).
    # Adapters that have not adopted :func:`launch_worker_process`
    # fail here, loudly, before semantic execution -- never
    # unsandboxed.
    if _autonomous_boundary.get() is not None and not _contained_launch.get():
        raise SandboxedProcessError(
            "refusing process launch outside the proven containment seam: "
            "an autonomous execution boundary is installed for this attempt"
        )

    resolved_cwd = str(Path(cwd).resolve()) if cwd is not None else None

    resolved_env: dict[str, str] = {}
    if inherit_env:
        resolved_env.update(os.environ)
        for key in inherit_env_exclude:
            resolved_env.pop(key, None)
    if env is not None:
        resolved_env.update(env)

    streaming = on_stdout is not None or on_stderr is not None or inactivity_timeout is not None
    # Soft inactivity supervision needs the streaming wait loop; the blocking
    # non-streaming path has no line granularity to evaluate it against.
    if inactivity_timeout is not None and not streaming:
        streaming = True

    start_time = time.monotonic()
    timed_out = False
    launch_failed = False
    stdout = ""
    stderr = ""
    exit_code = -1

    if not streaming:
        try:
            completed = subprocess.run(
                args,
                input=input_text,
                cwd=resolved_cwd,
                env=resolved_env,
                capture_output=True,
                text=True,
                timeout=timeout,
                shell=False,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            if exc.stdout:
                stdout = (
                    exc.stdout.decode("utf-8", errors="replace")
                    if isinstance(exc.stdout, bytes)
                    else str(exc.stdout)
                )
            if exc.stderr:
                stderr = (
                    exc.stderr.decode("utf-8", errors="replace")
                    if isinstance(exc.stderr, bytes)
                    else str(exc.stderr)
                )
            stderr += f"\n[Process timed out after {timeout} seconds]"
        except OSError as exc:
            exit_code = -1
            launch_failed = True
            if exc.errno == errno.E2BIG:
                stderr = (
                    f"[Execution error: Argument list too long (command arguments or "
                    f"environment exceeded OS limits / E2BIG): {exc}]"
                )
            elif exc.errno == errno.ENAMETOOLONG:
                stderr = f"[Execution error: File name or path too long (ENAMETOOLONG): {exc}]"
            else:
                stderr = f"[Execution error: {exc}]"
        except Exception as exc:
            exit_code = -1
            launch_failed = True
            stderr = f"[Execution error: {exc}]"

        duration = time.monotonic() - start_time
        return ProcessResult(
            args=args,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(duration, 3),
            timed_out=timed_out,
            launch_failed=launch_failed,
        )

    # Streaming path: concurrent stdout/stderr draining with line discipline.
    try:
        proc = subprocess.Popen(
            args,
            cwd=resolved_cwd,
            env=resolved_env,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            shell=False,
            start_new_session=True,
        )
    except OSError as exc:
        exit_code = -1
        launch_failed = True
        if exc.errno == errno.E2BIG:
            stderr = (
                f"[Execution error: Argument list too long (command arguments or "
                f"environment exceeded OS limits / E2BIG): {exc}]"
            )
        elif exc.errno == errno.ENAMETOOLONG:
            stderr = f"[Execution error: File name or path too long (ENAMETOOLONG): {exc}]"
        else:
            stderr = f"[Execution error: {exc}]"
        duration = time.monotonic() - start_time
        return ProcessResult(
            args=args,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(duration, 3),
            timed_out=False,
            launch_failed=True,
        )
    except Exception as exc:
        exit_code = -1
        launch_failed = True
        stderr = f"[Execution error: {exc}]"
        duration = time.monotonic() - start_time
        return ProcessResult(
            args=args,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=round(duration, 3),
            timed_out=False,
            launch_failed=True,
        )

    # Non-fatal callback: expose the real provider process identity for fencing.
    if on_process_start is not None:
        try:
            from orchestrator_mvp.ownership import ProcessIdentity as _ProcId

            identity = _ProcId.for_pid(proc.pid)
            if identity is not None:
                on_process_start(identity.to_dict())
        except Exception:
            # Process-start callback is advisory-only; a failure must not abort
            # the worker or truncate output.
            pass

    # Instantiate WorkerMonitor for the real PGID if one was not passed in.
    # Because run_process uses start_new_session=True, proc.pid == PGID.
    _monitor = worker_monitor
    if _monitor is None and (
        on_stdout is not None or on_stderr is not None or inactivity_timeout is not None
    ):
        # Build a monitor with the inactivity_timeout as the soft threshold
        # so both the _ActivityTracker and the monitor agree on the boundary.
        try:
            from orchestrator_mvp import monitor as _mon_mod
            from orchestrator_mvp.monitor import WorkerMonitor as _WM
            _monitor = _WM(
                proc.pid,
                soft_inactivity=float(inactivity_timeout or 300.0),
                stall_confirmation=(
                    float(stall_confirmation)
                    if stall_confirmation is not None
                    else _mon_mod.STALL_CONFIRMATION_SECONDS
                ),
                # Read at call time (not class-default time) so tests can
                # tighten the sampling cadence via the module constant.
                sample_interval=float(_mon_mod.MONITOR_SAMPLE_INTERVAL_SECONDS),
            )
        except Exception:
            _monitor = None

    stdout_chunks: list[str] = []
    stderr_chunks: list[str] = []
    drain_errors: list[str] = []
    threads: list[threading.Thread] = []
    tracker = _ActivityTracker()
    termination_incomplete = False
    termination_note: str | None = None

    # Bounded monitor-state projection: emit ONLY on MonitorState change,
    # never per sample.  The baseline is the monitor's construction state.
    _last_emitted_state: list[Any] = [
        _monitor.state if (_monitor is not None and on_monitor_transition is not None) else None
    ]

    def _emit_monitor_transition(state: Any, reason: str) -> None:
        if on_monitor_transition is None or _monitor is None:
            return
        if state is _last_emitted_state[0]:
            return
        _last_emitted_state[0] = state
        evidence = _monitor.evidence
        payload = {
            "state": getattr(state, "value", str(state)),
            "reason": reason,
            "pgid": getattr(_monitor, "pgid", None),
            "member_count": getattr(evidence, "member_count", None),
            "evidence_incomplete": bool(getattr(evidence, "evidence_incomplete", False)),
            "timestamp": time.time(),
        }
        _fire_callback(on_monitor_transition, (payload,), drain_errors, "monitor_transition")

    try:
        _mon_stdout_touch = _monitor.touch_stdout if _monitor is not None else None
        _mon_stderr_touch = _monitor.touch_stderr if _monitor is not None else None
        if proc.stdout is not None:
            t_out = threading.Thread(
                target=_drain_stream,
                args=(
                    proc.stdout,
                    stdout_chunks,
                    on_stdout,
                    drain_errors,
                    "stdout",
                    tracker,
                    _mon_stdout_touch,
                ),
                daemon=True,
            )
            t_out.start()
            threads.append(t_out)
        if proc.stderr is not None:
            t_err = threading.Thread(
                target=_drain_stream,
                args=(
                    proc.stderr,
                    stderr_chunks,
                    on_stderr,
                    drain_errors,
                    "stderr",
                    tracker,
                    _mon_stderr_touch,
                ),
                daemon=True,
            )
            t_err.start()
            threads.append(t_err)

        writer_thread: threading.Thread | None = None
        if input_text is not None and proc.stdin is not None:
            writer_thread = threading.Thread(
                target=_write_stdin,
                args=(proc.stdin, input_text),
                daemon=True,
            )
            writer_thread.start()

        try:
            hard_deadline: float | None = start_time + timeout if timeout is not None else None
            stall_reported = False
            monitor_stall_terminated = False
            while True:
                try:
                    proc.wait(timeout=_WAIT_SLICE_SECONDS)
                    exit_code = proc.returncode if proc.returncode is not None else -1
                    # Process exited.  Drain threads may still be delivering
                    # their last buffered lines; give them a moment and run
                    # one final activity check so any last-minute output
                    # that arrived just before exit is not mistaken for a
                    # sustained stall.
                    for t in threads:
                        t.join(timeout=1.0)
                    if inactivity_timeout is not None and stall_reported:
                        quiet = tracker.quiet_seconds()
                        if quiet < inactivity_timeout:
                            stall_reported = False
                            if on_resume is not None:
                                _fire_callback(on_resume, (), drain_errors, "resume")
                    # Sample monitor on normal exit to mark TERMINAL
                    if _monitor is not None:
                        try:
                            final_state = _monitor.sample()
                            _emit_monitor_transition(
                                final_state, getattr(_monitor, "reason", "")
                            )
                        except Exception:
                            pass
                    # Provider-leader exit does NOT imply worker-tree
                    # extinction: orphan descendants (compilers, test runners,
                    # tools spawned by the provider CLI) may still be alive and
                    # mutating the worktree.  Require confirmed group
                    # extinction before returning a normal completion.
                    _leader_status = _check_leader_exit_liveness(proc.pid)
                    if _leader_status is LeaderExitStatus.LIVE:
                        if _monitor is not None:
                            try:
                                _monitor.mark_terminating()
                                _emit_monitor_transition(
                                    _monitor.state, getattr(_monitor, "reason", "")
                                )
                            except Exception:
                                pass
                        ok, reason = _terminate_group_and_reap(
                            proc, termination_grace_seconds
                        )
                        if ok:
                            if _monitor is not None:
                                try:
                                    _monitor.mark_terminal()
                                    _emit_monitor_transition(
                                        _monitor.state, getattr(_monitor, "reason", "")
                                    )
                                except Exception:
                                    pass
                        else:
                            # Extinction could NOT be confirmed.  Never report
                            # a normal completion: the worktree must not be
                            # treated as safely quiescent by commit/gate/retry.
                            termination_incomplete = True
                            exit_code = -1
                            termination_note = (
                                f"\n[Execution error: worker process group {proc.pid} "
                                f"extinction could not be confirmed after leader "
                                f"exit ({reason})]"
                            )
                    elif _leader_status is LeaderExitStatus.UNKNOWN:
                        # Extinction cannot be proven because /proc evidence
                        # remains incomplete.  Pass through the canonical
                        # terminator — it is safe to try — but normal
                        # completion is never allowed without confirmed
                        # extinction.
                        if _monitor is not None:
                            try:
                                _monitor.mark_terminating()
                                _emit_monitor_transition(
                                    _monitor.state, getattr(_monitor, "reason", "")
                                )
                            except Exception:
                                pass
                        ok, reason = _terminate_group_and_reap(
                            proc, termination_grace_seconds
                        )
                        if ok:
                            if _monitor is not None:
                                try:
                                    _monitor.mark_terminal()
                                    _emit_monitor_transition(
                                        _monitor.state, getattr(_monitor, "reason", "")
                                    )
                                except Exception:
                                    pass
                        else:
                            termination_incomplete = True
                            exit_code = -1
                            termination_note = (
                                f"\n[Execution error: worker process group {proc.pid} "
                                f"extinction could not be confirmed after leader "
                                f"exit ({reason})]"
                            )
                    # else LeaderExitStatus.EXTINCT → normal completion
                    break
                except subprocess.TimeoutExpired:
                    pass

                now_mono = time.monotonic()

                # Hard absolute deadline: regardless of activity this execution
                # cannot run forever.  Stop gracefully, then force if needed.
                if hard_deadline is not None and now_mono >= hard_deadline:
                    timed_out = True
                    # Preserve the established timeout contract: the deadline
                    # result reports exit_code -1 regardless of which signal
                    # actually stopped the child.
                    if _monitor is not None:
                        try:
                            _monitor.mark_terminating()
                            _emit_monitor_transition(
                                _monitor.state, getattr(_monitor, "reason", "")
                            )
                        except Exception:
                            pass
                    ok, reason = _terminate_group_and_reap(proc, termination_grace_seconds)
                    if _monitor is not None and ok:
                        try:
                            _monitor.mark_terminal()
                            _emit_monitor_transition(
                                _monitor.state, getattr(_monitor, "reason", "")
                            )
                        except Exception:
                            pass
                    if not ok:
                        # Extinction unconfirmed: deterministic failure, never
                        # a plain timeout with a possibly-live worktree.
                        termination_incomplete = True
                        termination_note = (
                            f"\n[Execution error: worker process group {proc.pid} "
                            f"extinction could not be confirmed after timeout "
                            f"({reason})]"
                        )
                    exit_code = -1
                    break

                # Sample the WorkerMonitor at its configured interval.
                # State transitions (not every sample) may drive termination.
                if _monitor is not None and not monitor_stall_terminated:
                    try:
                        from orchestrator_mvp.monitor import MonitorState as _MS
                        mon_state = _monitor.sample()
                        _emit_monitor_transition(
                            mon_state, getattr(_monitor, "reason", "")
                        )
                        if mon_state is _MS.TERMINAL:
                            # Process tree gone — confirm via proc.poll
                            rc = proc.poll()
                            if rc is not None:
                                exit_code = rc
                                break
                        elif mon_state is _MS.STALLED and _monitor.should_terminate():
                            # Confirmed deterministic stall — authorise graceful
                            # tree termination.  This is the ONLY path that
                            # auto-terminates a worker (no tier-derived deadline).
                            monitor_stall_terminated = True
                            if _monitor is not None:
                                _monitor.mark_terminating()
                                _emit_monitor_transition(
                                    _monitor.state, getattr(_monitor, "reason", "")
                                )
                            ok, reason = _terminate_group_and_reap(
                                proc, termination_grace_seconds
                            )
                            if _monitor is not None and ok:
                                _monitor.mark_terminal()
                                _emit_monitor_transition(
                                    _monitor.state, getattr(_monitor, "reason", "")
                                )
                            if not ok:
                                # Extinction unconfirmed: deterministic
                                # execution failure, never a normal completion.
                                termination_incomplete = True
                                termination_note = (
                                    f"\n[Execution error: worker process group "
                                    f"{proc.pid} extinction could not be confirmed "
                                    f"after confirmed stall ({reason})]"
                                )
                            exit_code = proc.returncode if proc.returncode is not None else -1
                            break
                    except Exception:
                        pass  # Monitor errors must never abort execution

                # Soft inactivity expectation: quiet past the threshold means
                # report attention, never kill.  Activity resets the state so a
                # resumed worker continues without restart.
                if inactivity_timeout is not None:
                    quiet = tracker.quiet_seconds()
                    if quiet >= inactivity_timeout and not stall_reported:
                        stall_reported = True
                        if on_stall is not None:
                            _fire_callback(on_stall, (quiet,), drain_errors, "stall")
                    elif quiet < inactivity_timeout and stall_reported:
                        stall_reported = False
                        if on_resume is not None:
                            _fire_callback(on_resume, (), drain_errors, "resume")
        finally:
            # After process-group extinction (confirmed in the wait loop),
            # close owned pipe handles and join drain threads with short
            # bounded cleanup.  Never permit cleanup to extend indefinitely.
            if writer_thread is not None:
                writer_thread.join(timeout=5.0)
                try:
                    if proc.stdin is not None and not proc.stdin.closed:
                        proc.stdin.close()
                except Exception:
                    pass
            for t in threads:
                t.join(timeout=5.0)
            # Close owned pipe handles without blocking: a surviving orphan
            # descendant may hold the write end, and BufferedReader.close()
            # would block on the buffer lock held by a drain thread blocked
            # in readline.  Close the raw descriptor instead, and keep the
            # stream wrappers referenced so GC can never attempt a blocking
            # close behind run_process's back.  Drain threads are daemons
            # and were bounded-joined above.
            if any(t.is_alive() for t in threads):
                _ORPHANED_PIPE_STREAMS.append((proc.stdout, proc.stderr))
            for stream in (proc.stdout, proc.stderr):
                if stream is None:
                    continue
                try:
                    os.close(stream.fileno())
                except Exception:
                    try:
                        stream.close()
                    except Exception:
                        pass

        stdout = "".join(stdout_chunks)
        stderr = "".join(stderr_chunks)
        if drain_errors:
            error_block = "\n".join(drain_errors)
            if stderr:
                if not stderr.endswith("\n"):
                    stderr += "\n"
                stderr += error_block
            else:
                stderr = error_block
        if timed_out:
            stderr += f"\n[Process timed out after {timeout} seconds]"
        if termination_note:
            stderr += termination_note

    except OSError as exc:
        exit_code = -1
        launch_failed = True
        if exc.errno == errno.E2BIG:
            stderr = (
                f"[Execution error: Argument list too long (command arguments or "
                f"environment exceeded OS limits / E2BIG): {exc}]"
            )
        elif exc.errno == errno.ENAMETOOLONG:
            stderr = f"[Execution error: File name or path too long (ENAMETOOLONG): {exc}]"
        else:
            stderr = f"[Execution error: {exc}]"
    except Exception as exc:
        exit_code = -1
        launch_failed = True
        stderr = f"[Execution error: {exc}]"

    duration = time.monotonic() - start_time
    return ProcessResult(
        args=args,
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_seconds=round(duration, 3),
        timed_out=timed_out,
        termination_incomplete=termination_incomplete,
        launch_failed=launch_failed,
    )
