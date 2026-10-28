# SPDX-License-Identifier: Apache-2.0
# Copyright 2026 Jordan Hosler
from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

import pytest

from steward.core.scheduler import effective_schedules, next_run, parse_when
from steward.sdk import Daily, Every, Schedule

UTC_TZ = ZoneInfo("UTC")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("every 30m", Every(timedelta(minutes=30))),
        ("every 6h", Every(timedelta(hours=6))),
        ("Every 1d", Every(timedelta(days=1))),
        ("daily 03:00", Daily(time(3, 0))),
        ("daily 23:59", Daily(time(23, 59))),
        ("off", None),
    ],
)
def test_parse_when(text: str, expected: object) -> None:
    assert parse_when(text) == expected


@pytest.mark.parametrize("text", ["hourly", "every 5s", "daily 24:00", "every h", ""])
def test_parse_when_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError, match="must be"):
        parse_when(text)


def test_str_round_trips() -> None:
    for text in ("every 30m", "every 6h", "every 2d", "daily 04:05"):
        when = parse_when(text)
        assert when is not None
        assert str(when) == text


def test_next_run_every() -> None:
    after = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    assert next_run(Every(timedelta(hours=6)), after, UTC_TZ) == after + timedelta(hours=6)


def test_next_run_daily_later_today_and_tomorrow() -> None:
    daily = Daily(time(3, 0))
    assert next_run(daily, datetime(2026, 1, 1, 1, 0, tzinfo=UTC), UTC_TZ) == datetime(
        2026, 1, 1, 3, 0, tzinfo=UTC
    )
    assert next_run(daily, datetime(2026, 1, 1, 3, 0, tzinfo=UTC), UTC_TZ) == datetime(
        2026, 1, 2, 3, 0, tzinfo=UTC
    )


def test_next_run_daily_uses_the_configured_timezone() -> None:
    berlin = ZoneInfo("Europe/Berlin")  # UTC+1 in January, UTC+2 in July
    daily = Daily(time(3, 0))
    assert next_run(daily, datetime(2026, 1, 1, 0, 0, tzinfo=UTC), berlin) == datetime(
        2026, 1, 1, 2, 0, tzinfo=UTC
    )
    assert next_run(daily, datetime(2026, 7, 1, 0, 0, tzinfo=UTC), berlin) == datetime(
        2026, 7, 1, 1, 0, tzinfo=UTC
    )


def test_effective_schedules_applies_overrides() -> None:
    registered = {
        "a.one": Schedule(Daily(time(4, 0)), "a.job", {}),
        "a.two": Schedule(Every(timedelta(hours=1)), "a.job", {}),
        "a.three": Schedule(Every(timedelta(hours=1)), "a.job", {}),
    }
    result = effective_schedules(registered, {"a.one": "every 2h", "a.two": "off"})
    assert result == {
        "a.one": Schedule(Every(timedelta(hours=2)), "a.job", {}),
        "a.three": registered["a.three"],
    }


def test_effective_schedules_rejects_unknown_names() -> None:
    with pytest.raises(ValueError, match="unknown schedule"):
        effective_schedules({}, {"a.typo": "off"})
