"""CLI 入口。

当前只有两件事：
- `geopublish --version`：部署脚本与 CI 的探活点
- `geopublish check-config [PATH]`：**发布之前**把配置问题暴露出来

退出码约定（后续 publish 复用）：
  0 成功 / 1 用法错误 / 2 需重新授权 / 3 配置错误
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .core.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_AUTH = 2
EXIT_CONFIG_ERROR = 3


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="geopublish",
        description="GEO 内容自动分发引擎",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"geopublish {__version__}",
    )
    sub = parser.add_subparsers(dest="command")
    check = sub.add_parser("check-config", help="校验配置文件并打印摘要")
    check.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check-config":
        return _check_config(Path(args.path))

    parser.print_help()
    return EXIT_OK


def _check_config(path: Path) -> int:
    try:
        cfg = load_config(path)
    except ConfigError as exc:
        # 定时任务里抛 traceback 没人看得见，一律转成退出码 + 一行原因
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    for line in _summary(cfg, path):
        print(line)
    return EXIT_OK


def _summary(cfg: Config, path: Path) -> list[str]:
    lines = [
        f"配置 OK：{path}",
        f"  gate       = {cfg.gate}",
        f"  platforms  = {len(cfg.platforms)} 个（启用 {len(cfg.enabled_platforms)} 个）",
    ]
    for item in cfg.platforms:
        state = "" if item.enabled else "  [已禁用]"
        lines.append(
            f"    - {item.key}  daily_limit={item.daily_limit}"
            f"  publish_at={item.publish_at.strftime('%H:%M')}"
            f"  words={item.word_min}-{item.word_max}"
            f"  title<={item.title_max}  tags<={item.tags_max}{state}"
        )
    lines += [
        f"  report     = {cfg.report_email}",
        f"  data_dir   = {cfg.data_dir}",
        f"  retry_max  = {cfg.retry_max}",
        f"  refresh    = {cfg.refresh_days} 天",
    ]
    return lines


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
