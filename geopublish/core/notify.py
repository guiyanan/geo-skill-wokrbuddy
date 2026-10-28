"""T-19 邮件推送（PRD R18 推送要求）。

三条红线：

1. **推送失败不得影响发布状态判定**（UT-19-03）：本模块永远不碰 publish-log，
   稿件该 `published` 还是 `published`；推送失败只记进
   `data/reports/<date>.notify.json` 的 `notify_failed`。
2. **SMTP 凭据只走环境变量**（`GEOPUBLISH_SMTP_*`）：配置文件会进笔记、
   会贴截图，密码不该出现在那里（N2 的延伸）。
3. **异常告警与日报分开、同日同种只发一次**：告警轰炸 = 告警失效。
   去重标记就在 notify.json 里，重启进程也丢不了。

分层纪律：本模块只做「发邮件 + 记状态」，**不决定发什么**。主题行、正文、
「今天该不该告警」都是上层（report 渲染 / CLI 编排）算好传进来的 ——
所以整条链路可以用假 transport 原样跑，不需要真 SMTP 服务器。
"""

from __future__ import annotations

import hashlib
import json
import os
import smtplib
import tempfile
from dataclasses import dataclass, field
from datetime import date
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from .clock import Clock, SystemClock

__all__ = [
    "ALERT_NEEDS_HUMAN",
    "ALERT_ZERO_STREAK",
    "ENV_SMTP_HOST",
    "ENV_SMTP_PASSWORD",
    "ENV_SMTP_PORT",
    "ENV_SMTP_SENDER",
    "ENV_SMTP_USER",
    "NOTIFY_SCHEMA",
    "AlertRecord",
    "NotifyConfig",
    "NotifyError",
    "NotifyRecord",
    "NotifyState",
    "notify_day",
    "notify_state_path",
    "send_email",
]

NOTIFY_SCHEMA = 1

ENV_SMTP_HOST = "GEOPUBLISH_SMTP_HOST"
ENV_SMTP_PORT = "GEOPUBLISH_SMTP_PORT"
ENV_SMTP_USER = "GEOPUBLISH_SMTP_USER"
ENV_SMTP_PASSWORD = "GEOPUBLISH_SMTP_PASSWORD"
ENV_SMTP_SENDER = "GEOPUBLISH_SMTP_FROM"

ALERT_NEEDS_HUMAN = "needs_human"
ALERT_ZERO_STREAK = "zero_streak"

SMTP_TIMEOUT_SECONDS = 30


class NotifyError(ValueError):
    """推送配置/状态文件相关的所有问题。SMTP 发送失败不走这里 —— 那是
    notify_failed，记进状态文件，不抛。"""


# ---------------------------------------------------------------- SMTP 配置
@dataclass(frozen=True, slots=True)
class NotifyConfig:
    """SMTP 连接参数 + 收发双方。凭据全部来自环境变量。"""

    host: str
    port: int
    user: str
    password: str
    sender: str
    recipients: tuple[str, ...]

    @property
    def use_ssl(self) -> bool:
        """465 = SMTP over SSL；其余端口（587/25）走 STARTTLS 升级。"""
        return self.port == 465

    @classmethod
    def from_env(
        cls,
        env: Mapping[str, str] | None = None,
        *,
        recipients: str | Sequence[str],
    ) -> NotifyConfig:
        """从环境变量读配置。缺项一次报全，不留「修一个又冒一个」的回合。"""
        env = os.environ if env is None else env
        missing = [
            name
            for name in (ENV_SMTP_HOST, ENV_SMTP_USER, ENV_SMTP_PASSWORD, ENV_SMTP_SENDER)
            if not str(env.get(name) or "").strip()
        ]
        if missing:
            raise NotifyError(
                "SMTP 未配置，缺少环境变量："
                + ", ".join(missing)
                + "（在调度机器上 export 后重试；密码用邮箱授权码不是登录密码）"
            )
        port_raw = str(env.get(ENV_SMTP_PORT) or "465").strip()
        try:
            port = int(port_raw)
        except ValueError as exc:
            raise NotifyError(f"{ENV_SMTP_PORT} 必须是整数，收到 {port_raw!r}") from exc
        if not 1 <= port <= 65535:
            raise NotifyError(f"{ENV_SMTP_PORT} 越界（1-65535）：{port}")

        if isinstance(recipients, str):
            recipients = [r.strip() for r in recipients.split(",")]
        clean = tuple(r for r in recipients if r)
        if not clean:
            raise NotifyError("收件人不能为空（配置的 report.email）")
        for r in clean:
            if "@" not in r or " " in r:
                raise NotifyError(f"收件人不是合法邮箱：{r!r}")
        return cls(
            host=str(env[ENV_SMTP_HOST]).strip(),
            port=port,
            user=str(env[ENV_SMTP_USER]).strip(),
            password=str(env[ENV_SMTP_PASSWORD]),
            sender=str(env[ENV_SMTP_SENDER]).strip(),
            recipients=clean,
        )


