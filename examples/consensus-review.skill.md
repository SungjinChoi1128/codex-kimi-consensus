# Consensus Review

Use this skill when the user asks for consensus review, Kimi review, architect review, OMX generation, Ralph handoff, or multi-agent planning.

Workflow:
1. Prefer the MCP tool `start_consensus_review` when the `consensus` MCP server is already available.
2. If the MCP server is not available, run the one-shot local command from the current repo instead of asking the user to open another terminal:
   `uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run consensusd review "<objective>" --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit`
3. Return the `run_id`, current status, and the approval gate state.
4. Use `get_consensus_status` when the user asks for progress and MCP is available.
5. Use `get_consensus_transcript` when the user asks for the transcript and MCP is available.
6. If using CLI fallback, use `consensusd status`, `consensusd transcript`, and `consensusd approve` through `uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run ...`.
7. Do not claim the workflow is complete until status is `OMX_GENERATED`, `AWAITING_HUMAN_APPROVAL`, or `RALPH_HANDOFF_COMPLETE`.
8. Pause for explicit user approval before Ralph handoff by calling `approve_ralph_handoff` or running `consensusd approve`.

For background MCP setup without visible server terminals, use:

`uv --project /Users/sungjinchoi/Developer/codex-kimi-consensus run consensusd up --project-root . --db .consensusd/consensusd.sqlite --runner-mode codex-kimi-edit`

Do not pass an `agent` or `role` argument to tools. The daemon derives caller identity from MCP auth context.

Runner warning: `runner=codex-kimi-edit` runs real Codex proposal/revision/OMX plus real Kimi architect review. Codex may edit files between Kimi review rounds to resolve blockers, but Ralph handoff still pauses for human approval. `runner=codex-kimi` is read-only and may fail if Kimi requires actual repo changes. `runner=codex` is only a partial smoke with deterministic mock Kimi review. `runner=mock` is only a workflow/plumbing rehearsal.
