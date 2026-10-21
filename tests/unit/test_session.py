"""T-06 登录态管理（R6 / N2）。

R6 有一条最容易被实现成 bug 的细节：登录态检查「顺手加个重试」。
反复撞登录页是风控最敏感的信号，所以 UT-06-01 不用「结果对不对」断言，
而是用**调用次数 == 1** 钉死 —— 结果对不对看不出重试，次数看得出来。

其余四条：CLI 退出码 2、报错里带可执行命令、profile 目录 700、profile 不在 git 里。
后两条是 N2，属于「出事之前没人管、出事之后没法补救」的那类，必须测。
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from geopublish.adapters.base import AuthResult, AuthState
from geopublish.core import session as session_mod
from geopublish.core.session import (
    AuthRequired,
    BrowserUnavailable,
    SessionConfig,
    check_login,
    ensure_profile_dir,
    login,
    login_command,
    profile_path,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


class _AuthStub:
    """假 adapter：只实现 check_auth，并数调用次数。"""

    def __init__(self, state: AuthState, *, has_challenge: bool = False, reason: str = ""):
        self.name = "csdn"
        self.calls = 0
        self._result = AuthResult(state=state, has_challenge=has_challenge, reason=reason)

    def check_auth(self, page) -> AuthResult:
        self.calls += 1
        return self._result


class _FakePage:
    def __init__(self) -> None:
        self.url = "about:blank"
        self.goto_calls: list[str] = []
        self.timeout: int | None = None

    def goto(self, url: str) -> None:
        self.goto_calls.append(url)
        self.url = url

    def set_default_timeout(self, ms: int) -> None:
        self.timeout = ms


class _FakeContext:
    def __init__(self) -> None:
        self.page = _FakePage()
        self.pages = [self.page]
        self.closed = False

    def new_page(self) -> _FakePage:
        return self.page

    def close(self) -> None:
        self.closed = True


class _FakeBrowser:
    def __init__(self) -> None:
        self.kwargs: dict = {}
        self.context = _FakeContext()

    def launch_persistent_context(self, **kwargs):
        self.kwargs = kwargs
        return self.context


class _FakePlaywright:
    def __init__(self) -> None:
        self.chromium = _FakeBrowser()


class _FakeLauncher:
    """冒充 sync_playwright 的上下文管理器。"""

    def __init__(self) -> None:
        self.pw = _FakePlaywright()

    def __call__(self):
        return self

    def __enter__(self):
        return self.pw

    def __exit__(self, *exc_info) -> None:
        return None


# ------------------------------------------------------- UT-06-01 只探一次

def test_ut_06_01_expired_is_not_retried():
    """遇 expired：抛 AuthRequired，且 check_auth **只被调 1 次**。"""
    adapter = _AuthStub(AuthState.EXPIRED, reason="被踢回登录页")

    with pytest.raises(AuthRequired) as exc:
        check_login(adapter, _FakePage())

    assert adapter.calls == 1, f"登录态检查重试了 {adapter.calls} 次 —— 反复撞登录页会触发风控"
    assert exc.value.state is AuthState.EXPIRED


@pytest.mark.parametrize(
    "state, has_challenge",
    [(AuthState.UNKNOWN, False), (AuthState.UNKNOWN, True)],
)
def test_ut_06_01b_unknown_and_challenge_are_also_single_call(state, has_challenge):
    """未知/人机校验同样只探一次：多探一次就是多撞一次风控。"""
    adapter = _AuthStub(state, has_challenge=has_challenge)

    with pytest.raises(AuthRequired) as exc:
        check_login(adapter, _FakePage())

    assert adapter.calls == 1
    assert exc.value.has_challenge is has_challenge


def test_ut_06_01c_logged_in_passes_without_raising():
    adapter = _AuthStub(AuthState.LOGGED_IN, reason="看到头像")

    result = check_login(adapter, _FakePage())

    assert result.state is AuthState.LOGGED_IN
    assert adapter.calls == 1


def test_challenge_keeps_its_flag_for_the_caller():
    """has_challenge 不能被压成一个布尔「失败」：现场包和日报要靠它区分处置方式。"""
    adapter = _AuthStub(AuthState.UNKNOWN, has_challenge=True)

    with pytest.raises(AuthRequired) as exc:
        check_login(adapter, _FakePage())

    assert exc.value.has_challenge is True


# ------------------------------------------------------- UT-06-03 命令可执行

def test_ut_06_03_message_contains_runnable_command():
    adapter = _AuthStub(AuthState.EXPIRED)

    with pytest.raises(AuthRequired) as exc:
        check_login(adapter, _FakePage(), platform="csdn")

    message = str(exc.value)
    assert login_command("csdn") in message
    assert login_command("csdn") == "python -m geopublish login csdn"
    assert "需重新授权" in message


def test_login_command_uses_the_platform_name():
    assert "login juejin" in login_command("juejin")


# ------------------------------------------------------- UT-06-04 / 05 权限

def test_ut_06_04_profile_dir_is_700(tmp_path):
    target = ensure_profile_dir(tmp_path / "profiles" / "csdn")

    assert target.is_dir()
    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_profile_dir_is_tightened_even_if_it_already_exists(tmp_path):
    """早就存在的目录（比如手 mkdir 出来的 755）也要收紧，不能只对新建生效。"""
    target = tmp_path / "profiles" / "csdn"
    target.mkdir(parents=True)
    target.chmod(0o755)

    ensure_profile_dir(target)

    assert stat.S_IMODE(target.stat().st_mode) == 0o700


def test_profile_root_is_700_too(tmp_path):
    """上层目录松着，等于目录名可枚举 —— 只锁子目录没意义。"""
    root = tmp_path / "profiles"

    ensure_profile_dir(profile_path("csdn", profile_root=root))

    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_ut_06_05_profile_path_is_outside_the_repo_and_ignored():
    """默认 profile 在 ~/.geo-publish 下（仓库外），仓库里的 profiles/ 另有忽略规则。"""
    default = profile_path("csdn")

    assert ".geo-publish" in default.parts
    assert not str(default).startswith(str(REPO_ROOT)), "登录态落进工作区就等于随时会被提交"

    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    rules = {line.strip() for line in ignore.splitlines()}
    assert "**/profiles/" in rules, "仓库内若有 profiles/，必须被 .gitignore 挡住"


# ------------------------------------------------------- 浏览器会话（channel）

def test_channel_is_configurable_and_omitted_when_none(tmp_path):
    """本地跑 chrome（复用系统 155），CI 不给 channel 就用自带 chromium。"""
    fake = _FakeLauncher()

    with session_mod.PersistentBrowser(
        SessionConfig(profile_root=tmp_path / "profiles", channel="chrome"), launcher=fake
    ).open("csdn"):
        pass
    assert fake.pw.chromium.kwargs["channel"] == "chrome"

    fake2 = _FakeLauncher()
    with session_mod.PersistentBrowser(
        SessionConfig(profile_root=tmp_path / "profiles", channel=None), launcher=fake2
    ).open("csdn"):
        pass
    assert "channel" not in fake2.pw.chromium.kwargs, "channel=None 时不许把它塞进 launch 参数"


def test_session_opens_the_profile_dir_with_700(tmp_path):
    fake = _FakeLauncher()
    cfg = SessionConfig(profile_root=tmp_path / "profiles")

    with session_mod.PersistentBrowser(cfg, launcher=fake).open("csdn"):
        pass

    used = Path(fake.pw.chromium.kwargs["user_data_dir"])
    assert used.is_dir()
    assert stat.S_IMODE(used.stat().st_mode) == 0o700


def test_session_closes_the_context_even_on_error(tmp_path):
    """不关 context，profile 目录的锁不会释放，下一次登录直接卡住。"""
    fake = _FakeLauncher()

    with pytest.raises(RuntimeError):
        with session_mod.PersistentBrowser(
            SessionConfig(profile_root=tmp_path / "profiles"), launcher=fake
        ).open("csdn"):
            raise RuntimeError("模拟中途崩溃")

    assert fake.pw.chromium.context.closed is True


def test_missing_playwright_is_reported_as_browser_unavailable(monkeypatch, tmp_path):
    def _boom():
        raise BrowserUnavailable("没装 playwright")

    monkeypatch.setattr(session_mod, "_default_launcher", _boom)

    with pytest.raises(BrowserUnavailable):
        with session_mod.PersistentBrowser(SessionConfig(profile_root=tmp_path)).open("csdn"):
            pass


# ------------------------------------------------------- login 子流程

def test_login_opens_login_page_then_verifies_in_the_editor(tmp_path):
    """先到登录页让人扫码，回读时去编辑器页 —— 那里才是权威信号。"""
    fake = _FakeLauncher()
    adapter = _AuthStub(AuthState.LOGGED_IN)
    adapter.login_url = "https://passport.csdn.net/login"
    adapter.editor_url = "https://mp.csdn.net/mp_blog/creation/editor"
    asked: list[str] = []

    result = login(
        adapter,
        SessionConfig(profile_root=tmp_path / "profiles"),
        launcher=fake,
        wait_for_user=lambda prompt: asked.append(prompt),
    )

    assert result.state is AuthState.LOGGED_IN
    assert fake.pw.chromium.context.page.goto_calls == [
        adapter.login_url,
        adapter.editor_url,
    ]
    assert len(asked) == 1, "必须停下来等人扫码，不能自己往下走"


def test_login_that_ends_still_logged_out_raises_auth_required(tmp_path):
    """扫完还是没登录（扫错号/超时）→ 走 AuthRequired，退出码 2，不自己重试。"""
    fake = _FakeLauncher()
    adapter = _AuthStub(AuthState.EXPIRED)
    adapter.login_url = "https://passport.csdn.net/login"
    adapter.editor_url = "https://mp.csdn.net/mp_blog/creation/editor"

    with pytest.raises(AuthRequired):
        login(
            adapter,
            SessionConfig(profile_root=tmp_path / "profiles"),
            launcher=fake,
            wait_for_user=lambda prompt: None,
        )

    assert adapter.calls == 1


# ------------------------------------------------------- UT-06-02 CLI 退出码

def test_ut_06_02_cli_exit_code_is_2(monkeypatch, capsys):
    from geopublish import cli

    def _raise(*args, **kwargs):
        raise AuthRequired("csdn", AuthState.EXPIRED, "被踢回登录页")

    monkeypatch.setattr(cli, "login", _raise)

    code = cli.main(["login", "csdn"])

    assert code == cli.EXIT_AUTH == 2
    err = capsys.readouterr().err
    assert login_command("csdn") in err, "退出码之外还要告诉人怎么修"


def test_cli_login_reports_missing_browser_as_env_error(monkeypatch, capsys):
    from geopublish import cli

    def _raise(*args, **kwargs):
        raise BrowserUnavailable("没装 playwright")

    monkeypatch.setattr(cli, "login", _raise)

    assert cli.main(["login", "csdn"]) == cli.EXIT_ENV == 5
    assert "playwright" in capsys.readouterr().err
