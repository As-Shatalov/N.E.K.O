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

"""Home-coming debrief (OD-16 v4, design §3.7.4 / §4.6): summary, chips, preview and the two-step diary write.

After the visit is finalized the cat tells the family, in two or three
sentences, who the visit was with and what they talked about:

* input: the in-memory transcript (the upload copy, both sides, ``(lp,
  side_rank)`` order), whatever ``visitMemoryEnabled`` says -- it is only
  said aloud, never stored; at most ``VISIT_DEBRIEF_INPUT_MAX_TOKENS``
  taken whole-line from the newest line backwards, the peer's lines inside
  ``VISIT_PEER_LINES_BLOCK``;
* one isolated-session turn (``VISIT_DEBRIEF_INSTRUCTION``, within
  ``VISIT_CEREMONY_TIMEOUT_S``) → ``strip_emotion_tags`` → ``redact_outbound``
  → token cap → ``assert_no_peer_ngram``; any failure says
  ``VISIT_DEBRIEF_FALLBACK``;
* spoken as a mirror speech registered for the inbox handoff (text only
  with voice off); when the family started talking after the takeover was
  released, it is only shown, after that ordinary turn ended;
* with visit memory on and something to digest: the chips ("keep as diary"
  / "don't keep") and ``state.json{debrief_choice:'ask_later',
  debrief_chip_pending:true}``. Nothing is written to private memory here.

:func:`render_chips` is also the ``render_chips`` callback of PR-08 startup
recovery (PR-09b wires it).

"Keep as diary" (PR-14, ``POST /api/visit/debrief/choice``, :data:`router`):
``diary`` only generates a preview (``main_logic.visit.debrief_writers``)
and shows it verbatim; ``confirm`` writes exactly that preview in two steps
(``visit_facts`` then the keyed ``/cache``) through :func:`run_commit`, the
single place that writes, shared with the backoff timer
(:func:`schedule_commit_retry`) and startup recovery
(:func:`resume_diary_commit`); ``forget`` writes nothing; ``abandon`` gives
up a permanently failed write. Every choice of one visit is serialized
under one per-visit lock. Labels come from ``static/locales`` (``visit.debrief.*``)
in the UI language. A freshly bound display gets whatever block the visit's
current choice owes (:func:`owed_block`).
"""

from __future__ import annotations

import asyncio
import json
import time
import weakref
from collections.abc import Mapping
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from config.visit_settings import (
    VISIT_CEREMONY_TIMEOUT_S,
    VISIT_DEBRIEF_INPUT_MAX_TOKENS,
    VISIT_DEBRIEF_MAX_TOKENS,
    VISIT_PEER_NGRAM_N,
)
from main_logic.visit.sanitize import PeerNgramHit, assert_no_peer_ngram, redact_outbound, strip_emotion_tags
from utils.logger_config import get_module_logger

logger = get_module_logger(__name__, "Main")

_STATE_WRITE_MAX_S = 5.0
"""Bound of the ``debrief_chip_pending`` state write (a stalled disk must not hold the exit flow)."""

DEBRIEF_ACTION = "visit_debrief_choice"
"""``react-chat-window:action`` of the chip buttons (handled by the page, PR-12 / PR-14)."""


def chips_request_id(visit_id: str) -> str:
    return f"visit-debrief:{visit_id}"


def t(key: str, lang: Optional[str]) -> str:
    """``visit.debrief.<key>`` from ``static/locales`` in ``lang`` (the page renders it verbatim)."""
    from main_logic.core._shared import get_locale_text

    dotted = f"visit.debrief.{key}"
    return get_locale_text(lang, dotted, dotted)


def chip_blocks(visit_id: str, lang: Optional[str]) -> list[dict]:
    """The chips message: one line of text and the two buttons (``diary`` / ``forget``)."""
    return [
        {"type": "text", "text": t("question", lang)},
        {"type": "buttons", "buttons": [
            {"id": "diary", "label": t("choiceDiary", lang), "action": DEBRIEF_ACTION,
             "payload": {"visit_id": visit_id, "choice": "diary"}},
            {"id": "forget", "label": t("choiceForget", lang), "action": DEBRIEF_ACTION, "variant": "danger",
             "payload": {"visit_id": visit_id, "choice": "forget"}},
        ]},
    ]


async def show_chips(host: Any, visit_id: str, *, own_char: str, lang: Optional[str]) -> bool:
    """Render the chips through ``host`` (a ``VisitHost``); False when no display took them."""
    try:
        return bool(await host.render_chat_blocks(chip_blocks(visit_id, lang),
                                                  request_id=chips_request_id(visit_id),
                                                  source_name=own_char))
    except Exception as exc:  # noqa: BLE001 - 页面不在：标记留在 state.json，等下次 bind 重放
        logger.info("visit %s: debrief chips not shown: %s", visit_id[:6], type(exc).__name__)
        return False


