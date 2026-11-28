"""并发登记的幂等性 —— 同一指纹被并发登记时，注册表里只能有一行。

这个文件钉住什么
----------------
* **8 个线程同时登记同一个指纹** → 注册表**一行**、命中**八行**、事件**八行**、
  没有异常抛出。「同一失败签名在全系统只产生一个修复 plan」这条不变式如果只在
  单线程下成立，它其实没有成立过：调度 tick 与验证子进程本来就是并发写的，
  而且写的不是同一个进程。
* **注册表行、命中行与事件行在同一个事务里** → 数据库忙到重试用尽时，抛错
  且**什么都不写**。半截的登记比没有登记更坏：读的人看见一行
  ``status='open'``，会以为这个缺陷已经有人在修了。
* **忙是有界的** → 重试次数用尽之后异常**原样冒出来**，不被吞掉：调用方必须
  知道自己这次登记没有发生。

为什么这些是*集成*测试
----------------------
唯一约束、事务边界与忙重试 —— 三者都是**真实 SQLite 文件**的属性，不是替身
的属性。数据库由共享的 ``state_db`` fixture 提供：迁移过的、位于该测试自己的
``tmp_path`` 里的文件，永远不是运维那台机器上的 ``state.db``。一个用替身
数据库跑出来的并发测试证明不了任何事 —— 替身没有锁。

两个计数读的是数据库本身（``SELECT COUNT(*)``），而不是注册表的读路径：读路径
自己出错的时候，靠它来判定「写对了」等于没判定。
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from framework_fix.registry import (
    FrameworkFixRegistry,
    FrameworkFixRegistryError,
)


#: 并发登记的线程数。8 是任务规格给的数字：足够让 SQLite 的写锁真正排队，
#: 又不至于把一次测试拖成一次压测。
THREADS = 8

#: 每个表一条字面量查询 —— 这个文件不拿变量拼 SQL。
_COUNT_QUERIES = {
    "framework_fix_registry": "SELECT COUNT(*) FROM framework_fix_registry",
    "framework_fix_hit": "SELECT COUNT(*) FROM framework_fix_hit",
    "framework_fix_event": "SELECT COUNT(*) FROM framework_fix_event",
}


def _hit(**overrides: Any) -> Dict[str, Any]:
    """一次命中 —— 8 个线程登记的是**同一份**事实，所以指纹必然相同。"""
    hit: Dict[str, Any] = {
        "plan_id": "plan-a",
        "vp_id": "VP-3",
        "framework_files": ["backend/framework_fix/registry.py"],
        "error_class": "AssertionError",
        "first_error_line": "AssertionError: expected 1, got 2",
    }
    hit.update(overrides)
    return hit


def _count(db_path: Path, table: str) -> int:
    """某个表有几行 —— 直接问数据库。"""
    query = _COUNT_QUERIES[table]
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(query).fetchone()[0])
    finally:
        conn.close()


class _CountingRegistry(FrameworkFixRegistry):
    """数着「试了几次」的注册表，并且能在第 N 次尝试时放开写锁。

    放锁的时机由测试定：第一次尝试时锁还攥着，所以第一次**必然**失败（写锁
    排队，SQLite 直接给 SQLITE_BUSY）。于是「第二次尝试才成功」不是运气，
    是构造出来的。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.attempts = 0
        self.release_after_attempts = 2
        self.on_attempt: Optional[Callable[[], None]] = None
        super().__init__(*args, **kwargs)
        # 构造期那次连接是迁移检查，不是一次登记尝试。
        self.attempts = 0

    def _connect(self) -> sqlite3.Connection:
        conn = super()._connect()
        self.attempts += 1
        if (
            self.on_attempt is not None
            and self.attempts >= self.release_after_attempts
        ):
            callback, self.on_attempt = self.on_attempt, None
            callback()
        return conn


def _register_concurrently(
    registry: FrameworkFixRegistry,
    hit: Dict[str, Any],
    threads: int = THREADS,
) -> List[BaseException]:
    """让 ``threads`` 个线程**同时**登记同一个指纹，返回它们抛出的异常。

    用 ``Barrier`` 而不是让线程各走各的：没有屏障的话，先到的线程可能在后到的
    线程还没开始之前就把事务做完了 —— 那测的是顺序，不是并发。屏障把 8 个线程
    一起放出去，写锁才会真的排队。
    """
    errors: List[BaseException] = []
    barrier = threading.Barrier(threads)

    def worker() -> None:
        try:
            barrier.wait(timeout=30)
            registry.register_hit(hit)
        except BaseException as exc:  # noqa: BLE001 — 异常本身就是被断言的东西
            errors.append(exc)

    workers = [threading.Thread(target=worker) for _ in range(threads)]
    for worker_thread in workers:
        worker_thread.start()
    for worker_thread in workers:
        worker_thread.join(timeout=60)
    assert all(not thread.is_alive() for thread in workers), (
        "a registration thread did not finish; the test would otherwise read "
        "a half-written database and call it a result"
    )
    return errors


