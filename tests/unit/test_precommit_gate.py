"""红线门禁自身的回归测试（禁止只测业务、不测门禁）。

事故记录：T-02 提交时 `recover_status` 的前身 `re|solve_interrupted` 被门禁
当成「验证码求解」拦下 —— 裸子串 `solve_` 会误伤 resolve_/absolve_ 这类正常词。
门禁误报的代价是有人开始加 noqa，最后红线形同摆设，所以这里钉住两侧：
正常词必须放过，真红线词必须拦住。
"""

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
GATE = REPO_ROOT / ".precommit" / "check_no_captcha_code.py"

SOLVE = "sol" + "ve_"
CAP = "cap" + "tcha"


def run_gate(tmp_path: Path, code: str) -> subprocess.CompletedProcess:
    sample = tmp_path / "sample.py"
    sample.write_text(code, encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(GATE), str(sample)],
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_gate_allows_words_containing_solve(tmp_path):
    """re|solve_interrupted 这种正常命名不能被当成红线词。"""
    result = run_gate(tmp_path, f"def re{SOLVE}interrupted(status):\n    return status\n")

    assert result.returncode == 0, result.stdout


@pytest.mark.unit
def test_gate_blocks_real_red_line(tmp_path):
    result = run_gate(tmp_path, f"def {SOLVE}challenge(img):\n    pass\n")

    assert result.returncode == 1
    assert "红线" in result.stdout


@pytest.mark.unit
def test_gate_blocks_captcha_by_name(tmp_path):
    result = run_gate(tmp_path, f"def handle_{CAP}(page):\n    pass\n")

    assert result.returncode == 1


# ------------------------------------------------- CI 必须真的执行这两条红线
def test_ci_runs_static_gates_before_unit_tests():
    """门禁不接进 CI = 不存在。事故：两条红线长期只在未安装的 pre-commit 里。"""
    workflow = (REPO_ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")

    assert "run_gates.py" in workflow, "CI 没跑静态门禁"
    assert workflow.index("run_gates.py") < workflow.index("pytest -m unit"), (
        "静态门禁要排在单元测试之前：便宜的先跑，红得越早越好"
    )


def test_local_hook_uses_same_runner():
    """本地钩子和 CI 必须共用同一份目标发现逻辑，否则两边 glob 会漂。"""
    config = (REPO_ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8")

    assert "run_gates.py" in config


# ------------------------------------------------- run_gates 自身
def _run_gates(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / ".precommit" / "run_gates.py"), "--root", str(root)],
        capture_output=True,
        text=True,
    )


@pytest.mark.unit
def test_run_gates_reports_file_counts(tmp_path):
    (tmp_path / "geopublish" / "adapters" / "csdn").mkdir(parents=True)
    (tmp_path / "geopublish" / "core").mkdir(parents=True)
    (tmp_path / "geopublish" / "core" / "state.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "geopublish" / "adapters" / "csdn" / "flow.py").write_text(
        "y = 2\n", encoding="utf-8"
    )

    result = _run_gates(tmp_path)

    assert result.returncode == 0, result.stdout
    assert "no-captcha-code" in result.stdout and "no-naked-selectors" in result.stdout
    # 报数量是关键：门禁静默通过和没跑起来看起来一模一样
    assert "1 个文件" in result.stdout


@pytest.mark.unit
def test_run_gates_fails_on_captcha_code(tmp_path):
    (tmp_path / "geopublish" / "core").mkdir(parents=True)
    (tmp_path / "geopublish" / "core" / "bad.py").write_text(
        f"def {SOLVE}challenge(img):\n    pass\n", encoding="utf-8"
    )

    result = _run_gates(tmp_path)

    assert result.returncode == 1
    assert "no-captcha-code" in result.stdout


@pytest.mark.unit
def test_run_gates_fails_on_naked_selector(tmp_path):
    (tmp_path / "geopublish" / "adapters" / "csdn").mkdir(parents=True)
    (tmp_path / "geopublish" / "adapters" / "csdn" / "flow.py").write_text(
        'page.click(".cm-content__x")\n', encoding="utf-8"
    )

    result = _run_gates(tmp_path)

    assert result.returncode == 1
    assert "no-naked-selectors" in result.stdout


@pytest.mark.unit
def test_run_gates_tolerates_missing_layer(tmp_path):
    """adapters 还没建起来时，选择器门禁应显式报「0 个文件」，不是假装通过。"""
    (tmp_path / "geopublish" / "core").mkdir(parents=True)

    result = _run_gates(tmp_path)

    assert result.returncode == 0
    assert "0 个文件" in result.stdout
