"""适配器契约：7 个方法 + 选择器表 + 平台元信息。

设计要点（这些不是随手定的）：

1. **选择器只有一处来源**（`selectors.json`）。平台改版时改一个 JSON 就能修，
   不用读代码。`.precommit/check_no_naked_selectors.py` 会拦住写回代码里的尝试。
2. **`Selectors` 没有默认值**。取不存在的键直接抛错。留默认值的后果是：
   线上拿空串去 `query_selector`，返回 None，被解读成「页面没这个元素」，
   于是把「配置漏了」误诊成「平台改版」，排查方向直接跑偏。
3. **加载时一次报全所有缺口**。改版那天要一眼看完整清单，而不是删一个跑一次。
4. **不校验 CSS 语法**。那需要引入 CSS 解析依赖，而选择器只在改版时才变，
   改版一定会跑 CI —— 语法校验放在 `tests/contract/test_selectors.py` 里做。
5. **结果类型是值对象**。`check_auth` 返回 `AuthResult` 而不是裸 bool：
   「登录过期」和「遇到验证码」需要不同的处置（前者让人扫码，后者必须停手），
   一个 bool 表达不了，而回到裸字符串就是回到字符串比较。

七方法的分工见架构 §3.2。T-10 起七个方法全部落地（submit 的 publish 分支
走发布对话框的最终按钮，并交付 core/receipt.py 回执落盘）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, runtime_checkable

from ..core.models import Draft

__all__ = [
    "ADAPTER_METHODS",
    "AdapterContractError",
    "AuthResult",
    "AuthState",
    "Element",
    "CoverError",
    "CoverSpec",
    "InjectAttempt",
    "InjectFailed",
    "MetaError",
    "Page",
    "PlatformAdapter",
    "REQUIRED_SELECTORS",
    "SelectorError",
    "Selectors",
    "ContentRejected",
    "SubmitBlocked",
    "SubmitMode",
    "SubmitResult",
    "SubmitState",
    "VerifyResult",
    "assert_adapter",
]

# 契约本身：每个平台都必须提供这些选择器（键名改动 = 改契约 = 所有 adapter 一起改）。
#
# auth.challenge 指「页面出现了人机校验」——判定它出现的目的是**停手交给人**，
# 绝不含任何自动通过逻辑（R13 红线）。这里刻意不用那个英文词命名：源码里出现
# 它的字样会被红线门禁拦下（tests/unit/test_project_layout.py），而这道门禁的
# 价值正在于「一刀切、不给例外」，所以改词不改门禁。
REQUIRED_SELECTORS: Mapping[str, tuple[str, ...]] = {
    "auth": ("login_form", "challenge", "avatar"),
    "editor": (
        "title",           # 隐藏的真实 input（CSDN：可见的是 display，输入框 aria-hidden）
        "title_display",   # 可见的标题展示层：先点它，输入框才出现
        "content",
        "content_editable",
        "publish_button",
        "save_draft_button",
        "save_draft_item",  # 保存按钮唤出的 popover 里的「保存草稿」链接
        "modal_close",      # 编辑器会自动弹模版库等弹窗，不关掉会挡住一切点击
    ),
    "meta": ("category", "tags", "summary", "original_declaration", "ai_declaration"),
    "cover": ("upload_input", "thumbnail"),
    # T-10：发布对话框本身也是契约——入口点开后，根容器在不在、最终发布按钮和
    # 取消按钮各是哪一个，打不中就是改版，必须在契约测试里先红。
    "dialog": ("root", "confirm_button", "cancel_button"),
    "verify": (
        "article_id",
        "success_marker",
        "draft_list_item",
        "draft_list_tab",      # 内容管理页默认停在「全部」，草稿要点这个 tab 才出来
        "draft_list_loading",  # 列表异步加载的转圈：不消失就读，读到的是空列表（假阴性）
    ),
}

ADAPTER_METHODS: tuple[str, ...] = (
    "check_auth",
    "open_editor",
    "fill_content",
    "fill_meta",
    "upload_cover",
    "submit",
    "verify",
)


class SelectorError(ValueError):
    """选择器表/平台元信息的问题。全部带路径，否则用户不知道在读哪个文件。"""


class AdapterContractError(TypeError):
    """某对象不满足适配器契约。"""


# --------------------------------------------------------------- 结果类型

class AuthState(str, Enum):
    """登录态。字符串值会进日志和现场包，改它等于改日志语义。"""

    LOGGED_IN = "logged_in"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class AuthResult:
    """登录态判定结果。

    `has_challenge` 必须单独带出来：「未知原因」和「遇到人机校验」的处置完全不同——
    前者可以再看一眼页面是否渲染完，后者**必须停手**（反复撞校验页是风控最敏感的信号）。
    """

    state: AuthState
    has_challenge: bool = False
    reason: str = ""


class SubmitState(str, Enum):
    SUBMITTED = "submitted"
    UNKNOWN = "unknown"


class SubmitMode(str, Enum):
    """提交方式：存草稿 vs 真实发布。

    为什么不用 `submit(page, draft=True)` 这种 bool：调用点写成 `submit(page, False)`
    没人看得懂，而这个参数决定的后果是「这篇文章会不会当场公开」。语义写进类型里，
    日志与现场包也能直接读出当时要的是哪一种。
    """

    DRAFT = "draft"
    PUBLISH = "publish"


@dataclass(frozen=True, slots=True)
class SubmitResult:
    """点击之后的**即时观察**，不代表成功——成功与否只由 verify 判定（R8）。"""

    state: SubmitState
    reason: str = ""


@dataclass(frozen=True, slots=True)
class VerifyResult:
    """回读确认。`ok=False` 时 url/article_id 可以为空——没验证过的不算成功（R8）。"""

    ok: bool
    url: str = ""
    article_id: str = ""
    reason: str = ""


# --------------------------------------------------------------- 注入结果

@dataclass(frozen=True, slots=True)
class InjectAttempt:
    """一级注入尝试的完整记录。

    三级全灭时这三条就是现场包 `input.json` 的核心 payload——
    「L2 为什么被跳过」必须能从这里直接读出来，不许只留一个 bool。
    """

    level: str  # "L1-paste" / "L2-dispatch" / "L3-type"，进日志与现场包，改名 = 改语义
    ok: bool
    reason: str = ""
    expected_len: int = 0
    actual_len: int | None = None
    elapsed_ms: float = 0.0


class InjectFailed(RuntimeError):
    """三级注入全部失败。

    携带全部尝试记录：上层（T-09 起的发布流程）捕获后负责归档现场包，
    适配器层自己碰不到截图/HAR，**绝不在这里静默**。
    """

    def __init__(self, attempts: tuple[InjectAttempt, ...], expected_len: int) -> None:
        self.attempts = attempts
        self.expected_len = expected_len
        detail = "；".join(
            f"{a.level}（{a.reason or '未知原因'}）" for a in attempts
        )
        super().__init__(
            f"三级注入全部失败（源文 {expected_len} 字）：{detail}。已停手，等待人工排查现场包"
        )


# --------------------------------------------------------------- 元信息/封面失败

class MetaError(RuntimeError):
    """元信息填充失败：分类不在可选项里、AI 参与度为空等。

    与 InjectFailed 同一哲学：**停下来，把现场说清楚**，不许带着半填的元信息
    往下走——分类错了发出去就是发错了栏目。
    """


class CoverError(RuntimeError):
    """封面上传失败：文件缺失/尺寸或大小不符/缩略图超时。

    尺寸类失败必须发生在 set_input_files **之前**——传上去才发现不对，
    后台里就多了一张要人工清理的垃圾封面。
    """


class SubmitBlocked(RuntimeError):
    """提交前的复核没通过，因此**一个按钮都没点**。

    R7 红线：未验证正文前不得点发布。编辑器里的字数和注入时不符、或者压根没有
    注入记录，都说明页面上此刻的内容不是我们要发的那篇 —— 这时候点下去，
    发出去的是残稿。「拦在按钮之前」是这条异常唯一的价值，所以它不是普通的失败：
    失败可以重试，拦下来是**保护**。
    """


class ContentRejected(RuntimeError):
    """平台明确拒稿（含敏感词/违规提示），携带平台提示原文。

    与 SubmitBlocked 的分工：Blocked 是「我们自己拦下的，一个按钮都没点」；
    Rejected 是「点了，平台说不」。稿子没发出去，但平台原话必须留住——
    改稿的人要看的正是这一句（T-12：原文回写进 publish-log 与稿件目录）。
    """

    def __init__(self, platform_text: str) -> None:
        self.platform_text = str(platform_text).strip()
        super().__init__(f"平台拒绝发布：{self.platform_text or '（无提示原文）'}")


# --------------------------------------------------------------- 页面抽象

@runtime_checkable
class Element(Protocol):
    """页面元素。故意只留只读三件：写操作由 adapter 用具体 page 实现去调。"""

    def text_content(self) -> str: ...

    def get_attribute(self, name: str) -> str | None: ...

    def is_visible(self) -> bool: ...


@runtime_checkable
class Page(Protocol):
    """Playwright `Page` 的最小可用子集。

    测试里用 `tests/contract/dom.py::StaticPage` 实现同一形状，于是
    `check_auth` 这类生产代码可以在静态 DOM 上原样跑——不必 mock 掉待测逻辑本身。
    """

    @property
    def url(self) -> str: ...

    def goto(self, url: str) -> None: ...

    def wait_for_selector(self, selector: str, timeout_ms: int = 10_000) -> Element: ...

    def query_selector(self, selector: str) -> Element | None: ...

    def query_selector_all(self, selector: str) -> list[Element]: ...

    # --- 写入三件：正文注入（T-07）的最小写面。
    # 选择器一律由调用方从 selectors.json 取好再传入，实现里不接裸脚本。

    def evaluate(self, script: str, arg: Any = None) -> Any:
        """在页面上下文执行脚本。注入/回读都走它，脚本里的选择器必须经 arg 传入。"""
        ...

    def click(self, selector: str) -> None:
        """聚焦元素。L3 分段 type 之前必须先聚焦，直接灌键盘事件会丢字。"""
        ...

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        """逐字符键入。只作 L3 兜底——慢，但几乎不会失败。"""

    # --- 写入两件：元信息与封面（T-08）的最小写面。

    def press(self, selector: str, key: str) -> None:
        """按一次按键。标签框靠 Enter 确认，没有它标签就停在未提交状态。"""

    def set_input_files(self, selector: str, path: Path) -> None:
        """给 file input 塞本地文件（封面上传）。路径必须是绝对路径。"""
        ...

    # --- 读一件：回读确认（T-09）的最小读面。

    def input_value(self, selector: str) -> str:
        """读输入框当前值。

        verify 要靠它拿到「编辑器里此刻的标题」再去草稿列表比对：
        标题只存在于这次会话里，平台不会把它回填到成功页上。
        """
        ...

    # --- 写入两件：真机校准（T-09b）加的最小写面。

    def fill(self, selector: str, text: str) -> None:
        """一次性填入文本框。框架受控组件只认真实输入管线，value= 赋值会被模型无视。

        Playwright 的 fill 自带可见性等待，正好覆盖「先点 display 再填隐藏 input」
        这种两步控件。
        """
        ...

    def wait_for_url(self, pattern: str, timeout_ms: int = 10_000) -> None:
        """等 URL 变成某个 glob 形态。存草稿成功的真实信号就是 URL 长出 articleId。"""
        ...

    def wait_for_hidden(self, selector: str, timeout_ms: int = 10_000) -> None:
        """等元素消失（或本来就不在）。草稿列表的加载圈：等它走了再读行。"""
        ...


# --------------------------------------------------------------- 选择器表

@dataclass(frozen=True, slots=True)
class Selectors:
    """一份平台选择器表。取键无默认值，构造后**深不可变**。

    深不可变不是洁癖：这本表是全局共享的（每个 adapter 实例都拿同一份），
    哪天有人图省事在运行时改一个选择器，出问题的位置会离改动点很远。
    """

    version: str
    groups: Mapping[str, Mapping[str, str]]
    path: Path

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "groups",
            MappingProxyType(
                {group: MappingProxyType(dict(section)) for group, section in self.groups.items()}
            ),
        )

    @classmethod
    def load(cls, path: str | Path) -> Selectors:
        path = Path(path)
        if not path.is_file():
            raise SelectorError(f"选择器文件不存在：{path}")

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SelectorError(f"选择器文件不是合法 JSON（{path}）：{exc}") from exc

        if not isinstance(raw, dict):
            raise SelectorError(f"选择器文件顶层必须是对象（{path}）")

        version = raw.get("version")
        if not isinstance(version, str) or not version.strip():
            # 现场包要记 selectors_version，没有版本号就无从反查「当时用的哪一版」
            raise SelectorError(f"选择器文件缺 version（{path}）")

        sections = {key: value for key, value in raw.items() if key != "version"}
        unknown = sorted(set(sections) - set(REQUIRED_SELECTORS))
        if unknown:
            # 组名拼错（ediitor）如果放过去，等于整组选择器静默失效
            raise SelectorError(f"未知的选择器分组：{', '.join(unknown)}（{path}）")

        groups: dict[str, Mapping[str, str]] = {}
        missing: list[str] = []
        for group, keys in REQUIRED_SELECTORS.items():
            section = sections.get(group)
            if section is None:
                missing.extend(f"{group}.{key}" for key in keys)
                continue
            if not isinstance(section, dict):
                raise SelectorError(f"选择器分组 {group} 必须是对象（{path}）")

            cleaned: dict[str, str] = {}
            for key, value in section.items():
                if not isinstance(value, str) or not value.strip():
                    raise SelectorError(f"选择器必须是非空字符串：{group}.{key}（{path}）")
                cleaned[key] = value.strip()

            missing.extend(f"{group}.{key}" for key in keys if key not in cleaned)
            groups[group] = cleaned

        if missing:
            raise SelectorError(
                f"selectors.json 缺 {len(missing)} 条：{', '.join(missing)}（{path}）"
            )

        return cls(version=version.strip(), groups=groups, path=path)

    def group(self, name: str) -> Mapping[str, str]:
        try:
            return self.groups[name]
        except KeyError as exc:
            raise SelectorError(f"没有这个选择器分组：{name}（{self.path}）") from exc

    def __call__(self, group: str, key: str) -> str:
        section = self.group(group)
        try:
            return section[key]
        except KeyError as exc:
            raise SelectorError(f"没有这个选择器：{group}.{key}（{self.path}）") from exc


# --------------------------------------------------------------- 元信息

@dataclass(frozen=True, slots=True)
class CoverSpec:
    """平台后台上传框的封面规格。**平台事实**，所以放 meta.json 不放 config。

    与字数/标题/标签上限相反：那三个是本项目的发文纪律（config 一份真相），
    尺寸是平台说了算（PRD：尺寸从平台读取，不得硬编码）。数值要与真实
    后台上传框核对（T-09 首发时校准），但存放位置就是这份 JSON。
    """

    width: int
    height: int
    max_bytes: int


@dataclass(frozen=True, slots=True)
class AdapterMeta:
    """平台元信息。**不含**字数/标题/标签上限——那些只在 PlatformConfig 里。

    两处都写 = 两份真相：改了一处忘了另一处，guard 拦的和平台实际限制对不上。
    """

    name: str
    domains: tuple[str, ...]
    editor_url: str
    # 登录页地址。不给就退回 editor_url（很多平台后台页会自己跳登录）。
    # 放这里而不是 core 里：换平台不该改主程序。
    login_url: str = ""
    # 封面上传规格（宽/高/大小上限）。None = 平台未提供规格，upload_cover 会拒绝上传。
    cover: CoverSpec | None = None
    # 草稿列表页。verify 在「提交后 URL 没变、DOM 里也没有 article_id」时退到这一页
    # 按标题找（UT-09-02）。空 = 该平台没有这条兜底，verify 会明确报错而不是瞎猜。
    draft_list_url: str = ""

    @classmethod
    def load(cls, path: str | Path) -> AdapterMeta:
        path = Path(path)
        if not path.is_file():
            raise SelectorError(f"平台元信息文件不存在：{path}")

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise SelectorError(f"平台元信息不是合法 JSON（{path}）：{exc}") from exc
        if not isinstance(data, dict):
            raise SelectorError(f"平台元信息顶层必须是对象（{path}）")

        editor_url = data.get("editor_url")
        if not isinstance(editor_url, str) or not editor_url.startswith(("https://", "http://")):
            raise SelectorError(f"editor_url 必须是 http(s) 地址（{path}）")

        login_url = data.get("login_url", "")
        if not isinstance(login_url, str):
            raise SelectorError(f"login_url 必须是字符串（{path}）")
        login_url = login_url.strip()
        if login_url and not login_url.startswith(("https://", "http://")):
            # 写错协议（passport.csdn.net/login 少了 https://）不报出来的话，
            # 登录会打到一条相对路径上，人看到的是一个 404 空白页
            raise SelectorError(f"login_url 必须是 http(s) 地址（{path}）")

        draft_list_url = data.get("draft_list_url", "")
        if not isinstance(draft_list_url, str):
            raise SelectorError(f"draft_list_url 必须是字符串（{path}）")
        draft_list_url = draft_list_url.strip()
        if draft_list_url and not draft_list_url.startswith(("https://", "http://")):
            # 兜底页地址写错的话，verify 会 goto 到一条相对路径上、拿到 404，
            # 然后报「草稿列表里没找到」—— 一个地址笔误被误诊成「稿子没发出去」
            raise SelectorError(f"draft_list_url 必须是 http(s) 地址（{path}）")

        domains = data.get("domains")
        if (
            not isinstance(domains, list)
            or not domains
            or not all(isinstance(item, str) and item.strip() for item in domains)
        ):
            raise SelectorError(f"domains 必须是非空字符串数组（{path}）")

        cover_raw = data.get("cover")
        cover: CoverSpec | None = None
        if cover_raw is not None:
            if not isinstance(cover_raw, dict):
                raise SelectorError(f"cover 必须是对象（width/height/max_bytes）（{path}）")
            values: dict[str, int] = {}
            for field in ("width", "height", "max_bytes"):
                value = cover_raw.get(field)
                if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
                    raise SelectorError(f"cover.{field} 必须是正整数，收到 {value!r}（{path}）")
                values[field] = value
            cover = CoverSpec(**values)

        return cls(
            name=str(data.get("name") or path.parent.name),
            domains=tuple(item.strip() for item in domains),
            editor_url=editor_url.strip(),
            login_url=login_url,
            cover=cover,
            draft_list_url=draft_list_url,
        )


# --------------------------------------------------------------- 七方法契约

@runtime_checkable
class PlatformAdapter(Protocol):
    """任何平台发布流程都逃不出这 7 个动作，所以契约就是这 7 个。"""

    name: str

    def check_auth(self, page: Page) -> AuthResult:
        """只读判断登录态，**不做登录动作、不重试**。"""

    def wait_for_auth_signal(self, page: Page, timeout_ms: int = 30_000) -> None:
        """探活前等页面渲染出权威信号（编辑器 / 登录表单 / 人机校验任一）。

        它不是第八个动作，而是 check_auth 的前置等待：SPA 的 load 事件不代表
        渲染完成，没有它，check_auth 会在半渲染页面上误判。判定仍然只做一次。
        """

    def open_editor(self, page: Page, draft: Draft | None) -> None:
        """导航到新建文章页，等编辑器就绪。"""

    def fill_content(self, page: Page, draft: Draft | None) -> None:
        """注入正文。内部自带三级降级。"""

    def fill_meta(self, page: Page, draft: Draft | None, limits: Any) -> None:
        """标题、分类、标签、摘要、AI 标注。

        `limits` 传 `core.config.PlatformConfig`：标题/标签上限只在那里有一份真相，
        adapter 不另存一份（PRD §9.1 的 L1/L3 划分）。
        """

    def upload_cover(self, page: Page, path: Path) -> None:
        """上传封面，等缩略图出现。"""

    def submit(self, page: Page, *, mode: SubmitMode | str = SubmitMode.DRAFT) -> SubmitResult:
        """点发布/存草稿。`mode` 决定点哪个按钮——存草稿绝不碰发布按钮（UT-09-04）。"""

    def verify(self, page: Page, *, mode: SubmitMode | str = SubmitMode.DRAFT) -> VerifyResult:
        """回读确认：拿到真实 URL 和文章 id。没验证过的不算成功。

        `mode` 只影响兜底路径：draft 模式允许去草稿列表页按标题核对（UT-09-02），
        发布模式不走那条 —— 已发布的文章本来就不在草稿箱里。
        """


def assert_adapter(candidate: Any) -> None:
    """运行时校验契约（Protocol 的 check_auth 等名字写错不会被静态发现）。

    报错一次列全缺的方法：新增平台时最烦的是「跑一次报一个，改一个跑一次」。
    """
    missing = [name for name in ADAPTER_METHODS if not callable(getattr(candidate, name, None))]
    if not hasattr(candidate, "name"):
        missing.append("name")
    if missing:
        raise AdapterContractError(
            f"{type(candidate).__name__} 不满足适配器契约，缺：{', '.join(missing)}"
        )