async def render_chips(visit_id: str, *, own_char: str, status: Optional[str] = None) -> bool:
    """PR-08 recovery callback: show the block ``visit_id`` currently owes on ``own_char``'s display now.

    ``status='interrupted'`` (a crashed visit) also tells the page the last
    visit was interrupted, but only while the owed block is the chips
    (``CHIP_CHOICES``, ``generating:diary`` included): the preview, the
    committing preview and the failure block never carry it, the same rule
    as the bind replay. The pending flag in ``state.json`` stays until the
    user decides.
    """
    from main_routers.visit_router.host_port import ManagerHost
    from main_routers.visit_router.local_context import prompt_lang

    from main_routers.visit_router.accounts import own_visit_uid

    host = ManagerHost.for_character(own_char)
    if host is None or not host.display_bound():
        # 页面还没 bind（启动补录通常早于页面连上）：芯片与「意外中断」都等 visit_bind 重放，不先发半套
        return False
    owner = await _visit_owner(visit_id)
    if owner is None or owner != await own_visit_uid():
        # 启动补录逐场回调、不分账号：不是当前登录账号的场次不发（换回那个账号后由 visit_bind 重放）
        return False
    if status == "interrupted" and await _owes_chips(visit_id):
        # 与芯片同一出口：校验 bind 与写入用同一个连接对象（send_status 会重读 mgr.websocket）。
        # 预览块 / 写入中 / 失败块不带这条提示（与 bind 重放同一口径）
        if not await host.send_frame({"type": "status", "message": json.dumps(
                {"code": "VISIT_INTERRUPTED_LAST_TIME", "details": {"visit_id": visit_id}}, ensure_ascii=False)}):
            # 提示没送出（连接刚被换掉等）：芯片也不单独发，下次 visit_bind 连同提示一起重放
            return False
        if await own_visit_uid() != owner:
            return False  # 写提示的这段 await 里登出 / 换了账号：芯片不再发
    return await show_owed(host, visit_id, own_char=own_char, lang=prompt_lang())


async def show_owed(host: Any, visit_id: str, *, own_char: str, lang: Optional[str]) -> bool:
    """Render the block ``visit_id`` currently owes (chips, preview or failure block) and its state frame.

    Under the per-visit lock, from the current ``state.json``: a choice made
    meanwhile on another page is never overtaken by an older block.
    """
    async with _visit_lock(visit_id):
        return await _show_owed_locked(host, visit_id, own_char=own_char, lang=lang)


async def _show_owed_locked(host: Any, visit_id: str, *, own_char: str, lang: Optional[str]) -> bool:
    try:
        state = await _spool(visit_id).read_state()
    except Exception as exc:  # noqa: BLE001 - 读不出：等 bind 重放
        logger.debug("visit %s: state unreadable: %s", visit_id[:6], type(exc).__name__)
        return False
    owed = owed_block(state, lang) if state is not None else None
    if owed is None:
        return False
    request_id, blocks, frame = owed
    from main_routers.visit_router.accounts import own_visit_uid

    if await _own_char(state) != own_char:
        # 补录拿到名字之后、等锁期间改了名（旧名字可能已归另一只猫）：不推给那个名字的页面，等 bind 重放
        return False
    if await own_visit_uid() != state.get("own_uid"):
        # 账号放在最后核（解析名字有 await）：换了账号就不把这一场的预览正文推给现在登录的账号
        return False
    try:
        shown = bool(await host.render_chat_blocks(blocks, request_id=request_id, source_name=own_char))
    except Exception as exc:  # noqa: BLE001 - 页面不在：标记留在 state.json，等下次 bind 重放
        logger.info("visit %s: debrief block not shown: %s", visit_id[:6], type(exc).__name__)
        return False
    if shown and frame is not None:
        await host.send_frame(frame)
    return shown


async def _owes_chips(visit_id: str) -> bool:
    try:
        state = await _spool(visit_id).read_state()
    except Exception:  # noqa: BLE001 - 读不出：不发提示，块本身也发不出，等 bind 重放
        return False
    return state is not None and state.get("debrief_choice") in _CHIP_CHOICES


async def _visit_owner(visit_id: str) -> Optional[str]:
    """``state.json`` ``own_uid`` of ``visit_id`` (None when unreadable: fail closed)."""
    from main_logic.visit.spool import VisitSpool
    from main_routers.visit_router import runtime

    try:
        state = await VisitSpool(runtime.runtime_deps().config_dir(), visit_id).read_state()
    except Exception as exc:  # noqa: BLE001
        logger.debug("visit %s: state unreadable: %s", visit_id[:6], type(exc).__name__)
        return None
    owner = (state or {}).get("own_uid")
    return owner if isinstance(owner, str) and owner else None


async def build_debrief_record(lines: list[dict], lang: Optional[str]) -> str:
    """The record block of the summary turn (newest lines first within the token budget)."""
    from main_logic.visit.memory_commit import record_block_within_budget

    if not lines:
        return ""
    return await asyncio.to_thread(record_block_within_budget, lines, lang, VISIT_DEBRIEF_INPUT_MAX_TOKENS)


async def clean_summary(raw: Optional[str], *, family_names: tuple[str, ...], neutral_term: str,
                        peer_lines: list[str]) -> Optional[str]:
    """Summary text safe to say at home, or None (fallback line) when it copies the peer or is empty."""
    from utils.tokenize import atruncate_to_tokens

    text = strip_emotion_tags(str(raw or "")).strip()
    if not text:
        return None
    if family_names:
        text = redact_outbound(text, family_names=family_names, replacement=neutral_term)
    text = (await atruncate_to_tokens(text, VISIT_DEBRIEF_MAX_TOKENS)).strip()
    try:
        # 整场对端句子可能很多：扫描放到线程里，不卡住事件循环上别的场次
        await asyncio.to_thread(assert_no_peer_ngram, text, peer_lines, n=VISIT_PEER_NGRAM_N)
    except PeerNgramHit:
        logger.info("visit debrief: summary copied the peer, using the fixed line")
        return None
    return text or None


