from types import SimpleNamespace

import pytest

from main_logic.context_window import (
    build_context_window_budget,
    estimate_message_context_tokens,
    extract_lmstudio_loaded_context_window,
    resolve_context_window_tokens,
)


def test_extract_lmstudio_loaded_context_window_prefers_loaded_runtime_context():
    payload = {
        "models": [
            {
                "key": "prism-m1/bonsai-27b",
                "max_context_length": 131072,
                "loaded_instances": [
                    {
                        "id": "prism-m1/bonsai-27b",
                        "config": {"context_length": 16384},
                    }
                ],
            }
        ]
    }

    assert extract_lmstudio_loaded_context_window(
        payload, "prism-m1/bonsai-27b"
    ) == 16384


def test_extract_lmstudio_loaded_context_window_accepts_instance_identifier():
    payload = {
        "models": [
            {
                "key": "publisher/model",
                "loaded_instances": [
                    {"id": "my-loaded-alias", "config": {"context_length": 32768}}
                ],
            }
        ]
    }

    assert extract_lmstudio_loaded_context_window(payload, "my-loaded-alias") == 32768


def test_context_budget_uses_real_window_instead_of_legacy_5000_token_trigger():
    budget = build_context_window_budget(16384, 300)
    assert budget is not None
    assert budget.window_tokens == 16384
    assert budget.renewal_threshold_tokens > 12000
    assert budget.renewal_threshold_tokens < 16384


def test_unlimited_reply_budget_still_leaves_most_context_for_dialogue():
    budget = build_context_window_budget(32768, 999999)
    assert budget is not None
    assert budget.output_reserve_tokens >= 768
    assert budget.renewal_threshold_tokens > 20000


def test_estimate_message_context_tokens_counts_system_and_recent_reply_text():
    messages = [
        SimpleNamespace(content="system instructions"),
        SimpleNamespace(content="hello there"),
        SimpleNamespace(content=[
            {"type": "text", "text": "visible text"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]),
    ]

    count = estimate_message_context_tokens(messages)
    assert count > 0
    # Image payload must not be counted as raw base64 text.
    image_only = estimate_message_context_tokens([
        SimpleNamespace(content=[
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 20000}}
        ])
    ])
    assert image_only == 0


@pytest.mark.asyncio
async def test_remote_openai_compatible_endpoint_is_never_probed_as_lmstudio(monkeypatch):
    class _ShouldNotConstruct:
        def __init__(self, *args, **kwargs):
            raise AssertionError("remote endpoint must not be probed with LM Studio native API")

    monkeypatch.setattr("main_logic.context_window.httpx.AsyncClient", _ShouldNotConstruct)
    value = await resolve_context_window_tokens(
        base_url="https://example.com/v1",
        model="some-model",
        api_key="secret",
    )
    assert value is None
