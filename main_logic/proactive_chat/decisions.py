# -*- coding: utf-8 -*-
# Copyright 2025-2026 Project N.E.K.O. Team
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Framework-independent proactive-chat entry and source decisions."""

import math
import random
import time
from dataclasses import dataclass
from typing import Any

from utils.logger_config import get_module_logger

from .contracts import (
    PROACTIVE_REASON_ERROR_CHARACTER_NOT_FOUND,
    PROACTIVE_REASON_PASS_BUSY,
    PROACTIVE_REASON_PASS_DISABLED,
    PROACTIVE_REASON_PASS_PRIVACY,
    PROACTIVE_REASON_PASS_RESTRICTED_SCREEN_ONLY,
    PROACTIVE_REASON_PASS_ROUTE_ACTIVE,
    PROACTIVE_REASON_PASS_SOURCE_EMPTY,
    PROACTIVE_REASON_PASS_THROTTLED,
    ProactiveChatCommand,
    ProactiveChatResult,
    _proactive_error_body,
    _proactive_pass_body,
)
from .state import (
    _RECENT_CHAT_MAX_AGE_SECONDS,
    _get_source_history_entry,
    _half_life_for,
    _recent_proactive_chat_entries,
    _reminiscence_usage_entries,
    _source_skip_probability,
)

logger = get_module_logger(__name__, "Main")


# Social-pressure policy for ordinary proactive chat.  Two ignored deliveries
# are enough to stop asking for attention on every scheduler tick; subsequent
# silence doubles the minimum retry delay up to two hours.  This is separate
# from source cooldowns: source decay answers "have I used this material?",
# while this policy answers "is it socially appropriate to speak again yet?".
_PROACTIVE_UNANSWERED_BACKOFF_BASE_SECONDS = 30 * 60
_PROACTIVE_UNANSWERED_BACKOFF_MAX_SECONDS = 2 * 60 * 60
_PROACTIVE_QUESTION_FATIGUE_MIN_SAMPLES = 3
_PROACTIVE_QUESTION_FATIGUE_RATIO = 0.5


def _public_web_link(link: dict[str, Any]) -> dict[str, Any]:
    """Strip every platform's internal metadata at the response boundary."""

    return {
        key: link[key]
        for key in ("title", "url", "source", "mode")
        if key in link
    }


def build_proactive_response(source_tag: str, ctx: dict) -> tuple[str, list]:
    """Resolve the effective delivery channel and its selected source links."""
    primary_channel = "unknown"
    source_links = []
    lanlan_name = ctx.get("lanlan_name", "System")

    match source_tag:
        case "CHAT":
            primary_channel = "chat"
        case "WEB":
            web_link = ctx.get("selected_web_link")
            primary_channel = web_link.get("mode", "web") if web_link else "web"
            if web_link:
                source_links.append(_public_web_link(web_link))
                logger.debug(
                    "[%s] Phase 2 确定选择 WEB (子通道: %s)，已添加链接",
                    lanlan_name,
                    primary_channel,
                )
        case "MUSIC":
            primary_channel = "music"
            if ctx.get("selected_music_link"):
                source_links.append(ctx["selected_music_link"])
                logger.debug("[%s] Phase 2 确定选择 MUSIC，已添加链接", lanlan_name)
        case "MEME":
            primary_channel = "meme"
            if ctx.get("selected_meme_link"):
                source_links.append(ctx["selected_meme_link"])
                logger.debug("[%s] Phase 2 确定选择 MEME，已添加相关链接", lanlan_name)
            else:
                logger.warning(
                    "[%s] Phase 2 AI 选择 MEME 但无可用表情包链接，回退处理",
                    lanlan_name,
                )
                if ctx.get("selected_web_link"):
                    primary_channel = ctx["selected_web_link"].get("mode", "web")
                    source_links.append(_public_web_link(ctx["selected_web_link"]))
                    logger.debug(
                        "[%s] Phase 2 回退到 WEB 通道 (子通道: %s)",
                        lanlan_name,
                        primary_channel,
                    )
                elif ctx.get("vision_content"):
                    primary_channel = "vision"
                    logger.debug("[%s] Phase 2 回退到 VISION 通道", lanlan_name)
                else:
                    logger.debug(
                        "[%s] Phase 2 MEME 无表情包且无回退通道，将跳过链接展示",
                        lanlan_name,
                    )
    return primary_channel, source_links


def _decide_manager_entry_guard(
    lanlan_name: Any,
    *,
    manager_exists: bool,
    goodbye_silent: bool = False,
) -> ProactiveChatResult | None:
    """Reject missing characters or sessions silenced by a goodbye."""
    if not manager_exists:
        return ProactiveChatResult(
            body=_proactive_error_body(
                PROACTIVE_REASON_ERROR_CHARACTER_NOT_FOUND,
                error=f"角色 {lanlan_name} 不存在",
            ),
            status_code=404,
        )
    if goodbye_silent:
        return ProactiveChatResult(
            body=_proactive_pass_body(
                PROACTIVE_REASON_PASS_DISABLED,
                message="goodbye silent; proactive skipped",
            )
        )
    return None


