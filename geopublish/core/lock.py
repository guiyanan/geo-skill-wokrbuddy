"""平台级并发锁（PRD R9 / 架构 §3.5，T-11 落地）。

用 `fcntl.flock` 而不是「存在即锁」的 pid 文件，原因只有一个：
flock 跟着文件描述符走，进程一死内核就释放 —— 不存在「进程崩了、
锁文件留下、从此谁也跑不动」这个经典事故。锁文件里写的 pid/时间只是
**给人看的线索**，不是锁的权威；权威永远是 flock 本身。

stale 检测（`inspect_lock`）的意义由此而来：能读到死进程的 pid 说明
上一家崩了，但 flock 已经随尸体释放 —— 本次直接接管，**不需要人删文件**。
`stale_after` 处理的是另一种可疑：持有者进程还活着、锁却持有了远超一次
发布（<5 分钟，架构 §5）的时长 —— 只标记，不抢锁。抢活进程的锁等于
两只浏览器同时操作一个账号，那才是真正的灾难。
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator

__all__ = [
    "DEFAULT_LOCK_ROOT",
    "DEFAULT_STALE_AFTER",
    "LockError",
    "LockInfo",
    "inspect_lock",
    "lock_path",
    "platform_lock",
]

logger = logging.getLogger("geopublish.lock")

DEFAULT_LOCK_ROOT = Path.home() / ".geo-publish" / "locks"

# 一次发布端到端 <5 分钟（IT-10-01 实测 25s）。持有超过这个时长的锁，
# 持有者大概率卡死了 —— 标记为 stale 供人排查，但不自动抢（见模块 docstring）。
DEFAULT_STALE_AFTER = 600.0

_LOCK_MODE = 0o600  # 锁文件不给别人读：里面有 pid，也算是运行痕迹


class LockError(RuntimeError):
    """锁被另一个活进程持有。信息里必须说清楚是谁占着，否则排查靠猜。"""

    def __init__(self, path: Path, holder: LockInfo | None) -> None:
        self.path = path
        self.holder = holder
        if holder is not None and holder.pid is not None:
            detail = f"持有者 pid={holder.pid}（{'存活' if holder.alive else '已死'}）"
        else:
            detail = "锁被持有但读不到持有者信息"
        super().__init__(f"平台锁被占用：{path}（{detail}）。等它跑完，别双开浏览器")


@dataclass(frozen=True, slots=True)
class LockInfo:
    """一把锁的现状。`stale=True` 只意味着「可疑/可接管」，不意味着能抢。"""

    pid: int | None
    acquired_at: float | None  # epoch 秒；锁文件里读不到就为 None
    alive: bool
    stale: bool


def lock_path(platform: str, *, lock_root: str | Path | None = None) -> Path:
    """某个平台的锁文件。平台名小写，与 config 的 `key` 口径一致。"""
    root = Path(lock_root).expanduser() if lock_root is not None else DEFAULT_LOCK_ROOT
    name = str(platform).strip().lower()
    if not name:
        raise ValueError("平台名不能为空：锁是按平台划的，匿名锁等于没锁")
    return root / f"{name}.lock"


def inspect_lock(
    path: str | Path,
    *,
    stale_after: float = DEFAULT_STALE_AFTER,
    now: float | None = None,
) -> LockInfo | None:
    """读一把锁的持有者信息。文件不存在/为空 → None（没人在跑）。

    `now` 可注入：stale 判定不该依赖测试跑得快慢。
    """
    p = Path(path)
    if not p.is_file():
        return None
    holder = _read_holder(p)
    if holder is None:
        return None
    pid = holder.get("pid")
    acquired_at = holder.get("acquired_at")
    alive = _pid_alive(pid) if isinstance(pid, int) else False
    age = None
    if isinstance(acquired_at, (int, float)):
        age = (now if now is not None else time.time()) - acquired_at
    stale = (not alive) or (age is not None and age > stale_after)
    return LockInfo(
        pid=pid if isinstance(pid, int) else None,
        acquired_at=acquired_at if isinstance(acquired_at, (int, float)) else None,
        alive=alive,
        stale=stale,
    )


@contextmanager
def platform_lock(
    platform: str,
    *,
    lock_root: str | Path | None = None,
    stale_after: float = DEFAULT_STALE_AFTER,
) -> Iterator[Path]:
    """拿一把平台锁进入临界区；拿不到 → LockError；出来必释放。

    拿锁成功时若发现文件里躺着死进程的 pid，记一条 info 日志（接管痕迹），
    然后覆写成自己的。释放时清空文件再 UNLOCK —— 下一个 `inspect_lock`
    读到空文件就知道没人在跑，而不是读到一具尸体。
    """
    path = lock_path(platform, lock_root=lock_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, _LOCK_MODE)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # flock 失败只有一种含义：持有者活着。stale 残留不可能挡路
            # （进程一死内核就放了），所以这里不用重试、不用强抢。
            raise LockError(path, inspect_lock(path, stale_after=stale_after)) from None

        prior = inspect_lock(path, stale_after=stale_after)
        if prior is not None and not prior.alive:
            logger.info("接管残留锁（pid=%s 已死）：%s", prior.pid, path)

        _write_holder(fd, {"pid": os.getpid(), "acquired_at": time.time()})
        try:
            yield path
        finally:
            _write_holder(fd, None)  # 清空再解锁：不留尸体给下一家读
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _read_holder(path: Path) -> dict[str, Any] | None:
    """读锁文件里的持有者 JSON。空文件/乱码都按「没有持有者」处理。"""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def _write_holder(fd: int, holder: dict[str, Any] | None) -> None:
    """整段覆写持有者信息并 fsync：半写的 holder 比没有 holder 更会误导人。"""
    payload = (json.dumps(holder) + "\n").encode("utf-8") if holder is not None else b""
    os.ftruncate(fd, 0)
    os.lseek(fd, 0, os.SEEK_SET)
    if payload:
        os.write(fd, payload)
    os.fsync(fd)


def _pid_alive(pid: int) -> bool:
    """进程是否活着。EPERM = 活着但归别人管（比如 sudo 起的），也算活。"""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