# ---------------------------------------------------------------- 发送
# smtp_factory 的签名：() -> 已连接的 SMTP 对象（支持 with 上下文）。
# 测试注入假 factory；生产缺省按 use_ssl 选 SMTP_SSL / SMTP+STARTTLS。
SmtpFactory = Callable[[], Any]


def send_email(
    cfg: NotifyConfig,
    subject: str,
    body: str,
    *,
    smtp_factory: SmtpFactory | None = None,
) -> None:
    """发一封纯文本邮件。任何 SMTP 失败原样抛出 —— 记不记 notify_failed
    是调用方的事，这里不吞。"""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(body)

    factory = smtp_factory or _default_factory(cfg)
    with factory() as server:
        if not cfg.use_ssl:
            server.starttls()
        server.login(cfg.user, cfg.password)
        server.send_message(msg)


def _default_factory(cfg: NotifyConfig) -> SmtpFactory:
    if cfg.use_ssl:
        return lambda: smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_SECONDS)
    return lambda: smtplib.SMTP(cfg.host, cfg.port, timeout=SMTP_TIMEOUT_SECONDS)


# ---------------------------------------------------------------- 状态落盘
@dataclass(slots=True)
class NotifyRecord:
    """一次推送的结果。status: sent / notify_failed / skipped。

    `digest` 是「上次发出的主题+正文」的指纹：T-18b 后一天有多个发车槽位，
    后面槽位的日报 X/Y 会变 —— 内容变了就该重发，没变才不打扰（防重跑轰炸）。
    """

    status: str
    at: str
    reason: str = ""
    digest: str = ""

    def to_json(self) -> dict[str, Any]:
        return {"status": self.status, "at": self.at, "reason": self.reason, "digest": self.digest}

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, where: str) -> NotifyRecord:
        if "status" not in data or "at" not in data:
            raise NotifyError(f"{where} 缺字段：status/at")
        return cls(
            status=str(data["status"]),
            at=str(data["at"]),
            reason=str(data.get("reason") or ""),
            digest=str(data.get("digest") or ""),
        )


def _digest(subject: str, body: str) -> str:
    return hashlib.sha256(f"{subject}\n{body}".encode("utf-8")).hexdigest()


@dataclass(slots=True)
class AlertRecord(NotifyRecord):
    """一条即时告警（与日报分开的即时通道）。kind: needs_human / zero_streak。"""

    kind: str = ""
    detail: str = ""

    def to_json(self) -> dict[str, Any]:
        # 不用零参 super()：slots=True 的 dataclass 会被装饰器重建，
        # __class__ 闭包指向旧类，super() 直接 TypeError（CPython 已知坑）
        data = NotifyRecord.to_json(self)
        data["kind"] = self.kind
        data["detail"] = self.detail
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, where: str) -> AlertRecord:
        base = NotifyRecord.from_json(data, where=where)
        if not str(data.get("kind") or "").strip():
            raise NotifyError(f"{where} 缺字段：kind")
        return cls(
            status=base.status,
            at=base.at,
            reason=base.reason,
            kind=str(data["kind"]),
            detail=str(data.get("detail") or ""),
        )


@dataclass(slots=True)
class NotifyState:
    """一天的推送台账（data/reports/<date>.notify.json）。"""

    date: str
    daily: NotifyRecord | None = None
    alerts: list[AlertRecord] = field(default_factory=list)

    def alert_sent(self, kind: str) -> bool:
        return any(a.kind == kind and a.status == "sent" for a in self.alerts)

    def to_json(self) -> dict[str, Any]:
        return {
            "schema": NOTIFY_SCHEMA,
            "date": self.date,
            "daily": self.daily.to_json() if self.daily else None,
            "alerts": [a.to_json() for a in self.alerts],
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any], *, where: str) -> NotifyState:
        if data.get("schema") != NOTIFY_SCHEMA:
            raise NotifyError(f"{where} 的 schema 必须是 {NOTIFY_SCHEMA}，收到 {data.get('schema')!r}")
        if "date" not in data:
            raise NotifyError(f"{where} 缺字段：date")
        daily_raw = data.get("daily")
        return cls(
            date=str(data["date"]),
            daily=NotifyRecord.from_json(daily_raw, where=f"{where}.daily") if daily_raw else None,
            alerts=[
                AlertRecord.from_json(a, where=f"{where}.alerts[{i}]")
                for i, a in enumerate(data.get("alerts") or [])
            ],
        )


