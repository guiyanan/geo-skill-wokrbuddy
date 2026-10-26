"""CSDN 适配器。

已实现：`check_auth` / `open_editor`（T-05）、`fill_content`（T-07 三级降级注入）、
`fill_meta` / `upload_cover`（T-08）、`submit`（T-09 草稿 + T-10 发布）/ `verify`（T-09/T-10）。

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

2026-10-22 真机校准（T-10，发布对话框，原料 data/l4/t10-dialog-dom.html）：

- 对话框根是 `.modal__publish-article`；最终发布按钮是 footer 里**唯一**的
  `button.btn-b-red`，取消是第一个 `.btn-b-normal`。
- 标签是 el-autocomplete：点「添加文章标签」展开 `.mark_selection_box`，
  在其 input 里键入 + Enter 收一个标签。
- 创作声明（AI 参与度）是 el-select 四档，没有独立「AI 生成」档（generated
  落到「部分内容由AI辅助生成」并 WARNING）。
- 分类专栏是点击展开控件；**测试账号没有任何专栏**，有专栏账号的展开形态未校准。
- 封面链：`input.el-upload__input` 塞文件 → `.vicp-wrap` 裁剪编辑器 →
  `div.vicp-operate-btn`「确认上传」→ 异步上传完成信号是 `.preview-box.hasImg`。
  已有封面时上传框从 DOM 消失，要先点 `.btn-remove-coverimg` 删掉再传。
- 发布模式下 verify 的裁决变了：编辑器 URL 的 `?articleId=` 是草稿信号，
  发布只认详情页 URL 或页面上的详情链接（`verify.published_link`）。
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
    ContentRejected,
    CoverError,
    CoverSpec,
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
# 标题框的键入延迟要比正文大：实测 5ms 时模型只吃到前两个字（它的受控输入
# 跟不上），15ms 才稳定（T-09b 正式验收抓出）。
TITLE_TYPING_DELAY_MS = 15.0
# UT-08-04：上传后缩略图的等待上限（毫秒）。
THUMBNAIL_TIMEOUT_MS = 10_000
# 封面「确认上传」后服务端异步处理，完成信号是 .preview-box 长出 .hasImg（T-10 实测）。
COVER_UPLOAD_TIMEOUT_MS = 30_000
# 点「保存草稿」后等 URL 长出 articleId 的上限。等不到不算失败——判定权在 verify。
SUBMIT_TIMEOUT_MS = 15_000
# 点最终「发布文章」后等跳详情页的上限。等不到不算失败——判定权同样在 verify。
PUBLISH_TIMEOUT_MS = 20_000
# 发布对话框打开的上限（点开是异步渲染）。
DIALOG_OPEN_TIMEOUT_MS = 10_000

# article_id 藏在哪儿：实测草稿地址是 editor.csdn.net/md?...&articleId=<id>，
# 详情页是 /article/details/<id>。查询参数优先，路径兜底。
_ARTICLE_ID_KEYS = ("articleId", "article_id", "articleid")
_ARTICLE_ID_PATH_RE = re.compile(r"/(?:article/details|creation/editor|editor)/(\d+)")

# 「保存草稿」链接的可见文本。它不是选择器，是用户看到的按钮文字——平台改文案时
# 这里要跟着改，契约测试管不到文案，只能靠现场包发现。
_SAVE_DRAFT_LABEL = "保存草稿"
# 内容管理页的草稿箱 tab 文案（文本里带数量，如「草稿箱(4)」，匹配用包含）。
_DRAFT_TAB_LABEL = "草稿箱"

# 创作声明（AI 参与度）的落档映射（T-10 真机校准）：平台的下拉只有四档
# 「无声明 / 部分内容由AI辅助生成 / 内容来源网络，进行整合/再创作 / 个人观点，仅供参考」，
# 没有独立的「AI 生成」档——generated 落到最近的 AI 档并 WARNING，不静默。
_AI_DECLARATION_TEXT: Mapping[str, str] = {
    "human": "无声明",
    "assisted": "部分内容由AI辅助生成",
    "generated": "部分内容由AI辅助生成",
}

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

# 关弹窗：先点可见的关闭按钮；然后把遮罩层整个从 DOM 摘掉。
# **摘 DOM ≠ 点任何按钮**——弹窗里预渲染的「确定删除该模板？」确认框随遮罩一起
# 消失，永远不会被执行。只点关闭按钮、只摘遮罩，绝不点「确定」。
# 两个实测坑：① 弹窗是 position:fixed，offsetParent 恒 null，不能拿它当可见性；
# ② .modal 可以是 display:contents（自己没盒子、子树照拦截），所以摘除不按
# 可见性过滤——摘掉一个本就隐藏的节点没有代价，漏摘一个拦截中的节点就是超时。
_CLOSE_MODALS_JS = """/* op=closemodals */
([modalSel, closeSel]) => {
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && el.getClientRects().length > 0;
  };
  const closers = Array.from(document.querySelectorAll(closeSel)).filter(visible);
  closers.forEach(b => b.click());
  const modals = Array.from(document.querySelectorAll(modalSel));
  const removedVisible = modals.filter(visible).length;
  modals.forEach(m => m.remove());
  return {ok: true, closed: closers.length, removed: removedVisible};
}
"""

# 聚焦：Playwright 的 click 会被遮罩拦截（pointer events 检查），而 DOM focus
# 不需要指针。blur 标题 / L3 打字前聚焦都走它，不跟遮罩玩猫鼠游戏。
_FOCUS_JS = """/* op=focus */
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "聚焦目标未命中"};
  el.focus();
  return {ok: true};
}
"""

# 温和版关弹窗：只点可见的关闭按钮，**不从 DOM 摘 .modal 节点**。
# 为什么需要它（T-10 真机实测 2026-10-26）：发布对话框的挂载点本身就是一个
# .modal 容器——_CLOSE_MODALS_JS 的 remove() 会把它一起摘掉，之后点发布入口
# 对话框永远渲染不出来（10s 超时）。开对话框前只能点关闭，不能摘节点。
_CLICK_CLOSE_BUTTONS_JS = """/* op=closebuttons */
(closeSel) => {
  const visible = (el) => {
    const s = getComputedStyle(el);
    return s.display !== "none" && s.visibility !== "hidden" && el.getClientRects().length > 0;
  };
  const closers = Array.from(document.querySelectorAll(closeSel)).filter(visible);
  closers.forEach(b => b.click());
  return {ok: true, closed: closers.length};
}
"""

# 通用 JS 点击：不挑可见性、不做指针命中检查——遮罩层拦截对它无效。
# 只用于「点一下让控件出现」这类触发动作；需要真实指针语义的场合不用它。
_CLICK_JS = """/* op=jsclick */
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "点击目标未命中"};
  el.click();
  return {ok: true};
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

