"""R4 合规自检（硬闸门）。

这一层的输入是「一篇已成型的稿子」+「目标平台的限制」，输出是**过或不过**。
它不认识浏览器、不认识 CSDN，纯字符串逻辑 —— 所以能在秒级跑完，也能在
成稿阶段（T-14）反复跑，而不是等到要发布时才发现稿子不能发。

## 检查项（PRD R4 五项 + 一条前置）

| 键 | 判据 | 说明 |
|---|---|---|
| `non_empty` | 正文非空 | 前置项：空稿直接拒，不叠一堆无意义命中 |
| `absolute` | 绝对化用语命中数 = 0 | 词表见 `references/banned-words.json` |
| `marketing` | 营销句式命中数 = 0 | 同上 |
| `number_source` | 数字类主张出处覆盖率 = 100% | 见下「引用约定」 |
| `platform_limits` | 标题长度、标签数在平台上限内 | 上限来自 `config`，不写死 |
| `ai_involvement` | AI 参与度 ∈ {human, assisted, generated} | 合规留痕，缺了不许发 |

**任一不过 → `status = rejected`，不进发布队列。** 没有 `skip` / `force` 参数，
也不接受 `**kwargs`（`tests/unit/test_guard.py` 里有元测试钉着）。理由是这条拦的
是不可逆后果：发出去的错稿收不回来（PRD §9.5）。

## 引用约定（数字主张怎么算「有出处」）

判据是**同句**内出现引用标记，四种写法都认：

- 脚注：`[1]`、`[^1]`、`[1][2]`
- 括注：`（来源：2025 年公开测试报告）`、`(Source: internal benchmark)`
- 方括号：`[来源：产品白皮书 第 3 页]`
- 六角括号：`【出处：官网】`

引用写在下一句**不算**——否则一句「数据来自报告」就能给全文免责。
代码块、行内代码、URL、小标题/列表序号里的数字不算数字类主张（它们不是 claim）。

成稿侧（T-14）必须按这个约定写；`facts.json` 里的 `source` 就是填进标记的内容。

## 为什么词表在 JSON 里而不是配置里

PRD §9.1 定了：R4 规则不进配置层，不给人「临时放宽」的口子。所以词表跟着代码走、
进评审、进 git；换口径 = 改 JSON + 过测试，而不是改一个运行时可改的开关。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Pattern, Sequence

from .clock import Clock, SystemClock
from .config import PlatformConfig
from .models import Draft, Meta
from .state import Status

__all__ = [
    "AI_INVOLVEMENT_VALUES",
    "CHECK_ORDER",
    "DEFAULT_WORDS_PATH",
    "R4_CHECKS",
    "BannedWords",
    "CheckOutcome",
    "GuardConfigError",
    "GuardRejected",
    "GuardResult",
    "Hit",
    "Term",
    "load_banned_words",
    "require_pass",
    "self_check",
]

DEFAULT_WORDS_PATH = Path(__file__).resolve().parents[1] / "references" / "banned-words.json"

# 顺序即 selfcheck.json 里的顺序：先前置、再按 R4 原文五条
CHECK_ORDER: tuple[str, ...] = (
    "non_empty",
    "absolute",
    "marketing",
    "number_source",
    "platform_limits",
    "ai_involvement",
)
# PRD R4 的五条（前置项不算）：DoD 要求这五项全部与 rejected 联动
R4_CHECKS: tuple[str, ...] = CHECK_ORDER[1:]

AI_INVOLVEMENT_VALUES: tuple[str, ...] = ("human", "assisted", "generated")

_ALLOWED_WORDS_KEYS = {"version", "absolute", "marketing"}

# 元信息命中没有正文位置，用 -1 明确表示「不在正文里」，别假装在第 0 个字
NO_OFFSET = -1
_EXCERPT_LIMIT = 80

# ---------------------------------------------------------------- 文本切分
# 句末标点与换行都算分隔；分隔符归前一句，于是切片是无缝平铺的，
# 任何一个正文字符都必落在某一段里 —— 就不需要「找不到就兜底」的分支
_DELIM_RE = re.compile(r"[。！？!?\n]")
# 数字主张：12 / 3.5 / 1,000 / 99% 都算
_NUMBER_RE = re.compile(r"\d+(?:[.,]\d+)*\s*[%％]?")
_CODE_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]*`")
_URL_RE = re.compile(r"https?://\S+")
# Markdown 小标题 / 列表的序号不是主张：`## 3. 为什么`、`1、第一步`
_LINE_PREFIX_RE = re.compile(r"(?m)^[ \t]*(?:#{1,6}[ \t]*)?\d+(?:\.\d+)*[.、)）]?[ \t]+")

_CITATION_RE = re.compile(
    r"\[\s*\^?\d+(?:\s*[,，]\s*\^?\d+)*\s*\]"  # [1] / [^1] / [1][2]（分段匹配）
    r"|\[\s*(?:数据)?(?:来源|出处|source|参考)[^\[\]]*\]"  # [来源：…]
    r"|【\s*(?:数据)?(?:来源|出处)[^】]*】"  # 【出处：…】
    r"|[（(]\s*(?:数据)?(?:来源|出处|source|参考)[^）)]*[）)]",  # （来源：…）/(source: …)
    re.IGNORECASE,
)


class GuardConfigError(ValueError):
    """词表缺失、写错、正则编译失败。"""


class GuardRejected(ValueError):
    """自检未通过 —— 调用方不该 catch 后继续发布。"""


# ---------------------------------------------------------------- 数据结构
@dataclass(frozen=True, slots=True)
class Hit:
    """一条命中。`rule` 比 `check` 更细：一个 check 可能出多种 rule。"""

    check: str
    rule: str
    term: str
    excerpt: str
    offset: int

    def to_json(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "rule": self.rule,
            "term": self.term,
            "excerpt": self.excerpt,
            "offset": self.offset,
        }


@dataclass(frozen=True, slots=True)
class CheckOutcome:
    name: str
    hits: tuple[Hit, ...] = ()

    @property
    def passed(self) -> bool:
        return not self.hits

    def to_json(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "hit_count": len(self.hits),
            "hits": [h.to_json() for h in self.hits],
        }


@dataclass(frozen=True, slots=True)
class GuardResult:
    """一次自检的完整结论，可直接落盘为 `selfcheck.json`。"""

    slug: str
    platform: str
    checks: tuple[CheckOutcome, ...]
    checked_at: datetime

    @property
    def hits(self) -> tuple[Hit, ...]:
        return tuple(h for c in self.checks for h in c.hits)

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_checks(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.checks if not c.passed)

    @property
    def status(self) -> Status:
        """过 → 可入队；不过 → rejected（PRD R4：任一不满足即拒）。"""
        return Status.PENDING if self.passed else Status.REJECTED

    def to_json(self) -> dict[str, Any]:
        return {
            "slug": self.slug,
            "platform": self.platform,
            "checked_at": self.checked_at.isoformat(),
            "passed": self.passed,
            "status": self.status.value,
            "failed_checks": list(self.failed_checks),
            "checks": [c.to_json() for c in self.checks],
            "hits": [h.to_json() for h in self.hits],
        }


@dataclass(frozen=True, slots=True)
class Term:
    """词表条目。`label` 是人看到的那个词，`pattern` 是实际匹配器。"""

    label: str
    pattern: Pattern[str]

    @classmethod
    def literal(cls, word: str) -> Term:
        return cls(label=word, pattern=re.compile(re.escape(word)))

    @classmethod
    def from_entry(cls, entry: Any, where: str) -> Term:
        """字符串 = 字面量；对象 = 正则（给中文误伤场景用）。"""
        if isinstance(entry, str):
            word = entry.strip()
            if not word:
                raise GuardConfigError(f"{where} 出现空词条")
            return cls.literal(word)
        if isinstance(entry, Mapping):
            raw = entry.get("pattern")
            if not isinstance(raw, str) or not raw.strip():
                raise GuardConfigError(f"{where} 的对象条目必须带非空 pattern")
            label = str(entry.get("label") or raw).strip()
            try:
                return cls(label=label, pattern=re.compile(raw))
            except re.error as exc:
                raise GuardConfigError(f"{where} 正则写错：{raw!r}（{exc}）") from exc
        raise GuardConfigError(f"{where} 条目只能是字符串或 {{pattern,label}} 对象")


@dataclass(frozen=True, slots=True)
class BannedWords:
    absolute: tuple[Term, ...]
    marketing: tuple[Term, ...]

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> BannedWords:
        if not isinstance(data, Mapping):
            raise GuardConfigError(f"词表必须是 JSON 对象，收到 {type(data).__name__}")

        # JSON 写不了注释，沿用仓库约定：`_` 开头的键当注释
        unknown = sorted(
            k for k in data if not str(k).startswith("_") and k not in _ALLOWED_WORDS_KEYS
        )
        if unknown:
            raise GuardConfigError(f"词表出现未知分类：{', '.join(unknown)}")

        groups = {}
        for key in ("absolute", "marketing"):
            if key not in data:
                raise GuardConfigError(f"词表缺分类：{key}")
            entries = data[key]
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                raise GuardConfigError(f"词表 {key} 必须是数组，收到 {type(entries).__name__}")
            groups[key] = tuple(
                Term.from_entry(entry, f"{key}[{i}]") for i, entry in enumerate(entries)
            )
        return cls(absolute=groups["absolute"], marketing=groups["marketing"])


def load_banned_words(path: str | Path | None = None) -> BannedWords:
    target = Path(path) if path is not None else DEFAULT_WORDS_PATH
    if not target.exists():
        raise GuardConfigError(f"词表文件不存在：{target}")
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise GuardConfigError(
            f"词表不是合法 JSON：{target}（第 {exc.lineno} 行：{exc.msg}）"
        ) from exc
    return BannedWords.from_dict(data)


# ---------------------------------------------------------------- 文本工具
def _blank(match: re.Match[str]) -> str:
    """把一段文本抹成等长空白，保留换行 —— 这样掩码前后偏移量完全一致。"""
    return re.sub(r"[^\n]", " ", match.group())


def _mask(text: str) -> str:
    masked = _CODE_FENCE_RE.sub(_blank, text)
    masked = _INLINE_CODE_RE.sub(_blank, masked)
    masked = _URL_RE.sub(_blank, masked)
    return _LINE_PREFIX_RE.sub(_blank, masked)


class _Segments:
    """把正文切成无缝平铺的句子段，并给命中点算摘录。"""

    __slots__ = ("text", "spans")

    def __init__(self, masked: str, original: str) -> None:
        self.text = original
        spans: list[tuple[int, int]] = []
        start = 0
        for m in _DELIM_RE.finditer(masked):
            spans.append((start, m.end()))
            start = m.end()
        if start < len(masked):
            spans.append((start, len(masked)))
        self.spans: tuple[tuple[int, int], ...] = tuple(spans)

    def span_at(self, offset: int) -> tuple[int, int]:
        return next(
            (s for s in self.spans if s[0] <= offset < s[1]),
            (offset, offset),
        )

    def excerpt(self, offset: int, length: int) -> str:
        start, end = self.span_at(offset)
        width = max(8, (_EXCERPT_LIMIT - length - 2) // 2)
        left = max(start, offset - width)
        right = min(end, offset + length + width)
        core = self.text[left:right].strip()
        lead = "…" if left > start else ""
        tail = "…" if right < end else ""
        return f"{lead}{core}{tail}"


# ---------------------------------------------------------------- 五项检查
def _check_non_empty(draft: Draft) -> tuple[Hit, ...]:
    if draft.body.strip():
        return ()
    return (
        Hit(
            check="non_empty",
            rule="empty_body",
            term="<空稿>",
            excerpt="正文为空，不允许空发",
            offset=NO_OFFSET,
        ),
    )


def _scan_terms(
    text: str, segs: _Segments, terms: Iterable[Term], check: str
) -> tuple[Hit, ...]:
    hits: list[Hit] = []
    for term in terms:
        for m in term.pattern.finditer(text):
            hits.append(
                Hit(
                    check=check,
                    rule=check,
                    term=term.label,
                    excerpt=segs.excerpt(m.start(), m.end() - m.start()),
                    offset=m.start(),
                )
            )
    hits.sort(key=lambda h: (h.offset, h.term))
    return tuple(hits)


def _check_numbers(masked: str, segs: _Segments, body_start: int) -> tuple[Hit, ...]:
    """同句没有引用标记的数字，逐个报出来。

    只查正文（`body_start` 之后）。标题里的数字不受这条管：100 字的标题里塞
    `[来源：…]` 既不美观也不现实，那属于成稿侧（T-14）的写作约束，不是发布闸门。
    """
    hits: list[Hit] = []
    for start, end in segs.spans:
        if start < body_start:  # 标题行：跳过，见 docstring
            continue
        sentence = masked[start:end]
        if not _NUMBER_RE.search(sentence):
            continue
        # 出处看原文切片：掩码会把引用的方括号一起抹掉
        if _CITATION_RE.search(segs.text[start:end]):
            continue
        for m in _NUMBER_RE.finditer(sentence):
            offset = start + m.start()
            hits.append(
                Hit(
                    check="number_source",
                    rule="unsourced_number",
                    term=m.group().strip(),
                    excerpt=segs.excerpt(offset, m.end() - m.start()),
                    offset=offset,
                )
            )
    return tuple(hits)


def _check_platform_limits(meta: Meta, platform: PlatformConfig) -> tuple[Hit, ...]:
    hits: list[Hit] = []

    title = meta.title.strip()
    if len(title) > platform.title_max:
        hits.append(
            Hit(
                check="platform_limits",
                rule="title_max",
                term=title,
                excerpt=f"标题 {len(title)} 字，平台上限 {platform.title_max} 字",
                offset=NO_OFFSET,
            )
        )

    if len(meta.tags) > platform.tags_max:
        hits.append(
            Hit(
                check="platform_limits",
                rule="tag_count",
                term=f"{len(meta.tags)} 个标签",
                excerpt=f"平台上限 {platform.tags_max} 个标签",
                offset=NO_OFFSET,
            )
        )

    for tag in meta.tags:
        if not str(tag).strip():
            hits.append(
                Hit(
                    check="platform_limits",
                    rule="tag_empty",
                    term=repr(tag),
                    excerpt="标签不能为空 —— 空标签在平台上会变成一个空格子",
                    offset=NO_OFFSET,
                )
            )
    return tuple(hits)


def _check_ai_involvement(meta: Meta) -> tuple[Hit, ...]:
    value = (meta.ai_involvement or "").strip().lower()
    if value in AI_INVOLVEMENT_VALUES:
        return ()

    rule = "ai_involvement_missing" if not value else "ai_involvement_invalid"
    detail = (
        "AI 参与度未填写，合规留痕缺失"
        if not value
        else f"AI 参与度 {meta.ai_involvement!r} 不在 {'/'.join(AI_INVOLVEMENT_VALUES)} 之内"
    )
    return (
        Hit(
            check="ai_involvement",
            rule=rule,
            term=meta.ai_involvement or "<空>",
            excerpt=detail,
            offset=NO_OFFSET,
        ),
    )


# ---------------------------------------------------------------- 入口
def self_check(
    draft: Draft,
    platform: PlatformConfig,
    *,
    words: BannedWords | None = None,
    clock: Clock | None = None,
) -> GuardResult:
    """跑完 R4 全部检查，一次给全量命中（不是只报第一个）。

    这里**故意不提供** `skip` / `allow` / `**kwargs`：PRD §9.5 把「跳过自检」列为红线。
    """
    wordlist = words if words is not None else load_banned_words()
    # 标题也是发布出去的文字，绝对化用语/营销句式要一起查（R4 说的是整篇稿件）。
    # 但标题行不参与数字出处检查，见 _check_numbers 的说明。
    scan_text = f"{draft.meta.title}\n{draft.body}"
    masked = _mask(scan_text)
    segs = _Segments(masked, scan_text)
    body_start = len(draft.meta.title) + 1

    body_hits = {
        "absolute": _scan_terms(scan_text, segs, wordlist.absolute, "absolute"),
        "marketing": _scan_terms(scan_text, segs, wordlist.marketing, "marketing"),
        "number_source": _check_numbers(masked, segs, body_start),
    }
    # 空稿时正文检查没有可查的内容，结论由前置项给出，避免叠一堆空命中
    hits_by_check = {
        "non_empty": _check_non_empty(draft),
        **body_hits,
        "platform_limits": _check_platform_limits(draft.meta, platform),
        "ai_involvement": _check_ai_involvement(draft.meta),
    }

    return GuardResult(
        slug=draft.meta.slug,
        platform=platform.name,
        checks=tuple(
            CheckOutcome(name=name, hits=hits_by_check[name]) for name in CHECK_ORDER
        ),
        checked_at=(clock if clock is not None else SystemClock()).now(),
    )


def require_pass(result: GuardResult) -> None:
    """给调用方的硬闸门写法：不过就抛，别让它静默继续去发布。"""
    if result.passed:
        return

    detail = "；".join(
        f"{h.check}:{h.term}（{h.excerpt}）" for h in result.hits[:3]
    )
    raise GuardRejected(
        f"R4 自检未通过（{', '.join(result.failed_checks)}）：{detail}"
    )
