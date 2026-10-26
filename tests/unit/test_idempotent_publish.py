"""T-11 幂等与并发锁（geopublish/publish.py 的状态机接入）。

钉死的行为（PRD R9 + 工单 UT-11-01/03）：
- 同稿连跑两次：第二次 SKIPPED_DUPLICATE，浏览器不开（open_page 没被调用）
- submit 之前必先落 submitting 行 —— 这是崩溃恢复的唯一锚点
- submitting 无 URL 的残留 → 重跑转 needs_human 落盘并拒发，绝不自动补发
- submitting 有 URL 的残留 → 其实已经发出去了，补记 published 后按重复跳过
- verify 没过 → needs_human（不是 failed：「失败重试」在发布场景 = 可能发两遍）
- failed / rejected / needs_human 三种历史的处置各不相同，一行都不能认错
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

from geopublish.adapters.base import (
    AuthResult,
    AuthState,
    SubmitMode,
    SubmitResult,
    SubmitState,
    VerifyResult,
)
from geopublish.core.clock import FakeClock
from geopublish.core.config import Gate, PlatformConfig
from geopublish.core.lock import LockError, platform_lock
from geopublish.core.models import LogEntry
from geopublish.core.state import PublishLog, Status
from geopublish.publish import LoadedDraft, load_draft, publish_draft

pytestmark = pytest.mark.unit

META = {"slug": "t11-idem", "title": "幂等探针", "ai_involvement": "assisted"}
PUBLISHED_URL = "https://blog.csdn.net/probe/article/details/168800001"
CLOCK = "2026-10-26T10:00:00+08:00"


# --------------------------------------------------------------- 假件

class RecordingAdapter:
    """和 test_publish 同款的最小假 adapter：verify 结果可编排，submit 可装死。"""

    name = "fake"
    editor_url = "https://mp.csdn.net/mp_blog/creation/editor"

    def __init__(self, *, verify_ok: bool = True, submit_error: Exception | None = None):
        self.calls: list[object] = []
        self._verify_ok = verify_ok
        self._submit_error = submit_error

    def wait_for_auth_signal(self, page, timeout_ms: int = 30_000):
        self.calls.append("wait_for_auth_signal")

    def check_auth(self, page):
        self.calls.append("check_auth")
        return AuthResult(state=AuthState.LOGGED_IN, reason="探针")

    def open_editor(self, page, draft):
        self.calls.append("open_editor")

    def fill_content(self, page, draft):
        self.calls.append("fill_content")

    def upload_cover(self, page, path):
        self.calls.append("upload_cover")

    def fill_meta(self, page, draft, limits, *, mode=SubmitMode.DRAFT):
        self.calls.append(("fill_meta", SubmitMode(mode).value))

    def submit(self, page, *, mode=SubmitMode.DRAFT):
        self.calls.append(("submit", SubmitMode(mode).value))
        if self._submit_error is not None:
            raise self._submit_error
        return SubmitResult(state=SubmitState.SUBMITTED, reason="看到了提示")

    def verify(self, page, *, mode=SubmitMode.DRAFT):
        self.calls.append(("verify", SubmitMode(mode).value))
        if self._verify_ok:
            return VerifyResult(ok=True, url=PUBLISHED_URL, article_id="168800001", reason="命中")
        return VerifyResult(ok=False, url="", reason="详情页信号没出现")


class StubPage:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def goto(self, url: str) -> None:
        self.calls.append(("goto", url))


def _opener(log: list[str]):
    @contextmanager
    def open_page():
        log.append("open_page")
        yield StubPage()

    return open_page


def _loaded(tmp_path: Path, *, body: str = "正文" * 100) -> LoadedDraft:
    root = tmp_path / "draft"
    if not root.exists():  # 沙箱对 exist_ok=True 的重复 mkdir 会报 EEXIST，绕行
        root.mkdir()
    (root / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (root / "csdn.md").write_text(body, encoding="utf-8")
    return load_draft(root)


def _cfg() -> PlatformConfig:
    return PlatformConfig(name="csdn")


def _run_publish(tmp_path: Path, adapter, opened: list[str], **kwargs):
    data_dir = kwargs.pop("data_dir", tmp_path / "data")
    return publish_draft(
        adapter,
        _loaded(tmp_path, body=kwargs.pop("body", "正文" * 100)),
        _cfg(),
        gate=Gate.PUBLISH,
        open_page=_opener(opened),
        mode="publish",
        data_dir=data_dir,
        clock=FakeClock(CLOCK),
        **kwargs,
    )


def _rows(data_dir: Path):
    log = PublishLog(data_dir / "publish-log.csv")
    return log.entries()


def _append_history(data_dir: Path, tmp_path: Path, status: Status, **fields) -> None:
    """手工铺一行历史（模拟上一次运行留下的现场）。"""
    draft = _loaded(tmp_path).draft
    log = PublishLog(data_dir / "publish-log.csv")
    log.append(
        LogEntry(
            ts=FakeClock(CLOCK).now(),
            platform="csdn",
            account="default",
            slug=draft.meta.slug,
            idem_key=draft.idem_key("csdn", "default"),
            status=status,
            **fields,
        )
    )


# --------------------------------------------------------------- UT-11-01：重复跳过

def test_second_run_of_same_draft_is_skipped_without_browser(tmp_path):
    """UT-11-01：同稿连跑两次，第二次 SKIPPED_DUPLICATE，浏览器未启动。"""
    data_dir = tmp_path / "data"
    first_opened: list[str] = []
    outcome1 = _run_publish(tmp_path, RecordingAdapter(), first_opened, data_dir=data_dir)
    assert outcome1.ok is True and first_opened == ["open_page"]

    second_opened: list[str] = []
    outcome2 = _run_publish(tmp_path, RecordingAdapter(), second_opened, data_dir=data_dir)

    assert outcome2.skipped_duplicate is True
    assert outcome2.reason == "SKIPPED_DUPLICATE"
    assert outcome2.ok is True, "重复跳过不是失败"
    assert outcome2.url == PUBLISHED_URL and outcome2.article_id == "168800001"
    assert second_opened == [], "幂等命中的运行连浏览器都不许开"


def test_successful_publish_writes_submitting_then_published(tmp_path):
    """R9 铁律落地：submitting 行在 published 行之前，且 submitting 行没有 URL。

    submitting 是「点发布之前」落盘的 —— 行里不可能有 URL；有 URL 的 submitting
    只可能来自恢复补记（见下方崩溃恢复用例），正常链路里它是裸锚点。
    """
    data_dir = tmp_path / "data"
    _run_publish(tmp_path, RecordingAdapter(), [], data_dir=data_dir)

    rows = _rows(data_dir)
    assert [r.status for r in rows] == [Status.SUBMITTING, Status.PUBLISHED]
    assert rows[0].url == "" and rows[0].article_id == ""
    assert rows[1].url == PUBLISHED_URL
    assert all(r.slug == "t11-idem" for r in rows)


def test_submitting_row_is_on_disk_before_submit_is_called(tmp_path):
    """顺序不能只靠结果推断：submit 被调用的那一刻，submitting 行必须已在盘上。"""
    data_dir = tmp_path / "data"
    observed: list[list[Status]] = []
    adapter = RecordingAdapter()
    original_submit = adapter.submit

    def submit_which_reads_the_log(page, *, mode=SubmitMode.DRAFT):
        if (data_dir / "publish-log.csv").exists():
            observed.append([e.status for e in _rows(data_dir)])
        else:
            observed.append([])
        return original_submit(page, mode=mode)

    adapter.submit = submit_which_reads_the_log  # type: ignore[method-assign]
    _run_publish(tmp_path, adapter, [], data_dir=data_dir)

    assert observed == [[Status.SUBMITTING]], (
        f"submit 时盘上必须有 submitting 锚点，实际：{observed}"
    )


def test_changed_body_is_a_new_idem_key_and_publishes_again(tmp_path):
    """同 slug 但正文改过 = 另一篇（幂等键含 content_sha）：不许被误杀。"""
    data_dir = tmp_path / "data"
    _run_publish(tmp_path, RecordingAdapter(), [], data_dir=data_dir, body="旧正文" * 100)

    opened: list[str] = []
    outcome = _run_publish(
        tmp_path, RecordingAdapter(), opened, data_dir=data_dir, body="新正文" * 100
    )

    assert outcome.ok is True and outcome.skipped_duplicate is False
    assert opened == ["open_page"], "改过的稿子必须真的再发一遍"


def test_other_slug_or_platform_does_not_trigger_dedup(tmp_path):
    """幂等匹配是精确的：别的 slug / 别的平台的历史不能误伤本篇。"""
    data_dir = tmp_path / "data"
    draft = _loaded(tmp_path).draft
    log = PublishLog(data_dir / "publish-log.csv")
    ts = FakeClock(CLOCK).now()
    log.append(LogEntry(ts=ts, platform="csdn", account="default", slug="other-post",
                        idem_key=draft.idem_key("csdn", "default"), status=Status.PUBLISHED,
                        url=PUBLISHED_URL))
    log.append(LogEntry(ts=ts, platform="juejin", account="default", slug=draft.meta.slug,
                        idem_key=draft.idem_key("juejin", "default"), status=Status.PUBLISHED,
                        url="https://juejin.cn/post/1"))

    opened: list[str] = []
    outcome = _run_publish(tmp_path, RecordingAdapter(), opened, data_dir=data_dir)
    assert outcome.ok is True and outcome.skipped_duplicate is False
    assert opened == ["open_page"]


# --------------------------------------------------------------- UT-11-03：崩溃恢复

def test_crash_after_submitting_marker_rerun_goes_needs_human(tmp_path):
    """UT-11-03 全链路：submit 瞬间炸死（盘上留 submitting 锚点）→ 重跑 needs_human。

    第一跑：submit 抛错，异常原样传播；盘上必须恰好一行 submitting。
    第二跑：不开浏览器，needs_human 落盘，outcome 明说拒发。
    """
    data_dir = tmp_path / "data"
    with pytest.raises(RuntimeError, match="提交瞬间断电"):
        _run_publish(
            tmp_path, RecordingAdapter(submit_error=RuntimeError("提交瞬间断电")), [],
            data_dir=data_dir,
        )
    assert [r.status for r in _rows(data_dir)] == [Status.SUBMITTING]

    opened: list[str] = []
    outcome = _run_publish(tmp_path, RecordingAdapter(), opened, data_dir=data_dir)

    assert outcome.ok is False
    assert "needs_human" in outcome.reason
    assert opened == [], "submitting 未决的运行不许开浏览器"
    statuses = [r.status for r in _rows(data_dir)]
    assert statuses == [Status.SUBMITTING, Status.NEEDS_HUMAN], "迁移必须落盘"
    assert "提交结果未知" in _rows(data_dir)[-1].note


def test_needs_human_history_refuses_to_run_again(tmp_path):
    """needs_human 只能由人转回 pending：历史停在这里 = 一直拒发，不开浏览器。"""
    data_dir = tmp_path / "data"
    _append_history(data_dir, tmp_path, Status.NEEDS_HUMAN)

    opened: list[str] = []
    outcome = _run_publish(tmp_path, RecordingAdapter(), opened, data_dir=data_dir)

    assert outcome.ok is False and "needs_human" in outcome.reason
    assert opened == []
    # 不写重复行：needs_human 已经落过盘，重跑不该让日志越长越脏
    assert [r.status for r in _rows(data_dir)] == [Status.NEEDS_HUMAN]


def test_submitting_with_url_recovers_to_published_and_skips(tmp_path):
    """死在提交瞬间但其实已发出（submitting 带 URL）→ 补记 published，按重复跳过。"""
    data_dir = tmp_path / "data"
    _append_history(
        data_dir, tmp_path, Status.SUBMITTING,
        url=PUBLISHED_URL, article_id="168800001",
    )

    opened: list[str] = []
    outcome = _run_publish(tmp_path, RecordingAdapter(), opened, data_dir=data_dir)

    assert outcome.skipped_duplicate is True and outcome.url == PUBLISHED_URL
    assert opened == []
    statuses = [r.status for r in _rows(data_dir)]
    assert statuses == [Status.SUBMITTING, Status.PUBLISHED], "补记 published 收敛日志"


def test_failed_history_is_retryable(tmp_path):
    """failed（submit 之前死的，稿子没碰平台）→ 放行重试，且状态机走 failed→submitting。"""
    data_dir = tmp_path / "data"
    _append_history(data_dir, tmp_path, Status.FAILED)

    opened: list[str] = []
    outcome = _run_publish(tmp_path, RecordingAdapter(), opened, data_dir=data_dir)

    assert outcome.ok is True and opened == ["open_page"]
    statuses = [r.status for r in _rows(data_dir)]
    assert statuses == [Status.FAILED, Status.SUBMITTING, Status.PUBLISHED]


def test_rejected_history_never_republishes(tmp_path):
    """rejected（R4 自检拦下的）：自动链路不许复活它，重新自检前一律拒发。"""
    data_dir = tmp_path / "data"
    _append_history(data_dir, tmp_path, Status.REJECTED)

    opened: list[str] = []
    outcome = _run_publish(tmp_path, RecordingAdapter(), opened, data_dir=data_dir)

    assert outcome.ok is False and "rejected" in outcome.reason
    assert opened == []


# --------------------------------------------------------------- verify 未过的处置

def test_verify_failure_marks_needs_human_not_failed(tmp_path, caplog):
    """verify 判不了成功 ≠ 没发出去。标 failed 会诱导重发 —— 只能 needs_human。"""
    data_dir = tmp_path / "data"
    outcome = _run_publish(
        tmp_path, RecordingAdapter(verify_ok=False), [], data_dir=data_dir
    )

    assert outcome.ok is False
    rows = _rows(data_dir)
    assert [r.status for r in rows] == [Status.SUBMITTING, Status.NEEDS_HUMAN]
    assert "详情页信号没出现" in rows[-1].note

    # 紧接着再跑：needs_human 历史拒发，不开浏览器（不重发）
    opened: list[str] = []
    outcome2 = _run_publish(tmp_path, RecordingAdapter(), opened, data_dir=data_dir)
    assert outcome2.ok is False and opened == []


# --------------------------------------------------------------- 锁与编排层的接缝

def test_lock_contention_raises_before_browser_opens(tmp_path):
    """UT-11-02 编排层：锁被占用 → LockError 在 open_page 之前抛，浏览器不开。"""
    opened: list[str] = []
    with platform_lock("csdn", lock_root=tmp_path / "locks"):
        with pytest.raises(LockError):
            _run_publish(
                tmp_path, RecordingAdapter(), opened,
                data_dir=tmp_path / "data", lock_root=tmp_path / "locks",
            )
    assert opened == []


def test_lock_is_released_after_run_so_second_run_enters(tmp_path):
    """UT-11-05 编排层：带锁跑完一遍，第二把锁拿得到（锁没漏）。"""
    lock_root = tmp_path / "locks"
    _run_publish(tmp_path, RecordingAdapter(), [], lock_root=lock_root)

    with platform_lock("csdn", lock_root=lock_root):  # 拿不到会抛 LockError
        pass


def test_draft_mode_takes_the_lock_but_writes_no_log(tmp_path):
    """draft 模式：锁照样拿（同一平台不许双开浏览器），但不写日志。

    状态机没有 drafted 态（加状态 = 改幂等状态机，PRD §9.5 要先评审），
    草稿重复是已接受的低危缺口。
    """
    data_dir = tmp_path / "data"
    outcome = publish_draft(
        RecordingAdapter(),
        _loaded(tmp_path),
        _cfg(),
        gate=Gate.DRAFT,
        open_page=_opener([]),
        mode="draft",
        data_dir=data_dir,
        clock=FakeClock(CLOCK),
        lock_root=tmp_path / "locks",
    )

    assert outcome.ok is True
    assert not (data_dir / "publish-log.csv").exists(), "draft 模式不许碰发布日志"
