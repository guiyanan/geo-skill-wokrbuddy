"""T-08 元信息填充（fill_meta）；2026-10-21 T-09b 真机校准后改版。

钉死的行为：
- 草稿模式（默认）**只填标题**：分类/标签/摘要/AI 声明的控件在发布对话框里，
  编辑器页面上没有——草稿模式碰它们只会撞选择器超时（真机实测）
- 标题是双层控件：先点 display 展示层，再 fill 真实 input，最后回读比对
- UT-08-01 标题超上限 → 截断到上限并 WARNING 告警，绝不静默丢弃
- 标题回读不符（框架模型没吃下去）→ MetaError，不带假标题往下走
- AI 参与度为空 → MetaError（guard 保证非空，这里是不变的第二道防线）
- 发布模式才走分类/标签/摘要/声明（选择器待 T-10 真机校准）：
  UT-08-02 标签截前 N、UT-08-03 分类读页面可选项
"""

from __future__ import annotations

import logging

import pytest

from geopublish.adapters.base import MetaError, SubmitMode
from geopublish.adapters.csdn.flow import CsdnAdapter
from geopublish.core.clock import FakeClock
from geopublish.core.config import PlatformConfig
from geopublish.core.models import Draft, Meta

pytestmark = pytest.mark.unit


class FakeMetaPage:
    """假元信息页。evaluate 按 op 注释分发；标题控件走 click/fill/input_value。

    - op=setvalue   参数 [sel, text] → values[sel] = text（发布对话框控件）
    - op=categories 参数 sel        → 返回 self.options
    - op=closemodals                → 不弹窗
    title_echo：fill 之后 input_value 读到的值（模拟「模型没吃下去」时让它不一致）。
    """

    def __init__(
        self,
        *,
        options: list[dict[str, str]] | None = None,
        setvalue_ok: bool = True,
        options_empty_ok: bool = False,
        title_echo: str | None = None,
    ) -> None:
        self.values: dict[str, str] = {}
        self.options = options or []
        self.setvalue_ok = setvalue_ok
        self.options_empty_ok = options_empty_ok
        self.title_echo = title_echo
        self.filled: dict[str, str] = {}
        self.typed: list[tuple[str, str]] = []
        self.pressed: list[tuple[str, str]] = []
        self.clicks: list[str] = []
        self.ops: list[str] = []
        self.url = "https://editor.csdn.net/md"

    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        self.ops.append(op)
        if op == "setvalue":
            sel, text = arg
            if not self.setvalue_ok:
                return {"ok": False, "reason": "输入控件不存在（选择器未命中）"}
            self.values[sel] = text
            return {"ok": True}
        if op == "categories":
            (sel,) = arg
            if self.options_empty_ok:
                return {"ok": True, "options": []}
            if not self.options:
                return {"ok": False, "reason": f"分类下拉没有可选项（{sel} 未命中）"}
            return {"ok": True, "options": self.options}
        if op == "closemodals":
            return {"ok": True, "closed": 0}
        if op == "focus":
            return {"ok": True}
        if op == "readtitle":
            # 模型态回读：title_echo 模拟「模型没吃下去」；否则回显最后一次键盘输入
            if self.title_echo is not None:
                return {"ok": True, "text": self.title_echo}
            return {"ok": True, "text": self.typed[-1][1] if self.typed else ""}
        raise AssertionError(f"未知脚本 op：{op}")

    def click(self, selector: str) -> None:
        self.clicks.append(selector)

    def fill(self, selector: str, text: str) -> None:
        self.filled[selector] = text

    def input_value(self, selector: str) -> str:
        if self.title_echo is not None:
            return self.title_echo
        return self.filled.get(selector, "")

    def press(self, selector: str, key: str) -> None:
        self.pressed.append((selector, key))

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        self.typed.append((selector, text))

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


# ----------------------------------------------------------- 标题（草稿与发布共用）

def test_title_goes_through_display_clear_then_real_keyboard():
    """双层标题控件 + 受控模型：点 display → Meta+A/Backspace 清空 → 真实键盘输入。

    fill() 只写 value 模型不吃（实测存出【无标题】草稿），所以这里钉死键盘路径。
    """
    adapter = _adapter()
    page = FakeMetaPage()

    adapter.fill_meta(page, _draft(), PlatformConfig(name="csdn"))

    display_sel = adapter.selectors("editor", "title_display")
    title_sel = adapter.selectors("editor", "title")
    assert page.clicks[:2] == [display_sel, title_sel], (
        f"必须先点展示层、再点进真实输入框，实际：{page.clicks[:2]}"
    )
    assert (title_sel, "Meta+A") in page.pressed, "输入前必须全选清掉恢复的标题"
    assert (title_sel, "Backspace") in page.pressed
    assert (title_sel, "探针标题") in page.typed, "标题必须走真实键盘"
    assert title_sel not in page.filled, "不许用 fill——模型只认键盘事件"


