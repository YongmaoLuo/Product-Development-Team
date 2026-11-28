"""框架缺陷注册表 —— 签名的落库与按指纹查询。

这个模块要解决什么
------------------
PRD 要求「同一失败签名在全系统只产生一个修复 plan」。要做到这一点，先得有一个
**跨 plan 共享的登记处**：这个签名是不是已经在修、被命中过几次、命中来自哪些
计划、这些事实按什么顺序发生过 —— 全都要落在一个进程之外的载体上。否则每个
plan 各自记一份，「同一个缺陷到底登记过没有」这个问题就没有答案，而没有答案
的问题最后只能靠人去翻日志。

载体是状态库里的五张表（架构决策点 2）：

* ``framework_fix_registry`` —— 每个规范指纹**一行**：这一行就是「这个缺陷此刻
  是什么状态」的答案；
* ``framework_fix_hit`` —— 每一次命中**一行**（同一个指纹可以命中很多次）；
* ``framework_fix_alias`` —— 语义复核判定为同一缺陷的指纹别名；
* ``framework_fix_attach`` —— 谁在等这个缺陷的修复（计划 / 验证点 / 轮次）；
* ``framework_fix_event`` —— 只追加的审计链。

五张表由 ``state_machine.db.schema`` 的迁移建好。**本模块不建表**：库没迁移就
明确报错。静默建表会让「这个库到底跑过迁移没有」这个问题永远没有答案，而且
建出来的表未必是生产那张 —— 一个没迁移的库上跑出来的注册表数据，生产环境根本
读不到，那比报错更糟。

三条不变式
----------
1. **同一指纹在任一时刻只有一行注册表记录。** 重复命中追加 ``hit`` 行，不追加
   注册表行 —— 否则「同一个缺陷两个修复 plan」就成立了，而这正是注册表要
   防住的那件事。这一条在**并发**下同样成立：唯一性由数据库的主键保证
   （``INSERT OR IGNORE`` 写，随后回读确认），不靠调用方的先后顺序。
2. **写操作单事务提交。** 「查表 + 插入注册表行 + 插入命中行 + 写首条事件」
   要么全成，要么全不成。只写了一半的注册表比没有注册表更坏：读的人看见一行
   ``status='open'``，会以为这个缺陷已经有人在修了。
3. **指纹是纯函数的产物。** 指纹由 ``framework_fix.fingerprint`` 算，本模块不
   重算、不改写 —— 两个模块各算一次，两次结果就会不一样，而「这是不是同一个
   缺陷」这个问题本身也就不可复算了。

状态词表
--------
注册表行的 ``status`` 有两个来源的写法，说的是**同一个**状态：

* 架构决策点 2 写的是 ``active | fixed | reopened``；
* 本层的对外契约把「刚登记、还没人修」读作 ``open``。

读的人只需要认一个词，所以库里统一写 ``open``；``active`` 作为同义写法被接受
（见 :func:`normalize_status`），这样两份文档里的任何一份都不会被当成非法值
拒掉。

包形态
------
与同包的 ``fingerprint.py`` / ``classifier.py`` / ``read_model.py`` 一致：
``backend/framework_fix/`` 是 PEP 420 的隐式命名空间包，没有 ``__init__.py``。
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence

try:  # pragma: no cover - 两条路径等价，取先成功的那条
    from config_paths import resolve_state_db_path
except ImportError:  # pragma: no cover
    from backend.config_paths import resolve_state_db_path  # type: ignore

try:  # pragma: no cover
    from framework.clock import utcnow_iso
except ImportError:  # pragma: no cover
    from backend.framework.clock import utcnow_iso  # type: ignore

try:  # pragma: no cover
    from framework_fix.classifier import DISPOSITIONS, REPRO_CLASSIFICATIONS
except ImportError:  # pragma: no cover
    from backend.framework_fix.classifier import (  # type: ignore
        DISPOSITIONS,
        REPRO_CLASSIFICATIONS,
    )

try:  # pragma: no cover
    from framework_fix.fingerprint import (
        canonical_key,
        compute_fingerprint,
        resolve_alias,
    )
except ImportError:  # pragma: no cover
    from backend.framework_fix.fingerprint import (  # type: ignore
        canonical_key,
        compute_fingerprint,
        resolve_alias,
    )

try:  # pragma: no cover
    from state_machine.db.connection import open as open_db
except ImportError:  # pragma: no cover
    from backend.state_machine.db.connection import open as open_db  # type: ignore

__all__ = [
    "ATTACH_STATUSES",
    "ATTACH_STATUS_BLOCKED",
    "ATTACH_STATUS_EXHAUSTED",
    "ATTACH_STATUS_REGISTERED",
    "DISPOSITION_ATTACH",
    "DISPOSITION_FIX_PLAN",
    "DISPOSITION_HITL",
    "DISPOSITION_LOCAL_CONTINUE",
    "DISPOSITION_REGISTER_FLAKE",
    "DISPOSITION_REGISTER_SIGNATURE",
    "FrameworkFixRegistry",
    "FrameworkFixRegistryError",
    "HIT_DISPOSITIONS",
    "REGISTRY_BUSY_RETRY_ATTEMPTS",
    "REGISTRY_BUSY_RETRY_BACKOFF",
    "REGISTRY_STATUSES",
    "REGISTRY_STATUS_ACTIVE",
    "REGISTRY_STATUS_FIXED",
    "REGISTRY_STATUS_OPEN",
    "REGISTRY_STATUS_REOPENED",
    "SchemaNotMigratedError",
    "normalize_status",
]


# ---------------------------------------------------------------------------
# 词表
# ---------------------------------------------------------------------------

#: 「缺陷开着，还没人修」—— 本层的对外写法。
REGISTRY_STATUS_OPEN = "open"

#: 「缺陷开着，还没人修」—— 架构决策点 2 的写法。与 :data:`REGISTRY_STATUS_OPEN`
#: 是同一个状态；库里统一写 ``open``。
REGISTRY_STATUS_ACTIVE = "active"

#: 「已修复合入」。
REGISTRY_STATUS_FIXED = "fixed"

#: 「修复合入后又命中」。
REGISTRY_STATUS_REOPENED = "reopened"

#: 注册表行的状态闭集。集合外的词是拼写错误，不是新语义。
REGISTRY_STATUSES = (
    REGISTRY_STATUS_OPEN,
    REGISTRY_STATUS_FIXED,
    REGISTRY_STATUS_REOPENED,
)

#: 同义写法 → 库里真正写的那一个。
_STATUS_ALIASES = {REGISTRY_STATUS_ACTIVE: REGISTRY_STATUS_OPEN}

#: 命中行 ``classification`` 的缺省值。「还没能下结论」是一个真实的结论，
#: 不是「不知道」—— 两者在表里必须分得开。
DEFAULT_CLASSIFICATION = "unclassified"

#: 命中行 ``disposition`` 的缺省值：只登记签名，不建修复 plan。架构决策点 2
#: 明确写了非阻塞命中只写 ``framework_fix_hit`` 并置
#: ``disposition='register_signature'``。
DEFAULT_DISPOSITION = "register_signature"

#: 命中行 ``disposition`` 的合法取值。
#:
#: 这里有**两套**词表，而且两套都在本仓里真实存在：架构决策点 2 给的是登记
#: 动作（``fix_plan`` / ``attach`` / ``register_signature`` /
#: ``register_flake`` / ``local_continue`` / ``hitl``），而
#: ``framework_fix.classifier`` 给的是**判定**（``blocking`` /
#: ``needs_repro`` / ``unclassified`` / ``exhausted`` / ``not_blocking``）。
#: 登记层不替调用方做判定→动作的映射，只把两套都收下；拼错的字（``blockng``）
#: 仍然会被拒。
#: 「为这次命中建了一个框架修复 plan」。
DISPOSITION_FIX_PLAN = "fix_plan"

#: 「挂到已经存在的框架修复 plan 上」（同一签名的第二次命中）。
DISPOSITION_ATTACH = "attach"

#: 「只登记签名」—— 归不了类、证据不全，先把签名记下来。
DISPOSITION_REGISTER_SIGNATURE = "register_signature"

#: 「登记为 flake」—— 复现不稳定，不是框架缺陷。
DISPOSITION_REGISTER_FLAKE = "register_flake"

#: 「本地继续」—— 干净 main 上复现不出来，只在 plan 分支复现。
DISPOSITION_LOCAL_CONTINUE = "local_continue"

#: 「升级问人」—— 连续多轮归不了类，交给人判断。
DISPOSITION_HITL = "hitl"

HIT_DISPOSITIONS = frozenset(
    {
        DISPOSITION_FIX_PLAN,
        DISPOSITION_ATTACH,
        DISPOSITION_REGISTER_SIGNATURE,
        DISPOSITION_REGISTER_FLAKE,
        DISPOSITION_LOCAL_CONTINUE,
        DISPOSITION_HITL,
    }
) | frozenset(DISPOSITIONS)

#: 仓库名未知时的占位。仓库是**运营字段**（决定复现排在哪、合并串行到哪），
#: 不参与指纹 —— 所以它缺失时不影响身份，只影响运营。
UNKNOWN_REPO = "unknown"

#: 本层读写的表。少了任何一张，库就没跑过框架修复迁移。
_REQUIRED_TABLES = frozenset(
    {
        "framework_fix_registry",
        "framework_fix_hit",
        "framework_fix_alias",
        "framework_fix_attach",
        "framework_fix_event",
    }
)

#: 字符串形态的假值。``"false"`` 在 Python 里是真值 —— 不认它，
#: ``blocking='false'`` 会被读成「阻塞」。
_FALSE_TOKENS = frozenset({"", "0", "false", "no", "none", "null", "off"})

#: 数据库忙时的**默认**重试次数。
#:
#: 为什么需要它：注册表会被调度 tick 与验证子进程同时写。SQLite 的写锁是排队的，
#: 排队期间后来的写者会拿到 ``SQLITE_BUSY``。一次忙是**正常的并发**，不是故障 ——
#: 直接把它抛出去，等于把「谁先写」变成了一次抛硬币：同一个缺陷会不会登记上，
#: 取决于那一刻谁抢到了锁。
#:
#: 为什么必须有界：无界重试把一次忙变成一次挂起。宁可有限次重试后把错误原样
#: 抛出去 —— 抛错是诚实的，挂起不是。
REGISTRY_BUSY_RETRY_ATTEMPTS = 5

#: 忙重试之间的基础退避（秒）。第 n 次重试等 ``n ×`` 这个值 —— 越等越久，
#: 给持锁者留出收尾的时间，而不是所有等待者一起再撞一次。
REGISTRY_BUSY_RETRY_BACKOFF = 0.05


# ---------------------------------------------------------------------------
# 挂接（attach）词表
# ---------------------------------------------------------------------------

#: 挂接行的状态：阻塞性命中 —— 这个计划/验证点**持有**执行，等修复合入。
#:
#: 阻塞与非阻塞的区别不是「谁更需要修」，而是「谁现在停着」：阻塞性命中
#: 让原计划停下来等修复，非阻塞命中只是登记了签名。
ATTACH_STATUS_BLOCKED = "blocked"

#: 挂接行的状态：非阻塞补挂 —— 只登记了签名，**不持有**执行，只等修复合入
#: 后重验。
ATTACH_STATUS_REGISTERED = "registered"

#: 挂接行的状态：轮次耗尽 —— 这条挂接**不再持有**执行，等人拍板。
#:
#: 耗尽是终态：同一个签名不再自动重试，所以这条挂接不能继续算进阻塞计数 ——
#: 否则那个计划会被一件已经不再等修复的事永久拖着。记录本身留着：挂接行是
#: 历史（「谁曾经等过这个修复」），耗尽不擦掉历史。
ATTACH_STATUS_EXHAUSTED = "exhausted"

#: 挂接行的状态闭集。集合外的词是拼写错误，不是新语义。
ATTACH_STATUSES = (
    ATTACH_STATUS_BLOCKED,
    ATTACH_STATUS_REGISTERED,
    ATTACH_STATUS_EXHAUSTED,
)


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------
class FrameworkFixRegistryError(RuntimeError):
    """注册表层的问题 —— 库的状态不对，或调用方给的东西不对。"""


class SchemaNotMigratedError(FrameworkFixRegistryError):
    """状态库还没跑框架修复迁移。

    单独一类而不是笼统的 ``RuntimeError``：调用方需要能区分「库没准备好」
    （该去跑迁移）和「调用方给错了东西」（该改代码）。这两件事的处理方式完全
    不同，混成一种错误就只会被 catch 掉然后忽略。
    """


def normalize_status(status: str) -> str:
    """把一个状态词归一成库里写的那一个。

    Args:
        status: 状态词。``active`` 是 ``open`` 的同义写法。

    Returns:
        归一后的状态词，取值在 :data:`REGISTRY_STATUSES` 内。

    Raises:
        ValueError: 状态词不在闭集里。未知状态是数据坏了 —— 把它悄悄当成
            「还开着」，会让一个已经修好的缺陷继续显示成待修。
    """
    if not isinstance(status, str) or not status.strip():
        raise ValueError("status must be a non-empty string")
    candidate = status.strip().lower()
    candidate = _STATUS_ALIASES.get(candidate, candidate)
    if candidate not in REGISTRY_STATUSES:
        raise ValueError(
            f"unknown registry status {status!r}; expected one of "
            f"{REGISTRY_STATUSES}"
        )
    return candidate


def _as_int_flag(value: Any) -> int:
    """把一个布尔事实读成 SQLite 里的 INTEGER（0 / 1）。"""
    if value is None:
        return 0
    if isinstance(value, bool):
        return 1 if value else 0
    if isinstance(value, (int, float)):
        return 1 if value else 0
    if isinstance(value, str):
        return 0 if value.strip().lower() in _FALSE_TOKENS else 1
    return 1 if value else 0


def _as_optional_int(value: Any, name: str) -> Optional[int]:
    """一个可空的整数列。"""
    if value is None:
        return None
    if isinstance(value, bool):
        raise TypeError(f"{name} must be an int or None; got a bool")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"{name} must be an int or None; got {value!r}"
        ) from exc


def _json_dumps(value: Any) -> str:
    """一行 JSON —— 列里存的是摘要，不是原文。"""
    return json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)


def _require_text(value: Any, name: str) -> str:
    """一个必填的非空字符串。"""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    return value.strip()


def _optional_text(value: Any, name: str) -> Optional[str]:
    """一个可空的字符串。

    ``None`` 与空串都读成「还没有」：PR 地址是开完 PR 才知道的，占槽的
    时候常常还没有 —— 但「空地址」和「没地址」在库里必须是一回事，
    否则读的人会以为地址被人抹掉了。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(
            f"{name} must be a string or None; got {type(value).__name__}"
        )
    text = value.strip()
    return text or None