# ---- T-10：发布对话框（全部按 2026-10-22 真机抓取校准，原料 data/l4/t10-dialog-dom.html）

# 打开对话框：点编辑器导航栏的「发布文章」入口（button.btn-publish）。
# 对话框是 .modal 的子树——打开前必须先 close_modals 清掉欢迎弹窗，
# 但**打开之后绝不能再 close_modals**（那会把发布对话框自己从 DOM 摘掉）。
_OPEN_DIALOG_JS = """/* op=opendialog */
(btnSel) => {
  const btn = document.querySelector(btnSel);
  if (!btn) return {ok: false, reason: "发布入口按钮未命中（editor.publish_button）"};
  btn.click();
  return {ok: true};
}
"""

# 文章类型确保「原创」：radio 默认态不保证（草稿可能带历史状态），没选中就点一下。
_ENSURE_ORIGINAL_JS = """/* op=original */
(sel) => {
  const el = document.querySelector(sel);
  if (!el) return {ok: false, reason: "文章类型「原创」控件未命中（meta.original_declaration）"};
  if (!el.checked) el.click();
  return {ok: true, checked: !!el.checked};
}
"""

# 创作声明：el-select 是只读 input + 内嵌 dropdown 列表。点 input 展开，
# 按文本命中 li 并点击。选项永远先从页面读——平台改措辞时这里给的是
# 「页面实际有哪几档」，不是一份硬编码清单的幻觉。
_AI_DECLARE_JS = """/* op=aideclare */
([inputSel, optionSel, text]) => {
  const input = document.querySelector(inputSel);
  if (!input) return {ok: false, reason: "创作声明控件未命中（meta.ai_declaration）"};
  input.click();
  const items = Array.from(document.querySelectorAll(optionSel));
  const texts = items.map(i => (i.textContent || "").trim()).filter(Boolean);
  if (!texts.length) return {ok: false, reason: "创作声明下拉没有任何选项"};
  const hit = items.find(i => (i.textContent || "").trim() === text);
  if (!hit) return {ok: false, reason: "创作声明下拉里没有「" + text + "」", options: texts};
  hit.click();
  return {ok: true};
}
"""

