"""T-02 · 数据模型与发布日志（UT-02-06）。

models.py 的职责是「让非法状态构造不出来」，不是只做数据搬运。
所以这里的断言重点在 **拒绝**：缺字段、假回执、sha 对不上，都必须当场炸。
"""

import hashlib
from datetime import datetime, timezone

import pytest

from geopublish.core.models import (
    LOG_COLUMNS,
    Draft,
    IncompleteLogEntry,
    LogEntry,
    Meta,
    Receipt,
)
from geopublish.core.state import Status

pytestmark = pytest.mark.unit

BODY = "# DeepParser 怎么解析复杂表格\n\n结论前置……\n"
TS = datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc)


def make_meta(**over) -> Meta:
    base = dict(
        slug="deepparser-table-extraction",
        title="DeepParser 如何解析复杂表格",
        summary="从版式分析到结构化输出",
        tags=("文档解析", "PDF", "OCR"),
        category="人工智能",
        cover="assets/cover.png",
        ai_involvement="assisted",
        anchor_query_id="S01",
        target_reader="企业 IT / 数据治理负责人",
        platforms=("csdn",),
    )
    base.update(over)
    return Meta(**base)


def make_entry(**over) -> LogEntry:
    base = dict(
        ts=TS,
        platform="csdn",
        account="joto_ai",
        slug="deepparser-table-extraction",
        idem_key="a" * 64,
        status=Status.PENDING,
    )
    base.update(over)
    return LogEntry(**base)


# ---------------------------------------------------------------- 模型基础
def test_meta_json_roundtrip():
    meta = make_meta()
    assert Meta.from_json(meta.to_json()) == meta


def test_meta_rejects_unknown_field():
    with pytest.raises(ValueError) as exc:
        Meta.from_json({**make_meta().to_json(), "titel": "拼错的键"})

    assert "titel" in str(exc.value), "拼错的键要报出来，不能静默丢弃"


def test_meta_rejects_bad_slug():
    for bad in ["", "  ", "DeepParser 表格", "../escape", "中文标题"]:
        with pytest.raises(ValueError):
            make_meta(slug=bad)


def test_draft_sha_is_derived_from_body():
    draft = Draft(meta=make_meta(), body=BODY)

    assert draft.content_sha == hashlib.sha256(BODY.encode("utf-8")).hexdigest()
    # 正文改一个字，sha 必须变（幂等键靠它）
    assert Draft(meta=make_meta(), body=BODY + "。").content_sha != draft.content_sha


def test_draft_rejects_meta_sha_mismatch():
    """meta 里的 content_sha 与正文不一致 → 说明稿件在落盘后被改过，不能发。"""
    stale = make_meta(content_sha="0" * 64)

    with pytest.raises(ValueError) as exc:
        Draft(meta=stale, body=BODY)

    assert "content_sha" in str(exc.value)


def test_draft_idem_key_follows_content():
    draft = Draft(meta=make_meta(), body=BODY)
    edited = Draft(meta=make_meta(), body=BODY + "追加一句。")

    assert draft.idem_key("csdn", "joto_ai") != edited.idem_key("csdn", "joto_ai")
    assert draft.idem_key("csdn", "joto_ai") == draft.idem_key("CSDN", " joto_ai ")


# ---------------------------------------------------------------- 回执
def test_receipt_requires_verified_url():
    """R8：没 verify 过的不算成功，所以「没有 url 的回执」构造不出来。"""
    for url in ["", "  ", None]:
        with pytest.raises(ValueError):
            Receipt(
                url=url,
                article_id="145012345",
                published_at=TS,
                screenshot="runs/20261021/after.png",
                content_sha="b" * 64,
            )


def test_receipt_requires_article_id():
    with pytest.raises(ValueError):
        Receipt(
            url="https://blog.csdn.net/joto_ai/article/details/145012345",
            article_id="",
            published_at=TS,
            screenshot="runs/20261021/after.png",
            content_sha="b" * 64,
        )


def test_receipt_indexed_at_starts_empty():
    receipt = Receipt(
        url="https://blog.csdn.net/joto_ai/article/details/145012345",
        article_id="145012345",
        published_at=TS,
        screenshot="runs/20261021/after.png",
        content_sha="b" * 64,
    )

    assert receipt.indexed_at is None
    assert receipt.status is Status.PUBLISHED
    assert Receipt.from_json(receipt.to_json()) == receipt


