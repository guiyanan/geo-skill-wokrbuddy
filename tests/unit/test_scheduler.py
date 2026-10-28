"""T-18 每日调度（PRD R16，两阶段）单元测试。

覆盖：UT-18-01~05 + 支撑机制的辅助用例（工作日判定、错峰校验、队列/日报读写、
节奏预算、心跳）。IT-18-01（假时钟 5 工作日）在 tests/integration/。
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.config import Config
from geopublish.core.errors import FailureKind
from geopublish.core.guard import RateLimited, check_rate, published_in_window
from geopublish.core.models import LogEntry
from geopublish.core.state import PublishLog, Status, idem_key
from geopublish.publish import PublishOutcome
from geopublish.schedule.scheduler import (
    HEARTBEAT_MAX_AGE_HOURS,
    SCHEDULE_TZ,
    Dispatch,
    DispatchItemResult,
    HeartbeatStale,
    PrepareResult,
    QueueItem,
    SchedulerError,
    Unresolved,
    check_heartbeat,
    check_stagger,
    is_workday,
    load_queue,
    load_report,
    queue_path,
    report_path,
    run_daily,
    save_queue,
    save_report,
    touch_heartbeat,
    unresolved_human,
)
from geopublish.select.picker import PickerError

TZ = timezone(timedelta(hours=8))
# 2026-10-26 是周一 …… 10-30 周五；10-24 周六、10-25 周日
MON = "2026-10-26T10:00:00+08:00"
SAT = "2026-10-24T10:00:00+08:00"


# ---------------------------------------------------------------- helpers
def make_config(tmp_path: Path, *, platforms=None, gate="publish", workdays_only=True) -> Config:
    if platforms is None:
        platforms = [{"name": "csdn", "publish_at": "10:00", "daily_limit": 1}]
    return Config.from_dict(
        {
            "gate": gate,
            "platforms": platforms,
            "report": {"email": "t@example.com"},
            "data_dir": str(tmp_path),
            "workdays_only": workdays_only,
        }
    )


def qi(slug: str, queued_at: str, *, platform: str = "csdn", query_id: str = "C01") -> QueueItem:
    return QueueItem(
        slug=slug,
        draft_dir=f"data/drafts/{slug}",
        platform=platform,
        query_id=query_id,
        queued_at=datetime.fromisoformat(queued_at),
    )


def log_row(
    data_dir: Path,
    *,
    ts: str,
    slug: str,
    status: Status,
    platform: str = "csdn",
    url: str = "",
    note: str = "",
    account: str = "default",
) -> None:
    key = idem_key(platform, account, slug, f"sha-{slug}")
    PublishLog(data_dir / "publish-log.csv").append(
        LogEntry(
            ts=datetime.fromisoformat(ts),
            platform=platform,
            account=account,
            slug=slug,
            idem_key=key,
            status=status,
            url=url,
            note=note,
        )
    )


class FakePublisher:
    """记录调用、按脚本返回结果的 publish_one 替身。"""

    def __init__(self, outcomes=None):
        self.calls: list[str] = []
        self._outcomes = dict(outcomes or {})

    def __call__(self, item: QueueItem, platform_cfg) -> PublishOutcome:
        self.calls.append(item.slug)
        outcome = self._outcomes.get(item.slug)
        if isinstance(outcome, BaseException):
            raise outcome
        if outcome is not None:
            return outcome
        return PublishOutcome(
            ok=True, url=f"https://example.test/{item.slug}", article_id=f"a-{item.slug}"
        )


def fake_prepare(result: PrepareResult):
    calls = []

    def _prepare() -> list[PrepareResult]:
        calls.append(1)
        return [result]

    _prepare.calls = calls
    return _prepare


def read_report(data_dir: Path, day: str) -> dict:
    return json.loads((data_dir / "reports" / f"{day}.json").read_text(encoding="utf-8"))


# ---------------------------------------------------------------- 工作日
@pytest.mark.unit
class TestWorkday:
    @pytest.mark.parametrize(
        "day,expected",
        [
            (date(2026, 10, 26), True),   # 周一
            (date(2026, 10, 30), True),   # 周五
            (date(2026, 10, 24), False),  # 周六
            (date(2026, 10, 25), False),  # 周日
        ],
    )
    def test_weekday_rules(self, day, expected):
        assert is_workday(day) is expected


# ---------------------------------------------------------------- 错峰
@pytest.mark.unit
class TestStagger:
    def test_distinct_publish_at_passes(self, tmp_path):
        cfg = make_config(
            tmp_path,
            platforms=[
                {"name": "csdn", "publish_at": "10:00"},
                {"name": "juejin", "publish_at": "15:00"},
            ],
        )
        check_stagger(cfg.enabled_platforms)  # 不抛即过

    def test_collision_rejected(self, tmp_path):
        """UT-18-02 另一半：两平台撞同一时间 = 配置错误，要在发车前被拦下。"""
        cfg = make_config(
            tmp_path,
            platforms=[
                {"name": "csdn", "publish_at": "10:00"},
                {"name": "juejin", "publish_at": "10:00"},
            ],
        )
        with pytest.raises(SchedulerError, match="错峰"):
            check_stagger(cfg.enabled_platforms)

    def test_disabled_platform_ignored(self, tmp_path):
        cfg = make_config(
            tmp_path,
            platforms=[
                {"name": "csdn", "publish_at": "10:00"},
                {"name": "juejin", "publish_at": "10:00", "enabled": False},
            ],
        )
        check_stagger(cfg.enabled_platforms)  # 禁用平台不占错峰位


# ---------------------------------------------------------------- 队列读写
@pytest.mark.unit
class TestQueueIO:
    def test_missing_file_is_empty(self, tmp_path):
        assert load_queue(tmp_path / "queue.json") == []

    def test_roundtrip(self, tmp_path):
        path = queue_path(tmp_path)
        items = [qi("a-1", "2026-10-25T10:00:00+08:00"), qi("b-2", "2026-10-25T11:00:00+08:00", platform="juejin")]
        save_queue(path, items)
        assert load_queue(path) == items

    def test_corrupt_json_rejected(self, tmp_path):
        path = queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(SchedulerError, match="queue"):
            load_queue(path)

    def test_schema_mismatch_rejected(self, tmp_path):
        path = queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 99, "items": []}), encoding="utf-8")
        with pytest.raises(SchedulerError, match="schema"):
            load_queue(path)

    def test_item_missing_field_rejected(self, tmp_path):
        path = queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({"schema": 1, "items": [{"slug": "a-1"}]}), encoding="utf-8"
        )
        with pytest.raises(SchedulerError, match="queued_at|draft_dir"):
            load_queue(path)

    def test_naive_queued_at_rejected(self, tmp_path):
        path = queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "items": [
                        {
                            "slug": "a-1",
                            "draft_dir": "data/drafts/a-1",
                            "platform": "csdn",
                            "query_id": "C01",
                            "queued_at": "2026-10-25 10:00:00",  # 无时区
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(SchedulerError, match="时区"):
            load_queue(path)

    def test_save_is_atomic_no_tmp_residue(self, tmp_path):
        path = queue_path(tmp_path)
        save_queue(path, [qi("a-1", "2026-10-25T10:00:00+08:00")])
        leftovers = [p for p in path.parent.iterdir() if p.name != "queue.json"]
        assert leftovers == []


# ---------------------------------------------------------------- 节奏（guard.check_rate，T-20 复用）
@pytest.mark.unit
class TestRate:
    def test_counts_recent_published(self, tmp_path):
        log_row(tmp_path, ts="2026-10-26T08:00:00+08:00", slug="a-1", status=Status.PUBLISHED)
        log = PublishLog(tmp_path / "publish-log.csv")
        assert published_in_window(log, "csdn", clock=FakeClock(MON)) == 1

    def test_older_than_window_not_counted(self, tmp_path):
        log_row(tmp_path, ts="2026-10-25T09:59:00+08:00", slug="a-1", status=Status.PUBLISHED)
        log = PublishLog(tmp_path / "publish-log.csv")
        assert published_in_window(log, "csdn", clock=FakeClock(MON)) == 0

    def test_same_idem_counted_once(self, tmp_path):
        """崩溃恢复会补第二行 published：去重靠 idem_key，不能按行数。"""
        log_row(tmp_path, ts="2026-10-26T08:00:00+08:00", slug="a-1", status=Status.PUBLISHED)
        log_row(tmp_path, ts="2026-10-26T08:01:00+08:00", slug="a-1", status=Status.PUBLISHED)
        log = PublishLog(tmp_path / "publish-log.csv")
        assert published_in_window(log, "csdn", clock=FakeClock(MON)) == 1

    def test_other_platform_not_counted(self, tmp_path):
        log_row(tmp_path, ts="2026-10-26T08:00:00+08:00", slug="a-1", status=Status.PUBLISHED, platform="juejin")
        log = PublishLog(tmp_path / "publish-log.csv")
        assert published_in_window(log, "csdn", clock=FakeClock(MON)) == 0

    def test_non_published_not_counted(self, tmp_path):
        log_row(tmp_path, ts="2026-10-26T08:00:00+08:00", slug="a-1", status=Status.NEEDS_HUMAN)
        log = PublishLog(tmp_path / "publish-log.csv")
        assert published_in_window(log, "csdn", clock=FakeClock(MON)) == 0

    def test_check_rate_raises_at_limit(self, tmp_path):
        cfg = make_config(tmp_path)
        log_row(tmp_path, ts="2026-10-26T08:00:00+08:00", slug="a-1", status=Status.PUBLISHED)
        log = PublishLog(tmp_path / "publish-log.csv")
        with pytest.raises(RateLimited, match="daily_limit"):
            check_rate(log, cfg.platform("csdn"), clock=FakeClock(MON))

    def test_check_rate_passes_below_limit(self, tmp_path):
        cfg = make_config(
            tmp_path, platforms=[{"name": "csdn", "publish_at": "10:00", "daily_limit": 2}]
        )
        log_row(tmp_path, ts="2026-10-26T08:00:00+08:00", slug="a-1", status=Status.PUBLISHED)
        log = PublishLog(tmp_path / "publish-log.csv")
        check_rate(log, cfg.platform("csdn"), clock=FakeClock(MON))  # 1 < 2，不抛


# ---------------------------------------------------------------- needs_human 扫描
@pytest.mark.unit
class TestUnresolved:
    def test_latest_needs_human_listed(self, tmp_path):
        log_row(tmp_path, ts="2026-10-25T10:00:00+08:00", slug="a-1", status=Status.SUBMITTING)
        log_row(
            tmp_path, ts="2026-10-25T10:01:00+08:00", slug="a-1",
            status=Status.NEEDS_HUMAN, note="verify 未确认成功：页面停在审核提示",
        )
        log = PublishLog(tmp_path / "publish-log.csv")
        found = unresolved_human(log)
        assert len(found) == 1
        assert found[0].slug == "a-1"
        assert "verify" in found[0].note

    def test_resolved_by_human_not_listed(self, tmp_path):
        """人工把状态改回 pending = 已处理，不再算未解决。"""
        log_row(tmp_path, ts="2026-10-25T10:01:00+08:00", slug="a-1", status=Status.NEEDS_HUMAN)
        log_row(tmp_path, ts="2026-10-25T18:00:00+08:00", slug="a-1", status=Status.PENDING)
        log = PublishLog(tmp_path / "publish-log.csv")
        assert unresolved_human(log) == []

    def test_empty_log(self, tmp_path):
        assert unresolved_human(PublishLog(tmp_path / "publish-log.csv")) == []


# ---------------------------------------------------------------- 心跳
@pytest.mark.unit
class TestHeartbeat:
    def test_touch_then_check_passes(self, tmp_path):
        clock = FakeClock(MON)
        touch_heartbeat(tmp_path, clock)
        check_heartbeat(tmp_path, clock=clock)  # 不抛即过

    def test_missing_heartbeat_is_stale(self, tmp_path):
        with pytest.raises(HeartbeatStale, match="从未运行"):
            check_heartbeat(tmp_path, clock=FakeClock(MON))

    def test_stale_beyond_26h(self, tmp_path):
        touch_heartbeat(tmp_path, FakeClock(MON))
        later = FakeClock(MON)
        later.advance((HEARTBEAT_MAX_AGE_HOURS + 1) * 3600)
        with pytest.raises(HeartbeatStale, match="26"):
            check_heartbeat(tmp_path, clock=later)

    def test_corrupt_heartbeat_is_stale(self, tmp_path):
        path = tmp_path / "schedule" / "heartbeat.json"
        path.parent.mkdir(parents=True)
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(HeartbeatStale, match="损坏"):
            check_heartbeat(tmp_path, clock=FakeClock(MON))


# ---------------------------------------------------------------- UT-18-01 非工作日不触发
@pytest.mark.unit
class TestNonWorkday:
    def test_saturday_does_nothing_but_report(self, tmp_path):
        """UT-18-01：周末不选题、不发布，但日报照落（R16：不许静默）。"""
        publisher = FakePublisher()
        prep = fake_prepare(PrepareResult(status="prepared", slug="x"))
        cfg = make_config(tmp_path)
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-23T10:00:00+08:00")])

        report = run_daily(cfg, clock=FakeClock(SAT), publish_one=publisher, prepare=prep)

        assert publisher.calls == [], "非工作日不许发车"
        assert prep.calls == [], "非工作日不许选题"
        assert report.workday is False
        assert report.expected == 0 and report.published == 0
        on_disk = read_report(tmp_path, "2026-10-24")
        assert on_disk["workday"] is False
        assert on_disk["expected"] == 0 and on_disk["published"] == 0
        # 周末心跳也要刷新：cron 每天跑，周一不该误报「调度挂了」
        check_heartbeat(tmp_path, clock=FakeClock(SAT))


# ---------------------------------------------------------------- UT-18-02 多平台错峰
@pytest.mark.unit
class TestDispatchStagger:
    def test_two_platforms_dispatch_times_differ(self, tmp_path):
        """UT-18-02：两平台各按自己的 publish_at 发车，时间不重叠。"""
        cfg = make_config(
            tmp_path,
            platforms=[
                {"name": "csdn", "publish_at": "10:00"},
                {"name": "juejin", "publish_at": "15:00"},
            ],
        )
        save_queue(
            queue_path(tmp_path),
            [
                qi("a-1", "2026-10-25T10:00:00+08:00"),
                qi("b-1", "2026-10-25T10:00:00+08:00", platform="juejin"),
            ],
        )
        publisher = FakePublisher()
        # 15:30：两个平台都已到点
        report = run_daily(cfg, clock=FakeClock("2026-10-26T15:30:00+08:00"), publish_one=publisher)

        times = [d.publish_at for d in report.dispatches]
        assert sorted(times) == ["10:00", "15:00"]
        assert len(set(times)) == 2, "发车时间必须错开"
        assert sorted(publisher.calls) == ["a-1", "b-1"]

    def test_collision_aborts_run_with_report(self, tmp_path):
        cfg = make_config(
            tmp_path,
            platforms=[
                {"name": "csdn", "publish_at": "10:00"},
                {"name": "juejin", "publish_at": "10:00"},
            ],
        )
        with pytest.raises(SchedulerError, match="错峰"):
            run_daily(cfg, clock=FakeClock(MON), publish_one=FakePublisher())
        # 配置错误也不许静默：日报里要有 error
        assert "错峰" in read_report(tmp_path, "2026-10-26")["error"]


# ---------------------------------------------------------------- UT-18-03 needs_human 未处理
@pytest.mark.unit
class TestNeedsHumanCarryOver:
    def test_unresolved_listed_and_run_continues(self, tmp_path):
        """UT-18-03：前一日 needs_human 未处理 → 当日仍发车（别的稿照发），
        日报标注未解决项；卡住的稿子留在队列里等人工，不自动重试。"""
        log_row(tmp_path, ts="2026-10-25T10:00:00+08:00", slug="stuck-1", status=Status.SUBMITTING)
        log_row(
            tmp_path, ts="2026-10-25T10:01:00+08:00", slug="stuck-1",
            status=Status.NEEDS_HUMAN, note="verify 未确认成功",
        )
        save_queue(
            queue_path(tmp_path),
            [
                qi("stuck-1", "2026-10-25T09:00:00+08:00"),
                qi("fresh-1", "2026-10-25T09:30:00+08:00"),
            ],
        )
        publisher = FakePublisher()
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)

        assert publisher.calls == ["fresh-1"], "卡住的稿子不自动重试（R9/R13）"
        assert [u.slug for u in report.unresolved] == ["stuck-1"]
        assert "verify" in report.unresolved[0].note
        disp = report.dispatches[0]
        assert disp.published == 1
        results = {i.slug: i.result for i in disp.items}
        assert results["stuck-1"] == "needs_human"
        assert results["fresh-1"] == "published"
        # 队列：stuck-1 留着等人工，fresh-1 已发出队
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["stuck-1"]


# ---------------------------------------------------------------- UT-18-04 崩溃也出日报
@pytest.mark.unit
class TestCrashStillReports:
    def test_unexpected_exception_still_writes_report(self, tmp_path):
        """UT-18-04：进程异常退出 → 日报仍产出（finally 保证），error 字段带原因。"""
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        publisher = FakePublisher({"a-1": RuntimeError("磁盘炸了")})
        cfg = make_config(tmp_path)

        with pytest.raises(RuntimeError, match="磁盘炸了"):
            run_daily(cfg, clock=FakeClock(MON), publish_one=publisher)

        on_disk = read_report(tmp_path, "2026-10-26")
        assert "磁盘炸了" in on_disk["error"]
        assert "RuntimeError" in on_disk["error"]
        # 队列没动：稿子留着，明天（或修复后）再发，幂等层兜底
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1"]


# ---------------------------------------------------------------- UT-18-05 调度也过节奏检查
@pytest.mark.unit
class TestRateLimitUnderSchedule:
    def test_limit_reached_skips_with_reason(self, tmp_path):
        """UT-18-05：调度自动触发且当日已达阈值 → 跳过发布，日报写明原因（G6）。"""
        log_row(
            tmp_path, ts="2026-10-26T08:00:00+08:00", slug="earlier-1",
            status=Status.PUBLISHED, url="https://example.test/earlier-1",
        )
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        publisher = FakePublisher()

        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)

        assert publisher.calls == [], "已达阈值不许发车"
        disp = report.dispatches[0]
        assert disp.published == 0
        assert "阈值" in disp.skipped_reason and "daily_limit" in disp.skipped_reason
        assert report.expected == 1 and report.published == 0
        # 稿子不出队：明天窗口滑过去之后再发
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1"]


# ---------------------------------------------------------------- 两阶段与同日重跑
@pytest.mark.unit
class TestTwoPhase:
    def test_queued_today_not_published_today(self, tmp_path):
        """两阶段红线：今天入队的稿子明天才发（R18.1 留出人工干预窗口）。"""
        save_queue(queue_path(tmp_path), [qi("a-1", MON)])  # 今天 10:00 入队
        publisher = FakePublisher()
        report = run_daily(make_config(tmp_path), clock=FakeClock("2026-10-26T10:05:00+08:00"), publish_one=publisher)

        assert publisher.calls == []
        assert report.expected == 0
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1"]

    def test_same_day_rerun_does_not_redispatch(self, tmp_path):
        """cron 同日触发两次（如 10:00 与 15:00 两个时刻）：已发车平台不重复发。"""
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        cfg = make_config(tmp_path)
        publisher = FakePublisher()
        prep = fake_prepare(
            PrepareResult(status="prepared", slug="b-1", query_id="C02",
                          draft_dir="data/drafts/b-1", platform="csdn")
        )

        run_daily(cfg, clock=FakeClock(MON), publish_one=publisher, prepare=prep)
        report2 = run_daily(cfg, clock=FakeClock("2026-10-26T11:00:00+08:00"), publish_one=publisher, prepare=prep)

        assert publisher.calls == ["a-1"], "第二次运行不许重发"
        assert prep.calls == [1], "第二次运行不许重复选题"
        assert len(report2.prepare) == 1 and report2.prepare[0].status == "prepared"
        assert report2.published == 1  # 合并后的当日总量不丢
        # a-1 出队、b-1 入队
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["b-1"]

    def test_not_yet_due_platform_waits(self, tmp_path):
        """10:00 的平台上 09:00 跑：不发车但 prepare 照跑（选题不看出车时间）。"""
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        publisher = FakePublisher()
        prep = fake_prepare(PrepareResult(status="no_topic", reason="14 条缺口全部出局"))
        report = run_daily(
            make_config(tmp_path), clock=FakeClock("2026-10-26T09:00:00+08:00"),
            publish_one=publisher, prepare=prep,
        )

        assert publisher.calls == []
        disp = report.dispatches[0]
        assert disp.due is False and disp.dispatched is False
        assert "未到" in disp.skipped_reason
        assert prep.calls == [1]
        assert len(report.prepare) == 1 and report.prepare[0].status == "no_topic"
        # 同日第二次运行到点了：还能发车（未到点不算「已发车」）
        report2 = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)
        assert publisher.calls == ["a-1"]
        assert report2.published == 1


# ---------------------------------------------------------------- prepare 阶段
@pytest.mark.unit
class TestPrepare:
    def test_prepared_appends_queue_item(self, tmp_path):
        prep = fake_prepare(
            PrepareResult(status="prepared", slug="a-1", query_id="C01",
                          draft_dir="data/drafts/a-1", platform="csdn")
        )
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=FakePublisher(), prepare=prep)

        assert len(report.prepare) == 1 and report.prepare[0].status == "prepared"
        (item,) = load_queue(queue_path(tmp_path))
        assert item.slug == "a-1" and item.query_id == "C01"
        assert item.queued_at.date().isoformat() == "2026-10-26"

    def test_no_topic_is_a_legit_result(self, tmp_path):
        prep = fake_prepare(PrepareResult(status="no_topic", reason="全部出局"))
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=FakePublisher(), prepare=prep)
        assert [p.status for p in report.prepare] == ["no_topic"]
        assert load_queue(queue_path(tmp_path)) == []

    def test_prepare_failure_recorded_not_raised(self, tmp_path):
        def boom():
            raise PickerError("geo-monitor 没有任何一期 samples.json")

        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=FakePublisher(), prepare=boom)
        assert len(report.prepare) == 1 and report.prepare[0].status == "failed"
        assert "samples" in report.prepare[0].reason

    def test_existing_select_trace_skips_prepare(self, tmp_path):
        """生产侧防重：今日留痕已存在 = 选题已做过，prepare 不再跑。"""
        trace = tmp_path / "select" / "2026-10-26.json"
        trace.parent.mkdir(parents=True)
        trace.write_text("{}", encoding="utf-8")
        prep = fake_prepare(PrepareResult(status="prepared", slug="x"))
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=FakePublisher(), prepare=prep)

        assert prep.calls == []
        assert len(report.prepare) == 1 and report.prepare[0].status == "skipped"

    def test_prepare_not_wired(self, tmp_path):
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=FakePublisher())
        assert len(report.prepare) == 1 and report.prepare[0].status == "skipped"


# ---------------------------------------------------------------- 队列内终态处置
@pytest.mark.unit
class TestTerminalStates:
    def test_already_published_dropped(self, tmp_path):
        """队列里躺着一篇其实已经发过的（如人工直发）：出队，不占应发。"""
        log_row(
            tmp_path, ts="2026-10-25T15:00:00+08:00", slug="a-1",
            status=Status.PUBLISHED, url="https://example.test/a-1",
        )
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        publisher = FakePublisher()
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)

        assert publisher.calls == []
        assert report.expected == 0
        assert report.dispatches[0].items[0].result == "already_published"
        assert load_queue(queue_path(tmp_path)) == []

    def test_rejected_dropped_with_note(self, tmp_path):
        """自检打回的稿子出队并在日报留话：改稿后重新入队（不许自动复活）。"""
        log_row(tmp_path, ts="2026-10-25T15:00:00+08:00", slug="a-1", status=Status.REJECTED)
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=FakePublisher())

        assert report.dispatches[0].items[0].result == "rejected_dropped"
        assert load_queue(queue_path(tmp_path)) == []

    def test_failed_publish_stays_queued(self, tmp_path):
        """平台拒稿（分类失败）：留在队列，日报带 failure_kind 与原话。"""
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        publisher = FakePublisher(
            {
                "a-1": PublishOutcome(
                    ok=False,
                    reason="failure_kind=content_rejected；平台原话：含营销内容",
                    failure_kind=FailureKind.CONTENT_REJECTED,
                )
            }
        )
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)

        assert report.published == 0 and report.expected == 1
        item = report.dispatches[0].items[0]
        assert item.result == "failed"
        assert item.failure_kind == "CONTENT_REJECTED"
        assert "平台原话" in item.reason
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1"]

    def test_skipped_duplicate_drops_from_queue(self, tmp_path):
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        publisher = FakePublisher(
            {"a-1": PublishOutcome(ok=True, skipped_duplicate=True, url="https://example.test/a-1")}
        )
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)

        assert report.dispatches[0].items[0].result == "skipped_duplicate"
        assert report.published == 0, "幂等命中不算今天发的"
        assert load_queue(queue_path(tmp_path)) == []

    def test_over_budget_deferred(self, tmp_path):
        """daily_limit=1 但队列里有两篇：发第一篇，第二篇留到下一工作日。"""
        save_queue(
            queue_path(tmp_path),
            [qi("a-1", "2026-10-25T10:00:00+08:00"), qi("a-2", "2026-10-25T11:00:00+08:00")],
        )
        publisher = FakePublisher()
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)

        assert publisher.calls == ["a-1"]
        results = {i.slug: i.result for i in report.dispatches[0].items}
        assert results["a-2"] == "deferred"
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-2"]


# ---------------------------------------------------------------- 日报读写
@pytest.mark.unit
class TestReportIO:
    def test_roundtrip(self, tmp_path):
        from geopublish.schedule.scheduler import DayReport

        report = DayReport(
            date="2026-10-26",
            workday=True,
            generated_at=MON,
            updated_at=MON,
            dispatches=[
                Dispatch(
                    platform="csdn", publish_at="10:00", due=True, dispatched=True,
                    planned=1, published=1, skipped_reason="",
                    items=[DispatchItemResult(slug="a-1", result="published", url="https://x/a-1")],
                )
            ],
            unresolved=[Unresolved(slug="s-1", platform="csdn", note="n", since=MON)],
            prepare=[PrepareResult(status="no_topic", reason="全部出局")],
            notes=["测试"],
            error=None,
        )
        save_report(tmp_path, report, FakeClock(MON))
        loaded = load_report(tmp_path, date(2026, 10, 26))
        assert loaded is not None
        assert loaded == report

    def test_missing_report_is_none(self, tmp_path):
        assert load_report(tmp_path, date(2026, 10, 26)) is None

    def test_corrupt_report_rejected(self, tmp_path):
        path = report_path(tmp_path, date(2026, 10, 26))
        path.parent.mkdir(parents=True)
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(SchedulerError, match="日报"):
            load_report(tmp_path, date(2026, 10, 26))

    def test_report_json_has_expected_published_fields(self, tmp_path):
        """IT-18-01 的字段契约：「应发/实发」必须落在 JSON 里。"""
        run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=FakePublisher())
        on_disk = read_report(tmp_path, "2026-10-26")
        assert on_disk["schema"] == 1
        assert isinstance(on_disk["expected"], int)
        assert isinstance(on_disk["published"], int)
        assert isinstance(on_disk["dispatches"], list)
        assert isinstance(on_disk["unresolved"], list)


# ---------------------------------------------------------------- 边界与对抗用例
@pytest.mark.unit
class TestEdgeCases:
    def test_queue_item_invalid_queued_at(self, tmp_path):
        path = queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1,
                    "items": [
                        {
                            "slug": "a-1",
                            "draft_dir": "data/drafts/a-1",
                            "platform": "csdn",
                            "query_id": "C01",
                            "queued_at": "不是时间",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(SchedulerError, match="合法时间"):
            load_queue(path)

    def test_queue_items_must_be_a_list(self, tmp_path):
        path = queue_path(tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": 1, "items": {"slug": "a-1"}}), encoding="utf-8")
        with pytest.raises(SchedulerError, match="数组"):
            load_queue(path)

    def test_report_schema_mismatch_rejected(self, tmp_path):
        path = report_path(tmp_path, date(2026, 10, 26))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": 99}), encoding="utf-8")
        with pytest.raises(SchedulerError, match="schema"):
            load_report(tmp_path, date(2026, 10, 26))

    def test_report_missing_field_rejected(self, tmp_path):
        path = report_path(tmp_path, date(2026, 10, 26))
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": 1, "date": "2026-10-26"}), encoding="utf-8")
        with pytest.raises(SchedulerError, match="缺字段"):
            load_report(tmp_path, date(2026, 10, 26))

    def test_atomic_write_cleans_tmp_on_replace_failure(self, tmp_path, monkeypatch):
        """os.replace 失败：临时文件必须清掉，不留 .queue.*.tmp 残骸。"""
        import geopublish.schedule.scheduler as sched

        def boom(*args, **kwargs):
            raise OSError("磁盘满")

        monkeypatch.setattr(sched.os, "replace", boom)
        with pytest.raises(OSError, match="磁盘满"):
            save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        parent = queue_path(tmp_path).parent
        assert [p.name for p in parent.iterdir()] == []

    def test_no_enabled_platforms_is_config_error(self, tmp_path):
        cfg = make_config(
            tmp_path, platforms=[{"name": "csdn", "publish_at": "10:00", "enabled": False}]
        )
        with pytest.raises(SchedulerError, match="启用的平台"):
            run_daily(cfg, clock=FakeClock(MON), publish_one=FakePublisher())
        assert "启用的平台" in read_report(tmp_path, "2026-10-26")["error"]

    def test_publish_one_not_wired_keeps_items(self, tmp_path):
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=None)
        disp = report.dispatches[0]
        assert "未接线" in disp.skipped_reason
        assert disp.published == 0
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1"]

    def test_operational_error_recorded_not_raised(self, tmp_path):
        """闸门关闭（gate=draft）这类操作异常：记日报、留队、不炸进程。"""
        from geopublish.publish import GateClosed

        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        publisher = FakePublisher({"a-1": GateClosed("gate=draft：只允许存草稿")})
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)

        item = report.dispatches[0].items[0]
        assert item.result == "failed"
        assert "GateClosed" in item.reason and "gate=draft" in item.reason
        assert report.error is None
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1"]

    def test_heartbeat_missing_key_is_stale(self, tmp_path):
        path = tmp_path / "schedule" / "heartbeat.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": 1}), encoding="utf-8")
        with pytest.raises(HeartbeatStale, match="损坏"):
            check_heartbeat(tmp_path, clock=FakeClock(MON))

    def test_rerun_after_crash_repicks_prepare(self, tmp_path):
        """第一次运行崩在发车阶段（prepare 没跑成），同日修好后重跑：
        已发车的平台不重发，prepare 补跑 —— 崩溃不许吃掉明天的稿。"""
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        cfg = make_config(tmp_path)
        with pytest.raises(RuntimeError):
            run_daily(cfg, clock=FakeClock(MON),
                      publish_one=FakePublisher({"a-1": RuntimeError("炸")}),
                      prepare=fake_prepare(PrepareResult(status="prepared", slug="never")))

        prep = fake_prepare(
            PrepareResult(status="prepared", slug="b-1", query_id="C02",
                          draft_dir="data/drafts/b-1", platform="csdn")
        )
        report = run_daily(cfg, clock=FakeClock("2026-10-26T11:00:00+08:00"),
                           publish_one=FakePublisher(), prepare=prep)
        assert prep.calls == [1], "崩溃时没跑成的 prepare，同日重跑要补上"
        assert len(report.prepare) == 1 and report.prepare[0].slug == "b-1"


# ---------------------------------------------------------------- T-18b：周末发车 + 单槽限 1 + 批量 prepare
@pytest.mark.unit
class TestWeekendAndBatch:
    def test_weekend_publishes_when_workdays_only_false(self, tmp_path):
        """T-18b（拾一米 2026-10-28 决策）：workdays_only=false → 周末照常发车选题。"""
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-23T10:00:00+08:00")])
        publisher = FakePublisher()
        prep = fake_prepare(
            PrepareResult(status="prepared", slug="b-1", query_id="C02",
                          draft_dir="data/drafts/b-1", platform="csdn")
        )
        cfg = make_config(tmp_path, workdays_only=False)
        report = run_daily(cfg, clock=FakeClock(SAT), publish_one=publisher, prepare=prep)

        assert publisher.calls == ["a-1"]
        assert prep.calls == [1]
        assert report.published == 1
        on_disk = read_report(tmp_path, "2026-10-24")
        assert on_disk["workday"] is False, "workday 只是事实记录，不再是闸门"
        assert on_disk["published"] == 1

    def test_per_run_cap_one_per_slot(self, tmp_path):
        """单槽限 1 篇：daily_limit=2 + 队列两篇 → 本槽发 1 篇，另一篇留到下一槽。"""
        cfg = make_config(
            tmp_path, platforms=[{"name": "csdn", "publish_at": "10:00", "daily_limit": 2}]
        )
        save_queue(
            queue_path(tmp_path),
            [qi("a-1", "2026-10-25T10:00:00+08:00"), qi("a-2", "2026-10-25T11:00:00+08:00")],
        )
        publisher = FakePublisher()
        report = run_daily(cfg, clock=FakeClock(MON), publish_one=publisher)

        assert publisher.calls == ["a-1"], "同一槽位不许连发两篇"
        results = {i.slug: i.result for i in report.dispatches[0].items}
        assert results["a-2"] == "deferred"
        assert "单槽" in [i for i in report.dispatches[0].items if i.slug == "a-2"][0].reason
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-2"]

    def test_prepare_batch_of_two_queues_two(self, tmp_path):
        """prepare 一次产两篇 → 两篇都入队，明天起按槽位分别发出。"""
        prep = fake_prepare_batch(
            [
                PrepareResult(status="prepared", slug="a-1", query_id="C01",
                              draft_dir="data/drafts/a-1", platform="csdn"),
                PrepareResult(status="prepared", slug="a-2", query_id="C02",
                              draft_dir="data/drafts/a-2", platform="csdn"),
            ]
        )
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON),
                           publish_one=FakePublisher(), prepare=prep)

        assert [p.slug for p in report.prepare] == ["a-1", "a-2"]
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1", "a-2"]

    def test_prepare_empty_list_means_no_topic(self, tmp_path):
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON),
                           publish_one=FakePublisher(), prepare=lambda: [])
        assert [p.status for p in report.prepare] == ["no_topic"]

    def test_two_slots_drain_two_items_same_day(self, tmp_path):
        """10:00 槽发 a-1，15:00 槽发 a-2：一天两篇、间隔 5 小时、各自落日报。"""
        cfg = make_config(
            tmp_path, platforms=[{"name": "csdn", "publish_at": "10:00", "daily_limit": 2}]
        )
        save_queue(
            queue_path(tmp_path),
            [qi("a-1", "2026-10-25T10:00:00+08:00"), qi("a-2", "2026-10-25T11:00:00+08:00")],
        )
        publisher = FakePublisher()
        run_daily(cfg, clock=FakeClock("2026-10-26T10:00:00+08:00"), publish_one=publisher)
        report2 = run_daily(cfg, clock=FakeClock("2026-10-26T15:05:00+08:00"), publish_one=publisher)

        assert publisher.calls == ["a-1", "a-2"]
        assert report2.published == 2, "当日日报合并两个槽位的实发"
        assert load_queue(queue_path(tmp_path)) == []


def fake_prepare_batch(results):
    calls = []

    def _prepare():
        calls.append(1)
        return list(results)

    _prepare.calls = calls
    return _prepare


# ---------------------------------------------------------------- T-18b：周末发车 + 单槽限 1 + 批量 prepare
@pytest.mark.unit
class TestWeekendAndBatch:
    def test_weekend_publishes_when_workdays_only_false(self, tmp_path):
        """T-18b（拾一米 2026-10-28 决策）：workdays_only=false → 周末照常发车选题。"""
        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-23T10:00:00+08:00")])
        publisher = FakePublisher()
        prep = fake_prepare(
            PrepareResult(status="prepared", slug="b-1", query_id="C02",
                          draft_dir="data/drafts/b-1", platform="csdn")
        )
        cfg = make_config(tmp_path, workdays_only=False)
        report = run_daily(cfg, clock=FakeClock(SAT), publish_one=publisher, prepare=prep)

        assert publisher.calls == ["a-1"]
        assert prep.calls == [1]
        assert report.published == 1
        on_disk = read_report(tmp_path, "2026-10-24")
        assert on_disk["workday"] is False, "workday 只是事实记录，不再是闸门"
        assert on_disk["published"] == 1

    def test_per_run_cap_one_per_slot(self, tmp_path):
        """单槽限 1 篇：daily_limit=2 + 队列两篇 → 本槽发 1 篇，另一篇留到下一槽。"""
        cfg = make_config(
            tmp_path, platforms=[{"name": "csdn", "publish_at": "10:00", "daily_limit": 2}]
        )
        save_queue(
            queue_path(tmp_path),
            [qi("a-1", "2026-10-25T10:00:00+08:00"), qi("a-2", "2026-10-25T11:00:00+08:00")],
        )
        publisher = FakePublisher()
        report = run_daily(cfg, clock=FakeClock(MON), publish_one=publisher)

        assert publisher.calls == ["a-1"], "同一槽位不许连发两篇"
        results = {i.slug: i.result for i in report.dispatches[0].items}
        assert results["a-2"] == "deferred"
        assert "单槽" in [i for i in report.dispatches[0].items if i.slug == "a-2"][0].reason
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-2"]

    def test_prepare_batch_of_two_queues_two(self, tmp_path):
        """prepare 一次产两篇 → 两篇都入队，明天起按槽位分别发出。"""
        prep = fake_prepare_batch(
            [
                PrepareResult(status="prepared", slug="a-1", query_id="C01",
                              draft_dir="data/drafts/a-1", platform="csdn"),
                PrepareResult(status="prepared", slug="a-2", query_id="C02",
                              draft_dir="data/drafts/a-2", platform="csdn"),
            ]
        )
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON),
                           publish_one=FakePublisher(), prepare=prep)

        assert [p.slug for p in report.prepare] == ["a-1", "a-2"]
        assert [i.slug for i in load_queue(queue_path(tmp_path))] == ["a-1", "a-2"]

    def test_prepare_empty_list_means_no_topic(self, tmp_path):
        report = run_daily(make_config(tmp_path), clock=FakeClock(MON),
                           publish_one=FakePublisher(), prepare=lambda: [])
        assert [p.status for p in report.prepare] == ["no_topic"]

    def test_two_slots_drain_two_items_same_day(self, tmp_path):
        """10:00 槽发 a-1，15:00 槽发 a-2：一天两篇、间隔 5 小时、各自落日报。"""
        cfg = make_config(
            tmp_path, platforms=[{"name": "csdn", "publish_at": "10:00", "daily_limit": 2}]
        )
        save_queue(
            queue_path(tmp_path),
            [qi("a-1", "2026-10-25T10:00:00+08:00"), qi("a-2", "2026-10-25T11:00:00+08:00")],
        )
        publisher = FakePublisher()
        run_daily(cfg, clock=FakeClock("2026-10-26T10:00:00+08:00"), publish_one=publisher)
        report2 = run_daily(cfg, clock=FakeClock("2026-10-26T15:05:00+08:00"), publish_one=publisher)

        assert publisher.calls == ["a-1", "a-2"]
        assert report2.published == 2, "当日日报合并两个槽位的实发"
        assert load_queue(queue_path(tmp_path)) == []


def fake_prepare_batch(results):
    calls = []

    def _prepare():
        calls.append(1)
        return list(results)

    _prepare.calls = calls
    return _prepare


@pytest.mark.unit
class TestReportCompatAndSlotEdge:
    def test_report_without_prepare_reads_as_empty(self, tmp_path):
        """更老的日报（连 prepare 键都没有）读起来不炸，按空列表处理。"""
        path = report_path(tmp_path, date(2026, 10, 26))
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1, "date": "2026-10-26", "workday": True,
                    "generated_at": MON, "updated_at": MON,
                    "dispatches": [], "unresolved": [], "notes": [], "error": None,
                }
            ),
            encoding="utf-8",
        )
        loaded = load_report(tmp_path, date(2026, 10, 26))
        assert loaded is not None and loaded.prepare == []

    def test_legacy_single_object_prepare_wrapped(self, tmp_path):
        """T-18b 前的日报格式（prepare 是单对象）读起来不炸，包成列表。"""
        path = report_path(tmp_path, date(2026, 10, 26))
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "schema": 1, "date": "2026-10-26", "workday": True,
                    "generated_at": MON, "updated_at": MON,
                    "dispatches": [], "unresolved": [],
                    "prepare": {"status": "no_topic", "reason": "旧格式"},
                    "notes": [], "error": None,
                }
            ),
            encoding="utf-8",
        )
        loaded = load_report(tmp_path, date(2026, 10, 26))
        assert loaded is not None
        assert len(loaded.prepare) == 1 and loaded.prepare[0].status == "no_topic"

    def test_malformed_dispatched_at_allows_redispatch(self, tmp_path):
        """同槽去重的时间串坏了：宁可再发一次（幂等层兜底），不许卡住不发。"""
        from geopublish.schedule.scheduler import DayReport

        save_queue(queue_path(tmp_path), [qi("a-1", "2026-10-25T10:00:00+08:00")])
        existing = DayReport(
            date="2026-10-26", workday=True, generated_at=MON, updated_at=MON,
            dispatches=[Dispatch(platform="csdn", publish_at="10:00", due=True,
                                 dispatched=True, planned=0, published=0,
                                 dispatched_at="坏数据")],
        )
        save_report(tmp_path, existing, FakeClock(MON))
        publisher = FakePublisher()
        run_daily(make_config(tmp_path), clock=FakeClock(MON), publish_one=publisher)
        assert publisher.calls == ["a-1"]