# 分类专栏：点击展开的控件（不是 <select>）。展开层异步渲染、且**本账号没有任何
# 专栏**（T-10 实测：点了没弹层），所以可选项恒为空 → MetaError。有专栏账号的
# 展开列表形态未校准——拿到那样的账号后第一件事是重抓这段 DOM。
_CATEGORY_PICK_JS = """/* op=categorypick */
([boxSel, optionSel, category]) => {
  const box = document.querySelector(boxSel);
  if (!box) return {ok: false, reason: "分类专栏控件未命中（meta.category）"};
  box.click();
  const items = Array.from(document.querySelectorAll(optionSel))
    .filter(e => (e.textContent || "").trim());
  const options = items.map(e => (e.textContent || "").trim());
  const hit = items.find(e => (e.textContent || "").trim() === category);
  if (!hit) return {ok: false, reason: "可选项里没有「" + category + "」", options};
  hit.click();
  return {ok: true};
}
"""

# 点最终「发布文章」按钮。走 JS 点击：footer 可能被其他浮层压住，
# Playwright 的 actionability 检查会卡到超时，而 JS click 不挑遮挡。
_PUBLISH_CLICK_JS = """/* op=publishclick */
(confirmSel) => {
  const btn = document.querySelector(confirmSel);
  if (!btn) return {ok: false, reason: "最终发布按钮未命中（dialog.confirm_button）"};
  btn.click();
  return {ok: true};
}
"""


def _canonical_article_url(url: str, article_id: str) -> str:
    """把详情页 URL 归一成裸形态：去掉 sharetype/sharerId 等追踪参数。

    回执和日志要的是文章的身份，不是一次分享的追踪链（T-10 实测：发布跳转的
    URL 带一串 share* 参数，原样落盘会让同一篇的 URL 每次都不一样）。
    """
    parts = urlsplit(str(url or ""))
    segs = [s for s in parts.path.split("/") if s]
    if "article" in segs and "details" in segs:
        i = segs.index("article")
        account = "/".join(segs[:i])
        prefix = f"/{account}" if account else ""
        return f"{parts.scheme}://{parts.netloc}{prefix}/article/details/{article_id}"
    return str(url or "")


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


def _wait_hidden(page: Page, sel: str, timeout_ms: int) -> None:
    """等元素消失的统一入口。真实 Page 没有 `wait_for_hidden`，等价物是
    `wait_for_selector(sel, state="hidden")`——与 `_type_text` 同一类协议假象。"""
    direct = getattr(page, "wait_for_hidden", None)
    if direct is not None:
        direct(sel, timeout_ms=timeout_ms)
        return
    page.wait_for_selector(sel, state="hidden", timeout=timeout_ms)


def _wait_selector(page: Page, sel: str, timeout_ms: int) -> None:
    """等元素出现的统一入口。协议的参数叫 `timeout_ms`，真机 Playwright 叫
    `timeout`——假页面掩盖过这层差异（T-09b 的协议假象同款），这里一次收掉。"""
    try:
        page.wait_for_selector(sel, timeout_ms=timeout_ms)
    except TypeError:
        page.wait_for_selector(sel, timeout=timeout_ms)


def _wait_url(page: Page, pattern: str, timeout_ms: int) -> None:
    """等 URL 变形的统一入口，参数名差异同 `_wait_selector`。"""
    try:
        page.wait_for_url(pattern, timeout_ms=timeout_ms)
    except TypeError:
        page.wait_for_url(pattern, timeout=timeout_ms)


