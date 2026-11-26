"""共存目录的伴随用例 —— 三态来源、只读消费、计数器的对照。

主用例（:mod:`tests.e2e.test_verification_plan_coexist_e2e`）走的是**一条**路：
目录里躺着已审阅的计划书，于是来源是 ``REVIEWED``。这份文件补上另外三件事。

1. **来源的三态**（:func:`test_the_source_is_decided_by_what_is_on_disk`）——
   ``REVIEWED`` / ``MACHINE_ONLY`` / ``NONE`` 三态互斥，且由磁盘状态唯一决定。
   主用例只证明了其中一态；**一态成立不等于三态互斥**。

2. **只读消费**（:func:`test_the_reviewed_plan_is_consumed_without_asking_the_model`）
   —— 已审阅的计划书是真源，消费它的那一趟**只读不写、也不问模型**。主用例
   证明了机器计划被消费，没证明消费它的那一趟没顺手改写它、没偷偷问一次模型。

3. **计数器的对照**（:func:`test_the_counter_sees_a_phase1_call_when_the_source_is_none`）
   —— 主用例断言「Phase-1 是 0 次」。**一个从不触发的计数器也能满足这个断言**，
   所以这里把来源钉成 ``NONE``：Phase-1 于是**应该**跑一次，计数器必须看得见
   它。看得见 ⇒ 主用例的那个 0 是测量结果，不是计数器没醒。

4. **负控的前提**（:func:`test_negctl_forcing_none_fails_prelude_case`）
   —— ``scripts/negctl_p0_plan_source.sh`` 把来源钉成 ``NONE``，然后要求主用例
   转红。这条用例把那句话的前提写进套件：钉子一旦失灵，套件先于负控脚本发现。

下面这一段（任务 13）补的是另外四件事 —— 主用例只覆盖 ``REVIEWED`` 那一态，
剩下的是**反向**与**契约**：

5. **另一态**（:func:`test_e2e_without_pre_generated_plan_uses_llm_stub`）——
   目录里既没有计划书也没有机器计划时（来源 ``NONE``），产品必须问一次模型
   （Phase-1），而 scripted 替身必须**答得上来**。替身被调用过 ≠ 替身有效。

6. **生成的副作用**（:func:`test_e2e_generate_does_not_start_execution`）——
   生成计划书是一次纯粹的写文档动作：它不得顺手把验证轮拉起来，也不得推进
   相位。

7. **改写使旧审核失效**（:func:`test_e2e_review_after_refine_invalidates_approvals`）
   —— 审核是针对**某一份文档字节**的；改写换了字节，那份审核针对的就是另一份
   文档了。

8. **审阅面自己的契约**（:func:`test_e2e_review_contract_unchanged`）——
   整篇粒度、一份状态文件、没有 ``plan_item_id``。

9. **守卫**（:func:`test_e2e_real_http_requires_request_guard_header`）——
   裸请求（不带 ``X-PDT-Request``）被挡下。测试助手 :func:`_request` **总是**
   带着那个头，所以它量不到这一条。
"""

from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

from config_paths import resolve_state_db_path
from plan_artifacts import (
    MACHINE_PLAN_FILENAME,
    PLAN_DOC_FILENAME,
    PlanSource,
    resolve_plan_source,
)
from request_guard import REQUEST_HEADER, REQUEST_HEADER_VALUE
from tests.e2e.test_verification_plan_coexist_e2e import (
    FORCED_SOURCE_ENV,
    Server,
    _accept_every_item,
    _read_records,
    _request,
    _wait_for_the_round_to_settle,
    backend,  # noqa: F401  (fixture re-exported into this module)
    backend_factory,  # noqa: F401  (fixture re-exported into this module)
    coexist_plan,  # noqa: F401  (fixture re-exported into this module)
    counter_file,  # noqa: F401  (fixture re-exported into this module)
    count_buckets,
    hermetic_env,  # noqa: F401  (fixture re-exported into this module)
    scripted_scripts,  # noqa: F401  (fixture re-exported into this module)
)

# The shared module is where the promoted fixtures live — pytest resolves a
# fixture in the namespace of the module that *asks* for it, so a fixture
# living in a sibling test module cannot be used here at all.  This lane
# consumes one of them by name.
from tests.fixtures.verification_mode import (  # noqa: E402
    make_dev_plan,  # noqa: F401  (fixture consumed by name in this module)
)
from verification_persistence import REPORT_EXTENSION_FIELDS
from verification_plan_review import REVIEW_FILENAME

