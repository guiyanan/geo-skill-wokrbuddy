"""图片尺寸嗅探：只读文件头，零第三方依赖。

为什么不引 PIL：这条路径要在「发布前预检」里跑，多一个 C 扩展依赖就多一个
装不上的环境；而封面只由 T-15 产出 PNG，需要的信息全在文件头前几十个字节里。

两种格式的判定都**必须**读到位才返回，读不到就抛错：
「猜一个尺寸」比「明确报错」危险得多——猜对了没人知道，猜错了封面被平台拒绝，
而报错信息里写着「上传失败」，排查方向直接从「图不对」跑偏到「网络不好」。
"""

from __future__ import annotations

import struct
from pathlib import Path

__all__ = ["ImageInfoError", "image_size"]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
JPEG_MAGIC = b"\xff\xd8"
# PNG 的 IHDR 必须是第一个 chunk：签名 8 字节 + 长度 4 + 类型 4，宽高紧随其后
PNG_IHDR_OFFSET = 16


class ImageInfoError(RuntimeError):
    """读不出图片尺寸：格式不支持、文件损坏或文件不存在。"""


def image_size(path: str | Path) -> tuple[int, int]:
    """返回 `(width, height)`。PNG/JPEG 之外一律 `ImageInfoError`。"""
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ImageInfoError(f"读不到图片文件：{path}（{exc}）") from exc

    if data.startswith(PNG_MAGIC):
        return _png_size(data, path)
    if data.startswith(JPEG_MAGIC):
        return _jpeg_size(data, path)
    raise ImageInfoError(
        f"识别不出图片格式（只支持 PNG/JPEG）：{path}，头 8 字节 {data[:8]!r}"
    )


def _png_size(data: bytes, path: Path) -> tuple[int, int]:
    if len(data) < PNG_IHDR_OFFSET + 8 or data[12:16] != b"IHDR":
        raise ImageInfoError(f"PNG 头部不完整（缺 IHDR）：{path}")
    width, height = struct.unpack(">II", data[PNG_IHDR_OFFSET : PNG_IHDR_OFFSET + 8])
    if width <= 0 or height <= 0:
        raise ImageInfoError(f"PNG 尺寸非法：{width}x{height}（{path}）")
    return width, height


# SOF0..SOF15 减去 DHT(0xC4)/JPG(0xC8)/DAC(0xCC)：这三段也落在同一个区间，但不是 SOF
_SOF_MARKERS = frozenset(range(0xC0, 0xD0)) - {0xC4, 0xC8, 0xCC}


def _jpeg_size(data: bytes, path: Path) -> tuple[int, int]:
    """遍历 JPEG 段找 SOF：段结构是 FF <marker> <2 字节长度，长度含自身。"""
    index = 2
    length = len(data)
    while index + 4 <= length:
        if data[index] != 0xFF:
            # 段间有填充 0x00（少见但合法）就跳过；其它情况当作损坏
            if data[index] == 0x00:
                index += 1
                continue
            raise ImageInfoError(f"JPEG 段结构损坏（偏移 {index}）：{path}")
        marker = data[index + 1]
        if marker in _SOF_MARKERS:
            if index + 9 > length:
                raise ImageInfoError(f"JPEG SOF 段不完整：{path}")
            height, width = struct.unpack(">HH", data[index + 5 : index + 9])
            if width <= 0 or height <= 0:
                raise ImageInfoError(f"JPEG 尺寸非法：{width}x{height}（{path}）")
            return width, height
        segment_len = struct.unpack(">H", data[index + 2 : index + 4])[0]
        if segment_len < 2:
            raise ImageInfoError(f"JPEG 段长度非法（偏移 {index}）：{path}")
        index += 2 + segment_len
    raise ImageInfoError(f"JPEG 里找不到 SOF 段（{length} 字节）：{path}")
