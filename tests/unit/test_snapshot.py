"""T-04 快照：`geopublish snapshot <url> <name>`。

浏览器不可用/页面是空壳这类事，必须在**写文件之前**就炸掉，否则 fixture 目录里
会出现一个 0 字节的 page.html，而 T-05 的契约测试会拿它去断言选择器能命中——
那时候的失败现场离真正的原因已经很远了。

所以这里全部用注入的假 driver 跑，真 chromium 只放在 integration 里验一次接线。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.incident import Capture
from geopublish.core.snapshot import (
    DriverUnavailable,
    PlaywrightDriver,
    SnapshotError,
    capture,
)

CLOCK_ISO = "2026-10-21T10:00:00+08:00"
pytestmark = pytest.mark.unit
HTML = "<html><head><title>写文章</title></head><body><div id='editor'></div></body></html>"
PNG = b"\x89PNG\r\n\x1a\n real-ish screenshot"


class FakeDriver:
    """记录被怎么调用，返回固定素材。"""

    name = "fake"

    def __init__(self, capture_data: Capture | None = None) -> None:
        self.calls: list[str] = []
        self._data = capture_data or Capture(
            page_html=HTML, screenshot=PNG, network_har='{"log":{}}', console_log="[log] ok"
        )

    def fetch(self, url: str) -> Capture:
        self.calls.append(url)
        return self._data


@pytest.fixture
def out_dir(tmp_path) -> Path:
    d = tmp_path / "snapshots"
    d.mkdir()
    return d


# --------------------------------------------------------- 正常路径

def test_capture_writes_three_files(out_dir):
    target = capture(
        "https://mp.csdn.net/edit", "csdn/editor", out_dir,
        driver=FakeDriver(), clock=FakeClock(CLOCK_ISO),
    )

    assert sorted(p.name for p in target.iterdir()) == ["meta.json", "page.html", "screenshot.png"]
    assert (target / "page.html").read_text(encoding="utf-8") == HTML
    assert (target / "screenshot.png").read_bytes() == PNG


def test_capture_passes_url_through_untouched(out_dir):
    """URL 里的 query 不能被改写去过：有些平台靠它定位稿子。"""
    driver = FakeDriver()
    url = "https://mp.csdn.net/edit?id=123&from=draft"
    capture(url, "csdn/editor", out_dir, driver=driver, clock=FakeClock(CLOCK_ISO))

    assert driver.calls == [url]


def test_meta_records_hash_so_fixture_drift_is_visible(out_dir):
    """T-05 的契约测试要知道 fixture 什么时候变过——hash 就是为了这个。"""
    target = capture(
        "https://mp.csdn.net/edit", "csdn/editor", out_dir,
        driver=FakeDriver(), clock=FakeClock(CLOCK_ISO),
    )
    meta = json.loads((target / "meta.json").read_text(encoding="utf-8"))

    assert meta["html_sha256"] == hashlib.sha256(HTML.encode("utf-8")).hexdigest()
    assert meta["html_bytes"] == len(HTML.encode("utf-8"))
    assert meta["screenshot_bytes"] == len(PNG)
    assert meta["driver"] == "fake"
    assert meta["captured_at"].startswith("2026-10-21T10:00:00")
    assert meta["name"] == "csdn/editor"


def test_meta_url_is_scrubbed_but_still_identifiable(out_dir):
    """截图页面的 URL 可能带着一次性 token，meta.json 是会被提交/分享的。"""
    target = capture(
        "https://mp.csdn.net/edit?id=9&token=tok_live_abcdef", "csdn/editor", out_dir,
        driver=FakeDriver(), clock=FakeClock(CLOCK_ISO),
    )
    raw = (target / "meta.json").read_text(encoding="utf-8")

    assert "tok_live_abcdef" not in raw
    assert "mp.csdn.net/edit" in raw and "id=9" in raw


# --------------------------------------------------------- 空素材即失败

@pytest.mark.parametrize(
    "data, reason",
    [
        (Capture(page_html="   ", screenshot=PNG), "page.html"),
        (Capture(page_html=HTML, screenshot=b""), "screenshot.png"),
    ],
)
def test_empty_capture_fails_before_writing_anything(out_dir, data, reason):
    with pytest.raises(SnapshotError) as exc:
        capture("https://x.test/", "csdn/editor", out_dir, driver=FakeDriver(data),
                clock=FakeClock(CLOCK_ISO))

    assert reason in str(exc.value)
    assert list(out_dir.iterdir()) == [], "抓取失败不许留下空壳 fixture"


# --------------------------------------------------------- name 校验

@pytest.mark.parametrize("url", ["", "   ", "ftp://x.test/a", "javascript:alert(1)", "mp.csdn.net/edit"])
def test_rejects_unusable_urls_before_opening_browser(out_dir, url):
    """只认 http/https/file：`javascript:` 和裸域名会让浏览器做出意外的事。"""
    driver = FakeDriver()
    with pytest.raises(SnapshotError):
        capture(url, "csdn/editor", out_dir, driver=driver, clock=FakeClock(CLOCK_ISO))
    assert driver.calls == []


@pytest.mark.parametrize(
    "name",
    ["../etc/passwd", "/etc/passwd", "csdn//editor", "csdn/../editor", "", "  ", "csdn/编辑", "csdn\\editor"],
)
def test_rejects_dangerous_fixture_names(out_dir, name):
    """name 会直接拼成路径，还可能是从命令行粘进来的。"""
    driver = FakeDriver()
    with pytest.raises(SnapshotError) as exc:
        capture("https://x.test/", name, out_dir, driver=driver, clock=FakeClock(CLOCK_ISO))

    assert "name" in str(exc.value)
    assert driver.calls == [], "名字不合法就不该去开浏览器"


def test_nested_name_creates_subdir(out_dir):
    target = capture("https://x.test/", "csdn/editor", out_dir,
                     driver=FakeDriver(), clock=FakeClock(CLOCK_ISO))
    assert target == out_dir / "csdn" / "editor"


def test_out_dir_that_is_a_file_is_reported_clearly(tmp_path):
    blocker = tmp_path / "shots"
    blocker.write_text("我不是目录", encoding="utf-8")

    with pytest.raises(SnapshotError) as exc:
        capture("https://x.test/", "csdn/editor", blocker,
                driver=FakeDriver(), clock=FakeClock(CLOCK_ISO))
    assert "不是目录" in str(exc.value)


# ------------------------------------------------------ playwright driver

class _FakeConsole:
    def __init__(self, kind: str, text: str) -> None:
        self.type = kind
        self.text = text


class _FakePage:
    def __init__(self) -> None:
        self.handlers: dict[str, object] = {}
        self.goto_args: tuple[str, str] | None = None
        self.timeout: int | None = None

    def set_default_timeout(self, ms: int) -> None:
        self.timeout = ms

    def on(self, event: str, handler) -> None:
        self.handlers[event] = handler

    def goto(self, url: str, wait_until: str) -> None:
        self.goto_args = (url, wait_until)
        self.handlers["console"](_FakeConsole("error", "boom: null reference"))

    def content(self) -> str:
        return HTML

    def screenshot(self) -> bytes:
        return PNG


class _FakeContext:
    def __init__(self, har_path: Path) -> None:
        self.har_path = har_path
        self.page = _FakePage()
        self.closed = False

    def new_page(self) -> _FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True
        # chromium 在 context.close() 时才把 HAR 刷到磁盘
        self.har_path.write_text('{"log": {"entries": [1]}}', encoding="utf-8")


class _FakeBrowser:
    def __init__(self, context: _FakeContext) -> None:
        self._context = context
        self.closed = False

    def new_context(self, record_har_path: str) -> _FakeContext:
        self._context.har_path = Path(record_har_path)
        return self._context

    def close(self) -> None:
        self.closed = True


class _FakeChromium:
    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser
        self.launch_args: dict[str, object] = {}

    def launch(self, **kwargs) -> _FakeBrowser:
        self.launch_args = kwargs
        return self._browser


class _FakePlaywright:
    def __init__(self, browser: _FakeBrowser) -> None:
        self.chromium = _FakeChromium(browser)


class _Launcher:
    """模拟 `sync_playwright()`：它返回的是上下文管理器，不是 playwright 对象本身。"""

    def __init__(self, browser: _FakeBrowser) -> None:
        self._browser = browser

    def __enter__(self) -> _FakePlaywright:
        return _FakePlaywright(self._browser)

    def __exit__(self, *exc: object) -> bool:
        return False


def test_playwright_driver_collects_html_screenshot_har_and_console(tmp_path):
    """真 driver 的流程用假 playwright 走一遍：HAR 必须在 context.close() 后读到。"""
    context = _FakeContext(tmp_path / "unused.har")
    browser = _FakeBrowser(context)

    result = PlaywrightDriver(launcher=lambda: _Launcher(browser), timeout_ms=5000).fetch(
        "https://x.test/edit"
    )

    assert result.page_html == HTML
    assert result.screenshot == PNG
    assert result.network_har == '{"log": {"entries": [1]}}'
    assert result.console_log == "[error] boom: null reference"
    assert context.page.goto_args == ("https://x.test/edit", "domcontentloaded")
    assert context.page.timeout == 5000
    assert context.closed and browser.closed, "context / browser 都必须关掉，否则进程不退出"


def test_playwright_missing_gives_actionable_install_hint(monkeypatch):
    """环境缺依赖时，报错必须能照着修，而不是一个 ImportError 抛到用户脸上。"""
    monkeypatch.setitem(sys.modules, "playwright.sync_api", None)

    with pytest.raises(DriverUnavailable) as exc:
        PlaywrightDriver().fetch("https://x.test/")

    message = str(exc.value)
    assert "pip install playwright" in message
    assert "playwright install chromium" in message


def test_default_launcher_returns_the_real_entrypoint(monkeypatch):
    """默认 launcher 必须真的去拿 `sync_playwright`，不能返回个空壳。"""
    import types

    from geopublish.core.snapshot import _default_launcher

    fake_module = types.SimpleNamespace(sync_playwright=lambda: None)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", fake_module)

    assert _default_launcher() is fake_module.sync_playwright
