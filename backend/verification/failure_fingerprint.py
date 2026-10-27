"""失败指纹 —— 一次失败的确定性身份。

这个模块解决什么
----------------
A2 要回答的是「同一 VP 相邻两次失败,根因有没有演进」。现有的
``same_failure_counts`` 只按 ``VP-x|分类`` 计数,回答不了这个问题:两次失败
分类相同(都是 ``deliverable_defect``),但一次断在 JSON 路径、一次断在状态码 ——
计数说「同一种失败又发生了一次」,根因其实换了。

失败指纹就是那个能分辨根因的键:同一 VP 的两次失败,指纹相同 => 根因没变;
指纹不同 => 根因变了。

指纹必须是纯函数
----------------
不读文件、不发请求、不看时钟、不依赖 ``hash()`` 的随机种子。同样的输入永远
给同样的输出 —— 否则「根因是否演进」这个判断本身就不可复算,跨进程比对更是
无从谈起。

指纹对什么敏感,对什么不敏感
----------------------------
敏感(必须能分辨):``vp_id`` / ``failure_class`` / ``failed_assertions`` /
``command`` / ``first_error_line``。

不敏感(噪声,不能进指纹):

* **tmp 路径** —— pytest 每次给每个用例一个新的临时根,同一个失败在两次运行
  里路径都不一样。
* **十六进制地址** —— 内存地址每次都变。
* **大小写** —— 同一件事换个写法。
* **空白** —— 换行、缩进、多余空格。
* **断言顺序** —— 断言是一个集合,不是序列。

归一化复用 failure_classification
----------------------------------
掩码**不另造第二套**:``backend/failure_classification.py`` 已经在做同一件事
(``_normalize_path_token`` 归一路径, ``_mask_hex`` 把十六进制摘要掩成
``<hex>``),这里直接复用它的那几个原语,两处就不会漂。复用清单:

======================  ==========================================
``_normalize_path_token``  路径归一(normpath + 临时根占位)
``_mask_hex``              十六进制摘要 -> ``<hex>``
``_HEX_RUN_RE``            配 ``_mask_hex`` 用的那条正则
``_PATH_TOKEN_RE``         从自由文本里找出路径的那条正则
``_NUMBER_RE``            数字 -> ``<n>``(只用在临时路径里)
``_TOKEN_SEP``            字段分隔符
======================  ==========================================

在此之上只加两件事,都是**同一条规则**的延伸,不是新规则:

1. **临时根的大小写不敏感匹配**。``_normalize_path_token`` 用
   ``tempfile.gettempdir()`` 做精确前缀比较,而指纹在归一化路径**之前**就把
   整段文本转成了小写 —— macOS 的临时根 ``.../T`` 于是变成 ``.../t``,精确
   匹配落空。这里补一次大小写不敏感的**同一条**规则。
2. **临时路径里的数字段掩成 ``<n>``**。``/tmp/pytest-of-root/pytest-12/...``
   里的 ``12`` 是本次运行的计数器,不是失败的身份。

字段缺失用固定哨兵
------------------
缺一个字段不是「这个字段是空的」,而是「我们不知道」。两者必须给不同的
指纹,所以缺失字段填固定哨兵 ``_MISSING``,而且**不能**把 ``None`` 字面量
拼进归一化输入 —— 那会让「字段缺失」和「字段内容恰好是 None」撞在一起。

已知的边界
----------
**临时路径**在两台机器之间是一致的(``<tmp>`` 占位),**项目内的相对路径**
也是(原样保留)。但一个**绝对的项目路径**(例如 ``/srv/app/proj/x.py``)
会原样留在指纹里 —— 所以指纹是「同一台机器的运行历史内」可比的,不是
跨机器可比的。跨机器比较失败指纹没有意义:那台机器上的失败根本不是这
台机器上的那次失败。

从指纹到决定
------------
指纹本身只是一个键。「根因有没有演进」这个**决定**由
:func:`needs_root_cause_task` 给出 —— 它是纯函数,读一条失败的历史
(连续失败几轮 + 前后两个指纹),回答「这一轮要不要先做根因任务,而不是
再生成一个盲目的修复任务」。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from collections.abc import Mapping
from typing import Any, List, Optional, Sequence

from failure_classification import (
    _HEX_RUN_RE,
    _NUMBER_RE,
    _PATH_TOKEN_RE,
    _TOKEN_SEP,
    _mask_hex,
    _normalize_path_token,
)

__all__ = [
    "FINGERPRINT_LENGTH",
    "canonical_fingerprint_input",
    "failure_fingerprint",
    "needs_root_cause_task",
]


#: The sentinel for a field the caller did not supply. A fixed string, so
#: the canonical input never carries a ``None`` literal into the hash.
_MISSING = "<missing>"

#: The fingerprint is the first ``FINGERPRINT_LENGTH`` hex digits of the
#: sha256 of the canonical input. 16 hex digits is 64 bits: short enough to
#: read out of a log line, wide enough that two unrelated failures in one
#: plan do not collide by accident.
FINGERPRINT_LENGTH = 16

#: Temporary roots that are spelled the same way on every machine. The
#: machine's own root (``tempfile.gettempdir()``) is added at call time —
#: macOS puts it under ``/var/folders/...``, Linux under ``/tmp`` — and
#: ``/tmp`` / ``/private/tmp`` are listed so the same failure fingerprints
#: the same on both.
_FIXED_TMP_ROOTS = ("/private/tmp", "/tmp")

#: The round on which a repeated failure becomes a *different* failure.
#:
#: The count is the number of consecutive rounds a failure has repeated —
#: one on the round it first failed. So ``2`` is the round on which the
#: failure repeats for the **second** time: the first round it failed, the
#: second round it failed again. That is the only round at which "it failed
#: again" and "it failed for a different reason" can be told apart.
_ROOT_CAUSE_ROUNDS = 2


def _tmp_roots() -> List[str]:
    """Every spelling of a temporary root, longest first.

    Longest first so ``/private/tmp`` is tried before ``/tmp``: on macOS
    ``/tmp`` is a symlink into ``/private/tmp``, and the longer spelling is
    the more specific one.
    """
    roots: List[str] = []
    for root in (tempfile.gettempdir(),) + _FIXED_TMP_ROOTS:
        normalized = os.path.normpath(root)
        if normalized and normalized != os.sep and normalized not in roots:
            roots.append(normalized)
    return sorted(roots, key=len, reverse=True)


def _tmp_relative(token: str) -> Optional[str]:
    """``<tmp>``-relative spelling of a temporary path, or ``None``.

    The comparison is case-insensitive on purpose: the text is lower-cased
    before it reaches here, so a temporary root spelled ``.../T`` arrives as
    ``.../t`` and no longer matches ``tempfile.gettempdir()`` exactly. This
    is the SAME rule :func:`_normalize_path_token` applies, not a second
    one — it only accepts the other case as well.
    """
    lowered = token.lower()
    for root in _tmp_roots():
        root_lower = root.lower()
        if lowered == root_lower:
            return "<tmp>"
        if lowered.startswith(root_lower + os.sep):
            # ``len(root)`` is the length of the ORIGINAL root; lower-casing
            # preserves it for the ASCII paths a temporary root is made of.
            return "<tmp>" + token[len(root):]
    return None


def _canonical_path_token(token: str) -> str:
    """One path, spelled the same way on every machine and every run."""
    normalized = _normalize_path_token(token)
    tmp = _tmp_relative(normalized)
    if tmp is None:
        if not normalized.startswith("<tmp>"):
            return normalized
        tmp = normalized
    # The digits of a temporary path are this run's counters, not the
    # failure's identity: ``pytest-12`` and ``pytest-987`` are the same run
    # of the same test.
    return "<tmp>" + _NUMBER_RE.sub("<n>", tmp[len("<tmp>"):])


def _normalize_text(text: str) -> str:
    """One free-text field, spelled the same way every run."""
    with_paths = _PATH_TOKEN_RE.sub(
        lambda match: _canonical_path_token(match.group(0)), text
    )
    lowered = with_paths.lower()
    masked = _HEX_RUN_RE.sub(_mask_hex, lowered)
    # The unit separator is the field delimiter; a stray one inside a value
    # would let two different field sets hash to the same input.
    return " ".join(masked.replace(_TOKEN_SEP, " ").split())


def _normalize_identifier(value: Any) -> str:
    """An identifier (a VP id, a failure class): case-folded, never masked.

    Identifiers are atomic names, not prose. They are deliberately NOT run
    through the hex/number masks: a VP id such as ``VP-abc12345`` would
    otherwise collapse into ``VP-<hex>`` and two different VPs would share
    a fingerprint.
    """
    if value is None:
        return _MISSING
    return " ".join(str(value).strip().lower().split())


def _normalize_scalar(value: Any) -> str:
    """One free-text field, with the missing/empty distinction kept."""
    if value is None:
        return _MISSING
    text = str(value).strip()
    if not text:
        return ""
    return _normalize_text(text)


def _normalize_assertions(value: Any) -> List[str]:
    """The failed assertions, normalized and sorted.

    Sorted, because the assertions are a SET: the same three assertions
    reported in a different order are the same failure.
    """
    if value is None:
        return [_MISSING]
    if isinstance(value, str):
        items: Sequence[Any] = [value]
    else:
        items = list(value)
    return sorted(_normalize_text(str(item)) for item in items)


def canonical_fingerprint_input(
    *,
    vp_id: Any = None,
    failure_class: Any = None,
    failed_assertions: Any = None,
    command: Any = None,
    first_error_line: Any = None,
) -> str:
    """The exact string the fingerprint is computed from.

    Exposed so a test can assert on the hashed input itself (the missing-
    field sentinel must be visible in it, and the ``None`` literal must
    not). It is the same string the fingerprint hashes — not a
    reconstruction of it.
    """
    parts = (
        "vp_id=" + _normalize_identifier(vp_id),
        "failure_class=" + _normalize_identifier(failure_class),
        "failed_assertions="
        + _TOKEN_SEP.join(_normalize_assertions(failed_assertions)),
        "command=" + _normalize_scalar(command),
        "first_error_line=" + _normalize_scalar(first_error_line),
    )
    return _TOKEN_SEP.join(parts)


def failure_fingerprint(
    *,
    vp_id: Any = None,
    failure_class: Any = None,
    failed_assertions: Any = None,
    command: Any = None,
    first_error_line: Any = None,
) -> str:
    """The deterministic identity of one failed attempt.

    Args:
        vp_id: The VP the failure belongs to, e.g. ``"VP-3"``.
        failure_class: The failure's classification, e.g.
            ``"deliverable_defect"``.
        failed_assertions: The assertions that failed. A set, not a
            sequence: the order does not change the fingerprint.
        command: The command that was run.
        first_error_line: The first line of the error output.

    Every argument is optional: an argument that is omitted and an
    argument that is ``None`` are the same thing — a field the caller does
    not have — and both carry the fixed ``_MISSING`` sentinel.

    Returns:
        16 lowercase hex digits: the first half of the sha256 of the
        canonical input. The same input always gives the same fingerprint,
        in this process and in any other.
    """
    payload = canonical_fingerprint_input(
        vp_id=vp_id,
        failure_class=failure_class,
        failed_assertions=failed_assertions,
        command=command,
        first_error_line=first_error_line,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def needs_root_cause_task(history: Mapping[str, Any]) -> bool:
    """Whether this failure needs a root-cause task instead of a repair.

    The old key (``VP-x|分类``) answers one question: *did the same failure
    happen again?* It cannot answer *is this the same root cause?* — two
    failures can share a classification and differ in everything that
    matters. So the loop keeps generating repair tasks for a failure whose
    cause has moved on.

    The fingerprint answers the second question, and this function turns
    that answer into a decision.

    Args:
        history: The failure's history. Read four keys:

            * ``vp_id`` — the verification point (carried along, not used
              to branch);
            * ``consecutive_failures`` — how many consecutive rounds this
              failure has repeated (one on the round it first failed);
            * ``fp_prev`` — the previous round's fingerprint of this
              point's failure;
            * ``fp_curr`` — this round's fingerprint of it.

    Returns:
        ``True`` only on the round where a failure repeats for the second
        time **and** its fingerprint changed — the round the root cause
        evolved. Everything else is ``False``:

        * one round of failure → not a repeat yet;
        * the same fingerprint → the *same* failure, which is the old
          key's business (the repeat count), not this branch's;
        * three rounds or more → the same-cause cap has been reached and
          the path that owns that verdict decides;
        * a missing key → we do not know, so we fail open to the old
          path. A record we cannot read must never raise: one
          unreadable payload must not be able to abort a round.

    The function is pure — same record in, same answer out, in this
    process and in any other.
    """
    if not isinstance(history, Mapping):
        return False

    rounds = history.get("consecutive_failures")
    if isinstance(rounds, bool) or not isinstance(rounds, int):
        return False
    if rounds != _ROOT_CAUSE_ROUNDS:
        return False

    fp_prev = history.get("fp_prev")
    fp_curr = history.get("fp_curr")
    if not isinstance(fp_prev, str) or not isinstance(fp_curr, str):
        return False
    if not fp_prev or not fp_curr:
        return False

    return fp_prev != fp_curr
