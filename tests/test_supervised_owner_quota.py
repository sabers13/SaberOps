"""Review admission in the detached owner must read the owner's quota store.

The real run ``run_70ead49549d5_f489d2a97d`` dispatched its Codex worker from a
supervisor that opened the project database with the global owner database
attached.  The supervisor then spawned the run's execution owner with only the
explicit project ``--db``.  That child opened the project store as its own
owner, so ``effective_quota_states`` saw no Anthropic evidence even though
``orch preflight`` (which reads the global store) reported the owner's fresh
``discovered-pool:claude-code-subscription`` figure.  Review admission then
refused with ``QUOTA_EVIDENCE_REQUIRED`` / ``pool_id: null`` and the reviewer
was skipped as ``quota_blocked``.

This test follows that exact handoff: supervisor argv -> child argument
parsing -> child database -> the shared control-plane decision and pre-dispatch
skip reason used by review admission.  The global store carries the discovered
binding pool's quota, and the frozen policy relies on the packaged ``claude-pro``
default that 76a9910 maps onto the discovered binding identity.
"""
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from saberops.capacity.snapshot import effective_quota_states
from saberops.cli import build_parser, db_for_run
from saberops.config import RunConfig
from saberops.control_plane import BindingRole, materialize_discovered_binding
from saberops.control_plane.policy import (
    freeze_control_plane_policy,
    load_control_plane_policy,
    reconstruct_frozen_control_plane,
)
from saberops.db import Database
from saberops.dispatch import control_plane_decision
from saberops.models import WorkerCandidate, WorkerRequest, WorkerResult
from saberops.review import _pre_dispatch_skip_reason
from saberops.supervisor import RunSupervisor
from saberops.workers.base import WorkerAdapter


class _Adapter(WorkerAdapter):
    provider_name = "anthropic"

    def is_available(self) -> bool:
        return True

    def run(self, request: WorkerRequest) -> WorkerResult:
        raise AssertionError("no vendor dispatch")


@pytest.mark.parametrize("remaining,eligible", [(93, True), (None, False), (0, False)])
@pytest.mark.parametrize("recovery", [False, True])
def test_detached_review_reads_owner_quota(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    remaining: int | None,
    eligible: bool,
    recovery: bool,
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    config_dir = tmp_path / "config" / "saberops"
    config_dir.mkdir(parents=True)
    (config_dir / "control_plane.json").write_text(
        json.dumps(
            {
                "version": 2,
                "providers": {},
                "quota_pools": {},
                "postures": {
                    "anthropic": {
                        "class": "A",
                        "policy_urls": ["https://example.com/policy"],
                        "ttl_days": 90,
                        "verified_on": datetime.now(UTC).date().isoformat(),
                    }
                },
            }
        )
    )
    owner = Database(tmp_path / "owner.db")
    project = Database(tmp_path / "project.db", owner_db=owner)
    binding, _, _ = materialize_discovered_binding(
        connection_id="claude-code-subscription",
        provider="anthropic",
        model="claude-haiku-4-5-20251001",
        binding_role=BindingRole.CHECK,
    )
    assert binding.binding_id == (
        "discovered-binding:reviewer:claude-code-subscription:claude-haiku-4-5-20251001"
    )
    assert binding.quota_pool_id == "discovered-pool:claude-code-subscription"
    if remaining is not None:
        owner.set_quota("anthropic", binding.quota_pool_id, remaining)

    supervisor = RunSupervisor(db=project)
    config = RunConfig(task="test", target_repo=str(tmp_path))
    argv = (
        supervisor._build_recovery_cli_args("run_test", config)
        if recovery
        else supervisor._build_cli_args("run_test", config)
    )
    command_index = argv.index("recover" if recovery else "run")
    args = build_parser().parse_args(argv[command_index:])
    child = db_for_run(args, "run_test")
    assert child.db_path == project.db_path
    # The exact regression: a detached owner that opens only its explicit
    # project ``--db`` treats the project store as the owner store.
    assert child.owner_db.db_path == owner.db_path

    frozen = freeze_control_plane_policy(load_control_plane_policy())
    policy = reconstruct_frozen_control_plane(frozen.as_json(), frozen.digest).policy
    candidate = WorkerCandidate(provider="anthropic", model=binding.model)
    quota_states = effective_quota_states(child, run_probes=False)
    decision = control_plane_decision(
        candidate=candidate,
        adapter=_Adapter(),
        training_allowed=True,
        quota_states=quota_states,
        prompt_bytes=2552,
        policy=policy,
        binding_pool_id=binding.quota_pool_id,
        db=child,
    )
    skip = _pre_dispatch_skip_reason(
        candidate=candidate,
        adapter=_Adapter(),
        training_allowed=True,
        quota_states=quota_states,
        prompt_bytes=2552,
        policy=policy,
        binding_pool_id=binding.quota_pool_id,
        db=child,
    )

    assert decision.eligible is eligible, decision.as_payload()
    if eligible:
        # The packaged ``claude-pro`` default is resolved to the discovered
        # binding identity, and the owner's evidence governs it.
        assert decision.quota.pool_id == binding.quota_pool_id
        assert decision.quota.mode.value == "STRICT"
        assert skip is None
    else:
        # Genuinely absent or depleted evidence still fails closed.
        assert decision.exclusion_reason is not None
        assert skip == "quota_blocked"
