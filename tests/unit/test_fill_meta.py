"""T-08 元信息填充（fill_meta）；2026-10-22 T-10 真机校准后再改版。

钉死的行为：
- 草稿模式（默认）**只填标题**：分类/标签/摘要/AI 声明的控件在发布对话框里，
  编辑器页面上没有——草稿模式碰它们只会撞选择器超时（真机实测）
- 标题是双层控件：先点 display 展示层，再真实键盘输入，最后回读比对
- UT-08-01 标题超上限 → 截断到上限并 WARNING 告警，绝不静默丢弃
- 标题回读不符（框架模型没吃下去）→ MetaError，不带假标题往下走
- AI 参与度为空 → MetaError（guard 保证非空，这里是不变的第二道防线）
- 发布模式（T-10 校准后的真实控件形态）：
  - 先确保发布对话框打开（op=opendialog），再确保文章类型=原创（op=original）
  - UT-08-02 标签：点「添加文章标签」展开面板，逐条键入 + Enter，截前 N
  - UT-08-03 分类：可选项从页面读（op=categorypick），选不中/为空 → MetaError
  - 摘要走 setvalue；AI 声明走 el-select 文本点选（op=aideclare）
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
    """假元信息页（T-10 形态）。evaluate 按 op 注释分发；标题控件走 click/键盘。

    - op=closemodals   → 不弹窗
    - op=readtitle     → 模型态回读（title_echo 模拟「模型没吃下去」）
    - op=opendialog    → 打开发布对话框（dialog_open=True）
    - op=original      → 文章类型选中原件
    - op=categorypick  → 参数 [box, option, category] → 按 self.options 裁决
    - op=setvalue      → 参数 [sel, text] → values[sel] = text（摘要）
    - op=aideclare     → 参数 [input, option, text] → 记 ai_declared
    query_selector：对话框根在 dialog_open 后命中；cover.done_marker 恒不命中（无封面）。
    """

    def __init__(
        self,
        *,
        options: list[str] | None = None,
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
        self.waits: list[str] = []
        self.dialog_open = False
        self.category_picked: str | None = None
        self.ai_declared: str | None = None
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
        if op == "categorypick":
            box_sel, option_sel, category = arg
            if self.options_empty_ok:
                return {"ok": False, "reason": f"可选项里没有「{category}」", "options": []}
            if not self.options:
                return {"ok": False, "reason": f"分类专栏控件未命中（{box_sel}）"}
            if category not in self.options:
                return {
                    "ok": False,
                    "reason": f"可选项里没有「{category}」",
                    "options": list(self.options),
                }
            self.category_picked = category
            return {"ok": True}
        if op == "aideclare":
            input_sel, option_sel, text = arg
            self.ai_declared = text
            return {"ok": True}
        if op == "opendialog":
            self.dialog_open = True
            return {"ok": True}
        if op == "original":
            return {"ok": True, "checked": True}
        if op == "closemodals":
            return {"ok": True, "closed": 0}
        if op == "closebuttons":
            return {"ok": True, "closed": 0}
        if op == "jsclick":
            self.clicks.append(arg)
            return {"ok": True}
        if op == "focus":
            return {"ok": True}
        if op == "readtitle":
            # 模型态回读：title_echo 模拟「模型没吃下去」；否则回显最后一次键盘输入
            if self.title_echo is not None:
                return {"ok": True, "text": self.title_echo}
            return {"ok": True, "text": self.typed[-1][1] if self.typed else ""}
        raise AssertionError(f"未知脚本 op：{op}")

    def query_selector(self, selector: str):
        if selector == _adapter().selectors("dialog", "root"):
            return object() if self.dialog_open else None
        return None

    def query_selector_all(self, selector: str):
        return []

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        self.waits.append(selector)
        return object()

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

    def goto(self, url: str) -> None:
        raise AssertionError("fill_meta 不该导航")


OPTIONS = ["后端", "人工智能", "架构"]


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
    assert not page.dialog_open, "草稿模式不该打开发布对话框"


def test_draft_mode_accepts_mode_as_string():
    page = FakeMetaPage()
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"), mode="draft")
    assert (_adapter().selectors("editor", "title"), "探针标题") in page.typed


# ------------------------------------------- 发布模式：完整元信息（T-10 真机校准后）

def test_publish_mode_opens_dialog_and_ensures_original_first():
    """发布模式：标题填完后先开对话框、确保文章类型=原创，再碰对话框里的控件。"""
    adapter = _adapter()
    page = FakeMetaPage(options=OPTIONS)

    adapter.fill_meta(page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH)

    assert page.dialog_open, "发布模式必须打开发布对话框"
    assert "opendialog" in page.ops and "original" in page.ops
    assert page.ops.index("opendialog") < page.ops.index("original")


def test_ut_08_02_over_limit_tags_keep_first_n_by_priority():
    """发布模式：7 个标签 + 上限 5 → 只键入前 5 个，每个后面跟 Enter。"""
    adapter = _adapter()
    page = FakeMetaPage(options=OPTIONS)
    adapter.fill_meta(
        page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )

    tags_sel = adapter.selectors("meta", "tags")
    typed = [t for sel, t in page.typed if sel == tags_sel]
    assert typed == ["文档解析", "PDF", "OCR", "RAG", "知识库"]
    entered = [k for sel, k in page.pressed if sel == tags_sel]
    assert entered == ["Enter"] * 5, "每个标签键入后必须回车确认"
    # 键入前必须点过「添加文章标签」把面板展开
    assert adapter.selectors("meta", "tags_add_button") in page.clicks


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


def test_category_matched_by_option_text_is_picked():
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(
        page, _draft(category="后端"), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )
    assert page.category_picked == "后端"


def test_category_control_missing_raises():
    page = FakeMetaPage(options=None)
    with pytest.raises(MetaError):
        _adapter().fill_meta(
            page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )


def test_category_options_empty_list_raises():
    """账号没有任何专栏（T-10 实测形态）→ MetaError 明说原因，不静默跳过。"""
    page = FakeMetaPage(options=OPTIONS, options_empty_ok=True)
    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "可选项为空" in str(exc.value)


def test_summary_and_ai_declaration_filled_in_publish_mode():
    """摘要走 setvalue；AI 声明是 el-select 文本点选（assisted → 部分内容由AI辅助生成）。"""
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(
        page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )
    assert page.values[_adapter().selectors("meta", "summary")] == "一段摘要。"
    assert page.ai_declared == "部分内容由AI辅助生成"


def test_ai_declaration_generated_falls_back_with_warning(caplog):
    """平台没有独立「AI 生成」档（T-10 校准四档）：generated 落到最近的 AI 档并告警。"""
    page = FakeMetaPage(options=OPTIONS)
    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        _adapter().fill_meta(
            page, _draft(ai_involvement="generated"), PlatformConfig(name="csdn"),
            mode=SubmitMode.PUBLISH,
        )
    assert page.ai_declared == "部分内容由AI辅助生成"
    assert any("AI" in r.message and "档" in r.message for r in caplog.records)


def test_ai_declaration_human_maps_to_no_statement():
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(
        page, _draft(ai_involvement="human"), PlatformConfig(name="csdn"),
        mode=SubmitMode.PUBLISH,
    )
    assert page.ai_declared == "无声明"


def test_no_category_skips_category_control_entirely():
    page = FakeMetaPage(options=OPTIONS)
    adapter = _adapter()
    adapter.fill_meta(
        page, _draft(category=""), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )
    assert "categorypick" not in page.ops


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
    assert adapter.selectors("meta", "tags_add_button") not in page.clicks


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


# ----------------------------------------------------------- 发布对话框的打开与失败分支

def test_publish_mode_reuses_an_already_open_dialog():
    """对话框已开（upload_cover 先开的）→ fill_meta 不重复点入口。"""
    adapter = _adapter()
    page = FakeMetaPage(options=OPTIONS)
    page.dialog_open = True  # 已被前序步骤打开

    adapter.fill_meta(page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH)

    assert "opendialog" not in page.ops, "对话框已开就不许再点发布入口"


def test_dialog_entry_button_missing_is_submit_blocked():
    """发布入口按钮未命中（改版）→ SubmitBlocked，明说打不开了。"""

    class _NoEntry(FakeMetaPage):
        def evaluate(self, script, arg=None):
            if "op=opendialog" in script:
                self.ops.append("opendialog")
                return {"ok": False, "reason": "发布入口按钮未命中（editor.publish_button）"}
            return super().evaluate(script, arg)

    from geopublish.adapters.base import SubmitBlocked

    with pytest.raises(SubmitBlocked) as exc:
        _adapter().fill_meta(
            _NoEntry(options=OPTIONS), _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "发布对话框打不开" in str(exc.value)


def test_dialog_never_renders_after_clicking_entry():
    """点了入口但对话框没渲染出来（超时）→ SubmitBlocked。"""

    class _NoRender(FakeMetaPage):
        def wait_for_selector(self, selector, timeout_ms: int = 10_000):
            if selector == _adapter().selectors("dialog", "root"):
                raise TimeoutError("Timeout exceeded")
            return super().wait_for_selector(selector, timeout_ms)

    from geopublish.adapters.base import SubmitBlocked

    with pytest.raises(SubmitBlocked) as exc:
        _adapter().fill_meta(
            _NoRender(options=OPTIONS), _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "没出现" in str(exc.value)


def test_original_radio_missing_is_meta_error():
    """文章类型控件未命中 → MetaError（不允许默认态赌博）。"""

    class _NoOriginal(FakeMetaPage):
        def evaluate(self, script, arg=None):
            if "op=original" in script:
                return {"ok": False, "reason": "文章类型「原创」控件未命中"}
            return super().evaluate(script, arg)

    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            _NoOriginal(options=OPTIONS), _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "原创" in str(exc.value)


def test_tags_panel_never_expands_is_meta_error():
    """点了「添加文章标签」但输入框没出现 → MetaError。"""

    class _NoTagInput(FakeMetaPage):
        def wait_for_selector(self, selector, timeout_ms: int = 10_000):
            if selector == _adapter().selectors("meta", "tags"):
                raise TimeoutError("Timeout exceeded")
            return super().wait_for_selector(selector, timeout_ms)

    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            _NoTagInput(options=OPTIONS), _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "标签面板展不开" in str(exc.value)


def test_tags_panel_close_failure_is_tolerated():
    """关面板按钮点不动不阻断流程（面板浮在对话框内部，不挡 footer）。"""

    class _StuckPanel(FakeMetaPage):
        def click(self, selector: str) -> None:
            if selector == _adapter().selectors("meta", "tags_panel_close"):
                raise TimeoutError("点不动")
            super().click(selector)

    # 不抛异常即为通过
    _adapter().fill_meta(
        _StuckPanel(options=OPTIONS), _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
    )


def test_category_script_returning_garbage_is_meta_error():
    """categorypick 脚本返回非 Mapping（页面上下文崩了）→ MetaError，不崩栈。"""

    class _Garbage(FakeMetaPage):
        def evaluate(self, script, arg=None):
            if "op=categorypick" in script:
                return None
            return super().evaluate(script, arg)

    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            _Garbage(options=OPTIONS), _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "无返回" in str(exc.value)


def test_ai_declaration_option_missing_lists_available(caplog):
    """创作声明下拉里没有目标档（平台改了措辞）→ MetaError 并列出页面当前档位。"""

    class _NoOption(FakeMetaPage):
        def evaluate(self, script, arg=None):
            if "op=aideclare" in script:
                return {"ok": False, "reason": "创作声明下拉里没有该选项",
                        "options": ["无声明", "全新措辞档"]}
            return super().evaluate(script, arg)

    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            _NoOption(options=OPTIONS), _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH
        )
    assert "全新措辞档" in str(exc.value)


def test_publish_mode_never_removes_modal_nodes_before_title():
    """发布模式的弹窗清理只能点关闭按钮（op=closebuttons）。

    close_modals 的 DOM 摘除会连发布对话框的 .modal 挂载点一起摘掉——
    摘了之后点发布入口，对话框永远渲染不出来（2026-10-26 真机踩出）。
    """
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH)

    assert "closebuttons" in page.ops
    assert "closemodals" not in page.ops, "发布模式不许做 DOM 摘除（挂载点会被一起摘掉）"


def test_publish_mode_never_removes_modal_nodes_before_title():
    """发布模式的弹窗清理只能点关闭按钮（op=closebuttons）。

    close_modals 的 DOM 摘除会连发布对话框的 .modal 挂载点一起摘掉——
    摘了之后点发布入口，对话框永远渲染不出来（2026-10-26 真机踩出）。
    """
    page = FakeMetaPage(options=OPTIONS)
    _adapter().fill_meta(page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH)

    assert "closebuttons" in page.ops
    assert "closemodals" not in page.ops, "发布模式不许做 DOM 摘除（挂载点会被一起摘掉）"


def test_publish_mode_title_uses_js_click_immune_to_overlay():
    """发布模式不能摘 DOM（会摘掉对话框挂载点），残留遮罩会拦 Playwright 指针点击——
    标题的 display/input 两步必须走 JS 点击（op=jsclick），键入仍是真实键盘。"""
    adapter = _adapter()
    page = FakeMetaPage(options=OPTIONS)

    adapter.fill_meta(page, _draft(), PlatformConfig(name="csdn"), mode=SubmitMode.PUBLISH)

    assert "jsclick" in page.ops, "发布模式标题必须走 JS 点击"
    assert (adapter.selectors("editor", "title"), "探针标题") in page.typed, "键入仍走真实键盘"


def test_publish_mode_title_jsclick_failure_is_meta_error():
    """jsclick 未命中（控件改版）→ MetaError，不降级、不静默。"""

    class _NoDisplay(FakeMetaPage):
        def evaluate(self, script, arg=None):
            if "op=jsclick" in script:
                return {"ok": False, "reason": "点击目标未命中"}
            return super().evaluate(script, arg)

    with pytest.raises(MetaError) as exc:
        _adapter().fill_meta(
            _NoDisplay(options=OPTIONS), _draft(), PlatformConfig(name="csdn"),
            mode=SubmitMode.PUBLISH,
        )
    assert "点不开标题控件" in str(exc.value)
