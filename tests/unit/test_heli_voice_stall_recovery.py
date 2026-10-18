"""Heli voice turns must not strand the realtime response lane.

Only provider-neutral arbiter internals are exercised. No LLM, network,
memory, or speaker-model dependencies are needed.
"""

import asyncio

import pytest

from main_logic.omni_realtime_client._response_arbiter import RealtimeResponseArbiter


def _harness():
    sent = []
    holder = {}

    async def send(event):
        sent.append(dict(event))
        if event.get("type") == "response.create":
            index = sum(e["type"] == "response.create" for e in sent)
            response = {"id": f"response-{index}", "status": "completed"}
            holder["arbiter"].notify_response_created(
                {"type": "response.created", "response": response}
            )
            holder["arbiter"].notify_response_terminal(
                {"type": "response.done", "response": response}
            )

    arbiter = RealtimeResponseArbiter(send)
    holder["arbiter"] = arbiter
    return arbiter, sent


@pytest.mark.asyncio
async def test_orphan_pause_expires_and_reopens_response_queue():
    arbiter, sent = _harness()
    arbiter._dispatch_pause_timeout = 0.02
    try:
        arbiter.pause_dispatch("orphaned-asr-turn")
        ticket = await arbiter.enqueue(source="proactive")
        await asyncio.wait_for(ticket.sent, 1.0)
        assert arbiter._dispatch_allowed.is_set()
        assert [e["type"] for e in sent] == ["response.create"]
    finally:
        await arbiter.shutdown()


@pytest.mark.asyncio
async def test_live_owner_probe_keeps_pause_until_owner_retires():
    arbiter, sent = _harness()
    arbiter._dispatch_pause_timeout = 0.02
    arbiter._dispatch_pause_max_timeout = 0.2
    live = True
    arbiter.pause_owner_alive = lambda owner: live and owner == "active-asr"
    try:
        arbiter.pause_dispatch("active-asr")
        ticket = await arbiter.enqueue(source="proactive")
        await asyncio.sleep(0.06)
        assert not ticket.sent.done()
        assert sent == []
        live = False
        await asyncio.wait_for(ticket.sent, 1.0)
        assert arbiter._dispatch_allowed.is_set()
    finally:
        await arbiter.shutdown()


@pytest.mark.asyncio
async def test_stuck_preparation_aborts_transport_not_send_around_it():
    aborted = []
    sent = []

    async def send(event):
        sent.append(event)

    async def abort(reason):
        aborted.append(reason)

    arbiter = RealtimeResponseArbiter(send, abort_transport=abort)
    arbiter._dispatch_pause_timeout = 0.02
    try:
        token = arbiter.begin_turn_preparation("stuck-preparation")
        ticket = await arbiter.enqueue(source="external_asr")
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(ticket.sent, 1.0)
        assert aborted
        assert not arbiter._connection_available
        assert sent == []
        arbiter.end_turn_preparation(token)  # Late cleanup is generation-safe.
    finally:
        await arbiter.shutdown()


@pytest.mark.asyncio
async def test_older_preparation_cannot_unlock_replacement_connection():
    arbiter, sent = _harness()
    try:
        old_token = arbiter.begin_turn_preparation("old")
        arbiter.reset_connection_state()
        new_token = arbiter.begin_turn_preparation("new")
        arbiter.end_turn_preparation(old_token)
        assert arbiter._turn_preparations == 1

        ticket = await arbiter.enqueue(source="external_asr")
        arbiter.allow_ticket_while_paused(ticket)
        for _ in range(8):
            await asyncio.sleep(0)
        assert not ticket.sent.done()
        assert sent == []

        arbiter.end_turn_preparation(new_token)
        await asyncio.wait_for(ticket.sent, 1.0)
        assert [e["type"] for e in sent] == ["response.create"]
    finally:
        await arbiter.shutdown()


@pytest.mark.asyncio
async def test_prior_user_ticket_bypasses_newer_pause_without_proactive_leak():
    arbiter, sent = _harness()
    try:
        arbiter.pause_dispatch("new-asr")
        proactive = await arbiter.enqueue(source="proactive", priority=10)
        earlier_user = await arbiter.enqueue(source="external_asr", priority=0)
        arbiter.allow_ticket_while_paused(earlier_user)
        await asyncio.wait_for(earlier_user.sent, 1.0)
        assert not proactive.sent.done()
        assert not arbiter._dispatch_allowed.is_set()
        assert [e["type"] for e in sent] == ["response.create"]
        arbiter.resume_dispatch()
        await asyncio.wait_for(proactive.sent, 1.0)
        assert [e["type"] for e in sent] == [
            "response.create", "response.create"
        ]
    finally:
        await arbiter.shutdown()
