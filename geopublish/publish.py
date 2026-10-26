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

幂等与并发（T-11 / R9）的三条铁律，都在 publish 模式的分支里：
1. **先拿平台锁再动手**（lock_root 传入时）：两个进程不能同时操作同一平台账号。
2. **submit 前先落 `submitting` 行**：进程死在提交瞬间，下次运行靠这行认出现场，
   转 `needs_human` 而不是自动补发第二篇。
3. **verify 没过 → needs_human，不是 failed**：「失败了重试」在发布场景等于
   「可能已发出但再发一遍」。failed 只留给 submit 之前（稿件还没碰平台）的阶段。

draft 模式不写日志：状态机没有 drafted 态，加状态属于「改幂等状态机」，
PRD §9.5 要求先评审 —— 草稿重复是已接受的低危缺口（草稿箱人工可见可删）。

失败分类（T-12 / R12 R13）：publish 模式的每一步异常都过 `core/errors.classify`——
人机校验零重试转 needs_human；登录失效原样抛出（CLI 退 2，不占重试配额）；
平台拒稿转 needs_human 并把原话回写进稿件目录；网络超时按 2/4/8s 退避重试，
到顶落 failed。重试只发生在 submit 之前：submitting 行落盘后出的任何异常，
一律转 needs_human + 草稿箱残留检查（N5），绝不自动再来一遍。
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .adapters.base import Page, SubmitMode
from .core.clock import Clock, SystemClock
from .core.config import Gate, PlatformConfig
from .core.errors import RETRY_HARD_CAP, FailureKind, Verdict, backoff_delays, classify
from .core.lock import platform_lock
from .core.models import Draft, LogEntry, Meta, ModelError, Receipt
from .core.session import check_login
from .core.state import PublishLog, Status, recover_status, transition
from .write.cover import require_cover_approved

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
    """从磁盘装出来的稿件：正文 + 元信息 + 已定位的封面。

    `source_dir` 是稿件目录本身：平台拒稿时要把原话回写进去（UT-12-03），
    没它就找不着回写位置。
    """

    draft: Draft
    cover: Path | None = None
    source_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class PublishOutcome:
    """一次投递的结果。`ok` 只由 verify 决定（R8：没验证过的不算成功）。"""

    ok: bool
    url: str = ""
    article_id: str = ""
    reason: str = ""
    # T-11：同一幂等键已 published，本次一行没发。ok=True（这不是失败），
    # CLI 凭这个字段打印 SKIPPED_DUPLICATE 而不是「已发布」。
    skipped_duplicate: bool = False
    run_dir: Path | None = None
    # T-10 回执：写成功是路径；写失败是 None + receipt_failed=True
    # （发成了但没存上证——与「没发成」是两种处置，必须分开表达）。
    receipt_path: Path | None = None
    receipt_failed: bool = False
    # T-12：失败分类（core/errors.FailureKind）。None = 不是分类失败
    # （例如幂等拦截、verify 未过）。CLI 把它打进输出，日报靠它分流。
    failure_kind: FailureKind | None = None


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

    return LoadedDraft(draft=draft, cover=_resolve_cover(root, meta), source_dir=root)


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
    lock_root: str | Path | None = None,
    retry_max: int | None = None,
    sleep: Callable[[float], None] | None = None,
) -> PublishOutcome:
    """按架构 §5 的顺序把一篇稿子投出去；publish 模式额外落回执（T-10 / R10）。

    `open_page` 是「开一次浏览器会话」的工厂（CLI 传 `PersistentBrowser.open`），
    注入之后单测用假会话跑同一条路径 —— 顺序即被测对象。

    `lock_root`（T-11）：传入则在**幂等预检之前**拿平台锁，全程持有到结束；
    None 表示不加锁（仅限单测 —— CLI 永远传值）。锁必须在预检外面：
    两个进程同时通过预检再同时发，等于没有幂等。

    `retry_max` / `sleep`（T-12）：网络瞬时故障（超时/断连）在本次运行内按
    2/4/8s 退避重试，次数 = min(retry_max, RETRY_HARD_CAP)，None 用硬顶。
    重试只发生在 submit 之前——submitting 行落盘后的异常一律转 needs_human。
    `sleep` 是等待动作的注入点：测试传假时钟推进，生产默认 time.sleep。
    """
    mode = SubmitMode(mode)
    check_gate(gate, platform_cfg, mode)
    draft = loaded.draft
    clk = clock or SystemClock()

    lock: AbstractContextManager[Any] = (
        platform_lock(platform_cfg.key, lock_root=lock_root)
        if lock_root is not None
        else nullcontext()
    )

    with lock:
        if mode is not SubmitMode.PUBLISH:
            # draft 模式不写日志（状态机无 drafted 态），失败分类也只作用于
            # publish 模式：草稿是交互式调试链路，异常原样抛给 CLI（退 6）。
            return _run(
                adapter, loaded, platform_cfg,
                mode=mode, data_dir=data_dir, clk=clk, screenshot=screenshot,
                open_page=open_page, log=None, prior=None,
            )

        log: PublishLog = PublishLog(Path(data_dir) / "publish-log.csv")
        delays = backoff_delays(RETRY_HARD_CAP if retry_max is None else retry_max)
        doze = time.sleep if sleep is None else sleep
        retries = 0
        while True:
            prior = _find_prior(log, draft, platform_cfg.key)
            decision = _idem_decision(log, draft, platform_cfg, prior, clk)
            if decision is not None:
                # SKIPPED_DUPLICATE / needs_human / rejected：一律不开浏览器
                return decision
            try:
                return _run(
                    adapter, loaded, platform_cfg,
                    mode=mode, data_dir=data_dir, clk=clk, screenshot=screenshot,
                    open_page=open_page, log=log, prior=prior,
                )
            except Exception as exc:
                verdict = classify(exc)
                if verdict is None or verdict.status is None:
                    # classify  None：不认识 = bug，带 traceback 炸出去，不许吞；
                    # status None（AUTH_EXPIRED）：CLI 退 2，不落 failed ——
                    # 授权问题不占重试配额，重新授权后这篇应当直接能发。
                    raise
                if verdict.retryable and retries < len(delays):
                    delay = delays[retries]
                    retries += 1
                    logger.warning(
                        "%s：%.0fs 后重试（第 %d/%d 次）：%s",
                        verdict.kind, delay, retries, len(delays), exc,
                    )
                    doze(delay)
                    continue
                note = _failure_note(verdict, exc, retries)
                _append_state(log, draft, platform_cfg, verdict.status, clk, prior=prior, note=note)
                logger.error("投递失败（%s）：%s", verdict.kind, note)
                return PublishOutcome(ok=False, reason=note, failure_kind=verdict.kind)