def _positive_int(value: Any, name: str, default: int) -> int:
    """一个正整数；``None`` 表示用缺省值。

    「重试 0 次」不是一个有用的配置：它把重试机制整个关掉，而调用方以为自己
    还留着它。
    """
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int; got {type(value).__name__}")
    if value < 1:
        raise ValueError(f"{name} must be >= 1; got {value}")
    return value


def _non_negative_float(value: Any, name: str, default: float) -> float:
    """一个非负浮点数；``None`` 表示用缺省值。"""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{name} must be a number; got {type(value).__name__}")
    seconds = float(value)
    if seconds < 0:
        raise ValueError(f"{name} must be >= 0; got {value}")
    return seconds


def _optional_non_negative_int(value: Any, name: str) -> Optional[int]:
    """一个可空的非负整数；``None`` 表示「用连接工厂的缺省值」。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int or None; got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{name} must be >= 0; got {value}")
    return value


def _is_busy_error(exc: sqlite3.OperationalError) -> bool:
    """这个 ``OperationalError`` 是「库忙」而不是「库坏了」。

    SQLite 只有两种常见理由说忙：``SQLITE_BUSY``（有人占着写锁）与
    ``database is locked``（同上，另一种措辞）。别的 ``OperationalError``
    —— 没有那张表、SQL 写错了 —— 重试多少次都不会好，必须立刻原样抛出。
    """
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _as_vp_id(value: Any) -> str:
    """一个验证点 id。

    空字符串表示「整条计划挂起，没有具体验证点」—— 计划级失败没有
    验证点可指，但那个计划仍然在等修复。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise TypeError(f"vp_id must be a string; got {type(value).__name__}")
    return value.strip()


