"""T-19 邮件推送（PRD R18 推送要求）单元测试。

覆盖：UT-19-03（推送失败不影响发布状态，记 notify_failed）、
UT-19-06/07 的告警送达与去重、SMTP 配置解析、SSL/STARTTLS 两条发送路径、
推送台账的读写与原子落盘。
"""

from __future__ import annotations

import json
import smtplib
from datetime import date
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.notify import (
    ENV_SMTP_HOST,
    ENV_SMTP_PASSWORD,
    ENV_SMTP_PORT,
    ENV_SMTP_SENDER,
    ENV_SMTP_USER,
    AlertRecord,
    NotifyConfig,
    NotifyError,
    NotifyRecord,
    NotifyState,
    load_notify_state,
    notify_day,
    notify_state_path,
    save_notify_state,
    send_email,
)

pytestmark = pytest.mark.unit

DAY = "2026-10-27"
NOW = "2026-10-27T10:05:00+08:00"

SMTP_ENV = {
    ENV_SMTP_HOST: "smtp.example.com",
    ENV_SMTP_USER: "bot@example.com",
    ENV_SMTP_PASSWORD: "授权码不是密码",
    ENV_SMTP_SENDER: "bot@example.com",
}


# ---------------------------------------------------------------- fakes
class FakeSMTP:
    """记录全部调用的假 SMTP：覆盖登录/STARTTLS/发送，不开真连接。"""

    instances: list[FakeSMTP] = []
    fail_on: str = ""  # login / send_message / connect

    def __init__(self, host, port, timeout=None):
        self.host, self.port, self.timeout = host, port, timeout
        self.calls: list[tuple] = []
        FakeSMTP.instances.append(self)
        if FakeSMTP.fail_on == "connect":
            raise OSError("连不上")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        self.calls.append(("starttls",))

    def login(self, user, password):
        self.calls.append(("login", user, password))
        if FakeSMTP.fail_on == "login":
            raise smtplib.SMTPAuthenticationError(535, b"auth failed")

    def send_message(self, msg):
        self.calls.append(("send_message", msg))
        if FakeSMTP.fail_on == "send_message":
            raise smtplib.SMTPException("发送被拒")


@pytest.fixture(autouse=True)
def _reset_fake_smtp():
    FakeSMTP.instances = []
    FakeSMTP.fail_on = ""
    yield
    FakeSMTP.fail_on = ""


def make_config(**over) -> NotifyConfig:
    kwargs = {
        "host": "smtp.example.com",
        "port": 465,
        "user": "bot@example.com",
        "password": "secret",
        "sender": "bot@example.com",
        "recipients": ("shiyimi@example.com",),
    }
    kwargs.update(over)
    return NotifyConfig(**kwargs)


# ---------------------------------------------------------------- 配置解析
class TestNotifyConfigFromEnv:
    def test_missing_vars_reported_all_at_once(self):
        with pytest.raises(NotifyError, match="GEOPUBLISH_SMTP_HOST") as exc:
            NotifyConfig.from_env({}, recipients="a@x.com")
        for name in (ENV_SMTP_HOST, ENV_SMTP_USER, ENV_SMTP_PASSWORD, ENV_SMTP_SENDER):
            assert name in str(exc.value), "缺项一次报全，不留「修一个又冒一个」"

    def test_blank_values_count_as_missing(self):
        env = {k: "  " for k in SMTP_ENV}
        with pytest.raises(NotifyError, match="缺少环境变量"):
            NotifyConfig.from_env(env, recipients="a@x.com")

    def test_default_port_is_465_ssl(self):
        cfg = NotifyConfig.from_env(SMTP_ENV, recipients="a@x.com")
        assert cfg.port == 465 and cfg.use_ssl is True

    def test_explicit_port_587_is_starttls(self):
        cfg = NotifyConfig.from_env({**SMTP_ENV, ENV_SMTP_PORT: "587"}, recipients="a@x.com")
        assert cfg.port == 587 and cfg.use_ssl is False

    def test_bad_port_rejected(self):
        with pytest.raises(NotifyError, match="必须是整数"):
            NotifyConfig.from_env({**SMTP_ENV, ENV_SMTP_PORT: "abc"}, recipients="a@x.com")
        with pytest.raises(NotifyError, match="越界"):
            NotifyConfig.from_env({**SMTP_ENV, ENV_SMTP_PORT: "70000"}, recipients="a@x.com")

    def test_recipients_string_split_and_cleaned(self):
        cfg = NotifyConfig.from_env(SMTP_ENV, recipients="a@x.com, , b@y.com")
        assert cfg.recipients == ("a@x.com", "b@y.com")

    def test_recipients_sequence_passthrough(self):
        cfg = NotifyConfig.from_env(SMTP_ENV, recipients=["a@x.com"])
        assert cfg.recipients == ("a@x.com",)

    def test_no_recipients_rejected(self):
        with pytest.raises(NotifyError, match="收件人不能为空"):
            NotifyConfig.from_env(SMTP_ENV, recipients=" , ")

    def test_bad_recipient_rejected(self):
        with pytest.raises(NotifyError, match="不是合法邮箱"):
            NotifyConfig.from_env(SMTP_ENV, recipients="not-an-email")

    def test_env_defaults_to_os_environ(self, monkeypatch):
        for k, v in SMTP_ENV.items():
            monkeypatch.setenv(k, v)
        cfg = NotifyConfig.from_env(recipients="a@x.com")
        assert cfg.host == "smtp.example.com"


