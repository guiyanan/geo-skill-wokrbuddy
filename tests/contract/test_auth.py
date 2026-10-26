"""CT-05-04 / CT-05-05：auth 状态判定在生产代码里跑。

三份 fixture 的区别只有一件事：页面上有什么。所以这里断言的是
「看到 A → 判 logged_in，看到 B → 判 expired，看到 C → 判 unknown 且带 capcha」。
"""

from __future__ import annotations

import pytest

from geopublish.adapters.base import AuthResult, AuthState
from geopublish.adapters.csdn.flow import CsdnAdapter
from dom import StaticPage  # 同目录的测试辅助（pytest 会把 tests/contract 放进 sys.path）

pytestmark = pytest.mark.contract


@pytest.fixture
def adapter() -> CsdnAdapter:
    return CsdnAdapter()


# 每份快照配一个真实 URL：check_auth 会先看域名，URL 不对就该判「不在平台内」。
PAGE_URLS = {
    "editor": "https://mp.csdn.net/mp_blog/creation/editor",
    "login": "https://passport.csdn.net/login",
    "captcha": "https://passport.csdn.net/v1/register/pc/login/doLogin",
    "success": "https://blog.csdn.net/m0_61028198/article/details/169160036",
}


def page_for(fixture_dom, name: str) -> StaticPage:
    return StaticPage(fixture_dom("csdn", name), url=PAGE_URLS[name])


def test_editor_page_is_logged_in(adapter, fixture_dom):
    """CT-05-04：编辑器 + 头像都在 → logged_in。"""
    result = adapter.check_auth(page_for(fixture_dom, "editor"))

    assert isinstance(result, AuthResult)
    assert result.state is AuthState.LOGGED_IN
    assert result.has_challenge is False


def test_login_page_is_expired(adapter, fixture_dom):
    """CT-05-03：只有登录表单，没有头像 → expired（不要在这里重试，重试=撞风控）。"""
    result = adapter.check_auth(page_for(fixture_dom, "login"))

    assert result.state is AuthState.EXPIRED
    assert result.has_challenge is False
    assert "登录" in result.reason


def test_captcha_page_is_unknown_with_flag(adapter, fixture_dom):
    """CT-05-05：验证码页 → unknown 且 has_challenge=True。"""
    result = adapter.check_auth(page_for(fixture_dom, "captcha"))

    assert result.state is AuthState.UNKNOWN
    assert result.has_challenge is True


def test_captcha_takes_precedence_over_login_form(adapter, fixture_dom):
    """风控页**同时**有登录表单和验证码。先看登录表单就会判 expired 并去重试，
    而重试正是风控最敏感的信号 —— 所以顺序必须是验证码优先。

    fixture 里两者都在，这条断言才有意义。
    """
    page = page_for(fixture_dom, "captcha")
    assert page.query_selector("#loginForm") is not None, "前提变了：fixture 里得有登录表单"
    assert page.query_selector("#captchaImg") is not None

    result = adapter.check_auth(page)

    assert result.state is not AuthState.EXPIRED
    assert result.has_challenge is True
    assert "验证码" in result.reason


def test_blank_page_is_unknown_not_expired(adapter):
    """既没登录标志也没登录表单（页面没渲染完 / 被墙）→ unknown。

    这里不能猜 expired：猜错的代价是「让人白扫一次码」，更糟的是反复重试。
    """
    result = adapter.check_auth(StaticPage("<html><body><div id='app'></div></body></html>"))

    assert result.state is AuthState.UNKNOWN
    assert result.has_challenge is False
    assert "既没" in result.reason


def test_off_domain_page_is_never_logged_in(adapter, fixture_dom):
    """浏览器停在别人家页面（代理错误页/别的标签页）时，即使 DOM 里恰好有头像，
    也不许判已登录。

    真出过这类事故：拿一个没登录的页面去填空，一路到「提交」，最后发现发到别处去了。
    """
    page = StaticPage(fixture_dom("csdn", "editor"), url="https://example.com/proxy-error")

    result = adapter.check_auth(page)

    assert result.state is AuthState.UNKNOWN
    assert "域名" in result.reason


def test_page_without_url_still_judged_by_dom(adapter, fixture_dom):
    """about:blank 阶段 url 可能是空串，这时按 DOM 判，不至于误报「不在平台内」。"""
    page = StaticPage(fixture_dom("csdn", "editor"), url="")

    assert adapter.check_auth(page).state is AuthState.LOGGED_IN
