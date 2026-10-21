"""T-05 契约层本身：七方法在不在、没实现的有没有说谎、meta 是不是单一来源。

契约测试（`tests/contract/`）证明选择器对 DOM 有效；这里证明**结构**成立：
少一个方法就不是 adapter，没实现的方法必须明说属于哪张工单。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from geopublish.adapters.base import (
    AdapterContractError,
    AuthState,
    PlatformAdapter,
    REQUIRED_SELECTORS,
    SelectorError,
    Selectors,
    assert_adapter,
)
from geopublish.adapters.csdn.flow import META_PATH, SELECTORS_PATH, CsdnAdapter

pytestmark = pytest.mark.unit

SEVEN_METHODS = (
    "check_auth",
    "open_editor",
    "fill_content",
    "fill_meta",
    "upload_cover",
    "submit",
    "verify",
)


class _Recorder:
    """记录调用顺序的假 page：只用来验证 open_editor 的两步。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.url = "about:blank"

    def goto(self, url: str) -> None:
        self.calls.append(("goto", url))
        self.url = url

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000):
        self.calls.append(("wait_for_selector", selector))
        return object()


# --------------------------------------------------- 七方法契约

def test_csdn_adapter_satisfies_the_protocol():
    adapter = CsdnAdapter()
    assert_adapter(adapter)

    for name in SEVEN_METHODS:
        assert callable(getattr(adapter, name)), f"缺方法：{name}"
    assert adapter.name == "csdn"


def test_assert_adapter_names_what_is_missing():
    class _Half:
        name = "half"

        def check_auth(self, page): ...

    with pytest.raises(AdapterContractError) as exc:
        assert_adapter(_Half())

    message = str(exc.value)
    assert "fill_content" in message and "verify" in message


def test_assert_adapter_rejects_non_callable_attribute():
    """有个同名属性但不是方法，比缺方法更难查 —— 单独挡一层。"""

    class _Broken:
        name = "broken"
        check_auth = "我不是方法"

    with pytest.raises(AdapterContractError) as exc:
        assert_adapter(_Broken())
    assert "check_auth" in str(exc.value)


def test_assert_adapter_requires_name():
    """方法齐了但没 name，日志/现场包里就不知道是哪个平台炸的。"""

    class _NoName:
        def check_auth(self, page): ...
        def open_editor(self, page, draft): ...
        def fill_content(self, page, draft): ...
        def fill_meta(self, page, draft): ...
        def upload_cover(self, page, path): ...
        def submit(self, page): ...
        def verify(self, page): ...

    with pytest.raises(AdapterContractError) as exc:
        assert_adapter(_NoName())
    assert "name" in str(exc.value)


def test_protocol_is_runtime_checkable():
    """`isinstance` 能用，才谈得上「新增平台不改主程序」。"""
    assert isinstance(CsdnAdapter(), PlatformAdapter)


@pytest.mark.parametrize(
    "method, ticket, args",
    [
        ("fill_meta", "T-08", (object(), None)),
        ("upload_cover", "T-08", (object(), Path("cover.png"))),
        ("submit", "T-09", (object(),)),
        ("verify", "T-09", (object(),)),
    ],
)
def test_unimplemented_methods_name_their_ticket(method, ticket, args):
    """没实现就要**说出来属于哪张工单**。

    最危险的是写个 `pass` —— 流程会一路绿到「发布成功」，然后线上什么都没有。
    """
    adapter = CsdnAdapter()
    with pytest.raises(NotImplementedError) as exc:
        getattr(adapter, method)(*args)

    assert ticket in str(exc.value), f"{method} 的报错必须指出 {ticket}"
    assert method in str(exc.value)


# --------------------------------------------------- open_editor

def test_open_editor_navigates_then_waits_for_the_editor():
    """顺序不能反：先 goto 再等选择器。反了就是在上一页等，必然超时。"""
    adapter = CsdnAdapter()
    page = _Recorder()

    adapter.open_editor(page, draft=None)

    assert page.calls[0][0] == "goto"
    assert page.calls[0][1] == adapter.editor_url
    assert page.calls[1] == ("wait_for_selector", adapter.selectors("editor", "content"))
    assert page.url == adapter.editor_url


# --------------------------------------------------- meta 与单一来源

