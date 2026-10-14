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

"""Derived relationship continuity for private character ↔ master chat.

PATCH 08 deliberately does *not* introduce a second authoritative relationship
memory store and does not let an LLM write opaque trust/intimacy scores.
Instead this module derives a small, rebuildable relationship lens from memory
that N.E.K.O already considers durable enough to render:

* learned, non-protected ``persona.relationship`` entries; and
* confirmed/promoted ``relationship`` reflections.

The source of truth therefore remains the existing facts → reflections →
persona pipeline.  If a reflection is corrected, suppressed, refined, promoted,
or archived, the derived lens follows automatically on the next new-dialog
bootstrap.

The continuity band describes *accumulated shared-history evidence only*.  It
never overrides the fictional relationship declared by the character card.
This distinction is important for characters whose premise already starts with
a close/familial relationship: a fresh installation can have little learned
history without implying that the characters are strangers.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import re
from typing import Iterable

_RELATION_TYPES = frozenset(
    {"dynamic", "milestone", "tension", "shared_memory", "agreement"}
)
_ANCHOR_TYPES = frozenset({"milestone", "shared_memory", "agreement"})
_ACTIVE_REFLECTION_STATUSES = frozenset({"confirmed", "promoted"})


@dataclass(frozen=True)
class RelationshipContinuity:
    """Small deterministic view over learned relationship memory."""

    band: str
    evidence_count: int
    distinct_days: int
    span_days: int
    dynamic_count: int
    milestone_count: int
    shared_memory_count: int
    agreement_count: int
    active_tension_count: int
    uncategorized_count: int

    @property
    def anchor_count(self) -> int:
        return self.milestone_count + self.shared_memory_count + self.agreement_count


@dataclass
class _EvidenceRecord:
    text_key: str
    relation_type: str | None
    observed_at: datetime | None
    active_tension: bool = False


def _normalize_text(value: object) -> str:
    """Normalize only for deduplication; never rewrite the stored memory text."""
    text = str(value or "").strip().casefold()
    return re.sub(r"\s+", " ", text)


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    raw = value.strip()
    if not raw:
        return None
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except (TypeError, ValueError):
        return None
    # All current N.E.K.O memory timestamps are local-naive or ISO-offset.
    # Comparing mixed aware/naive datetimes raises, so normalize aware values
    # to local wall-clock *without* inventing a timezone for naive legacy rows.
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def _observed_at(entry: dict) -> datetime | None:
    """Best available time at which this learned memory entered the system."""
    for key in (
        "created_at",
        "confirmed_at",
        "promoted_at",
        "updated_at",
        "event_start_at",
    ):
        parsed = _parse_datetime(entry.get(key))
        if parsed is not None:
            return parsed
    return None


def _is_past_reflection(entry: dict, *, now: datetime) -> bool:
    """Delegate expiry semantics to the canonical temporal helper.

    Import lazily so this pure helper remains cheap to import during startup and
    unit tests can exercise the rest of the policy without constructing the
    complete memory runtime.  Production N.E.K.O always provides this module.
    On an unexpected helper failure we conservatively treat the reflection as
    active: hiding a real current tension would be worse than keeping an old one
    for one bootstrap turn.
    """
    try:
        from memory.temporal import is_past_for_render

        return bool(is_past_for_render(entry, now=now))
    except Exception:
        return False


def _iter_relationship_entries(
    persona: dict | None,
    confirmed_reflections: Iterable[dict] | None,
    *,
    now: datetime,
):
    relationship = persona.get("relationship") if isinstance(persona, dict) else None
    if isinstance(relationship, dict):
        facts = relationship.get("facts")
        if isinstance(facts, list):
            for entry in facts:
                if not isinstance(entry, dict):
                    continue
                # Character-card relationship premise is authoritative baseline,
                # not learned shared history.  Counting it would instantly mark
                # a brand-new profile as an "established" learned relationship.
                if entry.get("protected"):
                    continue
                text_key = _normalize_text(entry.get("text"))
                if not text_key:
                    continue
                relation_type = str(entry.get("relation_type") or "").strip()
                if relation_type not in _RELATION_TYPES:
                    relation_type = None
                yield _EvidenceRecord(
                    text_key=text_key,
                    relation_type=relation_type,
                    observed_at=_observed_at(entry),
                    active_tension=(relation_type == "tension"),
                )

    for entry in confirmed_reflections or ():
        if not isinstance(entry, dict):
            continue
        if str(entry.get("entity") or "").strip() != "relationship":
            continue
        status = str(entry.get("status") or "").strip()
        if status and status not in _ACTIVE_REFLECTION_STATUSES:
            continue
        text_key = _normalize_text(entry.get("text"))
        if not text_key:
            continue
        relation_type = str(entry.get("relation_type") or "").strip()
        if relation_type not in _RELATION_TYPES:
            relation_type = None
        yield _EvidenceRecord(
            text_key=text_key,
            relation_type=relation_type,
            observed_at=_observed_at(entry),
            active_tension=(
                relation_type == "tension"
                and not _is_past_reflection(entry, now=now)
            ),
        )


def build_relationship_continuity(
    persona: dict | None,
    confirmed_reflections: Iterable[dict] | None,
    *,
    now: datetime | None = None,
) -> RelationshipContinuity | None:
    """Build a conservative relationship-continuity view.

    The policy intentionally requires both *volume* and *time spread* before the
    band advances.  A dozen relationship-flavoured messages in one evening must
    not manufacture instant intimacy ("fast-food intimacy").

    ``forming``
        default while learned evidence is sparse or confined to one day.
    ``developing``
        at least four distinct learned observations across at least two days.
    ``established``
        at least eight observations across five distinct days and >=14 days of
        span, with at least two concrete shared-history anchors
        (milestone/shared_memory/agreement).

    These are pacing guards, not psychological scores.  No band encodes love,
    trust, attachment, obedience, or permission.
    """
    now = now or datetime.now()
    deduped: dict[str, _EvidenceRecord] = {}

    for record in _iter_relationship_entries(
        persona,
        confirmed_reflections,
        now=now,
    ):
        current = deduped.get(record.text_key)
        if current is None:
            deduped[record.text_key] = record
            continue

        # The same promoted memory can be visible once in persona and once as a
        # still-confirmed reflection.  Count it once, but keep richer metadata.
        if current.relation_type is None and record.relation_type is not None:
            current.relation_type = record.relation_type
        if current.observed_at is None:
            current.observed_at = record.observed_at
        elif record.observed_at is not None and record.observed_at < current.observed_at:
            current.observed_at = record.observed_at
        current.active_tension = current.active_tension or record.active_tension

    if not deduped:
        return None

    counts = {kind: 0 for kind in _RELATION_TYPES}
    uncategorized = 0
    active_tensions = 0
    observed_times: list[datetime] = []

    for record in deduped.values():
        if record.relation_type in counts:
            counts[record.relation_type] += 1
        else:
            uncategorized += 1
        if record.active_tension:
            active_tensions += 1
        if record.observed_at is not None:
            observed_times.append(record.observed_at)

    distinct_days = len({dt.date() for dt in observed_times})
    span_days = 0
    if len(observed_times) >= 2:
        span_days = max(0, (max(observed_times).date() - min(observed_times).date()).days)

    evidence_count = len(deduped)
    anchor_count = sum(counts[kind] for kind in _ANCHOR_TYPES)

    band = "forming"
    if (
        evidence_count >= 8
        and distinct_days >= 5
        and span_days >= 14
        and anchor_count >= 2
    ):
        band = "established"
    elif evidence_count >= 4 and distinct_days >= 2 and span_days >= 1:
        band = "developing"

    return RelationshipContinuity(
        band=band,
        evidence_count=evidence_count,
        distinct_days=distinct_days,
        span_days=span_days,
        dynamic_count=counts["dynamic"],
        milestone_count=counts["milestone"],
        shared_memory_count=counts["shared_memory"],
        agreement_count=counts["agreement"],
        active_tension_count=active_tensions,
        uncategorized_count=uncategorized,
    )
