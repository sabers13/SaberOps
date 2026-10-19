# MCP interface (Step 014 / M3)

SaberOps exposes the **Orchestrator contract** of ADR-0004 D7 as a Model
Context Protocol (MCP) server speaking JSON-RPC 2.0 over standard I/O. The
implementation is `saberops.mcp`; it is a thin adapter: every tool validates its
arguments, then calls an existing service function, and holds no authority of
its own. "LLMs advise; deterministic software decides."

```bash
# Serve over stdio (the default action):
orch mcp serve
orch mcp                      # same as `serve`
orch mcp --transport stdio    # same, explicit transport
orch mcp tools                # print the exposed tool names
orch mcp tools --json         # print names + JSON input schemas
```

| Option | Meaning |
| --- | --- |
| `--db PATH` | Owner database path (default: the canonical SaberOps state DB). |
| `--lease PATH` | Orchestrator lease file (default: `<state>/mcp-orchestrator.lock`). |
| `--ledger PATH` | Append-only event ledger (default: `<state>/mcp-events.jsonl`). |
| `--questions-store PATH` | Owner question document (default: `<state>/questions.json`). |
| `--config-ledger PATH` | Configuration decision ledger (default: `<state>/config-ledger.jsonl`). |
| `--cwd PATH` | Base directory used to discover the `.orch` / `.saberops` evidence roots. |

Server identity: `name = "saberops"`, `version = "0.4.0"`. The newest protocol
revision implemented is `2025-06-18`; `2025-03-26` and `2024-11-05` are also
served, and an unknown requested revision falls back to `2025-06-18`.

## Lifecycle

| Method | Kind | Notes |
| --- | --- | --- |
| `initialize` | request | Returns `protocolVersion`, `capabilities` (`tools`, `resources`), `serverInfo`, `snapshotSchemaVersion: 1`, `readOnly` and the lease status. |
| `notifications/initialized` | notification | Completes the handshake; no response is sent. |
| `ping` | request | Health check; allowed before or after the handshake. |
| `tools/list` | request | The twelve exposed tools with their JSON input schemas. |
| `tools/call` | request | Invoke one tool by name with an arguments object. |
| `resources/list` | request | Synthetic resources plus discovered evidence files. |
| `resources/read` | request | Read one resource by URI. |

`tools/*` and `resources/*` are refused with JSON-RPC `-32600` until
`initialize` + `notifications/initialized` have completed.

## The twelve tools

| Tool | Mutating | Key arguments | Calls |
| --- | --- | --- | --- |
| `submit_job` | yes | `repo`, `goal`, `acceptance`, `base_ref`, `priority`, `deadline`, `not_before`, `idempotency_key`, `requirements` | `saberops.jobs.service.submit_job` |
| `get_job` | no | `job_id` | `saberops.jobs.service.get_job` |
| `list_jobs` | no | `state`, `limit` | `saberops.jobs.service.list_jobs` |
| `cancel_job` | yes | `job_id`, `reason` | `saberops.jobs.service.cancel_job` |
| `capacity` | no | — | `saberops.jobs.service.capacity` |
| `submit_wave` | yes | `wave` | `saberops.wave_models` + `saberops.wave_admission` structural checks |
| `wave_status` | no | `wave` or `wave_id` | `saberops.wave_models` + `saberops.wave_executor` projection |
| `read_evidence` | no | `path` | guarded evidence reader |
| `list_questions` | no | `status`, `run_id` | `saberops.questions.OwnerQuestionQueue.questions` |
| `answer_question` | yes | `question_id_or_key`, `answer_text`, `authority` | `saberops.questions.OwnerQuestionQueue.answer` |
| `propose_config_change` | yes | `changes`, `rationale`, `proposed_by` | `saberops.config.propose_config_change` |
| `list_proposals` | no | — | `saberops.config.ConfigLedger.entries` |

`submit_job` accepts **requirements only**. `provider`, `model`, `tier`,
`billing`, `route`, `quota` and `account` are rejected at the top level and
inside `requirements` (ADR-0001 invariant 3), and the tool schema sets
`additionalProperties: false`, so an unknown field is never silently ignored.

`submit_wave` validates the wave description and refuses overlapping lane
scopes before recording anything (ADR-0004 invariant 2). Live route-posture and
Git-ancestry admission need the repository and the current posture snapshot,
which a pure wave description does not carry, so that part of admission stays
with the engine; the tool records the validated submission in the event ledger.
Nothing is spawned and no ref moves. `wave_status` projects the wave state,
lane states, slot occupancy and barrier conditions (`merge_order`, `g3_budget`,
`fix_round_budget`) from `saberops.wave_executor` / `saberops.wave_models`.

`answer_question` accepts only `authority: "ORCHESTRATOR"`. Owner authority is
not exercisable over MCP: `authority: "OWNER"` is refused with `-32010`, and a
question whose decision class is owner-only is refused the same way. An
Orchestrator can never answer its own finding, and answers are idempotent --
repeating an identical answer records a confirmation and changes nothing
(ADR-0004 invariants 6 and 13).

`propose_config_change` validates the change and returns an approval-ready
proposal. It **applies nothing**: only the owner approving through
`orch config approve <id>` or the local web surface writes the config ledger
(ADR-0004 D8.2, invariant 7).

## Never exposed

ADR-0004 D7 and REHEARSAL §3 pin a list of authorities that MCP must never
reach. The registry refuses each one *before* any handler runs, with JSON-RPC
error code `-32010` and a message naming the invariant:

