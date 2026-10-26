"""G1–G8 缺口修复追溯元测试。

`docs/geo-publish-alignment-review.md` 核对出 8 处缺口（G1–G8），报告与
实施方案 v1.1 声称均已补齐。这张元测试防的是「报告说补了、文档或代码里
却悄悄丢了」的漂移 —— 与 `test_ci_tolerates_empty_test_layer` 同一思路：
凡是声称过的约束，必须有测试断言它真的存在。

断言分三层：
- 文档层：实施方案里每条补洞用例都带「补 Gx」追溯标记（双向追溯的锚点）；
- PRD 层：§7 分期表 P4 只写「监测回填」，掘金矛盾不得回归；
- 代码层：已开工工单（T-03、T-14）的补洞用例必须真的在测试代码里。
  未开工工单（T-10/15/18/19）的用例随各自工单落地，
  由工单 DoD 把关，这里不提前造一堆红测试卡死 CI。
"""

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PLAN_PATH = REPO_ROOT / "docs" / "geo-publish-implementation-plan.md"
PRD_PATH = REPO_ROOT / "docs" / "geo-publish-PRD.md"
GUARD_TESTS_PATH = REPO_ROOT / "tests" / "unit" / "test_guard.py"
PIPELINE_TESTS_PATH = REPO_ROOT / "tests" / "unit" / "test_pipeline.py"

# Gx → 该缺口的补洞用例编号（与 alignment-review §3 缺口清单一一对应）。
# G2 是 DoD 人工抽检清单，不是用例编号，单独在下面测。
GAP_CASES: dict[str, tuple[str, ...]] = {
    "G1": ("UT-13-05", "UT-13-06"),
    "G3": ("UT-14-08", "UT-14-09"),
    "G4": ("IT-18-01",),
    "G5": ("UT-03-07", "UT-03-08"),
    "G6": ("UT-18-05",),
    "G7": ("UT-19-06", "UT-19-07"),
    "G8": ("IT-10-01", "UT-12-07"),
}


@pytest.mark.unit
def test_plan_registers_every_gap_case_with_trace_tag():
    """实施方案里每条补洞用例必须存在，且所在行带「补 Gx」追溯标记。"""
    lines = PLAN_PATH.read_text(encoding="utf-8").splitlines()
    missing: list[str] = []
    for gap, cases in GAP_CASES.items():
        for case in cases:
            if not any(case in ln and f"补 {gap}" in ln for ln in lines):
                missing.append(f"{case}（{gap}）")
    assert not missing, f"实施方案丢了补洞用例或追溯标记：{', '.join(missing)}"


@pytest.mark.unit
def test_g2_manual_checklists_in_both_dod_sections():
    """G2：T-13、T-15 的 DoD 各有「人工抽检清单」，两处缺一不可。"""
    plan = PLAN_PATH.read_text(encoding="utf-8")
    assert "**人工抽检（补 G2，不可跳过）**" in plan, "T-13 DoD 缺人工抽检清单（G2）"
    assert "**人工抽检（补 G2）**" in plan, "T-15 DoD 缺人工抽检清单（G2）"


@pytest.mark.unit
def test_prd_p4_is_backfill_only_no_juejin():
    """PRD 内部矛盾修复留痕：P4 只写「监测回填」，掘金整体归二期。"""
    rows = [
        ln for ln in PRD_PATH.read_text(encoding="utf-8").splitlines()
        if ln.startswith("| P4 |")
    ]
    assert rows, "PRD §7 分期表找不到 P4 行"
    assert any("监测回填" in r for r in rows), "P4 行缺「监测回填」"
    assert all("掘金接入" not in r for r in rows), \
        "P4 又把掘金写回标题了 —— PRD §2.2（一期不做掘金）与 §7 的矛盾复发"


@pytest.mark.unit
@pytest.mark.parametrize(
    "test_name",
    [
        "test_ut_03_07_title_over_limit_rejects_draft",
        "test_ut_03_08_missing_ai_involvement_rejects_draft",
    ],
)
def test_g5_gap_cases_exist_in_test_code(test_name: str):
    """G5 是唯一宿主模块已开工的缺口：两条用例必须在 test_guard.py 里真的存在。"""
    assert test_name in GUARD_TESTS_PATH.read_text(encoding="utf-8"), \
        f"{test_name} 不在 test_guard.py —— G5 的代码层修复丢了"


@pytest.mark.unit
@pytest.mark.parametrize(
    "test_name",
    [
        "test_ut_14_08_body_length_within_platform_range",
        "test_ut_14_09_entity_density_and_single_canonical_name",
    ],
)
def test_g3_gap_cases_exist_in_test_code(test_name: str):
    """G3 的宿主工单 T-14 已开工（2026-10-26）：两条用例必须在 test_pipeline.py 里。"""
    assert test_name in PIPELINE_TESTS_PATH.read_text(encoding="utf-8"), \
        f"{test_name} 不在 test_pipeline.py —— G3 的代码层修复丢了"