#: 与主用例同一条 lane：这些伴随用例同样起真子进程、写真实文件系统。
#:
#: 一条流程和它的对照用例被拆到两条 lane 上，对照就失去了对照的意义 ——
#: 它们量的必须是同一个东西。
pytestmark = [pytest.mark.e2e]


def test_the_source_is_decided_by_what_is_on_disk(make_dev_plan) -> None:
    """三态互斥：由磁盘上的产物唯一决定，不由分支形状决定。

    同一份目录，三种内容，三种取值 —— 并且**没有第四种**。判据是产物在不在，
    所以目录里同时有任务清单与计划书时，取值仍然是 ``REVIEWED``（共存合法、
    任务优先，但消费哪一份计划由计划书决定）。

    这份目录是共享模块的 ``make_dev_plan`` 造的：开发分支的计划目录里**本来
    就躺着 ``tasks.json``**，所以「共存」那一段的前提是被测对象自己的形状，
    不是这条用例手写出来的。
    """
    plan_dir = make_dev_plan().plan_dir

    # The development branch's task list is already on disk: the fixture
    # built the directory, this case did not write it by hand.
    assert (plan_dir / "tasks.json").is_file(), (
        "the shared fixture did not build a development-branch plan directory"
    )

    # Nothing yet: there is no plan to consume.
    assert resolve_plan_source(plan_dir) is PlanSource.NONE

    # The machine plan alone: it is a derived artifact, and a plan without
    # the document that produced it has not been reviewed.
    (plan_dir / MACHINE_PLAN_FILENAME).write_text(
        json.dumps({"verification_points": []}), encoding="utf-8"
    )
    assert resolve_plan_source(plan_dir) is PlanSource.MACHINE_ONLY

    # The document: the reviewed source of truth.  The machine plan's
    # presence does not change this.
    (plan_dir / PLAN_DOC_FILENAME).write_text("# 验证计划书\n", encoding="utf-8")
    assert resolve_plan_source(plan_dir) is PlanSource.REVIEWED

    # Coexistence: the development branch's task list sits next to the
    # document, and the source is still the document.
    assert resolve_plan_source(plan_dir) is PlanSource.REVIEWED


def test_the_reviewed_plan_is_consumed_without_asking_the_model(
    backend,
    coexist_plan,
    counter_file: Path,
) -> None:
    """已审阅的计划书被消费时：只读、不写、不问模型。

    机器计划**已经在盘上**（它是审核过的计划书的派生物），于是这一趟既不该
    翻译一次，也不该问模型一次，更不该把盘上那份改写掉。
    """
    plan_id = coexist_plan.plan_id
    plan_dir = coexist_plan.plan_dir

    # The reviewed document and the machine plan derived from it.
    (plan_dir / PLAN_DOC_FILENAME).write_text(coexist_plan.doc, encoding="utf-8")
    (plan_dir / MACHINE_PLAN_FILENAME).write_text(
        json.dumps(coexist_plan.machine_plan, ensure_ascii=False), encoding="utf-8"
    )
    doc_bytes = (plan_dir / PLAN_DOC_FILENAME).read_bytes()
    machine_bytes = (plan_dir / MACHINE_PLAN_FILENAME).read_bytes()

    status, body = _request(
        backend, "POST", f"/api/verification/{plan_id}/start", {"auto_fix": False}
    )
    assert status == 200, f"starting the verification round failed: {status} {body}"

    # The round consumed the machine plan that was on disk.
    _wait_for_the_round_to_settle(backend, plan_id)

    assert (plan_dir / PLAN_DOC_FILENAME).read_bytes() == doc_bytes, (
        "the round rewrote the reviewed plan document: consuming it must be "
        "read-only"
    )
    assert (plan_dir / MACHINE_PLAN_FILENAME).read_bytes() == machine_bytes, (
        "the round rewrote the machine plan that was already on disk: the "
        "reviewed plan is the source of truth and this trip reads it"
    )

    counts = count_buckets(_read_records(counter_file))
    assert counts.get("translate", 0) == 0, (
        "the plan document was translated although the machine plan was "
        f"already on disk: {counts}"
    )
    assert counts.get("phase1", 0) == 0, (
        "the Phase-1 generation ran although a reviewed plan document was "
        f"on disk: {counts}"
    )


