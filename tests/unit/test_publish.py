"""T-09 发布编排（geopublish/publish.py）：顺序、闸门、稿件装载、存证。

为什么这层值得单独测：它是唯一把七个 adapter 动作串起来的地方。顺序错了
（封面在元信息之后、提交在复核之前）在真机上表现为「看起来都成功、草稿却是残的」，
而单测里只是两个字符串的前后关系。
"""

from __future__ import annotations

import json
import logging
from contextlib import contextmanager
from pathlib import Path

import pytest

from geopublish.adapters.base import AuthResult, AuthState, SubmitMode, SubmitResult, SubmitState, VerifyResult
from geopublish.core.clock import FakeClock
from geopublish.core.config import Gate, PlatformConfig
from geopublish.core.session import AuthRequired
from geopublish.publish import (
    DraftLoadError,
    GateClosed,
    LoadedDraft,
    load_draft,
    publish_draft,
)

pytestmark = pytest.mark.unit

META = {"slug": "t09-publish", "title": "探针标题", "ai_involvement": "assisted"}


# --------------------------------------------------------------- 假件

class RecordingAdapter:
    """记录调用顺序的假 adapter。七个动作一个不少，谁跳过就少一个字符串。"""

    name = "fake"
    editor_url = "https://mp.csdn.net/mp_blog/creation/editor"

    def __init__(
        self,
        *,
        auth: AuthState = AuthState.LOGGED_IN,
        verify_ok: bool = True,
        submit_error: Exception | None = None,
    ) -> None:
        self.calls: list[object] = []
        self._auth = auth
        self._verify_ok = verify_ok
        self._submit_error = submit_error

    def check_auth(self, page):
        self.calls.append("check_auth")
        return AuthResult(state=self._auth, reason="探针结果")

    def open_editor(self, page, draft):
        self.calls.append("open_editor")

    def fill_content(self, page, draft):
        self.calls.append("fill_content")

    def upload_cover(self, page, path):
        self.calls.append(("upload_cover", Path(path).name))

    def fill_meta(self, page, draft, limits):
        self.calls.append("fill_meta")

    def submit(self, page, *, mode=SubmitMode.DRAFT):
        self.calls.append(("submit", SubmitMode(mode).value))
        if self._submit_error is not None:
            raise self._submit_error
        return SubmitResult(state=SubmitState.SUBMITTED, reason="看到了提示")

    def verify(self, page, *, mode=SubmitMode.DRAFT):
        self.calls.append(("verify", SubmitMode(mode).value))
        if self._verify_ok:
            return VerifyResult(ok=True, url="https://mp.csdn.net/x/1", article_id="1", reason="命中")
        return VerifyResult(ok=False, url="https://mp.csdn.net/x", reason="草稿列表页没找到")


