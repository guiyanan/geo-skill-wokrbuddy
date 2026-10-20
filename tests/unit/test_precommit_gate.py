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
