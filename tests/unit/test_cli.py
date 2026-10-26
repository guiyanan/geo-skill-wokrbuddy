"""T-01b · CLI 冒烟（`--version` + `check-config`）。

配置错误必须在发布之前暴露。所以 check-config 是运维入口，不是玩具：
合法配置退出 0 并打印摘要，非法配置退出 3 并把字段名说清楚。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

import geopublish
from geopublish.cli import (
    EXIT_AUTH,
    EXIT_CONFIG_ERROR,
    EXIT_ENV,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_REPRODUCED,
    EXIT_USAGE,
    main,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def config_file(tmp_path):
    def _write(**over):
        data = {
            "gate": "draft",
            "platforms": [{"name": "csdn", "daily_limit": 1, "publish_at": "10:00"}],
            "report": {"email": "shiyimi@joto.example"},
        }
        data.update(over)
        path = tmp_path / "geopublish.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    return _write


def test_version_prints_package_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])

    assert exc.value.code == 0
    assert geopublish.__version__ in capsys.readouterr().out


def test_version_runs_as_module():
    """`python -m geopublish --version` 必须能跑（部署脚本靠它探活）。"""
    result = subprocess.run(
        [sys.executable, "-m", "geopublish", "--version"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0
    assert geopublish.__version__ in result.stdout


def test_no_args_is_a_usage_error(capsys):
    """无子命令 = 用法错误，退出码必须是 1。

    cron 里跑 `geopublish`（忘了写子命令）如果退 0，监控系统会当成「今天跑成功了」，
    第二天才发现一篇都没发 —— 静默成功比直接报错危险得多。
    """
    assert main([]) == EXIT_USAGE
    assert "check-config" in capsys.readouterr().err, "用法错误走 stderr，别污染 stdout"


def test_unknown_flag_is_a_usage_error():
    """argparse 默认是退出 2，这里统一到约定的 1。"""
    with pytest.raises(SystemExit) as exc:
        main(["--nope"])

    assert exc.value.code == EXIT_USAGE


def test_unknown_subcommand_is_a_usage_error():
    """子命令拼错也要是 1（子解析器必须继承同一个 error 行为）。"""
    with pytest.raises(SystemExit) as exc:
        main(["publsh"])

    assert exc.value.code == EXIT_USAGE


def test_no_args_exit_code_is_visible_to_the_shell():
    """真正跑一遍 `python -m geopublish`：退出码要能被 shell 看到。"""
    result = subprocess.run(
        [sys.executable, "-m", "geopublish"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )

    assert result.returncode == EXIT_USAGE


def test_help_still_exits_zero():
    """`--help` 是求救不是犯错，别一起打成 1。"""
    with pytest.raises(SystemExit) as exc:
        main(["--help"])

    assert exc.value.code == EXIT_OK

    with pytest.raises(SystemExit) as exc2:
        main(["check-config", "--help"])

    assert exc2.value.code == EXIT_OK


def test_check_config_ok_prints_summary(config_file, capsys):
    code = main(["check-config", str(config_file())])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "csdn" in out and "10:00" in out and "draft" in out


def test_check_config_reports_error_with_exit_3(config_file, capsys):
    path = config_file(gate="ship")

    code = main(["check-config", str(path)])

    captured = capsys.readouterr()
    assert code == EXIT_CONFIG_ERROR
    assert "gate" in captured.err


def test_check_config_missing_file(tmp_path, capsys):
    code = main(["check-config", str(tmp_path / "nope.json")])

    assert code == EXIT_CONFIG_ERROR
    assert "不存在" in capsys.readouterr().err


def test_check_config_never_raises(config_file):
    """CLI 的错误一律转成退出码，别把 traceback 丢给定时任务。"""
    assert main(["check-config", str(config_file(platforms=[]))]) == EXIT_CONFIG_ERROR


# ------------------------------------------------- T-04：snapshot / incident

from geopublish.core.clock import FakeClock  # noqa: E402
from geopublish.core.incident import Capture, Incident, archive  # noqa: E402


class _StubDriver:
    name = "stub"

    def fetch(self, url):
        return Capture(page_html="<html><body>ok</body></html>", screenshot=b"\x89PNG ok",
                       network_har="{}", console_log="")


def _bundle(tmp_path):
    incident = Incident(
        ticket="T-07", stage="inject", adapter="csdn",
        adapter_version="0.1.0", selectors_version="2026-10-21.1",
        prd_refs=("R12",), error="InjectFailed: 三级注入全失败",
        payload={"slug": "x-y"},
        capture=Capture(page_html="<html></html>", screenshot=b"\x89PNG", network_har="{}",
                        console_log=""),
    )
    return archive(incident, tmp_path / "incidents", clock=FakeClock("2026-10-21T10:00:00+08:00"))


def test_snapshot_command_writes_fixture(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("geopublish.cli.PlaywrightDriver", _StubDriver)
    out = tmp_path / "shots"

    code = main(["snapshot", "https://mp.csdn.net/edit", "csdn/editor", "--out", str(out)])

    assert code == EXIT_OK
    assert (out / "csdn" / "editor" / "page.html").exists()
    assert "csdn/editor" in capsys.readouterr().out


def test_snapshot_bad_name_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("geopublish.cli.PlaywrightDriver", _StubDriver)

    code = main(["snapshot", "https://x.test/", "../etc/passwd", "--out", str(tmp_path)])

    assert code == EXIT_USAGE
    assert "name" in capsys.readouterr().err


def test_snapshot_missing_driver_exits_5(tmp_path, monkeypatch, capsys):
    """缺浏览器依赖不是「用法错」，cron 要能据此报「环境没装好」。"""

    class _Boom:
        name = "boom"

        def fetch(self, url):
            from geopublish.core.snapshot import DriverUnavailable

            raise DriverUnavailable("没装: pip install playwright")

    monkeypatch.setattr("geopublish.cli.PlaywrightDriver", _Boom)

    code = main(["snapshot", "https://x.test/", "csdn/editor", "--out", str(tmp_path)])

    assert code == EXIT_ENV
    assert "pip install playwright" in capsys.readouterr().err


def test_incident_replay_command_exits_4(tmp_path, capsys):
    """repro.sh 调的就是这条；退出码 4 = 现场已复现（预期失败）。"""
    path = _bundle(tmp_path)

    code = main(["incident", "replay", str(path)])

    captured = capsys.readouterr()
    assert code == EXIT_REPRODUCED
    assert "T-07" in captured.err and "inject" in captured.err


def test_incident_replay_missing_bundle_is_a_usage_error(tmp_path, capsys):
    code = main(["incident", "replay", str(tmp_path / "nope")])

    assert code == EXIT_USAGE
    assert "现场包" in capsys.readouterr().err


def test_snapshot_empty_page_is_an_env_error(tmp_path, monkeypatch, capsys):
    """页面抓回来是空的（登录墙/跳转失败）：环境问题，不是用法错。"""

    class _Blank:
        name = "blank"

        def fetch(self, url):
            return Capture(page_html="", screenshot=b"\x89PNG", network_har="{}")

    monkeypatch.setattr("geopublish.cli.PlaywrightDriver", _Blank)

    code = main(["snapshot", "https://x.test/", "csdn/editor", "--out", str(tmp_path)])

    assert code == EXIT_ENV
    assert "空" in capsys.readouterr().err


def test_snapshot_bad_url_is_a_usage_error(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr("geopublish.cli.PlaywrightDriver", _StubDriver)

    code = main(["snapshot", "ftp://x.test/a", "csdn/editor", "--out", str(tmp_path)])

    assert code == EXIT_USAGE
    assert "http" in capsys.readouterr().err


def test_incident_replay_broken_bundle_is_an_env_error(tmp_path, capsys):
    """包在但坏了：不能报「复现成功」(4)，否则 repro.sh 会假装拿到了证据。"""
    path = _bundle(tmp_path)
    (path / "bundle.json").write_text("{ 坏掉了", encoding="utf-8")

    code = main(["incident", "replay", str(path)])

    assert code == EXIT_ENV
    assert "bundle.json" in capsys.readouterr().err


# ------------------------------------------------- T-09：publish --mode draft

from geopublish.adapters.base import AuthState, SubmitBlocked  # noqa: E402
from geopublish.core.config import Gate  # noqa: E402
from geopublish.core.session import AuthRequired, BrowserUnavailable  # noqa: E402
from geopublish.publish import GateClosed, PublishOutcome  # noqa: E402


@pytest.fixture
def draft_dir(tmp_path):
    root = tmp_path / "draft"
    root.mkdir()
    (root / "meta.json").write_text(
        json.dumps({"slug": "t09-cli", "title": "探针标题", "ai_involvement": "assisted"}),
        encoding="utf-8",
    )
    (root / "csdn.md").write_text("正文" * 100, encoding="utf-8")
    return root


def _stub_publish(monkeypatch, outcome=None, error=None):
    seen: dict = {}

    def _fake(adapter, loaded, platform_cfg, **kwargs):
        seen["title"] = loaded.draft.meta.title
        seen["adapter"] = adapter.name
        seen["platform"] = platform_cfg.name
        seen.update(kwargs)
        if error is not None:
            raise error
        return outcome or PublishOutcome(
            ok=True, url="https://mp.csdn.net/x/1", article_id="1", reason="命中"
        )

    monkeypatch.setattr("geopublish.cli.publish_draft", _fake)
    return seen


def test_publish_draft_ok_prints_article_id(monkeypatch, capsys, config_file, draft_dir):
    seen = _stub_publish(monkeypatch)

    code = main(["publish", str(draft_dir), "--config", str(config_file())])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "探针标题" in out and "article_id = 1" in out
    assert seen["gate"] is Gate.DRAFT, "闸门必须从配置读，不能默认放行"
    assert seen["mode"] == "draft"
    assert seen["adapter"] == "csdn"


def test_publish_mode_publish_passes_through_to_orchestration(
    monkeypatch, capsys, config_file, draft_dir
):
    """T-10 起 publish 模式不再在参数层拒绝——原样传给编排层，闸门在 check_gate 守。"""
    seen = _stub_publish(monkeypatch)

    code = main(["publish", str(draft_dir), "--mode", "publish", "--config", str(config_file())])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert seen["mode"] == "publish"
    assert "已发布" in out and "article_id = 1" in out


def test_publish_mode_publish_prints_receipt_path(
    monkeypatch, capsys, config_file, draft_dir, tmp_path
):
    """发布成功要打印回执路径（R10：一条命令出 URL + 回执落盘位置）。"""
    receipt = tmp_path / "runs" / "20261022_100000" / "receipt.json"
    _stub_publish(
        monkeypatch,
        outcome=PublishOutcome(
            ok=True,
            url="https://blog.csdn.net/probe/article/details/42",
            article_id="42",
            reason="命中",
            run_dir=receipt.parent,
            receipt_path=receipt,
        ),
    )

    code = main(["publish", str(draft_dir), "--mode", "publish", "--config", str(config_file())])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert str(receipt) in out
    assert "before.png / after.png" in out


def test_publish_mode_receipt_failed_warns_on_stderr(
    monkeypatch, capsys, config_file, draft_dir
):
    """发成了但没存上证（receipt_failed）：退出码仍是 0，但 stderr 必须喊出来。"""
    _stub_publish(
        monkeypatch,
        outcome=PublishOutcome(
            ok=True,
            url="https://blog.csdn.net/probe/article/details/42",
            article_id="42",
            reason="命中",
            receipt_failed=True,
        ),
    )

    code = main(["publish", str(draft_dir), "--mode", "publish", "--config", str(config_file())])

    captured = capsys.readouterr()
    assert code == EXIT_OK
    assert "receipt_failed" in captured.err


def test_publish_unconfirmed_is_exit_6(monkeypatch, capsys, config_file, draft_dir):
    _stub_publish(
        monkeypatch,
        outcome=PublishOutcome(ok=False, url="https://mp.csdn.net/x", reason="草稿列表页没找到"),
    )

    code = main(["publish", str(draft_dir), "--config", str(config_file())])

    err = capsys.readouterr().err
    assert code == EXIT_FAILED
    assert "未确认成功" in err and "没找到" in err
    assert "当前页面" in err


def test_publish_unconfirmed_without_url_still_fails_cleanly(
    monkeypatch, capsys, config_file, draft_dir
):
    """verify 连当前页面都回读不到时，不该多打印一行空 URL。"""
    _stub_publish(monkeypatch, outcome=PublishOutcome(ok=False, reason="读不到编辑器标题"))

    code = main(["publish", str(draft_dir), "--config", str(config_file())])

    captured = capsys.readouterr()
    assert code == EXIT_FAILED
    assert "当前页面" not in captured.err


def test_publish_success_with_screenshot_and_no_url(monkeypatch, capsys, config_file, draft_dir):
    """草稿模式成功时可能没有公开 URL（只进了草稿箱），但要打印截图路径。"""
    run_dir = draft_dir.parent / "runs" / "20261021_152030"
    _stub_publish(
        monkeypatch,
        outcome=PublishOutcome(ok=True, url="", article_id="139821477", reason="列表命中", run_dir=run_dir),
    )

    code = main(["publish", str(draft_dir), "--config", str(config_file())])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "139821477" in out
    assert "url        =" not in out
    assert "draft.png" in out


def test_publish_config_error_is_exit_3(monkeypatch, capsys, config_file, draft_dir):
    _stub_publish(monkeypatch)

    assert main(["publish", str(draft_dir), "--config", str(config_file(gate="ship"))]) == (
        EXIT_CONFIG_ERROR
    )
    assert "gate" in capsys.readouterr().err


def test_publish_platform_not_in_config_is_exit_3(monkeypatch, capsys, config_file, draft_dir):
    _stub_publish(monkeypatch)

    code = main(
        ["publish", str(draft_dir), "--platform", "juejin", "--config", str(config_file())]
    )

    assert code == EXIT_CONFIG_ERROR
    assert "juejin" in capsys.readouterr().err


def test_publish_bad_draft_dir_is_a_usage_error(monkeypatch, capsys, config_file, tmp_path):
    _stub_publish(monkeypatch)

    code = main(["publish", str(tmp_path / "nope"), "--config", str(config_file())])

    assert code == EXIT_USAGE
    assert "稿件目录不存在" in capsys.readouterr().err


def test_publish_real_load_draft_error_is_a_usage_error(
    monkeypatch, capsys, config_file, tmp_path
):
    """真 load_draft 跑一遍：meta.json 缺字段属于「稿件有问题」，不是环境问题。"""
    _stub_publish(monkeypatch)
    root = tmp_path / "d"
    root.mkdir()
    (root / "meta.json").write_text(json.dumps({"slug": "x"}), encoding="utf-8")
    (root / "csdn.md").write_text("正文", encoding="utf-8")

    code = main(["publish", str(root), "--config", str(config_file())])

    assert code == EXIT_USAGE
    assert "meta.json" in capsys.readouterr().err


@pytest.mark.parametrize(
    "error, expected",
    [
        (AuthRequired("csdn", AuthState.EXPIRED, "被踢回登录页"), EXIT_AUTH),
        (BrowserUnavailable("没装 playwright"), EXIT_ENV),
        (GateClosed("gate=dry_run"), EXIT_CONFIG_ERROR),
        (SubmitBlocked("提交前复核失败"), EXIT_FAILED),
    ],
    ids=["auth", "browser", "gate", "blocked"],
)
def test_publish_maps_each_failure_to_its_own_exit_code(
    monkeypatch, capsys, config_file, draft_dir, error, expected
):
    """退出码得能指向正确的修复动作：去扫码 / 装浏览器 / 改配置 / 看后台。"""
    _stub_publish(monkeypatch, error=error)

    assert main(["publish", str(draft_dir), "--config", str(config_file())]) == expected
    assert capsys.readouterr().err


# ------------------------------------------------- T-11：幂等与并发锁

from geopublish.core.lock import DEFAULT_LOCK_ROOT, LockError  # noqa: E402


def test_publish_lock_conflict_exits_3_without_browser(monkeypatch, capsys, config_file, draft_dir):
    """UT-11-02 CLI 面：另一进程持有平台锁 → 退 3（与配置错误同码：这次运行根本没开始）。

    锁在 publish_draft 内部、open_page 之前拿 —— 这里 stub 掉 publish_draft，
    锁冲突抛 LockError，PersistentBrowser 从头到尾只是被构造，open 不会被调。
    """
    _stub_publish(monkeypatch, error=LockError(Path("/fake/csdn.lock"), None))

    code = main(["publish", str(draft_dir), "--config", str(config_file())])

    assert code == EXIT_CONFIG_ERROR == 3
    err = capsys.readouterr().err
    assert "并发冲突" in err and "csdn.lock" in err


def test_publish_skipped_duplicate_exits_0_and_says_so(monkeypatch, capsys, config_file, draft_dir):
    """R9 验收原话：第二次输出 SKIPPED_DUPLICATE。退 0 —— 幂等命中不是失败。"""
    _stub_publish(
        monkeypatch,
        outcome=PublishOutcome(
            ok=True,
            url="https://blog.csdn.net/probe/article/details/42",
            article_id="42",
            reason="SKIPPED_DUPLICATE",
            skipped_duplicate=True,
        ),
    )

    code = main(["publish", str(draft_dir), "--mode", "publish", "--config", str(config_file())])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "SKIPPED_DUPLICATE" in out
    assert "details/42" in out, "要把已发布的 URL 给人，方便核对"
    assert "已发布：" not in out, "本次一行没发，不许说「已发布」"


def test_cli_passes_default_lock_root_to_orchestration(monkeypatch, config_file, draft_dir):
    """CLI 永远带锁跑（T-11）：lock_root 默认落到 ~/.geo-publish/locks。"""
    seen = _stub_publish(monkeypatch)

    main(["publish", str(draft_dir), "--config", str(config_file())])

    assert seen["lock_root"] == DEFAULT_LOCK_ROOT


def test_cli_lock_root_override(monkeypatch, config_file, draft_dir, tmp_path):
    seen = _stub_publish(monkeypatch)

    main(["publish", str(draft_dir), "--config", str(config_file()),
          "--lock-root", str(tmp_path / "locks")])

    assert seen["lock_root"] == tmp_path / "locks"


def test_publish_skipped_duplicate_without_url_still_exits_0(
    monkeypatch, capsys, config_file, draft_dir
):
    """幂等命中但历史行没 URL（早期数据形态）：照常退 0，只是少打一行 url。"""
    _stub_publish(
        monkeypatch,
        outcome=PublishOutcome(ok=True, reason="SKIPPED_DUPLICATE", skipped_duplicate=True),
    )

    code = main(["publish", str(draft_dir), "--mode", "publish", "--config", str(config_file())])

    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "SKIPPED_DUPLICATE" in out and "url" not in out


# ------------------------------------------------- T-12：失败分类的 CLI 出口

from geopublish.adapters.base import ContentRejected  # noqa: E402
from geopublish.core.config import MAX_RETRY  # noqa: E402
from geopublish.core.errors import FailureKind  # noqa: E402


def test_publish_auth_expired_is_exit_2(monkeypatch, capsys, config_file, draft_dir):
    """UT-12-02：登录失效 → 退 2，输出重新授权命令，且没有重试。"""
    _stub_publish(
        monkeypatch,
        error=AuthRequired("csdn", AuthState.EXPIRED, "被踢回登录页"),
    )

    code = main(["publish", str(draft_dir), "--mode", "publish", "--config", str(config_file())])

    err = capsys.readouterr().err
    assert code == EXIT_AUTH
    assert "需重新授权" in err and "login csdn" in err


def test_publish_passes_retry_max_from_config(monkeypatch, config_file, draft_dir):
    """T-12：运行内重试预算从配置读，默认 = MAX_RETRY（全局硬顶）。"""
    seen = _stub_publish(monkeypatch)
    main(["publish", str(draft_dir), "--config", str(config_file())])
    assert seen["retry_max"] == MAX_RETRY

    seen2 = _stub_publish(monkeypatch)
    main(["publish", str(draft_dir), "--config", str(config_file(retry_max=1))])
    assert seen2["retry_max"] == 1


def test_publish_classified_failure_prints_kind_tag(monkeypatch, capsys, config_file, draft_dir):
    """分类失败的输出带类别标签：人与 cron 一眼看出该扫码、改稿还是等网络。"""
    _stub_publish(
        monkeypatch,
        outcome=PublishOutcome(
            ok=False,
            reason="平台拒稿：内容包含敏感词",
            failure_kind=FailureKind.CONTENT_REJECTED,
        ),
    )

    code = main(["publish", str(draft_dir), "--mode", "publish", "--config", str(config_file())])

    err = capsys.readouterr().err
    assert code == EXIT_FAILED
    assert "[CONTENT_REJECTED]" in err
    assert "敏感词" in err


def test_publish_content_rejected_in_draft_mode_is_exit_6(
    monkeypatch, capsys, config_file, draft_dir
):
    """draft 模式没有日志层，拒稿异常原样上来：退 6 且平台原话不丢。"""
    _stub_publish(monkeypatch, error=ContentRejected("内容包含敏感词：「免费试用」"))

    code = main(["publish", str(draft_dir), "--config", str(config_file())])

    err = capsys.readouterr().err
    assert code == EXIT_FAILED
    assert "敏感词" in err
