"""T-15 封面生成：AI 主视觉 + 代码叠字（PRD R5 / 架构 §3.9）。

两层分离的原因（架构 §3.9 原话）：模型拼中文标题容易出错字/怪字，生成出来
不能用。所以——

1. **主视觉**：AI 生成、**不带文字**的 PNG，人工放进稿件目录（或 --visual
   指定）。本模块对它**只读**：改标题重新渲染时主视觉文件一个字节都不动
   （UT-15-02 的哈希断言钉的就是这件事）。
2. **标题字**：代码叠加。渲染方式是 HTML + 无头浏览器截图——字体、字号、
   位置全部可控，改标题只是重新截一张图，不重新生图。

产出三张卡进稿件目录：

- `cover.png`：成品，尺寸与平台规格严格相等（UT-15-01），字节数不超平台
  上限（UT-15-03）。校验不过的产物**当场销毁**，不留在盘上骗人。
- `cover-preview.html`：G2 人工抽检页。打开它确认两项——标题无错别字、
  主视觉无水印/多余文字叠加——然后 `geopublish cover <dir> --approve`。
- `cover.json`：渲染记录 + 审批状态。审批绑定封面文件的 sha256：
  「审的是 A 图、发出去是 B 图」这条缝必须焊死（确认后封面被改动 →
  发布闸照样拦）。

**未确认的封面不进入发布流程**（DoD）：`publish.py` 在 publish 模式、
开浏览器之前调 `require_cover_approved`，不过这关就原地停下。

字体纪律（UT-15-04）：字体缺失/缺字形时 **fail fast**，不产出空白图。
字形级校验靠解析字体 cmap 表（纯 struct，零依赖，与 imageinfo 同一哲学：
需要的信息都在文件头里，猜比报错危险）。
"""

from __future__ import annotations

import base64
import hashlib
import html as html_module
import json
import os
import shutil
import struct
import tempfile
from bisect import bisect_right
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..adapters.base import CoverSpec
from ..adapters.imageinfo import PNG_MAGIC, ImageInfoError, image_size
from ..core.clock import Clock, SystemClock
from ..core.models import Meta, ModelError

__all__ = [
    "COVER_FILENAME",
    "PREVIEW_FILENAME",
    "RECORD_FILENAME",
    "VISUAL_FILENAME",
    "CoverNotApproved",
    "CoverRecord",
    "CoverRenderError",
    "CoverResult",
    "CoverShooterUnavailable",
    "PlaywrightShooter",
    "Shooter",
    "approve_cover",
    "build_cover_html",
    "find_missing_glyphs",
    "load_font_coverage",
    "load_record",
    "render_cover",
    "require_cover_approved",
    "resolve_font",
]

COVER_FILENAME = "cover.png"
RECORD_FILENAME = "cover.json"
PREVIEW_FILENAME = "cover-preview.html"
VISUAL_FILENAME = "assets/visual.png"

# 字体候选清单：--font 没给时按顺序找第一个存在的。
# 没有跨平台统一的「系统中文字体」路径，硬编码一个就是猜环境；
# 全落空时报错让人显式给——这正是 UT-15-04 要的 fail fast。
DEFAULT_FONT_CANDIDATES: tuple[str, ...] = (
    "/System/Library/Fonts/PingFang.ttc",                       # macOS 苹方
    "/System/Library/Fonts/Hiragino Sans GB.ttc",               # macOS 冬青黑体
    "/System/Library/Fonts/STHeiti Medium.ttc",                 # macOS 黑体
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",      # Linux Noto
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
    "C:/Windows/Fonts/msyh.ttc",                                # Windows 雅黑
)

# 字号分档：标题越长字越小。版式宽度约 1012px（1140 - 两侧 64  padding），
# 两行封顶——档位按「每行大约能放几个字」反推。
_FONT_SIZE_TIERS: tuple[tuple[int, int], ...] = (
    (16, 72),
    (28, 56),
    (40, 44),
)


