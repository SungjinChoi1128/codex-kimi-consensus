---
name: consensus-review
description: Start and monitor consensusd Codex/Kimi consensus reviews, generate OMX plans, and keep Ralph handoff approval-gated.
---

# Consensus Review

Use this skill when the user asks for consensus review, Kimi review, architect review, OMX generation, Ralph handoff, or multi-agent planning.

Workflow:
1. Prefer the MCP tool `start_consensus_review` when the `consensus` MCP server is already available. For Codex CLI chat-triggered reviews, start attached so an interrupted chat stops the daemon-side run: pass `lease_mode="attached"` and `lease_ttl_seconds=300`. The longer initial lease gives Codex enough time to start watching after setup/context gathering. Also pass `session_context` when the user is referring to recent chat state, a Ralph completion summary, a prior Codex report, or phrases like "the P11B implementation" without an exact commit; keep it concise and include relevant commit ids, artifact paths, safety outcomes, and the user's current ask.
2. If the MCP server is not available, run the one-shot local command from the current repo instead of asking the user to open another terminal:
   `uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run consensusd review "<objective>" --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit --session-context "<recent Ralph/Codex summary when relevant>"`
3. Return the `run_id` immediately, then keep the user connected with progress. If the run is not terminal or approval-gated, call `watch_consensus_progress(run_id, wait_seconds=30, interval_seconds=5, refresh_lease=true, lease_ttl_seconds=90)` and summarize the returned samples. Repeat while the user is waiting for the review, unless they ask you to stop or switch tasks. Keep watch refreshes at 90 seconds so an interrupted chat still cancels promptly after the last refresh.
4. Use `get_consensus_brief` first when the user asks what is happening; it is the Codex-friendly point-in-time progress surface.
5. Use `watch_consensus_progress` instead of raw `sleep` when waiting in Codex CLI. Never silently sleep and then fetch a full transcript as the default progress UX. If the user interrupts the chat/tool wait, the attached lease will expire and consensusd will cancel active Codex/Kimi subprocesses.
6. Use `get_consensus_status` for raw state and `get_consensus_transcript` when the user asks for the full transcript or when the run reaches `AWAITING_HUMAN_APPROVAL`, `RALPH_HANDOFF_COMPLETE`, `FAILED`, or `CANCELLED`.
7. While the run is active, print a concise progress update when phase, heartbeat, artifact counts, Kimi status, live changed files, OMX path, or error changes. Example shape: `round 1/5 · codex.proposal · heartbeat 2 · waiting for Codex proposal`. During `codex.revision`, if `changed_files` is non-empty, show `Codex is editing: <paths>`.
8. At `AWAITING_HUMAN_APPROVAL`, show `ralph_handoff_prompt` from `get_consensus_brief` or `watch_consensus_progress` as the user-facing Codex CLI handoff. Do not replace it with a terminal `consensusd approve` command. `approve_ralph_handoff` is audit bookkeeping for consensusd; the actual Ralph UX is pasting the `$ralph ...` prompt into Codex CLI.
9. If using CLI fallback, use `consensusd review` for the one-command path, `consensusd brief` for progress, `consensusd status` for raw state, `consensusd transcript` only when asked, and `consensusd approve` only when the user wants to close the daemon's audit gate.
10. Do not claim the workflow is complete until status is `OMX_GENERATED`, `AWAITING_HUMAN_APPROVAL`, or `RALPH_HANDOFF_COMPLETE`.
11. Pause for explicit user approval before Ralph handoff. When approved, use the displayed `$ralph ...` prompt in Codex CLI for actual implementation, then call `approve_ralph_handoff` only if the daemon audit trail should record the handoff as approved.

For background MCP setup without visible server terminals, use:

`uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run consensusd up --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit --subprocess-timeout-sec 3600`

Do not pass an `agent` or `role` argument to tools. The daemon derives caller identity from MCP auth context.

Runner warning: `runner=codex-kimi-edit` runs real Codex proposal/revision/OMX plus real Kimi architect review. Codex may edit files between Kimi review rounds to resolve blockers, but Ralph handoff still pauses for human approval. `runner=codex-kimi` is read-only and may fail if Kimi requires actual repo changes. `runner=codex` is only a partial smoke with deterministic mock Kimi review. `runner=mock` is only a workflow/plumbing rehearsal.

Timeout warning: real Codex/Kimi phases default to 60 minutes. Codex proposal receives a bounded deep context packet first: explicit objective commit diff when provided, relevant current files, P11B/Revolut artifacts, filtered package/test context, and capped repo-local file contents. When the objective names a commit, consensusd intentionally excludes unrelated current-HEAD commit metadata so other session cleanup commits do not pollute the review target. Codex should understand that context properly, inspect only a few named extra files if needed, and name missing external API facts rather than inventing them.

Session-context rule: `session_context` is the primary anchor when the user refers to recent UX flow without naming an exact commit. Use it for prompts such as "Ralph just completed P11B.1 and reported these artifact paths..." or "User now asks to review the P11B implementation and plan the next safest step." When `session_context` is present and the objective does not name a commit, consensusd intentionally omits git status, diff, and HEAD commit evidence from the agent prompt to avoid cross-session contamination. It still records `user_session_context` and may supply bounded relevant file contents so Codex/Kimi can check the summary against actual files.

Editable guardrails: `codex-kimi-edit` requires a git worktree, records changed files after every revision, and fails the run if Codex touches denied paths. Defaults deny `.git/`, `.consensusd/`, `.env`, `.env.`-prefixed files such as `.env.local`, `.ssh/`, and `secrets/`. Optional environment variables: `CONSENSUSD_EDITABLE_ALLOWED_PATHS` and `CONSENSUSD_EDITABLE_DENIED_PATHS`, comma-separated repo-relative paths.
