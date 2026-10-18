# Worker isolation and owner-action security

This document describes the local security boundary the M4 milestone
("Isolate Workers & Protect Owner Actions") implements and how to verify it.
It is the reference for `src/saberops/sandbox/isolation.py`,
`src/saberops/sandbox/policy.py`, `src/saberops/sandbox/enforcement.py` and
`src/saberops/owner_actions.py`.

Two guarantees are in scope:

1. a **worker execution** cannot leave its assigned worktree, rewrite the
   repository git control surface, or touch owner/host configuration;
2. an **owner action** (detached Review / Accept) can only be claimed or
   executed under owner session authority, and fails closed in an automated
   worker session.

Everything below fails closed: when the answer is not a proven yes, the
operation is refused with a typed reason before any child process, model call
or repository mutation exists.

## 1. Session authority

The single vocabulary is an explicit environment marker:

| Value of `SABEROPS_SESSION_AUTHORITY` | Meaning |
| --- | --- |
| unset / empty | the local owner's own process (Web UI, `orch` CLI, tests) |
| `owner` | owner (human) session |
| `worker` | automated worker session |
| anything else | **refused** — an unknown marker is never treated as owner |

`resolve_session_authority()` (in `saberops.sandbox.isolation`) is the only
resolver. `worker_session_env()` builds the environment a worker child must
run with: it sets the `worker` marker, refuses a base environment that claims
`owner` (`WORKER_AUTHORITY_ESCALATION`), points `HOME` / `XDG_CONFIG_HOME` at
the worker scratch root, and neutralises ambient git configuration
(`GIT_CONFIG_GLOBAL` / `GIT_CONFIG_SYSTEM` to `/dev/null`,
`GIT_CONFIG_NOSYSTEM=1`, `GIT_TERMINAL_PROMPT=0`).

The default (no marker) preserves existing behaviour: the owner's Web UI and
CLI run owner actions exactly as before.

## 2. Worktree containment

`WorkerIsolationPolicy(worktree_root, scratch_roots=...)` is the deterministic
contract for one worker execution. Every worker effect resolves through
`os.path.realpath` and must land inside `worktree_root` or inside one of the
declared `scratch_roots`:

* relative paths resolve against the worktree root;
* parent traversal (`..`) is refused;
* a symlink such as `worktree/link -> /etc` resolves outside and is refused —
  the link's own location does not authorise the escape;
* owner/host configuration (`~/.config`, `~/.bashrc`, `~/.profile`,
  `~/.gitconfig`, `~/.ssh`, `~/.aws`, ..., `/etc`, `/root`) is refused with
  `HOST_CONFIG_MUTATION` even when a scratch root would otherwise cover it.

Typed refusals are a `WorkerIsolationViolation` (a subclass of
`SandboxEnforcementError`, so existing fail-closed handlers keep working) with
a `WorkerIsolationReason`:

`OUTSIDE_WORKTREE`, `OUTSIDE_SCRATCH_ROOT`, `HOST_CONFIG_MUTATION`,
`REPO_GIT_INTEGRITY_MUTATION`, `FORBIDDEN_GIT_INVOCATION`,
`CWD_OUTSIDE_WORKTREE`, `UNKNOWN_SESSION_AUTHORITY`,
`WORKER_AUTHORITY_ESCALATION`.

## 3. Git control surface

A worker may commit, check out, inspect and configure *nothing*. The
repository control surface is protected at two independent layers.

**Path level.** `assert_worker_write()` refuses writes to `GIT_INTEGRITY_DENY`
(`.git/config`, `.git/config.worktree`, `.git/hooks/**`) while leaving
`.git/refs/**`, `.git/objects/**`, `.git/index` and `.git/logs` inside the
granted scope, so ordinary commits still work.

**Command level.** `assert_git_command_allowed()` analyses the git argv:

* `git config` may only read (`--get*`, `--list`, `-l`); writes, `--local`
  value forms, `--global`, `--system`, `--file`, `--blob` and `--edit` are
  refused;
* `git remote add|remove|rename|set-url|...`, `git hook`, `git filter-branch`,
  `git credential*`, `git daemon` and friends are refused;
* `git -c` / `--config-env` (inline config can redirect `core.hooksPath` and
  `core.pager`), `--exec-path`, `--upload-pack`, `--receive-pack` are refused;
* `-C`, `--git-dir`, `--work-tree` and absolute path arguments must resolve
  inside the worktree or a scratch root;
* `git init --template` and `git clone --template` (hook installation) are
  refused.

Arguments that are data rather than paths (`git commit -m ...`,
`--grep`, `--author`, ...) are not mistaken for paths. Non-git commands are
not this guard's business: they are governed by containment, the hardened
environment and the effective `SandboxPolicy`.

**Kernel level.** `bwrap_mount_args_for_policy()` (the containment seam used
by `saberops.process.run_sandboxed_process`) adds a read-only mask over
`.git/config`, `.git/config.worktree` and `.git/hooks` for every contained
launch whenever `<repo_root>/.git` is a real directory. A denied regular
*file* is masked with an empty file (binding a directory over a file fails);
a directory target uses the empty directory mask. A deny target whose nearest
existing ancestor is not a directory can never exist (the kernel answers
`ENOTDIR` below it) and is skipped rather than breaking the launch — which is
why a linked worktree, where `.git` is a *file* naming a gitdir elsewhere, is
governed by the argv guard instead of a mask.

