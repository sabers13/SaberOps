"""F23: ``orch watch`` opens a read-only tmux view over running jobs.

Every test runs against a fake tmux binary (``tests/fixtures/fake_tmux.py``)
through ``ORCH_TMUX_BIN``; nothing ever connects to a real tmux server, so the
owner's own sessions on the default socket are never created, renamed, killed
or sent keys.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any, cast

import pytest

from saberops.cli import main as orch_main
from saberops.db import Database
from saberops.jobs.models import JobRequirements, JobState
from saberops.jobs.service import list_jobs, submit_job
from saberops.jobs.store import mark_dispatched, reconcile_transition
from saberops.models import Run, RunStatus, Tier
from saberops.watch import (
    DEFAULT_SESSION_BASE,
    Tmux,
    WatchError,
    WindowAction,
    apply_window_actions,
    default_session_name,
    plan_window_actions,
    render_summary,
    resolve_session_name,
)

pytestmark = pytest.mark.packaged_routing_default

FAKE_TMUX = Path(__file__).resolve().parent / "fixtures" / "fake_tmux.py"


class FakeTmux:
    """Install and inspect the fake tmux binary for one test."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        self.root = tmp_path / "fake-tmux"
        self.root.mkdir(parents=True, exist_ok=True)
        shim = tmp_path / "bin" / "fake-tmux"
        shim.parent.mkdir(parents=True, exist_ok=True)
        shim.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{FAKE_TMUX}" "$@"\n',
            encoding="utf-8",
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        monkeypatch.setenv("ORCH_TMUX_BIN", str(shim))
        monkeypatch.setenv("ORCH_FAKE_TMUX_STATE", str(self.root))
        self.shim = shim

    @property
    def state(self) -> dict[str, Any]:
        path = self.root / "state.json"
        if not path.exists():
            return {"sessions": {}}
        return cast(dict[str, Any], json.loads(path.read_text(encoding="utf-8")))

    def windows(self, session: str) -> list[dict[str, Any]]:
        return list(self.state["sessions"][session]["windows"])

    def window_names(self, session: str) -> list[str]:
        return [window["name"] for window in self.windows(session)]

    def calls(self) -> list[list[str]]:
        path = self.root / "calls.jsonl"
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def create_session(
        self, name: str, *, ours: bool = False, window: str = "foreign"
    ) -> None:
        env = dict(os.environ)
        subprocess.run(
            [str(self.shim), "new-session", "-d", "-s", name, "-n", window, "--", "sleep", "1"],
            check=True,
            env=env,
        )
        if ours:
            subprocess.run(
                [str(self.shim), "set-option", "-t", name, "@saberops_watch", "1"],
                check=True,
                env=env,
            )
        # The direct set-up calls are not part of what the watcher asked for.
        (self.root / "calls.jsonl").write_text("", encoding="utf-8")


