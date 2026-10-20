"""可注入时间。

确定性三约束之一：**所有 now() 走这里**，测试用 FakeClock，代码里禁止直接
datetime.now()。否则日志时间戳、日报日期、调度窗口全都不稳定。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:  # pragma: no cover - Protocol 只声明
        ...


class SystemClock:
    """生产用：UTC，带时区。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class FakeClock:
    """测试用：从固定时刻开始，手动推进。"""

    def __init__(self, iso: str) -> None:
        self._now = _parse(iso)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> datetime:
        self._now = self._now + timedelta(seconds=seconds)
        return self._now

    def set(self, iso: str) -> datetime:
        self._now = _parse(iso)
        return self._now


def _parse(iso: str) -> datetime:
    value = datetime.fromisoformat(iso)
    if value.tzinfo is None:
        raise ValueError(f"时间必须带时区（如 +08:00）：{iso}")
    return value
