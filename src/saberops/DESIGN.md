# SaberOps UI design

## Ongoing design contract

Apply [UI design principles](../../docs/ui-design-principles.md) to every frontend
change. Apple HIG informs hierarchy, disclosure, adaptability, accessible controls
and contextual feedback within the approved SaberOps visual language. These
principles guide implementation and browser verification, including small fixes.
The agent inspector keeps global terminal actions near its top, uses compact
status text, and supports resizable output and an expanded reading view. Native
Apple styling and platform-specific chrome are not required for this web app.

## Approved reconstruction direction — 5 October 2026

The owner requested the complete Apple-guided reconstruction following the
Workspace and Providers review. This authorizes UI INIT/BUILD and fixing related
visual and interaction bugs. Earlier adoption-only restrictions below describe
the historical baseline, not this reconstruction's scope.

- **References:** Apple HIG Layout (visual hierarchy and disclosure), Sidebars,
  Typography (legibility), Writing, Feedback, Color, and Accessibility. HIG is
  an important reference for principles applied to a desktop web application.
- **Reference screens:** Workspace and Providers. One labelled navigation column,
  a central task/content area, and a contextual run inspector. Configuration
  screens use the full content area. Runs have a browsable main view.
- **Priority:** understand the selected project and next action; understand
  execution/configuration status; inspect detailed evidence on demand.
- **Appearance:** light-first, with equally functional dark mode using the
  existing mode preference. Graphite Blue is the default color palette;
  Neutral Ink is the second owner-approved option. Each has paired light/dark
  colors selected independently of mode in Settings → General → Appearance.
  Neutral surfaces, opaque content, separators and consistent spacing. No simulated native
  window chrome. Success is green, unknown status neutral, warnings amber and
  real failures red, independently of the selected palette.
- **Type:** system-ui body/display, monospace identifiers. Body 14–15px, secondary
  text 12–13px, section titles 16–20px. Values are web design choices, not Apple
  native point-size requirements. Spacing: 4/8/12/16/24/32px; radii 6/10/14px.
- **Interaction:** visible focus, keyboard-accessible dialogs and run tabs,
  200% zoom/reflow, reduced motion, meaningful pending feedback, and disabled
  actions explained beside their controls. Full technical evidence remains
  available in disclosures.
- **Structure:** reusable Jinja primitives and shell with a dedicated
  `workspace.css` layer after the historical styles. Preserve `console-v2.css`
  as the approved legacy extraction. `console.js` remains the sole theme owner;
  run events and PTY retain their existing stream owners.
- **Delivery:** rebuild the current frontend directly for live iteration. React
  and a typed API remain a future decision based on interaction complexity.
  No model execution is needed for UI inspection.