def test_the_counter_sees_a_phase1_call_when_the_source_is_none(
    backend_factory,
    coexist_plan,
    counter_file: Path,
) -> None:
    """计数器的对照：来源钉成 ``NONE``，Phase-1 必须被数到。

    这是主用例那个「Phase-1 是 0 次」的**对照组**：同一个计数器、同一个子进程
    通道、同样的分类规则，这一次 Phase-1 真的跑了 —— 于是计数器看得见它。

    看得见 ⇒ 主用例的 0 次是**测量结果**，不是「没数到」。
    """
    # The child reads this when it starts.  It is applied to the child's
    # environment at spawn time, so it does not depend on fixture ordering
    # and it does not touch this process's own environment.
    server = backend_factory({FORCED_SOURCE_ENV: "none"})
    try:
        plan_id = coexist_plan.plan_id
        status, body = _request(
            server, "POST", f"/api/verification/{plan_id}/start", {"auto_fix": False}
        )
        assert status == 200, (
            f"starting the verification round failed: {status} {body}"
        )

        _wait_for_the_round_to_settle(server, plan_id)

        counts = count_buckets(_read_records(counter_file))
        _status, round_state = _request(
            server, "GET", f"/api/verification/{plan_id}/status"
        )
        assert counts.get("phase1", 0) >= 1, (
            "the counter did not see the Phase-1 call that a plan with no "
            "reviewed document must make — so the zero the main case asserts "
            f"is not a measurement: {counts}. Round state: {round_state}. "
            f"Backend output:\n{server.log_tail()}"
        )
    finally:
        server.stop()


def test_negctl_forcing_none_fails_prelude_case(
    backend_factory,
    coexist_plan,
    counter_file: Path,
) -> None:
    """负控的前提，就写在套件里：钉成 ``NONE`` 之后，前奏用例必须失败。

    ``scripts/negctl_p0_plan_source.sh`` 的全部力量来自一句话：**把来源钉成
    ``NONE``，共存主用例就必须转红**。这句话是承重的 —— 钉子没钉住，主用例照样
    绿，负控就只会报一句「差异不存在」，而它量到的是「没有抱怨」，不是产品。

    所以这里把那句话写下来，并且**只写前提**：把钉子钉上，主用例的那条断言
    （机器计划必须落盘）就不再成立 —— 计划书明明在盘上，来源却被钉成 ``NONE``，
    前奏于是拒绝翻译，Phase-1 顶上来，机器计划没有落盘。

    钉子本身由 :func:`backend_factory` 传给子进程（:data:`FORCED_SOURCE_ENV`），
    不依赖测试的执行顺序，也不碰本进程的环境。

    钉子一旦失灵（产品改了 ``NONE`` 的语义，或者子进程没读到那个变量），
    这条用例会**先于**负控脚本转绿 —— 负控脚本于是知道自己量到的是什么。
    """
    plan_id = coexist_plan.plan_id

    # The reviewed document is on disk.  That is the point: the source is
    # NONE **only** because of the pin, not because the directory is empty.
    (coexist_plan.plan_dir / PLAN_DOC_FILENAME).write_text(
        coexist_plan.doc, encoding="utf-8"
    )

    server = backend_factory({FORCED_SOURCE_ENV: "none"})
    try:
        status, body = _request(
            server, "POST", f"/api/verification/{plan_id}/start", {"auto_fix": False}
        )
        assert status == 200, (
            f"starting the verification round failed: {status} {body}"
        )

        _wait_for_the_round_to_settle(server, plan_id)

        assert not coexist_plan.machine_plan_path.exists(), (
            "the machine plan landed although the plan source was pinned to "
            "NONE — the pin no longer stops the prelude from translating, so "
            "the negative control would be measuring a difference that the "
            "product does not make"
        )

        counts = count_buckets(_read_records(counter_file))
        assert counts.get("translate", 0) == 0, (
            "the plan document was translated although the plan source was "
            f"pinned to NONE: {counts}"
        )
        assert counts.get("phase1", 0) >= 1, (
            "with the source pinned to NONE the Phase-1 generation is what "
            f"runs, and it was not counted: {counts}"
        )
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# 任务 13：反向与契约用例
# ---------------------------------------------------------------------------
#
# 起后端、脚本目录、计数器一律用任务 12 的（``backend_factory`` /
# ``scripted_scripts`` / ``counter_file``），这里不复制第二套 —— 复制出来的
# 第二套会跟第一套漂移，而漂移的方向恰好是「对照组不再是对照组」。