def test_concurrent_register(state_db: Path) -> None:
    """8 个线程登记同一个指纹 → 注册表一行、命中八行、不抛异常。"""
    registry = FrameworkFixRegistry(db_path=state_db)

    errors = _register_concurrently(registry, _hit())

    assert not errors, (
        f"concurrent registration must not raise; got {errors!r}"
    )

    rows = registry.list_registry()
    assert len(rows) == 1, (
        "one fingerprint must produce exactly one registry row — two rows "
        f"means two repair plans for the same defect; got {len(rows)}"
    )
    assert rows[0]["status"] == "open", (
        f"a freshly registered defect is open; got {rows[0]['status']!r}"
    )
    assert rows[0]["fix_plan_id"] == "plan-a"

    assert _count(state_db, "framework_fix_registry") == 1, (
        "the database itself must hold exactly one registry row"
    )
    assert _count(state_db, "framework_fix_hit") == THREADS, (
        "every concurrent hit is still a hit — dedup belongs to the registry "
        "row, not to the hit history"
    )


def test_concurrent_events(state_db: Path) -> None:
    """事件数与命中数一致 —— 审计链不能少写一条。"""
    registry = FrameworkFixRegistry(db_path=state_db)

    errors = _register_concurrently(registry, _hit())

    assert not errors, f"concurrent registration must not raise; got {errors!r}"

    hits = _count(state_db, "framework_fix_hit")
    events = _count(state_db, "framework_fix_event")

    assert hits == THREADS
    assert events == hits, (
        "every hit must leave exactly one audit event; a hit without an event "
        f"is a fact the audit chain does not know — hits={hits}, events={events}"
    )


def test_a_silently_ignored_write_is_caught_by_the_read_back(state_db: Path) -> None:
    """``INSERT OR IGNORE`` 的盲区：它把**任何**约束冲突都变成「没写进去」。

    给注册表加一条「一个修复计划只能登记一个缺陷」的唯一索引（一个不合法
    的形状，但正好落在 ``OR IGNORE`` 的盲区里），第二次登记就会撞上它：写入
    被静默忽略，注册表里没有这一行。回读必须发现这件事并报错 —— 否则这次
    登记就是一次**说谎**：调用方拿着一个指纹回来，而这个指纹根本没有登记过。
    """
    conn = sqlite3.connect(str(state_db))
    try:
        conn.execute(
            "CREATE UNIQUE INDEX idx_one_defect_per_plan "
            "ON framework_fix_registry(fix_plan_id)"
        )
        conn.commit()
    finally:
        conn.close()

    registry = FrameworkFixRegistry(db_path=state_db)
    registry.register_hit(_hit())

    with pytest.raises(FrameworkFixRegistryError):
        registry.register_hit(
            _hit(framework_files=["backend/framework_fix/other.py"])
        )

    rows = registry.list_registry()
    assert len(rows) == 1, (
        "the failed registration must leave the registry as it was; got "
        f"{len(rows)} rows"
    )
    assert _count(state_db, "framework_fix_hit") == 1, (
        "the failed registration must not leave a hit row behind either"
    )
    assert _count(state_db, "framework_fix_event") == 1


def test_busy_database_raises_after_bounded_retries(state_db: Path) -> None:
    """数据库忙 → 有限重试后抛错，且一个字都没写进去。

    「有限」是重点：无界重试会把一次忙变成一次挂起；「不吞异常」也是重点：
    吞掉它，调用方就会以为这次登记发生了。
    """
    registry = FrameworkFixRegistry(
        db_path=state_db,
        busy_timeout_ms=50,
        busy_retry_attempts=3,
        busy_retry_backoff=0.01,
    )

    blocker = sqlite3.connect(str(state_db), isolation_level=None)
    try:
        # 另一个写者占住写锁：注册表的每一次尝试都会拿到 SQLITE_BUSY。
        blocker.execute("BEGIN EXCLUSIVE")
        with pytest.raises(sqlite3.OperationalError) as excinfo:
            registry.register_hit(_hit())
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    message = str(excinfo.value).lower()
    assert "busy" in message or "locked" in message, (
        f"the busy error must reach the caller; got {excinfo.value!r}"
    )
    # 「attempt」是重试用尽那条路径才写得出来的词 —— 一次就失败的话，冒出来
    # 的是 SQLite 原话（"database is locked"），不带它。
    assert "attempt" in message, (
        "the error must come from the exhausted-retry path, not from a "
        f"single failed attempt; got {excinfo.value!r}"
    )

    # 事务边界：一次失败的登记不留任何痕迹。
    assert _count(state_db, "framework_fix_registry") == 0
    assert _count(state_db, "framework_fix_hit") == 0
    assert _count(state_db, "framework_fix_event") == 0


def test_busy_database_is_retried_until_the_lock_is_released(state_db: Path) -> None:
    """锁一放开，登记就该成功 —— 证明重试真的在重试。

    上一个用例证明「重试用尽会抛错」；这个用例证明「在用尽之前它真的会再试」。
    两者的区别是实质的：只抛错不重试的实现在第一个用例里照样是绿的。
    """
    registry = _CountingRegistry(
        db_path=state_db,
        busy_timeout_ms=50,
        busy_retry_attempts=10,
        busy_retry_backoff=0.01,
    )

    blocker = sqlite3.connect(str(state_db), isolation_level=None)
    blocker.execute("BEGIN EXCLUSIVE")

    def release() -> None:
        blocker.execute("ROLLBACK")

    registry.release_after_attempts = 2
    registry.on_attempt = release
    try:
        fingerprint = registry.register_hit(_hit())
    finally:
        blocker.close()

    assert fingerprint.startswith("sha256:")
    assert registry.attempts >= 2, (
        "the first attempt must fail while the lock is held, so the "
        f"registration must have taken more than one attempt; took "
        f"{registry.attempts}"
    )
    assert _count(state_db, "framework_fix_registry") == 1
    assert _count(state_db, "framework_fix_hit") == 1
    assert _count(state_db, "framework_fix_event") == 1

