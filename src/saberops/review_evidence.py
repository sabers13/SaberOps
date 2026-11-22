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
from pathlib import Path

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

    if gate_command:
        exit_text = "not recorded" if gate_exit_code is None else str(gate_exit_code)
        gate_body = _bounded(
            (gate_stdout or "").strip() or "(no stdout recorded)", _GATE_CHARS
        )
        gate_err = (gate_stderr or "").strip()
        if gate_err:
            gate_body += "\n[stderr]\n" + _bounded(gate_err, _GATE_CHARS)
        sections.append(
            f"Gate and test output (engine-run):\n$ {gate_command}\nexit {exit_text}\n{gate_body}"
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