def test_fill_meta_closes_modals_before_touching_the_title():
    """弹窗会在注入正文后再冒出来（实测）：点标题 display 前必须再关一轮。"""
    adapter = _adapter()
    page = FakeMetaPage()

    adapter.fill_meta(page, _draft(), PlatformConfig(name="csdn"))

    assert page.ops and page.ops[0] == "closemodals", (
        f"第一个动作必须是关弹窗，实际：{page.ops[:3]}"
    )


def test_ut_08_01_overlong_title_is_truncated_with_warning(caplog):
    """120 字标题 + 上限 100 → 只填前 100 字，且 WARNING 里写明截断。"""
    long_title = "超" * 120
    page = FakeMetaPage()

    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(page, _draft(title=long_title), PlatformConfig(name="csdn"))

    filled = [t for sel, t in page.typed if sel == _adapter().selectors("editor", "title")]
    assert filled == ["超" * 100], f"应截断到 100 字，实际 {filled}"
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("截断" in r.message and "120" in r.message for r in warnings)


def test_title_readback_mismatch_is_meta_error():
    """键盘输入后模型态回读不符（模型没吃下去）→ MetaError。"""
    page = FakeMetaPage(title_echo="残缺的标题")
    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))
    assert "标题回读不符" in str(exc.value)


def test_title_control_missing_is_meta_error():
    """display/input 任何一个点不到（改版）→ MetaError，不静默继续。"""

    class _BrokenTitlePage(FakeMetaPage):
        def click(self, selector: str) -> None:
            raise TimeoutError("元素不存在")

    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(_BrokenTitlePage(), _draft(), PlatformConfig(name="csdn"))
    assert "填不进 editor.title" in str(exc.value)


def test_empty_ai_involvement_raises_meta_error():
    """AI 参与度为空 → MetaError。guard 漏放的，这里拦第二道。"""
    page = FakeMetaPage()
    with pytest.raises(MetaError):
        _adapter().fill_meta(
            page, _draft(ai_involvement="  "), PlatformConfig(name="csdn")
        )


def test_fill_meta_requires_draft_and_limits():
    page = FakeMetaPage()
    with pytest.raises(TypeError):
        _adapter().fill_meta(page, None, PlatformConfig(name="csdn"))
    with pytest.raises(TypeError):
        _adapter().fill_meta(page, _draft(), None)  # type: ignore[arg-type]


# ----------------------------------------------------------- 草稿模式：只填标题

def test_draft_mode_touches_nothing_but_the_title():
    """草稿模式下分类/标签/摘要/AI 声明控件一律不碰（它们在发布对话框里）。"""
    adapter = _adapter()
    page = FakeMetaPage(options=OPTIONS)

    adapter.fill_meta(page, _draft(), PlatformConfig(name="csdn"))  # 默认 mode=draft

    tags_sel = adapter.selectors("meta", "tags")
    assert not [t for sel, t in page.typed if sel == tags_sel], "草稿模式不该键入标签"
    assert [t for _, t in page.typed] == ["探针标题"], "草稿模式只键入标题"
    assert not [k for sel, k in page.pressed if sel == tags_sel], "草稿模式不该按 Enter"
    assert page.values == {}, "草稿模式不该 setvalue（摘要/声明/分类）"


def test_draft_mode_accepts_mode_as_string():
    page = FakeMetaPage()
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"), mode="draft")
    assert (_adapter().selectors("editor", "title"), "探针标题") in page.typed


# ----------------------------------------------------------- 发布模式：完整元信息（T-10 校准候选）

def test_ut_08_02_over_limit_tags_keep_first_n_by_priority():
    """发布模式：7 个标签 + 上限 5 → 只键入前 5 个，每个后面跟 Enter。"""
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(
        page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )

    tags_sel = _adapter().selectors("meta", "tags")
    typed = [t for sel, t in page.typed if sel == tags_sel]
    assert typed == ["文档解析", "PDF", "OCR", "RAG", "知识库"]
    entered = [k for sel, k in page.pressed if sel == tags_sel]
    assert entered == ["Enter"] * 5, "每个标签键入后必须回车确认"


