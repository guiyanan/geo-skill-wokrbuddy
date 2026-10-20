"""T-02 · 幂等键与状态机（UT-02-01 ~ UT-02-05）。

本文件先于实现存在：先跑红，再写 geopublish/core/state.py。

断言里的合法转换表是从 PRD R9 / 实施方案 T-02 抄下来的**独立副本**，
不引用实现里的常量 —— 否则实现写错、测试跟着错，等于没测。
"""

import itertools
import re

import pytest

from geopublish.core.state import (
    ALLOWED_TRANSITIONS,
    IllegalTransition,
    Status,
    idem_key,
    recover_status,
    transition,
)

pytestmark = pytest.mark.unit

CSDN_URL = "https://blog.csdn.net/joto_ai/article/details/145012345"

# 规格白名单：pending→submitting→published / failed / needs_human
EXPECTED_TRANSITIONS = {
    Status.PENDING: {Status.SUBMITTING, Status.FAILED, Status.NEEDS_HUMAN},
    Status.SUBMITTING: {Status.PUBLISHED, Status.FAILED, Status.NEEDS_HUMAN},
    Status.FAILED: {Status.PENDING, Status.SUBMITTING, Status.NEEDS_HUMAN},
    Status.NEEDS_HUMAN: {Status.PENDING},
    Status.PUBLISHED: set(),
}


# ---------------------------------------------------------------- UT-02-01
def test_ut_02_01_same_inputs_produce_same_key():
    a = idem_key("csdn", "joto_ai", "deepparser-pdf-extraction", "9f2c" * 16)
    b = idem_key("csdn", "joto_ai", "deepparser-pdf-extraction", "9f2c" * 16)

    assert a == b
    assert re.fullmatch(r"[0-9a-f]{64}", a), "幂等键必须是 64 位小写 hex"


# ---------------------------------------------------------------- UT-02-02
def test_ut_02_02_one_char_content_change_changes_key():
    base = idem_key("csdn", "joto_ai", "deepparser-pdf-extraction", "9f2c" * 16)
    # 正文改一个字 → content_sha 变 → 幂等键必须变，否则新稿会被误判为重发
    edited_sha = __import__("hashlib").sha256("支持 13 种格式".encode()).hexdigest()
    edited = idem_key("csdn", "joto_ai", "deepparser-pdf-extraction", edited_sha)

    assert base != edited


def test_idem_key_is_stable_across_platform_account_normalisation():
    """平台名/账号大小写、前后空格不应产生两个不同的键（防手滑重发）。"""
    a = idem_key("CSDN", " Joto_AI ", "deepparser-pdf-extraction", "9f2c" * 16)
    b = idem_key("csdn", "joto_ai", "deepparser-pdf-extraction", "9f2c" * 16)

    assert a == b


@pytest.mark.parametrize("field", ["platform", "account", "slug", "content_sha"])
def test_idem_key_rejects_blank_component(field):
    parts = {
        "platform": "csdn",
        "account": "joto_ai",
        "slug": "deepparser-pdf-extraction",
        "content_sha": "9f2c" * 16,
    }
    parts[field] = "   "

    with pytest.raises(ValueError) as exc:
        idem_key(**parts)

    assert field in str(exc.value), "报错必须点名是哪个字段为空"


def test_idem_key_fields_do_not_bleed_into_each_other():
    """防止拼接歧义：'a|b' + 'c' 与 'a' + 'b|c' 必须不同（加分隔/长度前缀）。"""
    x = idem_key("csdn", "joto", "a-b", "cc")
    y = idem_key("csdn", "joto-a", "b", "cc")

    assert x != y


# ---------------------------------------------------------------- UT-02-03
def test_ut_02_03_published_is_terminal():
    assert ALLOWED_TRANSITIONS[Status.PUBLISHED] == frozenset()

    with pytest.raises(IllegalTransition) as exc:
        transition(Status.PUBLISHED, Status.SUBMITTING)

    msg = str(exc.value)
    assert "published" in msg and "submitting" in msg, "报错要带出当前态与目标态"

    for target in Status:
        if target is Status.PUBLISHED:
            continue
        with pytest.raises(IllegalTransition):
            transition(Status.PUBLISHED, target)


