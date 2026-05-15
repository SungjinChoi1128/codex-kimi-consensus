from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


@dataclass(frozen=True)
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    db_path: Path = Path(".consensusd/consensusd.sqlite")
    project_root: Path = Path(".")
    poll_interval: float = 0.25
    codex_command: tuple[str, ...] = ("codex", "exec")
    kimi_command: tuple[str, ...] = ("kimi",)
    project_test_command: Optional[tuple[str, ...]] = None
    control_token: str = "dev-control-token"
    codex_token: str = "dev-codex-token"
    kimi_token: str = "dev-kimi-token"
    orchestrator_token: str = "dev-orchestrator-token"
    dev_auth_role: Optional[str] = None
    runner_mode: str = "mock"
    subprocess_timeout_sec: int = 600
    heartbeat_interval_sec: float = 30.0
    editable_allowed_paths: tuple[str, ...] = ()
    editable_denied_paths: tuple[str, ...] = (
        ".git/",
        ".consensusd/",
        ".env",
        ".env.",
        ".ssh/",
        "secrets/",
    )

    @classmethod
    def from_env(
        cls,
        db_path: Optional[Union[str, Path]] = None,
        project_root: Optional[Union[str, Path]] = None,
        host: Optional[str] = None,
        port: Optional[int] = None,
        dev_auth_role: Optional[str] = None,
        runner_mode: Optional[str] = None,
    ) -> "Settings":
        test_cmd = os.getenv("CONSENSUSD_PROJECT_TEST_COMMAND")
        allowed = _csv_env("CONSENSUSD_EDITABLE_ALLOWED_PATHS")
        denied = _csv_env("CONSENSUSD_EDITABLE_DENIED_PATHS")
        return cls(
            host=host or os.getenv("CONSENSUSD_HOST", "127.0.0.1"),
            port=port or int(os.getenv("CONSENSUSD_PORT", "8787")),
            db_path=Path(db_path or os.getenv("CONSENSUSD_DB", ".consensusd/consensusd.sqlite")),
            project_root=Path(project_root or os.getenv("CONSENSUSD_PROJECT_ROOT", ".")),
            poll_interval=float(os.getenv("CONSENSUSD_POLL_INTERVAL", "0.25")),
            codex_command=tuple(os.getenv("CONSENSUSD_CODEX_COMMAND", "codex exec").split()),
            kimi_command=tuple(os.getenv("CONSENSUSD_KIMI_COMMAND", "kimi").split()),
            project_test_command=tuple(test_cmd.split()) if test_cmd else None,
            control_token=os.getenv("CONSENSUSD_CONTROL_TOKEN", "dev-control-token"),
            codex_token=os.getenv("CONSENSUSD_CODEX_TOKEN", "dev-codex-token"),
            kimi_token=os.getenv("CONSENSUSD_KIMI_TOKEN", "dev-kimi-token"),
            orchestrator_token=os.getenv("CONSENSUSD_ORCHESTRATOR_TOKEN", "dev-orchestrator-token"),
            dev_auth_role=dev_auth_role or os.getenv("CONSENSUSD_DEV_AUTH_ROLE"),
            runner_mode=runner_mode or os.getenv("CONSENSUSD_RUNNER_MODE", "mock"),
            subprocess_timeout_sec=int(os.getenv("CONSENSUSD_SUBPROCESS_TIMEOUT_SEC", "600")),
            heartbeat_interval_sec=float(os.getenv("CONSENSUSD_HEARTBEAT_INTERVAL_SEC", "30")),
            editable_allowed_paths=tuple(allowed),
            editable_denied_paths=tuple(denied)
            if denied
            else (
                ".git/",
                ".consensusd/",
                ".env",
                ".env.",
                ".ssh/",
                "secrets/",
            ),
        )


def _csv_env(name: str) -> list[str]:
    value = os.getenv(name, "")
    return [item.strip() for item in value.split(",") if item.strip()]
