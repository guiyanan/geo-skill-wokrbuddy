"""CLI 入口。

子命令：
- `geopublish --version`：部署脚本与 CI 的探活点
- `geopublish check-config [PATH]`：**发布之前**把配置问题暴露出来
- `geopublish login <platform>`：扫码登录，登录态写进持久 profile（T-06）
- `geopublish snapshot <url> <name>`：把页面存成 fixture（T-04）
- `geopublish incident replay <dir>`：回放现场包（T-04，repro.sh 调的就是它）
- `geopublish publish <draft_dir> --mode draft`：把稿件投进草稿箱（T-09）
- `geopublish ingest <资料>`：产品资料入库，产出 facts.json + REVIEW.md（T-13）
- `geopublish write <product_id> --query <QID>`：成稿五步，过 R4 自检才落盘（T-14）

退出码约定：
  0 成功（含 SKIPPED_DUPLICATE：已发过，幂等跳过）/ 1 用法错误 / 2 需重新授权 /
  3 配置错误 / 并发锁冲突（另一进程在跑，等它完）/
  4 现场已复现（replay 的预期结果）/ 5 环境不可用（缺浏览器驱动、抓不到内容）/
  6 发布未确认成功（verify 没过、提交前复核被拦、上次 submitting 未决 → needs_human）

「1 用法错误」包括**没给子命令**：cron 里跑 `geopublish` 忘写子命令如果退 0，
监控会当成「今天跑过了」，第二天才发现一篇都没发。argparse 默认对解析失败退 2，
这里统一映射到 1（`_ArgumentParser.error`），子解析器也走同一个类，不漏。

4 和 5 是刻意分开的：repro.sh 拿 4 判断「复现成功」，而 5 表示这台机器压根
没装好浏览器 —— 那时候「复现失败」是环境问题，不能当成「这个 bug 不复现」。

6 与 5 也是刻意分开的：5 是「这台机器不行，修机器」，6 是「这次投递没被确认，
去平台后台看一眼」。混在一起的话，cron 的告警会指向错误的修复动作。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from . import __version__
from .adapters.base import ContentRejected, CoverError, InjectFailed, MetaError, SubmitBlocked
from .adapters.csdn.flow import EditorReadError
from .core.config import DEFAULT_CONFIG_PATH, Config, ConfigError, load_config
from .core.incident import BundleNotFound, IncidentError, describe_failure, load
from .core.lock import DEFAULT_LOCK_ROOT, LockError
from .core.session import (
    DEFAULT_PROFILE_ROOT,
    AuthRequired,
    BrowserUnavailable,
    PersistentBrowser,
    SessionConfig,
    SessionError,
    login,
    profile_path,
)
from .core.snapshot import (
    DriverUnavailable,
    EmptyCapture,
    InvalidName,
    PlaywrightDriver,
    SnapshotError,
    capture,
)
from .publish import DraftLoadError, GateClosed, load_draft, publish_draft
from .write.ingest import IngestError, ingest
from .write.pipeline import DraftRejected, PipelineError, load_query_config, write_article

EXIT_OK = 0
EXIT_USAGE = 1
EXIT_AUTH = 2
EXIT_CONFIG_ERROR = 3
EXIT_REPRODUCED = 4
EXIT_ENV = 5
EXIT_FAILED = 6

DEFAULT_SNAPSHOT_DIR = Path("data/snapshots")

# 问句口径与 geo-monitor 同源（PRD R17：不另建问句表）。默认指向本机
# geo-monitor 技能的配置；不在时要靠 --queries 显式给，不静默猜。
DEFAULT_QUERIES_PATH = Path.home() / ".workbuddy" / "skills" / "geo-monitor" / "config" / "joto.json"


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

    login_cmd = sub.add_parser("login", help="扫码登录，把登录态存进持久 profile")
    login_cmd.add_argument("platform", help="平台名，如 csdn")
    login_cmd.add_argument(
        "--channel",
        default="chrome",
        help="浏览器来源：chrome=复用系统 Chrome（默认）；bundled=用 playwright 自带 chromium（CI/无 Chrome 时）",
    )
    login_cmd.add_argument(
        "--profile-root",
        default=None,
        help=f"profile 根目录（默认 {DEFAULT_PROFILE_ROOT}）",
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

    publish_cmd = sub.add_parser("publish", help="把一篇稿件投到平台（draft=存草稿；publish=真实发布）")
    publish_cmd.add_argument(
        "draft_dir", help="稿件目录（含 meta.json，正文为 <platform>.md / body.md / draft.md）"
    )
    publish_cmd.add_argument(
        "--mode",
        default="draft",
        choices=("draft", "publish"),
        help="draft=只存草稿（默认）；publish=真实发布并落回执（需配置 gate=publish）",
    )
    publish_cmd.add_argument("--platform", default="csdn", help="平台名（默认 csdn）")
    publish_cmd.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}）",
    )
    publish_cmd.add_argument(
        "--channel",
        default="chrome",
        help="浏览器来源：chrome=复用系统 Chrome（默认）；bundled=用 playwright 自带 chromium",
    )
    publish_cmd.add_argument(
        "--profile-root",
        default=None,
        help=f"profile 根目录（默认 {DEFAULT_PROFILE_ROOT}）",
    )
    publish_cmd.add_argument(
        "--lock-root",
        default=None,
        help=f"平台锁目录（默认 {DEFAULT_LOCK_ROOT}；T-11 并发锁，一般不用动）",
    )

    ingest_cmd = sub.add_parser("ingest", help="把产品资料入库到产品库（T-13，facts.json + REVIEW.md）")
    ingest_cmd.add_argument(
        "source",
        help="资料文件（.md/.txt/.pptx；条目式行文，页码用 <!-- page N --> 标记）",
    )
    ingest_cmd.add_argument(
        "--product-id",
        default=None,
        help="产品 id（小写字母/数字/连字符；缺省从文件名推导，推导不出必须显式给）",
    )
    ingest_cmd.add_argument(
        "--data-dir",
        default="data",
        help="数据根目录（默认 data；产品库落在 <data-dir>/products/<id>/）",
    )

    write_cmd = sub.add_parser("write", help="从产品库生成一篇稿件（T-14 成稿五步，过 R4 自检才落盘）")
    write_cmd.add_argument("product_id", help="产品库 id（data/products/<id>/，须已回填 REVIEW.md）")
    write_cmd.add_argument(
        "--query",
        required=True,
        help="锚定问句 id（C/D/S 类需求侧问句；B/P 类品牌词不做，SOV 已 83% 无增量）",
    )
    write_cmd.add_argument(
        "--queries",
        default=str(DEFAULT_QUERIES_PATH),
        help=f"问句配置路径（默认 {DEFAULT_QUERIES_PATH}，与 geo-monitor 同源）",
    )
    write_cmd.add_argument(
        "--data-dir",
        default="data",
        help="数据根目录（默认 data；稿件落在 <data-dir>/drafts/<slug>/）",
    )
    write_cmd.add_argument(
        "--config",
        default=str(DEFAULT_CONFIG_PATH),
        help=f"配置文件路径（默认 {DEFAULT_CONFIG_PATH}，取平台字数/标题/标签上限）",
    )
    write_cmd.add_argument("--platform", default="csdn", help="目标平台（默认 csdn）")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command == "check-config":
        return _check_config(Path(args.path))
    if args.command == "login":
        return _login(args)
    if args.command == "snapshot":
        return _snapshot(args)
    if args.command == "incident":
        return _incident(args)
    if args.command == "publish":
        return _publish(args)
    if args.command == "ingest":
        return _ingest(args)
    if args.command == "write":
        return _write(args)

    # 没给子命令不是「打印帮助然后成功」，是用法错了。
    # 退 0 会让 cron 以为今天跑过了 —— 静默成功比报错危险得多。
    parser.print_help(sys.stderr)
    return EXIT_USAGE


def _login(args: argparse.Namespace) -> int:
    """扫码登录。退出码就是 R6 的验收：没登上退 2，环境坏退 5。

    注意这里**不重试**：重试逻辑一旦写进来，扫码失败就会变成反复撞登录页。
    """
    try:
        adapter = _adapter_for(args.platform)
        channel = None if str(args.channel).strip().lower() in {"bundled", "none", ""} else args.channel
        config = SessionConfig(
            profile_root=Path(args.profile_root) if args.profile_root else DEFAULT_PROFILE_ROOT,
            channel=channel,
        )
        result = login(adapter, config)
    except AuthRequired as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AUTH
    except BrowserUnavailable as exc:
        print(f"环境不可用：{exc}", file=sys.stderr)
        return EXIT_ENV
    except SessionError as exc:
        print(f"登录失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    where = profile_path(adapter.name, profile_root=config.profile_root)
    print(f"登录成功：{adapter.name}（{result.reason}）")
    print(f"登录态已保存：{where}（权限 700，不进 git）")
    return EXIT_OK


def _adapter_for(platform: str) -> object:
    """平台名 → adapter。目前只有 csdn；新增平台在这里登记一行。"""
    from .adapters.csdn.flow import CsdnAdapter

    registry = {"csdn": CsdnAdapter}
    key = str(platform).strip().lower()
    if key not in registry:
        raise SessionError(f"未知平台：{platform}（可用：{', '.join(sorted(registry))}）")
    return registry[key]()


def _publish(args: argparse.Namespace) -> int:
    """一条命令投递稿件：draft 进草稿箱（T-09）；publish 真实发布 + 回执（T-10）。

    失败全部转成退出码，不给 cron 丢 traceback。这里**没有重试**：
    提交前复核被拦、verify 没过，重试只会多留一篇（幂等是 T-11 的事）。
    publish 模式还有配置闸门：gate 不是 publish 会在 check_gate 被拦（GateClosed）。
    """
    try:
        cfg = load_config(Path(args.config))
        platform_cfg = cfg.platform(args.platform)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except KeyError as exc:
        # cfg.platform 用 KeyError 表达「配置里没这个平台」：是配置问题，不是用法问题
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        adapter = _adapter_for(args.platform)
        loaded = load_draft(Path(args.draft_dir), platform=platform_cfg.key)
    except (DraftLoadError, SessionError) as exc:
        print(f"用法错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    channel = None if str(args.channel).strip().lower() in {"bundled", "none", ""} else args.channel
    browser = PersistentBrowser(
        SessionConfig(
            profile_root=Path(args.profile_root) if args.profile_root else DEFAULT_PROFILE_ROOT,
            channel=channel,
        )
    )

    try:
        outcome = publish_draft(
            adapter,
            loaded,
            platform_cfg,
            gate=cfg.gate,
            open_page=lambda: browser.open(adapter.name),
            mode=args.mode,
            data_dir=cfg.data_dir,
            screenshot=lambda page, path: page.screenshot(path=str(path)),
            # T-11：CLI 永远带锁跑（UT-11-02：并发第二个进程退 3，不开浏览器）
            lock_root=Path(args.lock_root) if args.lock_root else DEFAULT_LOCK_ROOT,
            # T-12：网络瞬时故障的运行内重试预算（全局硬顶 RETRY_HARD_CAP，配置只能往小改）
            retry_max=cfg.retry_max,
        )
    except LockError as exc:
        # 与配置错误共用 3：都是「这次运行根本没开始」，cron 语义一致
        print(f"并发冲突：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except AuthRequired as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_AUTH
    except BrowserUnavailable as exc:
        print(f"环境不可用：{exc}", file=sys.stderr)
        return EXIT_ENV
    except GateClosed as exc:
        print(f"闸门关闭：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR
    except (InjectFailed, MetaError, CoverError, SubmitBlocked, EditorReadError, ContentRejected) as exc:
        # publish 模式的失败已由 core/errors.py 分类处置（T-12），不会走到这里；
        # 这里只剩 draft 模式（不写日志，异常原样上来）和极少见的逃逸路径。
        print(f"发布失败：{exc}", file=sys.stderr)
        return EXIT_FAILED

    if outcome.skipped_duplicate:
        # R9 验收原话：第二次输出 SKIPPED_DUPLICATE。退 0 —— 这不是失败，
        # 是幂等命中；cron 不该为一个「已经发过了」告警。
        print(f"SKIPPED_DUPLICATE：{loaded.draft.meta.title}")
        print(f"  平台     = {platform_cfg.key}（该幂等键已 published，本次未发）")
        if outcome.url:
            print(f"  url      = {outcome.url}")
        return EXIT_OK

    if not outcome.ok:
        # T-12：分类失败带上类别标签（人机校验 / 平台拒稿 / 网络超时），
        # 人与 cron 都能一眼看出该扫码、改稿还是等网络。
        kind = f"[{outcome.failure_kind}] " if outcome.failure_kind else ""
        print(f"未确认成功：{kind}{outcome.reason}", file=sys.stderr)
        if outcome.url:
            print(f"  当前页面：{outcome.url}", file=sys.stderr)
        return EXIT_FAILED

    if args.mode == "publish":
        print(f"已发布：{loaded.draft.meta.title}")
        print(f"  article_id = {outcome.article_id}")
        print(f"  url        = {outcome.url}")
        if outcome.receipt_path:
            print(f"  回执       = {outcome.receipt_path}")
        if outcome.receipt_failed:
            # 发成了但没存上证：单独喊出来，后续靠这条线索去平台后台核对
            print("  ⚠️ 回执写盘失败（receipt_failed）：发布状态不变，请人工核对平台后台", file=sys.stderr)
        if outcome.run_dir:
            print(f"  截图       = {outcome.run_dir}（before.png / after.png）")
        return EXIT_OK

    print(f"草稿已保存：{loaded.draft.meta.title}")
    print(f"  article_id = {outcome.article_id}")
    if outcome.url:
        print(f"  url        = {outcome.url}")
    if outcome.run_dir:
        print(f"  截图       = {outcome.run_dir / 'draft.png'}")
    return EXIT_OK


def _ingest(args: argparse.Namespace) -> int:
    """产品资料入库（T-13）。

    失败全是「这次输入有问题」（文件不存在 / 格式不支持 / 零事实 / 出处缺失），
    归 EXIT_USAGE；成功后必须提醒：REVIEW.md 未回填前不进自动成稿队列（G2）。
    """
    try:
        result = ingest(Path(args.source), args.product_id, Path(args.data_dir))
    except IngestError as exc:
        print(f"入库失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    print(f"已入库：{result.product_id}")
    print(f"  事实总数 = {result.facts_total}（本次新增 {result.facts_added}）")
    print(f"  库位置   = {result.product_dir}")
    print(f"  抽检单   = {result.product_dir / 'REVIEW.md'}")
    print("  ⚠️ 人工回填 REVIEW.md 的 reviewed_by / reviewed_at 后，该产品才进入自动成稿队列")
    return EXIT_OK


def _write(args: argparse.Namespace) -> int:
    """成稿（T-14）。配置错误退 3；问句非法/产品未抽检/字数撑不够退 1；
    R4 自检不过退 6 —— 稿子已落盘（含 selfcheck.json），是「改稿」不是「改用法」。
    """
    try:
        cfg = load_config(Path(args.config))
        platform_cfg = cfg.platform(args.platform)
    except (ConfigError, KeyError) as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return EXIT_CONFIG_ERROR

    try:
        qcfg = load_query_config(Path(args.queries))
        result = write_article(
            args.product_id,
            args.query,
            data_root=Path(args.data_dir),
            query_config=qcfg,
            platform=platform_cfg,
        )
    except DraftRejected as exc:
        print(f"自检未通过：{exc}", file=sys.stderr)
        for hit in exc.result.hits[:5]:
            print(f"  [{hit.check}] {hit.term}（{hit.excerpt}）", file=sys.stderr)
        return EXIT_FAILED
    except PipelineError as exc:
        print(f"成稿失败：{exc}", file=sys.stderr)
        return EXIT_USAGE

    print(f"成稿完成：{result.slug}")
    print(f"  目录     = {result.draft_dir}")
    print(f"  字数     = {result.word_count}")
    print(f"  锚定问句 = {args.query}")
    print(f"  自检     = 通过（{result.selfcheck_path.name}）")
    print("  下一步   = geopublish publish %s --mode draft" % result.draft_dir)
    return EXIT_OK


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
