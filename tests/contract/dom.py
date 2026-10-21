"""把 fixtures 里的 HTML 包成和 Playwright `Page` 同形状的只读对象。

为什么要有它：契约测试要断言的是「**选择器对真实 DOM 是否命中**」。
如果这里 mock 掉 adapter，就只剩自己跟自己对答案——选择器写错了测试照样绿。
有了这层，`check_auth` 这类生产代码**原样执行**，只是跑在静态 HTML 上。

代价是只实现了只读部分：`goto` / `fill` / `click` 一律抛错。这不是缺陷——
如果哪天生产代码在 check_auth 里偷偷导航，这个类会立刻把它顶出来。
"""

from __future__ import annotations

from bs4 import BeautifulSoup


class StaticPageError(RuntimeError):
    """静态快照不支持的动作。"""


class StaticElement:
    def __init__(self, tag) -> None:
        self._tag = tag

    def text_content(self) -> str:
        return self._tag.get_text()

    def get_attribute(self, name: str) -> str | None:
        value = self._tag.get(name)
        if value is None:
            return None
        if isinstance(value, list):  # class="a b" 被解析成列表的情况
            return " ".join(value)
        return str(value)

    def is_visible(self) -> bool:
        return not self._tag.has_attr("hidden")

    def __repr__(self) -> str:  # 断言失败时能看出命中了哪个标签
        return f"<{self._tag.name} {dict(self._tag.attrs)}>"


class StaticPage:
    def __init__(self, html: str, url: str = "https://mp.csdn.net/") -> None:
        self._soup = BeautifulSoup(html, "html.parser")
        self._url = url

    @property
    def url(self) -> str:
        return self._url

    def query_selector(self, selector: str) -> StaticElement | None:
        found = self._soup.select(selector)
        return StaticElement(found[0]) if found else None

    def query_selector_all(self, selector: str) -> list[StaticElement]:
        return [StaticElement(tag) for tag in self._soup.select(selector)]

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000) -> StaticElement:
        element = self.query_selector(selector)
        if element is None:
            raise StaticPageError(f"静态快照里没有命中：{selector}")
        return element

    # --- 以下动作需要真浏览器，静态快照明确拒绝 ---

    def goto(self, url: str) -> None:
        raise StaticPageError(
            "静态快照不能导航。需要导航的流程请用 integration 测试（真 chromium 或假站点）。"
        )

    def evaluate(self, script: str, arg=None):
        raise StaticPageError(
            "静态快照不能执行脚本。注入/回读请用 unit 的假编辑器页或 integration 真浏览器。"
        )

    def click(self, selector: str) -> None:
        raise StaticPageError("静态快照不能点击。")

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        raise StaticPageError("静态快照不能键入。")

    def press(self, selector: str, key: str) -> None:
        raise StaticPageError("静态快照不能按键。")

    def set_input_files(self, selector: str, path) -> None:
        raise StaticPageError("静态快照不能上传文件。")
