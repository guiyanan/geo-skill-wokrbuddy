"""CLI 入口。

子命令：
- `geopublish --version`：部署脚本与 CI 的探活点
- `geopublish check-config [PATH]`：**发布之前**把配置问题暴露出来
- `geopublish snapshot <url> <name>`：把页面存成 fixture（T-04）
- `geopublish incident replay <dir>`：回放现场包（T-04，repro.sh 调的就是它）

退出码约定（后续 publish 复用）：
  0 成功 / 1 用法错误 / 2 需重新授权 / 3 配置错误 /
  4 现场已复现（replay 的预期结果）/ 5 环境不可用（缺浏览器驱动、抓不到内容）

「1 用法错误」包括**没给子命令**：cron 里跑 `geopublish` 忘写子命令如果退 0，
监控会当成「今天跑过了」，第二天才发现一篇都没发。argparse 默认对解析失败退 2，
这里统一映射到 1（`_ArgumentParser.error`），子解析器也走同一个类，不漏。

4 和 5 是刻意分开的：repro.sh 拿 4 判断「复现成功」，而 5 表示这台机器压根
没装好浏览器 —— 那时候「复现失败」是环境问题，不能当成「这个 bug 不复现」。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .core.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from .core.incident import BundleNotFound, IncidentError, describe_failure, load
from .core.snapshot import (
    DriverUnavailable,
    EmptyCapture,
    InvalidName,
    PlaywrightDriver,
    SnapshotError,
    capture,
)

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_AUTH = 2
EXIT_CONFIG_ERROR = 3
EXIT_REPRODUCED = 4
EXIT_ENV = 5

DEFAULT_SNAPSHOT_DIR = Path("data/snapshots")


class _ArgumentParser(argparse.ArgumentParser):
    """把 argparse 默认的「解析失败退出 2」归到约定里的 EXIT_USAGE（1）。

    退出码是给 cron 和 CI 看的：它们只判断「0 还是非 0」，所以只要非 0 都算拦住了。
    但对人来说 1 和 2 得有稳定含义，否则写监控规则时要翻代码。子解析器也必须继承
    同一个行为（`add_subparsers(parser_class=...)`），不然子命令拼错会漏回 2。
    """

    def error(self, message: str) -> None:  # type: ignore[override]
        self.print_usage(sys.stderr)
        print(f"{self.prog}: 用法错误：{message}", file=sys.stderr)
        raise SystemExit(EXIT_USAGE)


def build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(
        prog="geopublish",
        description="GEO 内容自动分发引擎",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"geopublish {__version__}",
    )
    sub = parser.add_subparsers(dest="command", parser_class=_ArgumentParser)
    check = sub.add_parser("check-config", help="校验配置文件并打印摘要")
    check.add_argument(
        "path",
        nargs="?",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )

    shot = sub.add_parser("snapshot", help="把一个页面存成 fixture（DOM/截图/元信息）")
    shot.add_argument("url", help="页面地址（http/https/file）")
    shot.add_argument("name", help="fixture 名字，如 csdn/editor")
    shot.add_argument(
        "--out",
        default=str(DEFAULT_SNAPSHOT_DIR),
        help=f"输出根目录（默认 {DEFAULT_SNAPSHOT_DIR}；要入库的 fixture 传 fixtures）",
    )

    incident = sub.add_parser("incident", help="现场包操作")
    incident_sub = incident.add_subparsers(dest="action", parser_class=_ArgumentParser, required=True)
    replay_cmd = incident_sub.add_parser("replay", help="回放一个现场包（预期退出 4）")
    replay_cmd.add_argument("path", help="现场包目录")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check-config":
        return _check_config(Path(args.path))
    if args.command == "snapshot":
        return _snapshot(args)
    if args.command == "incident":
        return _incident(args)

    # 没给子命令不是「打印帮助然后成功」，是用法错了。
    # 退 0 会让 cron 以为今天跑过了 —— 静默成功比报错危险得多。
    parser.print_help(sys.stderr)
    return EXIT_USAGE


def _snapshot(args: argparse.Namespace) -> int:
    """抓快照。四种失败各有归宿：名字错是用法，驱动缺失/页面空是环境。"""
    try:
        target = capture(args.url, args.name, Path(args.out), driver=PlaywrightDriver())
    except InvalidName as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except DriverUnavailable as exc:
        print(f"环境不可用：{exc}", file=sys.stderr)
        return EXIT_ENV
    except EmptyCapture as exc:
        print(f"抓取失败：{exc}", file=sys.stderr)
        return EXIT_ENV
    except SnapshotError as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    print(f"已保存快照：{target}")
    return EXIT_OK


def _incident(args: argparse.Namespace) -> int:
    """回放现场包。**退出 4 = 复现成功**，repro.sh 靠这个判断。"""
    try:
        incident = load(Path(args.path))
    except BundleNotFound as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    except IncidentError as exc:
        print(f"现场包不可用：{exc}", file=sys.stderr)
        return EXIT_ENV

    print(describe_failure(incident), file=sys.stderr)
    return EXIT_REPRODUCED


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
