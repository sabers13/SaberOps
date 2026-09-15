"""Ox Alpha Conversational Manager Service and OpenCode client.

Provides conversational management for the orchestrator, enabling natural-language
interaction, run inspection, retry/dispatch operations, and diagnostics.

Note: orch_accept and orch_cleanup are intentionally restricted from automated execution
in this slice to uphold fail-closed safety and require explicit human confirmation in the
browser UI. Direct wrapper execution is reserved for future controlled policies.
"""
# ruff: noqa: E501

from __future__ import annotations

import json
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from orchestrator_mvp.config import resolve_run_config
from orchestrator_mvp.git import GitManager
from orchestrator_mvp.process import ProcessResult, run_process
from orchestrator_mvp.projects import ProjectRegistry
from orchestrator_mvp.runtime import ProjectRuntimeManager
from orchestrator_mvp.service import OrchestratorService


class ManagerClient(Protocol):
    """Protocol for manager model client backends."""

    def send(self, prompt: str) -> str:
        """Send a prompt to the manager backend and return its text response."""
        ...


class OpenCodeManagerClient:
    """Non-interactive OpenCode client executing against an isolated session directory."""

    def __init__(
        self,
        opencode_bin: str = "opencode",
        model: str = "opencode-go/ox-alpha-free",
        agent: str = "orchestrator",
        timeout_seconds: float = 240.0,
        session_dir: Path | str | None = None,
        runner: Callable[..., ProcessResult] = run_process,
    ) -> None:
        self.opencode_bin = opencode_bin
        self.model = model
        self.agent = agent
        self.timeout_seconds = timeout_seconds
        self.runner = runner

        if session_dir is None:
            xdg_state_home = os.environ.get("XDG_STATE_HOME")
            if xdg_state_home and xdg_state_home.strip():
                base_dir = Path(xdg_state_home).expanduser()
            else:
                base_dir = Path.home() / ".local" / "state"
            self.session_dir = base_dir / "orchestrator-mvp" / "manager-session"
        else:
            self.session_dir = Path(session_dir).resolve()
        self.session_dir.mkdir(parents=True, exist_ok=True)

    def send(self, prompt: str) -> str:
        """Execute headless opencode run in the isolated session directory."""
        cmd = [
            self.opencode_bin,
            "run",
            prompt,
            "--model",
            self.model,
            "--agent",
            self.agent,
            "--pure",
        ]
        res = self.runner(cmd, cwd=self.session_dir, timeout=self.timeout_seconds)
        if not res.passed:
            err = res.stderr.strip() or res.stdout.strip()
            raise RuntimeError(
                f"OpenCode manager process failed (exit code {res.exit_code}): {err}"
            )
        return res.stdout


@dataclass
class ManagerReply:
    """Outcome of manager message processing."""

    reply_text: str
    tool_calls_executed: list[dict[str, Any]] = field(default_factory=list)


def _extract_tool_calls(text: str) -> list[dict[str, Any]]:
    """Extract strict fenced JSON tool calls from manager model output."""
    tool_calls: list[dict[str, Any]] = []

    # Look for ```json ... ``` blocks
    code_blocks = re.findall(r"```(?:json)?\s*(\{[\s\S]*?\})\s*```", text, re.IGNORECASE)
    for block in code_blocks:
        try:
            data = json.loads(block)
            if isinstance(data, dict) and "tool" in data:
                tool_calls.append(data)
        except Exception:
            pass

    # If no code blocks found, check for raw JSON objects
    if not tool_calls:
        matches = re.finditer(r"\{[^{}]*\"tool\"\s*:\s*\"[^\"]+\"[^{}]*\}", text)
        for m in matches:
            try:
                data = json.loads(m.group(0))
                if isinstance(data, dict) and "tool" in data:
                    tool_calls.append(data)
            except Exception:
                pass

    return tool_calls


