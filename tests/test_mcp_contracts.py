"""MCP contract/adversarial tests: D7 tools and the never-exposed invariants.

Covers the Step 014 authority boundary from the outside: the never-exposed
operations refuse with a distinct JSON-RPC code, mutating tools require the
single-writer lease, job submission accepts requirements but never
provider/model/billing fields, ``answer_question`` enforces the D6 authority
split, and ``propose_config_change`` validates without applying anything.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from saberops.config import (
    ConfigLedger,
    approve_config_proposal,
    current_workflow_settings,
    propose_config_change,
)
from saberops.mcp.evidence import EvidenceRoots
from saberops.mcp.lease import OrchestratorLease
from saberops.mcp.ledger import EventLedger
from saberops.mcp.protocol import McpServer
from saberops.mcp.tools import (
    FORBIDDEN_OPERATIONS,
    MUTATING_TOOLS,
    READ_TOOLS,
    ServerContext,
)
from saberops.questions import (
    AskRole,
    DecisionClass,
    Escalation,
    OwnerQuestionQueue,
    QuestionRequest,
)


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "mcp@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "MCP"], cwd=path, check=True)
    (path / "README.md").write_text("# repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(
        ["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True
    )
    return path


def _context(tmp_path: Path, *, lease_pid: int | None = None) -> ServerContext:
    orch = tmp_path / ".orch"
    orch.mkdir(parents=True, exist_ok=True)
    return ServerContext(
        db_path=tmp_path / "mcp.db",
        questions_store=tmp_path / "questions.json",
        config_ledger_path=tmp_path / "config.jsonl",
        evidence=EvidenceRoots(roots=(orch,)),
        ledger=EventLedger(tmp_path / "events.jsonl"),
        lease=OrchestratorLease(
            tmp_path / "lease.json",
            holder="mcp-test",
            pid=os.getpid() if lease_pid is None else lease_pid,
        ),
    )


def _server(tmp_path: Path, *, read_only: bool = False) -> McpServer:
    if read_only:
        OrchestratorLease(tmp_path / "lease.json", holder="primary", pid=1).acquire()
        server = McpServer(_context(tmp_path, lease_pid=os.getpid()))
        assert server.read_only is True
    else:
        server = McpServer(_context(tmp_path))
    server.handle({"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": {}})
    server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    return server


def _call(server: McpServer, name: str, arguments: Mapping[str, object]) -> dict[str, Any]:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": dict(arguments)},
        }
    )
    assert response is not None
    return response


def _payload(server: McpServer, name: str, arguments: Mapping[str, object]) -> dict[str, Any]:
    response = _call(server, name, arguments)
    assert "result" in response, response
    result: dict[str, Any] = response["result"]
    assert result["isError"] is False, result
    structured: dict[str, Any] = result["structuredContent"]
    return structured


def _ask(
    store: Path,
    *,
    key: str = "q-1",
    decision_class: DecisionClass = DecisionClass.ROUTINE_QUESTION,
    raised_by_role: AskRole = AskRole.WORKER,
    raised_by: str = "worker-1",
) -> str:
    queue = OwnerQuestionQueue(store)
    question = queue.ask(
        QuestionRequest(
            key=key,
            subject="what should happen",
            kind="routine",
            decision_class=decision_class,
            raised_by_role=raised_by_role,
            raised_by=raised_by,
            escalation=Escalation(
                requirement="req",
                proposed_expansion="expand",
                smallest_compliant="smallest",
                consequences="consequences",
                recommendation="recommendation",
            ),
            options=("yes", "no"),
        )
    )
    return question.question_id


def _wave(*, lane_a: str = "src/a.py", lane_b: str = "src/b.py") -> dict[str, Any]:
    return {
        "wave_id": "w-1",
        "base_sha": "a" * 40,
        "target_branch": "main",
        "merge_order": ["l1", "l2"],
        "named_glue": [],
        "budgets": {"g3_runs": 1, "fix_rounds": 2},
        "lanes": [
            {
                "lane_id": "l1",
                "task_id": "t1",
                "prompt": "lane one",
                "touches": [lane_a],
                "worker_route": "worker-route",
                "reviewer_route": "reviewer-route",
                "effort": "medium",
            },
            {
                "lane_id": "l2",
                "task_id": "t2",
                "prompt": "lane two",
                "touches": [lane_b],
                "worker_route": "worker-route",
                "reviewer_route": "reviewer-route",
                "effort": "medium",
            },
        ],
    }


# --------------------------------------------------------------------------- #
# Never-exposed invariants
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("operation", sorted(FORBIDDEN_OPERATIONS))
def test_every_never_exposed_operation_is_refused(tmp_path: Path, operation: str) -> None:
    """Covers: ADR-0004 D7 'Never exposed' + REHEARSAL §3 never-verbs."""
    server = _server(tmp_path)
    response = _call(server, operation, {})
    assert "error" in response, response
    assert response["error"]["code"] == -32010
    assert operation in response["error"]["message"]


def test_never_exposed_operations_are_absent_from_tools_list(tmp_path: Path) -> None:
    """Covers: ADR-0004 D7 (no tool can reach the owner-only authority)."""
    server = _server(tmp_path)
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
    assert response is not None
    names = {tool["name"] for tool in response["result"]["tools"]}
    assert names.isdisjoint(set(FORBIDDEN_OPERATIONS))


def test_owner_only_authorities_cannot_be_reached_by_alias(tmp_path: Path) -> None:
    """Covers: ADR-0004 D7 (synonyms cannot smuggle authority)."""
    server = _server(tmp_path)
    for alias in ("approve_proposal", "git_push", "move_main", "gate_full", "read_token"):
        response = _call(server, alias, {})
        assert response["error"]["code"] == -32010, alias


@pytest.mark.parametrize("tool", sorted(MUTATING_TOOLS))
def test_mutating_tools_require_the_writer_lease(tmp_path: Path, tool: str) -> None:
    """Covers: prompt section 'read-only second client' (mutating refusal)."""
    server = _server(tmp_path, read_only=True)
    response = _call(server, tool, {})
    assert response["error"]["code"] == -32020, response


def test_read_tools_succeed_for_a_read_only_client(tmp_path: Path) -> None:
    """Covers: prompt section 'read-only second client' (reads keep working)."""
    server = _server(tmp_path, read_only=True)
    arguments: dict[str, dict[str, object]] = {
        "get_job": {"job_id": "unknown"},
        "list_jobs": {},
        "capacity": {},
        "wave_status": {"wave_id": "unknown"},
        "read_evidence": {"path": "missing.md"},
        "list_questions": {},
        "list_proposals": {},
    }
    for tool in sorted(READ_TOOLS):
        response = _call(server, tool, arguments[tool])
        assert "error" not in response, (tool, response)


# --------------------------------------------------------------------------- #
# submit_job / get_job / list_jobs / cancel_job / capacity
# --------------------------------------------------------------------------- #


def test_submit_job_returns_job_id_and_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section 'submit_job' (returns job_id and initial state)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = _git_repo(tmp_path / "repo")
    server = _server(tmp_path)
    payload = _payload(server, "submit_job", {"repo": str(repo), "goal": "ship the thing"})
    assert payload["job_id"].startswith("job-") or payload["job_id"]
    assert payload["state"] in {"QUEUED", "FAILED"}
    assert payload["job"]["goal"] == "ship the thing"
    assert payload["created"] is True


@pytest.mark.parametrize("field", ["provider", "model", "tier", "billing", "route", "quota"])
def test_submit_job_refuses_requirement_denylisted_fields(
    tmp_path: Path, field: str
) -> None:
    """Covers: ADR-0001 invariant 3 (callers state requirements, not routes)."""
    server = _server(tmp_path)
    response = _call(server, "submit_job", {"repo": "/tmp", "goal": "g", field: "x"})
    assert "result" in response
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == -32602


def test_submit_job_refuses_a_nested_denylisted_field(tmp_path: Path) -> None:
    """Covers: ADR-0001 invariant 3 (nested requirements are checked too)."""
    server = _server(tmp_path)
    response = _call(
        server,
        "submit_job",
        {"repo": "/tmp", "goal": "g", "requirements": {"provider": "openai"}},
    )
    assert response["result"]["isError"] is True


def test_submit_job_is_idempotent_with_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: ADR-0004 invariant 13 (idempotent tools)."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = _git_repo(tmp_path / "repo")
    server = _server(tmp_path)
    args = {"repo": str(repo), "goal": "same goal", "idempotency_key": "k-1"}
    first = _payload(server, "submit_job", args)
    second = _payload(server, "submit_job", args)
    assert first["job_id"] == second["job_id"]
    assert second["created"] is False
    # The ledger recorded the logical submission exactly once.
    assert server.context.ledger.replay().counts.get("job_submitted") == 1


def test_get_job_and_list_jobs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Covers: prompt section 'get_job / list_jobs'."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = _git_repo(tmp_path / "repo")
    server = _server(tmp_path)
    submitted = _payload(server, "submit_job", {"repo": str(repo), "goal": "a job"})
    job_id = submitted["job_id"]
    got = _payload(server, "get_job", {"job_id": job_id})
    assert got["job"]["job_id"] == job_id
    listed = _payload(server, "list_jobs", {"limit": 10})
    assert any(job["job_id"] == job_id for job in listed["jobs"])


def test_get_job_refuses_an_unknown_job(tmp_path: Path) -> None:
    """Covers: prompt section 'get_job' (not found is explicit)."""
    server = _server(tmp_path)
    response = _call(server, "get_job", {"job_id": "job-nope"})
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == -32040


def test_cancel_job_marks_a_queued_job_cancelled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Covers: prompt section 'cancel_job'."""
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    repo = _git_repo(tmp_path / "repo")
    server = _server(tmp_path)
    job_id = _payload(server, "submit_job", {"repo": str(repo), "goal": "cancel me"})["job_id"]
    payload = _payload(server, "cancel_job", {"job_id": job_id, "reason": "owner change"})
    assert payload["cancelled"] is True
    assert payload["state"] == "CANCELLED"