# ---------------------------------------------------------------- 发送
class TestSendEmail:
    def test_ssl_path_login_and_send(self):
        cfg = make_config()
        send_email(cfg, "主题", "正文", smtp_factory=lambda: FakeSMTP("h", 1))
        server = FakeSMTP.instances[0]
        kinds = [c[0] for c in server.calls]
        assert kinds == ["login", "send_message"], "SSL 直连不走 STARTTLS"
        msg = server.calls[-1][1]
        assert msg["Subject"] == "主题"
        assert msg["From"] == "bot@example.com"
        assert msg["To"] == "shiyimi@example.com"
        assert "正文" in msg.get_content()

    def test_starttls_path(self):
        cfg = make_config(port=587)
        send_email(cfg, "s", "b", smtp_factory=lambda: FakeSMTP("h", 1))
        kinds = [c[0] for c in FakeSMTP.instances[0].calls]
        assert kinds == ["starttls", "login", "send_message"]

    def test_default_factory_ssl(self, monkeypatch):
        monkeypatch.setattr(smtplib, "SMTP_SSL", FakeSMTP)
        send_email(make_config(), "s", "b")
        server = FakeSMTP.instances[0]
        assert (server.host, server.port, server.timeout) == ("smtp.example.com", 465, 30)

    def test_default_factory_starttls(self, monkeypatch):
        monkeypatch.setattr(smtplib, "SMTP", FakeSMTP)
        send_email(make_config(port=587), "s", "b")
        kinds = [c[0] for c in FakeSMTP.instances[0].calls]
        assert kinds[0] == "starttls"

    def test_smtp_failure_propagates(self):
        """发送失败原样抛 —— 记不记 notify_failed 是调用方的事，这里不吞。"""
        FakeSMTP.fail_on = "login"
        with pytest.raises(smtplib.SMTPAuthenticationError):
            send_email(make_config(), "s", "b", smtp_factory=lambda: FakeSMTP("h", 1))