def _run(
    adapter: Any,
    loaded: LoadedDraft,
    platform_cfg: PlatformConfig,
    *,
    mode: SubmitMode,
    data_dir: str | Path,
    clk: Clock,
    screenshot: Screenshot | None,
    open_page: Callable[[], AbstractContextManager[Page]],
    log: PublishLog | None,
    prior: LogEntry | None,
) -> PublishOutcome:
    """临界区内的实际投递。到这里幂等预检已通过，可以放心开浏览器。

    每个阶段推进时更新 `stage`：任何异常逃逸出去，外层都能把「停在第几步」
    贴到异常上（`_mark_stage`），并最终写进 publish-log 的 note（R12）。
    逃逸路径还会尽力补一张 failed.png——失败截图不可省略（PRD §9.3 R12）。
    """
    draft = loaded.draft
    page: Any = None
    stage = "open_page"
    run_dir: Path | None = None
    try:
        if mode is SubmitMode.PUBLISH and loaded.cover is not None:
            # T-15 / G2：未过人工抽检的封面不进发布流程。拦在 open_page 之前——
            # 为一张没确认的封面启动浏览器，和为不存在的封面启动一样冤。
            # draft 模式不上传封面（控件在发布对话框里），审批闸不拦草稿链路。
            require_cover_approved(loaded.source_dir or loaded.cover.parent, loaded.cover)
        with open_page() as opened:
            page = opened
            # 新开的会话停在空白页上，check_auth 在空白页只能判出 unknown。
            # 先导航到编辑器页再探活：已登录 → 看到头像/编辑器；被踢 → 平台自己跳登录页，
            # 两种情况都给出权威信号。探活一次即停，不通过就抛 AuthRequired（R6）。
            stage = "check_login"
            page.goto(adapter.editor_url)
            try:
                # SPA 的 load 事件不代表渲染完成（真机实测会在半渲染页面上误判 unknown）：
                # 先等一个权威信号出现，再让 check_auth 做那一次判定。
                adapter.wait_for_auth_signal(page)
            except Exception:
                # 等不到信号不在这里失败：check_auth 会给出一个能对应到退出码的标准结论。
                logger.debug("探活信号等待未命中，交由 check_auth 判定")
            check_login(adapter, page)
            stage = "open_editor"
            adapter.open_editor(page, draft)
            stage = "fill_content"
            adapter.fill_content(page, draft)

            if mode is SubmitMode.PUBLISH:
                # 真机约束（T-10）：标题要在发布对话框打开前填（对话框是遮罩层，
                # 会挡住编辑器页的点击），封面在对话框里。所以发布模式 meta 在前。
                stage = "fill_meta"
                adapter.fill_meta(page, draft, platform_cfg, mode=mode)
                if loaded.cover is not None:
                    stage = "upload_cover"
                    adapter.upload_cover(page, loaded.cover)
            else:
                if loaded.cover is not None:
                    # 真机校准（T-09b）：封面上传框在「发布文章」对话框里，编辑器页面上
                    # 没有这个控件。草稿模式强行上传只会撞选择器超时，明说跳过。
                    logger.warning("草稿模式不上传封面（封面控件在发布对话框里）：%s", loaded.cover)
                stage = "fill_meta"
                adapter.fill_meta(page, draft, platform_cfg, mode=mode)

            receipt_path: Path | None = None
            receipt_failed = False

            if mode is SubmitMode.PUBLISH and screenshot is not None:
                # R10：before.png 在点发布按钮之前截——之后页面什么样谁都不知道。
                run_dir = _run_dir(data_dir, clk)
                _snap(page, screenshot, run_dir, "before.png")

            stage = "submit"
            if mode is SubmitMode.PUBLISH:
                # R9 铁律：先落 submitting 再点发布。进程死在提交瞬间时，
                # 这行是下次运行认出现场（→ needs_human）的唯一依据。
                assert log is not None  # publish 模式的 log 在预检处已建
                _append_state(
                    log, draft, platform_cfg, Status.SUBMITTING, clk,
                    prior=prior, note="提交前落盘：崩溃恢复锚点",
                )
                try:
                    submitted = adapter.submit(page, mode=mode)
                    logger.info("submit 返回 %s：%s", submitted.state.value, submitted.reason)
                    result = adapter.verify(page, mode=mode)
                except Exception as exc:
                    # 提交行已落盘：稿子可能已碰平台。绝不重试（重试 = 可能发两遍），
                    # 分类处置（needs_human + 残留检查）全在 _post_submit_failure。
                    handled = _post_submit_failure(
                        adapter, page, log, loaded, platform_cfg,
                        exc=exc, clk=clk, prior=prior,
                        screenshot=screenshot, run_dir=run_dir,
                    )
                    if handled is not None:
                        return handled
                    raise
            else:
                submitted = adapter.submit(page, mode=mode)
                logger.info("submit 返回 %s：%s", submitted.state.value, submitted.reason)
                result = adapter.verify(page, mode=mode)

            if mode is SubmitMode.PUBLISH and not result.ok:
                # verify 判不了成功 ≠ 没发出去。标 failed 会诱导重发（可能变两篇），
                # 只能交人工去平台后台看一眼 —— 与「submitting 无 URL」同一条铁律。
                assert log is not None
                _append_state(
                    log, draft, platform_cfg, Status.NEEDS_HUMAN, clk,
                    prior=prior, note=f"verify 未确认成功：{result.reason}",
                )

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
    except Exception as exc:
        _mark_stage(exc, stage)
        if screenshot is not None and page is not None:
            # R12：失败截图不可省略。截图本身失败不改判定（_snap 内部已降级为告警）
            run_dir = run_dir or _run_dir(data_dir, clk)
            _snap(page, screenshot, run_dir, "failed.png")
        raise

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


