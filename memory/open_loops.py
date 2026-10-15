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

"""Persistent prospective-memory / open-loop store (PATCH 09).

This module deliberately does **not** replace the existing activity
``unfinished_thread`` / semantic ``open_threads`` mechanisms:

* activity open threads are short-lived conversational state;
* reflection follow-ups are retrospective memory cues;
* open loops here are explicit *prospective* commitments whose outcome is
  still unknown (plans, promises, tasks, decisions waiting for an outcome).

The LLM is only allowed to propose typed create/update actions.  Lifecycle,
validation, idempotency, expiry, resurfacing cooldowns and persistence stay in
code.  This keeps a weak/creative model from directly owning durable state.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import threading
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Iterable

from memory.temporal import compute_event_timestamps, normalize_event_when, to_naive_local
from utils.config_manager import get_config_manager
from utils.file_utils import atomic_write_json, robust_json_loads
from utils.logger_config import get_module_logger
from utils.token_tracker import set_call_type

logger = get_module_logger(__name__, "Memory")

SCHEMA_VERSION = 1
MAX_ACTIVE_LOOPS = 24
MAX_STORED_LOOPS = 64
MAX_PROCESSED_TURNS = 128
MAX_CREATE_PER_TURN = 3
MAX_UPDATE_PER_TURN = 4
MAX_CONTEXT_LOOPS = 6
MAX_CANDIDATES = 3
MAX_SURFACES = 4
NO_DUE_EXPIRY_DAYS = 45
DUE_GRACE_DAYS = 30
OPEN_LOOP_LLM_TIMEOUT_SECONDS = 20
OPEN_LOOP_LLM_OUTPUT_MAX_TOKENS = 1200

_ALLOWED_KINDS = frozenset({"plan", "promise", "task", "decision", "question", "followup"})
_ALLOWED_OWNERS = frozenset({"master", "shared"})
_ALLOWED_UPDATE_ACTIONS = frozenset({"resolve", "cancel", "reschedule", "revise"})
_TERMINAL_STATUSES = frozenset({"resolved", "cancelled", "expired"})

# Conservative lexical gate: false positives cost one background summary-tier
# call; false negatives merely leave the ordinary memory/reflection path in
# charge.  Keep it explicit/auditable instead of asking an LLM every turn.
_PROSPECTIVE_PATTERNS = (
    # ru
    r"\b(?:завтра|послезавтра|позже|потом|на\s+следующ(?:ей|ую|ий)|через\s+\d+|сегодня\s+(?:вечером|позже))\b",
    r"\b(?:планирую|собираюсь|проверю|сделаю|доделаю|попробую|запущу|настрою|установлю|посмотрю|вернусь|напомни|не\s+забудь|надо\s+будет|нужно\s+будет|должен\s+буду)\b",
    # en
    r"\b(?:tomorrow|later|next\s+(?:day|week|month)|in\s+\d+\s+(?:minutes?|hours?|days?|weeks?))\b",
    r"\b(?:i(?:'ll|\s+will)|i\s+plan\s+to|i(?:'m|\s+am)\s+going\s+to|remind\s+me|need\s+to\s+remember|must\s+remember)\b",
    # es / pt
    r"\b(?:mañana|más\s+tarde|la\s+próxima\s+semana|voy\s+a|planeo|recuérdame)\b",
    r"\b(?:amanhã|mais\s+tarde|na\s+próxima\s+semana|vou\s+|planejo|lembre-me)\b",
    # zh / ja / ko (word boundaries are intentionally omitted)
    r"(?:明天|之后|稍后|以后|下周|我打算|我准备|我要记得|提醒我)",
    r"(?:明日|あとで|後で|来週|する予定|つもり|覚えておいて|リマインド)",
    r"(?:내일|나중에|다음\s*주|할\s*예정|하려고|기억해|알려줘)",
)
_PROSPECTIVE_RE = re.compile("|".join(f"(?:{p})" for p in _PROSPECTIVE_PATTERNS), re.IGNORECASE)

_RESOLUTION_RE = re.compile(
    r"(?:"
    r"\b(?:сделал|сделала|готово|закончил|закончила|проверил|проверила|получилось|не\s+получилось|отменяю|отмена|не\s+буду|перенесу|перенёс|перенесла|забей)\b"
    r"|\b(?:done|finished|completed|tested|checked|worked|didn['’]?t\s+work|cancel(?:led)?|won['’]?t\s+do|postpone(?:d)?|reschedule(?:d)?)\b"
    r"|(?:做完了|完成了|试过了|取消|不做了|改到|延期)"
    r"|(?:終わった|完了した|試した|キャンセル|延期)"
    r"|(?:끝냈|완료|해봤|취소|미뤘)"
    r"|\b(?:hecho|terminé|completé|probé|cancel(?:é|ado)|pospuse|reprogram)\w*\b"
    r"|\b(?:feito|terminei|concluí|testei|cancel(?:ei|ado)|adiei|reagend)\w*\b"
    r")",
    re.IGNORECASE,
)

_TOKEN_RE = re.compile(r"[\w\-]{4,}", re.UNICODE)
_STOP_TOKENS = frozenset({
    "который", "которая", "которые", "потом", "сегодня", "завтра", "будет", "буду",
    "this", "that", "with", "later", "tomorrow", "will", "have", "from",
    "para", "mais", "depois", "mañana", "amanhã",
})


def _now_naive() -> datetime:
    return datetime.now()


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return to_naive_local(datetime.fromisoformat(value))
    except (TypeError, ValueError):
        return None


def _clamp_int(value: object, lo: int, hi: int, default: int) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, result))


def _clean_text(value: object, *, max_len: int = 320) -> str:
    if not isinstance(value, str):
        return ""
    text = re.sub(r"\s+", " ", value).strip()
    if len(text) > max_len:
        text = text[:max_len].rstrip()
    return text


def _fingerprint_text(value: str) -> str:
    value = value.casefold()
    value = re.sub(r"[^\w]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def _texts_similar(a: str, b: str) -> bool:
    """Conservative duplicate guard for two prospective-intention texts.

    This is *not* contradiction detection and intentionally uses no embedding
    threshold.  It only prevents obvious duplicate loop creation on concurrent
    or replayed turns.
    """
    aa = _fingerprint_text(a)
    bb = _fingerprint_text(b)
    if not aa or not bb:
        return False
    if aa == bb or aa in bb or bb in aa:
        return True
    return SequenceMatcher(a=aa, b=bb).ratio() >= 0.86


def _salient_tokens(text: str) -> set[str]:
    return {
        token.casefold()
        for token in _TOKEN_RE.findall(text)
        if token.casefold() not in _STOP_TOKENS
    }


def _lexically_mentions_loop(user_text: str, loop_text: str) -> bool:
    user_tokens = _salient_tokens(user_text)
    loop_tokens = _salient_tokens(loop_text)
    if not user_tokens or not loop_tokens:
        return False
    overlap = user_tokens & loop_tokens
    if len(overlap) >= 2:
        return True
    return bool(overlap and any(len(t) >= 8 for t in overlap))


def compute_turn_hash(messages_dict: list[dict]) -> str:
    canonical = json.dumps(messages_dict, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _derive_loop_id(source_turn_hash: str, kind: str, text: str) -> str:
    raw = f"{source_turn_hash}\0{kind}\0{_fingerprint_text(text)}"
    return "ol_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _compute_due(due_when: object, anchor: datetime) -> tuple[dict | None, str | None]:
    normalized = normalize_event_when(due_when)
    if normalized is None:
        return None, None
    start, end = compute_event_timestamps(
        normalized,
        anchor.isoformat(),
        fallback_start=False,
        fallback_end=False,
    )
    return normalized, start or end




def _initial_eligible_at(anchor: datetime, due_at: str | None) -> str:
    """Choose when a new/rescheduled loop may first drive proactive chat.

    Prospective memory should preserve the intention immediately, but proactive
    resurfacing should wait until the item is plausibly timely.  This avoids the
    classic failure mode where a user says "I'll test it tomorrow" and the agent
    asks about it again minutes later.
    """
    due_dt = _parse_dt(due_at)
    if due_dt is None:
        return (anchor + timedelta(hours=12)).isoformat()
    delta = due_dt - anchor
    seconds = delta.total_seconds()
    if seconds <= 0:
        return anchor.isoformat()
    if seconds <= 3600:
        lead = timedelta(minutes=5)
    elif seconds <= 6 * 3600:
        lead = timedelta(minutes=30)
    elif seconds <= 24 * 3600:
        lead = timedelta(hours=2)
    elif seconds <= 7 * 86400:
        lead = timedelta(hours=12)
    else:
        lead = timedelta(days=2)
    eligible = due_dt - lead
    if eligible < anchor:
        eligible = anchor
    return eligible.isoformat()

def _expiry_for(anchor: datetime, due_at: str | None) -> str:
    due_dt = _parse_dt(due_at)
    if due_dt is None:
        return (anchor + timedelta(days=NO_DUE_EXPIRY_DAYS)).isoformat()
    return max(
        anchor + timedelta(days=NO_DUE_EXPIRY_DAYS),
        due_dt + timedelta(days=DUE_GRACE_DAYS),
    ).isoformat()


def _effective_open(loop: dict, now: datetime) -> bool:
    if loop.get("status") != "open":
        return False
    expires = _parse_dt(loop.get("expires_at"))
    return expires is None or expires > now


def _normalize_loop(raw: object) -> dict | None:
    if not isinstance(raw, dict):
        return None
    loop_id = _clean_text(raw.get("id"), max_len=64)
    text = _clean_text(raw.get("text"))
    kind = raw.get("kind") if raw.get("kind") in _ALLOWED_KINDS else "followup"
    owner = raw.get("owner") if raw.get("owner") in _ALLOWED_OWNERS else "master"
    status = raw.get("status") if raw.get("status") in ({"open"} | _TERMINAL_STATUSES) else "open"
    created_at = _clean_text(raw.get("created_at"), max_len=64)
    updated_at = _clean_text(raw.get("updated_at"), max_len=64) or created_at
    if not loop_id or not text or _parse_dt(created_at) is None:
        return None
    due_when = normalize_event_when(raw.get("due_when"))
    due_at = _clean_text(raw.get("due_at"), max_len=64) or None
    expires_at = _clean_text(raw.get("expires_at"), max_len=64) or None
    return {
        "id": loop_id,
        "text": text,
        "kind": kind,
        "owner": owner,
        "status": status,
        "importance": _clamp_int(raw.get("importance"), 1, 10, 5),
        "resolution_hint": _clean_text(raw.get("resolution_hint"), max_len=200),
        "created_at": created_at,
        "updated_at": updated_at,
        "due_when": due_when,
        "due_at": due_at,
        "expires_at": expires_at,
        "source_turn_hash": _clean_text(raw.get("source_turn_hash"), max_len=80),
        "closed_at": _clean_text(raw.get("closed_at"), max_len=64) or None,
        "resolution_text": _clean_text(raw.get("resolution_text"), max_len=240),
        "last_surfaced_at": _clean_text(raw.get("last_surfaced_at"), max_len=64) or None,
        "next_eligible_at": _clean_text(raw.get("next_eligible_at"), max_len=64) or None,
        "surface_count": _clamp_int(raw.get("surface_count"), 0, 1000, 0),
    }


class OpenLoopStore:
    """Small, auditable store for deferred intentions.

    No process cache is kept: the file is tiny (<=64 rows) and every mutating
    path re-reads under the per-character lock.  That makes cloud restore,
    crash replay and tests far easier to reason about than another cache that
    needs invalidation hooks.
    """

    def __init__(self, config_manager=None) -> None:
        self._cm = config_manager or get_config_manager()
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _path(self, name: str) -> str:
        from memory import ensure_character_dir
        return os.path.join(
            ensure_character_dir(self._cm.memory_dir, name),
            "open_loops.json",
        )

    def _get_lock(self, name: str) -> threading.Lock:
        if name not in self._locks:
            with self._locks_guard:
                if name not in self._locks:
                    self._locks[name] = threading.Lock()
        return self._locks[name]

    def _load_unlocked(self, name: str) -> dict:
        path = self._path(name)
        payload: dict[str, Any] = {"version": SCHEMA_VERSION, "loops": [], "processed_turns": []}
        if not os.path.exists(path):
            return payload
        try:
            with open(path, encoding="utf-8") as handle:
                raw = json.load(handle)
        except Exception as exc:
            logger.warning("[OpenLoops] %s: load failed; using empty view: %s", name, exc)
            return payload
        if not isinstance(raw, dict):
            return payload
        loops: list[dict] = []
        for item in raw.get("loops") or []:
            normalized = _normalize_loop(item)
            if normalized is not None:
                loops.append(normalized)
        processed: list[dict] = []
        for item in raw.get("processed_turns") or []:
            if not isinstance(item, dict):
                continue
            turn_hash = _clean_text(item.get("hash"), max_len=80)
            processed_at = _clean_text(item.get("processed_at"), max_len=64)
            if turn_hash and _parse_dt(processed_at) is not None:
                processed.append({"hash": turn_hash, "processed_at": processed_at})
        return {"version": SCHEMA_VERSION, "loops": loops, "processed_turns": processed[-MAX_PROCESSED_TURNS:]}

    def _expire_stale_unlocked(self, payload: dict, now: datetime) -> bool:
        changed = False
        for loop in payload["loops"]:
            if loop.get("status") != "open":
                continue
            expires = _parse_dt(loop.get("expires_at"))
            if expires is not None and expires <= now:
                loop["status"] = "expired"
                loop["closed_at"] = now.isoformat()
                loop["updated_at"] = now.isoformat()
                changed = True
        return changed

    def _rotate_unlocked(self, payload: dict) -> None:
        loops = payload["loops"]
        active = [loop for loop in loops if loop.get("status") == "open"]
        terminal = [loop for loop in loops if loop.get("status") != "open"]
        terminal.sort(key=lambda row: row.get("updated_at") or row.get("created_at") or "", reverse=True)
        # Never silently discard an active intention.  Creation is capped at
        # MAX_ACTIVE_LOOPS, and legacy files above it remain readable until the
        # user resolves/cancels them.
        terminal_room = max(0, MAX_STORED_LOOPS - len(active))
        payload["loops"] = active + terminal[:terminal_room]
        payload["processed_turns"] = payload.get("processed_turns", [])[-MAX_PROCESSED_TURNS:]

    def _save_unlocked(self, name: str, payload: dict) -> None:
        from utils.cloudsave_runtime import assert_cloudsave_writable
        assert_cloudsave_writable(self._cm, operation="save", target=f"memory/{name}/open_loops.json")
        self._rotate_unlocked(payload)
        atomic_write_json(self._path(name), payload, indent=2, ensure_ascii=False)

    def load(self, name: str) -> dict:
        with self._get_lock(name):
            return self._load_unlocked(name)

    async def aload(self, name: str) -> dict:
        return await asyncio.to_thread(self.load, name)

    def is_turn_processed(self, name: str, turn_hash: str) -> bool:
        if not turn_hash:
            return False
        with self._get_lock(name):
            payload = self._load_unlocked(name)
            return any(row.get("hash") == turn_hash for row in payload.get("processed_turns", []))

    async def ais_turn_processed(self, name: str, turn_hash: str) -> bool:
        return await asyncio.to_thread(self.is_turn_processed, name, turn_hash)

    def active_loops(self, name: str, *, limit: int = MAX_CONTEXT_LOOPS) -> list[dict]:
        now = _now_naive()
        with self._get_lock(name):
            payload = self._load_unlocked(name)
            rows = [dict(loop) for loop in payload["loops"] if _effective_open(loop, now)]
        rows.sort(key=lambda row: (
            _parse_dt(row.get("due_at")) or datetime.max,
            -int(row.get("importance", 5)),
            row.get("created_at") or "",
        ))
        return rows[:max(0, limit)]

    async def aactive_loops(self, name: str, *, limit: int = MAX_CONTEXT_LOOPS) -> list[dict]:
        return await asyncio.to_thread(self.active_loops, name, limit=limit)

    def should_analyze_turn(self, name: str, user_texts: Iterable[str]) -> bool:
        texts = [_clean_text(text, max_len=1200) for text in user_texts]
        texts = [text for text in texts if text]
        if not texts:
            return False
        merged = "\n".join(texts)
        if _PROSPECTIVE_RE.search(merged):
            return True
        active = self.active_loops(name, limit=MAX_ACTIVE_LOOPS)
        if not active:
            return False
        if _RESOLUTION_RE.search(merged):
            return True
        return any(_lexically_mentions_loop(merged, loop.get("text", "")) for loop in active)

    async def ashould_analyze_turn(self, name: str, user_texts: Iterable[str]) -> bool:
        return await asyncio.to_thread(self.should_analyze_turn, name, list(user_texts))

    @staticmethod
    def _render_turn(messages_dict: list[dict]) -> str:
        lines: list[str] = []
        for item in messages_dict[-6:]:
            if not isinstance(item, dict):
                continue
            role = item.get("type")
            if role not in {"human", "ai"}:
                continue
            content = (item.get("data") or {}).get("content", "")
            if isinstance(content, list):
                content = " ".join(
                    str(part.get("text", ""))
                    for part in content
                    if isinstance(part, dict) and part.get("type") == "text"
                )
            text = _clean_text(content, max_len=1800)
            if text:
                lines.append(("USER" if role == "human" else "ASSISTANT") + ": " + text)
        return "\n".join(lines)[-6000:]

    @staticmethod
    def _render_active_for_prompt(active: list[dict]) -> str:
        if not active:
            return "[]"
        view = []
        for loop in active[:MAX_ACTIVE_LOOPS]:
            view.append({
                "id": loop.get("id"),
                "text": loop.get("text"),
                "kind": loop.get("kind"),
                "owner": loop.get("owner"),
                "importance": loop.get("importance"),
                "due_at": loop.get("due_at"),
                "resolution_hint": loop.get("resolution_hint"),
            })
        return json.dumps(view, ensure_ascii=False, separators=(",", ":"))

    async def analyze_and_update(
        self,
        name: str,
        messages_dict: list[dict],
        *,
        source_turn_hash: str,
        language: str | None = None,
    ) -> dict[str, int]:
        """Ask the summary-tier model for typed prospective-memory actions.

        LLM work happens outside the file lock.  The apply step re-reads latest
        state under lock and validates every id/action, so concurrent turns may
        race in inference but cannot blindly overwrite one another.
        """
        if not source_turn_hash:
            source_turn_hash = compute_turn_hash(messages_dict)
        if await self.ais_turn_processed(name, source_turn_hash):
            return {"created": 0, "updated": 0, "skipped": 1}

        active = await self.aactive_loops(name, limit=MAX_ACTIVE_LOOPS)
        from config.prompts.prompts_memory import get_open_loop_update_prompt
        from utils.llm_client import create_chat_llm_async

        anchor = _now_naive()
        prompt = get_open_loop_update_prompt(language).format(
            ANCHOR_TIME=anchor.isoformat(timespec="seconds"),
            ACTIVE_LOOPS=self._render_active_for_prompt(active),
            CURRENT_TURN=self._render_turn(messages_dict),
        )
        set_call_type("memory_open_loops")
        api_config = await self._cm.aget_model_api_config("summary")
        llm = await create_chat_llm_async(
            api_config["model"],
            api_config["base_url"],
            api_config["api_key"],
            timeout=OPEN_LOOP_LLM_TIMEOUT_SECONDS,
            max_retries=0,
            max_completion_tokens=OPEN_LOOP_LLM_OUTPUT_MAX_TOKENS,
            provider_type=api_config.get("provider_type"),
        )
        try:
            response = await llm.ainvoke(prompt)  # noqa: LLM_INPUT_BUDGET — current turn and active store are hard-capped above.
        finally:
            await llm.aclose()
        raw = (response.content or "").strip()
        if raw.startswith("```"):
            raw = raw.replace("```json", "").replace("```", "").strip()
        parsed = robust_json_loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError("open-loop model output is not a JSON object")
        creates = parsed.get("create") or []
        updates = parsed.get("update") or []
        if not isinstance(creates, list) or not isinstance(updates, list):
            raise ValueError("open-loop create/update must be arrays")
        return await asyncio.to_thread(
            self._apply_actions,
            name,
            creates[:MAX_CREATE_PER_TURN],
            updates[:MAX_UPDATE_PER_TURN],
            source_turn_hash,
            anchor,
        )

    def _apply_actions(
        self,
        name: str,
        creates: list[dict],
        updates: list[dict],
        source_turn_hash: str,
        anchor: datetime | None = None,
    ) -> dict[str, int]:
        now = to_naive_local(anchor) or _now_naive()
        now_iso = now.isoformat()
        with self._get_lock(name):
            payload = self._load_unlocked(name)
            if any(row.get("hash") == source_turn_hash for row in payload.get("processed_turns", [])):
                return {"created": 0, "updated": 0, "skipped": 1}
            self._expire_stale_unlocked(payload, now)
            loops = payload["loops"]
            by_id = {loop.get("id"): loop for loop in loops}
            updated_count = 0

            for action in updates:
                if not isinstance(action, dict):
                    continue
                loop_id = _clean_text(action.get("id"), max_len=64)
                verb = action.get("action")
                loop = by_id.get(loop_id)
                if verb not in _ALLOWED_UPDATE_ACTIONS or loop is None or loop.get("status") != "open":
                    continue
                if verb in {"resolve", "cancel"}:
                    loop["status"] = "resolved" if verb == "resolve" else "cancelled"
                    loop["closed_at"] = now_iso
                    loop["resolution_text"] = _clean_text(action.get("resolution"), max_len=240)
                    loop["updated_at"] = now_iso
                    loop["next_eligible_at"] = None
                    updated_count += 1
                    continue

                if verb == "reschedule":
                    due_when, due_at = _compute_due(action.get("due_when"), now)
                    if due_when is None or due_at is None:
                        continue
                    loop["due_when"] = due_when
                    loop["due_at"] = due_at
                    loop["expires_at"] = _expiry_for(now, due_at)
                    loop["updated_at"] = now_iso
                    loop["surface_count"] = 0
                    loop["next_eligible_at"] = _initial_eligible_at(now, due_at)
                    updated_count += 1
                    continue

                # revise: only whitelisted fields; id/created/source lineage stay immutable.
                new_text = _clean_text(action.get("text"))
                if new_text:
                    loop["text"] = new_text
                if action.get("kind") in _ALLOWED_KINDS:
                    loop["kind"] = action["kind"]
                if action.get("owner") in _ALLOWED_OWNERS:
                    loop["owner"] = action["owner"]
                if "importance" in action:
                    loop["importance"] = _clamp_int(action.get("importance"), 1, 10, loop.get("importance", 5))
                if "resolution_hint" in action:
                    loop["resolution_hint"] = _clean_text(action.get("resolution_hint"), max_len=200)
                if "due_when" in action:
                    due_when, due_at = _compute_due(action.get("due_when"), now)
                    loop["due_when"] = due_when
                    loop["due_at"] = due_at
                    loop["expires_at"] = _expiry_for(now, due_at)
                    loop["next_eligible_at"] = _initial_eligible_at(now, due_at)
                    loop["surface_count"] = 0
                loop["updated_at"] = now_iso
                updated_count += 1

            active_now = [loop for loop in loops if _effective_open(loop, now)]
            created_count = 0
            for proposal in creates:
                if not isinstance(proposal, dict) or len(active_now) >= MAX_ACTIVE_LOOPS:
                    continue
                text = _clean_text(proposal.get("text"))
                if len(text) < 6:
                    continue
                kind = proposal.get("kind") if proposal.get("kind") in _ALLOWED_KINDS else "followup"
                owner = proposal.get("owner") if proposal.get("owner") in _ALLOWED_OWNERS else "master"
                if any(_texts_similar(text, loop.get("text", "")) for loop in active_now):
                    continue
                loop_id = _derive_loop_id(source_turn_hash, kind, text)
                if loop_id in by_id:
                    continue
                due_when, due_at = _compute_due(proposal.get("due_when"), now)
                loop = {
                    "id": loop_id,
                    "text": text,
                    "kind": kind,
                    "owner": owner,
                    "status": "open",
                    "importance": _clamp_int(proposal.get("importance"), 1, 10, 5),
                    "resolution_hint": _clean_text(proposal.get("resolution_hint"), max_len=200),
                    "created_at": now_iso,
                    "updated_at": now_iso,
                    "due_when": due_when,
                    "due_at": due_at,
                    "expires_at": _expiry_for(now, due_at),
                    "source_turn_hash": source_turn_hash,
                    "closed_at": None,
                    "resolution_text": "",
                    "last_surfaced_at": None,
                    "next_eligible_at": _initial_eligible_at(now, due_at),
                    "surface_count": 0,
                }
                loops.append(loop)
                active_now.append(loop)
                by_id[loop_id] = loop
                created_count += 1

            payload.setdefault("processed_turns", []).append({
                "hash": source_turn_hash,
                "processed_at": now_iso,
            })
            self._save_unlocked(name, payload)
            if created_count or updated_count:
                logger.info(
                    "[OpenLoops] %s: created=%d updated=%d active=%d",
                    name,
                    created_count,
                    updated_count,
                    len(active_now),
                )
            return {"created": created_count, "updated": updated_count, "skipped": 0}

    def candidates(self, name: str, *, limit: int = MAX_CANDIDATES) -> list[dict]:
        now = _now_naive()
        with self._get_lock(name):
            payload = self._load_unlocked(name)
            rows = []
            for loop in payload["loops"]:
                if not _effective_open(loop, now):
                    continue
                if int(loop.get("surface_count", 0)) >= MAX_SURFACES:
                    continue
                eligible_at = _parse_dt(loop.get("next_eligible_at"))
                if eligible_at is not None and eligible_at > now:
                    continue
                if int(loop.get("importance", 5)) < 4:
                    continue
                due = _parse_dt(loop.get("due_at"))
                due_boost = 0.0
                if due is not None:
                    delta = (due - now).total_seconds()
                    if delta <= 0:
                        due_boost = 5.0
                    elif delta <= 6 * 3600:
                        due_boost = 4.0
                    elif delta <= 24 * 3600:
                        due_boost = 3.0
                    elif delta <= 3 * 86400:
                        due_boost = 2.0
                    elif delta <= 7 * 86400:
                        due_boost = 1.0
                score = float(loop.get("importance", 5)) + due_boost - 1.5 * int(loop.get("surface_count", 0))
                row = dict(loop)
                row["candidate_score"] = round(score, 3)
                rows.append(row)
        rows.sort(key=lambda row: (
            -float(row.get("candidate_score", 0.0)),
            _parse_dt(row.get("due_at")) or datetime.max,
            row.get("created_at") or "",
        ))
        return rows[:max(0, min(limit, MAX_CANDIDATES))]

    async def acandidates(self, name: str, *, limit: int = MAX_CANDIDATES) -> list[dict]:
        return await asyncio.to_thread(self.candidates, name, limit=limit)

    def record_surfaced(self, name: str, loop_ids: Iterable[str]) -> int:
        wanted = {str(loop_id) for loop_id in loop_ids if loop_id}
        if not wanted:
            return 0
        now = _now_naive()
        now_iso = now.isoformat()
        changed = 0
        with self._get_lock(name):
            payload = self._load_unlocked(name)
            self._expire_stale_unlocked(payload, now)
            for loop in payload["loops"]:
                if loop.get("id") not in wanted or not _effective_open(loop, now):
                    continue
                count = int(loop.get("surface_count", 0)) + 1
                loop["surface_count"] = count
                loop["last_surfaced_at"] = now_iso
                # Exponential social-pressure backoff: 6h -> 12h -> 24h.
                # At MAX_SURFACES the candidate selector stops proactive surfacing
                # entirely, while normal new-dialog context still remembers it.
                cooldown_hours = min(48, 6 * (2 ** max(0, count - 1)))
                loop["next_eligible_at"] = (now + timedelta(hours=cooldown_hours)).isoformat()
                loop["updated_at"] = now_iso
                changed += 1
            if changed:
                self._save_unlocked(name, payload)
        return changed

    async def arecord_surfaced(self, name: str, loop_ids: Iterable[str]) -> int:
        return await asyncio.to_thread(self.record_surfaced, name, list(loop_ids))


_GLOBAL_STORE: OpenLoopStore | None = None
_GLOBAL_STORE_LOCK = threading.Lock()


def get_open_loop_store() -> OpenLoopStore:
    global _GLOBAL_STORE
    if _GLOBAL_STORE is None:
        with _GLOBAL_STORE_LOCK:
            if _GLOBAL_STORE is None:
                _GLOBAL_STORE = OpenLoopStore()
    return _GLOBAL_STORE
