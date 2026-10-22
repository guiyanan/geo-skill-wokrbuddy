"""T-09 提交（submit）：点之前必须复核正文，且只走「保存草稿」两步。

钉死的行为：
- UT-09-03 提交前**必有一次**正文回读复核，且发生在保存动作之前（调用序列断言）
- UT-09-04 草稿模式的保存按钮参数是 save_draft_button，全程不碰 publish_button
- 没有注入记录 / 回读失败 / 长度不符 → `SubmitBlocked`，**一个保存动作都不做**
- 保存前必须关弹窗（真机实测：模版库弹窗会挡住点击，Playwright 卡到超时）
- 成功信号是 URL 长出 articleId（真机实测）；等不到返回 unknown，判定权交 verify
"""

from __future__ import annotations

import pytest

from geopublish.adapters.base import InjectFailed, SubmitBlocked, SubmitMode, SubmitState
from geopublish.adapters.csdn.flow import CsdnAdapter
from geopublish.core.clock import FakeClock
from geopublish.core.models import Draft, Meta

pytestmark = pytest.mark.unit

EDITOR_URL = "https://editor.csdn.net/md?not_checkout=1"


class FakeEditorPage:
    """假 cledit 编辑器页：按 op 分发 evaluate，覆盖注入与保存两步流程。"""

    def __init__(
        self,
        *,
        text: str = "",
        url: str = EDITOR_URL,
        save_ok: bool = True,
        url_gains_article_id: bool = True,
        readback_raises: bool = False,
    ) -> None:
        self.text = text
        self.url = url
        self.save_ok = save_ok
        self.url_gains_article_id = url_gains_article_id
        self.readback_raises = readback_raises
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        self.calls.append((op, arg))
        if op == "insert":
            sel, text = arg
            self.text = text
            return {"ok": True}
        if op == "paste":
            sel, text = arg
            self.text = text
            return {"ok": True}
        if op == "clear":
            self.text = ""
            return {"ok": True}
        if op == "readback":
            if self.readback_raises:
                raise RuntimeError("页面已销毁")
            return {"ok": True, "text": self.text}
        if op == "closemodals":
            return {"ok": True, "closed": 0}
        if op == "savedraft":
            if not self.save_ok:
                return {"ok": False, "reason": "popover 里没找到「保存草稿」"}
            return {"ok": True}
        raise AssertionError(f"未预期的脚本 op：{op}")

    def click(self, selector: str) -> None:
        self.calls.append(("click", selector))

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        self.calls.append(("type", selector))
        self.text += text

    def fill(self, selector: str, text: str) -> None:
        raise AssertionError("submit 不该填表")

    def input_value(self, selector: str) -> str:
        return ""

    def wait_for_url(self, pattern: str, timeout_ms: int = 10_000) -> None:
        self.calls.append(("wait_for_url", pattern))
        if not self.url_gains_article_id:
            raise TimeoutError(f"Timeout {timeout_ms}ms exceeded")
        if "articleId=" in pattern:
            self.url = f"{EDITOR_URL}&articleId=168659812"

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        self.calls.append(("wait_for_selector", selector))
        return object()

    def query_selector(self, selector: str):
        return None

    def query_selector_all(self, selector: str):
        return []

    def goto(self, url: str) -> None:
        raise AssertionError("submit 不该导航")

    # --- 断言辅助 ---
    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]


def _adapter() -> CsdnAdapter:
    return CsdnAdapter(clock=FakeClock("2026-10-21T15:00:00+08:00"))


def _draft(body: str = "正文" * 100) -> Draft:
    return Draft(
        meta=Meta(slug="t09-submit", title="探针标题", ai_involvement="assisted"), body=body
    )


# ----------------------------------------------------------- UT-09-04

