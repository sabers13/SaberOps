"""Engine-produced evidence for an acceptance-clause review (ADR-0055).

The acceptance protocol requires a reviewer to answer every clause, and a
clause it cannot verify is a FAIL.  A candidate diff alone cannot show the
content of an unchanged implementation file, the output of the gate, or a
repository-wide absence.  Without that evidence honest work was rejected by a
contract that could only see the diff (N14 false negatives).

This module builds the missing, *trusted* material from the candidate tree
itself: the full content of every touched file, the in-repository modules they
import, the recorded gate output, and - for a clause that is repository-wide -
the engine's own ``git grep`` result.  The reviewer receives it as
engine-verified evidence; worker and reviewer prose never enters this text.

It is deliberately small and bounded.  Every read is against the candidate
commit (``HEAD:path`` / ``git grep ... HEAD``), never the mutable working tree,
so the evidence describes exactly the revision under review.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path, PurePosixPath

from saberops.gate_scope import (
    GateScopeError,
    check_test_retirement,
    check_write_scope,
)
from saberops.git import GitManager
from saberops.process import run_process

#: Per-file and whole-block limits: enough for a small touched module and its
#: direct imports, never an unbounded repository dump.
_PER_FILE_CHARS = 12_000
_TOTAL_CHARS = 48_000
_GATE_CHARS = 8_000
_SEARCH_CHARS = 4_000
_MAX_SEARCH_TERMS = 3

_IMPORT_RE = re.compile(
    r"^\s*(?:from\s+(?P<from_module>[A-Za-z_][\w.]*)\s+import"
    r"|import\s+(?P<import_module>[A-Za-z_][\w.]*))",
    re.MULTILINE,
)
_REFERENCE_RE = re.compile(
    r"reference\s+to\s+[`'\"]?(?P<term>[A-Za-z_][\w.]{0,63})", re.IGNORECASE
)
_RENAME_RE = re.compile(
    r"rename\s+[`'\"]?(?P<term>[A-Za-z_][\w.]{0,63})[`'\"]?\s+to\b", re.IGNORECASE
)
_BACKTICK_RE = re.compile(r"`(?P<term>[A-Za-z_][\w.]{0,63})`")
_QUOTED_RE = re.compile(r"[\"'](?P<term>[A-Za-z_][\w.]{0,63})[\"']")
_REPO_WIDE_RE = re.compile(
    r"\b(repository|repo-wide|anywhere|everywhere|all occurrences"
    r"|no reference|no remaining|remaining reference)\b",
    re.IGNORECASE,
)


def _git_show(worktree: Path, path: str) -> str | None:
    result = run_process(["git", "show", f"HEAD:{path}"], cwd=worktree, timeout=10.0)
    if not result.passed or "\x00" in result.stdout:
        return None
    return result.stdout


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n... [truncated {len(text) - limit} chars by the engine]"


def _resolve_module(worktree: Path, module: str) -> str | None:
    """Map an imported module to a readable in-repository file path."""
    top = module.split(".", 1)[0]
    if not top or top.startswith("_"):
        return None
    for candidate in (f"{top}.py", f"{top}/__init__.py"):
        if (worktree / candidate).is_file():
            return candidate
    return None


def _imported_modules(worktree: Path, path: str, content: str) -> tuple[str, ...]:
    if not path.endswith(".py"):
        return ()
    found: list[str] = []
    for match in _IMPORT_RE.finditer(content):
        module = match.group("from_module") or match.group("import_module") or ""
        resolved = _resolve_module(worktree, module)
        if resolved is not None and resolved != path and resolved not in found:
            found.append(resolved)
    return tuple(found)


def _repository_wide_terms(task: str, clauses: tuple[str, ...]) -> tuple[str, ...]:
    """Return identifiers a repository-wide clause asks the engine to search.

    Only clauses (and the goal) that explicitly talk about the repository,
    "anywhere"/"everywhere", all occurrences or "no reference ... remains"
    trigger a search; ordinary behaviour clauses do not.
    """
    terms: list[str] = []
    for text in (*clauses, task):
        if not _REPO_WIDE_RE.search(text):
            continue
        for pattern in (_REFERENCE_RE, _RENAME_RE, _BACKTICK_RE, _QUOTED_RE):
            for match in pattern.finditer(text):
                term = match.group("term").strip(".")
                if term and term not in terms:
                    terms.append(term)
    return tuple(terms[:_MAX_SEARCH_TERMS])


def _search_evidence(worktree: Path, term: str) -> str:
    result = run_process(
        ["git", "grep", "-n", "-I", "-i", "-e", term, "HEAD", "--", "."],
        cwd=worktree,
        timeout=10.0,
    )
    command = f"$ git grep -n -I -i -e {term} HEAD -- ."
    if result.exit_code == 1 and not result.stdout.strip():
        return f"{command}\nexit 1 (no matches)"
    if result.passed:
        matches = result.stdout.splitlines()
        body = _bounded("\n".join(matches), _SEARCH_CHARS)
        return f"{command}\nexit 0 ({len(matches)} matching lines)\n{body}"
    return f"{command}\nexit {result.exit_code}\n{_bounded(result.stderr, _SEARCH_CHARS)}"


_SUPPRESSION_RE = re.compile(
    r"#\s*(?:noqa|type:\s*ignore|pylint:\s*disable|ruff:\s*noqa|"
    r"pyright:\s*ignore|mypy:|eslint-disable)",
    re.IGNORECASE,
)
_CATCH_ALL_RE = re.compile(r"except\s*(?:\(\s*)?(?:BaseException|Exception)\b|except\s*:")
_SKIP_MARKER_RE = re.compile(
    r"pytest\.mark\.(?:skip|skipif|xfail)|pytest\.skip\(|unittest\.skip|"
    r"@skip\b|skipIf|expectedFailure",
    re.IGNORECASE,
)


def _is_test_path(path: str) -> bool:
    name = PurePosixPath(path).name
    return (
        path.startswith("tests/")
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def _added_lines(
    worktree: Path, base_commit: str, paths: Sequence[str] = ()
) -> dict[str, list[str]]:
    """Added lines per changed path from the committed candidate diff."""
    command = ["git", "diff", "--unified=0", f"{base_commit}..HEAD"]
    if paths:
        command.extend(["--", *paths])
    result = run_process(command, cwd=worktree, timeout=30.0)
    if not result.passed:
        return {}
    added: dict[str, list[str]] = {}
    current = ""
    for line in result.stdout.splitlines():
        if line.startswith("+++ b/"):
            current = line[6:]
            continue
        if current and line.startswith("+") and not line.startswith("+++"):
            added.setdefault(current, []).append(line[1:])
    return added


def _source_at(worktree: Path, revision: str, path: str) -> str | None:
    result = run_process(
        ["git", "show", f"{revision}:{path}"], cwd=worktree, timeout=10.0
    )
    return result.stdout if result.passed else None


def _matching_added_lines(
    added: dict[str, list[str]], pattern: re.Pattern[str], paths: Sequence[str] = ()
) -> list[str]:
    wanted = set(paths)
    matches: list[str] = []
    for path, lines in added.items():
        if wanted and path not in wanted:
            continue
        for line in lines:
            if pattern.search(line):
                matches.append(f"{path}: {line.strip()}")
    return matches


def _scope_evidence(
    changed: tuple[str, ...], touches: Sequence[str], must_not_touch: Sequence[str]
) -> str:
    if not touches and not must_not_touch:
        return (
            "Write scope: the lane declared no write scope (empty owns or a "
            "legacy plan); no write-scope evidence."
        )
    try:
        scope = check_write_scope(changed, touches, must_not_touch)
    except GateScopeError as exc:
        return f"Write scope: the declaration is unreadable ({exc}); no verdict."
    if scope.allowed:
        return (
            "Write scope: every changed path is inside the lane's declared "
            "area and outside its must-not-touch paths."
        )
    return (
        "Write scope: changed paths outside the lane's declared area "
        f"(evidence only; the reviewer decides): {scope.reason}"
    )


def _retirement_evidence(
    worktree: Path, base_commit: str, changed: tuple[str, ...]
) -> str:
    test_paths = tuple(path for path in changed if _is_test_path(path))
    if not test_paths:
        return "Test retirement: no test file changed."
    base_sources: dict[str, str] = {}
    head_sources: dict[str, str] = {}
    for path in test_paths:
        before = _source_at(worktree, base_commit, path)
        after = _source_at(worktree, "HEAD", path)
        if before is not None:
            base_sources[path] = before
        if after is not None:
            head_sources[path] = after
    try:
        retirement = check_test_retirement(base_sources, head_sources)
    except GateScopeError as exc:
        return f"Test retirement: could not parse a changed test file ({exc})."
    parts = ["Test retirement (evidence only; ordinary fixes edit tests):"]
    if retirement.removed:
        parts.append(f"- deleted tests: {', '.join(retirement.removed)}")
    if retirement.rewritten:
        parts.append(
            "- rewritten test bodies (possible weakening): "
            + ", ".join(retirement.rewritten)
        )
    if not retirement.removed and not retirement.rewritten:
        parts.append("- no test was deleted or rewritten")
    added = _added_lines(worktree, base_commit, test_paths)
    skips = _matching_added_lines(added, _SKIP_MARKER_RE)
    if skips:
        parts.append("- new skip/xfail markers: " + "; ".join(skips[:20]))
    return "\n".join(parts)


def _diff_scan_evidence(worktree: Path, base_commit: str) -> str:
    added = _added_lines(worktree, base_commit)
    suppressions = _matching_added_lines(added, _SUPPRESSION_RE)
    catch_alls = _matching_added_lines(added, _CATCH_ALL_RE)
    parts = ["Added-line scan (evidence only; the reviewer decides):"]
    parts.append(
        "- new suppression comments: "
        + ("; ".join(suppressions[:20]) if suppressions else "none")
    )
    parts.append(
        "- new catch-all exception handlers: "
        + ("; ".join(catch_alls[:20]) if catch_alls else "none")
    )
    return "\n".join(parts)


def build_review_evidence(
    *,
    worktree: Path,
    base_commit: str,
    task: str,
    clauses: tuple[str, ...],
    gate_command: str | None = None,
    gate_exit_code: int | None = None,
    gate_stdout: str = "",
    gate_stderr: str = "",
    touches: Sequence[str] = (),
    must_not_touch: Sequence[str] = (),
) -> str:
    """Render the engine-verified evidence block for one review prompt.

    Returns an empty string only when there is nothing to describe at all.
    A candidate with an empty diff still yields a block that says so, because
    "the engine saw no changed file" is itself evidence.
    """
    changed = tuple(
        GitManager.get_changed_paths_between(worktree, base_commit, "HEAD")
    )
    sections: list[str] = []
    total = 0

    file_blocks: list[str] = []
    included: set[str] = set()
    pending = list(changed)
    index = 0
    while index < len(pending) and total < _TOTAL_CHARS:
        path = pending[index]
        index += 1
        if path in included:
            continue
        included.add(path)
        content = _git_show(worktree, path)
        if content is None:
            file_blocks.append(f"--- {path} (binary or unreadable; content not shown) ---")
            continue
        for module in _imported_modules(worktree, path, content):
            if module not in included and module not in pending:
                pending.append(module)
        body = _bounded(content, _PER_FILE_CHARS)
        block = f"--- {path} (candidate HEAD content) ---\n{body}"
        if total + len(block) > _TOTAL_CHARS:
            file_blocks.append("... [further files omitted: evidence size limit]")
            break
        total += len(block)
        file_blocks.append(block)

    if changed:
        sections.append(
            "Changed files and imported modules at the candidate commit:\n"
            + ("\n\n".join(file_blocks) if file_blocks else "(no file content read)")
        )
    else:
        sections.append(
            "Changed files at the candidate commit: none. The candidate diff is "
            "empty; the engine found no file changed between the base commit and "
            "the candidate."
        )

    if changed:
        sections.append(
            _scope_evidence(changed, touches, must_not_touch)
            + "\n\n"
            + _retirement_evidence(worktree, base_commit, changed)
            + "\n\n"
            + _diff_scan_evidence(worktree, base_commit)
        )

    if gate_command:
        exit_text = "not recorded" if gate_exit_code is None else str(gate_exit_code)
        gate_body = _bounded(
            (gate_stdout or "").strip() or "(no stdout recorded)", _GATE_CHARS
        )
        gate_err = (gate_stderr or "").strip()
        if gate_err:
            gate_body += "\n[stderr]\n" + _bounded(gate_err, _GATE_CHARS)
        sections.append(
            "Gate command output (engine-run; exact command below):\n"
            f"$ {gate_command}\nexit {exit_text}\n{gate_body}"
        )

    terms = _repository_wide_terms(task, clauses)
    if terms:
        searches = "\n\n".join(_search_evidence(worktree, term) for term in terms)
        sections.append(
            "Repository-wide search run by the engine against the candidate tree:\n"
            + searches
        )

    header = (
        "Engine-verified evidence (trusted; produced by the engine from the "
        "candidate tree, not by the worker or the reviewer):\n"
        f"Candidate commit: HEAD ({base_commit} is the base)\n"
    )
    return header + "\n\n".join(sections)
