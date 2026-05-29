from datetime import datetime
from zoneinfo import ZoneInfo

from followup import parse_followup_time


def _fixed_now():
    return datetime(2026, 5, 29, 14, 38, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


def test_relative_callback_minutes():
    now = _fixed_now()
    expected = datetime(2026, 5, 29, 15, 8, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert parse_followup_time("in 30 minutes", now=now) == expected
    assert parse_followup_time("after 30 minutes", now=now) == expected
    assert parse_followup_time("call after 30 minutes", now=now) == expected


def test_common_daypart_phrases():
    now = _fixed_now()
    assert parse_followup_time("tomorrow morning", now=now) == datetime(2026, 5, 30, 9, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))
    assert parse_followup_time("today evening", now=now) == datetime(2026, 5, 29, 18, 0, 0, tzinfo=ZoneInfo("Asia/Kolkata"))


if __name__ == "__main__":
    test_relative_callback_minutes()
    test_common_daypart_phrases()
    print("followup time parser smoke ok")