def test_capacity_returns_the_read_model(tmp_path: Path) -> None:
    """Covers: prompt section 'capacity (pools, spare/hour, reset deadlines)'."""
    server = _server(tmp_path)
    payload = _payload(server, "capacity", {})
    assert set(payload["capacity"]) >= {"generated_at", "ledger_wired", "pools"}


# --------------------------------------------------------------------------- #
# waves
# --------------------------------------------------------------------------- #


def test_submit_wave_admits_a_valid_wave_and_records_it(tmp_path: Path) -> None:
    """Covers: prompt section 'submit_wave' (validated + recorded)."""
    server = _server(tmp_path)
    payload = _payload(server, "submit_wave", {"wave": _wave()})
    assert payload["admitted"] is True
    assert payload["wave_id"] == "w-1"
    assert server.context.ledger.replay().waves["w-1"]["wave_id"] == "w-1"


def test_submit_wave_refuses_overlapping_lane_scopes(tmp_path: Path) -> None:
    """Covers: ADR-0004 invariant 2 (overlapping lane files are rejected)."""
    server = _server(tmp_path)
    shared = "src/shared.py"
    response = _call(server, "submit_wave", {"wave": _wave(lane_a=shared, lane_b=shared)})
    assert response["result"]["isError"] is True
    data = response["result"]["structuredContent"]["error"]["data"]
    assert data["reasons"] and data["reasons"][0].startswith("OVERLAPPING_TOUCHES:")


