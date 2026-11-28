# ADR-0110: A connect never leaves a half-installed setup

- **Status:** Accepted. Implemented in phase 1.1 lane H52.
- **Date:** 2026-11-28
- **Decider:** Saber (owner), phase 1.1 plan lane H52, from the H37 recheck on
  2026-11-28 ("fix it": the owner ran `claude doctor`, which reported the
  user-level SaberOps worker agent as broken and offered to delete it).
- **Related:** [ADR-0023](0023-proxy-subagents-per-cli.md) (the generated
  subagent definitions), [ADR-0030](0030-hybrid-native-workers.md) (the
  `saberops-worker` native subagent), [ADR-0089](0089-skills-tool-descriptions-and-worker-instructions.md)
  (the instruction skills), the H37 report §8 D6.

## 1. Context

`orch connect claude --project <dir> --skill` registered the MCP server in the
project's `.mcp.json`, but installed the four instruction skills and
`saberops-worker.md` under `~/.claude/skills` and `~/.claude/agents`. The
paths came from `skill_dir` and `agent_dir`, which always resolve under the
home directory.

That left two inconsistent installs. The user-level worker declares
`mcpServers: [saberops]` and `mcp__saberops__*` tools, while the `saberops`
server existed only for one project, so `claude doctor` reported the agent as
broken and offered to delete it; the owner accepted mid-run and the files were
removed. Every Claude session also listed SaberOps skills for a project-only
connection.

Reproduced on the H52 branch with a temporary HOME and project: a
project-scoped connect wrote five files under HOME and none under the project.

## 2. Decision

1. **Files follow the MCP entry's scope.** The `Agent` table records a
   project-scope skill and subagent root beside the user-scope ones
   (`project_skill_dir`, `project_agent_dir`). Claude Code uses `.claude/skills`
   and `.claude/agents`; OpenCode uses its own project layout `.opencode/skills`
   and `.opencode/agents`. `skill_path`, `agent_file_path`, `worker_file_path`,
   `install_skill`, `remove_skill`, `install_worker`, `remove_worker`,
   `install_agents`, `remove_agents` and `is_connected` take the project folder
   and resolve only inside it. `connect`, `disconnect`, `--remove` and
   `--dry-run` pass the same folder, so removal cleans exactly what the same
   scope wrote.
2. **No scope falls back to the other.** A CLI whose project scope has no
   skill or subagent format (Codex, Antigravity) refuses `--project --skill`
   with a message naming the missing scope and the user-scope command, and
   writes nothing. Removing or disconnecting at a project scope those CLIs do
   not have is refused the same way, so a user-level entry is never deleted by
   a project command.
3. **The connection check reads the same scope.** `is_connected` with a project
   reads only the project's registration file (`.mcp.json` for Claude Code,
   `opencode.json`/`.cursor/mcp.json` for the JSON agents), so the post-install
   hint in `orch connect --agents` names the same scope.
4. **The scope rule is unchanged for user connects.** Without `--project`, the
   skills, worker and proxies stay under the home directory as before.

## 3. Consequences

- A project connect writes nothing under HOME, and a user connect writes
  nothing under the project; the regression tests assert both with temporary
  directories.
- An older half-install is not migrated automatically: a user-level file from
  an earlier version is left in place, and a project connect does not touch it.
  `orch doctor` has no connection check today, so it reports nothing about it;
  a future check can name the stale file and the matching
  `orch connect <agent> --remove` command.
- Claude Code still decides on its own whether to load a project `.mcp.json`
  (`disabledMcpjsonServers`); detecting that rejection is a later lane, as is
  Antigravity's user-level MCP edit.
