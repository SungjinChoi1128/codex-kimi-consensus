import pytest

from consensusd.db import ConcurrencyError, Database
from consensusd.models import ReviewStatus, RunStatus


def test_run_creation(tmp_path):
    db = Database(tmp_path / "consensus.sqlite")
    db.init()
    run = db.create_run("review this", str(tmp_path), "approval-gated")

    loaded = db.get_run(run.run_id)
    assert loaded.objective == "review this"
    assert loaded.status == RunStatus.INIT
    assert loaded.version == 0
    assert loaded.lease_mode == "detached"
    assert loaded.lease_expires_at is None


def test_attached_run_lease_can_refresh_without_version_bump(tmp_path):
    db = Database(tmp_path / "consensus.sqlite")
    db.init()
    run = db.create_run("review this", str(tmp_path), "approval-gated", lease_mode="attached", lease_ttl_seconds=5)

    refreshed = db.refresh_run_lease(run.run_id, ttl_seconds=30)

    assert refreshed.lease_mode == "attached"
    assert refreshed.lease_expires_at is not None
    assert refreshed.version == 0
    events = db.transcript(run.run_id).events
    assert [event.event_type for event in events] == [
        "run.created",
        "run.lease_started",
        "run.lease_refreshed",
    ]


def test_transition_increments_version_and_events_are_ordered(tmp_path):
    db = Database(tmp_path / "consensus.sqlite")
    db.init()
    run = db.create_run("review this", str(tmp_path), "approval-gated")

    updated = db.transition_run(run.run_id, RunStatus.AWAITING_CODEX_PROPOSAL, expected_version=0)

    assert updated.version == 1
    events = db.transcript(run.run_id).events
    assert [event.sequence for event in events] == [1, 2]


def test_optimistic_concurrency_rejects_stale_version(tmp_path):
    db = Database(tmp_path / "consensus.sqlite")
    db.init()
    run = db.create_run("review this", str(tmp_path), "approval-gated")
    db.transition_run(run.run_id, RunStatus.AWAITING_CODEX_PROPOSAL, expected_version=0)

    with pytest.raises(ConcurrencyError):
        db.transition_run(run.run_id, RunStatus.CODEX_DRAFTING, expected_version=0)


def test_proposal_and_review_persistence(tmp_path):
    db = Database(tmp_path / "consensus.sqlite")
    db.init()
    run = db.create_run("review this", str(tmp_path), "approval-gated")

    proposal = db.add_proposal(run.run_id, 1, "proposal")
    review = db.add_review(run.run_id, 1, ReviewStatus.NEEDS_REVISION, "fix security")

    transcript = db.transcript(run.run_id)
    assert transcript.proposals == [proposal]
    assert transcript.reviews == [review]
    assert transcript.events[-2].event_type == "proposal.created"
    assert transcript.events[-1].event_type == "review.created"