The mount computation is deny-wins: it can only remove authority, never add
it.

## 4. Owner actions

`OwnerActionSupervisor` claims and detaches durable Review / Accept work.
`require_owner_action_authority()` now gates both halves:

* **Claim** (`request_review` / `request_accept`) requires owner session
  authority. A worker session (or an unknown marker) raises
  `OwnerActionAuthorityError(OWNER_AUTHORITY_REQUIRED)` *before* any database
  write or `Popen`: no action row, no event, no child.
* **Execution** (`run_review_action_child` / `run_accept_action_child`) checks
  authority before it reads the persisted action row and persists nothing on
  refusal. A worker session therefore blocks immediately and cannot mutate
  owner state, reach `review_run` / `accept_run`, or mutate the target
  repository.
* **Transport.** A claimed owner-action transport origin is checked against
  the local-only allowlist via `loopback_host_allowed()`; a non-loopback
  origin raises `OWNER_TRANSPORT_NOT_LOOPBACK`.

The composed argument is that the durable action row is the owner's
capability: only an owner-authorized claim can create one, and the sandbox
keeps a worker inside its worktree, so a worker cannot reach (or forge) the
project store that holds it.

## 5. Retained Web Host guard

The early Step 020 commit `2ec8eee` added a Host-header allowlist to the local
Web guard in `src/saberops/web.py` (`_host_header_allowed`,
`_is_private_web_path`): every private route — dashboard, run pages,
report / actions / event-stream reads and the accept/reject POSTs — refuses a
foreign, duplicate or malformed `Host` with 403 before any run data is
returned or any mutation runs. `/health` and `/static/*` stay open by design,
and the synthetic `testserver` Host is accepted only inside a synthetic ASGI
test scope, never as a production bypass. That guard is retained unchanged and
its contract tests live in `tests/test_contract_web_host_guard.py`.

It is extended into the durable-action layer here: the same loopback-only
transport invariant is expressed as `loopback_host_allowed()` in
`saberops.sandbox.isolation` and enforced by
`require_owner_action_authority()` for any claimed `transport_host`. The
transport allowlist is therefore checked twice — at the HTTP boundary and
again at the authority boundary that actually performs the owner action.
`web.py` remains the transport *enforcement* authority; consolidating the two
allowlists into one shared constant is a separate, non-security-scope change
because `web.py` is outside this lane's write scope.

## 6. Threat model summary

| Threat | Refusal |
| --- | --- |
| Worker writes outside its worktree | `OUTSIDE_WORKTREE` |
| Worker escapes via symlink or `..` | `OUTSIDE_WORKTREE` |
| Worker edits `~/.bashrc`, `~/.gitconfig`, `/etc/...` | `HOST_CONFIG_MUTATION` |
| Worker rewrites `.git/config` or installs a hook | `REPO_GIT_INTEGRITY_MUTATION` / `FORBIDDEN_GIT_INVOCATION` |
| Worker runs `git config --global`, `git remote add`, `git hook` | `FORBIDDEN_GIT_INVOCATION` |
| Worker redirects hooks with `git -c core.hooksPath` | `FORBIDDEN_GIT_INVOCATION` |
| Worker claims owner authority | `WORKER_AUTHORITY_ESCALATION` |
| Unknown session marker | `UNKNOWN_SESSION_AUTHORITY` / `OWNER_AUTHORITY_REQUIRED` |
| Worker claims/executes an owner action | `OWNER_AUTHORITY_REQUIRED` (no row, no child) |
| Non-loopback owner-action origin | `OWNER_TRANSPORT_NOT_LOOPBACK` |
| Foreign `Host` on a private Web route | 403 from the retained Web Host guard |

## 7. Verification

```bash
# Adversarial isolation + owner-action authority tests
.venv/bin/python -m pytest tests/test_sandbox.py tests/test_owner_actions.py -q

# The lane gate for this change
bash scripts/lane-check.sh --base <base> \
  --also tests/test_sandbox.py tests/test_owner_actions.py
```

`tests/test_sandbox.py` includes a real bubblewrap case: the generated mount
arguments are used to launch `/bin/sh` inside the containment namespace, the
append to `.git/config` fails with `Read-only file system`, and the host file
is unchanged. It is skipped only when `bwrap` is absent from the host.

## 8. Known limitations

* The session authority marker is an environment signal. It is set by
  `worker_session_env()` for the worker seam and checked by the sandbox and
  owner-action layers; a stronger, non-forgeable signal would require the
  worker runner (`saberops.process` / the vendor worker adapters, outside this
  lane's write scope) to be the only component able to spawn a worker child.
  Until then the layered controls above are the enforcement.
* Masking a not-yet-existing `.git/config` makes bubblewrap create an empty
  mount-point file, which is what the denied path is meant to contain; this
  equals what `git` itself creates. An existing `.git/config` is never
  modified.
* Neither the argv guard nor the kernel mask protects against a worker that
  runs a *different* VCS or writes repository files directly (for example
  editing `.git/config` through a non-git program): the path-level
  `assert_worker_write()` guard and the kernel mask cover that case, and a
  host without `bwrap` cannot enforce it at the kernel — the launch fails
  closed instead (`FILESYSTEM CONTAINMENT UNAVAILABLE`).
