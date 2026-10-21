"""T-07 正文注入（三级降级）。

钉死的行为：
- L1 模拟粘贴 → L2 CodeMirror 实例 → L3 分段 type，顺序固定，不许跳级
- 每级注入后**回读**比对，长度偏差 > 2% 视为该级失败（不验证就提交 = 盲发）
- 单级超时限 = 该级失败（时间走注入时钟，不是墙钟）
- 三级全灭 → InjectFailed，携带三级尝试记录（上层拿它归档现场包），**不做任何提交动作**
- 每次降级都有 WARNING 日志（UT-07-03 要能看到两次）
"""

from __future__ import annotations

import logging
import math

import pytest

from geopublish.adapters.base import InjectFailed
from geopublish.adapters.csdn.flow import (
    SEGMENT_SIZE,
    CsdnAdapter,
    read_editor_text,
)
from geopublish.core.clock import FakeClock
from geopublish.core.models import Draft, Meta

pytestmark = pytest.mark.unit

CLOCK_START = "2026-10-21T10:00:00+08:00"


def _draft(body: str) -> Draft:
    return Draft(meta=Meta(slug="t07-probe", title="探针稿件"), body=body)


class FakeEditorPage:
    """假编辑器页：按 op 注释分发 evaluate，内部维护一份 doc 模拟 CodeMirror。

    behavior 取值：
    - "ok"       注入成功，doc 变成全文
    - "truncate" 注入「成功」但只进了一半（粘贴被平台拦就是这种）
    - "deny"     脚本返回 ok:false（拿不到 CM 实例）
    - "raise"    evaluate 直接抛异常（元素不存在/超时）
    """

    def __init__(
        self,
        *,
        l1: str = "ok",
        l2: str = "ok",
        type_ok: bool = True,
        readback_ok: bool = True,
        clock: FakeClock | None = None,
        evaluate_cost_s: float = 0.0,
    ) -> None:
        self.doc = ""
        self.plan = {"paste": l1, "dispatch": l2}
        self.type_ok = type_ok
        self.readback_ok = readback_ok
        self.clock = clock
        self.evaluate_cost_s = evaluate_cost_s
        self.calls: list[tuple[str, object]] = []
        self.url = "https://mp.csdn.net/"

    # --- 写操作（生产代码只允许通过这三个口子碰页面） ---

    def click(self, selector: str) -> None:
        self.calls.append(("click", selector))

    def press_sequentially(self, selector: str, text: str, delay_ms: float = 0) -> None:
        self.calls.append(("type", len(text)))
        if not self.type_ok:
            raise RuntimeError("键盘事件被页面拒绝")
        self.doc += text

    def evaluate(self, script: str, arg=None):
        op = script.split("/* op=", 1)[1].split(" ", 1)[0]
        if self.clock is not None and op != "readback":
            self.clock.advance(self.evaluate_cost_s)
        if op == "readback":
            self.calls.append(("readback", None))
            if not self.readback_ok:
                return {"ok": False, "reason": "容器与编辑区都未命中"}
            return {"ok": True, "text": self.doc}

        sel, text = arg
        self.calls.append((op, len(text)))
        behavior = self.plan[op]
        if behavior == "raise":
            raise RuntimeError(f"{op} 元素不存在")
        if behavior == "deny":
            return {"ok": False, "reason": f"{op} 被编辑器拒绝"}
        if behavior == "truncate":
            self.doc = text[: len(text) // 2]
            return {"ok": True}
        self.doc = text
        return {"ok": True}


# ----------------------------------------------------------- UT-07-01

def test_ut_07_01_l1_paste_success_readback_within_tolerance(caplog):
    """L1 成功后回读长度与源文差异 < 2%，且不再碰 L2/L3。"""
    body = "正文" * 500
    adapter = CsdnAdapter()
    page = FakeEditorPage()

    with caplog.at_level(logging.INFO, logger="geopublish.csdn"):
        adapter.fill_content(page, _draft(body))

    assert page.doc == body
    ops = [name for name, _ in page.calls]
    assert ops == ["paste", "readback"], f"L1 成功不该碰 L2/L3：{ops}"
    assert any("L1-paste" in r.message for r in caplog.records)


# ----------------------------------------------------------- UT-07-02

def test_ut_07_02_l1_length_deviation_degrades_to_l2(caplog):
    """L1 注入后回读长度偏差 50% → 自动降级 L2，不直接失败。"""
    body = "甲" * 1000
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="truncate")

    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        adapter.fill_content(page, _draft(body))

    assert page.doc == body, "L2 要把全文补进去"
    ops = [name for name, _ in page.calls]
    assert ops == ["paste", "readback", "dispatch", "readback"]
    downgrades = [r for r in caplog.records if "降级" in r.message]
    assert len(downgrades) == 1 and "L1-paste" in downgrades[0].message


# ----------------------------------------------------------- UT-07-03

