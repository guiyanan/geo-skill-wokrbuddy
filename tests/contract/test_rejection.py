"""平台拒稿检测的契约测试（T-12 / UT-12-03 的选择器侧）。

钉住两件事：
- verify.rejection_toast 选择器在拒稿 fixture 上命中，且能取出提示原文；
- 正常页面（编辑器/发布对话框）上它必须**不命中**——误命中会把正常发布判成拒稿。
"""

from __future__ import annotations

import pytest

from geopublish.adapters.base import ContentRejected
from geopublish.adapters.csdn.flow import CsdnAdapter
from dom import StaticPage  # 同目录的测试辅助（pytest 会把 tests/contract 放进 sys.path）

pytestmark = pytest.mark.contract

EDITOR_URL = "https://mp.csdn.net/mp_blog/creation/editor"


@pytest.fixture
def adapter() -> CsdnAdapter:
    return CsdnAdapter()


def test_rejection_toast_hits_on_rejected_page(adapter, fixture_dom):
    """拒稿 toast 命中，且原文取得出来（回写靠它）。"""
    page = StaticPage(fixture_dom("csdn", "rejected"), url=EDITOR_URL)

    hits = page.query_selector_all(adapter.selectors("verify", "rejection_toast"))

    assert len(hits) == 1, "命中 0 个是改版/笔误，命中 2 个是选择器太松"
    assert "敏感词" in hits[0].text_content()


def test_raise_if_rejected_raises_with_platform_text(adapter, fixture_dom):
    """adapter 层的判定入口：命中 → ContentRejected，异常里带平台原话。"""
    page = StaticPage(fixture_dom("csdn", "rejected"), url=EDITOR_URL)

    with pytest.raises(ContentRejected) as exc:
        adapter.raise_if_rejected(page)

    assert "敏感词" in exc.value.platform_text


def test_rejection_toast_absent_on_normal_pages(adapter, fixture_dom):
    """正常页面不许误命中：编辑器页和发布对话框都没有错误 toast。"""
    for name in ("editor", "publish-dialog"):
        page = StaticPage(fixture_dom("csdn", name), url=EDITOR_URL)
        assert page.query_selector(adapter.selectors("verify", "rejection_toast")) is None, name
        adapter.raise_if_rejected(page)  # 不抛


def test_empty_toast_is_not_a_rejection(adapter):
    """toast 壳在但取不出文字（渲染到一半）→ 不算拒稿，不抛。"""
    page = StaticPage(
        "<html><body><div class='el-message el-message--error'>"
        "<p class='el-message__content'></p></div></body></html>",
        url=EDITOR_URL,
    )

    adapter.raise_if_rejected(page)  # 不抛：没原话就没有可回写的东西
