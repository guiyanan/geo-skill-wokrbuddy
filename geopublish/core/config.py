"""配置：L1 可变项的唯一出口（PRD §9.1）。

原则：**能改的放配置，不能改的写死代码**。
- 放这里的：闸门模式、平台白名单、单日上限、错峰时间、字数/标题/标签上限、
  重试上限、翻新周期、输出目录、日报收件人。
- 不放这里的：R4 自检、R13 风控 —— 这两处不设「跳过」开关（PRD §9.5 最后一条）。

所有校验失败一律抛 `ConfigError`，且信息里必须带上字段名：
运维看到一个 KeyError 是没用的，要知道改哪一行。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, fields
from datetime import time
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "DEFAULT_CONFIG_PATH",
    "MAX_RETRY",
    "Config",
    "ConfigError",
    "Gate",
    "PlatformConfig",
    "load_config",
]

DEFAULT_CONFIG_PATH = Path("config/geopublish.json")

# 全局硬上限：同一篇一次运行最多重试 2 次（架构 §3.6），配置只能往小改
MAX_RETRY = 2

_TOP_LEVEL_KEYS = {"gate", "platforms", "report", "data_dir", "retry_max", "refresh_days"}


class ConfigError(ValueError):
    """配置缺失、拼错、越界。"""


class Gate(str, Enum):
    DRAFT = "draft"
    PUBLISH = "publish"
    DRY_RUN = "dry_run"

    def __str__(self) -> str:
        return self.value


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{field} 不能为空")
    return value.strip()


def _int(value: Any, field: str, *, minimum: int, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{field} 必须是整数，收到 {value!r}")
    if value < minimum:
        raise ConfigError(f"{field} 不能小于 {minimum}，收到 {value}")
    if maximum is not None and value > maximum:
        raise ConfigError(f"{field} 不能大于 {maximum}，收到 {value}")
    return value


def _parse_clock(value: Any, field: str) -> time:
    if isinstance(value, time):
        return value
    try:
        return time.fromisoformat(_text(value, field))
    except ValueError as exc:
        raise ConfigError(f"{field} 必须是 HH:MM（如 10:00），收到 {value!r}") from exc


@dataclass(frozen=True, slots=True)
class PlatformConfig:
    name: str
    enabled: bool = True
    # R14 节奏控制：同一平台 24 小时内最多发几篇
    daily_limit: int = 1
    # R16 错峰：CSDN 10:00 / 掘金 15:00 这类排期
    publish_at: time = time(10, 0)
    # R3 成稿字数区间（GEO 文章的切片友好长度）
    word_min: int = 3000
    word_max: int = 5000
    # R4 自检的平台侧上限
    title_max: int = 100
    tags_max: int = 5

    def __post_init__(self) -> None:
        _text(self.name, "platforms[].name")
        _int(self.daily_limit, "daily_limit", minimum=1)
        _int(self.word_min, "word_min", minimum=1)
        _int(self.word_max, "word_max", minimum=1)
        _int(self.title_max, "title_max", minimum=1)
        _int(self.tags_max, "tags_max", minimum=0)
        if self.word_max <= self.word_min:
            raise ConfigError(
                f"word_max（{self.word_max}）必须大于 word_min（{self.word_min}）"
            )
        if not isinstance(self.publish_at, time):
            raise ConfigError(f"publish_at 必须是 time，收到 {self.publish_at!r}")
        if not isinstance(self.enabled, bool):
            raise ConfigError(f"enabled 必须是 true/false，收到 {self.enabled!r}")

    @property
    def key(self) -> str:
        """用于查找与去重：平台名大小写不敏感。"""
        return self.name.strip().lower()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], where: str) -> PlatformConfig:
        data = _strip_comments(data)
        known = {f.name for f in fields(cls)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ConfigError(f"{where} 未知字段：{', '.join(unknown)}")

        if "name" not in data:
            raise ConfigError(f"{where}.name 不能为空")

        kwargs = dict(data)
        if "publish_at" in kwargs:
            kwargs["publish_at"] = _parse_clock(kwargs["publish_at"], "publish_at")
        return cls(**kwargs)


@dataclass(frozen=True, slots=True)
class Config:
    gate: Gate
    platforms: tuple[PlatformConfig, ...]
    report_email: str
    data_dir: Path = Path("data")
    retry_max: int = MAX_RETRY
    refresh_days: int = 30

    def __post_init__(self) -> None:
        if not isinstance(self.gate, Gate):
            raise ConfigError(f"gate 必须是 {[g.value for g in Gate]}，收到 {self.gate!r}")
        if not self.platforms:
            raise ConfigError("platforms 不能为空：至少要有一个平台")

        keys = [p.key for p in self.platforms]
        dupes = sorted({k for k in keys if keys.count(k) > 1})
        if dupes:
            raise ConfigError(f"platforms 出现重复平台：{', '.join(dupes)}")

        email = _text(self.report_email, "report.email")
        local, _, domain = email.partition("@")
        if not local or not domain or " " in email:
            raise ConfigError(f"report.email 不是合法邮箱：{email!r}")

        _text(str(self.data_dir), "data_dir")
        _int(self.retry_max, "retry_max", minimum=0, maximum=MAX_RETRY)
        _int(self.refresh_days, "refresh_days", minimum=1)

    def platform(self, name: str) -> PlatformConfig:
        wanted = str(name).strip().lower()
        for item in self.platforms:
            if item.key == wanted:
                return item
        raise KeyError(f"配置里没有平台：{name}")

    @property
    def enabled_platforms(self) -> tuple[PlatformConfig, ...]:
        return tuple(p for p in self.platforms if p.enabled)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Config:
        if not isinstance(data, Mapping):
            raise ConfigError(f"配置必须是 JSON 对象，收到 {type(data).__name__}")
        # JSON 不能写注释，约定 _ 开头的键当注释用（示例文件里就有）
        data = _strip_comments(data)

        missing: list[str] = []
        if "gate" not in data:
            missing.append("gate")
        if "platforms" not in data:
            missing.append("platforms")

        report = data.get("report")
        if report is None:
            missing.append("report.email")
        elif not isinstance(report, Mapping):
            raise ConfigError("report 必须是对象，如 {\"email\": \"...\"}")
        elif not str(report.get("email") or "").strip():
            missing.append("report.email")

        if missing:
            raise ConfigError(f"缺少必填配置项：{', '.join(missing)}")

        unknown = sorted(set(data) - _TOP_LEVEL_KEYS)
        if unknown:
            raise ConfigError(f"未知配置项：{', '.join(unknown)}")

        raw_platforms = data["platforms"]
        if not isinstance(raw_platforms, list):
            raise ConfigError(f"platforms 必须是数组，收到 {type(raw_platforms).__name__}")
        platforms = tuple(
            PlatformConfig.from_dict(item, f"platforms[{i}]")
            if isinstance(item, Mapping)
            else _not_a_mapping(i)
            for i, item in enumerate(raw_platforms)
        )

        gate_raw = _text(data["gate"], "gate")
        try:
            gate = Gate(gate_raw.lower())
        except ValueError as exc:
            raise ConfigError(
                f"gate 只能是 {' / '.join(g.value for g in Gate)}，收到 {gate_raw!r}"
            ) from exc

        return cls(
            gate=gate,
            platforms=platforms,
            report_email=report.get("email", ""),
            data_dir=Path(_text(str(data.get("data_dir", "data")), "data_dir")),
            retry_max=data.get("retry_max", MAX_RETRY),
            refresh_days=data.get("refresh_days", 30),
        )


def _not_a_mapping(index: int) -> PlatformConfig:
    raise ConfigError(f"platforms[{index}] 必须是对象，如 {{\"name\": \"csdn\"}}")


def _strip_comments(data: Mapping[str, Any]) -> dict[str, Any]:
    """JSON 不支持注释，约定 `_` 开头的键不参与校验（纯给人看）。"""
    return {k: v for k, v in data.items() if not str(k).startswith("_")}


def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> Config:
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"配置文件不存在：{p}")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ConfigError(f"配置文件不是合法 JSON：{p}（第 {exc.lineno} 行：{exc.msg}）") from exc
    return Config.from_dict(data)
