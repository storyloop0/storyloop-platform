"""Typed player receipts preserve request identity across query and turn APIs."""

import hashlib
import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text

from storyloop_platform.adapters.sql_database import open_database, sqlite_url, upgrade_database
from storyloop_platform.portal.sql_repository import SQLPlayerRepository


def migration_config(url):
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[1] /
                                                  "src/storyloop_platform/migrations"))
    config.set_main_option("sqlalchemy.url", url)
    return config


@pytest.fixture
def players(tmp_path):
    url = sqlite_url(str(tmp_path / "receipts.db"))
    upgrade_database(url)
    engine = open_database(url)
    try:
        yield SQLPlayerRepository(engine)
    finally:
        engine.dispose()


def canonical_text(fact_id="fact-甲", speaker_id="speaker-乙"):
    return json.dumps({"kind": "fact_query", "fact_id": fact_id,
                       "speaker_id": speaker_id}, ensure_ascii=False,
                      sort_keys=True, separators=(",", ":"))


def receipt_row(players, request_id):
    with players.engine.connect() as db:
        return db.execute(text("""SELECT request_kind,input_hash,input_text,response_json
            FROM portal_turns WHERE game_id='game' AND request_id=:request_id"""),
            {"request_id": request_id}).mappings().one()


def test_upgrade_populated_0019_preserves_plain_receipt_bytes_and_defaults(tmp_path):
    url = sqlite_url(str(tmp_path / "migration.db"))
    config = migration_config(url)
    command.upgrade(config, "0019_generated_scene_generation_markers")
    engine = open_database(url)
    old_json = '{"body": "原样保留", "nested": {"a": 1}}'
    old_hash = hashlib.sha256("旧行动".encode()).digest()
    try:
        with engine.begin() as db:
            db.execute(text("""INSERT INTO portal_turns
                (game_id,request_id,input_hash,input_text,response_json,created_order)
                VALUES ('game','old',:input_hash,'旧行动',:response_json,1)"""),
                {"input_hash": old_hash, "response_json": old_json})
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = open_database(url)
    try:
        players = SQLPlayerRepository(engine)
        row = receipt_row(players, "old")
        assert row["request_kind"] == "turn"
        assert row["input_hash"] == old_hash
        assert row["response_json"] == old_json
        assert players.get_turn_response("game", "old", "旧行动") == json.loads(old_json)
        players.store_turn_response("game", "new", "新行动", {"body": "普通"})
        assert receipt_row(players, "new")["request_kind"] == "turn"
        assert players.get_turn_response("game", "new", "新行动") == {"body": "普通"}
        columns = {column["name"]: column for column in inspect(engine).get_columns("portal_turns")}
        assert columns["request_kind"]["nullable"] is False
        assert "turn" in str(columns["request_kind"]["default"])
    finally:
        engine.dispose()


def test_fact_receipt_has_canonical_hash_and_appears_in_history(players):
    response = {"body": "已知", "segments": [{"text": "回答"}]}
    players.store_turn_response("game", "first", "行动", {"body": "先前"})
    players.store_fact_query_response("game", "fact", "fact-甲", "speaker-乙", response)

    row = receipt_row(players, "fact")
    assert row["request_kind"] == "fact_query"
    assert row["input_hash"] == hashlib.sha256(canonical_text().encode("utf-8")).digest()
    assert row["input_text"] == "询问已登记事实"
    assert players.get_fact_query_response("game", "fact", "fact-甲", "speaker-乙") == response
    assert players.list_turns("game") == [
        {"request_id": "first", "input": "行动", "response": {"body": "先前"}},
        {"request_id": "fact", "input": "询问已登记事实", "response": response},
    ]
    assert players.latest_turn_response("game") == response


