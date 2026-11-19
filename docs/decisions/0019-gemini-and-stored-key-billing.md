# ADR-0019: Gemini runs only through Antigravity; stored keys stay off plan routes

- **Status:** Accepted (implemented in this change).
- **Date:** 2026-11-19
- **Decider:** Saber (owner), owner words quoted below
- **Related:** [ADR-0001](0001-saberops-engine-mcp-first.md) §billing
  invariants, [ADR-0011](0011-bounded-automatic-actions.md) (automatic actions
  cannot spend tokens), [ADR-0005](0005-mcp-first-engine-and-phase-2-board.md)

## 1. Context

The owner ruled on 2026-11-19:

> live gemini out we need to run it from agy only

> 5. yes unless user explicitly add an API or ask for it

SaberOps already stripped `GEMINI_API_KEY`/`GOOGLE_API_KEY` from every worker
child and never sent Antigravity into an automatic chain, but nothing refused a
Gemini-family *model* routed through OpenCode, a proxy provider or another
harness. A posture record for `opencode/google/*` would have made such a route
admissible. The same gap existed for credentials OpenCode reads from its own
`auth.json`: the stored Anthropic API key is not an environment variable, so the
env canaries could not see it, and only the absence of a posture record kept it
from billing.

## 2. Decision

1. **Gemini is Antigravity-only.** Any Gemini-family model whose provider is not
   `antigravity` is refused by routing (`parse_candidate_override`,
   `resolve_candidate_chain`) and by canonical admission
   (`GEMINI_REQUIRES_ANTIGRAVITY`), regardless of a posture record, owner
   override, snapshot or arrival path. This covers explicit
   `opencode/google/*`, proxy providers and every other harness. Antigravity
   still serves Gemini only as an explicit attended owner override; automatic
   chains never select it (ADR-0001 Amendment 1).
2. **A stored OpenCode credential cannot bill outside a plan.** Before any
   OpenCode process launches, the worker adapter inspects the credential type in
   the provider's `auth.json` structurally (type only, never the secret):
   - `opencode/anthropic/*` is refused: OpenCode has no Anthropic subscription
     sign-in, so the stored key is pay-per-token.
   - `opencode/openai/*` is admitted only when the stored credential is OAuth
     (the ChatGPT-plan sign-in).
   - Any other provider whose stored credential is an API key is refused unless
     it is backed by a packaged subscription-plan account (`opencode-go`,
     `opencode`), which is a plan credential by definition.
   - `opencode/google/*` can therefore never carry an API key, matching the
     Gemini rule above.
3. **One owner escape hatch.** The owner may explicitly enable API-key billing
   for named OpenCode providers in the owner control-plane document:

   ```json
   {"api_billing": {"providers": ["anthropic"]}}
   ```

   An empty or absent block is the default and keeps billing on plan limits. A
   malformed block fails closed, and the allowlist is part of the frozen run
   snapshot so in-run dispatch sees the same owner decision.
4. **Codex hooks stay off.** Every Codex invocation carries
   `-c features.hooks=false` on the normal and resume paths, so user-level
   hooks cannot run even if they are enabled and trusted in
   `$CODEX_HOME/config.toml`. `--ignore-user-config` remains, but the guarantee
   no longer depends on that vendor detail.

No refusal in this record weakens owner acceptance, independent review,
isolation, or the existing posture/admission guarantees; every rule is additive
and fail-closed.

## 3. Consequences

- A configured `opencode/google/gemini-*` route, or a proxy provider carrying a
  Gemini model, is refused even with a verified posture record.
- The owner's existing OpenCode Go route keeps working: its stored API key is
  the credential for a packaged subscription plan, not pay-per-token billing.
- An Anthropic key stored for OpenCode cannot bill unless the owner names
  `anthropic` under `api_billing.providers`.
- The guard is per dispatch; a credential file that cannot be read yields no
  evidence, and the route is refused (fail closed) rather than assumed safe.
