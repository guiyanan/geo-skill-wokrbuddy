"""IT-18-01（补 G4）：注入假时钟快进 5 个工作日，验证 R16 的 P3 核心验收。

断言：
- 产出 5 份日报（data/reports/<date>.json），每份「应发/实发」字段完整
- 第 3 天注入一次平台拒绝，当日日报必须体现（不静默失败）
- 被拒的稿子卡在 needs_human：第 4 天日报的未解决项里有它，且不自动重试
- 两阶段成立：当天 prepare 入队的稿子，次日才出现在发布阶段

无浏览器无网络：发布/选题都是注入的替身，但状态流转走真实的
publish-log.csv、queue.json、日报文件 —— 被测的是调度编排本身。
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.config import Config
from geopublish.core.errors import FailureKind
from geopublish.core.models import LogEntry
from geopublish.core.state import PublishLog, Status, idem_key
from geopublish.publish import PublishOutcome
from geopublish.schedule.scheduler import PrepareResult, QueueItem, run_daily

# 2026-10-26（周一）~ 2026-10-30（周五）
WORKDAYS = [f"2026-10-{d}T10:05:00+08:00" for d in range(26, 31)]
REJECT_DAY = 3  # 第 3 个工作日（10-28）注入平台拒绝


@pytest.fixture
def cfg(tmp_path) -> Config:
    return Config.from_dict(
        {
            "gate": "publish",
            "platforms": [{"name": "csdn", "publish_at": "10:00", "daily_limit": 1}],
            "report": {"email": "t@example.com"},
            "data_dir": str(tmp_path),
        }
    )


def _log_row(data_dir: Path, *, ts: datetime, slug: str, status: Status, url: str = "", note: str = "") -> None:
    PublishLog(data_dir / "publish-log.csv").append(
        LogEntry(
            ts=ts,
            platform="csdn",
            account="default",
            slug=slug,
            idem_key=idem_key("csdn", "default", slug, f"sha-{slug}"),
            status=status,
            url=url,
            note=note,
        )
    )


class SimulatedPlatform:
    """假平台：正常日子发成功并落 published 行；REJECT_DAY 拒稿并落 needs_human 行。

    写日志行是模拟真实 publish_draft 的副作用（T-10/T-12 的行为），
    这样第 4 天的 needs_human 扫描、每天的节奏计数走的都是真实口径。
    """

    def __init__(self, data_dir: Path, clock: FakeClock):
        self.data_dir = data_dir
        self.clock = clock
        self.attempts: list[str] = []

    def __call__(self, item: QueueItem, platform_cfg) -> PublishOutcome:
        self.attempts.append(item.slug)
        now = self.clock.now()
        day_index = now.day - 26 + 1  # 10-26 → 第 1 天
        if day_index == REJECT_DAY:
            _log_row(
                self.data_dir, ts=now, slug=item.slug, status=Status.NEEDS_HUMAN,
                note="failure_kind=CONTENT_REJECTED；stage=submit；平台原话：含有营销推广内容，审核未通过",
            )
            return PublishOutcome(
                ok=False,
                reason="failure_kind=CONTENT_REJECTED；平台原话：含有营销推广内容，审核未通过",
                failure_kind=FailureKind.CONTENT_REJECTED,
            )
        url = f"https://blog.csdn.net/joto/article/details/{1000 + day_index}"
        _log_row(self.data_dir, ts=now, slug=item.slug, status=Status.PUBLISHED, url=url)
        return PublishOutcome(ok=True, url=url, article_id=str(1000 + day_index))


class SimulatedPrepare:
    """假 prepare：每天选中一个选题并「成稿」入队（slug 按天递增）。"""

    def __init__(self):
        self.counter = 0

    def __call__(self) -> PrepareResult:
        self.counter += 1
        return PrepareResult(
            status="prepared",
            slug=f"topic-{self.counter}",
            query_id=f"C{self.counter:02d}",
            draft_dir=f"data/drafts/topic-{self.counter}",
            platform="csdn",
        )


@pytest.mark.integration
def test_five_workdays_unattended(cfg, tmp_path):
    clock = FakeClock(WORKDAYS[0])
    platform = SimulatedPlatform(tmp_path, clock)
    prepare = SimulatedPrepare()

    for day_iso in WORKDAYS:
        clock.set(day_iso)
        run_daily(cfg, clock=clock, publish_one=platform, prepare=prepare)

    reports = {
        day: json.loads((tmp_path / "reports" / f"2026-10-{day}.json").read_text(encoding="utf-8"))
        for day in range(26, 31)
    }

    # ① 5 个工作日，每天一份日报，「应发/实发」字段完整
    assert len(reports) == 5
    for day, report in reports.items():
        assert report["workday"] is True
        assert isinstance(report["expected"], int), f"10-{day} 日报缺应发"
        assert isinstance(report["published"], int), f"10-{day} 日报缺实发"
        assert report["error"] is None, f"10-{day} 日报带异常：{report['error']}"

    # ② 正常日子应发 1 实发 1（前一天的 prepare 今日发车）
    for day in (27, 28, 29, 30):
        assert reports[day]["expected"] == 1, f"10-{day} 应发应为 1"
    assert reports[26]["expected"] == 0, "首日队列尚空，应发 0"
    assert reports[27]["published"] == 1

    # ③ 第 3 天平台拒绝：当日日报必须体现（不静默失败）
    day3 = reports[26 + REJECT_DAY - 1]
    assert day3["published"] == 0
    (dispatch,) = day3["dispatches"]
    (failed_item,) = [i for i in dispatch["items"] if i["result"] == "failed"]
    assert failed_item["failure_kind"] == "CONTENT_REJECTED"
    assert "平台原话" in failed_item["reason"]

    # ④ 被拒稿子卡在 needs_human：第 4 天日报标注未解决，且不自动重试
    day4 = reports[29]
    assert [u["slug"] for u in day4["unresolved"]] == [f"topic-{REJECT_DAY - 1}"]
    assert platform.attempts.count(f"topic-{REJECT_DAY - 1}") == 1, "needs_human 不许自动重试"

    # ⑤ 第 4、5 天照常发车（前一天的异常不阻塞后续工作日）
    assert reports[29]["published"] == 1
    assert reports[30]["published"] == 1

    # ⑥ 两阶段：prepare 共跑了 5 次，但第 5 天入队的 topic-5 从未进过发布尝试
    assert prepare.counter == 5
    assert "topic-5" not in platform.attempts