# ---------------------------------------------------------------- 台账读写
class TestNotifyStateIO:
    def test_roundtrip(self, tmp_path):
        state = NotifyState(
            date=DAY,
            daily=NotifyRecord("sent", NOW, digest="abc"),
            alerts=[AlertRecord("sent", NOW, kind="needs_human", detail="主题行")],
        )
        save_notify_state(tmp_path, state)
        loaded = load_notify_state(tmp_path, DAY)
        assert loaded is not None
        assert loaded.daily is not None and loaded.daily.status == "sent"
        assert loaded.daily.digest == "abc"
        assert loaded.alerts[0].kind == "needs_human"
        assert loaded.alert_sent("needs_human") is True
        assert loaded.alert_sent("zero_streak") is False

    def test_missing_returns_none(self, tmp_path):
        assert load_notify_state(tmp_path, DAY) is None

    def test_corrupt_json_rejected(self, tmp_path):
        path = notify_state_path(tmp_path, DAY)
        path.parent.mkdir(parents=True)
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(NotifyError, match="合法 JSON"):
            load_notify_state(tmp_path, DAY)

    def test_schema_mismatch_rejected(self, tmp_path):
        path = notify_state_path(tmp_path, DAY)
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"schema": 99, "date": DAY}), encoding="utf-8")
        with pytest.raises(NotifyError, match="schema"):
            load_notify_state(tmp_path, DAY)

    def test_missing_fields_rejected(self, tmp_path):
        with pytest.raises(NotifyError, match="缺字段：date"):
            NotifyState.from_json({"schema": 1}, where="测试")
        with pytest.raises(NotifyError, match="缺字段：status/at"):
            NotifyRecord.from_json({}, where="测试")
        with pytest.raises(NotifyError, match="缺字段：kind"):
            AlertRecord.from_json({"status": "sent", "at": NOW}, where="测试")

    def test_state_without_daily(self, tmp_path):
        state = NotifyState.from_json({"schema": 1, "date": DAY, "daily": None}, where="测试")
        assert state.daily is None

    def test_path_accepts_date_object(self, tmp_path):
        assert notify_state_path(tmp_path, date(2026, 10, 27)).name == f"{DAY}.notify.json"

    def test_replace_failure_cleans_tmp(self, tmp_path, monkeypatch):
        import geopublish.core.notify as notify_mod

        def boom(*args, **kwargs):
            raise OSError("磁盘满")

        monkeypatch.setattr(notify_mod.os, "replace", boom)
        with pytest.raises(OSError, match="磁盘满"):
            save_notify_state(tmp_path, NotifyState(date=DAY))
        parent = tmp_path / "reports"
        assert [p.name for p in parent.iterdir()] == [], "不留 .tmp 残骸"


# ---------------------------------------------------------------- 一天的推送
def fake_send_ok(calls):
    def _send(cfg, subject, body, *, smtp_factory=None):
        calls.append((subject, body))

    return _send


def fake_send_boom(cfg, subject, body, *, smtp_factory=None):
    raise OSError("SMTP 服务器拒了")


