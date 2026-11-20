// Ephemeral OpenCode 2 plugin: read the OpenCode Go account's own usage.
//
// The credential never leaves OpenCode. This plugin asks OpenCode's v2
// integration API for the active OpenCode Go connection (falling back to the
// active OpenCode Console account), performs one read-only GET against
// OpenCode's usage endpoint, and writes only percentages and reset dates to
// the path named by SABEROPS_GO_USAGE_OUTPUT. No model prompt, no inference,
// and no secret is written back to SaberOps.
import { Plugin } from "@opencode/plugin";
import { writeFile } from "node:fs/promises";

const USAGE_URL = "https://opencode.ai/zen/go/v1/usage";

// The Go subscription is its own integration; the Console account carries the
// same quota when Go is not connected separately. Ordered deliberately: the
// credential the owner's Go prompts use comes first, so the reading always
// belongs to the account that actually runs Go.
const INTEGRATIONS = ["opencode-go", "opencode"];

// Only this exact remote code changes the message; everything else stays a
// fixed, secret-free HTTP status class.
const EXPIRED_CODES = new Set(["api_key_expired"]);

export default Plugin.define({
  id: "saberops.go-usage",
  async setup(ctx) {
    const output = process.env.SABEROPS_GO_USAGE_OUTPUT;
    if (!output) return;
    const emit = (data) => writeFile(output, JSON.stringify(data), { mode: 0o600 });
    try {
      const token = await activeToken(ctx);
      if (!token) {
        await emit({ error: "GO_NOT_CONNECTED" });
        return;
      }
      const response = await fetch(USAGE_URL, {
        headers: {
          Authorization: `Bearer ${token}`,
          "User-Agent": "SaberOps-Go-usage/1.0",
        },
        redirect: "error",
        signal: AbortSignal.timeout(8000),
      });
      if (!response.ok) {
        let expired = false;
        try {
          const body = await response.json();
          const code = body?.error?.code;
          expired = typeof code === "string" && EXPIRED_CODES.has(code);
        } catch {
          expired = false;
        }
        await emit({ error: expired ? "HTTP_401_EXPIRED" : `HTTP_${response.status}` });
        return;
      }
      const raw = await response.text();
      if (raw.length > 65536) throw new Error("oversized quota reply");
      const body = JSON.parse(raw);
      const usage = {};
      for (const name of ["rolling", "weekly", "monthly"]) {
        const row = body?.usage?.[name];
        if (!row || typeof row.percent !== "number" || !Number.isFinite(row.percent) || row.percent < 0) continue;
        usage[name] = {
          percent: row.percent,
          resetsAt: typeof row.resetsAt === "string" ? row.resetsAt : null,
        };
      }
      await emit({ usage });
    } catch (_) {
      await emit({ error: "QUERY_UNAVAILABLE" });
    }
  },
});

/// Return the saved token for the first integration that has an active
/// connection. A missing integration is skipped, never guessed.
async function activeToken(ctx) {
  for (const integrationID of INTEGRATIONS) {
    let connection;
    try {
      connection = await ctx.integration.connection.active(integrationID);
    } catch {
      continue;
    }
    if (!connection) continue;
    let credential;
    try {
      credential = await ctx.integration.connection.resolve(connection);
    } catch {
      continue;
    }
    if (credential?.type === "key" && typeof credential.key === "string" && credential.key) {
      return credential.key;
    }
    if (credential?.type === "oauth" && typeof credential.access === "string" && credential.access) {
      return credential.access;
    }
  }
  return undefined;
}
