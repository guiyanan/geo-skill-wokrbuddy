"""pytest 公共夹具。

确定性三原则在这里落地：时间可注入、随机可固定、网络可拦截。
"""

import json
import random
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = REPO_ROOT / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def fixture_dom(fixtures_dir):
    """加载 DOM 快照：fixture_dom("csdn", "editor")"""

    def _load(platform: str, name: str) -> str:
        p = fixtures_dir / platform / f"{name}.html"
        if not p.exists():
            raise FileNotFoundError(f"缺少 DOM 快照：{p}")
        return p.read_text(encoding="utf-8")

    return _load


@pytest.fixture
def fixture_json(fixtures_dir):
    """加载 JSON 夹具。"""

    def _load(platform: str, name: str):
        p = fixtures_dir / platform / f"{name}.json"
        if not p.exists():
            raise FileNotFoundError(f"缺少 JSON 夹具：{p}")
        return json.loads(p.read_text(encoding="utf-8"))

    return _load


@pytest.fixture
def clock():
    """固定时钟，禁止测试依赖系统时间。"""
    from geopublish.core.clock import FakeClock

    return FakeClock("2026-10-21T10:00:00+08:00")


@pytest.fixture
def seed_random(monkeypatch):
    """固定随机种子，让抖动可复现。"""
    rnd = random.Random(20261021)
    monkeypatch.setattr(random, "random", rnd.random)
    monkeypatch.setattr(random, "uniform", rnd.uniform)
    return rnd


@pytest.fixture
def data_dir(tmp_path):
    """隔离的 data 目录，测试不碰真实数据。"""
    for sub in ("products", "drafts", "runs", "incidents", "reports"):
        (tmp_path / sub).mkdir(parents=True, exist_ok=True)
    return tmp_path
