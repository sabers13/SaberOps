"""Observational sessions must preserve execution, persistence and isolation."""
from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from saberops import agent_sessions
from saberops.agent_sessions import observe_invocation, owner_directory, read_sessions
from saberops.models import AttemptStatus, WorkerRequest, WorkerResult


def result() -> WorkerResult:
    return WorkerResult(provider='codex', model='requested', status=AttemptStatus.SUCCESS,
                        summary='Finished', stdout='Final answer', stderr='warning',
                        actual_provider='openai', actual_model='reported')


def test_output_is_live_callbacks_are_chained_and_final_identity_retained(tmp_path: Path) -> None:
    chunks: list[str] = []
    request = WorkerRequest(task='PRIVATE PROMPT', model='requested', worktree_path=tmp_path,
                            env={'SECRET': 'PRIVATE ENV'}, on_stdout=chunks.append)
    root = tmp_path / 'sessions'

    def invoke(observed: WorkerRequest) -> WorkerResult:
        assert observed.on_stdout is not None
        observed.on_stdout('first output\n')
        live = read_sessions(root, 'run-one')
        assert len(live) == 1 and live[0]['state'] == 'ACTIVE'
        assert live[0]['output'] == 'first output\n'
        assert observed.task == request.task and observed.env == request.env
        assert observed.read_only == request.read_only
        return result()

    returned = observe_invocation(invoke, request, root=root, owner='run-one',
                                  role='worker', provider='codex', association='attempt-2')
    assert returned.stdout == 'Final answer'
    assert chunks == ['first output\n']
    saved = read_sessions(root, 'run-one')[0]
    assert saved['state'] == 'SUCCESS'
    assert saved['actual_model'] == 'reported'
    assert saved['response'] == 'Final answer'
    assert 'did not report' in saved['native_cli_reason']
    assert saved['control_mode'] == 'DONE'
    text = ''.join(p.read_text() for p in owner_directory(root, 'run-one').iterdir())
    assert 'PRIVATE PROMPT' not in text and 'PRIVATE ENV' not in text
    assert read_sessions(root, 'run-other') == []


def test_nonstreaming_adapters_keep_final_output_and_restart_read_is_observational(
    tmp_path: Path,
) -> None:
    root = tmp_path / 'sessions'
    request = WorkerRequest(task='test', model='requested', worktree_path=tmp_path)
    observe_invocation(lambda _: result(), request, root=root, owner='x', role='reviewer',
                       provider='claude-code', association='review-1')
    before = {p: p.read_bytes() for p in owner_directory(root, 'x').iterdir()}
    saved = read_sessions(root, 'x')[0]
    assert saved['output'] == 'Final answer\n[stderr]\nwarning'
    assert 'did not report' in saved['native_cli_reason']
    assert saved['control_mode'] == 'DONE'
    assert before == {p: p.read_bytes() for p in owner_directory(root, 'x').iterdir()}


def test_broken_observer_storage_cannot_prevent_adapter_execution(tmp_path: Path) -> None:
    root = tmp_path / 'not-a-directory'
    root.write_text('file')
    expected = result()
    actual = observe_invocation(lambda _: expected,
                                WorkerRequest(task='t', model='m', worktree_path=tmp_path),
                                root=root, owner='x', role='worker',
                                provider='codex', association='a')
    assert actual is expected


def test_failed_invocation_remains_interrupted_and_does_not_swallow_failure(tmp_path: Path) -> None:
    def broken(_: WorkerRequest) -> WorkerResult:
        raise RuntimeError('adapter failure')
    with pytest.raises(RuntimeError, match='adapter failure'):
        observe_invocation(broken, WorkerRequest(task='t', model='m', worktree_path=tmp_path),
                           root=tmp_path / 'sessions', owner='x', role='worker',
                           provider='opencode', association='a')
    assert read_sessions(tmp_path / 'sessions', 'x')[0]['state'] == 'INTERRUPTED'


def test_log_bound_and_view_bound_are_explicit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_sessions, 'LOG_MAX_BYTES', 100)
    monkeypatch.setattr(agent_sessions, 'VIEW_MAX_BYTES', 30)
    def verbose(request: WorkerRequest) -> WorkerResult:
        assert request.on_stdout is not None
        request.on_stdout('x' * 250)
        return result()
    root = tmp_path / 'sessions'
    observe_invocation(verbose, WorkerRequest(task='t', model='m', worktree_path=tmp_path),
                       root=root, owner='x', role='worker', provider='opencode', association='a')
    saved = read_sessions(root, 'x')[0]
    assert saved['truncated'] is True and saved['view_truncated'] is True
    assert saved['output'] == 'x' * 30
    assert next(owner_directory(root, 'x').glob('*.log')).stat().st_size == 100


def test_stale_active_session_after_restart_is_not_shown_as_live(tmp_path: Path) -> None:
    folder = owner_directory(tmp_path, 'x')
    folder.mkdir()
    sid = 'a' * 32
    (folder / f'{sid}.log').write_text('saved output')
    (folder / f'{sid}.json').write_text(json.dumps({'id': sid, 'state': 'ACTIVE',
        'owner_identity': json.dumps({'host': socket.gethostname(),
            'boot_id': 'old-boot', 'pid': 99999999, 'start_ticks': 1,
            'session_id': 1, 'process_group_id': 1})}))
    assert read_sessions(tmp_path, 'x')[0]['state'] == 'INTERRUPTED'
    assert json.loads((folder / f'{sid}.json').read_text())['state'] == 'ACTIVE'