def _decide_game_route_entry_guard(
    game_route_active: bool | None,
) -> ProactiveChatResult | None:
    """Fail closed when the game-route ownership check is active or unavailable."""
    if game_route_active is False:
        return None
    message = (
        "game route active; ordinary proactive skipped"
        if game_route_active
        else "game route guard unavailable; ordinary proactive skipped"
    )
    return ProactiveChatResult(
        body=_proactive_pass_body(
            PROACTIVE_REASON_PASS_ROUTE_ACTIVE,
            message=message,
        )
    )


def _decide_busy_entry_guard(
    can_start: bool,
    *,
    state_snapshot: Any,
) -> ProactiveChatResult | None:
    """Return the stable 409 contract when a proactive turn cannot start."""
    if can_start:
        return None
    return ProactiveChatResult(
        body=_proactive_error_body(
            PROACTIVE_REASON_PASS_BUSY,
            error="AI正在响应中，无法主动搭话",
            message="请等待当前响应完成",
            state=state_snapshot,
        ),
        status_code=409,
    )


def _should_use_voice_fast_path(
    *,
    voice_mode: bool,
    manager_active: bool,
    realtime_session: bool,
) -> bool:
    """Select the voice entry path without depending on its session class."""
    return voice_mode and manager_active and realtime_session


@dataclass(frozen=True, slots=True)
class ActivityScheduleDecision:
    """Scheduling effects derived from an activity snapshot."""

    fixed_mode: bool = False
    base_interval: float = 0.0
    jitter_max: float = 0.0
    has_must_fire: bool = False


def _should_fetch_activity_snapshot(privacy_mode: bool) -> bool:
    """Privacy mode disables activity inspection and falls back to open policy."""
    return not privacy_mode


def _decide_closed_activity_gate(
    activity_snapshot: Any,
    *,
    debug_force_invite: bool,
) -> ProactiveChatResult | None:
    """Stop before source work when the activity tracker closes propensity."""
    if (
        debug_force_invite
        or activity_snapshot is None
        or activity_snapshot.propensity != "closed"
    ):
        return None
    return ProactiveChatResult(
        body=_proactive_pass_body(
            PROACTIVE_REASON_PASS_PRIVACY,
            message=(
                f"user state={activity_snapshot.state} "
                "→ closed (privacy lockdown)"
            ),
        )
    )


def _decide_activity_schedule(
    activity_snapshot: Any,
    *,
    base_interval_seconds: Any,
) -> ActivityScheduleDecision:
    """Derive fixed scheduling and bounded jitter without sleeping."""
    if (
        activity_snapshot is None
        or activity_snapshot.propensity != "restricted_screen_only"
    ):
        return ActivityScheduleDecision()

    has_must_fire = (
        activity_snapshot.anti_slack_pending is not None
        or activity_snapshot.work_break_pending is not None
    )
    if has_must_fire:
        return ActivityScheduleDecision(
            fixed_mode=True,
            has_must_fire=True,
        )
    try:
        base_interval = (
            float(base_interval_seconds)
            if base_interval_seconds is not None
            else 0.0
        )
    except (TypeError, ValueError):
        base_interval = 0.0
    jitter_max = (
        min(base_interval * 0.5, 60.0)
        if base_interval > 0
        else 0.0
    )
    return ActivityScheduleDecision(
        fixed_mode=True,
        base_interval=base_interval,
        jitter_max=jitter_max,
        has_must_fire=False,
    )


