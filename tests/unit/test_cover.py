"""T-15 封面生成的单元测试（UT-15-01~04 + G2 审批流）。

不开浏览器：PlaywrightShooter 走假 launcher（与 test_snapshot.py 同一打法），
渲染编排走 FakeShooter（写固定 PNG 字节）。字体校验用手工构造的最小
TTF/TTC 字节——cmap 解析是纯函数，不需要真字体文件。
"""

from __future__ import annotations

import json
import struct
import sys
from pathlib import Path

import pytest
from _imgfactory import png_bytes

from geopublish.adapters.base import CoverSpec
from geopublish.core.clock import FakeClock
from geopublish.write import cover as cover_mod
from geopublish.write.cover import (
    COVER_FILENAME,
    PREVIEW_FILENAME,
    RECORD_FILENAME,
    CoverNotApproved,
    CoverRecord,
    CoverRenderError,
    CoverShooterUnavailable,
    PlaywrightShooter,
    approve_cover,
    build_cover_html,
    find_missing_glyphs,
    load_font_coverage,
    load_record,
    render_cover,
    require_cover_approved,
    resolve_font,
)

SPEC = CoverSpec(width=1140, height=640, max_bytes=5_242_880)
CLOCK = FakeClock("2026-10-26T20:30:00+08:00")
TITLE = "文档解析为什么是 RAG 的第一道工序？"

# ------------------------------------------------------------- 最小字体构造


def _cmap_format4(ranges: list[tuple[int, int]], *, sentinel: bool = True) -> bytes:
    segs = list(ranges) + ([(0xFFFF, 0xFFFF)] if sentinel else [])
    seg_count = len(segs)
    end_codes = b"".join(struct.pack(">H", e) for _, e in segs)
    start_codes = b"".join(struct.pack(">H", s) for s, _ in segs)
    header = struct.pack(">HHHHHHH", 4, 0, 0, seg_count * 2, 0, 0, 0)
    body = header + end_codes + b"\x00\x00" + start_codes + b"\x00\x00" * seg_count * 2
    return struct.pack(">HHHHHHH", 4, len(body), 0, seg_count * 2, 0, 0, 0) + body[14:]


def _cmap_format12(ranges: list[tuple[int, int]]) -> bytes:
    groups = b"".join(struct.pack(">III", s, e, 0) for s, e in ranges)
    return struct.pack(">HHIII", 12, 0, 16 + len(groups), 0, len(ranges)) + groups


def _ttf(subtable: bytes, *, tag: bytes = b"cmap", base: int = 0) -> bytes:
    cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + subtable
    header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
    # TTC 里表偏移是相对整个文件起点的（OpenType 规范），base 就是 face 的落点
    record = tag + struct.pack(">III", 0, base + 12 + 16, len(cmap))
    return header + record + cmap


def _ttc(subtable: bytes, *, count: int = 1) -> bytes:
    face = _ttf(subtable, base=16)
    return b"ttcf" + struct.pack(">II", 0x00010000, count) + struct.pack(">I", 16) + face


# 覆盖 ASCII + CJK 常用区 + 全角标点（标题里有「？」U+FF1F）
FULL_FONT = _ttf(_cmap_format4([(0x20, 0x7E), (0x3000, 0x303F), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF)]))
ASCII_ONLY_FONT = _ttf(_cmap_format4([(0x20, 0x7E)]))


@pytest.fixture
def font_path(tmp_path) -> Path:
    p = tmp_path / "font.ttf"
    p.write_bytes(FULL_FONT)
    return p


# ------------------------------------------------------------- 稿件目录助手

META = {
    "slug": "demo-slug",
    "title": TITLE,
    "summary": "摘要",
    "tags": ["RAG"],
    "ai_involvement": "generated",
    "content_sha": "",
}


