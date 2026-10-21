"""Durable query identity and ordinary recovery isolation."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest
from sqlalchemy import text

from storyloop_harness import ScenarioPackage
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.config import load_settings
from storyloop_platform.portal.turn_errors import TurnRecoveryRequired
from test_player_fact_queries import QUERY, ROOT, counts, fact_api, post


def test_postcommit_receipt_failure_retries_without_duplicate_world_effects(fact_api, monkeypatch):
    portal, client, headers, _ = fact_api
    original = portal.accounts.store_fact_query_response
    calls = 0

    def fail_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("synthetic receipt write failure")
        return original(*args, **kwargs)

    monkeypatch.setattr(portal.accounts, "store_fact_query_response", fail_once)
    failure = post(client, headers)
    assert failure.status_code == 500
    assert failure.json()["commit_state"] == "unknown"
    after_commit = counts(portal)
    assert after_commit["portal_turns"] == 0
    response = post(client, headers)
    assert response.status_code == 200, response.text
    after_retry = counts(portal)
    assert after_retry["portal_turns"] == 1
    assert all(after_retry[key] == after_commit[key] for key in ("events", "observations",
        "model_execution_audits", "turn_settlement_intents", "credit_ledger"))


@pytest.mark.parametrize("change", [
    {"fact_id": "harbor.private"}, {"speaker_id": "player"},
])
def test_same_id_changed_identity_rejected_without_new_event(fact_api, change):
    portal, client, headers, _ = fact_api
    assert post(client, headers).status_code == 200
    before = counts(portal)
    response = post(client, headers, {**QUERY, **change})
    assert response.status_code == 400
    assert counts(portal) == before


def test_same_id_canonical_json_ordinary_turn_receipt_cannot_replay_as_fact(fact_api):
    portal, client, headers, _ = fact_api
    canonical = json.dumps({"kind": "fact_query", "fact_id": QUERY["fact_id"],
        "speaker_id": QUERY["speaker_id"]}, ensure_ascii=False,
        sort_keys=True, separators=(",", ":"))
    portal.accounts.store_turn_response("fact-save", QUERY["request_id"], canonical,
                                        {"body": "ordinary"})
    before = counts(portal)
    response = post(client, headers)
    assert response.status_code == 400
    assert counts(portal) == before


def test_same_id_settlement_and_other_unresolved_ordinary_block_new_query(fact_api):
    portal, client, headers, _ = fact_api
    with portal.accounts.engine.begin() as db:
        db.execute(text("""INSERT INTO turn_settlement_intents
            (game_id,request_id,input_hash,schema_version,response_json,usage_json,
             policy_json,created_order) VALUES
            ('fact-save','ordinary','hash',1,'{}','[]','{}',1)"""))
    before = counts(portal)
    assert post(client, headers).status_code == 409
    assert counts(portal) == before
    assert post(client, headers, {**QUERY, "request_id": "ordinary"}).status_code == 409
    portal.accounts.store_turn_response("fact-save", "ordinary", "old action", {"body": "done"})
    assert post(client, headers).status_code == 200


def test_same_id_recovery_context_blocks_query_before_world_write(fact_api):
    portal, client, headers, _ = fact_api
    player_id = portal.accounts.resolve_token(headers["Authorization"].split()[1])
    with portal.accounts.engine.begin() as db:
        db.execute(text("""INSERT INTO turn_recovery_contexts
            (game_id,request_id,player_id,turn_id,input_hash,generation_identity,
             created_order,canonical_json,checksum)
            VALUES ('fact-save','fact-one',:player_id,'portal-fact-one','hash','generation',
                    1,'{}','bad')"""), {"player_id": player_id})
    before = counts(portal)
    assert post(client, headers).status_code == 409
    assert counts(portal) == before


def test_fact_receipt_does_not_hide_same_id_ordinary_orphan(fact_api):
    portal, client, headers, _ = fact_api
    assert post(client, headers).status_code == 200
    with portal.accounts.engine.begin() as db:
        db.execute(text("""INSERT INTO generated_scene_generation_markers
            (game_id,turn_id,marker_id,canonical_json,checksum)
            VALUES ('fact-save','portal-fact-one','poison','{}','bad')"""))
    assert post(client, headers).status_code == 409


def test_unfinished_fact_replays_after_story_completion_but_new_query_does_not(fact_api, monkeypatch):
    portal, client, headers, _ = fact_api
    original = portal.accounts.store_fact_query_response
    monkeypatch.setattr(portal.accounts, "store_fact_query_response",
                        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("receipt failed")))
    failure = post(client, headers)
    assert failure.status_code == 500
    assert failure.json()["commit_state"] == "unknown"
    monkeypatch.setattr(portal.accounts, "store_fact_query_response", original)
    with portal.accounts.engine.begin() as db:
        row = db.execute(text("SELECT data FROM games WHERE game_id='fact-save'")).scalar_one()
        state = json.loads(row)
        state["story_progress"] = {"complete": True}
        db.execute(text("UPDATE games SET data=:data WHERE game_id='fact-save'"),
                   {"data": json.dumps(state, ensure_ascii=False)})
    assert post(client, headers).status_code == 200
    new = post(client, headers, {**QUERY, "request_id": "new-fact"})
    assert new.status_code == 400
    assert new.json()["commit_state"] == "not_started"


def test_plain_recovery_status_cannot_treat_fact_receipt_as_ordinary(fact_api):
    portal, client, headers, _ = fact_api
    assert post(client, headers).status_code == 200
    token = headers["Authorization"].split()[1]
    input_hash = hashlib.sha256("ordinary action".encode()).hexdigest()
    status = portal.turn_recovery_status(token, "fact-save", QUERY["request_id"], input_hash)
    assert status["reason_code"] == "INPUT_MISMATCH"


def test_other_ordinary_input_event_without_receipt_blocks_new_query(fact_api):
    portal, client, headers, _ = fact_api
    with portal.accounts.engine.begin() as db:
        db.execute(text("""INSERT INTO events
            (game_id,event_id,kind,actor_id,cause_id,tick,effects,details,state_version)
            VALUES ('fact-save','portal-orphan:input','player_input','player',NULL,0,
                    '[]','{}',1)"""))
    response = post(client, headers)
    assert response.status_code == 409, response.text


def test_oversized_fact_query_json_rejected(fact_api):
    _, client, headers, _ = fact_api
    response = post(client, headers, {**QUERY, "padding": "x" * 100_000})
    assert response.status_code == 413, response.text


@pytest.mark.parametrize("table,extra", [
    ("generated_scene_snapshots", "snapshot_id"),
    ("generated_scene_generation_markers", "marker_id"),
])
def test_orphan_generation_evidence_blocks_new_query(fact_api, table, extra):
    portal, client, headers, _ = fact_api
    with portal.accounts.engine.begin() as db:
        db.execute(text(f"""INSERT INTO {table}
            (game_id,turn_id,{extra},canonical_json,checksum)
            VALUES ('fact-save','portal-ordinary','synthetic','{{}}','bad')"""))
    assert post(client, headers).status_code == 409


def test_cached_fact_receipt_replays_despite_other_pending_ordinary_evidence(fact_api):
    portal, client, headers, _ = fact_api
    first = post(client, headers)
    assert first.status_code == 200
    with portal.accounts.engine.begin() as db:
        db.execute(text("""INSERT INTO generated_scene_generation_markers
            (game_id,turn_id,marker_id,canonical_json,checksum)
            VALUES ('fact-save','portal-other','poison','{}','bad')"""))
    assert post(client, headers).content == first.content


def test_same_player_turn_and_fact_query_share_lock_while_other_player_proceeds(fact_api, monkeypatch):
    portal, _, headers, other = fact_api
    item = portal.gameplay.game_access.listing_for(other["player_id"], "facts")
    ScenarioPackage.load(item.package_path).seed_game(portal.gameplay.store, "other-save")
    portal.accounts.create_save(other["player_id"], "facts", "other-save",
                                item.package_id, item.package_version, item.fingerprint,
                                "freeform")
    owner_token = headers["Authorization"].split()[1]
    other_token = other["token"]
    entered = asyncio.Event()
    release = asyncio.Event()

    async def held_turn(*_args, **_kwargs):
        entered.set()
        await release.wait()
        return {"body": "held"}

    monkeypatch.setattr(portal.turns, "_turn_for_player", held_turn)

    async def run():
        turn = asyncio.create_task(portal.turn(owner_token, "fact-save", "hello", "turn-lock"))
        await entered.wait()
        own_query = asyncio.create_task(portal.fact_query(owner_token, "fact-save", "fact-own",
                                                          "harbor.secret", "dockhand"))
        await asyncio.sleep(0)
        assert not own_query.done()
        other_query = await portal.fact_query(other_token, "other-save", "fact-other",
                                              "harbor.secret", "dockhand")
        assert other_query["segments"][0]["text"] == "旧码头的钥匙在钟楼。"
        assert not own_query.done()
        release.set()
        await turn
        assert (await own_query)["segments"][0]["text"] == "旧码头的钥匙在钟楼。"

    asyncio.run(run())


def test_shutdown_cancels_admitted_fact_query_and_closes_resources(fact_api, monkeypatch):
    portal, _, headers, _ = fact_api
    item = portal.gameplay.game_access.listing_for("unused", "facts")
    sibling = build_portal(item.package_path.parent / "catalog.json",
                           load_settings(ROOT / "config/local.json"), portal.db_path)
    entered = asyncio.Event()

    class DelayedEngine:
        async def run_fact_query(self, _input):
            entered.set()
            await asyncio.Event().wait()

    monkeypatch.setattr(sibling.fact_queries.factory, "fact_query_engine",
                        lambda *_args: DelayedEngine())

    async def run():
        task = asyncio.create_task(sibling.operations.run(
            lambda: sibling.fact_query(headers["Authorization"].split()[1], "fact-save",
                                       "shutdown", "harbor.secret", "dockhand"),
            name="fact-query"))
        await entered.wait()
        await sibling.shutdown(grace_timeout=0, cancel_timeout=1)
        assert task.cancelled() or isinstance((await asyncio.gather(task, return_exceptions=True))[0],
                                               asyncio.CancelledError)
        assert not sibling.operations.accepting
    asyncio.run(run())
