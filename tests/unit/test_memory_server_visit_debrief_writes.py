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

"""memory_server side of "keep as diary" (OD-16 v4, design §4.6, §5 PR-14).

* keyed ``/cache`` (``idempotency_key='visit-debrief:{visit_id}'``): two
  stores confirmed before ``done``, recovery of a ``pending`` key from the
  stores themselves (time-index row, recent entry, retired-key sidecar);
* ``/internal/memory/{name}/visit_facts``: server-stamped fields, ``visit_id``
  idempotency, strict pool reads;
* ``recent.retired_keys.json``: every rewrite of ``recent.json`` records the
  keyed entries it drops.

Everything is rooted in ``tmp_path`` (the real runtime root is never touched).
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from memory import trust_store
from memory.facts import FactStore
from memory.recent import CompressedRecentHistoryManager
from utils import recent_file
from utils.llm_client import AIMessage, HumanMessage, messages_to_dict

pytestmark = pytest.mark.unit

NAME = "Neko"
VISIT = "AbCdEfGhIjKlMnOpQrStUv"
KEY = f"visit-debrief:{VISIT}"


class FakeTimeIndex:
    """``time_indexed.db`` double: rows by session id, with injectable failures."""

    def __init__(self):
        self.rows: dict[str, list] = {}
        self.stores = 0
        self.fail_store = False
        self.fail_lookup = False

    async def ahas_conversation(self, session_id, name):
        if self.fail_lookup:
            raise RuntimeError("time index unavailable")
        return session_id in self.rows

    async def astore_conversation_strict(self, session_id, messages, name):
        if self.fail_store:
            raise RuntimeError("engine unavailable")
        self.stores += 1
        self.rows.setdefault(session_id, []).extend(messages)


class _FakeConfig:
    def __init__(self, path):
        self._path = path

    async def aget_character_data(self):
        return (None, None, None, None, {}, None, None, None, {NAME: self._path})

    def get_character_data(self):
        return (None, None, None, None, {}, None, None, None, {NAME: self._path})


def _recent_manager(recent_path: str) -> CompressedRecentHistoryManager:
    mgr = object.__new__(CompressedRecentHistoryManager)
    mgr._config_manager = _FakeConfig(recent_path)
    mgr.max_history_length = 4
    mgr.compress_threshold = 5
    mgr.log_file_path = {NAME: recent_path}
    mgr.name_mapping = {"human": "H", "ai": NAME, "system": "SYSTEM_MESSAGE"}
    mgr.user_histories = {}
    return mgr


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.memory_server import post_turn, routes, runtime

    memory_root = tmp_path / "memory"
    (memory_root / NAME).mkdir(parents=True)
    assert str(memory_root).startswith(str(tmp_path))
    cm = MagicMock()
    cm.memory_dir = str(memory_root)
    monkeypatch.setattr(runtime, "_config_manager", cm)
    monkeypatch.setattr("memory.recent.assert_cloudsave_writable", lambda *a, **k: None)
    monkeypatch.setattr("app.memory_server.idempotency.assert_cloudsave_writable", lambda *a, **k: None)
    monkeypatch.setattr(trust_store, "pool_path", lambda: str(tmp_path / "trust.json"))
    trust_store.reset_for_tests()
    fs = FactStore()
    fs._config_manager = cm
    monkeypatch.setattr("memory.facts.assert_cloudsave_writable", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(runtime, "fact_store", fs)
    recent_path = str(memory_root / NAME / "recent.json")
    recent = _recent_manager(recent_path)
    tsdb = FakeTimeIndex()
    monkeypatch.setattr(runtime, "recent_history_manager", recent)
    monkeypatch.setattr(runtime, "time_manager", tsdb)
    spawn = AsyncMock(return_value=None)
    monkeypatch.setattr(post_turn, "_spawn_outbox_post_turn_signals", spawn)
    monkeypatch.setattr("app.memory_server.gates._aclear_review_clean", AsyncMock(return_value=None))
    yield SimpleNamespace(routes=routes, recent=recent, recent_path=recent_path, tsdb=tsdb, spawn=spawn,
                          fs=fs, root=memory_root)
    trust_store.reset_for_tests()


def _diary_request(text="今天去了邻居家，聊了很久。", key=KEY):
    from app.memory_server.routes import HistoryRequest

    history = json.dumps([{"role": "assistant", "content": text}], ensure_ascii=False)
    return HistoryRequest(input_history=history, idempotency_key=key)


async def _cache(env, request):
    response = await env.routes.cache_conversation(request, NAME)
    if hasattr(response, "status_code"):
        return response.status_code, json.loads(response.body)
    return 200, response


def _recent_entries(env):
    if not os.path.exists(env.recent_path):
        return []
    with open(env.recent_path, encoding="utf-8") as handle:
        return json.load(handle)


def _keyed(env):
    return [e for e in _recent_entries(env) if (e.get("data", {}).get("metadata") or {}).get("idempotency_key") == KEY]


async def _key_record(key=KEY):
    from app.memory_server import idempotency

    return await idempotency.read_key(NAME, key)


async def _set_pending(env, key=KEY, request=None):
    from app.memory_server import idempotency, routes

    request = request or _diary_request()
    await idempotency.update_key(NAME, key, idempotency.transition(
        idempotency.KEY_STATE_PENDING, content_hash=routes._canonical_history_hash(request.input_history),
        kind="cache"))


def _session(env):
    return env.routes._keyed_cache_session_id(KEY)


# ── keyed /cache ─────────────────────────────────────────────────────────


async def test_keyed_cache_writes_once_and_a_retry_is_a_duplicate(env):
    status, body = await _cache(env, _diary_request())
    assert (status, body) == (200, {"status": "cached", "count": 1})
    assert len(_keyed(env)) == 1 and env.tsdb.stores == 1
    stored = _keyed(env)[0]
    assert stored["type"] == "ai" and stored["data"]["content"] == "今天去了邻居家，聊了很久。"
    assert (await _key_record())["state"] == "done"
    # 带键写入不登记 post-turn 后处理：恢复判定只看两处存储
    env.spawn.assert_not_awaited()
    status, body = await _cache(env, _diary_request())
    assert body == {"status": "cached", "duplicate": True, "count": 0}
    assert len(_keyed(env)) == 1 and env.tsdb.stores == 1


async def test_unkeyed_cache_still_registers_post_turn_signals(env):
    from app.memory_server.routes import HistoryRequest

    history = json.dumps([{"role": "user", "content": "hi"}, {"role": "assistant", "content": "yo"}])
    env.tsdb.astore_conversation = AsyncMock(return_value=None)
    status, body = await _cache(env, HistoryRequest(input_history=history))
    assert body == {"status": "cached", "count": 2}
    env.spawn.assert_awaited_once()
    assert recent_file.keyed_entry_keys(_recent_entries(env)) == set()


async def test_a_non_visit_key_keeps_the_theater_only_answer(env):
    status, body = await _cache(env, _diary_request(key="visit-debrief:short"))
    assert body == {"status": "error", "message": "idempotency_key_requires_theater_episode"}
    assert _recent_entries(env) == [] and env.tsdb.stores == 0


async def test_pending_key_with_both_stores_written_is_a_duplicate(env):
    # 写完 recent / tsdb、标 done 之前崩溃：重试按两处存储判为已写，不重复追加
    await _cache(env, _diary_request())
    from app.memory_server import idempotency

    data = json.loads(Path(idempotency.keys_path(NAME)).read_text(encoding="utf-8"))
    data[KEY]["state"] = "pending"
    Path(idempotency.keys_path(NAME)).write_text(json.dumps(data), encoding="utf-8")
    status, body = await _cache(env, _diary_request())
    assert body["duplicate"] is True
    assert len(_keyed(env)) == 1 and env.tsdb.stores == 1
    assert (await _key_record())["state"] == "done"


async def test_time_index_row_without_recent_entry_writes_recent_only(env):
    await _set_pending(env)
    env.tsdb.rows[_session(env)] = [AIMessage(content="x")]
    status, body = await _cache(env, _diary_request())
    assert body == {"status": "cached", "count": 1}
    assert env.tsdb.stores == 0 and len(_keyed(env)) == 1


async def test_pending_key_with_nothing_written_writes_both(env):
    # 标 pending 之后、写入之前崩溃：两处都没有，重试照常写入（不能当作已写）
    await _set_pending(env)
    status, body = await _cache(env, _diary_request())
    assert body == {"status": "cached", "count": 1}
    assert env.tsdb.stores == 1 and len(_keyed(env)) == 1


async def test_entry_moved_out_of_recent_is_not_written_back(env):
    # 两处都写完、标 done 前中断，随后 recent 被整表改写（压缩 / 裁剪 / 浏览器保存）挤掉这条：
    # 退役旁路记下了键，重试判为已写
    await _cache(env, _diary_request())
    from app.memory_server import idempotency

    data = json.loads(Path(idempotency.keys_path(NAME)).read_text(encoding="utf-8"))
    data[KEY]["state"] = "pending"
    Path(idempotency.keys_path(NAME)).write_text(json.dumps(data), encoding="utf-8")
    recent_file.write_recent_payload(env.recent_path, messages_to_dict([HumanMessage(content="later")]))
    assert _keyed(env) == []
    assert KEY in recent_file.read_retired_keys(env.recent_path)
    env.recent.user_histories.clear()
    status, body = await _cache(env, _diary_request())
    assert body["duplicate"] is True and _keyed(env) == []


async def test_same_text_of_another_visit_is_written(env):
    # 按键核对，不按正文互认：两场日记正文相同，第二场照常写
    await _cache(env, _diary_request())
    other = "visit-debrief:" + "Z" * 22
    status, body = await _cache(env, _diary_request(key=other))
    assert body == {"status": "cached", "count": 1}
    assert recent_file.keyed_entry_keys(_recent_entries(env)) == {KEY, other}


@pytest.mark.parametrize("failure", ["store", "lookup"])
async def test_unconfirmed_time_index_answers_503_and_keeps_pending(env, failure):
    setattr(env.tsdb, f"fail_{failure}", True)
    status, body = await _cache(env, _diary_request())
    assert status == 503 and body["status"] == "error"
    assert (await _key_record())["state"] == "pending"
    assert _recent_entries(env) == []


async def test_unreadable_recent_answers_503(env, monkeypatch):
    def unreadable(*_a, **_k):
        raise PermissionError("locked")

    Path(env.recent_path).write_text("[]", encoding="utf-8")
    monkeypatch.setattr(recent_file, "read_recent_text_unlocked", unreadable)
    status, _ = await _cache(env, _diary_request())
    assert status == 503 and (await _key_record())["state"] == "pending"


async def test_keyed_cache_accepts_assistant_messages_only(env):
    from app.memory_server.routes import HistoryRequest

    history = json.dumps([{"role": "user", "content": "记住我喜欢鱼"}])
    status, _ = await _cache(env, HistoryRequest(input_history=history, idempotency_key=KEY))
    assert status == 422 and _recent_entries(env) == []


async def test_pending_key_with_other_content_is_refused(env):
    await _set_pending(env, request=_diary_request("另一份"))
    status, _ = await _cache(env, _diary_request())
    assert status == 422 and env.tsdb.stores == 0


async def test_keys_survive_a_restart_and_compaction(env):
    # 键记录在 idempotency_keys.json（不在 recent.json）：重启（内存清空）+ 压缩后同键仍去重
    await _cache(env, _diary_request())
    recent_file.write_recent_payload(env.recent_path, [])
    env.recent.user_histories.clear()
    status, body = await _cache(env, _diary_request())
    assert body["duplicate"] is True and _keyed(env) == []


# ── recent.retired_keys.json ──────────────────────────────────────────────


def _keyed_entry(text, key):
    msg = AIMessage(content=text)
    msg.metadata = {"idempotency_key": key}
    return msg


def test_rewrites_record_dropped_keyed_entries(tmp_path):
    path = str(tmp_path / "recent.json")
    recent_file.write_recent_payload(path, messages_to_dict([_keyed_entry("d", "k1"), HumanMessage(content="h")]))
    recent_file.write_recent_payload(path, messages_to_dict([HumanMessage(content="h")]))
    assert set(recent_file.read_retired_keys(path)) == {"k1"}
    # 已记的键保留第一次的时间
    first = recent_file.read_retired_keys(path)["k1"]
    recent_file.write_recent_payload(path, [])
    assert recent_file.read_retired_keys(path)["k1"] == first
    # recent.json 的根仍是 list
    assert isinstance(json.loads(Path(path).read_text(encoding="utf-8")), list)


def test_unkeyed_rewrites_leave_no_sidecar(tmp_path):
    path = str(tmp_path / "recent.json")
    recent_file.write_recent_payload(path, messages_to_dict([HumanMessage(content="a"), AIMessage(content="b")]))
    recent_file.write_recent_payload(path, [])
    assert not os.path.exists(recent_file.recent_sidecar_path(path, recent_file.RETIRED_KEYS_FILENAME))


def test_sidecar_is_written_before_recent_is_replaced(tmp_path, monkeypatch):
    # 故障注入：写完旁路、替换 recent.json 之前崩溃 → 键已记、条目仍在，两者都是「写过」的证据
    path = str(tmp_path / "recent.json")
    recent_file.write_recent_payload(path, messages_to_dict([_keyed_entry("d", "k1")]))

    real = recent_file.atomic_write_json

    def crash(target, *a, **k):
        if os.path.basename(str(target)) == "recent.json":
            raise OSError("killed")
        return real(target, *a, **k)

    monkeypatch.setattr(recent_file, "atomic_write_json", crash)
    with pytest.raises(OSError):
        recent_file.write_recent_payload(path, [])
    assert recent_file.keyed_entry_keys(json.loads(Path(path).read_text(encoding="utf-8"))) == {"k1"}
    assert "k1" in recent_file.read_retired_keys(path)


async def test_manager_keyed_append_sees_present_and_retired_keys(tmp_path, monkeypatch):
    # 压缩、硬裁剪、改人设清空、浏览器保存都经 write_recent_payload_unlocked 整表改写
    monkeypatch.setattr("memory.recent.assert_cloudsave_writable", lambda *a, **k: None)
    path = str(tmp_path / "recent.json")
    mgr = _recent_manager(path)
    assert await mgr.append_keyed([_keyed_entry("d", "k1")], NAME, "k1") == "written"
    assert await mgr.append_keyed([_keyed_entry("d", "k1")], NAME, "k1") == "present"
    await mgr.update_history([HumanMessage(content="x")], NAME, compress=False)
    with recent_file.recent_file_access(path) as resolved:
        recent_file.write_recent_payload_unlocked(resolved, messages_to_dict([HumanMessage(content="x")]))
    mgr.user_histories.clear()
    assert await mgr.append_keyed([_keyed_entry("d", "k1")], NAME, "k1") == "retired"
    assert recent_file.keyed_entry_keys(json.loads(Path(path).read_text(encoding="utf-8"))) == set()


def test_cloud_apply_records_keys_before_replacing_recent(tmp_path):
    from utils.cloudsave_runtime.staging import retire_recent_keys_before_replace

    target = tmp_path / "recent.json"
    recent_file.write_recent_payload(str(target), messages_to_dict([_keyed_entry("d", "k1")]))
    staged = tmp_path / "staged.json"
    staged.write_text("[]", encoding="utf-8")
    retire_recent_keys_before_replace(target, staged)
    assert "k1" in recent_file.read_retired_keys(str(target))
    recent_file.write_recent_payload(str(target), messages_to_dict([_keyed_entry("d", "k3")]))
    retire_recent_keys_before_replace(target, None)
    assert "k3" in recent_file.read_retired_keys(str(target))


async def test_cleanup_drops_old_records_of_terminal_keys_only(env):
    from app.memory_server import idempotency

    path = os.path.join(str(env.root), NAME, "recent.json")
    recent_file.write_recent_payload(path, messages_to_dict([_keyed_entry("a", "done-key"),
                                                             _keyed_entry("b", "pending-key")]))
    recent_file.write_recent_payload(path, [])
    await idempotency.update_key(NAME, "done-key", idempotency.transition(idempotency.KEY_STATE_DONE))
    await idempotency.update_key(NAME, "pending-key", idempotency.transition(idempotency.KEY_STATE_PENDING))
    report = await idempotency.cleanup_expired([NAME], ttl_s=0, now=10**12)
    assert report["retired_removed"] == 1
    # pending 键的退役记录不过期：它是「已写过 recent」的唯一证据
    assert set(recent_file.read_retired_keys(path)) == {"pending-key"}
    # done 键记录本身永久保留
    assert (await idempotency.read_key(NAME, "done-key"))["state"] == "done"


def test_only_the_shared_writer_and_cloud_apply_replace_recent_json():
    """Static guard: no other code replaces or deletes ``recent.json`` directly.

    Every rewrite must go through ``utils.recent_file.write_recent_payload_unlocked``
    (which records dropped keyed entries); cloud applies call
    ``retire_recent_keys_before_replace`` first.
    """
    import ast

    root = Path(__file__).resolve().parents[2]
    allowed = {Path("utils/recent_file.py"), Path("utils/cloudsave_runtime/staging.py"),
               Path("utils/cloudsave_runtime/operations.py")}
    writers = {"atomic_write_json", "atomic_write_text", "atomic_write_json_async", "replace", "remove", "unlink"}
    offenders = []
    for folder in ("app", "main_logic", "main_routers", "memory", "utils"):
        for path in (root / folder).rglob("*.py"):
            rel = path.relative_to(root)
            if rel in allowed:
                continue
            source = path.read_text(encoding="utf-8")
            if "recent" not in source:
                continue
            for node in ast.walk(ast.parse(source)):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name not in writers:
                    continue
                target = ast.unparse(node.args[0]) if node.args else ""
                if isinstance(func, ast.Attribute) and name == "unlink":
                    target = ast.unparse(func.value)
                if "recent" in target.lower() and "sidecar" not in target.lower():
                    offenders.append(f"{rel}:{node.lineno}: {ast.unparse(node)[:80]}")
    assert offenders == []


# ── visit_facts ──────────────────────────────────────────────────────────


async def _facts(env, facts, visit_id=VISIT, **extra):
    from app.memory_server.routes import VisitFactsRequest

    req = VisitFactsRequest(visit_id=visit_id, facts=[{"text": f, **extra} for f in facts])
    return await env.routes.write_visit_facts(NAME, req)


def _pool(env):
    return env.fs.load_facts_strict(NAME, include_archive=True)


async def test_visit_facts_are_stamped_by_the_server(env):
    body = await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"], importance=9, absorbed=False)
    assert body == {"ok": True, "written": 2, "deduped": 0}
    rows = [r for r in _pool(env) if r.get("visit_id") == VISIT]
    assert len(rows) == 2
    for row in rows:
        assert (row["importance"], row["absorbed"], row["origin"], row["source"]) == (
            4, True, "neko_visit", "ai_disclosure")
    # reflection 只取 importance>=5 且未 absorbed：永远取不到串门事实
    unabsorbed = await env.fs.aget_unabsorbed_facts(NAME, min_importance=5)
    assert not any(f.get("origin") == "neko_visit" for f in unabsorbed)
    unabsorbed_any = await env.fs.aget_unabsorbed_facts(NAME, min_importance=0)
    assert not any(f.get("origin") == "neko_visit" for f in unabsorbed_any)


async def test_same_visit_id_is_a_duplicate_with_the_first_count(env):
    await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"])
    body = await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"])
    assert body["duplicate"] is True and body["written"] == 2
    assert len([r for r in _pool(env) if r.get("visit_id") == VISIT]) == 2


async def test_pending_key_after_a_saved_batch_reports_the_real_count(env):
    from app.memory_server import idempotency

    await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"])
    data = json.loads(Path(idempotency.keys_path(NAME)).read_text(encoding="utf-8"))
    data[f"visit-facts:{VISIT}"] = {"state": "pending", "written_at": 1.0}
    Path(idempotency.keys_path(NAME)).write_text(json.dumps(data), encoding="utf-8")
    body = await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"])
    assert body == {"ok": True, "written": 2, "deduped": 0, "duplicate": True}
    assert (await _key_record(f"visit-facts:{VISIT}"))["state"] == "done"


async def test_archived_facts_of_the_visit_still_count(env):
    from app.memory_server import idempotency

    await _facts(env, ["在邻居家看了星星"])
    active = Path(env.fs._facts_path(NAME))
    archive = Path(env.fs._facts_archive_path(NAME))
    archive.write_text(active.read_text(encoding="utf-8"), encoding="utf-8")
    active.write_text("[]", encoding="utf-8")
    env.fs._facts.pop(NAME, None)
    data = json.loads(Path(idempotency.keys_path(NAME)).read_text(encoding="utf-8"))
    data[f"visit-facts:{VISIT}"] = {"state": "pending", "written_at": 1.0}
    Path(idempotency.keys_path(NAME)).write_text(json.dumps(data), encoding="utf-8")
    body = await _facts(env, ["在邻居家看了星星"])
    assert body["duplicate"] is True and body["written"] == 1


@pytest.mark.parametrize("which", ["active", "archive"])
async def test_an_unreadable_pool_writes_nothing(env, which):
    from fastapi import HTTPException

    await _facts(env, ["旧事实"], visit_id="Y" * 22)
    path = Path(env.fs._facts_path(NAME) if which == "active" else env.fs._facts_archive_path(NAME))
    path.write_text("{not json", encoding="utf-8")
    before = path.read_bytes()
    env.fs._facts.pop(NAME, None)
    with pytest.raises(HTTPException) as info:
        await _facts(env, ["在邻居家看了星星"])
    assert info.value.status_code == 503
    assert path.read_bytes() == before
    assert NAME not in env.fs._facts


async def test_empty_fact_list_records_the_key(env):
    assert await _facts(env, []) == {"ok": True, "written": 0, "deduped": 0}
    assert (await _facts(env, []))["duplicate"] is True


async def test_visit_facts_is_a_fenced_write_op():
    from app.memory_server import runtime

    assert runtime._character_write_name_from_path(f"/internal/memory/{NAME}/visit_facts", "POST") == NAME


def test_visit_facts_request_bounds():
    from pydantic import ValidationError

    from app.memory_server.routes import VisitFactsRequest

    with pytest.raises(ValidationError):
        VisitFactsRequest(visit_id=VISIT, facts=[{"text": "x"}] * 4)
    with pytest.raises(ValidationError):
        VisitFactsRequest(visit_id=VISIT, facts=[{"text": "长" * 61}])
    with pytest.raises(ValidationError):
        VisitFactsRequest(visit_id="short", facts=[])


async def test_new_facts_only_get_visit_provenance(env):
    # 精确去重命中的旧事实不升级这些字段
    await env.fs._apersist_new_facts(NAME, [{"text": "邻居家有只白猫", "importance": 6}])
    body = await _facts(env, ["邻居家有只白猫"])
    assert body == {"ok": True, "written": 0, "deduped": 1}
    rows = [r for r in _pool(env) if r["text"] == "邻居家有只白猫"]
    assert len(rows) == 1 and "origin" not in rows[0] and rows[0]["absorbed"] is False


async def test_concurrent_same_visit_writes_once(env):
    results = await asyncio.gather(_facts(env, ["在邻居家看了星星"]), _facts(env, ["在邻居家看了星星"]))
    assert sorted(r.get("duplicate", False) for r in results) == [False, True]
    assert len([r for r in _pool(env) if r.get("visit_id") == VISIT]) == 1


# ── 评审第 1 轮 ──────────────────────────────────────────────────────────


async def test_a_retired_key_writes_neither_store(env):
    # 云存档连 time_indexed.db 一起回滚、退役旁路记着本键：不把恢复掉的日记补回时间索引
    await _set_pending(env)
    recent_file.write_recent_payload(env.recent_path, messages_to_dict([_keyed_entry("d", KEY)]))
    recent_file.write_recent_payload(env.recent_path, [])
    env.recent.user_histories.clear()
    status, body = await _cache(env, _diary_request())
    assert body["duplicate"] is True and env.tsdb.stores == 0 and _keyed(env) == []
    assert (await _key_record())["state"] == "done"


async def test_a_done_key_with_other_content_is_refused(env):
    await _cache(env, _diary_request())
    status, body = await _cache(env, _diary_request("改过的日记"))
    assert status == 422 and len(_keyed(env)) == 1


def test_periodic_extraction_skips_keyed_diaries():
    from app.memory_server.rows import _extract_role_tagged_messages_from_rows

    diary = messages_to_dict([_keyed_entry("今天去了邻居家，她说她住在三号楼", KEY)])[0]
    rows = [
        (1, "s", json.dumps({"type": "human", "data": {"content": "回来啦"}})),
        (2, "s", json.dumps(diary, ensure_ascii=False)),
        (3, "s", json.dumps({"type": "ai", "data": {"content": "嗯"}})),
    ]
    out = _extract_role_tagged_messages_from_rows(rows)
    assert [m["data"]["content"] for m in out] == ["回来啦", "嗯"]


async def test_strict_load_counts_an_interrupted_archive_row_once(env):
    from app.memory_server import idempotency

    await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"])
    active = Path(env.fs._facts_path(NAME))
    Path(env.fs._facts_archive_path(NAME)).write_text(active.read_text(encoding="utf-8"), encoding="utf-8")
    assert len([r for r in _pool(env) if r.get("visit_id") == VISIT]) == 2
    data = json.loads(Path(idempotency.keys_path(NAME)).read_text(encoding="utf-8"))
    data[f"visit-facts:{VISIT}"] = {"state": "pending", "written_at": 1.0}
    Path(idempotency.keys_path(NAME)).write_text(json.dumps(data), encoding="utf-8")
    assert (await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"]))["written"] == 2


async def test_visit_facts_skip_semantic_arbitration(env, monkeypatch):
    seen = {}
    real = env.fs._apersist_new_facts

    async def spy(*args, **kwargs):
        seen.update(kwargs)
        return await real(*args, **kwargs)

    monkeypatch.setattr(env.fs, "_apersist_new_facts", spy)
    await _facts(env, ["在邻居家看了星星"])
    assert seen["semantic_dedup"] is False


# ── 评审第 2 轮 ──────────────────────────────────────────────────────────


@pytest.mark.parametrize("settle", ["done", "pending"])
async def test_visit_facts_reject_a_changed_fact_list(env, settle):
    from fastapi import HTTPException

    from app.memory_server import idempotency

    await _facts(env, ["在邻居家看了星星"])
    if settle == "pending":
        data = json.loads(Path(idempotency.keys_path(NAME)).read_text(encoding="utf-8"))
        data[f"visit-facts:{VISIT}"]["state"] = "pending"
        Path(idempotency.keys_path(NAME)).write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(HTTPException) as info:
        await _facts(env, ["改过的事实"])
    assert info.value.status_code == 422
    assert [r["text"] for r in _pool(env) if r.get("visit_id") == VISIT] == ["在邻居家看了星星"]


# ── 评审第 4 轮 ──────────────────────────────────────────────────────────


async def test_model_output_cannot_claim_visit_provenance(env):
    # 普通抽取路径里模型多吐一个 _visit_origin：不能把事实标成串门事实、躲开 reflection 与铸卡
    await env.fs._apersist_new_facts(NAME, [
        {"text": "她喜欢晒太阳", "importance": 7, "_visit_origin": {"visit_id": VISIT}},
    ])
    row = [r for r in _pool(env) if r["text"] == "她喜欢晒太阳"][0]
    assert "origin" not in row and "visit_id" not in row and row["absorbed"] is False


def test_extraction_skips_only_visit_diary_keys():
    from app.memory_server.rows import _extract_role_tagged_messages_from_rows

    other = messages_to_dict([_keyed_entry("别的带键消息", "theater:abc")])[0]
    human = {"type": "human", "data": {"content": "人说的", "metadata": {"idempotency_key": "visit-debrief:x"}}}
    rows = [(1, "s", json.dumps(other, ensure_ascii=False)), (2, "s", json.dumps(human, ensure_ascii=False))]
    assert [m["data"]["content"] for m in _extract_role_tagged_messages_from_rows(rows)] == ["别的带键消息", "人说的"]


async def test_cleanup_reads_key_records_under_the_recent_lock(env, monkeypatch):
    # 清理排队等 recent 文件锁期间，云存档恢复删掉了键记录：锁内重读，保留退役记录
    from app.memory_server import idempotency

    path = os.path.join(str(env.root), NAME, "recent.json")
    recent_file.write_recent_payload(path, messages_to_dict([_keyed_entry("a", "done-key")]))
    recent_file.write_recent_payload(path, [])
    await idempotency.update_key(NAME, "done-key", idempotency.transition(idempotency.KEY_STATE_DONE))
    real = recent_file.prune_retired_keys

    def restore_then_prune(target, should_drop):
        os.remove(idempotency.keys_path(NAME))
        return real(target, should_drop)

    monkeypatch.setattr(recent_file, "prune_retired_keys", restore_then_prune)
    report = await idempotency.cleanup_expired([NAME], ttl_s=0, now=10**12)
    assert report["retired_removed"] == 0
    assert "done-key" in recent_file.read_retired_keys(path)


# ── 评审第 5 轮 ──────────────────────────────────────────────────────────


async def test_a_pending_retry_fills_in_missing_visit_facts(env):
    from app.memory_server import idempotency

    await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"])
    # 标 done 之前中断，且其中一条随后被删掉
    rows = json.loads(Path(env.fs._facts_path(NAME)).read_text(encoding="utf-8"))
    Path(env.fs._facts_path(NAME)).write_text(json.dumps([r for r in rows if r["text"] != "邻居家有只白猫"],
                                                         ensure_ascii=False), encoding="utf-8")
    env.fs._facts.pop(NAME, None)
    data = json.loads(Path(idempotency.keys_path(NAME)).read_text(encoding="utf-8"))
    data[f"visit-facts:{VISIT}"]["state"] = "pending"
    Path(idempotency.keys_path(NAME)).write_text(json.dumps(data), encoding="utf-8")
    body = await _facts(env, ["在邻居家看了星星", "邻居家有只白猫"])
    assert body["written"] == 2
    assert sorted(r["text"] for r in _pool(env) if r.get("visit_id") == VISIT) == ["在邻居家看了星星", "邻居家有只白猫"]


def test_extraction_skips_only_well_formed_diary_keys():
    from app.memory_server.rows import _extract_role_tagged_messages_from_rows

    manual = messages_to_dict([_keyed_entry("普通助手消息", "visit-debrief:manual")])[0]
    rows = [(1, "s", json.dumps(manual, ensure_ascii=False))]
    assert [m["data"]["content"] for m in _extract_role_tagged_messages_from_rows(rows)] == ["普通助手消息"]


def test_a_deeply_nested_sidecar_does_not_block_recent_writes(tmp_path):
    path = str(tmp_path / "recent.json")
    recent_file.write_recent_payload(path, messages_to_dict([_keyed_entry("d", "k1")]))
    sidecar = recent_file.recent_sidecar_path(path, recent_file.RETIRED_KEYS_FILENAME)
    Path(sidecar).write_text("[" * 100000 + "]" * 100000, encoding="utf-8")
    recent_file.write_recent_payload(path, [])
    assert json.loads(Path(path).read_text(encoding="utf-8")) == []
    assert "k1" in recent_file.read_retired_keys(path)
