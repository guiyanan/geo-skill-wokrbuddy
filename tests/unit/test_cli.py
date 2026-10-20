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
    EXIT_CONFIG_ERROR,
    EXIT_ENV,
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
        main(["publish"])

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