def test_submit_wave_refuses_a_self_overlapping_lane(tmp_path: Path) -> None:
    """Covers: ADR-0004 invariant 2 (a lane may not touch its own no-touch set)."""
    server = _server(tmp_path)
    wave = _wave()
    wave["lanes"][0]["must_not_touch"] = ["src/a.py"]
    response = _call(server, "submit_wave", {"wave": wave})
    assert response["result"]["isError"] is True


def test_wave_status_returns_lanes_barriers_and_slot_states(tmp_path: Path) -> None:
    """Covers: prompt section 'wave_status (state, lanes, barrier conditions)'."""
    server = _server(tmp_path)
    _payload(server, "submit_wave", {"wave": _wave()})
    payload = _payload(server, "wave_status", {"wave_id": "w-1"})
    assert payload["state"] == "PLANNED"
    assert payload["terminal"] is False
    assert [lane["lane_id"] for lane in payload["lanes"]] == ["l1", "l2"]
    assert payload["barriers"]["merge_order"] == ["l1", "l2"]
    assert payload["barriers"]["g3_budget"] == 1
    assert payload["lanes"][0]["slot_state"] == "queued"


def test_wave_status_accepts_an_inline_wave(tmp_path: Path) -> None:
    """Covers: prompt section 'wave_status' (inline wave projection)."""
    server = _server(tmp_path)
    payload = _payload(server, "wave_status", {"wave": _wave()})
    assert payload["wave_id"] == "w-1"


