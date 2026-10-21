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


def test_png_zero_dimensions_are_illegal(tmp_path: Path):
    """IHDR 里写 width=0 是非法 PNG（规范要求正数），不许当尺寸读出去。"""
    with pytest.raises(ImageInfoError) as exc:
        image_size(_img(tmp_path, "g.png", png_bytes(0, 5)))
    assert "非法" in str(exc.value)


def test_jpeg_skips_padding_bytes(tmp_path: Path):
    """段间的 0x00 填充字节（少见但合法）要跳过，不许误判成损坏。"""
    data = jpeg_bytes(800, 450)
    padded = data[:2] + b"\x00" + data[2:]
    assert image_size(_img(tmp_path, "h.jpg", padded)) == (800, 450)


def test_jpeg_corrupt_segment_structure_raises(tmp_path: Path):
    """非 FF 非填充的垃圾字节 → 报损坏，不往后瞎读。"""
    with pytest.raises(ImageInfoError) as exc:
        image_size(_img(tmp_path, "i.jpg", b"\xff\xd8\x05\x00\xff\xd9"))
    assert "损坏" in str(exc.value)


def test_jpeg_incomplete_sof_raises(tmp_path: Path):
    """SOF 段头在、尺寸字段被截断 → 报错，不越界读。"""
    with pytest.raises(ImageInfoError) as exc:
        image_size(_img(tmp_path, "j.jpg", b"\xff\xd8\xff\xc0\x00\x11\x08"))
    assert "不完整" in str(exc.value)


def test_jpeg_zero_dimensions_are_illegal(tmp_path: Path):
    import struct

    bad = b"\xff\xd8\xff\xc0" + struct.pack(">H", 17) + bytes([8]) + struct.pack(">HH", 0, 100) + b"\x03" + b"\x00" * 9
    with pytest.raises(ImageInfoError) as exc:
        image_size(_img(tmp_path, "k.jpg", bad))
    assert "非法" in str(exc.value)


def test_jpeg_segment_length_below_two_raises(tmp_path: Path):
    """段长度字段 <2（连自身长度都装不下）是损坏文件，报错。"""
    with pytest.raises(ImageInfoError) as exc:
        image_size(_img(tmp_path, "l.jpg", b"\xff\xd8\xff\xe0\x00\x00"))
    assert "长度" in str(exc.value)
