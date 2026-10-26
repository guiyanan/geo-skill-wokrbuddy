"""T-08/T-10 封面上传（upload_cover）；2026-10-22 T-10 真机校准后改版。

钉死的行为：
- UT-08-05 尺寸/大小不符平台要求 → **在本地就拒绝**，绝不 set_input_files 后才失败
- 封面规格只认 meta.json（PRD：尺寸从平台读取，不得硬编码）；
  meta.json 没写 cover 规格 → 直接 CoverError，不许「先传了再说」
- T-10 真机链路：发布对话框 → （已有封面先删）→ 塞文件 → .vicp-wrap 裁剪编辑器
  → 点 div.vicp-operate-btn「确认上传」→ 等 .preview-box.hasImg 完成信号
- 裁剪编辑器不出现 / 确认后完成信号超时 → CoverError（不算成功）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from geopublish.adapters.base import CoverError
from geopublish.adapters.csdn.flow import CsdnAdapter, META_PATH
from geopublish.core.clock import FakeClock

pytestmark = pytest.mark.unit

from _imgfactory import jpeg_bytes, png_bytes


class FakeCoverPage:
    """假封面上传页（T-10 链路）。

    - crop_never_appears：选文件后裁剪编辑器一直不出现
    - upload_stalls：点了确认上传，但 .preview-box 一直不长出 hasImg
    - has_cover：对话框里已有封面（done_marker 命中，需先删再传）
    - upload_fails：set_input_files 直接抛（上传框改版）
    """

    def __init__(
        self,
        *,
        crop_never_appears: bool = False,
        upload_stalls: bool = False,
        has_cover: bool = False,
        upload_fails: bool = False,
    ) -> None:
        self.crop_never_appears = crop_never_appears
        self.upload_stalls = upload_stalls
        self.has_cover = has_cover
        self.upload_fails = upload_fails
        self.uploads: list[tuple[str, Path]] = []
        self.clicks: list[str] = []
        self.waits: list[str] = []
        self.dialog_open = False
        self.url = "https://editor.csdn.net/md"

    # --- 对话框与脚本

    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        if op == "opendialog":
            self.dialog_open = True
            return {"ok": True}
        if op == "closemodals":
            return {"ok": True, "closed": 0}
        if op == "closebuttons":
            return {"ok": True, "closed": 0}
        raise AssertionError(f"upload_cover 不该跑这个脚本：{op}")

    def query_selector(self, selector: str):
        adapter = _adapter()
        if selector == adapter.selectors("dialog", "root"):
            return object() if self.dialog_open else None
        if selector == adapter.selectors("cover", "done_marker"):
            return object() if self.has_cover else None
        return None

    # --- 上传链

    def set_input_files(self, selector: str, path: Path) -> None:
        if self.upload_fails:
            raise RuntimeError("上传框元素不存在")
        self.uploads.append((selector, Path(path)))

    def click(self, selector: str) -> None:
        self.clicks.append(selector)
        if selector == _adapter().selectors("cover", "remove_button"):
            self.has_cover = False

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        self.waits.append(selector)
        adapter = _adapter()
        if selector == adapter.selectors("cover", "crop_editor") and self.crop_never_appears:
            raise TimeoutError(f"等待 {selector} 超过 {timeout_ms}ms")
        if selector == adapter.selectors("cover", "done_marker") and self.upload_stalls:
            raise TimeoutError(f"等待 {selector} 超过 {timeout_ms}ms")
        return object()


def _adapter(meta_path: str | Path | None = None) -> CsdnAdapter:
    return CsdnAdapter(
        clock=FakeClock("2026-10-21T11:00:00+08:00"),
        meta_path=meta_path,
    )


def _good_cover(tmp_path: Path) -> Path:
    good = tmp_path / "cover.png"
    good.write_bytes(png_bytes(1140, 640))
    return good


# ----------------------------------------------------------- UT-08-05 本地预检

def test_ut_08_05_wrong_dimensions_rejected_before_any_upload(tmp_path: Path):
    """800×400 的图对 1140×640 的平台规格 → 本地 CoverError，一次都没传。"""
    bad = tmp_path / "cover.png"
    bad.write_bytes(png_bytes(800, 400))
    page = FakeCoverPage()

    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(page, bad)

    assert "800x400" in str(exc.value) and "1140x640" in str(exc.value)
    assert page.uploads == [], "尺寸不对就不许碰上传框"


def test_wrong_dimensions_jpeg_also_rejected(tmp_path: Path):
    """JPEG 同样被预检（不是只认 PNG）。"""
    bad = tmp_path / "cover.jpg"
    bad.write_bytes(jpeg_bytes(1140, 641))
    page = FakeCoverPage()
    with pytest.raises(CoverError):
        _adapter().upload_cover(page, bad)
    assert page.uploads == []


def test_oversized_file_rejected_before_upload(tmp_path: Path):
    """文件超过 meta.json 的 max_bytes → 本地拒绝。规格收紧到 200 字节即可测。"""
    big = tmp_path / "cover.png"
    big.write_bytes(png_bytes(1140, 640))
    meta = tmp_path / "meta.json"
    data = json.loads(META_PATH.read_text(encoding="utf-8"))
    data["cover"] = {"width": 1140, "height": 640, "max_bytes": 200}
    meta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CoverError) as exc:
        _adapter(meta).upload_cover(FakeCoverPage(), big)
    assert "max_bytes" in str(exc.value) or "字节" in str(exc.value)


# ----------------------------------------------------------- T-10 上传链

def test_crop_editor_never_appears_is_an_error_not_success(tmp_path: Path):
    """UT-08-04 的 T-10 形态：选文件后裁剪编辑器没出现 → CoverError，不算成功。"""
    page = FakeCoverPage(crop_never_appears=True)

    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(page, _good_cover(tmp_path))

    assert "裁剪编辑器" in str(exc.value)
    assert len(page.uploads) == 1, "失败前确实执行过上传（现场包要能看出传到哪一步）"


def test_upload_stall_after_crop_confirm_is_cover_error(tmp_path: Path):
    """点了「确认上传」但完成信号（.preview-box.hasImg）超时 → CoverError。"""
    page = FakeCoverPage(upload_stalls=True)

    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(page, _good_cover(tmp_path))

    assert "未完成" in str(exc.value)
    adapter = _adapter()
    assert adapter.selectors("cover", "crop_confirm") in page.clicks, "确认上传确实点过"


def test_upload_success_walks_the_real_chain(tmp_path: Path):
    """正常路径：开对话框 → 塞文件 → 裁剪编辑器出现 → 点确认 → 等 hasImg。"""
    good = _good_cover(tmp_path)
    page = FakeCoverPage()
    adapter = _adapter()

    adapter.upload_cover(page, good)

    assert page.dialog_open, "封面上传必须先打开（或复用）发布对话框"
    selector, uploaded = page.uploads[0]
    assert selector == adapter.selectors("cover", "upload_input")
    assert uploaded == good
    assert adapter.selectors("cover", "crop_confirm") in page.clicks
    # 等待顺序：先等裁剪编辑器，再等完成信号
    assert page.waits.index(adapter.selectors("cover", "crop_editor")) < page.waits.index(
        adapter.selectors("cover", "done_marker")
    )


def test_existing_cover_is_removed_before_upload(tmp_path: Path):
    """已有封面时上传框不在 DOM 里（T-10 实测）：必须先点删除入口再传。"""
    good = _good_cover(tmp_path)
    page = FakeCoverPage(has_cover=True)
    adapter = _adapter()

    adapter.upload_cover(page, good)

    remove_sel = adapter.selectors("cover", "remove_button")
    assert remove_sel in page.clicks, "已有封面必须先删"
    assert page.clicks.index(remove_sel) < page.clicks.index(
        adapter.selectors("cover", "crop_confirm")
    ), "删除必须发生在上传之前"
    assert page.uploads, "删完要真的传新封面"


# ----------------------------------------------------------- 入参防线

def test_missing_cover_file_rejected_without_touching_page():
    """文件不存在 → 本地 CoverError，页面零调用。"""
    page = FakeCoverPage()
    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(page, Path("/nonexistent/cover.png"))
    assert "不存在" in str(exc.value)
    assert page.uploads == []


def test_unreadable_image_rejected_before_upload(tmp_path: Path):
    """不是 PNG/JPEG（改格式/损坏文件）→ 本地拒绝，报错写明「识别不出」。"""
    junk = tmp_path / "cover.png"
    junk.write_bytes(b"NOT-AN-IMAGE" * 10)
    page = FakeCoverPage()
    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(page, junk)
    assert "识别" in str(exc.value)
    assert page.uploads == []


def test_meta_without_cover_spec_fails_loud(tmp_path: Path):
    """meta.json 没写 cover 规格 → CoverError。没有规格就上传等于裸奔。"""
    meta = tmp_path / "meta.json"
    data = json.loads(META_PATH.read_text(encoding="utf-8"))
    data.pop("cover", None)
    meta.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    with pytest.raises(CoverError):
        _adapter(meta).upload_cover(FakeCoverPage(), _good_cover(tmp_path))


def test_upload_input_rejection_is_wrapped_as_cover_error(tmp_path: Path):
    """上传框元素不存在（改版）→ CoverError 说清是哪一步，不裸漏 playwright 异常。"""
    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(FakeCoverPage(upload_fails=True), _good_cover(tmp_path))
    assert "上传框" in str(exc.value)


def test_remove_old_cover_failure_is_cover_error(tmp_path: Path):
    """旧封面的删除入口点不动 → CoverError（不带着旧封面硬传）。"""

    class _StuckRemove(FakeCoverPage):
        def click(self, selector: str) -> None:
            if selector == _adapter().selectors("cover", "remove_button"):
                raise TimeoutError("点不动")
            super().click(selector)

    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(
            _StuckRemove(has_cover=True), _good_cover(tmp_path)
        )
    assert "删不掉" in str(exc.value)


def test_upload_input_never_returns_after_removal(tmp_path: Path):
    """删掉旧封面后上传框没回来 → CoverError。"""

    class _NoReturn(FakeCoverPage):
        def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
            if selector == _adapter().selectors("cover", "upload_input"):
                raise TimeoutError("Timeout exceeded")
            return super().wait_for_selector(selector, timeout_ms)

    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(_NoReturn(has_cover=True), _good_cover(tmp_path))
    assert "没回来" in str(exc.value)


def test_crop_confirm_click_failure_is_cover_error(tmp_path: Path):
    """「确认上传」点不动 → CoverError，且不等待完成信号（没确认就不会完成）。"""

    class _StuckConfirm(FakeCoverPage):
        def click(self, selector: str) -> None:
            if selector == _adapter().selectors("cover", "crop_confirm"):
                raise TimeoutError("点不动")
            super().click(selector)

    page = _StuckConfirm()
    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(page, _good_cover(tmp_path))
    assert "确认上传" in str(exc.value)
    assert _adapter().selectors("cover", "done_marker") not in page.waits
