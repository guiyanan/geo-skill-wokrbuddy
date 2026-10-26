"""T-11 并发锁（geopublish/core/lock.py）。

钉死的行为：
- flock 是权威：持有者活着 → LockError；持有者死了 → 锁随进程释放，直接接管
- 锁文件里的 pid/时间是诊断线索，不是锁本身
- 正常结束必须释放且清空文件，下一家进来读到的是「没人在跑」
- 并发互斥用真线程测：flock 按 open file description 隔离，同进程两次独立
  open 照样互斥 —— 这正好模拟两个进程，又不用起子进程拖慢单元测试
"""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path

import pytest

from geopublish.core.lock import (
    DEFAULT_STALE_AFTER,
    LockError,
    inspect_lock,
    lock_path,
    platform_lock,
)

pytestmark = pytest.mark.unit

# 一个保证不存在的 pid：取一个远超 pid_max 的值（macOS/Linux pid_max 远小于它）。
# 不玩「spawn 一个子进程再杀掉」的花样 —— pid 复用会让测试偶发绿/红。
DEAD_PID = 2**22


def _write_holder(path: Path, pid: int, acquired_at: float | None = None) -> None:
    """伪造一个持有者（模拟崩溃后残留的锁文件，没有 flock 跟着）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"pid": pid, "acquired_at": acquired_at or time.time()}),
        encoding="utf-8",
    )


# --------------------------------------------------------------- 路径与基本信息

def test_lock_path_is_per_platform_and_lowercase(tmp_path):
    assert lock_path("CSDN", lock_root=tmp_path) == tmp_path / "csdn.lock"
    assert lock_path("csdn", lock_root=tmp_path) == tmp_path / "csdn.lock"
    with pytest.raises(ValueError):
        lock_path("  ", lock_root=tmp_path)


def test_inspect_missing_or_empty_file_means_nobody(tmp_path):
    assert inspect_lock(tmp_path / "none.lock") is None
    empty = tmp_path / "empty.lock"
    empty.write_text("", encoding="utf-8")
    assert inspect_lock(empty) is None


def test_inspect_corrupt_file_is_stale_not_crash(tmp_path):
    """锁文件被写花了（半写/乱码）：按「读不到持有者」处理，不许抛异常挡住发布。

    flock 才是权威：文件内容读不出来只说明没线索，不等于有人在跑。
    """
    bad = tmp_path / "csdn.lock"
    bad.write_text("{ 这不是 json", encoding="utf-8")
    assert inspect_lock(bad) is None


# --------------------------------------------------------------- UT-11-05：释放与重进

def test_normal_run_releases_lock_and_second_run_enters(tmp_path):
    """UT-11-05：正常结束释放锁，二次运行可正常进入。"""
    with platform_lock("csdn", lock_root=tmp_path):
        info = inspect_lock(tmp_path / "csdn.lock")
        assert info is not None
        assert info.pid == os.getpid() and info.alive is True

    # 出来后：文件被清空（不留尸体），inspect 读到「没人在跑」
    assert inspect_lock(tmp_path / "csdn.lock") is None

    with platform_lock("csdn", lock_root=tmp_path):  # 二次运行顺利进入
        pass


def test_exception_inside_critical_section_still_releases(tmp_path):
    """临界区里炸了（比如 submit 抛错）锁照样释放 —— 否则一次失败永远锁死。"""
    with pytest.raises(RuntimeError):
        with platform_lock("csdn", lock_root=tmp_path):
            raise RuntimeError("浏览器炸了")

    assert inspect_lock(tmp_path / "csdn.lock") is None
    with platform_lock("csdn", lock_root=tmp_path):
        pass


# --------------------------------------------------------------- UT-11-02：互斥

def test_concurrent_holder_blocks_second_acquisition(tmp_path):
    """UT-11-02 锁语义层：锁被持有时第二个 acquire 立刻 LockError（不等待、不重试）。"""
    entered = threading.Event()
    release = threading.Event()
    second_result: list[str] = []

    def holder():
        with platform_lock("csdn", lock_root=tmp_path):
            entered.set()
            release.wait(5)

    def contender():
        entered.wait(5)
        try:
            with platform_lock("csdn", lock_root=tmp_path):
                second_result.append("acquired")
        except LockError:
            second_result.append("blocked")
        finally:
            release.set()

    threads = [threading.Thread(target=holder), threading.Thread(target=contender)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)

    assert second_result == ["blocked"], f"第二个进程必须被拦下，实际：{second_result}"
    for t in threads:
        assert not t.is_alive(), "线程没退出 = 锁可能死锁"


def test_lock_error_carries_holder_info(tmp_path):
    """LockError 必须说清是谁占着锁 —— 「锁被占用」四个字没法排查。"""
    _write_holder(tmp_path / "csdn.lock", pid=os.getpid())  # 活 pid 但不持 flock

    with platform_lock("csdn", lock_root=tmp_path):  # 先真拿上
        # 上面伪造的 holder 已被覆写成本进程；现在文件里就是当前进程
        try:
            with platform_lock("csdn", lock_root=tmp_path):
                pass
        except LockError as exc:
            assert exc.holder is not None
            assert exc.holder.pid == os.getpid()
            assert exc.holder.alive is True
            assert "存活" in str(exc) and "csdn.lock" in str(exc)
        else:  # pragma: no cover - 没拦住就是实现错了
            raise AssertionError("第二把锁不该拿到")


def test_lock_error_without_readable_holder_info(tmp_path):
    """持有者信息读不出来（别的实现写的锁/权限问题）：错误信息也要能落地。"""
    with platform_lock("csdn", lock_root=tmp_path):
        path = tmp_path / "csdn.lock"
        # 模拟「锁被持有但文件内容读不出 holder」：掏一个不存在的路径形态不可行
        # （inspect 读的是同一个文件），直接构造 LockError 验证文案分支。
        err = LockError(path, None)
        assert "读不到持有者信息" in str(err)


# --------------------------------------------------------------- UT-11-04：stale 接管

def test_stale_lock_file_from_dead_process_is_taken_over(tmp_path, caplog):
    """UT-11-04：锁文件残留但进程已死 → inspect 可检测（stale），acquire 直接接管。

    flock 语义下死进程的锁已被内核释放，残留的只是文件内容 —— 所以接管
    不需要人删文件，这正是选 flock 而不是 pid 文件的原因。
    """
    _write_holder(tmp_path / "csdn.lock", pid=DEAD_PID)

    info = inspect_lock(tmp_path / "csdn.lock")
    assert info is not None and info.alive is False and info.stale is True

    import logging

    with caplog.at_level(logging.INFO, logger="geopublish.lock"):
        with platform_lock("csdn", lock_root=tmp_path):
            taken = inspect_lock(tmp_path / "csdn.lock")
            assert taken is not None and taken.pid == os.getpid()

    assert any("接管残留锁" in r.message for r in caplog.records), "接管要留日志痕迹"


def test_stale_after_marks_long_held_lock_as_suspicious(tmp_path):
    """UT-11-04 的「stale lock 超时」：持有者活着但持有时长超阈值 → stale 标记。

    只标记，不抢锁 —— 抢活进程的锁 = 两只浏览器同时操作一个账号。
    """
    _write_holder(
        tmp_path / "csdn.lock",
        pid=os.getpid(),  # 活着
        acquired_at=time.time() - DEFAULT_STALE_AFTER - 1,  # 但持有了太久
    )
    info = inspect_lock(tmp_path / "csdn.lock")
    assert info is not None
    assert info.alive is True, "进程活着"
    assert info.stale is True, "超过 stale_after 就算可疑"

    # 注入 now 控制时钟：不超时不算 stale（stale 判定不依赖测试跑得多快）
    fresh_at = time.time()
    _write_holder(tmp_path / "csdn.lock", pid=os.getpid(), acquired_at=fresh_at)
    info2 = inspect_lock(tmp_path / "csdn.lock", now=fresh_at + 1)
    assert info2 is not None and info2.stale is False


def test_stale_lock_does_not_block_flock_authority(tmp_path):
    """残留锁文件（死 pid）不挡 acquire：flock 是权威，文件内容只是线索。"""
    _write_holder(tmp_path / "csdn.lock", pid=DEAD_PID)
    # 不经过 inspect，直接拿锁 —— 必须拿得到
    with platform_lock("csdn", lock_root=tmp_path):
        pass


# --------------------------------------------------------------- 边角：覆盖率也是契约

def test_holder_without_acquired_at_is_not_stale_by_age(tmp_path):
    """持有者 JSON 缺 acquired_at（别的实现写的锁）：没有年龄可算，只能按存活判。"""
    (tmp_path / "csdn.lock").write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
    info = inspect_lock(tmp_path / "csdn.lock")
    assert info is not None
    assert info.acquired_at is None and info.alive is True and info.stale is False


def test_read_holder_tolerates_oserror(tmp_path, monkeypatch):
    """文件读不动（权限/IO 错）按「没有持有者」处理，不许把异常漏给调用方。"""
    target = tmp_path / "csdn.lock"
    target.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        Path, "read_text",
        lambda self, **kw: (_ for _ in ()).throw(OSError("盘抖了一下")),
    )
    assert inspect_lock(target) is None


def test_pid_alive_treats_permission_error_as_alive(monkeypatch):
    """EPERM = 进程活着但归别人管（sudo 起的）—— 算活，不许当成尸体接管。"""
    from geopublish.core import lock as lock_mod

    monkeypatch.setattr(
        lock_mod.os, "kill",
        lambda pid, sig: (_ for _ in ()).throw(PermissionError("别人的进程")),
    )
    assert lock_mod._pid_alive(12345) is True
