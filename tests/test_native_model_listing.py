"""Discovery regressions: no credentials or inference needed by these tests."""
from __future__ import annotations

import asyncio
import json
import sys
from email.message import Message
from pathlib import Path
from typing import Any
from urllib.error import HTTPError

import pytest

from saberops.model_access import ConnectionTestReason, ModelListingError, native_listing
from saberops.web import _fetch_openai_compatible_models, _model_list_url, _probe_api_connection


@pytest.mark.parametrize(('endpoint', 'expected'), [
    ('https://openrouter.ai/api/alpha/decisions', 'https://openrouter.ai/api/v1/models'),
    ('https://openrouter.ai', 'https://openrouter.ai/api/v1/models'),
    ('https://openrouter.ai/api/v1/', 'https://openrouter.ai/api/v1/models'),
    ('https://openrouter.ai/api/v1/models', 'https://openrouter.ai/api/v1/models'),
    ('https://example.org/v1/models/', 'https://example.org/v1/models'),
    ('https://example.org/v1/chat/completions', 'https://example.org/v1/models'),
    ('https://example.org/proxy/v1/responses', 'https://example.org/proxy/v1/models'),
    ('https://openrouter.ai.example.org/api', 'https://openrouter.ai.example.org/api/models'),
])
def test_catalog_url(endpoint: str, expected: str) -> None:
    assert _model_list_url(endpoint) == expected


def test_openrouter_discovery_uses_public_catalog_without_secret() -> None:
    def opener(url: str, headers: dict[str, str]) -> bytes:
        assert url == 'https://openrouter.ai/api/v1/models'
        assert headers == {'Accept': 'application/json'}
        return json.dumps({'data': [{'id': 'provider/model'}]}).encode()
    assert _fetch_openai_compatible_models(
        'https://openrouter.ai/api/alpha/decisions', opener=opener
    ) == ['provider/model']


def test_unauthenticated_listing_does_not_blame_configured_key() -> None:
    def opener(url: str, headers: dict[str, str]) -> bytes:
        raise HTTPError(url, 401, 'unauthorized', Message(), None)
    with pytest.raises(ModelListingError) as error:
        _fetch_openai_compatible_models('https://example.org/v1', opener=opener)
    assert error.value.reason == ConnectionTestReason.NOT_AUTHENTICATED
    assert 'NOT transmitted' in str(error.value)


def test_probe_404_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    def opener(request: Any, timeout: int) -> Any:
        assert request.full_url == 'https://example.org/v1/models'
        assert not request.has_header('Authorization')
        raise HTTPError(request.full_url, 404, 'not found', Message(), None)
    monkeypatch.setattr('urllib.request.urlopen', opener)
    result = _probe_api_connection(endpoint='https://example.org/v1')
    assert result['ok'] == 'FALSE'
    assert result['reason'] == 'ENDPOINT_UNREACHABLE'
    assert 'Authentication NOT VERIFIED' in result['detail']


@pytest.mark.parametrize('protocol', ['claude', 'copilot'])
@pytest.mark.parametrize('malformed', [False, True])
def test_native_protocol_only_requests_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, protocol: str, malformed: bool
) -> None:
    script = tmp_path / 'fake_cli.py'
    log = tmp_path / 'request.json'
    script.write_text('''import json,sys
protocol,log,malformed=sys.argv[1:4]
if protocol=='copilot':
 header=sys.stdin.buffer.readline();sys.stdin.buffer.readline()
 request=json.loads(sys.stdin.buffer.read(int(header.split(b':')[1])))
 reply={'jsonrpc':'2.0','id':request['id'],'result':{'models':[{'id':'native-model'}]}}
else:
 request=json.loads(sys.stdin.buffer.readline())
 reply={'type':'control_response','response':{'subtype':'success',
 'request_id':request['request_id'],'response':{'models':[{'value':'native-model'}]}}}
open(log,'w').write(json.dumps(request))
body=(b'not-json' if malformed=='True' else json.dumps(reply).encode())
if protocol=='copilot':
 sys.stdout.buffer.write(('Content-Length: %s\\r\\n\\r\\n'%len(body)).encode()+body)
else:sys.stdout.buffer.write(body+b'\\n')
sys.stdout.buffer.flush()
sys.stdin.buffer.read()
''')
    original = asyncio.create_subprocess_exec
    async def spawn(*argv: str, **kwargs: Any) -> Any:
        # Exercise actual pipe framing/cleanup without a provider or SDK install.
        return await original(sys.executable, str(script), protocol, str(log),
                              str(malformed), **kwargs)
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    if malformed:
        with pytest.raises(ModelListingError):
            native_listing.list_native_models('fake', protocol)
    else:
        assert native_listing.list_native_models('fake', protocol) == ['native-model']
    request = json.loads(log.read_text())
    if protocol == 'copilot':
        assert request['method'] == 'models.list'
        assert request['params'] == {}
    else:
        assert request['type'] == 'control_request'
        assert request['request'] == {'subtype': 'initialize', 'hooks': None}


def test_native_timeout_reaps_process(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    script = tmp_path / 'unresponsive.py'
    script.write_text('import sys\nsys.stdin.buffer.read()\n')
    original = asyncio.create_subprocess_exec
    spawned: list[asyncio.subprocess.Process] = []
    async def spawn(*argv: str, **kwargs: Any) -> asyncio.subprocess.Process:
        process = await original(sys.executable, str(script), **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(asyncio, 'create_subprocess_exec', spawn)
    monkeypatch.setattr(native_listing, '_LIST_TIMEOUT', 0.2)
    with pytest.raises(ModelListingError, match='timed out'):
        native_listing.list_native_models('fake', 'claude')
    assert spawned and spawned[0].returncode is not None