def _draft(tmp_path: Path, *, visual: bool = True, meta_over: dict | None = None) -> Path:
    root = tmp_path / "drafts" / "demo"
    root.mkdir(parents=True, exist_ok=True)
    meta = dict(META)
    if meta_over:
        meta.update(meta_over)
    (root / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    (root / "csdn.md").write_text("正文" * 50, encoding="utf-8")
    if visual:
        (root / "assets").mkdir(exist_ok=True)
        (root / "assets" / "visual.png").write_bytes(png_bytes(1140, 640))
    return root


class FakeShooter:
    """按脚本产出 PNG 字节（或抛错），记录每次调用的画面尺寸。"""

    def __init__(self, payloads: list[bytes] | None = None, error: Exception | None = None):
        self.payloads = list(payloads or [])
        self.error = error
        self.calls: list[tuple[int, int]] = []
        self.html: list[str] = []

    def shoot(self, html: str, width: int, height: int, out: Path) -> None:
        self.calls.append((width, height))
        self.html.append(html)
        if self.error is not None:
            raise self.error
        Path(out).write_bytes(self.payloads.pop(0) if self.payloads else png_bytes(width, height))


def _render(root: Path, font: Path, **kwargs) -> object:
    kwargs.setdefault("clock", CLOCK)
    kwargs.setdefault("spec", SPEC)
    return render_cover(root, font=font, **kwargs)


# ================================================================ 字体解析


@pytest.mark.unit
class TestResolveFont:
    def test_explicit_path_missing_fails_fast(self, tmp_path):
        with pytest.raises(CoverRenderError, match="字体文件不存在"):
            resolve_font(tmp_path / "nope.ttf")

    def test_explicit_path_returned(self, font_path):
        assert resolve_font(font_path) == font_path

    def test_default_candidates_first_existing_wins(self, monkeypatch, tmp_path):
        p = tmp_path / "candidate.ttc"
        p.write_bytes(b"x")
        monkeypatch.setattr(cover_mod, "DEFAULT_FONT_CANDIDATES", (str(tmp_path / "no"), str(p)))
        assert resolve_font(None) == p

    def test_default_candidates_all_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(cover_mod, "DEFAULT_FONT_CANDIDATES", (str(tmp_path / "a"), str(tmp_path / "b")))
        with pytest.raises(CoverRenderError, match="--font"):
            resolve_font(None)


@pytest.mark.unit
class TestFontCoverage:
    def test_format4_coverage(self):
        coverage = load_font_coverage(FULL_FONT)
        assert find_missing_glyphs(coverage, TITLE) == ""
        assert find_missing_glyphs(coverage, "abc") == ""  # ASCII 段

    def test_format4_reports_missing_in_order_without_duplicates(self):
        coverage = load_font_coverage(ASCII_ONLY_FONT)
        assert find_missing_glyphs(coverage, "中文中文") == "中文"

    def test_format4_skips_whitespace(self):
        coverage = load_font_coverage(_ttf(_cmap_format4([(0x41, 0x5A)])))
        assert find_missing_glyphs(coverage, "A B\tC") == ""

    def test_format4_below_first_range_is_missing(self):
        coverage = load_font_coverage(_ttf(_cmap_format4([(0x4E00, 0x9FFF)])))
        assert find_missing_glyphs(coverage, "\x01中") == "\x01"

    def test_format12_coverage_beyond_bmp(self):
        coverage = load_font_coverage(_ttf(_cmap_format12([(0x20, 0x7E), (0x1F600, 0x1F64F)])))
        assert find_missing_glyphs(coverage, "ok\U0001F600") == ""
        assert find_missing_glyphs(coverage, "\U0001F650") == "\U0001F650"

    def test_format12_preferred_over_format4(self):
        """两种子表都在时选 format 12：它的覆盖才包含 BMP 之外的字符。"""
        f4 = _cmap_format4([(0x20, 0x7E)])
        f12 = _cmap_format12([(0x20, 0x7E), (0x1F600, 0x1F64F)])
        cmap = (
            struct.pack(">HH", 0, 2)
            + struct.pack(">HHI", 3, 1, 4 + 16)
            + struct.pack(">HHI", 3, 10, 4 + 16 + len(f4))
        )
        font_cmap = cmap + f4 + f12
        header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
        record = b"cmap" + struct.pack(">III", 0, 28, len(font_cmap))
        coverage = load_font_coverage(header + record + font_cmap)
        assert find_missing_glyphs(coverage, "\U0001F600") == ""

    def test_ttc_first_face_is_used(self):
        coverage = load_font_coverage(_ttc(_cmap_format4([(0x20, 0x7E), (0x3000, 0x303F), (0x4E00, 0x9FFF), (0xFF00, 0xFFEF)])))
        assert find_missing_glyphs(coverage, TITLE) == ""

    def test_ttc_without_faces(self):
        with pytest.raises(CoverRenderError, match="numFonts=0"):
            load_font_coverage(_ttc(b"", count=0))

    def test_too_small(self):
        with pytest.raises(CoverRenderError, match="太小"):
            load_font_coverage(b"\x00" * 8)

    def test_unknown_magic(self):
        with pytest.raises(CoverRenderError, match="不是 TTF/OTF/TTC"):
            load_font_coverage(b"WOFF" + b"\x00" * 32)

    def test_truncated_table_directory(self):
        font = struct.pack(">IHHHH", 0x00010000, 5, 0, 0, 0) + b"\x00" * 4
        with pytest.raises(CoverRenderError, match="表目录越界"):
            load_font_coverage(font)

    def test_table_range_beyond_file(self):
        cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 12) + _cmap_format4([(0x20, 0x7E)])
        header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
        record = b"cmap" + struct.pack(">III", 0, 28, len(cmap) + 9999)
        with pytest.raises(CoverRenderError, match="越界"):
            load_font_coverage(header + record + cmap)

    def test_missing_cmap_table(self):
        font = _ttf(_cmap_format4([(0x20, 0x7E)]), tag=b"head")
        with pytest.raises(CoverRenderError, match="cmap"):
            load_font_coverage(font)

    def test_ttc_face_offset_beyond_file(self):
        font = b"ttcf" + struct.pack(">III", 0x00010000, 1, 5000) + b"\x00" * 8
        with pytest.raises(CoverRenderError, match="头部不完整"):
            load_font_coverage(font)

    def test_cmap_header_truncated(self):
        header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
        record = b"cmap" + struct.pack(">III", 0, 28, 2)
        with pytest.raises(CoverRenderError, match="cmap 表头不完整"):
            load_font_coverage(header + record + b"\x00\x00")

    def test_cmap_subtable_directory_truncated(self):
        cmap = struct.pack(">HH", 0, 1) + b"\x00" * 4  # 声称 1 个子表但记录不全
        header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
        record = b"cmap" + struct.pack(">III", 0, 28, len(cmap))
        with pytest.raises(CoverRenderError, match="子表目录越界"):
            load_font_coverage(header + record + cmap)

    def test_cmap_subtable_offset_beyond_file(self):
        cmap = struct.pack(">HHHHI", 0, 1, 3, 1, 9999)
        header = struct.pack(">IHHHH", 0x00010000, 1, 0, 0, 0)
        record = b"cmap" + struct.pack(">III", 0, 28, len(cmap))
        with pytest.raises(CoverRenderError, match="子表偏移越界"):
            load_font_coverage(header + record + cmap)

    def test_no_supported_subtable_format(self):
        sub = struct.pack(">HHHH", 6, 10, 0, 0)  # format 6：不支持
        font = _ttf(sub)
        with pytest.raises(CoverRenderError, match="format 4/12"):
            load_font_coverage(font)

    def test_format12_header_truncated(self):
        sub = struct.pack(">HHI", 12, 0, 16) + b"\x00" * 4
        with pytest.raises(CoverRenderError, match="format 12 表头不完整"):
            load_font_coverage(_ttf(sub))

    def test_format12_group_beyond_file(self):
        sub = struct.pack(">HHIII", 12, 0, 28, 0, 2) + struct.pack(">III", 0x20, 0x7E, 0)
        with pytest.raises(CoverRenderError, match="group 越界"):
            load_font_coverage(_ttf(sub))

    def test_format12_inverted_group(self):
        with pytest.raises(CoverRenderError, match="倒挂"):
            load_font_coverage(_ttf(_cmap_format12([(0x7E, 0x20)])))

    def test_format12_empty(self):
        with pytest.raises(CoverRenderError, match="没有任何 group"):
            load_font_coverage(_ttf(_cmap_format12([])))

    def test_format4_header_truncated(self):
        with pytest.raises(CoverRenderError, match="format 4 表头不完整"):
            load_font_coverage(_ttf(struct.pack(">HH", 4, 16) + b"\x00" * 4))

    def test_format4_length_beyond_file(self):
        sub = _cmap_format4([(0x20, 0x7E)])
        sub = struct.pack(">HH", 4, len(sub) + 500) + sub[4:]
        with pytest.raises(CoverRenderError, match="越界"):
            load_font_coverage(_ttf(sub))

    def test_format4_segment_arrays_incomplete(self):
        sub = _cmap_format4([(0x20, 0x7E)])
        # 把 length 改小到装不下段数组
        sub = struct.pack(">HH", 4, 20) + sub[4:]
        with pytest.raises(CoverRenderError, match="段数组不完整"):
            load_font_coverage(_ttf(sub))

    def test_format4_inverted_segment(self):
        with pytest.raises(CoverRenderError, match="倒挂"):
            load_font_coverage(_ttf(_cmap_format4([(0x7E, 0x20)])))

    def test_format4_only_sentinel_is_empty(self):
        with pytest.raises(CoverRenderError, match="没有任何有效段"):
            load_font_coverage(_ttf(_cmap_format4([])))

    def test_format4_adjacent_segments_merge(self):
        """相邻区间必须合并——否则二分查找会在接缝上误判。"""
        coverage = load_font_coverage(_ttf(_cmap_format4([(0x41, 0x4A), (0x4B, 0x5A)], sentinel=False)))
        assert find_missing_glyphs(coverage, "AZ") == ""


