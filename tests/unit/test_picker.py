"""T-17 选题串联 geo-master（geopublish/select/picker.py）。

钉死的行为（对应方案 UT-17-01~06 与 PRD R17）：
- UT-17-01 缺口清单交给 geo-master 的 prioritize()，档位/分数原样来自脚本输出
- UT-17-02 只有 P0/P1 档进候选，P2/P3 直接出局
- UT-17-03 「同问句 × 同平台」refresh_days 天内已发 → 排除（窗口来自配置，默认 30）
- UT-17-04 候选为空 → no_topic，当日跳过且留痕（不硬写）
- UT-17-05 选题必须能映射到已抽检产品库事实，映射不上 → 排除
- UT-17-06 问句 id 全部来自 joto.json，自建问句在入口就被拒
- DoD：选题留痕 data/select/<date>.json，可回溯到当日缺口输入与打分
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.state import PublishLog
from geopublish.write.ingest import ingest
from geopublish.write.pipeline import load_query_config
from geopublish.select.picker import (
    QA_MIN_MATCH,
    Gap,
    PickerError,
    build_gaps,
    build_qa_matrix,
    latest_samples_path,
    load_prioritizer,
    load_qa_index,
    load_samples,
    pick_topic,
    save_trace,
    write_qa_matrix,
)

pytestmark = pytest.mark.unit

FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "products" / "deepparser-pitch.md"
REAL_PRIORITIZER = (
    Path.home() / ".workbuddy" / "skills" / "geo-master" / "scripts" / "gap_prioritizer.py"
)

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
        {"id": "C02", "text": "企业知识库 AI 产品怎么选", "intent": "评估", "priority": "P1"},
        {"id": "D01", "text": "中小企业怎么选 AI 解决方案供应商", "intent": "决策", "priority": "P0"},
        {"id": "S01", "text": "PDF 里的表格怎么自动提取成结构化数据", "intent": "场景", "priority": "P0"},
        {"id": "S02", "text": "企业内部文档怎么用 AI 做问答", "intent": "场景", "priority": "P1"},
        {"id": "X01", "text": "Dify 和 Coze 哪个好", "intent": "竞品", "priority": "P0"},
    ],
}

NOW = "2026-10-27T10:00:00+08:00"


# ---------------------------------------------------------------- 夹具

def _qcfg(tmp_path: Path):
    path = tmp_path / "joto.json"
    path.write_text(json.dumps(QUERIES, ensure_ascii=False), encoding="utf-8")
    return load_query_config(path)


def _product(data_dir: Path, clock, *, reviewed: bool = True, product_id: str = "deepparser"):
    result = ingest(FIXTURE, product_id, data_dir, clock=clock)
    if reviewed:
        review = result.product_dir / "REVIEW.md"
        text = review.read_text(encoding="utf-8")
        text = re.sub(r"^reviewed_by:[ \t]*$", "reviewed_by: 拾一米", text, count=1, flags=re.M)
        text = re.sub(
            r"^reviewed_at:[ \t]*$", "reviewed_at: 2026-10-26T10:00:00+08:00", text, count=1, flags=re.M
        )
        review.write_text(text, encoding="utf-8")
    return result


def _write_qa(data_dir: Path, product_id: str, query_ids: list[str]) -> Path:
    product_dir = data_dir / "products" / product_id
    product_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "product_id": product_id,
        "generated_at": NOW,
        "queries": [{"query_id": qid, "score": 3, "matched": ["文档", "解析"]} for qid in query_ids],
    }
    path = product_dir / "qa.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _gap(query_id: str, value: int = 5, reach: int = 5, win: int = 5, cost: int = 1) -> Gap:
    return Gap(query_id=query_id, value=value, reach=reach, win=win, cost=cost)


def _fake_prioritize(rows):
    """测试替身：不打分，只按输入顺序回传固定档位。真实打分是 geo-master 的事。"""

    def _run(received):
        _fake_prioritize.calls.append([dict(r) for r in received])
        out = []
        for i, row in enumerate(received):
            out.append(
                {
                    "缺口": row["缺口"],
                    "优先级分": 100.0 - i,
                    "档位": rows[i % len(rows)],
                    "象限": "攻坚区",
                    "处理": "测试",
                }
            )
        return out

    return _run


_fake_prioritize.calls = []


def _log_row(data_dir: Path, slug: str, status: str, ts: str, platform: str = "csdn") -> None:
    PublishLog(data_dir / "publish-log.csv").append_row(
        {
            "ts": ts,
            "platform": platform,
            "account": "acct",
            "slug": slug,
            "idem_key": "k" * 64,
            "status": status,
            "url": "https://example.com/x" if status == "published" else "",
            "article_id": "1" if status == "published" else "",
            "screenshot": "",
            "indexed_at": "",
            "note": "",
        }
    )


def _pick(
    tmp_path: Path,
    gaps,
    *,
    prioritize,
    qa_query_ids=("S01",),
    reviewed=True,
    refresh_days=30,
    platform="csdn",
    count=1,
):
    """装配一套最小可用的 pick 环境。返回 (PickResult, data_dir)。"""
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True, exist_ok=True)
    clock = FakeClock(NOW)
    qcfg = _qcfg(tmp_path)
    _product(data_dir, clock, reviewed=reviewed)
    if qa_query_ids:
        _write_qa(data_dir, "deepparser", list(qa_query_ids))
    qa_index = load_qa_index(data_dir / "products", qcfg)
    result = pick_topic(
        gaps,
        query_config=qcfg,
        qa_index=qa_index,
        log=PublishLog(data_dir / "publish-log.csv"),
        platform=platform,
        refresh_days=refresh_days,
        data_root=data_dir,
        prioritize=prioritize,
        clock=clock,
        count=count,
    )
    return result, data_dir


# ---------------------------------------------------------------- UT-17-01

def test_ut_17_01_calls_prioritizer_exactly_once_with_assembled_rows(tmp_path):
    _fake_prioritize.calls.clear()
    gaps = [_gap("S01"), _gap("D01")]
    result, _ = _pick(tmp_path, gaps, prioritize=_fake_prioritize(["P0 立即做", "P1 计划做"]))
    assert len(_fake_prioritize.calls) == 1
    assert _fake_prioritize.calls[0] == [g.to_row() for g in gaps]
    assert result.status == "picked"


def test_ut_17_01_tier_comes_from_script_output_not_picker(tmp_path):
    # 这个 gap 的四个分数按公式远够不上 P1；picker 若自己算就会露馅
    gaps = [_gap("S01", value=1, reach=1, win=1, cost=5)]
    result, _ = _pick(tmp_path, gaps, prioritize=_fake_prioritize(["P1 计划做"]))
    assert result.choice is not None
    assert result.choice.tier == "P1 计划做"
    assert result.choice.score == 100.0


@pytest.mark.skipif(not REAL_PRIORITIZER.is_file(), reason="geo-master 不在本机（CI 无此依赖）")
def test_ut_17_01_real_gap_prioritizer_passthrough(tmp_path):
    """拿真脚本验一次接线：picker 的 prioritized 输出与脚本直跑完全一致。"""
    prioritize = load_prioritizer(REAL_PRIORITIZER)
    gaps = [
        _gap("S01", value=5, reach=5, win=5, cost=2),   # 62.5 → P0
        _gap("D01", value=1, reach=2, win=5, cost=1),   # 10.0 → P2
        _gap("C01", value=5, reach=5, win=1, cost=5),   # 5.0 → P3
    ]
    result, _ = _pick(tmp_path, gaps, prioritize=prioritize)
    expected = prioritize([g.to_row() for g in gaps])
    assert [dict(r) for r in result.prioritized] == expected
    # 只有 P0 的 S01 活下来（D01=P2、C01=P3 被 UT-17-02 拦掉）
    assert result.choice is not None and result.choice.query_id == "S01"
    assert result.choice.tier == "P0 立即做"


# ---------------------------------------------------------------- UT-17-02

def test_ut_17_02_only_p0_and_p1_enter_candidates(tmp_path):
    # 脚本把 S01 判成 P2、D01 判成 P1：哪怕 S01 分数更高也只能选 D01
    gaps = [_gap("S01"), _gap("D01")]
    prio = _fake_prioritize(["P2 观察", "P1 计划做"])
    result, _ = _pick(tmp_path, gaps, prioritize=prio, qa_query_ids=("S01", "D01"))
    assert result.choice is not None
    assert result.choice.query_id == "D01"
    tier_out = [e for e in result.exclusions if e.stage == "tier"]
    assert [e.query_id for e in tier_out] == ["S01"]
    assert "P2" in tier_out[0].reason


def test_ut_17_02_p3_is_excluded(tmp_path):
    result, _ = _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P3 放弃"]))
    assert result.status == "no_topic"
    assert result.exclusions[0].stage == "tier"


# ---------------------------------------------------------------- UT-17-03

def test_ut_17_03_recently_published_same_query_same_platform_excluded(tmp_path):
    gaps = [_gap("S01"), _gap("D01")]
    prio = _fake_prioritize(["P0 立即做", "P0 立即做"])
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True, exist_ok=True)
    clock = FakeClock(NOW)
    qcfg = _qcfg(tmp_path)
    _product(data_dir, clock)
    _write_qa(data_dir, "deepparser", ["S01", "D01"])
    # 7 天前发过 deepparser-s01 → S01 必须被排除，落到 D01
    _log_row(data_dir, "deepparser-s01", "published", "2026-10-20T10:00:00+08:00")
    result = pick_topic(
        gaps,
        query_config=qcfg,
        qa_index=load_qa_index(data_dir / "products", qcfg),
        log=PublishLog(data_dir / "publish-log.csv"),
        platform="csdn",
        refresh_days=30,
        data_root=data_dir,
        prioritize=prio,
        clock=clock,
    )
    assert result.choice is not None and result.choice.query_id == "D01"
    recent = [e for e in result.exclusions if e.stage == "recently_published"]
    assert [e.query_id for e in recent] == ["S01"]


def test_ut_17_03_older_than_refresh_days_is_allowed(tmp_path):
    # 31 天前发过 → 过了翻新周期，允许再发
    data_dir_setup = tmp_path
    gaps = [_gap("S01")]
    result_data = data_dir_setup / "data"
    (result_data / "products").mkdir(parents=True, exist_ok=True)
    clock = FakeClock(NOW)
    qcfg = _qcfg(tmp_path)
    _product(result_data, clock)
    _write_qa(result_data, "deepparser", ["S01"])
    _log_row(result_data, "deepparser-s01", "published", "2026-09-26T10:00:00+08:00")
    result = pick_topic(
        gaps,
        query_config=qcfg,
        qa_index=load_qa_index(result_data / "products", qcfg),
        log=PublishLog(result_data / "publish-log.csv"),
        platform="csdn",
        refresh_days=30,
        data_root=result_data,
        prioritize=_fake_prioritize(["P0 立即做"]),
        clock=clock,
    )
    assert result.status == "picked"


def test_ut_17_03_exactly_refresh_days_old_is_allowed(tmp_path):
    # 恰好 30 天整：「30 天内」按 age < 30 天计，边界值放行（翻新周期语义）
    result, data_dir = _pick_setup_with_log(
        tmp_path, "2026-09-27T10:00:00+08:00", refresh_days=30
    )
    assert result.status == "picked"


def _pick_setup_with_log(tmp_path: Path, log_ts: str, *, refresh_days: int, status="published",
                         platform="csdn", slug="deepparser-s01"):
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True, exist_ok=True)
    clock = FakeClock(NOW)
    qcfg = _qcfg(tmp_path)
    _product(data_dir, clock)
    _write_qa(data_dir, "deepparser", ["S01"])
    _log_row(data_dir, slug, status, log_ts, platform=platform)
    result = pick_topic(
        [_gap("S01")],
        query_config=qcfg,
        qa_index=load_qa_index(data_dir / "products", qcfg),
        log=PublishLog(data_dir / "publish-log.csv"),
        platform="csdn",
        refresh_days=refresh_days,
        data_root=data_dir,
        prioritize=_fake_prioritize(["P0 立即做"]),
        clock=clock,
    )
    return result, data_dir


def test_ut_17_03_failed_status_does_not_block(tmp_path):
    # 「已发」只认 published；failed 是稿子没碰平台，不占用翻新窗口
    result, _ = _pick_setup_with_log(tmp_path, NOW, refresh_days=30, status="failed")
    assert result.status == "picked"


def test_ut_17_03_other_platform_does_not_block(tmp_path):
    result, _ = _pick_setup_with_log(tmp_path, NOW, refresh_days=30, platform="juejin")
    assert result.status == "picked"


def test_ut_17_03_other_query_slug_does_not_block(tmp_path):
    result, _ = _pick_setup_with_log(tmp_path, NOW, refresh_days=30, slug="deepparser-d01")
    assert result.status == "picked"


# ---------------------------------------------------------------- UT-17-04

def test_ut_17_04_empty_gaps_returns_no_topic(tmp_path):
    result, _ = _pick(tmp_path, [], prioritize=_fake_prioritize(["P0 立即做"]))
    assert result.status == "no_topic"
    assert result.choice is None


def test_ut_17_04_all_excluded_returns_no_topic_and_trace_records_reasons(tmp_path):
    result, data_dir = _pick(
        tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P2 观察"])
    )
    assert result.status == "no_topic"
    trace = save_trace(data_dir, result, clock=FakeClock(NOW))
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert payload["status"] == "no_topic"
    assert payload["choice"] is None
    assert payload["exclusions"], "no_topic 也必须留下排除原因（日报要靠它写原因）"


# ---------------------------------------------------------------- UT-17-05

def test_ut_17_05_unmapped_query_excluded(tmp_path):
    # qa.json 只认领了 D01，S01 没有产品映射 → 排除
    result, _ = _pick(
        tmp_path,
        [_gap("S01")],
        prioritize=_fake_prioritize(["P0 立即做"]),
        qa_query_ids=("D01",),
    )
    assert result.status == "no_topic"
    assert result.exclusions[0].stage == "no_mapping"


def test_ut_17_05_mapped_to_missing_product_excluded(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True, exist_ok=True)
    clock = FakeClock(NOW)
    qcfg = _qcfg(tmp_path)
    # qa.json 指向一个根本没入库的产品
    _write_qa(data_dir, "ghost-product", ["S01"])
    result = pick_topic(
        [_gap("S01")],
        query_config=qcfg,
        qa_index=load_qa_index(data_dir / "products", qcfg),
        log=PublishLog(data_dir / "publish-log.csv"),
        platform="csdn",
        refresh_days=30,
        data_root=data_dir,
        prioritize=_fake_prioritize(["P0 立即做"]),
        clock=clock,
    )
    assert result.status == "no_topic"
    assert result.exclusions[0].stage == "product_unusable"


def test_ut_17_05_mapped_to_unreviewed_product_excluded(tmp_path):
    # G2：REVIEW.md 未回填的产品不进自动成稿队列，选题层同样不许放行
    result, _ = _pick(
        tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0 立即做"]), reviewed=False
    )
    assert result.status == "no_topic"
    assert result.exclusions[0].stage == "product_unusable"
    assert "抽检" in result.exclusions[0].reason


def test_ut_17_05_mapped_and_reviewed_is_picked(tmp_path):
    result, _ = _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0 立即做"]))
    assert result.status == "picked"
    assert result.choice.product_id == "deepparser"
    assert result.choice.query_text == "PDF 里的表格怎么自动提取成结构化数据"


# ---------------------------------------------------------------- UT-17-06

def test_ut_17_06_gap_with_unknown_query_id_is_excluded(tmp_path):
    result, _ = _pick(tmp_path, [_gap("Z99")], prioritize=_fake_prioritize(["P0 立即做"]))
    assert result.status == "no_topic"
    assert result.exclusions[0].stage == "unknown_query"
    assert "joto.json" in result.exclusions[0].reason


def test_ut_17_06_qa_with_unknown_query_id_rejected_at_load(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True, exist_ok=True)
    _write_qa(data_dir, "deepparser", ["S01", "Z99"])
    with pytest.raises(PickerError, match="joto.json"):
        load_qa_index(data_dir / "products", _qcfg(tmp_path))


def test_ut_17_06_build_gaps_never_invents_queries(tmp_path):
    qcfg = _qcfg(tmp_path)
    samples = [
        {"query_id": "Z99", "cited": False, "mentioned": False, "competitors_seen": []},
        {"query_id": "S01", "cited": False, "mentioned": True, "competitors_seen": []},
    ]
    gaps = build_gaps(samples, qcfg, qa_index={}, facts_count=lambda pid: 0)
    ids = {g.query_id for g in gaps}
    assert "S01" in ids
    assert "Z99" not in ids  # 样本里冒出来的表外 id 不进选题


# ---------------------------------------------------------------- 打分输入（build_gaps）

def _sample(query_id, *, cited=False, mentioned=True, competitors=()):
    return {
        "query_id": query_id,
        "cited": cited,
        "mentioned": mentioned,
        "competitors_seen": list(competitors),
    }


def test_build_gaps_cited_query_is_not_a_gap(tmp_path):
    qcfg = _qcfg(tmp_path)
    gaps = build_gaps([_sample("S01", cited=True)], qcfg, qa_index={}, facts_count=lambda p: 0)
    ids = {g.query_id for g in gaps}
    assert "S01" not in ids  # 已被引用 = 不是缺口
    assert ids  # 其余未被引用的问句仍是缺口


def test_build_gaps_uncited_query_is_gap_with_traceable_evidence(tmp_path):
    qcfg = _qcfg(tmp_path)
    qa_index = {"S01": _qa_entry("deepparser")}
    gaps = build_gaps(
        [_sample("S01", cited=False, competitors=["Dify", "Coze"])],
        qcfg,
        qa_index=qa_index,
        facts_count=lambda p: 24,
    )
    gap = next(g for g in gaps if g.query_id == "S01")
    assert gap.evidence["cited"] is False
    assert gap.evidence["competitors"] == ["Coze", "Dify"]
    assert gap.evidence["mapped_product"] == "deepparser"
    assert gap.evidence["facts"] == 24


def _qa_entry(product_id: str, score: int = 3):
    from geopublish.select.picker import QaEntry

    return QaEntry(product_id=product_id, score=score, matched=("文档", "解析"))


def test_build_gaps_missing_sample_is_gap_with_neutral_win(tmp_path):
    qcfg = _qcfg(tmp_path)
    gaps = build_gaps([], qcfg, qa_index={}, facts_count=lambda p: 0)
    s01 = next(g for g in gaps if g.query_id == "S01")
    assert s01.win == 3  # 没监测到 = 不知道竞对占没占，取中性分
    assert s01.evidence["sampled"] is False


def test_build_gaps_skips_non_anchorable_categories(tmp_path):
    qcfg = _qcfg(tmp_path)
    samples = [_sample(qid, cited=False) for qid in ("B01", "P01", "X01", "C01")]
    gaps = build_gaps(samples, qcfg, qa_index={}, facts_count=lambda p: 0)
    ids = {g.query_id for g in gaps}
    assert "C01" in ids
    assert not ({"B01", "P01", "X01"} & ids)  # B/P/X 永远不成选题（PRD R2）


def test_build_gaps_value_scoring_is_pinned(tmp_path):
    qcfg = _qcfg(tmp_path)
    gaps = {g.query_id: g for g in build_gaps([], qcfg, qa_index={}, facts_count=lambda p: 0)}
    assert gaps["D01"].value == 5  # 决策(5) + P0 加成已被上限压住
    assert gaps["C02"].value == 4  # 评估(4)，P1 无加成
    assert gaps["S01"].value == 4  # 场景(3) + P0 加成
    assert gaps["S02"].value == 3  # 场景(3)
    for gap in gaps.values():
        assert gap.cost == 1  # 模板成稿边际成本≈0，成本恒为 1（写死的口径）


def test_build_gaps_reach_bucketed_by_facts_count(tmp_path):
    qcfg = _qcfg(tmp_path)
    qa_index = {"S01": _qa_entry("deepparser")}

    def reach_for(n):
        gaps = build_gaps([], qcfg, qa_index=qa_index, facts_count=lambda p: n)
        return next(g for g in gaps if g.query_id == "S01").reach

    assert reach_for(0) == 1
    assert reach_for(1) == 2
    assert reach_for(4) == 3
    assert reach_for(8) == 4
    assert reach_for(12) == 5


def test_build_gaps_win_from_competitor_count(tmp_path):
    qcfg = _qcfg(tmp_path)

    def win_for(competitors):
        gaps = build_gaps([_sample("S01", competitors=competitors)], qcfg, qa_index={}, facts_count=lambda p: 0)
        return next(g for g in gaps if g.query_id == "S01").win

    assert win_for(()) == 5
    assert win_for(("Dify",)) == 4
    assert win_for(("Dify", "Coze", "阿里百炼", "腾讯元器")) == 1
    assert win_for(("a", "b", "c", "d", "e")) == 1  # 封底


def test_build_gaps_multi_engine_any_cited_clears_gap(tmp_path):
    qcfg = _qcfg(tmp_path)
    samples = [
        {"query_id": "S01", "engine": "websearch", "cited": False, "competitors_seen": ["Dify"]},
        {"query_id": "S01", "engine": "future", "cited": True, "competitors_seen": ["Coze"]},
    ]
    gaps = build_gaps(samples, qcfg, qa_index={}, facts_count=lambda p: 0)
    assert "S01" not in {g.query_id for g in gaps}  # 任一引擎引用了就不是缺口


def test_build_gaps_multi_engine_competitors_union(tmp_path):
    qcfg = _qcfg(tmp_path)
    samples = [
        {"query_id": "S01", "engine": "a", "cited": False, "competitors_seen": ["Dify"]},
        {"query_id": "S01", "engine": "b", "cited": False, "competitors_seen": ["Dify", "Coze"]},
    ]
    gaps = build_gaps(samples, qcfg, qa_index={}, facts_count=lambda p: 0)
    gap = next(g for g in gaps if g.query_id == "S01")
    assert gap.evidence["competitors"] == ["Coze", "Dify"]
    assert gap.win == 3  # 去重后 2 家：5 - 2


# ---------------------------------------------------------------- qa.json 映射

def _synthetic_product(data_dir: Path, claims: list[str], product_id: str = "deepparser"):
    """手写 facts.json 的最小产品：qa 测试要断言「什么绝不命中」，语料必须完全可控。"""
    product_dir = data_dir / "products" / product_id
    product_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "facts": [
            {"claim": c, "source": "手册 第1页", "tier": "B", "date": "2026-10-26"}
            for c in claims
        ]
    }
    (product_dir / "facts.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )
    from geopublish.write.pipeline import load_product

    return load_product(product_id, data_dir, _qcfg(data_dir).brand, require_reviewed=False)


def test_build_qa_matrix_matches_with_bigram_evidence(tmp_path):
    qcfg = _qcfg(tmp_path)
    product = _synthetic_product(
        tmp_path,
        ["DeepParser 支持 PDF 表格提取并输出结构化数据", "支持扫描件版面还原与批量处理"],
    )
    rows = build_qa_matrix(product, qcfg)
    by_id = {r["query_id"]: r for r in rows}
    assert "S01" in by_id  # 「表格 / 提取 / 结构化」与事实高度重叠
    assert by_id["S01"]["score"] >= QA_MIN_MATCH
    assert by_id["S01"]["matched"], "必须带命中证据，人工审查要看"
    # 与解析无关的问句（知识库选型）不得出现
    assert "C02" not in by_id


def test_build_qa_matrix_real_fixture_ranks_s01_first(tmp_path):
    """真实 fixture 冒烟：DeepParser 语料下 S01（表格提取）必须高分在榜。"""
    clock = FakeClock(NOW)
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True)
    _product(data_dir, clock)
    from geopublish.write.pipeline import load_product

    product = load_product("deepparser", data_dir, _qcfg(tmp_path).brand)
    rows = build_qa_matrix(product, _qcfg(tmp_path))
    assert rows and rows[0]["query_id"] == "S01"


def test_build_qa_matrix_ignores_non_anchorable_queries(tmp_path):
    clock = FakeClock(NOW)
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True)
    _product(data_dir, clock)
    from geopublish.write.pipeline import load_product

    product = load_product("deepparser", data_dir, _qcfg(tmp_path).brand)
    rows = build_qa_matrix(product, _qcfg(tmp_path))
    ids = {r["query_id"] for r in rows}
    assert "P01" not in ids  # 产品词尽管字字命中也不可锚（R2）
    assert "B01" not in ids
    assert "X01" not in ids


def test_build_qa_matrix_question_words_are_not_evidence(tmp_path):
    from geopublish.select.picker import _bigrams

    grams = _bigrams("怎么选 什么产品 哪家好")
    assert "怎么" not in grams and "什么" not in grams and "哪家" not in grams


def test_write_and_load_qa_roundtrip(tmp_path):
    clock = FakeClock(NOW)
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True)
    _product(data_dir, clock)
    qcfg = _qcfg(tmp_path)
    path = write_qa_matrix("deepparser", data_dir, qcfg, clock=clock)
    assert path.name == "qa.json"
    index = load_qa_index(data_dir / "products", qcfg)
    assert index, "写出去的 qa.json 必须能被选题侧读回来"
    assert all(entry.product_id == "deepparser" for entry in index.values())


def test_load_qa_index_conflict_between_products_rejected(tmp_path):
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True)
    _write_qa(data_dir, "deepparser", ["S01"])
    _write_qa(data_dir, "team-kb", ["S01"])
    with pytest.raises(PickerError, match="多个产品认领"):
        load_qa_index(data_dir / "products", _qcfg(tmp_path))


def test_load_qa_index_product_id_mismatch_rejected(tmp_path):
    data_dir = tmp_path / "data"
    product_dir = data_dir / "products" / "deepparser"
    product_dir.mkdir(parents=True)
    (product_dir / "qa.json").write_text(
        json.dumps({"schema": 1, "product_id": "other", "queries": []}, ensure_ascii=False),
        encoding="utf-8",
    )
    with pytest.raises(PickerError, match="product_id"):
        load_qa_index(data_dir / "products", _qcfg(tmp_path))


def test_load_qa_index_bad_schema_and_bad_json_rejected(tmp_path):
    data_dir = tmp_path / "data"
    product_dir = data_dir / "products" / "deepparser"
    product_dir.mkdir(parents=True)
    (product_dir / "qa.json").write_text("{不是 json", encoding="utf-8")
    with pytest.raises(PickerError, match="不是合法 JSON"):
        load_qa_index(data_dir / "products", _qcfg(tmp_path))
    (product_dir / "qa.json").write_text(
        json.dumps({"schema": 2, "product_id": "deepparser", "queries": []}), encoding="utf-8"
    )
    with pytest.raises(PickerError, match="schema"):
        load_qa_index(data_dir / "products", _qcfg(tmp_path))
    (product_dir / "qa.json").write_text(
        json.dumps({"schema": 1, "product_id": "deepparser", "queries": {"S01": {}}}),
        encoding="utf-8",
    )
    with pytest.raises(PickerError, match="queries 必须是数组"):
        load_qa_index(data_dir / "products", _qcfg(tmp_path))


def test_load_qa_index_empty_products_dir_is_empty_index(tmp_path):
    assert load_qa_index(tmp_path / "products", _qcfg(tmp_path)) == {}
    (tmp_path / "products").mkdir()
    assert load_qa_index(tmp_path / "products", _qcfg(tmp_path)) == {}


# ---------------------------------------------------------------- 留痕（DoD 可回溯）

def test_save_trace_records_gaps_scores_and_decision(tmp_path):
    result, data_dir = _pick(
        tmp_path,
        [_gap("S01", value=4, reach=5, win=4, cost=1)],
        prioritize=_fake_prioritize(["P0 立即做"]),
    )
    trace = save_trace(data_dir, result, clock=FakeClock(NOW))
    assert trace == data_dir / "select" / "2026-10-27.json"
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert payload["status"] == "picked"
    assert payload["platform"] == "csdn"
    assert payload["refresh_days"] == 30
    # DoD 原话：可回溯到当日的缺口输入和优先级打分
    assert payload["gaps"][0]["商业价值"] == 4
    assert payload["gaps"][0]["可达性"] == 5
    assert payload["prioritized"][0]["档位"] == "P0 立即做"
    assert payload["choice"]["query_id"] == "S01"
    assert payload["choice"]["product_id"] == "deepparser"


def test_save_trace_refuses_to_overwrite_same_day(tmp_path):
    result, data_dir = _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0 立即做"]))
    clock = FakeClock(NOW)
    save_trace(data_dir, result, clock=clock)
    with pytest.raises(PickerError, match="已存在"):
        save_trace(data_dir, result, clock=clock)


# ---------------------------------------------------------------- 顺序与防御

def test_pick_preserves_prioritize_order_no_resort(tmp_path):
    # 脚本把低分的 S01 排在高分的 D01 前面（脚本的事picker不管）：
    # picker 必须按返回顺序取第一个活下来的，不许自己重排
    def weird_order(received):
        return [
            {"缺口": "S01", "优先级分": 41.0, "档位": "P0 立即做", "象限": "攻坚区", "处理": "x"},
            {"缺口": "D01", "优先级分": 99.0, "档位": "P0 立即做", "象限": "攻坚区", "处理": "x"},
        ]

    result, _ = _pick(
        tmp_path,
        [_gap("S01"), _gap("D01")],
        prioritize=weird_order,
        qa_query_ids=("S01", "D01"),
    )
    assert result.choice.query_id == "S01"


def test_pick_rejects_duplicate_gap_ids(tmp_path):
    with pytest.raises(PickerError, match="重复"):
        _pick(tmp_path, [_gap("S01"), _gap("s01")], prioritize=_fake_prioritize(["P0 立即做"]))


def test_pick_rejects_malformed_prioritizer_output(tmp_path):
    def broken(received):
        return [{"缺口": "S01"}]  # 缺 档位

    with pytest.raises(PickerError, match="档位"):
        _pick(tmp_path, [_gap("S01")], prioritize=broken)


def test_pick_rejects_non_anchorable_gap_even_if_prioritizer_likes_it(tmp_path):
    # B01 不该出现在缺口清单里；真出现了（有人手工喂）也绝不选品牌词
    result, _ = _pick(
        tmp_path,
        [_gap("B01")],
        prioritize=_fake_prioritize(["P0 立即做"]),
        qa_query_ids=("B01",),
    )
    assert result.status == "no_topic"
    assert result.exclusions[0].stage == "category"


def test_gap_score_out_of_range_rejected():
    with pytest.raises(PickerError, match="1..5"):
        _gap("S01", value=0)
    with pytest.raises(PickerError, match="1..5"):
        _gap("S01", cost=6)
    with pytest.raises(PickerError, match="不能为空"):
        Gap(query_id="  ", value=5, reach=5, win=5, cost=1)


def test_build_gaps_skips_sample_rows_without_query_id(tmp_path):
    qcfg = _qcfg(tmp_path)
    samples = [{"cited": False}, {"query_id": "  ", "cited": False}]
    # 无 query_id 的样本行被跳过，不炸也不捏造缺口
    ids = {g.query_id for g in build_gaps(samples, qcfg, qa_index={}, facts_count=lambda p: 0)}
    assert "S01" in ids and "" not in ids


def test_write_qa_matrix_leaves_no_temp_file_on_replace_failure(tmp_path, monkeypatch):
    clock = FakeClock(NOW)
    data_dir = tmp_path / "data"
    (data_dir / "products").mkdir(parents=True)
    _product(data_dir, clock)
    qcfg = _qcfg(tmp_path)

    import os

    import geopublish.select.picker as picker_mod

    monkeypatch.setattr(os, "replace", _raise_oserror)
    with pytest.raises(OSError):
        write_qa_matrix("deepparser", data_dir, qcfg, clock=clock)
    assert not list((data_dir / "products" / "deepparser").glob(".qa.*.tmp"))
    monkeypatch.undo()
    # 恢复正常后同一调用必须能成功（临时文件没把目录搞脏）
    assert picker_mod.write_qa_matrix("deepparser", data_dir, qcfg, clock=clock).exists()


def _raise_oserror(*args, **kwargs):
    raise OSError("磁盘满了（模拟）")


def test_save_trace_leaves_no_temp_file_on_replace_failure(tmp_path, monkeypatch):
    import os

    result, data_dir = _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0 立即做"]))
    monkeypatch.setattr(os, "replace", _raise_oserror)
    with pytest.raises(OSError):
        save_trace(data_dir, result, clock=FakeClock(NOW))
    assert not list((data_dir / "select").glob(".select.*.tmp"))
    assert not list((data_dir / "select").glob("*.json"))  # 留痕本身也没落下半成品


def test_pick_rejects_bad_refresh_days_and_blank_platform(tmp_path):
    with pytest.raises(PickerError, match="refresh_days"):
        _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0 立即做"]), refresh_days=0)
    with pytest.raises(PickerError, match="platform"):
        _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0 立即做"]), platform=" ")


# ---------------------------------------------------------------- 装载器

def test_load_prioritizer_missing_file(tmp_path):
    with pytest.raises(PickerError, match="gap_prioritizer"):
        load_prioritizer(tmp_path / "nope.py")


def test_load_prioritizer_without_prioritize_attr(tmp_path):
    fake = tmp_path / "fake.py"
    fake.write_text("X = 1\n", encoding="utf-8")
    with pytest.raises(PickerError, match="prioritize"):
        load_prioritizer(fake)


def test_load_prioritizer_broken_script(tmp_path):
    fake = tmp_path / "broken.py"
    fake.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
    with pytest.raises(PickerError, match="加载失败"):
        load_prioritizer(fake)


def test_latest_samples_path_picks_newest_run(tmp_path):
    runs = tmp_path / "runs"
    (runs / "2026-10-20").mkdir(parents=True)
    (runs / "2026-10-26").mkdir(parents=True)
    (runs / "2026-10-20" / "samples.json").write_text("[]", encoding="utf-8")
    (runs / "2026-10-26" / "samples.json").write_text("[]", encoding="utf-8")
    assert latest_samples_path(runs) == runs / "2026-10-26" / "samples.json"


def test_latest_samples_path_missing_raises(tmp_path):
    with pytest.raises(PickerError, match="samples.json"):
        latest_samples_path(tmp_path / "runs")


def test_load_samples_validates_shape(tmp_path):
    path = tmp_path / "samples.json"
    path.write_text(json.dumps({"not": "a list"}), encoding="utf-8")
    with pytest.raises(PickerError, match="数组"):
        load_samples(path)
    path.write_text("[{]".replace("{]", "{]"), encoding="utf-8")  # 非法 JSON
    with pytest.raises(PickerError, match="不是合法 JSON"):
        load_samples(path)
    path.write_text(json.dumps([{"query_id": "S01"}]), encoding="utf-8")
    assert load_samples(path) == [{"query_id": "S01"}]


# ---------------------------------------------------------------- T-18b 一次多选（count）

def test_t18b_count_two_picks_top_two_in_priority_order(tmp_path):
    """count=2：按 prioritize 返回顺序取前两个活候选，choice == choices[0]（兼容单选）。"""
    prio = _fake_prioritize(["P0", "P0", "P2"])
    result, _ = _pick(
        tmp_path,
        [_gap("S01"), _gap("C01"), _gap("D01")],
        prioritize=prio,
        qa_query_ids=("S01", "C01", "D01"),
        count=2,
    )
    assert result.status == "picked"
    assert [c.query_id for c in result.choices] == ["S01", "C01"]
    assert result.choice is result.choices[0], "旧消费方读的 choice 必须还是首选"


def test_t18b_count_larger_than_alive_degrades(tmp_path):
    """活候选不足 N 个：有几个选几个，不硬凑。"""
    prio = _fake_prioritize(["P0", "P2"])
    result, _ = _pick(
        tmp_path, [_gap("S01"), _gap("C01")], prioritize=prio,
        qa_query_ids=("S01", "C01"), count=5,
    )
    assert [c.query_id for c in result.choices] == ["S01"]


def test_t18b_count_below_one_rejected(tmp_path):
    with pytest.raises(PickerError, match="count"):
        _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0"]), count=0)


def test_t18b_trace_records_all_choices(tmp_path):
    """留痕：choices 全量在，choice 保留给旧消费方。"""
    prio = _fake_prioritize(["P0", "P0"])
    result, data_dir = _pick(
        tmp_path, [_gap("S01"), _gap("C01")], prioritize=prio,
        qa_query_ids=("S01", "C01"), count=2,
    )
    trace = save_trace(data_dir, result)
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert [c["query_id"] for c in payload["choices"]] == ["S01", "C01"]
    assert payload["choice"]["query_id"] == "S01"


# ---------------------------------------------------------------- T-18b 一次多选（count）

def test_t18b_count_two_picks_top_two_in_priority_order(tmp_path):
    """count=2：按 prioritize 返回顺序取前两个活候选，choice == choices[0]（兼容单选）。"""
    prio = _fake_prioritize(["P0", "P0", "P2"])
    result, _ = _pick(
        tmp_path,
        [_gap("S01"), _gap("C01"), _gap("D01")],
        prioritize=prio,
        qa_query_ids=("S01", "C01", "D01"),
        count=2,
    )
    assert result.status == "picked"
    assert [c.query_id for c in result.choices] == ["S01", "C01"]
    assert result.choice is result.choices[0], "旧消费方读的 choice 必须还是首选"


def test_t18b_count_larger_than_alive_degrades(tmp_path):
    """活候选不足 N 个：有几个选几个，不硬凑。"""
    prio = _fake_prioritize(["P0", "P2"])
    result, _ = _pick(
        tmp_path, [_gap("S01"), _gap("C01")], prioritize=prio,
        qa_query_ids=("S01", "C01"), count=5,
    )
    assert [c.query_id for c in result.choices] == ["S01"]


def test_t18b_count_below_one_rejected(tmp_path):
    with pytest.raises(PickerError, match="count"):
        _pick(tmp_path, [_gap("S01")], prioritize=_fake_prioritize(["P0"]), count=0)


def test_t18b_trace_records_all_choices(tmp_path):
    """留痕：choices 全量在，choice 保留给旧消费方。"""
    prio = _fake_prioritize(["P0", "P0"])
    result, data_dir = _pick(
        tmp_path, [_gap("S01"), _gap("C01")], prioritize=prio,
        qa_query_ids=("S01", "C01"), count=2,
    )
    trace = save_trace(data_dir, result)
    payload = json.loads(trace.read_text(encoding="utf-8"))
    assert [c["query_id"] for c in payload["choices"]] == ["S01", "C01"]
    assert payload["choice"]["query_id"] == "S01"