def _mark_stage(exc: BaseException, stage: str) -> None:
    """把失败阶段贴到异常对象上（不改异常类型，classify 仍认原来的类）。

    异常实例允许挂属性；极端情况（自定义 __setattr__ 拒写）下阶段信息丢了
    也不许掩盖原异常——所以这里吞掉一切二次失败。
    """
    try:
        exc._gp_stage = stage  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - 见 docstring：标阶段失败不许盖过原异常
        pass


def _failure_note(verdict: Verdict, exc: BaseException, retries: int, *, stage: str = "") -> str:
    """publish-log 的 note：类别 + 阶段 + 重试次数 + 平台原话/原报错（R12 三件套）。"""
    parts = [f"failure_kind={verdict.kind.value}"]
    stage = stage or str(getattr(exc, "_gp_stage", "") or "")
    if stage:
        parts.append(f"stage={stage}")
    if retries:
        parts.append(f"已重试 {retries} 次后仍失败")
    if verdict.platform_text:
        parts.append(f"平台原话：{verdict.platform_text}")
    parts.append(str(exc))
    return "；".join(parts)


def _post_submit_failure(
    adapter: Any,
    page: Any,
    log: PublishLog,
    loaded: LoadedDraft,
    platform_cfg: PlatformConfig,
    *,
    exc: BaseException,
    clk: Clock,
    prior: LogEntry | None,
    screenshot: Screenshot | None,
    run_dir: Path | None,
) -> PublishOutcome | None:
    """提交行落盘之后出的异常：绝不重试，查草稿箱残留（N5），转 needs_human。

    返回 None = 不认识这个异常：按 bug 原样抛出。submitting 行已在日志里，
    下次运行走崩溃恢复照样转 needs_human —— 两条路都到人工，
    没有一条到「再发一篇」。
    """
    verdict = classify(exc)
    if verdict is None or verdict.status is None:
        return None
    if screenshot is not None:
        assert run_dir is not None  # publish + screenshot：before.png 时 run_dir 已建
        _snap(page, screenshot, run_dir, "failed.png")
    residue = _residue_probe(adapter, page)
    note = _failure_note(verdict, exc, 0, stage="submit") + f"；{residue}"
    _append_state(
        log, loaded.draft, platform_cfg, Status.NEEDS_HUMAN, clk, prior=prior, note=note,
    )
    if verdict.kind is FailureKind.CONTENT_REJECTED:
        _writeback_rejection(loaded, verdict.platform_text)
    logger.error("提交后失败，转 needs_human：%s", note)
    return PublishOutcome(ok=False, reason=note, failure_kind=verdict.kind)