Official sources: Apple HIG [Layout](https://developer.apple.com/design/human-interface-guidelines/layout),
[Sidebars](https://developer.apple.com/design/human-interface-guidelines/sidebars),
[Typography](https://developer.apple.com/design/human-interface-guidelines/typography),
[Color](https://developer.apple.com/design/human-interface-guidelines/color),
[Feedback](https://developer.apple.com/design/human-interface-guidelines/feedback),
and [Accessibility](https://developer.apple.com/design/human-interface-guidelines/accessibility).
Visual acceptance remains iterative; this records the authorized direction.

### Accepted palette choices

The owner selected Graphite Blue and Neutral Ink from the temporary standalone
color study, replacing Midnight Blue with Graphite Blue after the initial choice
and removing the original Moss palette. Both Graphite modes use the exact study
colors. Saved Midnight selections fall back to Graphite without changing mode.
The paired colors now live in `workspace.css`, using `data-palette` alongside the existing
`data-theme` mode. `console.js` owns both preferences, with the existing
pre-paint restoration in `base.html`. Browser-local preferences persist across
pages and sessions; invalid or removed palettes fall back to Graphite Blue.
Semantic success/warning/error colors retain their meanings in either palette.
Settings follows the color study's surface hierarchy: canvas background, panel
navigation and surface cards. The raised control color must not fill its main pane.
The temporary study and its preview service were removed after verification.

### Per-run settings

The Run settings dialog is removed. The composer places Bounded/Max review mode
and a positive reviewer-call count beside a circular arrow action. Max hides and
disables the count; Bounded restores it. Project policy determines whether review
is required. Other execution controls use the existing backend defaults and
project gate/data/review policies. The project-resolved training permission is
carried as a hidden field to preserve the current POST contract.

Run detail retains the task and shows a square stop action while cancellation is
allowed. It posts to the existing cancel endpoint. The single SSE owner refreshes
all cancel controls from authoritative action state and reveals the new-task
arrow once cancellation is no longer allowed. No second event stream is added.

T1/T2/T3 remain active engine concepts in this checkout: `routing_decision.py`
classifies task/package requirements, routing chains are keyed by tier/context,
`config.py` provides tier time limits, and the orchestrator gates escalation and
review using tiers. Their backend retirement is UNRESOLVED; removing UI labels
alone would not remove this behavior. No execution rules change in this UI batch.

### Live run results

The single SSE controller in `app.js` refreshes marked `data-run-refresh`
regions from the existing read-only run-detail GET after lifecycle events,
reconnection, tab selection and returning to the page. Reads are coalesced and
serialized; a response overtaken by an event is discarded. Server rendering
retains the existing escaping and canonical candidate/check/review projections.
Persisted events omitted by the named SSE subscriptions are merged into the
mounted event log without dropping newer streamed rows.

The composer, acceptance confirmations and PTY remain mounted. Refreshes preserve
expanded evidence and the selected tab; switching tabs resets the content scroll.
An unavailable snapshot leaves existing evidence visible with a retry control.
The elapsed timer reads current lifecycle attributes and stops on completion.

Overview begins with the recorded result, canonical changed-file count, candidate
folder, check and review receipts, and durable acceptance status. The matching
candidate attempt's worker response is available in a disclosure; its narrative
does not establish gate, review or acceptance authority. Completion and acceptance
remain separate, with accepted, rejected and unavailable evidence states rendered
explicitly. These UI reads never integrate a candidate or dispatch a model.

### Reconstruction verification

- GET/render checks cover the shared shell, real run history, a missing run,
  canonical account evidence and unavailable model-listing actions.
- Browser inspection covers 320/390/820/1024/1440px widths, light and dark mode,
  keyboard dialog/drawer focus, run tabs, model search and a 720px reflow viewport.
- Populated run screens use temporary inspection state; no AI task is dispatched.
  Existing SSE, terminal and owner-action authorities remain in their original owners.
- Ruff and strict mypy pass. The full suite passed 458 tests; the final affected
  regression set passed 46, including the added native-listing availability check.
- New templates and styles are included by the existing package-data globs.
  No additional runtime or frontend build dependencies are required.

## Historical adoption baseline

The following records the pre-reconstruction product and source evidence.
The approved direction above supersedes its unresolved design choices. The global `frontend-ui-workflow` skill
contains general guidance; [repository instructions](../../AGENTS.md) define scope
and gates. Open choices are explicitly UNRESOLVED.

## Product

- **Purpose:** A local control plane for supervised multi-model AI engineering.
  Models propose work; deterministic software controls execution, verification,
  provenance, and acceptance. Source: [README](../../README.md).
- **Primary user:** The repository owner/operator configuring providers, exact
  Worker/Reviewer/Orchestrator bindings, project policies, and candidate decisions.
- **Product type:** Professional developer tool; v0.3.7 dogfood/preview release.
- **Current surfaces:** Shared console shell, new-run dashboard, run inspector,
  first-run setup, access/providers, routing, reviewer, orchestrator, quota, and
  candidate Inbox. Sources: [templates](templates/) and `web.py`.

## Current architecture and style evidence

- **Rendering:** FastAPI with Jinja2Templates. Python in `web.py` builds template
  contexts; server-rendered forms and routes perform owner actions. `/static`
  serves the package's assets. There is no React/Vite frontend or frontend build.
- **Structure:** `templates/base.html` contains the navigation rail/pane, central
  workspace, workers dock, status bar, settings modal, and command palette.
  Dashboard and run-detail templates extend this shell.
- **Application CSS order:** `static/css/console-v2.css`, then
  `static/css/saberops-console.css`. Run detail also loads vendored xterm styling.
- **Application JavaScript:** `static/js/console.js` controls shell presentation;
  `app.js` and `terminal.js` also support backend-connected interactions. IDs and
  `data-od-id` hooks are existing contracts, not decorative attributes to rename.
- **Provenance:** The base template and stylesheet identify OpenDesign project
  `25e96df8-02ca-468a-9335-0ea38e57308f`, artifact
  `orchestrator-console-v2.html`, as the approved source. The referenced
  `DESIGN-HANDOFF.md` is absent from this checkout; that missing artifact is not
  treated as available design evidence.
- **Editing constraint:** `console-v2.css` says it is extracted verbatim and must
  not be hand-edited. Use the integration stylesheet for bounded additions.
  This adoption retains that constraint.
- **Observed styling:** Neutral surfaces, green OKLCH accent, muted text/borders,
  compact typography, side panes and inspector sections. These are source facts,
  not a fresh design endorsement or a completed accessibility review.

## Existing tokens

Keep [console-v2.css](static/css/console-v2.css) as the current token source; its
`:root` values have `html[data-theme="dark"]` overrides. Integration styles already
reuse these properties. No replacement `tokens.css` or new import is needed.

| Category | Existing properties / conventions |
| --- | --- |
| Surfaces and content | `--bg`, `--panel`, `--surface`, `--raised`, `--fg`, `--muted` |
| Accent and secondary treatment | `--accent`, `--accent-strong`, `--accent-soft`, `--bark`, `--bark-soft` |
| Borders and elevation | `--border`, `--shadow` |
| Typography | `--font-display`, `--font-body`, `--font-mono`; sizes are mostly inline CSS values |
| Layout and radius | `--left`, `--right`, `--radius`; spacing and other radii are mostly inline CSS values |
| Status and interaction | `--warning`, `--danger`, accent-based states; focus/hover/disabled/selected rules exist |

A systematic spacing/type/radius scale and separate comprehensive state/status
tokens are **UNRESOLVED**. Do not fill gaps with a bootstrap palette or edit the
approved extraction during adoption. Plan any consolidation in a later UI INIT,
with separately scoped changes where shared architecture is affected.

## Design intake for the next UI INIT

- **Reference screen:** UNRESOLVED. Dashboard and run inspector exist; neither is
  selected as the future reference screen by this adoption.
- **Information priority:** UNRESOLVED for that reference screen.
- **Current colour behavior:** The HTML initially declares light. The base
  template restores `saberops-theme` from localStorage, otherwise honors system
  dark preference; a user theme toggle exists. Both themes have current tokens.
- **Future default/primary colour mode:** UNRESOLVED. Preserve current behavior.
- **Current typography:** Display uses ui-rounded/Avenir Next/Segoe UI/system
  fallbacks; body uses Aptos/Segoe UI/system; mono uses SFMono-Regular/Cascadia Code/
  Roboto Mono/ui-monospace. The base body is 14px; many controls/metadata are smaller.
- **Future typography, density, spacing scale and final accent:** UNRESOLVED.
- **Product-specific accessibility/responsive targets:** UNRESOLVED. Existing
  focus styles, 44px control minima, and CSS breakpoints are evidence, not proof
  of WCAG conformance or a settled supported-device matrix.

## Direction for future work

The user-specified starting direction is a professional developer-tool UI with
Linear/GitHub-style clarity and density. Avoid the generic purple AI-product
aesthetic. This is guidance for the next UI INIT; it neither replaces the current
approved artifact nor settles SaberOps identity.

- **Final visual language and concrete reference screens:** UNRESOLVED.
- **Screenshot references:** None supplied or created for this adoption.
- **Exceptional product design requirements:** UNRESOLVED.

## Apple HIG usage

**principles-only**: clarity, hierarchy, consistency, feedback, accessibility,
typography, contrast, and restraint. Do not copy Apple visual styling or native
navigation conventions into this web tool. The global `apple-design` skill is
restricted to UI REVIEW, relevant UI INIT work, or explicit requests; it is not
part of routine BUILD/TWEAK context.

## API boundary

- **Current:** Backend-coupled Jinja templates, server-rendered forms, static JS,
  and existing JSON/SSE/WebSocket interactions. Context shape, capability/status
  conditions, routes, and action authority belong to the backend.
- **Intended later:** Frontend → typed API client → JSON API → backend services,
  planned incrementally before React migration.
- **Specific API contracts, frontend directory/tooling, migration sequence, and
  reference screen:** UNRESOLVED.
- **Mock mode:** Not used or introduced by this workflow. Existing test fixtures
  remain test infrastructure, not a product mock mode.

No product redesign, token conversion, new client/API code, React setup, or
backend behavior change is authorized by workflow adoption alone.

### Text wrapping and font study

Table content uses natural word boundaries and intrinsic column minimums.
Dense tables scroll inside their existing wrapper when the viewport is narrow;
buttons, status pills, and headers retain readable labels. Emergency breaks are
reserved for long technical content in flexible cards, not inherited by action
controls. Form labels use the existing body font rather than legacy monospace.
The user selected pairing B: locally bundled Manrope for interface text and
IBM Plex Mono for identifiers, code, and terminal output. Both font licenses ship
with the application; font loading requires no external service. Shared status
badges center their text on both axes, including when stretched by a parent row.
Scrollbars use thin, transparent tracks and subtle thumbs that become clearer
on hover or focus; forced-color mode retains system scrollbar colors.

### Worker routing cards

Routing chains are ordered cards, with a small scope subtitle instead of a
Context column. Pointer handles support mouse/touch dragging and keyboard
arrow reordering. A complete order is saved through the canonical writer;
server validation preserves membership and refuses stale client orders.
The provider catalog uses two disclosure stages: addable models first, then
the remaining models with eligibility evidence. Existing exact binding checks
and explicit enrollment remain authoritative. Removal uses a compact close
control; backend add/remove/move contracts remain available.

### Tier-only routing

Owner-requested policy: one chain each for T1/T2/T3. Peak-hour scheduling and
training-based exclusions are retired across worker, preflight and reviewer
admission, including legacy saved flags. Schema 2 uses T1.default, T2.default
and T3.default. Legacy schema 1 owner chains migrate by stable union, preserving
primary order, every selected candidate and approval evidence. Historic frozen
snapshots retain digest validation and their serialized provenance. Runtime
consumes their tier union without the former training/peak restrictions.

### Unified routing layout A

The owner selected five compact accordion rows on one Routing page:
Orchestrator, Reviewer, T1, T2 and T3. Each row summarizes its saved selection;
only one role editor opens at a time. Role pickers use canonical eligible
bindings and existing selection handlers. Worker tiers retain draggable order
and provider disclosures (available models first, then the remaining catalog).
Add actions target the expanded tier; saves return to the same role editor.
The sidebar has one Routing entry. Legacy role routes remain compatible.

### Shared routing picker and CLI usage

The model picker lives once below the five routing roles. Each discovered model
lists only targets with exact eligible bindings; T1/T2/T3 still require fresh
worker discovery evidence. Canonical role bindings that predate discovery remain
available for their own roles. Adding to a tier appends; selecting Orchestrator
or Reviewer replaces its exact selection through the existing authority.

Usage & quota detects the supported CLIs from PATH and separates native account
limits, numeric local session activity, and cross-project SaberOps call records.
Codex account/rateLimits/read and Copilot account.getQuota are metadata-only
requests through their own CLI authentication, never inference or direct use of
subscription credentials. Other CLIs show local counters where readable and
explicit unavailable account-limit evidence. Local histories are bounded samples
when scan limits are reached, with source/coverage/timestamps shown. Account
observations refresh on page entry (60-second cache) or manually; failed refresh
keeps last-good evidence marked older. Observations never overwrite routing quota
state. Owner quota mutation and sticky block controls remain available separately.

### First-use provider access

Providers distinguishes installation, native login evidence and execution support.
Copilot's Check login uses its native metadata-only auth.getStatus protocol;
unsupported versions return UNKNOWN. OpenCode credential listings are configuration,
not model access evidence. OpenCode and Antigravity expose an explicit model
selector and Verify model access form before the first task. The form discloses
account usage and the 45-second deadline, prevents repeated submission while
pending, and denies cross-site POSTs. Startup, GETs, status checks and discovery
never trigger this inference action. Native tests run outside the project with
tools denied, require a completed challenge response, and persist a sanitized
MODEL_ACCESS_TEST receipt naming the exact model. Readiness retains its existing
connection-level semantics and freshness; no model entitlement is fabricated.

### Chat quick access and orchestrator selection

The sidebar footer opens a compact workspace menu with Settings, Providers,
Routing and an expandable usage summary. The summary uses the quota page's
CliUsageService cache and native CLI observation paths. It shows each provider's
most limited reported window first; expanding a provider shows all reported
windows, reset times, source and observation time. Unavailable readings stay
explicit. Opening usage can refresh metadata; it never starts model inference,
changes routing or writes quota policy.

Workspace and run chat headers have a searchable orchestrator picker. It lists
only canonical, resolvable ORCHESTRATOR-role bindings and persists an explicit
choice through the existing orchestrator selection authority. Binding identity,
provider/account connection and CLI/API billing remain distinct. The picker
refreshes on opening and window focus so changes made in Routing are reflected.
Worker tier routing remains in Routing. Both popovers support Escape, focus
return, Tab navigation, outside-click dismissal and narrow-screen layouts.


## Ordered role routing and chat selection — 5 October 2026

Orchestrator and Reviewer now use the same compact ordered-list interaction
as T1/T2/T3: add from the shared picker, remove, drag or arrow-key reorder.
The first chosen model is primary. Exact account and binding identity remain
authoritative. Old pinned selections render as one-entry lists. Orchestrator
order uses the existing preferred-with-fallback policy; Reviewer order is in
the canonical owner binding document, with the first id retained as its legacy
primary. Explicitly clearing Reviewer routing does not restore the legacy ladder.

Chat lists only resolvable owner-picked Orchestrator bindings. Choosing one
moves it to the front while preserving every fallback. No catalog model is
automatically admitted. Provider failures and unavailable members may advance
to the next chosen binding; Orchestrator attempts share one invocation deadline,
and Reviewer attempts consume the existing review budget. A substantive review
verdict, including Block, stops the chain. Subscription calls continue through
native CLI harnesses; direct API bindings retain their exact connection.


## Native project folder chooser — 5 October 2026

“Select project” in Setup, the sidebar and Settings opens the local computer’s
folder dialog and submits the exact selected path through the existing project
selection and Git validation. Cancel leaves the selection unchanged. “Use path”
retains manual entry. Native dialogs use macOS osascript, Windows FolderBrowserDialog,
or Linux Zenity/KDialog; an unavailable desktop picker shows a manual fallback.
The dialog endpoint accepts only same-origin requests to the loopback app.


Project selection now explicitly prepares a new folder: create the directory if
needed, initialize Git when absent, and create an empty root commit for an unborn
repository. Existing commits, remotes, staged files and working files are retained.
Validation and browsing remain read-only; only the explicit selection action
bootstraps Git. Invalid existing Git metadata refuses selection without repair.


## Project workflow modes — 5 October 2026

Project policy owns gate mode, review policy, bounded/Max review mode and the
maximum reviewer-call count. The chat composer links to the active policy rather
than carrying separate review settings. Presets persist immediately; manual
changes require “Save custom settings”. Balanced is the default.

| Preset | Gate | Independent review | Review budget |
| --- | --- | --- | --- |
| Quick | Disabled | Disabled | Not used |
| Balanced | Required | Risk-adaptive | 3 reviewer calls |
| Thorough | Required | Always required | 5 reviewer calls |
| Max | Required | Always required | No owner-set review limit |
| Custom | Owner choice | Owner choice | Bounded count or Max |

Max retains runtime, attempt and failure bounds. A review call limit counts
reviewer dispatches, including fallback calls, rather than promising that many
successful repair rounds. Required gates need an explicit command or detected
Makefile gate; an empty command does not implicitly disable the gate.

The owner explicitly requested Quick and manual disabling. This introduces a
frozen owner-authorized exception to the previous mandatory validation policy:
Quick skips candidate and post-integration gates and independent review, while
retaining candidate isolation, provenance and acceptance authorization. Durable
gate evidence reports SKIPPED, never PASS, and review records OWNER_DISABLED.
Missing authorization fields on legacy runs retain required gates and the
existing review decision rules; malformed authorization refuses reconstruction.
Policy edits affect future runs only. Disabled review does not require a Reviewer
selection for Setup completion.

Setup step 6 also offers the four workflow presets as radio cards. Selection is
local until “Apply and continue” saves the project policy and opens step 7 (Ready).
When a required-gate preset has no configured/detected gate, the setup form asks
for a command and validates it before saving. Quick needs no gate command.
Manual configuration remains available through “Customize manually”. Earlier
incomplete setup steps continue to affect the Ready status.


## Conversational orchestrator — 5 October 2026

The owner requested an OpenCode-style main conversation that replies naturally
and coordinates workers. The workspace composer now starts a durable conversation;
run screens accept follow-up messages and import their objective and run evidence.
The chat header retains the exact owner-selected Orchestrator picker. Messages,
pending replies, errors, linked runs and outcomes remain in the same transcript.
Enter sends; Shift+Enter adds a line. Drafts and retry identities survive reload.
Replies scroll into view when the reader is already at the end; older messages
remain readable without forced scrolling. Layout uses existing message/theme tokens.

Model replies can suggest only respond or work. A work suggestion enters the
existing run-creation and supervised-worker authorities using the original owner
objective and follow-ups, with Orchestrator text as advisory guidance. The model
cannot set providers, validation, acceptance or publication policy. Each work
request creates a new isolated candidate; unaccepted prior candidates are not
silently integrated. Terminal outcomes are summarized once, with a factual
software explanation if the selected model cannot reply. Conversation evidence
lives separately from runs; reads never fabricate run rows or invoke models.

The failed-run Chat panel explains routing admission before implementation and
provides a Try action for exact owner-picked workers blocked on UNKNOWN readiness.
This explicit owner action uses the existing bootstrap authority and retains the
failed run's frozen checks. Automatic routing still skips UNKNOWN evidence.

Verification covers durable history, duplicate sends, interrupted replies,
question-only turns, invalid model actions, provider failures, shared project
policy, output escaping, terminal outcome deduplication, cross-site mutation
refusal and exact worker bootstrap. Real provider replies were checked with the
owner-selected Codex Orchestrator. Browser inspection covered 1440px light and
390px dark chat, pending/reload drafts, latest-reply scrolling, and the failed run.

### Conversation, activity and synchronized control

Conversation is the default view, with Split view and All consoles preserving
access to agent output. Each reply has copy and optional expansion controls;
long replies collapse without losing text. One composer shows the current
recipient. Technical session metadata is disclosed on demand. Follow live does
not steal focus, drafts survive polling, and console scroll positions remain.

Captured calls use private bounded transcripts and an execution-owner identity.
Legacy calls show recorded results. Read-only browser/desktop viewers remain
separate from the synchronized CLI. That CLI routes messages through the same
coordinator and chat store; arbitrary externally opened vendor TUIs are outside
the sync contract. Desktop launches use fixed argv and a server-scope loopback
URL, never a shell or a caller-controlled Host destination.

Native Codex, OpenCode and Claude account sessions persist reported session IDs.
Only successful matching orchestrator sessions are reused. Live takeover uses a
durable ACTIVE → REQUESTED → MANUAL → RESUMING state machine. The execution
owner confirms child process-group extinction before acknowledging MANUAL.
Manual turns are serial, preserve frozen adapter policy and share the original
absolute invocation deadline. Resume verifies branch and revision; dirty edits
remain for the original task to inspect. Reviewer resume starts a fresh formal
review. Expiry, failed interruption, changed revision or a dead owner fails closed.
A disconnected terminal never automatically resumes automation. Worker/reviewer
messages have scoped chat roles so they cannot become orchestrator objectives.

The Total changes card uses the same terminal diff component as file cards, with
full numstat counts independent of bounded patch display. Binary and truncated
content is identified. Expansions survive refresh using separate total/file keys.
Diff reads never execute external diff drivers or textconv. Acceptance remains
under existing project policy.

## Applied chat prototype — 7 October 2026

The owner requested applying the temporary port-8767 prototype to the main app.
The conversation pages now use its monochrome canvas, compact history sidebar,
Chat/Work switch, 740px transcript, right-aligned user bubbles, rounded composer,
and on-demand work inspector. Both themes and mobile navigation use the existing
shell controller. Setup, connections, exact model selection, routing, project
policy, native control and run history retain their existing authorities.

This adopts the prototype into the current FastAPI/Jinja frontend. It does not
introduce a React build or a second execution/state owner. The prototype's
assistant-ui runtime and copied LibreChat/Emdash helpers remain in the temporary
React study; their interaction patterns informed this integration. Candidate
diffs and checks come from SaberOps' canonical server-rendered run projections,
not the prototype's sample evidence or Myers helper. The inspector preserves
existing acceptance/rejection confirmation requests and supports multiple runs
in one conversation. Work uses configured workers and project policy; the
prototype's simulated Solo execution, connection creation and attachment entries
are not advertised as implemented capabilities.

The new UI explicitly sends `chat` or `work` with each owner message. Per-turn
mode is durable in an additive `chat_turn_modes` table, without resetting history.
Chat cannot dispatch workers even when the provider returns action=work. Shared
native replies use the original message's mode, including when another turn is
queued. Existing API/terminal clients that omit mode retain their legacy `auto`
behavior. Start work reviews an editable brief and submits it in the same chat.

Live replies read bounded, current-turn provider events from the existing session
journal. Claude partial-message output, Codex reported item updates and direct
API reported text become readable previews; raw stderr is not rendered in chat.
The JSON orchestrator envelope is removed before display. Reasoning appears only
when reported by the provider, with elapsed time and actual activity while
waiting. Providers may emit coarse updates or final text rather than token deltas.
App-owned replies can be stopped through their existing process interruption
callback; a stopped turn cannot dispatch a late work result. Native terminal
control stays with its existing session owner. Interrupted requests never replay.

Assistant Markdown uses locally bundled Marked 17.0.1 and DOMPurify 3.3.1 with
restricted tags/attributes, raw HTML rendered as text, remote images disabled,
and safe link protocols. Original license notices are in `static/vendor/chat`.
User messages remain escaped plain text. Drafts and retry identities survive
reload; reading earlier messages does not force scrolling to the end.

Verification: all affected conversation/progress/session/Claude/picker checks,
Jinja compilation, JavaScript syntax, Ruff and strict mypy. Live browser checks
cover saved history and real candidate evidence, model picker, modes, editable
brief, search, draft reload, keyboard/sidebar controls, Markdown safety, scrolling,
themes and 320/390/760/1024/1440px layouts. Browser-only intercepted responses
exercise partial-reply/Stop presentation without invoking a provider or changing
owner data. The full suite recorded 801 passes and one inherited native-Claude
permission-mode expectation failure; the same failure was reproduced against the
pre-adoption source snapshot. No unrelated permission policy was changed.

## Project folders and run conversations — 9 October 2026

Run conversations use the same chat canvas, Markdown, composer, mode switch and
on-demand inspector as standalone chats. Runs open on Chat; explicit evidence
tab links still work. The existing evidence panels, actions, SSE owner and
terminal monitor remain available. A linked conversation has one visible
composer in Chat. Switching evidence tabs preserves its draft and reading position.
The first message on a run without an existing conversation also carries the
selected Chat/Work mode; clients that omit mode retain their existing auto behavior.

The history sidebar groups chats and runs together in repository folders, with
different icons, current-page selection, status hints, search and saved disclosure
state. Same-named repositories show their parent paths for distinction. History
reads use persisted project markers and read-only SQLite connections; they do not
create project runtimes, change the active project or compute Inbox lifecycle
projections. Listings are bounded to 50 runs and 20 chats per repository and
cached for three seconds on HTML requests. Session/chat polling skips that scan.
Choosing Use project remains an explicit existing project-selection action.

Runs have an additive nullable `display_title` independent of the original task
and identifier. The orchestrator chooses a short title in its existing bounded
startup guidance call, before the first implementation dispatch. The response
contains `title` and `guidance`; legacy plain-text responses remain supported.
Titles now retain action and purpose, up to ten words/80 characters, and omit repository names/paths.
No additional model invocation is introduced. Unavailable or malformed naming,
or a failed title save, leaves a short objective-derived label. Historical runs
receive durable labels on writable startup, preserving objectives and avoiding model calls on GET.
Chat run cards and the linked-run picker use the same display labels.

Validation covers additive migration, title persistence, one-call naming and
fallbacks, same-named repository isolation, read-only project-local history,
bounded per-repository listings and chat/run rendering. Browser checks cover
320/390/760/1024/1440px in both themes, search, evidence tabs, inspector, Markdown,
draft and scroll preservation. Provider naming is tested with controlled results;
visual inspection does not launch provider work or mutate owner execution state.

Ruff, strict mypy, the four fast-gate checks and affected tests pass. The full
suite recorded 823 passes and the inherited native-Claude permission-mode
expectation failure (`plan` versus `default`), reproduced against the source
snapshot from before these changes. Final Chat/Work route checks pass separately.

## Shared navigation, balanced naming and Chat/Work separation — 9 October 2026

Every page uses the same project sidebar, including Connections, Routing, Setup,
Quota and run history. Shared search and folder disclosure work on those pages.
The previous duplicate navigation entries have been removed. Conversation pages
retain their chat canvas; configuration pages retain their existing forms.

Chat is a plain conversation space. Run status, evidence tabs, run actions,
agents, implementation cards, native command records and the inspector appear
in Work. Changing modes preserves conversation history and drafts; Chat closes
work drawers and returns to the conversation panel. Work conversation links use
`#work`, preserving the selected mode on reload. Worker and reviewer messages
remain saved and accessible in Work. Copy and View run use accessible icons with
labels and tooltips, including for messages received during polling.

Run naming is part of creation and the existing startup guidance call. Labels
retain action, subject and task purpose, capped at ten words/80 characters.
The model sees recent titles from the same repository and is asked for a distinct
name. SQLite reserves case-insensitive names inside a write transaction; identical
objectives receive stable numbered suffixes. Existing unnamed/bare application
run labels are upgraded on writable database startup. Conversation labels use
full initial prompts and a Discuss prefix, with a one-time legacy migration.
Original objectives, message text, run identity and execution state are preserved.
Sidebar labels wrap to two lines rather than hiding the task behind ellipses.

Validation for this iteration: 85 affected tests, the UI fast gate, Ruff and strict
mypy pass. Live browser checks cover both themes and 320/390/760/1024/1440px,
Chat/Work switching and reload, retained drafts, evidence deep links, inspector
visibility, native record separation, saved distinct names, message icons during
polling, clipboard copying, and shared navigation/search on owner pages.


## Composer, contrast and file inputs — 9 October 2026

Dark Chat and Work use a black canvas with white text and distinct dark surfaces.
A restrained teal accent marks selected navigation, success tags and primary actions
(#5de0cb in dark appearance, #087f70 in light appearance). Warning and error colors
keep their existing meanings. Message bubbles keep neutral surfaces so color stays
a small accent. Light appearance remains supported.

The orchestrator model picker sits immediately beside the centered send arrow in
both conversation and evidence composers. Its single shared panel opens above the
composer without clipping. The redundant All agents and All consoles buttons are
removed; inspector selection, per-agent focus and shared tmux controls remain.
Start work is labeled Review brief: it opens an editable brief; submitting the brief
continues through configured workers, gates and review. Chat remains discussion-only.

The plus button accepts up to five files, 5 MB each: PNG/JPEG/WebP/GIF, PDF, DOCX,
UTF-8 text and code. File selection, image paste and drag/drop use the same upload
route. Previews can be removed before sending; drafts survive reloads. Accepted
messages retain their original file names, download links and image thumbnails in
SQLite-backed history. Failed sends retain the draft. Original bytes live inside the
active profile's private attachment directory, with random storage names.

App-owned Codex requests use --image; Claude requests use structured stream-json
image blocks; direct API requests use image_url data URLs. Images require a model
with vision support. Other CLI adapters receive staged reference file paths.
PDF extraction uses pdftotext when available; DOCX uses bounded standard-library
XML extraction. Extraction failures retain the original and identify unavailable
text. Text previews are bounded to 12,000 characters and the first 30 PDF pages.
Native shared chat keeps its existing single provider owner and supplies file paths
and bounded text references; its visible messages retain the owner's original prose.
Uploads and embedded content cannot change Chat/Work authority or model bindings.

Validation covers durable and atomic message binding, rejected file types and sizes,
preview/removal/reload, image transport in Codex/Claude/direct API, native queue failure
and discussion-only dispatch, responsive composers and both appearances. The existing
native-session permission-mode expectation failure (plan versus default) remains
unchanged. Ruff, strict mypy and the UI fast gate pass.


## Work selection and composer Stop — 9 October 2026

Project folders label their displayed counts separately as runs and saved chats.
Failed runs use a cross rather than an ambiguous dot. The test repository retains
six runs (three completed, three failed) and five conversation threads; no saved
history is deleted or merged. Run links open Work; conversation links open Chat.

Header utilities align to the right edge of the conversation canvas. The redundant
three-dot menu, whose sole item was Cancel run, is removed. Selecting Work opens
the inspector (a drawer on narrow screens); Chat closes it. Selecting an agent
expands and scrolls to its console without changing native session ownership.

In Work, an active run replaces the composer arrow with a square Stop button.
It invokes the existing cancellation endpoint for that exact run and restores the
arrow from authoritative action state. Draft text is preserved. Rejected stop
requests display an error and allow retry; duplicate clicks are coalesced. The
conversation composer and evidence composer share this behavior. On a chat page,
the control follows its latest linked run. In Chat it remains Stop reply, using
the existing reply-cancellation endpoint. Read-only action polling never dispatches
or cancels work. Existing detailed run actions and acceptance remain available.

Validation includes 54 affected checks, the UI fast gate, Ruff and strict mypy.
Live browser checks cover both appearances and 320/390/760/1024/1440px, right-edge
alignment at 1161px, inspector selection and scrolling, primary/evidence run Stop,
Chat reply Stop, completed-run arrow restoration and rejected-stop draft retention.
Cancellation checks use controlled responses, preserving all owner runs and chats.

## Separate Chat and Work threads — 9 October 2026

Chat and Work are durable conversation kinds. Each space lists its own project
history; the folder count labels both kinds explicitly. New chat always discusses,
even if the model requests implementation. New work uses the existing governed
worker flow. The top switch navigates between spaces rather than changing a saved
conversation's authority. Work links back to its original chat when available.

Turn into work reviews an explicit brief, keeps the source conversation, and creates
a separate linked Work conversation. It snapshots the last 20 discussion messages
(up to 4,000 characters each), including attachment references, as reference context.
The reviewed brief supplies the new objective; results and follow-up work stay in
Work. Conversion identifiers prevent duplicate threads and duplicate dispatch on
retry. Rejected sends retain the draft. Uploaded content cannot change authority.

At startup, finished idle legacy runs receive separate Work conversations containing
their own objective and explicitly tagged replies, or a saved evidence summary.
Original message rows are preserved exactly. Original links are recorded separately;
canonical run links point to the dedicated Work thread. Linked Work conversations
are represented by their run in the sidebar, avoiding duplicate history rows.
Active runs, replying conversations, native channels, and manual owners are left
alone. Standalone idle legacy conversations become plain Chat. Migration is
idempotent and invokes no providers. The inspector's duplicate collapse button is
removed; the main header toggle and mobile drawer close remain available.

Git authority is unchanged: candidate worktrees/branches start from each run's frozen
base, changes are committed and checked, and acceptance requires a clean checkout
still at that base. Completed work is not accepted automatically. Candidate branch
publication is off by default and requires the run's explicit publishing decision;
it never grants merge authority. Starting Work neither accepts another run's candidate
nor imports its unaccepted files. Concurrent candidates require resolving a changed
base before acceptance.

Validation: 80 affected checks, the UI fast gate, Ruff and strict mypy pass.
Browser checks cover Chat/Work navigation, source links, reviewed conversion and
rejected-send recovery, scoped Stop controls, both appearances and five widths
from 320 to 1440 pixels. Original owner message rows, six run records and Git
HEAD/status/refs match the pre-migration snapshot. Browser mutations use controlled
responses; no owner provider run, cancellation, acceptance or push was triggered.

## Consistent Work console and navigation — 9 October 2026

The right sidebar uses the existing shared agent console dock on both Work
conversation pages and run pages: tmux actions, agent selection, Follow live and
expandable console cards. Remove the alternate Activity/Changes/Checks/Console
sidebar and its HTML-cloning script. Evidence and acceptance remain in the existing
run tabs and controls; they are not copied into a competing sidebar.

A linked Work conversation URL redirects to its latest valid run's Work view.
Sidebar entries, related-work links and saved URLs therefore converge on the same
view with the correct project-history selection. An unbound Work conversation keeps
the same console dock while awaiting its first run. When polling observes its run,
it navigates to the canonical view after the reply completes, preserving the shared
conversation draft/attachment keys. Original Chat URLs continue to open Chat.
Missing run links cannot redirect to a stale earlier run or unrelated project.
This changes navigation and presentation only; execution and acceptance authority,
conversation identities, history and Git ownership remain unchanged.

Validation: 59 affected tests, the UI fast gate, Ruff and strict mypy pass.
Live browser checks cover all six canonical Work routes, original Chat and related
links, agent selection, both themes at 320/390/760/1024/1440 pixels and enlarged UI.
Loaded completed/failed runs show their own saved consoles. An isolated pending-Work
page with controlled GET responses verifies first-run navigation preserves text and
file drafts. Owner conversations, messages, attachments, runs/attempts/reviews and
Git HEAD/status/refs match the pre-change snapshot. No provider calls or owner
run/acceptance/terminal actions were started during validation.
