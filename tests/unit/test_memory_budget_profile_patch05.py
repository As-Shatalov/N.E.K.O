"""Contract tests for fork PATCH 05 memory-context headroom.

The patch intentionally raises existing safety ceilings by a modest amount;
it does not remove truncation or make the memory renderer unbounded.
"""
from __future__ import annotations

import inspect

from config import memory_settings as ms
from memory.recent import CompressedRecentHistoryManager
from memory.persona import rendering as persona_rendering


def test_patch05_private_memory_budgets_are_modestly_larger() -> None:
    assert ms.PERSONA_RENDER_MAX_TOKENS == 2400
    assert ms.REFLECTION_RENDER_MAX_TOKENS == 2400
    assert ms.RECENT_HISTORY_MAX_ITEMS == 12
    assert ms.RECENT_COMPRESS_THRESHOLD_ITEMS == 24
    assert ms.RECENT_SUMMARY_MAX_TOKENS == 1200
    assert ms.RECENT_PER_MESSAGE_MAX_TOKENS == 600
    assert ms.RECENT_COMPRESS_INPUT_BUDGET_TOKENS == 9600
    assert ms.REFLECTION_TEXT_MAX_TOKENS == 180


def test_patch05_recent_manager_defaults_follow_the_config() -> None:
    signature = inspect.signature(CompressedRecentHistoryManager.__init__)
    assert signature.parameters["max_history_length"].default == ms.RECENT_HISTORY_MAX_ITEMS
    assert signature.parameters["compress_threshold"].default == ms.RECENT_COMPRESS_THRESHOLD_ITEMS

    # Keep meaningful breathing room after a compression instead of immediately
    # re-compressing the next couple of messages.
    assert ms.RECENT_COMPRESS_THRESHOLD_ITEMS >= 2 * ms.RECENT_HISTORY_MAX_ITEMS


def test_patch05_scoped_budget_preserves_four_full_subjects() -> None:
    per_subject = ms.PERSONA_RENDER_MAX_TOKENS + ms.REFLECTION_RENDER_MAX_TOKENS
    assert ms.SCOPED_RENDER_TOTAL_MAX_TOKENS == 4 * per_subject
    assert ms.SCOPED_RENDER_TOTAL_MAX_TOKENS == 19200


def test_patch05_renderer_imports_the_same_budget_contract() -> None:
    assert persona_rendering.PERSONA_RENDER_MAX_TOKENS == ms.PERSONA_RENDER_MAX_TOKENS
    assert persona_rendering.REFLECTION_RENDER_MAX_TOKENS == ms.REFLECTION_RENDER_MAX_TOKENS
    assert persona_rendering.SCOPED_RENDER_TOTAL_MAX_TOKENS == ms.SCOPED_RENDER_TOTAL_MAX_TOKENS