def _register_scripted_answer(
    script_dir: Path, key: str, matcher: str, answer: str
) -> None:
    """在任务 12 的脚本目录里登记一条答案。

    登记的是**提示词的身份**（一段稳定的片段），不是提示词全文：真实提示词
    带时间戳、计划 id 与文件内容，全文匹配几乎不可能命中。替身按最长片段优先
    匹配（见 :class:`coding_tool.ScriptedLLMRegistry`），所以片段要挑得够窄。

    Args:
        script_dir: 脚本目录（任务 12 的 ``scripted_scripts`` 夹具给的就是它）。
        key: 答案的键名；答案文件是 ``<key>.txt``。
        matcher: 提示词里那段能认出这次调用的文字。
        answer: 答案正文。
    """
    registry_path = script_dir / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry.setdefault("prompts", {})[key] = matcher
    registry_path.write_text(
        json.dumps(registry, ensure_ascii=False), encoding="utf-8"
    )
    (script_dir / f"{key}.txt").write_text(answer, encoding="utf-8")


def _verification_row(plan_id: str) -> Optional[Dict[str, Any]]:
    """``plan_verification`` 那一行 —— 验证执行器的**持久**状态。

    执行器状态（``executor_state``）、轮次（``round``）与起始时间
    （``started_at``）都住在这张表里。轮次跑过就一定有行；没跑过就没有行 ——
    所以「这一行不存在」是「执行器没启动过」最直接的证据。

    Args:
        plan_id: 计划 id。

    Returns:
        那一行（字典），或者 ``None``（这张表里没有这个计划）。
    """
    from state_machine.db.connection import open as open_db

    conn = open_db(resolve_state_db_path())
    try:
        conn.row_factory = sqlite3.Row
        cursor = conn.execute(
            "SELECT * FROM plan_verification WHERE plan_id = ?", (plan_id,)
        )
        row = cursor.fetchone()
        if row is None:
            return None
        return {key: row[key] for key in row.keys()}
    finally:
        conn.close()


def _raw_request(
    server: Any,
    method: str,
    path: str,
    headers: Optional[Dict[str, str]] = None,
    timeout: float = 30.0,
) -> Tuple[int, Any]:
    """发一个**不带**测试助手那套头的请求。

    :func:`_request` 总是带着守卫头（``X-PDT-Request``），所以它量不到
    「守卫把裸请求挡下了」。这里发的是最朴素的一个请求：只有自己给的那些头。

    Args:
        server: 后端（:class:`Server`）或它的 base URL。
        method: HTTP 方法。
        path: 路径。
        headers: 要带的头；``None`` 就是一个不带自定义头的请求。
        timeout: 超时秒数。

    Returns:
        ``(状态码, 载荷)``。
    """
    base_url = server.base_url if isinstance(server, Server) else str(server)
    request = urllib.request.Request(
        base_url + path, method=method, headers=dict(headers or {})
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            body = response.read().decode("utf-8")
            return int(response.status), (json.loads(body) if body else {})
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8")
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = {"raw": body}
        return int(exc.code), parsed


def _results_are_empty(results: Any) -> bool:
    """结果载荷里有没有内容。

    空载荷**不是** ``{}``：产品给的是一个空骨架（``pytest_summary`` /
    ``llm_findings`` / ``performance_metrics`` 三项，值都是空的）。所以判据是
    「每一项都没有内容」，不是「载荷本身是假值」。

    Args:
        results: 状态端点里的 ``results``。

    Returns:
        True 表示没有内容。
    """
    if not isinstance(results, dict):
        return not results
    for value in results.values():
        if isinstance(value, (dict, list)):
            if value:
                return False
        elif str(value).strip():
            return False
    return True


def _refined_variant(doc: str) -> str:
    """一份**改过**的计划书：基线行与验证点 id 都不动，正文多一句话。

    改写契约只要求两件事：基线行逐字不变、验证点 id 集合不变。所以在最后
    一段（``## 风险面``）的正文里加一句话就够了 —— 它碰不到基线、碰不到
    章节标题、碰不到任何一条验证点。

    为什么要真的改字节：改写结果与原文逐字节相同时，哈希不变，旧哈希照样
    能用 —— 那样量到的是「重置」，不是「失效」。

    Args:
        doc: 原文。

    Returns:
        改过之后的文档。
    """
    lines = doc.splitlines()
    trailing = "\n" if doc.endswith("\n") else ""
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].startswith("## "):
            lines.insert(index + 1, "本次改写把风险面收窄到本次改动本身。")
            break
    else:  # pragma: no cover — the fixture document always has sections
        raise AssertionError("the plan document has no section to refine")
    return "\n".join(lines) + trailing


