# -*- coding: utf-8 -*-
"""PATCH 06 regression tests for adaptive bootstrap-memory routing."""

from __future__ import annotations

import asyncio

from memory.context_budget import (
    build_memory_context_budget,
    fit_recent_lines_to_budget,
)
from utils.tokenize import count_tokens


def _fit(lines, budget, *, pin_first=False):
    return asyncio.run(
        fit_recent_lines_to_budget(lines, budget, pin_first=pin_first)
    )


def test_unknown_context_keeps_legacy_path():
    assert build_memory_context_budget(None) is None
    assert build_memory_context_budget(0) is None
    assert build_memory_context_budget("not-a-number") is None
    assert build_memory_context_budget(1024) is None


def test_16k_profile_stays_close_to_patch05_but_gains_headroom():
    budget = build_memory_context_budget(16384)
    assert budget is not None
    assert budget.total_tokens == 6881
    # PATCH 05 used 2400 / 2400.  A common 16k local model should not see a
    # dramatic policy jump merely because adaptive routing became available.
    assert 2400 <= budget.persona_tokens <= 2600
    assert 2400 <= budget.reflection_tokens <= 2600
    assert budget.recent_ceiling_tokens >= 2000
    assert budget.total_tokens <= int(16384 * 0.43)


def test_large_context_scales_then_caps_to_avoid_memory_dumping():
    b16 = build_memory_context_budget(16384)
    b32 = build_memory_context_budget(32768)
    b64 = build_memory_context_budget(65536)
    assert b16 and b32 and b64
    assert b32.total_tokens > b16.total_tokens
    assert b32.persona_tokens > b16.persona_tokens
    assert b32.reflection_tokens > b16.reflection_tokens
    assert b32.recent_ceiling_tokens > b16.recent_ceiling_tokens
    # Long context is not a reason to stuff unlimited memory into the prompt.
    assert b32.total_tokens == 10000
    assert b64.total_tokens == b32.total_tokens
    assert b64.persona_tokens == b32.persona_tokens == 3500
    assert b64.reflection_tokens == b32.reflection_tokens == 3500
    assert b64.recent_ceiling_tokens == b32.recent_ceiling_tokens == 3200


def test_small_known_window_downscales_memory_instead_of_using_2400_each():
    budget = build_memory_context_budget(8192)
    assert budget is not None
    assert budget.persona_tokens < 2400
    assert budget.reflection_tokens < 2400
    assert budget.total_tokens < 4000


def test_recent_budget_is_hard_total_gate_after_actual_prefix_usage():
    budget = build_memory_context_budget(16384)
    assert budget is not None
    # Sparse persona/reflection lets recent borrow up to its own ceiling.
    assert budget.recent_tokens_after_prefix(1000) == budget.recent_ceiling_tokens
    # Dense/protected persona reduces the raw-recent lane deterministically.
    remaining = budget.content_tokens - 6000
    assert budget.recent_tokens_after_prefix(6000) == max(0, remaining)
    assert budget.recent_tokens_after_prefix(999999) == 0


def test_recent_selector_pins_memo_and_keeps_newest_raw_turns():
    memo = "SYSTEM | " + ("old-summary " * 60) + "\n"
    old = "User | " + ("old-turn " * 80) + "\n"
    middle = "Heli | " + ("middle-turn " * 80) + "\n"
    newest = "User | latest intent must survive\n"

    budget = 180
    kept = _fit([memo, old, middle, newest], budget, pin_first=True)

    assert kept
    assert kept[0].startswith("SYSTEM |")
    assert any("latest intent must survive" in line for line in kept)
    assert sum(count_tokens(line) for line in kept) <= budget
    # Old raw text should be the first thing sacrificed under pressure.
    assert not any("old-turn" in line for line in kept[1:])


def test_recent_selector_without_memo_prefers_newest_chronologically():
    lines = [
        "User | oldest " + ("x " * 70) + "\n",
        "Heli | middle " + ("y " * 70) + "\n",
        "User | newest\n",
    ]
    kept = _fit(lines, 90, pin_first=False)
    assert kept
    assert kept[-1].startswith("User | newest")
    # Returned order must remain chronological for the model.
    original_positions = [lines.index(line) for line in kept if line in lines]
    assert original_positions == sorted(original_positions)


def test_recent_selector_truncates_single_huge_latest_turn_head_and_tail():
    huge = "User | BEGIN " + ("middle " * 500) + "END\n"
    kept = _fit([huge], 80, pin_first=False)
    assert len(kept) == 1
    assert "BEGIN" in kept[0]
    assert "END" in kept[0]
    assert count_tokens(kept[0]) <= 80
