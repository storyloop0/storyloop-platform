import json

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from storyloop_harness.usage import ModelAttempt, ModelUsage, summarize_attempt_usage
from storyloop_platform.adapters.sql_database import open_database, sqlite_url, upgrade_database
from storyloop_platform.portal.attempt_costs import estimate_attempt_costs
from storyloop_platform.portal.model_execution_audits import SQLModelExecutionAudits
from test_attempt_costs import policy


@pytest.fixture
def repo(tmp_path):
    url = sqlite_url(str(tmp_path / "audit.db"))
    upgrade_database(url)
    engine = open_database(url)
    yield SQLModelExecutionAudits(engine)
    engine.dispose()


def test_running_then_terminal_freezes_pricing_and_replays_identically(repo):
    tariff = policy()
    repo.start(execution_id="e", player_id="p", game_id="g", request_id="r",
               input_hash="h", policy=tariff)
    running = repo.list_for_request("g", "r")[0]
    assert running["outcome"] == "running"
    assert running["attempts"] is None and running["summary"] is None
    usage = ModelUsage("m", "task", 1000, 500)
    summary = summarize_attempt_usage([ModelAttempt("i", 1, "m", "task", "succeeded", usage,
                                                     "reported", True, None)])
    repo.finish("e", summary, outcome="succeeded", error_type=None)
    repo.finish("e", summary, outcome="succeeded", error_type=None)
    final = repo.list_for_request("g", "r")[0]
    assert final["attempts"] == summary.to_dict()["attempts"]
    assert final["summary"]["total_cost_milli_points"] == 600
    assert final["policy"] == tariff.to_dict()
    assert final["input_hash"] == "h"
    with pytest.raises(ValueError):
        repo.finish("e", summary, outcome="failed", error_type="RuntimeError")


def test_retry_rows_and_corrupt_read_are_explicit(repo):
    for execution in ("a", "b"):
        repo.start(execution_id=execution, player_id="p", game_id="g", request_id="r",
                   input_hash="h", policy=None)
    assert {r["execution_id"] for r in repo.list_for_request("g", "r")} == {"a", "b"}
    with repo.engine.begin() as db:
        db.execute(text("UPDATE model_execution_audits SET policy_json='broken' WHERE execution_id='a'"))
    with pytest.raises(ValueError, match="^invalid model execution audit$"):
        repo.list_for_request("g", "r")


def test_upgrade_preserves_old_wallet_and_save(tmp_path):
    url = sqlite_url(str(tmp_path / "old.db"))
    config = Config()
    config.set_main_option("script_location", str(__import__("pathlib").Path(__file__).resolve().parents[1]
                                                   / "src/storyloop_platform/migrations"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "0015_checkpoint_provenance")
    engine = open_database(url)
    with engine.begin() as db:
        db.execute(text("INSERT INTO credit_wallets VALUES ('p',123456)"))
        db.execute(text("""INSERT INTO player_saves
            (game_id,player_id,catalog_id,package_id,package_version,package_hash,created_at)
            VALUES ('g','p','c','pkg','v','hash',1)"""))
    engine.dispose()
    upgrade_database(url)
    engine = open_database(url)
    try:
        with engine.connect() as db:
            assert db.execute(text("SELECT balance_milli_points FROM credit_wallets WHERE player_id='p'")).scalar_one() == 123456
            assert db.execute(text("SELECT package_hash FROM player_saves WHERE game_id='g'")).scalar_one() == "hash"
        assert SQLModelExecutionAudits(engine).list_for_request("g", "r") == []
    finally:
        engine.dispose()


def test_frozen_price_and_corrupt_terminal_summary(repo):
    tariff = policy()
    repo.start(execution_id="f", player_id="p", game_id="g", request_id="r",
               input_hash="h", policy=tariff)
    tariff.rates["m"] = tariff.rates["m"].__class__(100, 100, 100)
    usage = ModelUsage("m", "task", 1000, 500)
    summary = summarize_attempt_usage([ModelAttempt("i", 1, "m", "task", "succeeded",
                                                     usage, "reported", True, None)])
    repo.finish("f", summary, outcome="succeeded", error_type=None)
    assert repo.list_for_request("g", "r")[0]["summary"]["total_cost_milli_points"] == 600
    with repo.engine.begin() as db:
        db.execute(text("UPDATE model_execution_audits SET summary_json='{}' WHERE execution_id='f'"))
    with pytest.raises(ValueError, match="^invalid model execution audit$"):
        repo.list_for_request("g", "r")