class ManagerService:
    """Manager service orchestrating multi-round-trip model conversation and tool dispatch."""

    def __init__(
        self,
        client: ManagerClient,
        projects: ProjectRegistry,
        service: OrchestratorService | None = None,
        supervisor: Any | None = None,
        runtimes: ProjectRuntimeManager | None = None,
    ) -> None:
        if service is None and runtimes is None:
            raise ValueError("ManagerService requires either an explicit service or runtimes")
        self.client = client
        self.projects = projects
        self.runtimes = runtimes
        self._service = service
        self._supervisor = supervisor

    @property
    def service(self) -> OrchestratorService:
        """Service for the currently selected project (navigation scope only)."""
        if self._service is not None:
            return self._service
        assert self.runtimes is not None
        return self.runtimes.for_active(self.projects.active()).service

    @property
    def supervisor(self) -> Any | None:
        """Supervisor for the currently selected project."""
        if self._supervisor is not None:
            return self._supervisor
        if self.runtimes is None:
            return None
        return self.runtimes.for_active(self.projects.active()).supervisor

    def _service_for_run(self, run_id: str) -> OrchestratorService | None:
        """Resolve an existing run to its own project's service.

        Run-scoped tools must act on the run's repository, never on whichever
        project the conversation happens to have selected.
        """
        if self.runtimes is None:
            return self._service
        runtime = self.runtimes.for_run(run_id)
        return runtime.service if runtime is not None else None

    def _supervisor_for_run(self, run_id: str) -> Any | None:
        """Resolve an existing run to its own project's supervisor."""
        if self.runtimes is None:
            return self._supervisor
        runtime = self.runtimes.for_run(run_id)
        return runtime.supervisor if runtime is not None else None

    def _build_context(self) -> str:
        active_repo = self.projects.active()
        repo_str = str(active_repo) if active_repo else "None"

        # Recent runs are scoped to the selected project so the manager can
        # never propose acting on an unrelated repository's run.
        runs = self.service.list_runs(limit=5)
        run_summaries: list[str] = []
        for r in runs:
            run_summaries.append(
                f"- {r.id}: status={r.status.value}, tier={r.tier.value}, task='{r.task[:50]}'"
            )
        runs_block = "\n".join(run_summaries) if run_summaries else "No recent runs."

        return (
            "You are the Ox Alpha Orchestrator Manager, an AI engineering manager helping "
            "the user inspect, run, diagnose, and coordinate autonomous coding tasks.\n\n"
            f"Active target repository: {repo_str}\n"
            f"Recent runs:\n{runs_block}\n\n"
            "Available tools (invoke using a strict JSON block like ```json\n"
            '{"tool": "TOOL_NAME", "args": {...}}\n```):\n'
            "- orch_run: start a new run on active repository. "
            'args: {"task": str, "routing": "auto" (default), '
            '"manual_tier": "T1"|"T2"|"T3" (optional), "gate_command": str, '
            '"provider_override": str (optional, e.g. "opencode", "codex"), '
            '"model_override": str (optional, e.g. "opencode-go/deepseek-v4-pro", "gpt-5.6-terra")}\n'  # noqa: E501
            "Canonical model mapping: V4 Pro -> opencode-go/deepseek-v4-pro (provider opencode), "
            "V4 Flash -> opencode-go/deepseek-v4-flash (provider opencode), "
            "Terra -> gpt-5.6-terra (provider codex), Sol -> gpt-5.6-sol (provider codex). "
            "Emit exact provider_override/model_override instead of guessing tiers.\n"
            '- orch_status: get details of a specific run. args: {"run_id": str}\n'
            '- orch_plan: persisted plan projection. args: {"run_id": str}\n'
            '- orch_supervision: worker health/manager state. args: {"run_id": str}\n'
            '- orch_handoff: latest durable handoff. args: {"run_id": str, "capsule": bool}\n'
            '- orch_reconcile: explicit orphan reconciliation. args: {"run_id": str, "dry_run": bool}\n'
            '- orch_wait: block on engine events, never ask owner for status. args: {"run_id": str, "after": int, "timeout": number}\n'
            '- orch_list: list recent runs. args: {"limit": int (optional)}\n'
            '- orch_events: get lifecycle events. args: {"run_id": str, "after_id": int}\n'
            '- orch_review: request review for completed run. args: {"run_id": str}\n'
            "- orch_models: list available execution tiers and models.\n"
            "- orch_quota: list configured quota states.\n"
            "- orch_accept: request acceptance (NOTE: directs user to Web UI).\n"
            "- orch_cleanup: request cleanup (NOTE: directs user to Web UI).\n\n"
            "If no tool call is needed, provide your natural language answer directly."
        )

    def _execute_tool(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Execute a single typed tool call against OrchestratorService."""
        if tool == "orch_run":
            active_repo = self.projects.active()
            if not active_repo:
                return {"error": "No active project selected in project registry"}
            task = args.get("task", "")
            if not task:
                return {"error": "task parameter is required for orch_run"}
            legacy_tier = args.get("tier")
            routing = str(args.get("routing", "manual" if legacy_tier else "auto"))
            manual_tier = args.get("manual_tier", legacy_tier)
            training_allowed = bool(args.get("training_allowed", True))
            config = resolve_run_config(
                task=task,
                target_repo=active_repo,
                routing_mode=routing,
                manual_tier=manual_tier,
                max_auto_tier=args.get("max_auto_tier", "T3"),
                training_allowed=training_allowed,
                gate_command=args.get("gate_command", "make gate"),
                provider_override=args.get("provider_override"),
                model_override=args.get("model_override"),
                max_attempts=int(args.get("max_attempts", 2)),
                worker_timeout=args.get("worker_timeout"),
                gate_timeout=args.get("gate_timeout"),
                review=bool(args.get("review", False)),
                review_timeout=args.get("review_timeout"),
            )
            if self.supervisor is not None:
                res = self.supervisor.start_supervised_run(config=config)
            else:
                res = self.service.start_run(
                    task=config.task,
                    repo_path=active_repo,
                    config=config,
                )
            return {"run_id": res.run_id, "status": "PENDING", "repo": str(active_repo)}

        elif tool == "orch_status":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_status"}
            svc = self._service_for_run(str(run_id))
            if svc is None:
                return {"error": f"Run '{run_id}' not found in any known project"}
            run = svc.get_run(run_id)
            if not run:
                return {"error": f"Run '{run_id}' not found"}
            attempts = svc.db.get_attempts_for_run(run_id)
            reviews = svc.db.get_reviews_for_run(run_id)
            return {
                "run_id": run.id,
                "task": run.task,
                "status": run.status.value,
                "tier": run.tier.value,
                "target_repo": run.target_repo,
                "base_commit": run.base_commit,
                "created_at": run.created_at,
                "completed_at": run.completed_at,
                "summary": run.result_summary,
                "attempts": [
                    {
                        "number": a.attempt_number,
                        "provider": a.provider,
                        "model": a.model,
                        "status": a.status.value,
                        "commit_sha": a.commit_sha,
                        "error": a.worker_error,
                    }
                    for a in attempts
                ],
                "reviews": [
                    {
                        "provider": r.provider,
                        "model": r.model,
                        "verdict": r.verdict.value,
                        "summary": r.summary,
                    }
                    for r in reviews
                ],
            }

        elif tool == "orch_list":
            limit = int(args.get("limit", 10))
            runs = self.service.list_runs(limit=limit)
            return {
                "runs": [
                    {
                        "id": r.id,
                        "task": r.task,
                        "status": r.status.value,
                        "tier": r.tier.value,
                        "created_at": r.created_at,
                        "completed_at": r.completed_at,
                        "summary": r.result_summary,
                    }
                    for r in runs
                ]
            }

        elif tool == "orch_events":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_events"}
            svc = self._service_for_run(str(run_id))
            if svc is None:
                return {"error": f"Run '{run_id}' not found in any known project"}
            after_id = int(args.get("after_id", 0))
            events = svc.get_events(run_id=run_id, after_id=after_id)
            return {
                "events": [
                    {
                        "id": ev.id,
                        "attempt_id": ev.attempt_id,
                        "event_type": ev.event_type,
                        "payload": ev.payload,
                        "created_at": ev.created_at,
                    }
                    for ev in events
                ]
            }

        elif tool == "orch_plan":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_plan"}
            svc = self._service_for_run(str(run_id))
            if svc is None:
                return {"error": f"Run '{run_id}' not found in any known project"}
            plan = svc.db.get_run_plan(run_id)
            return {
                "plan": None
                if plan is None
                else {"id": plan.id, "status": plan.status.value, "version": plan.version},
                "steps": [
                    {
                        "id": step.id,
                        "title": step.title,
                        "status": step.status.value,
                        "dependencies": list(step.dependency_ids),
                    }
                    for step in svc.db.get_plan_steps(run_id)
                ],
            }

        elif tool == "orch_supervision":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_supervision"}
            svc = self._service_for_run(str(run_id))
            if svc is None:
                return {"error": f"Run '{run_id}' not found in any known project"}
            evidence = svc.db.get_supervision(run_id)
            return {
                "supervision": None
                if evidence is None
                else {
                    "pid": evidence.worker_pid,
                    "provider": evidence.provider,
                    "model": evidence.model,
                    "health": evidence.health_state.value,
                    "health_reason": evidence.health_reason,
                    "last_activity_at": evidence.last_activity_at,
                    "manager_state": evidence.manager_state.value,
                    "wake_condition": evidence.wake_condition,
                }
            }

        elif tool == "orch_handoff":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_handoff"}
            svc = self._service_for_run(str(run_id))
            if svc is None:
                return {"error": f"Run '{run_id}' not found in any known project"}
            kind = "handoff_resume_capsule" if bool(args.get("capsule", False)) else "handoff_full"
            handoff = svc.db.get_latest_handoff(run_id, kind)
            return {
                "handoff": None
                if handoff is None
                else {"kind": handoff.kind, "version": handoff.version, "content": handoff.content}
            }

        elif tool == "orch_reconcile":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_reconcile"}
            run_supervisor = self._supervisor_for_run(str(run_id))
            if run_supervisor is None:
                return {"error": "durable supervisor is unavailable"}
            reconciled = run_supervisor.reconcile(
                str(run_id), dry_run=bool(args.get("dry_run", True))
            )
            return dict(reconciled)

        elif tool == "orch_wait":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_wait"}
            svc = self._service_for_run(str(run_id))
            if svc is None:
                return {"error": f"Run '{run_id}' not found in any known project"}
            after = int(args.get("after", 0))
            # The manager consumes a durable event edge; the CLI ``orch wait``
            # is the actual blocking bridge used by external managers.
            events = svc.get_events(run_id, after_id=after, limit=1)
            return {
                "events": [
                    {"id": event.id, "type": event.event_type, "payload": event.payload}
                    for event in events
                ],
                "watch_contract": "orch wait <run-id> --after N",
            }

        elif tool == "orch_review":
            run_id = args.get("run_id")
            if not run_id:
                return {"error": "run_id parameter is required for orch_review"}
            svc = self._service_for_run(str(run_id))
            if svc is None:
                return {"error": f"Run '{run_id}' not found in any known project"}
            rev = svc.review_run(run_id=run_id)
            return {
                "review_id": rev.id,
                "provider": rev.provider,
                "model": rev.model,
                "verdict": rev.verdict.value,
                "summary": rev.summary,
                "details": rev.details,
            }

        elif tool == "orch_models":
            models_info = self.service.list_models()
            snap = models_info.get("routing_snapshot")
            src = snap.source if snap else "unknown"
            src_path = snap.source_path if snap else None
            ch = snap.content_hash if snap else None
            return {
                "peak": models_info["peak"],
                "source": src,
                "source_path": src_path,
                "content_hash": ch,
                "tier_1": [f"[{c.provider}] {c.model}" for c in models_info["tier_1_chain"]],
                "tier_2": [
                    f"[{c.provider}] {c.model}"
                    for c in models_info["tier_2_training_allowed_chain"]
                ],
                "tier_3": [
                    f"[{c.provider}] {c.model}" for c in models_info["tier_3_off_peak_chain"]
                ],
            }

        elif tool == "orch_quota":
            quotas = self.service.list_quota()
            return {
                "quotas": [
                    {
                        "provider": q.provider,
                        "pool": q.pool,
                        "remaining_percent": q.remaining_percent,
                        "routing_state": q.routing_state.value,
                        "reset_at": q.reset_at,
                    }
                    for q in quotas
                ]
            }

        elif tool == "orch_accept":
            run_id = args.get("run_id", "<run_id>")
            return {
                "error": (
                    f"Acceptance requires explicit typed confirmation in Web UI at /runs/{run_id}. "
                    "The manager cannot automatically accept changes."
                )
            }

        elif tool == "orch_cleanup":
            run_id = args.get("run_id", "<run_id>")
            return {
                "error": (
                    f"Cleanup requires explicit confirmation in the Web UI at /runs/{run_id}. "
                    "The manager cannot automatically clean up worktrees."
                )
            }

        else:
            return {"error": f"Unknown tool '{tool}'"}

    def handle_message(self, user_text: str) -> ManagerReply:
        """Process a user message through a bounded multi-round-trip model loop."""
        active_repo = self.projects.active()
        head_before: str | None = None
        clean_before: bool = True

        if active_repo and GitManager.is_git_repo(active_repo):
            try:
                head_before = GitManager.get_head_commit(active_repo)
                clean_before = GitManager.is_clean(active_repo)
            except Exception:
                pass

        conversation_history: list[str] = [
            self._build_context(),
            f"User request: {user_text}",
        ]
        executed_tool_calls: list[dict[str, Any]] = []
        final_reply = ""

        for _ in range(3):
            # Check integrity invariant before model call
            if active_repo and GitManager.is_git_repo(active_repo):
                current_head = GitManager.get_head_commit(active_repo)
                current_clean = GitManager.is_clean(active_repo)
                if current_head != head_before or current_clean != clean_before:
                    return ManagerReply(
                        reply_text=(
                            "Manager integrity violation: active target repository was mutated "
                            "during manager execution. Further actions refused."
                        ),
                        tool_calls_executed=executed_tool_calls,
                    )

            prompt = "\n\n".join(conversation_history)
            model_response = self.client.send(prompt)
            tool_calls = _extract_tool_calls(model_response)

            if not tool_calls:
                final_reply = model_response.strip()
                break

            conversation_history.append(f"Assistant: {model_response}")

            tool_results: list[str] = []
            for tc in tool_calls:
                tool_name = tc.get("tool", "")
                tool_args = tc.get("args", {})
                res = self._execute_tool(tool_name, tool_args)
                executed_tool_calls.append({"tool": tool_name, "args": tool_args, "result": res})
                tool_results.append(f"Tool {tool_name} result:\n{json.dumps(res, indent=2)}")

            conversation_history.append("\n\n".join(tool_results))
        else:
            final_reply = "Reached maximum tool execution limit (3 rounds). Latest state returned."

        # Final integrity check after loop
        if active_repo and GitManager.is_git_repo(active_repo):
            current_head = GitManager.get_head_commit(active_repo)
            current_clean = GitManager.is_clean(active_repo)
            if current_head != head_before or current_clean != clean_before:
                return ManagerReply(
                    reply_text=(
                        "Manager integrity violation: active target repository was mutated "
                        "during manager execution. Further actions refused."
                    ),
                    tool_calls_executed=executed_tool_calls,
                )

        return ManagerReply(reply_text=final_reply, tool_calls_executed=executed_tool_calls)
