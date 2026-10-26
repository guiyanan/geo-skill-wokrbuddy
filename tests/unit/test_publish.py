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
        auth_signal_raises: bool = False,
    ) -> None:
        self.calls: list[object] = []
        self._auth = auth
        self._verify_ok = verify_ok
        self._submit_error = submit_error
        self._auth_signal_raises = auth_signal_raises

    def wait_for_auth_signal(self, page, timeout_ms: int = 30_000):
        self.calls.append("wait_for_auth_signal")
        if self._auth_signal_raises:
            raise TimeoutError("30s 内没等到权威信号")

    def check_auth(self, page):
        self.calls.append("check_auth")
        return AuthResult(state=self._auth, reason="探针结果")

    def open_editor(self, page, draft):
        self.calls.append("open_editor")

    def fill_content(self, page, draft):
        self.calls.append("fill_content")

    def upload_cover(self, page, path):
        self.calls.append(("upload_cover", Path(path).name))

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

def test_publish_runs_the_steps_in_order(tmp_path):
    adapter = RecordingAdapter()
    opened: list[str] = []
    loaded = _loaded(tmp_path, cover=False)  # 草稿链路：标题 + 正文 + 保存
    open_page, page = _opener(opened)

    outcome = publish_draft(adapter, loaded, _cfg(), gate=Gate.DRAFT, open_page=open_page)

    assert outcome.ok is True
    assert outcome.article_id == "1"
    assert opened == ["open_page"]
    assert page.calls == [("goto", adapter.editor_url)], (
        "探活前必须先导航到编辑器页：空白页上 check_auth 只能判出 unknown"
    )
    assert adapter.calls == [
        "wait_for_auth_signal",
        "check_auth",
        "open_editor",
        "fill_content",
        ("fill_meta", "draft"),
        ("submit", "draft"),
        ("verify", "draft"),
    ], f"实际顺序：{adapter.calls}"


def test_draft_mode_skips_cover_upload_with_a_warning(tmp_path, caplog):
    """封面上传框在发布对话框里（真机校准）：草稿模式跳过并明说，不撞选择器。"""
    adapter = RecordingAdapter()
    loaded = _loaded(tmp_path, cover=True)

    with caplog.at_level(logging.WARNING, logger="geopublish.publish"):
        outcome = publish_draft(
            adapter, loaded, _cfg(), gate=Gate.DRAFT, open_page=_opener([])[0]
        )

    assert outcome.ok is True
    assert not [c for c in adapter.calls if isinstance(c, tuple) and c[0] == "upload_cover"]
    assert any("草稿模式不上传封面" in r.message for r in caplog.records)


def test_cover_step_is_skipped_when_the_draft_has_none(tmp_path):
    adapter = RecordingAdapter()
    loaded = _loaded(tmp_path, cover=False)
    assert loaded.cover is None

    publish_draft(adapter, loaded, _cfg(), gate=Gate.DRAFT, open_page=_opener([])[0])

    assert not [c for c in adapter.calls if isinstance(c, tuple) and c[0] == "upload_cover"]


def test_publish_mode_fills_meta_then_uploads_cover_and_passes_mode_down(tmp_path):
    """发布模式（T-10 真机约束）：元信息在封面**之前**——标题必须在发布对话框
    打开前填（对话框是遮罩层，会挡住编辑器页点击），封面控件在对话框里。
    mode 一路传到 fill_meta/submit/verify。"""
    adapter = RecordingAdapter()

    publish_draft(
        adapter, _loaded(tmp_path, cover=True), _cfg(), gate=Gate.PUBLISH,
        open_page=_opener([])[0], mode="publish",
        data_dir=tmp_path / "data",  # 不传会写真仓库的 data/——csv 污染就是这么来的
    )

    assert adapter.calls == [
        "wait_for_auth_signal",
        "check_auth",
        "open_editor",
        "fill_content",
        ("fill_meta", "publish"),
        ("upload_cover", "cover.png"),
        ("submit", "publish"),
        ("verify", "publish"),
    ]


