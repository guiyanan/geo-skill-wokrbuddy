"""T-19 日报渲染（PRD R18）：把 ``data/reports/<date>.json`` 渲染成 ``<date>.md``。

日报是拾一米的**唯一检查入口**（R18），必须独立回答四问，缺一即渲染失败
（UT-19-01）—— 渲染不出四问齐全的报告，宁可报错让人修数据，也不发一份
「看起来没问题」的残报：

1. 今天该发几篇？实发几篇？ —— 应发/实发取自 DayReport 的汇总属性（不许手填）
2. 每篇的平台、标题、URL —— published 条目必须有 URL 且标题可解析，
   URL 一律渲染成 Markdown 链接（UT-19-05，邮件里可直接点开）
3. 失败的：失败在哪一步、平台原话、截图路径 —— 截图取自 publish-log 最新记录；
   没走到浏览器阶段的失败（如 GateClosed）本来就没有截图，如实写「无截图」
4. 明天会发什么 —— 队列是唯一事实来源（T-22 的 hold 也是在队列上动手），
   所以队列是必传参数；空队列合法，写「明日无待发」并附原因

零发布日（UT-19-04）：日报照常生成，头部就写「今日无发布」并汇总原因
（节奏到顶 / 平台拒稿 / 无题可写 / 周末停发 / 运行异常），不许留白。

分层纪律：本模块不认识浏览器、不认识 SMTP。渲染是纯函数（读文件但不写），
落盘走 `write_daily_md`（原子写，与调度器的日报 JSON 同一哲学）。
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

from ..core.models import Meta, ModelError
from ..core.state import PublishLog
from ..schedule.scheduler import (
    DayReport,
    Dispatch,
    DispatchItemResult,
    QueueItem,
    load_report,
)

__all__ = [
    "ReportError",
    "ZERO_STREAK_ALERT_DAYS",
    "render_daily_md",
    "zero_reasons",
    "subject_line",
    "write_daily_md",
    "zero_publish_streak",
]

# 连续多少天零发布触发即时告警（PRD R18 推送要求 3：连续两日零发布 → 告警）
ZERO_STREAK_ALERT_DAYS = 2

_WEEKDAYS = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


class ReportError(ValueError):
    """日报数据回答不了四问 —— 渲染失败的唯一理由。"""


# ---------------------------------------------------------------- 主题行
def subject_line(report: DayReport) -> str:
    """``[JOTO GEO] MM-DD 发布 X/Y · 平台``（UT-19-02）：主题行即结论。

    X/Y = 实发/应发；平台取当日有发车记录的平台（去重、保持出现顺序）。
    没有任何发车（周末停发/未到点/崩溃在开箱前）时平台位写「无」——
    空着比写错更误导人。
    """
    try:
        day = date.fromisoformat(report.date)
    except ValueError as exc:
        raise ReportError(f"日报日期不是 ISO 格式：{report.date!r}") from exc
    platforms: list[str] = []
    for d in report.dispatches:
        if d.platform not in platforms:
            platforms.append(d.platform)
    where = "、".join(platforms) if platforms else "无"
    return f"[JOTO GEO] {day.strftime('%m-%d')} 发布 {report.published}/{report.expected} · {where}"


# ---------------------------------------------------------------- 标题解析
def _load_title(meta_path: Path, *, where: str) -> str:
    """从 meta.json 读标题。读不到/不合法 → ReportError（Q2 缺数据）。"""
    try:
        raw = meta_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReportError(f"{where}：读不到 meta.json（{meta_path}）：{exc.strerror or exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ReportError(f"{where}：meta.json 不是合法 JSON（{meta_path} 第 {exc.lineno} 行）") from exc
    try:
        return Meta.from_json(data).title
    except ModelError as exc:
        raise ReportError(f"{where}：meta.json 不合法（{meta_path}）：{exc}") from exc


def _published_title(data_root: Path, slug: str) -> str:
    """已发布稿件的标题：必须能解析（Q2 的硬性数据，解析不了 = 渲染失败）。"""
    return _load_title(data_root / "drafts" / slug / "meta.json", where=f"已发布稿件 {slug}")


def _queue_title(data_root: Path, item: QueueItem) -> str | None:
    """待发稿件的标题：尽力而为。稿子可能还在 prepare 中途（meta 未落盘），
    此时 Q4 用 slug 标识已经足够（喊停靠的是 slug），不判渲染失败。"""
    candidates = [Path(item.draft_dir) / "meta.json", data_root / "drafts" / item.slug / "meta.json"]
    for path in candidates:
        if path.is_file():
            try:
                return _load_title(path, where=f"待发稿件 {item.slug}")
            except ReportError:
                return None
    return None


# ---------------------------------------------------------------- 失败明细
def _latest_screenshot(log: PublishLog, platform: str, slug: str) -> str:
    """该篇稿件最近一次有截图的日志行。截图在失败链路的哪一环拍的都有，
    取最新一张即可 —— 它就是「出事那一刻」的现场。"""
    shot = ""
    key = platform.strip().lower()
    for entry in log.entries():
        if entry.platform.strip().lower() == key and entry.slug == slug and entry.screenshot:
            shot = entry.screenshot
    return shot


def _failure_lines(report: DayReport, log: PublishLog) -> list[str]:
    """Q3：每条失败/待人工记录一行「哪一步、平台原话、截图路径」。"""
    lines: list[str] = []
    for d in report.dispatches:
        for item in d.items:
            if item.result in ("published", "already_published", "skipped_duplicate"):
                continue
            step = item.failure_kind or "运行期异常"
            reason = item.reason or item.result
            if item.result in ("failed", "needs_human") and not item.reason:
                # 平台原话是 Q3 的硬数据：连 reason 都没有 = 上游把现场弄丢了
                raise ReportError(
                    f"{d.platform} 的 {item.slug} 结果 {item.result} 缺少失败原因（reason 为空）"
                )
            shot = _latest_screenshot(log, d.platform, item.slug)
            shot_text = shot if shot else "无截图（publish-log 无记录，未走到浏览器阶段）"
            lines.append(
                f"- `{item.slug}`（{d.platform}）结果 **{item.result}**\n"
                f"  - 失败在哪一步：{step}\n"
                f"  - 平台原话：{reason}\n"
                f"  - 截图：{shot_text}"
            )
    return lines


def _item_line(data_root: Path, dispatch: Dispatch, item: DispatchItemResult) -> str:
    """Q2：一篇已发布稿件 = 平台 + 标题 + 可点击 URL。缺 URL/标题即渲染失败。"""
    if not item.url.strip():
        raise ReportError(
            f"{dispatch.platform} 的 {item.slug} 标记 published 但没有 URL"
            "（R8：没 verify 过的不算成功）"
        )
    title = _published_title(data_root, item.slug)
    return f"- [{title}]({item.url}) — {dispatch.platform} · `{item.slug}`"


# ---------------------------------------------------------------- 零发布原因
def zero_reasons(report: DayReport) -> list[str]:
    """UT-19-04：零发布日必须写得出「为什么没发」。"""
    reasons: list[str] = []
    if report.error:
        reasons.append(f"运行异常：{report.error}")
    for d in report.dispatches:
        if d.skipped_reason:
            reasons.append(f"{d.platform}：{d.skipped_reason}")
        for item in d.items:
            if item.result not in ("published", "already_published"):
                reasons.append(f"{d.platform} 的 {item.slug}：{item.reason or item.result}")
    for prep in report.prepare:
        if prep.status != "prepared":
            reasons.append(f"选题/成稿 {prep.status}：{prep.reason}")
    reasons.extend(report.notes)
    if not reasons:
        # 唯一「正常」的零发布：发车阶段队列就是空的。
        # 不能写「无新题」——prepare 可能刚备了两篇（两阶段红线：明天才上车）
        reasons.append("发车时队列无可发稿件（两阶段：今日 prepare 的稿明日才发）")
    # 保序去重：同槽防重这类 note 重跑时会重复出现
    seen: set[str] = set()
    unique: list[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            unique.append(r)
    return unique


# ---------------------------------------------------------------- 渲染主函数
def render_daily_md(
    report: DayReport,
    *,
    data_root: str | Path,
    queue: Sequence[QueueItem] | None,
    log: PublishLog | None,
) -> str:
    """把当日日报渲染成 Markdown。四问缺一即抛 ReportError（UT-19-01）。

    - `queue`：明日预告的唯一事实来源（Q4），必传；空列表合法。
    - `log`：失败截图的出处（Q3），必传。
    """
    if queue is None:
        raise ReportError("缺少队列数据：回答不了「明天会发什么」（Q4）")
    if log is None:
        raise ReportError("缺少 publish-log：回答不了「失败的截图在哪」（Q3）")
    try:
        day = date.fromisoformat(report.date)
    except ValueError as exc:
        raise ReportError(f"日报日期不是 ISO 格式：{report.date!r}") from exc
    data_root = Path(data_root)

    lines: list[str] = []
    weekday = _WEEKDAYS[day.weekday()]
    lines.append(f"# GEO 发布日报 · {report.date}（{weekday}{'' if report.workday else '，非工作日'}）")
    lines.append("")
    if report.error:
        lines.append(f"> ⚠️ 本次运行异常终止：{report.error}")
        lines.append("")
    if report.published == 0:
        # UT-19-04：零发布日的第一屏就要有结论 + 原因
        lines.append("> **今日无发布。** 原因：")
        for r in zero_reasons(report):
            lines.append(f"> - {r}")
        lines.append("")

    # Q1：应发/实发
    lines.append(f"## 一、应发/实发：{report.expected} / {report.published}")
    lines.append("")
    if not report.dispatches:
        lines.append("- 今日无任何平台发车记录")
    for d in report.dispatches:
        state = d.skipped_reason or f"发 {d.published}/{d.planned}"
        lines.append(f"- {d.platform}（{d.publish_at} 发车）：{state}")
    lines.append("")

    # Q2：发布明细
    published_items = [
        (d, i) for d in report.dispatches for i in d.items if i.result == "published"
    ]
    lines.append(f"## 二、发布明细（{len(published_items)} 篇）")
    lines.append("")
    if published_items:
        for d, item in published_items:
            lines.append(_item_line(data_root, d, item))
    else:
        lines.append("- 今日无成功发布")
    lines.append("")

    # Q3：失败与异常
    failures = _failure_lines(report, log)
    lines.append("## 三、失败与异常")
    lines.append("")
    if failures:
        lines.extend(failures)
    else:
        lines.append("- 今日无失败")
    if report.unresolved:
        lines.append("")
        lines.append(f"⚠️ 未解决 needs_human（{len(report.unresolved)} 篇，处理前不会自动重试）：")
        for u in report.unresolved:
            lines.append(f"- `{u.slug}`（{u.platform}，{u.since} 起）：{u.note}")
    lines.append("")

    # Q4：明日预告
    lines.append(f"## 四、明日预告（{len(queue)} 篇待发）")
    lines.append("")
    if queue:
        for item in queue:
            title = _queue_title(data_root, item)
            label = f"{title}（`{item.slug}`）" if title else f"`{item.slug}`"
            lines.append(f"- {label} — {item.platform} · 锚定 {item.query_id}")
        lines.append("")
        lines.append("要喊停某篇：`geopublish hold <slug>`（T-22）")
    else:
        lines.append("- 明日无待发稿件")
        for prep in report.prepare:
            if prep.status != "prepared":
                lines.append(f"  - 原因：选题/成稿 {prep.status}（{prep.reason}）")
    lines.append("")

    if report.notes:
        lines.append("## 备注")
        lines.append("")
        for note in report.notes:
            lines.append(f"- {note}")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


# ---------------------------------------------------------------- 落盘
def write_daily_md(data_root: str | Path, day: date | str, md: str) -> Path:
    """``data/reports/<date>.md`` 原子写：收件箱/人都不会读到写一半的文件。"""
    day_str = day if isinstance(day, str) else day.isoformat()
    target = Path(data_root) / "reports" / f"{day_str}.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(md)
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


# ---------------------------------------------------------------- 零发布连击
def zero_publish_streak(data_root: str | Path, day: date) -> int:
    """截至 `day`（含）连续零发布的天数（UT-19-07 的告警依据）。

    只数「 scheduler 确实有机会发车」的日子：
    - 日报缺失 → 停止向前数。「进程没跑」由心跳告警负责（它更准），
      连击混进「没跑的日子」会把两种故障揉成一条误报。
    - 非工作日且无发车记录（workdays_only 的周末）→ 跳过不计：
      那是配置让停的，不是「发了没发出去」。
    """
    streak = 0
    cursor = day
    while True:
        report = load_report(data_root, cursor)
        if report is None:
            break
        if report.published > 0:
            break
        if report.workday or report.dispatches or report.expected > 0:
            streak += 1
        cursor -= timedelta(days=1)
    return streak