async def run_debrief(rt: Any, *, input_stamp: float) -> None:
    """Finalize step 4 for ``rt`` (a ``VisitRuntime``): summary, chips, ``ask_later``."""
    from config.prompts.prompts_visit import build_visit_debrief_prompt, get_visit_debrief_fallback

    # 上传头卡住、超过封存期限时台词还在积压里：与 /state 一样合并进来，不因此退成通用简述
    lines = rt._replay_lines()
    peer_lines = [str(line.get("text") or "") for line in lines if str(line.get("from")).startswith("peer_")]
    text: Optional[str] = None
    if lines:
        block = await build_debrief_record(lines, rt.lang)
        # 输入只用有预算的整场记录：不再带隔离会话里的近期历史（重复、超预算、对端原文出了数据块）
        raw = await rt.one_shot_turn(build_visit_debrief_prompt(block, rt.lang), timeout=VISIT_CEREMONY_TIMEOUT_S,
                                     without_history=True)
        text = await clean_summary(raw, family_names=tuple(rt.family_names), neutral_term=rt.neutral_term,
                                   peer_lines=peer_lines)
    if not text:
        text = get_visit_debrief_fallback(rt.lang)
    family_spoke = rt.host.last_user_input() > input_stamp
    if family_spoke:
        # 亲人在仪式句 / 简述生成中先开口：简述与芯片排到那一轮 turn end 之后、只上屏不出声
        await rt.wait_family_turn()
        if rt.handoff is not None:
            rt.handoff.skip("debrief")
        try:
            await rt.host.mirror_assistant_output(text, metadata=rt._mirror_meta("visit_debrief"),
                                                  request_id=f"visit-debrief-summary:{rt.visit_id}")
        except Exception as exc:  # noqa: BLE001
            logger.warning("visit %s: debrief summary not shown: %s", rt.visit_id[:6], type(exc).__name__)
    else:
        await rt.speak_home_segment("debrief", text, kind="visit_debrief", silent=rt.finalize_reason == "goodbye")
    await _push_state(rt, "summary")
    # 日记读的是 spool：有没有可记的句子按 spool 实际写进去的算（与上传流水各自独立）。
    # spool_lines 要等写盘线程写完才加一：还有句子在写（落盘慢、收尾没等它）就先限时等它落定再判断，
    # 不把这一场当成「没有可记的」。追加在提交时就排进了单线程写盘队列、关 spool 排在它后面，不会被关掉
    await rt.settle_spool_appends(_STATE_WRITE_MAX_S)
    await _offer_chips(rt, has_lines=rt.spool_lines > 0)


async def _push_state(rt: Any, phase: str) -> None:
    """``visit_debrief{phase}`` status frame (§4.5; the chips themselves are ``chat_blocks``)."""
    await rt.host.send_frame({"type": "visit_debrief", "visit_id": rt.visit_id, "phase": phase,
                              "request_id": chips_request_id(rt.visit_id), "ts": rt.wall()})


async def _offer_chips(rt: Any, *, has_lines: bool) -> None:
    spool = rt.spool
    if not rt.memory_enabled or spool is None or not has_lines:
        return
    # 有界：磁盘卡住时不能让退出流程停在这里（交还回调、teardown 都在后面）；到点不等，写没写成由启动补录兜底
    # 有条件写入：芯片先出、用户选得快时，晚到的这次写不会把已记下的选择改回「以后再说」
    writing = asyncio.ensure_future(spool.mark_debrief_pending())
    writing.add_done_callback(lambda t: t.cancelled() or t.exception())
    # 一创建就登记（做完即移除）：退出流程在这里被取消时也不会漏掉它
    rt._keep_background(writing)
    await asyncio.wait([writing], timeout=_STATE_WRITE_MAX_S)
    if not writing.done():
        logger.warning("visit %s: debrief state still writing; continuing", rt.visit_id[:6])
    elif not writing.cancelled() and writing.exception() is not None:
        # 标记写不进：芯片照常出，下次启动由补录兜底
        logger.warning("visit %s: debrief state not written: %s", rt.visit_id[:6],
                       type(writing.exception()).__name__)
    await show_chips(rt.host, rt.visit_id, own_char=rt.lanlan_name, lang=rt.lang)
    await _push_state(rt, "asked")


# =====================================================================
# 「记成日记」：预览、两步写入与状态机（OD-16 v4，§3.7.4 / §4.6，PR-14）
# =====================================================================

CHOICES = ("diary", "confirm", "forget", "abandon")
_CHIP_CHOICES = (None, "ask_later", "generating:diary")
CHIP_CHOICES = _CHIP_CHOICES
"""Choices whose owed block is the chips (the "interrupted" notice only ever accompanies them)."""
_FINAL_CHOICES = ("diary", "forget", "abandoned")
_PENDING_COMMIT = ("committing:diary", "commit_failed:diary")
_DIARY_LLM_MAX_TOKENS = 900
"""Output budget of the diary call: the entry (``VISIT_DIARY_MAX_TOKENS``) plus up to three facts and the JSON."""

router = APIRouter()

_locks: "weakref.WeakValueDictionary[tuple[int, str], asyncio.Lock]" = weakref.WeakValueDictionary()
_timers: dict[str, asyncio.Task] = {}
_running: set[asyncio.Task] = set()
_timer_epoch = 0

_now = time.time
"""Wall clock of the retry schedule (``debrief_retry.next_at`` survives restarts); tests replace it."""

_sleep = asyncio.sleep
"""Sleep of the retry timers; tests replace it."""


def preview_request_id(visit_id: str) -> str:
    return f"visit-debrief-preview:{visit_id}"


def failed_request_id(visit_id: str, seq: int) -> str:
    return f"visit-debrief-failed:{visit_id}:{int(seq)}"


