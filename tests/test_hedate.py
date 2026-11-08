from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from jarvis.hedate import TZ, parse, say

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 10, 11, 10, 0, tzinfo=TZ)  # Sunday 10:00


def at(y, mo, d, h, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


@pytest.mark.parametrize("text,expected", [
    ("מחר בשלוש", at(2026, 10, 12, 15)),
    ("תזכיר לי מחר ב3", at(2026, 10, 12, 15)),
    ("מחר בשמונה בבוקר", at(2026, 10, 12, 8)),
    ("מחר בשמונה בערב", at(2026, 10, 12, 20)),
    ("יום חמישי הבא בערב", at(2026, 10, 15, 19)),
    ("ביום חמישי בשבע וחצי בערב", at(2026, 10, 15, 19, 30)),
    ("בעוד שעתיים", at(2026, 10, 11, 12)),
    ("בעוד חצי שעה", at(2026, 10, 11, 10, 30)),
    ("בעוד 20 דקות", at(2026, 10, 11, 10, 20)),
    ("עוד יומיים", at(2026, 10, 13, 10)),
    ("ב-15:30", at(2026, 10, 11, 15, 30)),
    ("בשעה 9:15", at(2026, 10, 12, 9, 15)),  # already passed today → tomorrow
    ("מחרתיים בצהריים", at(2026, 10, 13, 12)),
    ("ב12.10 בעשר", at(2026, 10, 12, 10)),
    ("ביום ראשון", at(2026, 10, 18, 9)),  # said on Sunday → next Sunday
    ("היום בערב", at(2026, 10, 11, 19)),
    ("רבע לחמש", at(2026, 10, 11, 16, 45)),
    ("מחר בבוקר", at(2026, 10, 12, 9)),
])
def test_parse(text, expected):
    assert parse(text, NOW) == expected


def test_no_time_expression():
    assert parse("תקנה חלב", NOW) is None
    assert parse("יש לי 3 תרגילים", NOW) is None


def test_relative_across_dst_end():
    # Israel DST ends Sun 25.10.2026 02:00 → 01:00. 4 real hours after 22:00 IDT = 01:00 IST.
    now = at(2026, 10, 24, 22)
    got = parse("בעוד 4 שעות", now)
    assert got.astimezone(UTC) == datetime(2026, 10, 24, 23, 0, tzinfo=UTC)
    assert (got.hour, got.utcoffset().total_seconds()) == (1, 7200)


def test_wallclock_across_dst_end():
    got = parse("מחר בשלוש", at(2026, 10, 24, 22))
    assert (got.day, got.hour) == (25, 15)
    assert got.astimezone(UTC).hour == 13  # IST = UTC+2


def test_spring_forward_gap_is_valid_instant():
    # DST starts Fri 26.3.2027 02:00 → 03:00; 02:30 does not exist locally.
    got = parse("מחר ב2:30 בלילה", at(2027, 3, 25, 20))
    assert got.astimezone(UTC).astimezone(TZ) == got
    assert got.hour == 3


def test_say():
    assert say(at(2026, 10, 12, 15), NOW) == "מחר, יום שני 12.10 בשעה 15:00"
