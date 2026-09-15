"""GitHub Copilot CLI adapter with profile-distinguished quota ownership.

C11-C introduces the Copilot backend as a *backend*, not as a single
provider.  Two profiles share the ``copilot_cli`` executable but differ
in every dimension the binding registry cares about:

* **GitHub-hosted profile** -- the canonical Copilot backend where
  GitHub itself chooses a provider/model.  The pool is ``github-aic``
  (or whichever configured GitHub scarcity pool is in scope).
* **MiniMax BYOK profile** -- the user supplies a ``MINIMAX_API_KEY`` and
  configures Copilot with an OpenAI-compatible base URL.  The pool is
  ``minimax-subscription``.

The adapter itself is deliberately *thin*: it constructs the right argv
for the active profile, applies per-Attempt environment construction, and
refuses to make Copilot a structural requirement anywhere else in the
codebase.  Copilot's session-resume / session-id is treated as *optional*
historical evidence only -- it is never Project memory.

Harness capabilities are *declared* in the same way as the other adapters
but only *after* they have actually been observed and tested.  The
:class:`CopilotAdapter` deliberately does not claim
``NATIVE_STRUCTURED`` tracing unless the harness emits a verifiable event
stream; the autonomous eligibility predicate
(:func:`saberops.observability.trace.is_autonomous_eligible`)
still applies at the dispatch boundary.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from saberops.git import GitManager
from saberops.models import (
    AttemptStatus,
    DispatchOutcome,
    WorkerRequest,
    WorkerResult,
)
from saberops.process import (
    ProcessResult,
    get_prompt_arg_limit,
    launch_worker_process,
    run_process,
)
from saberops.telemetry import parse_opencode_jsonl
from saberops.workers.base import WorkerAdapter, streaming_process_kwargs

__all__ = [
    "COPILOT_PROVIDER_NAME",
    "COPILOT_BACKEND",
    "COPILOT_GITHUB_PROFILE_ID",
    "COPILOT_GITHUB_POOL_ID",
    "COPILOT_MINIMAX_PROFILE_ID",
    "COPILOT_MINIMAX_POOL_ID",
    "COPILOT_PROFILE_ENV_VAR",
    "MINIMAX_PROVIDER_NAME",
    "MINIMAX_DEFAULT_BASE_URL",
    "MINIMAX_DEFAULT_MODEL",
    "CopilotAdapter",
    "CopilotProfile",
    "CopilotProfileKind",
    "copilot_overlay_for_binding",
]


# Canonical identifiers used by the binding registry.  Two distinct profiles
# share the same executable but never the same quota pool.
COPILOT_PROVIDER_NAME: str = "copilot"
COPILOT_BACKEND: str = "copilot_cli"
COPILOT_GITHUB_PROFILE_ID: str = "copilot-github-hosted"
COPILOT_GITHUB_POOL_ID: str = "github-aic"
COPILOT_MINIMAX_PROFILE_ID: str = "copilot-minimax-byok"
COPILOT_MINIMAX_POOL_ID: str = "minimax-subscription"
MINIMAX_PROVIDER_NAME: str = "minimax"
MINIMAX_DEFAULT_BASE_URL: str = "https://api.minimax.chat/v1"
MINIMAX_DEFAULT_MODEL: str = "MiniMax-M3"


class CopilotProfileKind(StrEnum):
    """The two profile kinds the Copilot backend may run under.

    * ``GITHUB_HOSTED``  -- GitHub-authenticated, GitHub-chosen pool.
    * ``MINIMAX_BYOK``   -- user-supplied MiniMax API key, BYOK pool.
    """

    GITHUB_HOSTED = "GITHUB_HOSTED"
    MINIMAX_BYOK = "MINIMAX_BYOK"


@dataclass(frozen=True)
class CopilotProfile:
    """The bounded, runtime-visible identity of one Copilot profile.

    ``profile_id``, ``provider``, and ``pool_id`` are the three fields the
    binding registry requires to discriminate profiles.  Two profiles that
    share any one of them are not distinct; ``provider == copilot`` does
    *not* automatically imply they share a quota pool, and
    ``provider == copilot`` does not automatically imply a single
    provider-neutral identity for MiniMax BYOK.

    ``base_url`` and ``api_key_env`` only matter for ``MINIMAX_BYOK``;
    GitHub-hosted profile delegates auth to the user's ``gh auth login``
    / ``GITHUB_TOKEN`` / ``COPILOT_TOKEN`` chain and never reads a
    MiniMax key.  ``api_key_value_env`` is the optional alternative name
    the adapter should *write* the resolved credential under for the
    child process -- distinct from ``api_key_env`` so a per-Attempt
    environment can carry exactly one resolved value without the global
    ambient env being mutated.
    """

    kind: CopilotProfileKind
    profile_id: str
    provider: str
    pool_id: str
    model: str = ""
    base_url: str | None = None
    api_key_env: str | None = None
    api_key_value_env: str | None = None

    def __post_init__(self) -> None:
        if self.kind is CopilotProfileKind.GITHUB_HOSTED:
            if self.profile_id != COPILOT_GITHUB_PROFILE_ID:
                raise ValueError(
                    f"GitHub-hosted profile must use {COPILOT_GITHUB_PROFILE_ID!r}"
                )
            if self.provider != COPILOT_PROVIDER_NAME:
                raise ValueError(
                    f"GitHub-hosted profile must use provider {COPILOT_PROVIDER_NAME!r}"
                )
            if self.pool_id != COPILOT_GITHUB_POOL_ID:
                raise ValueError(
                    f"GitHub-hosted profile must use pool {COPILOT_GITHUB_POOL_ID!r}"
                )
            if self.base_url is not None:
                raise ValueError("GitHub-hosted profile must not carry a base_url")
            if self.api_key_env is not None:
                raise ValueError("GitHub-hosted profile must not carry an api_key_env")
            if self.api_key_value_env is not None:
                raise ValueError(
                    "GitHub-hosted profile must not carry an api_key_value_env"
                )
        elif self.kind is CopilotProfileKind.MINIMAX_BYOK:
            if self.profile_id != COPILOT_MINIMAX_PROFILE_ID:
                raise ValueError(
                    f"MiniMax BYOK profile must use {COPILOT_MINIMAX_PROFILE_ID!r}"
                )
            if self.provider != MINIMAX_PROVIDER_NAME:
                raise ValueError(
                    f"MiniMax BYOK profile must use provider {MINIMAX_PROVIDER_NAME!r}"
                )
            if self.pool_id != COPILOT_MINIMAX_POOL_ID:
                raise ValueError(
                    f"MiniMax BYOK profile must use pool {COPILOT_MINIMAX_POOL_ID!r}"
                )
            if not self.base_url:
                raise ValueError("MiniMax BYOK profile must carry a base_url")
            if not self.api_key_env:
                raise ValueError("MiniMax BYOK profile must carry an api_key_env")
            if not self.api_key_value_env:
                raise ValueError(
                    "MiniMax BYOK profile must carry an api_key_value_env"
                )
            if not self.model:
                raise ValueError("MiniMax BYOK profile must carry an explicit model")
        else:  # pragma: no cover - exhaustive enum
            raise ValueError(f"unknown Copilot profile kind: {self.kind!r}")


GITHUB_HOSTED_PROFILE = CopilotProfile(
    kind=CopilotProfileKind.GITHUB_HOSTED,
    profile_id=COPILOT_GITHUB_PROFILE_ID,
    provider=COPILOT_PROVIDER_NAME,
    pool_id=COPILOT_GITHUB_POOL_ID,
)

MINIMAX_BYOK_PROFILE = CopilotProfile(
    kind=CopilotProfileKind.MINIMAX_BYOK,
    profile_id=COPILOT_MINIMAX_PROFILE_ID,
    provider=MINIMAX_PROVIDER_NAME,
    pool_id=COPILOT_MINIMAX_POOL_ID,
    base_url=MINIMAX_DEFAULT_BASE_URL,
    api_key_env="MINIMAX_API_KEY",
    api_key_value_env="MINIMAX_API_KEY_RESOLVED",
    model=MINIMAX_DEFAULT_MODEL,
)


# The set of profile factories a caller may use to construct a profile
# without re-deriving the canonical identifiers.  Kept as a tuple so the
# order is deterministic.
COPILOT_PROFILES: tuple[CopilotProfile, ...] = (
    GITHUB_HOSTED_PROFILE,
    MINIMAX_BYOK_PROFILE,
)


def profile_for_kind(kind: CopilotProfileKind) -> CopilotProfile:
    """Return the canonical :class:`CopilotProfile` for ``kind``."""
    for profile in COPILOT_PROFILES:
        if profile.kind is kind:
            return profile
    raise ValueError(f"unknown Copilot profile kind: {kind!r}")


def profile_for_id(profile_id: str) -> CopilotProfile:
    """Return the canonical :class:`CopilotProfile` for ``profile_id``."""
    for profile in COPILOT_PROFILES:
        if profile.profile_id == profile_id:
            return profile
    raise ValueError(f"unknown Copilot profile id: {profile_id!r}")


#: Non-secret request-env key selecting the active Copilot profile for
#: one dispatch.  Already honoured by :func:`_resolve_profile_from_request`
#: and already allowlisted for autonomous children; the C11-D exact
#: binding resolver writes it (never a secret) so the selected C11-B
#: profile controls the actual invocation.
COPILOT_PROFILE_ENV_VAR = "ORCH_COPILOT_PROFILE_ID"


def copilot_overlay_for_binding(
    *,
    provider: str,
    profile_id: str,
    account_ref: str = "",
    backend_ref: str = "",
) -> dict[str, str]:
    """Return the non-secret request-env overlay for one C11-B profile.

    A ``profile_id`` naming a known Copilot profile reuses the
    adapter's existing ``ORCH_COPILOT_PROFILE_ID`` seam, so the exact
    resolved profile -- not ambient state -- selects the child
    environment.  An unknown id on a profile that claims distinct
    account/backend behavior raises :class:`ValueError` (the adapter
    cannot truthfully enforce it); an unknown id with no distinct
    claims returns no overlay and the proven ordinary adapter path
    remains truthfully the exact binding.
    """
    known = {profile.profile_id for profile in COPILOT_PROFILES}
    if profile_id in known:
        return {COPILOT_PROFILE_ENV_VAR: profile_id}
    if account_ref.strip() or backend_ref.strip():
        raise ValueError(
            f"Copilot binding profile {profile_id!r} claims distinct "
            "account/backend behavior the adapter cannot enforce"
        )
    return {}


# ---------------------------------------------------------------------------
# Per-Attempt environment construction.
#
# Autonomous Copilot execution NEVER blindly inherits ``os.environ``.
# The child environment is an explicit allowlist: non-secret runtime
# variables required to execute the CLI plus exactly the credential
# material the selected profile requires.  An OpenAI / Anthropic /
# DeepSeek / OmniRoute / random ambient secret can never enter either
# child unless that exact profile explicitly requires it.  There is no
# blacklist to maintain: anything not allowlisted is absent.
# ---------------------------------------------------------------------------


#: Non-secret runtime variables a Copilot child may need to execute.
_COPILOT_RUNTIME_ENV_ALLOWLIST: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "LC_MESSAGES",
    "LANGUAGE",
    "TZ",
    "TERM",
)

#: Credential variables the GitHub-hosted profile may carry.  The child
#: delegates auth to the user's ``gh auth login`` / GitHub token chain;
#: no MiniMax (or any other provider) key is ever materialised here.
_COPILOT_GITHUB_CREDENTIAL_ENV: tuple[str, ...] = (
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "COPILOT_TOKEN",
)


def build_attempt_env(
    profile: CopilotProfile,
    *,
    parent_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Return a hermetic per-Attempt environment for ``profile``.

    Only allowlisted keys survive: the non-secret runtime variables in
    :data:`_COPILOT_RUNTIME_ENV_ALLOWLIST` plus exactly the credential
    material the selected profile requires:

    * ``GITHUB_HOSTED`` -- the GitHub/Copilot credential chain only
      (:data:`_COPILOT_GITHUB_CREDENTIAL_ENV`); any ambient MiniMax key
      (or any other provider secret) is dropped.
    * ``MINIMAX_BYOK`` -- the resolved MiniMax API key under
      ``api_key_value_env`` plus ``MINIMAX_BASE_URL``; the
      source-of-truth name (``api_key_env``) is never passed, and no
      GitHub credential enters this child.

    ``parent_env=None`` reads ``os.environ`` and filters it through the
    same allowlist, so the function never mutates ``os.environ`` and
    never depends on a blacklist of secret names.
    """
    source = dict(parent_env) if parent_env is not None else dict(os.environ)
    child: dict[str, str] = {}
    for key in _COPILOT_RUNTIME_ENV_ALLOWLIST:
        value = source.get(key)
        if value is not None:
            child[key] = value
    if profile.kind is CopilotProfileKind.GITHUB_HOSTED:
        for key in _COPILOT_GITHUB_CREDENTIAL_ENV:
            value = source.get(key)
            if value is not None:
                child[key] = value
        return child
    # MiniMax BYOK: only the resolved key and the base URL travel with
    # the child.  The child reads the key under ``api_key_value_env``;
    # the parent's source-of-truth name (``api_key_env``) is *not* passed.
    source_key = source.get(profile.api_key_env or "")
    if source_key:
        child[profile.api_key_value_env or "MINIMAX_API_KEY_RESOLVED"] = source_key
    if profile.base_url:
        child["MINIMAX_BASE_URL"] = profile.base_url
    return child