def test_e2e_without_pre_generated_plan_uses_llm_stub(
    backend_factory,
    coexist_plan,
    scripted_scripts: Path,
    counter_file: Path,
) -> None:
    """没有计划书也没有机器计划时：走 Phase-1，scripted 替身答得上来。

    主用例只覆盖 ``REVIEWED`` 那一态。这里的对照是另外一态：目录里既没有
    计划书（``verification-plan.md``）也没有机器计划（``verification_plan.json``），
    来源于是 ``NONE`` —— 产品必须**问一次模型**（Phase-1），而不是拿盘上的旧货
    凑合。

    断言的是**替身有效**，不是「替身被调用过」：Phase-1 的答案必须被采纳，
    机器计划必须因此落盘。一个被调用却答不上来的替身，证明不了任何事。

    Args:
        backend_factory: 任务 12 的后端工厂（带额外环境变量的那个）。
        coexist_plan: 共存目录的夹具。
        scripted_scripts: 任务 12 的脚本目录。
        counter_file: 模型调用计数文件。
    """
    plan_id = coexist_plan.plan_id
    plan_dir = coexist_plan.plan_dir

    # The input: neither artifact is on disk.
    assert not (plan_dir / PLAN_DOC_FILENAME).exists(), (
        "the plan document is on disk, so the source is not NONE and this "
        "case would be measuring the reviewed path"
    )
    assert not (plan_dir / MACHINE_PLAN_FILENAME).exists(), (
        "the machine plan is on disk, so the source is MACHINE_ONLY and this "
        "case would be measuring the reuse path"
    )

    # Register the answer for the Phase-1 prompt.  The marker is the one
    # section heading that only the planning prompt carries.
    _register_scripted_answer(
        scripted_scripts,
        "verification_phase1",
        "## 本次任务清单",
        json.dumps(coexist_plan.machine_plan, ensure_ascii=False),
    )

    server = backend_factory()
    try:
        status, body = _request(
            server, "POST", f"/api/verification/{plan_id}/start", {"auto_fix": False}
        )
        assert status == 200, f"starting the verification round failed: {status} {body}"

        _wait_for_the_round_to_settle(server, plan_id)

        counts = count_buckets(_read_records(counter_file))
        assert counts.get("phase1", 0) >= 1, (
            "the plan source was NONE, so the Phase-1 generation is what runs, "
            f"and it was not counted: {counts}. Backend output:\n"
            f"{server.log_tail()}"
        )

        # The double is *effective*: its answer was adopted, so the machine
        # plan is on disk.
        assert coexist_plan.machine_plan_path.exists(), (
            "the Phase-1 answer was never adopted: the scripted double refused "
            "the prompt, so the round produced no machine plan at all. Counts: "
            f"{counts}. Backend output:\n{server.log_tail()}"
        )
        landed = json.loads(
            coexist_plan.machine_plan_path.read_text(encoding="utf-8")
        )
        landed_ids = [
            point.get("id") for point in landed.get("verification_points") or []
        ]
        assert landed_ids == ["VP-001"], (
            "the machine plan on disk is not the one the scripted double "
            f"answered with: {landed_ids}"
        )

        # No document on disk, so there is nothing to translate.
        assert counts.get("translate", 0) == 0, (
            "a plan document was translated although there is none on disk: "
            f"{counts}"
        )
    finally:
        server.stop()