| Refused operation (and aliases) | Why |
| --- | --- |
| `approve_config_proposal`, `config_approve`, `record_config_decision` | Approving a config proposal is owner-only. |
| `merge_main`, `move_main`, `fast_forward_main`, `merge_ff_only` | Moving `main` is owner-only. |
| `push`, `git_push`, `push_remote` | Pushing is owner-only. |
| `approve_quarantine`, `quarantine_approve` | Quarantine approval is owner-only. |
| `attest_posture`, `posture_attest` | Posture attestation is owner-only. |
| `run_gate`, `gate_run`, `gate_full`, `run_full_gate` | Gate execution belongs to the scripts and the Integrator. |
| `edit_source`, `write_file`, `apply_patch` | No direct tracked-source edits over MCP. |
| `delete_path`, `delete_outside_orch`, `remove_path` | Deletions outside `.orch`/`.saberops` are owner-only. |
| `read_credentials`, `get_credentials`, `read_secret`, `read_token` | Read tools never return credentials. |

Refusals are JSON-RPC **errors**; an ordinary tool execution failure is an
`isError: true` result whose `structuredContent.error` carries the code and
detail. A client can therefore always distinguish "this authority is not
offered" from "this call failed".

### Read-boundary redaction

Every value a client receives -- tool results, resources, the snapshot -- passes
through `saberops.mcp.redaction.redact_secrets`. A mapping key whose normalized
name contains a secret marker (`token`, `secret`, `api_key`, `credential`,
`authorization`, `password`, `privatekey`, ...) has its value replaced with
`[REDACTED]`, and strings lose embedded credential shapes (`sk-…`, `ghp_…`,
`AIza…`, `Bearer …`, PEM private-key headers, ...). No read tool can echo a
credential or a secret reference's value.

## Single-writer Orchestrator lease

Exactly one MCP client may be the Orchestrator **writer** at a time; every other
live client is **read-only**. The lease is a small JSON record guarded by an
exclusive `flock` during each read-modify-write.

* The first client to acquire becomes the writer; the lease is acquired during
  `initialize` and released at EOF.
* A second live client (a different, live `pid` holds the record) is read-only:
  calls to `submit_job`, `cancel_job`, `submit_wave`, `answer_question` and
  `propose_config_change` are refused with JSON-RPC error `-32020`. Its read
  tools (`get_job`, `list_jobs`, `capacity`, `wave_status`, `read_evidence`,
  `list_questions`, `list_proposals`) keep working.
* **Stale self-healing:** a record whose `pid` is dead is stale and is stolen by
  the next client (`os.kill(pid, 0)` is the liveness probe), so a crashed client
  never locks out future writers. A non-empty but unreadable/malformed record is
  treated as stale for the same reason -- a corrupt lock file must not
  permanently disable the writer role.
* Dead-PID detection is deliberately optimistic: a *live* PID is respected. The
  failure mode of respecting a recycled PID is a read-only session (safe); the
  failure mode of stealing a live lease is two writers.

The authority refusal happens **before** argument validation, so a read-only
client always gets the explicit lease error rather than a schema error.

## Versioned read snapshot and event ledger

Read payloads carry `{"v": 1, ...}` so a client can pin the shape it parses.
The `saberops://snapshot` resource assembles the lease, the ledger digest and
the bounded read sections (`list_jobs`, `capacity`, `list_questions`,
`list_proposals`) under one versioned document.

The append-only event ledger (`saberops.mcp.ledger`) records `job_submitted`,
`job_cancelled`, `wave_submitted`, `question_answered` and `config_proposed`
events as a SHA-256 hash chain. Appends are idempotent on the event key
(replaying the same event returns the existing entry; the same key with a
different payload is a typed conflict), `replay()` folds the log into state
deterministically, and a tampered chain fails closed.

## Resources

| URI | Content |
| --- | --- |
| `saberops://snapshot` | Versioned read snapshot (`v: 1`). |
| `saberops://lease` | Current lease status. |
| `saberops://ledger` | Ledger digest/counts and submitted waves. |
| `file://<abs path>` | One allowed evidence file (see below). |

Evidence reads are restricted in two independent ways: the fully resolved path
(symlinks followed) must live under `.orch/` or `.saberops/`, and the file name
must match `prompt.md`, `report.md`, `outcome.json`, `failures.md`, `gate.txt`,
`hygiene.md` or `review*.md`. Path traversal, an absolute path outside the
roots, a symlink that escapes, and a disallowed file name are all refused with
`-32030`. Reads are capped at 1 MiB and report `truncated` when the cap is hit.

## Client configuration

The server is a local stdio process; there is no network listener and no token.
Use the absolute path to the `orch` executable if it is not on `PATH`.

### Claude Code

```bash
claude mcp add saberops -- orch mcp serve
```

or, in `.mcp.json` at the project root:

```json
{
  "mcpServers": {
    "saberops": { "command": "orch", "args": ["mcp", "serve"] }
  }
}
```

### Cursor

`.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "saberops": { "command": "orch", "args": ["mcp", "serve"] }
  }
}
```

### Codex

`~/.codex/config.toml`:

```toml
[mcp_servers.saberops]
command = "orch"
args = ["mcp", "serve"]
```

Checkout development (an installed package must not shadow the edited source):

```bash
PYTHONPATH=src .venv/bin/python -m saberops.cli mcp serve
```

## Tests

`tests/test_mcp_server.py` covers the transport, lease, ledger, redaction and
evidence guard; `tests/test_mcp_contracts.py` covers the tool contracts and the
adversarial never-exposed invariants; `tests/test_cli_mcp.py` covers the CLI
subcommand.
