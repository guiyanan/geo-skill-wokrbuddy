"""T-13 产品库入库（geopublish/write/ingest.py）。

钉死的行为（对应方案 UT-13-01~06 与 PRD R1/R15）：
- 每条事实必有 claim/source/tier/date，tier ∈ A/B/C/D，source 为空 = 入库失败
- 二次入库同产品是**合并**：旧事实保留原 source，新事实追加，claim 去重
- 解析出 0 条事实 = 失败，磁盘上不留产品目录（不留半份产品）
- ≥15 页 fixture 必须抽出 ≥12 条事实（G1：防空列表过检）
- 入库必出 REVIEW.md 人工抽检单；有新事实入库则抽检状态重置（G2）
"""

from __future__ import annotations

import json
import zipfile
from pathlib import Path

import pytest

from geopublish.cli import EXIT_OK, EXIT_USAGE, main
from geopublish.write.ingest import (
    IngestError,
    RawFact,
    classify_tier,
    ingest,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "products" / "deepparser-pitch.md"


def _write_md(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    return path


def _facts(product_dir: Path) -> list[dict]:
    return json.loads((product_dir / "facts.json").read_text(encoding="utf-8"))["facts"]


# --------------------------------------------------------------- tier 分级

def test_tier_a_numeric_indicator():
    assert classify_tier("DeepParser 对中文复杂版式文档的解析准确率达到 98.2%") == "A"
    assert classify_tier("某头部券商尽调资料整理人力投入下降 70%") == "A"  # 带数字的案例仍是 A


def test_tier_c_scenario_without_numbers():
    assert classify_tier("例如某制造企业将图纸审查流程整体迁入系统") == "C"


def test_tier_b_capability_statement():
    assert classify_tier("支持私有化部署与纯内网离线运行") == "B"


def test_tier_d_fallback():
    assert classify_tier("让每一份企业文档都被直接读懂") == "D"


# --------------------------------------------------------------- 抽取规则

def test_extract_uses_page_markers(tmp_path, data_dir):
    src = _write_md(tmp_path, "deck.md", "<!-- page 1 -->\n# 标题\n- 第一条有足够长度的事实陈述\n"
                                         "<!-- page 3 -->\n- 第二条有足够长度的事实陈述\n")
    result = ingest(src, "demo", data_dir)
    facts = _facts(result.product_dir)
    assert [f["source"] for f in facts] == ["deck.md 第1页", "deck.md 第3页"]


def test_extract_without_markers_falls_back_to_page_1(tmp_path, data_dir):
    src = _write_md(tmp_path, "notes.txt", "- 一条没有任何页码标记的事实陈述\n")
    result = ingest(src, "demo", data_dir)
    assert _facts(result.product_dir)[0]["source"] == "notes.txt 第1页"


def test_extract_skips_noise_lines(tmp_path, data_dir):
    src = _write_md(tmp_path, "deck.md", "<!-- page 1 -->\n# 大标题不是事实\n\n"
                                         "<!-- 注释不是事实 -->\n- 短\n"
                                         "* 星号风格的一条有效事实陈述\n"
                                         "• 圆点风格的一条有效事实陈述\n"
                                         "1. 编号风格的一条有效事实陈述\n"
                                         "正文段落不缩进也不带符号，不进入事实清单。\n")
    result = ingest(src, "demo", data_dir)
    claims = [f["claim"] for f in _facts(result.product_dir)]
    assert claims == [
        "星号风格的一条有效事实陈述",
        "圆点风格的一条有效事实陈述",
        "编号风格的一条有效事实陈述",
    ]


def test_extract_pptx_reads_slides_in_numeric_order(tmp_path, data_dir):
    """pptx 用 stdlib zipfile 解，slide10 不能排在 slide2 前面。"""
    pptx = tmp_path / "deck.pptx"
    slides = {
        2: ["第二页的第一条有效事实陈述", ""],
        10: ["第十页的第一条有效事实陈述"],
    }
    ns = ('xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" '
          'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
    with zipfile.ZipFile(pptx, "w") as z:
        for num, paras in slides.items():
            body = "".join(f"<a:p><a:r><a:t>{t}</a:t></a:r></a:p>" for t in paras)
            z.writestr(
                f"ppt/slides/slide{num}.xml",
                f"<p:sld {ns}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>",
            )
    result = ingest(pptx, "demo", data_dir)
    facts = _facts(result.product_dir)
    assert [f["source"] for f in facts] == ["deck.pptx 第2页", "deck.pptx 第10页"]


def test_extract_corrupt_pptx_fails(tmp_path, data_dir):
    src = _write_md(tmp_path, "broken.pptx", "这不是 zip\n")
    with pytest.raises(IngestError, match="不是合法的 pptx"):
        ingest(src, "demo", data_dir)
    assert not (data_dir / "products" / "demo").exists()


def test_extract_unsupported_suffix_fails(tmp_path, data_dir):
    src = _write_md(tmp_path, "deck.key", "- 一条有足够长度的事实陈述\n")
    with pytest.raises(IngestError, match="不支持的资料格式"):
        ingest(src, "demo", data_dir)
    assert not (data_dir / "products" / "demo").exists()


# --------------------------------------------------------------- UT-13-01：结构

def test_ut_13_01_every_fact_has_claim_source_tier(tmp_path, data_dir):
    result = ingest(FIXTURE, "deepparser", data_dir)
    for fact in _facts(result.product_dir):
        assert fact["claim"].strip()
        assert fact["source"].strip()
        assert fact["tier"] in {"A", "B", "C", "D"}
        assert fact["date"].strip()


# --------------------------------------------------------------- UT-13-02：合并更新

def test_ut_13_02_second_ingest_merges_and_preserves_source(tmp_path, data_dir):
    first = _write_md(tmp_path, "v1.md", "<!-- page 1 -->\n- 解析准确率达到百分之九十八点二的陈述\n")
    ingest(first, "deepparser", data_dir)

    second = _write_md(tmp_path, "v2.md", "<!-- page 5 -->\n"
                                          "- 解析准确率达到百分之九十八点二的陈述\n"
                                          "- 新增一条关于并发吞吐能力的事实陈述\n")
    result = ingest(second, "deepparser", data_dir)

    facts = _facts(result.product_dir)
    assert result.facts_total == 2 and result.facts_added == 1
    # 旧事实保留原出处：v1.md 第1页，不被 v2.md 第5页覆盖
    assert facts[0]["source"] == "v1.md 第1页"
    assert facts[1]["source"] == "v2.md 第5页"
    manifest = json.loads((result.product_dir / "facts.json").read_text(encoding="utf-8"))
    assert len(manifest["sources"]) == 2


# --------------------------------------------------------------- UT-13-03：零事实即失败

def test_ut_13_03_zero_facts_fails_without_product_dir(tmp_path, data_dir):
    src = _write_md(tmp_path, "empty.md", "<!-- page 1 -->\n# 只有标题\n没有任何条目。\n")
    with pytest.raises(IngestError, match="0 条事实"):
        ingest(src, "demo", data_dir)
    assert not (data_dir / "products" / "demo").exists()


def test_zero_facts_on_merge_also_fails_and_keeps_library(tmp_path, data_dir):
    src = _write_md(tmp_path, "ok.md", "<!-- page 1 -->\n- 一条有足够长度的事实陈述\n")
    ingest(src, "demo", data_dir)
    before = (data_dir / "products" / "demo" / "facts.json").read_text(encoding="utf-8")

    empty = _write_md(tmp_path, "empty.md", "# 没有条目\n")
    with pytest.raises(IngestError, match="0 条事实"):
        ingest(empty, "demo", data_dir)
    assert (data_dir / "products" / "demo" / "facts.json").read_text(encoding="utf-8") == before


# --------------------------------------------------------------- UT-13-04：claim 去重

def test_ut_13_04_duplicate_claim_within_one_doc_ingested_once(tmp_path, data_dir):
    src = _write_md(tmp_path, "dup.md", "<!-- page 1 -->\n- 同一条事实在这里出现第一次\n"
                                        "<!-- page 2 -->\n- 同一条事实在这里出现第一次。\n")
    result = ingest(src, "demo", data_dir)
    assert result.facts_total == 1  # 尾部标点差异不影响去重


def test_ut_13_04_duplicate_claim_across_ingests_stays_once(tmp_path, data_dir):
    src = _write_md(tmp_path, "a.md", "<!-- page 1 -->\n- 跨两次入库重复的事实陈述\n")
    ingest(src, "demo", data_dir)
    result = ingest(src, "demo", data_dir)
    assert result.facts_total == 1 and result.facts_added == 0


# --------------------------------------------------------------- UT-13-05：≥15 页 → ≥12 条

def test_ut_13_05_fixture_yields_at_least_12_facts(tmp_path, data_dir):
    text = FIXTURE.read_text(encoding="utf-8")
    assert text.count("<!-- page ") >= 15, "fixture 自身不足 15 页，用例失效"

    result = ingest(FIXTURE, "deepparser", data_dir)
    facts = _facts(result.product_dir)
    assert facts and len(facts) >= 12


# --------------------------------------------------------------- UT-13-06：空 source 即失败

def test_ut_13_06_empty_source_fails_without_half_product(tmp_path, data_dir):
    def bad_extractor(path: Path) -> list[RawFact]:
        return [RawFact(claim="一条来源字段被掏空的事实陈述", source="  ")]

    src = _write_md(tmp_path, "x.md", "占位，extractor 被注入不会真读\n")
    with pytest.raises(IngestError, match="source 为空"):
        ingest(src, "demo", data_dir, extractor=bad_extractor)
    assert not (data_dir / "products" / "demo").exists()


def test_extractor_exception_propagates_and_leaves_nothing(tmp_path, data_dir):
    def boom(path: Path) -> list[RawFact]:
        raise RuntimeError("解析器自己炸了")

    src = _write_md(tmp_path, "x.md", "占位\n")
    with pytest.raises(RuntimeError, match="自己炸了"):
        ingest(src, "demo", data_dir, extractor=boom)
    assert not (data_dir / "products" / "demo").exists()


# --------------------------------------------------------------- 产物布局与抽检单

def test_product_dir_layout_and_manifest(tmp_path, data_dir, clock):
    result = ingest(FIXTURE, "deepparser", data_dir, clock=clock)
    product = result.product_dir
    assert product == data_dir / "products" / "deepparser"
    assert (product / "sources" / "deepparser-pitch.md").is_file()  # 源文件留档，可追溯

    manifest = json.loads((product / "facts.json").read_text(encoding="utf-8"))
    assert manifest["product_id"] == "deepparser"
    assert manifest["sources"][0]["name"] == "deepparser-pitch.md"
    assert manifest["sources"][0]["pages"] >= 15
    assert manifest["sources"][0]["sha256"]
    assert manifest["facts"][0]["date"] == "2026-10-21"  # FakeClock，确定性


def test_review_md_checklist_and_blank_fields(tmp_path, data_dir):
    result = ingest(FIXTURE, "deepparser", data_dir)
    review = (result.product_dir / "REVIEW.md").read_text(encoding="utf-8")
    assert "未越界" in review and "tier" in review and "遗漏" in review  # 三项抽检
    assert "reviewed_by:" in review and "reviewed_at:" in review
    assert "不进入自动成稿队列" in review


def test_review_resets_when_new_facts_arrive(tmp_path, data_dir):
    first = _write_md(tmp_path, "v1.md", "<!-- page 1 -->\n- 第一批的一条有效事实陈述\n")
    result = ingest(first, "demo", data_dir)
    review_path = result.product_dir / "REVIEW.md"
    review_path.write_text(
        review_path.read_text(encoding="utf-8").replace("reviewed_by: ", "reviewed_by: 拾一米"),
        encoding="utf-8",
    )

    second = _write_md(tmp_path, "v2.md", "<!-- page 1 -->\n- 第二批新增的一条有效事实陈述\n")
    result = ingest(second, "demo", data_dir)
    review = review_path.read_text(encoding="utf-8")
    assert "reviewed_by: \n" in review or review.rstrip().endswith("reviewed_by:")  # 被重置
    assert "拾一米" in review  # 但上一次抽检记录留痕


def test_review_preserved_when_nothing_new(tmp_path, data_dir):
    src = _write_md(tmp_path, "a.md", "<!-- page 1 -->\n- 一条重复入库的有效事实陈述\n")
    result = ingest(src, "demo", data_dir)
    review_path = result.product_dir / "REVIEW.md"
    review_path.write_text("人工改过的抽检单", encoding="utf-8")

    ingest(src, "demo", data_dir)
    assert review_path.read_text(encoding="utf-8") == "人工改过的抽检单"


# --------------------------------------------------------------- 产品 id

def test_product_id_must_be_slug(tmp_path, data_dir):
    src = _write_md(tmp_path, "a.md", "<!-- page 1 -->\n- 一条有足够长度的事实陈述\n")
    with pytest.raises(IngestError, match="product_id"):
        ingest(src, "Deep Parser", data_dir)
    assert not (data_dir / "products" / "Deep Parser").exists()


def test_product_id_derived_from_filename(tmp_path, data_dir):
    src = _write_md(tmp_path, "DeepParser Pitch 2026.md", "<!-- page 1 -->\n- 一条有足够长度的事实陈述\n")
    result = ingest(src, None, data_dir)
    assert result.product_id == "deepparser-pitch-2026"


def test_product_id_not_derivable_from_chinese_filename(tmp_path, data_dir):
    src = _write_md(tmp_path, "产品资料.md", "<!-- page 1 -->\n- 一条有足够长度的事实陈述\n")
    with pytest.raises(IngestError, match="--product-id"):
        ingest(src, None, data_dir)


# --------------------------------------------------------------- Fact 模型边界

def test_fact_model_rejects_invalid_construction():
    from geopublish.write.ingest import Fact

    with pytest.raises(IngestError, match="claim 不能为空"):
        Fact(claim=" ", source="a.md 第1页", tier="A", date="2026-10-26")
    with pytest.raises(IngestError, match="source 不能为空"):
        Fact(claim="有足够长度的事实", source="", tier="A", date="2026-10-26")
    with pytest.raises(IngestError, match="tier 只能是"):
        Fact(claim="有足够长度的事实", source="a.md 第1页", tier="S", date="2026-10-26")
    with pytest.raises(IngestError, match="date 不能为空"):
        Fact(claim="有足够长度的事实", source="a.md 第1页", tier="A", date=" ")


def test_fact_from_json_missing_field(tmp_path, data_dir):
    src = _write_md(tmp_path, "a.md", "<!-- page 1 -->\n- 一条有足够长度的事实陈述\n")
    result = ingest(src, "demo", data_dir)
    facts_path = result.product_dir / "facts.json"
    broken = json.loads(facts_path.read_text(encoding="utf-8"))
    del broken["facts"][0]["source"]
    facts_path.write_text(json.dumps(broken, ensure_ascii=False), encoding="utf-8")

    new = _write_md(tmp_path, "b.md", "<!-- page 1 -->\n- 另一条有足够长度的事实陈述\n")
    with pytest.raises(IngestError, match="缺字段"):
        ingest(new, "demo", data_dir)


def test_corrupt_facts_json_refuses_merge(tmp_path, data_dir):
    src = _write_md(tmp_path, "a.md", "<!-- page 1 -->\n- 一条有足够长度的事实陈述\n")
    result = ingest(src, "demo", data_dir)
    (result.product_dir / "facts.json").write_text("{ 写花了", encoding="utf-8")

    with pytest.raises(IngestError, match="产品库损坏"):
        ingest(src, "demo", data_dir)


def test_source_without_page_marker_counts_as_page_1(tmp_path, data_dir):
    def extractor(path: Path) -> list[RawFact]:
        return [RawFact(claim="来自外部链接的一条事实陈述", source="https://example.com/spec")]

    src = _write_md(tmp_path, "x.md", "占位\n")
    result = ingest(src, "demo", data_dir, extractor=extractor)
    manifest = json.loads((result.product_dir / "facts.json").read_text(encoding="utf-8"))
    assert manifest["sources"][0]["pages"] == 1
    assert manifest["facts"][0]["source"] == "https://example.com/spec"


def test_atomic_write_failure_propagates_without_residue(tmp_path, data_dir, monkeypatch):
    import os as _os

    import geopublish.write.ingest as ingest_mod

    def boom(src, dst):
        raise OSError("磁盘满了")

    monkeypatch.setattr(_os, "replace", boom)
    src = _write_md(tmp_path, "a.md", "<!-- page 1 -->\n- 一条有足够长度的事实陈述\n")
    with pytest.raises(OSError, match="磁盘满了"):
        ingest(src, "demo", data_dir)
    products = data_dir / "products"
    leftovers = list(products.iterdir()) if products.exists() else []
    assert leftovers == [], f"失败留下残留：{leftovers}"


def test_review_note_records_previous_reviewer_with_timestamp(tmp_path, data_dir):
    first = _write_md(tmp_path, "v1.md", "<!-- page 1 -->\n- 第一批的一条有效事实陈述\n")
    result = ingest(first, "demo", data_dir)
    review_path = result.product_dir / "REVIEW.md"
    text = review_path.read_text(encoding="utf-8")
    review_path.write_text(
        text.replace("reviewed_by: ", "reviewed_by: 拾一米")
            .replace("reviewed_at: ", "reviewed_at: 2026-10-25"),
        encoding="utf-8",
    )

    second = _write_md(tmp_path, "v2.md", "<!-- page 1 -->\n- 第二批新增的一条有效事实陈述\n")
    ingest(second, "demo", data_dir)
    assert "拾一米 @ 2026-10-25" in review_path.read_text(encoding="utf-8")


def test_review_note_absent_when_never_reviewed(tmp_path, data_dir):
    first = _write_md(tmp_path, "v1.md", "<!-- page 1 -->\n- 第一批的一条有效事实陈述\n")
    ingest(first, "demo", data_dir)
    second = _write_md(tmp_path, "v2.md", "<!-- page 1 -->\n- 第二批新增的一条有效事实陈述\n")
    result = ingest(second, "demo", data_dir)
    assert "- 上次抽检：无" in (result.product_dir / "REVIEW.md").read_text(encoding="utf-8")


def test_review_regenerated_cleanly_when_review_file_deleted(tmp_path, data_dir):
    """库在、抽检单被人删了 → 合并且有新事实时重新生成，按「无历史抽检」处理。"""
    first = _write_md(tmp_path, "v1.md", "<!-- page 1 -->\n- 第一批的一条有效事实陈述\n")
    result = ingest(first, "demo", data_dir)
    (result.product_dir / "REVIEW.md").unlink()

    second = _write_md(tmp_path, "v2.md", "<!-- page 1 -->\n- 第二批新增的一条有效事实陈述\n")
    result = ingest(second, "demo", data_dir)
    review = (result.product_dir / "REVIEW.md").read_text(encoding="utf-8")
    assert "- 上次抽检：无" in review and "reviewed_by:" in review


# --------------------------------------------------------------- CLI

def test_cli_ingest_success(tmp_path, data_dir, capsys):
    code = main(["ingest", str(FIXTURE), "--product-id", "deepparser", "--data-dir", str(data_dir)])
    out = capsys.readouterr().out
    assert code == EXIT_OK
    assert "deepparser" in out and "REVIEW.md" in out
    assert (data_dir / "products" / "deepparser" / "facts.json").is_file()


def test_cli_ingest_missing_file_is_usage_error(tmp_path, data_dir, capsys):
    code = main(["ingest", str(tmp_path / "nope.md"), "--data-dir", str(data_dir)])
    assert code == EXIT_USAGE
    assert "不存在" in capsys.readouterr().err


def test_cli_ingest_failure_is_usage_error(tmp_path, data_dir, capsys):
    src = _write_md(tmp_path, "empty.md", "# 没有条目\n")
    code = main(["ingest", str(src), "--data-dir", str(data_dir)])
    assert code == EXIT_USAGE
    assert "入库失败" in capsys.readouterr().err
    assert not (data_dir / "products" / "empty").exists()