def test_e2e_generate_does_not_start_execution(
    backend,
    coexist_plan,
    counter_file: Path,
) -> None:
    """生成计划书**不启动执行**：执行器没动，相位没推进。

    共存目录里生成计划书是一次纯粹的写文档动作。它不得顺手把验证轮拉起来 ——
    验证轮是另一次显式的启动；相位是开发流程自己的事，生成与否都不改它。

    判据是**执行器的持久状态**（``plan_verification`` 那张表：轮次、执行器
    状态、起始时间）与状态端点对外的那张脸。两者都得说「没跑过」。

    Args:
        backend: 任务 12 起的后端。
        coexist_plan: 共存目录的夹具。
        counter_file: 模型调用计数文件。
    """
    plan_id = coexist_plan.plan_id
    plan_dir = coexist_plan.plan_dir

    before_row = _verification_row(plan_id)
    status, before_status = _request(
        backend, "GET", f"/api/verification/{plan_id}/status"
    )
    assert status == 200, f"reading the verification status failed: {status}"

    # --- 生成 ----------------------------------------------------------
    status, body = _request(
        backend, "POST", f"/api/verification-plan/{plan_id}/generate", {}
    )
    assert status == 200, f"generating the plan document failed: {status} {body}"
    assert coexist_plan.doc_path.exists(), (
        "the generate endpoint answered 200 but no plan document is on disk"
    )

    # --- 生成之后：执行器没启动 ------------------------------------------
    after_row = _verification_row(plan_id)
    assert after_row == before_row, (
        "generating the plan document wrote the verification executor's row: "
        f"the executor started. Before: {before_row}. After: {after_row}"
    )
    if after_row is not None:
        assert not after_row.get("executor_state"), (
            "the executor wrote its state although only the plan document "
            f"was generated: {after_row}"
        )
        assert after_row.get("round") == 0, (
            f"a verification round was counted although only the plan "
            f"document was generated: {after_row}"
        )
        assert not after_row.get("started_at"), (
            f"the executor started although only the plan document was "
            f"generated: {after_row}"
        )

    status, after_status = _request(
        backend, "GET", f"/api/verification/{plan_id}/status"
    )
    assert status == 200, f"reading the verification status failed: {status}"
    for key in ("verification_status", "verification_round", "started_at", "results"):
        assert after_status.get(key) == before_status.get(key), (
            f"the executor's status changed on {key!r} because the plan "
            f"document was generated: {before_status.get(key)!r} -> "
            f"{after_status.get(key)!r}"
        )
    assert after_status.get("verification_round") == 0, (
        "a verification round was counted although only the plan document was "
        f"generated: {after_status.get('verification_round')}"
    )
    assert not after_status.get("started_at"), (
        "the executor started although only the plan document was generated: "
        f"{after_status.get('started_at')!r}"
    )
    assert _results_are_empty(after_status.get("results")), (
        "the executor produced results although only the plan document was "
        f"generated: {after_status.get('results')!r}"
    )

    # Neither the machine plan nor a report is on disk: no translation and
    # no round happened.
    assert not coexist_plan.machine_plan_path.exists(), (
        "the machine plan landed although only the plan document was "
        "generated: the verification round was started"
    )
    assert not (plan_dir / "verification_report.json").exists(), (
        "the round wrote a report although only the plan document was "
        "generated: the verification round was started"
    )

    # --- 相位未推进 -----------------------------------------------------
    status, state = _request(backend, "GET", f"/api/plan/{plan_id}/state")
    assert status == 200, f"reading the plan state failed: {status} {state}"
    assert state.get("current_phase") == "executing", (
        "generating the plan document moved the development plan's phase: "
        f"{state.get('current_phase')!r}"
    )

    # --- 只发生了一次「生成」调用 ---------------------------------------
    counts = count_buckets(_read_records(counter_file))
    assert counts.get("generate", 0) == 1, (
        f"the plan document was not generated exactly once: {counts}"
    )
    assert counts.get("translate", 0) == 0, (
        "the plan document was translated although generating it must not "
        f"start anything: {counts}"
    )
    assert counts.get("phase1", 0) == 0, (
        "the Phase-1 generation ran although generating the plan document "
        f"must not start anything: {counts}"
    )


