"""T-08 封面上传（upload_cover）。

钉死的行为：
- UT-08-04 上传后等缩略图出现，10s 超时 → CoverError（不算成功）
- UT-08-05 尺寸/大小不符平台要求 → **在本地就拒绝**，绝不 set_input_files 后才失败
- 封面规格只认 meta.json（PRD：尺寸从平台读取，不得硬编码）；
  meta.json 没写 cover 规格 → 直接 CoverError，不许「先传了再说」
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
    """假封面上传页。thumbnail_ok=False 模拟「传上去了但缩略图一直不出现」。"""

    def __init__(self, *, thumbnail_ok: bool = True, upload_fails: bool = False) -> None:
        self.thumbnail_ok = thumbnail_ok
        self.upload_fails = upload_fails
        self.uploads: list[tuple[str, Path]] = []
        self.url = "https://mp.csdn.net/"

    def set_input_files(self, selector: str, path: Path) -> None:
        if self.upload_fails:
            raise RuntimeError("上传框元素不存在")
        self.uploads.append((selector, Path(path)))

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        if not self.thumbnail_ok:
            raise TimeoutError(f"等待 {selector} 超过 {timeout_ms}ms")
        return object()

    def evaluate(self, script: str, arg=None):
        raise AssertionError("upload_cover 不该跑页面脚本")


def _adapter(meta_path: str | Path | None = None) -> CsdnAdapter:
    return CsdnAdapter(
        clock=FakeClock("2026-10-21T11:00:00+08:00"),
        meta_path=meta_path,
    )


# ----------------------------------------------------------- UT-08-05

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


# ----------------------------------------------------------- UT-08-04

def test_ut_08_04_thumbnail_timeout_is_an_error_not_success(tmp_path: Path):
    """上传后缩略图 10s 没出现 → CoverError。等不到就停，不许当成功。"""
    good = tmp_path / "cover.png"
    good.write_bytes(png_bytes(1140, 640))
    page = FakeCoverPage(thumbnail_ok=False)

    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(page, good)

    assert "缩略图" in str(exc.value) and "10" in str(exc.value)
    assert len(page.uploads) == 1, "失败前确实执行过上传（现场包要能看出传到哪一步）"


def test_upload_success_passes_path_and_waits_thumbnail(tmp_path: Path):
    """正常路径：set_input_files(规格一致的 PNG) → 等到缩略图 → 无异常。"""
    good = tmp_path / "cover.png"
    good.write_bytes(png_bytes(1140, 640))
    page = FakeCoverPage()

    _adapter().upload_cover(page, good)

    selector, uploaded = page.uploads[0]
    assert selector == _adapter().selectors("cover", "upload_input")
    assert uploaded == good


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

    good = tmp_path / "cover.png"
    good.write_bytes(png_bytes(1140, 640))
    with pytest.raises(CoverError):
        _adapter(meta).upload_cover(FakeCoverPage(), good)


def test_upload_input_rejection_is_wrapped_as_cover_error(tmp_path: Path):
    """上传框元素不存在（改版）→ CoverError 说清是哪一步，不裸漏 playwright 异常。"""
    good = tmp_path / "cover.png"
    good.write_bytes(png_bytes(1140, 640))
    with pytest.raises(CoverError) as exc:
        _adapter().upload_cover(FakeCoverPage(upload_fails=True), good)
    assert "上传框" in str(exc.value)
