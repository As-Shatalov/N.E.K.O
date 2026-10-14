# -*- coding: utf-8 -*-
from __future__ import annotations

from datetime import datetime, timedelta

from memory.relationship_state import build_relationship_continuity


def _reflection(
    text: str,
    created_at: datetime,
    relation_type: str = "dynamic",
    *,
    status: str = "confirmed",
    entity: str = "relationship",
    temporal_scope: str = "pattern",
):
    return {
        "id": f"r_{abs(hash((text, created_at.isoformat()))) % 100000}",
        "text": text,
        "entity": entity,
        "relation_type": relation_type,
        "status": status,
        "temporal_scope": temporal_scope,
        "created_at": created_at.isoformat(),
    }


def test_character_card_relationship_does_not_fake_learned_continuity():
    persona = {
        "relationship": {
            "facts": [
                {
                    "text": "They are siblings and have always been close.",
                    "protected": True,
                    "created_at": "2026-01-01T00:00:00",
                }
            ]
        }
    }
    assert build_relationship_continuity(persona, []) is None


def test_many_same_day_memories_do_not_create_fast_intimacy():
    now = datetime(2026, 10, 14, 12, 0, 0)
    reflections = [
        _reflection(f"same-day relationship event {i}", now - timedelta(minutes=i), "shared_memory")
        for i in range(12)
    ]
    state = build_relationship_continuity({}, reflections, now=now)
    assert state is not None
    assert state.evidence_count == 12
    assert state.distinct_days == 1
    assert state.band == "forming"


def test_developing_requires_evidence_across_multiple_days():
    now = datetime(2026, 10, 14, 12, 0, 0)
    reflections = [
        _reflection("pattern one", now - timedelta(days=3), "dynamic"),
        _reflection("shared one", now - timedelta(days=2), "shared_memory"),
        _reflection("agreement one", now - timedelta(days=1), "agreement"),
        _reflection("pattern two", now, "dynamic"),
    ]
    state = build_relationship_continuity({}, reflections, now=now)
    assert state is not None
    assert state.band == "developing"
    assert state.distinct_days == 4
    assert state.span_days == 3


def test_established_requires_time_volume_and_shared_history_anchors():
    now = datetime(2026, 10, 14, 12, 0, 0)
    kinds = [
        "shared_memory",
        "milestone",
        "agreement",
        "dynamic",
        "dynamic",
        "shared_memory",
        "dynamic",
        "agreement",
    ]
    offsets = [21, 18, 14, 10, 7, 4, 2, 0]
    reflections = [
        _reflection(f"relationship evidence {i}", now - timedelta(days=days), kind)
        for i, (days, kind) in enumerate(zip(offsets, kinds))
    ]
    state = build_relationship_continuity({}, reflections, now=now)
    assert state is not None
    assert state.band == "established"
    assert state.evidence_count == 8
    assert state.distinct_days == 8
    assert state.span_days == 21
    assert state.anchor_count >= 2


def test_promoted_persona_and_reflection_duplicate_counts_once():
    now = datetime(2026, 10, 14, 12, 0, 0)
    text = "They routinely tease each other during technical work."
    persona = {
        "relationship": {
            "facts": [
                {
                    "text": text,
                    "created_at": (now - timedelta(days=10)).isoformat(),
                    "relation_type": "dynamic",
                }
            ]
        }
    }
    reflections = [
        _reflection(text, now - timedelta(days=10), "dynamic", status="promoted")
    ]
    state = build_relationship_continuity(persona, reflections, now=now)
    assert state is not None
    assert state.evidence_count == 1
    assert state.dynamic_count == 1


def test_non_relationship_reflections_do_not_affect_relationship_state():
    now = datetime(2026, 10, 14, 12, 0, 0)
    reflections = [
        _reflection("user likes porridge", now, "dynamic", entity="master"),
        _reflection("AI likes hot chocolate", now, "dynamic", entity="neko"),
    ]
    assert build_relationship_continuity({}, reflections, now=now) is None


def test_unconfirmed_relationship_reflection_is_ignored():
    now = datetime(2026, 10, 14, 12, 0, 0)
    pending = _reflection("maybe a pattern", now, "dynamic", status="pending")
    assert build_relationship_continuity({}, [pending], now=now) is None


def test_expired_tension_does_not_count_as_active(monkeypatch):
    import sys
    import types

    now = datetime(2026, 10, 14, 12, 0, 0)
    temporal = types.ModuleType("memory.temporal")
    temporal.is_past_for_render = lambda entry, now=None: True
    monkeypatch.setitem(sys.modules, "memory.temporal", temporal)

    reflections = [
        _reflection(
            "They had a disagreement during an earlier episode.",
            now - timedelta(days=10),
            "tension",
            temporal_scope="episode",
        )
    ]
    state = build_relationship_continuity({}, reflections, now=now)
    assert state is not None
    assert state.evidence_count == 1
    assert state.active_tension_count == 0


def test_relationship_continuity_renderer_supports_all_locales():
    from config.prompts.prompts_memory import render_relationship_continuity_block

    for lang in ("zh", "zh-TW", "en", "ja", "ko", "ru", "es", "pt"):
        rendered = render_relationship_continuity_block(
            lang,
            band="developing",
            evidence_count=5,
            distinct_days=3,
            span_days=4,
            shared_memory_count=1,
            milestone_count=1,
            agreement_count=1,
            active_tension_count=0,
        )
        assert "<RelationshipContinuity>" in rendered
        assert "5" in rendered
        assert "</RelationshipContinuity>" in rendered


def test_malformed_legacy_rows_degrade_without_crashing():
    persona = {
        "relationship": {
            "facts": [
                None,
                {"text": 12345, "created_at": ["bad"], "relation_type": "dynamic"},
                {"text": "   ", "created_at": "not-a-date"},
            ]
        }
    }
    state = build_relationship_continuity(persona, [{"entity": "relationship"}])
    assert state is not None
    assert state.evidence_count == 1
    assert state.band == "forming"


def test_unknown_renderer_band_fails_closed_to_forming():
    from config.prompts.prompts_memory import render_relationship_continuity_block

    rendered = render_relationship_continuity_block(
        "en",
        band="instant_soulmate",
        evidence_count=99,
        distinct_days=99,
        span_days=99,
        shared_memory_count=99,
        milestone_count=99,
        agreement_count=99,
        active_tension_count=0,
    )
    assert "forming" in rendered
    assert "instant_soulmate" not in rendered