def _visit_lock(visit_id: str) -> asyncio.Lock:
    # 同一场的选择、生成与提交串行；按事件循环分开（测试一个用例一个循环）
    key = (id(asyncio.get_running_loop()), visit_id)
    lock = _locks.get(key)
    if lock is None:
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def written_of(writes: Mapping[str, Any]) -> dict:
    """``written`` of §4.6: facts only when some fact was actually new, the diary when ``/cache`` confirmed."""
    return {"facts": int(writes.get("facts_written") or 0) > 0, "cache": writes.get("cache") is True}


def unconfirmed_of(writes: Mapping[str, Any]) -> dict:
    """``unconfirmed`` of §4.6: a step that may have been written without confirmation (in flight counts)."""
    return {
        step: bool(writes.get(f"{step}_unconfirmed") or writes.get(f"{step}_inflight"))
        for step in ("facts", "cache")
    }


def apply_transition(writes: Mapping[str, Any], step: str, event: str, detail: Mapping[str, Any]) -> dict:
    """``debrief_writes`` after one ``commit_diary`` transition (a new dict)."""
    new = dict(writes)
    if event == "inflight":
        new[f"{step}_inflight"] = True
    elif event == "confirmed":
        new[step] = True
        new[f"{step}_inflight"] = False
        new[f"{step}_unconfirmed"] = False
        if step == "facts":
            new["facts_written"] = max(0, int(detail.get("facts_written") or 0))
    elif event == "rejected":
        new[f"{step}_inflight"] = False
    elif event == "indeterminate":
        new[f"{step}_inflight"] = False
        new[f"{step}_unconfirmed"] = True
    else:
        raise ValueError(f"unknown transition {event!r}")
    return new


def preview_blocks(visit_id: str, pending: Mapping[str, Any], lang: Optional[str], *,
                   committing: bool = False) -> list[dict]:
    """The preview message: the diary entry and every fact verbatim (``plain_text`` blocks) and "save / don't keep".

    ``committing`` (replayed while the two-step write is pending): "don't
    keep" is disabled and a status line says it is being saved; "save"
    stays clickable for an immediate retry.
    """
    blocks: list[dict] = [
        {"type": "text", "text": t("previewTitle", lang)},
        {"type": "text", "text": str(pending.get("diary") or ""), "plain_text": True},
    ]
    facts = [str(f) for f in (pending.get("facts") or [])]
    if facts:
        blocks.append({"type": "text", "text": t("previewFactsLabel", lang)})
        blocks.extend({"type": "text", "text": fact, "plain_text": True} for fact in facts)
    forget = {"id": "forget", "label": t("choiceForget", lang), "action": DEBRIEF_ACTION, "variant": "danger",
              "payload": {"visit_id": visit_id, "choice": "forget"}}
    if committing:
        forget["disabled"] = True
    blocks.append({"type": "buttons", "buttons": [
        {"id": "confirm", "label": t("previewConfirm", lang), "action": DEBRIEF_ACTION,
         "payload": {"visit_id": visit_id, "choice": "confirm"}},
        forget,
    ]})
    if committing:
        blocks.append({"type": "status", "tone": "info", "text": t("committing", lang)})
    return blocks


def failed_blocks(visit_id: str, state: Mapping[str, Any], lang: Optional[str]) -> list[dict]:
    """The "write failed" message of a ``commit_failed:diary`` visit (retry / give up, both carrying ``seq``)."""
    seq = int(state["debrief_commit_error"]["seq"])
    written = written_of(state["debrief_writes"])
    unconfirmed = unconfirmed_of(state["debrief_writes"])
    blocks: list[dict] = [{"type": "text", "text": t("commitFailed", lang)}]
    if written["facts"] and not written["cache"]:
        blocks.append({"type": "text", "text": t("commitFailedPartial", lang)})
    if any(unconfirmed.values()):
        blocks.append({"type": "text", "text": t("commitFailedUnknown", lang)})
    blocks.append({"type": "buttons", "buttons": [
        {"id": "retry", "label": t("commitRetry", lang), "action": DEBRIEF_ACTION,
         "payload": {"visit_id": visit_id, "choice": "confirm", "seq": seq}},
        {"id": "abandon", "label": t("commitAbandon", lang), "action": DEBRIEF_ACTION, "variant": "danger",
         "payload": {"visit_id": visit_id, "choice": "abandon", "written": written,
                     "unconfirmed": unconfirmed, "seq": seq}},
    ]})
    return blocks


def _state_frame(visit_id: str, phase: str, request_id: str, **extra: Any) -> dict:
    return {"type": "visit_debrief", "visit_id": visit_id, "phase": phase, "request_id": request_id,
            **extra, "ts": _now()}


def committing_frame(state: Mapping[str, Any]) -> dict:
    """``visit_debrief{committing, seq}``: pages grey "don't keep" and failure blocks up to ``seq``."""
    error = state.get("debrief_commit_error")
    seq = int(error["seq"]) if error else 0
    return _state_frame(state["visit_id"], "committing", preview_request_id(state["visit_id"]), seq=seq)


def commit_failed_frame(state: Mapping[str, Any]) -> dict:
    seq = int(state["debrief_commit_error"]["seq"])
    return _state_frame(state["visit_id"], "commit_failed", failed_request_id(state["visit_id"], seq), seq=seq,
                        written=written_of(state["debrief_writes"]),
                        unconfirmed=unconfirmed_of(state["debrief_writes"]))


def owed_request_id(state: Mapping[str, Any]) -> Optional[str]:
    """The ``request_id`` of the block :func:`owed_block` would deliver (None when nothing is owed)."""
    visit_id = state["visit_id"]
    choice = state["debrief_choice"]
    if choice in _CHIP_CHOICES:
        return chips_request_id(visit_id)
    if choice in ("preview:diary", "committing:diary"):
        return preview_request_id(visit_id)
    if choice == "commit_failed:diary":
        return failed_request_id(visit_id, state["debrief_commit_error"]["seq"])
    return None


