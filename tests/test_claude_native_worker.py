"""Native Claude dispatch contracts; no provider calls or real credentials."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from saberops.models import AttemptStatus, WorkerRequest
from saberops.process import ProcessResult
from saberops.workers import AdapterRegistry, ClaudeCodeAdapter


@pytest.fixture()
def worktree(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr('saberops.workers.claude.GitManager.get_changed_paths', lambda path: [])
    return tmp_path


def _auth() -> str:
    return json.dumps({'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'firstParty'})


def _completion(*, error: bool = False) -> str:
    return json.dumps({'type': 'system', 'subtype': 'init', 'model': 'claude-native-id'}) + '\n' + \
        json.dumps({'type': 'result', 'subtype': 'success', 'is_error': error, 'result': 'Done'})


def test_native_dispatch_and_billing_source(
    worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []
    monkeypatch.setenv('ANTHROPIC_API_KEY', 'ambient-test-value')
    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        calls.append((argv, kwargs))
        return ProcessResult(argv, 0, _auth() if 'auth' in argv else _completion(), '', 0.1)
    adapter = ClaudeCodeAdapter(runner=runner)
    def callback(line: str) -> None:
        pass
    request = WorkerRequest('Task with unicode λ', worktree, 'sonnet', timeout_seconds=30,
                            env={'ANTHROPIC_AUTH_TOKEN': 'explicit-test-value', 'CUSTOM': 'kept'},
                            on_stdout=callback)
    result = adapter.run(request)
    assert result.status == AttemptStatus.SUCCESS
    assert result.model == 'sonnet' and result.actual_model == 'claude-native-id'
    assert len(calls) == 2 and calls[0][0][-3:] == ['auth', 'status', '--json']
    argv, kwargs = calls[1]
    assert argv[0] == 'claude' and '--print' in argv
    assert argv[argv.index('--model') + 1] == 'sonnet'
    assert request.task not in argv and kwargs['input_text'] == request.task
    assert kwargs['cwd'] == worktree and kwargs['timeout'] == 29.9
    assert kwargs['on_stdout'] is callback
    assert 'on_stdout' not in calls[0][1]
    assert 'ANTHROPIC_API_KEY' in kwargs['inherit_env_exclude']
    assert kwargs['env'] == {'CUSTOM': 'kept', 'CLAUDE_CODE_DISABLE_FAST_MODE': '1'}
    assert '--bare' not in argv and '--dangerously-skip-permissions' not in argv
    assert json.loads(argv[argv.index('--settings') + 1])['forceLoginMethod'] == 'claudeai'
    assert '--setting-sources=' in argv


@pytest.mark.parametrize('login', [
    {'loggedIn': False}, {'loggedIn': True, 'authMethod': 'api_key'},
    {'loggedIn': True, 'authMethod': 'claude.ai', 'apiProvider': 'bedrock'}, None,
])
def test_no_prompt_or_api_fallback_without_subscription(worktree: Path, login: Any) -> None:
    calls: list[list[str]] = []
    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        calls.append(argv)
        return ProcessResult(argv, 0, json.dumps(login), '', 0.1)
    result = ClaudeCodeAdapter(runner=runner).run(WorkerRequest('Do work', worktree, 'sonnet'))
    assert result.status == AttemptStatus.FAILED
    assert len(calls) == 1 and 'auth' in calls[0] and '--print' not in calls[0]
    assert 'fallback is disabled' in (result.error or '')


@pytest.mark.parametrize('payload,exit_code,timed_out,incomplete', [
    (_completion(error=True), 0, False, False), ('not-json', 0, False, False),
    (_completion(), 1, False, False), (_completion(), 0, True, False),
    (_completion(), 0, False, True),
])
def test_exit_alone_never_invents_success(
    worktree: Path, payload: str, exit_code: int, timed_out: bool, incomplete: bool
) -> None:
    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        if 'auth' in argv:
            return ProcessResult(argv, 0, _auth(), '', 0.1)
        return ProcessResult(argv, exit_code, payload, '', 0.1,
                             timed_out=timed_out, termination_incomplete=incomplete)
    result = ClaudeCodeAdapter(runner=runner).run(WorkerRequest('Do work', worktree, 'sonnet'))
    assert result.status != AttemptStatus.SUCCESS


def test_reviewer_has_no_write_shell_or_external_tools(worktree: Path) -> None:
    calls: list[list[str]] = []
    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        calls.append(argv)
        return ProcessResult(argv, 0, _auth() if 'auth' in argv else _completion(), '', 0.1)
    result = ClaudeCodeAdapter(runner=runner).run(
        WorkerRequest('Review', worktree, 'sonnet', read_only=True)
    )
    assert result.status == AttemptStatus.SUCCESS
    argv = calls[-1]
    assert argv[argv.index('--tools') + 1] == 'Read,Glob,Grep'
    assert argv[argv.index('--permission-mode') + 1] == 'plan'
    assert '--allowedTools' not in argv
    assert '--strict-mcp-config' in argv


def test_registration_and_prompt_ceiling(worktree: Path) -> None:
    adapter = AdapterRegistry.default().get('anthropic')
    assert isinstance(adapter, ClaudeCodeAdapter)
    def never_run(argv: list[str], **kwargs: Any) -> ProcessResult:
        raise AssertionError('Oversized prompt must fail before any CLI launch')
    result = ClaudeCodeAdapter(runner=never_run).run(
        WorkerRequest('λ' * 5_000_001, worktree, 'sonnet')
    )
    assert result.status == AttemptStatus.FAILED


def test_native_image_inputs_use_structured_content(worktree: Path) -> None:
    import base64

    image = worktree / 'reference.png'
    image.write_bytes(b'owner-provided-picture')
    calls: list[tuple[list[str], dict[str, Any]]] = []
    def runner(argv: list[str], **kwargs: Any) -> ProcessResult:
        calls.append((argv, kwargs))
        return ProcessResult(argv, 0, _auth() if 'auth' in argv else _completion(), '', .1)
    result = ClaudeCodeAdapter(runner=runner).run(WorkerRequest(
        'Describe this picture', worktree, 'sonnet', read_only=True, input_images=(image,)))
    assert result.status is AttemptStatus.SUCCESS
    argv, kwargs = calls[-1]
    assert argv[argv.index('--input-format') + 1] == 'stream-json'
    assert argv[argv.index('--permission-mode') + 1] == 'plan'
    content = json.loads(kwargs['input_text'])['message']['content']
    assert content[0]['text'] == 'Describe this picture'
    assert content[1]['source']['media_type'] == 'image/png'
    assert base64.b64decode(content[1]['source']['data']) == image.read_bytes()
