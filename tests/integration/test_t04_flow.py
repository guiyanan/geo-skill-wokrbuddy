"""T-04 DoD：真失败 → 现场包 → repro.sh 回放，一条链走通。

单元测试证明了「打包逻辑对」，但 DoD 要求的是另一件事：**手动制造一次失败，
产出的包能被回放**。这两件事差着一层：单测里的 Capture 是我自己捏的，
真浏览器给的 html/screenshot 可能带着单测想不到的东西（空 HAR、巨长 DOM、
控制台警告）。所以这里用真 chromium 抓一个本地页面，拿它当失败现场。

跑法：pytest -m integration tests/integration/test_t04_flow.py
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.incident import Incident, archive, load
from geopublish.core.snapshot import PlaywrightDriver, capture

pytestmark = pytest.mark.integration


@pytest.fixture
def local_page(tmp_path) -> str:
    """一个本地页面：不联网，但走真浏览器的完整渲染管线。"""
    page = tmp_path / "editor.html"
    page.write_text(
        "<!doctype html><html><head><title>写文章</title></head>"
        "<body><div id='editor' contenteditable='true'>占位</div></body></html>",
        encoding="utf-8",
    )
    return page.as_uri()


def test_snapshot_real_browser_local_page(local_page, tmp_path):
    target = capture(local_page, "fakesite/editor", tmp_path / "shots",
                     driver=PlaywrightDriver(), clock=FakeClock("2026-10-21T10:00:00+08:00"))

    html = (target / "page.html").read_text(encoding="utf-8")
    assert "id='editor'" in html.replace('"', "'"), "真浏览器应该把 DOM 原样带回来"
    assert (target / "screenshot.png").stat().st_size > 1000, "截图不该是几字节的空白图"
    assert (target / "meta.json").exists()


def test_real_failure_bundle_replays_with_exit_4(local_page, tmp_path):
    """DoD 全链路：抓快照 → 造一次真失败 → 打包 → 跑 repro.sh → 退出码 4。"""
    shot = capture(local_page, "fakesite/editor", tmp_path / "shots",
                   driver=PlaywrightDriver(), clock=FakeClock("2026-10-21T10:00:00+08:00"))

    incident = Incident(
        ticket="T-07",
        stage="inject",
        adapter="fakesite",
        adapter_version="0.1.0",
        selectors_version="2026-10-21.1",
        prd_refs=("R7", "R12"),
        error="InjectFailed: 三级注入（keyboard / execCommand / paste）全部失败",
        payload={"slug": "fakesite-editor", "cookie": "sessionid=should-not-survive"},
        capture=load_capture_from(shot),
    )
    bundle = archive(incident, tmp_path / "incidents", clock=FakeClock("2026-10-21T10:00:12+08:00"))

    assert bundle.name == "20261021_100012_T-07_inject"

    proc = subprocess.run([str(bundle / "repro.sh")], cwd="/", capture_output=True,
                          text=True, timeout=120)
    assert proc.returncode == 4, f"回放没复现出失败：{proc.stdout}\n{proc.stderr}"
    assert "T-07" in proc.stderr and "inject" in proc.stderr
    assert "sessionid" not in (bundle / "input.json").read_text(encoding="utf-8")


def load_capture_from(directory: Path):
    """把刚抓的 fixture 当成失败瞬间的素材（真实场景里两者都来自同一个页面对象）。"""
    from geopublish.core.incident import Capture

    return Capture(
        page_html=(directory / "page.html").read_text(encoding="utf-8"),
        screenshot=(directory / "screenshot.png").read_bytes(),
        network_har='{"log": {"entries": []}}',
        console_log="",
    )
