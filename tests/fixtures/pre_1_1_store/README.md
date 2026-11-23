# Pre-1.1 store fixture

A captured copy of an owner store built on pre-1.1 `main` (`7384d36`), used by
the H36 upgrade check. It is **data**, not code: later phase-1.1 lanes delete
the readers that wrote these files, and the restored store must still start.

| Path | What it carries |
| --- | --- |
| `config/saberops/bindings.json` | The binding registry, a **PINNED orchestrator binding**, the reviewer binding |
| `config/saberops/authority.json` | The `autonomous` authority preset |
| `config/saberops/routing.json` | A BUILD chain that still names Cline (`cline/cline-pass/glm-5.3-flash`) |
| `config/saberops/control_plane.json` | Billing postures and account plans |
| `state/saberops/orchestrator.db` | The captured database (old schema, empty job history) |
| `state/saberops/access-*.json` | Captured CLI readiness and model evidence |
| `project/project-settings.json` | Review knobs (`review_mode: max`, `review_limit: 5`) and the gate command |
| `project/review-policy.json` | The project review policy (`ALWAYS_REQUIRED`) |
| `surfaces.json` | Old view names and recovery verbs the store's owner used |
| `manifest.json` | Capture metadata and what the fixture exists for |

`{python}`, `{repo}`, `{state}`, `{config}`, `{home}` and `{root}` stand for the
capture machine's paths. `tests/pre_1_1_store.py` restores the fixture into an
isolated test store and rewrites them.

**Owner settings are never rewritten on restore.** Only runtime evidence with a
short freshness window (CLI readiness, quota readings) is refreshed, because a
test host cannot inherit the capture host's.

`capture.py` records how the store was produced. It imports the lifecycle rig
and modules that later lanes delete, so it is not expected to run after those
deletions; the committed files are the fixture.