class TestNotifyDay:
    def test_daily_sent_and_recorded(self, tmp_path):
        calls: list[tuple] = []
        state = notify_day(
            tmp_path, DAY,
            daily=("[JOTO GEO] 10-27 发布 1/1 · csdn", "正文"),
            mail=make_config(),
            clock=FakeClock(NOW),
            send=fake_send_ok(calls),
        )
        assert calls == [("[JOTO GEO] 10-27 发布 1/1 · csdn", "正文")]
        assert state.daily is not None and state.daily.status == "sent"
        on_disk = load_notify_state(tmp_path, DAY)
        assert on_disk is not None and on_disk.daily is not None
        assert on_disk.daily.status == "sent"

    def test_same_content_not_resent(self, tmp_path):
        """重跑调度不重复轰炸收件箱：内容没变，已送达的那封就是最新结论。"""
        calls: list[tuple] = []
        kwargs = dict(daily=("同一主题", "同一正文"), mail=make_config(),
                      clock=FakeClock(NOW), send=fake_send_ok(calls))
        notify_day(tmp_path, DAY, **kwargs)
        state = notify_day(tmp_path, DAY, **kwargs)
        assert len(calls) == 1
        assert state.daily is not None and state.daily.status == "sent"

    def test_changed_content_resent(self, tmp_path):
        """T-18b 多槽位：15:00 的日报 X/Y 变了，就该再发一封。"""
        calls: list[tuple] = []
        send = fake_send_ok(calls)
        notify_day(tmp_path, DAY, daily=("主题", "10:00 的内容"), mail=make_config(),
                   clock=FakeClock(NOW), send=send)
        notify_day(tmp_path, DAY, daily=("主题", "15:00 的内容"), mail=make_config(),
                   clock=FakeClock(NOW), send=send)
        assert len(calls) == 2

    def test_previous_failure_retried(self, tmp_path):
        calls: list[tuple] = []
        notify_day(tmp_path, DAY, daily=("主题", "正文"), mail=make_config(),
                   clock=FakeClock(NOW), send=fake_send_boom)
        notify_day(tmp_path, DAY, daily=("主题", "正文"), mail=make_config(),
                   clock=FakeClock(NOW), send=fake_send_ok(calls))
        assert calls == [("主题", "正文")], "上次 notify_failed 的本次重试"
        state = load_notify_state(tmp_path, DAY)
        assert state is not None and state.daily is not None and state.daily.status == "sent"

    def test_ut_19_03_failure_recorded_not_raised(self, tmp_path):
        """UT-19-03：推送失败 → 记 notify_failed，不抛、不碰发布状态。"""
        state = notify_day(
            tmp_path, DAY,
            daily=("主题", "正文"),
            mail=make_config(),
            clock=FakeClock(NOW),
            send=fake_send_boom,
        )
        assert state.daily is not None and state.daily.status == "notify_failed"
        assert "OSError" in state.daily.reason and "SMTP 服务器拒了" in state.daily.reason

    def test_mail_not_configured_marks_skipped(self, tmp_path):
        state = notify_day(
            tmp_path, DAY, daily=("主题", "正文"), mail=None, clock=FakeClock(NOW),
            send=fake_send_boom,  # 不该被调用
        )
        assert state.daily is not None and state.daily.status == "skipped"
        assert "SMTP 未配置" in state.daily.reason

    def test_alerts_sent_and_deduped(self, tmp_path):
        """UT-19-06/07 的送达面：告警走即时通道（独立邮件），同日同种只发一次。"""
        calls: list[tuple] = []
        alerts = [
            ("needs_human", "[JOTO GEO 告警] 10-27 needs_human 待处理（1 篇）", "明细"),
            ("zero_streak", "[JOTO GEO 告警] 10-27 已连续 2 日零发布", "原因"),
        ]
        kwargs = dict(daily=None, alerts=alerts, mail=make_config(),
                      clock=FakeClock(NOW), send=fake_send_ok(calls))
        state = notify_day(tmp_path, DAY, **kwargs)
        assert [c[0] for c in calls] == [a[1] for a in alerts], "告警与日报分开，各自一封"
        assert {a.kind for a in state.alerts} == {"needs_human", "zero_streak"}

        state2 = notify_day(tmp_path, DAY, **kwargs)
        assert len(calls) == 2, "同日同种告警只发一次（告警轰炸 = 告警失效）"
        assert len(state2.alerts) == 2

    def test_alert_failure_recorded_not_raised(self, tmp_path):
        state = notify_day(
            tmp_path, DAY, daily=None,
            alerts=[("needs_human", "主题", "正文")],
            mail=make_config(), clock=FakeClock(NOW), send=fake_send_boom,
        )
        assert state.alerts[0].status == "notify_failed"
        assert "OSError" in state.alerts[0].reason

    def test_alert_without_mail_marked_skipped(self, tmp_path):
        state = notify_day(
            tmp_path, DAY, daily=None,
            alerts=[("zero_streak", "主题", "正文")],
            mail=None, clock=FakeClock(NOW), send=fake_send_boom,
        )
        assert state.alerts[0].status == "skipped"
        assert state.alerts[0].detail == "主题"

    def test_daily_none_alerts_still_go(self, tmp_path):
        """调度没跑出日报的日子，告警照发（「没日报」本身就是异常）。"""
        calls: list[tuple] = []
        state = notify_day(
            tmp_path, DAY, daily=None,
            alerts=[("needs_human", "告警", "正文")],
            mail=make_config(), clock=FakeClock(NOW), send=fake_send_ok(calls),
        )
        assert state.daily is None
        assert calls == [("告警", "正文")]

    def test_accepts_date_object_and_default_clock(self, tmp_path):
        calls: list[tuple] = []
        state = notify_day(
            tmp_path, date(2026, 10, 27),
            daily=("主题", "正文"), mail=make_config(), send=fake_send_ok(calls),
        )
        assert state.date == DAY and calls, "缺省时钟 = 系统时钟（生产路径）"

    def test_corrupt_state_raises(self, tmp_path):
        path = notify_state_path(tmp_path, DAY)
        path.parent.mkdir(parents=True)
        path.write_text("{oops", encoding="utf-8")
        with pytest.raises(NotifyError):
            notify_day(tmp_path, DAY, daily=("s", "b"), mail=make_config(), clock=FakeClock(NOW))
