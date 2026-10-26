"""T-10 回执落盘（core/receipt.py + Receipt/LogEntry 模型）。

钉死的行为（实施计划 UT-10-01~04）：
- UT-10-01 回执含 url/article_id/published_at/screenshot 四字段
- UT-10-02 publish-log.csv 追加一行且列顺序固定（LOG_COLUMNS 是契约）
- UT-10-03 回执写入失败 → 不掩盖发布成功，csv 那行 note 记 receipt_failed
- UT-10-04 时间字段用注入时钟（FakeClock），receipt.py 自己不读系统时间
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.models import LOG_COLUMNS, LogEntry, Receipt
from geopublish.core.receipt import RECEIPT_FAILED, save_receipt
from geopublish.core.state import PublishLog, Status

pytestmark = pytest.mark.unit

CLOCK = FakeClock("2026-10-22T10:00:00+08:00")


def _receipt() -> Receipt:
    return Receipt(
        url="https://blog.csdn.net/probe/article/details/42",
        article_id="42",
        published_at=CLOCK.now(),
        screenshot="data/runs/20261022_100000/after.png",
        content_sha="0" * 64,
    )


def _entry() -> LogEntry:
    return LogEntry(
        ts=CLOCK.now(),
        platform="csdn",
        account="probe",
        slug="t10-probe",
        idem_key="f" * 64,
        status=Status.PUBLISHED,
        url="https://blog.csdn.net/probe/article/details/42",
        article_id="42",
        screenshot="data/runs/20261022_100000/after.png",
    )


# ----------------------------------------------------------- UT-10-01

def test_ut_10_01_receipt_json_has_the_four_required_fields(tmp_path: Path):
    record = save_receipt(
        _receipt(), tmp_path / "run", PublishLog(tmp_path / "publish-log.csv"), _entry()
    )

    data = json.loads((tmp_path / "run" / "receipt.json").read_text(encoding="utf-8"))
    for key in ("url", "article_id", "published_at", "screenshot"):
        assert key in data, f"receipt.json 缺 {key}"
    assert data["article_id"] == "42"
    assert record.receipt_path == tmp_path / "run" / "receipt.json"
    assert record.failed is False


# ----------------------------------------------------------- UT-10-02

def test_ut_10_02_csv_appends_one_row_with_fixed_column_order(tmp_path: Path):
    log = PublishLog(tmp_path / "publish-log.csv")
    save_receipt(_receipt(), tmp_path / "r1", log, _entry())
    save_receipt(_receipt(), tmp_path / "r2", log, _entry())

    lines = (tmp_path / "publish-log.csv").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3, "表头只写一次，两行数据各追一行"
    assert lines[0].split(",") == list(LOG_COLUMNS), "列顺序是契约，不许变"
    assert lines[1].split(",")[0] == "2026-10-22T10:00:00+08:00"
    assert lines[2].split(",")[5] == "published"


# ----------------------------------------------------------- UT-10-03

def test_ut_10_03_receipt_write_failure_does_not_mask_publish_success(tmp_path: Path):
    """磁盘满模拟：run_dir 的位置被一个**文件**占住，receipt.json 写不进去。

    发布状态不变（record 不抛、note 标记 receipt_failed），csv 照常追加一行——
    「存证失败」和「没发成」是两种处置，混起来会让人去平台重发一篇。
    """
    blocker = tmp_path / "run"
    blocker.write_text("我不是目录", encoding="utf-8")  # run_dir/receipt.json 必然 OSError
    log = PublishLog(tmp_path / "publish-log.csv")

    record = save_receipt(_receipt(), blocker, log, _entry())

    assert record.receipt_path is None
    assert record.failed is True
    assert record.note == RECEIPT_FAILED
    entries = log.entries()
    assert len(entries) == 1, "回执写不进也要留日志行——平台侧那篇是真的发出去了"
    assert entries[0].note == RECEIPT_FAILED
    assert entries[0].status is Status.PUBLISHED, "发布成功状态不许被回执失败改写"


# ----------------------------------------------------------- UT-10-04

def test_ut_10_04_time_fields_come_from_the_injected_clock(tmp_path: Path):
    other = FakeClock("2031-01-02T03:04:05+08:00")
    receipt = Receipt(
        url="https://blog.csdn.net/probe/article/details/43",
        article_id="43",
        published_at=other.now(),
        screenshot="",
        content_sha="1" * 64,
    )
    entry = LogEntry(
        ts=other.now(), platform="csdn", account="probe", slug="t10-probe",
        idem_key="e" * 64, status=Status.PUBLISHED, url=receipt.url, article_id="43",
    )

    save_receipt(receipt, tmp_path / "run", PublishLog(tmp_path / "publish-log.csv"), entry)

    data = json.loads((tmp_path / "run" / "receipt.json").read_text(encoding="utf-8"))
    assert data["published_at"] == "2031-01-02T03:04:05+08:00"
    row = PublishLog(tmp_path / "publish-log.csv").entries()[0]
    assert row.ts.isoformat() == "2031-01-02T03:04:05+08:00"


def test_receipt_module_has_no_system_clock_dependency():
    """receipt.py 里不许出现 datetime.now(——时间由调用方注入（UT-10-04 的元测试）。"""
    import inspect

    from geopublish.core import receipt

    source = inspect.getsource(receipt)
    assert "datetime.now(" not in source
    assert "time.time(" not in source
