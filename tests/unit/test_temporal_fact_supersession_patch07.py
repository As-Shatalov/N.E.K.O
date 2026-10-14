from __future__ import annotations

import json
from pathlib import Path

import pytest

from config.prompts.prompts_memory import get_fact_dedup_prompt
from memory.fact_dedup import FactDedupResolver
from memory.facts import _merge_archive_entries
from memory.hybrid_recall import (
    _aload_archive_facts,
    _entry_event_window,
    _invalidate_pool_cache,
)


class _FakeFactStore:
    def __init__(self, facts):
        self._facts = facts
        self._config_manager = object()
        self.archive_call = None

    async def aload_facts(self, name):
        return self._facts

    async def aarchive_arbitrated_facts(
        self,
        name,
        archive_specs,
        *,
        survivor_updates=None,
        expected_survivors=None,
        expected_losers=None,
    ):
        self.archive_call = {
            "name": name,
            "archive_specs": archive_specs,
            "survivor_updates": survivor_updates or {},
            "expected_survivors": expected_survivors or {},
            "expected_losers": expected_losers or {},
        }
        return len(archive_specs)


class _ArchiveStore:
    def __init__(self, path: Path):
        self.path = path

    def _facts_archive_path(self, name):
        return str(self.path)


@pytest.mark.asyncio
async def test_supersede_keeps_new_fact_and_records_temporal_lineage():
    old = {
        "id": "fact_old",
        "text": "Пользователь предпочитает Unity",
        "importance": 7,
        "entity": "master",
        "created_at": "2026-09-01T10:00:00+00:00",
        "belief_valid_from_at": "2026-09-01T10:00:00+00:00",
    }
    new = {
        "id": "fact_new",
        "text": "Пользователь теперь предпочитает Blender",
        "importance": 5,
        "entity": "master",
        "created_at": "2026-10-13T12:00:00+00:00",
    }
    store = _FakeFactStore([old, new])
    resolver = FactDedupResolver(store)

    applied, processed = await resolver._aapply_decisions(
        "Heli",
        [{"candidate_id": "fact_new", "existing_id": "fact_old"}],
        [{"index": 0, "action": "supersede"}],
    )

    assert applied == 1
    assert processed
    assert store.archive_call is not None

    specs = list(store.archive_call["archive_specs"].values())
    assert len(specs) == 1
    spec = specs[0]
    assert spec["reason"] == "fact_temporal_supersede"
    assert spec["superseded_by"] == "fact_new"
    assert spec["supersession_kind"] == "temporal_update"
    assert spec["belief_valid_from_at"] == "2026-09-01T10:00:00+00:00"
    assert spec["belief_valid_to_at"] == "2026-10-13T12:00:00+00:00"

    survivors = list(store.archive_call["survivor_updates"].values())
    assert len(survivors) == 1
    survivor = survivors[0]
    assert survivor["id"] == "fact_new"
    assert survivor["supersedes_fact_id"] == "fact_old"
    assert survivor["supersession_kind"] == "temporal_update"
    assert survivor["belief_valid_from_at"] == "2026-10-13T12:00:00+00:00"
    assert survivor["belief_valid_to_at"] is None
    # Temporal updates are lineage, not paraphrase consolidation.
    assert "fact_old" not in (survivor.get("merged_from_ids") or [])
    # Preserve importance across a state transition.
    assert survivor["importance"] == 7


@pytest.mark.asyncio
async def test_archive_loader_only_surfaces_temporal_arbitration_for_historical_recall(tmp_path):
    path = tmp_path / "facts_archive.json"
    rows = [
        {"id": "absorbed", "text": "old episode", "created_at": "2026-01-01T00:00:00"},
        {
            "id": "wording_replace",
            "text": "bad old wording",
            "created_at": "2026-01-01T00:00:00",
            "arbitration_archived_at": "2026-02-01T00:00:00",
            "arbitration_reason": "fact_dedup_replace",
            "superseded_by": "better_wording",
        },
        {
            "id": "old_state",
            "text": "preferred Unity",
            "created_at": "2026-01-01T00:00:00",
            "arbitration_archived_at": "2026-10-13T12:00:00",
            "arbitration_reason": "fact_temporal_supersede",
            "superseded_by": "new_state",
            "supersession_kind": "temporal_update",
            "belief_valid_from_at": "2026-01-01T00:00:00",
            "belief_valid_to_at": "2026-10-13T12:00:00",
        },
        {
            "id": "forgotten_subject",
            "text": "must never recall",
            "subject_archived_at": "2026-03-01T00:00:00",
        },
    ]
    path.write_text(json.dumps(rows), encoding="utf-8")
    store = _ArchiveStore(path)
    _invalidate_pool_cache(str(path))

    current = await _aload_archive_facts(store, "Heli")
    assert {row["id"] for row in current} == {"absorbed"}

    historical = await _aload_archive_facts(
        store, "Heli", include_temporal_superseded=True,
    )
    assert {row["id"] for row in historical} == {"absorbed", "old_state"}
    old_state = next(row for row in historical if row["id"] == "old_state")
    assert old_state["belief_valid_to_at"] == "2026-10-13T12:00:00"
    assert old_state["superseded_by"] == "new_state"


@pytest.mark.parametrize("lang", ["zh", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"])
def test_all_dedup_prompts_expose_supersede_action(lang):
    prompt = get_fact_dedup_prompt(lang)
    assert "supersede" in prompt


def test_temporal_lineage_uses_belief_interval_not_point_event_time():
    archived = {
        "id": "old_state",
        "created_at": "2026-01-01T00:00:00",
        "event_start_at": "2026-01-01T00:00:00",
        "event_end_at": None,
        "supersession_kind": "temporal_update",
        "belief_valid_from_at": "2026-01-01T00:00:00",
        "belief_valid_to_at": "2026-06-01T00:00:00",
    }
    start, end = _entry_event_window(archived)
    assert start.isoformat() == "2026-01-01T00:00:00"
    assert end.isoformat() == "2026-06-01T00:00:00"


def test_archive_merge_preserves_temporal_arbitration_markers_after_half_commit():
    previous = {
        "id": "fact_old",
        "text": "old state",
        "arbitration_archived_at": "2026-10-13T12:00:00",
        "arbitration_reason": "fact_temporal_supersede",
        "superseded_by": "fact_new",
        "supersession_kind": "temporal_update",
        "belief_valid_from_at": "2026-09-01T00:00:00",
        "belief_valid_to_at": "2026-10-13T12:00:00",
        "temporal_superseded_at": "2026-10-13T12:00:01",
    }
    # Simulate an archive-first crash leaving the loser active, followed by a
    # later archive pass that sees the newer active copy without markers.
    incoming = {"id": "fact_old", "text": "old state", "absorbed": True}
    merged = _merge_archive_entries([previous], [incoming])
    assert len(merged) == 1
    row = merged[0]
    assert row["absorbed"] is True
    assert row["supersession_kind"] == "temporal_update"
    assert row["belief_valid_to_at"] == "2026-10-13T12:00:00"
    assert row["superseded_by"] == "fact_new"
