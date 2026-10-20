"""T-03 · 合规自检引擎（UT-03-01 ~ UT-03-08）。

本文件先于实现存在：先跑红，再写 `geopublish/core/guard.py`。

两条测试纪律：
1. **PRD R4 的词表在测试里抄一份独立副本**，不引用实现里的常量 ——
   实现把「最好」删掉了，测试得跟着红，而不是跟着一起忘。
2. **只测「拒」不够，还要测「不该拒的不拒」**。自检是硬闸门，
   误伤等于把好稿也卡死；中文绝对化用语的误伤（第一次 / 第一步）尤其要钉住。
"""

import inspect
import json

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.config import PlatformConfig
from geopublish.core.guard import (
    CHECK_ORDER,
    R4_CHECKS,
    BannedWords,
    GuardConfigError,
    GuardRejected,
    GuardResult,
    load_banned_words,
    require_pass,
    self_check,
)
from geopublish.core.models import Draft, Meta
from geopublish.core.state import Status

pytestmark = pytest.mark.unit

CSDN = PlatformConfig(name="csdn", title_max=100, tags_max=5)

# PRD R4 第 1、2 项点名的词，一个都不能少
PRD_ABSOLUTE = ("最好", "第一", "唯一", "领先", "顶级", "保证", "100%", "绝对", "永久")
PRD_MARKETING = ("欢迎咨询", "联系我们", "立即购买", "扫码添加", "免费试用")

CLEAN_BODY = """# DeepParser 怎么把复杂表格变成结构化数据

结论前置：DeepParser 先做版式分析，再按单元格网格还原结构，支持 12 种表格格式[1]。

## 为什么跨页表格容易解析错

扫描件里的跨行合并单元格没有显式边界，模型只能靠对齐关系推断[1]。

## 数字都从哪来

本文数字均引自公开测试报告[1]。
"""


def make_meta(**over) -> Meta:
    base = dict(
        slug="deepparser-table-extraction",
        title="DeepParser 如何解析复杂表格",
        summary="从版式分析到结构化输出",
        tags=("文档解析", "PDF", "OCR"),
        category="人工智能",
        ai_involvement="assisted",
    )
    base.update(over)
    return Meta(**base)


def make_draft(body: str = CLEAN_BODY, **meta_over) -> Draft:
    return Draft(meta=make_meta(**meta_over), body=body)


def check(body: str = CLEAN_BODY, platform: PlatformConfig = CSDN, **meta_over) -> GuardResult:
    return self_check(make_draft(body, **meta_over), platform)


# ---------------------------------------------------------------- UT-03-01
def test_ut_03_01_absolute_word_hits():
    result = check("DeepParser 是最好的文档解析工具。")

    assert result.passed is False
    assert [h.term for h in result.hits] == ["最好"], "命中项要能指名道姓说是哪个词"
    assert result.hits[0].check == "absolute"


# ---------------------------------------------------------------- UT-03-02
def test_ut_03_02_marketing_phrase_hits():
    result = check("我们的方案覆盖文档解析与审核，欢迎咨询。")

    assert result.passed is False
    assert [h.term for h in result.hits] == ["欢迎咨询"]
    assert result.hits[0].check == "marketing"


# ---------------------------------------------------------------- UT-03-03
def test_ut_03_03_number_without_source_is_marked():
    result = check("DeepParser 支持 12 种格式。")

    assert result.passed is False
    hit = next(h for h in result.hits if h.rule == "unsourced_number")
    assert hit.term == "12", "要把没出处的那个数字本身报出来，而不是只报整句"


# ---------------------------------------------------------------- UT-03-04
def test_ut_03_04_all_hits_reported_not_just_first():
    result = check("DeepParser 是最好、最领先、顶级的选择。")

    terms = {h.term for h in result.hits}
    assert {"最好", "顶级"} <= terms
    assert len(result.hits) >= 3, "同一句命中多个词必须全部列出"


def test_ut_03_04b_each_check_failure_is_reported():
    """一个检查失败不影响其他检查继续跑：全量报告，一次改完。"""
    result = check("DeepParser 是最好的工具，欢迎咨询，支持 12 种格式。")

    assert result.failed_checks == ("absolute", "marketing", "number_source")
    assert {h.check for h in result.hits} == {"absolute", "marketing", "number_source"}


