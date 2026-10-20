"""快照：把线上页面存成 fixture（`geopublish snapshot <url> <name>`）。

T-05 的契约测试全部建立在 DOM 快照上，没有浏览器、没有网络。所以这里的产出
必须是**能长期存在仓库里**的东西，而不是一次性的调试输出：

- `page.html`   —— 契约测试唯一依赖的文件
- `screenshot.png` —— 人看「这个页面长什么样」，选择器改坏时对照用
- `meta.json`   —— URL（脱敏后）、抓取时间、html 的 sha256

`html_sha256` 是刻意加的：平台改版时 fixture 会悄悄变旧，契约测试仍然绿，
但线上发布挂了。有了 hash，`geopublish snapshot` 重抓一次就能 diff 出「变了没有」。

浏览器入口做成可注入的 `Driver`：单测用假 driver（毫秒级、无依赖），
真 chromium 只在 integration 里验一次接线。
"""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import urlsplit

from .clock import Clock, SystemClock
from .fs import staged_dir
from .incident import Capture, scrub_text

__all__ = [
    "DriverUnavailable",
    "Driver",
    "EmptyCapture",
    "InvalidName",
    "PAGE_FILES",
    "PlaywrightDriver",
    "SnapshotError",
    "capture",
]

PAGE_FILES: tuple[str, ...] = ("page.html", "screenshot.png", "meta.json")

_NAME_SEGMENT_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_ALLOWED_SCHEMES = frozenset({"http", "https", "file"})


class SnapshotError(ValueError):
    """快照失败。"""


class InvalidName(SnapshotError):
    """fixture 名字不合法 —— 属于用法错误，不是环境问题。"""


class DriverUnavailable(SnapshotError):
    """浏览器驱动装不上/起不来。"""


class EmptyCapture(SnapshotError):
    """页面抓回来了，但是空的 —— 比抓不到更危险，会污染 fixture。"""


class Driver(Protocol):
    name: str

    def fetch(self, url: str) -> Capture:  # pragma: no cover - Protocol 只声明
        ...


def _require_url(url: str) -> None:
    if not isinstance(url, str) or not url.strip():
        raise SnapshotError("url 不能为空")
    scheme = urlsplit(url).scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        raise SnapshotError(f"只支持 {sorted(_ALLOWED_SCHEMES)} 开头的地址，收到：{url!r}")


def _target_dir(out_dir: Path, name: str) -> Path:
    """校验 fixture 名字并拼出目标目录。

    name 会被直接拼成路径，而且常常是从命令行粘进来的。`..`、绝对路径、反斜杠
    一个都不能放过 —— 写错一次就是往仓库外面写文件。
    """
    if not isinstance(name, str) or not name.strip():
        raise InvalidName("fixture 名字（name）不能为空")
    segments = name.strip().split("/")
    for segment in segments:
        if not _NAME_SEGMENT_RE.match(segment):
            raise InvalidName(
                f"fixture 名字（name）不合法：{name!r}。"
                "只允许小写字母/数字/点/下划线/连字符，用 / 分目录（如 csdn/editor）"
            )
    return out_dir.joinpath(*segments)


def capture(
    url: str,
    name: str,
    out_dir: str | Path,
    *,
    driver: Driver | None = None,
    clock: Clock | None = None,
) -> Path:
    """抓一个页面存成 fixture，返回目标目录。

    校验顺序是刻意的：先校 name（不合法就不该去开浏览器），再抓，再检查内容非空，
    最后才落盘。顺序反了就会出现「浏览器白跑一趟」「留下一个空 html」这类事。
    """
    _require_url(url)
    out_root = Path(out_dir)
    if out_root.exists() and not out_root.is_dir():
        raise SnapshotError(f"输出目录不是目录：{out_root}")
    target = _target_dir(out_root, name)
    active = driver if driver is not None else PlaywrightDriver()
    shot = active.fetch(url)

    if not shot.page_html.strip():
        raise EmptyCapture(f"抓回来的 page.html 是空的：{url}（登录墙？跳转失败？）")
    if not shot.screenshot:
        raise EmptyCapture(f"没拿到 screenshot.png：{url}")

    now = (clock or SystemClock()).now()
    html_bytes = shot.page_html.encode("utf-8")
    meta: dict[str, Any] = {
        "url": scrub_text(url),
        "name": name.strip(),
        "captured_at": now.isoformat(),
        "driver": getattr(active, "name", "unknown"),
        "html_sha256": hashlib.sha256(html_bytes).hexdigest(),
        "html_bytes": len(html_bytes),
        "screenshot_bytes": len(shot.screenshot),
    }

    with staged_dir(target) as staging:
        (staging / "page.html").write_text(shot.page_html, encoding="utf-8")
        (staging / "screenshot.png").write_bytes(shot.screenshot)
        (staging / "meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return target


def _default_launcher():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise DriverUnavailable(
            "没装 playwright，快照功能不可用。修复：\n"
            "  pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


def _format_console(message: Any) -> str:
    return f"[{getattr(message, 'type', 'log')}] {getattr(message, 'text', '')}"


class PlaywrightDriver:
    """真浏览器。`launcher` 可注入，单测用它把整条流程走一遍（不用下载浏览器）。"""

    name = "playwright"

    def __init__(self, *, launcher=None, timeout_ms: int = 30_000) -> None:
        self._launcher = launcher
        self._timeout_ms = timeout_ms

    def fetch(self, url: str) -> Capture:
        launcher = self._launcher or _default_launcher()
        console: list[str] = []
        with tempfile.TemporaryDirectory(prefix="geopublish-har-") as tmpdir:
            har_path = Path(tmpdir) / "network.har"
            with launcher() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(record_har_path=str(har_path))
                try:
                    page = context.new_page()
                    page.set_default_timeout(self._timeout_ms)
                    page.on("console", lambda msg: console.append(_format_console(msg)))
                    page.goto(url, wait_until="domcontentloaded")
                    html = page.content()
                    screenshot = page.screenshot()
                finally:
                    # HAR 是在 context.close() 时才刷到磁盘的，先关再读
                    context.close()
                    browser.close()
            har = har_path.read_text(encoding="utf-8") if har_path.exists() else ""

        return Capture(
            page_html=html,
            screenshot=screenshot,
            network_har=har,
            console_log="\n".join(console),
        )
