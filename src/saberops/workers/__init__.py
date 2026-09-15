"""Worker adapters package."""

from saberops.process import ORCH_PROMPT_ARG_LIMIT, get_prompt_arg_limit
from saberops.workers.antigravity import AntigravityAdapter
from saberops.workers.base import WorkerAdapter
from saberops.workers.cline import ClineAdapter
from saberops.workers.codex import CodexAdapter
from saberops.workers.copilot import (
    COPILOT_GITHUB_POOL_ID,
    COPILOT_GITHUB_PROFILE_ID,
    COPILOT_MINIMAX_POOL_ID,
    COPILOT_MINIMAX_PROFILE_ID,
    COPILOT_PROVIDER_NAME,
    GITHUB_HOSTED_PROFILE,
    MINIMAX_BYOK_PROFILE,
    MINIMAX_DEFAULT_MODEL,
    MINIMAX_PROVIDER_NAME,
    CopilotAdapter,
    CopilotProfile,
    CopilotProfileKind,
    build_attempt_env,
)
from saberops.workers.opencode import OpenCodeAdapter
from saberops.workers.registry import AdapterRegistry

__all__ = [
    "AdapterRegistry",
    "AntigravityAdapter",
    "ClineAdapter",
    "CodexAdapter",
    "COPILOT_GITHUB_POOL_ID",
    "COPILOT_GITHUB_PROFILE_ID",
    "COPILOT_MINIMAX_POOL_ID",
    "COPILOT_MINIMAX_PROFILE_ID",
    "COPILOT_PROVIDER_NAME",
    "CopilotAdapter",
    "CopilotProfile",
    "CopilotProfileKind",
    "GITHUB_HOSTED_PROFILE",
    "MINIMAX_BYOK_PROFILE",
    "MINIMAX_DEFAULT_MODEL",
    "MINIMAX_PROVIDER_NAME",
    "OpenCodeAdapter",
    "ORCH_PROMPT_ARG_LIMIT",
    "WorkerAdapter",
    "build_attempt_env",
    "get_prompt_arg_limit",
]
