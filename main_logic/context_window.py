# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""Adaptive context-window discovery and session rollover policy.

The old dialog lifecycle renewed text sessions after either 10 user turns or
5k visible conversation tokens.  That was intentionally conservative, but it
also discarded useful raw context long before large/local models needed it.

This module keeps the safety boundary separate from semantic memory policy:

* discover the *loaded* model context window when the provider can report it;
* let the live dialog grow until that real window is nearly full;
* reserve space for the next completion plus transport/tool/template overhead;
* fall back to the legacy fixed thresholds when capacity is unknown.

Older/distant memory remains available through the existing ``recall_memory``
tool.  The model therefore gets to use as much live context as its current
runtime can actually hold, without pretending it can choose text Core never
sent to it.
"""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpx

from utils.tokenize import count_tokens


# A local native-model probe should never make session startup noticeably slower.
_LM_STUDIO_PROBE_TIMEOUT_SECONDS = 1.25
_LM_STUDIO_CACHE_TTL_SECONDS = 30.0
_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}

# Capacity is a hard physical constraint, not a personality setting.  Keep a
# modest transport/tool/template reserve, then leave the rest to the live chat.
# This is deliberately a fraction of the *actual loaded window*, not a magic
# absolute prompt size such as the old 5000-token trigger.
_CONTEXT_SAFETY_FRACTION = 0.10
_UNLIMITED_RESPONSE_SENTINEL = 100_000


@dataclass(frozen=True)
class ContextWindowBudget:
    window_tokens: int
    renewal_threshold_tokens: int
    output_reserve_tokens: int
    safety_reserve_tokens: int


_context_window_cache: dict[tuple[str, str], tuple[float, int | None]] = {}
_cache_lock = asyncio.Lock()


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _native_lmstudio_models_url(base_url: str) -> str | None:
    """Return LM Studio's native v1 models URL for a *local* endpoint only.

    We intentionally do not probe arbitrary remote OpenAI-compatible servers:
    ``/api/v1/models`` is LM Studio-specific and credentials must never be sent
    to a guessed third-party route.
    """

    try:
        parts = urlsplit(str(base_url or "").strip())
    except ValueError:
        return None
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        return None
    if parts.hostname.lower() not in _LOCAL_HOSTS:
        return None
    if not parts.netloc:
        return None
    return f"{parts.scheme}://{parts.netloc}/api/v1/models"


def extract_lmstudio_loaded_context_window(payload: Any, model: str) -> int | None:
    """Extract the loaded instance ``context_length`` for ``model``.

    LM Studio v1 reports model records with ``key`` plus ``loaded_instances``;
    each loaded instance has ``config.context_length``.  The configured N.E.K.O
    model id may be either the model key or an explicit loaded-instance id, so
    both are accepted.  We never use ``max_context_length`` here: the loaded
    runtime may have been intentionally started with a smaller n_ctx.
    """

    if not isinstance(payload, dict):
        return None
    rows = payload.get("models")
    if not isinstance(rows, list):
        return None

    wanted = str(model or "").strip()
    if not wanted:
        return None

    # First prefer an exact loaded-instance id match.
    for row in rows:
        if not isinstance(row, dict):
            continue
        instances = row.get("loaded_instances")
        if not isinstance(instances, list):
            continue
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            if str(instance.get("id") or "").strip() != wanted:
                continue
            config = instance.get("config")
            if isinstance(config, dict):
                value = _positive_int(config.get("context_length"))
                if value:
                    return value

    # Then match the configured model key and use its loaded instance.
    for row in rows:
        if not isinstance(row, dict):
            continue
        key = str(row.get("key") or row.get("id") or "").strip()
        if key != wanted:
            continue
        instances = row.get("loaded_instances")
        if not isinstance(instances, list):
            return None
        loaded = [item for item in instances if isinstance(item, dict)]
        if not loaded:
            return None
        # Prefer an instance whose id mirrors the key; otherwise the first
        # loaded instance belongs to this exact model record.
        loaded.sort(key=lambda item: str(item.get("id") or "") != wanted)
        for instance in loaded:
            config = instance.get("config")
            if not isinstance(config, dict):
                continue
            value = _positive_int(config.get("context_length"))
            if value:
                return value
    return None


async def resolve_context_window_tokens(
    *,
    base_url: str,
    model: str,
    api_key: str | None = None,
) -> int | None:
    """Best-effort discovery of the current model's *loaded* context window.

    Today the automatic provider adapter is intentionally narrow: local LM
    Studio exposes the exact loaded ``context_length`` through its native v1
    API.  Unknown/cloud endpoints return ``None`` and keep N.E.K.O's legacy
    rollover policy until provider metadata is added explicitly.
    """

    models_url = _native_lmstudio_models_url(base_url)
    if not models_url:
        return None

    cache_key = (models_url, str(model or "").strip())
    now = time.monotonic()
    async with _cache_lock:
        cached = _context_window_cache.get(cache_key)
        if cached and now - cached[0] <= _LM_STUDIO_CACHE_TTL_SECONDS:
            return cached[1]

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    value: int | None = None
    try:
        async with httpx.AsyncClient(
            timeout=_LM_STUDIO_PROBE_TIMEOUT_SECONDS,
            trust_env=False,
        ) as client:
            response = await client.get(models_url, headers=headers)
            response.raise_for_status()
            value = extract_lmstudio_loaded_context_window(response.json(), model)
    except (httpx.HTTPError, ValueError, TypeError):
        value = None

    async with _cache_lock:
        _context_window_cache[cache_key] = (time.monotonic(), value)
    return value


def build_context_window_budget(
    context_window_tokens: int | None,
    max_response_tokens: int | None,
) -> ContextWindowBudget | None:
    """Calculate the rollover high-water mark for a known context window."""

    window = _positive_int(context_window_tokens)
    if not window:
        return None

    safety = max(256, int(math.ceil(window * _CONTEXT_SAFETY_FRACTION)))

    response = _positive_int(max_response_tokens)
    if response is None or response >= _UNLIMITED_RESPONSE_SENTINEL:
        # Unlimited user setting cannot mean "reserve the whole window".  Give
        # a free-form model a reasonable completion lane while preserving the
        # majority of its context for dialog continuity.
        output = max(768, int(math.ceil(window * 0.15)))
    else:
        # A configured response guard is already the best statement of how much
        # completion space the user expects.  Clamp only pathological values so
        # a reply cap cannot consume most of the model window by itself.
        output = min(max(128, response), max(256, int(window * 0.25)))

    threshold = window - safety - output
    # Very small windows still need a usable prompt lane.  If the arithmetic
    # leaves less than half the window, renew at half rather than at an absurd
    # negative/near-zero value.
    threshold = max(int(window * 0.50), threshold)
    threshold = min(threshold, window - 1)
    return ContextWindowBudget(
        window_tokens=window,
        renewal_threshold_tokens=max(1, threshold),
        output_reserve_tokens=output,
        safety_reserve_tokens=safety,
    )


def estimate_message_context_tokens(messages: Any) -> int:
    """Token-count visible text from a LangChain-style message list.

    Provider usage is more accurate (it includes templates/tools/images), but
    it describes the request *before* the just-generated assistant reply.  This
    estimate includes the current persisted reply; callers should take the max
    of both values.
    """

    total = 0
    for message in list(messages or []):
        content = getattr(message, "content", "")
        if isinstance(content, str):
            text = content
        elif isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    kind = str(item.get("type") or "")
                    if kind in {"text", "input_text", "output_text"}:
                        parts.append(str(item.get("text") or ""))
                else:
                    parts.append(str(item))
            text = "\n".join(parts)
        else:
            text = str(content or "")
        if text:
            total += count_tokens(text)
    return total
