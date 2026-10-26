"""T-10 真实发布（submit/verify 的 publish 分支 + 兼容助手）。

钉死的行为：
- 没有注入记录 → SubmitBlocked（复核红线 R7，publish 同样适用）
- 发布对话框不在 → SubmitBlocked：拒绝在状态不明时点发布
- 发布模式**绝不调 close_modals**：发布对话框自己就是 .modal，关了等于摘了自己
- 点最终按钮后的成功信号按序裁决：跳详情页 → 页面长出详情链接 → URL 变化 → unknown
- verify(publish)：编辑器 URL 的 ?articleId= 是**草稿**信号（假阳性），发布只认
  详情页 URL 或详情链接
- _wait_selector/_wait_url：协议（timeout_ms）与真机 Playwright（timeout）两种
  参数名都要能活——假页面掩盖过这层差异（T-09b 协议假象），这里钉死双形态
"""

from __future__ import annotations

import pytest

from geopublish.adapters.base import SubmitBlocked, SubmitMode, SubmitState
from geopublish.adapters.csdn.flow import CsdnAdapter, _wait_selector, _wait_url
from geopublish.core.clock import FakeClock
from geopublish.core.models import Draft, Meta

pytestmark = pytest.mark.unit

EDITOR_URL = "https://editor.csdn.net/md?not_checkout=1"
DETAILS_URL = "https://blog.csdn.net/probe/article/details/168800001"


class FakePublishPage:
    """假发布页：对话框状态、最终按钮、URL 跳转都可编排。

    - dialog_open：发布对话框是否在（submit 的第二道刹车）
    - publish_ok：最终按钮点没点成
    - jump：点按钮后 URL 变成什么（None=纹丝不动；details=跳详情页）
    - link_href：页面上的详情链接（成功浮层形态；None=没有）
    """

    def __init__(
        self,
        *,
        dialog_open: bool = True,
        publish_ok: bool = True,
        jump: str | None = "details",
        link_href: str | None = None,
        text: str = "",
    ) -> None:
        self.dialog_open = dialog_open
        self.publish_ok = publish_ok
        self.jump = jump
        self.link_href = link_href
        self.text = text
        self.url = EDITOR_URL
        self.calls: list[tuple[str, object]] = []

    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        self.calls.append((op, arg))
        if op == "readback":
            return {"ok": True, "text": self.text}
        if op == "publishclick":
            if not self.publish_ok:
                return {"ok": False, "reason": "最终发布按钮未命中"}
            if self.jump == "details":
                self.url = DETAILS_URL
            elif self.jump:
                self.url = self.jump
            return {"ok": True}
        if op == "closemodals":
            raise AssertionError("发布模式绝不该关弹窗（会摘掉发布对话框自己）")
        raise AssertionError(f"未预期的脚本 op：{op}")

    def wait_for_url(self, pattern: str, timeout_ms: int = 10_000) -> None:
        self.calls.append(("wait_for_url", pattern))
        if "/article/details/" in pattern and "/article/details/" not in self.url:
            raise TimeoutError(f"Timeout {timeout_ms}ms exceeded")

    def query_selector(self, selector: str):
        adapter = _adapter()
        if selector == adapter.selectors("dialog", "root"):
            return object() if self.dialog_open else None
        if selector == adapter.selectors("verify", "published_link"):
            return _FakeLink(self.link_href) if self.link_href else None
        if selector == adapter.selectors("verify", "article_id"):
            return None
        return None

    def query_selector_all(self, selector: str):
        return []


class _FakeLink:
    def __init__(self, href: str) -> None:
        self._href = href

    def get_attribute(self, name: str):
        return self._href if name == "href" else None

    def text_content(self) -> str:
        return "查看文章"

    def is_visible(self) -> bool:
        return True


def _adapter() -> CsdnAdapter:
    return CsdnAdapter(clock=FakeClock("2026-10-22T10:00:00+08:00"))


def _draft(body: str = "正文" * 100) -> Draft:
    return Draft(
        meta=Meta(slug="t10-submit", title="探针标题", ai_involvement="assisted"), body=body
    )


def _prepared(adapter: CsdnAdapter, page: FakePublishPage) -> None:
    """走一遍 fill_content，让 adapter 有注入记录（复核的前提）。"""
    # fill_content 的 evaluate 序列（insert/paste/clear/focus/readback）在 FakePublishPage
    # 里没全实现——直接复用 readback 通路：注入记录 = 正文长度。
    adapter._injected_len = len(page.text) if page.text else None