# ================================================================ HTML 叠字


@pytest.mark.unit
class TestBuildHtml:
    def test_escapes_title_and_embeds_resources(self):
        page = build_cover_html(
            title='a<b>"c',
            visual_data=b"\x89PNG-fake",
            font_data=b"font-bytes",
            font_family="F",
            spec=SPEC,
        )
        assert "a&lt;b&gt;&quot;c" in page
        assert 'a<b>"c' not in page.split("<style>")[1].split("</style>")[1]
        assert "data:font/ttf;base64," in page
        assert "data:image/png;base64," in page
        assert "width: 1140px; height: 640px" in page

    @pytest.mark.parametrize(
        "length,expected",
        [(16, 72), (17, 56), (28, 56), (29, 44), (40, 44), (41, 36), (80, 36)],
    )
    def test_font_size_tiers(self, length, expected):
        page = build_cover_html(
            title="字" * length, visual_data=b"v", font_data=b"f", font_family="F", spec=SPEC
        )
        assert f"font-size: {expected}px" in page


# ================================================================ Shooter


class _FakePage:
    def __init__(self, browser):
        self._browser = browser
        self.viewport = None
        self.dsf = None
        self.content = None

    def set_content(self, html, wait_until, timeout):
        self.content = html

    def screenshot(self, path):
        Path(path).write_bytes(png_bytes(*self.viewport))


