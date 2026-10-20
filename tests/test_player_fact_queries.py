"""Strict fact queries through the authenticated API and real SQLite store."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from storyloop_harness import ScenarioPackage
from storyloop_harness.world.facts import UNKNOWN_FACT_ANSWER
from storyloop_platform.bootstrap import build_portal
from storyloop_platform.config import load_settings
from storyloop_platform.portal.http_api import create_app


ROOT = Path(__file__).resolve().parents[1]
QUERY = {"request_id": "fact-one", "fact_id": "harbor.secret", "speaker_id": "dockhand"}


@pytest.fixture
def fact_api(tmp_path, request):
    package_dir = tmp_path / "freeform"
    shutil.copytree(ROOT / "examples/freeform", package_dir)
    manifest_file = package_dir / "manifest.json"
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    manifest["initial_work"] = []
    if getattr(request, "param", None) == "novel":
        manifest["presentation_mode"] = "novel"
    manifest["facts"] = [
        {"id": "harbor.secret", "text": "旧码头的钥匙在钟楼。",
         "known_by": ["dockhand"], "disclose_to": ["player"]},
        {"id": "harbor.private", "text": "不能透露的事实。",
         "known_by": ["dockhand"], "disclose_to": []},
        {"id": "harbor.unaware", "text": "这事码头工不知道。",
         "known_by": ["player"], "disclose_to": ["player"]},
        {"id": "harbor.expired", "text": "过期的事实。",
         "known_by": ["dockhand"], "disclose_to": ["player"], "valid_until_tick": 0},
    ]
    manifest_file.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"games": [{"id": "facts", "title": "Facts",
        "mode": "freeform", "package": "freeform"}]}), encoding="utf-8")
    db_path = tmp_path / "portal.db"
    portal = build_portal(catalog, load_settings(ROOT / "config/local.json"), str(db_path))
    owner = portal.register("fact-owner", "password-123")
    other = portal.register("fact-other", "password-123")
    item = portal.gameplay.game_access.listing_for(owner["player_id"], "facts")
    ScenarioPackage.load(item.package_path).seed_game(portal.gameplay.store, "fact-save")
    portal.accounts.create_save(owner["player_id"], "facts", "fact-save",
                                item.package_id, item.package_version, item.fingerprint,
                                "freeform")
    with TestClient(create_app(portal), base_url="http://127.0.0.1",
                    raise_server_exceptions=False) as client:
        yield portal, client, {"Authorization": f"Bearer {owner['token']}"}, other


def post(client, headers, body=QUERY):
    return client.post("/v1/saves/fact-save/fact-queries", json=body, headers=headers)


def counts(portal):
    with portal.accounts.engine.connect() as db:
        return {name: db.execute(text(f"SELECT COUNT(*) FROM {name}")).scalar_one()
                for name in ("events", "observations", "portal_turns",
                             "model_execution_audits", "turn_settlement_intents",
                             "credit_ledger")}


def test_known_query_replays_exact_view_and_history_once_without_billing(fact_api):
    portal, client, headers, _ = fact_api
    before = counts(portal)
    first = post(client, headers)
    assert first.status_code == 200, first.text
    view = first.json()
    assert view["segments"][0]["text"] == "旧码头的钥匙在钟楼。"
    assert view["body"].endswith("旧码头的钥匙在钟楼。")
    assert view["turn_id"] == "portal-fact-one"
    assert view["suggestions"] == view["action_options"] == []
    assert "billing" not in view
    after = counts(portal)
    assert after["portal_turns"] == before["portal_turns"] + 1
    assert all(after[name] == before[name] for name in (
        "model_execution_audits", "turn_settlement_intents", "credit_ledger"))
    assert post(client, headers).json() == view
    assert counts(portal) == after
    assert len(portal.history(headers["Authorization"].split()[1], "fact-save")["turns"]) == 1


@pytest.mark.parametrize("fact_id", ["missing", "harbor.private", "harbor.unaware", "harbor.expired"])
def test_unknown_classes_share_one_answer(fact_api, fact_id):
    _, client, headers, _ = fact_api
    response = post(client, headers, {**QUERY, "request_id": fact_id.replace(".", "-"),
                                      "fact_id": fact_id})
    assert response.status_code == 200, response.text
    assert response.json()["segments"][0]["text"] == UNKNOWN_FACT_ANSWER


@pytest.mark.parametrize("change", [
    {"request_id": "bad space"}, {"request_id": ""}, {"request_id": "a" * 65},
    {"fact_id": ""}, {"fact_id": "*"}, {"fact_id": "white space"},
    {"speaker_id": "*"}, {"speaker_id": "dock hand"}, {"speaker_id": "x" * 257},
    {"extra": "ignored"},
])
def test_invalid_body_rejected_before_world_write(fact_api, change):
    portal, client, headers, _ = fact_api
    before = counts(portal)
    response = post(client, headers, {**QUERY, **change})
    assert response.status_code in (400, 422), response.text
    assert counts(portal) == before


def test_escaped_max_length_unicode_identifiers_reach_speaker_validation(fact_api):
    portal, client, headers, _ = fact_api
    identifier = "😀" * 256
    body = json.dumps({"request_id": "unicode-boundary", "fact_id": identifier,
                       "speaker_id": identifier}).encode("utf-8")
    assert 4096 < len(body) <= 8192
    before = counts(portal)

    response = client.post("/v1/saves/fact-save/fact-queries", content=body,
                           headers={**headers, "Content-Type": "application/json"})

    assert response.status_code == 400, response.text
    assert response.json()["commit_state"] == "not_started"
    assert "reachable NPC speaker" in response.json()["detail"]
    assert counts(portal) == before


def test_auth_ownership_and_unreachable_speaker_rejected_before_write(fact_api):
    portal, client, headers, other = fact_api
    before = counts(portal)
    assert post(client, {}).status_code == 401
    assert post(client, {"Authorization": f"Bearer {other['token']}"}).status_code == 404
    response = post(client, headers, {**QUERY, "speaker_id": "player"})
    assert response.status_code == 400, response.text
    assert counts(portal) == before


def test_forbidden_paid_and_memory_collaborators_with_zero_balance(fact_api, monkeypatch):
    portal, client, headers, _ = fact_api
    from storyloop_platform.runtime.guidance import GuidanceAdvisor
    from storyloop_platform.portal.sql_billing import SQLBillingRepository

    def forbidden(*_args, **_kwargs):
        raise AssertionError("forbidden collaborator called")

    monkeypatch.setattr(portal.gameplay.factory.models, "require_credentials", forbidden)
    monkeypatch.setattr(portal.gameplay.factory.models, "create_model", forbidden)
    monkeypatch.setattr(portal.gameplay.factory, "generation_settings", forbidden)
    monkeypatch.setattr(portal.memory_service, "preferences_for", forbidden)
    monkeypatch.setattr(portal.memory_service, "queue_input", forbidden)
    monkeypatch.setattr(GuidanceAdvisor, "advise", forbidden)
    monkeypatch.setattr(SQLBillingRepository, "require_credit", forbidden)
    owner_id = portal.accounts.resolve_token(headers["Authorization"].split()[1])
    with portal.accounts.engine.begin() as db:
        db.execute(text("""UPDATE credit_wallets SET balance_milli_points=0
            WHERE player_id=:player_id"""), {"player_id": owner_id})
    before = counts(portal)
    response = post(client, headers)
    assert response.status_code == 200, response.text
    after = counts(portal)
    assert all(after[name] == before[name] for name in (
        "model_execution_audits", "turn_settlement_intents", "credit_ledger"))
    with portal.accounts.engine.connect() as db:
        assert db.execute(text("""SELECT balance_milli_points FROM credit_wallets
            WHERE player_id=:player_id"""), {"player_id": owner_id}).scalar_one() == 0


def test_reopened_api_and_sqlite_replays_identical_response(fact_api):
    portal, client, headers, _ = fact_api
    original = post(client, headers)
    assert original.status_code == 200
    before = counts(portal)
    item = portal.gameplay.game_access.listing_for("unused", "facts")
    reopened = build_portal(item.package_path.parent / "catalog.json",
                            load_settings(ROOT / "config/local.json"), portal.db_path)
    try:
        with TestClient(create_app(reopened), base_url="http://127.0.0.1") as second:
            replay = post(second, headers)
            assert replay.status_code == 200, replay.text
            assert replay.content == original.content
            assert counts(reopened) == before
    finally:
        reopened.close()


def test_campaign_save_rejected_before_write(fact_api):
    portal, client, headers, _ = fact_api
    with portal.accounts.engine.begin() as db:
        db.execute(text("UPDATE player_saves SET play_mode='campaign' WHERE game_id='fact-save'"))
    before = counts(portal)
    response = post(client, headers)
    assert response.status_code == 400, response.text
    assert response.json()["commit_state"] == "not_started"
    assert counts(portal) == before


def test_campaign_state_rejected_before_query_execution(fact_api):
    portal, client, headers, _ = fact_api
    with portal.accounts.engine.begin() as db:
        raw = db.execute(text("SELECT data FROM games WHERE game_id='fact-save'")).scalar_one()
        state = json.loads(raw)
        state["campaign"] = {"day": 1}
        db.execute(text("UPDATE games SET data=:data WHERE game_id='fact-save'"),
                   {"data": json.dumps(state, ensure_ascii=False)})
    before = counts(portal)
    response = post(client, headers)
    assert response.status_code == 400, response.text
    assert response.json()["commit_state"] == "not_started"
    assert counts(portal) == before


@pytest.mark.parametrize("fact_api", ["novel"], indirect=True)
def test_novel_package_rejected_before_write(fact_api):
    portal, client, headers, _ = fact_api
    before = counts(portal)
    response = post(client, headers)
    assert response.status_code == 400, response.text
    assert counts(portal) == before


def test_preview_save_rejected_before_write(fact_api, monkeypatch):
    portal, client, headers, _ = fact_api
    monkeypatch.setattr(portal.moderation, "preview_for", lambda *_args: object())
    before = counts(portal)
    response = post(client, headers)
    assert response.status_code == 400, response.text
    assert counts(portal) == before


def test_history_and_summary_poison_cannot_change_authored_answer(fact_api):
    portal, client, headers, _ = fact_api
    portal.accounts.store_turn_response("fact-save", "poison", "The secret is FAKE",
                                        {"body": "The secret is FAKE"})
    with portal.accounts.engine.begin() as db:
        db.execute(text("""INSERT INTO agent_contexts
            (game_id,actor_id,through_version,summary)
            VALUES ('fact-save','player',0,'The secret is FAKE')"""))
    response = post(client, headers)
    assert response.status_code == 200, response.text
    assert response.json()["segments"][0]["text"] == "旧码头的钥匙在钟楼。"


def test_authored_blueprint_save_rejected_before_query_write(tmp_path):
    import asyncio
    from player_contract_corpus import _packages

    catalog_path = _packages(tmp_path)
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    source = next(item for item in catalog["games"] if item["id"] == "source")
    source["mode"] = "freeform"
    source["play_modes"] = ["freeform"]
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")
    portal = build_portal(catalog_path, load_settings(ROOT / "config/local.json"),
                          str(tmp_path / "blueprint.db"))
    try:
        owner = portal.register("blueprint-owner", "password-123")
        saved = asyncio.run(portal.create_save(owner["token"], "source"))
        with TestClient(create_app(portal), base_url="http://127.0.0.1") as client:
            before = counts(portal)
            result = client.post(f"/v1/saves/{saved['game_id']}/fact-queries",
                                 headers={"Authorization": f"Bearer {owner['token']}"},
                                 json=QUERY)
            assert result.status_code == 400, result.text
            assert result.json()["commit_state"] == "not_started"
            assert counts(portal) == before
    finally:
        portal.close()


def test_stopping_service_rejects_query_before_start(fact_api):
    portal, client, headers, _ = fact_api
    before = counts(portal)
    portal.operations.stop_admission()
    response = post(client, headers)
    assert response.status_code == 503
    assert response.json()["commit_state"] == "not_started"
    assert counts(portal) == before
