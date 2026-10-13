# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Adaptive bootstrap-memory budgeting for conversation context.

PATCH 06 keeps memory policy intentionally conservative:

* the character card / protected persona remains outside trimming just as before;
* persona + reflections are still ranked by the existing evidence scorer;
* recent history still prefers the latest raw turns and the compressed memo;
* the only new input is the model's *known* context-window capacity.

When capacity is known, `/new_dialog` receives one bounded budget for ordinary
memory rather than treating persona, reflections and recent history as unrelated
fixed pools.  Protected character-card identity keeps its existing exemption; if
it exceeds the ordinary envelope, recent history yields first rather than trimming
identity.  Unknown providers keep the legacy PATCH-05 limits unchanged.

The envelope is deliberately capped even for very large context models.  Long
contexts are not a licence to dump every memory into the prompt: retrieval
quality and position bias degrade when irrelevant material grows without bound.
Older/distant material remains available through the existing `recall_memory`
tool.
"""

from __future__ import annotations

from dataclasses import dataclass

from utils.tokenize import acount_tokens, atruncate_head_tail_tokens


# Hard policy knobs.  Keep these local to the router so the ordinary memory
# settings remain the legacy/fallback contract for unknown providers.
_MEMORY_WINDOW_FRACTION = 0.42
_MEMORY_TOTAL_MIN_TOKENS = 1600
_MEMORY_TOTAL_MAX_TOKENS = 10000
_MEMORY_TAIL_RESERVE_MAX_TOKENS = 256

# Section ceilings intentionally overlap.  The *total* envelope is the hard
# gate for ordinary memory; overlapping ceilings let recent history borrow spare
# capacity when the persona/reflection stores are small without another LLM call.
# Protected character-card persona remains exempt by the pre-existing contract.
_PERSONA_SHARE = 0.375
_REFLECTION_SHARE = 0.375
_RECENT_SHARE = 0.40
_PERSONA_MAX_TOKENS = 3500
_REFLECTION_MAX_TOKENS = 3500
_RECENT_MAX_TOKENS = 3200
_SECTION_MIN_TOKENS = 256

# Avoid treating absurd/corrupt values as a real model context window.
_MIN_REASONABLE_CONTEXT_WINDOW = 2048
_MAX_REASONABLE_CONTEXT_WINDOW = 4_000_000


@dataclass(frozen=True)
class MemoryContextBudget:
    """One deterministic memory-envelope decision for a session bootstrap."""

    context_window_tokens: int
    total_tokens: int
    tail_reserve_tokens: int
    persona_tokens: int
    reflection_tokens: int
    recent_ceiling_tokens: int

    @property
    def content_tokens(self) -> int:
        return max(0, self.total_tokens - self.tail_reserve_tokens)

    def recent_tokens_after_prefix(self, used_prefix_tokens: int) -> int:
        """Return what recent history may still spend after persona/prefix.

        The caller passes the real token count of everything already emitted
        inside the memory bootstrap block (persona/reflections + inner-thought
        headers).  This makes unused ordinary persona/reflection space flow into
        recent history while enforcing the ordinary-memory envelope.  Protected
        character-card entries can exceed that envelope by design; in that case
        this method reduces recent history to zero rather than trimming identity.
        """

        try:
            used = max(0, int(used_prefix_tokens))
        except (TypeError, ValueError, OverflowError):
            used = self.content_tokens
        remaining = max(0, self.content_tokens - used)
        return min(self.recent_ceiling_tokens, remaining)


def _valid_context_window(value: object) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not (_MIN_REASONABLE_CONTEXT_WINDOW <= parsed <= _MAX_REASONABLE_CONTEXT_WINDOW):
        return None
    return parsed


def build_memory_context_budget(context_window_tokens: object) -> MemoryContextBudget | None:
    """Build an adaptive memory budget for a known model context window.

    Unknown/invalid capacity returns ``None`` on purpose: all existing callers
    then retain the exact PATCH-05 static limits.  For a 16k window the result
    is intentionally close to PATCH 05, while 32k models get more memory and
    8k models get less so the bootstrap cannot crowd out the live conversation.
    """

    window = _valid_context_window(context_window_tokens)
    if window is None:
        return None

    total = round(window * _MEMORY_WINDOW_FRACTION)
    total = min(_MEMORY_TOTAL_MAX_TOKENS, max(_MEMORY_TOTAL_MIN_TOKENS, total))

    # Reserve a small tail for chat-gap / holiday / other fixed memory-server
    # annotations appended after recent history.  The reserve scales down for
    # tiny envelopes instead of consuming a fixed 256 tokens unconditionally.
    tail_reserve = min(_MEMORY_TAIL_RESERVE_MAX_TOKENS, max(96, total // 12))
    content = max(1, total - tail_reserve)

    persona = min(
        _PERSONA_MAX_TOKENS,
        max(_SECTION_MIN_TOKENS, round(content * _PERSONA_SHARE)),
    )
    reflection = min(
        _REFLECTION_MAX_TOKENS,
        max(_SECTION_MIN_TOKENS, round(content * _REFLECTION_SHARE)),
    )
    recent_ceiling = min(
        _RECENT_MAX_TOKENS,
        max(_SECTION_MIN_TOKENS, round(content * _RECENT_SHARE)),
    )

    return MemoryContextBudget(
        context_window_tokens=window,
        total_tokens=total,
        tail_reserve_tokens=tail_reserve,
        persona_tokens=persona,
        reflection_tokens=reflection,
        recent_ceiling_tokens=recent_ceiling,
    )


async def fit_recent_lines_to_budget(
    lines: list[str],
    max_tokens: int,
    *,
    pin_first: bool = False,
) -> list[str]:
    """Fit rendered recent-history lines into a token budget.

    Policy mirrors the existing compressed-history semantics:

    * when present, the first SystemMessage memo is pinned because it contains
      information already evicted from raw history;
    * raw turns are selected newest-first, then restored to chronological order;
    * if the newest single line is too large for the remaining space, keep its
      head *and* tail instead of dropping the user's latest intent entirely.

    Lines should already contain their speaker prefix and trailing newline; the
    function therefore budgets the exact text that `/new_dialog` will append.
    """

    try:
        budget = max(0, int(max_tokens))
    except (TypeError, ValueError, OverflowError):
        return []
    if budget <= 0 or not lines:
        return []

    source = [str(line) for line in lines if str(line)]
    if not source:
        return []

    kept_prefix: list[str] = []
    used = 0
    body_start = 0

    if pin_first:
        first = source[0]
        first_cost = await acount_tokens(first)
        # A compressed memo is valuable, but it must not consume the entire
        # recent lane when raw turns still exist.  Reserve roughly 45% for the
        # newest raw dialogue; if the memo is already small it naturally uses
        # less and the remainder flows to raw turns.
        first_allowance = budget
        if len(source) > 1:
            first_allowance = max(32, int(budget * 0.55))
        if first_cost > first_allowance:
            half = max(1, first_allowance // 2)
            first = await atruncate_head_tail_tokens(
                first, half, first_allowance - half,
            )
            first_cost = await acount_tokens(first)
        if first and first_cost <= budget:
            kept_prefix.append(first)
            used += first_cost
        body_start = 1

    kept_newest_reversed: list[str] = []
    remaining = max(0, budget - used)
    for line in reversed(source[body_start:]):
        cost = await acount_tokens(line)
        if cost <= remaining:
            kept_newest_reversed.append(line)
            used += cost
            remaining -= cost
            continue

        # Preserve at least a bounded slice of the newest raw message when no
        # raw line has fitted yet.  Once a newer raw line is already kept, an
        # older over-budget line is simply skipped: recency wins.
        if not kept_newest_reversed and remaining >= 32:
            head = max(1, remaining // 2)
            tail = max(0, remaining - head)
            trimmed = await atruncate_head_tail_tokens(line, head, tail)
            trimmed_cost = await acount_tokens(trimmed)
            if trimmed and trimmed_cost <= remaining:
                kept_newest_reversed.append(trimmed)
                used += trimmed_cost
                remaining -= trimmed_cost
        break

    kept_newest_reversed.reverse()
    return kept_prefix + kept_newest_reversed
