"""Patch 12.0.1: text-stream HTTP 400 negotiation without losing safety limits."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from utils.llm_client.stream_compat_12_0_1 import (
    _CompatibleAsyncClient,
    _CompatibleCompletions,
    unsupported_stream_parameter,
)


class ProviderError(Exception):
    def __init__(self, status: int, message: str, *, body=None):
        super().__init__(message)
        self.status_code = status
        self.body = body


def _owner():
    return SimpleNamespace(
        _compat_without_stream_options=False,
        _compat_with_max_tokens=False,
    )


def _kwargs():
    return {
        "model": "test-model",
        "messages": [{"role": "user", "content": "hello"}],
        "stream": True,
        "stream_options": {"include_usage": True},
        "max_completion_tokens": 200,
    }


@pytest.mark.asyncio
async def test_stream_options_400_retries_without_changing_text_or_budget():
    raw = SimpleNamespace(create=AsyncMock(side_effect=[
        ProviderError(400, "Unrecognized request argument supplied: stream_options"),
        "OPEN_STREAM",
    ]))
    owner = _owner()
    wrapper = _CompatibleCompletions(raw, owner)

    assert await wrapper.create(**_kwargs()) == "OPEN_STREAM"
    assert raw.create.await_count == 2
    first, second = [c.kwargs for c in raw.create.await_args_list]
    assert first["stream_options"] == {"include_usage": True}
    assert "stream_options" not in second
    assert first["messages"] == second["messages"]
    assert second["max_completion_tokens"] == 200
    assert owner._compat_without_stream_options is True

    # One discovery cost only; subsequent requests never include the field.
    raw.create.reset_mock()
    raw.create.side_effect = None
    raw.create.return_value = "NEXT_STREAM"
    assert await wrapper.create(**_kwargs()) == "NEXT_STREAM"
    assert raw.create.await_args.kwargs.get("stream_options") is None


@pytest.mark.asyncio
async def test_two_sequential_400s_recover_without_duplicate_generation():
    raw = SimpleNamespace(create=AsyncMock(side_effect=[
        ProviderError(400, 'unknown field "stream_options"'),
        ProviderError(400, 'unsupported parameter "max_completion_tokens"'),
        "STREAM",
    ]))
    owner = _owner()
    assert await _CompatibleCompletions(raw, owner).create(**_kwargs()) == "STREAM"
    assert raw.create.await_count == 3
    final = raw.create.await_args.kwargs
    assert "stream_options" not in final
    assert "max_completion_tokens" not in final
    assert final["max_tokens"] == 200
    assert owner._compat_without_stream_options
    assert owner._compat_with_max_tokens


@pytest.mark.asyncio
async def test_400_unknown_model_is_not_hidden_or_retried():
    failure = ProviderError(400, "The model does not exist")
    raw = SimpleNamespace(create=AsyncMock(side_effect=failure))
    owner = _owner()
    with pytest.raises(ProviderError) as caught:
        await _CompatibleCompletions(raw, owner).create(**_kwargs())
    assert caught.value is failure
    assert raw.create.await_count == 1
    assert not owner._compat_with_max_tokens


@pytest.mark.asyncio
async def test_400_invalid_token_limit_is_not_retried():
    failure = ProviderError(400, "max_completion_tokens must be <= 100")
    raw = SimpleNamespace(create=AsyncMock(side_effect=failure))
    with pytest.raises(ProviderError):
        await _CompatibleCompletions(raw, _owner()).create(**_kwargs())
    assert raw.create.await_count == 1


@pytest.mark.asyncio
async def test_429_is_never_retried_as_schema_issue():
    failure = ProviderError(429, "unsupported stream_options, rate limit reached")
    raw = SimpleNamespace(create=AsyncMock(side_effect=failure))
    with pytest.raises(ProviderError):
        await _CompatibleCompletions(raw, _owner()).create(**_kwargs())
    assert raw.create.await_count == 1


@pytest.mark.asyncio
async def test_nonstream_calls_keep_original_request_even_after_cache():
    owner = _owner()
    owner._compat_without_stream_options = True
    owner._compat_with_max_tokens = True
    raw = SimpleNamespace(create=AsyncMock(return_value="sync_result"))
    request = _kwargs() | {"stream": False}
    assert await _CompatibleCompletions(raw, owner).create(**request) == "sync_result"
    assert raw.create.await_args.kwargs == request


@pytest.mark.asyncio
async def test_proxy_delegates_close_and_regular_chat_fields():
    completions = SimpleNamespace(create=AsyncMock(return_value="STREAM"))
    close = AsyncMock()
    raw = SimpleNamespace(
        chat=SimpleNamespace(completions=completions, extras="intact"),
        close=close,
        api_key="example",
    )
    proxy = _CompatibleAsyncClient(raw, _owner())
    assert proxy.chat.extras == "intact"
    assert proxy.api_key == "example"
    assert await proxy.chat.completions.create(**_kwargs()) == "STREAM"
    await proxy.close()
    close.assert_awaited_once()


def test_classifier_recognizes_provider_422_schema_rejection():
    exc = ProviderError(422, "validation failed", body={
        "detail": [{
            "loc": ["body", "stream_options", "include_usage"],
            "msg": "Extra inputs are not permitted",
            "type": "extra_forbidden",
        }],
    })
    assert unsupported_stream_parameter(exc) == "stream_options"


def test_classifier_uses_400_error_body_without_leaking_it():
    exc = ProviderError(400, "400 Bad Request", body={
        "error": {"message": "unsupported parameter max_completion_tokens"}
    })
    assert unsupported_stream_parameter(exc) == "max_completion_tokens"


def test_classifier_does_not_mutate_auth_model_or_context_errors():
    examples = [
        ProviderError(400, "unknown model"),
        ProviderError(401, "unknown stream_options"),
        ProviderError(400, "context_length_exceeded"),
        ProviderError(400, "tools are unsupported for this model"),
    ]
    assert all(unsupported_stream_parameter(exc) is None for exc in examples)


@pytest.mark.asyncio
async def test_retry_log_never_includes_raw_error_body(caplog):
    token = "SECRET-SHOULD-NOT-APPEAR-IN-LOG"
    err = ProviderError(400, f"unknown field stream_options; credential={token}")
    raw = SimpleNamespace(create=AsyncMock(side_effect=[err, "STREAM"]))
    with caplog.at_level("WARNING", logger="N.E.K.O.Main.text_chat_compat"):
        assert await _CompatibleCompletions(raw, _owner()).create(**_kwargs()) == "STREAM"
    assert token not in caplog.text


def test_streaming_compat_retains_base_chat_parser():
    from utils.llm_client.openai_client import ChatOpenAI
    from utils.llm_client.stream_compat_12_0_1 import StreamingCompatChatOpenAI

    assert issubclass(StreamingCompatChatOpenAI, ChatOpenAI)
    # No separate parsing path: tools, reasoning, image/text fragments and
    # provider thought signatures keep using the original implementation.
    assert StreamingCompatChatOpenAI.astream is ChatOpenAI.astream


def test_client_instance_wraps_only_its_own_sdk_object(monkeypatch):
    from utils.llm_client.openai_client import ChatOpenAI
    from utils.llm_client.stream_compat_12_0_1 import StreamingCompatChatOpenAI

    raw = SimpleNamespace(
        chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock()))
    )

    def fake_init(self, **kwargs):
        self._aclient = raw

    monkeypatch.setattr(ChatOpenAI, "__init__", fake_init)
    instance = StreamingCompatChatOpenAI(model="test")
    assert instance._aclient._raw is raw
    assert instance._compat_without_stream_options is False
    assert instance._compat_with_max_tokens is False
