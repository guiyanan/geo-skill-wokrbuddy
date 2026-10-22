"""登录态管理（R6 / N2）。

三件事，每件都有明确的事故背景：

1. **profile 目录（默认 `~/.geo-publish/profiles/<platform>`）**：权限 700，落在仓库外，
   `.gitignore` 另挡一道。登录态就是账号的钥匙 —— 它进过一次 git，就只能改密码了。
2. **探活只做一次**：`check_login` 调 `adapter.check_auth` **恰好一次**，非 logged_in
   一律抛 `AuthRequired`（CLI 退出码 2），**不重试**。反复撞登录页是风控最敏感的信号
   （架构 §3.3）。写重试的人不是想不开，是觉得「再试一次也许就好了」—— 在这里，
   「也许就好了」的代价是账号被风控。
3. **login 子命令**：开有头浏览器（持久 profile）→ 停在登录页让人扫码 → 等回车 →
   去编辑器页回读一次。回读放在**编辑器页**而不是登录页：头像/编辑器正文才是权威信号，
   登录页扫码后是否跳转成功不可靠。

`channel` 可配置（架构 §3.3）：本地默认 `chrome`（复用系统 Chrome，不下载 chromium），
CI 传 `bundled` → `channel=None`，用 playwright 自带的那个。**channel=None 时必须
不传这个参数**，而不是传 None：playwright 会把 None 当成非法值。

不做什么：不存密码、不填登录表单、不碰人机校验（R13 红线）。到这里为止，人的事归人。
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterator

import logging

# AuthState / AuthResult 是 adapter 契约里的值对象。core 只借这两个类型，
# 不依赖任何具体平台实现（adapters 反过来依赖 core.models，方向仍然单向）。
from ..adapters.base import AuthResult, AuthState, Page

__all__ = [
    "AuthRequired",
    "BrowserUnavailable",
    "DEFAULT_PROFILE_ROOT",
    "PROFILE_MODE",
    "PersistentBrowser",
    "SessionConfig",
    "SessionError",
    "check_login",
    "ensure_profile_dir",
    "login",
    "login_command",
    "profile_path",
]

DEFAULT_PROFILE_ROOT = Path.home() / ".geo-publish" / "profiles"
PROFILE_MODE = 0o700

logger = logging.getLogger("geopublish.session")

# Chrome 进程异常退出（被杀、崩溃、宿主沙箱拦截）会留下 Singleton* 锁文件，
# 下一次启动直接报 ProcessSingleton 错。这是真机验收（T-09）撞上两次的真实故障。
_STALE_LOCK_FILES = ("SingletonLock", "SingletonSocket", "SingletonCookie")

# 登录页扫码等人：默认走标准输入，测试注入假的等待函数
WaitForUser = Callable[[str], Any]


class SessionError(RuntimeError):
    """登录态层面可预期的失败（不是 bug）。"""


class BrowserUnavailable(SessionError):
    """浏览器驱动不可用 —— 环境问题，CLI 映射到退出码 5。"""


class AuthRequired(SessionError):
    """需要人工重新授权 —— CLI 映射到退出码 2。

    带上 `state` 和 `has_challenge` 而不是压成一个字符串：「登录过期」和「撞到人机校验」
    的处置不同（前者去扫码，后者必须停手等人），日报和现场包要能区分。
    """

    def __init__(
        self,
        platform: str,
        state: AuthState,
        reason: str = "",
        *,
        has_challenge: bool = False,
    ) -> None:
        self.platform = platform
        self.state = state
        self.reason = reason
        self.has_challenge = has_challenge
        super().__init__(self._message())

    def _message(self) -> str:
        detail = self.reason or "登录态不可用"
        if self.has_challenge:
            detail += "（页面出现人机校验：需人工完成，本工具不会自动重试）"
        return (
            f"{self.platform} 登录态不可用（{self.state.value}）：{detail}。"
            f"需重新授权：{login_command(self.platform)}"
        )


def login_command(platform: str) -> str:
    """报错里必须能直接复制粘贴的那条命令（UT-06-03）。"""
    return f"python -m geopublish login {platform}"


# --------------------------------------------------------------- profile 目录

def profile_path(platform: str, *, profile_root: str | Path | None = None) -> Path:
    """某个平台的 profile 目录。平台名小写，与 config 的 `key` 口径一致。"""
    root = Path(profile_root).expanduser() if profile_root is not None else DEFAULT_PROFILE_ROOT
    return root / str(platform).strip().lower()


def _harden(path: Path) -> None:
    """把目录收成 700。改不动就报错 —— 登录态目录不严，等于把钥匙放门口。"""
    if os.name != "posix":  # pragma: no cover - Windows 的 chmod 语义不同，不假装能管
        return
    try:
        os.chmod(path, PROFILE_MODE)
    except OSError as exc:
        raise SessionError(f"profile 目录权限无法设为 {PROFILE_MODE:o}：{path}（{exc}）") from exc


def ensure_profile_dir(path: str | Path) -> Path:
    """建目录并收紧权限：**新建的、早就存在的**都要收紧。

    只对新建生效是个常见漏法：手 `mkdir` 出来的 755 目录会一直留着，
    而它恰好是最可能被顺手 `cp` 进仓库的那一份。

    只收**绝对路径**：相对路径意味着「当前目录」，而这里的第一个动作就是 chmod 700 ——
    `--profile-root .` 会把整个 cwd 改成 700，且登录态落在随手 `cd` 的位置，
    下次换个目录就找不到、也更容易被误提交。
    """
    target = Path(path).expanduser()
    if not target.is_absolute():
        raise SessionError(
            f"profile 目录必须是绝对路径：{target}（默认 {DEFAULT_PROFILE_ROOT}）"
        )
    if target == target.parent:
        # 根目录自己是自己的父目录。放过去的话下一个动作就是 chmod / 成 700。
        raise SessionError(f"不能把根目录当 profile 目录：{target}")

    if target.exists() and not target.is_dir():
        # 先问再建，不靠异常类：macOS 的 mkdir 撞到同名文件时，
        # 异常类在 FileExistsError / PermissionError 之间漂移（errno 甚至还是 EEXIST），
        # 用 except 归类会写出「在 A 路径绿、B 路径红」的代码。
        raise SessionError(f"profile 路径被一个文件占住了，不是目录：{target}")

    try:
        target.mkdir(parents=True, exist_ok=True, mode=PROFILE_MODE)
    except OSError as exc:
        raise SessionError(f"profile 目录建不出来：{target}（{exc}）") from exc

    _harden(target)

    parent = target.parent
    # 上层跟着收紧：子目录 700 而上层 755，目录名和数量仍然可枚举。
    # 根目录的 name 是空串，跳过 —— 绝不去 chmod "/"。
    if parent.name and parent.is_dir():
        _harden(parent)
    return target


# --------------------------------------------------------------- 探活

def check_login(adapter: Any, page: Page, *, platform: str | None = None) -> AuthResult:
    """探活**一次**，不通过就抛 `AuthRequired`。

    这里刻意没有 `retry` / `attempts` 参数：R6 的验收是「不发起任何自动重试」，
    而这个函数是唯一的探活入口，堵在这里就够了（UT-06-01 数调用次数）。
    """
    name = platform or str(getattr(adapter, "name", "unknown"))
    result = adapter.check_auth(page)  # 恰好一次
    if result.state is AuthState.LOGGED_IN:
        return result
    raise AuthRequired(
        name, result.state, result.reason, has_challenge=result.has_challenge
    )


# --------------------------------------------------------------- 会话配置

@dataclass(frozen=True, slots=True)
class SessionConfig:
    profile_root: Path = DEFAULT_PROFILE_ROOT
    # None = 用 playwright 自带 chromium（CI）；"chrome" = 复用系统 Chrome
    channel: str | None = "chrome"
    # 必须 headed：扫码要人看，且 headless 更容易被判成机器人
    headless: bool = False
    locale: str = "zh-CN"
    viewport: tuple[int, int] = (1440, 900)
    timeout_ms: int = 30_000

    def __post_init__(self) -> None:
        object.__setattr__(self, "profile_root", Path(self.profile_root).expanduser())
        if self.timeout_ms <= 0:
            raise SessionError(f"timeout_ms 必须为正，收到 {self.timeout_ms}")
        if len(self.viewport) != 2 or min(self.viewport) <= 0:
            raise SessionError(f"viewport 必须是 (宽, 高)，收到 {self.viewport!r}")


def _default_launcher():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserUnavailable(
            "没装 playwright，登录不可用。修复：\n"
            "  pip install playwright && playwright install chromium"
        ) from exc
    return sync_playwright


# --------------------------------------------------------------- 持久会话

class PersistentBrowser:
    """持久 profile 的浏览器会话。`launcher` 可注入，单测不碰真浏览器。

    用 `launch_persistent_context` 而不是 `launch + new_context`：登录态要靠
    磁盘上的 user-data-dir 续命，后者关掉就没了（架构 §3.1 选型表那一行）。
    独立的 user_data_dir 还有个好处：和日常 Chrome 并存，不抢 profile 锁。
    """

    def __init__(self, config: SessionConfig | None = None, *, launcher: Any = None) -> None:
        self._config = config or SessionConfig()
        self._launcher = launcher

    @contextmanager
    def open(self, platform: str) -> Iterator[Page]:
        launcher = self._launcher or _default_launcher()
        cfg = self._config
        profile = ensure_profile_dir(profile_path(platform, profile_root=cfg.profile_root))

        kwargs: dict[str, Any] = {
            "user_data_dir": str(profile),
            "headless": cfg.headless,
            "locale": cfg.locale,
            "viewport": {"width": cfg.viewport[0], "height": cfg.viewport[1]},
        }
        if cfg.channel:
            # 只有真给了 channel 才传。传 None 会被 playwright 当成非法值。
            kwargs["channel"] = cfg.channel

        with launcher() as playwright:
            context = self._launch(playwright, profile, kwargs)
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.set_default_timeout(cfg.timeout_ms)
                yield page
            finally:
                # 不关就锁着 profile 目录，下一次登录会直接卡住
                context.close()

    def _launch(self, playwright: Any, profile: Path, kwargs: dict[str, Any]) -> Any:
        """启动持久上下文；撞上陈旧 Singleton 锁就清掉重试一次（只重试一次）。

        锁文件只在「上一次进程没正常退出」时残留。不处理的话，用户每次都要
        手工去删三个文件——而这个故障恰恰是自动化场景的高发态（进程被杀）。
        只清 Singleton* 三个文件，别的什么都不动；清完还起不来就原样抛出，
        那时多半是真有活着的实例在占着 profile，不该抢。
        """
        try:
            return playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:
            if "ProcessSingleton" not in str(exc) and "SingletonLock" not in str(exc):
                raise
            removed = []
            for name in _STALE_LOCK_FILES:
                lock = profile / name
                try:
                    lock.unlink(missing_ok=True)
                    removed.append(name)
                except OSError:
                    pass
            logger.warning("清掉陈旧 Chrome 锁文件后重试启动：%s", "、".join(removed))
            return playwright.chromium.launch_persistent_context(**kwargs)


# --------------------------------------------------------------- login 流程

def _url_of(adapter: Any, attribute: str) -> str:
    value = getattr(adapter, attribute, "") or ""
    return str(value).strip()


def login(
    adapter: Any,
    config: SessionConfig | None = None,
    *,
    launcher: Any = None,
    wait_for_user: WaitForUser | None = None,
) -> AuthResult:
    """开浏览器 → 停在登录页等人扫码 → 回车 → 去编辑器页回读一次。

    顺序不能省：先把人放到登录页，再等；回读放在编辑器页，因为那里才有权威信号。
    """
    cfg = config or SessionConfig()
    name = str(getattr(adapter, "name", "unknown"))
    editor_url = _url_of(adapter, "editor_url")
    if not editor_url:
        raise SessionError(f"{name} adapter 没给 editor_url，无法确认登录是否生效")
    login_url = _url_of(adapter, "login_url") or editor_url

    pause = wait_for_user or _wait_for_enter
    with PersistentBrowser(cfg, launcher=launcher).open(name) as page:
        page.goto(login_url)
        pause(
            f"请在弹出的浏览器里完成 {name} 登录（扫码）。"
            "完成后回到这里按回车，我会去编辑器页确认一次。"
        )
        page.goto(editor_url)
        return check_login(adapter, page, platform=name)


def _wait_for_enter(prompt: str) -> None:
    print(prompt, flush=True)
    try:
        input()
    except EOFError as exc:
        # cron / 管道 / 重定向里跑 login：stdin 没有可等的东西。
        # 不接住就是一句 EOFError 回溯，没人看得出「这命令必须在终端里跑」。
        raise SessionError(
            "登录需要交互：请在终端里执行 geopublish login <platform>（不要放在 cron 或管道里）"
        ) from exc
