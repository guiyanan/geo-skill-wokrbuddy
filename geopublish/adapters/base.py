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

七方法的分工见架构 §3.2；本工单只落地 `check_auth` / `open_editor`，
其余五个用 `NotImplementedError` 明确标注归属工单——**绝不写 `pass`**。
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
    "InjectAttempt",
    "InjectFailed",
    "Page",
    "PlatformAdapter",
    "REQUIRED_SELECTORS",
    "SelectorError",
    "Selectors",
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
    "editor": ("title", "content", "content_editable", "publish_button", "save_draft_button"),
    "meta": ("category", "tags", "summary", "original_declaration", "ai_declaration"),
    "cover": ("upload_input", "thumbnail"),
    "verify": ("article_id", "success_marker"),
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


@dataclass(frozen=True, slots=True)
class SubmitResult:
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

        domains = data.get("domains")
        if (
            not isinstance(domains, list)
            or not domains
            or not all(isinstance(item, str) and item.strip() for item in domains)
        ):
            raise SelectorError(f"domains 必须是非空字符串数组（{path}）")

        return cls(
            name=str(data.get("name") or path.parent.name),
            domains=tuple(item.strip() for item in domains),
            editor_url=editor_url.strip(),
            login_url=login_url,
        )


# --------------------------------------------------------------- 七方法契约

@runtime_checkable
class PlatformAdapter(Protocol):
    """任何平台发布流程都逃不出这 7 个动作，所以契约就是这 7 个。"""

    name: str

    def check_auth(self, page: Page) -> AuthResult:
        """只读判断登录态，**不做登录动作、不重试**。"""

    def open_editor(self, page: Page, draft: Draft | None) -> None:
        """导航到新建文章页，等编辑器就绪。"""

    def fill_content(self, page: Page, draft: Draft | None) -> None:
        """注入正文。内部自带三级降级。"""

    def fill_meta(self, page: Page, draft: Draft | None) -> None:
        """标题、分类、标签、摘要、原创声明、AI 标注。"""

    def upload_cover(self, page: Page, path: Path) -> None:
        """上传封面，等缩略图出现。"""

    def submit(self, page: Page) -> SubmitResult:
        """点发布/存草稿。"""

    def verify(self, page: Page) -> VerifyResult:
        """回读确认：拿到真实 URL 和文章 id。没验证过的不算成功。"""


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