# ---------------------------------------------------------------------------
# The adapter itself.
# ---------------------------------------------------------------------------


# Sentinel env-var the adapter uses when the child process should read
# its MiniMax API key under a non-default name (Copilot picks the key
# value up at request time, never as command-line text).
_MINIMAX_KEY_SENTINEL_ENV = "_ORCH_COPILOT_MINIMAX_SENTINEL"


@dataclass(frozen=True)
class _CopilotInvocation:
    """The canonical, typed record of one Copilot adapter invocation.

    Persisted in process-trace evidence so the supervisor can audit
    *exactly* what was sent to the harness without leaking secret
    payloads.  ``argv`` is the redacted form (no env values, no
    command-line key text).
    """

    profile_id: str
    provider: str
    pool_id: str
    argv_redacted: tuple[str, ...]
    model: str
    transport: str
    session_id: str | None = None


class CopilotAdapter(WorkerAdapter):
    """Adapter dispatching tasks via the GitHub Copilot CLI.

    The adapter is *profile-aware*: the same ``copilot`` executable is
    reused, but the per-Attempt environment and the binding identity
    differ between the GitHub-hosted profile and the MiniMax BYOK
    profile.  Copilot's session-resume feature is intentionally NOT used
    here -- it is historical evidence (see :attr:`_CopilotInvocation.session_id`),
    never Project memory.
    """

    capability_transport = "DIRECT_CLI"
    # Factual capabilities the harness has been observed to expose.  The
    # adapter deliberately does NOT claim ``structured_usage`` /
    # ``large_prompt_stdin`` until those have been proven -- this is the
    # exact distinction C11-C draws between "the executable is on disk"
    # and "the capability has actually been observed for autonomous use".
    # It likewise does NOT claim ``sandbox_intercepted``: interception
    # is per-Attempt enforcement evidence derived from the actual
    # execution path (see ``resolve_trace_capability``), never a static
    # adapter declaration.
    factual_capabilities = frozenset(
        {
            "streaming",
            "process_identity",
            "cancellation",
        }
    )

    def __init__(
        self,
        runner: Callable[..., ProcessResult] = run_process,
        copilot_bin: str = "copilot",
    ) -> None:
        self.runner = runner
        self.copilot_bin = copilot_bin

    @property
    def provider_name(self) -> str:
        # The adapter itself reports ``copilot``; the profile narrows the
        # binding identity.  Bindings select between the two profiles via
        # ``profile_id`` / ``pool_id``.
        return COPILOT_PROVIDER_NAME

    def is_available(self) -> bool:
        if self.runner is not run_process:
            return True
        return shutil.which(self.copilot_bin) is not None

    def max_prompt_bytes(self) -> int | None:
        return None  # stdin fallback path verified; no byte ceiling.

    def prompt_transport(self) -> str:
        return "stdin"

    def prompt_transport_for(self, prompt_bytes: int) -> str:
        return "stdin" if prompt_bytes > get_prompt_arg_limit() else "argv"

    def run(self, request: WorkerRequest) -> WorkerResult:
        """Dispatch a task via the Copilot CLI under the active profile."""
        profile = _resolve_profile_from_request(request)
        if not self.is_available():
            return _missing_binary_result(request, profile)

        prompt_bytes = len(request.task.encode("utf-8"))
        use_stdin = prompt_bytes > get_prompt_arg_limit()
        argv = [self.copilot_bin, "exec"]
        prompt_arg = "-" if use_stdin else request.task
        if not use_stdin:
            argv.append(prompt_arg)
        argv.extend(["--model", request.model])
        argv.extend(["--cd", str(request.worktree_path)])
        argv.extend(["--format", "json"])

        kwargs: dict[str, Any] = {
            "cwd": request.worktree_path,
            "timeout": request.timeout_seconds,
            # Autonomous Copilot execution is hermetic: the child sees
            # ONLY the explicit allowlisted environment, never the
            # ambient parent environment.
            "inherit_env": False,
            **streaming_process_kwargs(request),
        }
        kwargs["inherit_env"] = False
        kwargs["env"] = _select_env_override(request, profile, kwargs, argv)

        if use_stdin:
            kwargs["input_text"] = request.task

        # The generic autonomous execution seam shared with every
        # other adapter: direct launch when no boundary is installed,
        # kernel containment (or a typed fail-closed refusal) when one
        # is.  Copilot carries no provider-specific sandbox branch.
        res = launch_worker_process(argv, runner=self.runner, **kwargs)
        transport_used = "stdin" if use_stdin else "argv"

        display_stdout, usage = parse_opencode_jsonl(res.stdout)
        changed_paths = GitManager.get_changed_paths(request.worktree_path)
        has_changes = len(changed_paths) > 0

        if res.timed_out:
            status = AttemptStatus.TIMEOUT
            outcome = DispatchOutcome.TIMEOUT
            error = f"Copilot CLI timed out after {request.timeout_seconds}s"
            summary = "Copilot execution timed out"
        elif res.termination_incomplete:
            status = AttemptStatus.FAILED
            outcome = DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE
            error = (
                "Copilot process group extinction could not be confirmed; "
                "worktree is not safely quiescent"
            )
            summary = error
        elif res.passed:
            status = AttemptStatus.SUCCESS
            outcome = None
            error = None
            out_strip = display_stdout.strip()
            summary = out_strip[:300] if out_strip else "Copilot completed successfully"
        else:
            status = AttemptStatus.FAILED
            outcome = DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE
            error = f"Copilot CLI exited with code {res.exit_code}"
            summary = (res.stderr.strip() or res.stdout.strip())[:300]

        # Identity: the requested provider/backend identity stays in
        # ``provider``/``model``; ``actual_provider``/``actual_model``
        # carry ONLY provider-reported identity.  Copilot did not report
        # one here, so they stay ``None`` -- never fabricated, and the
        # requested model string is never mistaken for a provider.
        return WorkerResult(
            provider=profile.provider,
            model=request.model,
            status=status,
            summary=summary,
            stdout=display_stdout,
            stderr=res.stderr,
            changed_paths=changed_paths,
            has_changes=has_changes,
            error=error,
            duration_seconds=res.duration_seconds,
            prompt_bytes=prompt_bytes,
            prompt_transport_used=transport_used,
            actual_provider=None,
            actual_model=None,
            outcome=outcome,
            usage=usage,
        )


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------


