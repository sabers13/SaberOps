// Ephemeral native OpenCode auth hook. The key stays inside OpenCode; only
// account percentages/reset dates are written back to SaberOps. No inference.
import { writeFile } from "node:fs/promises";

export default async () => ({
  auth: {
    provider: "opencode-go",
    methods: [],
    async loader(getAuth) {
      const output = process.env.SABEROPS_GO_USAGE_OUTPUT;
      if (!output) return {};
      const emit = (data) => writeFile(output, JSON.stringify(data), { mode: 0o600 });
      try {
        const auth = await getAuth();
        if (auth?.type !== "api" || typeof auth.key !== "string" || !auth.key) {
          await emit({ error: "GO_NOT_CONNECTED" });
          return {};
        }
        const response = await fetch("https://opencode.ai/zen/go/v1/usage", {
          headers: { Authorization: `Bearer ${auth.key}`, "User-Agent": "SaberOps-Go-usage/1.0" },
          redirect: "error",
          signal: AbortSignal.timeout(8000),
        });
        if (!response.ok) {
          await emit({ error: `HTTP_${response.status}` });
          return {};
        }
        const raw = await response.text();
        if (raw.length > 65536) throw new Error("oversized quota reply");
        const body = JSON.parse(raw);
        const usage = {};
        for (const name of ["rolling", "weekly", "monthly"]) {
          const row = body?.usage?.[name];
          if (!row || typeof row.percent !== "number" || !Number.isFinite(row.percent) || row.percent < 0) continue;
          usage[name] = { percent: row.percent, resetsAt: typeof row.resetsAt === "string" ? row.resetsAt : null };
        }
        await emit({ usage });
      } catch (_) {
        await emit({ error: "QUERY_UNAVAILABLE" });
      }
      return {};
    },
  },
});
