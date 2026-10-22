"""CSDN 适配器。

已实现：`check_auth` / `open_editor`（T-05）、`fill_content`（T-07 三级降级注入）、
`fill_meta` / `upload_cover`（T-08）、`submit`（T-09，仅草稿）/ `verify`（T-09）。

2026-10-21 真机校准（T-09b）：真实 CSDN 的默认编辑器是 CKEditor（正文在 iframe 里），
Markdown 编辑器在独立地址 `editor.csdn.net/md`，正文是 `pre.editor__inner` 的
contenteditable（自研 cledit，非 CodeMirror）。关键实测结论：

- 标题是双层控件：可见的 `.article-bar__title-display` 只是展示层，点它才会出现
  真实输入框 `input.article-bar__title--input`（aria-hidden）。`value=` 赋值会被
  前端模型无视，必须走 `fill`（真实输入管线）。
- 编辑器**会恢复上次未保存的内容**：注入前必须清场，否则新正文被追加到残稿后面。
- 回读只能用 `textContent`：`innerText` 会把每个空行算两次；编辑器还会自动补一个
  尾换行（实测 2846 vs 2845），所以比对时双方都先剥掉尾换行。
- 存草稿的真实成功信号是 **URL 长出 `?articleId=` 参数**（`window.articleId` 实测
  恒为 null，不要依赖它）。
- 编辑器打开时可能弹「模版库」等弹窗，不关掉会挡住一切点击（内含「确定删除该
  模板？」的预渲染确认框——本适配器**只点关闭按钮**）。

`submit(mode=publish)` **故意**抛 `NotImplementedError` 并写明归属工单 T-10：
写 `pass` 是最危险的做法——流程会一路绿到「发布成功」，然后线上什么都没有。
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlsplit

from ..base import (
    AdapterMeta,
    AuthResult,
    AuthState,
    CoverError,
    InjectAttempt,
    InjectFailed,
    MetaError,
    Page,
    Selectors,
    SubmitBlocked,
    SubmitMode,
    SubmitResult,
    SubmitState,
    VerifyResult,
)
from ..imageinfo import ImageInfoError, image_size
from ...core.clock import Clock, SystemClock
from ...core.config import PlatformConfig
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
# UT-08-04：上传后缩略图的等待上限（毫秒）。
THUMBNAIL_TIMEOUT_MS = 10_000
# 点「保存草稿」后等 URL 长出 articleId 的上限。等不到不算失败——判定权在 verify。
SUBMIT_TIMEOUT_MS = 15_000

# article_id 藏在哪儿：实测草稿地址是 editor.csdn.net/md?...&articleId=<id>，
# 详情页是 /article/details/<id>。查询参数优先，路径兜底。
_ARTICLE_ID_KEYS = ("articleId", "article_id", "articleid")
_ARTICLE_ID_PATH_RE = re.compile(r"/(?:article/details|creation/editor|editor)/(\d+)")

# 「保存草稿」链接的可见文本。它不是选择器，是用户看到的按钮文字——平台改文案时
# 这里要跟着改，契约测试管不到文案，只能靠现场包发现。
_SAVE_DRAFT_LABEL = "保存草稿"
# 内容管理页的草稿箱 tab 文案（文本里带数量，如「草稿箱(4)」，匹配用包含）。
_DRAFT_TAB_LABEL = "草稿箱"

# 选择器一律经参数传入，代码里不出现裸选择器——.precommit/check_no_naked_selectors.py
# 盯着这个目录。

# 标题模型态回读：隐藏 input 的 value 不等于模型（实测 fill() 写了 value 但存出来
# 是【无标题】），模型态展示在 display 层。
_READ_TITLE_DISPLAY_JS = """/* op=readtitle */
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "标题展示层未命中"};
  return {ok: true, text: (el.textContent || "").trim()};
}
"""

# 内容管理页默认停在「全部」tab：草稿要点「草稿箱」tab 才出来。文本匹配而不是
# 固定类名——tab 文案里带数量，类名随版本变。
_OPEN_DRAFT_TAB_JS = """/* op=drafttab */
([tabSel, label]) => {
  const tabs = Array.from(document.querySelectorAll(tabSel));
  const hit = tabs.find(t => (t.textContent || "").includes(label));
  if (!hit) return {ok: false, reason: "没找到「" + label + "」tab"};
  hit.click();
  return {ok: true};
}
"""

# 清场：全选编辑器内容并删除。编辑器会恢复上次未保存的草稿（实测），不清场的话
# 新正文会被**追加到残稿后面**——那是双倍正文事故的真实来源。
_CLEAR_JS = """/* op=clear */
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "清场失败：编辑区未命中"};
  el.focus();
  const range = document.createRange();
  range.selectNodeContents(el);
  const selection = window.getSelection();
  selection.removeAllRanges();
  selection.addRange(range);
  const deleted = document.execCommand("delete");
  return deleted ? {ok: true} : {ok: false, reason: "execCommand(delete) 被拒"};
}
"""

# L1 注入：execCommand("insertText")。它走编辑器自己的输入管线，前端模型一定同步，
# 这是真机上最稳的一档。
_INSERT_JS = """/* op=insert */
([sel, text]) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "编辑区未命中"};
  el.focus();
  return document.execCommand("insertText", false, text)
    ? {ok: true}
    : {ok: false, reason: "execCommand(insertText) 被拒"};
}
"""

# L2 注入：合成 paste 事件。真机实测可用（2846/2845 字精确落地），但依赖编辑器
# 的 paste 处理器，改版风险比 insertText 高，所以排第二。
_PASTE_JS = """/* op=paste */
([sel, text]) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "正文编辑区元素不存在（content 未命中）"};
  el.focus();
  const dt = new DataTransfer();
  dt.setData("text/plain", text);
  el.dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true}));
  return {ok: true};
}
"""

# 回读：只认 textContent。innerText 会把空行算两次（实测），拿它当裁决会假报失败。
_READBACK_JS = """/* op=readback */
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "回读失败：编辑区未命中"};
  return {ok: true, text: el.textContent || ""};
}
"""

# 关弹窗：先点可见的关闭按钮；关不掉的遮罩层（没有可见关闭按钮、或点了没消失）
# 直接从 DOM 摘掉。**摘 DOM ≠ 点任何按钮**——弹窗里预渲染的「确定删除该模板？」
# 确认框随遮罩一起消失，永远不会被执行。只点关闭按钮、只摘遮罩，绝不点「确定」。
_CLOSE_MODALS_JS = """/* op=closemodals */
([modalSel, closeSel]) => {
  const closers = Array.from(document.querySelectorAll(closeSel)).filter(b => b.offsetParent !== null);
  closers.forEach(b => b.click());
  const remaining = Array.from(document.querySelectorAll(modalSel)).filter(m => m.offsetParent !== null);
  remaining.forEach(m => m.remove());
  return {ok: true, closed: closers.length, removed: remaining.length};
}
"""

# 存草稿：点保存按钮唤出 popover，再点里面的「保存草稿」。JS 点击不挑可见性——
# el-popover 的链接在 DOM 里常驻但带 hide 类，Playwright 的可见性检查会卡死。
_SAVE_DRAFT_JS = """/* op=savedraft */
([btnSel, itemSel, label]) => {
  const btn = document.querySelector(btnSel);
  if (btn) btn.click();
  const links = Array.from(document.querySelectorAll(itemSel));
  const hit = links.find(a => (a.textContent || "").trim() === label);
  if (!hit) return {ok: false, reason: "popover 里没找到「" + label + "」"};
  hit.click();
  return {ok: true};
}
"""

# 元信息填入：直接写 value 并派发 input/change —— 这类表单受框架控制，
# 只改 value 不派发事件，页面上看着填好了，提交时状态仍是空的。
# （发布对话框的控件，选择器待 T-10 真机校准）
_SET_VALUE_JS = """/* op=setvalue */
([sel, text]) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "输入控件不存在（选择器未命中）"};
  el.focus();
  if (el.tagName === "SELECT") {
    const opts = Array.from(el.options || []);
    const hit = opts.find(o => o.value === text || (o.textContent || "").trim() === text);
    if (!hit) return {ok: false, reason: "下拉里找不到该选项：" + text};
    el.value = hit.value;
  } else {
    el.value = text;
  }
  el.dispatchEvent(new Event("input", {bubbles: true}));
  el.dispatchEvent(new Event("change", {bubbles: true}));
  return {ok: true};
}
"""

# 分类可选项必须从页面读：平台会改栏目，硬编码一份清单等于给自己埋定时炸弹。
_CATEGORY_OPTIONS_JS = """/* op=categories */
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "分类控件不存在（meta.category 未命中）"};
  const list = el.options ? Array.from(el.options) : Array.from(el.querySelectorAll("option"));
  const options = list.map(o => ({value: o.value, text: (o.textContent || "").trim()}));
  if (!options.length) return {ok: false, reason: "分类下拉没有任何可选项"};
  return {ok: true, options};
}
"""


def _article_id_from_url(url: str) -> str:
    """从地址里挖 article_id：查询参数优先，再退到路径。

    挖不到返回空串而不是 None：调用方（verify）要做的是「有没有」，空串在
    日志里也好读——`article_id=""` 比 `article_id=None` 少一次类型判断。
    """
    parts = urlsplit(str(url or ""))
    query = parse_qs(parts.query)
    for key in _ARTICLE_ID_KEYS:
        values = query.get(key)
        if values:
            # parse_qs 默认就丢掉空值，这里只需要去首尾空白
            return str(values[0]).strip()
    hit = _ARTICLE_ID_PATH_RE.search(parts.path)
    return hit.group(1) if hit else ""


def _norm_text(text: str) -> str:
    """比对前归一：编辑器会自动补一个尾换行（实测 +1 字），双方一起剥掉再比。"""
    return text.rstrip("\n")


def _squash(text: str) -> str:
    """压掉全部空白做标题比对：平台会归一标题空白（实测「 RAG 」两侧空格被吃掉），
    逐字节比会把「存成功了」判成失败。"""
    return "".join(str(text or "").split())


class EditorReadError(RuntimeError):
    """注入后回读不到编辑器内容。读不出来就不能当成功。"""


def read_editor_text(page: Page, content_sel: str, editable_sel: str) -> str:
    """回读正文：cledit 编辑器的权威文本是 `textContent`（editable_sel 留作契约形状）。

    页面层的任何异常（页面已销毁、浏览器崩了）都折算成 EditorReadError：
    调用方要回答的只有「读没读出来」，不该被迫区分浏览器的一百种死法。
    """
    try:
        outcome = page.evaluate(_READBACK_JS, content_sel)
    except Exception as exc:
        raise EditorReadError(f"编辑器回读失败：{type(exc).__name__}: {exc}") from exc
    if not isinstance(outcome, Mapping) or not outcome.get("ok"):
        reason = outcome.get("reason") if isinstance(outcome, Mapping) else "回读脚本无返回"
        raise EditorReadError(f"编辑器回读失败：{reason}")
    text = outcome.get("text")
    if not isinstance(text, str):
        raise EditorReadError("回读结果里没有文本字段")
    return text


def _clear_editor(page: Page, content_sel: str) -> None:
    """尽力清场。失败不在这里炸——注入后的回读会把残留拦成该级失败。"""
    try:
        page.evaluate(_CLEAR_JS, content_sel)
    except Exception:
        pass


def _type_text(page: Page, sel: str, text: str, delay_ms: float) -> None:
    """逐字键入的统一入口。

    Playwright 的 `press_sequentially` 在 **Locator** 上，Page 上没有——
    假页面（协议形状）有这个方法所以测试一直没暴露，真机一跑就 AttributeError
    （T-09b 正式验收抓出）。测试路径走协议方法，真机路径走 locator。
    """
    direct = getattr(page, "press_sequentially", None)
    if direct is not None:
        direct(sel, text, delay_ms=delay_ms)
        return
    page.locator(sel).press_sequentially(text, delay=delay_ms)


def _type_in_segments(page: Page, content_sel: str, text: str) -> dict[str, Any]:
    """L3 兜底：聚焦 → 清场 → 分段逐字键入。慢，但走的是真实键盘管线。"""
    page.click(content_sel)
    _clear_editor(page, content_sel)
    for offset in range(0, len(text), SEGMENT_SIZE):
        _type_text(
            page, content_sel, text[offset : offset + SEGMENT_SIZE], delay_ms=TYPING_DELAY_MS
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
        # 最近一次注入成功的正文长度。**这是提交前复核的唯一依据**（R7 红线）：
        # 记录在 adapter 上而不是让调用方再传一遍，是因为「注入了多少」只有这里知道，
        # 传参等于允许调用方和事实不一致。
        self._injected_len: int | None = None

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

    def wait_for_auth_signal(self, page: Page, timeout_ms: int = 30_000) -> None:
        """探活前等页面渲染出权威信号（编辑器 / 登录表单 / 人机校验任一）。

        真机实测（T-09b）：编辑器是 SPA，`goto` 的 load 事件**不代表渲染完成**，
        立刻 check_auth 会在半渲染页面上误判 unknown（登录态明明是好的）。
        这不是重试——是等页面到达一个**可判定**的状态；判定本身仍由 check_auth
        恰好做一次（R6 不变）。
        """
        combined = ", ".join(
            [
                self.selectors("editor", "content"),
                self.selectors("auth", "login_form"),
                self.selectors("auth", "challenge"),
            ]
        )
        page.wait_for_selector(combined, timeout_ms=timeout_ms)

    def open_editor(self, page: Page, draft: Draft | None) -> None:
        """导航到新建文章页，等编辑器就绪，然后**关掉自动弹出的弹窗**。

        顺序不能反：先 `goto` 再等选择器。反过来就是在上一页等，必然超时。
        弹窗必须关：真机实测编辑器会弹「模版库」，它拦在一切点击前面。
        """
        # 新开一页 = 上一次的注入记录作废。不在这里清，就会出现「换了稿子、
        # 复核却拿旧长度通过」——那正是 R7 要防的盲发。
        self._injected_len = None
        page.goto(self.editor_url)
        page.wait_for_selector(self.selectors("editor", "content"))
        self.close_modals(page)

    def close_modals(self, page: Page) -> None:
        """清掉挡点击的弹窗（最多 3 轮）：先点关闭按钮，点不掉的遮罩直接摘 DOM。

        实测两种形态都要管：模版库弹窗有 `.modal__close-button` 可点；另一些
        （`.modal_outer` 遮罩）按钮点了不消失，Playwright 点击会被它拦到超时。
        **本方法永不点「确定」**——摘 DOM 只是把遮罩层从文档里拿走，不等于执行
        弹窗里的任何动作。
        """
        args = [self.selectors("editor", "modal_root"), self.selectors("editor", "modal_close")]
        for _ in range(3):
            outcome = page.evaluate(_CLOSE_MODALS_JS, args)
            if not isinstance(outcome, Mapping):
                break
            cleared = int(outcome.get("closed", 0)) + int(outcome.get("removed", 0))
            if not cleared:
                break
            logger.info(
                "关弹窗：点关闭 %d 个，摘遮罩 %d 个",
                outcome.get("closed", 0),
                outcome.get("removed", 0),
            )

    def fill_content(self, page: Page, draft: Draft | None) -> None:
        """三级降级注入正文：L1 insertText → L2 合成 paste → L3 分段 type。

        真机校准后的降级链（T-09b）：L1 走编辑器自己的输入管线，最稳；L2 实测可用
        但依赖 paste 处理器；L3 真实键盘，慢但几乎不会失败。
        每级注入后立刻回读比对（架构 §3.4）：长度偏差超 2% 判该级失败，自动降级，
        不许跳级。回读是唯一裁决——内容落地且回读吻合就是成功，慢只记日志。
        三级全灭抛 `InjectFailed`（带齐三级尝试记录），归档现场包是上层的事——
        适配器碰不到截图/HAR，但**绝不在这里静默**。
        """
        if draft is None:
            raise TypeError("fill_content 需要 draft（open_editor 才允许 None）")
        # 先作废上一次的记录：这次注入若失败，submit 必须因为「没有记录」被拦下，
        # 而不是拿着上一次的长度放行。
        self._injected_len = None
        body = draft.body
        if not body.strip():
            # guard 漏放的空稿不能走到注入：盲发空稿比失败更糟。
            raise ValueError("正文为空，拒绝注入")
        body = body.replace("\r\n", "\n").replace("\r", "\n")

        content_sel = self.selectors("editor", "content")
        expected_len = len(_norm_text(body))

        # 编辑器会恢复上次未保存的内容（实测）：先清场，否则正文被追加到残稿后面。
        _clear_editor(page, content_sel)

        levels: tuple[tuple[str, Any], ...] = (
            ("L1-insert", lambda: page.evaluate(_INSERT_JS, [content_sel, body])),
            ("L2-paste", lambda: page.evaluate(_PASTE_JS, [content_sel, body])),
            ("L3-type", lambda: _type_in_segments(page, content_sel, body)),
        )
        attempts: list[InjectAttempt] = []
        for level, inject in levels:
            if attempts:
                logger.warning(
                    "%s 失败（%s）；降级到 %s", attempts[-1].level, attempts[-1].reason, level
                )
                # 上一级可能写了半截：降级前重新清场，否则残留 + 新内容 = 双倍正文
                _clear_editor(page, content_sel)
            attempt = self._run_level(
                page,
                level=level,
                inject=inject,
                content_sel=content_sel,
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
                # 只在这里记：只有「注入并回读通过」的长度才有资格作为提交前基准。
                self._injected_len = expected_len
                return
        raise InjectFailed(tuple(attempts), expected_len=expected_len)

    def _run_level(
        self,
        page: Page,
        *,
        level: str,
        inject: Any,
        content_sel: str,
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
            actual = read_editor_text(page, content_sel, content_sel)
        except Exception as exc:
            return InjectAttempt(
                level=level,
                ok=False,
                reason=f"{type(exc).__name__}: {exc}",
                expected_len=expected_len,
            )

        elapsed_ms = (self._clock.now() - started).total_seconds() * 1000
        actual_len = len(_norm_text(actual))
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

    # ----------------------------------------------------------- T-08/T-09b 元信息

    def fill_meta(
        self,
        page: Page,
        draft: Draft | None,
        limits: PlatformConfig,
        *,
        mode: SubmitMode | str = SubmitMode.DRAFT,
    ) -> None:
        """填标题（草稿模式）；分类/标签/摘要/AI 声明只在发布模式填。

        真机校准结论（T-09b）：CSDN 的分类/标签/摘要/声明控件**不在编辑器页面上**，
        它们在点「发布文章」后弹出的对话框里。草稿模式只填标题——把不存在的控件
        当成失败会挡住整条草稿链路，而它们对「存草稿」本来就不是必填。

        - 标题超上限**截断并告警**（UT-08-01）：静默丢弃会发出一个读者看不懂的标题。
        - 标题控件是双层的：先点 display 展示层，真实 input 才出现，再 fill。
          `value=` 赋值会被前端模型无视（实测），只能走真实输入管线。
        - AI 参与度为空 → `MetaError`。guard（T-03）保证非空，这里是不变的第二道防线。
        - **不点原创声明**：那是账号级风险决策，交给人工或后续工单，不在这层偷偷勾。
        """
        if draft is None:
            raise TypeError("fill_meta 需要 draft（open_editor 才允许 None）")
        if limits is None:
            raise TypeError("fill_meta 需要 limits（PlatformConfig）：上限只认 config 一份真相")
        mode = SubmitMode(mode)
        meta = draft.meta

        involvement = (meta.ai_involvement or "").strip()
        if not involvement:
            raise MetaError(
                "AI 参与度为空：meta.ai_involvement 必须是 human/assisted/generated 之一"
            )

        title = meta.title
        if len(title) > limits.title_max:
            logger.warning(
                "标题 %d 字超平台上限 %d，已截断（原题：%s…）",
                len(title),
                limits.title_max,
                title[:12],
            )
            title = title[: limits.title_max]
        # 弹窗会在注入正文后再冒出来（实测：open_editor 关过一轮，fill 完又被挡住）。
        # 点标题 display 之前必须再关一轮，否则 Playwright 卡到超时。
        self.close_modals(page)
        self._fill_title(page, title)

        if mode is SubmitMode.DRAFT:
            logger.info(
                "fill_meta（草稿模式）完成：标题 %d 字；分类/标签/摘要/声明在发布对话框里（T-10）",
                len(title),
            )
            return

        # ---- 以下只在发布模式执行；控件在发布对话框里，选择器待 T-10 真机校准 ----
        if meta.category.strip():
            self._fill_category(page, meta.category.strip())

        tags_sel = self.selectors("meta", "tags")
        tags = list(meta.tags)[: limits.tags_max]
        if len(meta.tags) > limits.tags_max:
            logger.warning(
                "标签 %d 个超上限 %d，丢弃 %d 个（保留列表前 %d 个：%s）",
                len(meta.tags),
                limits.tags_max,
                len(meta.tags) - limits.tags_max,
                len(tags),
                "、".join(tags),
            )
        for tag in tags:
            _type_text(page, tags_sel, tag, delay_ms=TYPING_DELAY_MS)
            page.press(tags_sel, "Enter")

        if meta.summary.strip():
            self._set_value(page, "meta", "summary", meta.summary)

        self._set_value(page, "meta", "ai_declaration", involvement)
        logger.info(
            "fill_meta 完成：标题 %d 字、分类 %r、标签 %d 个",
            len(title),
            meta.category or "（未设置）",
            len(tags),
        )

    def _fill_title(self, page: Page, title: str) -> None:
        """双层标题控件 + 受控模型：点 display → 清空 → **真实键盘**输入 → blur → 回读。

        两个实测教训（T-09b）：
        1. `fill()` 只写 input 的 value，模型不吃——存出来的草稿是【无标题】。
           唯一被模型承认的输入是真实键盘事件（press / press_sequentially）。
        2. 回读要看 display 展示层（模型态），不是 input 的 value。
        """
        display_sel = self.selectors("editor", "title_display")
        input_sel = self.selectors("editor", "title")
        try:
            page.click(display_sel)
            page.press(input_sel, "Meta+A")
            page.press(input_sel, "Backspace")
            _type_text(page, input_sel, title, delay_ms=TYPING_DELAY_MS)
            page.click(self.selectors("editor", "content"))
        except Exception as exc:
            raise MetaError(f"填不进 editor.title：{type(exc).__name__}: {exc}") from exc

        outcome = page.evaluate(_READ_TITLE_DISPLAY_JS, display_sel)
        actual = outcome.get("text", "") if isinstance(outcome, Mapping) else ""
        if _squash(actual) != _squash(title):
            raise MetaError(
                f"标题回读不符：写入 {title!r}，模型态是 {actual!r}"
                "（平台会归一空白，比对已忽略空白）"
            )

    def _set_value(self, page: Page, group: str, key: str, text: str) -> None:
        """往输入控件写值并派发事件。控件不存在 → MetaError（改版要当场暴露）。"""
        sel = self.selectors(group, key)
        outcome = page.evaluate(_SET_VALUE_JS, [sel, text])
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "页面脚本无返回"
            raise MetaError(f"填不进 {group}.{key}: {reason}")

    def _fill_category(self, page: Page, category: str) -> None:
        """分类必须命中页面上的真实可选项（UT-08-03）。"""
        sel = self.selectors("meta", "category")
        outcome = page.evaluate(_CATEGORY_OPTIONS_JS, [sel])
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "页面脚本无返回"
            raise MetaError(f"读不到分类可选项：{reason}")
        options = outcome["options"]
        if not isinstance(options, list) or not options:
            raise MetaError("分类可选项为空：页面没渲染完或控件改版")
        texts = [str(o.get("text") or "") for o in options if isinstance(o, Mapping)]
        matched = [
            o for o in options
            if isinstance(o, Mapping)
            and (o.get("text") == category or o.get("value") == category)
        ]
        if not matched:
            raise MetaError(
                f"分类「{category}」不在 {self.name} 的可选项里，可选项有：{'、'.join(texts)}"
            )
        self._set_value(page, "meta", "category", category)

    # ----------------------------------------------------------- T-08 封面

    def upload_cover(self, page: Page, path: Path) -> None:
        """上传封面并等缩略图。

        三道本地预检**全部发生在 set_input_files 之前**（UT-08-05）：
        文件在不在、尺寸对不对、大小超没超。传上去才发现不对，
        后台就多了一张要人工清理的垃圾封面。
        规格只认 meta.json 的 cover 字段（PRD：尺寸从平台读取，不得硬编码）。

        注意：真实 CSDN 的封面上传框在**发布对话框**里（T-10 校准），
        草稿模式不该走到这里——publish.py 会跳过。
        """
        path = Path(path)
        if not path.is_file():
            raise CoverError(f"封面文件不存在：{path}")

        spec = self._meta.cover
        if spec is None:
            raise CoverError(
                f"meta.json 未提供 cover 规格（width/height/max_bytes）：没有规格就不上传"
            )
        try:
            width, height = image_size(path)
        except ImageInfoError as exc:
            raise CoverError(f"封面识别失败，拒绝上传：{exc}") from exc
        if (width, height) != (spec.width, spec.height):
            raise CoverError(
                f"封面尺寸 {width}x{height} 与 {self.name} 要求 {spec.width}x{spec.height} "
                f"不符：本地拒绝，未上传"
            )
        size = path.stat().st_size
        if size > spec.max_bytes:
            raise CoverError(
                f"封面 {size} 字节超过上限 {spec.max_bytes} 字节：本地拒绝，未上传"
            )

        try:
            page.set_input_files(self.selectors("cover", "upload_input"), path)
        except Exception as exc:
            raise CoverError(f"封面塞不进上传框（cover.upload_input）：{exc}") from exc
        try:
            page.wait_for_selector(
                self.selectors("cover", "thumbnail"), timeout_ms=THUMBNAIL_TIMEOUT_MS
            )
        except Exception as exc:
            raise CoverError(
                f"封面上传后缩略图 {THUMBNAIL_TIMEOUT_MS // 1000}s 内未出现：{exc}"
            ) from exc
        logger.info("封面已上传：%s（%dx%d，%d 字节）", path.name, width, height, size)

    # ----------------------------------------------------------- T-09 提交

    def check_content_length(self, page: Page) -> int:
        """提交前复核：编辑器里的正文必须还是注入时那一份（R7 红线）。

        为什么单独成一个方法而不是顺手写在 submit 里：这是整条链上唯一一处
        「用平台侧的现状去否定本地的乐观」，出错时要能单独测、单独在日志里留痕。
        返回实际字数，便于把「复核过多少字」写进日志。

        没有注入记录 = 不知道页面上是什么 → 直接 `SubmitBlocked`。宁可让人重跑，
        也不发一篇内容不明的稿子。
        """
        expected = self._injected_len
        if expected is None:
            raise SubmitBlocked(
                "没有正文注入长度记录：未经验证的正文不许提交（先跑 fill_content，"
                "或在 open_editor 之后重新注入）"
            )
        try:
            actual = len(
                _norm_text(
                    read_editor_text(
                        page,
                        self.selectors("editor", "content"),
                        self.selectors("editor", "content_editable"),
                    )
                )
            )
        except EditorReadError as exc:
            raise SubmitBlocked(f"提交前复核失败，读不回正文：{exc}") from exc

        if abs(actual - expected) > expected * LENGTH_TOLERANCE:
            raise SubmitBlocked(
                f"提交前复核失败：编辑器里 {actual} 字，注入时 {expected} 字，"
                f"偏差超 {LENGTH_TOLERANCE:.0%}。已停手，一个按钮都没点"
            )
        logger.info("提交前复核通过：编辑器 %d 字与注入时一致", actual)
        return actual

    def submit(self, page: Page, *, mode: SubmitMode | str = SubmitMode.DRAFT) -> SubmitResult:
        """存草稿（T-09）。真实发布属于 T-10，这里**明确拒绝**而不是半套实现。

        顺序是硬的：复核正文 → 关弹窗 → 点保存。弹窗不关的话点击会被拦截
        （真机实测：模版库弹窗挡在按钮前面，Playwright 卡到超时）。
        """
        mode = SubmitMode(mode)
        if mode is not SubmitMode.DRAFT:
            raise NotImplementedError("publish 模式未实现：真实发布与回执属于 T-10")

        self.check_content_length(page)
        self.close_modals(page)

        before_url = page.url
        outcome = page.evaluate(
            _SAVE_DRAFT_JS,
            [
                self.selectors("editor", "save_draft_button"),
                self.selectors("editor", "save_draft_item"),
                _SAVE_DRAFT_LABEL,
            ],
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "保存脚本无返回"
            return SubmitResult(
                state=SubmitState.UNKNOWN,
                reason=f"「{_SAVE_DRAFT_LABEL}」没点成（{reason}）：交给 verify 判定",
            )

        # 真机实测的成功信号：URL 长出 articleId 参数
        try:
            page.wait_for_url("**articleId=*", timeout_ms=SUBMIT_TIMEOUT_MS)
        except Exception as exc:
            if page.url != before_url:
                return SubmitResult(
                    state=SubmitState.SUBMITTED, reason=f"点击后页面已变化：{page.url}"
                )
            return SubmitResult(
                state=SubmitState.UNKNOWN,
                reason=(
                    f"点击后 {SUBMIT_TIMEOUT_MS // 1000}s 内 URL 没长出 articleId"
                    f"（{type(exc).__name__}）：交给 verify 判定"
                ),
            )
        return SubmitResult(
            state=SubmitState.SUBMITTED, reason=f"URL 已带 articleId：{page.url}"
        )

    def verify(self, page: Page, *, mode: SubmitMode | str = SubmitMode.DRAFT) -> VerifyResult:
        """回读确认：拿到 article_id 才算成功（UT-09-01）。

        三条取 id 的路按代价从低到高（真机校准后，第 2 条是实测主路径）：
        1. DOM 里的 `verify.article_id`（发布成功页 / 带 id 的编辑器页）
        2. 当前 URL：`?articleId=`（实测主路径）或 `/creation/editor/<id>`、`/article/details/<id>`
        3. 草稿列表页按标题核对（UT-09-02）—— 存草稿后 URL 不变时唯一能确认的办法；
           列表行结构尚未真机校准，找不到时会明确判失败而不是猜成功

        **有成功提示但没有 id 一律判失败**：那正是「以为发出去了、其实列表里没有」
        的形态，比明确的报错危险得多。
        """
        mode = SubmitMode(mode)
        url = page.url
        article_id = self._read_article_id(page) or _article_id_from_url(url)
        if article_id:
            return VerifyResult(
                ok=True, url=url, article_id=article_id, reason="从页面标记或 URL 读到 article_id"
            )
        if mode is not SubmitMode.DRAFT:
            # 已发布的文章不在草稿箱里，拿草稿列表兜底只会得到一个假阴性
            return VerifyResult(
                ok=False, url=url, reason="发布模式下拿不到 article_id：不走草稿列表兜底"
            )
        return self._verify_via_draft_list(page)

    def _read_article_id(self, page: Page) -> str:
        """读 DOM 里的文章 id。元素在但没 content 就退回文本内容，都没有算没读到。"""
        element = page.query_selector(self.selectors("verify", "article_id"))
        if element is None:
            return ""
        raw = element.get_attribute("content") or element.text_content() or ""
        return raw.strip()

    def _verify_via_draft_list(self, page: Page) -> VerifyResult:
        """草稿列表兜底：按「编辑器里此刻的标题」找同一篇。

        标题必须在**导航之前**从编辑器读出来：一跳页，标题输入框就没了，
        而平台不会把标题回填到列表页以外的任何地方。
        """
        title = self._read_title(page)
        if not title:
            return VerifyResult(
                ok=False, url=page.url, reason="读不到编辑器标题：没法在草稿列表里核对是哪一篇"
            )

        list_url = self._meta.draft_list_url
        if not list_url:
            return VerifyResult(
                ok=False,
                url=page.url,
                reason=f"meta.json 没配 draft_list_url：无法确认「{title}」是否已进草稿箱",
            )

        try:
            page.goto(list_url)
            # 默认停在「全部」tab：先点「草稿箱」，再等异步加载圈消失（T-09b 实测：
            # 不等的话读到的是空列表，会把「存成了」误判成失败）。
            tab = page.evaluate(
                _OPEN_DRAFT_TAB_JS,
                [self.selectors("verify", "draft_list_tab"), _DRAFT_TAB_LABEL],
            )
            if isinstance(tab, Mapping) and not tab.get("ok"):
                return VerifyResult(
                    ok=False, url=list_url,
                    reason=f"草稿箱 tab 点不开（{tab.get('reason')}）：无法核对「{title}」",
                )
            page.wait_for_hidden(
                self.selectors("verify", "draft_list_loading"), timeout_ms=20_000
            )
            items = page.query_selector_all(self.selectors("verify", "draft_list_item"))
        except Exception as exc:
            return VerifyResult(
                ok=False,
                url=page.url,
                reason=f"草稿列表页打不开（{type(exc).__name__}）：{exc}",
            )

        wanted = _squash(title)
        href_without_id = ""
        for item in items:
            if _squash(item.text_content()) != wanted:
                continue
            href = item.get_attribute("href") or ""
            article_id = _article_id_from_url(href)
            if not article_id:
                href_without_id = href or "（无 href）"
                continue
            return VerifyResult(
                ok=True,
                url=urljoin(list_url, href),
                article_id=article_id,
                reason=f"草稿列表页按标题命中（共 {len(items)} 条）",
            )

        if href_without_id:
            # 标题对上了、链接里却解析不出 id：这就是「看着像发成了」的样子，
            # 但下游拿不到 id 就没法回执、没法监测，判失败比判成功安全。
            return VerifyResult(
                ok=False,
                url=list_url,
                reason=f"草稿列表里找到「{title}」，但链接解析不出 article_id：{href_without_id}",
            )
        return VerifyResult(
            ok=False,
            url=list_url,
            reason=f"草稿列表页（{len(items)} 条）里没有标题「{title}」：草稿大概率没存上",
        )

    def _read_title(self, page: Page) -> str:
        """读编辑器标题。成功页没有标题框，读不到是正常态，不算错误。"""
        try:
            return (page.input_value(self.selectors("editor", "title")) or "").strip()
        except Exception:
            return ""