def _decide_probabilistic_activity_gate(
    activity_snapshot: Any,
    *,
    debug_force_invite: bool,
    random_value: float | None = None,
) -> ProactiveChatResult | None:
    """Apply the activity skip roll while preserving unfinished-thread priority."""
    if (
        debug_force_invite
        or activity_snapshot is None
        or activity_snapshot.skip_probability <= 0
        or activity_snapshot.unfinished_thread is not None
    ):
        return None
    if random_value is None:
        random_value = random.random()
    if random_value >= activity_snapshot.skip_probability:
        return None
    return ProactiveChatResult(
        body=_proactive_pass_body(
            PROACTIVE_REASON_PASS_THROTTLED,
            message=(
                f"probabilistic skip: state={activity_snapshot.state} "
                f"intensity={activity_snapshot.game_intensity} "
                f"skip_prob={activity_snapshot.skip_probability:.2f}"
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class EngagementPolicyDecision:
    """Social-pressure decision derived from persisted engagement state."""

    result: ProactiveChatResult | None = None
    avoid_questions: bool = False
    unanswered_streak: int = 0
    question_ratio: float = 0.0
    retry_after_seconds: float = 0.0


def _decide_engagement_policy(
    engagement_snapshot: Any,
    *,
    has_unfinished_thread: bool = False,
    now: float | None = None,
) -> EngagementPolicyDecision:
    """Throttle ignored proactive turns and flag question fatigue.

    A real unfinished thread keeps its one allowed follow-up priority: PATCH 01
    already removes generic "what next?" glue before such a thread can exist.
    Even in that override case ``avoid_questions`` may stay true, nudging the
    follow-up toward a self-contained statement rather than another demand for a
    reply.
    """
    if engagement_snapshot is None:
        return EngagementPolicyDecision()

    try:
        unanswered = max(0, int(engagement_snapshot.unanswered_streak))
    except (AttributeError, TypeError, ValueError):
        unanswered = 0
    try:
        last_proactive = max(0.0, float(engagement_snapshot.last_proactive_at))
    except (AttributeError, TypeError, ValueError, OverflowError):
        last_proactive = 0.0
    try:
        flags = tuple(bool(v) for v in engagement_snapshot.recent_question_flags)
    except (AttributeError, TypeError):
        flags = ()

    question_ratio = (
        sum(1 for flag in flags if flag) / len(flags) if flags else 0.0
    )
    fatigue = (
        len(flags) >= _PROACTIVE_QUESTION_FATIGUE_MIN_SAMPLES
        and question_ratio >= _PROACTIVE_QUESTION_FATIGUE_RATIO
    )
    avoid_questions = unanswered >= 1 or fatigue

    if unanswered < 2 or has_unfinished_thread or last_proactive <= 0:
        return EngagementPolicyDecision(
            avoid_questions=avoid_questions,
            unanswered_streak=unanswered,
            question_ratio=question_ratio,
        )

    exponent = min(unanswered - 2, 8)
    backoff_seconds = min(
        _PROACTIVE_UNANSWERED_BACKOFF_BASE_SECONDS * (2 ** exponent),
        _PROACTIVE_UNANSWERED_BACKOFF_MAX_SECONDS,
    )
    ref = float(time.time() if now is None else now)
    retry_after = max(0.0, last_proactive + backoff_seconds - ref)
    if retry_after <= 0:
        return EngagementPolicyDecision(
            avoid_questions=True,
            unanswered_streak=unanswered,
            question_ratio=question_ratio,
        )

    return EngagementPolicyDecision(
        result=ProactiveChatResult(
            body=_proactive_pass_body(
                PROACTIVE_REASON_PASS_THROTTLED,
                message=(
                    "proactive social backoff: recent initiatives unanswered"
                ),
                unanswered_streak=unanswered,
                retry_after_seconds=int(math.ceil(retry_after)),
            )
        ),
        avoid_questions=True,
        unanswered_streak=unanswered,
        question_ratio=question_ratio,
        retry_after_seconds=retry_after,
    )


@dataclass(frozen=True, slots=True)
class SourceModeSelection:
    """Initial source modes plus activity-based restrictions."""

    enabled_modes: Any
    has_unfinished_thread: bool
    result: ProactiveChatResult | None = None
    restricted_to_vision: bool = False
    text_only_followup: bool = False


def _select_source_modes(
    command: ProactiveChatCommand,
    activity_snapshot: Any,
    *,
    debug_force_invite: bool,
) -> SourceModeSelection:
    """Resolve legacy source fields and apply restricted-screen policy."""
    if command.enabled_modes_provided:
        enabled_modes = command.enabled_modes or []
    elif command.screenshot_data and isinstance(command.screenshot_data, str):
        enabled_modes = ["vision"]
    elif command.use_window_search:
        enabled_modes = ["window"]
    elif command.content_type == "news":
        enabled_modes = ["news"]
    elif command.content_type == "community":
        enabled_modes = ["community"]
    elif command.content_type == "video":
        enabled_modes = ["video"]
    elif command.use_personal_dynamic:
        enabled_modes = ["personal"]
    else:
        enabled_modes = ["home"]

    has_unfinished_thread = (
        activity_snapshot is not None
        and activity_snapshot.unfinished_thread is not None
    )
    restricted = (
        not debug_force_invite
        and activity_snapshot is not None
        and activity_snapshot.propensity == "restricted_screen_only"
    )
    if not restricted:
        return SourceModeSelection(
            enabled_modes=enabled_modes,
            has_unfinished_thread=has_unfinished_thread,
        )
    if "vision" in enabled_modes:
        return SourceModeSelection(
            enabled_modes=["vision"],
            has_unfinished_thread=has_unfinished_thread,
            restricted_to_vision=True,
        )
    if has_unfinished_thread:
        return SourceModeSelection(
            enabled_modes=[],
            has_unfinished_thread=True,
            text_only_followup=True,
        )
    return SourceModeSelection(
        enabled_modes=enabled_modes,
        has_unfinished_thread=False,
        result=ProactiveChatResult(
            body=_proactive_pass_body(
                PROACTIVE_REASON_PASS_RESTRICTED_SCREEN_ONLY,
                message=(
                    f"user state={activity_snapshot.state} restricts proactive "
                    "to screen-only, but vision not enabled this round"
                ),
            )
        ),
    )


def _decide_empty_source_gate(
    enabled_modes: Any,
    *,
    has_unfinished_thread: bool,
    has_memory_initiative: bool = False,
) -> ProactiveChatResult | None:
    """Pass only when no external source, thread, or memory cue can drive a turn.

    Memory follow-up topics are an independent proactive-chat source. This gate
    therefore runs *after* the memory-server follow-up lookup in ``service.py``:
    explicitly disabling web/screen/music sources must not silence a valid
    reminiscence cue.
    """
    if enabled_modes or has_unfinished_thread or has_memory_initiative:
        return None
    return ProactiveChatResult(
        body=_proactive_pass_body(
            PROACTIVE_REASON_PASS_SOURCE_EMPTY,
            message=(
                "no source modes enabled, no unfinished thread, and no memory "
                "follow-up cue"
            ),
        )
    )


@dataclass(frozen=True, slots=True)
class SourceWeightSelection:
    """Computed source weights and channels suppressed for this round."""

    weights: dict[str, float]
    suppressed: set[str]


def _select_weighted_sources(
    lanlan_name: str,
    enabled_modes: Any,
    available_channels: Any,
    *,
    has_reminiscence: bool,
) -> SourceWeightSelection:
    """Apply source-history decay without mutating fetched source payloads."""
    candidates = [
        mode
        for mode in enabled_modes
        if mode != "vision" and mode in available_channels
    ]
    if has_reminiscence:
        candidates.append("reminiscence")
    if not candidates:
        return SourceWeightSelection(weights={}, suppressed=set())
    weights = _compute_source_weights(lanlan_name, candidates)
    return SourceWeightSelection(
        weights=weights,
        suppressed=_filter_sources_by_weight(weights),
    )


def _should_skip_source(url_hash: str) -> bool:
    """Return whether source decay should suppress a stable source hash."""
    entry = _get_source_history_entry(url_hash)
    if not entry:
        return False
    age = time.time() - entry.get('ts', 0.0)
    probability = _source_skip_probability(
        age,
        _half_life_for(entry.get('kind', 'web')),
    )
    if probability >= 1.0:
        return True
    if probability <= 0.0:
        return False
    return random.random() < probability


_SOURCE_WEIGHT_DECAY_LAMBDA = 0.002
_SOURCE_WEIGHT_K = 0.30
_SOURCE_WEIGHT_FLOOR = 0.20
_SOURCE_WEIGHT_WINDOW = _RECENT_CHAT_MAX_AGE_SECONDS


def _compute_source_weights(
    lanlan_name: str,
    candidate_channels: list[str],
) -> dict[str, float]:
    """Compute normalized freshness weights for candidate source channels."""
    channel_count = len(candidate_channels)
    if channel_count == 0:
        return {}

    now = time.time()
    raw_scores: dict[str, float] = {
        channel: 0.0 for channel in candidate_channels
    }

    for timestamp, _message, channel in _recent_proactive_chat_entries(lanlan_name):
        age = now - timestamp
        if age <= _SOURCE_WEIGHT_WINDOW and channel in raw_scores:
            raw_scores[channel] += math.exp(-_SOURCE_WEIGHT_DECAY_LAMBDA * age)

    if 'reminiscence' in raw_scores:
        for timestamp in _reminiscence_usage_entries(lanlan_name):
            age = now - timestamp
            if age <= _SOURCE_WEIGHT_WINDOW:
                raw_scores['reminiscence'] += math.exp(
                    -_SOURCE_WEIGHT_DECAY_LAMBDA * age
                )

    freshness = {
        channel: 1.0 / (1.0 + _SOURCE_WEIGHT_K * raw_scores[channel])
        for channel in candidate_channels
    }
    total = sum(freshness.values())
    if total <= 0:
        return {
            channel: 1.0 / channel_count
            for channel in candidate_channels
        }
    return {
        channel: value / total
        for channel, value in freshness.items()
    }


def _filter_sources_by_weight(weights: dict[str, float]) -> set[str]:
    """Return channels whose normalized weight falls below the dynamic floor."""
    channel_count = len(weights)
    if channel_count <= 1:
        return set()
    threshold = min(_SOURCE_WEIGHT_FLOOR, 1.0 / channel_count)
    return {
        channel
        for channel, weight in weights.items()
        if weight < threshold
    }