# ---------------------------------------------------------------- UT-03-05
def test_ut_03_05_clean_draft_passes():
    result = check()

    assert result.passed is True
    assert result.hits == ()
    assert result.failed_checks == ()
    assert result.status is Status.PENDING


# ---------------------------------------------------------------- UT-03-06
@pytest.mark.parametrize("body", ["", "   ", "\n\n\t  \n"])
def test_ut_03_06_empty_draft_is_rejected(body):
    result = check(body)

    assert result.passed is False
    assert [h.rule for h in result.hits] == ["empty_body"], "空稿只报空稿，不叠一堆无意义命中"


# ---------------------------------------------------------------- UT-03-07
def test_ut_03_07_title_over_limit_rejects_draft():
    long_title = "DeepParser 如何把复杂表格解析成结构化数据的一份非常详细的说明"
    assert len(long_title) > 20

    result = check(platform=PlatformConfig(name="csdn", title_max=20, tags_max=5), title=long_title)

    assert result.passed is False
    assert result.hits[0].rule == "title_max"
    assert result.status is Status.REJECTED, "长度越界也必须联动 rejected，不只是前三项"
    assert "rejected" in result.to_json()["status"]


# ---------------------------------------------------------------- UT-03-08
def test_ut_03_08_missing_ai_involvement_rejects_draft():
    result = check(ai_involvement="")

    assert result.passed is False
    assert result.hits[0].rule == "ai_involvement_missing"
    assert result.status is Status.REJECTED, "AI 参与度留痕是合规硬要求，缺了就不许发"


# ------------------------------------------------- R4 五项必须全部与 rejected 联动
def test_every_r4_check_can_reject():
    """R4 说「任一不满足即 rejected」，那就逐条证明确实都能把稿子打回。"""
    cases = {
        "absolute": dict(body="这是最好的方案。"),
        "marketing": dict(body="详情欢迎咨询。"),
        "number_source": dict(body="支持 12 种格式。"),
        "platform_limits": dict(
            body=CLEAN_BODY, tags=("a", "b", "c", "d", "e", "f")
        ),
        "ai_involvement": dict(body=CLEAN_BODY, ai_involvement="generated-by-robot"),
    }
    assert set(cases) == set(R4_CHECKS)

    for name, kwargs in cases.items():
        result = check(**kwargs)
        assert result.passed is False, f"{name} 不满足却没拒"
        assert result.status is Status.REJECTED, f"{name} 没联动 rejected"
        assert result.failed_checks == (name,)


# ------------------------------------------------- 数字出处：只认同句引用
@pytest.mark.parametrize(
    "citation",
    [
        "[1]",
        "[^1]",
        "[1][2]",
        "（来源：2025 年公开测试报告）",
        "[来源：产品白皮书 第 3 页]",
        "【出处：官网】",
        "(Source: internal benchmark)",
    ],
)
def test_number_with_inline_citation_passes(citation):
    result = check(f"DeepParser 支持 12 种格式{citation}。")

    assert result.passed is True, f"{citation} 应被认作出处"


def test_citation_must_be_in_the_same_sentence():
    """引用写在下一句不算数 —— 否则一句「数据来自报告」就能给全文免责。"""
    result = check("DeepParser 支持 12 种格式。以上数据引自公开测试报告[1]。")

    assert result.passed is False
    assert result.hits[0].term == "12"


@pytest.mark.parametrize(
    "body",
    [
        "示例代码：\n\n```python\npage = 3\nrows = 128\n```\n\n这段逻辑在文档里有说明[1]。\n",
        "调用 `extract(page=3)` 即可，参数含义见文档[1]。",
        "## 1. 为什么表格难解\n\n版式分析先定位网格线[1]。\n",
        "详见 https://example.com/docs/v2/release-2025 的说明[1]。",
    ],
)
def test_code_urls_and_heading_numbers_are_not_claims(body):
    """代码块、行内代码、小标题序号、URL 里的数字不是「数字类主张」。"""
    assert check(body).passed is True