def test_publish_success_writes_receipt_csv_and_two_screenshots(tmp_path):
    """R10 编排层：发布成功 → receipt.json 四字段 + csv 一行 + before/after 两张截图，
    时间字段全部来自注入时钟（UT-10-01/02/04）。"""
    adapter = RecordingAdapter()
    shots: list[Path] = []
    data_dir = tmp_path / "data"
    clock = FakeClock("2026-10-22T10:00:00+08:00")

    outcome = publish_draft(
        adapter,
        _loaded(tmp_path),
        _cfg(),
        gate=Gate.PUBLISH,
        open_page=_opener([])[0],
        mode="publish",
        data_dir=data_dir,
        clock=clock,
        screenshot=lambda page, path: (path.parent.mkdir(parents=True, exist_ok=True),
                                       path.write_bytes(b"\x89PNG"), shots.append(path)),
    )

    assert outcome.ok is True
    run_dir = data_dir / "runs" / "20261022_100000"
    # UT-10-01：receipt.json 含 url/article_id/published_at/screenshot 四字段
    receipt = json.loads((run_dir / "receipt.json").read_text(encoding="utf-8"))
    for key in ("url", "article_id", "published_at", "screenshot"):
        assert key in receipt, f"receipt.json 缺 {key}"
    assert receipt["article_id"] == "1"
    # UT-10-04：时间来自注入时钟，不是系统时间
    assert receipt["published_at"] == "2026-10-22T10:00:00+08:00"
    # R10：before/after 双截图
    assert sorted(p.name for p in shots) == ["after.png", "before.png"]
    assert (run_dir / "before.png").is_file() and (run_dir / "after.png").is_file()
    # UT-10-02：csv 追加一行且列顺序固定
    csv_lines = (data_dir / "publish-log.csv").read_text(encoding="utf-8").splitlines()
    assert len(csv_lines) == 2, f"表头 + 一行数据，实际 {len(csv_lines)} 行"
    assert csv_lines[0] == "ts,platform,account,slug,idem_key,status,url,article_id,screenshot,indexed_at,note"
    row = dict(zip(csv_lines[0].split(","), csv_lines[1].split(",")))
    assert row["ts"] == "2026-10-22T10:00:00+08:00"
    assert row["status"] == "published" and row["article_id"] == "1"
    assert outcome.receipt_path == run_dir / "receipt.json"
    assert outcome.receipt_failed is False


def test_publish_gate_blocks_real_publish_when_gate_is_draft(tmp_path):
    """闸门（R8 前置刹车）：gate=draft 时 --mode publish 必须在开浏览器之前被拦。"""
    adapter = RecordingAdapter()
    opened: list[str] = []

    with pytest.raises(GateClosed) as exc:
        publish_draft(
            adapter, _loaded(tmp_path), _cfg(), gate=Gate.DRAFT,
            open_page=_opener(opened)[0], mode="publish",
        )

    assert "不允许真实发布" in str(exc.value)
    assert opened == [], "闸门拦下的运行连浏览器都不许开"
    assert adapter.calls == []


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
    assert adapter.calls == ["wait_for_auth_signal", "check_auth"]


def test_auth_signal_timeout_does_not_fail_the_probe(tmp_path):
    """等不到权威信号（网慢/改版）→ 照样交给 check_auth 判，不在这里炸。

    这是 T-09b 真机教训的反面：等信号是为了减少误判，不是新增一道会失败的闸门。
    """
    adapter = RecordingAdapter(auth_signal_raises=True)

    outcome = publish_draft(
        adapter, _loaded(tmp_path), _cfg(), gate=Gate.DRAFT, open_page=_opener([])[0]
    )

    assert outcome.ok is True
    assert adapter.calls[:2] == ["wait_for_auth_signal", "check_auth"]


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
    assert ("fill_meta", "publish") in adapter.calls


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


# --------------------------------------------------------------- T-10 回执的失败形态