# ----------------------------------------------------------- 刹车

def test_publish_without_injection_record_is_blocked():
    adapter = _adapter()
    with pytest.raises(SubmitBlocked) as exc:
        adapter.submit(FakePublishPage(), mode=SubmitMode.PUBLISH)
    assert "注入" in str(exc.value)


def test_publish_without_dialog_is_blocked_nothing_clicked():
    """对话框不在 = 状态不明：SubmitBlocked，且一个发布脚本都没跑。"""
    adapter = _adapter()
    page = FakePublishPage(dialog_open=False, text="正文" * 100)
    _prepared(adapter, page)

    with pytest.raises(SubmitBlocked) as exc:
        adapter.submit(page, mode=SubmitMode.PUBLISH)

    assert "对话框" in str(exc.value)
    assert "publishclick" not in [op for op, _ in page.calls]


def test_publish_never_closes_modals():
    """发布对话框就是 .modal：submit(publish) 全程不许碰 closemodals。"""
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100)
    _prepared(adapter, page)

    adapter.submit(page, mode=SubmitMode.PUBLISH)  # closemodals 一跑 Fake 就炸
    assert "closemodals" not in [op for op, _ in page.calls]


# ----------------------------------------------------------- 点击与裁决

def test_publish_jump_to_details_is_submitted():
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100, jump="details")
    _prepared(adapter, page)

    result = adapter.submit(page, mode=SubmitMode.PUBLISH)

    assert result.state is SubmitState.SUBMITTED
    assert "详情页" in result.reason


def test_publish_button_miss_is_unknown_and_verify_decides():
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100, publish_ok=False)
    _prepared(adapter, page)

    result = adapter.submit(page, mode=SubmitMode.PUBLISH)

    assert result.state is SubmitState.UNKNOWN
    assert "没点成" in result.reason
    assert "wait_for_url" not in [op for op, _ in page.calls], "没点成就别等 URL"


def test_publish_link_appearing_is_submitted():
    """没跳详情页，但页面长出详情链接（成功浮层形态）→ submitted。"""
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100, jump=None, link_href=DETAILS_URL)
    _prepared(adapter, page)

    result = adapter.submit(page, mode=SubmitMode.PUBLISH)

    assert result.state is SubmitState.SUBMITTED
    assert "详情链接" in result.reason


def test_publish_url_change_without_details_is_submitted():
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100, jump="https://mp.csdn.net/console/home")
    _prepared(adapter, page)

    result = adapter.submit(page, mode=SubmitMode.PUBLISH)

    assert result.state is SubmitState.SUBMITTED
    assert "已变化" in result.reason


def test_publish_nothing_happens_is_unknown():
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100, jump=None)
    _prepared(adapter, page)

    result = adapter.submit(page, mode=SubmitMode.PUBLISH)

    assert result.state is SubmitState.UNKNOWN
    assert "verify" in result.reason


# ----------------------------------------------------------- verify(publish)

def test_verify_publish_reads_id_from_details_url():
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100)
    page.url = DETAILS_URL  # submit 之后跳过来的状态

    result = adapter.verify(page, mode=SubmitMode.PUBLISH)

    assert result.ok is True
    assert result.article_id == "168800001"
    assert result.url == DETAILS_URL


def test_verify_publish_details_url_without_id_fails():
    """在详情页却读不到 id = 页面结构改版：明确失败，不猜成功。"""

    class _NoId(FakePublishPage):
        @property
        def url(self):  # type: ignore[override]
            return "https://blog.csdn.net/probe/article/details/"

        @url.setter
        def url(self, value):
            pass

    adapter = _adapter()
    result = adapter.verify(_NoId(), mode=SubmitMode.PUBLISH)
    assert result.ok is False
    assert "读不到 article_id" in result.reason


def test_verify_publish_reads_id_from_published_link():
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100, jump=None, link_href=DETAILS_URL)

    result = adapter.verify(page, mode=SubmitMode.PUBLISH)

    assert result.ok is True
    assert result.article_id == "168800001"
    assert result.url == DETAILS_URL


def test_verify_publish_link_without_id_fails():
    adapter = _adapter()
    page = FakePublishPage(
        text="正文" * 100, jump=None, link_href="https://blog.csdn.net/probe"
    )

    result = adapter.verify(page, mode=SubmitMode.PUBLISH)

    assert result.ok is False
    assert "没有发布成功信号" in result.reason


