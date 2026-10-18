"""External ASR turn dispatch pauses must survive failed submissions.

``prepare_external_voice_turn`` pauses arbiter dispatch under a per-turn
pause id and ``submit_external_text_turn`` briefly resumes dispatch so an
older completed turn can flow ahead of a newer paused turn (WARM_IDLE
overlap). A previous turn that has not sent its response yet must not be
cancelled by the newer turn, and proactive work must remain paused until the
new voice turn completes.
"""

import asyncio
import os
import sys
from unittest.mock import AsyncMock

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "../../")))

from main_logic.omni_realtime_client import OmniRealtimeClient
from main_logic.omni_realtime_client._response_arbiter import RealtimeResponseArbiter


def _make_client(send) -> tuple[OmniRealtimeClient, RealtimeResponseArbiter]:
    client = OmniRealtimeClient.__new__(OmniRealtimeClient)
    client._is_gemini = False
    client._connection_generation = 0
    arbiter = RealtimeResponseArbiter(send)
    client._response_arbiter = arbiter
    return client, arbiter


async def test_failed_submit_rearms_newer_turn_pause():
    sent_events: list[dict] = []
    arbiter: RealtimeResponseArbiter | None = None

    async def send(event):
        if event["type"] == "conversation.item.create":
            raise RuntimeError("transport send failed")
        sent_events.append(dict(event))
        if event["type"] == "response.create":
            arbiter.notify_response_created({})
            arbiter.notify_response_terminal({})

    client, arbiter = _make_client(send)
    # A newer turn prepared before this older turn's final dispatch.
    client._external_voice_turn_pause_id = "turn-new"
    arbiter.pause_dispatch()

    with pytest.raises(RuntimeError):
        await client.submit_external_text_turn("hello", turn_id="turn-old")

    # The failure path must restore the newer turn's pause.
    assert client._external_voice_turn_pause_id == "turn-new"
    assert not arbiter._dispatch_allowed.is_set()

    # Queued proactive work stays gated behind the restored pause.
    proactive = await arbiter.enqueue(source="proactive")
    for _ in range(10):
        await asyncio.sleep(0)
    assert proactive.sent.done() is False
    assert sent_events == []

    client.abandon_external_voice_turn("turn-new")
    await asyncio.wait_for(proactive.sent, 1)
    assert sent_events[-1]["type"] == "response.create"
    await arbiter.shutdown()


async def test_newer_prepare_preserves_older_unsent_response_and_pauses_proactive():
    sent_events: list[dict] = []
    arbiter: RealtimeResponseArbiter | None = None
    release_item_send = asyncio.Event()

    async def send(event):
        if event["type"] == "conversation.item.create":
            await release_item_send.wait()
        sent_events.append(dict(event))
        if event["type"] == "response.create":
            arbiter.notify_response_created({})
            arbiter.notify_response_terminal({})

    client, arbiter = _make_client(send)
    client.handle_interruption = AsyncMock()

    submit = asyncio.create_task(
        client.submit_external_text_turn("hello", turn_id="turn-old")
    )
    for _ in range(50):
        if arbiter.current_source == "external_asr":
            break
        await asyncio.sleep(0)
    assert arbiter.current_source == "external_asr"

    # The next turn must not cancel an older user sentence before response.create.
    prepare = asyncio.create_task(
        client.prepare_external_voice_turn(turn_id="turn-new")
    )
    for _ in range(10):
        await asyncio.sleep(0)
    release_item_send.set()
    await asyncio.wait_for(prepare, 2)
    await asyncio.wait_for(submit, 2)

    # The old answer was dispatched, but the new user's pause survives.
    assert client._external_voice_turn_pause_id == "turn-new"
    assert not arbiter._dispatch_allowed.is_set()
    assert sum(
        event["type"] == "response.create" for event in sent_events
    ) == 1

    proactive = await arbiter.enqueue(source="proactive")
    for _ in range(6):
        await asyncio.sleep(0)
    assert not proactive.sent.done()
    client.abandon_external_voice_turn("turn-new")
    await asyncio.wait_for(proactive.sent, 2)
    await arbiter.shutdown()