def test_wave_status_refuses_an_unknown_wave(tmp_path: Path) -> None:
    """Covers: prompt section 'wave_status' (unknown wave is explicit)."""
    server = _server(tmp_path)
    response = _call(server, "wave_status", {"wave_id": "nope"})
    assert response["result"]["isError"] is True
    assert response["result"]["structuredContent"]["error"]["code"] == -32040


def test_wave_status_requires_a_wave_or_wave_id(tmp_path: Path) -> None:
    """Covers: prompt section 'wave_status' (arguments are validated)."""
    server = _server(tmp_path)
    response = _call(server, "wave_status", {})
    assert response["result"]["isError"] is True


# --------------------------------------------------------------------------- #
# questions
# --------------------------------------------------------------------------- #


def test_list_questions_returns_views(tmp_path: Path) -> None:
    """Covers: prompt section 'list_questions'."""
    store = tmp_path / "questions.json"
    _ask(store)
    server = _server(tmp_path)
    payload = _payload(server, "list_questions", {"status": "OPEN"})
    assert payload["count"] == 1
    assert payload["questions"][0]["answerable_by"] == "ORCHESTRATOR"


def test_answer_question_refuses_owner_authority(tmp_path: Path) -> None:
    """Covers: ADR-0004 invariant 6 (owner-only decisions refuse over MCP)."""
    store = tmp_path / "questions.json"
    _ask(store)
    server = _server(tmp_path)
    response = _call(
        server,
        "answer_question",
        {"question_id_or_key": "q-1", "answer_text": "yes", "authority": "OWNER"},
    )
    assert response["error"]["code"] == -32010


def test_answer_question_refuses_an_owner_only_decision_class(tmp_path: Path) -> None:
    """Covers: ADR-0004 D6/D7 (owner-only classes are not Orchestrator-answerable)."""
    store = tmp_path / "questions.json"
    _ask(store, decision_class=DecisionClass.MOVE_MAIN)
    server = _server(tmp_path)
    response = _call(
        server,
        "answer_question",
        {"question_id_or_key": "q-1", "answer_text": "yes", "authority": "ORCHESTRATOR"},
    )
    assert response["error"]["code"] == -32010


def test_answer_question_refuses_its_own_finding(tmp_path: Path) -> None:
    """Covers: prompt section 'implementer never answers its own finding'."""
    store = tmp_path / "questions.json"
    _ask(store, raised_by_role=AskRole.ORCHESTRATOR, raised_by="orch-1")
    server = _server(tmp_path)
    response = _call(
        server,
        "answer_question",
        {"question_id_or_key": "q-1", "answer_text": "yes", "authority": "ORCHESTRATOR"},
    )
    assert response["result"]["isError"] is True
    assert "own finding" in response["result"]["structuredContent"]["error"]["message"]