def test_verify_publish_editor_article_id_is_a_draft_signal_not_success():
    """编辑器 URL 的 ?articleId= 在草稿阶段就长出来了——拿它当发布成功是假阳性。"""
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100, jump=None)
    page.url = f"{EDITOR_URL}&articleId=168659812"  # 草稿信号

    result = adapter.verify(page, mode=SubmitMode.PUBLISH)

    assert result.ok is False, "编辑器 URL 的 articleId 不许算发布成功"


# ----------------------------------------------------------- 兼容助手（协议 vs 真机参数名）

class _RealShapedPage:
    """只认真机参数名（timeout=）的假页——timeout_ms 一律 TypeError。"""

    def __init__(self) -> None:
        self.kwargs: list[dict] = []

    def wait_for_selector(self, selector: str, **kwargs):
        if "timeout_ms" in kwargs:
            raise TypeError("unexpected keyword argument 'timeout_ms'")
        self.kwargs.append(kwargs)
        return object()

    def wait_for_url(self, pattern: str, **kwargs):
        if "timeout_ms" in kwargs:
            raise TypeError("unexpected keyword argument 'timeout_ms'")
        self.kwargs.append(kwargs)


def test_wait_selector_falls_back_to_real_playwright_kwarg():
    page = _RealShapedPage()
    _wait_selector(page, ".x", 1234)
    assert page.kwargs == [{"timeout": 1234}]


def test_wait_url_falls_back_to_real_playwright_kwarg():
    page = _RealShapedPage()
    _wait_url(page, "**/article/details/*", 5678)
    assert page.kwargs == [{"timeout": 5678}]


def test_publish_link_without_id_falls_through_to_unknown():
    """页面上有详情链接但 href 里解析不出 id（改版）→ 不能算成功信号。"""
    adapter = _adapter()
    page = FakePublishPage(
        text="正文" * 100, jump=None, link_href="https://blog.csdn.net/probe"
    )
    _prepared(adapter, page)

    result = adapter.submit(page, mode=SubmitMode.PUBLISH)

    assert result.state is SubmitState.UNKNOWN


def test_verify_publish_normalizes_share_params_off_the_url():
    """发布跳转的 URL 带 share* 追踪参数（实测）——回执必须落裸详情页地址。"""
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100)
    page.url = DETAILS_URL + "?sharetype=blogdetail&sharerId=169160036&sharesource=probe"

    result = adapter.verify(page, mode=SubmitMode.PUBLISH)

    assert result.ok is True
    assert result.url == DETAILS_URL, f"URL 必须归一化，实际 {result.url}"


def test_verify_publish_normalizes_share_params_off_the_url():
    """发布跳转的 URL 带 share* 追踪参数（实测）——回执必须落裸详情页地址。"""
    adapter = _adapter()
    page = FakePublishPage(text="正文" * 100)
    page.url = DETAILS_URL + "?sharetype=blogdetail&sharerId=169160036&sharesource=probe"

    result = adapter.verify(page, mode=SubmitMode.PUBLISH)

    assert result.ok is True
    assert result.url == DETAILS_URL, f"URL 必须归一化，实际 {result.url}"


def test_canonical_url_passthrough_and_no_account_cases():
    """_canonical_article_url 的边界：非详情 URL 原样返回；无账号段详情 URL 也归一。"""
    from geopublish.adapters.csdn.flow import _canonical_article_url

    assert _canonical_article_url("https://editor.csdn.net/md?articleId=1", "1") == (
        "https://editor.csdn.net/md?articleId=1"
    )
    assert _canonical_article_url(
        "https://blog.csdn.net/article/details/42?sharetype=x", "42"
    ) == "https://blog.csdn.net/article/details/42"
    assert _canonical_article_url("", "1") == ""


def test_canonical_url_passthrough_and_no_account_cases():
    """_canonical_article_url 的边界：非详情 URL 原样返回；无账号段详情 URL 也归一。"""
    from geopublish.adapters.csdn.flow import _canonical_article_url

    assert _canonical_article_url("https://editor.csdn.net/md?articleId=1", "1") == (
        "https://editor.csdn.net/md?articleId=1"
    )
    assert _canonical_article_url(
        "https://blog.csdn.net/article/details/42?sharetype=x", "42"
    ) == "https://blog.csdn.net/article/details/42"
    assert _canonical_article_url("", "1") == ""
