"""TEST-3 리셋 시각 형식. 실사용에서 본 형식은 'resets 2:50pm (Asia/Seoul)' 하나뿐이다.
날짜가 붙은 형식(주간 한도)은 실제 메시지 샘플을 아직 못 받아, 정규식이 허용하는 모양으로만 고정한다."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import KST, kst, load_module

ulr = load_module()
NY = ZoneInfo("America/New_York")


@pytest.mark.parametrize("message, blocked_at, expected", [
    ("You've hit your session limit · resets 2:50pm (Asia/Seoul)", kst(13, 21), kst(14, 50)),
    ("resets 6pm (Asia/Seoul)", kst(13, 21), kst(18)),
    ("resets 2:50 PM (Asia/Seoul)", kst(13, 21), kst(14, 50)),
    ("resets 12pm (Asia/Seoul)", kst(9), kst(12)),
    # 자정 넘김: 오늘 시각이 이미 지났으면 다음 날
    ("resets 12:30am (Asia/Seoul)", kst(23, 30), kst(0, 30, day=20)),
    ("resets 1pm (Asia/Seoul)", kst(13, 0), kst(13, 0, day=20)),
    # 날짜가 붙은 형식
    ("resets Oct 22, 9am (Asia/Seoul)", kst(13), kst(9, day=22)),
    ("resets Jan 2, 2027, 9am (Asia/Seoul)", kst(13), kst(9, day=2, month=1, year=2027)),
    ("resets Jan 2, 9am (Asia/Seoul)", kst(13, day=30, month=12), kst(9, day=2, month=1, year=2027)),
])
def test_formats(message, blocked_at, expected):
    assert ulr.parse_reset(message, blocked_at) == expected


def test_timezone_missing_uses_blocked_at_zone():
    assert ulr.parse_reset("resets 2:50pm", kst(13, 21)) == kst(14, 50)


def test_other_timezone_is_respected():
    got = ulr.parse_reset("resets 3pm (America/New_York)", kst(13, 21))
    assert got == datetime(2026, 10, 19, 15, tzinfo=NY)


def test_unknown_timezone_falls_back_to_blocked_at_zone():
    assert ulr.parse_reset("resets 2:50pm (Mars/Base)", kst(13, 21)) == kst(14, 50)


def test_dst_spring_forward():
    # 2026-03-08 02:00 EST → 03:00 EDT. 01:30 EST 에 막히고 3:30am 리셋이면 1시간 뒤다
    blocked = datetime(2026, 3, 8, 1, 30, tzinfo=NY)
    got = ulr.parse_reset("resets 3:30am (America/New_York)", blocked)
    assert got == datetime(2026, 3, 8, 3, 30, tzinfo=NY)
    assert got.timestamp() - blocked.timestamp() == timedelta(hours=1).total_seconds()


def test_unparsable_returns_none():
    assert ulr.parse_reset("You've hit your session limit", kst(13)) is None
    assert ulr.parse_reset("", kst(13)) is None
