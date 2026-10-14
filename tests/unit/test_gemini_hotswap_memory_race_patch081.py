"""Regression coverage for PATCH 08.1: Gemini hot-swap/memory ordering."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from main_logic.hotswap_memory_barrier import (
    COMPLETION_KEY,
    await_memory_settlement,
    complete_memory_settlement,
)


@pytest.mark.asyncio
async def test_memory_settlement_barrier_reports_success():
    loop = asyncio.get_running_loop()
    completion = loop.create_future()
    message = {COMPLETION_KEY: completion}

    assert complete_memory_settlement(message, True) is True
    assert complete_memory_settlement(message, False) is False  # exactly once
    assert await await_memory_settlement(completion, timeout_seconds=0.1) is True


@pytest.mark.asyncio
async def test_memory_settlement_barrier_reports_failure():
    loop = asyncio.get_running_loop()
    completion = loop.create_future()
    complete_memory_settlement({COMPLETION_KEY: completion}, False)

    assert await await_memory_settlement(completion, timeout_seconds=0.1) is False


@pytest.mark.asyncio
async def test_memory_settlement_timeout_does_not_cancel_shared_future():
    loop = asyncio.get_running_loop()
    completion = loop.create_future()

    assert await await_memory_settlement(completion, timeout_seconds=0.001) is False
    assert not completion.cancelled()
    assert not completion.done()

    # The connector can still complete the same object after the background
    # waiter timed out, which is the whole reason for asyncio.shield().
    assert complete_memory_settlement({COMPLETION_KEY: completion}, True) is True
    assert completion.result() is True


@pytest.mark.asyncio
async def test_missing_settlement_ack_preserves_legacy_fast_path():
    assert await await_memory_settlement(None, timeout_seconds=0.1) is None


def test_hotswap_config_has_bounded_recovery_windows():
    root = Path(__file__).resolve().parents[2]
    settings = (root / "config/session_settings.py").read_text(encoding="utf-8")

    assert "HOTSWAP_MEMORY_SETTLEMENT_WAIT_SECONDS = 32.0" in settings
    assert "HOTSWAP_NEW_DIALOG_RECOVERY_TIMEOUT_SECONDS = 20.0" in settings


def test_integration_uses_ordered_barrier_and_gemini_context_compression():
    root = Path(__file__).resolve().parents[2]
    turn = (root / "main_logic/core/turn.py").read_text(encoding="utf-8")
    cross = (root / "main_logic/cross_server.py").read_text(encoding="utf-8")
    lifecycle = (root / "main_logic/core/lifecycle.py").read_text(encoding="utf-8")
    gemini = (
        root / "main_logic/omni_realtime_client/_gemini_support.py"
    ).read_text(encoding="utf-8")

    assert COMPLETION_KEY in turn
    assert "complete_memory_settlement(message" in cross
    assert "await await_memory_settlement(" in lifecycle
    assert "context_window_compression" in gemini
    assert "ContextWindowCompressionConfig" in gemini
    assert "SlidingWindow" in gemini