def _resolve_profile_from_request(request: WorkerRequest) -> CopilotProfile:
    """Derive the active Copilot profile from a worker request.

    The selection is deterministic:

    1. If ``request.env`` carries ``ORCH_COPILOT_PROFILE_ID`` and it
       matches a known profile, that profile wins.
    2. Otherwise, if ``request.env`` carries ``MINIMAX_API_KEY`` (or
       ``api_key_env`` value), the MiniMax BYOK profile wins.
    3. Otherwise the GitHub-hosted profile wins.
    """
    env = request.env or {}
    explicit = env.get(COPILOT_PROFILE_ENV_VAR)
    if explicit:
        return profile_for_id(explicit)
    if env.get(MINIMAX_BYOK_PROFILE.api_key_env or "MINIMAX_API_KEY"):
        return MINIMAX_BYOK_PROFILE
    return GITHUB_HOSTED_PROFILE


#: Request-overlay keys the caller may contribute to the child
#: environment.  ``request.env`` is an approved *overlay* source, never
#: a replacement for the safe runtime baseline: only profile selection
#: and the selected profile's credential source names are honoured
#: from it.  Anything else in ``request.env`` (notably hostile ambient
#: credentials smuggled through the request) is dropped before the
#: allowlisted projection below.
_REQUEST_ENV_OVERLAY_KEYS: frozenset[str] = frozenset(
    {
        "ORCH_COPILOT_PROFILE_ID",
        "MINIMAX_API_KEY",
        "GITHUB_TOKEN",
        "GH_TOKEN",
        "COPILOT_TOKEN",
    }
)


