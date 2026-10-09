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
from pathlib import Path
from typing import Any

from saberops.process import ProcessResult, run_process

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
        if backend == "opencode":
            agent = "saberops-access-check"
            # A dedicated agent overrides ambient build/plan permissions. Pure
            # disables plugins. No user's config or credential file is read.
            env["OPENCODE_CONFIG_CONTENT"] = json.dumps({
                "permission": "deny", "share": "disabled",
                # steps=1 injects OpenCode's final-step summary instruction
                # before the first response, overriding the challenge prompt.
                "agent": {agent: {"mode": "primary", "steps": 2,
                                  "permission": "deny", "prompt": prompt}},
            })
            argv = [executable, "run", prompt, "--model", model, "--dir", directory,
                    "--pure", "--agent", agent, "--format", "json"]
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
        kwargs: dict[str, Any] = {
            "cwd": cwd, "env": env, "timeout": MODEL_CHECK_TIMEOUT,
            # Streaming activates the existing process-group timeout/cleanup.
            "on_stdout": lambda _line: None, "on_stderr": lambda _line: None,
            "input_text": "",
        }
        try:
            result = runner(argv, **kwargs)
        except (OSError, ValueError):
            return "MODEL_CHECK_FAILED"
    if result.timed_out:
        return "MODEL_CHECK_TIMEOUT"
    if not result.passed:
        return "MODEL_CHECK_FAILED"
    return "READY" if _completed(backend, result.stdout, challenge) else "MODEL_CHECK_UNCONFIRMED"
