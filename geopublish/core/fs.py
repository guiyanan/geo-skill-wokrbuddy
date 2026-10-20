"""原子落盘：目录要么完整出现，要么根本不出现。

为什么要这个：现场包和 fixture 都是**事后证据**。写到一半崩掉（磁盘满、进程被杀、
artifact 其实没抓到）留下的半成品目录，`--replay` 能加载、`fixture_dom()` 能读，
但复现出来的失败跟当初不是同一个 —— 这比没有证据更糟，因为它会把人引到错的方向。

做法：先在同级临时目录里写全，校验通过了再 rename。同一文件系统内 rename 是原子的。
"""

from __future__ import annotations

import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

__all__ = ["staged_dir"]


@contextmanager
def staged_dir(final: Path) -> Iterator[Path]:
    """在 `final` 的临时邻居里写，正常退出时整体 rename 到 `final`。

    块内抛异常 → 临时目录整个删掉，`final` 不会出现。
    """
    final = Path(final)
    if final.exists():
        # 不覆盖：调用方应该先算出唯一名字。悄悄合并两个现场包是最坏的选项。
        raise FileExistsError(f"目标目录已存在，拒绝覆盖：{final}")

    final.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(dir=final.parent, prefix=f".{final.name}.", suffix=".partial"))
    try:
        yield staging
        os.replace(staging, final)
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