def test_meta_points_at_the_real_editor_and_domains():
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))

    assert meta["editor_url"].startswith("https://mp.csdn.net/")
    assert "passport.csdn.net" in meta["domains"], "登录态判定要靠它识别被踢回登录页"


def test_meta_does_not_duplicate_platform_limits():
    """字数/标题/标签上限**只**在 config/geopublish.json 里。

    两处都写 = 两份真相：改了一处忘了另一处，guard 会和实际发布对不上。
    """
    meta = json.loads(META_PATH.read_text(encoding="utf-8"))

    assert "limits" not in meta
    for suspicious in ("title_max", "tags_max", "word_min", "word_max"):
        assert suspicious not in meta


def test_adapter_reports_its_selectors_version():
    """现场包要记 selectors_version，改版后才能反查「当时用的哪一版」。"""
    assert CsdnAdapter().selectors_version == Selectors.load(SELECTORS_PATH).version


def test_adapter_uses_the_shipped_files_by_default():
    adapter = CsdnAdapter()

    assert adapter.selectors_path == SELECTORS_PATH
    assert adapter.selectors("auth", "challenge")
    assert "mp.csdn.net" in adapter.domains


def test_adapter_accepts_injected_selectors():
    """测试与排障时能塞一份改过的选择器，不用改仓库里的文件。"""
    custom = Selectors(
        version="test-1",
        groups={"auth": {"login_form": "#a", "captcha": "#b", "avatar": "#c"},
                "editor": {k: "#x" for k in REQUIRED_SELECTORS["editor"]}},
        path=Path("memory://test"),
    )

    adapter = CsdnAdapter(selectors=custom)

    assert adapter.selectors_version == "test-1"
    assert adapter.selectors("auth", "avatar") == "#c"


def test_missing_meta_file_is_reported_with_path(tmp_path):
    with pytest.raises(SelectorError) as exc:
        CsdnAdapter(meta_path=tmp_path / "nope.json")
    assert "nope.json" in str(exc.value)


def test_broken_meta_json_and_wrong_shape_are_reported(tmp_path):
    path = tmp_path / "meta.json"

    path.write_text("{ 坏掉的", encoding="utf-8")
    with pytest.raises(SelectorError) as exc:
        CsdnAdapter(meta_path=path)
    assert "meta.json" in str(exc.value)

    path.write_text(json.dumps(["不是对象"]), encoding="utf-8")
    with pytest.raises(SelectorError):
        CsdnAdapter(meta_path=path)


def test_meta_requires_editor_url_and_domains(tmp_path):
    for payload in ({}, {"editor_url": "https://mp.csdn.net/x"}, {"domains": ["mp.csdn.net"]}):
        path = tmp_path / "meta.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        with pytest.raises(SelectorError):
            CsdnAdapter(meta_path=path)


def test_meta_rejects_non_https_editor_url(tmp_path):
    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps({"editor_url": "ftp://mp.csdn.net/x", "domains": ["mp.csdn.net"]}),
        encoding="utf-8",
    )

    with pytest.raises(SelectorError) as exc:
        CsdnAdapter(meta_path=path)
    assert "editor_url" in str(exc.value)


def test_meta_login_url_must_be_absolute_http(tmp_path):
    """少写 `https://` 的登录页地址会打到一条相对路径上，人看到的是 404 空白页。"""
    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps(
            {
                "editor_url": "https://mp.csdn.net/x",
                "domains": ["mp.csdn.net"],
                "login_url": "passport.csdn.net/login",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SelectorError) as exc:
        CsdnAdapter(meta_path=path)
    assert "login_url" in str(exc.value)


def test_meta_login_url_must_be_a_string(tmp_path):
    path = tmp_path / "meta.json"
    path.write_text(
        json.dumps(
            {
                "editor_url": "https://mp.csdn.net/x",
                "domains": ["mp.csdn.net"],
                "login_url": 123,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SelectorError) as exc:
        CsdnAdapter(meta_path=path)
    assert "login_url" in str(exc.value)


def test_auth_state_values_are_stable_strings():
    """状态值会进日志/现场包，改字符串等于改日志语义。"""
    assert {s.value for s in AuthState} == {"logged_in", "expired", "unknown"}