# ---------------------------------------------------------------- UT-02-06
def test_ut_02_06_missing_field_raises_and_names_it():
    row = make_entry().to_row()
    del row["idem_key"]

    with pytest.raises(IncompleteLogEntry) as exc:
        LogEntry.from_row(row)

    assert "idem_key" in str(exc.value)


def test_ut_02_06_append_bad_row_leaves_log_untouched(tmp_path):
    """写日志必须整行成功或整行不写，不能落半行脏数据。"""
    from geopublish.core.state import PublishLog

    log = PublishLog(tmp_path / "publish-log.csv")
    log.append(make_entry(status=Status.SUBMITTING))

    before = (tmp_path / "publish-log.csv").read_bytes()
    row = make_entry(status=Status.PUBLISHED).to_row()
    del row["status"]

    with pytest.raises(IncompleteLogEntry):
        log.append_row(row)

    assert (tmp_path / "publish-log.csv").read_bytes() == before, "失败时文件不许有任何变化"
    assert len(log.entries()) == 1


def test_log_row_rejects_extra_field():
    row = make_entry().to_row()
    row["artical_id"] = "145012345"

    with pytest.raises(IncompleteLogEntry) as exc:
        LogEntry.from_row(row)

    assert "artical_id" in str(exc.value)


# ---------------------------------------------------------------- 日志落地
def test_log_columns_frozen():
    """R10：列语义不可改，改列等于破坏历史日志。"""
    assert LOG_COLUMNS == (
        "ts",
        "platform",
        "account",
        "slug",
        "idem_key",
        "status",
        "url",
        "article_id",
        "screenshot",
        "indexed_at",
        "note",
    )


def test_log_roundtrip_preserves_entry(tmp_path):
    from geopublish.core.state import PublishLog

    path = tmp_path / "publish-log.csv"
    log = PublishLog(path)
    entry = make_entry(
        status=Status.PUBLISHED,
        url="https://blog.csdn.net/joto_ai/article/details/145012345",
        article_id="145012345",
        screenshot="runs/20261021/after.png",
        indexed_at=None,
        note="verify ok",
    )

    log.append(entry)

    assert log.entries() == [entry]
    assert path.read_text(encoding="utf-8").splitlines()[0].split(",") == list(LOG_COLUMNS)


def test_log_header_written_once(tmp_path):
    from geopublish.core.state import PublishLog

    path = tmp_path / "publish-log.csv"
    log = PublishLog(path)
    log.append(make_entry())
    log.append(make_entry(status=Status.SUBMITTING))

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0].startswith("ts,platform")
    assert sum(1 for ln in lines if ln.startswith("ts,platform")) == 1


def test_log_last_status_wins(tmp_path):
    from geopublish.core.state import PublishLog

    log = PublishLog(tmp_path / "publish-log.csv")
    log.append(make_entry(status=Status.SUBMITTING))
    log.append(make_entry(status=Status.PUBLISHED))
    log.append(make_entry(idem_key="b" * 64, status=Status.PENDING))

    assert log.last_status("a" * 64) is Status.PUBLISHED
    assert log.last_status("b" * 64) is Status.PENDING
    assert log.last_status("c" * 64) is None


def test_log_append_is_readable_by_other_process(tmp_path):
    """第二个进程打开同一文件也要能看到刚写的行（flush + fsync）。"""
    from geopublish.core.state import PublishLog

    path = tmp_path / "publish-log.csv"
    PublishLog(path).append(make_entry(status=Status.SUBMITTING))

    assert "submitting" in path.read_text(encoding="utf-8")
    assert PublishLog(path).entries()[0].status is Status.SUBMITTING


# ------------------------------------------------- 拒绝路径（时间与时区）
def test_log_entry_requires_aware_datetime():
    """不带时区的时间会让跨时区排序出错，直接拒绝。"""
    from datetime import datetime as dt

    with pytest.raises(ValueError) as exc:
        make_entry(ts=dt(2026, 10, 21, 10, 0))

    assert "时区" in str(exc.value)


def test_log_entry_rejects_non_datetime_ts():
    with pytest.raises(ValueError) as exc:
        make_entry(ts="2026-10-21T10:00:00+08:00")

    assert "datetime" in str(exc.value)


def test_log_entry_rejects_naive_indexed_at():
    from datetime import datetime as dt

    with pytest.raises(ValueError):
        make_entry(status=Status.PUBLISHED, indexed_at=dt(2026, 10, 22, 9, 0))