def test_answer_question_records_an_orchestrator_answer(tmp_path: Path) -> None:
    """Covers: prompt section 'answer_question' (answerable class succeeds)."""
    store = tmp_path / "questions.json"
    _ask(store, decision_class=DecisionClass.FIX_UP_CONTENT)
    server = _server(tmp_path)
    payload = _payload(
        server,
        "answer_question",
        {
            "question_id_or_key": "q-1",
            "answer_text": "route it to the same worker",
            "authority": "ORCHESTRATOR",
            "answered_by": "orch-1",
            "evidence": "lane-check.txt",
            "rejected_alternative": "a Surgeon handoff",
        },
    )
    assert payload["question"]["status"] == "ANSWERED"
    assert payload["question"]["answer"]["authority"] == "ORCHESTRATOR"


def test_answer_question_is_idempotent(tmp_path: Path) -> None:
    """Covers: ADR-0004 invariant 13 (a repeated identical answer is a no-op)."""
    store = tmp_path / "questions.json"
    _ask(store, decision_class=DecisionClass.FIX_UP_CONTENT)
    server = _server(tmp_path)
    arguments = {
        "question_id_or_key": "q-1",
        "answer_text": "same words",
        "authority": "ORCHESTRATOR",
        "answered_by": "orch-1",
        "evidence": "e",
        "rejected_alternative": "r",
    }
    first = _payload(server, "answer_question", arguments)
    second = _payload(server, "answer_question", arguments)
    assert first["question"]["status"] == second["question"]["status"] == "ANSWERED"
    assert OwnerQuestionQueue(store).divergences() == ()
    assert server.context.ledger.replay().counts.get("question_answered") == 1


# --------------------------------------------------------------------------- #
# config proposals
# --------------------------------------------------------------------------- #


def test_propose_config_change_validates_and_applies_nothing(tmp_path: Path) -> None:
    """Covers: ADR-0004 D8.2 (a proposal changes nothing)."""
    server = _server(tmp_path)
    payload = _payload(
        server,
        "propose_config_change",
        {
            "changes": {"max_worker_sessions_per_project": 4},
            "rationale": "more sessions per project",
            "proposed_by": "orchestrator",
        },
    )
    assert payload["applied"] is False
    assert payload["proposal"]["changed"] == ["max_worker_sessions_per_project"]
    assert ConfigLedger(tmp_path / "config.jsonl").entries() == ()


def test_propose_config_change_refuses_an_unknown_setting(tmp_path: Path) -> None:
    """Covers: ADR-0004 D8.1 (one validating service layer, fail-closed)."""
    server = _server(tmp_path)
    response = _call(
        server,
        "propose_config_change",
        {"changes": {"not_a_setting": 1}, "rationale": "r", "proposed_by": "o"},
    )
    assert response["result"]["isError"] is True


def test_list_proposals_lists_approved_decisions(tmp_path: Path) -> None:
    """Covers: prompt section 'list_proposals' (ConfigLedger entries)."""
    ledger_path = tmp_path / "config.jsonl"
    ledger = ConfigLedger(ledger_path)
    approve_config_proposal(
        propose_config_change(
            current_workflow_settings(ledger),
            {"max_worker_sessions_per_project": 4},
            rationale="approved change",
            proposed_by="orchestrator",
        ),
        ledger=ledger,
        approved_by="owner",
    )
    server = _server(tmp_path)
    payload = _payload(server, "list_proposals", {})
    assert payload["count"] == 1
    assert payload["proposals"][0]["approved_by"] == "owner"


def test_no_tool_can_approve_a_config_proposal(tmp_path: Path) -> None:
    """Covers: ADR-0004 invariant 7 (no MCP tool can apply a config change)."""
    server = _server(tmp_path)
    for name in ("approve_config_proposal", "config_approve", "record_config_decision"):
        response = _call(server, name, {})
        assert response["error"]["code"] == -32010, name
    assert ConfigLedger(tmp_path / "config.jsonl").entries() == ()
