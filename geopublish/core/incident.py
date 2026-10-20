"""现场包：把「偶发失败」变成「可回放的确定性失败」。

方案 §2 是这么要求的：任何一次执行失败，自动落一个目录，里面七件套齐全，
`repro.sh` 一跑就能拿到**同一个**失败。规则是硬的——复现不出来就不许改代码，
因为改完你分不清是修好了还是碰巧。

三个设计决定：

1. **脱敏发生在构造期，不是写出时。** `Incident.payload` / `error` 在
   `__post_init__` 里就被洗过，之后无论谁拿去写文件、打日志、发邮件，都拿不到原文。
   写在「写出时」迟早会有人加第二条出口（日报、飞书通知）然后忘了洗。
2. **缺件即失败，且不留半成品。** 见 `fs.staged_dir`。浏览器没给截图就是没给，
   不许拿空文件凑数。
3. **`console.log` 允许是空的。** 页面没打日志是正常情况，为此拒收整个现场包
   等于把真正的失败也丢了。其余六件必须非空。
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any, Mapping, NoReturn

from .clock import Clock, SystemClock
from .fs import staged_dir

__all__ = [
    "BUNDLE_FILES",
    "BundleNotFound",
    "Capture",
    "Incident",
    "IncidentError",
    "IncompleteBundle",
    "ReplayFailed",
    "archive",
    "describe_failure",
    "load",
    "replay",
    "scrub",
    "scrub_text",
]

# 顺序即 bundle.json 里 files 字段的顺序，也是方案 §2.1 的书写顺序。改顺序 = 改格式。
BUNDLE_FILES: tuple[str, ...] = (
    "bundle.json",
    "page.html",
    "screenshot.png",
    "network.har",
    "console.log",
    "input.json",
    "repro.sh",
)

# 允许为空的只有 console.log：页面没打日志是正常的，缺了它没有诊断价值。
_MAY_BE_EMPTY = frozenset({"console.log"})

_BUNDLE_KEYS: tuple[str, ...] = (
    "ticket",
    "stage",
    "adapter",
    "adapter_version",
    "selectors_version",
    "prd_refs",
    "error",
    "created_at",
    "files",
)


class IncidentError(ValueError):
    """现场包相关的所有失败。"""


class IncompleteBundle(IncidentError):
    """七件套不齐（含"文件在但是空的"）。"""


class BundleNotFound(IncidentError):
    """目录不存在 —— 通常是把路径写错了。"""


class ReplayFailed(IncidentError):
    """回放成功复现了当初的失败。**这是预期结果，不是意外。**"""


# --------------------------------------------------------------- 脱敏

# 命中即以整值替换成 ***。用「包含」而不是「等于」：Authorization / X-Csrf-Token
# 这类带前缀的字段名太常见，漏一个就等于没脱敏。
_SENSITIVE_KEY_PARTS: tuple[str, ...] = (
    "cookie",
    "token",
    "secret",
    "password",
    "passwd",
    "authorization",
    "session",
    "apikey",
    "api_key",
    "credential",
    "phone",
    "mobile",
    "email",
)

_PHONE_RE = re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)")
_EMAIL_RE = re.compile(r"(?<![\w.+-])([\w.+-]+)@([\w-]+(?:\.[\w-]+)+)")
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+[A-Za-z0-9._~+/=-]{8,}")
# 常见密钥前缀。真实 cookie 里 JWT 也极常见，单独一条。
_TOKEN_VALUE_RE = re.compile(r"\b(?:gh[pousr]_|sk-|xox[abp]-)[A-Za-z0-9_-]{8,}")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}")
# URL query 里的敏感参数：保留参数名（排障要看是哪个参数），只抹值。
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|access_token|api_key|apikey|sig|signature|session|cookie|key)=)[^&\s#\"']+"
)


def _is_sensitive_key(key: str) -> bool:
    flat = key.lower().replace("-", "").replace("_", "")
    return any(part.replace("_", "") in flat for part in _SENSITIVE_KEY_PARTS)


def scrub_text(text: str) -> str:
    """洗一段文本：手机号留前 3 后 4，其余密钥类整段抹掉。"""
    text = _BEARER_RE.sub("Bearer ***", text)
    text = _URL_SECRET_RE.sub(r"\1***", text)
    text = _TOKEN_VALUE_RE.sub("***", text)
    text = _JWT_RE.sub("***", text)
    text = _EMAIL_RE.sub("***@***", text)
    return _PHONE_RE.sub(lambda m: f"{m.group()[:3]}****{m.group()[-4:]}", text)


def scrub(value: Any) -> Any:
    """递归洗一个结构，并顺手转成可 JSON 化的形式。**不改调用方传进来的对象。**

    敏感键整个换成 `***` 而不是删掉字段：排障时要能看出「当时确实带了 cookie」，
    至于内容是什么，本来就不该留在磁盘上。

    为什么要在这里做类型转换：失败钩子交给我们的 payload 里有 `Path`（稿件路径）、
    `datetime`（时间戳）是常态，而 `json.dumps` 会把它们变成 TypeError ——
    **在出事的路径上再抛一个异常**，等于把唯一能用的证据也弄丢了。所以：
    常见的类型就地转成字符串（转完照样过脱敏），实在不认识的用 `str()` 兜底。
    """
    if isinstance(value, Mapping):
        return {
            key: ("***" if _is_sensitive_key(str(key)) else scrub(item))
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [scrub(item) for item in value]
    if isinstance(value, str):
        return scrub_text(value)
    if isinstance(value, Path):
        return scrub_text(str(value))
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, bytes):
        # 二进制内容（截图、证书）不进 JSON，只留长度：既留下线索又不撑爆包
        return f"<{len(value)} bytes>"
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return scrub_text(str(value))


# --------------------------------------------------------------- 数据结构

@dataclass(frozen=True, slots=True)
class Capture:
    """失败瞬间抓到的四件原始素材（snapshot 复用同一个形状）。"""

    page_html: str = ""
    screenshot: bytes = b""
    network_har: str = ""
    console_log: str = ""

    def artifact(self, name: str) -> bytes:
        if name == "page.html":
            return self.page_html.encode("utf-8")
        if name == "screenshot.png":
            return self.screenshot
        if name == "network.har":
            return self.network_har.encode("utf-8")
        if name == "console.log":
            return self.console_log.encode("utf-8")
        raise IncidentError(f"未知的 artifact：{name}")


def _require_text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise IncidentError(f"{field_name} 不能为空")
    return value


@dataclass(frozen=True, slots=True)
class Incident:
    """一次失败的完整上下文。构造出来就已经是脱敏过的。"""

    ticket: str
    stage: str
    adapter: str
    error: str
    adapter_version: str = ""
    selectors_version: str = ""
    prd_refs: tuple[str, ...] = ()
    payload: Mapping[str, Any] = field(default_factory=dict)
    capture: Capture = field(default_factory=Capture)

    def __post_init__(self) -> None:
        for name in ("ticket", "stage", "adapter", "error"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.payload, Mapping):
            raise IncidentError(f"payload 必须是映射，收到 {type(self.payload).__name__}")
        if not isinstance(self.capture, Capture):
            raise IncidentError(f"capture 必须是 Capture，收到 {type(self.capture).__name__}")
        object.__setattr__(self, "prd_refs", tuple(self.prd_refs))
        # 构造期脱敏：之后所有出口都是干净的，不依赖谁记得调 scrub
        object.__setattr__(self, "error", scrub_text(str(self.error)))
        object.__setattr__(self, "payload", scrub(dict(self.payload)))

    def bundle_json(self, created_at: datetime) -> dict[str, Any]:
        return {
            "ticket": self.ticket,
            "stage": self.stage,
            "adapter": self.adapter,
            "adapter_version": self.adapter_version,
            "selectors_version": self.selectors_version,
            "prd_refs": list(self.prd_refs),
            "error": self.error,
            "created_at": created_at.isoformat(),
            "files": list(BUNDLE_FILES),
        }


# --------------------------------------------------------------- 归档

_UNSAFE_RE = re.compile(r"[^\w.-]+", re.UNICODE)
_MAX_COMPONENT = 64


def _component(text: str) -> str:
    """把任意输入压成一个安全的路径段。

    ticket 和 stage 都可能来自外部（配置、异常类名），不能靠它们跳目录。
    保留中文：`工单7 / 注入` 这种阶段名是检索现场包的主要线索，冲成 unknown
    等于把目录名变成一串没用的数字。长度截到 64，避免 ENAMETOOLONG。
    """
    cleaned = _UNSAFE_RE.sub("-", str(text).strip()).strip("-.")
    cleaned = cleaned[:_MAX_COMPONENT].strip("-.")
    return cleaned or "unknown"


def _unique_dir(out_dir: Path, base: str) -> Path:
    candidate = out_dir / base
    serial = 2
    while candidate.exists():
        candidate = out_dir / f"{base}_{serial}"
        serial += 1
    return candidate


def _missing_artifacts(directory: Path) -> list[str]:
    missing: list[str] = []
    for name in BUNDLE_FILES:
        path = directory / name
        if not path.is_file():
            missing.append(name)
        elif name not in _MAY_BE_EMPTY and path.stat().st_size == 0:
            missing.append(f"{name}（空文件）")
    return missing


def _verify(directory: Path) -> None:
    missing = _missing_artifacts(directory)
    if missing:
        raise IncompleteBundle(
            f"现场包不完整，缺 {len(missing)} 件：{'、'.join(missing)}"
        )


def render_repro(incident: Incident, python: str | None = None) -> str:
    """生成可执行的一键复现脚本。

    - 内嵌**创建这个包的那台机器**的解释器与包路径：现场包通常当场就要跑，
      过几天换到别人机器上再改 `GEOPUBLISH_PY`。
    - 只引用包目录，不内联任何输入内容 —— 脚本本身也是会被粘贴/上传的东西。
    """
    interpreter = python or sys.executable
    package_root = str(Path(__file__).resolve().parents[2])
    return f"""#!/bin/sh
