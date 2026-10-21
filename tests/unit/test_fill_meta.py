"""T-08 元信息填充（fill_meta）。

钉死的行为：
- UT-08-01 标题超上限 → 截断到上限并 WARNING 告警，绝不静默丢弃
- UT-08-02 标签超上限 → 按优先级（列表顺序）取前 N 个，告警丢了几个
- UT-08-03 分类在页面上不存在 → MetaError 且**列出全部可选项**
- AI 参与度为空 → MetaError（guard 保证非空，这里是不变的第二道防线）
- limits 是必填参数：上限只认 PlatformConfig 一份真相，不许 adapter 自己再存一份
"""

from __future__ import annotations

import logging

import pytest

from geopublish.adapters.base import MetaError
from geopublish.adapters.csdn.flow import CsdnAdapter
from geopublish.core.clock import FakeClock
from geopublish.core.config import PlatformConfig
from geopublish.core.models import Draft, Meta

pytestmark = pytest.mark.unit


class FakeMetaPage:
    """假元信息页。evaluate 按 op 注释分发，value 落进 self.values。

    - op=setvalue   参数 [sel, text] → values[sel] = text
    - op=categories 参数 sel        → 返回 self.options
    """

    def __init__(self, *, options: list[dict[str, str]] | None = None) -> None:
        self.values: dict[str, str] = {}
        self.options = options or []
        self.typed: list[tuple[str, str]] = []
        self.pressed: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.url = "https://mp.csdn.net/"

    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        if op == "setvalue":
            sel, text = arg
            self.values[sel] = text
            return {"ok": True}
        if op == "categories":
            (sel,) = arg
            if not self.options:
                return {"ok": False, "reason": f"分类下拉没有可选项（{sel} 未命中）"}
            return {"ok": True, "options": self.options}
        raise AssertionError(f"未知脚本 op：{op}")

    def press(self, selector: str, key: str) -> None:
        self.pressed.append((selector, key))

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        self.typed.append((selector, text))

    def click(self, selector: str) -> None:
        self.clicks.append(selector)

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        raise AssertionError("fill_meta 不该等选择器")

    def query_selector(self, selector: str):
        return None

    def query_selector_all(self, selector: str):
        return []

    def goto(self, url: str) -> None:
        raise AssertionError("fill_meta 不该导航")


OPTIONS = [
    {"value": "backend", "text": "后端"},
    {"value": "ai", "text": "人工智能"},
    {"value": "arch", "text": "架构"},
]


def _adapter() -> CsdnAdapter:
    return CsdnAdapter(clock=FakeClock("2026-10-21T11:00:00+08:00"))


def _draft(**overrides) -> Draft:
    fields = dict(
        slug="t08-probe",
        title="探针标题",
        summary="一段摘要。",
        tags=("文档解析", "PDF", "OCR", "RAG", "知识库", "第六个", "第七个"),
        category="人工智能",
        ai_involvement="assisted",
    )
    fields.update(overrides)
    return Draft(meta=Meta(**fields), body="正文" * 100)


# ----------------------------------------------------------- UT-08-01

def test_ut_08_01_overlong_title_is_truncated_with_warning(caplog):
    """120 字标题 + 上限 100 → 只填前 100 字，且 WARNING 里写明截断。"""
    long_title = "超" * 120
    page = FakeMetaPage(options=OPTIONS)

    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(page, _draft(title=long_title), PlatformConfig(name="csdn"))

    filled = page.values[_adapter().selectors("editor", "title")]
    assert len(filled) == 100, f"应截断到 100 字，实际 {len(filled)}"
    assert filled == "超" * 100
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("截断" in r.message and "120" in r.message for r in warnings), (
        f"告警必须写明原始长度与动作：{[r.message for r in warnings]}"
    )


def test_title_within_limit_is_not_touched_or_warned(caplog):
    """标题在上限内 → 原样填入，零告警（不误报）。"""
    page = FakeMetaPage(options=OPTIONS)
    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))
    assert page.values[_adapter().selectors("editor", "title")] == "探针标题"
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


# ----------------------------------------------------------- UT-08-02

def test_ut_08_02_over_limit_tags_keep_first_n_by_priority():
    """7 个标签 + 上限 5 → 只键入前 5 个（列表顺序即优先级），每个后面跟 Enter。"""
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))

    tags_sel = _adapter().selectors("meta", "tags")
    typed = [t for sel, t in page.typed if sel == tags_sel]
    assert typed == ["文档解析", "PDF", "OCR", "RAG", "知识库"]
    entered = [k for sel, k in page.pressed if sel == tags_sel]
    assert entered == ["Enter"] * 5, "每个标签键入后必须回车确认"


def test_over_limit_tags_drop_is_warned(caplog):
    """丢标签必须告警写明数量，不许静默砍。"""
    page = FakeMetaPage(options=OPTIONS)
    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))
    assert any("2" in r.message and "标签" in r.message for r in caplog.records)


def test_tags_max_zero_types_nothing():
    """tags_max=0 是合法配置（PlatformConfig 允许 ≥0）→ 一个标签都不键入。"""
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn", tags_max=0))
    assert page.typed == []


# ----------------------------------------------------------- UT-08-03

def test_ut_08_03_unknown_category_raises_listing_available_options():
    """分类不在页面可选项里 → MetaError，消息里列出全部可选项。"""
    page = FakeMetaPage(options=OPTIONS)
    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(page, _draft(category="云计算"), PlatformConfig(name="csdn"))
    message = str(exc.value)
    for option in ("后端", "人工智能", "架构"):
        assert option in message, f"可选项 {option} 必须出现在报错里"


def test_category_matched_by_option_text_fills_value():
    """分类命中可选项文本 → 正常填入。"""
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(page, _draft(category="后端"), PlatformConfig(name="csdn"))
    assert page.values[_adapter().selectors("meta", "category")] == "后端"


def test_category_select_unreadable_raises():
    """页面上读不到分类可选项（元素没渲染/改版）→ MetaError，不瞎填。"""
    page = FakeMetaPage(options=None)
    with pytest.raises(MetaError):
        _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))


# ----------------------------------------------------------- 入参防线

def test_empty_ai_involvement_raises_meta_error():
    """AI 参与度为空 → MetaError。guard 漏放的，这里拦第二道。"""
    page = FakeMetaPage(options=OPTIONS)
    with pytest.raises(MetaError):
        _adapter().fill_meta(
            page, _draft(ai_involvement="  "), PlatformConfig(name="csdn")
        )


def test_summary_set_when_present_and_absent_when_empty():
    """有摘要就填，没摘要就不碰那个输入框（不填空串覆盖）。"""
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))
    assert page.values[_adapter().selectors("meta", "summary")] == "一段摘要。"

    page2 = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(
        page2, _draft(summary=""), PlatformConfig(name="csdn")
    )
    assert _adapter().selectors("meta", "summary") not in page2.values


def test_ai_declaration_gets_filled():
    """AI 参与度要写进平台标注框（assisted 照实填）。"""
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))
    assert page.values[_adapter().selectors("meta", "ai_declaration")] == "assisted"


def test_fill_meta_requires_draft_and_limits():
    """draft=None 或不给 limits 都是用法错误（TypeError），不是静默跳过。"""
    page = FakeMetaPage(options=OPTIONS)
    with pytest.raises(TypeError):
        _adapter().fill_meta(page, None, PlatformConfig(name="csdn"))
    with pytest.raises(TypeError):
        _adapter().fill_meta(page, _draft(), None)  # type: ignore[arg-type]