def test_ut_07_03_l1_l2_fail_degrade_to_l3_with_two_warnings(caplog):
    """L1 抛异常、L2 被拒绝 → 落到 L3 分段 type，日志记录两次降级。"""
    body = "分段兜底正文。" * 400  # 2800 字 → 6 段
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="raise", l2="deny")

    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        adapter.fill_content(page, _draft(body))

    assert page.doc == body
    ops = [name for name, _ in page.calls]
    type_calls = [n for name, n in page.calls if name == "type"]
    assert ops[0] == "paste" and "dispatch" in ops
    assert len(type_calls) == math.ceil(len(body) / SEGMENT_SIZE)
    assert all(n <= SEGMENT_SIZE for n in type_calls), "每段不得超过 SEGMENT_SIZE"
    downgrades = [r for r in caplog.records if "降级" in r.message and r.levelno == logging.WARNING]
    assert len(downgrades) == 2, f"两次降级都要留痕：{[r.message for r in downgrades]}"


def test_l3_clicks_editor_before_typing():
    """L3 先聚焦再打字；直接往没聚焦的页面灌键盘事件会丢字。"""
    body = "聚焦测试" * 200
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="raise", l2="raise")

    adapter.fill_content(page, _draft(body))

    ops = [name for name, _ in page.calls]
    first_type = ops.index("type")
    assert "click" in ops[:first_type], "第一次 type 之前必须有 click 聚焦"


# ----------------------------------------------------------- UT-07-04

def test_ut_07_04_all_three_levels_fail_raise_inject_failed():
    """三级全失败 → InjectFailed，带齐三级尝试记录；不做任何提交动作。"""
    body = "注定失败的正文" * 100
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="raise", l2="deny", type_ok=False)

    with pytest.raises(InjectFailed) as exc:
        adapter.fill_content(page, _draft(body))

    err = exc.value
    assert len(err.attempts) == 3
    assert [a.level for a in err.attempts] == ["L1-paste", "L2-dispatch", "L3-type"]
    assert all(not a.ok for a in err.attempts)
    assert err.expected_len == len(body)
    for level in ("L1-paste", "L2-dispatch", "L3-type"):
        assert level in str(err), "异常消息要能说清三级各自怎么死的"
    # 现场包 payload 直接可用：每条尝试都有原因
    assert all(a.reason for a in err.attempts)


def test_inject_failed_is_not_silently_swallowed_by_readback_failure():
    """注入动作成了但回读挂掉，也算该级失败 —— 读不出来就不能当成功。"""
    adapter = CsdnAdapter()
    page = FakeEditorPage(readback_ok=False)

    with pytest.raises(InjectFailed):
        adapter.fill_content(page, _draft("回读挂了" * 100))


# ----------------------------------------------------------- UT-07-05

def test_ut_07_05_long_body_l1_within_time_limit():
    """5000 字长文 L1 在 10s 时限内完成，不算超时。"""
    body = "长文" * 2500  # 5000 字
    clock = FakeClock(CLOCK_START)
    adapter = CsdnAdapter(clock=clock)
    page = FakeEditorPage(clock=clock, evaluate_cost_s=9.9)

    adapter.fill_content(page, _draft(body))

    assert page.doc == body


def test_l1_over_time_limit_counts_as_failed_and_degrades():
    """L1 超过 10s：哪怕内容进去了也判失败降级 —— 卡住的编辑器不可信。"""
    body = "超时正文" * 100
    clock = FakeClock(CLOCK_START)
    adapter = CsdnAdapter(clock=clock)
    page = FakeEditorPage(l1="ok", clock=clock, evaluate_cost_s=10.1)

    adapter.fill_content(page, _draft(body))

    ops = [name for name, _ in page.calls]
    assert "dispatch" in ops, "L1 超时后必须降级到 L2"


# ----------------------------------------------------------- UT-07-06

def test_ut_07_06_markdown_structure_survives_injection():
    """Markdown 表格与代码块注入后结构完整（行数、代码块数一致）。"""
    table = "| 列A | 列B |\n|---|---|\n" + "".join(f"| r{i} | v{i} |\n" for i in range(8))
    code = "```python\nprint('hi')\n```\n```bash\nls\n```\n"
    body = f"# 标题\n\n{table}\n{code}\n结尾段落。"
    adapter = CsdnAdapter()
    page = FakeEditorPage()

    adapter.fill_content(page, _draft(body))

    readback = read_editor_text(
        page, adapter.selectors("editor", "content"), adapter.selectors("editor", "content_editable")
    )
    assert readback.count("\n|") == body.count("\n|"), "表格行数不能丢"
    assert readback.count("```") == body.count("```"), "代码块围栏不能丢"


# ----------------------------------------------------------- 入口守卫

def test_fill_content_rejects_none_draft():
    """open_editor 允许 draft=None，fill_content 不行 —— 没正文注入什么。"""
    adapter = CsdnAdapter()
    with pytest.raises(TypeError):
        adapter.fill_content(FakeEditorPage(), None)


def test_fill_content_rejects_empty_body():
    """空正文不该走到注入：guard 漏放了，这里也得拦住（盲发空稿比失败更糟）。"""
    adapter = CsdnAdapter()
    with pytest.raises(ValueError):
        adapter.fill_content(FakeEditorPage(), _draft("   \n  "))


def test_inject_result_without_ok_counts_as_failure():
    """脚本返回 None/怪东西 → 该级失败。信「没返回」等于盲发。"""
    adapter = CsdnAdapter()
    page = FakeEditorPage()

    def weird_evaluate(script, arg=None):
        return None

    page.evaluate = weird_evaluate  # type: ignore[method-assign]
    with pytest.raises(InjectFailed):
        adapter.fill_content(page, _draft("返回怪东西" * 50))
