"""T-09 提交（submit）：点之前必须复核正文，且只点「存草稿」。

钉死的行为：
- UT-09-03 提交前**必有一次**正文回读复核，且发生在点击之前（调用序列断言）
- UT-09-04 `--mode draft` 绝不点发布按钮（点击集合里不许出现 publish_button）
- 没有注入记录 / 回读失败 / 长度不符 → `SubmitBlocked`，**一个按钮都不点**
- 等不到成功提示不算失败也不算成功：`unknown`，判定权交给 verify
"""

from __future__ import annotations

import pytest

from geopublish.adapters.base import InjectFailed, SubmitBlocked, SubmitMode, SubmitState
from geopublish.adapters.csdn.flow import CsdnAdapter
from geopublish.core.clock import FakeClock
from geopublish.core.models import Draft, Meta

pytestmark = pytest.mark.unit

EDITOR_URL = "https://mp.csdn.net/mp_blog/creation/editor"


class FakeEditorPage:
    """按 `/* op= */` 分发的假编辑器页。

    三级注入的四个脚本都实现，是为了让「先 fill_content 再 submit」能原样跑完整条链：
    submit 的前置复核要测的就是「注入之后编辑器里是什么」，绕过 fill_content 就测不到。
    """

    def __init__(
        self,
        *,
        text: str = "",
        url: str = EDITOR_URL,
        marker_ok: bool = True,
        redirect_to: str | None = None,
        readback_raises: bool = False,
    ) -> None:
        self.text = text
        self.url = url
        self.marker_ok = marker_ok
        self.redirect_to = redirect_to
        self.readback_raises = readback_raises
        self.calls: list[tuple[str, str]] = []

    # --- 读面 ---
    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        self.calls.append((op, ""))
        if op == "paste":
            editable_sel, text = arg
            self.calls[-1] = (op, editable_sel)
            self.text = text
            return {"ok": True}
        if op == "dispatch":
            content_sel, text = arg
            self.calls[-1] = (op, content_sel)
            self.text = text
            return {"ok": True}
        if op == "readback":
            if self.readback_raises:
                raise RuntimeError("页面已销毁")
            return {"ok": True, "text": self.text}
        if op == "clear":
            self.text = ""
            return {"ok": True}
        raise AssertionError(f"未预期的脚本 op：{op}")

    # --- 写面 ---
    def click(self, selector: str) -> None:
        self.calls.append(("click", selector))
        if self.redirect_to:
            self.url = self.redirect_to

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        self.calls.append(("type", selector))
        self.text += text

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        self.calls.append(("wait_for_selector", selector))
        if not self.marker_ok:
            raise TimeoutError(f"Timeout {timeout_ms}ms exceeded")
        return object()

    def query_selector(self, selector: str):
        return None

    def query_selector_all(self, selector: str):
        return []

    def input_value(self, selector: str) -> str:
        return ""

    def goto(self, url: str) -> None:
        raise AssertionError("submit 不该导航")

    # --- 断言辅助 ---
    def ops(self) -> list[str]:
        return [op for op, _ in self.calls]

    def clicks(self) -> list[str]:
        return [arg for op, arg in self.calls if op == "click"]


def _adapter() -> CsdnAdapter:
    return CsdnAdapter(clock=FakeClock("2026-10-21T15:00:00+08:00"))


def _draft(body: str = "正文" * 100) -> Draft:
    return Draft(
        meta=Meta(slug="t09-submit", title="探针标题", ai_involvement="assisted"), body=body
    )


# ----------------------------------------------------------- UT-09-04

def test_ut_09_04_draft_mode_only_clicks_save_draft():
    """存草稿只许点 save_draft_button。点错按钮 = 当场公开一篇没自检的稿子。"""
    adapter = _adapter()
    page = FakeEditorPage()
    draft = _draft()

    adapter.fill_content(page, draft)
    adapter.submit(page)

    save_sel = adapter.selectors("editor", "save_draft_button")
    publish_sel = adapter.selectors("editor", "publish_button")
    assert page.clicks() == [save_sel], f"点了这些：{page.clicks()}"
    assert publish_sel not in page.clicks()


