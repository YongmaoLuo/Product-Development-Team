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
import re
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
    "FINGERPRINT_RULE_IDENTITY",
    "FINGERPRINT_RULE_LEGACY",
    "canonical_fingerprint_input",
    "failure_fingerprint",
    "fingerprint_rule",
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


#: The rule a fingerprint was computed under.
#:
#: The value is 16 hex digits and that shape is a contract (the golden
#: fixture pins it), so the rule cannot ride *inside* the value. It
#: rides beside it instead: the loop's payload records the rule next to
#: the fingerprint pair, and the decision reads both.
#:
#: Why the rule has to be recorded at all: two fingerprints may only be
#: compared for equality when the same rule produced them. The legacy
#: rule hashes the run's prose; the identity rule hashes the failing
#: case set. The *same* failure therefore has two different values, and
#: comparing them reads "the instrument changed" as "the root cause
#: evolved" — which re-opens a root-cause task for a failure nobody
#: touched, every round, forever. That is the loop the report calls
#: structurally unable to converge.
#:
#: The id is versioned: change a rule and its id changes with it, so a
#: fingerprint written under the old rule is recognisably incomparable
#: with one written under the new.
FINGERPRINT_RULE_IDENTITY = "identity-v1"
FINGERPRINT_RULE_LEGACY = "legacy-v2"


# ---------------------------------------------------------------------------
# 运行噪声 —— 这几样描述的是「这一轮跑得怎么样」，不是「哪里坏了」
# ---------------------------------------------------------------------------
#
# 旧路的输入是一段运行摘要（耗时、负载、脱选数、进程号、时间戳）。它们每轮
# 都不同，于是同一批失败用例在两轮里得到两个指纹，「根因是否演进」这个问题
# 就没有答案了。这里把它们逐个掩掉 —— **只**掩这些，不做全局的数字掩码：
# 全局掩码会把 ``/tmp/a.py:12`` 里的 ``12`` 也掩掉，那会改掉旧路的字节。

#: ``duration_seconds=1559.31`` / ``elapsed: 49.3s`` —— 这一轮花了多久。
_NOISE_DURATION_RE = re.compile(
    r"\b(duration_seconds|duration|elapsed|took)\s*([:=])\s*\d+(?:\.\d+)?s?\b"
)

#: ``loadavg=22.46`` —— 这台机器当时有多忙。
_NOISE_LOAD_RE = re.compile(
    r"\b(loadavg|load_avg)\s*([:=])\s*\d+(?:\.\d+)?\b"
)

#: ``11787 deselected`` / ``3 passed`` —— pytest 摘要里的计数。
_NOISE_COUNT_RE = re.compile(
    r"\b\d+\s+(deselected|passed|failed|errors?|skipped|xfailed|xpassed"
    r"|warnings?|selected)\b"
)

#: ``pid=20464`` —— 这一次运行的进程号。
_NOISE_PID_RE = re.compile(r"\b(pid|ppid|pgid)\s*([:=])\s*\d+\b")

#: ``2026-11-02T01:46:45Z`` —— 这一次运行的时间戳。文本在掩码**之前**已经被
#: 转成小写，所以 ``T`` / ``Z`` 两种写法都要认。
_NOISE_TIMESTAMP_RE = re.compile(
    r"\d{4}-\d{2}-\d{2}[Tt ]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"
    r"(?:Z|z|[+-]\d{2}:?\d{2})?"
)

#: 掩码的顺序。计数在前面（它会吃掉 ``3 passed`` 这种带数字的片段），
#: 时间戳在后面（它要看到完整的形状）。
_NOISE_MASKS = (
    (_NOISE_DURATION_RE, r"\1\2<n>"),
    (_NOISE_LOAD_RE, r"\1\2<n>"),
    (_NOISE_COUNT_RE, r"<n> \1"),
    (_NOISE_PID_RE, r"\1\2<n>"),
    (_NOISE_TIMESTAMP_RE, "<ts>"),
)


def _mask_run_noise(text: str) -> str:
    """Hide what the run looked like, keep what broke."""
    for pattern, replacement in _NOISE_MASKS:
        text = pattern.sub(replacement, text)
    return text


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
    quiet = _mask_run_noise(lowered)
    masked = _HEX_RUN_RE.sub(_mask_hex, quiet)
    # The unit separator is the field delimiter; a stray one inside a value
    # would let two different field sets hash to the same input.
    return " ".join(masked.replace(_TOKEN_SEP, " ").split())


def _normalize_case_id(text: str) -> str:
    """One failing case's id, spelled the same way every run.

    A case id is an **identifier**, not prose: ``tests/x.py::TestY::test_z``.
    So the case is kept, the digits are kept, the spelling is kept — a
    pytest node id is case sensitive, and two node ids differing only in
    case are two different tests. Only the temporary paths inside it are
    normalized, for the same reason as everywhere else.
    """
    with_paths = _PATH_TOKEN_RE.sub(
        lambda match: _canonical_path_token(match.group(0)), text
    )
    return " ".join(with_paths.replace(_TOKEN_SEP, " ").split())


