"""T-12 失败分类与红线（core/errors.py + publish.py 的失败处置接入）。

钉死的行为（对应实现计划 UT-12-01~05、07；UT-12-06 元测试在 test_project_layout.py）：

- UT-12-01 人机校验 → 判定 CAPTCHA，needs_human，**零次重试**（浏览器只开一次）
- UT-12-02 跳登录页 → 判定 AUTH_EXPIRED，原样抛出（CLI 退 2），**不落 failed**
- UT-12-03 平台拒稿 → CONTENT_REJECTED，原话回写（日志 note + 稿件目录 platform-rejection.txt）
- UT-12-04 网络超时 → 2/4/8s 退避重试 3 次后 failed（假时钟断言间隔）
- UT-12-05 同一篇累计 failed ≥ 3 → 强制 needs_human，浏览器都不开（全局上限）
- UT-12-07 提交后失败 → 绝不重试（即使类别可重试），needs_human + 残留检查：
  有残留标 failed_residue，无残留明说，查不了就说没查成
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path

import pytest

from geopublish.adapters.base import (
    AuthResult,
    AuthState,
    ContentRejected,
    SubmitMode,
    SubmitResult,
    SubmitState,
    VerifyResult,
)
from geopublish.core.clock import FakeClock
from geopublish.core.config import Gate, PlatformConfig
from geopublish.core.errors import (
    RETRY_HARD_CAP,
    RETRY_SCHEDULE_S,
    FailureKind,
    backoff_delays,
    classify,
)
from geopublish.core.models import LogEntry
from geopublish.core.session import AuthRequired
from geopublish.core.state import PublishLog, Status
from geopublish.publish import LoadedDraft, load_draft, publish_draft

pytestmark = pytest.mark.unit

META = {"slug": "t12-errors", "title": "失败分类探针", "ai_involvement": "assisted"}
CLOCK = "2026-10-26T16:00:00+08:00"
PUBLISHED_URL = "https://blog.csdn.net/probe/article/details/168800002"


# --------------------------------------------------------------- 假件

class FaultyAdapter:
    """可编排故障位置的最小假 adapter。

    - auth_result：check_auth 的返回值（默认已登录）
    - raise_at / error：在哪个阶段抛什么（"fill_content"=提交前；"submit"=提交后）
    - fail_times：只在前 N 次调用时抛，之后恢复正常（测「重试后成功」）
    - residue_id：草稿模式的 verify（残留探针用）是否查到残留
    """

    name = "fake"
    editor_url = "https://mp.csdn.net/mp_blog/creation/editor"

    def __init__(
        self,
        *,
        auth_result: AuthResult | None = None,
        raise_at: str | None = None,
        error: Exception | None = None,
        fail_times: int | None = None,
        residue_id: str = "",
    ):
        self.calls: list[object] = []
        self._auth_result = auth_result
        self._raise_at = raise_at
        self._error = error
        self._fail_times = fail_times  # None = 永远抛
        self._residue_id = residue_id

    def _maybe_raise(self, stage: str) -> None:
        if self._raise_at != stage:
            return
        if self._fail_times is not None:
            raised = sum(1 for c in self.calls if c == f"raised:{stage}")
            if raised >= self._fail_times:
                return
        self.calls.append(f"raised:{stage}")
        raise self._error  # type: ignore[misc]

    def wait_for_auth_signal(self, page, timeout_ms: int = 30_000):
        self.calls.append("wait_for_auth_signal")

    def check_auth(self, page):
        self.calls.append("check_auth")
        return self._auth_result or AuthResult(state=AuthState.LOGGED_IN, reason="探针")

    def open_editor(self, page, draft):
        self.calls.append("open_editor")

    def fill_content(self, page, draft):
        self.calls.append("fill_content")
        self._maybe_raise("fill_content")

    def fill_meta(self, page, draft, limits, *, mode=SubmitMode.DRAFT):
        self.calls.append("fill_meta")

    def upload_cover(self, page, path):
        self.calls.append("upload_cover")

    def submit(self, page, *, mode=SubmitMode.DRAFT):
        self.calls.append("submit")
        self._maybe_raise("submit")
        return SubmitResult(state=SubmitState.SUBMITTED, reason="看到了提示")

    def verify(self, page, *, mode=SubmitMode.DRAFT):
        self.calls.append(("verify", SubmitMode(mode).value))
        if SubmitMode(mode) is SubmitMode.DRAFT:
            # 残留探针口径：查到 = ok + article_id
            if self._residue_id:
                return VerifyResult(ok=True, article_id=self._residue_id, reason="草稿箱里有一篇")
            return VerifyResult(ok=False, reason="草稿箱里没有这篇")
        return VerifyResult(ok=True, url=PUBLISHED_URL, article_id="168800002", reason="命中")


class StubPage:
    def __init__(self) -> None:
        self.url = "about:blank"

    def goto(self, url: str) -> None:
        self.url = url


def _opener(log: list[str]):
    @contextmanager
    def open_page():
        log.append("open_page")
        yield StubPage()

    return open_page


def _draft_dir(tmp_path: Path) -> Path:
    root = tmp_path / "draft"
    if not root.exists():  # 沙箱对 exist_ok=True 的重复 mkdir 会报 EEXIST，绕行
        root.mkdir()
    (root / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (root / "csdn.md").write_text("正文" * 100, encoding="utf-8")
    return root


def _loaded(tmp_path: Path) -> LoadedDraft:
    return load_draft(_draft_dir(tmp_path))


def _cfg() -> PlatformConfig:
    return PlatformConfig(name="csdn")


def _publish(tmp_path: Path, adapter: FaultyAdapter, opened: list[str], **kwargs):
    kwargs.setdefault("mode", SubmitMode.PUBLISH)
    kwargs.setdefault("gate", Gate.PUBLISH)
    kwargs.setdefault("data_dir", tmp_path / "data")
    kwargs.setdefault("clock", FakeClock(CLOCK))
    return publish_draft(
        adapter, _loaded(tmp_path), _cfg(), open_page=_opener(opened), **kwargs
    )


def _log(tmp_path: Path) -> PublishLog:
    return PublishLog(tmp_path / "data" / "publish-log.csv")


def _notes(tmp_path: Path) -> list[str]:
    return [e.note for e in _log(tmp_path).entries()]


class _FakeSleep:
    """假睡眠：记录间隔并推进假时钟（UT-12-04 的断言素材）。"""

    def __init__(self, clock: FakeClock) -> None:
        self.intervals: list[float] = []
        self._clock = clock

    def __call__(self, seconds: float) -> None:
        self.intervals.append(seconds)
        self._clock.advance(seconds)


# --------------------------------------------------------------- classify 纯函数

def test_classify_challenge_is_captcha_no_retry():
    """人机校验 → CAPTCHA：零重试、落 needs_human。"""
    exc = AuthRequired("csdn", AuthState.UNKNOWN, "页面出现人机校验", has_challenge=True)

    verdict = classify(exc)

    assert verdict is not None
    assert verdict.kind is FailureKind.CAPTCHA
    assert verdict.retryable is False
    assert verdict.status is Status.NEEDS_HUMAN


def test_classify_auth_expired_no_log_row():
    """登录失效 → AUTH_EXPIRED：不重试、不落库（status=None）。

    不落库是有讲究的：授权问题不占重试配额，重新授权后这篇应当直接能发。
    """
    exc = AuthRequired("csdn", AuthState.EXPIRED, "被踢回登录页")

    verdict = classify(exc)

    assert verdict is not None
    assert verdict.kind is FailureKind.AUTH_EXPIRED
    assert verdict.retryable is False
    assert verdict.status is None


def test_classify_content_rejected_carries_platform_text():
    verdict = classify(ContentRejected("  内容包含敏感词  "))

    assert verdict is not None
    assert verdict.kind is FailureKind.CONTENT_REJECTED
    assert verdict.retryable is False
    assert verdict.status is Status.NEEDS_HUMAN
    assert verdict.platform_text == "内容包含敏感词"  # 原话要留住，但首尾空白不算原话


@pytest.mark.parametrize(
    "exc",
    [
        TimeoutError("net"),
        ConnectionError("reset"),
        type("TimeoutError", (Exception,), {})("playwright 风格的同名超时"),
    ],
    ids=["builtin-timeout", "connection-reset", "playwright-timeout-by-name"],
)
def test_classify_network_timeout_is_retryable(exc):
    verdict = classify(exc)

    assert verdict is not None
    assert verdict.kind is FailureKind.NETWORK_TIMEOUT
    assert verdict.retryable is True
    assert verdict.status is Status.FAILED


def test_classify_unknown_exception_returns_none():
    """不认识的异常是 bug：返回 None，调用方原样抛出——不许吞成「已分类失败」。"""
    assert classify(ValueError("自己的 bug")) is None
    assert classify(RuntimeError("按钮点不动")) is None


def test_content_rejected_message_fallback():
    assert ContentRejected("").platform_text == ""
    assert "无提示原文" in str(ContentRejected("  "))


def test_backoff_delays_schedule():
    """UT-12-04 的间隔表：2/4/8，预算取前缀。"""
    assert backoff_delays(0) == ()
    assert backoff_delays(2) == RETRY_SCHEDULE_S[:2] == (2.0, 4.0)
    assert backoff_delays(RETRY_HARD_CAP) == RETRY_SCHEDULE_S == (2.0, 4.0, 8.0)


@pytest.mark.parametrize("bad", [-1, RETRY_HARD_CAP + 1, True, "2"])
def test_backoff_delays_out_of_range(bad):
    """预算是安全参数（UT-12-05 的全局上限），越界当场报错，不静默截断。"""
    with pytest.raises(ValueError):
        backoff_delays(bad)  # type: ignore[arg-type]


# --------------------------------------------------------------- UT-12-01 人机校验

def test_ut_12_01_challenge_goes_needs_human_with_zero_retry(tmp_path):
    """页面出现人机校验 → needs_human，**零次重试**：浏览器只开一次，check_auth 只判一次。"""
    adapter = FaultyAdapter(
        auth_result=AuthResult(
            state=AuthState.UNKNOWN, has_challenge=True, reason="页面出现人机校验"
        )
    )
    sleeps = _FakeSleep(FakeClock(CLOCK))
    opened: list[str] = []

    outcome = _publish(tmp_path, adapter, opened, sleep=sleeps)

    assert outcome.ok is False
    assert outcome.failure_kind is FailureKind.CAPTCHA
    assert opened == ["open_page"], "零次重试：浏览器只许开一次"
    assert adapter.calls.count("check_auth") == 1
    assert "fill_content" not in adapter.calls, "探活拦下的运行不许碰编辑器"
    assert sleeps.intervals == [], "人机校验一次都不许重试"
    entries = _log(tmp_path).entries()
    assert entries[-1].status is Status.NEEDS_HUMAN
    assert "failure_kind=CAPTCHA" in entries[-1].note
    assert "stage=check_login" in entries[-1].note


# --------------------------------------------------------------- UT-12-02 登录失效

def test_ut_12_02_auth_expired_reraises_for_exit_2_and_writes_no_row(tmp_path):
    """跳登录页 → AuthRequired 原样抛出（CLI 映射退 2），且**不落 failed 行**。"""
    adapter = FaultyAdapter(
        auth_result=AuthResult(state=AuthState.EXPIRED, reason="被踢回登录页")
    )
    opened: list[str] = []

    with pytest.raises(AuthRequired) as exc_info:
        _publish(tmp_path, adapter, opened)

    exc = exc_info.value
    assert exc.has_challenge is False
    verdict = classify(exc)
    assert verdict is not None and verdict.kind is FailureKind.AUTH_EXPIRED
    assert _log(tmp_path).entries() == [], "授权问题不落库、不占重试配额"


# --------------------------------------------------------------- UT-12-03 平台拒稿

def test_ut_12_03_content_rejected_writes_back_platform_text(tmp_path):
    """平台拒稿 → needs_human + 原话回写：日志 note 和稿件目录各留一份。"""
    adapter = FaultyAdapter(
        raise_at="submit", error=ContentRejected("内容包含敏感词：「免费试用」")
    )
    opened: list[str] = []

    outcome = _publish(tmp_path, adapter, opened, sleep=_FakeSleep(FakeClock(CLOCK)))

    assert outcome.ok is False
    assert outcome.failure_kind is FailureKind.CONTENT_REJECTED
    assert "免费试用" in outcome.reason
    statuses = [e.status for e in _log(tmp_path).entries()]
    assert statuses == [Status.SUBMITTING, Status.NEEDS_HUMAN]
    note = _notes(tmp_path)[-1]
    assert "failure_kind=CONTENT_REJECTED" in note
    assert "平台原话：内容包含敏感词：「免费试用」" in note, "提示原文必须进日志（日报要报）"
    # 回写进稿件目录：改稿的人打开目录抬头就看见
    writeback = _draft_dir(tmp_path) / "platform-rejection.txt"
    assert writeback.is_file()
    assert "内容包含敏感词：「免费试用」" in writeback.read_text(encoding="utf-8")
    # 拒稿不该留残留，但流程上必须查过才算数（UT-12-07 的探针在这里也跑了）
    assert "草稿箱无残留" in note
    assert adapter.calls.count("submit") == 1, "拒稿不重试：改稿前重发等于顶风作案"


# --------------------------------------------------------------- UT-12-04 网络超时退避

def test_ut_12_04_network_timeout_retries_with_backoff_then_failed(tmp_path):
    """网络超时：2/4/8s 退避重试 3 次，仍失败 → failed。间隔用假时钟断言。"""
    clk = FakeClock(CLOCK)
    sleeps = _FakeSleep(clk)
    adapter = FaultyAdapter(raise_at="fill_content", error=TimeoutError("read timed out"))
    opened: list[str] = []

    outcome = _publish(tmp_path, adapter, opened, retry_max=3, sleep=sleeps, clock=clk)

    assert outcome.ok is False
    assert outcome.failure_kind is FailureKind.NETWORK_TIMEOUT
    assert sleeps.intervals == [2.0, 4.0, 8.0], "退避间隔必须是 2/4/8"
    assert clk.now() - FakeClock(CLOCK).now() == timedelta(seconds=14)
    assert adapter.calls.count("fill_content") == 4, "首次 + 3 次重试"
    assert len(opened) == 4, "每次重试都是全新会话（旧会话的网络状态不可信）"
    entries = _log(tmp_path).entries()
    assert entries[-1].status is Status.FAILED
    assert "failure_kind=NETWORK_TIMEOUT" in entries[-1].note
    assert "stage=fill_content" in entries[-1].note
    assert "已重试 3 次" in entries[-1].note


def test_ut_12_04_retry_succeeds_on_second_attempt(tmp_path):
    """重试不是摆设：第一次超时、第二次成功 → 正常发布，无 failed 行。"""
    sleeps = _FakeSleep(FakeClock(CLOCK))
    adapter = FaultyAdapter(
        raise_at="fill_content", error=TimeoutError("read timed out"), fail_times=1
    )

    outcome = _publish(tmp_path, adapter, [], retry_max=3, sleep=sleeps)

    assert outcome.ok is True, outcome.reason
    assert outcome.url == PUBLISHED_URL
    assert sleeps.intervals == [2.0], "只退避了实际用掉的那一次"
    assert adapter.calls.count("fill_content") == 2
    assert _log(tmp_path).entries()[-1].status is Status.PUBLISHED


def test_ut_12_04_retry_budget_zero_fails_immediately(tmp_path):
    """retry_max=0：一次都不重试，首次失败即 failed。"""
    sleeps = _FakeSleep(FakeClock(CLOCK))
    adapter = FaultyAdapter(raise_at="fill_content", error=TimeoutError("read timed out"))

    outcome = _publish(tmp_path, adapter, [], retry_max=0, sleep=sleeps)

    assert outcome.ok is False
    assert outcome.failure_kind is FailureKind.NETWORK_TIMEOUT
    assert sleeps.intervals == []
    assert adapter.calls.count("fill_content") == 1
    assert _log(tmp_path).entries()[-1].status is Status.FAILED


def test_ut_12_04_post_submit_timeout_is_never_retried(tmp_path):
    """铁律 > 重试表：submitting 行落盘后，连网络超时这种「可重试」类别也不许再来。"""
    sleeps = _FakeSleep(FakeClock(CLOCK))
    adapter = FaultyAdapter(raise_at="submit", error=TimeoutError("submit timed out"))

    outcome = _publish(tmp_path, adapter, [], retry_max=3, sleep=sleeps)

    assert outcome.ok is False
    assert outcome.failure_kind is FailureKind.NETWORK_TIMEOUT
    assert sleeps.intervals == [], "提交后失败零重试——再来一次可能发两遍"
    assert adapter.calls.count("submit") == 1
    assert _log(tmp_path).entries()[-1].status is Status.NEEDS_HUMAN


# --------------------------------------------------------------- UT-12-05 全局上限

def test_ut_12_05_global_retry_cap_forces_stop(tmp_path):
    """同一篇累计 failed ≥ 3 → 强制 needs_human，浏览器都不开。"""
    loaded = _loaded(tmp_path)
    log_dir = tmp_path / "data"
    log = PublishLog(log_dir / "publish-log.csv")
    clk = FakeClock(CLOCK)
    for i in range(RETRY_HARD_CAP):
        clk.advance(3600)
        log.append(
            LogEntry(
                ts=clk.now(),
                platform="csdn",
                account="default",
                slug=loaded.draft.meta.slug,
                idem_key=loaded.draft.idem_key("csdn", "default"),
                status=Status.FAILED,
                note=f"第 {i + 1} 次失败",
            )
        )

    adapter = FaultyAdapter()
    opened: list[str] = []
    outcome = publish_draft(
        adapter, loaded, _cfg(),
        gate=Gate.PUBLISH, mode=SubmitMode.PUBLISH,
        open_page=_opener(opened), data_dir=log_dir, clock=clk,
    )

    assert outcome.ok is False
    assert "全局上限" in outcome.reason
    assert opened == [], "强制停止：连浏览器都不许开"
    assert adapter.calls == []
    entries = _log(tmp_path).entries()
    assert entries[-1].status is Status.NEEDS_HUMAN
    assert "全局上限" in entries[-1].note


def test_ut_12_05_below_cap_still_retries(tmp_path):
    """差一次到顶（2 行 failed）→ 照常放行重试，上限不误伤。"""
    loaded = _loaded(tmp_path)
    log_dir = tmp_path / "data"
    log = PublishLog(log_dir / "publish-log.csv")
    clk = FakeClock(CLOCK)
    for i in range(RETRY_HARD_CAP - 1):
        log.append(
            LogEntry(
                ts=clk.now(),
                platform="csdn",
                account="default",
                slug=loaded.draft.meta.slug,
                idem_key=loaded.draft.idem_key("csdn", "default"),
                status=Status.FAILED,
                note=f"第 {i + 1} 次失败",
            )
        )

    outcome = publish_draft(
        FaultyAdapter(), loaded, _cfg(),
        gate=Gate.PUBLISH, mode=SubmitMode.PUBLISH,
        open_page=_opener([]), data_dir=log_dir, clock=clk,
    )

    assert outcome.ok is True, outcome.reason


# --------------------------------------------------------------- UT-12-07 失败残留

def test_ut_12_07_residue_is_flagged_failed_residue(tmp_path):
    """提交后失败 + 草稿箱查得到这篇 → needs_human 且 note 标 failed_residue。"""
    adapter = FaultyAdapter(
        raise_at="submit", error=TimeoutError("submit timed out"), residue_id="168800099"
    )
    shots: list[Path] = []

    def snap(page, path: Path) -> None:
        shots.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    outcome = _publish(tmp_path, adapter, [], sleep=_FakeSleep(FakeClock(CLOCK)), screenshot=snap)

    assert outcome.ok is False
    note = _notes(tmp_path)[-1]
    assert _log(tmp_path).entries()[-1].status is Status.NEEDS_HUMAN
    assert "failed_residue" in note
    assert "168800099" in note
    assert ("verify", "draft") in adapter.calls, "残留探针必须真的去草稿箱查过"
    assert any(p.name == "failed.png" for p in shots), "R12：提交后失败也要有失败截图"


def test_ut_12_07_no_residue_is_stated_explicitly(tmp_path):
    """提交后失败 + 草稿箱没有这篇 → note 明说「无残留」，不留歧义。"""
    adapter = FaultyAdapter(raise_at="submit", error=TimeoutError("submit timed out"))

    _publish(tmp_path, adapter, [], sleep=_FakeSleep(FakeClock(CLOCK)))

    note = _notes(tmp_path)[-1]
    assert "草稿箱无残留" in note
    assert "failed_residue" not in note


def test_ut_12_07_residue_probe_failure_is_reported_not_hidden(tmp_path):
    """残留检查本身炸了 → 明说「没查成」。假装查过的后果是半成品被手滑发出去。"""

    class ProbeExplodes(FaultyAdapter):
        def verify(self, page, *, mode=SubmitMode.DRAFT):
            self.calls.append(("verify", SubmitMode(mode).value))
            raise OSError("草稿列表页打不开")

    adapter = ProbeExplodes(raise_at="submit", error=TimeoutError("submit timed out"))

    outcome = _publish(tmp_path, adapter, [], sleep=_FakeSleep(FakeClock(CLOCK)))

    assert outcome.ok is False
    note = _notes(tmp_path)[-1]
    assert "残留检查未完成" in note
    assert "OSError" in note


# --------------------------------------------------------------- 逃逸路径与存证

def test_unclassified_exception_crashes_with_stage_and_failed_png(tmp_path):
    """不认识的异常 = bug：带 stage 原样炸出，同时尽力留下 failed.png（R12）。"""
    adapter = FaultyAdapter(raise_at="fill_content", error=RuntimeError("自己的 bug"))
    shots: list[Path] = []

    def snap(page, path: Path) -> None:
        shots.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="自己的 bug"):
        _publish(tmp_path, adapter, [], screenshot=snap)

    assert _log(tmp_path).entries() == [], "bug 不落库——它不是「投稿失败」，是「代码错了」"
    failed_pngs = [p for p in shots if p.name == "failed.png"]
    assert len(failed_pngs) == 1 and failed_pngs[0].is_file()


def test_unclassified_post_submit_exception_still_crashes(tmp_path):
    """提交后出 bug：同样原样炸出（submitting 行已在，下次运行崩溃恢复 → needs_human）。"""
    adapter = FaultyAdapter(raise_at="submit", error=RuntimeError("提交瞬间断电"))
    shots: list[Path] = []

    def snap(page, path: Path) -> None:
        shots.append(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="提交瞬间断电"):
        _publish(tmp_path, adapter, [], screenshot=snap)

    entries = _log(tmp_path).entries()
    assert [e.status for e in entries] == [Status.SUBMITTING], "submitting 锚点必须还在"
    assert any(p.name == "failed.png" for p in shots)


def test_mark_stage_marks_and_survives_sealed_exceptions():
    """阶段标记：正常异常贴上 _gp_stage；拒写属性的异常静默跳过，不许掩盖原异常。

    注意：不能拿「完全封死 __setattr__」的异常去跑整条 publish 链路——
    解释器传播异常时要写 __traceback__，那种异常在 with 块里就自己先炸了，
    跟被测代码无关。所以这里直测 _mark_stage 本身。
    """
    from geopublish.publish import _mark_stage

    exc = TimeoutError("net")
    _mark_stage(exc, "fill_content")
    assert exc._gp_stage == "fill_content"  # type: ignore[attr-defined]

    class SealedError(Exception):
        def __setattr__(self, name, value):
            if name == "_gp_stage":
                raise RuntimeError("sealed")
            super().__setattr__(name, value)

    sealed = SealedError("封死的异常")
    _mark_stage(sealed, "fill_content")  # 不炸
    assert getattr(sealed, "_gp_stage", "") == ""


def test_writeback_skipped_without_source_dir(tmp_path):
    """直接构造的 LoadedDraft（无 source_dir）→ 回写跳过，不炸。"""
    from geopublish.core.models import Draft, Meta
    from geopublish.publish import _writeback_rejection

    draft = Draft(meta=Meta(slug="s", title="t", ai_involvement="assisted"), body="正文")
    assert _writeback_rejection(LoadedDraft(draft=draft), "原文") is None
    assert _writeback_rejection(_loaded(tmp_path), "") is None


def test_failure_note_omits_empty_stage_and_zero_retries():
    """note 的每个可选段只在有内容时出现：空 stage / 零重试不留占位符。"""
    from geopublish.publish import _failure_note

    exc = TimeoutError("net")
    verdict = classify(exc)
    assert verdict is not None
    note = _failure_note(verdict, exc, 0)

    assert "failure_kind=NETWORK_TIMEOUT" in note
    assert "stage=" not in note
    assert "重试" not in note
    assert "net" in note


def test_count_failures_filters_by_platform_slug_key_and_status(tmp_path):
    """_count_failures 只数「同平台 + 同 slug + 同幂等键 + failed」的行，一行不多。"""
    from geopublish.publish import _count_failures

    loaded = _loaded(tmp_path)
    draft = loaded.draft
    clk = FakeClock(CLOCK)
    log = PublishLog(tmp_path / "data" / "publish-log.csv")

    key = draft.idem_key("csdn", "default")
    entries = [
        # 命中：同平台同 slug 同键 + failed ×2
        LogEntry(ts=clk.now(), platform="csdn", account="default", slug=draft.meta.slug,
                 idem_key=key, status=Status.FAILED),
        LogEntry(ts=clk.now(), platform="csdn", account="default", slug=draft.meta.slug,
                 idem_key=key, status=Status.FAILED),
        # 不命中：平台不同
        LogEntry(ts=clk.now(), platform="juejin", account="default", slug=draft.meta.slug,
                 idem_key=draft.idem_key("juejin", "default"), status=Status.FAILED),
        # 不命中：slug 不同
        LogEntry(ts=clk.now(), platform="csdn", account="default", slug="别的稿",
                 idem_key=key, status=Status.FAILED),
        # 不命中：同 slug 但正文改过（键不同）
        LogEntry(ts=clk.now(), platform="csdn", account="default", slug=draft.meta.slug,
                 idem_key="0" * 64, status=Status.FAILED),
        # 不命中：键相同但状态不是 failed
        LogEntry(ts=clk.now(), platform="csdn", account="default", slug=draft.meta.slug,
                 idem_key=key, status=Status.SUBMITTING),
    ]
    for entry in entries:
        log.append(entry)

    assert _count_failures(log, draft, "csdn") == 2


def test_default_retry_and_sleep_are_real(tmp_path, monkeypatch):
    """不传 retry_max/sleep：预算=全局硬顶，sleep 默认接到 time.sleep（接线验证，不真睡）。"""
    seen: list[float] = []
    monkeypatch.setattr(time, "sleep", seen.append)
    adapter = FaultyAdapter(raise_at="fill_content", error=TimeoutError("net"), fail_times=1)

    outcome = _publish(tmp_path, adapter, [])

    assert outcome.ok is True, outcome.reason
    assert seen == [2.0], "默认 sleep 必须是 time.sleep（生产真等 2s，测试里换成记录器）"
