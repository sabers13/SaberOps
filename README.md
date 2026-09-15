# SaberOps

**Local AI agent orchestration, validation & observability platform.**

SaberOps is a local, provider-independent control plane for coordinating
external AI coding agents against a project treated as the source of truth.
A durable project ledger owns project state; a worker executes a bounded
work package inside an isolated Git worktree candidate; a deterministic gate
decides pass/fail; candidates carry immutable provenance; and review/repair
loops converge or stall safely. SaberOps itself is a control plane, not a
model: it does not require any single provider, model, harness, or session.

## Install

Requires Python 3.12+.

```sh
python -m venv .venv
.venv/bin/pip install .
.venv/bin/orch --help
```

For development (tests, lint, type checks):

```sh
.venv/bin/pip install -e ".[dev]"
```

## Quick start

```sh
# Show CLI help
orch --help

# Check local orchestration readiness
orch doctor

# Inspect configured model routing
orch models

# Review supervised-run options before starting a task
orch run --help

# Review local dashboard options
orch ui --help
```

## What SaberOps does

- Durable, per-project state with ledger authority and frozen run identity.
- Bounded work packages executed in isolated Git worktree candidates.
- Deterministic verification gates with captured gate receipts.
- Candidate provenance bound to project, objective, run, and base SHA.
- Clear PASS / FAIL / BLOCKED / infrastructure outcomes.
- Durable supervision, orphan reconciliation, and resumable project state.
- Structured run history and durable events.
- Optional local web dashboard (`orch ui --help`).

## Repository layout

```
src/orchestrator_mvp/   # runtime (Python package `orchestrator_mvp`, CLI `orch`)
pyproject.toml          # build metadata (setuptools)
README.md               # this file
LICENSE                 # Apache License 2.0
THIRD_PARTY_NOTICES.md  # vendored third-party license notices
```

Runtime state (databases, worktrees, logs) lives under the platform XDG
directories or the project-local `.orch/` directory — never inside `src/`.

## Configuration

SaberOps keeps runtime state in the standard XDG locations
(`XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`). Provider
credentials are never stored in this repository; each external provider
(OpenCode, Codex, Copilot, and others where supported) authenticates
through its own normal mechanism.

## License

Apache License 2.0 — see [LICENSE](LICENSE).
Third-party notices — see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
