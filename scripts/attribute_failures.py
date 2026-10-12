#!/usr/bin/env python3
"""Attribute integration evidence to lane diffs, without executing tests."""

from __future__ import annotations

import argparse
import re
import subprocess
import tempfile
from pathlib import Path

import select_tests
from gate_tools import git, junit


def lane_evidence(repo: Path, base: str, branch: str) -> tuple[set[str], set[str], bool]:
    changed = set(git(repo, "diff", "--name-only", f"{base}..{branch}").splitlines())
    # Selector reads HEAD and the working tree. A private shared clone gives it the
    # lane's exact tree/history without switching the integration checkout.
    with tempfile.TemporaryDirectory(prefix="saberops-attribution-") as directory:
        tree = Path(directory) / "tree"
        subprocess.run(
            ["git", "clone", "--quiet", "--shared", "--no-checkout", str(repo), str(tree)],
            check=True,
            capture_output=True,
        )
        subprocess.run(
            ["git", "checkout", "--quiet", "--detach", git(repo, "rev-parse", branch)],
            cwd=tree,
            check=True,
            capture_output=True,
        )
        selection = select_tests.select(
            tree,
            git(repo, "rev-parse", base),
            strategy="coverage",
            map_path=select_tests.default_map_path(repo),
        )
    return changed, {item.target for item in selection.tests}, selection.full


def attribute(
    failures: list[str],
    lanes: dict[str, tuple[set[str], set[str], bool]],
    errors: dict[str, str],
    gate_log: str = "",
) -> str:
    buckets: dict[str, list[str]] = {}
    for node in failures:
        signals: dict[str, list[str]] = {}
        file = node.split("::")[0]
        error = errors.get(node, "")
        for name, (changed, selected, full) in lanes.items():
            found = []
            if full or node in selected or file in selected:
                found.append("selects")
            if any(
                re.search(r"(?<![\w/])" + re.escape(path) + r'(?=[:\s"\']|$)', error)
                for path in changed
            ):
                found.append("traceback")
            if file in changed:
                found.append("owns-test")
            if found:
                signals[name] = found
        if len(signals) > 1:
            bucket = "interaction"
        elif signals:
            name = next(iter(signals))
            bucket = f"{name} ({'high' if len(signals[name]) >= 2 else 'low'} confidence)"
        else:
            bucket = "unattributed"
        lines = [line.strip() for line in error.splitlines() if line.strip()]
        first = next(
            (line for line in lines if re.search(r"^E\s|\b(?:\w+Error|Failed|Timeout)\b", line)),
            lines[0] if lines else "no traceback supplied",
        )
        summary = "; ".join(f"{name}: {', '.join(found)}" for name, found in signals.items())
        buckets.setdefault(bucket, []).append(f"- `{node}` — {first}; {summary or 'no signals'}")
    diagnostic = "lint/type error"
    for line in gate_log.splitlines():
        if re.match(r"^[A-Z]+[0-9]+ ", line):
            diagnostic = line.strip()
        match = re.match(r"([^\s:]+\.py):\d+(?::\d+)?:\s*(.+)", line)
        location = re.match(r"\s*(?:-->|:::)\s+([^\s:]+\.py):\d+(?::\d+)?", line)
        if not match and not location:
            continue
        if match:
            path = match[1]
        else:
            assert location
            path = location[1]
        names = [name for name, (changed, _, _) in lanes.items() if path in changed]
        bucket = names[0] if len(names) == 1 else "interaction" if names else "unattributed"
        buckets.setdefault(bucket, []).append(
            f"- lint/type `{path}` — {match[2] if match else diagnostic}; changed-file"
        )
    return (
        "\n\n".join(f"## {name}\n\n" + "\n".join(items) for name, items in sorted(buckets.items()))
        + "\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--lane", action="append", required=True)
    parser.add_argument("--failed", type=Path, required=True)
    parser.add_argument("--junit", type=Path)
    parser.add_argument("--gate-log", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    repo = Path.cwd()
    lanes = {}
    for value in args.lane:
        name, separator, branch = value.partition("=")
        if not separator or not name or not branch:
            parser.error("--lane requires NAME=BRANCH")
        lanes[name] = lane_evidence(repo, args.base, branch)
    errors = junit(args.junit).failed if args.junit else {}
    text = attribute(
        args.failed.read_text().splitlines(),
        lanes,
        errors,
        args.gate_log.read_text() if args.gate_log else "",
    )
    if args.out:
        args.out.write_text(text)
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
