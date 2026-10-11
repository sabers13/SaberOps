"""Worker adapters package."""

from saberops.process import ORCH_PROMPT_ARG_LIMIT, get_prompt_arg_limit
from saberops.workers.antigravity import AntigravityAdapter
from saberops.workers.base import WorkerAdapter
from saberops.workers.claude import ClaudeCodeAdapter
from saberops.workers.cline import ClineAdapter
from saberops.workers.codex import CodexAdapter
from saberops.workers.opencode import OpenCodeAdapter
from saberops.workers.registry import AdapterRegistry

__all__ = [
    "AdapterRegistry",
    "AntigravityAdapter",
    "ClineAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "OpenCodeAdapter",
    "ORCH_PROMPT_ARG_LIMIT",
    "WorkerAdapter",
    "get_prompt_arg_limit",
]
