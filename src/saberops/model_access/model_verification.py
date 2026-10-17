"""Explicit first-use model check through native CLIs, outside the user's project.

Never called by startup, rendering or discovery. The owner chooses one exact
catalog model and submits a form explaining the small inference request. CLI
permissions/custom agents deny tools; only a structured completed response to a
fresh challenge proves access. Raw CLI output is neither returned nor persisted.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from saberops.process import ProcessResult, run_process
from saberops.workers.env import hermetic_env
from saberops.workers.opencode_isolation import (
    OpenCodeIsolationUnavailable,
    opencode_isolation,
)

MODEL_CHECK_TIMEOUT = 45
SUPPORTED_BACKENDS = frozenset({"opencode", "antigravity"})


def _completed(backend: str, output: str, challenge: str) -> bool:
    """Exit zero alone (including login/help screens) is insufficient evidence."""
    if len(output.encode()) > 1_000_000:
        return False
    try:
        if backend == "antigravity":
            result = json.loads(output)
            if not isinstance(result, dict):
                return False
            # Native agy 1.2.x --output-format json. This is different
            # from Claude's result envelope despite similar CLI options.
            return (result.get("status") == "SUCCESS"
                    and isinstance(result.get("response"), str)
                    and result["response"].strip() == challenge)
        text: list[str] = []
        finished = False
        for line in output.splitlines():
            event = json.loads(line)
            if not isinstance(event, dict):
                return False
            part = event.get("part")
            if event.get("type") == "error":
                return False
            if not isinstance(part, dict):
                continue
            if event.get("type") == "text" and isinstance(part.get("text"), str):
                text.append(part["text"])
            if event.get("type") == "step_finish" and part.get("reason") == "stop":
                finished = True
        return finished and "".join(text).strip() == challenge
    except (ValueError, AttributeError):
        return False


def verify_native_model(
    executable: str, backend: str, model: str,
    *, runner: Callable[..., ProcessResult] = run_process,
) -> str:
    """Return a fixed, secret-free reason for one bounded native model request."""
    if backend not in SUPPORTED_BACKENDS:
        return "PROTOCOL_UNSUPPORTED"
    challenge = "SABEROPS_ACCESS_" + uuid.uuid4().hex
    prompt = f"Reply with exactly {challenge}. Do not use any tools or inspect any files."
    with tempfile.TemporaryDirectory(prefix="saberops-model-check-") as directory:
        cwd = Path(directory)
        env: dict[str, str] = {}
        # The opencode probe runs with config/plugin isolation: the parent
        # environment is projected through the hermetic allowlist (ambient
        # configuration selectors such as OPENCODE_CONFIG are dropped), then a
        # private config/home root is created with project/ancestor discovery
        # disabled.  Refuse before launch when isolation cannot be established
        # instead of inheriting the ambient environment.  The antigravity
        # probe is untouched: it is a different vendor CLI whose login session
        # is not subject to the OpenCode discovery guarantee.
        isolation: Any = None
        base_env: dict[str, str] = {}
        if backend == "opencode":
            agent = "saberops-access-check"
            # A dedicated agent overrides ambient build/plan permissions.  The
            # check runs in an empty temporary directory (the child ``cwd``),
            # with ancestor discovery disabled by the owned isolation root; the
            # inline config below additionally denies plugins/MCP/skills.
            # The installed opencode v2.0.29 ``run --help`` documents no
            # ``--dir`` or ``--pure`` flag (HV-1 F1, fixed in Step 024/HV-2),
            # so the directory is the child ``cwd``.
            access_config: dict[str, Any] = {
                "permission": "deny", "share": "disabled",
                # steps=1 injects OpenCode's final-step summary instruction
                # before the first response, overriding the challenge prompt.
                "agent": {agent: {"mode": "primary", "steps": 2,
                                  "permission": "deny", "prompt": prompt}},
            }
            # Pin billing to the checked model's provider when the model
            # carries a provider prefix (inline config outranks project and
            # global config files).  Prefix-less models keep the previous
            # behaviour: no allowlist is added.
            provider, separator, _ = model.partition("/")
            if separator and provider:
                access_config["enabled_providers"] = [provider]
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps(access_config)
            argv = [executable, "run", prompt, "--model", model,
                    "--agent", agent, "--format", "json"]
            base_env = hermetic_env("opencode", {}, None)
            try:
                isolation = opencode_isolation(base_env)
                kwargs_env = isolation.env_for(
                    base_env, config=isolation.config(access_config)
                )
            except OpenCodeIsolationUnavailable:
                return "MODEL_CHECK_FAILED"
        else:
            agent = "saberops-access-" + uuid.uuid4().hex
            agents = cwd / ".agents" / "agents"
            agents.mkdir(parents=True)
            (agents / f"{agent}.md").write_text(
                "---\n" + f"name: {agent}\n"
                "description: A single access check with no tools\n"
                "tools: []\nmainAgent: true\nsubagent: false\nmodel: inherit\n"
                "commandExecutionPolicy: off\nmcpServers: []\nskills: []\nplugins: []\n"
                "---\n" + prompt + "\n", encoding="utf-8",
            )
            argv = [executable, "--print", prompt, "--model", model, "--agent", agent,
                    "--mode", "plan", "--sandbox", "--disable-slash-commands",
                    "--output-format", "json", "--print-timeout", f"{MODEL_CHECK_TIMEOUT}s"]
            kwargs_env = env
        kwargs: dict[str, Any] = {
            "cwd": cwd, "env": kwargs_env, "timeout": MODEL_CHECK_TIMEOUT,
            # Never inherit the parent environment: only the child environment
            # built here reaches the probe.
            "inherit_env": False,
            # Streaming activates the existing process-group timeout/cleanup.
            "on_stdout": lambda _line: None, "on_stderr": lambda _line: None,
            "input_text": "",
        }
        try:
            with isolation if isolation is not None else nullcontext():
                result = runner(argv, **kwargs)
        except (OSError, ValueError):
            return "MODEL_CHECK_FAILED"
    if result.timed_out:
        return "MODEL_CHECK_TIMEOUT"
    if not result.passed:
        return "MODEL_CHECK_FAILED"
    return "READY" if _completed(backend, result.stdout, challenge) else "MODEL_CHECK_UNCONFIRMED"
