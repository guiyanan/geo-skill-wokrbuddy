"""T-14 成稿五步（geopublish/write/pipeline.py）。

钉死的行为（对应方案 UT-14-01~09 与 PRD R1/R2/R3）：
- UT-14-01 锚定问句恰好 1 个且在问句配置中存在
- UT-14-02 B/P 类品牌词拒绝锚定（品牌词 SOV 已 83%，无增量）
- UT-14-03 H2 ≥ 3 且均为问句
- UT-14-04 每节 200–400 字（去空白计）
- UT-14-05 FAQ ≥ 3 条
- UT-14-06 正文每个数字都能在 facts 的 claim 里找到出处
- UT-14-07 首段 ≤ 80 字且直接回答锚定问句
- UT-14-08 全文 3000–5000 字（补 G3）
- UT-14-09 品牌/产品实体 ≥ 3 次且全文只用全称（补 G3）
- DoD：产出稿件通过 T-03 自检，且能被 publish.load_draft 原样回读
- G2：REVIEW.md 未回填的产品不进成稿队列
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from geopublish.core.config import PlatformConfig
from geopublish.core.guard import require_pass
from geopublish.publish import load_draft
from geopublish.write.ingest import ingest
from geopublish.write.pipeline import (
    LEAD_MAX,
    SECTION_MAX,
    SECTION_MIN,
    DraftRejected,
    PipelineError,
    anchor_query,
    count_entity_mentions,
    count_faq,
    extract_lead,
    find_alias_leaks,
    find_unsourced_numbers,
    is_question,
    iter_sections,
    load_query_config,
    write_article,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "products" / "deepparser-pitch.md"

QUERIES = {
    "brand": {
        "name": "上海聚托",
        "aliases": ["上海聚托", "聚托科技", "聚托", "JOTO TECH", "JOTO"],
        "products": ["DeepParser", "team KB"],
    },
    "queries": [
        {"id": "B01", "text": "上海聚托 是什么公司", "intent": "认知", "priority": "P0"},
        {"id": "P01", "text": "DeepParser 文档解析", "intent": "认知", "priority": "P0"},
        {"id": "C01", "text": "国内有哪些 AI 文档解析工具", "intent": "评估", "priority": "P0"},
        {"id": "D01", "text": "中小企业怎么选 AI 解决方案供应商", "intent": "决策", "priority": "P0"},
        {"id": "S01", "text": "PDF 里的表格怎么自动提取成结构化数据", "intent": "场景", "priority": "P0"},
        {"id": "X01", "text": "Dify 和 Coze 哪个好", "intent": "竞品", "priority": "P0"},
    ],
}


def _platform() -> PlatformConfig:
    return PlatformConfig(name="csdn", word_min=3000, word_max=5000, title_max=100, tags_max=5)


def _write_queries(tmp_path: Path) -> Path:
    path = tmp_path / "joto.json"
    path.write_text(json.dumps(QUERIES, ensure_ascii=False), encoding="utf-8")
    return path


def _make_product(data_dir: Path, clock, *, reviewed: bool = True) -> None:
    result = ingest(FIXTURE, "deepparser", data_dir, clock=clock)
    if reviewed:
        review = result.product_dir / "REVIEW.md"
        text = review.read_text(encoding="utf-8")
        text = re.sub(r"^reviewed_by:[ \t]*$", "reviewed_by: 拾一米", text, count=1, flags=re.M)
        text = re.sub(r"^reviewed_at:[ \t]*$", "reviewed_at: 2026-10-26T10:00:00+08:00", text, count=1, flags=re.M)
        review.write_text(text, encoding="utf-8")


@pytest.fixture(scope="module")
def produced(tmp_path_factory, ):
    """跑一次完整流水线，供 UT-14-03~09 与 DoD 共享（生成是确定性的，跑一遍就够）。"""
    from geopublish.core.clock import FakeClock

    root = tmp_path_factory.mktemp("t14")
    data_dir = root / "data"
    for sub in ("products", "drafts"):
        (data_dir / sub).mkdir(parents=True)
    clock = FakeClock("2026-10-26T10:00:00+08:00")
    _make_product(data_dir, clock)
    qcfg = load_query_config(_write_queries(root))
    result = write_article(
        "deepparser", "S01",
        data_root=data_dir, query_config=qcfg, platform=_platform(), clock=clock,
    )
    body = result.body_path.read_text(encoding="utf-8")
    return result, body, qcfg


# --------------------------------------------------------------- UT-14-01/02 锚定

def test_ut_14_01_anchor_exactly_one_and_exists(tmp_path, clock):
    qcfg = load_query_config(_write_queries(tmp_path))
    q = anchor_query(qcfg, "s01")  # 大小写不敏感
    assert q.id == "S01"
    with pytest.raises(PipelineError, match="不在问句配置"):
        anchor_query(qcfg, "S99")
    with pytest.raises(PipelineError, match="不能为空"):
        anchor_query(qcfg, "  ")


def test_ut_14_01_brief_and_meta_carry_single_anchor(produced):
    result, _, _ = produced
    brief = json.loads(result.brief_path.read_text(encoding="utf-8"))
    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert brief["anchor_query_id"] == "S01"
    assert meta["anchor_query_id"] == "S01"


@pytest.mark.parametrize("qid", ["B01", "P01", "X01"])
def test_ut_14_02_brand_and_competitor_queries_rejected(tmp_path, qid):
    qcfg = load_query_config(_write_queries(tmp_path))
    with pytest.raises(PipelineError, match="C/D/S"):
        anchor_query(qcfg, qid)


def test_ut_14_02_reject_message_explains_why(tmp_path):
    qcfg = load_query_config(_write_queries(tmp_path))
    with pytest.raises(PipelineError, match="83%"):
        anchor_query(qcfg, "B01")


# --------------------------------------------------------------- UT-14-03~05 结构

def test_ut_14_03_h2_at_least_3_and_all_questions(produced):
    _, body, _ = produced
    sections = iter_sections(body)
    assert len(sections) >= 3
    for h2, _ in sections:
        assert is_question(h2), f"非问句 H2：{h2}"


def test_ut_14_04_each_section_200_to_400_chars(produced):
    _, body, _ = produced
    for h2, text in iter_sections(body):
        n = len(re.sub(r"\s", "", text))
        assert SECTION_MIN <= n <= SECTION_MAX, f"章节「{h2}」{n} 字越界"


def test_ut_14_05_at_least_3_faq(produced):
    _, body, _ = produced
    assert count_faq(body) >= 3


# --------------------------------------------------------------- UT-14-06~09 内容

def test_ut_14_06_every_number_has_source(produced):
    result, body, _ = produced
    facts = json.loads(
        (result.draft_dir.parent.parent / "products" / "deepparser" / "facts.json")
        .read_text(encoding="utf-8")
    )["facts"]
    from geopublish.write.ingest import Fact

    unsourced = find_unsourced_numbers(body, [Fact.from_json(f) for f in facts])
    assert unsourced == [], f"无出处数字：{unsourced}"


def test_ut_14_07_lead_answers_query_within_80_chars(produced):
    _, body, _ = produced
    lead = extract_lead(body)
    assert len(re.sub(r"\s", "", lead)) <= LEAD_MAX
    assert "DeepParser" in lead  # 结论前置：直接点名答案主体
    assert "PDF 里的表格怎么自动提取成结构化数据" in lead  # 直接回应锚定问句


def test_ut_14_08_body_length_within_platform_range(produced):
    result, body, _ = produced
    assert 3000 <= len(body) <= 5000
    assert result.word_count == len(body)


def test_ut_14_09_entity_density_and_single_canonical_name(produced):
    _, body, qcfg = produced
    assert count_entity_mentions(body, "上海聚托", "DeepParser") >= 3
    assert find_alias_leaks(body, qcfg.brand) == []


def test_ut_14_09_claim_aliases_are_unified(produced):
    """fixture 第一条事实写的是「上海聚托（JOTO）」，成稿后 JOTO 不得残留。"""
    _, body, _ = produced
    assert "JOTO" not in body
    assert "（上海聚托）" not in body  # 括号别名删除，不是替换成全称再套娃


# --------------------------------------------------------------- DoD / G2 / 边界

def test_dod_article_passes_guard_and_roundtrips(produced):
    result, _, _ = produced
    selfcheck = json.loads(result.selfcheck_path.read_text(encoding="utf-8"))
    assert selfcheck["passed"] is True
    assert selfcheck["status"] == "pending"
    # 能被发布层原样回读（T-07 的入口契约），且 content_sha 一致
    loaded = load_draft(result.draft_dir, platform="csdn")
    require_pass(result.guard_result)
    assert loaded.draft.meta.anchor_query_id == "S01"
    assert loaded.draft.body == result.body_path.read_text(encoding="utf-8")


def test_unreviewed_product_rejected(tmp_path, data_dir, clock):
    _make_product(data_dir, clock, reviewed=False)
    qcfg = load_query_config(_write_queries(tmp_path))
    with pytest.raises(PipelineError, match="未回填"):
        write_article(
            "deepparser", "S01",
            data_root=data_dir, query_config=qcfg, platform=_platform(), clock=clock,
        )
    assert not (data_dir / "drafts").exists() or not list((data_dir / "drafts").iterdir())


def test_missing_product_rejected(tmp_path, data_dir, clock):
    qcfg = load_query_config(_write_queries(tmp_path))
    with pytest.raises(PipelineError, match="产品库不存在"):
        write_article(
            "ghost", "S01",
            data_root=data_dir, query_config=qcfg, platform=_platform(), clock=clock,
        )


def test_existing_draft_dir_not_overwritten(tmp_path, data_dir, clock):
    _make_product(data_dir, clock)
    qcfg = load_query_config(_write_queries(tmp_path))
    write_article(
        "deepparser", "S01",
        data_root=data_dir, query_config=qcfg, platform=_platform(), clock=clock,
    )
    with pytest.raises(PipelineError, match="已存在"):
        write_article(
            "deepparser", "S01",
            data_root=data_dir, query_config=qcfg, platform=_platform(), clock=clock,
        )


def test_draft_rejected_carries_guard_result(tmp_path, data_dir, clock):
    """构造一篇必挂的稿：把违禁词混进事实，自检必须拦下且留证。"""
    src = tmp_path / "poison.md"
    lines = ["<!-- page 1 -->"]
    for i in range(15):
        lines.append(f"- 第{i}条事实：本产品是最好的企业级文档解析工具，支持私有化部署")
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = ingest(src, "poison", data_dir, clock=clock)
    review = result.product_dir / "REVIEW.md"
    text = review.read_text(encoding="utf-8")
    text = re.sub(r"^reviewed_by:[ \t]*$", "reviewed_by: 拾一米", text, count=1, flags=re.M)
    text = re.sub(r"^reviewed_at:[ \t]*$", "reviewed_at: 2026-10-26T10:00:00+08:00", text, count=1, flags=re.M)
    review.write_text(text, encoding="utf-8")

    qcfg = load_query_config(_write_queries(tmp_path))
    with pytest.raises(DraftRejected) as exc_info:
        write_article(
            "poison", "S01",
            data_root=data_dir, query_config=qcfg, platform=_platform(), clock=clock,
        )
    assert "absolute" in exc_info.value.result.failed_checks
    # 稿件目录照留，selfcheck.json 里有命中明细（拿去改稿，不是悄悄消失）
    selfcheck = json.loads(
        (exc_info.value.draft_dir / "selfcheck.json").read_text(encoding="utf-8")
    )
    assert selfcheck["passed"] is False
    assert selfcheck["status"] == "rejected"


# --------------------------------------------------------------- CLI

def _write_config(tmp_path: Path, data_dir: Path) -> Path:
    cfg = {
        "gate": "draft",
        "platforms": [{"name": "csdn", "word_min": 3000, "word_max": 5000}],
        "report": {"email": "t@example.com"},
        "data_dir": str(data_dir),
    }
    path = tmp_path / "geopublish.json"
    path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    return path


def test_cli_write_ok(tmp_path, data_dir, clock, capsys):
    from geopublish.cli import EXIT_OK, main

    _make_product(data_dir, clock)
    queries = _write_queries(tmp_path)
    config = _write_config(tmp_path, data_dir)
    rc = main([
        "write", "deepparser", "--query", "S01",
        "--queries", str(queries), "--data-dir", str(data_dir), "--config", str(config),
    ])
    assert rc == EXIT_OK
    assert "成稿完成：deepparser-s01" in capsys.readouterr().out


def test_cli_write_brand_query_is_usage_error(tmp_path, data_dir, clock, capsys):
    from geopublish.cli import EXIT_USAGE, main

    _make_product(data_dir, clock)
    queries = _write_queries(tmp_path)
    config = _write_config(tmp_path, data_dir)
    rc = main([
        "write", "deepparser", "--query", "B01",
        "--queries", str(queries), "--data-dir", str(data_dir), "--config", str(config),
    ])
    assert rc == EXIT_USAGE
    assert "成稿失败" in capsys.readouterr().err



# --------------------------------------------------------------- 覆盖率补钉（T-15 发现）
# T-14 交付时只跑了「415 绿」，没跑 CI 的 100% 覆盖门禁——下面这些是漏网的
# 错误分支。门禁的意义就是不许有「没被断言过的行为」，补齐它们。

from geopublish.write.ingest import Fact  # noqa: E402
from geopublish.write.pipeline import (  # noqa: E402
    BrandKit,
    Product,
    Query,
    _Frame,
    _assert_structure,
    _render_faq,
    _render_lead,
    _render_section,
    load_product,
)

_BRAND = BrandKit(name="上海聚托", aliases=("JOTO", "聚托"), products=("DeepParser",))
_QUERY = Query(id="S01", text="PDF 里的表格怎么自动提取成结构化数据", intent="场景", priority="P0")


def _fact(claim: str, tier: str = "B") -> Fact:
    return Fact(claim=claim, source="测试资料 第 1 页", tier=tier, date="2026-10-26")


def _ctx() -> dict:
    return {"query": _QUERY.text, "product": "DeepParser", "brand": "上海聚托", "brand_obj": _BRAND}


def test_load_query_config_error_paths(tmp_path):
    with pytest.raises(PipelineError, match="问句配置不存在"):
        load_query_config(tmp_path / "no.json")

    bad = tmp_path / "bad.json"
    bad.write_text("{坏掉了", encoding="utf-8")
    with pytest.raises(PipelineError, match="不是合法 JSON"):
        load_query_config(bad)

    empty = tmp_path / "empty.json"
    empty.write_text('{"queries": []}', encoding="utf-8")
    with pytest.raises(PipelineError, match="非空数组"):
        load_query_config(empty)

    missing_field = tmp_path / "mf.json"
    missing_field.write_text(json.dumps({"queries": [{"id": "S01"}], "brand": {"name": "x"}}), encoding="utf-8")
    with pytest.raises(PipelineError, match="缺字段"):
        load_query_config(missing_field)

    blank = tmp_path / "blank.json"
    blank.write_text(
        json.dumps({"queries": [{"id": " ", "text": "x"}], "brand": {"name": "x"}}), encoding="utf-8"
    )
    with pytest.raises(PipelineError, match="不能为空"):
        load_query_config(blank)

    dup = tmp_path / "dup.json"
    dup.write_text(
        json.dumps({
            "queries": [
                {"id": "S01", "text": "甲"}, {"id": "s01", "text": "乙"},
            ],
            "brand": {"name": "x"},
        }),
        encoding="utf-8",
    )
    with pytest.raises(PipelineError, match="id 重复"):
        load_query_config(dup)

    nobrand = tmp_path / "nb.json"
    nobrand.write_text(json.dumps({"queries": [{"id": "S01", "text": "x"}]}), encoding="utf-8")
    with pytest.raises(PipelineError, match="brand.name"):
        load_query_config(nobrand)


def test_load_product_broken_and_empty_facts(tmp_path, data_dir):
    qcfg = load_query_config(_write_queries(tmp_path))

    broken = data_dir / "products" / "broken"
    broken.mkdir(parents=True)
    (broken / "facts.json").write_text("{坏", encoding="utf-8")
    with pytest.raises(PipelineError, match="产品库损坏"):
        load_product("broken", data_dir, qcfg.brand)

    empty = data_dir / "products" / "empty"
    empty.mkdir(parents=True)
    (empty / "facts.json").write_text('{"facts": []}', encoding="utf-8")
    with pytest.raises(PipelineError, match="空的"):
        load_product("empty", data_dir, qcfg.brand)


def test_load_product_review_gate_can_be_skipped_explicitly(tmp_path, data_dir, clock):
    """require_reviewed=False 是测试/调试旁路：显式关才关，默认必须拦。"""
    _make_product(data_dir, clock, reviewed=False)
    qcfg = load_query_config(_write_queries(tmp_path))
    product = load_product("deepparser", data_dir, qcfg.brand, require_reviewed=False)
    assert product.product_id == "deepparser"


def test_load_product_missing_review_file(tmp_path, data_dir, clock):
    _make_product(data_dir, clock, reviewed=False)
    (data_dir / "products" / "deepparser" / "REVIEW.md").unlink()
    qcfg = load_query_config(_write_queries(tmp_path))
    with pytest.raises(PipelineError, match="缺 REVIEW.md"):
        load_product("deepparser", data_dir, qcfg.brand)


def test_render_lead_budget_exhausted_raises():
    """问句 + 实体名太长，首段压不进 80 字 → 大声失败，不写残稿。"""
    long_query = Query(id="S02", text="很" * 60, intent="场景", priority="P1")
    product = Product("deepparser", "DeepParser", (_fact("短事实"),), Path("."))
    with pytest.raises(PipelineError, match="无法压进"):
        _render_lead(long_query, product, _BRAND)


def test_render_lead_skips_oversized_clause_for_shorter_one():
    """首条无数字事实太长就放过去找短的——80 字预算比「用第一条」重要。"""
    product = Product(
        "deepparser",
        "DeepParser",
        (
            _fact("这是一段特别特别长的能力描述" + "长" * 40),  # 超预算
            _fact("开箱即用"),  # 短到能进首段
        ),
        Path("."),
    )
    lead = _render_lead(_QUERY, product, _BRAND)
    assert "开箱即用" in lead
    assert "特别特别长" not in lead


def test_render_section_untrimmable_raises():
    """单条 claim 就顶穿 400 字：先砍填充句，砍无可砍就报错让产品库拆分。"""
    frame = _Frame("怎么看这件事？", "先看引导。", ("B",))
    giant = _fact("巨" * 500)
    with pytest.raises(PipelineError, match="无法再剪"):
        _render_section(frame, [giant], _ctx(), 0)


def test_render_faq_loop_exhaustion_and_trim(monkeypatch):
    """短事实 + 抬高 SECTION_MIN：四条 FAQ 全用完也不够 300 字（循环自然结束，
    无提前 break）；长事实：顶破 400 字 → 保前 3 条（数量下限优先于字数上限）。"""
    from geopublish.write import pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "SECTION_MIN", 300)  # 四条模板 ~216 字，永达不到 → 不 break
    tiny_query = Query(id="S01", text="提取", intent="场景", priority="P0")
    short_facts = tuple(Fact(claim=f"能{i}", source="s", tier="B", date="2026-10-26") for i in range(6))
    used = {i: 0 for i in range(len(short_facts))}
    faq = _render_faq(tiny_query, Product("deepparser", "DeepParser", short_facts, Path(".")), _BRAND, used)
    assert faq.count("**问：") == 4  # 循环跑满 4 条模板，没有提前 break

    monkeypatch.undo()  # 长事实半边用真实常量
    long_facts = tuple(_fact("长" * 120) for _ in range(6))
    used = {i: 0 for i in range(len(long_facts))}
    faq = _render_faq(_QUERY, Product("deepparser", "DeepParser", long_facts, Path(".")), _BRAND, used)
    assert faq.count("**问：") == 3  # 顶破 400 字 → 只保前 3 条


def test_build_body_refuses_beyond_physical_capacity(tmp_path, data_dir, clock):
    """word_min 高过版面物理容量（首段 80 + 11 节 × 400 封顶 = 4480）时：
    每节顶到 400 后补不动（585 continue）、一轮零增长（589 break）、
    总量仍不够 → 592 报错，不注水。"""
    from geopublish.write.pipeline import _build_body

    facts = tuple(_fact(f"短事实{i}", tier="A") for i in range(30))
    product = Product("deepparser", "DeepParser", facts, Path("."))
    # 实测容量 4746 字（含 markdown 符号与引用标记）：word_min 抬过它就永远够不着
    platform = PlatformConfig(name="csdn", word_min=5000, word_max=6000)
    with pytest.raises(PipelineError, match="撑不起这篇稿"):
        _build_body(_QUERY, product, _BRAND, platform)


# --- _assert_structure：八条断言各自的拒绝面（用通过稿做底，一次弄坏一处）


@pytest.fixture(scope="module")
def structure_kit(produced):
    result, body, qcfg = produced
    facts = tuple(
        Fact.from_json(f)
        for f in json.loads(
            (result.draft_dir.parent.parent / "products" / "deepparser" / "facts.json")
            .read_text(encoding="utf-8")
        )["facts"]
    )
    product = Product("deepparser", "DeepParser", facts, result.draft_dir)
    return body, product, qcfg.brand, qcfg.query("S01")


def test_assert_structure_accepts_produced_body(structure_kit):
    body, product, brand, query = structure_kit
    _assert_structure(body, product, brand, query)  # 不抛即通过


def test_assert_structure_rejects_too_few_h2(structure_kit):
    body, product, brand, query = structure_kit
    lead = extract_lead(body)
    first_h2 = body.split("\n## ")[1]
    mutilated = lead + "\n\n## " + first_h2
    with pytest.raises(PipelineError, match="H2 章节"):
        _assert_structure(mutilated, product, brand, query)


def test_assert_structure_rejects_non_question_h2(structure_kit):
    body, product, brand, query = structure_kit
    mutilated = body.replace("## ", "## 陈述句标题\n\n", 1)
    # 把第一个问句 H2 换成陈述句：问句标题行被吃掉，新标题不是问句
    with pytest.raises(PipelineError, match="非问句 H2"):
        _assert_structure(mutilated, product, brand, query)


def test_assert_structure_rejects_section_out_of_range(structure_kit):
    body, product, brand, query = structure_kit
    sections = iter_sections(body)
    lead = extract_lead(body)
    h2, _ = sections[0]
    mutilated = lead + "\n\n## " + h2 + "\n\n太短了\n\n" + "\n\n".join(
        f"## {h}\n\n{t}" for h, t in sections[1:]
    ) + "\n"
    with pytest.raises(PipelineError, match="不在 .* 区间"):
        _assert_structure(mutilated, product, brand, query)


def test_assert_structure_rejects_too_few_faq(structure_kit):
    """FAQ 节只留一条但字数撑够：绕过节长检查，单独命中「FAQ 不足」。"""
    body, product, brand, query = structure_kit
    faq_start = body.index("**问：")
    second_item = body.index("**问：", faq_start + 1)
    mutilated = body[:second_item] + "补充说明。" * 40 + "\n"  # FAQ 节 ≥200 字但只剩 1 条
    with pytest.raises(PipelineError, match="FAQ 不足"):
        _assert_structure(mutilated, product, brand, query)


def test_assert_structure_rejects_long_lead(structure_kit):
    body, product, brand, query = structure_kit
    mutilated = "超长的首段" * 20 + "\n\n## " + body.split("\n## ", 1)[1]
    with pytest.raises(PipelineError, match="超过 80 字"):
        _assert_structure(mutilated, product, brand, query)


def test_assert_structure_rejects_unsourced_number(structure_kit):
    body, product, brand, query = structure_kit
    with pytest.raises(PipelineError, match="无出处数字"):
        _assert_structure(body + "\n\n附录：命中率 87%。\n", product, brand, query)


def test_assert_structure_rejects_alias_leak(structure_kit):
    body, product, brand, query = structure_kit
    with pytest.raises(PipelineError, match="别名"):
        _assert_structure(body + "\n\n补一句：JOTO 的方案。\n", product, brand, query)


def test_assert_structure_rejects_low_entity_density(structure_kit):
    body, product, brand, query = structure_kit
    mutilated = body.replace("上海聚托", "某公司").replace("DeepParser", "某产品")
    with pytest.raises(PipelineError, match="实体仅出现"):
        _assert_structure(mutilated, product, brand, query)


# --- 标题压缩（680/682）与 CLI 错误分支


def test_title_compressed_once_when_over_limit(tmp_path, data_dir, clock):
    """title_max 收紧一档：先换短模板（680 行）；压得下就照常出稿。
    原始标题 37 字，短模板 34 字——35 正好卡住「压一次就够」的区间。"""
    _make_product(data_dir, clock)
    qcfg = load_query_config(_write_queries(tmp_path))
    platform = PlatformConfig(name="csdn", word_min=3000, word_max=5000, title_max=35)
    result = write_article(
        "deepparser", "S01",
        data_root=data_dir, query_config=qcfg, platform=platform, clock=clock,
    )
    meta = json.loads(result.meta_path.read_text(encoding="utf-8"))
    assert len(meta["title"]) <= 35
    assert meta["title"].endswith("答案")


def test_title_uncompressible_raises(tmp_path, data_dir, clock):
    _make_product(data_dir, clock)
    qcfg = load_query_config(_write_queries(tmp_path))
    platform = PlatformConfig(name="csdn", word_min=3000, word_max=5000, title_max=10)
    with pytest.raises(PipelineError, match="无法再压缩"):
        write_article(
            "deepparser", "S01",
            data_root=data_dir, query_config=qcfg, platform=platform, clock=clock,
        )


def test_cli_write_bad_config_is_exit_3(tmp_path, capsys):
    from geopublish.cli import EXIT_CONFIG_ERROR, main

    rc = main([
        "write", "deepparser", "--query", "S01",
        "--queries", str(tmp_path / "q.json"), "--config", str(tmp_path / "no.json"),
    ])
    assert rc == EXIT_CONFIG_ERROR
    assert "配置错误" in capsys.readouterr().err


def test_cli_write_rejected_is_exit_6_with_hit_details(tmp_path, data_dir, clock, capsys):
    """自检不过退 6，且前几条命中明细要打到 stderr——改稿的人靠它定位。"""
    from geopublish.cli import EXIT_FAILED, main

    src = tmp_path / "poison.md"
    lines = ["<!-- page 1 -->"]
    for i in range(15):
        lines.append(f"- 第{i}条事实：本产品是最好的企业级文档解析工具，支持私有化部署")
    src.write_text("\n".join(lines) + "\n", encoding="utf-8")
    result = ingest(src, "poison", data_dir, clock=clock)
    review = result.product_dir / "REVIEW.md"
    text = review.read_text(encoding="utf-8")
    text = re.sub(r"^reviewed_by:[ \t]*$", "reviewed_by: 拾一米", text, count=1, flags=re.M)
    text = re.sub(r"^reviewed_at:[ \t]*$", "reviewed_at: 2026-10-26T10:00:00+08:00", text, count=1, flags=re.M)
    review.write_text(text, encoding="utf-8")

    queries = _write_queries(tmp_path)
    config = _write_config(tmp_path, data_dir)
    rc = main([
        "write", "poison", "--query", "S01",
        "--queries", str(queries), "--data-dir", str(data_dir), "--config", str(config),
    ])
    err = capsys.readouterr().err
    assert rc == EXIT_FAILED
    assert "自检未通过" in err and "[absolute]" in err
