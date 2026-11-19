"""Stored-credential billing guard for OpenCode worker routes.

OpenCode resolves its provider credentials from ``auth.json`` under
``XDG_DATA_HOME`` (``opencode_isolation`` deliberately passes the real data
directory through so sign-in and sessions keep working).  A stored credential
is invisible to the environment canaries that protect the other harnesses, so
this module states the owner's billing rule for it:

* a subscription plan is the only normal billing source ("5. yes unless user
  explicitly add an API or ask for it", owner, 2026-11-19);
* ``opencode/anthropic/*`` is refused outright: OpenCode has no Anthropic
  subscription sign-in, so the only credential it can use is the pay-per-token
  API key;
* ``opencode/openai/*`` is admitted only when the stored credential is OAuth
  (the ChatGPT-plan sign-in);
* any other provider whose stored credential is an API key is refused unless
  the owner explicitly enabled that provider for API billing in owner policy.

The file is read **structurally only**: the credential *type* is returned and
nothing else.  Secret values are never read into a return value, a log line or
an error message.

The escape hatch is the owner control-plane document::

    {"api_billing": {"providers": ["anthropic"]}}

Only an explicit entry there admits an API-key route, so plan limits stay the
default and a stored key can never bill silently.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path

__all__ = [
    "OPENCODE_API_KEY_REFUSED",
    "api_billing_refusal",
    "auth_requires_owner_api_billing",
    "plan_backed_opencode_providers",
    "stored_auth_type",
]

#: Typed refusal for a stored OpenCode credential that may bill per token.
OPENCODE_API_KEY_REFUSED = "OPENCODE_API_KEY_REFUSED"

_PLAN_BACKED_CACHE: frozenset[str] | None = None


def plan_backed_opencode_providers() -> frozenset[str]:
    """OpenCode provider ids backed by a packaged subscription-plan account.

    Derived from the packaged posture registry (``plans.json``): a selector
    ``opencode/<provider>/*`` with a non-null ``account`` names a provider
    whose stored credential belongs to a plan (for example
    ``opencode-go``), not to pay-per-token API billing.  Reading the packaged
    registry keeps the exemption deterministic and immune to an owner posture
    record added later, which is exactly what the hard Anthropic refusal needs.
    """
    global _PLAN_BACKED_CACHE
    if _PLAN_BACKED_CACHE is None:
        from saberops.control_plane.plans import load_packaged_plans_raw

        raw_postures = load_packaged_plans_raw().get("postures", {})
        postures = raw_postures if isinstance(raw_postures, dict) else {}
        prefix = "opencode/"
        suffix = "/*"
        providers: set[str] = set()
        for selector, record in postures.items():
            if not isinstance(selector, str) or not isinstance(record, dict):
                continue
            if (
                selector.startswith(prefix)
                and selector.endswith(suffix)
                and record.get("account")
            ):
                providers.add(selector[len(prefix) : -len(suffix)])
        _PLAN_BACKED_CACHE = frozenset(providers)
    return _PLAN_BACKED_CACHE


def stored_auth_type(
    provider_id: str,
    *,
    env: Mapping[str, str] | None = None,
    data_home: str | Path | None = None,
) -> str:
    """Return the stored credential type for one OpenCode provider id.

    Values: ``"oauth"`` (subscription sign-in), ``"api"`` (stored API key),
    ``"absent"`` (no stored credential) or ``"unknown"`` (unreadable file or
    unrecognised entry).  Only the ``type`` field is inspected; no secret
    content is returned or logged.
    """
    provider = provider_id.strip().lower()
    auth_path = _auth_file(env if env is not None else os.environ, data_home)
    if auth_path is None or not auth_path.is_file():
        return "absent"
    try:
        raw = json.loads(auth_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return "unknown"
    if not isinstance(raw, dict):
        return "unknown"
    entry = raw.get(provider)
    if not isinstance(entry, dict):
        return "absent"
    kind = entry.get("type")
    if kind == "oauth":
        return "oauth"
    if kind == "api":
        return "api"
    return "unknown"


def _auth_file(
    env: Mapping[str, str],
    data_home: str | Path | None,
) -> Path | None:
    if data_home is not None:
        return Path(data_home) / "opencode" / "auth.json"
    explicit = env.get("XDG_DATA_HOME")
    if explicit:
        return Path(explicit) / "opencode" / "auth.json"
    home = env.get("HOME")
    if not home:
        return None
    return Path(home) / ".local" / "share" / "opencode" / "auth.json"


def auth_requires_owner_api_billing(provider_id: str, auth_type: str) -> bool:
    """Return True when this stored credential needs explicit owner consent.

    Anthropic is always refused without that consent: OpenCode has no
    Anthropic subscription path.  OpenAI is admitted only as OAuth (the
    ChatGPT plan).  Any other API-key credential is admitted only for a
    provider backed by a packaged subscription plan.
    """
    provider = provider_id.strip().lower()
    if not provider:
        return False
    if provider == "anthropic":
        return True
    if provider == "openai":
        return auth_type != "oauth"
    if auth_type != "api":
        return False
    return provider not in plan_backed_opencode_providers()


def api_billing_refusal(provider_id: str, auth_type: str) -> str:
    """Return the typed refusal text for one refused stored credential."""
    provider = provider_id.strip().lower()
    detail = (
        "OpenCode route refused: the stored credential for provider "
        f"{provider!r} is not a subscription sign-in (auth type: {auth_type}). "
        "The owner's rule keeps billing on plan limits unless an API key is "
        "explicitly enabled for API billing in owner policy "
        '("api_billing": {"providers": ["<provider>"]}); '
        "Gemini is never served through OpenCode (Antigravity only)."
    )
    return f"{OPENCODE_API_KEY_REFUSED}: {detail}"