class _FakeBrowser:
    def __init__(self):
        self.page = _FakePage(self)
        self.closed = False
        self.launch_kwargs = None

    def new_page(self, viewport, device_scale_factor):
        self.page.viewport = (viewport["width"], viewport["height"])
        self.page.dsf = device_scale_factor
        return self.page

    def close(self):
        self.closed = True


class _FakeChromium:
    def __init__(self, browser):
        self._browser = browser

    def launch(self, **kwargs):
        self._browser.launch_kwargs = kwargs
        return self._browser


class _FakeLauncher:
    def __init__(self, browser):
        self._browser = browser

    def __enter__(self):
        return type("PW", (), {"chromium": _FakeChromium(self._browser)})()

    def __exit__(self, *exc):
        return False


@pytest.mark.unit
class TestPlaywrightShooter:
    def test_full_flow_with_fake_launcher(self, tmp_path):
        browser = _FakeBrowser()
        shooter = PlaywrightShooter(launcher=lambda: _FakeLauncher(browser), timeout_ms=1234)
        out = tmp_path / "out.png"

        shooter.shoot("<html>x</html>", 1140, 640, out)

        assert browser.launch_kwargs == {"headless": True, "channel": "chrome"}
        assert browser.page.viewport == (1140, 640)
        assert browser.page.dsf == 1, "device_scale_factor 必须锁 1，否则高分屏截出两倍尺寸"
        assert browser.page.content == "<html>x</html>"
        assert browser.closed
        assert out.read_bytes() == png_bytes(1140, 640)

    def test_channel_none_uses_bundled_chromium(self, tmp_path):
        browser = _FakeBrowser()
        shooter = PlaywrightShooter(launcher=lambda: _FakeLauncher(browser), channel=None)
        shooter.shoot("<html/>", 10, 10, tmp_path / "o.png")
        assert browser.launch_kwargs == {"headless": True}

    def test_launch_failure_wrapped(self, tmp_path):
        class Boom:
            def __enter__(self):
                raise RuntimeError("no chrome")

            def __exit__(self, *exc):
                return False

        with pytest.raises(CoverShooterUnavailable, match="渲染失败"):
            PlaywrightShooter(launcher=lambda: Boom()).shoot("x", 10, 10, tmp_path / "o.png")

    def test_default_launcher_requires_playwright(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "playwright.sync_api", None)
        with pytest.raises(CoverShooterUnavailable, match="pip install playwright"):
            cover_mod._default_launcher()

    def test_default_launcher_returns_real_entrypoint(self, monkeypatch):
        import types

        fake = types.SimpleNamespace(sync_playwright=lambda: None)
        monkeypatch.setitem(sys.modules, "playwright.sync_api", fake)
        assert cover_mod._default_launcher() is fake.sync_playwright


