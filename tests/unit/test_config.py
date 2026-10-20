"""T-01b · 配置加载与校验（UT-01-01 / UT-01-02）。

配置是把「L1 可变项」从代码里赶出去的唯一出口，所以它的报错质量等于运维体验：
缺哪个字段、哪个值越界、哪个键拼错，必须一次说清，而不是抛个 KeyError 了事。
"""

import json
import time
from pathlib import Path

import pytest

from geopublish.core.config import (
    MAX_RETRY,
    Config,
    ConfigError,
    Gate,
    PlatformConfig,
    load_config,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


def payload(**over):
    data = {
        "gate": "draft",
        "platforms": [{"name": "csdn"}],
        "report": {"email": "shiyimi@joto.example"},
    }
    data.update(over)
    return data


def platform(**over):
    data = {"name": "csdn"}
    data.update(over)
    return data


# ---------------------------------------------------------------- UT-01-01
def test_ut_01_01_missing_field_names_the_field():
    data = payload()
    del data["gate"]

    with pytest.raises(ConfigError) as exc:
        Config.from_dict(data)

    assert "gate" in str(exc.value)


def test_ut_01_01_missing_platform_and_email_reported_together():
    with pytest.raises(ConfigError) as exc:
        Config.from_dict({})

    msg = str(exc.value)
    assert "platforms" in msg and "report.email" in msg, "一次报全，别让人试三次"


def test_ut_01_01_unknown_key_is_rejected():
    """拼错的键必须报错：静默忽略等于配置写了个寂寞。"""
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(payload(gates="draft"))

    assert "gates" in str(exc.value)


def test_ut_01_01_unknown_platform_key_is_rejected():
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(payload(platforms=[platform(daily_limits=1)]))

    assert "daily_limits" in str(exc.value)


# ---------------------------------------------------------------- UT-01-02
@pytest.mark.parametrize("bad", [0, -1, -100])
def test_ut_01_02_daily_limit_out_of_range(bad):
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(payload(platforms=[platform(daily_limit=bad)]))

    assert "daily_limit" in str(exc.value)


@pytest.mark.parametrize("bad", [-1, MAX_RETRY + 1])
def test_ut_01_02_retry_max_out_of_range(bad):
    """重试上限是全局硬规则（同篇 ≤2 次），配置不许放宽。"""
    with pytest.raises(ConfigError) as exc:
        Config.from_dict(payload(retry_max=bad))

    assert "retry_max" in str(exc.value)


@pytest.mark.parametrize(
    "over",
    [
        {"gate": "ship"},
        {"refresh_days": 0},
        {"report": {"email": "not-an-email"}},
        {"report": {"email": ""}},
        {"platforms": []},
        {"platforms": [platform(), platform()]},
        {"platforms": [platform(word_min=5000, word_max=3000)]},
        {"platforms": [platform(publish_at="25:00")]},
        {"platforms": [platform(publish_at="十点")]},
        {"platforms": [platform(title_max=0)]},
        {"platforms": [platform(tags_max=-1)]},
        {"data_dir": ""},
    ],
    ids=lambda o: "-".join(f"{k}={v}" for k, v in o.items()),
)
def test_invalid_values_are_rejected(over):
    with pytest.raises(ConfigError):
        Config.from_dict(payload(**over))


# ---------------------------------------------------------------- 正常路径
def test_minimal_config_applies_defaults():
    cfg = Config.from_dict(payload())

    assert cfg.gate is Gate.DRAFT
    assert cfg.retry_max == MAX_RETRY
    assert cfg.refresh_days == 30
    csdn = cfg.platform("csdn")
    assert csdn.daily_limit == 1
    assert csdn.publish_at == time(10, 0)
    assert (csdn.word_min, csdn.word_max) == (3000, 5000)


def test_platform_lookup_is_case_insensitive():
    with pytest.raises(KeyError):
        Config.from_dict(payload()).platform("juejin")

    assert Config.from_dict(payload(platforms=[platform(name="CSDN")])).platform("csdn")


def test_stagger_schedule_is_per_platform():
    cfg = Config.from_dict(
        payload(
            platforms=[
                platform(name="csdn", publish_at="10:00"),
                platform(name="juejin", publish_at="15:30"),
            ]
        )
    )

    assert cfg.platform("csdn").publish_at == time(10, 0)
    assert cfg.platform("juejin").publish_at == time(15, 30)


def test_gate_accepts_publish_mode():
    assert Config.from_dict(payload(gate="publish")).gate is Gate.PUBLISH


def test_load_config_reads_json(tmp_path):
    path = tmp_path / "geopublish.json"
    path.write_text(json.dumps(payload()), encoding="utf-8")

    assert load_config(path).platform("csdn").name == "CSDN"


def test_load_config_missing_file(tmp_path):
    with pytest.raises(ConfigError) as exc:
        load_config(tmp_path / "nope.json")

    assert "不存在" in str(exc.value)


def test_load_config_broken_json(tmp_path):
    path = tmp_path / "geopublish.json"
    path.write_text("{gate: draft}", encoding="utf-8")

    with pytest.raises(ConfigError) as exc:
        load_config(path)

    assert "JSON" in str(exc.value)


def test_shipped_example_config_is_valid():
    """发给用户的示例配置必须自身合法，否则第一天就踩坑。"""
    example = REPO_ROOT / "config" / "geopublish.example.json"

    cfg = load_config(example)

    assert cfg.platforms, "示例里至少要有一个平台"
    assert cfg.report_email


def test_platform_config_is_frozen():
    cfg = Config.from_dict(payload())

    with pytest.raises(Exception):
        cfg.platforms[0].daily_limit = 9  # type: ignore[misc]