# 现场包复现脚本（{incident.ticket} / {incident.stage}）—— 由 geopublish 生成，别手改
# 退出码：4 = 已复现（预期结果）；5 = 环境不对，先修环境再谈复现
set -u
cd "$(dirname "$0")" || exit 1
PY="${{GEOPUBLISH_PY:-{interpreter}}}"
PYTHONPATH="${{PYTHONPATH:+$PYTHONPATH:}}{package_root}"
export PYTHONPATH
"$PY" -m geopublish --version >/dev/null 2>&1 || {{ echo "geopublish 不可用：先装依赖，或设 GEOPUBLISH_PY 指向能跑的解释器" >&2; exit 5; }}
exec "$PY" -m geopublish incident replay .
"""


def archive(
    incident: Incident,
    out_dir: str | Path,
    *,
    clock: Clock | None = None,
    python: str | None = None,
) -> Path:
    """落一个现场包，返回目录。缺件会抛 `IncompleteBundle`，且不留半成品。"""
    now = (clock or SystemClock()).now()
    out_dir = Path(out_dir)
    if out_dir.exists() and not out_dir.is_dir():
        # 否则 mkdir(parents=True) 会报 EEXIST，而真正的原因是「这是个文件」
        raise IncidentError(f"输出目录不是目录：{out_dir}")
    base = f"{now.strftime('%Y%m%d_%H%M%S')}_{_component(incident.ticket)}_{_component(incident.stage)}"
    target = _unique_dir(out_dir, base)

    with staged_dir(target) as staging:
        (staging / "bundle.json").write_text(
            json.dumps(incident.bundle_json(now), ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (staging / "page.html").write_bytes(incident.capture.artifact("page.html"))
        (staging / "screenshot.png").write_bytes(incident.capture.artifact("screenshot.png"))
        (staging / "network.har").write_bytes(incident.capture.artifact("network.har"))
        (staging / "console.log").write_bytes(incident.capture.artifact("console.log"))
        (staging / "input.json").write_text(
            json.dumps(incident.payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        script = staging / "repro.sh"
        script.write_text(render_repro(incident, python), encoding="utf-8")
        script.chmod(0o755)
        _verify(staging)

    return target


# --------------------------------------------------------------- 加载/回放

def _read_bundle(directory: Path) -> dict[str, Any]:
    path = directory / "bundle.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # 带上文件名：现场包里同名文件有好几个，只报 JSON 解析失败等于没说
        raise IncidentError(f"bundle.json 无法解析（{path}）：{exc}") from exc
    missing = [key for key in _BUNDLE_KEYS if key not in data]
    if missing:
        raise IncidentError(f"bundle.json 缺字段：{'、'.join(missing)}")
    return data


def load(directory: str | Path) -> Incident:
    """读回一个现场包。七件套不齐 / bundle.json 坏了都当场报错。"""
    directory = Path(directory)
    if not directory.is_dir():
        raise BundleNotFound(f"现场包不存在：{directory}")
    _verify(directory)
    data = _read_bundle(directory)

    return Incident(
        ticket=str(data["ticket"]),
        stage=str(data["stage"]),
        adapter=str(data["adapter"]),
        adapter_version=str(data["adapter_version"]),
        selectors_version=str(data["selectors_version"]),
        prd_refs=tuple(str(item) for item in data["prd_refs"]),
        error=str(data["error"]),
        payload=json.loads((directory / "input.json").read_text(encoding="utf-8")),
        capture=Capture(
            page_html=(directory / "page.html").read_text(encoding="utf-8"),
            screenshot=(directory / "screenshot.png").read_bytes(),
            network_har=(directory / "network.har").read_text(encoding="utf-8"),
            console_log=(directory / "console.log").read_text(encoding="utf-8"),
        ),
    )


def describe_failure(incident: Incident) -> str:
    """回放时打在终端上的那段话：一眼看出「哪个阶段、原报错是什么」。"""
    refs = ",".join(incident.prd_refs) or "-"
    return (
        f"现场复现：ticket={incident.ticket} stage={incident.stage} "
        f"adapter={incident.adapter}（需求 {refs}）\n"
        f"  原因：{incident.error}"
    )


def replay(directory: str | Path) -> NoReturn:
    """跑一遍现场包，把当初的失败重新抛出来。

    这里**永远**以 `ReplayFailed` 结束 —— 退出码 4 的含义就是「已复现」。
    将来接上真 adapter 之后，这个函数会变成「用包里的 DOM 重跑 flow，再把异常转成
    ReplayFailed」，对外契约不变。
    """
    raise ReplayFailed(describe_failure(load(directory)))