def test_over_limit_tags_drop_is_warned(caplog):
    page = FakeMetaPage(options=OPTIONS)
    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(
            page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert any("2" in r.message and "标签" in r.message for r in caplog.records)


def test_ut_08_03_unknown_category_raises_listing_available_options():
    page = FakeMetaPage(options=OPTIONS)
    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            page, _draft(category="云计算"), PlatformConfig(name="csdn"),
            mode=SubmitMode.PUBLISH,
        )
    message = str(exc.value)
    for option in ("后端", "人工智能", "架构"):
        assert option in message


def test_category_matched_by_option_text_fills_value():
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(
        page, _draft(category="后端"), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )
    assert page.values[_adapter().selectors("meta", "category")] == "后端"


def test_category_select_unreadable_raises():
    page = FakeMetaPage(options=None)
    with pytest.raises(MetaError):
        _adapter().fill_meta(
            page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )


def test_category_options_empty_list_raises():
    page = FakeMetaPage(options=OPTIONS, options_empty_ok=True)
    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "可选项为空" in str(exc.value)


def test_summary_and_ai_declaration_filled_in_publish_mode():
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(
        page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )
    assert page.values[_adapter().selectors("meta", "summary")] == "一段摘要。"
    assert page.values[_adapter().selectors("meta", "ai_declaration")] == "assisted"


def test_no_category_skips_category_control_entirely():
    page = FakeMetaPage(options=OPTIONS)
    adapter = _adapter()
    adapter.fill_meta(
        page, _draft(category=""), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )
    assert adapter.selectors("meta", "category") not in page.values


def test_no_summary_skips_summary_control():
    page = FakeMetaPage(options=OPTIONS)
    adapter = _adapter()
    adapter.fill_meta(
        page, _draft(summary=""), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )
    assert adapter.selectors("meta", "summary") not in page.values


def test_unwritable_control_raises_meta_error():
    page = FakeMetaPage(options=OPTIONS, setvalue_ok=False)
    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "填不进" in str(exc.value)


def test_tags_max_zero_types_nothing():
    page = FakeMetaPage(options=OPTIONS)
    adapter = _adapter()
    adapter.fill_meta(
        page, _draft(), PlatformConfig(name="csdn", tags_max=0), mode=SubmitMode.PUBLISH
    )
    tags_sel = adapter.selectors("meta", "tags")
    assert not [t for sel, t in page.typed if sel == tags_sel]


def test_publish_mode_tags_within_limit_are_not_warned(caplog):
    """标签没超限时零告警（不误报），每个照常键入 + Enter。"""
    page = FakeMetaPage(options=OPTIONS)
    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(
            page, _draft(tags=("文档解析", "PDF")), PlatformConfig(name="csdn"),
            mode=SubmitMode.PUBLISH,
        )
    assert not [r for r in caplog.records if r.levelno == logging.WARNING and "标签" in r.message]


def test_publish_mode_tags_within_limit_are_not_warned(caplog):
    """标签没超限时零告警（不误报），每个照常键入 + Enter。"""
    page = FakeMetaPage(options=OPTIONS)
    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(
            page, _draft(tags=("文档解析", "PDF")), PlatformConfig(name="csdn"),
            mode=SubmitMode.PUBLISH,
        )
    assert not [r for r in caplog.records if r.levelno == logging.WARNING and "标签" in r.message]


def test_title_retry_after_draft_restore_clobbers_the_first_attempt(caplog):
    """草稿恢复把第一次键入冲掉（实测时序不稳定）→ 自动重打一次，且告警留痕。"""
    page = FakeMetaPage()
    reads = {"n": 0}
    real = page.evaluate

    def flaky_read(script, arg=None):
        if "op=readtitle" in script:
            reads["n"] += 1
            if reads["n"] == 1:
                return {"ok": True, "text": "文档"}  # 恢复覆盖：只剩两个字
        return real(script, arg)

    page.evaluate = flaky_read  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))

    assert reads["n"] == 2, "第一次被冲掉必须重打"
    assert any("重打" in r.message for r in caplog.records)


def test_title_retry_after_draft_restore_clobbers_the_first_attempt(caplog):
    """草稿恢复把第一次键入冲掉（实测时序不稳定）→ 自动重打一次，且告警留痕。"""
    page = FakeMetaPage()
    reads = {"n": 0}
    real = page.evaluate

    def flaky_read(script, arg=None):
        if "op=readtitle" in script:
            reads["n"] += 1
            if reads["n"] == 1:
                return {"ok": True, "text": "文档"}  # 恢复覆盖：只剩两个字
        return real(script, arg)

    page.evaluate = flaky_read  # type: ignore[method-assign]

    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"))

    assert reads["n"] == 2, "第一次被冲掉必须重打"
    assert any("重打" in r.message for r in caplog.records)
