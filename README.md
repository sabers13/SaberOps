# SaberOps

SaberOps is a local control plane for supervised AI-assisted engineering. It
coordinates external worker tools against a Git project while retaining durable
project state, candidate provenance, validation results, and review decisions.
It is provider-independent: SaberOps does not ship a model or require a
specific provider session.

## Install

Python 3.12 or later is required.

```sh
python -m venv .venv
.venv/bin/pip install -e ".[dev]"
.venv/bin/orch --help
```

## Quick Start

```sh
orch doctor
orch models
orch run --help
orch ui --help
```

`orch ui` starts the local web UI. Runtime state, databases, worktrees, and
logs use standard XDG locations or project-local state; they are not written
into the installed package.

## Product Model

SaberOps supports bounded work in isolated Git worktrees, deterministic gates,
candidate provenance, durable run history, supervision, review policy, and
replay/developer tooling. Project identity and stored state are scoped to the
target project so operations do not silently retarget another checkout.

Connections describe how an already-selected binding may connect. Reachability
tests send no credentials and do not establish authentication or readiness.
Saving a connection alone never authorizes worker execution: discovery, exact
binding, readiness, and routing admission remain separate checks. Credential
references may be stored, but secret values are not.

Discovery provides evidence about available models. Routing uses eligible,
exact provider/model bindings and fails closed when required admission evidence
is unavailable. The routing chain is separate from the Orchestrator-model
configuration.

The Orchestrator page persists an exact eligible binding configuration. It does
not reorder worker routing. In this release, selecting that configuration does
not execute a separate Orchestrator-model process.

## Security And Privacy

SaberOps is designed for local use and keeps provider authentication with the
provider's normal mechanism. Do not place credentials in source, configuration
committed to Git, or task text. Review run commands and their project scope
before execution. Provider and model execution is never required for the test
suite or CI.

## Development

The public checks are deterministic and do not require provider accounts:

```sh
ruff check .
mypy --strict src tests
pytest -q
```

CI runs these commands on Python 3.12 without secrets.

## Repository Layout

```
src/saberops/            # runtime package
tests/                   # public capability tests
.github/workflows/       # public CI
pyproject.toml           # package metadata and tool configuration
LICENSE                  # Apache License 2.0
THIRD_PARTY_NOTICES.md   # shipped third-party notices
```

## License

Apache License 2.0. See [LICENSE](LICENSE) and
[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md).