def test_e2e_review_after_refine_invalidates_approvals(
    backend,
    coexist_plan,
    scripted_scripts: Path,
) -> None:
    """生成 → 逐条 accept → refine：旧审核全部作废。

    审核是**针对某一份文档字节**的。refine 改了字节，那份审核针对的就是另一份
    文档了 —— 于是条目必须全部回到 ``pending``，落盘的审核状态必须绑上新哈希，
    而带着旧 ``doc_sha256`` 的动作必须被拒（409 ``document_changed``）。

    注意：refine 的答案必须**真的改字节**。改写结果与原文逐字节相同时，哈希
    不变，旧哈希照样能用 —— 那样这条用例量到的是「重置」，不是「失效」。

    Args:
        backend: 任务 12 起的后端。
        coexist_plan: 共存目录的夹具。
        scripted_scripts: 任务 12 的脚本目录。
    """
    plan_id = coexist_plan.plan_id
    plan_dir = coexist_plan.plan_dir

    # The rewrite answers with a document whose bytes differ from the
    # original -- same baseline line, same verification-point ids.
    refined_doc = _refined_variant(coexist_plan.doc)
    assert refined_doc != coexist_plan.doc, (
        "the refined variant is byte-identical to the original, so the old "
        "hash would still be the current one"
    )
    (scripted_scripts / "verification_plan_refine.txt").write_text(
        refined_doc, encoding="utf-8"
    )

    # --- 1. 生成 --------------------------------------------------------
    status, body = _request(
        backend, "POST", f"/api/verification-plan/{plan_id}/generate", {}
    )
    assert status == 200, f"generating the plan document failed: {status} {body}"
    assert coexist_plan.doc_path.exists(), (
        "the generate endpoint answered 200 but no plan document is on disk"
    )
    original_bytes = coexist_plan.doc_path.read_bytes()

    # --- 2. 逐条 accept --------------------------------------------------
    status, body = _request(
        backend, "GET", f"/api/verification-plan/{plan_id}/review/items"
    )
    assert status == 200, f"reading the review items failed: {status} {body}"
    original_sha = body.get("doc_sha256")
    assert original_sha, "the review surface reported no document hash"
    items = body.get("items") or []
    assert items, "the review surface reported no items to accept"

    _accept_every_item(backend, plan_id)

    status, body = _request(
        backend, "GET", f"/api/verification-plan/{plan_id}/review/items"
    )
    assert all(item["status"] == "accepted" for item in body["items"]), (
        f"not every item is accepted after the review: {body['items']}"
    )

    # --- 3. refine ------------------------------------------------------
    status, body = _request(
        backend,
        "POST",
        f"/api/verification-plan/{plan_id}/refine",
        {"feedback": "把验证范围收窄到这次改动本身。"},
    )
    assert status == 200, f"refining the plan document failed: {status} {body}"
    assert coexist_plan.doc_path.read_bytes() != original_bytes, (
        "the rewrite did not change the document's bytes, so the old hash is "
        "still the current one -- this case would be measuring a reset, not "
        "an invalidation"
    )
    new_sha = body.get("doc_sha256")
    assert new_sha and new_sha != original_sha, (
        f"the rewrite did not change the document's hash: {original_sha} -> "
        f"{new_sha}"
    )

    # Every item is pending again.
    status, body = _request(
        backend, "GET", f"/api/verification-plan/{plan_id}/review/items"
    )
    assert status == 200, f"reading the review items failed: {status} {body}"
    assert body.get("items"), "the review surface reported no items after the rewrite"
    assert all(item["status"] == "pending" for item in body["items"]), (
        "the rewrite did not send the review back to pending: "
        f"{body['items']}"
    )
    assert body.get("doc_sha256") == new_sha, (
        f"the review surface reports a different hash than the rewrite did: "
        f"{body.get('doc_sha256')!r} != {new_sha!r}"
    )

    # The state on disk is bound to the new hash, and every item is pending.
    state = json.loads((plan_dir / REVIEW_FILENAME).read_text(encoding="utf-8"))
    assert state.get("doc_sha256") == new_sha, (
        f"the review state on disk is bound to another document: "
        f"{state.get('doc_sha256')!r} != {new_sha!r}"
    )
    assert all(item["status"] == "pending" for item in state.get("items") or []), (
        f"the review state on disk still carries the old decisions: "
        f"{state.get('items')}"
    )

    # An action carrying the old hash is refused: the review it belongs to
    # covers another document.
    status, body = _request(
        backend,
        "POST",
        f"/api/verification-plan/{plan_id}/review/item/0",
        {"action": "accept", "doc_sha256": original_sha},
    )
    assert status == 409, (
        f"an action carrying the pre-rewrite hash was accepted: {status} {body}"
    )
    assert body.get("error") == "document_changed", (
        f"the refusal does not name the changed document: {body}"
    )