class CoverRenderError(ValueError):
    """封面生成的本地拒绝：输入缺失、字体不行、产物不合格。

    全部发生在产出正式文件之前（或当场销毁产物）——一张带病封面流到
    平台后台，就变成要人工清理的垃圾。
    """


class CoverShooterUnavailable(RuntimeError):
    """无头浏览器不可用/渲染失败。对应 CLI 的环境类退出码（修机器，不是改稿子）。"""


class CoverNotApproved(RuntimeError):
    """封面未过 G2 人工抽检。发布流程在open_page之前被这道闸拦下。"""


# ---------------------------------------------------------------- 字体

def resolve_font(font: str | Path | None) -> Path:
    """定字体文件。显式路径优先；缺省按候选清单探测；全落空 → fail fast。"""
    if font is not None:
        path = Path(font).expanduser()
        if not path.is_file():
            raise CoverRenderError(
                f"字体文件不存在：{path}（UT-15-04：字体缺失宁可失败，不产出空白图）"
            )
        return path
    for candidate in DEFAULT_FONT_CANDIDATES:
        path = Path(candidate)
        if path.is_file():
            return path
    raise CoverRenderError(
        "找不到可用的中文字体文件。用 --font 显式指定一个 .ttf/.otf/.ttc；"
        f"已试过：{', '.join(DEFAULT_FONT_CANDIDATES)}"
    )


# 字形覆盖用「有序不相交区间」表示，而不是把码点全展开成 set：
# CJK 字体的 cmap 区间很大（一个 group 可能跨两万码点），展开就是几百万个
# int 的内存；区间 + 二分查找只要几百个字节。
GlyphCoverage = tuple[tuple[int, int], ...]


def load_font_coverage(font_data: bytes) -> GlyphCoverage:
    """解析 TTF/OTF/TTC 的 cmap 表，返回有字形的 Unicode 码点区间。

    只认 format 4（BMP）与 format 12（全平面）——这两种覆盖所有常见中文字体；
    别的格式不猜，直接报错（UT-15-04 的另一半：看不懂的字体也不许产空白图）。
    """
    if len(font_data) < 16:
        raise CoverRenderError(f"字体文件太小（{len(font_data)} 字节），不是合法的 TTF/OTF/TTC")
    face = _face_offset(font_data)
    cmap_off = _find_table(font_data, face, b"cmap")
    return _read_cmap(font_data, cmap_off)


