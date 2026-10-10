# Scenario harness (fake providers)

Runs the real orchestrator, web UI and run lifecycle against scripted stand-ins for the
`codex` and `claude` CLIs, so workflows and failure modes can be exercised without
accounts, network access or spend. It is a test aid; nothing here ships in the package.

- `fakecli.py` – one script, two dialects (chosen by the name it is invoked as). It
  answers the readiness/auth/model probes, the worker `exec` call, and the orchestrator
  and reviewer prompts. Behaviour is chosen by tags in the task text:
  `[fake:fail]` `[fake:quota]` `[fake:malformed]` `[fake:hang]` `[fake:crashmid]`
  `[fake:noop]` `[fake:slow=N]` `[fake:review-clean]` `[fake:review-block]`.
  Failure tags apply to the worker only, so an orchestrator turn still succeeds.
  `FAKE_LOGGED_OUT=1`, or the file `$FAKE_FLAGS/logged-out` (default `/tmp/fake-flags`),
  makes the login probes report a logged-out account; the file works while a server runs.
  Every call is appended as JSON to `$FAKE_LOG`.
- `matrix.py` – 14 backend cases, each with a fresh target repo and fresh owner state;
  exits non-zero when an asserted status differs. About 35 seconds.

```bash
.venv/bin/python scripts/scenarios/matrix.py
```

To drive the web UI, put a directory containing `codex` and `claude` symlinks to
`fakecli.py` first on `PATH`, start `orch ui` with isolated `XDG_*_HOME` directories, then
in Settings → Providers refresh models and pick the fake model as orchestrator, reviewer
and worker. A message containing `add`, `fix` or `implement` makes the fake orchestrator
request work; with Parallel team selected it returns a two-task plan.

The stubs reproduce the CLI *protocols the adapters parse*, not real model behaviour, and
they prove nothing about live provider accounts or quotas.
