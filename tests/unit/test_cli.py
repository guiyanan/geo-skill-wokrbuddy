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
from geopublish.cli import EXIT_CONFIG_ERROR, EXIT_OK, main

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


def test_no_args_shows_help(capsys):
    assert main([]) == EXIT_OK
    assert "check-config" in capsys.readouterr().out


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
