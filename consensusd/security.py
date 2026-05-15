from __future__ import annotations

import os
from typing import Optional

from fastapi import Header, HTTPException
from mcp.server.auth.provider import AccessToken, TokenVerifier

from .models import ToolRole
from .settings import Settings


TOOL_PERMISSIONS: dict[ToolRole, set[str]] = {
    ToolRole.CONTROL_SURFACE: {
        "start_consensus_review",
        "get_consensus_status",
        "get_consensus_brief",
        "watch_consensus_progress",
        "get_consensus_transcript",
        "cancel_consensus_review",
        "approve_ralph_handoff",
    },
    ToolRole.CODEX_PLANNER: {
        "get_current_state",
        "submit_proposal",
        "finalize_omx",
        "get_git_diff",
        "list_changed_files",
        "record_evidence",
    },
    ToolRole.KIMI_REVIEWER: {
        "get_current_state",
        "get_proposal",
        "submit_review",
        "get_git_diff",
        "list_changed_files",
        "record_evidence",
    },
    ToolRole.ORCHESTRATOR: {"*"},
}


def role_for_token(token: Optional[str], settings: Settings) -> Optional[ToolRole]:
    if not token:
        env_role = os.getenv("CONSENSUSD_CALLER_ROLE")
        return ToolRole(env_role) if env_role else None
    token_map = {
        settings.control_token: ToolRole.CONTROL_SURFACE,
        settings.codex_token: ToolRole.CODEX_PLANNER,
        settings.kimi_token: ToolRole.KIMI_REVIEWER,
        settings.orchestrator_token: ToolRole.ORCHESTRATOR,
    }
    return token_map.get(token)


def assert_tool_allowed(role: ToolRole, tool_name: str) -> None:
    allowed = TOOL_PERMISSIONS[role]
    if "*" not in allowed and tool_name not in allowed:
        raise PermissionError(f"{role} cannot call {tool_name}")


def bearer_token(authorization: Optional[str] = Header(default=None)) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise HTTPException(status_code=401, detail="expected bearer token")
    return token


def require_localhost(host: str) -> None:
    # Production deployments should replace these shared dev tokens with a local
    # secret bootstrap flow, short-lived tokens, and OS-level socket isolation.
    if host not in {"127.0.0.1", "localhost"}:
        raise ValueError("consensusd refuses to bind non-localhost interfaces by default")


class ConsensusTokenVerifier(TokenVerifier):
    def __init__(self, settings: Settings):
        self.settings = settings

    async def verify_token(self, token: str) -> Optional[AccessToken]:
        role = role_for_token(token, self.settings)
        if role is None:
            return None
        return AccessToken(token=token, client_id=role.value, scopes=[role.value])