def _face_offset(data: bytes) -> int:
    """TTC 是多个 face 打包，取第一个；裸 TTF/OTF 偏移为 0。"""
    tag = data[:4]
    if tag == b"ttcf":
        count = struct.unpack(">I", data[8:12])[0]
        if count < 1:
            raise CoverRenderError("TTC 文件里没有任何字体（numFonts=0）")
        return struct.unpack(">I", data[12:16])[0]
    if tag in (b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"):
        return 0
    raise CoverRenderError(f"不是 TTF/OTF/TTC 字体：头 4 字节 {tag!r}")


def _find_table(data: bytes, face: int, wanted: bytes) -> int:
    """在 sfnt 表目录里找一张表，返回它的绝对偏移。"""
    if face + 12 > len(data):
        raise CoverRenderError("字体头部不完整（读不到表目录）")
    num_tables = struct.unpack(">H", data[face + 4 : face + 6])[0]
    base = face + 12
    for i in range(num_tables):
        rec = base + i * 16
        if rec + 16 > len(data):
            raise CoverRenderError("字体表目录越界（文件被截断？）")
        if data[rec : rec + 4] == wanted:
            off, length = struct.unpack(">II", data[rec + 8 : rec + 16])
            if off + length > len(data):
                raise CoverRenderError(f"{wanted.decode()!r} 表越界（文件被截断？）")
            return off
    raise CoverRenderError(f"字体里没有 {wanted.decode()!r} 表：无法校验字形覆盖")


def _read_cmap(data: bytes, off: int) -> GlyphCoverage:
    """从 cmap 表里挑最优子表：format 12 优先（覆盖 BMP 之外），format 4 兜底。"""
    if off + 4 > len(data):
        raise CoverRenderError("cmap 表头不完整")
    count = struct.unpack(">H", data[off + 2 : off + 4])[0]
    fmt12: int | None = None
    fmt4: int | None = None
    for i in range(count):
        rec = off + 4 + i * 8
        if rec + 8 > len(data):
            raise CoverRenderError("cmap 子表目录越界（文件被截断？）")
        sub_off = off + struct.unpack(">I", data[rec + 4 : rec + 8])[0]
        if sub_off + 2 > len(data):
            raise CoverRenderError("cmap 子表偏移越界（文件被截断？）")
        fmt = struct.unpack(">H", data[sub_off : sub_off + 2])[0]
        if fmt == 12 and fmt12 is None:
            fmt12 = sub_off
        elif fmt == 4 and fmt4 is None:
            fmt4 = sub_off
    if fmt12 is not None:
        return _coverage_format12(data, fmt12)
    if fmt4 is not None:
        return _coverage_format4(data, fmt4)
    raise CoverRenderError("cmap 里没有 format 4/12 子表（不支持的字形映射格式）")


def _coverage_format12(data: bytes, off: int) -> GlyphCoverage:
    """format 12：一组 [startCharCode, endCharCode] 区间，直接就是我们要的形状。"""
    if off + 16 > len(data):
        raise CoverRenderError("cmap format 12 表头不完整")
    n_groups = struct.unpack(">I", data[off + 12 : off + 16])[0]
    ranges: list[tuple[int, int]] = []
    for i in range(n_groups):
        g = off + 16 + i * 12
        if g + 12 > len(data):
            raise CoverRenderError("cmap format 12 group 越界（文件被截断？）")
        start, end = struct.unpack(">II", data[g : g + 8])
        if end < start:
            raise CoverRenderError(f"cmap format 12 group 区间倒挂：{start} > {end}")
        ranges.append((start, end))
    if not ranges:
        raise CoverRenderError("cmap format 12 没有任何 group（空字体？）")
    return _normalize_ranges(ranges)


def _coverage_format4(data: bytes, off: int) -> GlyphCoverage:
    """format 4：分段映射 BMP。我们只要覆盖区间，不关心 glyph id。"""
    if off + 16 > len(data):
        raise CoverRenderError("cmap format 4 表头不完整")
    length = struct.unpack(">H", data[off + 2 : off + 4])[0]
    if off + length > len(data):
        raise CoverRenderError("cmap format 4 越界（文件被截断？）")
    seg_count = struct.unpack(">H", data[off + 6 : off + 8])[0] // 2
    end_base = off + 14
    start_base = end_base + seg_count * 2 + 2  # 中间隔一个 reservedPad
    if start_base + seg_count * 2 > off + length:
        raise CoverRenderError("cmap format 4 段数组不完整")
    ranges: list[tuple[int, int]] = []
    for i in range(seg_count):
        end = struct.unpack(">H", data[end_base + i * 2 : end_base + i * 2 + 2])[0]
        start = struct.unpack(">H", data[start_base + i * 2 : start_base + i * 2 + 2])[0]
        if start == 0xFFFF and end == 0xFFFF:
            continue  # 规范要求的收尾哨兵段，不是真覆盖
        if end < start:
            raise CoverRenderError(f"cmap format 4 段区间倒挂：{start} > {end}")
        ranges.append((start, end))
    if not ranges:
        raise CoverRenderError("cmap format 4 没有任何有效段（空字体？）")
    return _normalize_ranges(ranges)


def _normalize_ranges(ranges: list[tuple[int, int]]) -> GlyphCoverage:
    """排序并合并相邻/重叠区间，让二分查找成立。"""
    ordered = sorted(ranges)
    merged: list[list[int]] = [[ordered[0][0], ordered[0][1]]]
    for start, end in ordered[1:]:
        if start <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return tuple((s, e) for s, e in merged)


def _covers(coverage: GlyphCoverage, codepoint: int) -> bool:
    starts = [s for s, _ in coverage]
    idx = bisect_right(starts, codepoint) - 1
    return idx >= 0 and coverage[idx][0] <= codepoint <= coverage[idx][1]


def find_missing_glyphs(coverage: GlyphCoverage, text: str) -> str:
    """标题里字体覆盖不到的字符，按出现顺序去重返回。空串 = 全覆盖。"""
    missing: list[str] = []
    for ch in dict.fromkeys(text):
        if ch.isspace() or _covers(coverage, ord(ch)):
            continue
        missing.append(ch)
    return "".join(missing)


# ---------------------------------------------------------------- HTML 叠字

def _title_font_size(title: str) -> int:
    """字号分档。长标题用小字而不是无限缩——截断发生在 CSS（两行封顶）。"""
    n = len(title)
    for limit, size in _FONT_SIZE_TIERS:
        if n <= limit:
            return size
    return 36


def build_cover_html(
    *,
    title: str,
    visual_data: bytes,
    font_data: bytes,
    font_family: str,
    spec: CoverSpec,
) -> str:
    """叠字页：主视觉做满幅背景，标题压在底部渐变条上，两行封顶。

    一切资源内嵌成 data URI——渲染页不读任何外部文件/网络，截图结果
    只由输入决定（可复现是 UT-15-02 能成立的前提）。标题走 HTML 转义：
    标题来自 meta.json，带 < 或引号不该把页面结构戳破。
    """
    title_html = html_module.escape(title, quote=True)
    font_b64 = base64.b64encode(font_data).decode("ascii")
    visual_b64 = base64.b64encode(visual_data).decode("ascii")
    size = _title_font_size(title)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<style>
@font-face {{
  font-family: "{font_family}";
  src: url(data:font/ttf;base64,{font_b64});
}}
html, body {{
  margin: 0; padding: 0;
  width: {spec.width}px; height: {spec.height}px;
  overflow: hidden;
}}
.visual {{
  position: absolute; inset: 0;
  background: url(data:image/png;base64,{visual_b64}) center / cover no-repeat;
}}
.shade {{
  position: absolute; left: 0; right: 0; bottom: 0; height: 45%;
  background: linear-gradient(to bottom, rgba(0,0,0,0), rgba(0,0,0,0.72));
}}
.title {{
  position: absolute; left: 64px; right: 64px; bottom: 48px;
  font-family: "{font_family}", sans-serif;
  font-size: {size}px; font-weight: 700; line-height: 1.3;
  color: #ffffff; letter-spacing: 1px;
  display: -webkit-box; -webkit-box-orient: vertical;
  -webkit-line-clamp: 2; overflow: hidden;
  word-break: break-all;
  text-shadow: 0 2px 12px rgba(0,0,0,0.45);
}}
</style>
</head>
<body>
<div class="visual"></div>
<div class="shade"></div>
<div class="title">{title_html}</div>
</body>
</html>
"""


# ---------------------------------------------------------------- 渲染器

class Shooter(Protocol):
    """把 HTML 截成 PNG 的最小接口。注入它，单测就不必开浏览器。"""

    def shoot(self, html: str, width: int, height: int, out: Path) -> None:  # pragma: no cover - Protocol 只声明
        ...


def _default_launcher() -> Callable[..., Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise CoverShooterUnavailable(
            "没装 playwright，封面渲染不可用。修复：\n"
            "  pip install playwright && playwright install chromium\n"
            "（或装好系统 Chrome，用默认 channel=chrome）"
        ) from exc
    return sync_playwright


class PlaywrightShooter:
    """无头浏览器截图渲染。`launcher` 可注入——与 snapshot.PlaywrightDriver 同一套打法。

    `device_scale_factor=1` 是 UT-15-01 的硬保证：输出像素尺寸 == viewport
    CSS 尺寸 == 平台规格。不锁它，高分屏机器会截出 2280x1280 的图。
    """

    def __init__(
        self,
        *,
        launcher: Callable[..., Any] | None = None,
        channel: str | None = "chrome",
        timeout_ms: int = 15_000,
    ) -> None:
        self._launcher = launcher
        self._channel = channel
        self._timeout_ms = timeout_ms

    def shoot(self, html: str, width: int, height: int, out: Path) -> None:
        launcher = self._launcher or _default_launcher()
        try:
            with launcher() as playwright:
                launch_kwargs: dict[str, Any] = {"headless": True}
                if self._channel:
                    launch_kwargs["channel"] = self._channel
                browser = playwright.chromium.launch(**launch_kwargs)
                try:
                    page = browser.new_page(
                        viewport={"width": width, "height": height},
                        device_scale_factor=1,
                    )
                    page.set_content(html, wait_until="load", timeout=self._timeout_ms)
                    page.screenshot(path=str(out))
                finally:
                    browser.close()
        except Exception as exc:
            raise CoverShooterUnavailable(f"无头浏览器渲染失败：{exc}") from exc


# ---------------------------------------------------------------- 渲染编排

@dataclass(frozen=True, slots=True)
class CoverRecord:
    """cover.json：一次渲染的完整记录 + G2 审批状态。

    `cover_sha` 把审批钉死在具体文件上：审批后封面被换/被改，sha 对不上，
    发布闸照拦——审 A 发 B 这条缝不存在。`previous_approval` 学 T-13 的
    REVIEW.md：重新渲染会冲掉审批状态，但上一次「谁什么时候确认的」要留痕。
    """

    title: str
    width: int
    height: int
    size_bytes: int
    visual_sha: str
    cover_sha: str
    font: str
    rendered_at: str
    approved: bool = False
    approved_by: str = ""
    approved_at: str = ""
    previous_approval: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": 1,
            "title": self.title,
            "width": self.width,
            "height": self.height,
            "size_bytes": self.size_bytes,
            "visual_sha": self.visual_sha,
            "cover_sha": self.cover_sha,
            "font": self.font,
            "rendered_at": self.rendered_at,
            "approved": self.approved,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at,
            "previous_approval": self.previous_approval,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], where: Path) -> CoverRecord:
        known = {
            "schema", "title", "width", "height", "size_bytes", "visual_sha",
            "cover_sha", "font", "rendered_at", "approved", "approved_by",
            "approved_at", "previous_approval",
        }
        unknown = sorted(set(data) - known)
        if unknown:
            raise CoverRenderError(f"{where} 未知字段：{', '.join(unknown)}")
        missing = [k for k in ("title", "cover_sha", "visual_sha", "rendered_at") if k not in data]
        if missing:
            raise CoverRenderError(f"{where} 缺必填字段：{', '.join(missing)}")
        for field in ("width", "height", "size_bytes"):
            value = data.get(field)
            if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                raise CoverRenderError(f"{where} 的 {field} 必须是正整数，收到 {value!r}")
        if not isinstance(data.get("approved", False), bool):
            raise CoverRenderError(f"{where} 的 approved 必须是 true/false")
        return cls(
            title=str(data["title"]),
            width=data["width"],
            height=data["height"],
            size_bytes=data["size_bytes"],
            visual_sha=str(data["visual_sha"]),
            cover_sha=str(data["cover_sha"]),
            font=str(data.get("font", "")),
            rendered_at=str(data["rendered_at"]),
            approved=data.get("approved", False),
            approved_by=str(data.get("approved_by", "")),
            approved_at=str(data.get("approved_at", "")),
            previous_approval=str(data.get("previous_approval", "")),
        )


@dataclass(frozen=True, slots=True)
class CoverResult:
    """一次成功渲染的产物清单。approved 恒为 False——渲染和确认是两个动作。"""

    draft_dir: Path
    cover_path: Path
    record_path: Path
    preview_path: Path
    visual_path: Path
    title: str
    width: int
    height: int
    size_bytes: int
    cover_sha: str


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _load_meta(root: Path) -> Meta:
    meta_file = root / "meta.json"
    if not meta_file.is_file():
        raise CoverRenderError(f"缺 meta.json：{root}（封面标题默认取自它）")
    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CoverRenderError(f"meta.json 不是合法 JSON（{meta_file}）：{exc}") from exc
    if not isinstance(data, dict):
        raise CoverRenderError(f"meta.json 顶层必须是对象（{meta_file}）")
    try:
        return Meta.from_json(data)
    except ModelError as exc:
        raise CoverRenderError(f"meta.json 不合法（{meta_file}）：{exc}") from exc


def _ensure_visual(root: Path, visual: str | Path | None) -> Path:
    """定主视觉：默认稿件目录里的 assets/visual.png；外部给的先固化进去。

    固化（复制进稿件目录）是为了自包含：审批记的是这份文件的 sha，
    外部那份后来被人改掉不影响已审批内容。已存在且内容不同 → 拒绝覆盖，
    换视觉要先人工删旧文件——静默换图等于把「审过的是哪张」变成糊涂账。
    """
    target = root / VISUAL_FILENAME
    if visual is None:
        if not target.is_file():
            raise CoverRenderError(
                f"缺主视觉：{target}。把 AI 生成的无文字 PNG 放到这里，或用 --visual 指定"
            )
        return target
    src = Path(visual).expanduser()
    if not src.is_file():
        raise CoverRenderError(f"主视觉文件不存在：{src}")
    if src.resolve() == target.resolve():
        return target
    if target.exists():
        if _sha256(target) == _sha256(src):
            return target  # 同一份图，直接复用
        raise CoverRenderError(
            f"主视觉已存在且内容不同：{target}。要换视觉请先人工删除旧文件（不静默换图）"
        )
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src, target)
    return target


def _verify_output(path: Path, spec: CoverSpec) -> tuple[int, int, int]:
    """产物的三道验收：是合法图、尺寸=规格、字节数不超限。返回 (w, h, bytes)。"""
    try:
        width, height = image_size(path)
    except ImageInfoError as exc:
        raise CoverRenderError(f"渲染产物不是合法图片：{exc}") from exc
    if (width, height) != (spec.width, spec.height):
        raise CoverRenderError(
            f"渲染尺寸 {width}x{height} ≠ 平台要求 {spec.width}x{spec.height}（UT-15-01）"
        )
    size = path.stat().st_size
    if size > spec.max_bytes:
        raise CoverRenderError(
            f"封面 {size} 字节超过平台上限 {spec.max_bytes}（UT-15-03）"
        )
    return width, height, size


def render_cover(
    draft_dir: str | Path,
    *,
    spec: CoverSpec,
    visual: str | Path | None = None,
    title: str | None = None,
    font: str | Path | None = None,
    shooter: Shooter | None = None,
    clock: Clock | None = None,
) -> CoverResult:
    """主视觉 + 标题 → cover.png + cover.json + cover-preview.html。

    失败语义：**要么完整产出，要么什么都不留**。校验不过的渲染产物当场
    删除；正式文件（cover.png / cover.json）只在全部验收通过后落盘，
    所以盘上不会同时存在「新封面 + 旧记录」这种自相矛盾的状态。

    重新渲染（改标题）会重置审批：cover.json 里 approved 归 False，
    上一次确认记进 previous_approval。这是刻意的——新图没被眼睛看过，
    就不能进发布流程（UT-15-02 的另一半）。
    """
    clk = clock or SystemClock()
    root = Path(draft_dir)
    if not root.is_dir():
        raise CoverRenderError(f"稿件目录不存在：{root}")
    meta = _load_meta(root)

    final_title = (title if title is not None else meta.title).strip()
    if not final_title:
        raise CoverRenderError("封面标题为空：--title 或 meta.json 的 title 必须有一个")

    visual_path = _ensure_visual(root, visual)
    visual_data = visual_path.read_bytes()
    if not visual_data.startswith(PNG_MAGIC):
        raise CoverRenderError(
            f"主视觉必须是 PNG（AI 生图出口统一 PNG，不猜来路不明的格式）：{visual_path}"
        )
    try:
        image_size(visual_path)
    except ImageInfoError as exc:
        raise CoverRenderError(f"主视觉不是完整的 PNG：{exc}") from exc

    font_path = resolve_font(font)
    font_data = font_path.read_bytes()
    coverage = load_font_coverage(font_data)
    missing = find_missing_glyphs(coverage, final_title)
    if missing:
        raise CoverRenderError(
            f"字体 {font_path.name} 缺 {len(missing)} 个字形：{missing}。"
            "渲出来是豆腐块——换字体（--font），不产出空白图（UT-15-04）"
        )

    page_html = build_cover_html(
        title=final_title,
        visual_data=visual_data,
        font_data=font_data,
        font_family="GeopublishCover",
        spec=spec,
    )

    shooter = shooter if shooter is not None else PlaywrightShooter()
    # 先截到同目录的临时文件：校验不过就删，校验过才原子替换正式封面
    fd, tmp_name = tempfile.mkstemp(dir=root, prefix=".cover.", suffix=".png")
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shooter.shoot(page_html, spec.width, spec.height, tmp)
        width, height, size = _verify_output(tmp, spec)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, root / COVER_FILENAME)

    previous = ""
    old_record = _try_load_record(root)
    if old_record is not None and old_record.approved:
        previous = f"{old_record.approved_by} @ {old_record.approved_at}"

    record = CoverRecord(
        title=final_title,
        width=width,
        height=height,
        size_bytes=size,
        visual_sha=hashlib.sha256(visual_data).hexdigest(),
        cover_sha=_sha256(root / COVER_FILENAME),
        font=str(font_path),
        rendered_at=clk.now().isoformat(),
        previous_approval=previous,
    )
    _save_record(root, record)
    _write_preview(root, record)
    _update_meta(root, meta)

    return CoverResult(
        draft_dir=root,
        cover_path=root / COVER_FILENAME,
        record_path=root / RECORD_FILENAME,
        preview_path=root / PREVIEW_FILENAME,
        visual_path=visual_path,
        title=final_title,
        width=width,
        height=height,
        size_bytes=size,
        cover_sha=record.cover_sha,
    )


def _update_meta(root: Path, meta: Meta) -> None:
    """把 cover 字段回写进 meta.json，发布链（load_draft）才能找到封面。"""
    if meta.cover == COVER_FILENAME:
        return  # 已经指对了，不动盘
    updated = replace(meta, cover=COVER_FILENAME)
    (root / "meta.json").write_text(
        json.dumps(updated.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


# ---------------------------------------------------------------- 审批（G2）

def load_record(draft_dir: str | Path) -> CoverRecord:
    root = Path(draft_dir)
    record_file = root / RECORD_FILENAME
    if not record_file.is_file():
        raise CoverRenderError(f"缺 {RECORD_FILENAME}：{root}（先跑 geopublish cover 生成封面）")
    try:
        data = json.loads(record_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CoverRenderError(f"{RECORD_FILENAME} 不是合法 JSON（{record_file}）：{exc}") from exc
    if not isinstance(data, dict):
        raise CoverRenderError(f"{RECORD_FILENAME} 顶层必须是对象（{record_file}）")
    return CoverRecord.from_json(data, record_file)


def _try_load_record(root: Path) -> CoverRecord | None:
    try:
        return load_record(root)
    except CoverRenderError:
        return None


def _save_record(root: Path, record: CoverRecord) -> None:
    (root / RECORD_FILENAME).write_text(
        json.dumps(record.to_json(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def approve_cover(draft_dir: str | Path, *, by: str, clock: Clock | None = None) -> CoverRecord:
    """人工抽检通过后的确认动作（G2）：两项都看过，才准写 approved=true。

    确认前再核一次文件 sha：盘上封面和渲染记录不一致，说明它在渲染后
    被动过——这种封面不能批，批了就是把审批变成走过场。
    """
    clk = clock or SystemClock()
    root = Path(draft_dir)
    reviewer = (by or "").strip()
    if not reviewer:
        raise CoverRenderError("审批人不能为空：--by 写上名字，抽检才留痕（G2）")
    record = load_record(root)
    cover = root / COVER_FILENAME
    if not cover.is_file():
        raise CoverRenderError(f"封面不存在：{cover}（先跑 geopublish cover 生成）")
    if _sha256(cover) != record.cover_sha:
        raise CoverRenderError(
            "封面文件与渲染记录不一致（渲染后被改动/替换？）：重新渲染并重新过目后再确认"
        )
    if record.approved:
        raise CoverRenderError(
            f"封面已是确认状态（{record.approved_by} @ {record.approved_at}）："
            "重新渲染后才需要再次确认"
        )
    confirmed = replace(
        record, approved=True, approved_by=reviewer, approved_at=clk.now().isoformat()
    )
    _save_record(root, confirmed)
    _write_preview(root, confirmed)
    return confirmed


def require_cover_approved(draft_dir: str | Path, cover_path: str | Path) -> None:
    """发布闸：未过 G2 抽检的封面不进发布流程（DoD 原话）。

    publish 模式在 open_page 之前调它——为一张没确认的封面启动浏览器，
    和为一个不存在的封面启动浏览器一样冤。
    """
    root = Path(draft_dir)
    try:
        record = load_record(root)
    except CoverRenderError as exc:
        raise CoverNotApproved(f"封面未过人工抽检：{exc}") from exc
    if not record.approved:
        raise CoverNotApproved(
            f"封面未经人工确认（G2）：打开 {root / PREVIEW_FILENAME} 核对两项"
            f"（标题无错别字、主视觉无水印/多余文字），然后 geopublish cover {root} --approve --by <你的名字>"
        )
    actual = _sha256(Path(cover_path))
    if actual != record.cover_sha:
        raise CoverNotApproved(
            "封面在确认后被改动（sha 对不上）：审批绑定的是当时那张图，"
            "现在的封面必须重新过目并确认"
        )


# ---------------------------------------------------------------- 预览页（G2）

def _write_preview(root: Path, record: CoverRecord) -> None:
    """人工抽检页：封面大图 + 两项核对清单 + 当前审批状态。

    图片内嵌 base64——预览页不依赖相对路径，单独发给别人看也行。
    """
    cover_b64 = base64.b64encode((root / COVER_FILENAME).read_bytes()).decode("ascii")
    if record.approved:
        status = f'<p class="ok">已确认：{html_module.escape(record.approved_by)} @ {html_module.escape(record.approved_at)}</p>'
        hint = ""
    else:
        status = '<p class="pending">待人工确认</p>'
        hint = (
            '<p class="cmd">确认通过：<code>geopublish cover '
            + html_module.escape(str(root))
            + " --approve --by 你的名字</code></p>"
        )
    previous = (
        f'<p class="prev">上次抽检：{html_module.escape(record.previous_approval)}（本次渲染后需重新确认）</p>'
        if record.previous_approval
        else ""
    )
    page = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>封面抽检 - {html_module.escape(record.title)}</title>
<style>
body {{ font-family: sans-serif; max-width: 960px; margin: 32px auto; padding: 0 16px; color: #1a1a1a; }}
img {{ width: 100%; border: 1px solid #ddd; border-radius: 4px; }}
ul {{ line-height: 2; }}
.ok {{ color: #0a7a33; font-weight: 700; }}
.pending {{ color: #b26a00; font-weight: 700; }}
.cmd code {{ background: #f4f4f4; padding: 2px 6px; border-radius: 4px; }}
.prev {{ color: #666; }}
.meta {{ color: #666; font-size: 13px; }}
</style>
</head>
<body>
<h1>封面人工抽检（G2）</h1>
{status}
{previous}
<img src="data:image/png;base64,{cover_b64}" alt="cover">
<ul>
<li>标题文字无错别字、无怪字</li>
<li>主视觉无水印、无多余文字叠加</li>
</ul>
{hint}
<p class="meta">标题：{html_module.escape(record.title)}<br>
尺寸：{record.width}x{record.height} · {record.size_bytes} 字节 · 渲染于 {html_module.escape(record.rendered_at)}<br>
cover_sha：{record.cover_sha}</p>
</body>
</html>
"""
    (root / PREVIEW_FILENAME).write_text(page, encoding="utf-8")
