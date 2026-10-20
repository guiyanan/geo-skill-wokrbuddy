"""跨层数据结构：Meta / Draft / Receipt / LogEntry。

分层原则（架构 §2）：适配层之上不认识 DOM，引擎层之下不认识「文章」。
中间就靠这几个结构通信，所以它们的职责是**让非法状态构造不出来**：

- 标题为空、slug 带空格 → 构造即报错
- 回执没有 url → 构造即报错（R8：没 verify 过的不算成功）
- meta 里的 content_sha 和正文对不上 → 构造即报错（稿件被改过，重算幂等键）
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import asdict, dataclass, fields
from datetime import datetime
from typing import Any, Mapping

from .state import Status, idem_key as _compute_idem_key

__all__ = [
    "LOG_COLUMNS",
    "Draft",
    "IncompleteLogEntry",
    "LogEntry",
    "Meta",
    "ModelError",
    "Receipt",
]

# R10：列语义不可改。改列 = 历史日志全部失效。
LOG_COLUMNS: tuple[str, ...] = (
    "ts",
    "platform",
    "account",
    "slug",
    "idem_key",
    "status",
    "url",
    "article_id",
    "screenshot",
    "indexed_at",
    "note",
)

_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


class ModelError(ValueError):
    """数据模型层的所有非法构造。"""


class IncompleteLogEntry(ModelError):
    """日志行字段不全/多字段 —— 一行都不许写进去。"""


def _require_text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ModelError(f"{field} 不能为空")
    return value.strip()


def _require_iso(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime):
        raise ModelError(f"{field} 必须是 datetime，收到 {type(value).__name__}")
    if value.tzinfo is None:
        raise ModelError(f"{field} 必须带时区（日志跨时区排序要用）")
    return value


@dataclass(frozen=True, slots=True)
class Meta:
    """对应稿件目录下的 meta.json（架构 §4）。"""

    slug: str
    title: str
    summary: str = ""
    tags: tuple[str, ...] = ()
    category: str = ""
    cover: str | None = None
    ai_involvement: str = ""
    anchor_query_id: str = ""
    target_reader: str = ""
    platforms: tuple[str, ...] = ("csdn",)
    content_sha: str = ""

    def __post_init__(self) -> None:
        if not _SLUG_RE.match(self.slug or ""):
            raise ModelError(f"slug 只能是小写字母/数字/连字符：{self.slug!r}")
        _require_text(self.title, "title")
        object.__setattr__(self, "tags", tuple(self.tags))
        object.__setattr__(self, "platforms", tuple(self.platforms))

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["platforms"] = list(self.platforms)
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Meta:
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ModelError(f"meta.json 未知字段：{', '.join(unknown)}")
        missing = [n for n in ("slug", "title") if n not in data]
        if missing:
            raise ModelError(f"meta.json 缺必填字段：{', '.join(missing)}")

        kwargs = dict(data)
        for key in ("tags", "platforms"):
            if key in kwargs:
                kwargs[key] = tuple(kwargs[key])
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class Draft:
    """一篇待发布的稿件：元信息 + 正文。

    content_sha 由正文算出，不信任外部传入 —— 幂等键的可靠性全押在它身上。
    """

    meta: Meta
    body: str

    def __post_init__(self) -> None:
        declared = (self.meta.content_sha or "").strip().lower()
        if declared and declared != self.content_sha:
            raise ModelError(
                f"meta.content_sha 与正文不一致（{declared} != {self.content_sha}）："
                "稿件在落盘后被改过，请重算"
            )

    @property
    def content_sha(self) -> str:
        return hashlib.sha256(self.body.encode("utf-8")).hexdigest()

    def idem_key(self, platform: str, account: str) -> str:
        return _compute_idem_key(platform, account, self.meta.slug, self.content_sha)


@dataclass(frozen=True, slots=True)
class Receipt:
    """发布回执。只有 verify 通过才允许构造出来。"""

    url: str
    article_id: str
    published_at: datetime
    screenshot: str
    content_sha: str
    indexed_at: datetime | None = None
    status: Status = Status.PUBLISHED

    def __post_init__(self) -> None:
        _require_text(self.url, "url")
        _require_text(self.article_id, "article_id")
        _require_iso(self.published_at, "published_at")
        if self.indexed_at is not None:
            _require_iso(self.indexed_at, "indexed_at")
        if Status(self.status) is not Status.PUBLISHED:
            raise ModelError(f"回执只对应 published，收到 {self.status}")

    def to_json(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "article_id": self.article_id,
            "published_at": self.published_at.isoformat(),
            "screenshot": self.screenshot,
            "content_sha": self.content_sha,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "status": Status(self.status).value,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Receipt:
        return cls(
            url=data["url"],
            article_id=data["article_id"],
            published_at=datetime.fromisoformat(data["published_at"]),
            screenshot=data.get("screenshot", ""),
            content_sha=data.get("content_sha", ""),
            indexed_at=(
                datetime.fromisoformat(data["indexed_at"]) if data.get("indexed_at") else None
            ),
            status=Status(data.get("status", Status.PUBLISHED.value)),
        )


@dataclass(frozen=True, slots=True)
class LogEntry:
    """publish-log.csv 的一行。"""

    ts: datetime
    platform: str
    account: str
    slug: str
    idem_key: str
    status: Status
    url: str = ""
    article_id: str = ""
    screenshot: str = ""
    indexed_at: datetime | None = None
    note: str = ""

    def __post_init__(self) -> None:
        _require_iso(self.ts, "ts")
        for field in ("platform", "account", "slug", "idem_key"):
            _require_text(getattr(self, field), field)
        try:
            coerced = Status(self.status)
        except ValueError as exc:
            raise IncompleteLogEntry(f"status 值非法：{self.status!r}") from exc
        object.__setattr__(self, "status", coerced)
        if self.indexed_at is not None:
            _require_iso(self.indexed_at, "indexed_at")

    def to_row(self) -> dict[str, str]:
        return {
            "ts": self.ts.isoformat(),
            "platform": self.platform,
            "account": self.account,
            "slug": self.slug,
            "idem_key": self.idem_key,
            "status": Status(self.status).value,
            "url": self.url or "",
            "article_id": self.article_id or "",
            "screenshot": self.screenshot or "",
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else "",
            "note": self.note or "",
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> LogEntry:
        missing = [c for c in LOG_COLUMNS if c not in row]
        extra = sorted(k for k in row if k not in LOG_COLUMNS)
        if missing or extra:
            problems = []
            if missing:
                problems.append("缺字段：" + ", ".join(missing))
            if extra:
                problems.append("未知字段：" + ", ".join(extra))
            raise IncompleteLogEntry("；".join(problems))

        raw_ts = str(row["ts"]).strip()
        if not raw_ts:
            raise IncompleteLogEntry("缺字段：ts")
        indexed = str(row.get("indexed_at") or "").strip()
        try:
            status = Status(str(row["status"]).strip())
        except ValueError as exc:
            raise IncompleteLogEntry(f"status 值非法：{row['status']!r}") from exc

        return cls(
            ts=datetime.fromisoformat(raw_ts),
            platform=row["platform"],
            account=row["account"],
            slug=row["slug"],
            idem_key=row["idem_key"],
            status=status,
            url=row.get("url", ""),
            article_id=row.get("article_id", ""),
            screenshot=row.get("screenshot", ""),
            indexed_at=datetime.fromisoformat(indexed) if indexed else None,
            note=row.get("note", ""),
        )