def _residue_probe(adapter: Any, page: Any) -> str:
    """N5 / UT-12-07：提交后失败，查草稿箱有没有留下半成品。

    有残留 → failed_residue 标记（日报必须报：那是要人工删的半成品）；
    检查本身失败 → 明说「没查成」，不假装查过 —— 假装查过的后果是
    半成品留在草稿箱里，下次被人手滑发出去。
    """
    try:
        probe = adapter.verify(page, mode=SubmitMode.DRAFT)
    except Exception as exc:  # noqa: BLE001 - 残留检查失败不许掩盖原始失败
        return f"残留检查未完成（{type(exc).__name__}）：请人工核对草稿箱"
    if probe.ok:
        return f"failed_residue：草稿箱有残留（article_id={probe.article_id}），需人工删除"
    return "草稿箱无残留"


def _writeback_rejection(loaded: LoadedDraft, platform_text: str) -> Path | None:
    """UT-12-03：平台拒稿原话回写进稿件目录（platform-rejection.txt）。

    不写 selfcheck.json：那是 guard（R4 自检）的产物与格式，发布层覆写它
    等于越界改别人的文件。单独一个文件，改稿的人打开稿件目录抬头就看见。
    """
    if loaded.source_dir is None or not platform_text:
        return None
    target = loaded.source_dir / "platform-rejection.txt"
    target.write_text(platform_text + "\n", encoding="utf-8")
    logger.info("拒稿原文已回写：%s", target)
    return target


