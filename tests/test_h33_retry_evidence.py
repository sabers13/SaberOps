"""H33: the retry prompt carries both gate streams, each labelled and bounded.

Fixture first: a failed check writes a warning to stderr and a pytest failure
summary to stdout.  Until this lane ``build_retry_projection`` rendered stderr
and dropped stdout whenever stderr held any text, so the summary never reached
the next attempt.

The present streams share one evidence block: half each when both are present,
the whole block for a lone stream, so both streams together cost what the
single stream used.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from saberops.context_projection import (
    EVIDENCE_BLOCK_MAX_CHARS,
    build_retry_projection,
    render_projection,
)

STDERR_WARNING = "WARNING: coverage threshold not configured; continuing"
FAILING_TEST_NAME = "test_the_answer_is_forty_two"
TRUNCATION_MARKER = "\n...[truncated]...\n"
NOTICE = "[truncated]"

#: The present streams share one evidence block: half each when both are
#: present, the whole block for a lone stream (as before this lane).
TWO_STREAM_LIMIT = EVIDENCE_BLOCK_MAX_CHARS // 2
SINGLE_STREAM_LIMIT = EVIDENCE_BLOCK_MAX_CHARS

#: The fixed line naming the failed gate and one stream label; both labels
#: ("Gate stderr excerpt:\n", "Gate stdout excerpt:\n") are the same length.
GATE_LINE = "Prior gate 'pytest -q' failed with exit code 1."
LABEL = "Gate stdout excerpt:\n"


def pruned_max(limit: int) -> int:
    """Return the longest text one pruned stream keeps: head + marker + tail."""
    return 2 * limit + len(TRUNCATION_MARKER)


#: Both labelled streams with their truncation notices, plus the newlines that
#: join them to the gate line.
MAX_FAILURE_EXCERPT = (
    len(GATE_LINE) + 4 + 2 * (len(LABEL) + pruned_max(TWO_STREAM_LIMIT) + len(NOTICE))
)


def _failed_check_outputs(scratch: Path) -> tuple[int, str, str]:
    """Run one real failing check that warns on stderr.

    The check is a script (as a project's check usually is): it writes its
    warning to stderr, then runs a failing pytest, whose summary lands on
    stdout.  Returns the recorded ``(exit_code, stdout, stderr)``.
    """
    test_file = scratch / "test_fixture_check.py"
    test_file.write_text(
        "def test_the_answer_is_forty_two():\n"
        "    assert 41 + 1 == 43\n",
        encoding="utf-8",
    )
    check_file = scratch / "check.sh.py"
    check_file.write_text(
        "import subprocess\n"
        "import sys\n"
        "\n"
        f"print({STDERR_WARNING!r}, file=sys.stderr, flush=True)\n"
        "raise SystemExit(\n"
        "    subprocess.call(\n"
        "        [sys.executable, '-m', 'pytest', '-q', '-p', 'no:cacheprovider',\n"
        "         '-p', 'no:cov', 'test_fixture_check.py'],\n"
        "        cwd=__file__.rsplit('/', 1)[0],\n"
        "    )\n"
        ")\n",
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("PYTEST_", "COV_"))
    }
    completed = subprocess.run(
        [sys.executable, str(check_file)],
        cwd=scratch,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    return completed.returncode, completed.stdout, completed.stderr


def _prior_attempt() -> SimpleNamespace:
    return SimpleNamespace(
        attempt_number=1,
        provider="fixture-provider",
        model="fixture-model",
        base_sha="base-sha",
        worker_error=None,
    )


def _retry_prompt(
    *,
    stdout: str,
    stderr: str,
    exit_code: int = 1,
) -> str:
    check = SimpleNamespace(
        gate_command="pytest -q",
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
    )
    projection = build_retry_projection(
        original_task="Make the answer forty-two.",
        attempt_idx=2,
        prior_attempt=_prior_attempt(),
        prior_check=check,
        inherited_base_sha="base-sha",
    )
    rendered = render_projection(
        projection,
        validation_policy_text="VALIDATION POLICY\nRun the configured check.",
        validation_policy_marker="VALIDATION POLICY",
    )
    return rendered.prompt


def test_retry_prompt_carries_the_stdout_failure_summary(tmp_path: Path) -> None:
    """The fixture's pytest summary reaches the retry prompt beside the warning."""
    exit_code, stdout, stderr = _failed_check_outputs(tmp_path)
    assert exit_code != 0
    assert STDERR_WARNING in stderr
    assert FAILING_TEST_NAME in stdout

    prompt = _retry_prompt(stdout=stdout, stderr=stderr, exit_code=exit_code)

    assert "Gate stderr excerpt:" in prompt
    assert STDERR_WARNING in prompt
    assert "Gate stdout excerpt:" in prompt
    assert FAILING_TEST_NAME in prompt


def test_retry_prompt_still_carries_stdout_without_stderr(tmp_path: Path) -> None:
    """A stdout-only failure keeps working exactly as before."""
    exit_code, stdout, _stderr = _failed_check_outputs(tmp_path)
    prompt = _retry_prompt(stdout=stdout, stderr="", exit_code=exit_code)

    assert "Gate stdout excerpt:" in prompt
    assert FAILING_TEST_NAME in prompt
    assert "Gate stderr excerpt:" not in prompt


def test_retry_evidence_keeps_head_and_tail_of_both_streams() -> None:
    """Each stream is pruned head and tail and labelled, with a bounded total."""
    stdout = (
        "SHORT SUMMARY: " + FAILING_TEST_NAME + " failed\n"
        + "x" * 20000
        + "\nFINAL SUMMARY LINE: 1 failed, 2 passed"
    )
    stderr = (
        "STDERR HEAD: " + STDERR_WARNING + "\n"
        + "y" * 20000
        + "\nSTDERR TAIL: traceback ended"
    )
    projection = build_retry_projection(
        original_task="Make the answer forty-two.",
        attempt_idx=2,
        prior_attempt=_prior_attempt(),
        prior_check=SimpleNamespace(
            gate_command="pytest -q", exit_code=1, stdout=stdout, stderr=stderr
        ),
        inherited_base_sha="base-sha",
    )
    excerpt = projection.failure_excerpt or ""

    # Both streams, each labelled.
    assert "Gate stdout excerpt:" in excerpt
    assert "Gate stderr excerpt:" in excerpt
    # Head and tail of both streams survive the prune.
    assert "SHORT SUMMARY: " + FAILING_TEST_NAME in excerpt
    assert "FINAL SUMMARY LINE: 1 failed, 2 passed" in excerpt
    assert "STDERR HEAD: " + STDERR_WARNING in excerpt
    assert "STDERR TAIL: traceback ended" in excerpt
    # Each stream is marked as truncated.
    assert [line for line in excerpt.splitlines() if line == NOTICE] == [
        NOTICE,
        NOTICE,
    ]
    # Both streams together keep one old stream's worth of pruned text (two
    # heads and two tails at half the block each), plus their markers.
    assert len(excerpt) <= MAX_FAILURE_EXCERPT


def test_a_lone_stream_keeps_the_whole_evidence_block() -> None:
    """Splitting the budget must not shrink a single-stream retry's evidence."""
    stdout = "HEAD: " + FAILING_TEST_NAME + "\n" + "x" * 20000 + "\nTAIL: 1 failed"
    projection = build_retry_projection(
        original_task="Make the answer forty-two.",
        attempt_idx=2,
        prior_attempt=_prior_attempt(),
        prior_check=SimpleNamespace(
            gate_command="pytest -q", exit_code=1, stdout=stdout, stderr=""
        ),
        inherited_base_sha="base-sha",
    )
    excerpt = projection.failure_excerpt or ""

    assert "HEAD: " + FAILING_TEST_NAME in excerpt
    assert "TAIL: 1 failed" in excerpt
    # Exactly the one-stream size this lane found: the whole block, one label,
    # one marker and one notice.
    assert len(excerpt) == (
        len(GATE_LINE)
        + 2
        + len(LABEL)
        + pruned_max(SINGLE_STREAM_LIMIT)
        + len(NOTICE)
    )
