# consensusd

`consensusd` is a local, production-minded consensus review daemon for agent-infra workflows. Codex CLI stays the control surface, while `consensusd` owns orchestration, durable state, audit events, MCP tools, and the approval gate before Ralph handoff.

## Architecture

- Codex CLI calls local MCP tools exposed by `consensusd`.
- `consensusd` runs a durable SQLite state machine and append-only event log.
- Mock or subprocess Codex/Kimi runners negotiate until consensus is locked.
- In editable e2e mode, Codex applies scoped repo revisions between Kimi review rounds and records the revision as durable evidence.
- Phase and heartbeat events are appended for proposal, review, revision, and OMX generation so Codex can show progress without dumping the whole transcript.
- Codex then generates an OMX implementation plan.
- The daemon pauses at `AWAITING_HUMAN_APPROVAL`.
- Approval records a mock Ralph handoff packet for v1.

The daemon binds to `127.0.0.1` by default and refuses non-localhost binds. Tool permissions are enforced server-side from bearer tokens or a trusted internal caller role. Agents cannot spoof roles with normal tool arguments.

## Install

```bash
uv sync --extra dev
```

## Start The Daemon

For local Codex use, prefer the background launcher. It starts the localhost MCP endpoint and writes logs/PIDs under `.consensusd/`, so you do not need a second terminal:

```bash
uv run consensusd up --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit
```

Stop it with:

```bash
uv run consensusd down --db .consensusd/consensusd.sqlite
```

If you want to see the server logs live, you can still run the foreground server:

```bash
uv run consensusd init
uv run consensusd start --project-root . --db .consensusd/consensusd.sqlite
```

The default official MCP streamable HTTP URL is:

```text
http://127.0.0.1:8787/mcp
```

For local development, default tokens are:

```bash
export CONSENSUSD_CONTROL_TOKEN=dev-control-token
export CONSENSUSD_CODEX_TOKEN=dev-codex-token
export CONSENSUSD_KIMI_TOKEN=dev-kimi-token
```

Harden these before production use.

## Configure Codex MCP

Copy the shape from [examples/codex.config.toml](examples/codex.config.toml). Control-surface tools should use `CONSENSUSD_CONTROL_TOKEN`:

```toml
[mcp_servers.consensus]
url = "http://127.0.0.1:8787/mcp"
required = true
tool_timeout_sec = 120
bearer_token_env_var = "CONSENSUSD_CONTROL_TOKEN"
enabled_tools = [
  "start_consensus_review",
  "get_consensus_status",
  "get_consensus_brief",
  "watch_consensus_progress",
  "refresh_consensus_lease",
  "get_consensus_transcript",
  "cancel_consensus_review",
  "approve_ralph_handoff"
]
```

For a local-only end-to-end smoke test where the client cannot send bearer headers, `consensusd up` starts fixed-role localhost endpoints by default:

```bash
uv run consensusd up --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit
```

This starts:

- `http://127.0.0.1:8787/mcp` as `control_surface`
- `http://127.0.0.1:8788/mcp` as `kimi_reviewer`

You can also start one foreground fixed-role endpoint manually:

```bash
uv run consensusd start \
  --project-root . \
  --db .consensusd/consensusd.sqlite \
  --dev-auth-role control_surface
```

Then use [examples/codex.no-token.config.toml](examples/codex.no-token.config.toml), which omits `bearer_token_env_var`. This is localhost-only development mode.

If both Codex and Kimi cannot send bearer headers and you want foreground logs, run separate localhost endpoints with fixed roles against the same SQLite database:

```bash
# Codex control-surface endpoint
uv run consensusd start \
  --project-root . \
  --db .consensusd/consensusd.sqlite \
  --port 8787 \
  --dev-auth-role control_surface

# Kimi reviewer endpoint
uv run consensusd start \
  --project-root . \
  --db .consensusd/consensusd.sqlite \
  --port 8788 \
  --dev-auth-role kimi_reviewer
```

Use [examples/codex.no-token.config.toml](examples/codex.no-token.config.toml) for Codex and [examples/kimi.no-token.mcp.json](examples/kimi.no-token.mcp.json) for Kimi. Different ports provide the server-side caller role; no tool accepts a spoofable role argument.

## Invoke From Codex

Ask Codex:

```text
Start a consensus review for the current diff. Objective: safely refactor the auth boundary. Pause before Ralph handoff.
```

Codex should call:

```text
start_consensus_review(objective, project_root, mode="approval-gated", lease_mode="attached", lease_ttl_seconds=90)
```

The tool returns a `run_id`, lease metadata, and a watch command. Attached leases are the Codex CLI UX guardrail: while Codex is actively waiting, `watch_consensus_progress(..., refresh_lease=true)` keeps the run alive. If the Codex chat/tool wait is interrupted and no heartbeat refresh arrives, consensusd cancels the run and terminates active Codex/Kimi subprocesses.

For user-facing progress, ask Codex for a brief. It should call:

```text
get_consensus_brief(run_id)
```

The brief includes the current phase, latest Kimi verdict, Codex revision files, guardrail violations, OMX/context bridge paths, and the next action.

If the MCP server is not already connected in the current Codex session, Codex can run the whole approval-gated flow as a local command without a server terminal:

```bash
uv --project /path/to/codex-kimi-consensus run consensusd review \
  "safely refactor the auth boundary; pause before Ralph handoff" \
  --project-root . \
  --db .consensusd/consensusd.sqlite \
  --runner-mode codex-kimi-edit
```