def _run_dir(data_dir: str | Path, clock: Clock) -> Path:
    """本次运行的存证目录：data/runs/<ts>/。时间只走注入时钟（UT-10-04）。"""
    return Path(data_dir) / "runs" / clock.now().strftime("%Y%m%d_%H%M%S")


# --------------------------------------------------------------- 幂等（T-11）

# 发布前还拿不到 URL，account 无从解析 —— 提交前的日志行统一用 default，
# 与 _account_from_url 的兜底口径一致。预检匹配靠「按行内 account 重算键」，
# 所以提交前后两种 account 写出的行都能被同一篇稿子命中。
_PRE_PUBLISH_ACCOUNT = "default"


def _find_prior(log: PublishLog, draft: Draft, platform: str) -> LogEntry | None:
    """同一幂等键的最后一行历史。

    日志行里没有 content_sha 列，而发布前 account 未知，所以不能正着算一个键
    去比对 —— 反过来走：对同平台同 slug 的每一行，用**那一行的 account**
    重算幂等键，相等才算同一篇（slug 相同但正文改过的，键不同，允许重发）。
    """
    latest: LogEntry | None = None
    for entry in log.entries():
        if entry.platform.strip().lower() != platform:
            continue
        if entry.slug != draft.meta.slug:
            continue
        if entry.idem_key == draft.idem_key(platform, entry.account):
            latest = entry
    return latest


