# ADR-0039: OpenCode session runtime speaks the v2.3 `/api/*` server surface

- **Status:** Accepted. Implemented in lane so-ocapi.
- **Date:** 2026-11-20
- **Decider:** Saber (owner), Phase 1 defect fix after run `run_70ead49549d5_71a86a5c40`
- **Related:** [ADR-0030](0030-hybrid-native-workers.md) (hybrid native workers),
  [ADR-0031](0031-vendor-cli-self-healing.md) (vendor CLI self-healing),
  [ADR-0018](0018-vendor-cli-update-guard.md) (vendor CLI update guard)

## 1. Context

SaberOps hosts the owner's OpenCode CLI as a long-lived server for session-mode
workers. The runtime was written against the pre-2.3 HTTP surface: it launched
`opencode serve`, polled `GET /global/health` until `{"healthy": true}`, then
created sessions with `POST /session` and prompted with
`POST /session/{id}/prompt_async`.

The installed OpenCode v2.3.2 does not serve that surface. Everything is under
`/api/*`; `GET /openapi.json` lists 142 paths and none of them is a health
route. The web handler answers unknown paths (including `/global/health`) with
the SPA HTML and `200 text/html`, so the old JSON parse never succeeded and the
worker failed after 20 s with "Provider server did not become ready" before any
model prompt (run `run_70ead49549d5_71a86a5c40`). The vendor live check only
pinned `--help` text, which had not changed, so the drift was invisible.

## 2. Decision

1. **One supported server contract: OpenCode v2.3+ `/api/*`.** Readiness is
   `GET /api/info`, a cheap JSON endpoint returning `version`, `pid`, `urls`
   and `paths`. The parsed major/minor must be at least `2.3`; anything older
   or unrecognized is refused before a session is created with a fixed,
   secret-free message naming the required surface. There are no committed
   fixtures for the old surface, so no legacy compatibility branch is kept.
2. **Route mapping.**

   | Runtime need | v2.3.2 route | Shape used |
   | --- | --- | --- |
   | readiness / version | `GET /api/info` | `{version, pid, urls, paths}` |
   | create session | `POST /api/session` | body `{title, agent, model:{providerID,id,variant?}, permissions:[{action,resource,effect}], location:{directory}}`; response `{data: Session.Info}` |
   | resume/verify session | `GET /api/session/{sessionID}` | `{data: Session.Info}`; worktree from `data.location.directory` |
   | list messages | `GET /api/session/{sessionID}/message` | cursor-paginated `{data: [Session.Message.Info], cursor:{previous,next}}` |
   | send prompt | `POST /api/session/{sessionID}/prompt` | body `{id, text}`; session-level model/agent are frozen at create |
   | interrupt | `POST /api/session/{sessionID}/interrupt` | `{interrupted: bool}`; idle is confirmed through the active-session map, not the reply |
   | busy/idle | `GET /api/session/active` | `{data: {"ses…": {type: "running"}}}`; absence means idle |
   | live events | `GET /api/event` | SSE frames `{id, created, type, location, data}`; `data.sessionID` scopes them |

   Assistant output is read from the projected message (`content` text parts,
   `tokens`, `cost`, `finish`, `error`) instead of the removed `{info, parts}`
   wrapper. Completion authority stays the API: the turn ends when a completed
   assistant message exists and `/api/session/active` no longer lists the
   session.
3. **Config keys are the v2.3 names.** The inline config uses `agents` (with a
   `{providerID, model}` ref), `permissions` as a `Permission.Ruleset`, and
   keeps `enabled_providers: [<selected provider>]` for billing pinning.
4. **The managed-terminal proxy exposes the same surface.** It forwards only
   the read routes a native v2.3 client needs, scopes `/api/session*` to the one
   owned session, pins `directory` to the assignment worktree, queues native
   prompts through the existing manual-control path, and serves `/api/event`
   from the session journal. Its snapshot frame is a terminal
   `session.execution.{succeeded,failed,interrupted}`; message content is
   re-fetched by the client, so a late client never replays a stale busy state.
5. **Sign-in readiness has a zero-inference probe.** `opencode auth list
   --format json --standalone` prints integrations and credential ids (never
   secrets). `ReadinessService` treats at least one connection as
   authenticated. If the probe cannot run, the preflight remedy states plainly
   that sign-in is confirmed only by a first successful run; no auth file is
   read by SaberOps.
6. **The shipped route surface is pinned.** `tests/fixtures/vendor_cli/
   opencode/opencode-v2.3.2/openapi.json` records the v2.3.2 paths (schemas
   omitted). `python -m saberops.vendor_cli_fixtures check` starts `serve` in a
   private XDG root with a random basic-auth password, fetches
   `/openapi.json`, and fails with `SERVER-API DRIFT` when a pinned runtime
   route disappears or the retired `/global/health` reappears.

## 3. Consequences

- A future OpenCode release that moves the server surface fails the owner-run
  check and the runtime's explicit version refusal instead of hanging in
  readiness. Updating means porting the route map and refreshing the fixture.
- Session-mode workers no longer work on OpenCode versions older than 2.3.
  Direct `opencode run` dispatch is unaffected.
- The proxy is narrower than the provider API on purpose: credential, debug,
  PTY, shell, worktree and experimental routes stay unreachable from a managed
  terminal.
