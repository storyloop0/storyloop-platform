"""A saved scene survives a real interpreter exit without another model POST."""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import create_engine, text


ROOT = Path(__file__).resolve().parents[1]
CHILD = Path(__file__).with_name("player_recovery_process.py")


def child(tmp_path: Path, mode: str, *options: str) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT / "src")
    env["PYTHONUTF8"] = "1"
    return subprocess.run(
        [sys.executable, str(CHILD), mode, "--db", str(tmp_path / "world.sqlite3"),
         "--state", str(tmp_path / "state.json"), "--posts", str(tmp_path / "posts.log"),
         *options], cwd=ROOT, env=env, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
        timeout=60, check=False,
    )


def rows(db_path: Path, table: str, where: str) -> list[dict]:
    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    try:
        with engine.connect() as connection:
            return [dict(row) for row in connection.execute(
                text(f"SELECT * FROM {table} WHERE {where}")).mappings()]
    finally:
        engine.dispose()


@pytest.mark.parametrize("boundary", ["after_snapshot", "after_input", "after_timed_cue"])
def test_native_scene_recovery_after_hard_process_exit(tmp_path: Path, boundary: str):
    seeded = child(tmp_path, "seed")
    assert seeded.returncode == 0, seeded.stderr
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    crashed = child(tmp_path, "crash", "--boundary", boundary)
    assert crashed.returncode == 73, crashed.stderr
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]

    inspected = child(tmp_path, "status")
    assert inspected.returncode == 0, inspected.stderr
    assert json.loads(inspected.stdout)["status"] == {
        "request_id": "process-recovery", "status": "recoverable",
        "can_recover": True, "reason_code": "SCENE_READY",
    }
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]

    recovered = child(tmp_path, "resume", "--price", "99")
    assert recovered.returncode == 0, recovered.stderr
    payload = json.loads(recovered.stdout)
    assert payload["view"]["game_id"] == state["game_id"]
    assert "The morning boat is due soon." in json.dumps(payload["view"])
    assert "TIMED CUE RETAINED" in json.dumps(payload["view"])
    assert "OLD OBSERVATION EXCLUDED" not in json.dumps(payload["view"])
    assert [segment["text"] for segment in payload["view"]["segments"]] == [
        "The dockhand lowers the rope and turns toward me.",
        "The morning boat is due soon.", "TIMED CUE RETAINED",
    ]
    assert payload["view"]["billing"]["charged_milli_points"] == 190
    assert payload["view"]["billing"]["input_tokens"] == 1000
    assert payload["view"]["billing"]["output_tokens"] == 500
    assert payload["queued_inputs"] == 0
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]

    again = child(tmp_path, "resume", "--price", "99")
    assert again.returncode == 0, again.stderr
    assert json.loads(again.stdout)["view"] == payload["view"]
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]

    db = tmp_path / "world.sqlite3"
    events = rows(db, "events", f"game_id = '{state['game_id']}'")
    event_ids = [row["event_id"] for row in events]
    assert event_ids.count("portal-process-recovery:input") == 1
    assert len([event for event in event_ids
                if event.startswith("portal-process-recovery:input:reply:")
                and event.endswith(":spoken")]) == 1
    assert event_ids.count("morning-bell:occurred") == 1
    work = rows(db, "pending_work", f"game_id = '{state['game_id']}'")
    assert {item["status"] for item in work if item["work_id"] in {
        "morning-bell", "portal-process-recovery:input:reply:dockhand"}} == {"consumed"}
    assert len(rows(db, "turn_settlement_intents", f"game_id = '{state['game_id']}'")) == 1
    assert len(rows(db, "portal_turns", f"game_id = '{state['game_id']}'")) == 1
    charges = rows(db, "credit_ledger",
                   f"game_id = '{state['game_id']}' AND kind = 'turn'")
    assert len(charges) == 1
    assert "original-price" in charges[0]["pricing_version"]
    assert json.loads(charges[0]["usage_json"]) == [{
        "model": "offline-scene", "task": "single_turn",
        "input_tokens": 1000, "output_tokens": 500, "cached_input_tokens": 200,
    }]
    audits = rows(db, "model_execution_audits", f"game_id = '{state['game_id']}'")
    assert sorted(audit["outcome"] for audit in audits) == ["running", "succeeded"]
    recovered_audit = next(audit for audit in audits if audit["outcome"] == "succeeded")
    assert json.loads(recovered_audit["attempts_json"]) == []
    assert json.loads(recovered_audit["summary_json"])["known_usage"] == []
    assert len([row for row in payload["ledger"] if row["kind"] == "turn"]) == 1