# ------------------------------------------------- 标题也在检查范围内
def test_banned_word_in_title_is_rejected():
    """标题是最显眼的合规风险位（「最好的…」），不能只查正文。"""
    result = check(title="最好的文档解析工具：DeepParser 实测")

    assert result.passed is False
    assert result.hits[0].term == "最好"


def test_title_numbers_are_not_policed_but_body_numbers_are():
    """标题里的数字不要求带出处（100 字标题塞引用不现实），正文照查。"""
    result = check(
        title="5 个信号说明表格解析在变难",
        body="DeepParser 支持 12 种格式。",
    )

    assert result.passed is False
    assert [h.term for h in result.hits] == ["12"]


# ------------------------------------------------- 绝对化用语的误伤
def test_ordinal_usage_of_first_is_not_a_claim():
    """「第一步 / 第一次」是叙述顺序，不是「行业第一」。"""
    assert check("第一步先做版式分析，第一次跑通只花了半小时[1]。").passed is True


def test_superlative_usage_of_first_is_a_claim():
    result = check("DeepParser 是行业第一的表格解析引擎。")

    assert result.passed is False
    assert "第一" in {h.term for h in result.hits}


# ------------------------------------------------- 平台侧：标签
def test_tags_over_limit_rejected():
    result = check(tags=("文档解析", "PDF", "OCR", "表格", "结构化", "RAG"))

    assert result.passed is False
    assert result.hits[0].rule == "tag_count"


def test_blank_tag_rejected():
    result = check(tags=("文档解析", "  "))

    assert result.passed is False
    assert result.hits[0].rule == "tag_empty"


def test_tag_count_at_limit_passes():
    assert check(tags=("一", "二", "三", "四", "五")).passed is True


# ------------------------------------------------- AI 参与度取值
def test_ai_involvement_is_case_insensitive():
    assert check(ai_involvement=" Assisted ").passed is True


@pytest.mark.parametrize("value", ["robot", "AI", "生成"])
def test_ai_involvement_must_be_one_of_three_values(value):
    result = check(ai_involvement=value)

    assert result.passed is False
    assert result.hits[0].rule == "ai_involvement_invalid"


# ------------------------------------------------- 结果结构与可追溯
def test_checks_come_in_fixed_order():
    result = check()

    assert tuple(c.name for c in result.checks) == CHECK_ORDER
    assert set(R4_CHECKS) <= set(CHECK_ORDER), "R4 五项一个都不能少"
    assert all(c.passed for c in result.checks)


def test_hits_are_positioned_in_reading_order():
    result = check("这是最好的方案。详情欢迎咨询。")

    offsets = [h.offset for h in result.hits]
    assert offsets == sorted(offsets), "命中要按正文位置排，读报告时才对得上"
    assert all(h.excerpt for h in result.hits), "每条命中都要带上下文，否则没法判断要不要改"
    assert "欢迎咨询" in result.hits[-1].excerpt


def test_excerpt_is_trimmed_for_long_sentences():
    body = "铺垫" * 100 + "这是最好的方案" + "收尾" * 100 + "。"
    result = check(body)

    excerpt = result.hits[0].excerpt
    assert "最好" in excerpt
    assert len(excerpt) <= 80, "摘录要截断，日报里塞一整句没人看"


def test_meta_hits_use_offset_minus_one():
    """元信息命中没有正文位置，用 -1 明确区分，别假装在第 0 个字。"""
    result = check(tags=("一", "二", "三", "四", "五", "六"))

    assert result.hits[0].offset == -1


def test_to_json_is_serialisable_and_complete():
    clock = FakeClock("2026-10-21T10:00:00+08:00")
    result = self_check(
        make_draft("这是最好的方案。支持 12 种格式。"), CSDN, clock=clock
    )

    payload = json.loads(json.dumps(result.to_json(), ensure_ascii=False))

    assert payload["passed"] is False
    assert payload["status"] == "rejected"
    assert payload["slug"] == "deepparser-table-extraction"
    assert payload["platform"] == "csdn"
    assert payload["checked_at"] == "2026-10-21T10:00:00+08:00"
    assert payload["failed_checks"] == ["absolute", "number_source"]
    assert [c["name"] for c in payload["checks"]] == list(CHECK_ORDER)
    assert sum(c["hit_count"] for c in payload["checks"]) == len(payload["hits"])
    first = payload["hits"][0]
    assert set(first) == {"check", "rule", "term", "excerpt", "offset"}


