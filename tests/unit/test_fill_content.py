"""T-07 正文注入（三级降级）；2026-10-21 T-09b 真机校准后改版。

钉死的行为：
- L1 execCommand insertText → L2 合成 paste → L3 分段 type，顺序固定，不许跳级
- **先清场再注入**：编辑器会恢复上次未保存的内容（真机实测），不清场 = 双倍正文
- 每级注入后**回读**比对（textContent，剥离尾换行），长度偏差 > 2% 视为该级失败
- 每次降级前重新清场：上一级可能写了半截，残留 + 新内容 = 双倍正文
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
    """假 cledit 编辑器页：按 op 注释分发 evaluate，内部维护一份 doc。

    behavior 取值：
    - "ok"       注入成功，doc 变成全文
    - "truncate" 注入「成功」但只进了一半
    - "deny"     脚本返回 ok:false（execCommand 被拒就是这种）
    - "raise"    evaluate 直接抛异常（元素不存在/超时）
    - "deny_bare" 脚本返回 ok:false 且不给原因（页面脚本崩坏时的真实形态）
    """

    def __init__(
        self,
        *,
        l1: str = "ok",
        l2: str = "ok",
        type_ok: bool = True,
        clear_ok: bool = True,
        readback_ok: bool = True,
        restored: str = "",
        clock: FakeClock | None = None,
        evaluate_cost_s: float = 0.0,
    ) -> None:
        self.doc = restored
        self.plan = {"insert": l1, "paste": l2}
        self.type_ok = type_ok
        self.clear_ok = clear_ok
        self.readback_ok = readback_ok
        self.clock = clock
        self.evaluate_cost_s = evaluate_cost_s
        self.calls: list[tuple[str, object]] = []
        self.url = "https://editor.csdn.net/md"

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
        if self.clock is not None and op in self.plan:
            self.clock.advance(self.evaluate_cost_s)
        if op == "readback":
            self.calls.append(("readback", None))
            if not self.readback_ok:
                return {"ok": False, "reason": "编辑区未命中"}
            return {"ok": True, "text": self.doc}
        if op == "clear":
            self.calls.append(("clear", None))
            if not self.clear_ok:
                raise RuntimeError("清场脚本被页面拒绝")
            self.doc = ""
            return {"ok": True}
        if op == "closemodals":
            return {"ok": True, "closed": 0}

        sel, text = arg
        self.calls.append((op, len(text)))
        behavior = self.plan[op]
        if behavior == "raise":
            raise RuntimeError(f"{op} 元素不存在")
        if behavior == "deny":
            return {"ok": False, "reason": f"{op} 被编辑器拒绝"}
        if behavior == "deny_bare":
            return {"ok": False}
        if behavior == "truncate":
            self.doc = text[: len(text) // 2]
            return {"ok": True}
        self.doc = text
        return {"ok": True}


# ----------------------------------------------------------- UT-07-01

def test_ut_07_01_l1_insert_success_readback_within_tolerance(caplog):
    """L1 成功后回读长度与源文差异 < 2%，且不再碰 L2/L3。"""
    body = "正文" * 500
    adapter = CsdnAdapter()
    page = FakeEditorPage()

    with caplog.at_level(logging.INFO, logger="geopublish.csdn"):
        adapter.fill_content(page, _draft(body))

    assert page.doc == body
    ops = [name for name, _ in page.calls]
    assert ops == ["clear", "insert", "readback"], f"L1 成功不该碰 L2/L3：{ops}"
    assert any("L1-insert" in r.message for r in caplog.records)


def test_restored_content_is_cleared_before_injection():
    """编辑器恢复了上次未保存的内容（真机实测）→ 必须先清场再注入。"""
    body = "新稿正文" * 200
    adapter = CsdnAdapter()
    page = FakeEditorPage(restored="上次没发出去的残稿" * 100)

    adapter.fill_content(page, _draft(body))

    assert page.doc == body, "残稿不能被追加在新正文前面"
    assert page.calls[0][0] == "clear", "第一步必须是清场"


def test_trailing_newline_added_by_editor_is_not_a_failure():
    """编辑器自动补的尾换行（实测 +1 字）不该把成功判成失败。"""
    adapter = CsdnAdapter()
    body = "短正文" * 10  # 30 字，+1 尾换行超 2% 容忍
    page = FakeEditorPage()
    real_evaluate = page.evaluate

    def add_trailing_newline(script, arg=None):
        result = real_evaluate(script, arg)
        if "op=readback" in script and result.get("ok"):
            result = {"ok": True, "text": result["text"] + "\n"}
        return result

    page.evaluate = add_trailing_newline  # type: ignore[method-assign]
    adapter.fill_content(page, _draft(body))  # 不抛异常即为通过


# ----------------------------------------------------------- UT-07-02

def test_ut_07_02_l1_length_deviation_degrades_to_l2(caplog):
    """L1 注入后回读长度偏差 50% → 重新清场并降级 L2，不直接失败。"""
    body = "甲" * 1000
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="truncate")

    with caplog.at_level(logging.WARNING, logger="geopublish.csdn"):
        adapter.fill_content(page, _draft(body))

    assert page.doc == body, "L2 要把全文补进去"
    ops = [name for name, _ in page.calls]
    assert ops == ["clear", "insert", "readback", "clear", "paste", "readback"]
    downgrades = [r for r in caplog.records if "降级" in r.message]
    assert len(downgrades) == 1 and "L1-insert" in downgrades[0].message


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
    assert ops[0] == "clear" and "insert" in ops and "paste" in ops
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
    assert [a.level for a in err.attempts] == ["L1-insert", "L2-paste", "L3-type"]
    assert all(not a.ok for a in err.attempts)
    assert err.expected_len == len(body)
    for level in ("L1-insert", "L2-paste", "L3-type"):
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

def test_ut_07_05_long_body_l1_within_time_limit(caplog):
    """5000 字长文 L1 一次成功；耗时进日志（真机 <10s 属 L4 DoD 验证项）。"""
    body = "长文" * 2500  # 5000 字
    clock = FakeClock(CLOCK_START)
    adapter = CsdnAdapter(clock=clock)
    page = FakeEditorPage(clock=clock, evaluate_cost_s=9.9)

    with caplog.at_level(logging.INFO, logger="geopublish.csdn"):
        adapter.fill_content(page, _draft(body))

    assert page.doc == body
    success = [r for r in caplog.records if "fill_content 成功" in r.message]
    assert success and "耗时 9900ms" in success[0].message


def test_slow_but_verified_l1_still_counts_as_success():
    """L1 慢（>10s）但回读吻合 → 算成功，不降级。

    回读是唯一裁决：内容落地且对得上就是成了；为了「快」把已验证的注入
    判负、降级去跑更慢的路径，方向反了。
    """
    body = "慢但成功的正文" * 100
    clock = FakeClock(CLOCK_START)
    adapter = CsdnAdapter(clock=clock)
    page = FakeEditorPage(l1="ok", clock=clock, evaluate_cost_s=10.1)

    adapter.fill_content(page, _draft(body))

    ops = [name for name, _ in page.calls]
    assert ops == ["clear", "insert", "readback"], f"已验证的成功不该降级：{ops}"


def test_l3_clears_before_typing_so_no_duplicate_content():
    """L1 只进去半截、L2 被拒 → L3 先清场再打字，正文不能叠成两倍。"""
    body = "半截残留" * 100
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="truncate", l2="deny")

    adapter.fill_content(page, _draft(body))

    assert page.doc == body, f"残留 + 追加 = 双倍正文：实际 {len(page.doc)} 字"
    ops = [name for name, _ in page.calls]
    assert "clear" in ops[: ops.index("type")], "第一次 type 之前必须先清场"


def test_l3_clear_failure_does_not_crash_but_readback_catches_residue():
    """清场失败不许炸在半路；叠出来的双倍正文由回读拦下 → InjectFailed。"""
    body = "清不掉的残留" * 100
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="truncate", l2="deny", clear_ok=False)

    with pytest.raises(InjectFailed):
        adapter.fill_content(page, _draft(body))


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


def test_crlf_body_is_normalized_before_length_compare():
    """Windows 稿件的 \\r\\n：编辑器回读统一成 \\n，不归一会被误判成「丢字」。

    一篇 100 个 \\r\\n 的 3000 字稿，回读少 100 字 = 偏差 3.3% → 假 InjectFailed。
    """
    body = "第一行\r\n第二行\r\n" * 150
    adapter = CsdnAdapter()
    page = FakeEditorPage()

    adapter.fill_content(page, _draft(body))

    assert page.doc == body.replace("\r\n", "\n")
    ops = [name for name, _ in page.calls]
    assert ops == ["clear", "insert", "readback"], f"换行差异不该触发降级：{ops}"


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


def test_inject_denied_without_reason_still_degrades():
    """ok:false 但没给原因（页面脚本崩坏的真实形态）→ 照常降级，原因有兜底文案。"""
    adapter = CsdnAdapter()
    page = FakeEditorPage(l1="deny_bare")

    adapter.fill_content(page, _draft("无理由拒绝" * 50))

    ops = [name for name, _ in page.calls]
    assert "paste" in ops


def test_readback_returning_nothing_counts_as_failure():
    """回读脚本返回 None（页面上下文崩了）→ 该级失败，逐级降级后 InjectFailed。"""
    adapter = CsdnAdapter()
    page = FakeEditorPage()
    real_evaluate = page.evaluate

    def no_readback(script, arg=None):
        if "op=readback" in script:
            return None
        return real_evaluate(script, arg)

    page.evaluate = no_readback  # type: ignore[method-assign]
    with pytest.raises(InjectFailed):
        adapter.fill_content(page, _draft("回读为空" * 50))


def test_readback_without_text_field_counts_as_failure():
    """回读 ok:true 但没有 text 字段 → 不能当成功，三级全灭后 InjectFailed。"""
    adapter = CsdnAdapter()
    page = FakeEditorPage()
    real_evaluate = page.evaluate

    def no_text(script, arg=None):
        if "op=readback" in script:
            return {"ok": True}
        return real_evaluate(script, arg)

    page.evaluate = no_text  # type: ignore[method-assign]
    with pytest.raises(InjectFailed):
        adapter.fill_content(page, _draft("回读缺字段" * 50))


def test_l3_typing_uses_locator_when_page_lacks_press_sequentially():
    """真机形态：Playwright Page 没有 press_sequentially（在 Locator 上），
    必须自动走 locator——这条是 T-09b 正式验收抓出来的真实差异。"""

    class _Locator:
        def __init__(self, page):
            self._page = page

        def press_sequentially(self, text, delay=0):
            self._page.doc += text

    class _LocatorPage(FakeEditorPage):
        press_sequentially = None  # getattr 返回 None → 走 locator 路径

        def locator(self, selector):
            return _Locator(self)

    body = "真机键盘路径" * 50
    adapter = CsdnAdapter()
    page = _LocatorPage(l1="raise", l2="raise")

    adapter.fill_content(page, _draft(body))

    assert page.doc == body


def test_l3_typing_uses_locator_when_page_lacks_press_sequentially():
    """真机形态：Playwright Page 没有 press_sequentially（在 Locator 上），
    必须自动走 locator——这条是 T-09b 正式验收抓出来的真实差异。"""

    class _Locator:
        def __init__(self, page):
            self._page = page

        def press_sequentially(self, text, delay=0):
            self._page.doc += text

    class _LocatorPage(FakeEditorPage):
        press_sequentially = None  # getattr 返回 None → 走 locator 路径

        def locator(self, selector):
            return _Locator(self)

    body = "真机键盘路径" * 50
    adapter = CsdnAdapter()
    page = _LocatorPage(l1="raise", l2="raise")

    adapter.fill_content(page, _draft(body))

    assert page.doc == body
