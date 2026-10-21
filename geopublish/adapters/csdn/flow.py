"""CSDN 适配器。

已实现：`check_auth` / `open_editor`（T-05）、`fill_content`（T-07 三级降级注入）。

其余四个方法**故意**抛 `NotImplementedError` 并写明归属工单：
写 `pass` 是最危险的做法——流程会一路绿到「发布成功」，然后线上什么都没有。
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from ..base import (
    AdapterMeta,
    AuthResult,
    AuthState,
    InjectAttempt,
    InjectFailed,
    Page,
    Selectors,
    SubmitResult,
    VerifyResult,
)
from ...core.clock import Clock, SystemClock
from ...core.models import Draft

__all__ = [
    "CsdnAdapter",
    "EditorReadError",
    "META_PATH",
    "SEGMENT_SIZE",
    "SELECTORS_PATH",
    "read_editor_text",
]

logger = logging.getLogger("geopublish.csdn")

# 回读长度偏差超过 2% 即判该级失败（架构 §3.4：不验证就提交 = 盲发）。
LENGTH_TOLERANCE = 0.02
SEGMENT_SIZE = 500
TYPING_DELAY_MS = 5.0

# 选择器一律经参数传入（[sel, text] / [sel, editableSel]），代码里不出现裸选择器——
# .precommit/check_no_naked_selectors.py 盯着这个目录。
_PASTE_JS = """/* op=paste */
([sel, text]) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "正文编辑区元素不存在（content_editable 未命中）"};
  el.focus();
  const dt = new DataTransfer();
  dt.setData("text/plain", text);
  el.dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true}));
  return {ok: true};
}
"""

_DISPATCH_JS = """/* op=dispatch */
([sel, text]) => {
  const host = document.querySelector(sel);
  if (!host) return {ok: false, reason: "编辑器容器不存在（content 未命中）"};
  if (host.CodeMirror) { host.CodeMirror.getDoc().setValue(text); return {ok: true, mode: "cm5"}; }
  const view = host.cmView && host.cmView.view;
  if (view) {
    view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: text}});
    return {ok: true, mode: "cm6"};
  }
  return {ok: false, reason: "拿不到 CodeMirror 实例：CodeMirror/cmView 两条挂载路径都变了"};
}
"""

_READBACK_JS = """/* op=readback */
([sel, editableSel]) => {
  const host = document.querySelector(sel);
  if (host && host.CodeMirror) return {ok: true, text: host.CodeMirror.getDoc().getValue()};
  const view = host && host.cmView && host.cmView.view;
  if (view) return {ok: true, text: view.state.doc.toString()};
  const ed = document.querySelector(editableSel);
  if (ed) return {ok: true, text: ed.innerText || ""};
  return {ok: false, reason: "回读失败：容器与编辑区都未命中"};
}
"""

_CLEAR_JS = """/* op=clear */
([sel, editableSel]) => {
  const host = document.querySelector(sel);
  if (host && host.CodeMirror) { host.CodeMirror.getDoc().setValue(""); return {ok: true}; }
  const view = host && host.cmView && host.cmView.view;
  if (view) { view.dispatch({changes: {from: 0, to: view.state.doc.length, insert: ""}}); return {ok: true}; }
  const ed = document.querySelector(editableSel);
  if (ed) { ed.innerText = ""; return {ok: true}; }
  return {ok: false, reason: "清场失败：容器与编辑区都未命中"};
}
"""


class EditorReadError(RuntimeError):
    """注入后回读不到编辑器内容。读不出来就不能当成功。"""


def read_editor_text(page: Page, content_sel: str, editable_sel: str) -> str:
    """从 CodeMirror 实例回读正文；实例拿不到时退回编辑区 innerText。"""
    outcome = page.evaluate(_READBACK_JS, [content_sel, editable_sel])
    if not isinstance(outcome, Mapping) or not outcome.get("ok"):
        reason = outcome.get("reason") if isinstance(outcome, Mapping) else "回读脚本无返回"
        raise EditorReadError(f"编辑器回读失败：{reason}")
    text = outcome.get("text")
    if not isinstance(text, str):
        raise EditorReadError("回读结果里没有文本字段")
    return text


def _type_in_segments(page: Page, content_sel: str, editable_sel: str, text: str) -> dict[str, Any]:
    """L3 兜底：聚焦 → 尽力清场 → 分段逐字键入。

    清场是**尽力而为**：L1/L2 可能已写入半截内容，追加式键入叠上去就是双倍正文；
    但清场失败也不许在这里炸——清不掉就靠注入后的回读拦截（长度对不上 = 该级失败）。
    段间不插 sleep（确定性三约束）：节奏由 press_sequentially 的逐键延迟承担。
    """
    page.click(editable_sel)
    try:
        page.evaluate(_CLEAR_JS, [content_sel, editable_sel])
    except Exception:
        pass
    for offset in range(0, len(text), SEGMENT_SIZE):
        page.press_sequentially(
            editable_sel, text[offset : offset + SEGMENT_SIZE], delay_ms=TYPING_DELAY_MS
        )
    return {"ok": True}

PACKAGE_DIR = Path(__file__).resolve().parent
SELECTORS_PATH = PACKAGE_DIR / "selectors.json"
META_PATH = PACKAGE_DIR / "meta.json"


class CsdnAdapter:
    """CSDN 创作者中心。选择器全部来自 `selectors.json`，代码里不出现裸选择器。"""

    name = "csdn"

    def __init__(
        self,
        *,
        selectors: Selectors | None = None,
        selectors_path: str | Path | None = None,
        meta_path: str | Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        # selectors_path 只为测试/引用另一份表而存在；默认必须是包内那份，
        # 否则「改版只改 JSON」在打包后就会失效。
        self.selectors_path = Path(selectors_path) if selectors_path else SELECTORS_PATH
        self._selectors = selectors if selectors is not None else Selectors.load(self.selectors_path)
        self._meta = AdapterMeta.load(Path(meta_path) if meta_path else META_PATH)
        # 注入超时判定走注入时钟（确定性三约束：时间可注入），测试用 FakeClock。
        self._clock = clock if clock is not None else SystemClock()

    # ----------------------------------------------------------- 只读属性

    @property
    def selectors_version(self) -> str:
        return self._selectors.version

    @property
    def editor_url(self) -> str:
        return self._meta.editor_url

    @property
    def login_url(self) -> str:
        """登录页。`geopublish login` 先把人放到这一页再等扫码。"""
        return self._meta.login_url

    @property
    def domains(self) -> tuple[str, ...]:
        return self._meta.domains

    @property
    def selectors_book(self) -> Selectors:
        """整本选择器表。契约测试要逐条拿去打 DOM，所以得能整组取。"""
        return self._selectors

    def selectors(self, group: str, key: str) -> str:
        return self._selectors(group, key)

    # ----------------------------------------------------------- 已实现

    def check_auth(self, page: Page) -> AuthResult:
        """只读判断，不做登录动作，不重试。

        判定顺序有讲究，三条都有事故背景：

        1. **域名先看**。浏览器停在代理错误页/登录跳转中间态/别人家的标签页时，
           页面上既没有登录表单也没有头像，按「未知」往下走也还行；但如果那个页面
           恰好有个 `.user-avatar`，就会被判成已登录，然后拿着它去填空。
        2. **人机校验优先于登录表单**。风控页往往两者都在，先看登录表单就会判
           `expired`，上层于是去重试登录 —— 而反复撞登录页正是风控最敏感的信号。
        3. 最后才看好消息（头像/编辑器），而且**任一命中即可**：改版后可能只剩一个。
        """
        host = (urlsplit(page.url).hostname or "").lower()
        if host and host not in self._meta.domains:
            return AuthResult(
                state=AuthState.UNKNOWN,
                reason=f"当前页面不在 {self.name} 域名内（{host}）：先确认浏览器停在哪一页",
            )

        if page.query_selector(self.selectors("auth", "challenge")):
            return AuthResult(
                state=AuthState.UNKNOWN,
                has_challenge=True,
                reason="页面出现人机校验（验证码）：需人工完成后重跑，禁止自动重试",
            )

        if page.query_selector(self.selectors("auth", "login_form")):
            return AuthResult(
                state=AuthState.EXPIRED,
                reason="被踢回登录页：需重新授权（geopublish login csdn）",
            )

        if page.query_selector(self.selectors("auth", "avatar")) or page.query_selector(
            self.selectors("editor", "content")
        ):
            return AuthResult(state=AuthState.LOGGED_IN, reason="看到登录态标志（头像/编辑器）")

        return AuthResult(
            state=AuthState.UNKNOWN,
            reason="既没看到登录表单也没看到登录态标志（页面没渲染完 / 跳转失败）",
        )

    def open_editor(self, page: Page, draft: Draft | None) -> None:
        """导航到新建文章页，等编辑器就绪。

        顺序不能反：先 `goto` 再等选择器。反过来就是在上一页等，必然超时。
        """
        page.goto(self.editor_url)
        page.wait_for_selector(self.selectors("editor", "content"))

    def fill_content(self, page: Page, draft: Draft | None) -> None:
        """三级降级注入正文：L1 模拟粘贴 → L2 CodeMirror 实例 → L3 分段 type。

        每级注入后立刻回读比对（架构 §3.4）：长度偏差超 2% 判该级失败，自动降级，
        不许跳级。回读是唯一裁决——内容落地且回读吻合就是成功，慢只记日志。
        三级全灭抛 `InjectFailed`（带齐三级尝试记录），归档现场包是上层的事——
        适配器碰不到截图/HAR，但**绝不在这里静默**。
        """
        if draft is None:
            raise TypeError("fill_content 需要 draft（open_editor 才允许 None）")
        body = draft.body
        if not body.strip():
            # guard 漏放的空稿不能走到注入：盲发空稿比失败更糟。
            raise ValueError("正文为空，拒绝注入")
        # CodeMirror 回读统一用 \n；Windows 稿件的 \r\n 不归一的话，
        # 每个换行都被算成丢了一个字，长度比对会误判失败（假 InjectFailed）。
        body = body.replace("\r\n", "\n").replace("\r", "\n")

        content_sel = self.selectors("editor", "content")
        editable_sel = self.selectors("editor", "content_editable")
        expected_len = len(body)

        levels: tuple[tuple[str, Any], ...] = (
            ("L1-paste", lambda: page.evaluate(_PASTE_JS, [editable_sel, body])),
            ("L2-dispatch", lambda: page.evaluate(_DISPATCH_JS, [content_sel, body])),
            ("L3-type", lambda: _type_in_segments(page, content_sel, editable_sel, body)),
        )
        attempts: list[InjectAttempt] = []
        for level, inject in levels:
            if attempts:
                logger.warning(
                    "%s 失败（%s）；降级到 %s", attempts[-1].level, attempts[-1].reason, level
                )
            attempt = self._run_level(
                page,
                level=level,
                inject=inject,
                content_sel=content_sel,
                editable_sel=editable_sel,
                expected_len=expected_len,
            )
            attempts.append(attempt)
            if attempt.ok:
                logger.info(
                    "fill_content 成功：%s 注入 %d 字，回读 %d 字，耗时 %.0fms",
                    attempt.level,
                    expected_len,
                    attempt.actual_len or 0,
                    attempt.elapsed_ms,
                )
                return
        raise InjectFailed(tuple(attempts), expected_len=expected_len)

    def _run_level(
        self,
        page: Page,
        *,
        level: str,
        inject: Any,
        content_sel: str,
        editable_sel: str,
        expected_len: int,
    ) -> InjectAttempt:
        """执行一级注入并回读校验。页面层的任何异常都折算成该级失败，不外漏。"""
        started = self._clock.now()
        try:
            outcome = inject()
            if not isinstance(outcome, Mapping) or not outcome.get("ok"):
                reason = outcome.get("reason") if isinstance(outcome, Mapping) else "注入脚本无返回"
                return InjectAttempt(
                    level=level,
                    ok=False,
                    reason=str(reason or "注入被编辑器拒绝"),
                    expected_len=expected_len,
                )
            actual = read_editor_text(page, content_sel, editable_sel)
        except Exception as exc:
            return InjectAttempt(
                level=level,
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                expected_len=expected_len,
            )

        elapsed_ms = (self._clock.now() - started).total_seconds() * 1000
        actual_len = len(actual)
        if abs(actual_len - expected_len) > expected_len * LENGTH_TOLERANCE:
            return InjectAttempt(
                level=level,
                ok=False,
                reason=f"回读 {actual_len} 字与源文 {expected_len} 字偏差超 {LENGTH_TOLERANCE:.0%}",
                expected_len=expected_len,
                actual_len=actual_len,
                elapsed_ms=elapsed_ms,
            )
        return InjectAttempt(
            level=level,
            ok=True,
            expected_len=expected_len,
            actual_len=actual_len,
            elapsed_ms=elapsed_ms,
        )

    # ----------------------------------------------------------- 待实现

    def fill_meta(self, page: Page, draft: Draft | None) -> None:
        raise NotImplementedError(
            "fill_meta 未实现：标题/分类/标签/摘要/原创声明/AI 标注属于 T-08"
        )

    def upload_cover(self, page: Page, path: Path) -> None:
        raise NotImplementedError("upload_cover 未实现：封面规格与缩略图等待属于 T-08")

    def submit(self, page: Page) -> SubmitResult:
        raise NotImplementedError("submit 未实现：发布/存草稿与结果判定属于 T-09")

    def verify(self, page: Page) -> VerifyResult:
        raise NotImplementedError("verify 未实现：回读 URL 与文章 id 属于 T-09（P0 里程碑）")
