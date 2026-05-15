from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Optional, Union

from .models import (
    Event,
    Evidence,
    ContextBridge,
    OmxPlan,
    Proposal,
    RalphHandoff,
    Review,
    ReviewStatus,
    Run,
    RunStatus,
    Transcript,
)
from .state_machine import assert_transition


class ConcurrencyError(RuntimeError):
    pass


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def utcnow_dt() -> datetime:
    return datetime.now(timezone.utc)


def iso_after(seconds: int) -> str:
    return (utcnow_dt() + timedelta(seconds=seconds)).isoformat()


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex}"


class Database:
    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS runs (
                  run_id TEXT PRIMARY KEY,
                  project_root TEXT NOT NULL,
                  objective TEXT NOT NULL,
                  status TEXT NOT NULL,
                  mode TEXT NOT NULL,
                  runner_mode TEXT NOT NULL DEFAULT 'mock',
                  lease_mode TEXT NOT NULL DEFAULT 'detached',
                  lease_expires_at TEXT NULL,
                  max_rounds INTEGER NOT NULL DEFAULT 5,
                  current_round INTEGER NOT NULL DEFAULT 1,
                  version INTEGER NOT NULL DEFAULT 0,
                  created_at TEXT NOT NULL,
                  updated_at TEXT NOT NULL,
                  locked_at TEXT NULL,
                  error TEXT NULL
                );
                CREATE TABLE IF NOT EXISTS proposals (
                  proposal_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  round INTEGER NOT NULL,
                  runner TEXT NOT NULL DEFAULT 'unknown',
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS reviews (
                  review_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  round INTEGER NOT NULL,
                  runner TEXT NOT NULL DEFAULT 'unknown',
                  status TEXT NOT NULL CHECK(status IN ('APPROVED', 'NEEDS_REVISION')),
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                  evidence_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  round INTEGER NOT NULL,
                  kind TEXT NOT NULL,
                  command TEXT NULL,
                  status TEXT NOT NULL,
                  output TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS omx_plans (
                  omx_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  runner TEXT NOT NULL DEFAULT 'unknown',
                  path TEXT NULL,
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS context_bridges (
                  bridge_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  path TEXT NOT NULL,
                  content TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                  event_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  sequence INTEGER NOT NULL,
                  event_type TEXT NOT NULL,
                  payload_json TEXT NOT NULL,
                  created_at TEXT NOT NULL,
                  UNIQUE(run_id, sequence)
                );
                CREATE TABLE IF NOT EXISTS ralph_handoffs (
                  handoff_id TEXT PRIMARY KEY,
                  run_id TEXT NOT NULL REFERENCES runs(run_id),
                  packet_json TEXT NOT NULL,
                  status TEXT NOT NULL,
                  created_at TEXT NOT NULL
                );
                """
            )
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)").fetchall()}
            if "runner_mode" not in columns:
                conn.execute("ALTER TABLE runs ADD COLUMN runner_mode TEXT NOT NULL DEFAULT 'mock'")
            self._ensure_column(conn, "runs", "lease_mode", "TEXT NOT NULL DEFAULT 'detached'")
            self._ensure_column(conn, "runs", "lease_expires_at", "TEXT NULL")
            self._ensure_column(conn, "proposals", "runner", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "reviews", "runner", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "omx_plans", "runner", "TEXT NOT NULL DEFAULT 'unknown'")
            self._ensure_column(conn, "omx_plans", "path", "TEXT NULL")

    def create_run(
        self,
        objective: str,
        project_root: str,
        mode: str,
        max_rounds: int = 5,
        runner_mode: str = "mock",
        lease_mode: str = "detached",
        lease_ttl_seconds: Optional[int] = None,
    ) -> Run:
        now = utcnow()
        if lease_mode not in {"detached", "attached"}:
            raise ValueError("lease_mode must be 'detached' or 'attached'")
        lease_expires_at = iso_after(lease_ttl_seconds or 90) if lease_mode == "attached" else None
        run = Run(
            run_id=new_id("run"),
            project_root=str(Path(project_root).resolve()),
            objective=objective,
            status=RunStatus.INIT,
            mode=mode,
            runner_mode=runner_mode,
            lease_mode=lease_mode,
            lease_expires_at=lease_expires_at,
            max_rounds=max_rounds,
            created_at=now,
            updated_at=now,
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO runs(run_id, project_root, objective, status, mode, runner_mode, lease_mode, lease_expires_at, max_rounds,
                  current_round, version, created_at, updated_at, locked_at, error)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run.run_id,
                    run.project_root,
                    run.objective,
                    run.status,
                    run.mode,
                    run.runner_mode,
                    run.lease_mode,
                    run.lease_expires_at,
                    run.max_rounds,
                    run.current_round,
                    run.version,
                    run.created_at,
                    run.updated_at,
                    run.locked_at,
                    run.error,
                ),
            )
            self._append_event(conn, run.run_id, "run.created", run.model_dump(mode="json"))
            if run.lease_mode == "attached":
                self._append_event(
                    conn,
                    run.run_id,
                    "run.lease_started",
                    {
                        "lease_mode": run.lease_mode,
                        "lease_expires_at": run.lease_expires_at,
                    },
                )
        return run

    def get_run(self, run_id: str) -> Run:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        if not row:
            raise KeyError(f"run not found: {run_id}")
        return self._run(row)

    def list_active_runs(self) -> list[Run]:
        terminal = tuple(s.value for s in (RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.RALPH_HANDOFF_COMPLETE))
        with self.connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM runs WHERE status NOT IN ({','.join('?' for _ in terminal)}) ORDER BY created_at",
                terminal,
            ).fetchall()
        return [self._run(row) for row in rows]

    def transition_run(
        self,
        run_id: str,
        new_status: RunStatus,
        expected_version: Optional[int] = None,
        error: Optional[str] = None,
        increment_round: bool = False,
    ) -> Run:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run not found: {run_id}")
            run = self._run(row)
            if expected_version is not None and run.version != expected_version:
                raise ConcurrencyError(f"expected version {expected_version}, found {run.version}")
            assert_transition(run.status, new_status)
            now = utcnow()
            locked_at = now if new_status == RunStatus.CONSENSUS_LOCKED else run.locked_at
            current_round = run.current_round + 1 if increment_round else run.current_round
            result = conn.execute(
                """
                UPDATE runs SET status = ?, version = version + 1, updated_at = ?,
                  locked_at = ?, error = ?, current_round = ?
                WHERE run_id = ? AND version = ?
                """,
                (new_status, now, locked_at, error, current_round, run_id, run.version),
            )
            if result.rowcount != 1:
                raise ConcurrencyError("run was modified concurrently")
            self._append_event(
                conn,
                run_id,
                "run.transitioned",
                {
                    "from": run.status.value,
                    "to": new_status.value,
                    "version": run.version + 1,
                    "error": error,
                    "current_round": current_round,
                },
            )
        return self.get_run(run_id)

    def refresh_run_lease(self, run_id: str, ttl_seconds: int = 90) -> Run:
        ttl_seconds = max(5, min(ttl_seconds, 3600))
        expires_at = iso_after(ttl_seconds)
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if not row:
                raise KeyError(f"run not found: {run_id}")
            run = self._run(row)
            if run.lease_mode != "attached" or run.status in {RunStatus.FAILED, RunStatus.CANCELLED, RunStatus.RALPH_HANDOFF_COMPLETE}:
                return run
            # Heartbeats are liveness metadata, not workflow-state transitions.
            # They intentionally do not bump `version`, so they cannot race with
            # proposal/review/OMX optimistic concurrency checks.
            conn.execute("UPDATE runs SET lease_expires_at = ? WHERE run_id = ?", (expires_at, run_id))
            self._append_event(
                conn,
                run_id,
                "run.lease_refreshed",
                {"lease_mode": "attached", "lease_expires_at": expires_at},
            )
        return self.get_run(run_id)

    def add_proposal(self, run_id: str, round: int, content: str, runner: str = "unknown") -> Proposal:
        proposal = Proposal(
            proposal_id=new_id("proposal"),
            run_id=run_id,
            round=round,
            runner=runner,
            content=content,
            created_at=utcnow(),
        )
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO proposals(proposal_id, run_id, round, runner, content, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (proposal.proposal_id, run_id, round, runner, content, proposal.created_at),
            )
            self._append_event(conn, run_id, "proposal.created", proposal.model_dump(mode="json"))
        return proposal

    def add_review(self, run_id: str, round: int, status: ReviewStatus, content: str, runner: str = "unknown") -> Review:
        review = Review(
            review_id=new_id("review"),
            run_id=run_id,
            round=round,
            runner=runner,
            status=status,
            content=content,
            created_at=utcnow(),
        )
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO reviews(review_id, run_id, round, runner, status, content, created_at) VALUES(?, ?, ?, ?, ?, ?, ?)",
                (review.review_id, run_id, round, runner, status.value, content, review.created_at),
            )
            self._append_event(conn, run_id, "review.created", review.model_dump(mode="json"))
        return review

    def add_evidence(self, run_id: str, round: int, kind: str, status: str, output: str, command: Optional[str] = None) -> Evidence:
        evidence = Evidence(
            evidence_id=new_id("evidence"),
            run_id=run_id,
            round=round,
            kind=kind,
            command=command,
            status=status,
            output=output,
            created_at=utcnow(),
        )
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO evidence(evidence_id, run_id, round, kind, command, status, output, created_at)
                VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (evidence.evidence_id, run_id, round, kind, command, status, output, evidence.created_at),
            )
            self._append_event(conn, run_id, "evidence.recorded", evidence.model_dump(mode="json"))
        return evidence

    def add_omx_plan(self, run_id: str, content: str, runner: str = "unknown", path: Optional[str] = None) -> OmxPlan:
        plan = OmxPlan(omx_id=new_id("omx"), run_id=run_id, runner=runner, path=path, content=content, created_at=utcnow())
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO omx_plans(omx_id, run_id, runner, path, content, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                (plan.omx_id, run_id, runner, path, content, plan.created_at),
            )
            self._append_event(conn, run_id, "omx.created", plan.model_dump(mode="json"))
        return plan

    def add_context_bridge(self, run_id: str, path: str, content: str) -> ContextBridge:
        bridge = ContextBridge(
            bridge_id=new_id("bridge"),
            run_id=run_id,
            path=path,
            content=content,
            created_at=utcnow(),
        )
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO context_bridges(bridge_id, run_id, path, content, created_at) VALUES(?, ?, ?, ?, ?)",
                (bridge.bridge_id, run_id, path, content, bridge.created_at),
            )
            self._append_event(conn, run_id, "context_bridge.created", bridge.model_dump(mode="json"))
        return bridge

    def add_handoff(self, run_id: str, packet: dict[str, Any], status: str = "mock-complete") -> RalphHandoff:
        handoff = RalphHandoff(
            handoff_id=new_id("handoff"),
            run_id=run_id,
            packet_json=json.dumps(packet, sort_keys=True),
            status=status,
            created_at=utcnow(),
        )
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO ralph_handoffs(handoff_id, run_id, packet_json, status, created_at) VALUES(?, ?, ?, ?, ?)",
                (handoff.handoff_id, run_id, handoff.packet_json, handoff.status, handoff.created_at),
            )
            self._append_event(conn, run_id, "ralph_handoff.created", handoff.model_dump(mode="json"))
        return handoff

    def add_event(self, run_id: str, event_type: str, payload: dict[str, Any]) -> Event:
        with self.connect() as conn:
            self._append_event(conn, run_id, event_type, payload)
            row = conn.execute(
                "SELECT * FROM events WHERE run_id = ? ORDER BY sequence DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            if not row:
                raise RuntimeError("event append failed")
            return self._event(row)

    def latest_proposal(self, run_id: str) -> Optional[Proposal]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM proposals WHERE run_id = ? ORDER BY round DESC, created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._proposal(row) if row else None

    def get_proposal(self, run_id: str, round: int) -> Proposal:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM proposals WHERE run_id = ? AND round = ?", (run_id, round)).fetchone()
        if not row:
            raise KeyError(f"proposal not found for {run_id} round {round}")
        return self._proposal(row)

    def latest_review(self, run_id: str) -> Optional[Review]:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT * FROM reviews WHERE run_id = ? ORDER BY round DESC, created_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        return self._review(row) if row else None

    def list_evidence(self, run_id: str) -> list[Evidence]:
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY round, created_at", (run_id,)).fetchall()
        return [self._evidence(row) for row in rows]

    def transcript(self, run_id: str) -> Transcript:
        run = self.get_run(run_id)
        with self.connect() as conn:
            proposals = [self._proposal(r) for r in conn.execute("SELECT * FROM proposals WHERE run_id = ? ORDER BY round", (run_id,))]
            reviews = [self._review(r) for r in conn.execute("SELECT * FROM reviews WHERE run_id = ? ORDER BY round", (run_id,))]
            evidence = [self._evidence(r) for r in conn.execute("SELECT * FROM evidence WHERE run_id = ? ORDER BY round, created_at", (run_id,))]
            plans = [self._omx(r) for r in conn.execute("SELECT * FROM omx_plans WHERE run_id = ? ORDER BY created_at", (run_id,))]
            bridges = [self._bridge(r) for r in conn.execute("SELECT * FROM context_bridges WHERE run_id = ? ORDER BY created_at", (run_id,))]
            handoffs = [self._handoff(r) for r in conn.execute("SELECT * FROM ralph_handoffs WHERE run_id = ? ORDER BY created_at", (run_id,))]
            events = [self._event(r) for r in conn.execute("SELECT * FROM events WHERE run_id = ? ORDER BY sequence", (run_id,))]
        return Transcript(
            run=run,
            proposals=proposals,
            reviews=reviews,
            evidence=evidence,
            omx_plans=plans,
            context_bridges=bridges,
            ralph_handoffs=handoffs,
            events=events,
        )

    def _append_event(self, conn: sqlite3.Connection, run_id: str, event_type: str, payload: dict[str, Any]) -> None:
        payload_json = json.dumps(payload, sort_keys=True)
        for _ in range(8):
            row = conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM events WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            try:
                conn.execute(
                    "INSERT INTO events(event_id, run_id, sequence, event_type, payload_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
                    (new_id("event"), run_id, int(row["next_sequence"]), event_type, payload_json, utcnow()),
                )
                return
            except sqlite3.IntegrityError as exc:
                if "events.run_id, events.sequence" not in str(exc):
                    raise
        raise ConcurrencyError(f"could not append event for {run_id} after sequence contention")

    def _ensure_column(self, conn: sqlite3.Connection, table: str, column: str, definition: str) -> None:
        columns = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    def _run(self, row: sqlite3.Row) -> Run:
        return Run(**dict(row))

    def _proposal(self, row: sqlite3.Row) -> Proposal:
        return Proposal(**dict(row))

    def _review(self, row: sqlite3.Row) -> Review:
        return Review(**dict(row))

    def _evidence(self, row: sqlite3.Row) -> Evidence:
        return Evidence(**dict(row))

    def _omx(self, row: sqlite3.Row) -> OmxPlan:
        return OmxPlan(**dict(row))

    def _bridge(self, row: sqlite3.Row) -> ContextBridge:
        return ContextBridge(**dict(row))

    def _event(self, row: sqlite3.Row) -> Event:
        return Event(**dict(row))

    def _handoff(self, row: sqlite3.Row) -> RalphHandoff:
        return RalphHandoff(**dict(row))