def notify_state_path(data_root: str | Path, day: date | str) -> Path:
    day_str = day if isinstance(day, str) else day.isoformat()
    return Path(data_root) / "reports" / f"{day_str}.notify.json"


def load_notify_state(data_root: str | Path, day: date | str) -> NotifyState | None:
    p = notify_state_path(data_root, day)
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise NotifyError(f"推送台账不是合法 JSON：{p}（第 {exc.lineno} 行）") from exc
    return NotifyState.from_json(data, where=f"推送台账 {p}")


def save_notify_state(data_root: str | Path, state: NotifyState) -> Path:
    """原子写：cron 与人读台账时绝不读到写一半的文件。"""
    target = notify_state_path(data_root, state.date)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.stem}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(state.to_json(), ensure_ascii=False, indent=2) + "\n")
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    return target


# ---------------------------------------------------------------- 一天的推送
# 一封待发的邮件：(主题, 正文)。发什么由上层决定，这里只管送达与记账。
Outbound = tuple[str, str]


def notify_day(
    data_root: str | Path,
    day: date | str,
    *,
    daily: Outbound | None,
    alerts: Sequence[tuple[str, str, str]] = (),
    mail: NotifyConfig | None,
    clock: Clock | None = None,
    send: Callable[..., None] = send_email,
    smtp_factory: SmtpFactory | None = None,
) -> NotifyState:
    """跑一天的推送：日报一封 + 即时告警若干。返回台账（已落盘）。

    - `daily`：(主题, 正文)；None = 今天没有日报要发（告警照发）。
    - `alerts`：(kind, 主题, 正文) 列表；同日同种告警已 sent 的跳过（去重）。
    - 日报按内容去重：台账里已 sent 且内容（主题+正文）未变就不再发
      （重跑调度不轰炸收件箱）；内容变了（下一槽位的 X/Y 更新）或上次
      notify_failed 的，本次照发/重试。
    - **任何 SMTP 失败都不抛**：记 notify_failed（UT-19-03）。
      会抛的只有台账文件损坏这类数据问题（NotifyError）。
    """
    clk = clock if clock is not None else SystemClock()
    day_str = day if isinstance(day, str) else day.isoformat()
    state = load_notify_state(data_root, day_str) or NotifyState(date=day_str)
    now = clk.now().isoformat()

    if daily is not None:
        digest = _digest(daily[0], daily[1])
        if state.daily is not None and state.daily.status == "sent" and state.daily.digest == digest:
            pass  # 内容没变：已送达的那封就是最新结论，不重复打扰
        elif mail is None:
            state.daily = NotifyRecord("skipped", now, "SMTP 未配置（缺 GEOPUBLISH_SMTP_*）",
                                       digest=digest)
        else:
            try:
                send(mail, daily[0], daily[1], smtp_factory=smtp_factory)
            except Exception as exc:  # noqa: BLE001 - 推送失败不炸调度，记账即可
                state.daily = NotifyRecord("notify_failed", now, f"{type(exc).__name__}: {exc}",
                                           digest=digest)
            else:
                state.daily = NotifyRecord("sent", now, digest=digest)

    for kind, subject, body in alerts:
        if state.alert_sent(kind):
            continue  # 同日同种告警只发一次：告警轰炸 = 告警失效
        if mail is None:
            state.alerts.append(AlertRecord("skipped", now, "SMTP 未配置（缺 GEOPUBLISH_SMTP_*）",
                                            kind=kind, detail=subject))
            continue
        try:
            send(mail, subject, body, smtp_factory=smtp_factory)
        except Exception as exc:  # noqa: BLE001 - 同上：告警失败也是台账里的一行
            state.alerts.append(
                AlertRecord("notify_failed", now, f"{type(exc).__name__}: {exc}",
                            kind=kind, detail=subject)
            )
        else:
            state.alerts.append(AlertRecord("sent", now, kind=kind, detail=subject))

    save_notify_state(data_root, state)
    return state
