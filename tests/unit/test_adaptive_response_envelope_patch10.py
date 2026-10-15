# -*- coding: utf-8 -*-
"""PATCH 10 regression coverage: ordinary replies use a broad envelope, not a fixed target."""

from types import SimpleNamespace

import pytest

from config.prompts.prompts_chara import (
    get_lanlan_prompt,
    get_response_length_policy,
    is_default_prompt,
)
from config.session_settings import ADAPTIVE_RESPONSE_HARD_MAX_TOKENS
from main_logic.core.tts_runtime import TtsRuntimeMixin
from utils.preferences import _validate_conversation_settings


class _Dummy(TtsRuntimeMixin):
    def __init__(self, core_cfg):
        self._config_manager = SimpleNamespace(get_core_config=lambda: dict(core_cfg))


def test_response_policy_gives_model_choice_without_a_minimum():
    policy = get_response_length_policy("ru")
    assert "Choose the length of each ordinary reply yourself" in policy
    assert "fragment or one sentence" in policy
    assert "several short paragraphs" in policy
    assert "Do not pad" in policy
    assert "fixed number of sentences or lines" in policy
    assert "upper safety envelope" in policy


def test_response_policy_explicitly_overrides_generic_concise_bias():
    policy = get_response_length_policy("ru")
    assert 'overrides generic "concise" wording' in policy
    assert 'does NOT mean every reply should be short or the same size' in policy


def test_base_default_prompt_is_not_rewritten_by_patch10():
    # Specialised consumers (games, some plugins) reuse the base character
    # prompt without Core's ordinary-chat envelope and should keep their old
    # concise behaviour.
    prompt = get_lanlan_prompt("ru")
    assert "Strictly speak in CONCISE spoken language" in prompt
    assert is_default_prompt(prompt) is True


def test_adaptive_envelope_uses_new_hard_ceiling(monkeypatch):
    from main_logic.core import tts_runtime

    monkeypatch.setattr(
        tts_runtime._core_facade,
        "load_global_conversation_settings",
        lambda: {},
    )
    obj = _Dummy({
        "ADAPTIVE_RESPONSE_LENGTH_ENABLED": True,
        "ADAPTIVE_RESPONSE_HARD_MAX_TOKENS": ADAPTIVE_RESPONSE_HARD_MAX_TOKENS,
        "TEXT_GUARD_MAX_LENGTH": 300,
    })
    assert obj._get_text_guard_max_length() == ADAPTIVE_RESPONSE_HARD_MAX_TOKENS


def test_adaptive_envelope_can_be_disabled_for_legacy_guard(monkeypatch):
    from main_logic.core import tts_runtime

    monkeypatch.setattr(
        tts_runtime._core_facade,
        "load_global_conversation_settings",
        lambda: {
            "adaptiveResponseLengthEnabled": False,
            "textGuardMaxLength": 275,
        },
    )
    obj = _Dummy({
        "ADAPTIVE_RESPONSE_LENGTH_ENABLED": True,
        "ADAPTIVE_RESPONSE_HARD_MAX_TOKENS": ADAPTIVE_RESPONSE_HARD_MAX_TOKENS,
        "TEXT_GUARD_MAX_LENGTH": 300,
    })
    assert obj._get_text_guard_max_length() == 275


def test_adaptive_envelope_hard_cap_override_is_bounded(monkeypatch):
    from main_logic.core import tts_runtime

    monkeypatch.setattr(
        tts_runtime._core_facade,
        "load_global_conversation_settings",
        lambda: {
            "adaptiveResponseLengthEnabled": True,
            "adaptiveResponseHardMaxTokens": 99999,
        },
    )
    obj = _Dummy({})
    assert obj._get_text_guard_max_length() == 2000


def test_preferences_accept_adaptive_response_settings():
    out = _validate_conversation_settings({
        "adaptiveResponseLengthEnabled": True,
        "adaptiveResponseHardMaxTokens": 640,
    })
    assert out == {
        "adaptiveResponseLengthEnabled": True,
        "adaptiveResponseHardMaxTokens": 640,
    }
