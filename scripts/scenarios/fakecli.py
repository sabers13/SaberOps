#!/usr/bin/env python3
"""Scriptable stand-in for the codex / claude CLIs (argv[0] basename picks the dialect).

Scenario is chosen by a tag in the task text:  [fake:fail] [fake:noop] [fake:quota]
[fake:malformed] [fake:hang] [fake:crashmid] [fake:slow=SECONDS] [fake:review-clean]
Environment: FAKE_LOGGED_OUT=1 makes the auth probes report a logged-out account.
Every invocation is appended to $FAKE_LOG (default /tmp/fakecli.log) as JSON.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path

name = Path(sys.argv[0]).name
argv = sys.argv[1:]
logged_out = (
    os.environ.get("FAKE_LOGGED_OUT") == "1"
    or Path(os.environ.get("FAKE_FLAGS", "/tmp/fake-flags")).joinpath("logged-out").exists()
)


def log(**kw: object) -> None:
    with open(os.environ.get("FAKE_LOG", "/tmp/fakecli.log"), "a") as fh:
        fh.write(json.dumps({"t": time.time(), "cli": name, "cwd": os.getcwd(), **kw}) + "\n")


def emit(obj: object) -> None:
    print(json.dumps(obj), flush=True)


def tags(task: str) -> dict[str, str]:
    return {
        m.group(1): (m.group(2) or "")
        for m in re.finditer(r"\[fake:([a-z-]+)(?:=([^\]]*))?\]", task)
    }


def work(task: str, model: str, dialect: str) -> int:
    t = tags(task)
    role = (
        "orchestrator"
        if "conversational engineering orchestrator" in task
        else "reviewer"
        if ("VERDICT" in task and "findings" in task)
        else "worker"
    )
    log(
        event="run",
        role=role,
        model=model,
        tags=t,
        task_bytes=len(task),
        work_plan_requested=("work_plan" in task),
    )
    msg = "Fake worker finished."
    if role != "worker":  # failure-injection tags apply to workers only
        t = {
            k: v
            for k, v in t.items()
            if k not in ("fail", "quota", "malformed", "hang", "crashmid", "slow", "noop")
        }
    if "quota" in t:
        print("Error: usage limit reached. Try again later.", file=sys.stderr)
        return 1
    if "fail" in t:
        print("fake provider: simulated runtime failure", file=sys.stderr)
        return 1
    if "malformed" in t:
        print("{not json\n<<<garbage>>>", flush=True)
        return 0
    if dialect == "claude":
        emit({"type": "system", "subtype": "init", "model": model})
    else:
        emit({"type": "thread.started", "thread_id": "fake-thread"})
        emit({"type": "turn.started"})
    if "hang" in t:
        time.sleep(3600)
    for _ in range(int(float(t.get("slow", 0)))):
        time.sleep(1)
        emit(
            {"type": "item.started", "item": {"type": "reasoning"}}
            if dialect == "codex"
            else {"type": "stream_event", "event": {"type": "ping"}}
        )
    if "crashmid" in t:
        os._exit(137)
    read_only = "read-only" in argv or "plan" in argv
    if not (read_only or "noop" in t or "review-clean" in t):
        Path(f"fake_{name}_output.txt").write_text(f"change from {name}\n{task[:80]!r}\n")

    if "conversational engineering orchestrator" in task:  # orchestrator chat turn
        evidence = "Explain this newly finished run" in task
        latest = (
            task.rsplit("LATEST USER MESSAGE", 1)[-1] if "LATEST USER MESSAGE" in task else task
        )
        wants_work = (
            (
                "work" in t
                or re.search(r"\b(add|fix|implement|change|build|refactor)\b", latest.lower())
            )
            and "respond" not in t
            and not evidence
        )
        payload = {
            "reply": "On it." if wants_work else "Here is what happened.",
            "action": "work" if wants_work else "respond",
        }
        if wants_work and "work_plan" in task:
            payload["work_plan"] = [
                {
                    "title": "Add subtract",
                    "goal": "Add subtract() to calc.py",
                    "acceptance_criteria": ["subtract(3,1)==2"],
                    "relevant_paths": ["calc.py"],
                    "dependency_indexes": [],
                },
                {
                    "title": "Add docs",
                    "goal": "Add docs/notes.md",
                    "acceptance_criteria": ["file exists"],
                    "relevant_paths": ["docs/**"],
                    "dependency_indexes": [],
                },
            ]
        msg = json.dumps(payload)
    elif "VERDICT" in task and "findings" in task:  # reviewer
        if "review-block" in t:
            f = {
                "id": "F1",
                "classification": "B2",
                "title": "Fake defect",
                "severity": "high",
                "contract_reference": None,
                "invariant_or_requirement": "subtract must handle ints",
                "code_location": "calc.py:1",
                "failure_scenario": "subtract(1,2) returns 1",
                "evidence": "fake evidence",
                "minimum_correction": "return a-b",
                "repair_scope": "LOCAL_MECHANICAL",
            }
            msg = (
                "VERDICT: BLOCK\nFound a defect.\n```json\n"
                + json.dumps({"findings": [f]})
                + "\n```"
            )
        else:
            msg = 'VERDICT: PASS\nLooks fine.\n```json\n{"findings": []}\n```'
    if dialect == "claude":
        emit({"type": "assistant", "message": {"content": [{"type": "text", "text": msg}]}})
        emit(
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": msg,
                "usage": {"input_tokens": 1200, "output_tokens": 340},
            }
        )
    else:
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": msg}})
        emit(
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 1200,
                    "cached_input_tokens": 400,
                    "output_tokens": 340,
                    "reasoning_output_tokens": 50,
                },
            }
        )
    return 0


def main() -> int:
    if name == "codex":
        if argv[:1] == ["doctor"] or "doctor" in argv:
            st = "error" if logged_out else "ok"
            emit(
                {
                    "schemaVersion": 1,
                    "overallStatus": st,
                    "checks": {"auth.credentials": {"status": st}},
                }
            )
            return 0
        if argv[:2] == ["debug", "models"]:
            emit({"models": [{"slug": "fake-codex-1", "id": "fake-codex-1"}]})
            return 0
        if "exec" in argv:
            i = argv.index("exec")
            rest = argv[i + 1 :]
            if rest and rest[0] == "resume":
                rest = rest[2:]
            task = sys.stdin.read() if rest and rest[0] == "-" else (rest[0] if rest else "")
            model = argv[argv.index("--model") + 1] if "--model" in argv else "fake-codex-1"
            return work(task, model, "codex")
        log(event="unhandled", argv=argv)
        return 2
    if name == "claude":
        if "auth" in argv and "status" in argv:
            emit(
                {"loggedIn": not logged_out, "authMethod": "claude.ai", "apiProvider": "firstParty"}
            )
            return 0
        if "--print" in argv:
            if "--tools" in argv and argv[argv.index("--tools") + 1] == "":
                return 0  # model-catalog probe: no catalog, caller falls back
            task = sys.stdin.read()
            model = argv[argv.index("--model") + 1] if "--model" in argv else "fake-claude-1"
            return work(task, model, "claude")
        log(event="unhandled", argv=argv)
        return 2
    log(event="unknown-cli", argv=argv)
    return 2


sys.exit(main())