@pytest.mark.parametrize("fault", ["npc_work", "guidance", "view", "prepare",
                                   "settle_after_commit"])
def test_exception_fault_recovers_once_in_new_process(tmp_path: Path, fault: str):
    seeded = child(tmp_path, "seed")
    assert seeded.returncode == 0, seeded.stderr

    interrupted = child(tmp_path, "crash", "--fault", fault)
    assert interrupted.returncode != 0
    assert f"offline {fault} interruption" in interrupted.stderr
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]

    recovered = child(tmp_path, "resume", "--price", "99")
    assert recovered.returncode == 0, recovered.stderr
    result = json.loads(recovered.stdout)
    assert result["view"]["billing"]["charged_milli_points"] == 190
    assert "TIMED CUE RETAINED" in json.dumps(result["view"])
    assert "OLD OBSERVATION EXCLUDED" not in json.dumps(result["view"])
    assert result["queued_inputs"] == 0
    assert len([row for row in result["ledger"] if row["kind"] == "turn"]) == 1
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]

    replay = child(tmp_path, "resume", "--price", "99")
    assert replay.returncode == 0, replay.stderr
    assert json.loads(replay.stdout)["view"] == result["view"]


@pytest.mark.parametrize("change", ["disable", "correct"])
def test_recovered_input_is_not_requeued_after_memory_change(tmp_path: Path, change: str):
    seeded = child(tmp_path, "seed")
    assert seeded.returncode == 0, seeded.stderr
    crashed = child(tmp_path, "crash", "--boundary", "after_input")
    assert crashed.returncode == 73, crashed.stderr

    changed = child(tmp_path, "memory_change", "--memory-change", change)
    assert changed.returncode == 0, changed.stderr
    assert json.loads(changed.stdout)["queued_inputs"] == 0

    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["queued_inputs"] == 0
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]


def test_unknown_scene_usage_keeps_scene_and_blocks_manual_paid_recovery(tmp_path: Path):
    seeded = child(tmp_path, "seed")
    assert seeded.returncode == 0, seeded.stderr
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))

    crashed = child(tmp_path, "crash", "--boundary", "after_snapshot",
                    "--saved-usage-missing")
    assert crashed.returncode == 73, crashed.stderr
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]

    status = child(tmp_path, "status")
    assert status.returncode == 0, status.stderr
    result = json.loads(status.stdout)
    assert result["status"] == {
        "request_id": "process-recovery", "status": "blocked",
        "can_recover": False, "reason_code": "USAGE_UNAVAILABLE",
    }
    assert not [row for row in result["ledger"] if row["kind"] == "turn"]
    snapshot_rows = rows(tmp_path / "world.sqlite3", "generated_scene_snapshots",
                         f"game_id = '{state['game_id']}'")
    assert len(snapshot_rows) == 1
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]


def test_original_paid_terms_survive_runtime_billing_disabled(tmp_path: Path):
    seeded = child(tmp_path, "seed")
    assert seeded.returncode == 0, seeded.stderr
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    crashed = child(tmp_path, "crash", "--boundary", "after_snapshot")
    assert crashed.returncode == 73, crashed.stderr

    recovered = child(tmp_path, "resume", "--no-billing", "--price", "99")
    assert recovered.returncode == 0, recovered.stderr
    assert json.loads(recovered.stdout)["view"]["billing"]["charged_milli_points"] == 190
    ledger = rows(tmp_path / "world.sqlite3", "credit_ledger",
                  f"game_id = '{state['game_id']}' AND kind = 'turn'")
    assert len(ledger) == 1
    assert ledger[0]["usage_cost_milli_points"] == 190
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]


def test_original_unbilled_turn_stays_unbilled_after_billing_enabled(tmp_path: Path):
    seeded = child(tmp_path, "seed", "--no-billing")
    assert seeded.returncode == 0, seeded.stderr
    state = json.loads((tmp_path / "state.json").read_text(encoding="utf-8"))
    crashed = child(tmp_path, "crash", "--boundary", "after_snapshot",
                    "--no-billing")
    assert crashed.returncode == 73, crashed.stderr

    recovered = child(tmp_path, "resume")
    assert recovered.returncode == 0, recovered.stderr
    assert "billing" not in json.loads(recovered.stdout)["view"]
    assert not rows(tmp_path / "world.sqlite3", "credit_ledger",
                    f"game_id = '{state['game_id']}' AND kind = 'turn'")
    assert (tmp_path / "posts.log").read_bytes().splitlines() == [b"POST"]
