---
name: consensus-review
description: Start and monitor consensusd Codex/Kimi consensus reviews, generate OMX plans, and keep Ralph handoff approval-gated.
---

# Consensus Review

Use this skill when the user asks for consensus review, Kimi review, architect review, OMX generation, Ralph handoff, or multi-agent planning.

Workflow:
1. Prefer the MCP tool `start_consensus_review` when the `consensus` MCP server is already available. For Codex CLI chat-triggered reviews, start attached so an interrupted chat stops the daemon-side run: pass `lease_mode="attached"` and `lease_ttl_seconds=90`.
2. If the MCP server is not available, run the one-shot local command from the current repo instead of asking the user to open another terminal:
   `uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run consensusd review "<objective>" --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit`
3. Return the `run_id` immediately, then keep the user connected with progress. If the run is not terminal or approval-gated, call `watch_consensus_progress(run_id, wait_seconds=30, interval_seconds=5, refresh_lease=true, lease_ttl_seconds=90)` and summarize the returned samples. Repeat while the user is waiting for the review, unless they ask you to stop or switch tasks.
4. Use `get_consensus_brief` first when the user asks what is happening; it is the Codex-friendly point-in-time progress surface.
5. Use `watch_consensus_progress` instead of raw `sleep` when waiting in Codex CLI. Never silently sleep and then fetch a full transcript as the default progress UX. If the user interrupts the chat/tool wait, the attached lease will expire and consensusd will cancel active Codex/Kimi subprocesses.
6. Use `get_consensus_status` for raw state and `get_consensus_transcript` when the user asks for the full transcript or when the run reaches `AWAITING_HUMAN_APPROVAL`, `RALPH_HANDOFF_COMPLETE`, `FAILED`, or `CANCELLED`.
7. While the run is active, print a concise progress update when phase, heartbeat, artifact counts, Kimi status, live changed files, OMX path, or error changes. Example shape: `round 1/5 · codex.proposal · heartbeat 2 · waiting for Codex proposal`. During `codex.revision`, if `changed_files` is non-empty, show `Codex is editing: <paths>`.
8. If using CLI fallback, use `consensusd review` for the one-command path, `consensusd brief` for progress, `consensusd status` for raw state, `consensusd transcript` only when asked, and `consensusd approve` for handoff approval through `uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run ...`.
9. Do not claim the workflow is complete until status is `OMX_GENERATED`, `AWAITING_HUMAN_APPROVAL`, or `RALPH_HANDOFF_COMPLETE`.
10. Pause for explicit user approval before Ralph handoff by calling `approve_ralph_handoff` or running `consensusd approve`.

For background MCP setup without visible server terminals, use:

`uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run consensusd up --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit --subprocess-timeout-sec 3600`

Do not pass an `agent` or `role` argument to tools. The daemon derives caller identity from MCP auth context.

Runner warning: `runner=codex-kimi-edit` runs real Codex proposal/revision/OMX plus real Kimi architect review. Codex may edit files between Kimi review rounds to resolve blockers, but Ralph handoff still pauses for human approval. `runner=codex-kimi` is read-only and may fail if Kimi requires actual repo changes. `runner=codex` is only a partial smoke with deterministic mock Kimi review. `runner=mock` is only a workflow/plumbing rehearsal.

Timeout warning: real Codex/Kimi phases default to 30 minutes. If a run times out during `CODEX_DRAFTING`, restart the daemon with `--subprocess-timeout-sec 3600` and keep the objective focused. The proposal pass should be bounded and should name missing evidence rather than running broad scans indefinitely.

Editable guardrails: `codex-kimi-edit` requires a git worktree, records changed files after every revision, and fails the run if Codex touches denied paths. Defaults deny `.git/`, `.consensusd/`, `.env`, `.env.`-prefixed files such as `.env.local`, `.ssh/`, and `secrets/`. Optional environment variables: `CONSENSUSD_EDITABLE_ALLOWED_PATHS` and `CONSENSUSD_EDITABLE_DENIED_PATHS`, comma-separated repo-relative paths.
