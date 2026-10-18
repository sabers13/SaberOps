"""Codex worker adapter readonly execution (ported from the console cut).

Ported from test_orchestrator_chat.py::test_codex_image_input_preserves_readonly_execution."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from saberops.models import (
    AttemptStatus,
    WorkerRequest,
)


def test_ported_codex_image_input_preserves_readonly_execution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ported from test_codex_image_input_preserves_readonly_execution."""
    import sys as _sys

    from saberops.git import GitManager
    from saberops.workers.codex import CodexAdapter

    bindir = tmp_path / "bin"
    bindir.mkdir()
    outfile = tmp_path / "child.json"
    script = (
        f"#!{_sys.executable}\n"
        "import json, os, sys\n"
        f"with open({str(outfile)!r}, 'w', encoding='utf-8') as fh:\n"
        "    json.dump({'argv': sys.argv, 'env': dict(os.environ)}, fh, sort_keys=True)\n"
    )
    stub = bindir / "stub-codex"
    stub.write_text(script, encoding="utf-8")
    stub.chmod(0o755)
    monkeypatch.setenv("PATH", str(bindir) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setattr(GitManager, "get_changed_paths", lambda _path: [])
    image = tmp_path / "reference.png"
    image.write_bytes(b"owner-picture")
    adapter = CodexAdapter(codex_bin="stub-codex")
    result = adapter.run(
        WorkerRequest(
            "Describe this picture",
            tmp_path,
            "gpt-6.1-sol",
            read_only=True,
            input_images=(image,),
        )
    )
    assert result.status is AttemptStatus.SUCCESS
    recorded = json.loads(outfile.read_text(encoding="utf-8"))
    argv = recorded["argv"]
    assert "--image" in argv and str(image) in argv
    sandbox = argv[argv.index("--sandbox") + 1]
    assert sandbox == "read-only"
