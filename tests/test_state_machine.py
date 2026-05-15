import pytest

from consensusd.models import RunStatus
from consensusd.state_machine import InvalidTransition, assert_transition


def test_valid_state_transitions():
    assert_transition(RunStatus.INIT, RunStatus.AWAITING_CODEX_PROPOSAL)
    assert_transition(RunStatus.KIMI_REVIEWING, RunStatus.REVISION_REQUESTED)
    assert_transition(RunStatus.KIMI_REVIEWING, RunStatus.CONSENSUS_LOCKED)
    assert_transition(RunStatus.AWAITING_HUMAN_APPROVAL, RunStatus.RALPH_HANDOFF_APPROVED)


def test_invalid_state_transition_rejected():
    with pytest.raises(InvalidTransition):
        assert_transition(RunStatus.INIT, RunStatus.KIMI_REVIEWING)


def test_any_non_terminal_can_fail_or_cancel():
    assert_transition(RunStatus.CODEX_DRAFTING, RunStatus.FAILED)
    assert_transition(RunStatus.OMX_GENERATED, RunStatus.CANCELLED)


def test_terminal_cannot_transition():
    with pytest.raises(InvalidTransition):
        assert_transition(RunStatus.FAILED, RunStatus.CANCELLED)