def _idem_decision(
    log: PublishLog,
    draft: Draft,
    platform_cfg: PlatformConfig,
    prior: LogEntry | None,
    clock: Clock,
) -> PublishOutcome | None:
    """开浏览器之前的裁决：返回 None = 放行；否则这次运行到此为止。

    - published            → SKIPPED_DUPLICATE（终态，任何方向都转不出去）
    - submitting + 有 URL  → 其实已经发出去了：补记 published，按重复跳过
    - submitting + 无 URL  → 提交结果未知：状态迁移 needs_human 落盘，拒发
    - needs_human          → 人工没处理完，拒发（它只能由人转回 pending）
    - failed               → 放行重试；但累计 failed 达 RETRY_HARD_CAP →
                             强制 needs_human（UT-12-05 全局上限，不再自动放行）
    - rejected             → 自检未过的稿子不许自动复活（PRD R4）
    """
    if prior is None:
        return None

    recovered = recover_status(prior.status, prior.url)
    if recovered is Status.PUBLISHED:
        if prior.status is Status.SUBMITTING:
            # 死在提交瞬间但其实已发出：补一行 published 收敛日志，
            # 下次预检直接命中终态，不用每次重算恢复逻辑。
            _append_state(
                log, draft, platform_cfg, Status.PUBLISHED, clock,
                prior=prior, url=prior.url, article_id=prior.article_id,
                note="崩溃恢复：submitting 留有 URL，补记 published",
            )
        logger.info("幂等命中：%s 已发布过（%s），跳过", draft.meta.slug, prior.url or "无 URL")
        return PublishOutcome(
            ok=True,
            url=prior.url,
            article_id=prior.article_id,
            reason="SKIPPED_DUPLICATE",
            skipped_duplicate=True,
        )

    if recovered is Status.NEEDS_HUMAN:
        if prior.status is Status.SUBMITTING:
            # 迁移落盘：submitting → needs_human。不写的话每次重跑都停在
            # 「恢复中」，日报读到的永远是 submitting 这个中间态。
            _append_state(
                log, draft, platform_cfg, Status.NEEDS_HUMAN, clock,
                prior=prior,
                note="崩溃恢复：提交结果未知（无 URL），转人工核对平台后台",
            )
        return PublishOutcome(
            ok=False,
            reason=(
                f"needs_human：上次运行停在 {prior.status}，未确认是否发出。"
                "请到平台后台核对；确认未发出后人工把状态改回 pending，本引擎绝不自动重发"
            ),
        )

    if recovered is Status.FAILED:
        # UT-12-05 全局上限：同一篇累计 failed 到顶就强制停。失败原因各自不同，
        # 但「这篇就是发不出去」这个事实已经够明确了——再放行就是拿账号去撞墙。
        failures = _count_failures(log, draft, platform_cfg.key)
        if failures >= RETRY_HARD_CAP:
            _append_state(
                log, draft, platform_cfg, Status.NEEDS_HUMAN, clock,
                prior=prior,
                note=f"重试达全局上限（累计 failed {failures} 次 ≥ {RETRY_HARD_CAP}）：强制停止，转人工排查",
            )
            return PublishOutcome(
                ok=False,
                reason=(
                    f"needs_human：{draft.meta.slug} 累计失败 {failures} 次，"
                    f"达全局上限 {RETRY_HARD_CAP}，不再自动重试；"
                    "排查后请人工把状态改回 pending"
                ),
            )
        logger.info(
            "上次运行 failed（submit 之前），本次重试（历史 failed %d 次）：%s",
            failures, draft.meta.slug,
        )
        return None

    # rejected：R4 自检拦下的稿子，改稿后必须重新自检才能回队列（T-16 串联）。
    # 在那之前这里不放行 —— 自动重发被拒稿等于顶风作案。
    return PublishOutcome(
        ok=False,
        reason=f"rejected：{draft.meta.slug} 自检未过，改稿并重新自检前不发布",
    )


def _count_failures(log: PublishLog, draft: Draft, platform: str) -> int:
    """同一幂等键累计的 failed 行数（跨运行）。

    匹配口径与 `_find_prior` 一致：对同平台同 slug 的行按行内 account 重算键。
    只数 failed —— 它是「 submit 之前死的、可以再来」的唯一可重试状态，
    其余状态各有自己的处置分支，不该混进这个计数。
    """
    total = 0
    for entry in log.entries():
        if entry.platform.strip().lower() != platform:
            continue
        if entry.slug != draft.meta.slug:
            continue
        if entry.idem_key != draft.idem_key(platform, entry.account):
            continue
        if entry.status is Status.FAILED:
            total += 1
    return total


def _append_state(
    log: PublishLog,
    draft: Draft,
    platform_cfg: PlatformConfig,
    status: Status,
    clock: Clock,
    *,
    prior: LogEntry | None,
    url: str = "",
    article_id: str = "",
    note: str = "",
) -> None:
    """写一行状态迁移。先过状态机校验（transition），非法迁移在这里就炸，
    不许烂在 csv 里等日报读到一行讲不通的状态。"""
    account = prior.account if prior is not None else _PRE_PUBLISH_ACCOUNT
    transition(prior.status if prior is not None else Status.PENDING, status)
    log.append(
        LogEntry(
            ts=clock.now(),
            platform=platform_cfg.key,
            account=account,
            slug=draft.meta.slug,
            idem_key=draft.idem_key(platform_cfg.key, account),
            status=status,
            url=url,
            article_id=article_id,
            note=note,
        )
    )


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