def _type_in_segments(page: Page, content_sel: str, text: str) -> dict[str, Any]:
    """L3 兜底：聚焦 → 清场 → 分段逐字键入。慢，但走的是真实键盘管线。

    聚焦走 DOM focus 而不是 Playwright click：click 会被残存遮罩拦到超时，
    而逐字键入本身只要求元素有焦点。
    """
    page.evaluate(_FOCUS_JS, content_sel)
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
    def cover_spec(self) -> CoverSpec | None:
        """封面规格（宽/高/大小上限）。T-15 封面渲染与 upload_cover 预检共用这同一份事实。

        None = 平台没给规格：渲染/上传都该拒绝，不许猜（PRD：尺寸从平台读取）。
        """
        return self._meta.cover

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
        - 发布模式确保文章类型=原创（_ensure_original，T-10）：本平台默认即原创，
          这只在草稿带历史状态（转载/翻译）时纠回来。转载/翻译是另一种内容决策，
          Meta 模型没有对应字段，本平台发布的都是原创稿——要发转载得先改模型。
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
        # T-10 真机实测（2026-10-26）：发布模式只能用温和版（点关闭按钮）——
        # close_modals 的 DOM 摘除会连发布对话框的 .modal 挂载点一起摘掉，
        # 之后点发布入口对话框永远渲染不出来。
        if mode is SubmitMode.PUBLISH:
            page.evaluate(_CLICK_CLOSE_BUTTONS_JS, self.selectors("editor", "modal_close"))
        else:
            self.close_modals(page)
        self._fill_title(page, title, js_click=mode is SubmitMode.PUBLISH)

        if mode is SubmitMode.DRAFT:
            logger.info(
                "fill_meta（草稿模式）完成：标题 %d 字；分类/标签/摘要/声明在发布对话框里（T-10）",
                len(title),
            )
            return

        # ---- 发布模式：控件全在「发布文章」对话框里（2026-10-22 真机校准）----
        self._ensure_dialog_open(page)
        self._ensure_original(page)

        if meta.category.strip():
            self._fill_category(page, meta.category.strip())

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
        if tags:
            self._fill_tags(page, tags)

        if meta.summary.strip():
            self._set_value(page, "meta", "summary", meta.summary)

        self._fill_ai_declaration(page, involvement)
        logger.info(
            "fill_meta（发布模式）完成：标题 %d 字、分类 %r、标签 %d 个",
            len(title),
            meta.category or "（未设置）",
            len(tags),
        )

    def _fill_title(self, page: Page, title: str, *, js_click: bool = False) -> None:
        """双层标题控件 + 受控模型 + **异步草稿恢复**：点 display → 点 input →
        清空 → 真实键盘输入 → blur → 回读模型态。

        三个实测教训（T-09b）：
        1. `fill()` 只写 input 的 value，模型不吃——存出来的草稿是【无标题】。
           唯一被模型承认的输入是真实键盘事件。
        2. 回读要看 display 展示层（模型态），不是 input 的 value。
        3. 编辑器会**异步恢复上次未保存的标题**，恢复落盘的时机每次不一样：
           打快了会被恢复覆盖（实测只剩前两个字、甚至全灭）。恢复只发生一次，
           所以回读不符时**重打一次**——第二次落在已稳定的编辑器上。重打仍不符
           才是 MetaError（那时多半是改版，不是时序）。

        `js_click=True`（发布模式，T-10）：点 display/input 走 JS 点击。
        发布模式不能做 DOM 摘除（会摘掉发布对话框挂载点），而残留的遮罩层会
        拦截 Playwright 的指针点击（2026-10-26 实测卡 30s 超时）。JS 点击不做
        命中检查，天然免疫；键入仍是真实键盘事件，模型照认。
        """
        display_sel = self.selectors("editor", "title_display")
        input_sel = self.selectors("editor", "title")

        def _reveal(sel: str) -> None:
            if js_click:
                outcome = page.evaluate(_CLICK_JS, sel)
                if not isinstance(outcome, Mapping) or not outcome.get("ok"):
                    reason = outcome.get("reason") if isinstance(outcome, Mapping) else "脚本无返回"
                    raise MetaError(f"点不开标题控件（{sel}）：{reason}")
            else:
                page.click(sel)

        actual = ""
        for attempt in (1, 2):
            try:
                _reveal(display_sel)
                # display 点击只负责让 input 出现，焦点要显式点进 input（照实测路径）；
                # 之后全选清空、逐字键入。延迟 15ms：5ms 时受控模型会丢字。
                _reveal(input_sel)
                page.press(input_sel, "Meta+A")
                page.press(input_sel, "Backspace")
                _type_text(page, input_sel, title, delay_ms=TITLE_TYPING_DELAY_MS)
                # blur 走 DOM focus：Playwright click 可能被残存遮罩拦到超时（实测）
                page.evaluate(_FOCUS_JS, self.selectors("editor", "content"))
            except MetaError:
                raise
            except Exception as exc:
                raise MetaError(f"填不进 editor.title：{type(exc).__name__}: {exc}") from exc

            outcome = page.evaluate(_READ_TITLE_DISPLAY_JS, display_sel)
            actual = outcome.get("text", "") if isinstance(outcome, Mapping) else ""
            if _squash(actual) == _squash(title):
                return
            logger.warning(
                "标题第 %d 次键入后被模型拒绝（%r != %r），按草稿恢复覆盖处理，重打一次",
                attempt, actual, title,
            )
        raise MetaError(
            f"标题回读不符：写入 {title!r}，两次键入后模型态仍是 {actual!r}"
            "（平台会归一空白，比对已忽略空白）"
        )

    def _set_value(self, page: Page, group: str, key: str, text: str) -> None:
        """往输入控件写值并派发事件。控件不存在 → MetaError（改版要当场暴露）。"""
        sel = self.selectors(group, key)
        outcome = page.evaluate(_SET_VALUE_JS, [sel, text])
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "页面脚本无返回"
            raise MetaError(f"填不进 {group}.{key}: {reason}")

    # ----------------------------------------------------------- T-10 发布对话框

    def _ensure_dialog_open(self, page: Page) -> None:
        """确保「发布文章」对话框已打开。meta/cover/submit 三段共用——它们都住在
        对话框里，而编排层是先 upload_cover 再 fill_meta 最后 submit，任何一段
        都可能是第一个进来的。

        已开就直接返回（幂等）。没开：点掉欢迎弹窗的关闭按钮（**不摘 .modal
        节点**——发布对话框的挂载点就是 .modal，摘了就再也渲染不出来），
        点入口，等对话框根节点渲染出来。

        **对话框打开后绝不再 close_modals**：发布对话框本身就是 .modal，
        那套脚本会把它一起从 DOM 摘掉。
        """
        if page.query_selector(self.selectors("dialog", "root")) is not None:
            return
        # 只点关闭按钮、不摘 .modal 节点：发布对话框的挂载点就是 .modal，
        # 摘掉它对话框永远渲染不出来（2026-10-26 真机踩出）。
        page.evaluate(_CLICK_CLOSE_BUTTONS_JS, self.selectors("editor", "modal_close"))
        outcome = page.evaluate(
            _OPEN_DIALOG_JS, self.selectors("editor", "publish_button")
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "页面脚本无返回"
            raise SubmitBlocked(f"发布对话框打不开：{reason}")
        try:
            _wait_selector(page, self.selectors("dialog", "root"), DIALOG_OPEN_TIMEOUT_MS)
        except Exception as exc:
            raise SubmitBlocked(
                f"点了发布入口，但对话框 {DIALOG_OPEN_TIMEOUT_MS // 1000}s 内没出现"
                f"（{type(exc).__name__}）：可能改版，先抓现场"
            ) from exc

    def _ensure_original(self, page: Page) -> None:
        """文章类型确保选中「原创」。草稿可能带历史状态（转载/翻译），默认态不赌。"""
        outcome = page.evaluate(
            _ENSURE_ORIGINAL_JS, self.selectors("meta", "original_declaration")
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "页面脚本无返回"
            raise MetaError(f"文章类型选不中「原创」：{reason}")

    def _fill_tags(self, page: Page, tags: list[str]) -> None:
        """标签：点「添加文章标签」展开面板 → 逐条键入 + Enter → 关面板。

        面板是 el-autocomplete（T-10 实测）：输入框在展开层 `.mark_selection_box` 里，
        Enter 把当前文本收成一个自定义标签。展开层不收会盖住下面的控件。
        """
        add_sel = self.selectors("meta", "tags_add_button")
        input_sel = self.selectors("meta", "tags")
        try:
            page.click(add_sel)
            _wait_selector(page, input_sel, DIALOG_OPEN_TIMEOUT_MS)
        except Exception as exc:
            raise MetaError(
                f"标签面板展不开（{type(exc).__name__}）：{exc}"
            ) from exc
        for tag in tags:
            _type_text(page, input_sel, tag, delay_ms=TYPING_DELAY_MS)
            page.press(input_sel, "Enter")
        # 收面板：点它自己的关闭按钮。失败不拦——面板浮在对话框内部，不挡 footer。
        try:
            page.click(self.selectors("meta", "tags_panel_close"))
        except Exception:
            logger.debug("标签面板关闭按钮没点成（不阻断流程）")

    def _fill_category(self, page: Page, category: str) -> None:
        """分类必须命中页面上的真实可选项（UT-08-03）。

        T-10 真机校准：分类专栏是点击展开的控件（不是 <select>），且测试账号
        **没有任何专栏**——点了没弹层，可选项恒为空。空选项必须报 MetaError，
        不能静默跳过：「以为选了分类其实没选」比明确的失败难查得多。
        """
        outcome = page.evaluate(
            _CATEGORY_PICK_JS,
            [
                self.selectors("meta", "category"),
                self.selectors("meta", "category_option"),
                category,
            ],
        )
        if not isinstance(outcome, Mapping):
            raise MetaError("分类专栏选择脚本无返回")
        if outcome.get("ok"):
            return
        options = outcome.get("options")
        if isinstance(options, list) and not options:
            raise MetaError(
                "分类专栏可选项为空：本账号没有任何专栏（T-10 实测），"
                "或有专栏账号的展开形态尚未校准。请到平台后台先建专栏，"
                "或把 meta.category 留空跳过"
            )
        reason = outcome.get("reason", "未知原因")
        available = "、".join(str(o) for o in options) if isinstance(options, list) else ""
        raise MetaError(
            f"分类「{category}」选不中：{reason}"
            + (f"。当前可选项：{available}" if available else "")
        )

    def _fill_ai_declaration(self, page: Page, involvement: str) -> None:
        """创作声明（AI 参与度）：el-select 按文本点选，档位映射见 _AI_DECLARATION_TEXT。"""
        text = _AI_DECLARATION_TEXT[involvement]  # 上游已校验取值，KeyError 就是 bug
        if involvement == "generated":
            logger.warning(
                "平台没有独立的「AI 生成」档，落到最近的「%s」（T-10 校准的四档里没有更贴切的）",
                text,
            )
        outcome = page.evaluate(
            _AI_DECLARE_JS,
            [
                self.selectors("meta", "ai_declaration"),
                self.selectors("meta", "ai_declaration_option"),
                text,
            ],
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "页面脚本无返回"
            options = outcome.get("options") if isinstance(outcome, Mapping) else None
            detail = f"；页面当前档位：{'、'.join(options)}" if options else ""
            raise MetaError(f"创作声明选不中「{text}」：{reason}{detail}")

    # ----------------------------------------------------------- T-08 封面

    def upload_cover(self, page: Page, path: Path) -> None:
        """上传封面并等缩略图。

        三道本地预检**全部发生在 set_input_files 之前**（UT-08-05）：
        文件在不在、尺寸对不对、大小超没超。传上去才发现不对，
        后台就多了一张要人工清理的垃圾封面。
        规格只认 meta.json 的 cover 字段（PRD：尺寸从平台读取，不得硬编码）。

        注意：封面上传框在**发布对话框**里（T-10 真机校准），
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

        self._ensure_dialog_open(page)

        # 已有封面时上传框从 DOM 里消失（T-10 实测），只剩删除入口——先删再传。
        if page.query_selector(self.selectors("cover", "done_marker")) is not None:
            try:
                page.click(self.selectors("cover", "remove_button"))
            except Exception as exc:
                raise CoverError(f"旧封面删不掉（cover.remove_button）：{exc}") from exc
            try:
                _wait_selector(
                    page, self.selectors("cover", "upload_input"), THUMBNAIL_TIMEOUT_MS
                )
            except Exception as exc:
                raise CoverError(f"删掉旧封面后上传框没回来：{type(exc).__name__}") from exc

        try:
            page.set_input_files(self.selectors("cover", "upload_input"), path)
        except Exception as exc:
            raise CoverError(f"封面塞不进上传框（cover.upload_input）：{exc}") from exc

        # 选文件后弹出裁剪编辑器；「确认上传」是 div.vicp-operate-btn（不是 button）
        try:
            _wait_selector(page, self.selectors("cover", "crop_editor"), THUMBNAIL_TIMEOUT_MS)
        except Exception as exc:
            raise CoverError(
                f"选文件后裁剪编辑器 {THUMBNAIL_TIMEOUT_MS // 1000}s 内未出现"
                f"（{type(exc).__name__}）：控件改版或文件被拒"
            ) from exc
        try:
            page.click(self.selectors("cover", "crop_confirm"))
        except Exception as exc:
            raise CoverError(f"裁剪编辑器的「确认上传」点不动：{exc}") from exc

        # 上传是异步的（按钮转 loading），完成信号是 .preview-box 长出 .hasImg。
        # 别拿 img.preview 的 .src 属性判断——空 src 会被浏览器解析成页面 URL
        # （假阳性，T-10 探针踩过）；hasImg 是组件自己加的类，没有歧义。
        try:
            _wait_selector(
                page, self.selectors("cover", "done_marker"), COVER_UPLOAD_TIMEOUT_MS
            )
        except Exception as exc:
            raise CoverError(
                f"封面确认上传后 {COVER_UPLOAD_TIMEOUT_MS // 1000}s 内未完成"
                f"（{type(exc).__name__}）：已停手，文章未发布"
            ) from exc
        logger.info("封面已上传：%s（%dx%d，%d 字节）", path.name, width, height, size)

    # ----------------------------------------------------------- T-12 拒稿检测

    def raise_if_rejected(self, page: Page) -> None:
        """读平台上的拒稿提示（含敏感词/违规 toast），有就抛 `ContentRejected`。

        只做一次即时读，**不等待**：这类提示（Element UI 的 el-message 错误 toast）
        几秒后自动消失，等它反而可能等到一个已消失的；客户端校验类的拒绝在点击后
        几乎立刻出现。判定时机紧贴在点击之后（见 submit / _submit_publish）。

        选择器是平台事实，在 selectors.json 的 verify.rejection_toast；
        形态待真机校准（与 publish-dialog fixture 同流程），契约测试固定在
        fixtures/csdn/rejected.html 上。
        """
        el = page.query_selector(self.selectors("verify", "rejection_toast"))
        if el is None:
            return
        text = (el.text_content() or "").strip()
        if text:
            raise ContentRejected(text)

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
        """草稿走「保存草稿」两步（T-09）；发布走对话框的最终按钮（T-10）。

        顺序是硬的：复核正文 → （草稿：关弹窗）→ 点按钮。发布模式**不**关弹窗——
        发布对话框自己就是 .modal，close_modals 会把它从 DOM 摘掉。
        """
        mode = SubmitMode(mode)
        if mode is SubmitMode.PUBLISH:
            return self._submit_publish(page)

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
            # 点保存被拒（含敏感词等）时平台会弹错误 toast：先把原话带出来（T-12）
            self.raise_if_rejected(page)
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "保存脚本无返回"
            return SubmitResult(
                state=SubmitState.UNKNOWN,
                reason=f"「{_SAVE_DRAFT_LABEL}」没点成（{reason}）：交给 verify 判定",
            )

        # 真机实测的成功信号：URL 长出 articleId 参数
        try:
            _wait_url(page, "**articleId=*", SUBMIT_TIMEOUT_MS)
        except Exception as exc:
            if page.url != before_url:
                return SubmitResult(
                    state=SubmitState.SUBMITTED, reason=f"点击后页面已变化：{page.url}"
                )
            # 页面纹丝不动 + 错误 toast = 平台拒稿：原话必须带出来（T-12）
            self.raise_if_rejected(page)
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

    def _submit_publish(self, page: Page) -> SubmitResult:
        """真实发布（T-10）：复核正文 → 确认对话框开着 → 点 footer 的「发布文章」。

        两道刹车：
        1. `check_content_length`——页面正文必须还是注入时那一份（R7）；
        2. 对话框必须在——它不在，说明 fill_meta/upload_cover 没跑成或被意外
           关掉，这时点任何按钮都是盲发，直接 SubmitBlocked。

        发布模式不调 close_modals：发布对话框本身就是 .modal。
        """
        self.check_content_length(page)

        if page.query_selector(self.selectors("dialog", "root")) is None:
            raise SubmitBlocked(
                "发布对话框未打开：fill_meta/upload_cover 没跑成，或对话框被意外关闭。"
                "拒绝在状态不明时点发布——一个按钮都没点"
            )

        before_url = page.url
        outcome = page.evaluate(
            _PUBLISH_CLICK_JS, self.selectors("dialog", "confirm_button")
        )
        if not isinstance(outcome, Mapping) or not outcome.get("ok"):
            # 点发布被拒（含敏感词等）时平台会弹错误 toast：先把原话带出来（T-12）
            self.raise_if_rejected(page)
            reason = outcome.get("reason") if isinstance(outcome, Mapping) else "点击脚本无返回"
            return SubmitResult(
                state=SubmitState.UNKNOWN,
                reason=f"最终「发布文章」没点成（{reason}）：交给 verify 判定",
            )

        # 客户端校验类的拒稿几乎立刻弹 toast（如含敏感词），先判一次再等成功信号——
        # 不然要等满整个 PUBLISH_TIMEOUT 才罢休，而 toast 早就自动消失了（T-12）
        self.raise_if_rejected(page)

        # 成功形态（待 IT-10-01 真机确认优先级）：跳详情页，或页面长出详情链接
        try:
            _wait_url(page, "**/article/details/*", PUBLISH_TIMEOUT_MS)
            return SubmitResult(
                state=SubmitState.SUBMITTED, reason=f"已跳详情页：{page.url}"
            )
        except Exception:
            pass
        link = page.query_selector(self.selectors("verify", "published_link"))
        if link is not None:
            href = (link.get_attribute("href") or "").strip()
            if _article_id_from_url(href):
                return SubmitResult(
                    state=SubmitState.SUBMITTED, reason=f"页面出现详情链接：{href}"
                )
        if page.url != before_url:
            return SubmitResult(
                state=SubmitState.SUBMITTED, reason=f"点击后页面已变化：{page.url}"
            )
        # 所有成功信号都没出现：最后再认一次拒稿 toast（响应慢的拒稿会走到这里）
        self.raise_if_rejected(page)
        return SubmitResult(
            state=SubmitState.UNKNOWN,
            reason=(
                f"点击后 {PUBLISH_TIMEOUT_MS // 1000}s 内没有成功信号"
                "（没跳详情页、没出现详情链接）：交给 verify 判定"
            ),
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

        发布模式另有一套裁决（T-10）：编辑器 URL 上的 `?articleId=` 是**草稿**
        信号（存草稿阶段就长出来了），拿它当发布成功就是假阳性。发布只认
        详情页 URL 或页面上的详情链接。
        """
        mode = SubmitMode(mode)
        if mode is SubmitMode.PUBLISH:
            return self._verify_publish(page)

        url = page.url
        article_id = self._read_article_id(page) or _article_id_from_url(url)
        if article_id:
            return VerifyResult(
                ok=True, url=url, article_id=article_id, reason="从页面标记或 URL 读到 article_id"
            )
        return self._verify_via_draft_list(page)

    def _verify_publish(self, page: Page) -> VerifyResult:
        """发布成功的裁决：详情页 URL → 页面上的详情链接，两条都没有就是没发成。

        已发布的文章不在草稿箱里，拿草稿列表兜底只会得到假阴性（原逻辑保留此判断）。
        """
        url = page.url
        if "/article/details/" in url:
            article_id = self._read_article_id(page) or _article_id_from_url(url)
            if article_id:
                return VerifyResult(
                    ok=True, url=_canonical_article_url(url, article_id), article_id=article_id,
                    reason="已跳详情页并读到 article_id",
                )
            return VerifyResult(
                ok=False, url=url,
                reason="在详情页却读不到 article_id：页面结构改版，先抓现场",
            )

        link = page.query_selector(self.selectors("verify", "published_link"))
        if link is not None:
            href = (link.get_attribute("href") or "").strip()
            article_id = _article_id_from_url(href)
            if article_id:
                full = _canonical_article_url(urljoin(url, href), article_id)
                return VerifyResult(
                    ok=True, url=full, article_id=article_id,
                    reason="从页面详情链接读到 article_id",
                )
        return VerifyResult(
            ok=False, url=url,
            reason="没有发布成功信号（既没跳详情页，页面上也没有详情链接）：不走草稿列表兜底",
        )

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
            # 管理页是 SPA：tab 不随 load 事件出现，先等它渲染出来再点（实测：
            # 不等的话 evaluate 里一个 .nav-link 都找不到，报「tab 点不开」假失败）。
            page.wait_for_selector(self.selectors("verify", "draft_list_tab"))
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
            _wait_hidden(
                page, self.selectors("verify", "draft_list_loading"), timeout_ms=20_000
            )
            items = page.query_selector_all(self.selectors("verify", "draft_list_item"))
        except Exception as exc:
            return VerifyResult(
                ok=False,
                url=page.url,
                reason=f"草稿列表页打不开或没渲染完（{type(exc).__name__}）：{exc}",
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
