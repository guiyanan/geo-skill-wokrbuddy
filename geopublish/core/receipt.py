"""回执落盘（T-10 / R10）。

一次真实发布成功后要留三样东西：`receipt.json`、before/after 两张截图、
`publish-log.csv` 追加一行。截图由编排层（publish.py）截，这里管回执文件和日志行。

两条硬规矩：

1. **回执失败不掩盖发布成功**（UT-10-03）。文章已经发出去了，receipt.json 写不进去
   （磁盘满、权限坏）是另一件事：日志行照写，note 记 `receipt_failed`，调用方拿到的
   结论仍是「发成了」。把「存证失败」说成「发布失败」，会让人去平台重发一篇——
   那才是真正的重复发布事故。
2. **时间不读系统钟**（UT-10-04）。`Receipt.published_at` 和 `LogEntry.ts` 都由
   调用方用注入的 Clock 算好再传进来，本模块一个 now() 都不调。

不做的事：不写 csv 的并发锁（T-11）、不写失败分类（T-12）。
"""

from __future__ import annotations

import dataclasses
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from .models import LogEntry, Receipt
from .state import PublishLog

__all__ = ["RECEIPT_NAME", "ReceiptRecord", "save_receipt"]

logger = logging.getLogger("geopublish.receipt")

RECEIPT_NAME = "receipt.json"

# 回执写失败时 LogEntry.note 的固定取值。日报/排查靠这个词检索，不许换说法。
RECEIPT_FAILED = "receipt_failed"


@dataclass(frozen=True, slots=True)
class ReceiptRecord:
    """一次回执落盘的结果。

    `receipt_path=None` 且 `note=receipt_failed` 就是「发成了但没存上证」——
    它与「没发成」是两种完全不同的后续动作，所以用字段区分，不压进 bool。
    """

    receipt_path: Path | None
    note: str = ""

    @property
    def failed(self) -> bool:
        return self.note == RECEIPT_FAILED


def _write_receipt_json(path: Path, receipt: Receipt) -> Path:
    """写 receipt.json：整份一次写完 + fsync，不留半份文件（与 PublishLog 同一规格）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(receipt.to_json(), ensure_ascii=False, indent=2) + "\n"
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    return path


def save_receipt(
    receipt: Receipt,
    run_dir: str | Path,
    publish_log: PublishLog,
    entry: LogEntry,
) -> ReceiptRecord:
    """落一份回执 + 追加一行日志。

    顺序是「先尝试 receipt.json，再写 csv」：receipt 失败时 csv 那行的 note
    必须已经带上 receipt_failed。csv 本身写失败（比如磁盘真的满了）就让它抛——
    两个存证都写不进去时，没有任何本地动作能挽回，喊得越大声越好。
    """
    note = ""
    receipt_path: Path | None = None
    try:
        receipt_path = _write_receipt_json(Path(run_dir) / RECEIPT_NAME, receipt)
    except OSError as exc:
        # 只兜 OS 层失败（磁盘满/权限/路径被文件占用）。Receipt 本身的模型错误
        # 是程序 bug，不该被吞成 receipt_failed。
        note = RECEIPT_FAILED
        logger.warning("receipt.json 写不进去（%s）：发布成功状态不变，单记 %s", exc, note)

    if note:
        # frozen dataclass：replace 走 __post_init__，列校验不会绕开
        entry = dataclasses.replace(entry, note=note)
    publish_log.append(entry)

    return ReceiptRecord(receipt_path=receipt_path, note=note)
