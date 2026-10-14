"""UTC freshness and immutable provider snapshots."""

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from storyloop_platform.memory.policy import (
    FreshnessResult, MemoryRecord, classify, select_preferences, snapshot_record,
)


NOW = datetime(2026, 10, 14, tzinfo=timezone.utc)
OLD = MemoryRecord("m", "slow", "2026-07-16T00:00:00Z", NOW.isoformat(), "story_turn_batch")


@pytest.mark.parametrize("created,expected", [
    ("2026-07-16T00:00:01Z", "current"),
    ("2026-07-16T00:00:00Z", "expired"),
    ("2026-07-16T08:00:00+08:00", "expired"),
])
def test_utc_cutoff(created, expected):
    assert classify(replace(OLD, created_at=created), 90, NOW) == FreshnessResult(
        expected, "2026-10-14T00:00:00Z" if expected == "expired" else "2026-10-14T00:00:01Z")


@pytest.mark.parametrize("source,expected", [
    ("user_correction", FreshnessResult("current", None)),
    ("story_turn_batch", FreshnessResult("expired", "2026-10-14T00:00:00Z")),
    ("", FreshnessResult("expired", "2026-10-14T00:00:00Z")),
    ("other", FreshnessResult("expired", "2026-10-14T00:00:00Z")),
])
def test_only_exact_correction_is_permanent(source, expected):
    assert classify(replace(OLD, source=source), 90, NOW) == expected


@pytest.mark.parametrize("created", ["", "bad", "2026-07-01T00:00:00", "2026-10-15T00:00:00Z"])
def test_unknown_dates_are_excluded_unless_disabled(created):
    record = replace(OLD, created_at=created)
    assert classify(record, 90, NOW) == FreshnessResult("unknown", None)
    assert classify(record, None, NOW) == FreshnessResult("current", None)


def test_updated_at_does_not_renew_and_disabled_age_keeps_old():
    assert classify(OLD, 90, NOW) == FreshnessResult("expired", "2026-10-14T00:00:00Z")
    assert classify(OLD, None, NOW) == FreshnessResult("current", None)


def test_filter_before_eight_among_bounded_candidates():
    rows = tuple(replace(OLD, id=str(i), text="x" * 181 if i == 12 else str(i),
                         created_at="2026-10-01T00:00:00Z" if i >= 12 else OLD.created_at)
                 for i in range(20))
    assert select_preferences(rows, 90, NOW) == ("x" * 180, "13", "14", "15", "16", "17", "18", "19")


def test_snapshot_is_independent_of_mutable_provider_row():
    row = dict(id="m", text="slow", created_at=OLD.created_at, updated_at="", source="story_turn_batch")
    snap = snapshot_record(row)
    row.update(text="fast", created_at="bad", source="user_correction")
    assert snap == MemoryRecord("m", "slow", OLD.created_at, "", "story_turn_batch")
