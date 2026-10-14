# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
"""In-process acknowledgement barrier for hot-swap memory settlement.

The main session and ``cross_server`` share one asyncio event loop.  When a
realtime session reaches its renewal threshold, the core queues ``renew session``
and starts warming a replacement session shortly afterwards.  Memory renewal may
need an LLM summary and therefore can outlive the historical fixed prewarm delay.

This module provides the tiny, dependency-free ordering primitive used to wait
for the actual ``/renew`` or ``/settle`` completion without blocking the current
live session and without making an asyncio Future cross a process boundary.
"""

from __future__ import annotations

import asyncio
from typing import Any


COMPLETION_KEY = "_renew_memory_settlement_done"


def complete_memory_settlement(message: dict[str, Any], success: bool) -> bool:
    """Resolve the settlement Future carried by an internal sync-queue item.

    Returns ``True`` only when this call performed the transition.  Duplicate
    completion attempts are intentionally harmless; shutdown/error/finally paths
    may race while unwinding.
    """
    completion = message.get(COMPLETION_KEY)
    if not isinstance(completion, asyncio.Future) or completion.done():
        return False
    completion.set_result(bool(success))
    return True


async def await_memory_settlement(
    completion: Any,
    *,
    timeout_seconds: float,
) -> bool | None:
    """Wait for ordered memory settlement without cancelling the shared Future.

    Returns:
        ``True``  if ``/renew`` or ``/settle`` reported success.
        ``False`` if settlement failed, raised, or was not confirmed before the
        timeout.  The caller may then use a slower recovery ``/new_dialog`` path.
        ``None``  if no completion object exists (legacy/non-renew preparation),
        preserving the ordinary fast path.
    """
    if not isinstance(completion, asyncio.Future):
        return None

    if completion.done():
        try:
            return bool(completion.result())
        except Exception:
            return False

    try:
        return bool(
            await asyncio.wait_for(
                asyncio.shield(completion),
                timeout=max(0.0, float(timeout_seconds)),
            )
        )
    except asyncio.TimeoutError:
        return False
    except Exception:
        # Do not propagate connector/settlement failure into pending-session
        # preparation.  The lifecycle layer intentionally fails open with a
        # longer /new_dialog timeout, while the current session stays live.
        return False
