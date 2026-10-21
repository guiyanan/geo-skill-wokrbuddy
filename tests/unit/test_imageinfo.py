"""T-08 图片尺寸嗅探（adapters/imageinfo.py）。

只读文件头判尺寸，零第三方依赖（不引 PIL）。支持 PNG / JPEG；
其他格式一律 ImageInfoError——封面只由 T-15 产出 PNG，来路不明的图不猜。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from geopublish.adapters.imageinfo import ImageInfoError, image_size

from _imgfactory import jpeg_bytes, png_bytes

pytestmark = pytest.mark.unit


def _img(tmp_path: Path, name: str, data: bytes) -> Path:
    p = tmp_path / name
    p.write_bytes(data)
    return p


def test_png_dimensions(tmp_path: Path):
    assert image_size(_img(tmp_path, "a.png", png_bytes(1140, 640))) == (1140, 640)


def test_png_square_1x1(tmp_path: Path):
    assert image_size(_img(tmp_path, "b.png", png_bytes(1, 1))) == (1, 1)


def test_jpeg_dimensions(tmp_path: Path):
    assert image_size(_img(tmp_path, "c.jpg", jpeg_bytes(800, 450))) == (800, 450)


def test_unsupported_format_raises(tmp_path: Path):
    with pytest.raises(ImageInfoError) as exc:
        image_size(_img(tmp_path, "d.bmp", b"BM" + b"\x00" * 30))
    assert "识别" in str(exc.value)


def test_truncated_png_raises(tmp_path: Path):
    """PNG 魔数在但 IHDR 没了（截断/损坏）→ 报错，不返回垃圾尺寸。"""
    data = png_bytes(100, 100)[:20]  # 只有签名 + 半个 chunk 头
    with pytest.raises(ImageInfoError):
        image_size(_img(tmp_path, "e.png", data))


def test_truncated_jpeg_raises(tmp_path: Path):
    """JPEG 没有 SOF 段（纯 SOI）→ 报错，不读越界。"""
    with pytest.raises(ImageInfoError):
        image_size(_img(tmp_path, "f.jpg", b"\xff\xd8\xff\xd9"))


def test_missing_file_raises(tmp_path: Path):
    with pytest.raises(ImageInfoError):
        image_size(tmp_path / "nope.png")