def test_e2e_review_contract_unchanged(
    backend,
    coexist_plan,
) -> None:
    """审阅契约没有扩写：整篇粒度、一份状态文件、没有 ``plan_item_id``。

    架构决策点 6 定的契约是「审阅对象与接口契约保持整篇粒度，复用现有三端点」。
    这条用例把那句话钉在**落盘的字节**上：

    * 计划目录里恰好一份审核状态文件（``verification-plan-review.json``）；
    * 它的形状恰好是 ``{items, doc_sha256}``，条目恰好是五键；
    * 它与机器计划里都没有 ``plan_item_id`` —— 那是**报告**的词汇（验证点回溯到
      计划书条目的那一列），不属于审阅面。

    最后一条要有对照才不是空话：``plan_item_id`` 确实是产品的字段（报告的扩展
    字段里写着它，报告的验证点上也有它），所以「审阅面没有它」是一句有内容
    的断言，而不是「这个词没人用」。

    Args:
        backend: 任务 12 起的后端。
        coexist_plan: 共存目录的夹具。
    """
    plan_id = coexist_plan.plan_id
    plan_dir = coexist_plan.plan_dir

    # The reviewed document is on disk, so the source is REVIEWED and the
    # round translates it -- the machine plan on disk is then the product's
    # own, not this test's.
    (plan_dir / PLAN_DOC_FILENAME).write_text(coexist_plan.doc, encoding="utf-8")

    status, body = _request(
        backend, "POST", f"/api/verification/{plan_id}/start", {"auto_fix": False}
    )
    assert status == 200, f"starting the verification round failed: {status} {body}"
    _wait_for_the_round_to_settle(backend, plan_id)

    assert coexist_plan.machine_plan_path.exists(), (
        "the machine plan did not land: the reviewed document was not "
        "translated, so there is no product-written machine plan to check"
    )

    # One review action, so the review state is on disk at all.
    status, body = _request(
        backend, "GET", f"/api/verification-plan/{plan_id}/review/items"
    )
    assert status == 200, f"reading the review items failed: {status} {body}"
    items = body.get("items") or []
    assert items, "the review surface reported no items"
    status, body = _request(
        backend,
        "POST",
        f"/api/verification-plan/{plan_id}/review/item/{items[0]['index']}",
        {"action": "accept", "doc_sha256": body.get("doc_sha256")},
    )
    assert status == 200, f"accepting item 0 failed: {status} {body}"

    # --- 一份状态文件 ----------------------------------------------------
    review_files = sorted(path.name for path in plan_dir.glob("*review*.json"))
    assert review_files == [REVIEW_FILENAME], (
        "the plan directory holds more than one review state file: "
        f"{review_files}"
    )
    review_path = plan_dir / REVIEW_FILENAME
    assert review_path.exists(), (
        f"the review state file is not on disk: {review_path.name}"
    )

    # --- 形状：整篇粒度 --------------------------------------------------
    review_text = review_path.read_text(encoding="utf-8")
    state = json.loads(review_text)
    assert set(state) == {"items", "doc_sha256"}, (
        f"the review state grew keys: {sorted(state)}"
    )
    assert state["items"], "the review state carries no items"
    for item in state["items"]:
        assert set(item) == {"index", "id", "title", "status", "note"}, (
            f"a review item grew keys: {sorted(item)}"
        )

    # --- 没有 plan_item_id ------------------------------------------------
    # The control first: the vocabulary is the product's, on the report's
    # verification points.
    assert "plan_item_id" in REPORT_EXTENSION_FIELDS, (
        "plan_item_id is no longer a report field: the assertions below would "
        "be measuring a word nobody uses"
    )
    report_path = plan_dir / "verification_report.json"
    assert report_path.exists(), (
        "the round wrote no report: there is nothing to show that "
        "plan_item_id is the product's vocabulary"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    reported = report.get("verification_results") or []
    assert reported, "the round's report carries no verification results"
    assert all("plan_item_id" in point for point in reported), (
        "the report's verification points do not carry plan_item_id: the "
        "vocabulary this case asserts the review surface must not have is "
        f"not the product's after all: {reported}"
    )

    assert "plan_item_id" not in review_text, (
        "the review state carries plan_item_id: the review contract grew the "
        "report's vocabulary"
    )
    machine_text = coexist_plan.machine_plan_path.read_text(encoding="utf-8")
    assert "plan_item_id" not in machine_text, (
        "the machine plan carries plan_item_id: the plan contract grew the "
        "report's vocabulary"
    )


def test_e2e_real_http_requires_request_guard_header(
    backend,
    coexist_plan,
) -> None:
    """裸请求（不带 ``X-PDT-Request``）被守卫挡下：403。

    这是真 HTTP，而且**不经**测试里的 :func:`_request` 助手 —— 那个助手总是带着守卫头，所以它量不到「守卫拦下了裸请求」。

    对照组是同一个端点、同一份请求，只差那个头：带着头是 200，不带头是 403。
    两个断言缺一不可：只有 403 说明端点坏了，只有 200 说明守卫没开。

    Args:
        backend: 任务 12 起的后端。
        coexist_plan: 共存目录的夹具。
    """
    plan_id = coexist_plan.plan_id
    path = f"/api/verification/{plan_id}/status"

    # The bare request: no guard header at all.
    status, body = _raw_request(backend, "GET", path)
    assert status == 403, (
        f"a request without {REQUEST_HEADER} was not refused: {status} {body}"
    )
    assert body.get("error") == "forbidden", (
        f"the refusal does not say why: {body}"
    )
    assert REQUEST_HEADER in str(body.get("detail") or ""), (
        f"the refusal does not name the header it wants: {body}"
    )

    # The control: the same request with the header.
    status, body = _raw_request(
        backend, "GET", path, headers={REQUEST_HEADER: REQUEST_HEADER_VALUE}
    )
    assert status == 200, (
        f"the guarded request was refused: {status} {body}. Backend output:\n"
        f"{backend.log_tail()}"
    )