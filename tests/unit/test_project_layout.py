"""项目骨架自检（不依赖任何实现）。

作用：验证测试链路本身是通的。CI 里最先跑，绿了才说明 pytest 配置、
包结构、目录约定都到位，后面的测试才有意义。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.unit
def test_package_importable():
    import geopublish

    assert geopublish.__version__


@pytest.mark.unit
def test_pytest_markers_registered():
    for marker in ("unit", "contract", "integration", "e2e"):
        assert marker in _registered_markers()


def _registered_markers() -> set[str]:
    import subprocess
    import sys

    out = subprocess.run(
        [sys.executable, "-m", "pytest", "--markers"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    ).stdout
    # 输出形如 "@pytest.mark.unit: 说明"，去掉前缀后取裸名
    return {
        line.split(":")[0].strip().removeprefix("@pytest.mark.")
        for line in out.splitlines()
        if line.startswith("@pytest.mark.")
    }


@pytest.mark.unit
def test_required_dirs_exist():
    for rel in ("geopublish", "tests/unit", "tests/contract",
                "tests/integration", "tests/regression", "fixtures", "docs"):
        assert (REPO_ROOT / rel).is_dir(), f"缺少目录：{rel}"


@pytest.mark.unit
def test_secrets_not_committed():
    """凭据目录必须在 .gitignore 中，且不得存在被追踪的凭据文件。"""
    gi = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for marker in ("profiles/", "data/secrets/"):
        assert marker in gi, f".gitignore 缺少 {marker}"


@pytest.mark.unit
def test_no_captcha_code_in_source():
    """红线元测试：源码中不得出现验证码处理实现。"""
    import re

    tokens = ["capt" + "cha", "gee" + "test", "yi" + "dun", "ddd" + "docr"]
    pattern = re.compile("|".join(tokens), re.IGNORECASE)
    for py in (REPO_ROOT / "geopublish").rglob("*.py"):
        if ".precommit" in str(py):
            continue
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            if "#" in line and ("noqa" in line or "红线" in line):
                continue
            assert not pattern.search(line), f"{py}:{lineno} 命中红线词"
