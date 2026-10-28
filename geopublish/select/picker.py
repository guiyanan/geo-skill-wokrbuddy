"""T-17 选题串联 geo-master（PRD R17）。

每天回答一个问题：**今天写哪一篇？**

流水线（全部确定性，无 LLM）：

1. **组装缺口清单** `build_gaps`：geo-monitor 最新一期 samples.json 里
   「品牌未被引用」的 C/D/S 类问句 = 缺口。B/P/X 类永远不进清单
   （R2：品牌词 SOV 已 83% 无增量，竞品词不锚）。
2. **排序** 只能交给 geo-master 的 `gap_prioritizer.prioritize()`
   （PRD §9.3 红线：只能消费它的输出，不得自建排序）。本模块通过
   importlib 按路径加载，与 geo-monitor/analyze.py 同一做法——不复制代码。
   prioritize 的输出行只保留「缺口/优先级分/档位/象限/处理」，自定义字段
   会被丢弃，所以缺口名直接用问句 id，回溯不靠解析文本。
3. **过滤**：只取 P0/P1 档；「同问句 × 同平台」refresh_days 天内已发
   （publish-log.csv 里 status=published）的排除；映射不到已抽检产品库
   事实的排除；问句 id 必须全部来自 joto.json（不自建问句）。
4. **留痕** `save_trace`：gaps 打分输入 + prioritize 原始输出 + 逐条排除
   原因 + 最终决策写进 `data/select/<date>.json`（DoD：每日选题可回溯）。
   同日已有留痕则拒绝覆盖——定时任务静默换选题比不选题更难查。

qa.json（R15 的「问句映射」，T-13 推到这里落地）：
`write_qa_matrix` 用字符 bigram 重叠做启发式预填，产出
`data/products/<id>/qa.json`，每条映射带 matched 证据。**它是给人改的**
——生成完人工过一遍再交给 pick 用；选题侧只认文件里的 query_id 列表。
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from ..core.clock import Clock, SystemClock
from ..core.state import PublishLog, Status
from ..write.pipeline import ANCHORABLE_CATEGORIES, PipelineError, QueryConfig, load_product

__all__ = [
    "DEFAULT_GAP_PRIORITIZER_PATH",
    "DEFAULT_MONITOR_RUNS_DIR",
    "QA_MIN_MATCH",
    "Gap",
    "Exclusion",
    "PickResult",
    "PickerError",
    "QaEntry",
    "TopicChoice",
    "build_gaps",
    "build_qa_matrix",
    "latest_samples_path",
    "load_prioritizer",
    "load_qa_index",
    "load_samples",
    "pick_topic",
    "save_trace",
    "write_qa_matrix",
]

DEFAULT_GAP_PRIORITIZER_PATH = (
    Path.home() / ".workbuddy" / "skills" / "geo-master" / "scripts" / "gap_prioritizer.py"
)
DEFAULT_MONITOR_RUNS_DIR = Path.home() / ".workbuddy" / "skills" / "geo-monitor" / "data" / "runs"

# qa.json 预填的入选门槛：至少这么多个有信息量的 bigram 重叠才算「能映射」。
# 定 2 是因为单个重叠（如「企业」）在技术问句与任意企业产品间几乎必然发生。
QA_MIN_MATCH = 2

# 缺口四维打分的固定口径（写死，不进配置——打分口径漂移会让跨日选题不可比）：
_VALUE_BY_INTENT = {"决策": 5, "评估": 4, "场景": 3}
_GAP_COST = 1          # 模板成稿边际成本 ≈ 0
_WIN_NO_SAMPLE = 3     # 没监测到 = 不知道竞对占位，取中性分

# 疑问短语与平台泛词不是「产品能回答这个问句」的证据
_STOP_BIGRAMS = frozenset({"怎么", "什么", "哪些", "哪里", "哪家", "如何", "多少", "为什", "AI"})
# bigram 只在「连续文字段」内取，标点/空白/数字是断点（否则「。上海」这种跨界对会假命中）
_TOKEN_RE = re.compile(r"[A-Za-z0-9一-鿿]+")


class PickerError(ValueError):
    """选题层的所有非法输入：映射冲突、自建问句、留痕覆盖、装载失败等。"""


# ---------------------------------------------------------------- 数据结构

@dataclass(frozen=True, slots=True)
class Gap:
    """一条缺口：问句 id + 四维打分（1-5）+ 打分依据（留痕用）。

    分数构造即校验：geo-master 的口径就是 1-5，越界的分数只会让
    「档位与脚本一致」变成一句没法核对的空话。
    """

    query_id: str
    value: int
    reach: int
    win: int
    cost: int
    evidence: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not str(self.query_id).strip():
            raise PickerError("gap.query_id 不能为空")
        for name in ("value", "reach", "win", "cost"):
            score = getattr(self, name)
            if isinstance(score, bool) or not isinstance(score, int) or not 1 <= score <= 5:
                raise PickerError(f"gap.{name} 必须是 1..5 的整数，收到 {score!r}")

    def to_row(self) -> dict[str, Any]:
        """gap_prioritizer 的输入格式。缺口名 = 问句 id：脚本输出会丢弃
        自定义字段，只能靠「缺口」列回溯到问句。"""
        return {
            "缺口": self.query_id.strip().upper(),
            "商业价值": self.value,
            "可达性": self.reach,
            "可赢性": self.win,
            "成本": self.cost,
        }


@dataclass(frozen=True, slots=True)
class QaEntry:
    """qa.json 里的一条映射：这个问句由哪个产品支撑。"""

    product_id: str
    score: int
    matched: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class TopicChoice:
    """最终选题。`gap` 是这条选题的四维打分输入（DoD：可回溯）。"""

    query_id: str
    query_text: str
    product_id: str
    tier: str
    score: float
    quadrant: str
    gap: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Exclusion:
    """一条候选被排除的记录。no_topic 的日报就靠它写原因。"""

    query_id: str
    stage: str
    reason: str


@dataclass(frozen=True, slots=True)
class PickResult:
    status: str  # "picked" | "no_topic"
    platform: str
    refresh_days: int
    choice: TopicChoice | None
    gaps: tuple[Gap, ...]
    prioritized: tuple[dict[str, Any], ...]
    exclusions: tuple[Exclusion, ...]
    # T-18b：一天多发时选前 N 个活候选；choice == choices[0]（兼容单选调用方）
    choices: tuple[TopicChoice, ...] = ()


# ---------------------------------------------------------------- geo-master 装载

def load_prioritizer(path: str | Path | None = None) -> Callable[[list[dict]], list[dict]]:
    """按路径加载 geo-master 的 gap_prioritizer.prioritize（不复制代码）。"""
    p = Path(path) if path else DEFAULT_GAP_PRIORITIZER_PATH
    if not p.is_file():
        raise PickerError(
            f"geo-master 的 gap_prioritizer.py 不在：{p}"
            "（PRD §9.3：排序只能消费它的输出；请安装 geo-master 或用 --prioritizer 指定）"
        )
    spec = importlib.util.spec_from_file_location("geo_master_gap_prioritizer", p)
    if spec is None or spec.loader is None:  # pragma: no cover - 防御分支
        raise PickerError(f"无法为 {p} 构造模块 spec")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise PickerError(f"gap_prioritizer 加载失败：{p}（{exc}）") from exc
    fn = getattr(module, "prioritize", None)
    if not callable(fn):
        raise PickerError(f"{p} 里没有可调用的 prioritize()")
    return fn


# ---------------------------------------------------------------- geo-monitor 样本

def latest_samples_path(runs_dir: str | Path = DEFAULT_MONITOR_RUNS_DIR) -> Path:
    """geo-monitor 最新一期的 samples.json（目录名是 ISO 日期，字典序即时间序）。"""
    runs = Path(runs_dir)
    candidates = (
        sorted(d for d in runs.iterdir() if d.is_dir() and (d / "samples.json").is_file())
        if runs.is_dir()
        else []
    )
    if not candidates:
        raise PickerError(f"geo-monitor 没有任何一期 samples.json：{runs}（先跑监测）")
    return candidates[-1] / "samples.json"


def load_samples(path: str | Path) -> list[dict[str, Any]]:
    p = Path(path)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise PickerError(f"监测样本不存在：{p}") from exc
    except json.JSONDecodeError as exc:
        raise PickerError(f"监测样本不是合法 JSON：{p}（第 {exc.lineno} 行）") from exc
    if not isinstance(data, list):
        raise PickerError(f"监测样本必须是 JSON 数组：{p}")
    return data


# ---------------------------------------------------------------- 缺口清单组装

def build_gaps(
    samples: Sequence[Mapping[str, Any]],
    query_config: QueryConfig,
    qa_index: Mapping[str, QaEntry],
    facts_count: Callable[[str], int],
) -> list[Gap]:
    """geo-monitor 缺口 + 产品库问句矩阵 → 缺口清单（四维 1-5 分）。

    - 缺口判定：该问句在**所有**引擎样本里 cited 都是假（任一引擎引用了
      品牌就不算缺口）；从没被监测到的问句也算缺口（winnability 取中性 3）。
    - 只收 C/D/S 类（ANCHORABLE_CATEGORIES）：B/P 品牌词与 X 竞品词不做。
    - 样本里出现 joto.json 之外的 query_id 直接忽略（口径不同源的数据不进选题）。
    """
    by_query: dict[str, list[Mapping[str, Any]]] = {}
    for row in samples:
        qid = str(row.get("query_id") or "").strip().upper()
        if qid:
            by_query.setdefault(qid, []).append(row)

    gaps: list[Gap] = []
    for query in query_config.queries:
        if query.category not in ANCHORABLE_CATEGORIES:
            continue
        rows = by_query.get(query.id.upper(), [])
        if any(bool(r.get("cited")) for r in rows):
            continue  # 已被引用 = 不是缺口

        competitors = sorted(
            {str(c) for r in rows for c in (r.get("competitors_seen") or []) if str(c).strip()}
        )
        entry = qa_index.get(query.id.upper())
        facts = facts_count(entry.product_id) if entry else 0
        gaps.append(
            Gap(
                query_id=query.id,
                value=_value_score(query.intent, query.priority),
                reach=_reach_score(facts, mapped=entry is not None),
                win=_win_score(rows, competitors),
                cost=_GAP_COST,
                evidence={
                    "intent": query.intent,
                    "priority": query.priority,
                    "sampled": bool(rows),
                    "cited": False,
                    "competitors": competitors,
                    "mapped_product": entry.product_id if entry else None,
                    "facts": facts,
                },
            )
        )
    return gaps


def _value_score(intent: str, priority: str) -> int:
    base = _VALUE_BY_INTENT.get(intent, 3)
    return min(5, base + (1 if priority.strip().upper() == "P0" else 0))


def _reach_score(facts: int, *, mapped: bool) -> int:
    if not mapped or facts <= 0:
        return 1
    for floor, score in ((12, 5), (8, 4), (4, 3), (1, 2)):
        if facts >= floor:
            return score
    raise AssertionError("unreachable")  # pragma: no cover - facts>0 必命中上面某一档


def _win_score(rows: Sequence[Mapping[str, Any]], competitors: Sequence[str]) -> int:
    if not rows:
        return _WIN_NO_SAMPLE
    return max(1, 5 - len(competitors))


# ---------------------------------------------------------------- qa.json 问句映射

def _bigrams(text: str) -> set[str]:
    """连续文字段内的字符 bigram 集合，去掉疑问短语（它们不构成映射证据）。"""
    grams: set[str] = set()
    for token in _TOKEN_RE.findall(text):
        grams |= {token[i : i + 2] for i in range(len(token) - 1)}
    return grams - _STOP_BIGRAMS


def build_qa_matrix(product, query_config: QueryConfig) -> list[dict[str, Any]]:
    """启发式预填「这个产品能锚定哪些问句」：问句与产品语料（产品名 + 全部
    事实 claim）的 bigram 重叠 ≥ QA_MIN_MATCH 才入选，命中词全留作证据。

    注意：这是**预填稿**，产出后由人增删——picker 只认文件，不认这个函数。
    """
    corpus = product.display_name + "\n" + "\n".join(f.claim for f in product.facts)
    corpus_grams = _bigrams(corpus)
    rows: list[dict[str, Any]] = []
    for query in query_config.queries:
        if query.category not in ANCHORABLE_CATEGORIES:
            continue
        matched = sorted(_bigrams(query.text) & corpus_grams)
        if len(matched) >= QA_MIN_MATCH:
            rows.append({"query_id": query.id, "score": len(matched), "matched": matched})
    rows.sort(key=lambda r: (-r["score"], r["query_id"]))
    return rows


def write_qa_matrix(
    product_id: str,
    data_root: str | Path,
    query_config: QueryConfig,
    *,
    clock: Clock | None = None,
) -> Path:
    """生成并落盘 data/products/<id>/qa.json。允许重跑覆盖（新事实入库后
    映射要重算），但走临时文件 + os.replace，盘上不留写一半的 qa.json。"""
    clock = clock or SystemClock()
    product = load_product(product_id, data_root, query_config.brand, require_reviewed=False)
    rows = build_qa_matrix(product, query_config)
    payload = {
        "schema": 1,
        "product_id": product.product_id,
        "generated_at": clock.now().isoformat(),
        "_note": "启发式预填（bigram 重叠），请人工增删后交给 pick 使用；"
        "query_id 必须来自 joto.json，选题侧会拒收表外问句",
        "queries": rows,
    }
    target = product.product_dir / "qa.json"
    fd, tmp = tempfile.mkstemp(dir=product.product_dir, prefix=".qa.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


def load_qa_index(products_dir: str | Path, query_config: QueryConfig) -> dict[str, QaEntry]:
    """扫描 products/*/qa.json 建 问句→产品 反向索引。

    两条硬校验（都在入口炸，不留到选题时）：
    - qa.json 引用了 joto.json 之外的问句 → 拒（UT-17-06：不许自建问句）
    - 同一问句被两个产品认领 → 拒（歧义必须人来解，不许随便挑一个）
    """
    root = Path(products_dir)
    index: dict[str, QaEntry] = {}
    if not root.is_dir():
        return index
    for qa_path in sorted(root.glob("*/qa.json")):
        product_dir = qa_path.parent
        try:
            data = json.loads(qa_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise PickerError(f"{qa_path} 不是合法 JSON（第 {exc.lineno} 行）") from exc
        if data.get("schema") != 1:
            raise PickerError(f"{qa_path} 的 schema 必须是 1，收到 {data.get('schema')!r}")
        pid = str(data.get("product_id") or "").strip()
        if pid != product_dir.name:
            raise PickerError(
                f"{qa_path} 的 product_id（{pid!r}）与目录名（{product_dir.name!r}）不一致"
            )
        queries = data.get("queries")
        if not isinstance(queries, list):
            raise PickerError(f"{qa_path} 的 queries 必须是数组")
        for item in queries:
            qid = str(item.get("query_id") or "").strip().upper()
            if query_config.query(qid) is None:
                raise PickerError(
                    f"{qa_path} 引用了 joto.json 之外的问句 {qid!r}：不许自建问句（UT-17-06）"
                )
            if qid in index:
                raise PickerError(
                    f"问句 {qid} 被多个产品认领：{index[qid].product_id} 与 {pid}，请人工裁决"
                )
            matched = item.get("matched") or []
            index[qid] = QaEntry(
                product_id=pid,
                score=int(item.get("score") or 0),
                matched=tuple(str(m) for m in matched),
            )
    return index


# ---------------------------------------------------------------- 选题主流程

def pick_topic(
    gaps: Sequence[Gap],
    *,
    query_config: QueryConfig,
    qa_index: Mapping[str, QaEntry],
    log: PublishLog,
    platform: str,
    refresh_days: int,
    data_root: str | Path,
    prioritize: Callable[[list[dict]], list[dict]] | None = None,
    clock: Clock | None = None,
    count: int = 1,
) -> PickResult:
    """缺口清单 → 今日选题（或 no_topic）。

    排序只调一次 prioritize 且**不重排**：返回顺序就是优先级顺序，
    前 count 个活下来的候选就是答案（PRD §9.3：不得自建排序）。
    count=1 是 T-17 的单选行为；>1（T-18b 一天多发）按同一顺序取前 N 个。
    """
    clock = clock or SystemClock()
    platform = str(platform).strip().lower()
    if not platform:
        raise PickerError("platform 不能为空")
    if refresh_days < 1:
        raise PickerError(f"refresh_days 必须 ≥ 1，收到 {refresh_days}")
    if count < 1:
        raise PickerError(f"count 必须 ≥ 1，收到 {count}")

    seen: set[str] = set()
    for gap in gaps:
        qid = gap.query_id.strip().upper()
        if qid in seen:
            raise PickerError(f"缺口清单里问句重复：{qid}")
        seen.add(qid)
    gap_by_id = {g.query_id.strip().upper(): g for g in gaps}

    prioritize = prioritize or load_prioritizer()
    rows = prioritize([g.to_row() for g in gaps])

    exclusions: list[Exclusion] = []
    choices: list[TopicChoice] = []
    for row in rows:
        qid = str(row.get("缺口") or "").strip().upper()
        tier = str(row.get("档位") or "").strip()
        if not tier:
            raise PickerError(f"prioritize 输出缺「档位」字段：{row!r}")

        if not (tier.startswith("P0") or tier.startswith("P1")):
            exclusions.append(Exclusion(qid, "tier", f"档位 {tier}：只写 P0/P1 档的选题（R17）"))
            continue

        query = query_config.query(qid)
        if query is None:
            exclusions.append(
                Exclusion(qid, "unknown_query", f"问句 {qid} 不在 joto.json 里：不自建问句（UT-17-06）")
            )
            continue
        if query.category not in ANCHORABLE_CATEGORIES:
            exclusions.append(
                Exclusion(qid, "category", f"{qid} 是 {query.category} 类：品牌/产品/竞品词不锚（R2）")
            )
            continue
        published_at = _recently_published(log, platform, qid, refresh_days, clock)
        if published_at is not None:
            exclusions.append(
                Exclusion(
                    qid,
                    "recently_published",
                    f"同问句×{platform} 在 {refresh_days} 天内已发（{published_at}），翻新周期未到",
                )
            )
            continue
        entry = qa_index.get(qid)
        if entry is None:
            exclusions.append(
                Exclusion(qid, "no_mapping", "产品库没有产品认领这个问句（qa.json 无映射）")
            )
            continue
        try:
            load_product(entry.product_id, data_root, query_config.brand)
        except PipelineError as exc:
            exclusions.append(Exclusion(qid, "product_unusable", f"产品 {entry.product_id} 不可用：{exc}"))
            continue

        gap = gap_by_id[qid]
        choices.append(
            TopicChoice(
                query_id=query.id,
                query_text=query.text,
                product_id=entry.product_id,
                tier=tier,
                score=float(row.get("优先级分") or 0),
                quadrant=str(row.get("象限") or ""),
                gap=gap.to_row() | {"evidence": dict(gap.evidence)},
            )
        )
        if len(choices) >= count:
            break

    return PickResult(
        status="picked" if choices else "no_topic",
        platform=platform,
        refresh_days=refresh_days,
        choice=choices[0] if choices else None,
        gaps=tuple(gaps),
        prioritized=tuple(dict(r) for r in rows),
        exclusions=tuple(exclusions),
        choices=tuple(choices),
    )


def _recently_published(
    log: PublishLog, platform: str, query_id: str, refresh_days: int, clock: Clock
) -> str | None:
    """同问句×同平台在 refresh_days 天内已发 → 返回那次发布的时间串，否则 None。

    只认 published 行：failed/rejected 的稿子没碰平台，不占翻新窗口。
    时间口径：age < refresh_days 天算「内」，恰好满 refresh_days 天放行。
    slug 约定是 <product_id>-<query_id 小写>（T-14 定的），按后缀匹配。
    """
    suffix = f"-{query_id.lower()}"
    window = refresh_days * 86400
    now = clock.now()
    for entry in log.entries():
        if entry.status is not Status.PUBLISHED:
            continue
        if entry.platform.strip().lower() != platform:
            continue
        if not entry.slug.strip().lower().endswith(suffix):
            continue
        if 0 <= (now - entry.ts).total_seconds() < window:
            return entry.ts.isoformat()
    return None


# ---------------------------------------------------------------- 留痕

def save_trace(data_root: str | Path, result: PickResult, *, clock: Clock | None = None) -> Path:
    """选题留痕：data/select/<date>.json。同日已存在 → 拒（防定时任务静默换选题）。"""
    clock = clock or SystemClock()
    day = clock.now().date().isoformat()
    target = Path(data_root) / "select" / f"{day}.json"
    if target.exists():
        raise PickerError(f"今日选题留痕已存在：{target}（同日重选请先人工删除）")
    payload = {
        "schema": 1,
        "date": day,
        "generated_at": clock.now().isoformat(),
        "platform": result.platform,
        "refresh_days": result.refresh_days,
        "status": result.status,
        "choice": None
        if result.choice is None
        else {
            "query_id": result.choice.query_id,
            "query_text": result.choice.query_text,
            "product_id": result.choice.product_id,
            "tier": result.choice.tier,
            "score": result.choice.score,
            "quadrant": result.choice.quadrant,
            "gap": result.choice.gap,
        },
        # T-18b：一天多发时这里是全部选中项（含第一个），choice 保留是给旧消费者
        "choices": [
            {
                "query_id": c.query_id,
                "query_text": c.query_text,
                "product_id": c.product_id,
                "tier": c.tier,
                "score": c.score,
                "quadrant": c.quadrant,
                "gap": c.gap,
            }
            for c in result.choices
        ],
        "gaps": [g.to_row() | {"evidence": dict(g.evidence)} for g in result.gaps],
        "prioritized": list(result.prioritized),
        "exclusions": [
            {"query_id": e.query_id, "stage": e.stage, "reason": e.reason}
            for e in result.exclusions
        ],
    }
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".select.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target
