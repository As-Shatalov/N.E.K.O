from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from memory.open_loops import OpenLoopStore


class _CM:
    def __init__(self, root):
        self.memory_dir = str(root)


@pytest.fixture
def store(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "utils.cloudsave_runtime.assert_cloudsave_writable",
        lambda *args, **kwargs: None,
    )
    return OpenLoopStore(_CM(tmp_path))


def _future_day(days: int = 1):
    return {"start": {"offset": days, "unit": "day"}, "end": None}


def test_gate_is_conservative_without_active_loop(store):
    assert store.should_analyze_turn("Heli", ["Завтра проверю TTS после перезапуска"])
    assert store.should_analyze_turn("Heli", ["Remind me tomorrow to test the build"])
    assert not store.should_analyze_turn("Heli", ["Мне нравится каша"])
    assert not store.should_analyze_turn("Heli", ["Ну да, забавно получилось"])


def test_create_is_durable_and_replay_idempotent(store):
    now = datetime(2026, 10, 14, 12, 0, 0)
    result = store._apply_actions(
        "Heli",
        [{
            "text": "проверить, работает ли TTS после перезапуска",
            "kind": "task",
            "owner": "master",
            "importance": 8,
            "due_when": _future_day(1),
            "resolution_hint": "пользователь сообщает результат проверки",
        }],
        [],
        "turn-a",
        now,
    )
    assert result == {"created": 1, "updated": 0, "skipped": 0}
    loops = store.active_loops("Heli")
    assert len(loops) == 1
    assert loops[0]["due_at"].startswith("2026-10-15T12:00:00")

    replay = store._apply_actions(
        "Heli",
        [{"text": "проверить TTS после перезапуска", "kind": "task"}],
        [],
        "turn-a",
        now + timedelta(minutes=1),
    )
    assert replay["skipped"] == 1
    assert len(store.active_loops("Heli")) == 1


def test_duplicate_intention_is_not_created_again(store):
    now = datetime(2026, 10, 14, 12, 0, 0)
    store._apply_actions(
        "Heli",
        [{"text": "проверить TTS после перезапуска", "kind": "task"}],
        [],
        "turn-a",
        now,
    )
    result = store._apply_actions(
        "Heli",
        [{"text": "проверить TTS после перезапуска системы", "kind": "task"}],
        [],
        "turn-b",
        now + timedelta(minutes=2),
    )
    assert result["created"] == 0
    assert len(store.active_loops("Heli")) == 1


def test_existing_loop_enables_resolution_gate_and_resolve(store):
    now = datetime(2026, 10, 14, 12, 0, 0)
    store._apply_actions(
        "Heli",
        [{"text": "проверить TTS после перезапуска", "kind": "task"}],
        [],
        "turn-a",
        now,
    )
    loop = store.active_loops("Heli")[0]
    assert store.should_analyze_turn("Heli", ["Да, проверил, всё работает"])

    result = store._apply_actions(
        "Heli",
        [],
        [{"id": loop["id"], "action": "resolve", "resolution": "TTS работает"}],
        "turn-b",
        now + timedelta(hours=1),
    )
    assert result["updated"] == 1
    assert store.active_loops("Heli") == []
    persisted = store.load("Heli")["loops"][0]
    assert persisted["status"] == "resolved"
    assert persisted["resolution_text"] == "TTS работает"


def test_reschedule_reopens_surface_budget(store):
    now = datetime(2026, 10, 14, 12, 0, 0)
    store._apply_actions(
        "Heli",
        [{"text": "проверить новую модель", "kind": "plan", "due_when": _future_day(1)}],
        [],
        "turn-a",
        now,
    )
    loop = store.active_loops("Heli")[0]
    store.record_surfaced("Heli", [loop["id"]])
    assert store.load("Heli")["loops"][0]["surface_count"] == 1

    store._apply_actions(
        "Heli",
        [],
        [{"id": loop["id"], "action": "reschedule", "due_when": _future_day(7)}],
        "turn-b",
        now + timedelta(hours=2),
    )
    current = store.active_loops("Heli")[0]
    assert current["surface_count"] == 0
    assert current["next_eligible_at"] is not None
    assert current["due_at"].startswith("2026-10-21T14:00:00")
    assert current["next_eligible_at"].startswith("2026-10-21T02:00:00")


