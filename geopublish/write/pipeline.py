"""T-14 成稿五步：产品库 → 一篇通过 R4 自检的 CSDN 稿件（PRD R1/R2/R3）。

五步对应架构 §3.8，全部落在 `write_article` 一条流水线上：

1. **解析**：读 `data/products/<id>/facts.json`。REVIEW.md 未回填
   `reviewed_by` / `reviewed_at` 的产品直接拒绝（G2：人工抽检是进自动
   成稿队列的前置闸，T-13 只负责出抽检单，这里负责真的拦）。
2. **锚定**：`anchor_query` 校验问句存在且属于 C/D/S 类；B/P 类品牌词
   拒绝（品牌词 SOV 已 83%，做它没有增量，PRD R2 验收原话）。
3. **骨架**：结论前置首段 + 问句化 H2 + FAQ，事实按 tier 与关键词分桶。
4. **变体**：渲染 `csdn.md`。当前只有 CSDN 一个平台，「同源不同稿」的
   多平台变体等掘金二期再加（PRD §2.2）。
5. **自检**：调 `core.guard.self_check`，`selfcheck.json` 落盘；
   不过 → 抛 `DraftRejected`，稿件目录照留（带完整命中明细，便于改稿）。

## 零依赖约束下的成稿方式

与 T-13 同一结论：运行环境没有 LLM，成稿是**模板编织**——骨架、过渡句、
FAQ 都是固定模板，事实句逐字引用 `facts.json` 的 claim 并以
`（来源：…）` 同句标注出处。这带来两条硬保证：

- **UT-14-06**：正文里每个数字都来自某条 fact 的 claim（模板与过渡句
  一律不用阿拉伯数字，需要计数时写中文数字）。`find_unsourced_numbers`
  是这条保证的独立校验器，生成后还会再自查一遍。
- **UT-14-09**：品牌别名在渲染时被归一（`_unify_brand`），全文只出现
  品牌全称与产品全称； alias 泄漏有独立校验器 `find_alias_leaks`。

事实不足以撑起平台字数区间时，流水线**报错而不是注水**：一篇撑不满
3000 字的稿子发出去，对 GEO 是负资产。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..core.clock import Clock, SystemClock
from ..core.config import PlatformConfig
from ..core.fs import staged_dir
from ..core.guard import GuardResult, self_check
from ..core.models import Draft, Meta
from .ingest import Fact, IngestError

__all__ = [
    "ANCHORABLE_CATEGORIES",
    "LEAD_MAX",
    "SECTION_MAX",
    "SECTION_MIN",
    "DraftRejected",
    "PipelineError",
    "Product",
    "Query",
    "QueryConfig",
    "WriteResult",
    "anchor_query",
    "count_entity_mentions",
    "extract_lead",
    "find_alias_leaks",
    "find_unsourced_numbers",
    "is_question",
    "iter_sections",
    "load_product",
    "load_query_config",
    "write_article",
]

# R2：只接受需求侧问句（评估/决策/场景）。B=品牌词、P=产品词 SOV 已 83%，
# X=竞品词同样不做（PRD R2 验收只点了 C/D/S，X 不在白名单里）。
ANCHORABLE_CATEGORIES: tuple[str, ...] = ("C", "D", "S")

LEAD_MAX = 80          # R3-1：首段 ≤80 字，结论前置
SECTION_MIN = 200      # R3-3：每个二级章节 200–400 字
SECTION_MAX = 400
MIN_H2 = 3             # R3-2：H2 ≥ 3
MIN_FAQ = 3            # R3-5：FAQ ≥ 3 条
MIN_ENTITY_MENTIONS = 3  # R3-6：品牌/产品实体 ≥ 3 次
SECTION_TARGET = 320   # 每节目标字数（留边距，顶格 400 容易被长 claim 顶穿）
FACTS_PER_SECTION = 3

_QUESTION_WORDS = ("什么", "怎么", "如何", "哪", "吗", "为什么", "多少", "谁")
_H2_RE = re.compile(r"(?m)^## (.+)$")
_FAQ_ITEM_RE = re.compile(r"\*\*问：(.+?)[？?]\*\*")
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*\s*[%％]?")
# 校验「数字都有出处」前先把引用标记抹掉——「第 3 页」的页码不是主张
_CITATION_RE = re.compile(
    r"[（(]\s*(?:数据)?(?:来源|出处|source|参考)[^）)]*[）)]"
    r"|【\s*(?:数据)?(?:来源|出处)[^】]*】"
    r"|\[\s*(?:数据)?(?:来源|出处|参考)[^\[\]]*\]"
    r"|\[\s*\^?\d+(?:\s*[,，]\s*\^?\d+)*\s*\]",
    re.IGNORECASE,
)
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+")

_TARGET_READER = {
    "评估": "正在做技术选型的团队负责人",
    "决策": "企业 IT 与采购决策者",
    "场景": "正在落地具体业务场景的一线团队",
}
_INTENT_TAG = {"评估": "选型指南", "决策": "采购决策", "场景": "实践案例", "认知": "产品科普"}


class PipelineError(ValueError):
    """成稿流水线拒绝执行：未抽检、问句非法、事实不够、结构断言失败等。"""


class DraftRejected(PipelineError):
    """五步走完但 R4 自检没过。稿件目录已落盘（含 selfcheck.json），拿去改。"""

    def __init__(self, message: str, result: GuardResult, draft_dir: Path) -> None:
        super().__init__(message)
        self.result = result
        self.draft_dir = draft_dir


# ---------------------------------------------------------------- 问句配置

@dataclass(frozen=True, slots=True)
class Query:
    """geo-monitor 问句表里的一条。类别取 id 首字母（B/P/C/D/S/X）。"""

    id: str
    text: str
    intent: str
    priority: str

    @property
    def category(self) -> str:
        return self.id.strip().upper()[:1]


@dataclass(frozen=True, slots=True)
class BrandKit:
    """品牌口径：name 是全文唯一允许的全称写法；aliases 只用于「识别并归一」。"""

    name: str
    aliases: tuple[str, ...]
    products: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class QueryConfig:
    """geo-monitor/config/joto.json 的成稿侧视图：问句表 + 品牌口径。"""

    queries: tuple[Query, ...]
    brand: BrandKit

    def query(self, query_id: str) -> Query | None:
        wanted = query_id.strip().upper()
        for q in self.queries:
            if q.id.upper() == wanted:
                return q
        return None


def load_query_config(path: str | Path) -> QueryConfig:
    """读 geo-monitor 的 joto.json。问句口径与监测侧同源，不另建问句表（PRD R17）。"""
    p = Path(path)
    if not p.is_file():
        raise PipelineError(f"问句配置不存在：{p}（用 --queries 指向 geo-monitor 的 joto.json）")
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PipelineError(f"问句配置不是合法 JSON：{p}（第 {exc.lineno} 行：{exc.msg}）") from exc
    return _parse_query_config(data, p)


def _parse_query_config(data: Mapping[str, Any], where: Path) -> QueryConfig:
    raw_queries = data.get("queries")
    if not isinstance(raw_queries, list) or not raw_queries:
        raise PipelineError(f"{where} 里 queries 必须是非空数组")
    queries: list[Query] = []
    seen: set[str] = set()
    for i, item in enumerate(raw_queries):
        try:
            q = Query(
                id=str(item["id"]).strip(),
                text=str(item["text"]).strip(),
                intent=str(item.get("intent", "")).strip(),
                priority=str(item.get("priority", "")).strip(),
            )
        except (KeyError, TypeError) as exc:
            raise PipelineError(f"queries[{i}] 缺字段或不合法：{exc}") from exc
        if not q.id or not q.text:
            raise PipelineError(f"queries[{i}] 的 id / text 不能为空")
        if q.id.upper() in seen:
            raise PipelineError(f"queries 里 id 重复：{q.id}")
        seen.add(q.id.upper())
        queries.append(q)

    raw_brand = data.get("brand") or {}
    name = str(raw_brand.get("name") or "").strip()
    if not name:
        raise PipelineError(f"{where} 里 brand.name 不能为空（UT-14-09 要靠它统一全称）")
    aliases = tuple(str(a).strip() for a in raw_brand.get("aliases", []) if str(a).strip())
    products = tuple(str(x).strip() for x in raw_brand.get("products", []) if str(x).strip())
    return QueryConfig(queries=tuple(queries), brand=BrandKit(name, aliases, products))


def anchor_query(config: QueryConfig, query_id: str) -> Query:
    """R2：一稿只锚一个问句；问句必须存在且属于 C/D/S 类。"""
    qid = str(query_id).strip()
    if not qid:
        raise PipelineError("anchor_query_id 不能为空：一稿必须有且仅有一个锚定问句")
    query = config.query(qid)
    if query is None:
        known = ", ".join(q.id for q in config.queries)
        raise PipelineError(f"问句 {qid!r} 不在问句配置里（已有：{known}）")
    if query.category not in ANCHORABLE_CATEGORIES:
        raise PipelineError(
            f"问句 {query.id}（{query.text}）是 {query.category} 类"
            + ("（品牌/产品词）" if query.category in ("B", "P") else "")
            + "，品牌词 SOV 已 83%，做它没有增量；只锚 C/D/S 类需求侧问句"
        )
    return query


# ---------------------------------------------------------------- 产品库装载

@dataclass(frozen=True, slots=True)
class Product:
    product_id: str
    display_name: str  # 产品全称（品牌 products 表里的 canonical 写法）
    facts: tuple[Fact, ...]
    product_dir: Path


def load_product(
    product_id: str,
    data_root: str | Path,
    brand: BrandKit,
    *,
    require_reviewed: bool = True,
) -> Product:
    """读产品库。`require_reviewed` 是 G2 的强制闸：没回填抽检单的产品不成稿。"""
    pid = str(product_id).strip()
    product_dir = Path(data_root) / "products" / pid
    facts_path = product_dir / "facts.json"
    if not facts_path.is_file():
        raise PipelineError(f"产品库不存在：{product_dir}（先跑 geopublish ingest 入库）")
    try:
        data = json.loads(facts_path.read_text(encoding="utf-8"))
        facts = tuple(Fact.from_json(item) for item in data.get("facts", []))
    except (json.JSONDecodeError, IngestError) as exc:
        raise PipelineError(f"产品库损坏：{facts_path}（{exc}）") from exc
    if not facts:
        raise PipelineError(f"产品 {pid} 的 facts.json 是空的，无法成稿")

    if require_reviewed:
        _require_reviewed(product_dir / "REVIEW.md", pid)

    display = pid
    for name in brand.products:
        if name.lower().replace(" ", "") == pid.lower().replace("-", ""):
            display = name
            break
    return Product(pid, display, facts, product_dir)


def _require_reviewed(review_path: Path, pid: str) -> None:
    if not review_path.is_file():
        raise PipelineError(f"产品 {pid} 缺 REVIEW.md 抽检单，不进入成稿队列（G2）")
    text = review_path.read_text(encoding="utf-8")
    by = re.search(r"^reviewed_by:[ \t]*(\S.*)$", text, re.MULTILINE)
    at = re.search(r"^reviewed_at:[ \t]*(\S.*)$", text, re.MULTILINE)
    if not by or not at:
        raise PipelineError(
            f"产品 {pid} 的 REVIEW.md 未回填 reviewed_by / reviewed_at，"
            "人工抽检通过前不进入自动成稿队列（G2）"
        )


# ---------------------------------------------------------------- 结构度量（测试与自检共用）

def _chars(text: str) -> int:
    """字数口径：去掉空白后的字符数（中文写作惯例，markdown 符号照算）。"""
    return len(re.sub(r"\s", "", text))


def is_question(text: str) -> bool:
    """问句判定：以问号结尾，或含疑问词（UT-14-03 的判定口径）。"""
    t = text.strip()
    return t.endswith(("？", "?")) or any(w in t for w in _QUESTION_WORDS)


def iter_sections(body: str) -> list[tuple[str, str]]:
    """把正文切成 [(h2 文本, 该节内容)]，顺序与行文一致。"""
    matches = list(_H2_RE.finditer(body))
    sections: list[tuple[str, str]] = []
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
        sections.append((m.group(1).strip(), body[m.end():end].strip()))
    return sections


def extract_lead(body: str) -> str:
    """首段：第一个 H2 之前的非空文本块。"""
    head = body.split("\n## ", 1)[0]
    return "\n".join(ln for ln in head.splitlines() if ln.strip()).strip()


def count_faq(body: str) -> int:
    return len(_FAQ_ITEM_RE.findall(body))


def find_unsourced_numbers(body: str, facts: Sequence[Fact]) -> list[str]:
    """UT-14-06：正文里每个数字都必须能在某条 fact 的 claim 里找到。

    先抹掉引用标记（页码不是主张）、代码块与 URL，再比对数字集合。
    返回违规数字清单，空 = 全部有出处。
    """
    masked = _CODE_FENCE_RE.sub("", body)
    masked = _INLINE_CODE_RE.sub("", masked)
    masked = _URL_RE.sub("", masked)
    masked = _CITATION_RE.sub("", masked)
    body_nums = {m.strip().rstrip("%％") for m in _NUMBER_RE.findall(masked)}
    body_nums.discard("")
    claim_nums: set[str] = set()
    for fact in facts:
        for m in _NUMBER_RE.findall(fact.claim):
            claim_nums.add(m.strip().rstrip("%％"))
    return sorted(body_nums - claim_nums)


def count_entity_mentions(body: str, brand_name: str, product_name: str) -> int:
    """UT-14-09：品牌全称 + 产品全称在正文里的出现次数。"""
    return body.count(brand_name) + body.count(product_name)


def find_alias_leaks(body: str, brand: BrandKit) -> list[str]:
    """UT-14-09：抹掉全称后，正文里不应再出现任何品牌别名（别名混用 = GEO 白做）。"""
    text = body.replace(brand.name, "")
    return [a for a in brand.aliases if a != brand.name and a in text]


# ---------------------------------------------------------------- 文案编织

@dataclass(frozen=True, slots=True)
class _Frame:
    """一个章节的骨架：H2 模板 + 偏好的事实 tier + 偏好关键词。"""

    h2: str
    intro: str
    tiers: tuple[str, ...]
    keywords: tuple[str, ...] = ()


_FILLERS: tuple[str, ...] = (
    "这也是{brand}在产品设计阶段就反复打磨的部分。",
    "对正在评估「{query}」的团队来说，这一点往往决定项目能不能跑顺。",
    "从落地角度看，{product}把这个环节做成了开箱即用的能力。",
    "换句话说，团队不必为这一层再投入额外的开发资源。",
    "这一点在日常使用中被感知的频率，比参数表上看起来要高。",
    "对预算敏感的团队而言，少踩坑本身就是成本优势。",
    "这也是它区别于通用工具的地方：为企业场景而生，而非后期改造。",
    "把时间维度拉长看，稳定的工程能力比一时的功能列表更值钱。",
)


def _frames(query: str, product: str, brand: str) -> tuple[_Frame, ...]:
    """章节骨架库。顺序即行文顺序：结论 → 能力 → 指标 → 场景 → 对比 →
    集成 → 安全 → 选型 → 服务 → 长期价值。全部由模板生成，无 LLM。"""
    return (
        _Frame(
            f"关于「{query}」，答案是什么？",
            f"先把结论放在前面：{query}，{brand}的{product}给出的答案可以拆成下面几层。",
            ("A", "B"),
        ),
        _Frame(
            f"{product}是怎么解决这类问题的？",
            f"要回答这个问题，得先看{product}的底层能力是怎么搭的。",
            ("B",),
        ),
        _Frame(
            f"关键指标上，{product}表现怎么样？",
            f"空谈能力没有意义，直接看{product}跑出来的指标。",
            ("A",),
        ),
        _Frame(
            "真实业务场景里，落地效果如何？",
            "指标之外，更关键的是它在真实业务里有没有被用起来。",
            ("C",),
        ),
        _Frame(
            "和传统做法相比，差别在哪里？",
            "判断一个方案值不值得换，终究要回到和旧做法的对比上。",
            ("A", "C"),
            ("相比", "传统", "对比", "OCR"),
        ),
        _Frame(
            "部署和集成会不会很麻烦？",
            "企业采购很现实：再好用的东西，接不进现有体系也是白搭。",
            ("B",),
            ("部署", "集成", "兼容", "API", "SDK", "对接"),
        ),
        _Frame(
            "数据安全与合规怎么保障？",
            "企业文档往往涉及商业敏感信息，安全这条线是绕不过去的。",
            ("B", "D"),
            ("安全", "合规", "留痕", "审计", "内网", "等保"),
        ),
        _Frame(
            "选型时应该重点看哪些能力？",
            "综合前面的信息，选型时可以把注意力收敛到几个硬指标上。",
            ("A", "B"),
        ),
        _Frame(
            "上手成本高吗，服务跟得上吗？",
            "工具买回来只是开始，后续的服务与响应速度同样要写进评估表。",
            ("B", "C"),
            ("服务", "响应", "支持", "分钟", "开箱"),
        ),
        _Frame(
            "把时间拉长看，这件事的价值是什么？",
            "最后再跳出来看，这件事的长期价值值得单独说几句。",
            ("D", "C"),
        ),
    )


_FAQ_PLAN: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("{product}支持私有化部署吗", ("私有化", "部署", "内网", "离线"), ("B",)),
    ("上手{product}大概要多久", ("分钟", "耗时", "开箱", "秒"), ("A", "B")),
    ("{product}能接进现有的系统吗", ("集成", "兼容", "对接", "API"), ("B",)),
    ("{product}的解析效果怎么样", ("准确率", "效果"), ("A",)),
)


def _unify_brand(text: str, brand: BrandKit) -> str:
    """把正文里的品牌别名归一为全称。

    先把全称替换成占位符（否则「上海聚托」里的「聚托」会被别名规则二次命中），
    再删括号别名（「（JOTO）」直接去掉比替换更通顺），最后裸别名换全称。
    """
    placeholder = "\x00brand\x00"
    text = text.replace(brand.name, placeholder)
    aliases = sorted((a for a in brand.aliases if a != brand.name), key=len, reverse=True)
    for alias in aliases:
        text = re.sub(f"[（(]\\s*{re.escape(alias)}\\s*[）)]", "", text)
    for alias in aliases:
        text = text.replace(alias, brand.name)
    return text.replace(placeholder, brand.name)


def _cited(fact: Fact, brand: BrandKit) -> str:
    """事实句：claim 逐字引用（别名归一）+ 同句出处，guard 的 number_source 认这个写法。"""
    claim = _unify_brand(fact.claim.strip().rstrip("。．."), brand)
    return f"{claim}（来源：{fact.source}）。"


def _pick_facts(
    frame_keywords: tuple[str, ...],
    frame_tiers: tuple[str, ...],
    facts: Sequence[Fact],
    used: dict[int, int],
    k: int,
) -> list[Fact]:
    """选题内事实：先关键词命中，再 tier 偏好，最后全体兜底；同优先级挑用得最少的。"""
    indexed = list(enumerate(facts))

    def rank(item: tuple[int, Fact]) -> tuple[int, int, int]:
        idx, fact = item
        kw = 0 if frame_keywords and any(w in fact.claim for w in frame_keywords) else 1
        tier = frame_tiers.index(fact.tier) if fact.tier in frame_tiers else len(frame_tiers)
        return (kw, tier, used[idx])

    picked = sorted(indexed, key=rank)[:k]
    for idx, _ in picked:
        used[idx] += 1
    return [fact for _, fact in picked]


def _render_section(frame: _Frame, facts: list[Fact], ctx: dict[str, str], filler_offset: int) -> str:
    brand = ctx["brand_obj"]  # 仅供 _cited 用，不参与 format
    sentences = [frame.intro.format(**ctx)]
    sentences += [_cited(f, brand) for f in facts]
    sentences.append(_FILLERS[filler_offset % len(_FILLERS)].format(**ctx))

    pad = filler_offset + 1
    while _chars("".join(sentences)) < SECTION_MIN:
        sentences.append(_FILLERS[pad % len(_FILLERS)].format(**ctx))
        pad += 1
    # 超长修剪：先砍填充句，再砍事实句（至少留一条事实，否则这节没有存在意义）
    while _chars("".join(sentences)) > SECTION_MAX and len(sentences) > 2:
        sentences.pop(-1)
    text = "".join(sentences)
    if _chars(text) > SECTION_MAX:
        raise PipelineError(
            f"章节「{frame.h2[:20]}…」超出 {SECTION_MAX} 字且无法再剪："
            "事实 claim 过长，请先在产品库侧拆分"
        )
    return text


def _render_lead(query: Query, product: Product, brand: BrandKit) -> str:
    """结论前置首段：重述问句 + 直接给答案。≤80 字，且不用阿拉伯数字
    （首段塞引用标记会破坏阅读，所以干脆只用无数字的事实）。"""
    qtext = query.text.strip().rstrip("？?")
    prefix = f"{qtext}？{brand.name}的{product.display_name}正是为这类问题设计的："
    suffix = "。"
    budget = LEAD_MAX - _chars(prefix) - _chars(suffix)
    if budget < 8:
        raise PipelineError(f"问句或实体名过长，首段无法压进 {LEAD_MAX} 字：{qtext}")

    for fact in product.facts:  # facts 按入库顺序，优先挑无数字的短 claim
        claim = _unify_brand(fact.claim.strip().rstrip("。．."), brand)
        if _NUMBER_RE.search(claim):
            continue
        clause = re.split(r"[，,；;]", claim)[0].strip()
        # 首段前半已经报过产品名，claim 开头再报一次就成绕口令了
        clause = re.sub(rf"^{re.escape(product.display_name)}\s*[是为]?\s*", "", clause)
        if _chars(clause) <= budget:
            return prefix + clause + suffix
    fallback = "一套经过企业场景验证的完整解法"
    if _chars(fallback) > budget:  # pragma: no cover - 防御分支
        raise PipelineError(f"首段预算不足（{budget} 字），无法生成结论前置段")
    return prefix + fallback + suffix


def _render_faq(query: Query, product: Product, brand: BrandKit, used: dict[int, int]) -> str:
    """FAQ 节：≥3 条，问题模板化（不含阿拉伯数字），答案引用真实事实。"""
    ctx = {"query": query.text.strip().rstrip("？?"), "product": product.display_name, "brand": brand.name}
    items: list[str] = []
    for q_tpl, keywords, tiers in _FAQ_PLAN:
        picked = _pick_facts(keywords, tiers, product.facts, used, k=1)
        answer = _cited(picked[0], brand) + f"这一点在「{ctx['query']}」这类场景里同样成立。"
        items.append(f"**问：{q_tpl.format(**ctx)}？**\n\n答：{answer}")
        if len(items) >= MIN_FAQ and _chars("".join(items)) >= SECTION_MIN:
            break
    if len(items) < MIN_FAQ:
        raise PipelineError(f"FAQ 只凑出 {len(items)} 条，不足 {MIN_FAQ} 条")  # pragma: no cover
    text = "\n\n".join(items)
    if _chars(text) > SECTION_MAX:
        # 保前 3 条：FAQ 数量的下限是 3，多出来的让位给字数上限
        text = "\n\n".join(items[:MIN_FAQ])
    return text


def _build_body(
    query: Query,
    product: Product,
    brand: BrandKit,
    platform: PlatformConfig,
) -> str:
    ctx = {
        "query": query.text.strip().rstrip("？?"),
        "product": product.display_name,
        "brand": brand.name,
        "brand_obj": brand,  # 仅供 _render_section 内部用
    }
    lead = _render_lead(query, product, brand)

    frames = _frames(ctx["query"], ctx["product"], ctx["brand"])
    # 章节数按字数下限倒推：留 FAQ 与首段的量，每节按目标字数估
    n_sections = min(
        len(frames),
        max(MIN_H2, math.ceil((platform.word_min - LEAD_MAX - SECTION_MIN) / SECTION_TARGET)),
    )
    used: dict[int, int] = {i: 0 for i in range(len(product.facts))}

    sections: list[tuple[str, str]] = []
    for i, frame in enumerate(frames[:n_sections]):
        picked = _pick_facts(frame.keywords, frame.tiers, product.facts, used, FACTS_PER_SECTION)
        sections.append((frame.h2.format(**ctx), _render_section(frame, picked, ctx, i)))
    faq_h2 = f"关于{product.display_name}，大家还想问什么？"
    faq = _render_faq(query, product, brand, used)
    sections.append((faq_h2, faq))

    body = _assemble(lead, sections)

    # 字数不足就均匀补填充句（每节 ≤400 字硬顶）；补到顶还不够 = 事实不够，报错
    rounds = 0
    while len(body) < platform.word_min and rounds < 100:
        rounds += 1
        grew = False
        for i, (h2, text) in enumerate(sections):
            candidate = text + _FILLERS[(rounds + i) % len(_FILLERS)].format(**ctx)
            if _chars(candidate) > SECTION_MAX:  # 先试算再落笔，顶破 400 字上限的节跳过
                continue
            sections[i] = (h2, candidate)
            grew = True
        if not grew:
            break
        body = _assemble(lead, sections)
    if not platform.word_min <= len(body) <= platform.word_max:
        raise PipelineError(
            f"成稿字数 {len(body)} 不在平台区间 [{platform.word_min}, {platform.word_max}]："
            f"产品 {product.product_id} 的事实量（{len(product.facts)} 条）撑不起这篇稿，"
            "先补充资料入库，别发注水稿"
        )
    return body


def _assemble(lead: str, sections: Sequence[tuple[str, str]]) -> str:
    parts = [lead]
    for h2, text in sections:
        parts.append(f"## {h2}\n\n{text}")
    return "\n\n".join(parts) + "\n"


# ---------------------------------------------------------------- 结构自检（生成即校验）

def _assert_structure(body: str, product: Product, brand: BrandKit, query: Query) -> None:
    """UT-14-03~09 的内部断言版。这些不是「尽量满足」，是生成器的自我契约：
    任何一条不过，说明模板或事实分桶出 bug 了，必须大声失败。"""
    sections = iter_sections(body)
    if len(sections) < MIN_H2 + 1:  # 内容章节 + FAQ
        raise PipelineError(f"H2 章节 {len(sections)} 个，不足 {MIN_H2} 个")
    bad = [h2 for h2, _ in sections if not is_question(h2)]
    if bad:
        raise PipelineError(f"存在非问句 H2：{bad[0]}")
    for h2, text in sections:
        n = _chars(text)
        if not SECTION_MIN <= n <= SECTION_MAX:
            raise PipelineError(f"章节「{h2}」{n} 字，不在 [{SECTION_MIN}, {SECTION_MAX}] 区间")
    if count_faq(body) < MIN_FAQ:
        raise PipelineError(f"FAQ 不足 {MIN_FAQ} 条")
    lead = extract_lead(body)
    if _chars(lead) > LEAD_MAX:
        raise PipelineError(f"首段 {_chars(lead)} 字，超过 {LEAD_MAX} 字")
    unsourced = find_unsourced_numbers(body, product.facts)
    if unsourced:
        raise PipelineError(f"正文出现无出处数字：{', '.join(unsourced)}（UT-14-06）")
    leaks = find_alias_leaks(body, brand)
    if leaks:
        raise PipelineError(f"正文混用品牌别名：{', '.join(leaks)}（UT-14-09，全文只用全称）")
    mentions = count_entity_mentions(body, brand.name, product.display_name)
    if mentions < MIN_ENTITY_MENTIONS:
        raise PipelineError(f"品牌/产品实体仅出现 {mentions} 次，不足 {MIN_ENTITY_MENTIONS} 次")


# ---------------------------------------------------------------- 主流程

@dataclass(frozen=True, slots=True)
class WriteResult:
    slug: str
    draft_dir: Path
    body_path: Path
    brief_path: Path
    meta_path: Path
    selfcheck_path: Path
    word_count: int
    guard_result: GuardResult


def write_article(
    product_id: str,
    query_id: str,
    *,
    data_root: str | Path,
    query_config: QueryConfig,
    platform: PlatformConfig,
    clock: Clock | None = None,
) -> WriteResult:
    """五步流水线：解析 → 锚定 → 骨架 → 变体 → 自检。

    产出 `data/drafts/<slug>/`：brief.json / meta.json / csdn.md / selfcheck.json。
    目录已存在 → 报错不覆盖（同一选题重出稿请先人工删旧目录，避免静默换稿）。
    """
    clock = clock or SystemClock()
    data_root = Path(data_root)

    # 1 解析（含 G2 抽检闸）+ 2 锚定
    product = load_product(product_id, data_root, query_config.brand)
    query = anchor_query(query_config, query_id)

    # 3 骨架 + 4 变体
    body = _build_body(query, product, query_config.brand, platform)
    _assert_structure(body, product, query_config.brand, query)

    slug = f"{product.product_id}-{query.id.lower()}"
    title = f"{query.text.strip().rstrip('？?')}？{product.display_name}的实战答案"
    if len(title) > platform.title_max:
        title = f"{query.text.strip().rstrip('？?')}？{product.display_name}答案"
    if len(title) > platform.title_max:
        raise PipelineError(f"标题 {len(title)} 字超平台上限 {platform.title_max}，且无法再压缩")
    meta = Meta(
        slug=slug,
        title=title,
        summary=extract_lead(body),
        tags=(
            product.display_name,
            _INTENT_TAG.get(query.intent, "技术实践"),
            "人工智能",
        )[: platform.tags_max],
        category="人工智能",
        ai_involvement="generated",  # 模板生成，如实标注（R4-5）
        anchor_query_id=query.id,
        target_reader=_TARGET_READER.get(query.intent, "企业技术团队"),
        platforms=(platform.name,),
        content_sha=hashlib.sha256(body.encode("utf-8")).hexdigest(),
    )
    draft = Draft(meta=meta, body=body)

    # 5 自检
    result = self_check(draft, platform, clock=clock)

    brief = {
        "schema": 1,
        "product_id": product.product_id,
        "anchor_query_id": query.id,
        "query_text": query.text,
        "intent": query.intent,
        "priority": query.priority,
        "target_reader": meta.target_reader,
        "brand_name": query_config.brand.name,
        "product_name": product.display_name,
        "facts_total": len(product.facts),
        "created_at": clock.now().isoformat(),
    }

    draft_dir = data_root / "drafts" / slug
    try:
        with staged_dir(draft_dir) as staging:
            (staging / "brief.json").write_text(
                json.dumps(brief, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (staging / "meta.json").write_text(
                json.dumps(meta.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (staging / f"{platform.name}.md").write_text(body, encoding="utf-8")
            (staging / "selfcheck.json").write_text(
                json.dumps(result.to_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    except FileExistsError as exc:
        raise PipelineError(
            f"稿件目录已存在：{draft_dir}。同一选题重出稿请先人工确认并删除旧目录"
        ) from exc

    if not result.passed:
        raise DraftRejected(
            f"R4 自检未通过（{', '.join(result.failed_checks)}），"
            f"命中明细见 {draft_dir / 'selfcheck.json'}",
            result,
            draft_dir,
        )
    return WriteResult(
        slug=slug,
        draft_dir=draft_dir,
        body_path=draft_dir / f"{platform.name}.md",
        brief_path=draft_dir / "brief.json",
        meta_path=draft_dir / "meta.json",
        selfcheck_path=draft_dir / "selfcheck.json",
        word_count=len(body),
        guard_result=result,
    )
