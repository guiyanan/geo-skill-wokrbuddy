"""T-13 产品库入库：资料 → `data/products/<id>/`（PRD R1 / R15）。

入库契约（与方案 UT-13-01~06 一一对应）：
- 每条事实 = `claim`（陈述）+ `source`（出处：文件+页码）+ `tier`（A/B/C/D）+ `date`（入库日期）
- `source` 为空、解析出 0 条事实 → 抛 `IngestError`，**磁盘上不留产品目录**（N5 无半成品）
- 二次入库同产品是**合并更新**：旧事实原样保留（source 不被覆盖），新事实按 claim 去重后追加
- 每次入库产出 `REVIEW.md` 人工抽检单；有新事实入库则抽检状态重置，
  未回填 `reviewed_by`/`reviewed_at` 的产品不进自动成稿队列（G2，T-14 强制）

资料格式（「免整理」的边界）：
- `.md/.txt`：页码标记 `<!-- page N -->` + 条目式行文（`- ` / `* ` / `• ` / `1. ` 开头）。
  没有页码标记时整份按第 1 页计。非条目行（标题、段落、注释）不进事实清单。
- `.pptx`：stdlib zipfile + ElementTree 解析，一张幻灯片 = 一页，每个文本段落 = 一条候选事实。

tier 是**启发式初分**，最终由人工抽检确认（这正是 REVIEW.md 第 2 项存在的原因）：
- A 可引用的硬指标：含数字且带指标词（准确率/%/倍/秒/万/吞吐……）
- B 官方能力陈述：支持/提供/内置/兼容/部署/集成……
- C 场景与案例：案例/场景/客户/某X/例如……（若同时带硬数字则归 A）
- D 背景与愿景：其余
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from ..core.clock import Clock, SystemClock
from ..core.fs import staged_dir

__all__ = [
    "Fact",
    "IngestError",
    "IngestResult",
    "RawFact",
    "classify_tier",
    "ingest",
]

MIN_CLAIM_CHARS = 8
PRODUCTS_SUBDIR = "products"
REVIEW_SAMPLE_SIZE = 5

_PRODUCT_ID_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_PAGE_RE = re.compile(r"<!--\s*page\s+(\d+)\s*-->")
_BULLET_RE = re.compile(r"^\s*(?:[-*•]|\d+[.、)）])\s+")
_PAGE_IN_SOURCE_RE = re.compile(r"第(\d+)页")
_PPTX_PAGE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")
_TIERS = ("A", "B", "C", "D")
_TEXT_SUFFIXES = (".md", ".markdown", ".txt")

_NUM = re.compile(r"\d")
_A_WORDS = re.compile(r"[%％倍]|毫秒|秒|分钟|小时|万|亿|页|种|家|个百分点|准确率|提升|下降|缩短|吞吐|并发|响应")
_C_WORDS = re.compile(r"案例|场景|客户|某|例如")
_B_WORDS = re.compile(r"支持|提供|内置|兼容|覆盖|输出|接入|部署|集成|可|能")
_CLAIM_TRAILING_PUNCT = "。．.，,；;：:、！!？?"


class IngestError(ValueError):
    """入库失败：资料不可用、事实为空、出处缺失、产品 id 非法等。"""


@dataclass(frozen=True, slots=True)
class RawFact:
    """抽取器的原始产出：claim + source。tier/date 由入库层统一赋。"""

    claim: str
    source: str


@dataclass(frozen=True, slots=True)
class Fact:
    """facts.json 里的一条。构造即校验：缺 claim/source 的事实不允许存在。"""

    claim: str
    source: str
    tier: str
    date: str

    def __post_init__(self) -> None:
        if not self.claim.strip():
            raise IngestError("fact.claim 不能为空")
        if not self.source.strip():
            raise IngestError(f"fact.source 不能为空：{self.claim[:30]}")
        if self.tier not in _TIERS:
            raise IngestError(f"fact.tier 只能是 {'/'.join(_TIERS)}，收到 {self.tier!r}")
        if not self.date.strip():
            raise IngestError(f"fact.date 不能为空：{self.claim[:30]}")

    def to_json(self) -> dict[str, str]:
        return {"claim": self.claim, "source": self.source, "tier": self.tier, "date": self.date}

    @classmethod
    def from_json(cls, data: dict) -> "Fact":
        try:
            return cls(
                claim=str(data["claim"]),
                source=str(data["source"]),
                tier=str(data["tier"]),
                date=str(data["date"]),
            )
        except KeyError as exc:
            raise IngestError(f"facts.json 缺字段 {exc}：产品库文件损坏或被手改过") from exc


@dataclass(frozen=True, slots=True)
class IngestResult:
    product_id: str
    product_dir: Path
    facts_total: int
    facts_added: int
    sources_total: int


Extractor = Callable[[Path], list[RawFact]]


# --------------------------------------------------------------- tier 启发式

def classify_tier(claim: str) -> str:
    """启发式初分（人工抽检在 REVIEW.md 里确认，分级口径见模块 docstring）。"""
    if _NUM.search(claim) and _A_WORDS.search(claim):
        return "A"
    if _C_WORDS.search(claim):
        return "C"
    if _B_WORDS.search(claim):
        return "B"
    return "D"


# --------------------------------------------------------------- 抽取器

def _extract_markdown(path: Path) -> list[RawFact]:
    page = 1
    facts: list[RawFact] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        marker = _PAGE_RE.search(line)
        if marker:
            page = int(marker.group(1))
            continue
        if not _BULLET_RE.match(line):
            continue
        claim = _BULLET_RE.sub("", line).strip()
        if len(claim) < MIN_CLAIM_CHARS:
            continue
        facts.append(RawFact(claim=claim, source=f"{path.name} 第{page}页"))
    return facts


def _extract_pptx(path: Path) -> list[RawFact]:
    """stdlib 解 pptx：slide 即页，段落即候选事实；slide10 必须排在 slide2 后面。"""
    facts: list[RawFact] = []
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise IngestError(f"不是合法的 pptx（zip 容器损坏）：{path}") from exc
    with archive:
        slides = sorted(
            (
                (int(m.group(1)), name)
                for name in archive.namelist()
                if (m := _PPTX_PAGE_RE.match(name))
            ),
            key=lambda item: item[0],
        )
        for page, name in slides:
            root = ET.fromstring(archive.read(name))
            for para in root.iter():
                if not para.tag.endswith("}p"):
                    continue
                text = "".join(
                    node.text or "" for node in para.iter() if node.tag.endswith("}t")
                ).strip()
                if len(text) < MIN_CLAIM_CHARS:
                    continue
                facts.append(RawFact(claim=text, source=f"{path.name} 第{page}页"))
    return facts


def _default_extractor(path: Path) -> list[RawFact]:
    suffix = path.suffix.lower()
    if suffix in _TEXT_SUFFIXES:
        return _extract_markdown(path)
    if suffix == ".pptx":
        return _extract_pptx(path)
    raise IngestError(
        f"不支持的资料格式：{path.suffix or '(无后缀)'}"
        f"（支持 {', '.join(_TEXT_SUFFIXES)} / .pptx）"
    )


# --------------------------------------------------------------- 内部工具

def _claim_key(claim: str) -> str:
    """去重键：空白无关、尾部标点无关（「……第一次」与「……第一次。」是同一条）。"""
    compact = re.sub(r"\s+", "", claim)
    return compact.rstrip(_CLAIM_TRAILING_PUNCT)


def _slugify(stem: str) -> str:
    slug = re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", stem.lower())).strip("-")
    return slug


def _resolve_product_id(source: Path, product_id: str | None) -> str:
    if product_id is not None:
        pid = product_id.strip()
        if not _PRODUCT_ID_RE.match(pid):
            raise IngestError(f"product_id 只能是小写字母/数字/连字符：{product_id!r}")
        return pid
    pid = _slugify(source.stem)
    if not pid:
        raise IngestError(
            f"无法从文件名推导 product_id：{source.name}，请用 --product-id 显式指定"
        )
    return pid


def _source_pages(facts: Sequence[RawFact]) -> int:
    pages = 1
    for fact in facts:
        marker = _PAGE_IN_SOURCE_RE.search(fact.source)
        if marker:
            pages = max(pages, int(marker.group(1)))
    return pages


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    """同目录临时文件 + os.replace：写一半崩掉也不会留下残缺的 facts.json。"""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _load_library(product_dir: Path) -> tuple[list[Fact], list[dict]]:
    facts_path = product_dir / "facts.json"
    try:
        data = json.loads(facts_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise IngestError(f"已有产品库损坏，拒绝合并：{facts_path}（{exc}）") from exc
    facts = [Fact.from_json(item) for item in data.get("facts", [])]
    sources = list(data.get("sources", []))
    return facts, sources


def _sample(facts: Sequence[Fact], k: int = REVIEW_SAMPLE_SIZE) -> list[tuple[int, Fact]]:
    """系统抽样（等距），确定性：同一份库每次抽到的样本一致，可复核。"""
    if len(facts) <= k:
        return list(enumerate(facts, 1))
    step = math.ceil(len(facts) / k)
    return list(enumerate(facts, 1))[::step][:k]


def _previous_review_note(review_path: Path) -> str | None:
    """上一次人工抽检的留痕（人名@时间），用于新抽检单里提示「不是第一次审」。"""
    if not review_path.is_file():
        return None
    text = review_path.read_text(encoding="utf-8")
    # 注意用 [ \t]* 而不是 \s*：\s 会跨行吃掉换行，把下一行的 reviewed_at 误当成人名
    by = re.search(r"^reviewed_by:[ \t]*(\S.*)$", text, re.MULTILINE)
    if not by:
        return None
    at = re.search(r"^reviewed_at:[ \t]*(\S.*)$", text, re.MULTILINE)
    return f"{by.group(1).strip()} @ {at.group(1).strip()}" if at else by.group(1).strip()


def _render_review(
    product_id: str,
    facts: Sequence[Fact],
    *,
    added: int,
    now_iso: str,
    previous_note: str | None,
) -> str:
    prev_line = f"- 上次抽检：{previous_note}" if previous_note else "- 上次抽检：无"
    rows = "\n".join(
        f"| {idx} | {fact.tier} | {fact.claim} | {fact.source} |"
        for idx, fact in _sample(facts)
    )
    return f"""# 产品入库人工抽检：{product_id}