def owed_block(state: Mapping[str, Any], lang: Optional[str]) -> Optional[tuple[str, list[dict], Optional[dict]]]:
    """``(request_id, blocks, state frame)`` a page owes for this visit's current choice, or None.

    Chips while undecided (``generating:diary`` too: a crashed generation
    is offered again, never regenerated on its own), the preview while
    ``preview:diary``, the preview with "don't keep" disabled while
    ``committing:diary`` (then a ``committing`` frame), the failure block
    while ``commit_failed:diary`` (then a ``commit_failed`` frame). The
    blocks are built from ``state.json`` only (no LLM).
    """
    visit_id = state["visit_id"]
    choice = state["debrief_choice"]
    request_id = owed_request_id(state)
    if request_id is None:
        return None
    if choice in _CHIP_CHOICES:
        return request_id, chip_blocks(visit_id, lang), None
    if choice == "preview:diary":
        return request_id, preview_blocks(visit_id, state["debrief_pending"], lang), None
    if choice == "committing:diary":
        return request_id, preview_blocks(visit_id, state["debrief_pending"], lang, committing=True), committing_frame(state)
    return request_id, failed_blocks(visit_id, state, lang), commit_failed_frame(state)


def _spool(visit_id: str):
    from main_logic.visit.spool import VisitSpool
    from main_routers.visit_router import runtime

    return VisitSpool(runtime.runtime_deps().config_dir(), visit_id)


def _host(own_char: str):
    from main_routers.visit_router.host_port import ManagerHost

    return ManagerHost.for_character(own_char)


async def _owner_signed_in(owner: str) -> bool:
    from main_routers.visit_router.accounts import own_visit_uid

    # 按社区账号分区：别的账号登录着时不把这一场的内容推给页面（等那个账号回来 bind 时重放）
    return await own_visit_uid() == owner


async def _target(state: Mapping[str, Any]) -> Optional[tuple[str, Any]]:
    """``(current name, host)`` of the visit's character, resolved now from ``own_char_uid``.

    None when another community account is signed in, the character is gone,
    or it has no manager. Resolved at push time, never from a name looked up
    before an await: a rename meanwhile (the freed name maybe reused by
    another cat) must not send this visit's diary to that cat's page.
    """
    name = await _own_char(state)
    # 账号放在最后核：解析名字会读角色配置（有 await），期间换了账号也拦得住
    if not name or not await _owner_signed_in(state["own_uid"]):
        return None
    host = _host(name)
    return (name, host) if host is not None else None


async def _send(state: Mapping[str, Any], frame: dict) -> bool:
    target = await _target(state)
    if target is None:
        return False
    return await target[1].send_frame(frame)


async def _render(state: Mapping[str, Any], blocks: list[dict], request_id: str) -> bool:
    target = await _target(state)
    if target is None:
        return False
    name, host = target
    try:
        return bool(await host.render_chat_blocks(blocks, request_id=request_id, source_name=name))
    except Exception as exc:  # noqa: BLE001 - 页面不在：标记留在 state.json，等 bind 重放
        logger.info("visit debrief: block %s not shown: %s", request_id, type(exc).__name__)
        return False


def _lang() -> str:
    from main_routers.visit_router.local_context import prompt_lang

    return prompt_lang()


async def _own_char(state: Mapping[str, Any]) -> Optional[str]:
    from main_logic.visit import local_chars

    # 按 uid 认当前名字：改名后跟着新名字走；角色已删就是 None
    return await local_chars.resolve_char_name(state["own_char_uid"])


def _hold_lifecycle(character_uids):
    """Rename / delete of these characters answer 400 while held (``runtime.hold_character_lifecycle``)."""
    from main_routers.visit_router import runtime

    return runtime.hold_character_lifecycle(character_uids)


def _diary_llm():
    from config.visit_settings import VISIT_LLM_TIMEOUT_S
    from main_routers.visit_router.llm import one_shot_llm

    return one_shot_llm(max_tokens=_DIARY_LLM_MAX_TOKENS, timeout=VISIT_LLM_TIMEOUT_S)


async def _family_names() -> tuple[str, ...]:
    from main_routers.visit_router.background import _family_names as family_names

    return await family_names()


# ── 后台退避重试 ──────────────────────────────────────────────────────────


def _cancel_timer(visit_id: str) -> None:
    task = _timers.pop(visit_id, None)
    if task is not None and task is not asyncio.current_task() and not task.done():
        task.cancel()


def schedule_commit_retry(visit_id: str, next_at: float, *, epoch: Optional[int] = None,
                          persisted: bool = True) -> None:
    """(Re)arm the one in-process retry of a ``committing:diary`` visit at wall time ``next_at``.

    ``epoch`` (from a commit that started earlier): when :func:`cancel_commit_timers`
    ran since, nothing is armed -- the background was stopped meanwhile.
    """
    if epoch is not None and epoch != _timer_epoch:
        return
    _cancel_timer(visit_id)
    task = asyncio.ensure_future(_retry_at(visit_id, float(next_at), persisted=persisted))
    _timers[visit_id] = task


_LOCAL_FAILURE_RETRY_S = 600
"""Next retry after a retry that failed locally (``state.json`` unreadable / unwritable, ...)."""