# ---------------------------------------------------------------- UT-02-04
@pytest.mark.parametrize("url", ["", "   ", None])
def test_ut_02_04_submitting_without_url_goes_needs_human(url):
    """崩溃恢复：submitting 且没拿到 URL → 必须交人工，绝不回 pending 重发。"""
    recovered = recover_status(Status.SUBMITTING, url=url)

    assert recovered is Status.NEEDS_HUMAN
    assert recovered is not Status.PENDING


# ---------------------------------------------------------------- UT-02-05
def test_ut_02_05_submitting_with_url_is_published():
    assert recover_status(Status.SUBMITTING, url=CSDN_URL) is Status.PUBLISHED


@pytest.mark.parametrize("status", [Status.PENDING, Status.FAILED, Status.NEEDS_HUMAN])
def test_recover_status_leaves_other_states_untouched(status):
    assert recover_status(status, url="") is status


def test_recover_status_keeps_published_published():
    assert recover_status(Status.PUBLISHED, url=CSDN_URL) is Status.PUBLISHED


# ------------------------------------------------- 状态机全矩阵（100% 分支）
@pytest.mark.parametrize(
    "current,target", list(itertools.product(Status, Status)), ids=str
)
def test_transition_matrix_matches_spec(current, target):
    allowed = target in EXPECTED_TRANSITIONS[current] or target is current

    if allowed:
        assert transition(current, target) is target
    else:
        with pytest.raises(IllegalTransition):
            transition(current, target)


def test_transition_rejects_unknown_status():
    with pytest.raises(IllegalTransition):
        transition(Status.PENDING, "pubished")


def test_transition_accepts_str_status():
    """日志/CSV 里读回来的是字符串，转换入口要能吃。"""
    assert transition("pending", "submitting") is Status.SUBMITTING


def test_needs_human_never_jumps_straight_to_published():
    with pytest.raises(IllegalTransition):
        transition(Status.NEEDS_HUMAN, Status.PUBLISHED)


# ------------------------------------------------- 状态机辅助面（覆盖补齐）
def test_only_published_is_terminal():
    assert Status.PUBLISHED.is_terminal
    for status in (Status.PENDING, Status.SUBMITTING, Status.FAILED, Status.NEEDS_HUMAN):
        assert not status.is_terminal, f"{status} 不应是终态"


def test_status_str_is_the_log_value():
    """日志里落的是 pending 这种裸值，不是 Status.PENDING。"""
    assert str(Status.NEEDS_HUMAN) == "needs_human"
    assert Status("submitting") is Status.SUBMITTING


def test_missing_log_reads_as_empty(tmp_path):
    from geopublish.core.state import PublishLog

    log = PublishLog(tmp_path / "nope" / "publish-log.csv")

    assert log.entries() == []
    assert log.last_status("a" * 64) is None


def test_status_history_in_order(tmp_path):
    """状态轨迹要能读出来：submitting 有没有落盘，是排障第一现场。"""
    from geopublish.core.state import PublishLog

    log = PublishLog(tmp_path / "publish-log.csv")
    log.append(make_entry(status=Status.PENDING))
    log.append(make_entry(status=Status.SUBMITTING))
    log.append(make_entry(status=Status.PUBLISHED))

    assert list(log.statuses("a" * 64)) == [
        Status.PENDING,
        Status.SUBMITTING,
        Status.PUBLISHED,
    ]


def test_append_row_accepts_plain_dict(tmp_path):
    """编排层直接给 dict 也要能写（内部先校验再落盘）。"""
    from geopublish.core.state import PublishLog

    log = PublishLog(tmp_path / "publish-log.csv")
    log.append_row(make_entry(status=Status.SUBMITTING).to_row())

    assert log.entries()[0].status is Status.SUBMITTING


def make_entry(*, status: Status):
    from datetime import datetime, timezone

    from geopublish.core.models import LogEntry

    return LogEntry(
        ts=datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc),
        platform="csdn",
        account="joto_ai",
        slug="deepparser-table-extraction",
        idem_key="a" * 64,
        status=status,
    )