@pytest.mark.parametrize("fact_id,speaker_id", [
    ("other", "speaker-乙"), ("fact-甲", "other"),
])
def test_fact_read_and_write_reject_changed_identity(players, fact_id, speaker_id):
    response = {"body": "original"}
    players.store_fact_query_response("game", "same", "fact-甲", "speaker-乙", response)
    with pytest.raises(ValueError, match="different input"):
        players.get_fact_query_response("game", "same", fact_id, speaker_id)
    with pytest.raises(ValueError):
        players.store_fact_query_response("game", "same", fact_id, speaker_id, response)
    assert receipt_row(players, "same")["response_json"] == json.dumps(response, ensure_ascii=False)


def test_fact_write_is_idempotent_but_rejects_different_response(players):
    response = {"body": "original"}
    players.store_fact_query_response("game", "same", "fact-甲", "speaker-乙", response)
    first = dict(receipt_row(players, "same"))
    players.store_fact_query_response("game", "same", "fact-甲", "speaker-乙", response)
    assert dict(receipt_row(players, "same")) == first
    with pytest.raises(ValueError, match="different response"):
        players.store_fact_query_response("game", "same", "fact-甲", "speaker-乙",
                                          {"body": "changed"})
    assert dict(receipt_row(players, "same")) == first


def test_ordinary_text_matching_canonical_json_cannot_read_fact(players):
    literal = canonical_text()
    fact_response = {"body": "fact"}
    players.store_fact_query_response("game", "fact", "fact-甲", "speaker-乙", fact_response)
    with pytest.raises(ValueError, match="different input"):
        players.get_turn_response("game", "fact", literal)
    with pytest.raises(ValueError, match="different input"):
        players.get_turn_response_by_hash("game", "fact",
                                          hashlib.sha256(literal.encode()).hexdigest())
    with pytest.raises(ValueError):
        players.store_turn_response("game", "fact", literal, fact_response)

    players.store_turn_response("game", "turn", literal, {"body": "turn"})
    with pytest.raises(ValueError, match="different input"):
        players.get_fact_query_response("game", "turn", "fact-甲", "speaker-乙")
    with pytest.raises(ValueError):
        players.store_fact_query_response("game", "turn", "fact-甲", "speaker-乙",
                                          {"body": "turn"})
    assert players.get_turn_response("game", "turn", literal) == {"body": "turn"}


def test_downgrade_refuses_fact_rows_without_losing_them(tmp_path):
    url = sqlite_url(str(tmp_path / "danger.db"))
    config = migration_config(url)
    upgrade_database(url)
    engine = open_database(url)
    try:
        players = SQLPlayerRepository(engine)
        players.store_fact_query_response("game", "fact", "f", "s", {"body": "answer"})
    finally:
        engine.dispose()
    with pytest.raises(RuntimeError, match="fact_query"):
        command.downgrade(config, "0019_generated_scene_generation_markers")
    engine = open_database(url)
    try:
        players = SQLPlayerRepository(engine)
        assert players.get_fact_query_response("game", "fact", "f", "s") == {"body": "answer"}
        assert receipt_row(players, "fact")["request_kind"] == "fact_query"
    finally:
        engine.dispose()


def test_ordinary_only_downgrade_succeeds(tmp_path):
    url = sqlite_url(str(tmp_path / "safe.db"))
    config = migration_config(url)
    upgrade_database(url)
    engine = open_database(url)
    try:
        players = SQLPlayerRepository(engine)
        players.store_turn_response("game", "turn", "hello", {"body": "safe"})
    finally:
        engine.dispose()
    command.downgrade(config, "0019_generated_scene_generation_markers")
    engine = open_database(url)
    try:
        assert "request_kind" not in {column["name"] for column in
                                      inspect(engine).get_columns("portal_turns")}
        with engine.connect() as db:
            row = db.execute(text("""SELECT input_hash,response_json FROM portal_turns
                WHERE game_id='game' AND request_id='turn'""")).one()
        assert row == (hashlib.sha256(b"hello").digest(), '{"body": "safe"}')
    finally:
        engine.dispose()