def test_log_entry_rejects_unknown_status_value():
    with pytest.raises(IncompleteLogEntry) as exc:
        make_entry(status="pubished")

    assert "status" in str(exc.value)


def test_from_row_rejects_blank_ts():
    row = make_entry().to_row()
    row["ts"] = "  "

    with pytest.raises(IncompleteLogEntry) as exc:
        LogEntry.from_row(row)

    assert "ts" in str(exc.value)


def test_from_row_rejects_unknown_status():
    row = make_entry().to_row()
    row["status"] = "done"

    with pytest.raises(IncompleteLogEntry) as exc:
        LogEntry.from_row(row)

    assert "done" in str(exc.value)


def test_meta_from_json_requires_slug_and_title():
    with pytest.raises(ValueError) as exc:
        Meta.from_json({"slug": "a-b"})

    assert "title" in str(exc.value)


def test_meta_from_json_accepts_minimal_payload():
    meta = Meta.from_json({"slug": "a-b", "title": "标题"})

    assert meta.tags == ()
    assert meta.platforms == ("csdn",)


def test_receipt_rejects_naive_indexed_at():
    from datetime import datetime as dt

    with pytest.raises(ValueError):
        Receipt(
            url="https://blog.csdn.net/joto_ai/article/details/145012345",
            article_id="145012345",
            published_at=TS,
            screenshot="runs/20261021/after.png",
            content_sha="b" * 64,
            indexed_at=dt(2026, 10, 22, 9, 0),
        )


def test_receipt_only_exists_for_published():
    with pytest.raises(ValueError) as exc:
        Receipt(
            url="https://blog.csdn.net/joto_ai/article/details/145012345",
            article_id="145012345",
            published_at=TS,
            screenshot="runs/20261021/after.png",
            content_sha="b" * 64,
            status=Status.NEEDS_HUMAN,
        )

    assert "published" in str(exc.value)


# ------------------------------------------------- 对抗探测补漏（全覆盖审计）

def test_meta_from_json_rejects_string_tags():
    """`"tags": "人工智能"` 会被 tuple() 拆成逐字符 —— 字符串不是数组，必须当场报错。"""
    from geopublish.core.models import ModelError

    with pytest.raises(ModelError) as exc:
        Meta.from_json({"slug": "x-y", "title": "t", "tags": "人工智能"})

    assert "tags" in str(exc.value)


def test_meta_from_json_rejects_non_string_tag_entries():
    from geopublish.core.models import ModelError

    with pytest.raises(ModelError) as exc:
        Meta.from_json({"slug": "x-y", "title": "t", "tags": ["ok", 3]})

    assert "tags" in str(exc.value)


def test_meta_from_json_rejects_string_platforms():
    from geopublish.core.models import ModelError

    with pytest.raises(ModelError) as exc:
        Meta.from_json({"slug": "x-y", "title": "t", "platforms": "csdn"})

    assert "platforms" in str(exc.value)


def test_from_row_bad_ts_raises_incomplete_not_raw_valueerror():
    """损坏日志行的 ts 必须映射成 IncompleteLogEntry ——
    裸 ValueError 会绕过 replay/load 的错误处理链，直接甩 traceback。"""
    row = {
        "ts": "not-a-date",
        "platform": "csdn",
        "account": "a",
        "slug": "s",
        "idem_key": "k",
        "status": "pending",
        "url": "",
        "article_id": "",
        "screenshot": "",
        "indexed_at": "",
        "note": "",
    }

    with pytest.raises(IncompleteLogEntry) as exc:
        LogEntry.from_row(row)

    assert "ts" in str(exc.value)


def test_from_row_bad_indexed_at_raises_incomplete():
    row = {
        "ts": "2026-10-21T10:00:00+08:00",
        "platform": "csdn",
        "account": "a",
        "slug": "s",
        "idem_key": "k",
        "status": "pending",
        "url": "",
        "article_id": "",
        "screenshot": "",
        "indexed_at": "garbage",
        "note": "",
    }

    with pytest.raises(IncompleteLogEntry):
        LogEntry.from_row(row)


def test_receipt_from_json_missing_required_fields_names_them():
    """缺 url 不该是 KeyError：receipt 回读的报错要能被日报/CLI 用人话接住。"""
    from geopublish.core.models import ModelError

    with pytest.raises(ModelError) as exc:
        Receipt.from_json({"article_id": "1", "published_at": "2026-10-21T10:00:00+08:00"})

    assert "url" in str(exc.value)
