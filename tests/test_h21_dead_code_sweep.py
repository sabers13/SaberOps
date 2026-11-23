"""H21: the uncalled functions removed in the dead-code sweep stay deleted.

Every symbol below had no caller on local ``main`` (MCP, scheduler, CLI and web
paths, scripts, tests and the benchmark rig included). These checks fail if one
comes back without a caller.
"""

from __future__ import annotations

import ast
from pathlib import Path

from saberops import agent_sessions, db, review_policy, runtime, supervisor

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "benchmark_core.py"


def test_review_round_state_machine_and_codec_are_absent() -> None:
    for name in (
        "ReviewRoundState",
        "initial_review_round_state",
        "next_review_round",
        "final_repair_allowed",
        "review_round_state_to_json",
        "review_round_state_from_json",
        "_POLICY_VERDICT_ALIASES",
    ):
        assert not hasattr(review_policy, name)


def test_inbox_store_enumerators_are_absent() -> None:
    for name in (
        "InboxStore",
        "list_inbox_stores",
        "list_known_runtimes",
        "_owner_db_resolved_path",
    ):
        assert not hasattr(runtime, name)
        assert not hasattr(runtime.ProjectRuntimeManager, name)


def test_supervisor_and_store_helpers_are_absent() -> None:
    assert not hasattr(supervisor.RunSupervisor, "observe_long_worker")
    assert not hasattr(db.Database, "mark_worker_stalled")
    assert not hasattr(db.Database, "update_attempt_trace")
    assert not hasattr(agent_sessions, "legacy_session")


def test_benchmark_rig_defines_no_blocked_candidate_sha_helper() -> None:
    tree = ast.parse(SCRIPT.read_text())
    defined = {
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    assert "first_blocked_candidate_sha" not in defined