# ------------------------------------------------- 不许有「跳过自检」的口子
def test_self_check_has_no_bypass_switch():
    """PRD §9.5：R4 不得增加「跳过自检」参数。这里从签名上钉死。"""
    params = inspect.signature(self_check).parameters
    banned = ("skip", "force", "allow", "ignore", "bypass", "disable", "override", "enable")

    offenders = [n for n in params if any(b in n.lower() for b in banned)]
    assert not offenders, f"自检入口出现了开关参数：{offenders}"
    assert not any(
        p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
    ), "不许用 **kwargs 偷传开关"


def test_require_pass_raises_with_actionable_message():
    result = check("这是最好的方案。支持 12 种格式。")

    with pytest.raises(GuardRejected) as exc:
        require_pass(result)

    msg = str(exc.value)
    assert "absolute" in msg and "number_source" in msg
    assert "最好" in msg, "报错要带具体命中的词，人才知道改哪"


def test_require_pass_is_silent_when_clean():
    assert require_pass(check()) is None


# ------------------------------------------------- 词表：外置 + 可验
def test_default_wordlist_is_loadable_and_covers_prd_terms():
    words = load_banned_words()

    absolute = {t.label for t in words.absolute}
    marketing = {t.label for t in words.marketing}

    assert [w for w in PRD_ABSOLUTE if w not in absolute] == []
    assert [w for w in PRD_MARKETING if w not in marketing] == []


def test_default_wordlist_has_no_duplicate_labels():
    words = load_banned_words()

    for category in (words.absolute, words.marketing):
        labels = [t.label for t in category]
        assert len(labels) == len(set(labels)), f"词表重复：{labels}"


def test_wordlist_can_be_injected():
    """词表可替换（合规口径变化时换 JSON，不改代码）。"""
    custom = BannedWords.from_dict({"absolute": ["独家"], "marketing": []})

    hit = self_check(make_draft("这是独家的方案。"), CSDN, words=custom)
    assert hit.passed is False
    assert hit.hits[0].term == "独家"

    baseline = self_check(make_draft("这是最好的方案。"), CSDN, words=custom)
    assert baseline.passed is True, "注入自定义词表后，默认词表就不该再生效"


@pytest.mark.parametrize(
    "payload",
    [
        {"absolute": ["最好"]},  # 缺 marketing 分类
        {"absolute": [{}], "marketing": []},  # 条目既非字符串也非 pattern
        {"absolute": [{"pattern": "(未闭合"}], "marketing": []},  # 正则写错
        {"absolute": "最好", "marketing": []},  # 分类不是数组
        {"absolute": [], "marketing": [], "banned": []},  # 分类名拼错
        {"absolute": ["  "], "marketing": []},  # 空词条
        {"absolute": [123], "marketing": []},  # 条目类型不对
        ["absolute", "marketing"],  # 根本不是对象
    ],
)
def test_malformed_wordlist_fails_loudly(payload):
    with pytest.raises(GuardConfigError):
        BannedWords.from_dict(payload)


def test_missing_wordlist_file_fails_loudly(tmp_path):
    with pytest.raises(GuardConfigError) as exc:
        load_banned_words(tmp_path / "nope.json")

    assert "nope.json" in str(exc.value)


def test_broken_wordlist_json_fails_loudly(tmp_path):
    path = tmp_path / "banned.json"
    path.write_text("{ not json", encoding="utf-8")

    with pytest.raises(GuardConfigError):
        load_banned_words(path)


def test_comment_keys_in_wordlist_are_ignored():
    """JSON 不能写注释，沿用仓库约定：`_` 开头当注释。"""
    words = BannedWords.from_dict(
        {"_comment": "口径说明", "absolute": ["最好"], "marketing": []}
    )

    assert {t.label for t in words.absolute} == {"最好"}


def test_guard_result_is_frozen():
    result = check()

    with pytest.raises(Exception):
        result.checks = ()  # type: ignore[misc]
