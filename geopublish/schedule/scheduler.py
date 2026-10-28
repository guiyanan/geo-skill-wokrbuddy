"""T-18 每日调度（PRD R16，两阶段）。

每天回答两个问题：**昨天入队的稿子发出去没有？明天发什么？**

## 两阶段（PRD R18.1：稿子必须提前一天生成，留出人工干预窗口）

```
run_daily 一次运行：
  ① 发布阶段：各平台到点（now ≥ publish_at）→ 发队列里 queued_at < 今天 的稿
  ② prepare 阶段：选题（T-17 pick）→ 成稿（T-14 write）→ 入队，明天才发
```

「queued_at < 今天」是两阶段的红线：今天入队的稿子今天绝不上车，
T-22 的 hold/edit/pause 干预窗口就靠这一天。

## 不静默失败（R16 验收第 2 条）

- 每次运行（含周末跳过）都落 `data/reports/<date>.json` —— finally 保证，
  进程崩了日报也在（UT-18-04）。
- 心跳 `data/schedule/heartbeat.json` 每次运行刷新；`check_heartbeat`
  超 26h 无记录即告警 —— 「进程没跑」这种情况日报自己说不出话来，只能靠心跳。
- 节奏控制（R14）走 `core/guard.check_rate`：调度触发与手动发布同一条检查，
  到阈值就跳过并在日报写明原因（UT-18-05 / G6）。

## 运营时区

调度语义（工作日、10:00 发车）说的是**北京时间**，与机器时区无关：
全部判定先 `astimezone(tz)` 再取日期/时刻。默认 +08:00，可注入（测试用）。

分层纪律：本模块不认识浏览器与 DOM。发布动作是注入的 `publish_one`
（生产接线在 cli.py，走 publish_draft）；选题成稿是注入的 `prepare`。
所以整条链路可以用假时钟 + 假执行器原样跑（IT-18-01 就是这么快进 5 天的）。
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..adapters.base import ContentRejected, CoverError, InjectFailed, MetaError, SubmitBlocked
from ..core.clock import Clock, SystemClock
from ..core.config import Config, PlatformConfig
from ..core.guard import published_in_window
from ..core.lock import LockError
from ..core.session import AuthRequired, BrowserUnavailable
from ..core.state import PublishLog, Status
from ..publish import DraftLoadError, GateClosed
from ..write.cover import CoverNotApproved

__all__ = [
    "HEARTBEAT_MAX_AGE_HOURS",
    "QUEUE_SCHEMA",
    "REPORT_SCHEMA",
    "SCHEDULE_TZ",
    "DayReport",
    "Dispatch",
    "DispatchItemResult",
    "HeartbeatStale",
    "PrepareResult",
    "QueueItem",
    "SchedulerError",
    "Unresolved",
    "check_heartbeat",
    "check_stagger",
    "is_workday",
    "load_queue",
    "load_report",
    "queue_path",
    "report_path",
    "run_daily",
    "save_queue",
    "save_report",
    "touch_heartbeat",
    "unresolved_human",
]

QUEUE_SCHEMA = 1
REPORT_SCHEMA = 1

# 运营时区：平台（CSDN）与读者都在国内，「工作日 10:00」指北京时间。
# 写死而不是读机器时区：CI runner 在 UTC，服务器在哪不该改变发车时间。
SCHEDULE_TZ = timezone(timedelta(hours=8), name="CST")

# 心跳阈值：调度每天至少跑一次（cron 含周末），超过 26h 无记录 = 进程大概率挂了
HEARTBEAT_MAX_AGE_HOURS = 26

# prepare 产出的稿子投到哪个平台：目前取 publish_at 最早的启用平台。
# 多平台各写一篇是后续工单的事（选题留痕按天唯一，天然一天一篇）。
#


class SchedulerError(ValueError):
    """调度层的非法状态：队列/日报文件损坏、schema 不符、错峰冲突等。"""


class HeartbeatStale(RuntimeError):
    """心跳缺失或超龄：调度进程可能根本没在跑。"""


# ---------------------------------------------------------------- 数据结构
@dataclass(frozen=True, slots=True)
class QueueItem:
    """待发布队列的一条：前一天 prepare 入队的稿件。"""

    slug: str
    draft_dir: str
    platform: str
    query_id: str
    queued_at: datetime

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "draft_dir": self.draft_dir,
            "platform": self.platform,
            "query_id": self.query_id,
            "queued_at": self.queued_at.isoformat(),
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, where: str) -> QueueItem:
        missing = [
            k for k in ("slug", "draft_dir", "platform", "query_id", "queued_at") if k not in data
        ]
        if missing:
            raise SchedulerError(f"{where} 缺字段：{', '.join(missing)}")
        try:
            queued_at = datetime.fromisoformat(str(data["queued_at"]))
        except ValueError as exc:
            raise SchedulerError(f"{where}.queued_at 不是合法时间：{data['queued_at']!r}") from exc
        if queued_at.tzinfo is None:
            raise SchedulerError(f"{where}.queued_at 必须带时区：{data['queued_at']!r}")
        return cls(
            slug=str(data["slug"]),
            draft_dir=str(data["draft_dir"]),
            platform=str(data["platform"]).strip().lower(),
            query_id=str(data["query_id"]),
            queued_at=queued_at,
        )


@dataclass(frozen=True, slots=True)
class Unresolved:
    """一篇卡在 needs_human 的稿子（最新一行日志仍是 needs_human）。"""

    slug: str
    platform: str
    note: str
    since: str

    def to_json(self) -> dict[str, Any]:
        return {"slug": self.slug, "platform": self.platform, "note": self.note, "since": self.since}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Unresolved:
        return cls(
            slug=str(data["slug"]),
            platform=str(data["platform"]),
            note=str(data.get("note") or ""),
            since=str(data.get("since") or ""),
        )


@dataclass(frozen=True, slots=True)
class DispatchItemResult:
    """队列里一篇稿子今天的处置结果。"""

    slug: str
    result: str  # published / failed / skipped_duplicate / needs_human / already_published / rejected_dropped / deferred
    url: str = ""
    reason: str = ""
    failure_kind: str | None = None

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "result": self.result,
            "url": self.url,
            "reason": self.reason,
            "failure_kind": self.failure_kind,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> DispatchItemResult:
        return cls(
            slug=str(data["slug"]),
            result=str(data["result"]),
            url=str(data.get("url") or ""),
            reason=str(data.get("reason") or ""),
            failure_kind=data.get("failure_kind"),
        )


@dataclass(slots=True)
class Dispatch:
    """一个平台今天的一次发车记录。"""

    platform: str
    publish_at: str
    due: bool
    dispatched: bool
    planned: int
    published: int
    skipped_reason: str = ""
    items: list[DispatchItemResult] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "publish_at": self.publish_at,
            "due": self.due,
            "dispatched": self.dispatched,
            "planned": self.planned,
            "published": self.published,
            "skipped_reason": self.skipped_reason,
            "items": [i.to_json() for i in self.items],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> Dispatch:
        return cls(
            platform=str(data["platform"]),
            publish_at=str(data["publish_at"]),
            due=bool(data["due"]),
            dispatched=bool(data["dispatched"]),
            planned=int(data["planned"]),
            published=int(data["published"]),
            skipped_reason=str(data.get("skipped_reason") or ""),
            items=[DispatchItemResult.from_json(i) for i in data.get("items") or []],
        )


@dataclass(frozen=True, slots=True)
class PrepareResult:
    """prepare 阶段（选题+成稿）的结论。`prepare` 回调返回的就是它。"""

    status: str  # prepared / no_topic / failed / skipped
    slug: str = ""
    query_id: str = ""
    draft_dir: str = ""
    platform: str = ""
    reason: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "slug": self.slug,
            "query_id": self.query_id,
            "draft_dir": self.draft_dir,
            "platform": self.platform,
            "reason": self.reason,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PrepareResult:
        return cls(
            status=str(data["status"]),
            slug=str(data.get("slug") or ""),
            query_id=str(data.get("query_id") or ""),
            draft_dir=str(data.get("draft_dir") or ""),
            platform=str(data.get("platform") or ""),
            reason=str(data.get("reason") or ""),
        )


@dataclass(slots=True)
class DayReport:
    """当日日报数据（data/reports/<date>.json）。T-19 负责渲染成 md + 邮件。

    `expected` / `published`（应发/实发）是 IT-18-01 的字段契约，
    由 dispatches 汇总而来，不许手填 —— 手填的会和明细对不上。
    """

    date: str
    workday: bool
    generated_at: str
    updated_at: str
    dispatches: list[Dispatch] = field(default_factory=list)
    unresolved: list[Unresolved] = field(default_factory=list)
    prepare: PrepareResult | None = None
    notes: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def expected(self) -> int:
        return sum(d.planned for d in self.dispatches)

    @property
    def published(self) -> int:
        return sum(d.published for d in self.dispatches)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": REPORT_SCHEMA,
            "date": self.date,
            "workday": self.workday,
            "generated_at": self.generated_at,
            "updated_at": self.updated_at,
            "expected": self.expected,
            "published": self.published,
            "dispatches": [d.to_json() for d in self.dispatches],
            "unresolved": [u.to_json() for u in self.unresolved],
            "prepare": None if self.prepare is None else self.prepare.to_json(),
            "notes": list(self.notes),
            "error": self.error,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, where: str) -> DayReport:
        if data.get("schema") != REPORT_SCHEMA:
            raise SchedulerError(f"{where} 的 schema 必须是 {REPORT_SCHEMA}，收到 {data.get('schema')!r}")
        for key in ("date", "workday", "generated_at", "updated_at"):
            if key not in data:
                raise SchedulerError(f"{where} 缺字段：{key}")
        prepare_raw = data.get("prepare")
        return cls(
            date=str(data["date"]),
            workday=bool(data["workday"]),
            generated_at=str(data["generated_at"]),
            updated_at=str(data["updated_at"]),
            dispatches=[Dispatch.from_json(d) for d in data.get("dispatches") or []],
            unresolved=[Unresolved.from_json(u) for u in data.get("unresolved") or []],
            prepare=None if prepare_raw is None else PrepareResult.from_json(prepare_raw),
            notes=[str(n) for n in data.get("notes") or []],
            error=data.get("error"),
        )


# ---------------------------------------------------------------- 路径
def queue_path(data_root: str | Path) -> Path:
    return Path(data_root) / "queue.json"


def report_path(data_root: str | Path, day: date | str) -> Path:
    day_str = day if isinstance(day, str) else day.isoformat()
    return Path(data_root) / "reports" / f"{day_str}.json"


def heartbeat_path(data_root: str | Path) -> Path:
    return Path(data_root) / "schedule" / "heartbeat.json"


# ---------------------------------------------------------------- 小判定
def is_workday(day: date) -> bool:
    """工作日 = 周一到周五。节假日历不维护（已知边界）：法定假日的代价
    是多一篇稿，比维护一份每年变的日历便宜；要停的那天用 pause（T-22）。"""
    return day.weekday() < 5


def check_stagger(platforms: Sequence[PlatformConfig]) -> None:
    """错峰校验（UT-18-02）：启用平台的 publish_at 必须两两不同。

    撞车不自动错开（自动改时间 = 配置说了不算），直接报配置错误让人改。
    """
    seen: dict[time, str] = {}
    for p in platforms:
        if p.publish_at in seen:
            raise SchedulerError(
                f"平台 {seen[p.publish_at]} 与 {p.key} 的 publish_at 都是 "
                f"{p.publish_at.strftime('%H:%M')}：多平台必须错峰（改 config 的 publish_at）"
            )
        seen[p.publish_at] = p.key


def unresolved_human(log: PublishLog) -> list[Unresolved]:
    """扫描 publish-log：每个幂等键的最新一行仍是 needs_human 的，就是未解决项。

    人工把状态改回 pending 后自动从这里消失（UT-18-03 的另一面：
    「处理完次日自动恢复」在报表层的体现）。
    """
    latest: dict[str, Any] = {}
    for entry in log.entries():
        latest[entry.idem_key] = entry
    found = [
        Unresolved(
            slug=e.slug,
            platform=e.platform,
            note=e.note,
            since=e.ts.isoformat(),
        )
        for e in latest.values()
        if e.status is Status.NEEDS_HUMAN
    ]
    found.sort(key=lambda u: u.since)
    return found


# ---------------------------------------------------------------- 队列读写
def load_queue(path: str | Path) -> list[QueueItem]:
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"queue 文件不是合法 JSON：{p}（第 {exc.lineno} 行）") from exc
    if not isinstance(data, Mapping) or data.get("schema") != QUEUE_SCHEMA:
        raise SchedulerError(f"{p} 的 schema 必须是 {QUEUE_SCHEMA}")
    items = data.get("items")
    if not isinstance(items, list):
        raise SchedulerError(f"{p} 的 items 必须是数组")
    return [QueueItem.from_json(item, where=f"{p} items[{i}]") for i, item in enumerate(items)]


def save_queue(path: str | Path, items: Iterable[QueueItem]) -> None:
    payload = {"schema": QUEUE_SCHEMA, "items": [i.to_json() for i in items]}
    _atomic_write_json(Path(path), payload)


# ---------------------------------------------------------------- 日报读写
def load_report(data_root: str | Path, day: date | str) -> DayReport | None:
    p = report_path(data_root, day)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise SchedulerError(f"日报不是合法 JSON：{p}（第 {exc.lineno} 行）") from exc
    return DayReport.from_json(data, where=f"日报 {p}")


def save_report(data_root: str | Path, report: DayReport, clock: Clock | None = None) -> Path:
    clk = clock if clock is not None else SystemClock()
    report.updated_at = clk.now().isoformat()
    target = report_path(data_root, report.date)
    _atomic_write_json(target, report.to_json())
    return target


def _atomic_write_json(target: Path, payload: Mapping[str, Any]) -> None:
    """临时文件 + os.replace：cron 读日报时绝不读到写一半的文件。"""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# ---------------------------------------------------------------- 心跳
def touch_heartbeat(data_root: str | Path, clock: Clock) -> Path:
    """每次运行刷新心跳 —— 包括周末跳过（周末不跑 ≠ 进程死了）。"""
    target = heartbeat_path(data_root)
    _atomic_write_json(target, {"schema": 1, "last_run_at": clock.now().isoformat()})
    return target


def check_heartbeat(
    data_root: str | Path,
    *,
    clock: Clock | None = None,
    max_age_hours: int = HEARTBEAT_MAX_AGE_HOURS,
) -> None:
    """超龄即抛 HeartbeatStale。「调度没跑」只能靠这个发现：日报再全，
    也是「跑过的人写的」；没人跑的那一天，日报本身就不存在。"""
    clk = clock if clock is not None else SystemClock()
    p = heartbeat_path(data_root)
    if not p.exists():
        raise HeartbeatStale(f"调度从未运行过：{p} 不存在（先跑一次 geopublish schedule）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        last = datetime.fromisoformat(str(data["last_run_at"]))
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        raise HeartbeatStale(f"心跳文件损坏：{p}（{exc}）——按调度未运行处理") from exc
    age = clk.now() - last
    if age > timedelta(hours=max_age_hours):
        raise HeartbeatStale(
            f"调度已 {age.days * 24 + age.seconds // 3600} 小时未运行"
            f"（阈值 {max_age_hours}h，最后记录 {last.isoformat()}）：进程可能挂了"
        )


# ---------------------------------------------------------------- 主流程
# publish_one 的签名：(QueueItem, PlatformConfig) -> 带 ok/url/reason/skipped_duplicate/
# failure_kind 的对象（生产接线就是 publish_draft 的 PublishOutcome）。
PublishOne = Callable[[QueueItem, PlatformConfig], Any]
Prepare = Callable[[], PrepareResult]

# 这些异常是「这次运行环境/输入的问题」，记进日报继续走；其余异常（bug）
# 原样抛出 —— finally 保证日报照样落盘（UT-18-04）。
def run_daily(
    cfg: Config,
    *,
    clock: Clock | None = None,
    tz: timezone = SCHEDULE_TZ,
    publish_one: PublishOne | None = None,
    prepare: Prepare | None = None,
) -> DayReport:
    """跑一天的调度：先发车（昨天及以前入队的），再选题成稿（明天发）。

    同日可安全重跑：已发车平台跳过（标记在日报里）、已完成的 prepare 跳过
    （标记在日报与 data/select/<date>.json 留痕里），队列写回是原子替换。
    """
    clk = clock if clock is not None else SystemClock()
    now = clk.now()
    local = now.astimezone(tz)
    day = local.date()
    data_root = Path(cfg.data_dir)

    # 心跳在最前面：哪怕后面全崩了，「今天跑过」这个事实也要留下来
    touch_heartbeat(data_root, clk)

    report = DayReport(
        date=day.isoformat(),
        workday=is_workday(day),
        generated_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    try:
        existing = load_report(data_root, day)
        check_stagger(cfg.enabled_platforms)
        if not report.workday:
            report.notes.append(f"{day.isoformat()} 是周末：非工作日不触发（R16 只工作日发车）")
            return report
        if not cfg.enabled_platforms:
            raise SchedulerError("配置里没有启用的平台：没有平台就没有发车对象")

        log = PublishLog(data_root / "publish-log.csv")
        report.unresolved = unresolved_human(log)

        queue = load_queue(queue_path(data_root))
        carried = {d.platform: d for d in existing.dispatches if d.dispatched} if existing else {}
        for platform_cfg in sorted(cfg.enabled_platforms, key=lambda p: p.publish_at):
            if platform_cfg.key in carried:
                report.dispatches.append(carried[platform_cfg.key])
                report.notes.append(f"{platform_cfg.key} 今日已发车，本次运行跳过（防同日重发）")
                continue
            dispatch, queue = _dispatch(platform_cfg, queue, log, local, tz, publish_one, clk)
            report.dispatches.append(dispatch)
        save_queue(queue_path(data_root), queue)

        report.prepare, fresh = _run_prepare(data_root, day, existing, prepare)
        if fresh and report.prepare.status == "prepared":
            queue.append(
                QueueItem(
                    slug=report.prepare.slug,
                    draft_dir=report.prepare.draft_dir,
                    platform=report.prepare.platform,
                    query_id=report.prepare.query_id,
                    queued_at=now,
                )
            )
            save_queue(queue_path(data_root), queue)
        return report
    except Exception as exc:
        # 不吞：异常照样抛给调用方（CLI 退 6），但日报先把现场钉下来
        report.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        save_report(data_root, report, clk)


def _dispatch(
    platform_cfg: PlatformConfig,
    queue: list[QueueItem],
    log: PublishLog,
    local: datetime,
    tz: timezone,
    publish_one: PublishOne | None,
    clock: Clock,
) -> tuple[Dispatch, list[QueueItem]]:
    """一个平台的一天：到点 → 过滤队列 → 节奏检查 → 逐篇发布。

    返回（发车记录, 处理后的队列）。出队规则：
    published / skipped_duplicate / already_published / rejected_dropped 出队；
    failed / needs_human / deferred 留队（明天或人工处理后再来）。
    """
    day = local.date()
    dispatch = Dispatch(
        platform=platform_cfg.key,
        publish_at=platform_cfg.publish_at.strftime("%H:%M"),
        due=False,
        dispatched=False,
        planned=0,
        published=0,
    )
    others = [i for i in queue if i.platform != platform_cfg.key]
    mine = [i for i in queue if i.platform == platform_cfg.key]

    if local.time() < platform_cfg.publish_at:
        dispatch.skipped_reason = f"未到发车时间 {dispatch.publish_at}"
        return dispatch, queue

    dispatch.due = True
    dispatch.dispatched = True

    keep: list[QueueItem] = []
    candidates: list[QueueItem] = []
    for item in mine:
        if item.queued_at.astimezone(tz).date() >= day:
            keep.append(item)  # 两阶段红线：今天入队的今天不发
            continue
        status = _latest_status(log, platform_cfg.key, item.slug)
        if status is Status.PUBLISHED:
            dispatch.items.append(
                DispatchItemResult(slug=item.slug, result="already_published",
                                   reason="已是 published 终态，出队")
            )
        elif status is Status.NEEDS_HUMAN:
            keep.append(item)
            dispatch.items.append(
                DispatchItemResult(slug=item.slug, result="needs_human",
                                   reason="待人工处理，不自动重试（R9/R13）")
            )
        elif status is Status.REJECTED:
            dispatch.items.append(
                DispatchItemResult(slug=item.slug, result="rejected_dropped",
                                   reason="自检未过被 rejected，出队；改稿后重新入队")
            )
        else:
            candidates.append(item)

    dispatch.planned = len(candidates)
    if not candidates:
        return dispatch, others + keep

    if publish_one is None:
        dispatch.skipped_reason = "未接线 publish_one：本次运行不实际发布"
        keep.extend(candidates)
        return dispatch, others + keep

    published_count = published_in_window(log, platform_cfg.key, clock=clock)
    budget = platform_cfg.daily_limit - published_count
    if budget <= 0:
        # UT-18-05 / G6：调度触发同样过节奏检查，原因写进日报，不静默跳过
        dispatch.skipped_reason = (
            f"已达节奏阈值：24h 内已发 {published_count} 篇 ≥ daily_limit "
            f"{platform_cfg.daily_limit}（R14），今日跳过发布"
        )
        dispatch.items.extend(
            DispatchItemResult(slug=i.slug, result="deferred", reason="节奏阈值已满，留到下一窗口")
            for i in candidates
        )
        keep.extend(candidates)
        return dispatch, others + keep

    for item in candidates[:budget]:
        try:
            outcome = publish_one(item, platform_cfg)
        except _OPERATIONAL_ERRORS as exc:
            dispatch.items.append(
                DispatchItemResult(slug=item.slug, result="failed",
                                   reason=f"{type(exc).__name__}: {exc}")
            )
            keep.append(item)
            continue
        if getattr(outcome, "skipped_duplicate", False):
            dispatch.items.append(
                DispatchItemResult(slug=item.slug, result="skipped_duplicate",
                                   url=str(getattr(outcome, "url", "") or ""),
                                   reason="幂等命中：该幂等键已 published，本次未发")
            )
            continue
        if outcome.ok:
            dispatch.published += 1
            dispatch.items.append(
                DispatchItemResult(slug=item.slug, result="published",
                                   url=str(getattr(outcome, "url", "") or ""),
                                   reason=str(getattr(outcome, "reason", "") or ""))
            )
            continue
        kind = getattr(outcome, "failure_kind", None)
        dispatch.items.append(
            DispatchItemResult(
                slug=item.slug,
                result="failed",
                url=str(getattr(outcome, "url", "") or ""),
                reason=str(getattr(outcome, "reason", "") or ""),
                failure_kind=None if kind is None else str(getattr(kind, "value", kind)),
            )
        )
        keep.append(item)

    overflow = candidates[budget:]
    if overflow:
        dispatch.items.extend(
            DispatchItemResult(slug=i.slug, result="deferred",
                               reason=f"超出今日节奏预算（daily_limit {platform_cfg.daily_limit}），留到下一工作日")
            for i in overflow
        )
        keep.extend(overflow)
    return dispatch, others + keep


def _latest_status(log: PublishLog, platform: str, slug: str) -> Status | None:
    """同平台同 slug 的最新一行状态（不看 account：调度层只关心「这篇稿子怎么了」）。"""
    status: Status | None = None
    key = platform.strip().lower()
    for entry in log.entries():
        if entry.platform.strip().lower() == key and entry.slug == slug:
            status = entry.status
    return status


def _run_prepare(
    data_root: Path,
    day: date,
    existing: DayReport | None,
    prepare: Prepare | None,
) -> tuple[PrepareResult, bool]:
    """prepare 阶段（选题+成稿）。已完成过就跳过——同日重复选题比不选更难查。

    返回（结果, 是否本次新跑）：承接旧日报的 prepared 不算新跑，
    调用方只对「新跑成的」入队，否则同日重跑会把同一篇塞两次。

    判定「已完成」看两处：当日日报（库调用/测试路径）与 data/select/<date>.json
    留痕（生产路径，pick 落盘）。失败（failed）不算完成，同日重跑会重试。
    """
    if existing is not None and existing.prepare is not None and existing.prepare.status in (
        "prepared",
        "no_topic",
    ):
        return existing.prepare, False
    if (data_root / "select" / f"{day.isoformat()}.json").exists():
        return PrepareResult(
            status="skipped",
            reason=f"今日选题留痕已存在（data/select/{day.isoformat()}.json），跳过 prepare",
        ), False
    if prepare is None:
        return PrepareResult(status="skipped", reason="未接线 prepare：本次运行不选题"), False
    from ..select.picker import PickerError
    from ..write.pipeline import PipelineError

    try:
        return prepare(), True
    except (PickerError, PipelineError) as exc:
        # 选题/成稿的输入类失败是「今天的料不够」，不是进程故障：记进日报，明天再来
        return PrepareResult(status="failed", reason=f"{type(exc).__name__}: {exc}"), True


# 运行期可预期的操作异常：记日报、留队、继续。其余异常 = bug，原样抛（finally 出日报）。
_OPERATIONAL_ERRORS = (
    ContentRejected,
    CoverError,
    InjectFailed,
    MetaError,
    SubmitBlocked,
    DraftLoadError,
    GateClosed,
    LockError,
    AuthRequired,
    BrowserUnavailable,
    CoverNotApproved,
)
