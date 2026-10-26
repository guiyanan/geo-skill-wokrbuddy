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

