"""T-04 现场包：失败必须留得下、脱得干净、复现得出来。

对应的需求：R12「失败可诊断」+ 方案 §2.1/§2.2。

这个模块的价值不在「打包能跑通」，而在**它只在出事时才被执行**——
一年跑不了几次，所以每个边界都必须在测试里钉死，不能靠现场调试。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from geopublish.core.clock import FakeClock
from geopublish.core.fs import staged_dir
from geopublish.core.incident import (
    BUNDLE_FILES,
    BundleNotFound,
    Capture,
    Incident,
    IncidentError,
    IncompleteBundle,
    ReplayFailed,
    archive,
    load,
    replay,
    scrub,
    scrub_text,
)

CLOCK_ISO = "2026-10-21T10:00:00+08:00"

pytestmark = pytest.mark.unit


# ------------------------------------------------------------------ 夹具

def make_capture() -> Capture:
    return Capture(
        page_html="<html><body><h1>编辑器</h1></body></html>",
        screenshot=b"\x89PNG\r\n\x1a\n fake screenshot",
        network_har='{"log": {"entries": []}}',
        console_log="[error] Uncaught TypeError: cannot read 'value' of null",
    )


def make_incident(**overrides) -> Incident:
    base = dict(
        ticket="T-07",
        stage="inject",
        adapter="csdn",
        adapter_version="0.1.0",
        selectors_version="2026-10-21.1",
        prd_refs=("R7", "R12"),
        error="InjectFailed: 三级注入（keyboard / execCommand / paste）全部失败",
        payload={
            "slug": "deepparser-complex-tables",
            "title": "DeepParser 怎么处理复杂表格",
            "tags": ["PDF", "OCR"],
            "word_count": 1480,
        },
        capture=make_capture(),
    )
    base.update(overrides)
    return Incident(**base)


@pytest.fixture
def out_dir(tmp_path) -> Path:
    d = tmp_path / "incidents"
    d.mkdir()
    return d


# ------------------------------------------------- UT-04-01 七件套齐全

def test_archive_writes_all_seven_pieces(out_dir):
    """UT-04-01：打包后目录里必须正好有七件。"""
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))

    assert sorted(p.name for p in path.iterdir()) == sorted(BUNDLE_FILES)
    assert len(BUNDLE_FILES) == 7


def test_archive_rejects_incomplete_capture_and_leaves_nothing_behind(out_dir):
    """UT-04-01（反面）：截图没拿到 → 打包失败并说清缺哪件。

    宁可当场炸掉，也不要落一个「缺截图的现场包」——那种包 --replay 能加载，
    但复现出来的失败和当初不是同一个，等于把人引到错的方向。
    """
    incident = make_incident(capture=Capture(page_html="<html></html>", screenshot=b""))

    with pytest.raises(IncompleteBundle) as exc:
        archive(incident, out_dir, clock=FakeClock(CLOCK_ISO))

    assert "screenshot.png" in str(exc.value)
    assert list(out_dir.iterdir()) == [], "失败的打包不许留下半成品目录"


def test_bundle_json_carries_what_a_debugger_needs(out_dir):
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))
    bundle = json.loads((path / "bundle.json").read_text(encoding="utf-8"))

    assert bundle["ticket"] == "T-07"
    assert bundle["stage"] == "inject"
    assert bundle["adapter"] == "csdn"
    assert bundle["selectors_version"] == "2026-10-21.1"
    assert bundle["prd_refs"] == ["R7", "R12"]
    assert "InjectFailed" in bundle["error"]
    assert bundle["files"] == list(BUNDLE_FILES)
    assert bundle["created_at"].startswith("2026-10-21T10:00:00")


# ------------------------------------------------------ UT-04-02 脱敏

def test_input_json_is_scrubbed(out_dir):
    """UT-04-02：cookie / token / 手机号都不许落进现场包。"""
    incident = make_incident(
        payload={
            "slug": "x-y",
            "title": "DeepParser 实战",
            "cookie": "sessionid=abc123secret; uid=42",
            "headers": {"Authorization": "Bearer ghp_realtoken1234", "X-Trace": "ok"},
            "account": {"phone": "13812345678", "email": "shi@example.com"},
            "cover_url": "https://cdn.example.com/a.png?token=tok_live_9f8&v=2",
            "note": "临时用 sk-live-abcdef123456 调试",
            "word_count": 1480,
            "tags": ["PDF", "OCR"],
        }
    )
    path = archive(incident, out_dir, clock=FakeClock(CLOCK_ISO))
    raw = (path / "input.json").read_text(encoding="utf-8")

    for secret in (
        "abc123secret",
        "ghp_realtoken1234",
        "13812345678",
        "shi@example.com",
        "tok_live_9f8",
        "sk-live-abcdef123456",
    ):
        assert secret not in raw, f"{secret} 漏进 input.json 了"

    # 脱敏不是删字段：还要能看出「当时确实带了这个东西」
    data = json.loads(raw)
    assert data["cookie"] == "***"
    assert data["headers"]["Authorization"] == "***"
    assert data["headers"]["X-Trace"] == "ok"
    assert data["account"]["phone"] == "***"
    assert data["title"] == "DeepParser 实战"
    assert data["tags"] == ["PDF", "OCR"]
    assert data["word_count"] == 1480


def test_scrub_masks_phone_but_keeps_it_recognisable():
    """手机号保留前 3 后 4：排障时要能对照「是不是这个账号」。"""
    assert scrub_text("联系 13812345678 处理") == "联系 138****5678 处理"


def test_scrub_keeps_ordinary_numbers_untouched():
    """正文里的数字、版本号、时间戳都不是手机号，不许误伤。"""
    text = "支持 12 种格式，v1.2.3 发布于 20261021，耗时 1380ms，样本 1234567890 条"
    assert scrub_text(text) == text


def test_bundle_json_does_not_leak_secrets_from_error_text(out_dir):
    """异常消息里经常带着 URL（含 token），它进的是 bundle.json，也要洗。"""
    incident = make_incident(
        error="Timeout 30s at https://mp.csdn.net/edit?token=tok_live_deadbeef&id=9 "
        "Authorization: Bearer ghp_abcdefghijkl"
    )
    path = archive(incident, out_dir, clock=FakeClock(CLOCK_ISO))
    raw = (path / "bundle.json").read_text(encoding="utf-8")

    assert "tok_live_deadbeef" not in raw
    assert "ghp_abcdefghijkl" not in raw
    assert "mp.csdn.net/edit" in raw, "URL 本身要留着，那是线索"


def test_scrub_is_recursive_and_does_not_alias_input():
    payload = {"a": {"b": [{"token": "t0psecret"}]}, "keep": "v"}
    cleaned = scrub(payload)

    assert cleaned["a"]["b"][0]["token"] == "***"
    assert cleaned["keep"] == "v"
    assert payload["a"]["b"][0]["token"] == "t0psecret", "不许改调用方传进来的对象"


# ------------------------------------- UT-04-03 repro.sh 可执行且必然失败

def test_repro_script_is_executable_and_exits_non_zero(out_dir, tmp_path):
    """UT-04-03：一键复现脚本能直接跑，而且**必然**非 0（因为本来就要复现失败）。"""
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))
    script = path / "repro.sh"

    assert script.exists()
    assert os.access(script, os.X_OK), "repro.sh 必须带可执行位"

    proc = subprocess.run(
        [str(script)],
        cwd=str(tmp_path),  # 故意不在包目录里跑，验证脚本自己找得到
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode != 0, "复现脚本成功了，说明现场包没复现出问题"
    assert proc.returncode == 4, "复现失败要用专用退出码 4，别和用法错误 1 混在一起"
    combined = proc.stdout + proc.stderr
    assert "T-07" in combined and "inject" in combined
    assert "InjectFailed" in combined


def test_repro_script_does_not_embed_secrets(out_dir):
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))
    text = (path / "repro.sh").read_text(encoding="utf-8")

    assert "token" not in text.lower().replace("${", "")  # 只允许变量引用，不许写字面量
    assert len(text.splitlines()) <= 12, "复现脚本要短到能一眼看完"


def test_repro_script_distinguishes_env_problem_from_not_reproduced(out_dir):
    """环境坏了必须退 5，不能退 1 假装「没复现」。

    这条是纪律的支点：`复现不出来就不许改代码`。如果环境问题也表现为「没复现」，
    那这条纪律就退化成「看谁嘴硬」。
    """
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))

    proc = subprocess.run(
        [str(path / "repro.sh")],
        capture_output=True, text=True, timeout=60,
        env={**os.environ, "GEOPUBLISH_PY": "/bin/false"},
    )

    assert proc.returncode == 5
    assert "geopublish 不可用" in proc.stderr


# --------------------------------------- UT-04-04 目录名可检索

def test_dir_name_contains_ts_ticket_and_stage(out_dir):
    """UT-04-04：目录名要能一眼定位「谁、哪个阶段、什么时候」。"""
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))

    assert path.name == "20261021_100000_T-07_inject"
    assert path.parent == out_dir


def test_hostile_ticket_cannot_escape_out_dir(out_dir):
    """ticket 来自外部输入，不能靠它跳出 incidents 目录。"""
    incident = make_incident(ticket="T-07/../../etc", stage="../passwd")
    path = archive(incident, out_dir, clock=FakeClock(CLOCK_ISO))

    assert path.parent == out_dir
    assert "/" not in path.name
    assert path.resolve().is_relative_to(out_dir.resolve())


def test_same_second_same_stage_does_not_overwrite(out_dir):
    """同一秒同一阶段连炸两次（重试场景），第二次不许盖掉第一次的现场。"""
    clock = FakeClock(CLOCK_ISO)
    first = archive(make_incident(error="第一次"), out_dir, clock=clock)
    second = archive(make_incident(error="第二次"), out_dir, clock=clock)

    assert first != second
    assert second.name.startswith(first.name)
    assert "第一次" in (first / "bundle.json").read_text(encoding="utf-8")
    assert "第二次" in (second / "bundle.json").read_text(encoding="utf-8")


# ------------------------------------------------ 加载与回放（DoD）

def test_load_roundtrip(out_dir):
    original = make_incident()
    path = archive(original, out_dir, clock=FakeClock(CLOCK_ISO))
    loaded = load(path)

    assert loaded.ticket == original.ticket
    assert loaded.stage == original.stage
    assert loaded.error == original.error
    assert loaded.prd_refs == original.prd_refs
    assert loaded.capture == original.capture


def test_load_rejects_bundle_missing_a_file(out_dir):
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))
    (path / "network.har").unlink()

    with pytest.raises(IncompleteBundle) as exc:
        load(path)
    assert "network.har" in str(exc.value)


def test_load_rejects_missing_dir(tmp_path):
    with pytest.raises(BundleNotFound):
        load(tmp_path / "不存在")


def test_load_rejects_broken_bundle_json(out_dir):
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))
    (path / "bundle.json").write_text("{ 不是 json", encoding="utf-8")

    with pytest.raises(IncidentError) as exc:
        load(path)
    assert "bundle.json" in str(exc.value)


def test_load_rejects_bundle_missing_required_keys(out_dir):
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))
    data = json.loads((path / "bundle.json").read_text(encoding="utf-8"))
    del data["stage"]
    (path / "bundle.json").write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(IncidentError) as exc:
        load(path)
    assert "stage" in str(exc.value)


def test_replay_raises_with_stage_and_ticket(out_dir):
    path = archive(make_incident(), out_dir, clock=FakeClock(CLOCK_ISO))

    with pytest.raises(ReplayFailed) as exc:
        replay(path)
    message = str(exc.value)
    assert "T-07" in message and "inject" in message
    assert "InjectFailed" in message


# ------------------------------------------------------- 构造期防御

@pytest.mark.parametrize("field", ["ticket", "stage", "adapter", "error"])
def test_incident_requires_text_fields(field):
    with pytest.raises(IncidentError):
        make_incident(**{field: "  "})


def test_incident_rejects_non_mapping_payload():
    with pytest.raises(IncidentError):
        make_incident(payload=["不是", "字典"])


def test_bundle_files_order_is_stable():
    """顺序进 bundle.json 的 files 字段，改顺序等于改格式。"""
    assert BUNDLE_FILES[0] == "bundle.json"
    assert BUNDLE_FILES[-1] == "repro.sh"
    assert "input.json" in BUNDLE_FILES


def test_artifact_rejects_unknown_name():
    with pytest.raises(IncidentError) as exc:
        make_capture().artifact("../../etc/passwd")
    assert "未知的 artifact" in str(exc.value)


def test_incident_rejects_wrong_capture_type():
    """传了 dict 当 capture 就别硬写：写到一半才发现会很难查。"""
    with pytest.raises(IncidentError) as exc:
        make_incident(capture={"page_html": "<html></html>"})
    assert "capture" in str(exc.value)


def test_staged_dir_refuses_to_overwrite(tmp_path):
    """两个现场包绝不允许合并成一个目录 —— 那会把两次失败的证据混在一起。"""
    (tmp_path / "已有").mkdir()

    with pytest.raises(FileExistsError):
        with staged_dir(tmp_path / "已有"):
            pass


def test_payload_may_contain_paths_and_timestamps(out_dir):
    """真实失败钩子交上来的 payload 一定有 Path / datetime。

    这两种值 json 序列化不了。**在出事的路径上再抛一个异常**，等于把唯一能用的
    证据也弄丢 —— 所以构造期就转成字符串。
    """
    from datetime import date, datetime, timezone

    incident = make_incident(
        payload={
            "draft_path": Path("/data/drafts/x/body.md"),
            "when": datetime(2026, 10, 21, 10, 0, tzinfo=timezone.utc),
            "day": date(2026, 10, 21),
            "screenshot": b"\x89PNG binary",
            "attempt": 2,
            "dry_run": False,
            "nothing": None,
            "weird": object(),
        }
    )
    path = archive(incident, out_dir, clock=FakeClock(CLOCK_ISO))
    data = json.loads((path / "input.json").read_text(encoding="utf-8"))

    assert data["draft_path"] == "/data/drafts/x/body.md"
    assert data["when"].startswith("2026-10-21T10:00:00")
    assert data["day"] == "2026-10-21"
    assert data["screenshot"] == "<11 bytes>", "二进制只留长度，别把图片塞进 input.json"
    assert data["attempt"] == 2 and data["dry_run"] is False and data["nothing"] is None


def test_archive_rejects_out_dir_that_is_a_file(tmp_path):
    """报错要说「这是个文件」，而不是 MKdir 的 EEXIST。"""
    target = tmp_path / "incidents"
    target.write_text("我不是目录", encoding="utf-8")

    with pytest.raises(IncidentError) as exc:
        archive(make_incident(), target, clock=FakeClock(CLOCK_ISO))
    assert "不是目录" in str(exc.value)


def test_chinese_ticket_keeps_its_reading(out_dir):
    """中文阶段名是检索现场包的主要线索，不能被冲成 unknown。"""
    path = archive(
        make_incident(ticket="工单7", stage="注入"), out_dir, clock=FakeClock(CLOCK_ISO)
    )

    assert path.name == "20261021_100000_工单7_注入"


def test_absurdly_long_ticket_does_not_break_the_filesystem(out_dir):
    """500 字的 ticket 会撞 ENAMETOOLONG —— 截断，但仍然保留可读前缀。"""
    path = archive(make_incident(ticket="T" * 500), out_dir, clock=FakeClock(CLOCK_ISO))

    assert len(path.name) < 120
    assert path.name.startswith("20261021_100000_TTTT")
