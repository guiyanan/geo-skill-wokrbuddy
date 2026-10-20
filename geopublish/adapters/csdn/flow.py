"""CSDN 适配器。

本工单（T-05）落地的只有 `check_auth` 与 `open_editor`：契约测试要靠它们
证明「选择器对真实 DOM 有效」，而这两件事恰好只读、可在静态快照上跑。

其余五个方法**故意**抛 `NotImplementedError` 并写明归属工单：
写 `pass` 是最危险的做法——流程会一路绿到「发布成功」，然后线上什么都没有。
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

from ..base import (
    AdapterMeta,
    AuthResult,
    AuthState,
    Page,
    Selectors,
    SubmitResult,
    VerifyResult,
)
from ...core.models import Draft

__all__ = ["CsdnAdapter", "META_PATH", "SELECTORS_PATH"]

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
    ) -> None:
        # selectors_path 只为测试/引用另一份表而存在；默认必须是包内那份，
        # 否则「改版只改 JSON」在打包后就会失效。
        self.selectors_path = Path(selectors_path) if selectors_path else SELECTORS_PATH
        self._selectors = selectors if selectors is not None else Selectors.load(self.selectors_path)
        self._meta = AdapterMeta.load(Path(meta_path) if meta_path else META_PATH)

    # ----------------------------------------------------------- 只读属性

    @property
    def selectors_version(self) -> str:
        return self._selectors.version

    @property
    def editor_url(self) -> str:
        return self._meta.editor_url

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

    # ----------------------------------------------------------- 待实现

    def fill_content(self, page: Page, draft: Draft | None) -> None:
        raise NotImplementedError(
            "fill_content 未实现：三级降级注入（paste / execCommand / keyboard）属于 T-07"
        )

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