class StubPage:
    """只记录导航的假会话页：发布编排层自己只做 goto，剩下的动作全在 adapter 上。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def goto(self, url: str) -> None:
        self.calls.append(("goto", url))


def _opener(log: list[str], page: StubPage | None = None):
    stub = page or StubPage()

    @contextmanager
    def open_page():
        log.append("open_page")
        yield stub

    return open_page, stub


def _loaded(tmp_path: Path, *, cover: bool = False) -> LoadedDraft:
    root = tmp_path / "draft"
    root.mkdir(exist_ok=True)
    meta = dict(META)
    if cover:
        meta["cover"] = "cover.png"
        (root / "cover.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "csdn.md").write_text("正文" * 100, encoding="utf-8")
    return load_draft(root)


def _cfg(**over) -> PlatformConfig:
    return PlatformConfig(name="csdn", **over)


# --------------------------------------------------------------- 顺序

def test_publish_runs_the_seven_steps_in_order(tmp_path):
    adapter = RecordingAdapter()
    opened: list[str] = []
    loaded = _loaded(tmp_path, cover=True)
    open_page, page = _opener(opened)

    outcome = publish_draft(adapter, loaded, _cfg(), gate=Gate.DRAFT, open_page=open_page)

    assert outcome.ok is True
    assert outcome.article_id == "1"
    assert opened == ["open_page"]
    assert page.calls == [("goto", adapter.editor_url)], (
        "探活前必须先导航到编辑器页：空白页上 check_auth 只能判出 unknown"
    )
    assert adapter.calls == [
        "check_auth",
        "open_editor",
        "fill_content",
        ("upload_cover", "cover.png"),
        "fill_meta",
        ("submit", "draft"),
        ("verify", "draft"),
    ], f"实际顺序：{adapter.calls}"


def test_cover_step_is_skipped_when_the_draft_has_none(tmp_path):
    adapter = RecordingAdapter()
    loaded = _loaded(tmp_path, cover=False)
    assert loaded.cover is None

    publish_draft(adapter, loaded, _cfg(), gate=Gate.DRAFT, open_page=_opener([])[0])

    assert not [c for c in adapter.calls if isinstance(c, tuple) and c[0] == "upload_cover"]


def test_default_mode_is_draft(tmp_path):
    """不传 mode 必须是 draft：默认值决定的是「会不会当场公开」。"""
    adapter = RecordingAdapter()
    publish_draft(adapter, _loaded(tmp_path), _cfg(), gate=Gate.DRAFT, open_page=_opener([])[0])

    assert ("submit", "draft") in adapter.calls
    assert ("verify", "draft") in adapter.calls


# --------------------------------------------------------------- 判定

def test_verify_failure_is_reported_and_no_screenshot_is_taken(tmp_path):
    adapter = RecordingAdapter(verify_ok=False)
    shots: list[Path] = []
    loaded = _loaded(tmp_path)

    outcome = publish_draft(
        adapter,
        loaded,
        _cfg(),
        gate=Gate.DRAFT,
        open_page=_opener([])[0],
        data_dir=tmp_path / "data",
        screenshot=lambda page, path: shots.append(path),
    )

    assert outcome.ok is False
    assert "没找到" in outcome.reason
    assert outcome.run_dir is None
    assert shots == [], "没确认成功就没有「成功截图」可留"


def test_submit_exception_propagates_and_verify_never_runs(tmp_path):
    adapter = RecordingAdapter(submit_error=RuntimeError("按钮点不动"))

    with pytest.raises(RuntimeError):
        publish_draft(adapter, _loaded(tmp_path), _cfg(), gate=Gate.DRAFT, open_page=_opener([])[0])

    assert ("verify", "draft") not in adapter.calls


def test_auth_expired_stops_before_any_editor_action(tmp_path):
    """探活失败就停：不导航第二次、不注入，也不重试（R6）。"""
    adapter = RecordingAdapter(auth=AuthState.EXPIRED)
    open_page, page = _opener([])

    with pytest.raises(AuthRequired):
        publish_draft(adapter, _loaded(tmp_path), _cfg(), gate=Gate.DRAFT, open_page=open_page)

    assert page.calls == [("goto", adapter.editor_url)], "探活那次导航是唯一允许的"
    assert adapter.calls == ["check_auth"]


# --------------------------------------------------------------- 存证

def test_screenshot_lands_in_a_run_dir_named_by_the_injected_clock(tmp_path):
    shots: list[Path] = []

    def _shot(page, path: Path) -> None:
        shots.append(path)
        path.write_bytes(b"png")

    outcome = publish_draft(
        RecordingAdapter(),
        _loaded(tmp_path),
        _cfg(),
        gate=Gate.DRAFT,
        open_page=_opener([])[0],
        data_dir=tmp_path / "data",
        clock=FakeClock("2026-10-21T15:20:30+08:00"),
        screenshot=_shot,
    )

    expected = tmp_path / "data" / "runs" / "20261021_152030"
    assert outcome.run_dir == expected
    assert shots == [expected / "draft.png"]
    assert shots[0].is_file()
    assert outcome.ok is True


def test_screenshot_failure_is_a_warning_not_a_publish_failure(tmp_path, caplog):
    """发了就是发了。存证失败只配一条告警，不配把结论翻成失败。"""

    def _boom(page, path):
        raise OSError("磁盘满")

    with caplog.at_level(logging.WARNING, logger="geopublish.publish"):
        outcome = publish_draft(
            RecordingAdapter(),
            _loaded(tmp_path),
            _cfg(),
            gate=Gate.DRAFT,
            open_page=_opener([])[0],
            data_dir=tmp_path / "data",
            screenshot=_boom,
        )

    assert outcome.ok is True
    assert outcome.run_dir is None
    assert any("截图失败" in r.message for r in caplog.records)


# --------------------------------------------------------------- 闸门

def test_dry_run_gate_never_even_opens_the_browser(tmp_path):
    opened: list[str] = []

    with pytest.raises(GateClosed) as exc:
        publish_draft(
            RecordingAdapter(), _loaded(tmp_path), _cfg(), gate=Gate.DRY_RUN,
            open_page=_opener(opened)[0],
        )

    assert "dry_run" in str(exc.value)
    assert opened == []


def test_disabled_platform_is_refused(tmp_path):
    with pytest.raises(GateClosed) as exc:
        publish_draft(
            RecordingAdapter(), _loaded(tmp_path), _cfg(enabled=False),
            gate=Gate.DRAFT, open_page=_opener([])[0],
        )
    assert "enabled=false" in str(exc.value)


def test_publish_mode_needs_the_publish_gate(tmp_path):
    with pytest.raises(GateClosed) as exc:
        publish_draft(
            RecordingAdapter(), _loaded(tmp_path), _cfg(), gate=Gate.DRAFT,
            open_page=_opener([])[0], mode=SubmitMode.PUBLISH,
        )
    assert "只允许存草稿" in str(exc.value)


def test_publish_mode_passes_the_gate_and_travels_down_as_publish(tmp_path):
    adapter = RecordingAdapter()

    publish_draft(
        adapter, _loaded(tmp_path), _cfg(), gate=Gate.PUBLISH,
        open_page=_opener([])[0], mode="publish",
    )

    assert ("submit", "publish") in adapter.calls


# --------------------------------------------------------------- 稿件装载

def test_load_draft_reads_meta_body_and_cover(tmp_path):
    loaded = _loaded(tmp_path, cover=True)

    assert loaded.draft.meta.slug == "t09-publish"
    assert loaded.draft.body == "正文" * 100
    assert loaded.cover == tmp_path / "draft" / "cover.png"


def test_platform_named_body_wins_over_generic_ones(tmp_path):
    """同一份稿子不同平台可以有不同正文；`csdn.md` 优先于 `body.md`。"""
    root = tmp_path / "d"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (root / "csdn.md").write_text("CSDN 版正文", encoding="utf-8")
    (root / "body.md").write_text("通用正文", encoding="utf-8")

    assert load_draft(root).draft.body == "CSDN 版正文"


@pytest.mark.parametrize("name", ["body.md", "draft.md"])
def test_generic_body_names_are_also_accepted(tmp_path, name):
    root = tmp_path / "d"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (root / name).write_text("正文", encoding="utf-8")

    assert load_draft(root).draft.body == "正文"


def test_absolute_cover_path_is_used_as_is(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    cover = tmp_path / "outside.png"
    cover.write_bytes(b"\x89PNG\r\n\x1a\n")
    (root / "meta.json").write_text(
        json.dumps({**META, "cover": str(cover)}), encoding="utf-8"
    )
    (root / "csdn.md").write_text("正文", encoding="utf-8")

    assert load_draft(root).cover == cover


def test_missing_cover_file_is_caught_before_any_browser(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({**META, "cover": "cover.png"}), encoding="utf-8"
    )
    (root / "csdn.md").write_text("正文", encoding="utf-8")

    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    assert "封面不存在" in str(exc.value)


def test_missing_directory_is_reported(tmp_path):
    with pytest.raises(DraftLoadError) as exc:
        load_draft(tmp_path / "nope")
    assert "不存在" in str(exc.value)


def test_missing_meta_is_reported(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "csdn.md").write_text("正文", encoding="utf-8")

    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    assert "meta.json" in str(exc.value)


def test_broken_meta_json_and_wrong_shape_are_reported(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "csdn.md").write_text("正文", encoding="utf-8")
    meta_file = root / "meta.json"

    meta_file.write_text("{ 坏掉的", encoding="utf-8")
    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    assert "不是合法 JSON" in str(exc.value)

    meta_file.write_text("[]", encoding="utf-8")
    with pytest.raises(DraftLoadError) as exc2:
        load_draft(root)
    assert "顶层必须是对象" in str(exc2.value)


def test_invalid_meta_fields_are_reported(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "csdn.md").write_text("正文", encoding="utf-8")
    (root / "meta.json").write_text(json.dumps({**META, "slug": "有 空格"}), encoding="utf-8")

    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    assert "slug" in str(exc.value)


def test_content_sha_mismatch_is_reported(tmp_path):
    """稿件落盘后被改过：不重算幂等键就发，等于把同一篇当两篇发。"""
    root = tmp_path / "d"
    root.mkdir()
    (root / "csdn.md").write_text("正文", encoding="utf-8")
    (root / "meta.json").write_text(
        json.dumps({**META, "content_sha": "deadbeef"}), encoding="utf-8"
    )

    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    assert "content_sha" in str(exc.value)


def test_empty_body_is_refused_at_load_time(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (root / "csdn.md").write_text("   \n\n", encoding="utf-8")

    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    assert "正文为空" in str(exc.value)


def test_non_utf8_body_is_reported(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps(META), encoding="utf-8")
    (root / "csdn.md").write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    assert "UTF-8" in str(exc.value)


def test_no_body_file_lists_every_candidate(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps(META), encoding="utf-8")

    with pytest.raises(DraftLoadError) as exc:
        load_draft(root)
    message = str(exc.value)
    for name in ("csdn.md", "body.md", "draft.md"):
        assert name in message, f"要告诉人找过哪些名字：{name}"