def _select_env_override(
    request: WorkerRequest,
    profile: CopilotProfile,
    kwargs: dict[str, Any],
    argv: list[str],
) -> dict[str, str]:
    """Build the hermetic per-Attempt environment for the active profile.

    The function never mutates ``os.environ``: it returns the projected
    child environment, and the caller hands it to ``run_process`` with
    ``inherit_env=False``.  Construction is always:

    * safe runtime baseline: allowlisted ``PATH`` / ``HOME`` / ``TMP``
      / locale variables from ``os.environ`` (so a ``request.env``
      that carries only the selected profile/key -- and no ``PATH`` --
      still yields a launchable child);
    * plus permitted request-specific profile/config values overlaid
      from ``request.env`` (profile selection + credential sources
      only -- see :data:`_REQUEST_ENV_OVERLAY_KEYS`);
    * plus ONLY the credential required by the selected profile.

    Hostile unrelated credentials -- ambient or smuggled through
    ``request.env`` -- remain absent from both profiles' children.
    For ``MINIMAX_BYOK``, a sentinel env-var name replaces the
    resolved key in the argv; the child resolves the value at request
    time, so the secret never enters command-line text.  For
    ``GITHUB_HOSTED``, no MiniMax material ever enters the child.
    """
    parent_env: dict[str, str] = dict(os.environ)
    if request.env is not None:
        for key, value in request.env.items():
            if key in _REQUEST_ENV_OVERLAY_KEYS:
                parent_env[key] = value
    if profile.kind is CopilotProfileKind.GITHUB_HOSTED:
        return build_attempt_env(profile, parent_env=parent_env)
    # MiniMax BYOK: project the child env and add a sentinel env-var
    # name so the harness resolves the API key at request time.  The
    # sentinel name is per-Attempt; the resolved value lives under
    # ``api_key_value_env``.
    projected = build_attempt_env(profile, parent_env=parent_env)
    if profile.api_key_value_env and projected.get(profile.api_key_value_env):
        projected[_MINIMAX_KEY_SENTINEL_ENV] = profile.api_key_value_env
        argv.extend(
            [
                "--provider",
                "openai-compatible",
                "--base-url",
                profile.base_url or MINIMAX_DEFAULT_BASE_URL,
                "--api-key-env",
                _MINIMAX_KEY_SENTINEL_ENV,
            ]
        )
    return projected


def _missing_binary_result(
    request: WorkerRequest, profile: CopilotProfile
) -> WorkerResult:
    return WorkerResult(
        provider=profile.provider,
        model=request.model,
        status=AttemptStatus.FAILED,
        summary="Copilot CLI not found on system",
        stdout="",
        stderr=f"{profile.profile_id} profile: copilot binary not found on PATH",
        changed_paths=[],
        has_changes=False,
        error=f"Copilot binary not found on PATH (profile={profile.profile_id})",
        actual_provider=None,
        actual_model=None,
        outcome=DispatchOutcome.PROVIDER_OR_RUNTIME_FAILURE,
    )