async def _retry_at(visit_id: str, next_at: float, *, persisted: bool = True) -> None:
    epoch = _timer_epoch
    try:
        await _sleep(max(0.0, next_at - _now()))
    except asyncio.CancelledError:
        return
    me = asyncio.current_task()
    if _timers.get(visit_id) is me:
        # 先摘掉自己：这次再暂时失败会登记下一次定时，不能把正在跑的自己取消掉
        del _timers[visit_id]
    _running.add(me)
    try:
        # 按持久化的 next_at 排的定时：醒来后在锁内确认这仍是当前的排期。等锁期间一次手动「写入」
        # 已暂时失败、排了更新的 next_at 时，这次不再立刻重试（否则绕过退避、还会顶掉新定时）
        await run_commit(visit_id, "timer", expect_next_at=next_at if persisted else None)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - state.json 一时读写不了等：不能就此断掉自动重试
        logger.warning("visit %s: diary retry failed locally, again in %ds: %r",
                       visit_id[:6], _LOCAL_FAILURE_RETRY_S, exc)
        # 写入不设重试上限：本地失败同样再排一次（停止 / 回滚之后代次过期，不再排）
        if visit_id not in _timers:
            schedule_commit_retry(visit_id, _now() + _LOCAL_FAILURE_RETRY_S, epoch=epoch, persisted=False)
    finally:
        _running.discard(me)


def cancel_commit_timers() -> list[asyncio.Task]:
    """Cancel every pending or running diary retry (shutdown / startup rollback); returns the tasks.

    Bumps the timer epoch, so a commit already past its request does not arm
    a new retry afterwards.
    """
    global _timer_epoch
    _timer_epoch += 1
    tasks = list(_timers.values()) + list(_running)
    _timers.clear()
    for task in tasks:
        if not task.done():
            task.cancel()
    return tasks


def pending_commit_retries() -> dict[str, asyncio.Task]:
    """The armed retries by visit id (for tests and diagnostics)."""
    return dict(_timers)


async def resume_diary_commit(spool: Any, state: Mapping[str, Any]) -> None:
    """Startup recovery callback: arm the retry of a ``committing:diary`` visit at its persisted ``next_at``.

    A ``next_at`` still in the future is waited for (restarts never bypass the
    backoff); a visit without ``debrief_retry`` (older version) is due now.
    """
    visit_id = spool.visit_id
    async with _visit_lock(visit_id):
        # 补录拿的是扫描时的快照：期间手动「写入」可能已暂时失败、排了更新的 next_at，不能用旧值覆盖
        if visit_id in _timers:
            return
        current = await spool.read_state()
        if current is None or current["debrief_choice"] != "committing:diary":
            return
        retry = current.get("debrief_retry")
        next_at = float(retry["next_at"]) if isinstance(retry, Mapping) else 0.0
        schedule_commit_retry(visit_id, next_at)


# ── 两步提交（端点、定时重试、启动补录共用） ─────────────────────────────


async def run_commit(visit_id: str, trigger: str, *, expect_next_at: Optional[float] = None) -> tuple[int, dict]:
    """Run (or continue) the two-step write of ``visit_id`` under its lock; ``(http status, body)``.

    The only place a diary is written: the ``confirm`` endpoint, the retry
    timer and startup recovery all come here. Done → ``diary``; transient →
    stays ``committing:diary`` with the next backoff step persisted and
    armed; permanent → ``commit_failed:diary`` with the failure block.
    """
    async with _visit_lock(visit_id):
        return await _run_commit_locked(visit_id, trigger, expect_next_at=expect_next_at)


async def _run_commit_locked(visit_id: str, trigger: str, *,
                             expect_next_at: Optional[float] = None) -> tuple[int, dict]:
    epoch = _timer_epoch
    spool = _spool(visit_id)
    state = await spool.read_state()
    if state is None:
        return 404, {"error": "unknown_or_expired"}
    if state["debrief_choice"] != "committing:diary":
        return _not_committing(state)
    if expect_next_at is not None:
        retry = state["debrief_retry"]
        current = float(retry["next_at"]) if isinstance(retry, Mapping) else 0.0
        if current != float(expect_next_at):
            # 排期已被更新的一次尝试换掉：这次作废，新的定时会按它自己的 next_at 来
            return 409, {"error": "superseded"}
    # 两步写入期间按住这个角色的生命周期：改名 / 删除 400，不会一步写在旧名字下、一步写在新名字下
    async with _hold_lifecycle([state["own_char_uid"]]):
        return await _commit_held(visit_id, trigger, spool, state, epoch)


