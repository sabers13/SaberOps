"""Job CLI (M2-3 section I) through ``saberops.cli.main``.

Isolated ``XDG_*`` plus ``--db`` under ``tmp_path`` (pattern:
``tests/test_plans_cli.py``). No worker, provider CLI or scheduler
loop is ever launched: ``cancel`` of a RUNNING job uses a
monkeypatched canceller.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

import saberops.cli as cli_module
from saberops.cli import main
from saberops.db import Database
from saberops.jobs.service import mark_dispatched


@pytest.fixture()
def isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    state = tmp_path / "state"
    config = tmp_path / "config"
    cache = tmp_path / "cache"
    for directory in (state, config, cache):
        directory.mkdir(parents=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.delenv("SABEROPS_PROFILE", raising=False)
    return tmp_path


def _cli(*argv: str, capsys: pytest.CaptureFixture[str]) -> tuple[int, str, str]:
    code = main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-b", "main"], cwd=path, check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "cli@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "CLI"], cwd=path, check=True)
    (path / "README.md").write_text("# cli\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-m", "initial"], cwd=path, check=True, capture_output=True)
    return path


def _db_path(isolated: Path) -> str:
    return str(isolated / "jobs.db")


def _submit(
    isolated: Path,
    capsys: pytest.CaptureFixture[str],
    repo: Path,
    *extra: str,
) -> tuple[int, dict[str, Any], str]:
    code, out, err = _cli(
        "job", "submit", "--repo", str(repo), "--goal", "cli goal",
        "--db", _db_path(isolated), "--json", *extra, capsys=capsys,
    )
    assert out.strip(), f"expected JSON on stdout, got {out!r} {err!r}"
    return code, json.loads(out), err


# ---------------------------------------------------------------------------
# submit / get / list
# ---------------------------------------------------------------------------


def test_submit_get_list_json_shapes(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    code, payload, _ = _submit(isolated, capsys, repo)
    assert code == 0
    assert set(payload) == {"job", "created"}
    assert payload["created"] is True
    job = payload["job"]
    assert job["state"] == "QUEUED"
    assert job["base_ref"] == "HEAD" and job["base_sha"]
    assert job["requirements"]["role"] == "WORKER"
    job_id = job["job_id"]

    code, out, _ = _cli("job", "get", job_id, "--db", _db_path(isolated), "--json",
                        capsys=capsys)
    assert code == 0
    fetched = json.loads(out)
    assert fetched["job_id"] == job_id
    assert fetched["request_digest"] == job["request_digest"]

    code, out, _ = _cli("job", "list", "--db", _db_path(isolated), "--json",
                        capsys=capsys)
    assert code == 0
    listed = json.loads(out)
    assert set(listed) == {"jobs"}
    assert [j["job_id"] for j in listed["jobs"]] == [job_id]


def test_submit_human_and_list_human(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    code, out, _ = _cli(
        "job", "submit", "--repo", str(repo), "--goal", "human goal",
        "--db", _db_path(isolated), capsys=capsys,
    )
    assert code == 0
    assert "QUEUED" in out
    code, out, _ = _cli("job", "list", "--db", _db_path(isolated), capsys=capsys)
    assert code == 0
    assert "human goal" in out


def test_submit_idempotent_replay_exit_0(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    code, first, _ = _submit(isolated, capsys, repo, "--idempotency-key", "cli-k1")
    assert code == 0 and first["created"] is True
    code, second, _ = _submit(isolated, capsys, repo, "--idempotency-key", "cli-k1")
    assert code == 0
    assert second["created"] is False
    assert second["job"]["job_id"] == first["job"]["job_id"]


def test_submit_conflict_exit_1(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    code, _, _ = _submit(isolated, capsys, repo, "--idempotency-key", "cli-k2")
    assert code == 0
    code, out, err = _cli(
        "job", "submit", "--repo", str(repo), "--goal", "different goal",
        "--db", _db_path(isolated), "--json", "--idempotency-key", "cli-k2",
        capsys=capsys,
    )
    assert code == 1
    assert "conflict" in err.lower()


def test_submit_infeasible_role_exit_3_with_job_printed(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    code, payload, _ = _submit(isolated, capsys, repo, "--role", "SURGEON")
    assert code == 3
    assert payload["job"]["state"] == "FAILED"
    assert payload["job"]["failure_reason"] == "NO_ROUTE_FOR_REQUIREMENTS"
    code, out, _ = _cli(
        "job", "submit", "--repo", str(repo), "--goal", "other",
        "--role", "REVIEWER", "--db", _db_path(isolated), capsys=capsys,
    )
    assert code == 3
    assert "FAILED" in out


def test_submit_validation_exit_2(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    code, _, err = _cli(
        "job", "submit", "--repo", str(repo), "--goal", "x",
        "--capability", "teleport-unknown", "--db", _db_path(isolated),
        capsys=capsys,
    )
    assert code == 2
    code, _, err = _cli(
        "job", "submit", "--repo", str(repo), "--goal", "x",
        "--deadline", "2026-10-13T12:00:00", "--db", _db_path(isolated),
        capsys=capsys,
    )
    assert code == 2
    code, _, err = _cli(
        "job", "submit", "--repo", str(isolated / "missing"), "--goal", "x",
        "--db", _db_path(isolated), capsys=capsys,
    )
    assert code == 2
    # Outside the size vocabulary argparse itself refuses (exit 2).
    with pytest.raises(SystemExit) as exc:
        main(["job", "submit", "--repo", str(repo), "--goal", "x",
              "--size", "XL", "--db", _db_path(isolated)])
    assert exc.value.code == 2


def test_get_and_cancel_unknown_exit_1(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code, _, err = _cli("job", "get", "no-such", "--db", _db_path(isolated),
                        capsys=capsys)
    assert code == 1
    assert "unknown job" in err
    code, _, err = _cli("job", "cancel", "no-such", "--db", _db_path(isolated),
                        capsys=capsys)
    assert code == 1


def test_list_state_filter_and_limit(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    assert _submit(isolated, capsys, repo)[0] == 0
    assert _submit(isolated, capsys, repo, "--role", "SURGEON")[0] == 3
    code, out, _ = _cli(
        "job", "list", "--state", "FAILED", "--db", _db_path(isolated),
        "--json", capsys=capsys,
    )
    assert code == 0
    assert len(json.loads(out)["jobs"]) == 1
    code, out, _ = _cli(
        "job", "list", "--limit", "1", "--db", _db_path(isolated),
        "--json", capsys=capsys,
    )
    assert code == 0
    assert len(json.loads(out)["jobs"]) == 1


# ---------------------------------------------------------------------------
# cancel (QUEUED immediate; RUNNING via monkeypatched canceller)
# ---------------------------------------------------------------------------


def test_cancel_queued(
    isolated: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = _git_repo(isolated / "repo")
    _, payload, _ = _submit(isolated, capsys, repo)
    job_id = payload["job"]["job_id"]
    code, out, _ = _cli("job", "cancel", job_id, "--db", _db_path(isolated),
                        "--json", capsys=capsys)
    assert code == 0
    result = json.loads(out)
    assert result["cancelled"] is True
    assert result["job"]["state"] == "CANCELLED"


class _StubSupervisor:
    next_result: bool = True
    seen: list[str] = []  # noqa: RUF012 - reset per test via monkeypatch

    def __init__(self, db: object = None) -> None:
        self._db = db

    def cancel(self, run_id: str) -> bool:
        _StubSupervisor.seen.append(run_id)
        return _StubSupervisor.next_result


def _patch_supervisor(monkeypatch: pytest.MonkeyPatch, result: bool) -> None:
    _StubSupervisor.next_result = result
    _StubSupervisor.seen = []
    monkeypatch.setattr(cli_module, "RunSupervisor", _StubSupervisor)


def test_cancel_running_uses_canceller(
    isolated: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _git_repo(isolated / "repo")
    _, payload, _ = _submit(isolated, capsys, repo)
    job_id = payload["job"]["job_id"]
    mark_dispatched(Database(_db_path(isolated)), job_id, "run_live_1")

    _patch_supervisor(monkeypatch, True)
    code, out, _ = _cli("job", "cancel", job_id, "--db", _db_path(isolated),
                        "--json", capsys=capsys)
    assert code == 0
    result = json.loads(out)
    assert result["cancelled"] is True
    assert result["job"]["state"] == "CANCELLED"
    assert _StubSupervisor.seen == ["run_live_1"]


def test_cancel_running_canceller_false_keeps_job(
    isolated: Path, capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _git_repo(isolated / "repo")
    _, payload, _ = _submit(isolated, capsys, repo)
    job_id = payload["job"]["job_id"]
    mark_dispatched(Database(_db_path(isolated)), job_id, "run_live_2")

    _patch_supervisor(monkeypatch, False)
    code, out, _ = _cli("job", "cancel", job_id, "--db", _db_path(isolated),
                        "--json", capsys=capsys)
    assert code == 0
    result = json.loads(out)
    assert result["cancelled"] is False
    assert result["job"]["state"] == "RUNNING"
