"""发布编排（T-09：草稿投递；T-10：真实发布与回执）。

一条命令把「一个稿件目录」变成「平台草稿箱里的一篇」（draft），
或「平台上一篇公开文章 + 一份回执」（publish）：

    load_draft → open_page → check_login → open_editor → fill_content
    → fill_meta → upload_cover → submit → verify →（publish）回执落盘

发布模式的顺序有个真机约束（T-10 校准）：标题必须在发布对话框打开**之前**填
（对话框是遮罩层，会挡住编辑器页的点击），而封面、分类、标签、摘要、声明都在
对话框**里面**。fill_meta 内部先填标题再开对话框，所以发布模式是
meta 在前、cover 在后——与架构 §5 的草稿期直觉相反，以真机 DOM 为准。

分层纪律：这里不认识 DOM（那是 adapter 的事），也不认识命令行参数（那是 cli.py 的事）。
所以它可以用假 adapter + 假 page 原样跑，不需要浏览器 —— 顺序错了、少了一步，
单测会直接抓到，而不是等真机上发现「封面没传就点了发布」。

**不做的事**（都另有工单，半套实现比没实现更危险）：
- 幂等与并发锁（R9）→ T-11。这里连跑两次会发两遍，是已知缺口。
- 失败分类与红线（R12/R13）→ T-12。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .adapters.base import Page, SubmitMode
from .core.clock import Clock, SystemClock
from .core.config import Gate, PlatformConfig
from .core.models import Draft, LogEntry, Meta, ModelError, Receipt
from .core.session import check_login
from .core.state import Status

__all__ = [
    "DraftLoadError",
    "GateClosed",
    "LoadedDraft",
    "PublishOutcome",
    "load_draft",
    "publish_draft",
]

logger = logging.getLogger("geopublish.publish")

# 正文文件名候选，按优先级。`csdn.md` 这种「平台名.md」优先：同一份稿件在不同
# 平台可以有不同正文（改标题、改排版），而那不该靠改 meta 来决定。
BODY_CANDIDATES: tuple[str, ...] = ("{platform}.md", "body.md", "draft.md")

# 截图回调：拿到 (page, 目标路径)。用回调而不是给 Page 协议加 screenshot，
# 是因为「存证」是编排层的事，adapter 不该为了截图多依赖一个写面。
Screenshot = Callable[[Any, Path], None]


class DraftLoadError(ValueError):
    """稿件目录不可用：缺文件、JSON 坏、封面指空、正文与 content_sha 对不上。

    这一层刻意比 `ModelError` 更早：开浏览器之前就要把所有本地问题暴露完。
    为了一个不存在的封面启动 Chrome，是最冤的一次失败。
    """


class GateClosed(RuntimeError):
    """配置里的闸门不允许这次动作（gate=dry_run / 平台被禁用）。

    闸门是「人写的刹车」：dry_run 就是不许碰平台。绕过它去「试一下」，
    等于把刹车当装饰。
    """


@dataclass(frozen=True, slots=True)
class LoadedDraft:
    """从磁盘装出来的稿件：正文 + 元信息 + 已定位的封面。"""

    draft: Draft
    cover: Path | None = None


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """一次投递的结果。`ok` 只由 verify 决定（R8：没验证过的不算成功）。"""

    ok: bool
    url: str = ""
    article_id: str = ""
    reason: str = ""
    run_dir: Path | None = None
    # T-10 回执：写成功是路径；写失败是 None + receipt_failed=True
    # （发成了但没存上证——与「没发成」是两种处置，必须分开表达）。
    receipt_path: Path | None = None
    receipt_failed: bool = False


# --------------------------------------------------------------- 装载

def load_draft(directory: str | Path, *, platform: str = "csdn") -> LoadedDraft:
    """读一个稿件目录：`meta.json` + 正文 + 封面。

    所有本地问题都在这里报完（DraftLoadError），一个都不留到浏览器启动之后。
    """
    root = Path(directory)
    if not root.is_dir():
        raise DraftLoadError(f"稿件目录不存在：{root}")

    meta_file = root / "meta.json"
    if not meta_file.is_file():
        raise DraftLoadError(f"缺 meta.json：{root}")

    try:
        data = json.loads(meta_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise DraftLoadError(f"meta.json 不是合法 JSON（{meta_file}）：{exc}") from exc
    if not isinstance(data, dict):
        raise DraftLoadError(f"meta.json 顶层必须是对象（{meta_file}）")

    try:
        meta = Meta.from_json(data)
    except ModelError as exc:
        raise DraftLoadError(f"meta.json 不合法（{meta_file}）：{exc}") from exc

    body = _read_body(root, platform)
    try:
        draft = Draft(meta=meta, body=body)
    except ModelError as exc:
        raise DraftLoadError(f"稿件自相矛盾（{root}）：{exc}") from exc

    return LoadedDraft(draft=draft, cover=_resolve_cover(root, meta))


def _read_body(root: Path, platform: str) -> str:
    """按候选顺序找正文。找不到就把找过哪些名字全列出来。"""
    names = [name.format(platform=platform) for name in BODY_CANDIDATES]
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            raise DraftLoadError(f"正文不是 UTF-8：{path}（{exc}）") from exc
        if not text.strip():
            # 空稿走到 fill_content 会被拒，但那时浏览器已经开了
            raise DraftLoadError(f"正文为空：{path}")
        return text
    raise DraftLoadError(f"稿件目录里没有正文，找过：{'、'.join(names)}（{root}）")


def _resolve_cover(root: Path, meta: Meta) -> Path | None:
    """解析 `meta.cover`：相对路径按稿件目录算，绝对路径原样用。"""
    declared = (meta.cover or "").strip()
    if not declared:
        return None
    path = Path(declared).expanduser()
    if not path.is_absolute():
        path = root / path
    if not path.is_file():
        raise DraftLoadError(
            f"meta.cover 指向的封面不存在：{path}（本地就该拦住，别等开了浏览器再失败）"
        )
    return path


# --------------------------------------------------------------- 闸门

def check_gate(gate: Gate | str, platform_cfg: PlatformConfig, mode: SubmitMode | str) -> None:
    """配置闸门：dry_run 不动平台；只有 publish 闸门才允许真实发布。

    这是闸门最小可用形态（T-16 会把自检也串进这条链）。放在这里而不是 CLI 里，
    是为了让「配置说不行」这件事能被单测直接钉住，而不是靠人记得加 if。
    """
    gate = Gate(gate)
    mode = SubmitMode(mode)
    if gate is Gate.DRY_RUN:
        raise GateClosed("gate=dry_run：本次不连接平台、不写草稿")
    if not platform_cfg.enabled:
        raise GateClosed(f"平台 {platform_cfg.key} 在配置里是 enabled=false：本次不投递")
    if mode is SubmitMode.PUBLISH and gate is not Gate.PUBLISH:
        raise GateClosed(f"gate={gate.value}：只允许存草稿，不允许真实发布")


# --------------------------------------------------------------- 编排

def publish_draft(
    adapter: Any,
    loaded: LoadedDraft,
    platform_cfg: PlatformConfig,
    *,
    gate: Gate | str,
    open_page: Callable[[], AbstractContextManager[Page]],
    mode: SubmitMode | str = SubmitMode.DRAFT,
    data_dir: str | Path = "data",
    clock: Clock | None = None,
    screenshot: Screenshot | None = None,
) -> PublishOutcome:
    """按架构 §5 的顺序把一篇稿子投出去；publish 模式额外落回执（T-10 / R10）。

    `open_page` 是「开一次浏览器会话」的工厂（CLI 传 `PersistentBrowser.open`），
    注入之后单测用假会话跑同一条路径 —— 顺序即被测对象。
    """
    mode = SubmitMode(mode)
    check_gate(gate, platform_cfg, mode)
    draft = loaded.draft
    clk = clock or SystemClock()

    with open_page() as page:
        # 新开的会话停在空白页上，check_auth 在空白页只能判出 unknown。
        # 先导航到编辑器页再探活：已登录 → 看到头像/编辑器；被踢 → 平台自己跳登录页，
        # 两种情况都给出权威信号。探活一次即停，不通过就抛 AuthRequired（R6）。
        page.goto(adapter.editor_url)
        try:
            # SPA 的 load 事件不代表渲染完成（真机实测会在半渲染页面上误判 unknown）：
            # 先等一个权威信号出现，再让 check_auth 做那一次判定。
            adapter.wait_for_auth_signal(page)
        except Exception:
            # 等不到信号不在这里失败：check_auth 会给出一个能对应到退出码的标准结论。
            logger.debug("探活信号等待未命中，交由 check_auth 判定")
        check_login(adapter, page)
        adapter.open_editor(page, draft)
        adapter.fill_content(page, draft)

        if mode is SubmitMode.PUBLISH:
            # 真机约束（T-10）：标题要在发布对话框打开前填（对话框是遮罩层，
            # 会挡住编辑器页的点击），封面在对话框里。所以发布模式 meta 在前。
            adapter.fill_meta(page, draft, platform_cfg, mode=mode)
            if loaded.cover is not None:
                adapter.upload_cover(page, loaded.cover)
        else:
            if loaded.cover is not None:
                # 真机校准（T-09b）：封面上传框在「发布文章」对话框里，编辑器页面上
                # 没有这个控件。草稿模式强行上传只会撞选择器超时，明说跳过。
                logger.warning("草稿模式不上传封面（封面控件在发布对话框里）：%s", loaded.cover)
            adapter.fill_meta(page, draft, platform_cfg, mode=mode)

        run_dir: Path | None = None
        receipt_path: Path | None = None
        receipt_failed = False

        if mode is SubmitMode.PUBLISH and screenshot is not None:
            # R10：before.png 在点发布按钮之前截——之后页面什么样谁都不知道。
            run_dir = _run_dir(data_dir, clk)
            _snap(page, screenshot, run_dir, "before.png")

        submitted = adapter.submit(page, mode=mode)
        logger.info("submit 返回 %s：%s", submitted.state.value, submitted.reason)

        result = adapter.verify(page, mode=mode)

        if result.ok:
            if mode is SubmitMode.PUBLISH:
                if screenshot is not None:
                    # 不变量：publish + screenshot 时 run_dir 在 before.png 处已建
                    assert run_dir is not None
                    _snap(page, screenshot, run_dir, "after.png")
                record = _record_publish(
                    draft, platform_cfg, result, run_dir, data_dir, clk
                )
                receipt_path, receipt_failed = record.receipt_path, record.failed
            elif screenshot is not None:
                run_dir = _capture(page, screenshot, data_dir, clk)

    logger.info(
        "verify %s：%s（article_id=%r）",
        "通过" if result.ok else "未通过",
        result.reason,
        result.article_id,
    )
    return PublishOutcome(
        ok=result.ok,
        url=result.url,
        article_id=result.article_id,
        reason=result.reason,
        run_dir=run_dir,
        receipt_path=receipt_path,
        receipt_failed=receipt_failed,
    )


def _run_dir(data_dir: str | Path, clock: Clock) -> Path:
    """本次运行的存证目录：data/runs/<ts>/。时间只走注入时钟（UT-10-04）。"""
    return Path(data_dir) / "runs" / clock.now().strftime("%Y%m%d_%H%M%S")


def _snap(page: Any, screenshot: Screenshot, run_dir: Path, name: str) -> Path | None:
    """截一张图。**截图失败不改判定**：发了就是发了，别让存证拖垮结论。"""
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        target = run_dir / name
        screenshot(page, target)
    except Exception as exc:  # noqa: BLE001 - 存证失败的形态太多，一律降级为告警
        logger.warning("截图失败（不影响发布判定）：%s", exc)
        return None
    return target


def _account_from_url(url: str) -> str:
    """从已发布 URL 里取账号段（blog.csdn.net/<account>/article/details/<id>）。

    取不到用 "default"：account 是日志的必填列，宁可标注缺失也不能写空串
    混进历史数据。多账号支持留给真有那么一天再说。
    """
    parts = urlsplit(str(url or ""))
    if parts.netloc.endswith("csdn.net"):
        segs = [s for s in parts.path.split("/") if s]
        if segs and segs[0] not in {"article", "mp_blog"}:
            return segs[0]
    return "default"


def _record_publish(
    draft: Draft,
    platform_cfg: PlatformConfig,
    result: Any,
    run_dir: Path | None,
    data_dir: str | Path,
    clock: Clock,
) -> Any:
    """发布成功后的回执：receipt.json + publish-log.csv 一行（R10）。

    回执写失败不掩盖发布成功（UT-10-03）：core/receipt.save_receipt 会把
    note 记成 receipt_failed 并照常写日志行。两个时间字段都走注入时钟。
    """
    from .core.receipt import save_receipt
    from .core.state import PublishLog

    shot = ""
    if run_dir is not None and (run_dir / "after.png").is_file():
        shot = str(run_dir / "after.png")
    account = _account_from_url(result.url)
    receipt = Receipt(
        url=result.url,
        article_id=result.article_id,
        published_at=clock.now(),
        screenshot=shot,
        content_sha=draft.content_sha,
    )
    entry = LogEntry(
        ts=clock.now(),
        platform=platform_cfg.key,
        account=account,
        slug=draft.meta.slug,
        idem_key=draft.idem_key(platform_cfg.key, account),
        status=Status.PUBLISHED,
        url=result.url,
        article_id=result.article_id,
        screenshot=shot,
    )
    record = save_receipt(
        receipt,
        run_dir if run_dir is not None else _run_dir(data_dir, clock),
        PublishLog(Path(data_dir) / "publish-log.csv"),
        entry,
    )
    if record.failed:
        logger.warning("回执写盘失败（receipt_failed）：发布成功状态不变")
    return record


def _capture(
    page: Any,
    screenshot: Screenshot,
    data_dir: str | Path,
    clock: Clock | None,
) -> Path | None:
    """成功时留一张截图。**截图失败不改判定**：发了就是发了，别让存证拖垮结论。"""
    now = (clock or SystemClock()).now()
    run_dir = Path(data_dir) / "runs" / now.strftime("%Y%m%d_%H%M%S")
    try:
        run_dir.mkdir(parents=True, exist_ok=True)
        screenshot(page, run_dir / "draft.png")
    except Exception as exc:  # noqa: BLE001 - 存证失败的形态太多，一律降级为告警
        logger.warning("截图失败（不影响发布判定）：%s", exc)
        return None
    return run_dir