- 入库时间：{now_iso}
- 事实总数：{len(facts)}（本次新增 {added}）
- 状态：待人工确认
{prev_line}

## 抽检清单（三项全部通过后，回填文末两个字段）

1. 对照原始资料，核对下方抽样事实**未越界**（不含资料之外的表述，要求 0 条越界）
2. `tier` 分级是否合理（A=可引用的硬指标 / B=官方能力陈述 / C=场景案例 / D=背景信息）
3. 是否有明显遗漏的核心卖点

## 抽样事实（系统抽样 {REVIEW_SAMPLE_SIZE} 条，等距）

| # | tier | claim | source |
|---|------|-------|--------|
{rows}

## 确认回填

reviewed_by: 
reviewed_at: 

> 未回填的产品不进入自动成稿队列（T-14 成稿层强制校验）。
"""


# --------------------------------------------------------------- 入库主流程

def ingest(
    source: Path | str,
    product_id: str | None,
    data_root: Path | str,
    *,
    clock: Clock | None = None,
    extractor: Extractor | None = None,
) -> IngestResult:
    """把一份资料并入产品库。失败抛 IngestError，且不留半成品目录。"""
    clock = clock or SystemClock()
    extract = extractor or _default_extractor
    source = Path(source)
    data_root = Path(data_root)

    if not source.is_file():
        raise IngestError(f"资料文件不存在：{source}")
    pid = _resolve_product_id(source, product_id)

    raw = extract(source)
    if not raw:
        raise IngestError(
            f"{source.name} 解析出 0 条事实：入库要求条目式行文"
            "（'- 事实' / 幻灯片文本段落），请检查资料"
        )

    now = clock.now()
    date = now.date().isoformat()
    # 校验在落盘之前做完：任何一条 source 为空都整批拒绝（UT-13-06，不留半份产品）
    fresh: list[Fact] = []
    seen: set[str] = set()
    for item in raw:
        claim = item.claim.strip()
        if not item.source.strip():
            raise IngestError(f"事实 source 为空，整批拒绝入库：{claim[:30]}")
        key = _claim_key(claim)
        if key in seen:
            continue
        seen.add(key)
        fresh.append(Fact(claim=claim, source=item.source.strip(), tier=classify_tier(claim), date=date))

    product_dir = data_root / PRODUCTS_SUBDIR / pid
    new_entry = {
        "name": source.name,
        "sha256": _sha256(source),
        "pages": _source_pages(raw),
        "ingested_at": now.isoformat(),
    }

    if product_dir.exists():
        return _merge(product_dir, source, pid, fresh, new_entry, now)
    return _create(product_dir, source, pid, fresh, new_entry, now)


def _create(
    product_dir: Path,
    source: Path,
    pid: str,
    facts: list[Fact],
    source_entry: dict,
    now,
) -> IngestResult:
    """首次入库：staged_dir 保证目录要么完整出现，要么根本不出现。"""
    with staged_dir(product_dir) as staging:
        _copy_source(staging, source)
        _atomic_write(staging / "facts.json", _manifest(pid, facts, [source_entry], now))
        _atomic_write(
            staging / "REVIEW.md",
            _render_review(pid, facts, added=len(facts), now_iso=now.isoformat(), previous_note=None),
        )
    return IngestResult(pid, product_dir, len(facts), len(facts), 1)


def _merge(
    product_dir: Path,
    source: Path,
    pid: str,
    fresh: list[Fact],
    source_entry: dict,
    now,
) -> IngestResult:
    """合并更新（R15-2）：旧事实原样保留，新事实按 claim 去重后追加。"""
    existing, sources = _load_library(product_dir)
    keys = {_claim_key(f.claim) for f in existing}
    merged = list(existing)
    added = 0
    for fact in fresh:
        if _claim_key(fact.claim) in keys:
            continue
        merged.append(fact)
        keys.add(_claim_key(fact.claim))
        added += 1

    # sources 清单按文件名去重：同名新内容更新条目（事实上的 source 字段不受影响）
    by_name = {entry["name"]: entry for entry in sources}
    by_name[source_entry["name"]] = source_entry
    _copy_source(product_dir, source)
    _atomic_write(product_dir / "facts.json", _manifest(pid, merged, list(by_name.values()), now))

    if added > 0:
        # 有新事实入库 → 抽检状态重置，但上一次的抽检结论留痕
        _atomic_write(
            product_dir / "REVIEW.md",
            _render_review(
                pid,
                merged,
                added=added,
                now_iso=now.isoformat(),
                previous_note=_previous_review_note(product_dir / "REVIEW.md"),
            ),
        )
    return IngestResult(pid, product_dir, len(merged), added, len(by_name))


def _manifest(pid: str, facts: Sequence[Fact], sources: list[dict], now) -> str:
    payload = {
        "schema": 1,
        "product_id": pid,
        "updated_at": now.isoformat(),
        "sources": sources,
        "facts": [fact.to_json() for fact in facts],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2) + "\n"


def _copy_source(product_dir: Path, source: Path) -> None:
    """源文件留档（R15-3 / N4 可追溯）：任何一条事实都能指回原始资料。"""
    sources_dir = product_dir / "sources"
    # 沙箱对 exist_ok=True 的重复 mkdir 会误报 EEXIST，先判存在再建（见 T-12 环境坑）
    if not sources_dir.exists():
        sources_dir.mkdir()
    (sources_dir / source.name).write_bytes(source.read_bytes())