def _normalize_case_ids(value: Any) -> List[str]:
    """The failing case ids, deduplicated and sorted.

    Sorted and deduplicated, because the failing cases are a SET: the same
    nine cases reported in a different order (or with one of them listed
    twice) are the same failure.
    """
    if value is None:
        return []
    if isinstance(value, str):
        items: Sequence[Any] = [value]
    else:
        items = list(value)
    normalized = set()
    for item in items:
        text = str(item).strip()
        if not text:
            continue
        normalized.add(_normalize_case_id(text))
    return sorted(normalized)


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
    failed_cases: Any = None,
) -> str:
    """The exact string the fingerprint is computed from.

    Exposed so a test can assert on the hashed input itself (the missing-
    field sentinel must be visible in it, and the ``None`` literal must
    not). It is the same string the fingerprint hashes — not a
    reconstruction of it.

    Two shapes exist:

    * **身份态** (``failed_cases`` given) — the input is only
      ``vp_id`` / ``failure_class`` / the failing case set. The prose
      (``command`` / ``first_error_line``) describes the RUN, not the
      defect, so it does not belong in the identity: the same nine cases
      failing for the same reason must fingerprint the same however long
      the run took and however busy the machine was.
    * **旧路** (``failed_cases`` absent or empty) — the five-part input
      that has always been used, byte for byte. The golden fingerprints
      pinned against it must keep holding.
    """
    cases = _normalize_case_ids(failed_cases)
    if cases:
        parts = (
            "vp_id=" + _normalize_identifier(vp_id),
            "failure_class=" + _normalize_identifier(failure_class),
            "failed_cases=" + _TOKEN_SEP.join(cases),
        )
        return _TOKEN_SEP.join(parts)

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
    failed_cases: Any = None,
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
        failed_cases: The **machine-written** ids of the failing cases
            (pytest node ids). Given them, the fingerprint is the
            identity of the failing set — the prose above is deliberately
            left out, because it measures the run and not the defect.
            Left out, the legacy five-part input is used unchanged.

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
        failed_cases=failed_cases,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:FINGERPRINT_LENGTH]


def fingerprint_rule(*, failed_cases: Any = None) -> str:
    """Which rule produced — or would produce — this fingerprint.

    The rule is the *basis* of the value, and the basis decides whether
    two fingerprints may be compared at all:

    * :data:`FINGERPRINT_RULE_IDENTITY` — the fingerprint is the identity
      of the failing case set (``vp_id`` / ``failure_class`` / the set).
      The run's prose does not enter the hash.
    * :data:`FINGERPRINT_RULE_LEGACY` — the fingerprint is the identity of
      the five-part text input. Used when no failing set is known.

    The two rules give the same failure two different values. That is
    fine on its own — a fingerprint is a key — and it is fatal when the
    two are compared: the difference then says "the instrument changed",
    not "the defect changed". So the rule has to be recorded next to the
    value, and the decision has to read it.

    Args:
        failed_cases: The machine-written ids of the failing cases — the
            same argument :func:`failure_fingerprint` takes. Whether they
            are given decides which rule hashes the value.

    Returns:
        The rule's id. Never empty: a caller that cannot tell which rule
        applied is told so by the *absence* of a recorded rule, not by a
        special value.
    """
    if _normalize_case_ids(failed_cases):
        return FINGERPRINT_RULE_IDENTITY
    return FINGERPRINT_RULE_LEGACY


def _known_rule(value: Any) -> Optional[str]:
    """The rule id a record carries, or ``None`` when it carries none.

    A rule that is not a non-empty string is a rule we cannot read — the
    same "we do not know" the missing-field sentinel stands for. It must
    never raise: one unreadable record must not be able to abort a round.
    """
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _rules_are_comparable(rule_prev: Any, rule_curr: Any) -> bool:
    """Whether two fingerprints may be compared for equality at all.

    Two fingerprints are comparable when the same rule produced them.
    Comparing across rules reads the change of the measuring instrument
    as a change of the defect, which is how the loop ends up opening a
    root-cause task for a failure nobody touched.

    The three cases:

    * **both rules known and equal** — comparable; the fingerprints may
      be compared, and a difference means the defect changed.
    * **both rules known and different** — not comparable. We cannot say
      anything about the defect, so we say nothing about the root cause.
    * **one or both rules unknown** — the records pre-date the rule ids
      (or are unreadable). When *neither* side names a rule, the
      comparison is the one the loop has always made and it stays. When
      only one side names a rule, the other side's rule is unknown, so
      the two may have been made by different rules: not comparable.

    The "not comparable" answer is a *false*, not an error: the round
    then hands the failure to a repair task instead of re-opening a
    diagnosis, which is the direction that lets the chain close.
    """
    known_prev = _known_rule(rule_prev)
    known_curr = _known_rule(rule_curr)
    if known_prev is None and known_curr is None:
        return True
    if known_prev is None or known_curr is None:
        return False
    return known_prev == known_curr


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
        history: The failure's history. Read four keys, plus two
            optional ones:

            * ``vp_id`` — the verification point (carried along, not used
              to branch);
            * ``consecutive_failures`` — how many consecutive rounds this
              failure has repeated (one on the round it first failed);
            * ``fp_prev`` — the previous round's fingerprint of this
              point's failure;
            * ``fp_curr`` — this round's fingerprint of it;
            * ``rule_prev`` / ``rule_curr`` — **optional** — the rule
              each of the two fingerprints was computed under (see
              :func:`fingerprint_rule`). Records written before the
              rules existed simply do not carry them.

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
          unreadable payload must not be able to abort a round;
        * **the two fingerprints were computed under different rules** →
          not comparable, so no verdict: the difference between them
          says the instrument changed, not the defect, and reading it
          as "the root cause evolved" would re-open a diagnosis for a
          failure nobody touched. The round hands the failure to a
          repair task instead, which is the direction that lets the
          chain close.

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

    if not _rules_are_comparable(
        history.get("rule_prev"), history.get("rule_curr"),
    ):
        return False

    return fp_prev != fp_curr