async def _commit_held(visit_id: str, trigger: str, spool: Any, state: Mapping[str, Any],
                       epoch: int) -> tuple[int, dict]:
    from config.visit_settings import VISIT_DEBRIEF_COMMIT_BACKOFF_S
    from main_logic.visit import debrief_writers

    own_char = await _own_char(state)
    if not own_char:
        # 角色已删：退役流程负责这场的文件，不再写
        return 404, {"error": "unknown_or_expired"}
    writes = dict(state["debrief_writes"])

    async def on_transition(step: str, event: str, detail: Mapping[str, Any]) -> None:
        nonlocal writes
        writes = apply_transition(writes, step, event, detail)
        # 落盘之后 commit_diary 才继续：请求在途时崩溃，「可能已写」的证据也在
        await spool.update_state(debrief_writes=writes)

    result = await debrief_writers.commit_diary(own_char, visit_id, state["debrief_pending"], writes, on_transition)
    lang = _lang()
    if result.kind == "done":
        await spool.update_state(debrief_choice="diary", debrief_pending=None, debrief_retry=None,
                                 debrief_chip_pending=False)
        _cancel_timer(visit_id)
        await spool.delete_if_settled()
        await _send(state, _state_frame(visit_id, "chosen", preview_request_id(visit_id), choice="diary"))
        if trigger != "http":
            # 发起页自己追加状态；后台补完时没有发起页，单独发一条
            await _render(state, [{"type": "status", "tone": "success", "text": t("savedDiary", lang)}],
                          f"visit-debrief-done:{visit_id}")
        return 200, {"ok": True, "applied": "diary"}
    if result.kind == "transient":
        retry = state["debrief_retry"] or {"attempts": 0, "next_at": 0.0}
        attempts = int(retry["attempts"]) + 1
        delay = VISIT_DEBRIEF_COMMIT_BACKOFF_S[min(attempts, len(VISIT_DEBRIEF_COMMIT_BACKOFF_S)) - 1]
        next_at = _now() + delay
        await spool.update_state(debrief_retry={"attempts": attempts, "next_at": next_at})
        schedule_commit_retry(visit_id, next_at, epoch=epoch)
        logger.warning("visit %s: diary write step %s not done (status %s); retry %d in %ds",
                       visit_id[:6], result.step, result.status, attempts, delay)
        return 503, {"retry": True, "next_retry_at": next_at}
    previous = state["debrief_commit_error"]
    seq = (int(previous["seq"]) if previous else 0) + 1
    state = await spool.update_state(
        debrief_choice="commit_failed:diary", debrief_retry=None,
        debrief_commit_error={"step": result.step, "status": int(result.status), "at": _now(), "seq": seq},
    )
    _cancel_timer(visit_id)
    logger.warning("visit %s: diary write step %s rejected (status %s); waiting for the user",
                   visit_id[:6], result.step, result.status)
    await _send(state, commit_failed_frame(state))
    await _render(state, failed_blocks(visit_id, state, lang), failed_request_id(visit_id, seq))
    return 502, {"error": "commit_failed", "step": result.step, "status": int(result.status),
                 "written": written_of(state["debrief_writes"]),
                 "unconfirmed": unconfirmed_of(state["debrief_writes"])}


async def _moved_on(spool: Any) -> tuple[int, dict]:
    """Answer for a conditional transition that lost to another writer (e.g. "forget this person")."""
    state = await spool.read_state()
    if state is None:
        return 404, {"error": "unknown_or_expired"}
    return _already_chosen(state)


def _already_chosen(state: Mapping[str, Any]) -> tuple[int, dict]:
    choice = state["debrief_choice"]
    if choice in _PENDING_COMMIT:
        return 409, {"error": "already_chosen", "choice": "diary", "completed": False,
                     "state": "committing" if choice == "committing:diary" else "commit_failed"}
    return 409, {"error": "already_chosen", "choice": choice, "completed": True}


def _not_committing(state: Mapping[str, Any]) -> tuple[int, dict]:
    choice = state["debrief_choice"]
    if choice in _CHIP_CHOICES or choice == "preview:diary":
        return 409, {"error": "not_previewed"}
    return _already_chosen(state)


# ── POST /api/visit/debrief/choice ───────────────────────────────────────


async def _choose(visit_id: str, choice: str, seq: Optional[int]) -> tuple[int, dict]:
    from main_logic.visit.spool import is_digestable
    from main_routers.visit_router.accounts import own_visit_uid

    spool = _spool(visit_id)
    lock = _visit_lock(visit_id)
    async with lock:
        # 进锁先记代次：之后任何一个 await 期间关机 / 回滚都算数，确认失败时不再补排定时
        epoch = _timer_epoch
        state = await spool.read_state()
        # 别的社区账号的场次（共用电脑）当作不存在；记忆关的场次本来就没有芯片
        if (state is None or state["own_uid"] != await own_visit_uid() or not is_digestable(state)
                or state["finalized"] is None):
            return 404, {"error": "unknown_or_expired"}
        own_char = await _own_char(state)
        if not own_char:
            return 404, {"error": "unknown_or_expired"}
        current = state["debrief_choice"]
        if choice == "diary":
            return await _choose_diary(spool, state, own_char)
        if choice == "forget":
            if current in _CHIP_CHOICES or current == "preview:diary":
                from main_logic.visit.debrief_writers import write_forget

                await write_forget(spool)
                # 帧指向页上被点的那一块：从预览块选的「不记」置灰预览块，不是早先的芯片
                target = preview_request_id(visit_id) if current == "preview:diary" else chips_request_id(visit_id)
                await _send(state, _state_frame(visit_id, "chosen", target, choice="forget"))
                return 200, {"ok": True, "applied": "forget"}
            return _already_chosen(state)
        if choice == "abandon":
            if current != "commit_failed:diary":
                if current in _FINAL_CHOICES:
                    return _already_chosen(state)
                return 409, {"error": "not_failed"}
            if seq != state["debrief_commit_error"]["seq"]:
                return 409, {"error": "stale_failure", "seq": state["debrief_commit_error"]["seq"]}
            from main_logic.visit.debrief_writers import write_forget

            written = written_of(state["debrief_writes"])
            unconfirmed = unconfirmed_of(state["debrief_writes"])
            await write_forget(spool, final_choice="abandoned")
            _cancel_timer(visit_id)
            await _send(state, _state_frame(visit_id, "chosen", failed_request_id(visit_id, seq),
                                               choice="abandoned", written=written, unconfirmed=unconfirmed))
            return 200, {"ok": True, "applied": "abandoned", "written": written, "unconfirmed": unconfirmed}
        # confirm
        if current == "preview:diary":
            moved = await spool.update_state_if("preview:diary", debrief_choice="committing:diary")
            if moved is None:
                return await _moved_on(spool)
            state = moved
        elif current == "commit_failed:diary":
            if seq != state["debrief_commit_error"]["seq"]:
                # 点的是旧版失败块（或绕过前端不带 seq）：以新块为准，不重新提交
                return 409, {"error": "stale_failure", "seq": state["debrief_commit_error"]["seq"]}
            # 「重试」：退避计数清零，只补未完成那步
            state = await spool.update_state(debrief_choice="committing:diary", debrief_retry=None)
        elif current != "committing:diary":
            return _not_committing(state)
        _cancel_timer(visit_id)
        await _send(state, committing_frame(state))
        try:
            return await _run_commit_locked(visit_id, "http")
        except asyncio.CancelledError:
            raise
        except Exception:
            # 本地失败（state.json 一时写不了等）：上面已撤掉定时，补排一次，不让它停在 committing
            if visit_id not in _timers:
                schedule_commit_retry(visit_id, _now() + _LOCAL_FAILURE_RETRY_S, epoch=epoch, persisted=False)
            raise


