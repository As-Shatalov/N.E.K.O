"""PATCH 03: persistent social-pressure / question-fatigue policy."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from main_logic.proactive_chat import decisions, state


@pytest.fixture(autouse=True)
def _restore_globals():
    engagement = {
        name: dict(entry) for name, entry in state._proactive_engagement_state.items()
    }
    totals = dict(state._proactive_chat_totals)
    ever = dict(state._invite_ever_delivered)
    loaded = state._proactive_chat_totals_loaded
    loaded_path = state._proactive_chat_totals_loaded_path
    yield
    state._proactive_engagement_state.clear()
    state._proactive_engagement_state.update(engagement)
    state._proactive_chat_totals.clear()
    state._proactive_chat_totals.update(totals)
    state._invite_ever_delivered.clear()
    state._invite_ever_delivered.update(ever)
    state._proactive_chat_totals_loaded = loaded
    state._proactive_chat_totals_loaded_path = loaded_path


@pytest.mark.asyncio
async def test_delivery_state_persists_and_user_reply_resets(monkeypatch, tmp_path):
    writer = AsyncMock()
    monkeypatch.setattr(state, "read_json", lambda _path: {})
    monkeypatch.setattr(state, "atomic_write_json_async", writer)
    state._proactive_chat_totals_loaded = False
    state._proactive_chat_totals_loaded_path = None
    state._proactive_engagement_state.clear()

    snap1 = await state._record_proactive_engagement_delivery(
        "Heli",
        message="Ты опять оставил тест на потом?",
        delivered_at=100.0,
        memory_dir=tmp_path,
    )
    assert snap1.unanswered_streak == 1
    assert snap1.recent_question_flags == (True,)

    snap2 = await state._record_proactive_engagement_delivery(
        "Heli",
        message="Ладно. Я просто оставлю это здесь.",
        delivered_at=200.0,
        memory_dir=tmp_path,
    )
    assert snap2.unanswered_streak == 2
    assert snap2.recent_question_flags == (True, False)

    unchanged = await state._reconcile_proactive_user_engagement(
        "Heli",
        user_engagement_at=150.0,
        memory_dir=tmp_path,
    )
    assert unchanged.unanswered_streak == 2

    answered = await state._reconcile_proactive_user_engagement(
        "Heli",
        user_engagement_at=250.0,
        memory_dir=tmp_path,
    )
    assert answered.unanswered_streak == 0
    assert answered.last_user_engagement_at == 250.0
    # Style history survives an answer; it is a rolling tendency, not pressure.
    assert answered.recent_question_flags == (True, False)

    persisted = writer.await_args_list[-1].args[1]
    assert persisted["version"] == state._PROACTIVE_CHAT_TOTALS_SCHEMA_VERSION
    assert persisted["engagement"]["Heli"]["unanswered_streak"] == 0


def test_two_unanswered_turns_start_30_minute_backoff():
    snap = state.ProactiveEngagementSnapshot(
        unanswered_streak=2,
        last_proactive_at=1_000.0,
        recent_question_flags=(False, False),
    )
    policy = decisions._decide_engagement_policy(snap, now=1_100.0)
    assert policy.result is not None
    assert policy.avoid_questions is True
    assert policy.retry_after_seconds == pytest.approx(1_700.0)
    assert policy.result.body["retry_after_seconds"] == 1700


def test_backoff_doubles_and_caps_at_two_hours():
    for streak, expected in ((2, 1800), (3, 3600), (4, 7200), (8, 7200)):
        snap = state.ProactiveEngagementSnapshot(
            unanswered_streak=streak,
            last_proactive_at=10_000.0,
        )
        policy = decisions._decide_engagement_policy(snap, now=10_000.0)
        assert policy.retry_after_seconds == pytest.approx(expected)


def test_real_unfinished_thread_overrides_social_backoff_but_not_question_guard():
    snap = state.ProactiveEngagementSnapshot(
        unanswered_streak=4,
        last_proactive_at=1_000.0,
        recent_question_flags=(True, True, False),
    )
    policy = decisions._decide_engagement_policy(
        snap,
        has_unfinished_thread=True,
        now=1_001.0,
    )
    assert policy.result is None
    assert policy.avoid_questions is True


def test_question_fatigue_without_silence_requests_statement_style():
    snap = state.ProactiveEngagementSnapshot(
        unanswered_streak=0,
        recent_question_flags=(True, False, True, True),
    )
    policy = decisions._decide_engagement_policy(snap, now=10.0)
    assert policy.result is None
    assert policy.avoid_questions is True
    assert policy.question_ratio == pytest.approx(0.75)


def test_question_fatigue_needs_minimum_sample_count():
    snap = state.ProactiveEngagementSnapshot(
        unanswered_streak=0,
        recent_question_flags=(True, True),
    )
    policy = decisions._decide_engagement_policy(snap, now=10.0)
    assert policy.result is None
    assert policy.avoid_questions is False
