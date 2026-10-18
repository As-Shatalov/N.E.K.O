"""Patch 12.0.1: conservative HTTP 400/422 compatibility for text streaming.

OpenAI-compatible providers sometimes reject optional ``stream_options`` or
OpenAI's ``max_completion_tokens`` name, even though they support normal
Chat Completions streaming. Negotiate only a field the provider EXPLICITLY
reports unsupported. No silent fallback for model, auth, context, or tools.

An async-client proxy preserves the existing ChatOpenAI.astream implementation,
including tool-call thought signatures and usage/response handling; we never
copy or reinterpret provider-generated deltas.
"""
from __future__ import annotations

import logging
from typing import Any

from .openai_client import ChatOpenAI

_logger = logging.getLogger("N.E.K.O.Main.text_chat_compat")
_UNSUPPORTED_WORDS = (
    "unknown", "unrecognized", "unrecognised", "unsupported",
    "not supported", "not permitted", "not allowed", "unexpected",
    "not recognized", "extra_forbidden",
    "extra inputs are not permitted", "unknown field",
    "unrecognized request argument", "unrecognized parameter",
)


def unsupported_stream_parameter(exc: BaseException) -> str | None:
    """Classify only explicit field-rejection HTTP errors; do not log body.

    Error strings and ``body`` may contain private prompts or credentials;
    matching happens in memory and never prints these fields.
    """
    if getattr(exc, "status_code", None) not in (400, 422):
        return None
    body: Any = getattr(exc, "body", None)
    parts: list[str] = []
    if isinstance(body, dict):
        error = body.get("error", body)
        if isinstance(error, dict):
            param = error.get("param")
            if param and not any(
                field in str(param).casefold()
                for field in ("stream_options", "include_usage", "max_completion_tokens")
            ):
                # Provider named a different offending field. No guessing.
                return None
            parts.append(str(error.get("message") or ""))
            if param:
                parts.append(str(param))
        # Pydantic-like 422 bodies use `detail[].loc` + `detail[].msg`.
        details = body.get("detail")
        if isinstance(details, list):
            for detail in details[:8]:
                if isinstance(detail, dict):
                    parts.append(str(detail.get("loc") or ""))
                    parts.append(str(detail.get("msg") or ""))
                    parts.append(str(detail.get("type") or ""))
    if not any(parts):
        parts = [str(exc)]
    statement = " ".join(parts).casefold()
    if not any(word in statement for word in _UNSUPPORTED_WORDS):
        return None
    # Matching field names only; never rewrite tool, auth or model payloads.
    if "stream_options" in statement or "include_usage" in statement:
        return "stream_options"
    if "max_completion_tokens" in statement:
        return "max_completion_tokens"
    return None


class _CompatibleCompletions:
    def __init__(self, raw: Any, owner: "StreamingCompatChatOpenAI") -> None:
        self._raw = raw
        self._owner = owner

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)

    async def create(self, *args: Any, **kwargs: Any) -> Any:
        if not kwargs.get("stream"):
            return await self._raw.create(*args, **kwargs)

        request = dict(kwargs)
        if self._owner._compat_without_stream_options:
            request.pop("stream_options", None)
        if self._owner._compat_with_max_tokens and "max_completion_tokens" in request:
            request["max_tokens"] = request.pop("max_completion_tokens")

        # At most three attempts: original, then one downgrade for each of two
        # fields. HTTP 400 means the provider rejected the request before a
        # stream was opened, so retry cannot duplicate a generated answer.
        for _ in range(3):
            try:
                return await self._raw.create(*args, **request)
            except Exception as exc:
                field = unsupported_stream_parameter(exc)
                if field == "stream_options" and "stream_options" in request:
                    request.pop("stream_options")
                    self._owner._compat_without_stream_options = True
                    _logger.warning(
                        "[text-chat-compat] provider rejected stream_options; "
                        "retrying without optional usage reporting"
                    )
                    continue
                if field == "max_completion_tokens" and "max_completion_tokens" in request:
                    # Keep the original token ceiling: only rename the field.
                    request["max_tokens"] = request.pop("max_completion_tokens")
                    self._owner._compat_with_max_tokens = True
                    _logger.warning(
                        "[text-chat-compat] provider rejected max_completion_tokens; "
                        "retrying with max_tokens, unchanged token limit"
                    )
                    continue
                raise
        raise AssertionError("unreachable: streaming compatibility retries exhausted")


class _CompatibleChat:
    def __init__(self, raw: Any, owner: "StreamingCompatChatOpenAI") -> None:
        self._raw = raw
        self.completions = _CompatibleCompletions(raw.completions, owner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class _CompatibleAsyncClient:
    def __init__(self, raw: Any, owner: "StreamingCompatChatOpenAI") -> None:
        self._raw = raw
        self.chat = _CompatibleChat(raw.chat, owner)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._raw, name)


class StreamingCompatChatOpenAI(ChatOpenAI):
    """Use the unmodified upstream streaming parser with safe request retry."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._compat_without_stream_options = False
        self._compat_with_max_tokens = False
        # This wraps only this instance; no monkeypatching global SDK methods.
        self._aclient = _CompatibleAsyncClient(self._aclient, self)