async def _choose_diary(spool: Any, state: Mapping[str, Any], own_char: str) -> tuple[int, dict]:
    from main_logic.visit import debrief_writers
    from main_routers.visit_router.accounts import own_visit_uid

    visit_id = spool.visit_id
    current = state["debrief_choice"]
    if current == "preview:diary":
        # 预览已落盘：只复用，不再生成。前面解析角色名有 await：回给页面前再核一次账号
        if await own_visit_uid() != state["own_uid"]:
            return 404, {"error": "unknown_or_expired"}
        return 200, {"ok": True, "applied": "preview", "preview": dict(state["debrief_pending"])}
    if current not in _CHIP_CHOICES:
        return _already_chosen(state)
    # 条件转移：「清除这个人」的作废步骤不拿这把锁、会把未定的 debrief 直接改成 forget，不能盖掉它
    if await spool.update_state_if(current, debrief_choice="generating:diary") is None:
        return await _moved_on(spool)
    try:
        preview = await debrief_writers.generate_diary_preview(
            spool, llm=_diary_llm(), own_char=own_char, family_names=await _family_names(),
        )
    except debrief_writers.NothingToRecord:
        # 芯片只在 spool 有句子时出：走到这里是 spool 事后丢了。与「记忆开却无转录」同一终态（已放弃），
        # 不再每次 bind 重放一个点了必然 404 的芯片
        if await spool.update_state_if("generating:diary", debrief_choice="abandoned", debrief_pending=None,
                                       debrief_chip_pending=False) is None:
            return await _moved_on(spool)
        await spool.delete_if_settled()
        # 已打开的页面上芯片还亮着：让它置灰（choice='abandoned'）
        await _send(state, _state_frame(visit_id, "chosen", chips_request_id(visit_id), choice="abandoned"))
        return 404, {"error": "unknown_or_expired"}
    except debrief_writers.DiaryGenerationFailed:
        # 留在 generating:diary：再点「记成日记」重新生成
        return 503, {"retry": True}
    pending = preview.to_pending()
    # 预览、状态与待投递标记一次原子写：之后 bind 重放的就是这一份。只在仍处于 generating 时写：
    # 生成期间「清除这个人」已把这场作废（forget）时，不能把这个人的日记与事实重新落盘
    if await spool.update_state_if("generating:diary", debrief_pending=pending, debrief_choice="preview:diary",
                                   debrief_chip_pending=True) is None:
        return await _moved_on(spool)
    if await own_visit_uid() != state["own_uid"]:
        # 生成期间登出 / 换了账号：预览已落盘、等那个账号回来 bind 时重放，不推给现在这一页、也不回给它
        return 404, {"error": "unknown_or_expired"}
    lang = _lang()
    await _render(state, preview_blocks(visit_id, pending, lang), preview_request_id(visit_id))
    await _send(state, _state_frame(visit_id, "preview", preview_request_id(visit_id)))
    return 200, {"ok": True, "applied": "preview", "preview": pending}


def _parse_choice(payload: Mapping[str, Any]) -> tuple[Optional[str], Optional[str], Optional[int], Optional[str]]:
    from utils.visit_wire import VISIT_ID_RE

    visit_id = payload.get("visit_id")
    if not isinstance(visit_id, str) or VISIT_ID_RE.fullmatch(visit_id) is None:
        return None, None, None, "visit_id_format"
    choice = payload.get("choice")
    if choice not in CHOICES:
        return None, None, None, "invalid_choice"
    seq = payload.get("seq")
    if seq is not None and (isinstance(seq, bool) or not isinstance(seq, int) or seq < 1):
        return None, None, None, "invalid_seq"
    return visit_id, choice, seq, None


@router.post("/debrief/choice")
async def debrief_choice(request: Request):
    """The user's answer to the debrief chips / preview / failure block (§4.6; state machine in §3.7.4)."""
    from main_routers.system_router._shared import _read_json_object
    from main_routers.visit_router.local_guard import http_denied

    payload = await _read_json_object(request)
    denied = http_denied(request, payload)
    if denied is not None:
        return denied
    visit_id, choice, seq, error = _parse_choice(payload)
    if error is not None:
        return JSONResponse({"ok": False, "code": error}, status_code=400)
    try:
        status, body = await _choose(visit_id, choice, seq)
    except Exception as exc:  # noqa: BLE001 - state.json 读写失败等：可重试
        logger.warning("visit %s: debrief choice %s failed: %r", visit_id[:6], choice, exc)
        return JSONResponse({"retry": True}, status_code=503)
    return JSONResponse(body, status_code=status)
