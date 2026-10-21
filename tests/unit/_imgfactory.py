"""测试用图片工厂：纯标准库造 PNG / JPEG 字节，不引入 PIL。

只造「头都对」的图——imageinfo 只读头，不需要合法的像素数据。
"""

from __future__ import annotations

import struct
import zlib


def png_bytes(width: int, height: int) -> bytes:
    """最小合法 PNG：IHDR(8bit RGB) + 空像素 IDAT + IEND。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + b"\x00" * (width * 3)  # 每行前置 filter byte
    raw = row * height
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes(width: int, height: int) -> bytes:
    """最小 JPEG：SOI + APP0 + SOF0(带宽高) + EOI。无熵编码数据——读尺寸不需要。"""
    sof0 = b"\xff\xc0" + struct.pack(">H", 17) + bytes([8]) + struct.pack(">HH", height, width) + (
        bytes([3]) + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
    )
    return (
        b"\xff\xd8"
        + b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + sof0
        + b"\xff\xd9"
    )