def test_surface_cooldown_and_hard_nag_cap(store, monkeypatch):
    now = datetime(2026, 10, 14, 12, 0, 0)
    # Freeze the store clock too: real wall time moves beyond this fixture.
    monkeypatch.setattr("memory.open_loops._now_naive", lambda: now)
    store._apply_actions(
        "Heli",
        [{"text": "запустить тестовый билд", "kind": "task", "importance": 8}],
        [],
        "turn-a",
        now,
    )
    loop_id = store.active_loops("Heli")[0]["id"]
    # New loops are remembered immediately but do not nag immediately.
    assert store.candidates("Heli") == []
    with store._get_lock("Heli"):
        payload = store._load_unlocked("Heli")
        row = next(r for r in payload["loops"] if r["id"] == loop_id)
        row["next_eligible_at"] = (now - timedelta(seconds=1)).isoformat()
        store._save_unlocked("Heli", payload)
    assert [row["id"] for row in store.candidates("Heli")] == [loop_id]

    store.record_surfaced("Heli", [loop_id])
    assert store.candidates("Heli") == []

    # Advance next_eligible manually to exercise the repeated-surface cap
    for _ in range(3):
        with store._get_lock("Heli"):
            payload = store._load_unlocked("Heli")
            row = next(r for r in payload["loops"] if r["id"] == loop_id)
            row["next_eligible_at"] = (now - timedelta(seconds=1)).isoformat()
            store._save_unlocked("Heli", payload)
        store.record_surfaced("Heli", [loop_id])
    row = next(r for r in store.load("Heli")["loops"] if r["id"] == loop_id)
    assert row["surface_count"] == 4
    assert store.candidates("Heli") == []


def test_expired_loop_is_not_active_or_candidate(store):
    now = datetime(2026, 1, 1, 12, 0, 0)
    store._apply_actions(
        "Heli",
        [{"text": "когда-нибудь проверить старый билд", "kind": "followup"}],
        [],
        "turn-old",
        now,
    )
    assert store.active_loops("Heli") == []  # real current date is far beyond the 45d expiry
    assert store.candidates("Heli") == []


def test_prompt_renderers_cover_all_locales():
    from config.prompts.prompts_memory import (
        get_open_loop_update_prompt,
        render_open_loop_context_block,
        render_open_loop_proactive_block,
    )

    sample = [{"id": "x", "text": "check TTS", "kind": "task", "due_at": None}]
    for lang in ("zh", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"):
        update = get_open_loop_update_prompt(lang)
        assert "{ANCHOR_TIME}" in update
        assert '"create"' in update and '"update"' in update
        assert "<OpenLoops>" in render_open_loop_context_block(lang, sample)
        assert "<OpenLoopCandidates>" in render_open_loop_proactive_block(lang, sample)


def test_cloudsave_includes_open_loops():
    from utils.cloudsave_runtime._shared import MANAGED_MEMORY_FILENAMES

    assert "open_loops.json" in MANAGED_MEMORY_FILENAMES


def test_new_loop_waits_until_timely_before_proactive_surface(store):
    now = datetime(2026, 10, 14, 12, 0, 0)
    store._apply_actions(
        "Heli",
        [{"text": "проверить модель завтра", "kind": "task", "due_when": _future_day(1)}],
        [],
        "turn-delay",
        now,
    )
    row = store.active_loops("Heli")[0]
    assert row["next_eligible_at"].startswith("2026-10-15T10:00:00")


def test_no_due_loop_gets_twelve_hour_initial_backoff(store):
    now = datetime(2026, 10, 14, 12, 0, 0)
    store._apply_actions(
        "Heli",
        [{"text": "позже сравнить две модели", "kind": "plan"}],
        [],
        "turn-delay-2",
        now,
    )
    row = store.active_loops("Heli")[0]
    assert row["next_eligible_at"].startswith("2026-10-15T00:00:00")
