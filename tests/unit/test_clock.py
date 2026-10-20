"""可注入时钟（UT-01-03 的补齐）。

T-01 当时只落了骨架，clock 一直缺；T-02 的日志时间戳依赖它，所以一并补上。
"""

from datetime import datetime

import pytest

from geopublish.core.clock import FakeClock, SystemClock

pytestmark = pytest.mark.unit


def test_fake_clock_does_not_read_system_time():
    clock = FakeClock("2026-10-21T10:00:00+08:00")

    assert clock.now() == datetime.fromisoformat("2026-10-21T10:00:00+08:00")


def test_fake_clock_advance():
    clock = FakeClock("2026-10-21T10:00:00+08:00")

    clock.advance(90)

    assert clock.now().isoformat() == "2026-10-21T10:01:30+08:00"


def test_fake_clock_set():
    clock = FakeClock("2026-10-21T10:00:00+08:00")

    clock.set("2026-10-25T09:00:00+08:00")

    assert clock.now().day == 25


def test_clock_requires_timezone():
    with pytest.raises(ValueError):
        FakeClock("2026-10-21T10:00:00")


def test_system_clock_is_aware():
    assert SystemClock().now().tzinfo is not None