# ================================================================ 渲染编排


@pytest.mark.unit
class TestRenderCover:
    def test_happy_path_produces_three_files_and_updates_meta(self, tmp_path, font_path):
        root = _draft(tmp_path)
        result = _render(root, font_path, shooter=FakeShooter())

        assert (result.width, result.height) == (1140, 640)  # UT-15-01
        assert result.cover_path.read_bytes() == png_bytes(1140, 640)
        assert result.record_path.is_file() and result.preview_path.is_file()
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        assert meta["cover"] == COVER_FILENAME
        record = load_record(root)
        assert record.approved is False
        assert record.title == TITLE
        assert "待人工确认" in result.preview_path.read_text(encoding="utf-8")

    def test_shooter_receives_spec_dimensions_and_embedded_html(self, tmp_path, font_path):
        root = _draft(tmp_path)
        shooter = FakeShooter()
        _render(root, font_path, shooter=shooter)
        assert shooter.calls == [(1140, 640)]
        assert "data:image/png;base64" in shooter.html[0]

    def test_default_shooter_is_playwright(self, tmp_path, font_path, monkeypatch):
        """shooter 缺省时走真 PlaywrightShooter——用假 launcher 接住，不开浏览器。"""
        root = _draft(tmp_path)
        browser = _FakeBrowser()
        monkeypatch.setattr(
            cover_mod, "PlaywrightShooter",
                lambda: PlaywrightShooter(launcher=lambda: _FakeLauncher(browser)),
        )
        result = _render(root, font_path)
        assert (result.width, result.height) == (1140, 640)

    def test_wrong_size_output_is_destroyed(self, tmp_path, font_path):
        """UT-15-01 的另一半：渲染尺寸不符，产物当场销毁，不留废图。"""
        root = _draft(tmp_path)
        shooter = FakeShooter(payloads=[png_bytes(800, 600)])
        with pytest.raises(CoverRenderError, match="UT-15-01"):
            _render(root, font_path, shooter=shooter)
        assert not (root / COVER_FILENAME).exists()
        assert not (root / RECORD_FILENAME).exists()
        assert not (root / PREVIEW_FILENAME).exists()
        assert not list(root.glob(".cover.*")), "临时文件也必须清掉"

    def test_oversize_output_is_destroyed(self, tmp_path, font_path):
        """UT-15-03：超平台上限的封面不许留盘。"""
        root = _draft(tmp_path)
        tight = CoverSpec(width=1140, height=640, max_bytes=10)
        with pytest.raises(CoverRenderError, match="UT-15-03"):
            _render(root, font_path, spec=tight)
        assert not (root / COVER_FILENAME).exists()
        assert not list(root.glob(".cover.*"))

    def test_non_image_output_is_destroyed(self, tmp_path, font_path):
        root = _draft(tmp_path)
        shooter = FakeShooter(payloads=[b"not a png at all"])
        with pytest.raises(CoverRenderError, match="不是合法图片"):
            _render(root, font_path, shooter=shooter)
        assert not (root / COVER_FILENAME).exists()

    def test_shooter_failure_leaves_no_trace(self, tmp_path, font_path):
        root = _draft(tmp_path)
        shooter = FakeShooter(error=CoverShooterUnavailable("no chrome"))
        with pytest.raises(CoverShooterUnavailable):
            _render(root, font_path, shooter=shooter)
        assert not (root / COVER_FILENAME).exists()
        assert not list(root.glob(".cover.*"))

    def test_rerender_keeps_visual_untouched_and_resets_approval(self, tmp_path, font_path):
        """UT-15-02：改标题重渲染，主视觉哈希不变；新图没经眼，审批重置。"""
        root = _draft(tmp_path)
        visual = root / "assets" / "visual.png"
        visual_sha_before = cover_mod._sha256(visual)

        first = _render(root, font_path)
        approve_cover(root, by="拾一米", clock=CLOCK)

        payload_v2 = png_bytes(1140, 640) + b"\x00"  # 同尺寸、不同字节
        second = _render(root, font_path, title="换了一个标题", shooter=FakeShooter(payloads=[payload_v2]))

        assert cover_mod._sha256(visual) == visual_sha_before, "主视觉文件一个字节都不许动"
        assert second.cover_sha != first.cover_sha
        assert second.title == "换了一个标题"
        record = load_record(root)
        assert record.approved is False, "重渲染必须重置审批"
        assert record.previous_approval == "拾一米 @ 2026-10-26T20:30:00+08:00"
        meta = json.loads((root / "meta.json").read_text(encoding="utf-8"))
        assert meta["title"] == TITLE, "封面标题覆盖不改 meta 的标题"

    def test_rerender_without_prior_approval_leaves_previous_empty(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        _render(root, font_path, title="第二版")
        assert load_record(root).previous_approval == ""

    def test_font_missing_glyph_fails_fast_without_output(self, tmp_path):
        """UT-15-04：字体缺标题字形 → 豆腐块。宁可报错，不产出空白图。"""
        root = _draft(tmp_path)
        font = tmp_path / "ascii.ttf"
        font.write_bytes(ASCII_ONLY_FONT)
        with pytest.raises(CoverRenderError, match="缺 .* 个字形") as exc:
            _render(root, font)
        assert "文" in str(exc.value)
        assert not (root / COVER_FILENAME).exists()
        assert not (root / RECORD_FILENAME).exists()

    def test_font_file_missing_fails_fast(self, tmp_path):
        root = _draft(tmp_path)
        with pytest.raises(CoverRenderError, match="字体文件不存在"):
            _render(root, tmp_path / "no-font.ttf")
        assert not (root / COVER_FILENAME).exists()

    def test_broken_font_fails_fast(self, tmp_path):
        root = _draft(tmp_path)
        font = tmp_path / "junk.ttf"
        font.write_bytes(b"junk" * 10)
        with pytest.raises(CoverRenderError, match="不是 TTF/OTF/TTC"):
            _render(root, font)

    def test_missing_draft_dir(self, tmp_path, font_path):
        with pytest.raises(CoverRenderError, match="稿件目录不存在"):
            _render(tmp_path / "nope", font_path)

    def test_missing_meta(self, tmp_path, font_path):
        root = tmp_path / "draft"
        root.mkdir()
        with pytest.raises(CoverRenderError, match="meta.json"):
            _render(root, font_path)

    def test_broken_meta_json(self, tmp_path, font_path):
        root = tmp_path / "draft"
        root.mkdir()
        (root / "meta.json").write_text("{bad", encoding="utf-8")
        with pytest.raises(CoverRenderError, match="不是合法 JSON"):
            _render(root, font_path)

    def test_meta_not_an_object(self, tmp_path, font_path):
        root = tmp_path / "draft"
        root.mkdir()
        (root / "meta.json").write_text("[1]", encoding="utf-8")
        with pytest.raises(CoverRenderError, match="顶层必须是对象"):
            _render(root, font_path)

    def test_meta_model_error(self, tmp_path, font_path):
        root = _draft(tmp_path, meta_over={"slug": "BAD SLUG"})
        with pytest.raises(CoverRenderError, match="meta.json 不合法"):
            _render(root, font_path)

    def test_empty_title_rejected(self, tmp_path, font_path):
        root = _draft(tmp_path)
        with pytest.raises(CoverRenderError, match="标题为空"):
            _render(root, font_path, title="   ")

    def test_missing_default_visual(self, tmp_path, font_path):
        root = _draft(tmp_path, visual=False)
        with pytest.raises(CoverRenderError, match="缺主视觉"):
            _render(root, font_path)

    def test_missing_explicit_visual(self, tmp_path, font_path):
        root = _draft(tmp_path, visual=False)
        with pytest.raises(CoverRenderError, match="主视觉文件不存在"):
            _render(root, font_path, visual=tmp_path / "no.png")

    def test_external_visual_is_frozen_into_draft(self, tmp_path, font_path):
        root = _draft(tmp_path, visual=False)
        external = tmp_path / "ai-visual.png"
        external.write_bytes(png_bytes(1600, 900))  # 尺寸不必等于规格，CSS 负责裁切
        result = _render(root, font_path, visual=external)
        assert result.visual_path == root / "assets" / "visual.png"
        assert result.visual_path.read_bytes() == external.read_bytes()

    def test_same_visual_reused(self, tmp_path, font_path):
        root = _draft(tmp_path)
        same = tmp_path / "same.png"
        same.write_bytes((root / "assets" / "visual.png").read_bytes())
        _render(root, font_path, visual=same)
        assert (root / "assets" / "visual.png").read_bytes() == same.read_bytes()

    def test_visual_pointing_at_itself(self, tmp_path, font_path):
        root = _draft(tmp_path)
        target = root / "assets" / "visual.png"
        result = _render(root, font_path, visual=target)
        assert result.visual_path == target

    def test_different_visual_refuses_to_overwrite(self, tmp_path, font_path):
        root = _draft(tmp_path)
        other = tmp_path / "other.png"
        other.write_bytes(png_bytes(800, 600))
        with pytest.raises(CoverRenderError, match="人工删除"):
            _render(root, font_path, visual=other)

    def test_visual_must_be_png(self, tmp_path, font_path):
        root = _draft(tmp_path, visual=False)
        jpg = tmp_path / "visual.jpg"
        jpg.write_bytes(b"\xff\xd8\xff")  # JPEG 魔数
        with pytest.raises(CoverRenderError, match="必须是 PNG"):
            _render(root, font_path, visual=jpg)

    def test_visual_truncated_png(self, tmp_path, font_path):
        root = _draft(tmp_path, visual=False)
        bad = tmp_path / "visual.png"
        bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 4)  # 有魔数没 IHDR
        with pytest.raises(CoverRenderError, match="不是完整的 PNG"):
            _render(root, font_path, visual=bad)

    def test_meta_already_points_to_cover_is_left_alone(self, tmp_path, font_path):
        root = _draft(tmp_path, meta_over={"cover": COVER_FILENAME})
        before = (root / "meta.json").read_bytes()
        _render(root, font_path)
        assert (root / "meta.json").read_bytes() == before


