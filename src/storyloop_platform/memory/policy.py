"""Pure UTC freshness policy for player preference records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Literal, Mapping


Freshness = Literal["current", "expired", "unknown"]


@dataclass(frozen=True, slots=True)
class MemoryRecord:
    id: str
    text: str
    created_at: str
    updated_at: str
    source: str


@dataclass(frozen=True, slots=True)
class FreshnessResult:
    freshness: Freshness
    expires_at: str | None


def snapshot_record(row: Mapping[str, object]) -> MemoryRecord:
    return MemoryRecord(*(str(row.get(key) or "") for key in
                          ("id", "text", "created_at", "updated_at", "source")))


def classify(record: MemoryRecord, age_days: int | None, now: datetime) -> FreshnessResult:
    if age_days is None or record.source == "user_correction":
        return FreshnessResult("current", None)
    try:
        created = datetime.fromisoformat(record.created_at.replace("Z", "+00:00"))
        if created.tzinfo is None or created.utcoffset() is None:
            return FreshnessResult("unknown", None)
        created = created.astimezone(timezone.utc)
        if created > now:
            return FreshnessResult("unknown", None)
        cutoff = created + timedelta(days=age_days)
    except (ValueError, OverflowError):
        return FreshnessResult("unknown", None)
    return FreshnessResult(
        "expired" if now >= cutoff else "current",
        cutoff.isoformat().replace("+00:00", "Z"),
    )


def select_preferences(records: tuple[MemoryRecord, ...], age_days: int | None,
                       now: datetime) -> tuple[str, ...]:
    selected: list[str] = []
    for record in records:
        if classify(record, age_days, now).freshness == "current":
            selected.append(record.text[:180])
            if len(selected) == 8:
                break
    return tuple(selected)