def _as_round_no(value: Any) -> int:
    """轮次 —— 一个非负整数。"""
    if isinstance(value, bool):
        raise TypeError("round_no must be an int; got a bool")
    if isinstance(value, int):
        number = value
    elif isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
    else:
        raise TypeError(f"round_no must be an int; got {value!r}")
    if number < 0:
        raise ValueError(f"round_no must not be negative; got {number!r}")
    return number


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
class FrameworkFixRegistry:
    """框架缺陷注册表 —— 签名的唯一登记处。

    Examples:
        >>> registry = FrameworkFixRegistry()               # doctest: +SKIP
        >>> fp = registry.register_hit({                   # doctest: +SKIP
        ...     'plan_id': 'p1',
        ...     'vp_id': 'VP-3',
        ...     'framework_files': ['a.py'],
        ...     'error_class': 'E',
        ...     'first_error_line': 'x',
        ... })
        >>> registry.lookup(fp)['status']                  # doctest: +SKIP
        'open'
    """

    def __init__(
        self,
        db_path: Any = None,
        *,
        busy_retry_attempts: Optional[int] = None,
        busy_retry_backoff: Optional[float] = None,
        busy_timeout_ms: Optional[int] = None,
    ) -> None:
        """打开（或指向）一个状态库。

        Args:
            db_path: 状态库文件。``None`` 表示「问应用自己现在用哪个库」——
                即 :func:`config_paths.resolve_state_db_path`。它尊重
                ``PDT_STATE_DB_PATH``，所以测试与运维改一处就够。
            busy_retry_attempts: 数据库忙时重试几次。缺省
                :data:`REGISTRY_BUSY_RETRY_ATTEMPTS`。``1`` 表示不重试。
            busy_retry_backoff: 两次重试之间的基础退避秒数。缺省
                :data:`REGISTRY_BUSY_RETRY_BACKOFF`。
            busy_timeout_ms: 单次尝试里 SQLite 自己等锁的上限（毫秒）。缺省
                ``None`` —— 用连接工厂给的值（5 秒）。把它调小是为了让
                「忙到重试用尽」这件事在有限时间里发生，而不是把调用方挂在
                一次 5 秒的等待上。

        Raises:
            SchemaNotMigratedError: 目标库还没跑框架修复迁移。
            ValueError: 忙重试的三个参数不合法（次数不是正整数、退避是负数、
                超时是负数）。
        """
        self._db_path = (
            Path(db_path) if db_path is not None else Path(resolve_state_db_path())
        )
        self._busy_retry_attempts = _positive_int(
            busy_retry_attempts, "busy_retry_attempts", REGISTRY_BUSY_RETRY_ATTEMPTS
        )
        self._busy_retry_backoff = _non_negative_float(
            busy_retry_backoff, "busy_retry_backoff", REGISTRY_BUSY_RETRY_BACKOFF
        )
        self._busy_timeout_ms = _optional_non_negative_int(
            busy_timeout_ms, "busy_timeout_ms"
        )
        self._assert_migrated()

    # -- 属性 --------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        """这个注册表写的是哪个库。"""
        return self._db_path

    # -- 连接 --------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """一条新连接。

        每次操作开一条新连接，而不是把一条连接存成实例属性：SQLite 的连接
        不是并发安全的，而注册表会被调度 tick 与验证子进程同时写。新连接顺带
        拿到的是**已提交**的状态，不会看见别人半截的事务。
        """
        conn = open_db(self._db_path)
        conn.row_factory = sqlite3.Row
        if self._busy_timeout_ms is not None:
            # PRAGMA 不接受绑定参数 —— 值在这里已经被校验成整数，
            # 所以拼进语句里的只有这个数字。
            conn.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return conn

    def _assert_migrated(self) -> None:
        """库跑过框架修复迁移吗？没跑过就明确报错。

        **不建表。** 建表看起来更「方便」，但它让「这个库跑过迁移没有」这个
        问题永远没有答案：一个从没迁移过的库会在第一次写入时悄悄长出四张表，
        而那四张表未必是生产那张 —— 于是注册表里有了数据，生产却读不到。
        """
        conn = self._connect()
        try:
            present = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
        finally:
            conn.close()
        missing = sorted(_REQUIRED_TABLES - present)
        if missing:
            raise SchemaNotMigratedError(
                f"the state database at {self._db_path} has not run the "
                "framework-fix migration; these tables are missing: "
                f"{missing}. Run state_machine.db.schema.migrate() first — "
                "the registry refuses to create its own schema, because a "
                "table it invented is not the table production reads."
            )

    # -- 登记 --------------------------------------------------------------
    def register_hit(self, hit: Dict[str, Any]) -> str:
        """登记一次命中，返回它的指纹。

        一次登记写三样东西，**在同一个事务里**：注册表行（每个指纹一行）、
        命中行（每次命中一行）、首条 ``discovered`` 事件。少写任何一样，
        「这个缺陷被登记过吗」就会给出不同的答案。

        **并发安全。** 注册表会被调度 tick 与验证子进程同时写，所以「同一个
        指纹只登记一次」这条不变式不能只靠调用方自觉：注册表行由数据库的唯一
        约束兜底（``INSERT OR IGNORE`` 写，随后回读确认），数据库忙时按有限
        次数重试。8 个线程同时登记同一个指纹的结果是**注册表一行、命中八行、
        事件八行**。

        Args:
            hit: 这次命中的事实。可用的键：

                * ``plan_id``（**必填**）—— 命中所在的计划；
                * ``framework_files`` / ``error_class`` / ``first_error_line``
                  —— 指纹的原料，缺一不可（``canonical_key`` 会拒绝空的
                  文件集或错误类别）；
                * ``vp_id`` / ``task_id`` / ``round`` —— 命中在哪；
                * ``classification`` —— 复现结论，取值来自
                  ``framework_fix.classifier.REPRO_CLASSIFICATIONS``，缺省
                  ``unclassified``；
                * ``blocking`` —— 是否阻塞，缺省否；
                * ``disposition`` —— 登记这次命中时做了什么，缺省
                  ``register_signature``；
                * ``evidence_path`` —— 原始证据的位置（原文不入库）；
                * ``evidence_summary`` / ``semantic_review`` —— 摘要与语义复核
                  结论，进注册表行；
                * ``repo`` —— 运营字段（复现排在哪、合并串行到哪），缺省
                  ``unknown``；
                * ``fix_plan_id`` —— 修复计划号，缺省用 ``plan_id``（修复
                  计划还没建出来时，先记下首次发现它的那个计划）。

        Returns:
            该命中的指纹（``sha256:`` 前缀）。

        Raises:
            TypeError: ``hit`` 不是字典。
            ValueError: 材料不足以算指纹，或分类 / 处置词不在闭集里。
            FrameworkFixRegistryError: 库没迁移，或写入失败。
            sqlite3.OperationalError: 数据库在有限次重试之后仍然忙。**这次
                登记没有发生** —— 一个字都没写进去，所以调用方可以放心地
                重试。
        """
        material = self._hit_material(hit)
        fingerprint = compute_fingerprint(
            framework_files=material["framework_files"],
            error_class=material["error_class"],
            first_error_line=material["first_error_line"],
        )
        key = canonical_key(
            framework_files=material["framework_files"],
            error_class=material["error_class"],
            first_error_line=material["first_error_line"],
        )

        attempts = self._busy_retry_attempts
        busy: Optional[sqlite3.OperationalError] = None
        for attempt in range(1, attempts + 1):
            try:
                return self._register_hit_once(material, fingerprint, key)
            except sqlite3.OperationalError as exc:
                if not _is_busy_error(exc):
                    # 不是忙 —— 是库坏了或者 SQL 错了。重试不会变好，
                    # 原样抛出去。
                    raise
                busy = exc
                if attempt >= attempts:
                    break
                # 越等越久：给持锁者留出收尾的时间，而不是让所有等待者
                # 一起再撞一次同一把锁。
                time.sleep(self._busy_retry_backoff * attempt)
        raise sqlite3.OperationalError(
            f"the state database at {self._db_path} stayed busy after "
            f"{attempts} attempt(s); the hit was NOT registered: {busy}"
        ) from busy

    def _register_hit_once(
        self,
        material: Dict[str, Any],
        fingerprint: str,
        key: Dict[str, Any],
    ) -> str:
        """一次登记尝试 —— 注册表行、命中行、事件行在**同一个事务**里。

        注册表行用 ``INSERT OR IGNORE`` 写：唯一性由数据库自己保证（指纹是
        主键），而不是靠「先查再插」这两步之间的一个空窗。并发登记同一个指纹
        时，后到的那几次写入会被数据库自己挡掉。

        写完**回读**一次：``OR IGNORE`` 把**任何**约束冲突都变成「没写进去」，
        不只是主键冲突。回读确认这一行确实在 —— 否则这次登记是静默失败的，
        那比抛错糟得多：调用方会以为缺陷登记上了。

        Raises:
            FrameworkFixRegistryError: 写完之后回读不到注册表行。
            sqlite3.OperationalError: 数据库忙（由调用方决定要不要重试）。
        """
        now = utcnow_iso()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                conn.execute(
                    "INSERT OR IGNORE INTO framework_fix_registry ("
                    "  fingerprint, fix_plan_id, repo, status, key_json,"
                    "  framework_files_json, evidence_summary_json,"
                    "  semantic_review_json, first_seen_at, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        fingerprint,
                        material["fix_plan_id"],
                        material["repo"],
                        REGISTRY_STATUS_OPEN,
                        _json_dumps(key),
                        _json_dumps(key["framework_files"]),
                        _json_dumps(material["evidence_summary"]),
                        material["semantic_review_json"],
                        material["first_seen_at"],
                        now,
                    ),
                )
                registry_row = self._registered_row(conn, fingerprint)
                if registry_row is None:
                    raise FrameworkFixRegistryError(
                        f"the registry row for {fingerprint!r} is missing right "
                        "after its insert: the write was ignored for a reason "
                        "other than the fingerprint already being registered. "
                        "Refusing to report a hit that was not registered."
                    )
                conn.execute(
                    "INSERT INTO framework_fix_hit ("
                    "  hit_id, fingerprint, plan_id, vp_id, task_id, round,"
                    "  classification, blocking, disposition, evidence_path,"
                    "  first_seen_at, created_at"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        uuid.uuid4().hex,
                        fingerprint,
                        material["plan_id"],
                        material["vp_id"],
                        material["task_id"],
                        material["round"],
                        material["classification"],
                        material["blocking"],
                        material["disposition"],
                        material["evidence_path"],
                        material["first_seen_at"],
                        now,
                    ),
                )
                self._insert_event(
                    conn,
                    "discovered",
                    {
                        "plan_id": material["plan_id"],
                        "vp_id": material["vp_id"],
                        "task_id": material["task_id"],
                        "round": material["round"],
                        "classification": material["classification"],
                        "blocking": material["blocking"],
                        "disposition": material["disposition"],
                        "error_class": material["error_class"],
                        "first_error_line": material["first_error_line"],
                        "framework_files": key["framework_files"],
                        "evidence_path": material["evidence_path"],
                    },
                    fingerprint=fingerprint,
                    fix_plan_id=material["fix_plan_id"],
                    created_at=now,
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()
        return fingerprint

    def _hit_material(self, hit: Dict[str, Any]) -> Dict[str, Any]:
        """把调用方的命中字典规整成落库要用的形状。

        校验在这一层做完，是为了让「写了一半」的命中行不可能存在：列都是
        NOT NULL，缺一个值要么在这里响亮地失败，要么在 SQLite 里以一个更含糊的
        错误失败 —— 后者离原因更远。
        """
        if not isinstance(hit, dict):
            raise TypeError(f"hit must be a dict; got {type(hit).__name__}")

        plan_id = hit.get("plan_id")
        if not isinstance(plan_id, str) or not plan_id.strip():
            raise ValueError(
                "hit['plan_id'] must be a non-empty string: a hit that "
                "belongs to no plan cannot be attributed to anything"
            )

        framework_files = hit.get("framework_files")
        error_class = hit.get("error_class")
        first_error_line = hit.get("first_error_line")

        classification = hit.get("classification") or DEFAULT_CLASSIFICATION
        if classification not in REPRO_CLASSIFICATIONS:
            raise ValueError(
                f"unknown classification {classification!r}; expected one of "
                f"{tuple(REPRO_CLASSIFICATIONS)}"
            )

        disposition = hit.get("disposition") or DEFAULT_DISPOSITION
        if disposition not in HIT_DISPOSITIONS:
            raise ValueError(
                f"unknown disposition {disposition!r}; expected one of "
                f"{sorted(HIT_DISPOSITIONS)}"
            )

        evidence_summary = hit.get("evidence_summary")
        if evidence_summary is None:
            evidence_summary = {}
        if not isinstance(evidence_summary, dict):
            raise TypeError(
                "hit['evidence_summary'] must be a dict; got "
                f"{type(evidence_summary).__name__}"
            )

        semantic_review = hit.get("semantic_review")
        semantic_review_json = (
            None if semantic_review is None else _json_dumps(semantic_review)
        )

        fix_plan_id = hit.get("fix_plan_id") or plan_id
        if not isinstance(fix_plan_id, str) or not fix_plan_id.strip():
            raise ValueError(
                "hit['fix_plan_id'] must be a non-empty string when given"
            )

        repo = hit.get("repo") or UNKNOWN_REPO
        if not isinstance(repo, str) or not repo.strip():
            raise ValueError("hit['repo'] must be a non-empty string when given")

        first_seen_at = hit.get("first_seen_at") or utcnow_iso()
        if not isinstance(first_seen_at, str) or not first_seen_at.strip():
            raise ValueError(
                "hit['first_seen_at'] must be a non-empty string when given"
            )

        return {
            "plan_id": plan_id.strip(),
            "vp_id": hit.get("vp_id"),
            "task_id": hit.get("task_id"),
            "round": _as_optional_int(hit.get("round"), "hit['round']"),
            "framework_files": framework_files,
            "error_class": error_class,
            "first_error_line": "" if first_error_line is None else first_error_line,
            "classification": classification,
            "blocking": _as_int_flag(hit.get("blocking")),
            "disposition": disposition,
            "evidence_path": hit.get("evidence_path"),
            "evidence_summary": evidence_summary,
            "semantic_review_json": semantic_review_json,
            "fix_plan_id": fix_plan_id.strip(),
            "repo": repo.strip(),
            "first_seen_at": first_seen_at,
        }

    # -- 挂接 --------------------------------------------------------------
    def attach(
        self,
        fingerprint: str,
        plan_id: str,
        vp_id: str,
        round_no: int,
        source: str,
    ) -> str:
        """把一个验证点挂到修复计划上，返回挂接 id。

        挂接关系放在注册表里而不是 plan 目录里：同一个缺陷可能同时挡住
        五个计划的五个验证点，而一个 plan 目录看不见别的 plan ——
        「多 plan → 一个修复 plan」这个关系只有注册表能表达。

        挂接是**幂等**的：同一个 ``(fingerprint, plan, vp)`` 挂两次只有
        一行、一个 id。挂接表达的是「谁在等这个修复」，不是「等了几次」。

        Args:
            fingerprint: 被挂住的缺陷指纹。别名会被解析到规范指纹，所以挂到
                一个已被合并掉的指纹上，挂的是它现在归到的那个缺陷。
            plan_id: 等待修复的计划。
            vp_id: 等待修复的验证点。空字符串表示「整条计划挂起，没有具体
                验证点」。
            round_no: 挂起发生在第几轮。
            source: 挂接来源（如 ``clean_main_repro``）。

        Returns:
            挂接 id（``framework_fix_attach.attach_id``）。

        Raises:
            KeyError: 这个指纹没有登记过。挂一个不存在的缺陷等于什么都没
                挂住 —— 原计划会以为自己在等一个不会来的修复。
            ValueError: 参数不合法（空计划号、空来源、负轮次）。
            FrameworkFixRegistryError: 写入失败。
        """
        plan = _require_text(plan_id, "plan_id")
        vp = _as_vp_id(vp_id)
        number = _as_round_no(round_no)
        origin = _require_text(source, "source")

        canonical = self._resolve(fingerprint)
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                registered = self._registered_row(conn, canonical)
                if registered is None:
                    raise KeyError(
                        f"no registered framework defect for fingerprint "
                        f"{fingerprint!r} (canonical {canonical!r}): a hold "
                        "on a defect nobody registered holds nothing"
                    )
                existing = self._find_attach(conn, canonical, plan, vp)
                if existing is not None:
                    conn.execute("COMMIT")
                    return existing
                status = self._attach_status(conn, canonical, plan, vp)
                attach_id = uuid.uuid4().hex
                now = utcnow_iso()
                conn.execute(
                    "INSERT INTO framework_fix_attach ("
                    "  attach_id, fingerprint, plan_id, vp_id, round, source,"
                    "  status, held_since, resolved_at, result_id"
                    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)",
                    (
                        attach_id,
                        canonical,
                        plan,
                        vp,
                        number,
                        origin,
                        status,
                        now,
                    ),
                )
                self._insert_event(
                    conn,
                    "attached",
                    {
                        "attach_id": attach_id,
                        "plan_id": plan,
                        "vp_id": vp,
                        "round": number,
                        "source": origin,
                        "status": status,
                    },
                    fingerprint=canonical,
                    fix_plan_id=registered["fix_plan_id"],
                    created_at=now,
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()
        return attach_id

    def attach_registered_hits(self, fingerprint: str) -> int:
        """把此前只登记签名的命中一并挂接，返回挂接的计划数。

        修复计划创建时调用：此前登记过的签名（非阻塞命中）也要挂到新计划
        上 —— 它们不持有执行、不计入阻塞数，但修复合入后必须重验，否则
        「修复合入后所有受影响方都按树重演」会漏掉它们。

        Args:
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。

        Returns:
            该指纹当前挂接的计划数（去重）。这是**状态**而不是增量：重复
            调用返回同一个数 —— 挂接本身是幂等的，不会多出第二行。

        Raises:
            ValueError: 指纹不是非空字符串，或别名链成环。
        """
        canonical = self._resolve(fingerprint)
        conn = self._connect()
        try:
            if self._registered_row(conn, canonical) is None:
                # 没有登记过的指纹没有可挂接的东西 —— 「只对已登记的指纹
                # 生效」不是错误，是一个空动作。
                return 0
            family = self._fingerprint_family(conn, canonical)
            placeholders = ", ".join("?" for _ in family)
            rows = conn.execute(
                f"SELECT plan_id, vp_id, round FROM framework_fix_hit "
                f"WHERE fingerprint IN ({placeholders}) "
                f"AND disposition = ? "
                f"ORDER BY created_at, hit_id",
                family + [DEFAULT_DISPOSITION],
            ).fetchall()
        finally:
            conn.close()

        seen = set()
        for row in rows:
            plan = row["plan_id"]
            vp = _as_vp_id(row["vp_id"])
            if (plan, vp) in seen:
                continue
            seen.add((plan, vp))
            self.attach(
                canonical,
                plan,
                vp,
                0 if row["round"] is None else row["round"],
                DEFAULT_DISPOSITION,
            )

        return self._attached_plan_count(canonical)

    def _find_attach(
        self,
        conn: sqlite3.Connection,
        canonical: str,
        plan_id: str,
        vp_id: str,
    ) -> Optional[str]:
        """已经挂接过吗？返回那一行的 id，没有就返回 ``None``。

        查的是**整个家族**（规范指纹 + 指向它的别名）：语义复核把两个
        签名合并之后，同一个验证点不该在合并前后各挂一次。
        """
        family = self._fingerprint_family(conn, canonical)
        placeholders = ", ".join("?" for _ in family)
        row = conn.execute(
            f"SELECT attach_id FROM framework_fix_attach "
            f"WHERE fingerprint IN ({placeholders}) "
            f"AND plan_id = ? AND vp_id = ? "
            f"ORDER BY held_since, attach_id LIMIT 1",
            family + [plan_id, vp_id],
        ).fetchone()
        return None if row is None else row[0]

    def _attach_status(
        self,
        conn: sqlite3.Connection,
        canonical: str,
        plan_id: str,
        vp_id: str,
    ) -> str:
        """这次挂接的状态 —— 由命中它的那次命中决定。

        阻塞性命中挂成 ``blocked``（原计划停着等修复），只登记了签名的
        命中挂成 ``registered``（不持有执行，只等修复合入后重验）。没有
        对应命中时按 ``registered`` 处理：没有证据说它在停着。
        """
        family = self._fingerprint_family(conn, canonical)
        placeholders = ", ".join("?" for _ in family)
        row = conn.execute(
            f"SELECT MAX(blocking) FROM framework_fix_hit "
            f"WHERE fingerprint IN ({placeholders}) AND plan_id = ? "
            f"AND (vp_id = ? OR vp_id IS NULL OR vp_id = '')",
            family + [plan_id, vp_id],
        ).fetchone()
        blocked = int(row[0] or 0)
        return ATTACH_STATUS_BLOCKED if blocked else ATTACH_STATUS_REGISTERED

    def _attached_plan_count(self, canonical: str) -> int:
        """这个缺陷当前挂接了多少个计划（去重）。"""
        conn = self._connect()
        try:
            family = self._fingerprint_family(conn, canonical)
            placeholders = ", ".join("?" for _ in family)
            row = conn.execute(
                f"SELECT COUNT(DISTINCT plan_id) FROM framework_fix_attach "
                f"WHERE fingerprint IN ({placeholders})",
                family,
            ).fetchone()
        finally:
            conn.close()
        return int(row[0])

    def _registered_row(
        self, conn: sqlite3.Connection, fingerprint: str
    ) -> Optional[sqlite3.Row]:
        """这个指纹有注册表行吗？"""
        return conn.execute(
            "SELECT * FROM framework_fix_registry WHERE fingerprint = ?",
            (fingerprint,),
        ).fetchone()

    # -- 查询 --------------------------------------------------------------
    def lookup(self, fingerprint: str) -> Optional[Dict[str, Any]]:
        """这个缺陷现在是什么状态？

        Args:
            fingerprint: 指纹。别名会被解析到规范指纹，所以一个已经被语义复核
                合并掉的指纹仍然能查到它现在归到哪。

        Returns:
            注册表行（JSON 列已解开），或 ``None`` —— 没有登记过。

        Raises:
            ValueError: 指纹不是非空字符串，或别名链成环。
        """
        canonical = self._resolve(fingerprint)
        conn = self._connect()
        try:
            row = conn.execute(
                "SELECT * FROM framework_fix_registry WHERE fingerprint = ?",
                (canonical,),
            ).fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return self._registry_row(row)

    def list_registry(self) -> List[Dict[str, Any]]:
        """现在登记着哪些缺陷？整个注册表，一行一个指纹。

        「这个缺陷登记过没有」这个问题的答案，取自数据库而不是取自某个进程的
        日志 —— 所以读它的必须是数据库本身。

        并发登记之后这个列表**仍然**每个指纹一行：唯一性由数据库的主键保证，
        不由调用方的顺序保证。

        Returns:
            注册表行（JSON 列已解开，与 :meth:`lookup` 同一形状），按登记
            时间排序；同一时刻登记的按指纹排序，所以次序是稳定的。
        """
        conn = self._connect()
        try:
            rows = conn.execute(
                "SELECT * FROM framework_fix_registry "
                "ORDER BY created_at, fingerprint"
            ).fetchall()
        finally:
            conn.close()
        return [self._registry_row(row) for row in rows]

    def list_hits(self, fingerprint: str) -> List[Dict[str, Any]]:
        """这个签名被命中过几次、来自哪些计划？

        Args:
            fingerprint: 指纹。别名会被解析到规范指纹，并且**指向**规范指纹的
                别名也算数 —— 语义复核合并两个签名之后，两个签名的命中历史要
                能一起读出来。

        Returns:
            命中行，最早的在前（同一次登记里的多条按命中 id 排，保证次序
            稳定）。
        """
        canonical = self._resolve(fingerprint)
        conn = self._connect()
        try:
            family = self._fingerprint_family(conn, canonical)
            placeholders = ", ".join("?" for _ in family)
            rows = conn.execute(
                f"SELECT * FROM framework_fix_hit "
                f"WHERE fingerprint IN ({placeholders}) "
                f"ORDER BY created_at, hit_id",
                family,
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def list_attach(self, fingerprint: str) -> List[Dict[str, Any]]:
        """谁在等这个缺陷的修复？

        Args:
            fingerprint: 指纹。别名会被解析到规范指纹，并且**指向**规范指纹
                的别名也算数 —— 语义复核合并两个签名之后，两个签名的挂接
                关系要能一起读出来。

        Returns:
            挂接行，最早的在前（同一次挂接的多条按挂接 id 排，保证次序
            稳定）。没有登记过的指纹返回空列表。
        """
        canonical = self._resolve(fingerprint)
        conn = self._connect()
        try:
            family = self._fingerprint_family(conn, canonical)
            placeholders = ", ".join("?" for _ in family)
            rows = conn.execute(
                f"SELECT * FROM framework_fix_attach "
                f"WHERE fingerprint IN ({placeholders}) "
                f"ORDER BY held_since, attach_id",
                family,
            ).fetchall()
        finally:
            conn.close()
        return [dict(row) for row in rows]

    def _resolve(self, fingerprint: str) -> str:
        """把指纹解析成规范指纹（沿别名链）。

        别名链的解析用 ``framework_fix.fingerprint.resolve_alias``：成环的别名
        表没有链尾，解析会永远转下去 —— 那是数据坏了，必须响亮地失败。
        """
        if not isinstance(fingerprint, str) or not fingerprint.strip():
            raise ValueError("fingerprint must be a non-empty string")
        current = fingerprint.strip()
        conn = self._connect()
        try:
            return resolve_alias(current, self._alias_lookup(conn))
        finally:
            conn.close()

    def _alias_lookup(self, conn: sqlite3.Connection) -> Callable[[str], Optional[str]]:
        """别名表查询 —— 给一个指纹，返回它指向的规范指纹。"""

        def lookup(current: str) -> Optional[str]:
            row = conn.execute(
                "SELECT canonical_fingerprint FROM framework_fix_alias "
                "WHERE alias_fingerprint = ?",
                (current,),
            ).fetchone()
            if row is None:
                return None
            return row[0]

        return lookup

    def _fingerprint_family(
        self, conn: sqlite3.Connection, canonical: str
    ) -> List[str]:
        """规范指纹 + 所有指向它的别名。"""
        rows = conn.execute(
            "SELECT alias_fingerprint FROM framework_fix_alias "
            "WHERE canonical_fingerprint = ?",
            (canonical,),
        ).fetchall()
        return [canonical] + [row[0] for row in rows]

    def _registry_row(self, row: sqlite3.Row) -> Dict[str, Any]:
        """注册表行 → 字典。JSON 列既给原文，也给解开的那一份。"""
        record: Dict[str, Any] = dict(row)
        record["key"] = _loads(record.get("key_json"))
        record["framework_files"] = _loads(record.get("framework_files_json"))
        record["evidence_summary"] = _loads(record.get("evidence_summary_json"))
        record["semantic_review"] = _loads(record.get("semantic_review_json"))
        return record

    # -- 别名与合并 --------------------------------------------------------
    def write_alias(self, alias_fp: str, canonical_fp: str) -> None:
        """把一个指纹登记为另一个指纹的别名。

        机械指纹会漏掉「症状不同但语义相同」的命中；语义复核判为同一个
        缺陷时调用这个方法，后续同形命中靠别名链回到同一个规范指纹。

        Args:
            alias_fp: 别名指纹（重复的那个）。
            canonical_fp: 规范指纹。传进来的若本身是别名，会指向它最终的
                规范指纹 —— 这样一次家族查询就能取全整个家族。

        Raises:
            ValueError: 两个指纹相同（自别名是环），或者这次写入会让别名
                链成环。成环的别名表没有链尾，解析会永远转下去。
            FrameworkFixRegistryError: 写入失败。
        """
        alias = _require_text(alias_fp, "alias_fingerprint")
        canonical_raw = _require_text(canonical_fp, "canonical_fingerprint")
        if alias == canonical_raw:
            raise ValueError(
                f"cannot alias {alias!r} to itself: an alias cycle has no "
                "chain end, so the fingerprint could never be resolved"
            )
        canonical = self._resolve(canonical_raw)
        if canonical == alias:
            raise ValueError(
                f"cannot alias {alias!r} to {canonical_raw!r}: the alias "
                "chain would become a cycle"
            )
        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._write_alias_row(conn, alias, canonical, now)
                self._insert_event(
                    conn,
                    "alias_written",
                    {
                        "alias_fingerprint": alias,
                        "canonical_fingerprint": canonical,
                    },
                    fingerprint=canonical,
                    fix_plan_id=None,
                    created_at=now,
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    def merge_into(self, canonical_fp: str, duplicate_fp: str) -> None:
        """事后确认两个修复计划实为同一缺陷时，把它们合并成一个。

        合并做四件事，**都不删历史**：

        1. 规范指纹自己没有注册表行、而被合并方有时，从被合并方复制一行
           过来 —— 目标改成规范指纹，缺陷的状态必须在它下面可见；
        2. 挂接行迁到规范指纹下（同一个计划/验证点已经有挂接的不制造第
           二行，那一行留在原处，仍然读得到）；
        3. 写别名：被合并的指纹从此解析到规范指纹；
        4. 追加一条 ``merged`` 事件。

        Args:
            canonical_fp: 规范指纹 —— 由较早的那个修复计划继续的那个。
            duplicate_fp: 被合并的指纹。

        Raises:
            ValueError: 两个指纹解析到同一个规范指纹。自己合并自己没有
                意义，而别名成环则会让解析永远转下去。
            FrameworkFixRegistryError: 写入失败。
        """
        canonical_raw = _require_text(canonical_fp, "canonical_fingerprint")
        duplicate_raw = _require_text(duplicate_fp, "duplicate_fingerprint")
        canonical = self._resolve(canonical_raw)
        duplicate = self._resolve(duplicate_raw)
        if canonical == duplicate:
            raise ValueError(
                f"cannot merge {duplicate_raw!r} into {canonical_raw!r}: "
                "they already are the same defect (a fingerprint merged "
                "into itself is an alias cycle, and an alias cycle has no "
                "chain end)"
            )

        now = utcnow_iso()
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                canonical_row = self._registered_row(conn, canonical)
                duplicate_row = self._registered_row(conn, duplicate)
                if canonical_row is None and duplicate_row is not None:
                    # 目标改成规范指纹：它自己还没有注册表行，就从被合并方
                    # 复制一份 —— 被合并方那行是历史，不删。
                    self._copy_registry_row(conn, duplicate_row, canonical, now)
                sources = self._fingerprint_family(conn, duplicate)
                migrated = self._migrate_attach(conn, sources, canonical)
                for alias in (duplicate, duplicate_raw):
                    self._write_alias_row(conn, alias, canonical, now)
                self._insert_event(
                    conn,
                    "merged",
                    {
                        "canonical_fingerprint": canonical,
                        "duplicate_fingerprint": duplicate_raw,
                        "migrated_attach": migrated,
                    },
                    fingerprint=canonical,
                    fix_plan_id=None,
                    created_at=now,
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    def _write_alias_row(
        self,
        conn: sqlite3.Connection,
        alias: str,
        canonical: str,
        now: str,
    ) -> None:
        """在**已有事务**里写一行别名。

        同一条别名重复写不新增行 —— 别名表的主键就是别名指纹。
        """
        conn.execute(
            "INSERT INTO framework_fix_alias ("
            "  alias_fingerprint, canonical_fingerprint, created_at"
            ") VALUES (?, ?, ?) "
            "ON CONFLICT(alias_fingerprint) DO UPDATE SET "
            "  canonical_fingerprint = excluded.canonical_fingerprint, "
            "  created_at = excluded.created_at",
            (alias, canonical, now),
        )

    def _copy_registry_row(
        self,
        conn: sqlite3.Connection,
        source: sqlite3.Row,
        canonical: str,
        now: str,
    ) -> None:
        """把被合并方的注册表行复制到规范指纹下。

        复制而不是搬家：被合并方那行是历史（「这个签名曾经被登记过、
        在这个时刻是什么状态」），删掉它就答不上这个问题了。
        """
        conn.execute(
            "INSERT INTO framework_fix_registry ("
            "  fingerprint, fix_plan_id, repo, status, key_json,"
            "  framework_files_json, evidence_summary_json,"
            "  semantic_review_json, first_seen_at, created_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                canonical,
                source["fix_plan_id"],
                source["repo"],
                source["status"],
                source["key_json"],
                source["framework_files_json"],
                source["evidence_summary_json"],
                source["semantic_review_json"],
                source["first_seen_at"],
                now,
            ),
        )

    def _migrate_attach(
        self,
        conn: sqlite3.Connection,
        sources: List[str],
        canonical: str,
    ) -> int:
        """把挂接行迁到规范指纹下，返回迁了几行。

        规范指纹下已经有同一个 ``(plan_id, vp_id)`` 的挂接时不制造第二行：
        那一行留在原处，它仍然通过家族查询读得到，但「同一个验证点被挂了
        两次」这种重复不会进到表里。
        """
        migrated = 0
        for source in sources:
            rows = conn.execute(
                "SELECT attach_id, plan_id, vp_id FROM framework_fix_attach "
                "WHERE fingerprint = ?",
                (source,),
            ).fetchall()
            for row in rows:
                clash = conn.execute(
                    "SELECT attach_id FROM framework_fix_attach "
                    "WHERE fingerprint = ? AND plan_id = ? AND vp_id = ? "
                    "LIMIT 1",
                    (canonical, row["plan_id"], row["vp_id"]),
                ).fetchone()
                if clash is not None:
                    continue
                conn.execute(
                    "UPDATE framework_fix_attach SET fingerprint = ? "
                    "WHERE attach_id = ?",
                    (canonical, row["attach_id"]),
                )
                migrated += 1
        return migrated

    # -- 指向修复计划 ------------------------------------------------------
    def link_fix_plan(
        self,
        fingerprint: str,
        fix_plan_id: str,
        *,
        repo: Optional[str] = None,
    ) -> bool:
        """把注册表那一行指向某个修复计划。

        「这一行现在归哪个修复计划管」是注册表自己的事实，所以它由注册表写。
        别处用一条裸 ``UPDATE`` 改这一行，审计链上就没有这次改动的记录，
        而「这个缺陷归谁修」这个问题就只剩一条没有出处的答案。

        仓库是运营字段：这一行还不知道自己在哪个仓库时补上；已经知道时
        **不覆盖** —— 第一次看见它的那个仓库是事实，后来的一次链接改不掉它。

        Args:
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。
            fix_plan_id: 指向的修复计划号。
            repo: 补仓库用（可空）。

        Returns:
            这一行存在、并且已经指向那个修复计划，是 ``True``；没有这一行
            （没登记过的指纹）是 ``False``，**什么都不写**。

        Raises:
            ValueError: 指纹或计划号不是非空字符串，或别名链成环。
        """
        fp = _require_text(fingerprint, "fingerprint")
        plan = _require_text(fix_plan_id, "fix_plan_id")
        wanted_repo = _optional_text(repo, "repo")
        now = utcnow_iso()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                canonical = resolve_alias(fp, self._alias_lookup(conn))
                row = self._registered_row(conn, canonical)
                if row is None:
                    # 没登记过的指纹没有可指向的行 —— 「没有这一行」不是错误，
                    # 是一个空动作，也不该在审计链上留痕。
                    conn.execute("COMMIT")
                    return False
                current_repo = str(row["repo"] or "")
                effective_repo = current_repo
                if wanted_repo and current_repo in ("", UNKNOWN_REPO):
                    effective_repo = wanted_repo
                conn.execute(
                    "UPDATE framework_fix_registry SET fix_plan_id = ?, "
                    "repo = ? WHERE fingerprint = ?",
                    (plan, effective_repo, canonical),
                )
                self._insert_event(
                    conn,
                    "fix_plan_linked",
                    {
                        "fingerprint": canonical,
                        "fix_plan_id": plan,
                        "repo": effective_repo,
                    },
                    fingerprint=canonical,
                    fix_plan_id=plan,
                    created_at=now,
                )
                conn.execute("COMMIT")
                return True
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    def resolve_fingerprint(self, fingerprint: str) -> str:
        """这个指纹现在归到哪个规范指纹名下。

        语义复核把一个签名并进另一个之后，这一行的归属就变了 —— 问「它归谁」
        的人要的是**现在**的答案，不是它登记那一刻的答案。

        Args:
            fingerprint: 指纹。

        Returns:
            规范指纹。别名链的链尾就是它自己。

        Raises:
            ValueError: 指纹不是非空字符串，或别名链成环。
        """
        return self._resolve(_require_text(fingerprint, "fingerprint"))

    # -- 状态 --------------------------------------------------------------
    def set_status(self, fingerprint: str, status: str) -> bool:
        """把注册表那一行的状态改成某个词，返回改没改成。

        「这个缺陷此刻是什么状态」是注册表自己的事实，所以它由注册表写。别处
        用一条裸 ``UPDATE`` 改这一列，审计链上就没有这次改动的记录，而「这个
        缺陷修好了没有」这个问题就只剩一条没有出处的答案。

        状态词是闭集：集合外的词是拼写错误，不是新语义 —— 把它悄悄当成「还
        开着」，会让一个已经修好的缺陷继续显示成待修。

        Args:
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。
            status: 状态词，见 :data:`REGISTRY_STATUSES`。

        Returns:
            这一行存在、并且状态被改成了，是 ``True``；没有这一行（没登记过
            的指纹）是 ``False``，**什么都不写**。

        Raises:
            ValueError: 状态词不在闭集里，或指纹不是非空字符串。
        """
        wanted = normalize_status(status)
        fp = _require_text(fingerprint, "fingerprint")
        now = utcnow_iso()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                canonical = resolve_alias(fp, self._alias_lookup(conn))
                row = self._registered_row(conn, canonical)
                if row is None:
                    conn.execute("COMMIT")
                    return False
                previous = str(row["status"] or "")
                conn.execute(
                    "UPDATE framework_fix_registry SET status = ? "
                    "WHERE fingerprint = ?",
                    (wanted, canonical),
                )
                self._insert_event(
                    conn,
                    "status_changed",
                    {
                        "fingerprint": canonical,
                        "status": wanted,
                        "previous_status": previous,
                    },
                    fingerprint=canonical,
                    fix_plan_id=str(row["fix_plan_id"] or "") or None,
                    created_at=now,
                )
                conn.execute("COMMIT")
                return True
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    def mark_fixed(self, fingerprint: str) -> bool:
        """这个缺陷修好了 —— 修复合入的那一刻由注册表写下。

        Returns:
            写成功是 ``True``；没登记过的指纹是 ``False``。
        """
        return self.set_status(fingerprint, REGISTRY_STATUS_FIXED)

    def reopen(self, fingerprint: str) -> bool:
        """这个缺陷又被命中了 —— 修复合入后重跑仍失败。

        同一个修复计划继续跑下一轮，所以缺陷重新打开，而不是另起一个计划。

        Returns:
            写成功是 ``True``；没登记过的指纹是 ``False``。
        """
        return self.set_status(fingerprint, REGISTRY_STATUS_REOPENED)

    def set_attach_status(self, fingerprint: str, status: str) -> int:
        """把这个缺陷**全部**挂接行的状态改成某个词，返回改了几条。

        挂接行是历史（「谁曾经等过这个修复」），所以这里**标记**而不是删除：
        记录留着，状态改掉。状态是闭集：集合外的词是拼写错误，不是新语义。

        改的是**整个家族**（规范指纹 + 指向它的别名）—— 语义复核把两个签名
        并成一个之后，它们是同一件活，挂接的状态必须一起改，否则其中一份会
        继续挂着那个计划。

        Args:
            fingerprint: 缺陷指纹。别名会被解析到规范指纹。
            status: 挂接状态词，见 :data:`ATTACH_STATUSES`。

        Returns:
            改了几条。没人等过的缺陷是 0（不是错误 —— 没有东西可改）。

        Raises:
            ValueError: 状态词不在闭集里，或指纹不是非空字符串。
        """
        wanted = str(status or "").strip()
        if wanted not in ATTACH_STATUSES:
            raise ValueError(
                f"unknown attach status {status!r}; expected one of "
                f"{sorted(ATTACH_STATUSES)}"
            )
        fp = _require_text(fingerprint, "fingerprint")
        now = utcnow_iso()

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                canonical = resolve_alias(fp, self._alias_lookup(conn))
                registered = self._registered_row(conn, canonical)
                family = self._fingerprint_family(conn, canonical)
                placeholders = ", ".join("?" for _ in family)
                rows = conn.execute(
                    f"SELECT attach_id, plan_id, vp_id, status "
                    f"FROM framework_fix_attach WHERE fingerprint IN "
                    f"({placeholders})",
                    family,
                ).fetchall()
                changed = 0
                for row in rows:
                    if str(row["status"] or "") == wanted:
                        continue
                    conn.execute(
                        "UPDATE framework_fix_attach SET status = ? "
                        "WHERE attach_id = ?",
                        (wanted, row["attach_id"]),
                    )
                    changed += 1
                if changed:
                    self._insert_event(
                        conn,
                        "attach_status_changed",
                        {
                            "fingerprint": canonical,
                            "status": wanted,
                            "changed": changed,
                        },
                        fingerprint=canonical,
                        fix_plan_id=(
                            str(registered["fix_plan_id"] or "") or None
                            if registered is not None
                            else None
                        ),
                        created_at=now,
                    )
                conn.execute("COMMIT")
                return changed
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    # -- 合并租约 ----------------------------------------------------------
    def acquire_merge_lease(
        self,
        repo: str,
        fix_plan_id: str,
        pr_url: Optional[str] = None,
    ) -> bool:
        """占住这个仓库的合并槽。

        **一个仓库同时只允许一个未合并的框架修复 PR。** 两条修复 PR 同时
        挤进同一个仓库的合并窗口，先合的那条会把后一条的基线带偏 —— 而这种
        冲突在合并完成之前根本看不出来。所以「谁有资格合」这件事必须在
        **开 PR 之前**定下来：拿到槽的修复计划才有资格合，别的计划得等。

        槽是**互斥**的，不是一个计数器：

        * 槽被别人占着 → 返回 ``False``，**不覆盖**别人那一行；
        * 槽已经是自己的 → 返回 ``True``（幂等），并把 ``pr_url`` 补上
          （PR 是开完之后才有 URL 的）；
        * 槽空着 → 写入并返回 ``True``。

        并发由数据库兜底：``framework_fix_merge_lease.repo`` 是主键，两个
        进程同时抢同一个仓库时，撞唯一约束的那个**退化成 ``False``**，不抛
        错 —— 抢输不是事故，是「这个仓库已经有人在修了」的答案。

        占槽是一次**状态迁移**，所以它在同一个事务里往审计链上追加一条
        ``merge_lease_acquired``：「谁在什么时候占了这个仓库的槽」由数据库
        回答，而不是由当时恰好在跑的那个进程的日志回答。审计链上**不**记
        两次「获取」：幂等的重复占槽不是第二次占槽，而抢输的那一方压根没占
        到槽，更不能被记成占了。

        Args:
            repo: 仓库名。槽是**按仓库**分的，不是全局的一个槽。
            fix_plan_id: 占槽的修复计划号。
            pr_url: 这个修复的 PR 地址（可空 —— PR 还没开出来时先占槽）。

        Returns:
            占到了（或者本来就是自己的）就是 ``True``；槽被别人占着是
            ``False``。

        Raises:
            ValueError: ``repo`` 或 ``fix_plan_id`` 不是非空字符串。
            SchemaNotMigratedError: 库没跑框架修复迁移（没有租约这张表）。
        """
        repository = _require_text(repo, "repo")
        plan = _require_text(fix_plan_id, "fix_plan_id")
        url = _optional_text(pr_url, "pr_url")

        conn = self._connect()
        try:
            self._assert_lease_table(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._lease_row(conn, repository)
                if row is not None:
                    if str(row["fix_plan_id"]) == plan:
                        # 槽已经是自己的 —— 幂等。只补 PR 地址，不动
                        # ``acquired_at``：这个槽是什么时候占上的，是运营事实，
                        # 重复占槽不该把它洗成「刚刚才占」。
                        #
                        # 审计链上**不**再记一次「获取」：占槽只发生了一次。
                        if url is not None and row["pr_url"] != url:
                            conn.execute(
                                "UPDATE framework_fix_merge_lease "
                                "SET pr_url = ? WHERE repo = ?",
                                (url, repository),
                            )
                        conn.execute("COMMIT")
                        return True
                    # 别人占着这个仓库的槽 —— 不覆盖他的那一行，也不记「获取」：
                    # 这一方没占到槽，把它记成占了就是审计链在撒谎。
                    conn.execute("COMMIT")
                    return False
                now = utcnow_iso()
                conn.execute(
                    "INSERT INTO framework_fix_merge_lease "
                    "(repo, fix_plan_id, pr_url, acquired_at) "
                    "VALUES (?, ?, ?, ?)",
                    (repository, plan, url, now),
                )
                self._insert_event(
                    conn,
                    "merge_lease_acquired",
                    {
                        "repo": repository,
                        "fix_plan_id": plan,
                        "pr_url": url,
                    },
                    fingerprint=None,
                    fix_plan_id=plan,
                    created_at=now,
                )
                conn.execute("COMMIT")
                return True
            except sqlite3.IntegrityError:
                self._rollback(conn)
                # 唯一索引兜底：别的进程刚抢到同一个仓库的槽。抢输不是
                # 事故 —— 槽给先到的人。这一方没占到槽，所以没有事件。
                return False
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    def release_merge_lease(self, repo: str, fix_plan_id: str) -> bool:
        """释放这个仓库的合并槽。

        **只释放自己的。** 释放别人的槽等于把一个正在合并的 PR 从合并窗口
        里踢出去 —— 那条 PR 还开着、还带着基线，却已经没有人替它守着合并
        槽了。所以这里的删除条件里带着持有人：不是持有人的，删不掉。

        释放是一次**状态迁移**，所以它在同一个事务里往审计链上追加一条
        ``merge_lease_released``。没有释放掉任何东西时**不**追加：审计链上
        记一条没有发生过的释放，比不记更糟。

        Args:
            repo: 仓库名。
            fix_plan_id: 释放槽的修复计划号。

        Returns:
            确实释放了（这一行本来就在、且是它的）是 ``True``；槽本来就是
            空的、或者别人占着，就是 ``False``。

        Raises:
            ValueError: ``repo`` 或 ``fix_plan_id`` 不是非空字符串。
            SchemaNotMigratedError: 库没跑框架修复迁移（没有租约这张表）。
        """
        repository = _require_text(repo, "repo")
        plan = _require_text(fix_plan_id, "fix_plan_id")

        conn = self._connect()
        try:
            self._assert_lease_table(conn)
            conn.execute("BEGIN IMMEDIATE")
            try:
                cursor = conn.execute(
                    "DELETE FROM framework_fix_merge_lease "
                    "WHERE repo = ? AND fix_plan_id = ?",
                    (repository, plan),
                )
                released = cursor.rowcount > 0
                if released:
                    self._insert_event(
                        conn,
                        "merge_lease_released",
                        {
                            "repo": repository,
                            "fix_plan_id": plan,
                        },
                        fingerprint=None,
                        fix_plan_id=plan,
                        created_at=utcnow_iso(),
                    )
                conn.execute("COMMIT")
                return released
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()

    def _lease_row(
        self, conn: sqlite3.Connection, repo: str
    ) -> Optional[sqlite3.Row]:
        """这个仓库的合并槽现在是谁的；空槽返回 ``None``。"""
        return conn.execute(
            "SELECT repo, fix_plan_id, pr_url, acquired_at "
            "FROM framework_fix_merge_lease WHERE repo = ?",
            (repo,),
        ).fetchone()

    def _assert_lease_table(self, conn: sqlite3.Connection) -> None:
        """合并租约这张表在吗？

        租约表是**迁移**建出来的（与注册表那五张表同一步），本模块同样
        **不建表** —— 一个从没迁移过的库上凭空长出这张表，生产根本读不到，
        那比报错更糟。
        """
        row = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type = 'table' AND name = ?",
            ("framework_fix_merge_lease",),
        ).fetchone()
        if row is None:
            raise SchemaNotMigratedError(
                f"the state database at {self._db_path} has not run the "
                "framework-fix migration: the table framework_fix_merge_lease "
                "is missing. Run state_machine.db.schema.migrate() first — "
                "the registry refuses to create its own schema, because a "
                "table it invented is not the table production reads."
            )

    # -- 事件 --------------------------------------------------------------
    def has_event(
        self,
        event_type: str,
        *,
        fix_plan_id: Optional[str] = None,
        fingerprint: Optional[str] = None,
    ) -> bool:
        """审计链上有没有这一条标记事件。

        「这件事发生过了没有」必须**活得比这次进程长**：通知过一次的事，
        进程重启之后不该再通知一次；而静默窗口吞掉的那一次（根本没送
        出去）不算发生过了 —— 它要在窗口过去之后补上。

        所以标记写在审计链（数据库）里，不在某个进程的内存里：进程内的
        列表一重启就失忆，「只通知一次」会变成「每次重启都通知一次」。

        Args:
            event_type: 事件类型，如 ``notify_discovered_sent``。
            fix_plan_id: 关联的修复计划号（可空）。
            fingerprint: 关联的指纹（可空）。

            两个键都给出时，命中**任意一个**就算命中 —— 按修复计划去重是
            这份契约的要点：同一个修复计划哪怕修好几个缺陷，也只该通知
            一次。

        Returns:
            有没有这样一行。

        Raises:
            ValueError: ``event_type`` 不是非空字符串，或者两个键都没给 ——
                一条谁也认不出来的事件不是事实。
        """
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        clauses: List[str] = []
        params: List[Any] = [event_type.strip()]
        if fix_plan_id is not None:
            clauses.append("fix_plan_id = ?")
            params.append(str(fix_plan_id))
        if fingerprint is not None:
            clauses.append("fingerprint = ?")
            params.append(str(fingerprint))
        if not clauses:
            raise ValueError(
                "has_event needs a fix_plan_id or a fingerprint: an event "
                "nobody can be identified with is not a fact"
            )
        sql = (
            "SELECT 1 FROM framework_fix_event WHERE event_type = ? AND ("
            + " OR ".join(clauses)
            + ") LIMIT 1"
        )
        conn = self._connect()
        try:
            row = conn.execute(sql, params).fetchone()
        finally:
            conn.close()
        return row is not None

    def append_event(
        self,
        event_type: str,
        payload: Dict[str, Any],
        *,
        fingerprint: Optional[str] = None,
        fix_plan_id: Optional[str] = None,
    ) -> int:
        """往审计链上追加一条事件。

        审计链只追加，不修改、不删除：「这个缺陷经历过什么」由数据库回答，
        而不是由当时恰好在跑的那个进程的日志回答。

        Args:
            event_type: 事件类型，如 ``discovered`` / ``attached`` /
                ``fixed``。
            payload: 事件内容。序列化成 JSON 存进 ``payload_json``。
            fingerprint: 关联的指纹（可空 —— 有些事件不属于某一个缺陷）。
            fix_plan_id: 关联的修复计划号（可空）。

        Returns:
            追加了几行 —— 恒为 1。返回行数而不是事件 id，是为了让调用方能
            写 ``total += registry.append_event(...)`` 而不必再查一次表。

        Raises:
            ValueError: ``event_type`` 不是非空字符串。
        """
        if not isinstance(event_type, str) or not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise TypeError(
                f"payload must be a dict; got {type(payload).__name__}"
            )

        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                self._insert_event(
                    conn,
                    event_type.strip(),
                    payload,
                    fingerprint=fingerprint,
                    fix_plan_id=fix_plan_id,
                    created_at=utcnow_iso(),
                )
                conn.execute("COMMIT")
            except BaseException:
                self._rollback(conn)
                raise
        finally:
            conn.close()
        return 1

    def _insert_event(
        self,
        conn: sqlite3.Connection,
        event_type: str,
        payload: Dict[str, Any],
        *,
        fingerprint: Optional[str],
        fix_plan_id: Optional[str],
        created_at: str,
    ) -> str:
        """在**已有事务**里写一条事件。

        ``register_hit`` 用它，而不是公开的 :meth:`append_event`：那次写入必须
        和注册表行、命中行在同一个事务里，分成两个事务就等于给了「命中行写
        进去了、事件没写进去」这个机会。
        """
        event_id = uuid.uuid4().hex
        conn.execute(
            "INSERT INTO framework_fix_event ("
            "  event_id, fingerprint, fix_plan_id, event_type, payload_json,"
            "  created_at"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                event_id,
                fingerprint,
                fix_plan_id,
                event_type,
                _json_dumps(payload),
                created_at,
            ),
        )
        return event_id

    @staticmethod
    def _rollback(conn: sqlite3.Connection) -> None:
        """回滚 —— 失败时不能留下半截的写入。"""
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            # 没有活动事务（COMMIT 之后才失败的）：没什么可回滚的。
            pass


def _loads(value: Any) -> Any:
    """解开一个 JSON 列；空值解成 ``None``，坏值解成 ``None`` 而不是炸掉读取。"""
    if value is None:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None