def test_ut_09_04_draft_mode_saves_via_save_draft_button_never_publish():
    """保存动作的按钮参数必须是 save_draft_button；publish_button 全程不许出现。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    adapter.submit(page)

    save_calls = [arg for op, arg in page.calls if op == "savedraft"]
    assert len(save_calls) == 1
    btn_sel, item_sel, label = save_calls[0]
    assert btn_sel == adapter.selectors("editor", "save_draft_button")
    assert item_sel == adapter.selectors("editor", "save_draft_item")
    publish_sel = adapter.selectors("editor", "publish_button")
    assert publish_sel not in str(page.calls), f"publish 按钮出现在调用里：{page.calls}"


def test_submit_accepts_mode_from_string_and_enum():
    for mode in (SubmitMode.DRAFT, "draft"):
        adapter = _adapter()
        page = FakeEditorPage()
        adapter.fill_content(page, _draft())
        result = adapter.submit(page, mode=mode)
        assert result.state is SubmitState.SUBMITTED


def test_invalid_mode_is_rejected_before_any_save_action():
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    with pytest.raises(ValueError):
        adapter.submit(page, mode="draftt")
    assert "savedraft" not in page.ops()


# ----------------------------------------------------------- UT-09-03

def test_ut_09_03_content_length_is_rechecked_right_before_the_save():
    """注入时回读一次、提交前再回读一次，且提交前那次必须在保存动作之前。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    readbacks_after_inject = page.ops().count("readback")
    adapter.submit(page)

    assert page.ops().count("readback") == readbacks_after_inject + 1, (
        "提交前必须**恰好**多一次回读复核"
    )
    last_readback = max(i for i, (op, _) in enumerate(page.calls) if op == "readback")
    save_at = page.ops().index("savedraft")
    assert last_readback < save_at, "复核要在保存之前，点了再复核没有任何意义"


def test_modals_are_closed_after_recheck_before_save():
    """关弹窗要在复核之后、保存之前（复核要在编辑器原样时做，保存前再清障）。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())
    page.calls.clear()

    adapter.submit(page)

    ops = page.ops()
    assert ops.index("readback") < ops.index("closemodals") < ops.index("savedraft")


def test_check_content_length_returns_actual_length():
    adapter = _adapter()
    page = FakeEditorPage()
    draft = _draft("短一点的正文" * 10)
    adapter.fill_content(page, draft)

    assert adapter.check_content_length(page) == len(draft.body)


def test_submit_tolerates_small_deviation_from_platform_normalisation():
    """平台把末尾空行吃掉这类差异（<2%）不算问题，否则每次都要人肉确认。"""
    adapter = _adapter()
    page = FakeEditorPage()
    draft = _draft("正文" * 500)
    adapter.fill_content(page, draft)

    page.text = draft.body[:-2]  # 掉 2 个字 / 1000 字 = 0.2%
    assert adapter.submit(page).state is SubmitState.SUBMITTED


# ----------------------------------------------------------- 拦截（不做保存动作）

def test_submit_without_injection_record_is_blocked():
    """没有注入记录 = 不知道页面上是什么。宁可让人重跑，也不发内容不明的稿子。"""
    adapter = _adapter()
    page = FakeEditorPage(text="别人的半截草稿")

    with pytest.raises(SubmitBlocked) as exc:
        adapter.submit(page)

    assert "没有正文注入长度记录" in str(exc.value)
    assert "savedraft" not in page.ops(), "拦住了就一个保存动作都不许做"


def test_submit_blocks_when_editor_content_shrank():
    """编辑器里的字数与注入时差太多（页面回滚/编辑区被重置）→ 拦，且报两个数字。"""
    adapter = _adapter()
    page = FakeEditorPage()
    draft = _draft("正文" * 100)
    adapter.fill_content(page, draft)

    page.text = "残稿"
    with pytest.raises(SubmitBlocked) as exc:
        adapter.submit(page)

    message = str(exc.value)
    assert str(len(draft.body)) in message and str(len(page.text)) in message
    assert "savedraft" not in page.ops()


def test_submit_blocks_when_readback_fails():
    """读不回正文就不能提交：读不出来 = 无法证明页面上是我们那篇。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    page.readback_raises = True
    with pytest.raises(SubmitBlocked) as exc:
        adapter.submit(page)

    assert "读不回正文" in str(exc.value)
    assert "savedraft" not in page.ops()


def test_failed_reinjection_invalidates_the_previous_record():
    """再注入一次失败后，旧长度不许继续当基准 —— 否则复核会拿旧记录放行新残稿。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    def _boom(*_args, **_kwargs):
        return {"ok": False, "reason": "编辑器被锁"}

    page.evaluate = _boom  # 三级注入全灭（clear/insert/paste 全被拒）
    with pytest.raises(InjectFailed):
        adapter.fill_content(page, _draft("另一篇" * 50))

    with pytest.raises(SubmitBlocked):
        adapter.submit(page)


def test_open_editor_clears_the_previous_record():
    """换页 = 换了编辑器实例，旧注入记录必须作废。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    page.goto = lambda url: setattr(page, "url", url)  # type: ignore[assignment]
    adapter.open_editor(page, None)

    with pytest.raises(SubmitBlocked):
        adapter.submit(page)