def test_publish_screenshot_failure_does_not_change_the_verdict(tmp_path):
    """截图回调抛异常 → 降级为告警，发布成功结论不变（存证不该拖垮判定）。"""
    adapter = RecordingAdapter()

    def _broken(page, path):
        raise RuntimeError("磁盘满了")

    outcome = publish_draft(
        adapter, _loaded(tmp_path), _cfg(), gate=Gate.PUBLISH,
        open_page=_opener([])[0], mode="publish",
        data_dir=tmp_path / "data", clock=FakeClock("2026-10-22T10:00:00+08:00"),
        screenshot=_broken,
    )

    assert outcome.ok is True, "截图失败不许改判定"
    # 回执照写，screenshot 字段为空串（没截到就是不截到）
    import json as _json
    receipt = _json.loads(
        (tmp_path / "data/runs/20261022_100000/receipt.json").read_text(encoding="utf-8")
    )
    assert receipt["screenshot"] == ""


def test_publish_receipt_write_failure_marks_receipt_failed(tmp_path, caplog):
    """run_dir 被文件占住（磁盘满形态）→ 发布成功不变，outcome 带 receipt_failed。"""
    adapter = RecordingAdapter()
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    (data_dir / "runs").write_text("我不是目录", encoding="utf-8")  # runs/ 是文件

    with caplog.at_level(logging.WARNING, logger="geopublish.publish"):
        outcome = publish_draft(
            adapter, _loaded(tmp_path), _cfg(), gate=Gate.PUBLISH,
            open_page=_opener([])[0], mode="publish",
            data_dir=data_dir, clock=FakeClock("2026-10-22T10:00:00+08:00"),
        )

    assert outcome.ok is True, "回执失败不许掩盖发布成功"
    assert outcome.receipt_failed is True
    assert outcome.receipt_path is None
    assert any("receipt_failed" in r.message for r in caplog.records)


def test_account_falls_back_to_default_when_url_has_no_account_segment(tmp_path):
    """URL 里挖不出账号段 → csv 的 account 写 default（必填列不许留空）。"""
    adapter = RecordingAdapter()
    adapter.verify = lambda page, *, mode=SubmitMode.DRAFT: VerifyResult(  # type: ignore[method-assign]
        ok=True, url="https://mp.csdn.net/", article_id="7", reason="命中"
    )

    publish_draft(
        adapter, _loaded(tmp_path), _cfg(), gate=Gate.PUBLISH,
        open_page=_opener([])[0], mode="publish",
        data_dir=tmp_path / "data", clock=FakeClock("2026-10-22T10:00:00+08:00"),
    )

    import csv as _csv
    with open(tmp_path / "data/publish-log.csv", encoding="utf-8", newline="") as fh:
        rows = list(_csv.DictReader(fh))
    assert rows[0]["account"] == "default"
    assert rows[0]["article_id"] == "7"


def test_account_fallback_also_covers_article_path_without_account(tmp_path):
    """blog.csdn.net/article/details/<id>（无账号段的非常规形态）→ account=default。"""
    adapter = RecordingAdapter()
    adapter.verify = lambda page, *, mode=SubmitMode.DRAFT: VerifyResult(  # type: ignore[method-assign]
        ok=True, url="https://blog.csdn.net/article/details/9", article_id="9", reason="命中"
    )

    publish_draft(
        adapter, _loaded(tmp_path), _cfg(), gate=Gate.PUBLISH,
        open_page=_opener([])[0], mode="publish",
        data_dir=tmp_path / "data", clock=FakeClock("2026-10-22T10:00:00+08:00"),
    )

    import csv as _csv
    with open(tmp_path / "data/publish-log.csv", encoding="utf-8", newline="") as fh:
        rows = list(_csv.DictReader(fh))
    assert rows[0]["account"] == "default"
    assert rows[0]["article_id"] == "9"


def test_account_fallback_covers_non_csdn_url():
    """非 csdn 域名（未来多平台形态）→ account=default。直接钉 _account_from_url。"""
    from geopublish.publish import _account_from_url

    assert _account_from_url("https://juejin.cn/post/123") == "default"
    assert _account_from_url("") == "default"
    assert _account_from_url("https://blog.csdn.net/probe/article/details/42") == "probe"


def test_account_fallback_covers_non_csdn_url():
    """非 csdn 域名（未来多平台形态）→ account=default。直接钉 _account_from_url。"""
    from geopublish.publish import _account_from_url

    assert _account_from_url("https://juejin.cn/post/123") == "default"
    assert _account_from_url("") == "default"
    assert _account_from_url("https://blog.csdn.net/probe/article/details/42") == "probe"