This creates the run, advances the local orchestrator, watches brief-driven progress, and prints a final brief plus Kimi/Codex negotiation summary. It does not dump the full transcript unless you add `--show-transcript`. `--runner-mode codex-kimi-edit` uses real `codex exec` for proposals, scoped revision passes, and OMX generation, plus real local Kimi CLI for architect review. `--runner-mode codex-kimi` is read-only: Codex plans and Kimi reviews, but Codex will not edit files between rounds. `--runner-mode codex` is a partial smoke that keeps Kimi deterministic.

## CLI

```bash
uv run consensusd up --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit
uv run consensusd review "OBJECTIVE" --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit
uv run consensusd review "OBJECTIVE" --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit --show-transcript
uv run consensusd brief RUN_ID
uv run consensusd status RUN_ID
uv run consensusd watch RUN_ID --interval 5
uv run consensusd transcript RUN_ID
uv run consensusd approve RUN_ID
uv run consensusd cancel RUN_ID
uv run consensusd down --db .consensusd/consensusd.sqlite
```

`watch` exits at terminal states or `AWAITING_HUMAN_APPROVAL`.

`brief` is the friendlier default for Codex/App UX. It summarizes the latest phase events and points to the OMX plan/context bridge instead of printing the full SQLite transcript.

For MCP-driven reviews, Codex should use `watch_consensus_progress(run_id, wait_seconds=30, interval_seconds=5, refresh_lease=true, lease_ttl_seconds=90)` rather than raw sleeps. The optional CLI `watch` command also refreshes attached leases by default; pass `--no-refresh-lease` only when deliberately testing lease expiry.

Long runner calls also emit periodic `*.heartbeat` events. Configure the interval with:

```bash
export CONSENSUSD_HEARTBEAT_INTERVAL_SEC=30
```

Real Codex/Kimi phases default to a 60 minute subprocess timeout. For unusually deep reviews, raise it with either an environment variable or the CLI flag:

```bash
export CONSENSUSD_SUBPROCESS_TIMEOUT_SEC=3600
uv run consensusd up --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit --subprocess-timeout-sec 3600
```

Codex proposal prompts receive a bounded deep context packet before the subprocess starts: objective commit diff, relevant current files, P11B/Revolut artifacts, package/test context, and capped repo-local file contents. Codex should understand that packet deeply, inspect only a small number of named extra files if needed, and record missing external facts rather than inventing them. Nested `codex exec` runner calls use `--ignore-user-config`, `--ignore-rules`, and `--ephemeral` so user hooks, skills, repo rules, and MCP config do not accidentally turn a planner pass into a full interactive workflow.

## Editable Mode Guardrails

`--runner-mode codex-kimi-edit` allows Codex to make scoped repo edits after Kimi requests revision. Each revision records:

- `codex.revision.started` and `codex.revision.completed` events
- `codex_revision` evidence with the runner summary
- `editable_change_summary` evidence with changed files and guardrail results

Default denied paths are `.git/`, `.consensusd/`, `.env`, `.env.`-prefixed files such as `.env.local`, `.ssh/`, and `secrets/`. Override with comma-separated repo-relative paths:

```bash
export CONSENSUSD_EDITABLE_ALLOWED_PATHS="src/,tests/,.omx/plans/"
export CONSENSUSD_EDITABLE_DENIED_PATHS=".git/,.consensusd/,.env,.env.,.ssh/,secrets/"
```

If a revision touches a denied path, the run fails before consensus can lock.

Editable mode requires `project_root` to be inside a git worktree and requires `git` on `PATH`. This is deliberate: the v1 change detector uses `git status --porcelain=v1 -z` and diff hashes as the durable audit basis. Non-git roots should use `mock`, `codex`, or read-only `codex-kimi` mode.

## Tests

```bash
uv run pytest
```

Covered behavior includes valid/invalid transitions, run creation, proposal submission, review rejection and approval paths, max-round failure, OMX generation, approval-gated Ralph handoff, event ordering, MCP permission checks, and the mock full run.

## Security Assumptions

- `consensusd` is local-only and binds to `127.0.0.1`.
- Bearer tokens map to roles: `control_surface`, `codex_planner`, `kimi_reviewer`, and `orchestrator`.
- Tool permissions are enforced server-side.
- `--dev-auth-role` is available only for local smoke tests where an MCP client cannot send bearer headers.
- No generic shell execution tool exists.
- Verification commands are limited to `git diff`, `git diff --stat`, or a configured project test command for evidence records.
- Editable-mode path guardrails are local safety rails, not a sandbox. Use OS/container isolation before trusting unreviewed subprocess agents on sensitive repositories.
- Production auth should replace shared dev tokens with local secret bootstrap, short-lived tokens, and OS/socket isolation.

## Current Limitations

- MCP is exposed with the official Python SDK `FastMCP` streamable HTTP transport at `/mcp`.
- Ralph execution is still mocked behind the approval gate.
- Real subprocess runners require local `codex` and `kimi` CLIs to be installed and usable without interactive credential prompts.
- Editable mode lets Codex change files before consensus lock. Use read-only `codex-kimi` mode when you only want planning/review before Ralph.
- Admin endpoints are unauthenticated in v1 and intended for localhost development.

## Roadmap

1. Add real Ralph handoff execution behind the existing approval state.
2. Add richer evidence collection from configured, fixed verification commands.
3. Harden auth and add per-run capability tokens.
4. Add a first-class Codex App status panel once the app exposes a stable local UI extension surface.
