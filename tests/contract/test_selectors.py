"""CT-05-01/02/06：选择器本身。这是「改版只改 JSON」这条纪律的地基。

三条断言各挡一类事故：
- 选择器在 fixture 上**命中几个**——命中 0 个是改版，命中 2 个是选择器太松（会填错元素）
- 每个选择器都能被 CSS 引擎编译——手写 JSON 时最常见的错误是少个引号/括号
- 删掉任意一个键**立刻**报错——不留默认值，否则线上会拿一个空串去 query，判成「没找到」
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import soupsieve

from geopublish.adapters.base import REQUIRED_SELECTORS, SelectorError, Selectors
from geopublish.adapters.csdn.flow import CsdnAdapter, SELECTORS_PATH

pytestmark = pytest.mark.contract

REPO_ROOT = Path(__file__).resolve().parents[2]


def _leaf_paths() -> list[str]:
    """契约要求的叶子键（"组.键"），用于参数化删除。

    只取契约里的键：平台可以加自己的键（JSON 允许），但那不是「缺了就跑不了」的东西，
    拿它做删除测试会得到一条看起来像 bug 的失败。
    """
    return [f"{group}.{key}" for group, keys in REQUIRED_SELECTORS.items() for key in keys]


# ------------------------------------------------ CT-05-01 / CT-05-02 命中

def test_title_selector_hits_exactly_one_input(fixture_dom):
    """CT-05-01：标题选择器命中 1 个元素（真实 input，不是可见的 display 层）。

    命中 2 个是更隐蔽的错误：真跑的时候 Playwright 用第一个，你以为填了 A 其实填了 B。
    """
    from dom import StaticPage

    adapter = CsdnAdapter()
    hits = StaticPage(fixture_dom("csdn", "editor")).query_selector_all(
        adapter.selectors("editor", "title")
    )

    assert len(hits) == 1
    assert "article-bar__title--input" in (hits[0].get_attribute("class") or "")


def test_title_display_selector_hits_the_visible_layer(fixture_dom):
    """T-09b：display 展示层也得有 DOM 证明——填标题的第一步是点它。"""
    from dom import StaticPage

    adapter = CsdnAdapter()
    hits = StaticPage(fixture_dom("csdn", "editor")).query_selector_all(
        adapter.selectors("editor", "title_display")
    )

    assert len(hits) == 1


def test_content_selector_hits_the_cledit_container(fixture_dom):
    """CT-05-02：正文选择器落在 cledit 的 pre 上（T-09b：不是 CodeMirror）。"""
    from dom import StaticPage

    adapter = CsdnAdapter()
    hits = StaticPage(fixture_dom("csdn", "editor")).query_selector_all(
        adapter.selectors("editor", "content")
    )

    assert len(hits) == 1
    assert "editor__inner" in (hits[0].get_attribute("class") or "")


@pytest.mark.parametrize("group", ["editor"])
def test_every_selector_in_group_hits_on_editor_page(fixture_dom, group):
    """编辑器页上，editor 组选择器必须全部命中——任何一个落空都是改版或笔误。

    meta/cover 组不在这里：真机校准发现它们的控件在「发布文章」对话框里，
    单独对 publish-dialog fixture 断言（见下面的测试）。
    """
    from dom import StaticPage

    adapter = CsdnAdapter()
    page = StaticPage(fixture_dom("csdn", "editor"))
    missed = {
        key: selector
        for key, selector in adapter.selectors_book.group(group).items()
        if page.query_selector(selector) is None
    }

    assert not missed, f"{group} 组这些选择器没命中：{missed}"


@pytest.mark.parametrize("group", ["meta", "cover"])
def test_meta_and_cover_hit_on_the_publish_dialog(fixture_dom, group):
    """meta/cover 组的控件在发布对话框里。

    ⚠️ publish-dialog.html 目前是**占位快照**（未经真机校准，见 fixtures README）——
    这条测试只保证「选择器语法有效、形状自洽」，T-10 抓到真实对话框后替换 fixture，
    那时打不中会在这里先红。
    """
    from dom import StaticPage

    adapter = CsdnAdapter()
    page = StaticPage(fixture_dom("csdn", "publish-dialog"))
    missed = {
        key: selector
        for key, selector in adapter.selectors_book.group(group).items()
        if page.query_selector(selector) is None
    }

    assert not missed, f"{group} 组这些选择器没命中：{missed}"


def test_auth_selectors_hit_on_their_pages(fixture_dom):
    from dom import StaticPage

    adapter = CsdnAdapter()
    expectations = {
        ("login", "auth", "login_form"),
        ("captcha", "auth", "challenge"),
        ("captcha", "auth", "login_form"),
        ("editor", "auth", "avatar"),
    }
    for fixture, group, key in expectations:
        page = StaticPage(fixture_dom("csdn", fixture))
        assert page.query_selector(adapter.selectors(group, key)) is not None, (fixture, group, key)


def test_avatar_selector_hits_exactly_one_element(fixture_dom):
    """头像命中 2 个就说明选择器太松：推荐位里的作者头像也会算数。

    这不是洁癖——「看到头像 = 已登录」是个启发式，选择器越松越容易在
    半渲染的页面上误判成已登录，然后拿一个未登录的页面去填空。
    """
    from dom import StaticPage

    adapter = CsdnAdapter()
    page = StaticPage(fixture_dom("csdn", "editor"))

    hits = page.query_selector_all(adapter.selectors("auth", "avatar"))
    assert len(hits) == 1, f"命中 {len(hits)} 个，选择器太松：{hits}"


def test_verify_selectors_hit_on_the_success_page(fixture_dom):
    """回读用的两个选择器也要有 DOM 证明。

    没有断言过的选择器等于「写在 JSON 里的一句愿望」——T-09 真去回读时才发现打不中，
    那时已经发完文章了，代价高得多。
    """
    from dom import StaticPage

    adapter = CsdnAdapter()
    page = StaticPage(fixture_dom("csdn", "success"))

    for key in ("article_id", "success_marker"):
        assert page.query_selector(adapter.selectors("verify", key)) is not None, key
    assert page.query_selector(adapter.selectors("verify", "success_marker")).text_content().strip()


def test_draft_list_item_hits_on_the_draft_list_page(fixture_dom):
    """存草稿后 URL 不变时的唯一兜底：列表行每行都得有标题和 href（2026-10-22 真机校准）。

    href 是 article_id 的来源，所以不能只断言「命中」，还要断言「带得出链接」——
    否则 verify 会走到「标题对上了但解析不出 id」那条判失败的岔路上。
    """
    from dom import StaticPage

    adapter = CsdnAdapter()
    page = StaticPage(fixture_dom("csdn", "drafts"))

    hits = page.query_selector_all(adapter.selectors("verify", "draft_list_item"))
    assert len(hits) == 2
    for hit in hits:
        assert hit.text_content().strip(), "每行必须能读到标题"
        assert "articleId=" in (hit.get_attribute("href") or ""), "每行必须带可解析的 href"


def test_draft_list_tab_and_loading_hit_on_the_draft_list_page(fixture_dom):
    """兜底流程的两个步骤也要有 DOM 证明：tab 不点读到的是「全部」，加载圈不等就是空列表。"""
    from dom import StaticPage

    adapter = CsdnAdapter()
    page = StaticPage(fixture_dom("csdn", "drafts"))

    assert page.query_selector(adapter.selectors("verify", "draft_list_tab")) is not None
    assert page.query_selector(adapter.selectors("verify", "draft_list_loading")) is not None


# ------------------------------------------------ 结构校验

def test_contract_is_satisfied_by_the_shipped_file():
    """上线用的那份 selectors.json 必须满足 base.py 声明的契约。"""
    book = Selectors.load(SELECTORS_PATH)

    for group, keys in REQUIRED_SELECTORS.items():
        for key in keys:
            assert book(group, key).strip(), f"{group}.{key} 是空的"


def test_version_is_recorded_because_incidents_need_it():
    """现场包要记 selectors_version：改版后能反查「当时用的是哪版选择器」。"""
    book = Selectors.load(SELECTORS_PATH)

    assert book.version and book.version != "unknown"
    assert book.version == CsdnAdapter().selectors_version


def test_every_selector_compiles_as_css():
    """手写 JSON 最常见的错是少个引号/括号。编译一遍，1 毫秒。

    这条故意不在生产代码里做：那会让运行时要多一个 CSS 解析依赖。
    CI 挡住就够——选择器只在改版时变，改版一定会跑测试。
    """
    data = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    bad = {}
    for group, keys in data.items():
        if not isinstance(keys, dict):
            continue
        for key, selector in keys.items():
            try:
                soupsieve.compile(selector)
            except Exception as exc:  # noqa: BLE001 - 任何编译失败都要报出来
                bad[f"{group}.{key}"] = f"{selector!r}: {exc}"

    assert not bad, f"这些选择器编译不过：{bad}"


# ------------------------------------------------ CT-05-06 删键即报错

@pytest.mark.parametrize("leaf", _leaf_paths())
def test_deleting_any_selector_fails_fast(tmp_path, leaf):
    """CT-05-06：删掉任意一个键，加载就报错，**不许留默认值**。

    留默认值的后果：线上拿空串去 query_selector，返回 None，被解读成「页面没这个元素」，
    于是把「配置漏了」误诊成「平台改版」——排查方向直接跑偏。
    """
    data = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    group, key = leaf.split(".", 1)
    del data[group][key]
    broken = tmp_path / "selectors.json"
    broken.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(SelectorError) as exc:
        Selectors.load(broken)

    assert leaf in str(exc.value), "报错要说清是哪一条，不能只说「缺字段」"


def test_unknown_group_is_rejected(tmp_path):
    """组名拼错（ediitor）如果放过去，等于整组选择器静默失效。"""
    data = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    data["ediitor"] = data.pop("editor")
    broken = tmp_path / "selectors.json"
    broken.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(SelectorError) as exc:
        Selectors.load(broken)
    assert "ediitor" in str(exc.value)


def test_all_missing_keys_are_reported_at_once(tmp_path):
    """一次报全：改版时要一眼看完整缺口，而不是删一个跑一次。"""
    data = json.loads(SELECTORS_PATH.read_text(encoding="utf-8"))
    del data["editor"]["title"]
    del data["auth"]["challenge"]
    broken = tmp_path / "selectors.json"
    broken.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(SelectorError) as exc:
        Selectors.load(broken)
    message = str(exc.value)
    assert "editor.title" in message and "auth.challenge" in message


@pytest.mark.parametrize(
    "payload",
    [
        {},                                              # 空文件
        [],                                              # 顶层不是对象
        {"version": "1"},                                # 只有版本
        {"version": "1", "editor": {"title": ""}},       # 空选择器
        {"version": "1", "editor": {"title": "   "}},    # 全空格
        {"version": "1", "editor": {"title": 42}},       # 不是字符串
        {"version": "1", "editor": "#article-title"},    # 组写成字符串（改版时手滑的典型）
        {"version": "", "editor": {}},                   # 版本为空
        {"editor": {}},                                  # 缺 version
        {"version": "1", "editor": {}},                  # 组存在但空
    ],
)
def test_malformed_books_fail_loudly(tmp_path, payload):
    path = tmp_path / "selectors.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(SelectorError):
        Selectors.load(path)


def test_missing_file_and_broken_json_report_the_path(tmp_path):
    """配置文件出错时，报错必须带路径——否则用户不知道在读哪个文件。"""
    with pytest.raises(SelectorError) as exc:
        Selectors.load(tmp_path / "nope.json")
    assert "nope.json" in str(exc.value)

    broken = tmp_path / "broken.json"
    broken.write_text("{ 不是 json", encoding="utf-8")
    with pytest.raises(SelectorError) as exc2:
        Selectors.load(broken)
    assert "broken.json" in str(exc2.value)


def test_no_default_values_on_lookup():
    """取不存在的键必须炸。返回 ""/None 就是留了默认值。"""
    book = Selectors.load(SELECTORS_PATH)

    with pytest.raises(SelectorError):
        book("editor", "不存在的键")
    with pytest.raises(SelectorError):
        book("不存在的组", "title")
    with pytest.raises(SelectorError):
        book.group("不存在的组")


def test_selectors_are_deeply_immutable():
    """这本表是全局共享的。运行时被改一个选择器的话，出问题的位置会离改动点很远。"""
    book = Selectors.load(SELECTORS_PATH)

    with pytest.raises(TypeError):
        book.groups["editor"]["title"] = "#偷偷改掉"
    with pytest.raises(TypeError):
        book.groups["new_group"] = {}