def test_open_editor_closes_modals_after_editor_is_ready():
    """open_editor 的顺序：goto → 等编辑器 → 关弹窗。"""
    adapter = _adapter()
    page = FakeEditorPage()
    page.goto = lambda url: page.calls.append(("goto", url))  # type: ignore[assignment]

    adapter.open_editor(page, None)

    ops = page.ops()
    assert ops[0] == "goto"
    assert ops.index("wait_for_selector") < ops.index("closemodals")


# ----------------------------------------------------------- 保存之后的观察

def test_submit_reports_submitted_when_url_gains_article_id():
    adapter = _adapter()
    page = FakeEditorPage(url_gains_article_id=True)
    adapter.fill_content(page, _draft())

    result = adapter.submit(page)

    assert result.state is SubmitState.SUBMITTED
    assert "articleId" in result.reason
    assert "168659812" in page.url


def test_submit_reports_submitted_when_url_changes_otherwise():
    """URL 变了但没等到 articleId：也算已提交（判定权在 verify）。"""
    adapter = _adapter()
    page = FakeEditorPage(url_gains_article_id=False)
    adapter.fill_content(page, _draft())

    def _wait(url_pattern, timeout_ms=10_000):
        page.calls.append(("wait_for_url", url_pattern))
        page.url = f"{EDITOR_URL}&draftSaved=1"  # 变了，但不是 articleId
        raise TimeoutError("Timeout exceeded")

    page.wait_for_url = _wait  # type: ignore[method-assign]
    result = adapter.submit(page)

    assert result.state is SubmitState.SUBMITTED
    assert "已变化" in result.reason


def test_submit_is_unknown_when_nothing_confirms():
    """保存脚本成了但 URL 纹丝不动 → unknown。说「失败」会诱导人重发，说「成功」是撒谎。"""
    adapter = _adapter()
    page = FakeEditorPage(url_gains_article_id=False)
    adapter.fill_content(page, _draft())

    result = adapter.submit(page)

    assert result.state is SubmitState.UNKNOWN
    assert "verify" in result.reason


def test_submit_is_unknown_when_save_item_is_missing():
    """popover 里找不到「保存草稿」（改版改了文案/结构）→ unknown，不假装点上了。"""
    adapter = _adapter()
    page = FakeEditorPage(save_ok=False)
    adapter.fill_content(page, _draft())

    result = adapter.submit(page)

    assert result.state is SubmitState.UNKNOWN
    assert "没找到" in result.reason
    assert "wait_for_url" not in page.ops(), "没点成就别等 URL"


# ----------------------------------------------------------- 关弹窗

def test_close_modals_loops_until_nothing_left():
    """关到没有为止：第一轮关掉 2 个，第二轮 0 个才收手。"""
    adapter = _adapter()
    page = FakeEditorPage()
    rounds = {"n": 0}
    real = page.evaluate

    def two_then_none(script, arg=None):
        if "op=closemodals" in script:
            rounds["n"] += 1
            page.calls.append(("closemodals", arg))
            return {"ok": True, "closed": 2 if rounds["n"] == 1 else 0}
        return real(script, arg)

    page.evaluate = two_then_none  # type: ignore[method-assign]
    adapter.close_modals(page)
    assert rounds["n"] == 2


def test_close_modals_gives_up_after_three_rounds():
    """弹窗越关越多（改版恶作剧）→ 最多 3 轮就放弃，不许死循环。"""
    adapter = _adapter()
    page = FakeEditorPage()
    rounds = {"n": 0}
    real = page.evaluate

    def endless(script, arg=None):
        if "op=closemodals" in script:
            rounds["n"] += 1
            return {"ok": True, "closed": 1}
        return real(script, arg)

    page.evaluate = endless  # type: ignore[method-assign]
    adapter.close_modals(page)
    assert rounds["n"] == 3


def test_close_modals_tolerates_script_without_payload():
    """关弹窗脚本返回 None（页面上下文崩了）→ 当 0 处理，不炸。"""
    adapter = _adapter()
    page = FakeEditorPage()
    page.evaluate = lambda script, arg=None: None  # type: ignore[method-assign]

    adapter.close_modals(page)  # 不抛异常即为通过