def test_submit_accepts_mode_from_string_and_enum():
    """mode 允许 "draft" 字符串（CLI 传来的就是字符串），归一后行为一致。"""
    for mode in (SubmitMode.DRAFT, "draft"):
        adapter = _adapter()
        page = FakeEditorPage()
        adapter.fill_content(page, _draft())
        result = adapter.submit(page, mode=mode)
        assert result.state is SubmitState.SUBMITTED


def test_invalid_mode_is_rejected_before_any_click():
    """拼错的 mode 不能被当成 draft 放行 —— 那等于给自己留了个静默后门。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    with pytest.raises(ValueError):
        adapter.submit(page, mode="draftt")
    assert page.clicks() == []


# ----------------------------------------------------------- UT-09-03

def test_ut_09_03_content_length_is_rechecked_right_before_the_click():
    """注入时回读一次、提交前再回读一次，且提交前那次必须在点击之前。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    readbacks_after_inject = page.ops().count("readback")
    adapter.submit(page)

    assert page.ops().count("readback") == readbacks_after_inject + 1, (
        "提交前必须**恰好**多一次回读复核"
    )
    last_readback = max(i for i, (op, _) in enumerate(page.calls) if op == "readback")
    click_at = page.calls.index(("click", adapter.selectors("editor", "save_draft_button")))
    assert last_readback < click_at, "复核要在点击之前，点了再复核没有任何意义"


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


# ----------------------------------------------------------- 拦截（不点按钮）

def test_submit_without_injection_record_is_blocked():
    """没有注入记录 = 不知道页面上是什么。宁可让人重跑，也不发内容不明的稿子。"""
    adapter = _adapter()
    page = FakeEditorPage(text="别人的半截草稿")

    with pytest.raises(SubmitBlocked) as exc:
        adapter.submit(page)

    assert "没有正文注入长度记录" in str(exc.value)
    assert page.clicks() == [], "拦住了就一个按钮都不许点"


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
    assert page.clicks() == []


def test_submit_blocks_when_readback_fails():
    """读不回正文就不能提交：读不出来 = 无法证明页面上是我们那篇。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    page.readback_raises = True
    with pytest.raises(SubmitBlocked) as exc:
        adapter.submit(page)

    assert "读不回正文" in str(exc.value)
    assert page.clicks() == []


def test_failed_reinjection_invalidates_the_previous_record():
    """再注入一次失败后，旧长度不许继续当基准 —— 否则复核会拿旧记录放行新残稿。"""
    adapter = _adapter()
    page = FakeEditorPage()
    adapter.fill_content(page, _draft())

    def _boom(*_args, **_kwargs):
        return {"ok": False, "reason": "编辑器被锁"}

    page.evaluate = _boom  # 三级注入全灭（L3 的 click 也会失败在 readback 之外）
    with pytest.raises((InjectFailed, Exception)):
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


# ----------------------------------------------------------- 点击之后的观察

def test_submit_reports_submitted_when_marker_appears():
    adapter = _adapter()
    page = FakeEditorPage(marker_ok=True)
    adapter.fill_content(page, _draft())

    result = adapter.submit(page)

    assert result.state is SubmitState.SUBMITTED
    assert "成功提示" in result.reason


def test_submit_reports_submitted_when_url_changes_without_marker():
    """没有 toast 但跳转了：也算已提交（DRAFT→PUBLISH 的页面差别就在这儿）。"""
    adapter = _adapter()
    page = FakeEditorPage(
        marker_ok=False, redirect_to=f"{EDITOR_URL}/139821477"
    )
    adapter.fill_content(page, _draft())

    result = adapter.submit(page)

    assert result.state is SubmitState.SUBMITTED
    assert "跳转" in result.reason


def test_submit_is_unknown_when_nothing_confirms():
    """既没提示也没跳转 → unknown。说「失败」会诱导人重发，说「成功」是撒谎。"""
    adapter = _adapter()
    page = FakeEditorPage(marker_ok=False)
    adapter.fill_content(page, _draft())

    result = adapter.submit(page)

    assert result.state is SubmitState.UNKNOWN
    assert "verify" in result.reason
