"""失败分类与红线（T-12 / R12 R13）。

本模块是架构 §3.6「失败分类与处理矩阵」的代码化：**一种可预期的失败，
有且只有一种处置**。分类在这里判定，处置在 publish.py 执行——分开是因为
分类要能被单测独立钉死（不靠浏览器），而处置要碰日志、状态机和稿件目录。

## 失败处理矩阵

| 判定 | 来源（异常） | 落库状态 | 重试 | 出口 |
|---|---|---|---|---|
| 人机校验 | `AuthRequired(has_challenge=True)` | needs_human | **零次**（R13：遇风控即停） | CLI 退 6 |
| 登录失效 | `AuthRequired`（无校验） | **不落库** | 不重试（R6） | CLI 退 2 |
| 平台拒稿 | `ContentRejected`（带平台原话） | needs_human + 原文回写 | 不重试（改稿前重发 = 顶风作案） | CLI 退 6 |
| 网络超时 | `TimeoutError` / `ConnectionError` / playwright 同名超时 | failed | 指数退避 2/4/8s | CLI 退 6 |

## 三条不容商量的规矩

1. **不认识的一律不分类**（`classify` 返回 None）：未知异常是 bug，让它带着
   traceback 炸出来。把 bug 吞成「已分类失败」，比 bug 本身危险得多。
2. **重试只给瞬时故障，且只在 submit 之前**：submitting 行一旦落盘，
   稿子就可能已经碰过平台，任何重试都可能变成发两遍（R9 铁律在 publish.py）。
3. **全局上限（UT-12-05）**：同一篇累计 failed 达到 `RETRY_HARD_CAP`，
   强制转 needs_human，不再自动放行 —— 不管每次失败的原因是什么。

红线说明：本模块对人机校验只做「识别后停手」，不含、也永远不得加入任何
自动通过逻辑（`.precommit` 门禁与 UT-12-06 元测试双保险，R13）。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ..adapters.base import ContentRejected
from .session import AuthRequired
from .state import Status

__all__ = [
    "RETRY_HARD_CAP",
    "RETRY_SCHEDULE_S",
    "FailureKind",
    "Verdict",
    "backoff_delays",
    "classify",
]

# 网络瞬时故障的退避表（架构 §3.6 矩阵行）：重试间隔 2s → 4s → 8s，最多 3 次。
RETRY_SCHEDULE_S: tuple[float, ...] = (2.0, 4.0, 8.0)

# 全局上限 = 3（UT-12-05）：既是单次运行的重试次数硬顶（config.retry_max 只能往小改），
# 也是同一篇跨运行累计 failed 行数的强制停止线。两个口径共用一个数：
# 矩阵里「网络超时最多 3 次」和「总重试 ≥3 强制停止」本来就是同一条规矩的两面。
RETRY_HARD_CAP = len(RETRY_SCHEDULE_S)


class FailureKind(str, Enum):
    """失败类别。字符串值会进 publish-log 的 note 和 CLI 输出，改它 = 改日志语义。"""

    CAPTCHA = "CAPTCHA"  # noqa: 红线 - 仅作判定标签（遇即停），不含任何处理逻辑
    AUTH_EXPIRED = "AUTH_EXPIRED"
    CONTENT_REJECTED = "CONTENT_REJECTED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True)
class Verdict:
    """一次失败的分类结论。

    - `status`：该落到 publish-log 的状态。`None` = 不落库（登录失效：重新授权后
      这篇应当直接能发，授权问题不占重试配额，也不该把稿子标脏）。
    - `retryable`：是否允许**本次运行内**自动重试。只有网络超时为 True。
    - `platform_text`：平台原话。拒稿时必须留住——改稿的人要看的就是这一句。
    """

    kind: FailureKind
    retryable: bool
    status: Status | None
    platform_text: str = ""
    reason: str = ""


def classify(exc: BaseException) -> Verdict | None:
    """把异常翻译成处置结论。返回 None = 不认识，调用方必须原样抛出。

    判断顺序有讲究：AuthRequired 先看（它决定退 2 还是转人工），
    平台拒稿次之，网络超时兜底 —— 越特殊的越先判。
    """
    if isinstance(exc, AuthRequired):
        if exc.has_challenge:
            return Verdict(
                kind=FailureKind.CAPTCHA,  # noqa: 红线 - 仅判定标签（遇即停）
                retryable=False,
                status=Status.NEEDS_HUMAN,
                reason="触发人机校验：已停手并保留现场，等人工处理后重跑（本引擎不做任何绕过）",
            )
        return Verdict(
            kind=FailureKind.AUTH_EXPIRED,
            retryable=False,
            status=None,
            reason="登录态失效：需重新授权后重跑",
        )
    if isinstance(exc, ContentRejected):
        return Verdict(
            kind=FailureKind.CONTENT_REJECTED,
            retryable=False,
            status=Status.NEEDS_HUMAN,
            platform_text=exc.platform_text,
            reason=f"平台拒稿：{exc.platform_text}",
        )
    if _is_network_timeout(exc):
        return Verdict(
            kind=FailureKind.NETWORK_TIMEOUT,
            retryable=True,
            status=Status.FAILED,
            reason=f"网络超时（{type(exc).__name__}）",
        )
    return None


def _is_network_timeout(exc: BaseException) -> bool:
    """网络类瞬时故障。

    core 不 import playwright（它是 dev/运行期依赖，不是库的硬依赖），
    所以按类名认：playwright 的超时类名字恰好也叫 TimeoutError。
    名字撞车的第三方 TimeoutError 也会被当成网络超时 —— 可接受：
    名字叫这个的异常，语义上不会差太多。
    """
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    return type(exc).__name__ == "TimeoutError"


def backoff_delays(budget: int) -> tuple[float, ...]:
    """重试 budget 次对应的等待间隔：退避表的前缀。budget 越界当场报错。

    上限校验放在这里而不是调用方：budget 是安全相关参数（UT-12-05），
    不能只靠「上游一定传对」。
    """
    if isinstance(budget, bool) or not isinstance(budget, int):
        raise ValueError(f"重试预算必须是整数，收到 {budget!r}")
    if budget < 0:
        raise ValueError(f"重试预算不能为负，收到 {budget}")
    if budget > RETRY_HARD_CAP:
        raise ValueError(f"重试预算 {budget} 超过全局上限 {RETRY_HARD_CAP}")
    return RETRY_SCHEDULE_S[:budget]
