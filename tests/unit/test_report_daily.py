"""T-19 日报渲染（PRD R18）单元测试。

覆盖：UT-19-01（四问缺一即渲染失败）、UT-19-02（主题行格式）、
UT-19-04（零发布日写原因）、UT-19-05（URL 是可点击的 Markdown 链接）、
以及零发布连击（UT-19-07 的告警依据）与 md 原子落盘。
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.models import LogEntry
from geopublish.core.state import PublishLog, Status, idem_key
from geopublish.report.daily import (
    ReportError,
    render_daily_md,
    subject_line,
    write_daily_md,
    zero_publish_streak,
    zero_reasons,
)
from geopublish.schedule.scheduler import (
    DayReport,
    Dispatch,
    DispatchItemResult,
    PrepareResult,
    QueueItem,
    Unresolved,
    load_report,
    save_report,
)

pytestmark = pytest.mark.unit

DAY = "2026-10-27"  # 周二
NOW = f"{DAY}T10:05:00+08:00"


# ---------------------------------------------------------------- helpers
def make_report(
    *,
    date_str: str = DAY,
    workday: bool = True,
    dispatches=(),
    prepare=(),
    notes=(),
    unresolved=(),
    error=None,
) -> DayReport:
    return DayReport(
        date=date_str,
        workday=workday,
        generated_at=f"{date_str}T10:00:00+08:00",
        updated_at=f"{date_str}T10:05:00+08:00",
        dispatches=list(dispatches),
        unresolved=list(unresolved),
        prepare=list(prepare),
        notes=list(notes),
        error=error,
    )


def make_dispatch(
    platform: str = "csdn",
    *,
    planned: int = 1,
    published: int = 1,
    items=(),
    skipped: str = "",
) -> Dispatch:
    return Dispatch(
        platform=platform,
        publish_at="10:00",
        due=True,
        dispatched=True,
        planned=planned,
        published=published,
        skipped_reason=skipped,
        dispatched_at="10:05",
        items=list(items),
    )


def item(slug: str, result: str, *, url: str = "", reason: str = "", kind=None):
    return DispatchItemResult(slug=slug, result=result, url=url, reason=reason, failure_kind=kind)


def qi(slug: str, *, draft_dir: str = "", query_id: str = "C01") -> QueueItem:
    from datetime import datetime

    return QueueItem(
        slug=slug,
        draft_dir=draft_dir or f"data/drafts/{slug}",
        platform="csdn",
        query_id=query_id,
        queued_at=datetime.fromisoformat("2026-10-26T10:00:00+08:00"),
    )


def write_meta(data_root: Path, slug: str, title: str = "测试标题", *, raw: str | None = None) -> Path:
    d = data_root / "drafts" / slug
    d.mkdir(parents=True, exist_ok=True)
    path = d / "meta.json"
    path.write_text(
        raw if raw is not None else json.dumps({"slug": slug, "title": title}),
        encoding="utf-8",
    )
    return path


def log_with_screenshot(data_root: Path, slug: str, screenshot: str) -> PublishLog:
    from datetime import datetime

    log = PublishLog(data_root / "publish-log.csv")
    log.append(
        LogEntry(
            ts=datetime.fromisoformat(NOW),
            platform="csdn",
            account="default",
            slug=slug,
            idem_key=idem_key("csdn", "default", slug, "sha"),
            status=Status.FAILED,
            screenshot=screenshot,
            note="失败一行",
        )
    )
    return log


def empty_log(data_root: Path) -> PublishLog:
    return PublishLog(data_root / "publish-log.csv")


def render(report: DayReport, data_root: Path, queue=(), log=None) -> str:
    return render_daily_md(
        report, data_root=data_root, queue=list(queue), log=log or empty_log(data_root)
    )


# ---------------------------------------------------------------- UT-19-02 主题行
class TestSubjectLine:
    def test_format_exact(self):
        report = make_report(dispatches=[make_dispatch(planned=2, published=1)])
        assert subject_line(report) == "[JOTO GEO] 10-27 发布 1/2 · csdn"

    def test_platforms_deduped_in_order(self):
        report = make_report(
            dispatches=[make_dispatch("csdn"), make_dispatch("juejin"), make_dispatch("csdn")]
        )
        assert subject_line(report) == "[JOTO GEO] 10-27 发布 3/3 · csdn、juejin"

    def test_no_dispatch_platform_is_none_literal(self):
        assert subject_line(make_report()).endswith("· 无")

    def test_bad_date_raises(self):
        with pytest.raises(ReportError, match="ISO"):
            subject_line(make_report(date_str="10/27"))


# ---------------------------------------------------------------- UT-19-01 四问缺一即失败
class TestFourQuestionsStrict:
    def test_queue_missing_fails(self, tmp_path):
        with pytest.raises(ReportError, match="明天会发什么"):
            render_daily_md(make_report(), data_root=tmp_path, queue=None, log=empty_log(tmp_path))

    def test_log_missing_fails(self, tmp_path):
        with pytest.raises(ReportError, match="截图"):
            render_daily_md(make_report(), data_root=tmp_path, queue=[], log=None)

    def test_bad_date_fails(self, tmp_path):
        with pytest.raises(ReportError, match="ISO"):
            render(make_report(date_str="2026/10/27"), tmp_path)

    def test_published_without_url_fails(self, tmp_path):
        report = make_report(dispatches=[make_dispatch(items=[item("a-1", "published")])])
        with pytest.raises(ReportError, match="没有 URL"):
            render(report, tmp_path)

    def test_published_title_unreadable_fails(self, tmp_path):
        """Q2 的标题是硬数据：meta.json 读不到，宁可渲染失败也不发残报。"""
        report = make_report(
            dispatches=[make_dispatch(items=[item("a-1", "published", url="https://x/1")])]
        )
        with pytest.raises(ReportError, match="读不到 meta.json"):
            render(report, tmp_path)

    def test_published_title_invalid_json_fails(self, tmp_path):
        write_meta(tmp_path, "a-1", raw="{oops")
        report = make_report(
            dispatches=[make_dispatch(items=[item("a-1", "published", url="https://x/1")])]
        )
        with pytest.raises(ReportError, match="合法 JSON"):
            render(report, tmp_path)

    def test_published_title_model_error_fails(self, tmp_path):
        write_meta(tmp_path, "a-1", raw=json.dumps({"slug": "a-1"}))  # 缺 title
        report = make_report(
            dispatches=[make_dispatch(items=[item("a-1", "published", url="https://x/1")])]
        )
        with pytest.raises(ReportError, match="meta.json 不合法"):
            render(report, tmp_path)

    def test_failed_without_reason_fails(self, tmp_path):
        """Q3 的平台原话是硬数据：failed 却没有 reason = 上游把现场弄丢了。"""
        report = make_report(
            dispatches=[make_dispatch(planned=1, published=0, items=[item("a-1", "failed")])]
        )
        with pytest.raises(ReportError, match="缺少失败原因"):
            render(report, tmp_path)


# ---------------------------------------------------------------- 正常渲染（UT-19-05 链接可点）
class TestRenderHappyPath:
    def test_full_report_answers_four_questions(self, tmp_path):
        write_meta(tmp_path, "a-1", "文档解析为什么是 RAG 的第一道工序")
        write_meta(tmp_path, "b-1", "明日预告稿")
        report = make_report(
            dispatches=[make_dispatch(items=[item("a-1", "published", url="https://blog.csdn.net/x/1")])],
            notes=["csdn 10:05 已发车，本次运行跳过（同槽位防重发）"],
        )
        md = render(report, tmp_path, queue=[qi("b-1")])

        assert md.startswith("# GEO 发布日报 · 2026-10-27（周二）")
        assert "## 一、应发/实发：1 / 1" in md
        assert "- csdn（10:00 发车）：发 1/1" in md
        # UT-19-05：URL 是 Markdown 链接，邮件客户端里可直接点开
        assert "- [文档解析为什么是 RAG 的第一道工序](https://blog.csdn.net/x/1) — csdn · `a-1`" in md
        assert "## 三、失败与异常" in md and "今日无失败" in md
        assert "## 四、明日预告（1 篇待发）" in md
        assert "明日预告稿（`b-1`）" in md and "geopublish hold" in md
        assert "## 备注" in md and "同槽位防重发" in md
        assert md.endswith("\n") and not md.endswith("\n\n")

    def test_non_workday_label(self, tmp_path):
        md = render(make_report(workday=False), tmp_path)
        assert "（周二，非工作日）" in md.split("\n")[0]

    def test_error_banner(self, tmp_path):
        md = render(make_report(error="RuntimeError: 磁盘炸了"), tmp_path)
        assert "⚠️ 本次运行异常终止：RuntimeError: 磁盘炸了" in md

    def test_no_dispatch_line(self, tmp_path):
        assert "今日无任何平台发车记录" in render(make_report(), tmp_path)

    def test_dispatch_skipped_reason_line(self, tmp_path):
        d = make_dispatch(planned=0, published=0, skipped="未到发车时间 10:00")
        assert "- csdn（10:00 发车）：未到发车时间 10:00" in render(
            make_report(dispatches=[d]), tmp_path
        )

    def test_queue_item_without_meta_falls_back_to_slug(self, tmp_path):
        md = render(make_report(), tmp_path, queue=[qi("ghost-1")])
        assert "`ghost-1`" in md and "（`ghost-1`）" not in md

    def test_queue_item_title_from_draft_dir(self, tmp_path):
        """队列条目的 draft_dir 指向哪就读哪（不一定在 data_root 下）。"""
        draft = tmp_path / "elsewhere" / "c-1"
        draft.mkdir(parents=True)
        (draft / "meta.json").write_text(
            json.dumps({"slug": "c-1", "title": "外面来的稿"}), encoding="utf-8"
        )
        md = render(make_report(), tmp_path, queue=[qi("c-1", draft_dir=str(draft))])
        assert "外面来的稿（`c-1`）" in md

    def test_queue_item_invalid_meta_degrades_to_slug(self, tmp_path):
        draft = tmp_path / "drafts" / "c-2"
        draft.mkdir(parents=True)
        (draft / "meta.json").write_text("{broken", encoding="utf-8")
        md = render(make_report(), tmp_path, queue=[qi("c-2", draft_dir=str(draft))])
        assert "`c-2`" in md and "外面" not in md

    def test_queue_empty_with_no_topic_reason(self, tmp_path):
        report = make_report(prepare=[PrepareResult(status="no_topic", reason="缺口清单为空")])
        md = render(report, tmp_path, queue=[])
        assert "明日无待发稿件" in md and "no_topic（缺口清单为空）" in md

    def test_queue_empty_prepared_prep_prints_no_reason(self, tmp_path):
        """prepare 成功但队列是空的（边界：入队前进程被杀）——prepared 不打印原因行。"""
        report = make_report(prepare=[PrepareResult(status="prepared", slug="c-1")])
        md = render(report, tmp_path, queue=[])
        assert "明日无待发稿件" in md and "原因" not in md.split("## 四")[1]

    def test_unresolved_block(self, tmp_path):
        report = make_report(
            unresolved=[Unresolved(slug="stuck-1", platform="csdn", note="verify 未确认", since=NOW)]
        )
        md = render(report, tmp_path)
        assert "未解决 needs_human（1 篇" in md and "`stuck-1`" in md and "verify 未确认" in md


# ---------------------------------------------------------------- UT-19-04 零发布日
class TestZeroPublishDay:
    def test_zero_day_banner_with_reasons(self, tmp_path):
        d = make_dispatch(
            planned=1,
            published=0,
            skipped="已达节奏阈值：24h 内已发 1 篇 ≥ daily_limit 1（R14），今日跳过发布",
            items=[item("a-1", "deferred", reason="节奏阈值已满，留到下一窗口")],
        )
        report = make_report(
            dispatches=[d], prepare=[PrepareResult(status="no_topic", reason="缺口清单为空")]
        )
        md = render(report, tmp_path)
        assert "> **今日无发布。** 原因：" in md
        assert "已达节奏阈值" in md and "节奏阈值已满" in md and "缺口清单为空" in md
        assert "## 二、发布明细（0 篇）" in md and "今日无成功发布" in md

    def test_zero_reasons_empty_defaults(self):
        assert zero_reasons(make_report()) == [
            "发车时队列无可发稿件（两阶段：今日 prepare 的稿明日才发）"
        ]

    def test_zero_reasons_collects_and_dedups(self):
        d1 = make_dispatch("csdn", planned=0, published=0, skipped="同一句原因")
        d2 = make_dispatch("csdn", planned=0, published=0, skipped="同一句原因")
        d1.items.append(item("a-1", "already_published", reason="不该出现"))
        d1.items.append(item("a-2", "published", url="https://x/1"))
        d2.items.append(item("b-1", "failed", reason="平台拒了"))
        report = make_report(
            dispatches=[d1, d2],
            prepare=[PrepareResult(status="prepared", slug="c-1"),
                     PrepareResult(status="failed", reason="成稿炸了")],
            notes=["备注一条"],
            error="RuntimeError: 崩了",
        )
        reasons = zero_reasons(report)
        assert reasons[0] == "运行异常：RuntimeError: 崩了"
        assert reasons.count("csdn：同一句原因") == 1, "同样的原因不重复列"
        assert "csdn 的 b-1：平台拒了" in reasons
        assert "选题/成稿 failed：成稿炸了" in reasons
        assert "备注一条" in reasons
        assert not any("不该出现" in r for r in reasons), "终态条目不算零发布原因"


# ---------------------------------------------------------------- Q3 失败明细
class TestFailureDetails:
    def test_failure_with_step_reason_screenshot(self, tmp_path):
        log = log_with_screenshot(tmp_path, "a-1", "data/incidents/x/screenshot.png")
        report = make_report(
            dispatches=[
                make_dispatch(
                    planned=1,
                    published=0,
                    items=[item("a-1", "failed", reason="平台原话：含营销内容", kind="CONTENT_REJECTED")],
                )
            ]
        )
        md = render(report, tmp_path, log=log)
        assert "结果 **failed**" in md
        assert "失败在哪一步：CONTENT_REJECTED" in md
        assert "平台原话：含营销内容" in md
        assert "截图：data/incidents/x/screenshot.png" in md

    def test_failure_without_kind_and_screenshot(self, tmp_path):
        report = make_report(
            dispatches=[
                make_dispatch(
                    planned=1, published=0, items=[item("a-1", "failed", reason="GateClosed: 闸门未开")]
                )
            ]
        )
        md = render(report, tmp_path)
        assert "失败在哪一步：运行期异常" in md
        assert "无截图（publish-log 无记录" in md

    def test_terminal_and_skipped_items_not_in_failures(self, tmp_path):
        d = make_dispatch(
            planned=0,
            published=0,
            items=[
                item("a-1", "already_published", reason="终态出队"),
                item("a-2", "skipped_duplicate", reason="幂等命中", url="https://x/1"),
                item("a-3", "deferred", reason="节奏阈值已满"),
            ],
        )
        md = render(make_report(dispatches=[d]), tmp_path)
        section = md.split("## 三、失败与异常")[1]
        assert "a-1" not in section and "a-2" not in section
        assert "a-3" in section, "deferred 是「没发出去」，要进失败与异常"

    def test_needs_human_item_in_failures(self, tmp_path):
        d = make_dispatch(
            planned=0,
            published=0,
            items=[item("stuck-1", "needs_human", reason="待人工处理，不自动重试（R9/R13）")],
        )
        md = render(make_report(dispatches=[d]), tmp_path)
        assert "结果 **needs_human**" in md

    def test_latest_screenshot_wins(self, tmp_path):
        from datetime import datetime

        log = log_with_screenshot(tmp_path, "a-1", "第一张.png")
        log.append(
            LogEntry(
                ts=datetime.fromisoformat("2026-10-27T11:00:00+08:00"),
                platform="csdn",
                account="default",
                slug="a-1",
                idem_key=idem_key("csdn", "default", "a-1", "sha"),
                status=Status.FAILED,
                screenshot="第二张.png",
                note="又失败一次",
            )
        )
        log.append(
            LogEntry(
                ts=datetime.fromisoformat("2026-10-27T12:00:00+08:00"),
                platform="csdn",
                account="default",
                slug="a-1",
                idem_key=idem_key("csdn", "default", "a-1", "sha"),
                status=Status.FAILED,
                screenshot="",  # 最新一行没截图：取最近一张有图的
                note="第三次",
            )
        )
        d = make_dispatch(planned=0, published=0, items=[item("a-1", "failed", reason="又挂了")])
        md = render(make_report(dispatches=[d]), tmp_path, log=log)
        assert "截图：第二张.png" in md


# ---------------------------------------------------------------- md 落盘
class TestWriteDailyMd:
    def test_writes_atomically(self, tmp_path):
        path = write_daily_md(tmp_path, DAY, "# 日报\n")
        assert path == tmp_path / "reports" / f"{DAY}.md"
        assert path.read_text(encoding="utf-8") == "# 日报\n"

    def test_accepts_date_object(self, tmp_path):
        path = write_daily_md(tmp_path, date(2026, 10, 27), "x\n")
        assert path.name == f"{DAY}.md"

    def test_replace_failure_cleans_tmp(self, tmp_path, monkeypatch):
        import geopublish.report.daily as daily_mod

        def boom(*args, **kwargs):
            raise OSError("磁盘满")

        monkeypatch.setattr(daily_mod.os, "replace", boom)
        with pytest.raises(OSError, match="磁盘满"):
            write_daily_md(tmp_path, DAY, "x\n")
        parent = tmp_path / "reports"
        assert [p.name for p in parent.iterdir()] == [], "不留 .tmp 残骸"


# ---------------------------------------------------------------- 零发布连击（UT-19-07 的依据）
def _save(data_root: Path, day: str, *, published: int, workday: bool = True, dispatched: bool = True):
    report = make_report(
        date_str=day,
        workday=workday,
        dispatches=[make_dispatch(planned=1, published=published)] if dispatched else [],
    )
    save_report(data_root, report, FakeClock(f"{day}T23:00:00+08:00"))


class TestZeroPublishStreak:
    def test_published_today_is_zero(self, tmp_path):
        _save(tmp_path, DAY, published=1)
        assert zero_publish_streak(tmp_path, date(2026, 10, 27)) == 0

    def test_two_consecutive_zero_days(self, tmp_path):
        _save(tmp_path, "2026-10-27", published=0)
        _save(tmp_path, "2026-10-26", published=0)
        _save(tmp_path, "2026-10-25", published=1)
        assert zero_publish_streak(tmp_path, date(2026, 10, 27)) == 2

    def test_missing_report_stops_streak(self, tmp_path):
        """日报缺失由心跳告警负责，连击只数有日报的零发布日。"""
        _save(tmp_path, "2026-10-27", published=0)
        _save(tmp_path, "2026-10-25", published=0)  # 10-26 缺失
        assert zero_publish_streak(tmp_path, date(2026, 10, 27)) == 1

    def test_workdays_only_weekend_not_counted(self, tmp_path):
        """workdays_only 的周末（无发车记录）是配置让停的，不算「发了没发出去」。"""
        _save(tmp_path, "2026-10-26", published=0)  # 周一 0
        _save(tmp_path, "2026-10-25", published=0, workday=False, dispatched=False)  # 周日停发
        _save(tmp_path, "2026-10-24", published=0, workday=False, dispatched=False)  # 周六停发
        _save(tmp_path, "2026-10-23", published=0)  # 周五 0
        _save(tmp_path, "2026-10-22", published=1)
        assert zero_publish_streak(tmp_path, date(2026, 10, 26)) == 2

    def test_weekend_with_dispatch_counts(self, tmp_path):
        """workdays_only=false 的周末是正常发车日，零发布照数。"""
        _save(tmp_path, "2026-10-25", published=0, workday=False, dispatched=True)
        _save(tmp_path, "2026-10-24", published=0, workday=False, dispatched=True)
        assert zero_publish_streak(tmp_path, date(2026, 10, 25)) == 2

    def test_report_json_roundtrip_for_streak(self, tmp_path):
        """连击读的是落盘 JSON（load_report），不是内存对象。"""
        _save(tmp_path, DAY, published=0)
        loaded = load_report(tmp_path, DAY)
        assert loaded is not None and loaded.published == 0
