"""T-09 回读确认（verify）：拿不到 article_id 就不算成功。

钉死的行为：
- UT-09-01 拿不到 article_id → `ok=False`（即便页面上有成功提示）
- UT-09-02 URL 没变、DOM 里也没 id，但草稿列表页能查到同标题 → 判成功并带回 id
- id 的三个来源：DOM meta、URL 查询参数、URL 路径
- 发布模式不走草稿列表兜底（已发布的文章本来就不在草稿箱里）
"""

from __future__ import annotations

import pytest

from geopublish.adapters.base import SubmitMode
from geopublish.adapters.csdn.flow import CsdnAdapter
from geopublish.core.clock import FakeClock

pytestmark = pytest.mark.unit

EDITOR_URL = "https://mp.csdn.net/mp_blog/creation/editor"
LIST_URL = "https://mp.csdn.net/mp_blog/manage/article"
TITLE = "文档解析为什么是 RAG 的第一道工序"
DRAFT_HREF = "//mp.csdn.net/mp_blog/creation/editor/139821477"


class _Item:
    """草稿列表里的一行：标题文本 + 链接 href。"""

    def __init__(self, text: str, href: str | None) -> None:
        self._text = text
        self._href = href

    def text_content(self) -> str:
        return self._text

    def get_attribute(self, name: str) -> str | None:
        return self._href if name == "href" else None

    def is_visible(self) -> bool:
        return True


class _MetaElement:
    def __init__(self, content: str) -> None:
        self._content = content

    def text_content(self) -> str:
        return ""

    def get_attribute(self, name: str) -> str | None:
        return self._content if name == "content" else None

    def is_visible(self) -> bool:
        return True


class FakeVerifyPage:
    """假页面：能表达「成功页」「存草稿后没跳转的编辑器页」「草稿列表页」三种形态。"""

    def __init__(
        self,
        adapter: CsdnAdapter,
        *,
        url: str = EDITOR_URL,
        article_id: str | None = None,
        title: str | None = TITLE,
        items: list[_Item] | None = None,
        goto_raises: bool = False,
        tab_ok: bool = True,
    ) -> None:
        self._id_sel = adapter.selectors("verify", "article_id")
        self._item_sel = adapter.selectors("verify", "draft_list_item")
        self._title_sel = adapter.selectors("editor", "title")
        self.url = url
        self._article_id = article_id
        self._title = title
        self._items = items if items is not None else []
        self._goto_raises = goto_raises
        self._tab_ok = tab_ok
        self.navigated: list[str] = []
        self.tab_clicks = 0
        self.hidden_waits: list[str] = []

    def query_selector(self, selector: str):
        if selector == self._id_sel and self._article_id is not None:
            return _MetaElement(self._article_id)
        return None

    def query_selector_all(self, selector: str):
        return list(self._items) if selector == self._item_sel else []

    def input_value(self, selector: str) -> str:
        if selector == self._title_sel and self._title is not None:
            return self._title
        raise RuntimeError("元素不存在（成功页没有标题输入框）")

    def goto(self, url: str) -> None:
        if self._goto_raises:
            raise TimeoutError("net::ERR_TIMED_OUT")
        self.navigated.append(url)
        self.url = url

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        return object()

    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        if op == "drafttab":
            if not self._tab_ok:
                return {"ok": False, "reason": "没找到「草稿箱」tab"}
            self.tab_clicks += 1
            return {"ok": True}
        raise AssertionError(f"verify 不该执行脚本 op：{op}")

    def wait_for_hidden(self, selector: str, timeout_ms: int = 10_000) -> None:
        self.hidden_waits.append(selector)

    def click(self, selector: str) -> None:
        raise AssertionError("verify 不该点击")

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        raise AssertionError("verify 不该键入")

    def press(self, selector: str, key: str) -> None:
        raise AssertionError("verify 不该按键")

    def set_input_files(self, selector: str, path) -> None:
        raise AssertionError("verify 不该上传")


def _adapter(meta_path=None) -> CsdnAdapter:
    return CsdnAdapter(clock=FakeClock("2026-10-21T15:10:00+08:00"), meta_path=meta_path)


# ----------------------------------------------------------- id 的三个来源

@pytest.mark.parametrize(
    "url, expected",
    [
        (f"{EDITOR_URL}?articleId=888", "888"),
        (f"{EDITOR_URL}?article_id=889&x=1", "889"),
        ("https://blog.csdn.net/shiyimi/article/details/777", "777"),
        (f"{EDITOR_URL}/139821477", "139821477"),
    ],
    ids=["query_camel", "query_snake", "details_path", "editor_path"],
)
def test_article_id_is_read_from_the_url(url, expected):
    """平台把 id 放在哪儿不由我们决定：三种常见形态都认。"""
    adapter = _adapter()
    page = FakeVerifyPage(adapter, url=url, title=None)

    result = adapter.verify(page)

    assert result.ok is True
    assert result.article_id == expected
    assert page.navigated == [], "URL 里就有 id，不该再多跑一趟列表页"


def test_article_id_is_read_from_the_dom_marker():
    """发布成功页把 id 写进 meta：这是最可靠的来源，优先于 URL。"""
    adapter = _adapter()
    page = FakeVerifyPage(
        adapter, url=f"{EDITOR_URL}/111", article_id="139821477", title=None
    )

    result = adapter.verify(page)

    assert result.ok is True
    assert result.article_id == "139821477"
    assert page.navigated == []


# ----------------------------------------------------------- UT-09-01

