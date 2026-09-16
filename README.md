# SaberOps

SaberOps is a local control plane for supervised AI-assisted engineering. It
coordinates external worker tools against a Git project while retaining durable
project state, candidate provenance, validation results, and review decisions.
It is provider-independent: SaberOps does not ship a model or require a
specific provider session.

## Status

v0.3.0 is the first functional preview. Its core Orchestrator/worker flow is
covered by hermetic integration tests that use deterministic fake adapters and
never call a real provider or model. Real-provider and larger mock-project
dogfooding is the next validation phase. This release is **not** live-provider
certified, production ready, stable, or complete.

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

## What This Release Can Do

SaberOps v0.3.0 can:

- discover models from supported connections;
- preserve exact provider / backend / model identity through discovery,
  the catalog, and binding materialization;
- select an exact Orchestrator model;
- select and configure exact worker bindings;
- use the selected Orchestrator model for bounded initial semantic guidance;
- keep worker routing separate from Orchestrator selection;
- execute workers through exact bindings; and
- enforce deterministic control-plane authority.

## Division Of Responsibility

The Orchestrator LLM provides **bounded semantic guidance** only. It is asked
once, with a bounded prompt and timeout, to produce a short piece of initial
guidance that is attached to the first worker dispatch. It does not authorize
work, choose routes, or decide outcomes.

The SaberOps deterministic control plane owns everything that must not depend
on a model's judgement:

- authorization
- exact binding validation
- routing admission
- readiness
- budgets
- Git / worktrees
- gates
- state / ledger
- acceptance / safety

Worker routing uses eligible, exact provider/model bindings and fails closed
when required admission evidence is unavailable. The routing chain is separate
from the Orchestrator-model configuration, and changing one does not silently
change the other.

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

Discovery provides evidence about available models. A discovered identity keeps
its upstream namespace, so an OpenCode-native model and a same-named sibling
from another upstream stay distinct. Effort capability is reported truthfully:
when no controllable effort tier is known, the provider's own default reasoning
behaviour is used rather than a fabricated tier.

The Orchestrator page persists an exact eligible Orchestrator binding. During
the first worker dispatch of a run, the selected Orchestrator binding is
invoked once for bounded initial guidance, and that guidance is passed into the
worker context. The Orchestrator selection remains independent of worker
routing order.

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
