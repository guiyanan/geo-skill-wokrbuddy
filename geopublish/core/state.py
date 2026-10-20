"""发布状态机 + 幂等键 + 发布日志（PRD R9 的落地核心）。

两条铁律，改代码时先读这两行：

1. **published 是终态**，任何方向都转不出去 —— 防重发。
2. **submitting 遇到没有 URL 的恢复请求 → needs_human，绝不回 pending** ——
   进程可能在“点发布那一瞬间”被杀，回 pending 就等于自动补发第二篇。

日志写入要求整行成功或整行不写：先校验、再拼行、最后一次 write + fsync。
"""

from __future__ import annotations

import csv
import hashlib
import io
import os
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Mapping

if TYPE_CHECKING:  # models 依赖本模块的 Status，这里只用类型不导入，避免环形依赖
    from .models import LogEntry

__all__ = [
    "ALLOWED_TRANSITIONS",
    "IllegalTransition",
    "PublishLog",
    "StateError",
    "Status",
    "idem_key",
    "recover_status",
    "transition",
]

# 幂等键字段分隔符：用 ASCII Unit Separator，正文里不可能出现，避免拼接歧义
_KEY_SEP = "\x1f"


class StateError(ValueError):
    """状态机/日志层的所有非法操作。"""


class IllegalTransition(StateError):
    def __init__(self, current: Any, target: Any) -> None:
        self.current = current
        self.target = target
        super().__init__(f"非法状态转换：{current} → {target}")


class Status(str, Enum):
    PENDING = "pending"
    SUBMITTING = "submitting"
    PUBLISHED = "published"
    FAILED = "failed"
    NEEDS_HUMAN = "needs_human"

    def __str__(self) -> str:  # 日志和报错里直接看到 pending，而不是 Status.PENDING
        return self.value

    @property
    def is_terminal(self) -> bool:
        return self is Status.PUBLISHED


ALLOWED_TRANSITIONS: dict[Status, frozenset[Status]] = {
    Status.PENDING: frozenset({Status.SUBMITTING, Status.FAILED, Status.NEEDS_HUMAN}),
    Status.SUBMITTING: frozenset({Status.PUBLISHED, Status.FAILED, Status.NEEDS_HUMAN}),
    # failed 可以重试（次数由 T-11 控制），也可以直接转人工
    Status.FAILED: frozenset({Status.SUBMITTING, Status.PENDING, Status.NEEDS_HUMAN}),
    # 人工处理完把稿子放回队列，只能回到起点，不能跳到 published
    Status.NEEDS_HUMAN: frozenset({Status.PENDING}),
    Status.PUBLISHED: frozenset(),
}


def _coerce(value: Any) -> Status:
    if isinstance(value, Status):
        return value
    try:
        return Status(str(value).strip().lower())
    except ValueError as exc:
        raise IllegalTransition(value, "<未定义状态>") from exc


def transition(current: Any, target: Any) -> Status:
    """校验并返回目标状态。同态视为幂等空操作。"""
    cur = _coerce(current)
    dst = _coerce(target)
    if dst is cur:
        return dst
    if dst not in ALLOWED_TRANSITIONS[cur]:
        raise IllegalTransition(cur, dst)
    return dst


def idem_key(platform: str, account: str, slug: str, content_sha: str) -> str:
    """幂等键 = sha256(platform | account | slug | content_sha)。

    大小写与首尾空白先归一化：CSDN 账号大小写不一致时不能让同一篇发两遍。
    """
    parts = [
        _norm(platform, "platform"),
        _norm(account, "account"),
        _norm(slug, "slug"),
        _norm(content_sha, "content_sha"),
    ]
    return hashlib.sha256(_KEY_SEP.join(parts).encode("utf-8")).hexdigest()


def _norm(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"幂等键字段 {field} 不能为空")
    return value.strip().lower()


def recover_status(status: Any, url: str | None) -> Status:
    """崩溃恢复：上次停在某个状态，现在该认成什么。

    submitting + 没有 URL = 提交结果未知 → needs_human（交给人工去平台后台看一眼）。
    submitting + 有 URL   = 其实已经发出去了 → published（不许再发第二遍）。
    """
    cur = _coerce(status)
    if cur is Status.SUBMITTING:
        return Status.PUBLISHED if (url or "").strip() else Status.NEEDS_HUMAN
    return cur


class PublishLog:
    """append-only 的 publish-log.csv。

    不做原地修改：每次状态变化追加一行，最新一行是当前状态。
    并发加锁在 T-11 落地，这里只保证单次写入是原子的（整行或零字节）。
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(self, entry: LogEntry) -> None:
        self._write_row(entry.to_row())

    def append_row(self, row: Mapping[str, Any]) -> None:
        """外部（CLI/编排层）拿 dict 来写时的入口：先校验，再落盘。"""
        from .models import LogEntry

        self._write_row(LogEntry.from_row(row).to_row())

    def _write_row(self, row: Mapping[str, str]) -> None:
        from .models import LOG_COLUMNS

        buf = io.StringIO()
        csv.DictWriter(buf, fieldnames=list(LOG_COLUMNS), lineterminator="\n").writerow(row)
        payload = buf.getvalue()

        self.path.parent.mkdir(parents=True, exist_ok=True)
        needs_header = not self.path.exists() or self.path.stat().st_size == 0
        header = ",".join(LOG_COLUMNS) + "\n" if needs_header else ""

        with open(self.path, "a", encoding="utf-8", newline="") as fh:
            fh.write(header + payload)  # 一次写，不留半行
            fh.flush()
            os.fsync(fh.fileno())

    def entries(self) -> list[LogEntry]:
        from .models import LogEntry

        if not self.path.exists():
            return []
        with open(self.path, encoding="utf-8", newline="") as fh:
            return [LogEntry.from_row(row) for row in csv.DictReader(fh)]

    def last_status(self, idem_key: str) -> Status | None:
        status: Status | None = None
        for entry in self.entries():
            if entry.idem_key == idem_key:
                status = entry.status
        return status

    def statuses(self, idem_key: str) -> Iterable[Status]:
        return [e.status for e in self.entries() if e.idem_key == idem_key]