def test_ut_09_01_no_article_id_is_a_failure_even_with_success_tip():
    """有「成功」提示但拿不到 id，且列表里也查不到 → 判失败。

    这正是最危险的形态：人看到提示会以为发出去了，实际上什么都没有。
    """
    adapter = _adapter()
    page = FakeVerifyPage(adapter, items=[_Item("别的文章", DRAFT_HREF)])

    result = adapter.verify(page)

    assert result.ok is False
    assert result.article_id == ""
    assert TITLE in result.reason, "报错要说清在找哪一篇"
    assert "没有标题" in result.reason


# ----------------------------------------------------------- UT-09-02

def test_ut_09_02_draft_list_match_confirms_the_save():
    """URL 没变、DOM 里没有 id，但列表页有同标题 → 成功，id 与 URL 从 href 里取。

    列表必须先点「草稿箱」tab、再等加载圈消失（T-09b 实测：不等就是空列表假阴性）。
    """
    adapter = _adapter()
    page = FakeVerifyPage(adapter, items=[_Item("旧稿", DRAFT_HREF), _Item(TITLE, DRAFT_HREF)])

    result = adapter.verify(page)

    assert result.ok is True
    assert result.article_id == "139821477"
    assert result.url == "https://mp.csdn.net/mp_blog/creation/editor/139821477"
    assert page.navigated == [LIST_URL]
    assert page.tab_clicks == 1, "不点草稿箱 tab 读到的是「全部」"
    assert page.hidden_waits == [adapter.selectors("verify", "draft_list_loading")]


def test_draft_list_tab_missing_is_a_failure():
    """草稿箱 tab 都没了（大改版）→ 明确判失败，别拿「全部」tab 的列表瞎比对。"""
    adapter = _adapter()
    page = FakeVerifyPage(adapter, tab_ok=False, items=[_Item(TITLE, DRAFT_HREF)])

    result = adapter.verify(page)

    assert result.ok is False
    assert "tab 点不开" in result.reason


def test_title_match_ignores_surrounding_whitespace():
    """列表里的标题常带换行/缩进，逐字节比会把「发成功了」判成失败。"""
    adapter = _adapter()
    page = FakeVerifyPage(
        adapter, title=f"  {TITLE}  ", items=[_Item(f"\n  {TITLE}\n", DRAFT_HREF)]
    )

    assert adapter.verify(page).ok is True


def test_matched_title_without_article_id_is_still_a_failure():
    """标题对上了、链接里解析不出 id → 仍然失败：没有 id 就没法回执、没法监测。"""
    adapter = _adapter()
    page = FakeVerifyPage(adapter, items=[_Item(TITLE, "//mp.csdn.net/mp_blog/manage/article")])

    result = adapter.verify(page)

    assert result.ok is False
    assert "解析不出 article_id" in result.reason


def test_matched_title_without_href_is_a_failure():
    adapter = _adapter()
    page = FakeVerifyPage(adapter, items=[_Item(TITLE, None)])

    result = adapter.verify(page)

    assert result.ok is False
    assert "无 href" in result.reason


# ----------------------------------------------------------- 兜底路径的边界

def test_unreadable_title_skips_the_list_fallback():
    """读不到标题就没法比对，别去列表页瞎找一通再报「没找到」。"""
    adapter = _adapter()
    page = FakeVerifyPage(adapter, title=None, items=[_Item(TITLE, DRAFT_HREF)])

    result = adapter.verify(page)

    assert result.ok is False
    assert "读不到编辑器标题" in result.reason
    assert page.navigated == []


def test_missing_draft_list_url_is_reported_explicitly(tmp_path):
    """meta.json 没配兜底页 → 明确说「没法确认」，而不是猜成失败或成功。"""
    import json

    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps(
            {
                "editor_url": EDITOR_URL,
                "domains": ["mp.csdn.net"],
                "cover": {"width": 1140, "height": 640, "max_bytes": 5242880},
            }
        ),
        encoding="utf-8",
    )
    adapter = _adapter(meta_path=path)
    page = FakeVerifyPage(adapter, items=[_Item(TITLE, DRAFT_HREF)])

    result = adapter.verify(page)

    assert result.ok is False
    assert "draft_list_url" in result.reason
    assert page.navigated == []


def test_empty_draft_list_reports_the_count():
    adapter = _adapter()
    page = FakeVerifyPage(adapter, items=[])

    result = adapter.verify(page)

    assert result.ok is False
    assert "0 条" in result.reason


def test_draft_list_navigation_failure_is_a_result_not_an_exception():
    """列表页打不开也是「没验证成功」，不该让异常冒到上层去当崩溃。"""
    adapter = _adapter()
    page = FakeVerifyPage(adapter, goto_raises=True)

    result = adapter.verify(page)

    assert result.ok is False
    assert "打不开" in result.reason


def test_publish_mode_never_falls_back_to_the_draft_list():
    """发布模式的稿子不在草稿箱里，拿它兜底只会得到一个假阴性。"""
    adapter = _adapter()
    page = FakeVerifyPage(adapter, items=[_Item(TITLE, DRAFT_HREF)])

    result = adapter.verify(page, mode=SubmitMode.PUBLISH)

    assert result.ok is False
    assert "不走草稿列表兜底" in result.reason
    assert page.navigated == []


def test_verify_accepts_mode_from_string():
    adapter = _adapter()
    page = FakeVerifyPage(adapter, items=[_Item(TITLE, DRAFT_HREF)])

    assert adapter.verify(page, mode="publish").ok is False
    assert adapter.verify(page, mode="draft").ok is True