@pytest.fixture()
def rig(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    from lifecycle_rig import build_lifecycle_rig

    return build_lifecycle_rig(tmp_path, monkeypatch)


def _submit_running(rig: Any, goal: str, run_id: str) -> str:
    db = rig.db()
    result = submit_job(
        db,
        repo=rig.repo,
        goal=goal,
        acceptance="the change is checked",
        requirements=JobRequirements(),
        caller_id="f23-test",
    )
    assert result.job.state == JobState.QUEUED, result.job
    mark_dispatched(db, result.job.job_id, run_id)
    return result.job.job_id


def _start_watch(rig: Any, capsys: Any, *extra: str) -> int:
    db_path = rig.db().db_path
    code = orch_main(["watch", "--session", "f23-test", "--db", str(db_path), *extra])
    capsys.readouterr()
    return code


def test_two_running_jobs_get_two_windows_plus_the_summary(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    first = _submit_running(rig, "fix the parser", "run_f23_first")
    second = _submit_running(rig, "document the CLI", "run_f23_second")

    assert _start_watch(rig, capsys) == 0
    windows = fake.windows("f23-test")
    assert windows[0]["name"] == "summary"
    assert len(windows) == 3
    by_job = {window["job"]: window for window in windows if window["job"]}
    assert set(by_job) == {first, second}
    for job_id, window in by_job.items():
        assert window["name"].startswith(f"job-{job_id[-8:]}")
    # Each window runs the read-only follow loop for exactly one job.
    for window in by_job.values():
        assert window["argv"][:3] == [sys.executable, "-m", "saberops.cli"]
        assert window["argv"][3:5] == ["watch", "--follow"]
        assert "--job" in window["argv"]
        assert window["job"] in window["argv"]
    # The session carries our marker, so a later run can safely attach.
    assert fake.state["sessions"]["f23-test"]["ours"] is True


def test_finished_job_window_is_marked_and_kept(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    done = _submit_running(rig, "first lane", "run_f23_done")
    still = _submit_running(rig, "second lane", "run_f23_live")
    assert _start_watch(rig, capsys) == 0
    before = {window["job"]: window["name"] for window in fake.windows("f23-test") if window["job"]}

    reconcile_transition(rig.db(), done, to=JobState.SUCCEEDED)
    db_path = rig.db().db_path
    assert (
        orch_main(
            ["watch", "--summary", "--session", "f23-test", "--db", str(db_path), "--once"]
        )
        == 0
    )
    out = capsys.readouterr().out

    after = {window["job"]: window["name"] for window in fake.windows("f23-test") if window["job"]}
    assert after[still] == before[still]
    assert after[done] == f"{before[done]}-ok"
    assert len(after) == 2  # marked, never closed
    assert "SUCCEEDED" in out

    # The job window itself prints the final state and the next owner action.
    assert (
        orch_main(
            [
                "watch",
                "--follow",
                "--job",
                done,
                "--once",
                "--db",
                str(db_path),
                "--interval",
                "0.5",
            ]
        )
        == 0
    )
    follow_out = capsys.readouterr().out
    assert "Final: SUCCEEDED" in follow_out
    assert "Next owner action:" in follow_out
    assert "read-only; keystrokes never reach a worker" in follow_out


def test_new_job_adds_a_window_on_the_next_tick(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    _submit_running(rig, "first lane", "run_f23_one")
    assert _start_watch(rig, capsys) == 0
    assert len([w for w in fake.windows("f23-test") if w["job"]]) == 1

    third = _submit_running(rig, "third lane", "run_f23_three")
    db_path = rig.db().db_path
    assert (
        orch_main(
            ["watch", "--summary", "--session", "f23-test", "--db", str(db_path), "--once"]
        )
        == 0
    )
    capsys.readouterr()
    windows = [window for window in fake.windows("f23-test") if window["job"]]
    assert len(windows) == 2
    assert third in {window["job"] for window in windows}


def test_watch_never_delivers_input_to_a_worker(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    job = _submit_running(rig, "never touched", "run_f23_untouched")
    assert _start_watch(rig, capsys) == 0
    assert (
        orch_main(
            [
                "watch",
                "--follow",
                "--job",
                job,
                "--once",
                "--db",
                str(rig.db().db_path),
            ]
        )
        == 0
    )
    capsys.readouterr()
    for call in fake.calls():
        assert call[0] not in {
            "send-keys",
            "pipe-pane",
            "respawn-pane",
            "respawn-window",
            "set-buffer",
            "paste-buffer",
        }, call
    # No shell or worker CLI is ever the command a window runs.
    for window in fake.windows("f23-test"):
        if window["job"]:
            assert window["argv"][:3] == [sys.executable, "-m", "saberops.cli"]


def test_default_session_name_never_collides_with_a_foreign_session(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    _submit_running(rig, "a lane", "run_f23_collision")
    db_path = rig.db().db_path
    wanted = default_session_name(rig.repo.resolve(), db_path)
    fake.create_session(wanted)

    assert (
        orch_main(["watch", "--repo", str(rig.repo), "--db", str(db_path)]) == 0
    )
    capsys.readouterr()
    chosen = f"{wanted}-2"
    assert chosen in fake.state["sessions"]
    assert fake.state["sessions"][chosen]["ours"] is True
    # The foreign session is untouched: same single window, no marker.
    assert fake.window_names(wanted) == ["foreign"]
    assert fake.state["sessions"][wanted]["ours"] is False
    for call in fake.calls():
        if call[0] in ("new-window", "rename-window", "kill-session", "send-keys"):
            target = call[call.index("-t") + 1] if "-t" in call else ""
            assert target.split(":", 1)[0] != wanted, call


def test_explicit_foreign_session_is_refused_without_touching_it(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    _submit_running(rig, "a lane", "run_f23_foreign")
    fake.create_session("firstmate")
    db_path = rig.db().db_path

    code = orch_main(["watch", "--session", "firstmate", "--db", str(db_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "not created by orch watch" in captured.err
    assert fake.window_names("firstmate") == ["foreign"]
    assert fake.state["sessions"]["firstmate"]["ours"] is False
    assert not any(
        call[0] in ("new-window", "rename-window", "kill-session", "send-keys", "set-option")
        for call in fake.calls()
    )


def test_existing_own_session_is_reused(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    _submit_running(rig, "a lane", "run_f23_reuse")
    assert _start_watch(rig, capsys) == 0
    windows_before = fake.window_names("f23-test")
    calls_before = len(fake.calls())

    assert _start_watch(rig, capsys) == 0
    assert fake.window_names("f23-test") == windows_before
    new_calls = fake.calls()[calls_before:]
    assert not any(call[0] == "new-session" for call in new_calls)


def test_without_tmux_the_summary_names_what_to_follow_and_monitor_takes_over(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    monkeypatch.setenv("ORCH_TMUX_BIN", str(tmp_path / "no-such-tmux"))
    first = _submit_running(rig, "first lane", "run_f23_fallback")
    _submit_running(rig, "second lane", "run_f23_fallback_two")
    db_path = rig.db().db_path

    code = orch_main(["watch", "--db", str(db_path)])
    captured = capsys.readouterr()
    assert code == 0
    assert "tmux is not installed" in captured.err
    assert "--job ID" in captured.err
    assert first in captured.out

    db = Database(db_path)
    db.create_run(
        Run(
            id="run_f23_fallback",
            task="fallback lane",
            target_repo=str(rig.repo),
            base_commit=rig.base_sha,
            tier=Tier.T1,
            training_allowed=True,
            gate_command="true",
            status=RunStatus.RUNNING,
            created_at="2026-11-19T10:00:00+00:00",
        )
    )
    db.record_event(
        "run_f23_fallback", "worker_started", payload={"provider": "codex", "attempt": 1}
    )
    assert (
        orch_main(
            ["watch", "--follow", "--job", first, "--once", "--db", str(db_path)]
        )
        == 0
    )
    follow_out = capsys.readouterr().out
    assert "· worker_started" in follow_out
    assert "codex" in follow_out

    code = orch_main(
        [
            "watch",
            "--job",
            first,
            "--db",
            str(db_path),
            "--timeout",
            "0.05",
            "--interval",
            "0.5",
        ]
    )
    captured = capsys.readouterr()
    assert "following" in captured.err
    assert "`orch monitor run_f23_fallback`" in captured.err
    assert "Run run_f23_fallback" in captured.out
    assert code in (0, 2)


def test_session_name_resolution_is_safe_and_deterministic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    tmux = Tmux()
    assert resolve_session_name(tmux, "saberops-watch-x", explicit=False) == "saberops-watch-x"
    fake.create_session("saberops-watch-x")
    assert resolve_session_name(tmux, "saberops-watch-x", explicit=False) == "saberops-watch-x-2"
    with pytest.raises(WatchError):
        resolve_session_name(tmux, "saberops-watch-x", explicit=True)
    with pytest.raises(WatchError):
        resolve_session_name(tmux, "bad name", explicit=True)
    fake.create_session("saberops-watch-x-2", ours=True)
    assert resolve_session_name(tmux, "saberops-watch-x-2", explicit=True) == "saberops-watch-x-2"
    assert default_session_name(tmp_path, tmp_path / "x.db").startswith(DEFAULT_SESSION_BASE)


def test_plan_window_actions_creates_then_marks(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    job_id = _submit_running(rig, "apply the change", "run_f23_plan")
    job = next(job for job in list_jobs(rig.db()) if job.job_id == job_id)
    expected_label = f"job-{job.job_id[-8:]}-apply-the-change"
    assert plan_window_actions([job], [("summary", None)]) == [
        WindowAction("create", job.job_id, expected_label, expected_label)
    ]

    tmux = Tmux()
    tmux.new_session("f23-unit", [sys.executable, "-c", "pass"])
    tmux.mark_session_ours("f23-unit")
    apply_window_actions(
        tmux,
        "f23-unit",
        plan_window_actions([job], tmux.list_windows("f23-unit")),
        db_path=rig.db().db_path,
        interval=2.0,
    )
    assert len(fake.windows("f23-unit")) == 2
    assert fake.windows("f23-unit")[0]["job"] is None  # summary window

    reconcile_transition(rig.db(), job_id, to=JobState.FAILED, failure_reason="RUN_FAILED")
    failed = next(job for job in list_jobs(rig.db()) if job.job_id == job_id)
    actions = plan_window_actions([failed], tmux.list_windows("f23-unit"))
    assert [action.kind for action in actions] == ["rename"]
    assert actions[0].label == f"{expected_label}-failed"


def test_summary_flags_pause_state_and_missing_store(
    rig: Any, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: Any
) -> None:
    fake = FakeTmux(monkeypatch, tmp_path)
    db = rig.db()
    frame = render_summary(db, [], session="f23-summary", repo=None)
    assert "Pause" in frame
    assert "not paused" in frame
    assert "no jobs" in frame
    assert "read-only" in frame

    code = orch_main(
        ["watch", "--once", "--db", str(tmp_path / "missing.db"), "--session", "x"]
    )
    assert code == 1
    assert "no SaberOps store" in capsys.readouterr().err
    assert fake.calls() == []