# ================================================================ 审批（G2）


@pytest.mark.unit
class TestApprove:
    def test_approve_marks_record_and_preview(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        record = approve_cover(root, by="拾一米", clock=CLOCK)

        assert record.approved is True
        assert record.approved_by == "拾一米"
        assert record.approved_at == "2026-10-26T20:30:00+08:00"
        assert load_record(root).approved is True
        preview = (root / PREVIEW_FILENAME).read_text(encoding="utf-8")
        assert "已确认：拾一米" in preview
        assert "标题文字无错别字" in preview and "主视觉无水印" in preview

    def test_approve_requires_a_name(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        with pytest.raises(CoverRenderError, match="审批人不能为空"):
            approve_cover(root, by="  ")

    def test_approve_without_record(self, tmp_path):
        root = _draft(tmp_path)
        with pytest.raises(CoverRenderError, match="cover.json"):
            approve_cover(root, by="x")

    def test_approve_without_cover_file(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        (root / COVER_FILENAME).unlink()
        with pytest.raises(CoverRenderError, match="封面不存在"):
            approve_cover(root, by="x")

    def test_approve_rejects_tampered_cover(self, tmp_path, font_path):
        """审批绑定 sha：渲染后被换过的封面不能批——批了就是走过场。"""
        root = _draft(tmp_path)
        _render(root, font_path)
        (root / COVER_FILENAME).write_bytes(png_bytes(1140, 640) + b"\x01")
        with pytest.raises(CoverRenderError, match="不一致"):
            approve_cover(root, by="x")

    def test_double_approve_rejected(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        approve_cover(root, by="a", clock=CLOCK)
        with pytest.raises(CoverRenderError, match="已是确认状态"):
            approve_cover(root, by="b", clock=CLOCK)


@pytest.mark.unit
class TestRequireApproved:
    def test_missing_record_blocks_publish(self, tmp_path):
        root = _draft(tmp_path)
        with pytest.raises(CoverNotApproved, match="未过人工抽检"):
            require_cover_approved(root, root / COVER_FILENAME)

    def test_unapproved_blocks_publish_with_hint(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        with pytest.raises(CoverNotApproved, match="--approve"):
            require_cover_approved(root, root / COVER_FILENAME)

    def test_tampered_after_approval_blocks_publish(self, tmp_path, font_path):
        """确认之后封面被改动：审的是 A 发的是 B，这条缝必须焊死。"""
        root = _draft(tmp_path)
        _render(root, font_path)
        approve_cover(root, by="x", clock=CLOCK)
        (root / COVER_FILENAME).write_bytes(png_bytes(1140, 640) + b"\x02")
        with pytest.raises(CoverNotApproved, match="被改动"):
            require_cover_approved(root, root / COVER_FILENAME)

    def test_approved_and_intact_passes(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        approve_cover(root, by="x", clock=CLOCK)
        require_cover_approved(root, root / COVER_FILENAME)  # 不抛即通过


# ================================================================ 记录读写


@pytest.mark.unit
class TestRecord:
    def test_roundtrip(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        record = load_record(root)
        data = json.loads((root / RECORD_FILENAME).read_text(encoding="utf-8"))
        assert CoverRecord.from_json(data, root / RECORD_FILENAME) == record

    def test_load_missing(self, tmp_path):
        with pytest.raises(CoverRenderError, match="缺 cover.json"):
            load_record(tmp_path)

    def test_load_broken_json(self, tmp_path):
        (tmp_path / RECORD_FILENAME).write_text("{bad", encoding="utf-8")
        with pytest.raises(CoverRenderError, match="不是合法 JSON"):
            load_record(tmp_path)

    def test_load_not_an_object(self, tmp_path):
        (tmp_path / RECORD_FILENAME).write_text('"x"', encoding="utf-8")
        with pytest.raises(CoverRenderError, match="顶层必须是对象"):
            load_record(tmp_path)

    def test_unknown_field_rejected(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        data = json.loads((root / RECORD_FILENAME).read_text(encoding="utf-8"))
        data["surprise"] = 1
        (root / RECORD_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CoverRenderError, match="未知字段"):
            load_record(root)

    def test_missing_required_field(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        data = json.loads((root / RECORD_FILENAME).read_text(encoding="utf-8"))
        del data["cover_sha"]
        (root / RECORD_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CoverRenderError, match="缺必填字段"):
            load_record(root)

    @pytest.mark.parametrize("field", ["width", "height", "size_bytes"])
    def test_bad_int_fields(self, tmp_path, font_path, field):
        root = _draft(tmp_path)
        _render(root, font_path)
        data = json.loads((root / RECORD_FILENAME).read_text(encoding="utf-8"))
        data[field] = "1140"
        (root / RECORD_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CoverRenderError, match=field):
            load_record(root)

    def test_approved_must_be_bool(self, tmp_path, font_path):
        root = _draft(tmp_path)
        _render(root, font_path)
        data = json.loads((root / RECORD_FILENAME).read_text(encoding="utf-8"))
        data["approved"] = "yes"
        (root / RECORD_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        with pytest.raises(CoverRenderError, match="approved"):
            load_record(root)
