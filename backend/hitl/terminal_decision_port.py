"""The default implementation of the terminal-decision port.

Why this module exists
======================
:mod:`hitl.terminal_execution` names what a human's answer asks for —
*reset the round and start it again*, *close the plan as failed* — and
does so through a port it does not implement.  A port that has no
implementation behind it is a contract nobody honours: the executor
calls ``port.reset(...)`` and ``port.start(...)``, and nothing happens.

This module is the other half of that contract: a thin, mechanical
adapter that carries each of those calls to the thing that already
knows how to do it.

What the three actions call
---------------------------

==================  ==========================================================
``reset``           :func:`routes.verification.reset_verification`, with a
                     ``clear_artifacts`` request
``start``           :func:`routes.verification.start_verification`
``close_as_failed`` :meth:`plan_state.PlanState.transition_to` — the
                     ``failed`` phase
==================  ==========================================================

The fourth door: free text
--------------------------
The three actions above are what a human *picks*.  A human can also
write free text instead, and then nobody knows what they meant — so
the **analysis agent** reads it, in the conversation it already had
about the failure.

That conversation has an id.  The analysis call recorded the session
its answer came out of, and the sidecar keeps that id next to the
analysis.  So :meth:`DefaultTerminalDecisionPort.resume_agent`
re-opens that session by id.  When there is no id to re-open, it opens
a fresh one and puts the **whole analysis context** into the prompt,
so the new conversation starts from the same facts the old one had.

The port does not decide what the agent's answer means — it only
carries the call out.  The vocabulary of "what happens next" belongs to
the orchestrator, and this file stays out of it.

The fifth door: generate repair tasks
-------------------------------------
*Generate repair tasks* is the answer that says "the deliverable is
wrong, go and fix it" — and it is the one answer that used to have a
name and no implementation.  The executor called
``port.generate_repair_tasks(...)`` and nothing happened, so a plan
whose round had stopped sat in ``verification_failed`` with nobody
working on it.

This module closes that door, and it closes it in the shape the rest of
the chain already speaks:

* the sidecar record's ``failed_points`` become **failed-VP records**
  (:func:`failed_vps_from_record`) — the shape
  :class:`repair_generator.RepairTaskGenerator` reads;
* the generator turns them into **repair content**, and
  :class:`repair_generator.RepairTaskAssembler` stamps the local fields
  and hands back **task rows**;
* the rows go into ``state.db``'s ``plan_tasks`` table, where the
  executor's ``_get_pending_repair_tasks`` finds them.

The two ends of that chain are injected (``repair_generator_factory``
and ``task_writer``), so the mechanics can be exercised without a model
and without the operator's database.  A failure is **not** swallowed
here: the call is allowed to raise, and
:func:`hitl.terminal_execution.execute_terminal_decision` is what
records it in the audit (arch decision point 5).

Nothing here is scheduler-dependent, so nothing here takes an engine.
That is not an accident of the signature: a port that accepted an
engine would let the scheduler dependency back in through the front
door, and the mechanical actions would stop being mechanical.

The imports are function-level, and that is deliberate
-----------------------------------------------------
``routes.verification`` imports ``server`` at module level, and
``server`` imports the route modules at the bottom of its own file.  A
module-level ``from routes.verification import ...`` inside this
package therefore closes a cycle at *import* time — ``hitl`` would be
half-initialised while ``server`` is still being loaded, and whoever
imported ``hitl`` first would get a broken module.

So every call into the route layer is resolved **when the call is
made**, not when this module is imported.  By then ``server`` is
loaded and the cycle has already been closed by the normal import
order.  It also means a test can replace ``reset_verification`` on the
route module and see the replacement — which is how the mechanics of
this port are pinned in
``backend/tests/unit/test_terminal_decision_port_mechanics.py``.

The plan directory
------------------
``close_as_failed`` needs to know where the plan lives.  The port
takes a ``plan_dir_of`` resolver for that, and its default asks
``server._plan_dir`` — the one funnel every ``plans/{plan_id}`` access
goes through.  A test (or a caller that keeps its plans elsewhere)
supplies its own resolver instead.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

__all__ = [
    "AGENT_SYSTEM_PROMPT",
    "AGENT_TIMEOUT_SECONDS",
    "DefaultTerminalDecisionPort",
    "default_port",
    "failed_vps_from_record",
]


# ---------------------------------------------------------------------------
# The default plan-directory resolver
# ---------------------------------------------------------------------------


def _default_plan_dir_of(plan_id: str) -> Any:
    """Resolve a plan id to its directory, the way the routes do.

    ``server._plan_dir`` is the funnel every ``plans/{plan_id}`` access
    goes through, so using it here means this port cannot disagree with
    the routes about where a plan lives.  The import is function-level
    for the same reason the route imports are (see the module
    docstring).
    """
    import server as _server

    return _server._plan_dir(plan_id)


# ---------------------------------------------------------------------------
# The agent's constants
# ---------------------------------------------------------------------------

#: The system instruction the resumed analysis call runs under.
#:
#: The agent that reads a human's free text is the *same* agent that
#: read the failure scene: it is the conversation that gets re-opened,
#: so the instruction is the analysis instruction with the human's
#: words added to it.  What it is **not** given is a vocabulary of
#: actions — that belongs to the orchestrator, and this port stays out
#: of it.  The agent says what it read and what it would do; somebody
#: else decides whether that happens.
_AGENT_SYSTEM_PROMPT = (
    "你是验证轮的分析 agent。上一轮验证停下来问人怎么走，人用自由文本回答了。"
    "你之前已经和这个场景打过一次交道；现在轮到你读人的自由文本，"
    "解释人想要什么、你接下来要做什么。\n\n"
    "NO TOOLS. 你没有任何工具：不读写文件、不执行命令、不访问网络。"
    "你唯一的输入是 prompt 里给出的内容，唯一的工作是解释它。\n\n"
    "HARD RULES — 违反任何一条，这个回答会被丢弃：\n"
    "1. DO NOT FABRICATE. 不许编造事实。prompt 没有给出的失败原因、"
    "代码、日志都不许当成确定结论；证据不足就在 uncertainty 里说清楚。\n"
    "2. 只解释，不动手。你不重跑、不改动任何东西、不做任何操作；"
    "你只负责把人的意思和要做的事说清楚，动手是别的环节的事。\n"
    "3. 全部用中文回答。字段名（interpretation / next_step / reasoning / "
    "uncertainty）保持英文原样，其余内容一律中文。\n"
    "4. 只输出 JSON，不要输出解释性文字，不要用 markdown 代码块包裹。"
    "四个字段一个都不许少。"
)

#: How long one resumed analysis call may take.
_AGENT_TIMEOUT_SECONDS = 300


# ---------------------------------------------------------------------------
# The agent's prompt
# ---------------------------------------------------------------------------


def _json_text(value: Any) -> str:
    """Render ``value`` as JSON text; never raises.

    The text is the *original* — no re-shaping, no summarising — so
    that what the agent reads is what was recorded.  A value that
    cannot be serialised is rendered with ``str`` instead: the prompt
    is a rendering of what was given, and a value that cannot be shown
    must not take the whole call down with it.
    """
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _record_session_id(record: Any) -> Optional[str]:
    """The session id the record carries, or ``None``.

    Only a non-empty string counts.  An id that is ``None``, an empty
    string or whitespace names no session, and re-opening "no session"
    is the same as opening a new one.
    """
    if not isinstance(record, Mapping):
        return None
    value = record.get("session_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _agent_prompt(plan_id: str, record: Any, note: str) -> str:
    """Render the prompt for the resumed analysis call.

    The prompt carries three things, in this order:

    * **the session state** — whether the recorded analysis session is
      being re-opened, or whether a new one is being started because
      there was no id to re-open;
    * **the human's own words** — verbatim, or a placeholder when the
      human wrote nothing;
    * **the analysis context** — the analysis and the failed points, in
      full.

    The context is in the prompt even when the session already holds
    it, because the degraded path — no id to re-open — is exactly the
    path where the prompt is the *only* copy of it.  Rendering it
    always means the two paths say the same thing to the agent.

    Nothing here raises: a missing record is not an error, it is a
    round whose analysis was never recorded, and the prompt says so.
    """
    record_map = record if isinstance(record, Mapping) else {}
    session_id = _record_session_id(record_map)
    analysis = record_map.get("analysis")
    failed_points = record_map.get("failed_points")

    if isinstance(note, str):
        words = note.strip()
    elif note is None:
        words = ""
    else:
        words = str(note).strip()

    parts: List[str] = [
        "# 终局自由文本续跑请求",
        "",
        "## 会话状态",
        "",
        f"- plan_id: {plan_id}",
    ]
    if session_id:
        parts.append(
            f"- 分析会话：已续跑记录里的分析会话（session id: {session_id}）"
        )
    else:
        parts.append(
            "- 分析会话：没有可续跑的分析会话（记录里没有 session id），"
            "已开启新会话"
        )
        parts.append(
            "- 原会话的分析上下文不可用（没有 session id 可续跑），"
            "下面的分析上下文以全文形式带入。"
        )

    parts.extend(["", "## 人工的自由文本回答", ""])
    if words:
        parts.append(words)
    else:
        parts.append(
            "（人工没有填写自由文本；请只依据下面的分析上下文判断，"
            "并把不确定的地方说清楚。）"
        )

    parts.extend(["", "## 分析上下文", ""])
    if analysis:
        parts.append(_json_text(analysis))
    else:
        parts.append("分析上下文不可用：没有留下任何分析记录。")

    parts.extend(["", "## 失败验证点", ""])
    if failed_points:
        parts.append(_json_text(failed_points))
    else:
        parts.append("(没有失败验证点记录)")

    return "\n".join(parts)


# ---------------------------------------------------------------------------
# The repair path
# ---------------------------------------------------------------------------

#: The priority a failed point carries when the record names none.
#:
#: A repair derived from a terminal failure is a first-priority task by
#: construction: the round stopped *because* of this failure, and
#: nothing else the plan does can move until it is answered.
_DEFAULT_REPAIR_PRIORITY = "P1"

#: The failure class a failed point carries when the record names none.
#:
#: The same default the repair chain itself uses
#: (:func:`verification.failure_history.failure_class_of`): a record
#: that predates the classification reads as "nobody said otherwise",
#: and "nobody said otherwise" has always meant "repair it".
_DEFAULT_FAILURE_CLASS = "deliverable_defect"


def _record_round_number(record: Any) -> int:
    """The round the record says stopped; ``0`` when it says nothing.

    Only a real number counts.  A round that is missing — or that
    carries something that is not a number at all — reads as round 0,
    so the repair that follows it is round 1.  It is not
    ``None + 1``: the assembler refuses a round below 1, and a repair
    task with no round cannot be grouped into a batch.
    """
    if not isinstance(record, Mapping):
        return 0
    value = record.get("round_number")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def failed_vps_from_record(record: Any) -> List[Dict[str, Any]]:
    """The record's failed points, in the shape the repair chain reads.

    The sidecar keeps the failure scene the analysis was read against
    (:func:`verification.terminal_decision_session.build_record`), and
    that scene is not in the shape
    :class:`repair_generator.RepairTaskGenerator` wants.  This is the
    translation, and it is deliberately small — every field it invents
    says something the record does not:

    ``id``
        the verification point's id, verbatim.
    ``title``
        the point's title; a point that names none falls back to its
        id, because a task whose title is empty reads as a task nobody
        wrote.
    ``priority``
        the point's priority, or ``"P1"`` — see
        :data:`_DEFAULT_REPAIR_PRIORITY`.
    ``actual_result``
        the point's **verdict** — the failure text.  A point that
        carries no verdict has no text to report, and the empty string
        says exactly that.
    ``evidence``
        always empty.  The record carries no evidence array of its own:
        what it has is the verdict, and the verdict is already in
        ``actual_result``.  Inventing one would be a second copy of the
        same fact.
    ``expected_result``
        the point's expected result, verbatim.
    ``failure_class``
        the point's failure class, or ``"deliverable_defect"`` — see
        :data:`_DEFAULT_FAILURE_CLASS`.

    A record that is not there, or that names no failed points, maps to
    an empty list.  Nothing here raises: a record that cannot be read
    is a round whose failures were never recorded, and that is a normal
    state to be in (arch decision point 5: *read it or degrade, never
    raise*).
    """
    if not isinstance(record, Mapping):
        return []
    points = record.get("failed_points")
    if not isinstance(points, (list, tuple)):
        return []

    failed_vps: List[Dict[str, Any]] = []
    for point in points:
        if not isinstance(point, Mapping):
            continue
        vp_id = str(point.get("id", "") or "")
        title = str(point.get("title", "") or "").strip() or vp_id
        priority = (
            str(point.get("priority", "") or "").strip()
            or _DEFAULT_REPAIR_PRIORITY
        )
        failure_class = (
            str(point.get("failure_class", "") or "").strip()
            or _DEFAULT_FAILURE_CLASS
        )
        failed_vps.append(
            {
                "id": vp_id,
                "title": title,
                "priority": priority,
                "actual_result": str(point.get("verdict", "") or ""),
                "evidence": [],
                "expected_result": str(point.get("expected_result", "") or ""),
                "failure_class": failure_class,
            }
        )
    return failed_vps


def _default_write_tasks(plan_id: str, rows: List[Dict[str, Any]]) -> None:
    """Write the repair rows into ``state.db``'s ``plan_tasks`` table.

    This is where the repair becomes work: the executor's
    ``_get_pending_repair_tasks`` reads ``plan_tasks``, so a row that
    never reached the table is a repair nobody runs.

    The database is :func:`config_paths.resolve_state_db_path`'s — the
    one resolver every writer in the backend uses — and it is opened
    once, migrated, written row by row, and closed.

    **Failures are not swallowed.**  A repair that could not be queued
    is a fact the caller has to hear about, and the caller that knows
    how to record it is
    :func:`hitl.terminal_execution.execute_terminal_decision` — it
    writes the failure to the audit (arch decision point 5).
    """
    from config_paths import resolve_state_db_path
    from state_machine.db.connection import open as open_db
    from state_machine.db.schema import migrate as migrate_db
    from state_machine.repositories.plan_task_repository import (
        PlanTaskRepository,
    )

    conn = open_db(resolve_state_db_path())
    try:
        migrate_db(conn)
        repo = PlanTaskRepository(conn)
        for row in rows:
            repo.add_task(plan_id, row)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# The port
# ---------------------------------------------------------------------------


class DefaultTerminalDecisionPort:
    """The mechanical port: three actions, three calls, no engine.

    Every method takes the plan id and nothing else, so the port can be
    handed to :func:`hitl.terminal_execution.execute_terminal_decision`
    as-is.

    The fourth door — the free-text one — is here too: it re-opens the
    analysis session the human's words belong to, and degrades to a
    fresh session with the whole analysis context when there is no
    session to re-open.  It carries the call out and nothing more.
    """

    #: The system instruction the resumed analysis call runs under.
    AGENT_SYSTEM_PROMPT = _AGENT_SYSTEM_PROMPT

    #: How long one resumed analysis call may take.
    AGENT_TIMEOUT_SECONDS = _AGENT_TIMEOUT_SECONDS

    def __init__(
        self,
        *,
        plan_dir_of: Optional[Callable[[str], Any]] = None,
        coding_tool: Any = None,
        repair_generator_factory: Optional[Callable[[str], Any]] = None,
        task_writer: Optional[Callable[[str, List[Dict[str, Any]]], Any]] = None,
    ) -> None:
        """Build the port.

        ``plan_dir_of`` maps a plan id to the plan's directory; the
        default asks ``server._plan_dir``.  ``coding_tool`` is the tool
        the free-text path calls; without one the port builds a
        :class:`coding_tool.ClaudeCodingTool` on the ``verification``
        scene when it is first needed.

        ``repair_generator_factory`` builds the repair generator the
        *generate repair tasks* path calls; it takes a plan id and
        returns something with
        ``generate_repair_contents(failed_vps, round_number)``.  The
        default builds a real
        :class:`repair_generator.RepairTaskGenerator` for that plan.

        ``task_writer`` writes the assembled rows; it takes a plan id
        and the rows.  The default writes them into ``state.db``'s
        ``plan_tasks`` table.

        There is deliberately no ``engine`` parameter: none of these
        actions needs the scheduler.
        """
        self._plan_dir_of = (
            plan_dir_of if plan_dir_of is not None else _default_plan_dir_of
        )
        self._coding_tool = coding_tool
        self._repair_generator_factory = (
            repair_generator_factory
            if repair_generator_factory is not None
            else self._default_repair_generator_factory
        )
        self._task_writer = (
            task_writer if task_writer is not None else _default_write_tasks
        )

    # -- the tool --------------------------------------------------------

    @property
    def coding_tool(self) -> Any:
        """The tool the free-text path calls — built on first use when absent.

        Built on first use, not in the constructor: a port that only
        ever resets and starts a plan must not build a provider nobody
        asked for.
        """
        if self._coding_tool is None:
            from coding_tool import ClaudeCodingTool

            self._coding_tool = ClaudeCodingTool(scene="verification")
        return self._coding_tool

    # -- the three actions ---------------------------------------------

    def reset(self, plan_id: str, *, clear_artifacts: bool = True) -> Any:
        """Reset the round's state so it can run again.

        The round is cleared by the route that owns the reset procedure,
        with ``clear_artifacts`` saying whether the round's disk outputs
        go with it.  The flag is handed over unchanged — whether a stale
        artefact survives is not this port's decision to make.
        """
        import server as _server

        from routes.verification import reset_verification

        request = _server.ResetVerificationRequest(clear_artifacts=clear_artifacts)
        return reset_verification(plan_id, request)

    def start(self, plan_id: str) -> Any:
        """Start the plan (or the round) running.

        No request body: the round budget stays whatever the plan was
        set up with.
        """
        from routes.verification import start_verification

        return start_verification(plan_id, None)

    def close_as_failed(self, plan_id: str) -> Any:
        """Close the plan as failed — the failure is the accepted outcome.

        The plan moves to the ``failed`` phase through the state machine,
        which is what makes the outcome visible to everything that reads
        the plan's phase afterwards.
        """
        from plan_state import PlanState

        state = PlanState(self._plan_dir_of(plan_id))
        return state.transition_to("failed")

    # -- the repair path -----------------------------------------------

    def _default_repair_generator_factory(self, plan_id: str) -> Any:
        """The repair generator the port builds when nobody supplied one.

        Built on the plan's own directory and the plan's registered
        workspace, so the repair lands in the tree the verification
        branch owns.  The import is function-level for the same reason
        the route imports are (see the module docstring).
        """
        from repair_generator import RepairTaskGenerator

        plan_dir = Path(self._plan_dir_of(plan_id))
        return RepairTaskGenerator(
            self.coding_tool,
            plan_dir,
            self._repair_project_dir(plan_id, plan_dir),
        )

    def _repair_project_dir(self, plan_id: str, plan_dir: Path) -> Path:
        """Where the repair runs: the plan's registered workspace.

        The plan's execution row is the authority — it is the one field
        the whole execution chain reads — and the plan directory is the
        last resort.  A plan whose row cannot be read still gets a
        repair; it just gets one without a workspace to run in.
        """
        try:
            import server as _server

            registered = _server._get_project_dir(plan_id)
            if registered:
                return Path(registered)
        except Exception:  # noqa: BLE001 - the fallbacks cover this
            pass
        try:
            from repair_generator import resolve_repair_project_dir

            resolved = resolve_repair_project_dir(plan_dir)
            if resolved:
                return Path(resolved)
        except Exception:  # noqa: BLE001 - the fallbacks cover this
            pass
        return plan_dir

    def _assemble_repair_tasks(
        self,
        plan_id: str,
        contents: Any,
        failed_vps: List[Dict[str, Any]],
        round_number: int,
    ) -> List[Dict[str, Any]]:
        """Turn the generator's content into task rows.

        The assembler is local code by construction: it stamps ``id``,
        ``priority``, ``depends_on`` and ``execution_group`` without
        asking a model anything, so a model that misbehaves still
        yields rows the state machine can recognise.

        The plan's verification plan is handed over when it can be
        found, because the assembler reads the plan-side fields (the
        point's own command, its verification method) from there.  A
        plan whose directory cannot be resolved still gets its repair
        tasks — with less in them.
        """
        from repair_generator import RepairTaskAssembler

        plan_path = ""
        try:
            plan_dir = Path(self._plan_dir_of(plan_id))
            plan_path = str(plan_dir / "verification_plan.json")
        except Exception:  # noqa: BLE001 - the plan is optional here
            plan_path = ""

        return RepairTaskAssembler(
            round_number=round_number,
            failed_vps=failed_vps,
            plan_path=plan_path,
        ).assemble(contents)

    def generate_repair_tasks(self, plan_id: str, record: Any = None) -> int:
        """Turn the round's failures into repair tasks; return how many.

        This is the *generate repair tasks* answer, carried out:

        1. the record's failed points become failed-VP records
           (:func:`failed_vps_from_record`);
        2. the generator turns them into repair content, for the round
           **after** the one the record says stopped;
        3. the assembler stamps the local fields and hands back rows;
        4. the rows go into ``state.db``'s ``plan_tasks`` table, where
           the executor's ``_get_pending_repair_tasks`` finds them.

        **Nothing to repair means nothing is generated.**  A record with
        no failed points returns ``0`` without calling the generator: a
        call that has nothing to repair can only produce an empty
        repair, and an empty repair task in the queue is work the
        executor has to pick up and throw away.

        The rows that reach the database carry **no ``status``**:
        :meth:`PlanTaskRepository.add_task` rejects a payload with a
        field it does not know, and ``status`` is the runtime mirror's
        field rather than a static one — the table applies its own
        ``pending`` default.

        Failures are **not** swallowed here.  The call is allowed to
        raise, and :func:`hitl.terminal_execution.execute_terminal_decision`
        is what records it in the audit (arch decision point 5).
        """
        failed_vps = failed_vps_from_record(record)
        if not failed_vps:
            return 0

        round_number = _record_round_number(record) + 1

        generator = self._repair_generator_factory(plan_id)
        contents = generator.generate_repair_contents(failed_vps, round_number)

        tasks = self._assemble_repair_tasks(
            plan_id, contents, failed_vps, round_number,
        )

        rows = [
            {k: v for k, v in task.items() if k != "status"}
            for task in tasks
            if isinstance(task, dict)
        ]
        self._task_writer(plan_id, rows)
        return len(rows)

    # -- the free-text path ---------------------------------------------

    def _agent_prompt(self, plan_id: str, record: Any, note: str) -> str:
        """The prompt for the resumed analysis call.

        A thin door into the module-level renderer, so the rendering can
        be read — and exercised — without a port in hand.
        """
        return _agent_prompt(plan_id, record, note)

    def resume_agent(
        self, plan_id: str, record: Any = None, note: str = ""
    ) -> None:
        """Continue the recorded analysis session with the human's own words.

        This is the free-text door.  The human wrote something nobody
        can match against a table of options, so the *analysis agent*
        reads it — and it reads it in the conversation it already had
        about the failure, because that conversation already holds the
        facts.

        Two paths, one call:

        * **There is a session id** in the record.  The session is
          re-opened: the handle carries that id and says it was
          *resumed*, so the agent keeps the context it had.
        * **There is no session id** (or no record at all).  A fresh
          session opens and the **whole analysis context** goes into
          the prompt, so the new conversation starts from the same
          facts the old one had.  The prompt says that the original
          session's context was unavailable, so nobody reads the
          substitution as a claim that the session was re-opened.

        Neither path raises.  A missing record is not an error — it is
        a round whose analysis was never recorded — so the prompt says
        the analysis context is unavailable and the call still happens.

        What this method does **not** do is swallow the provider's
        failure: the call is allowed to raise, and
        :func:`hitl.terminal_execution.execute_terminal_decision` is
        what records it in the audit.

        The port does not read the agent's answer and does not decide
        what it means.  The vocabulary of what happens next belongs to
        the orchestrator; this method only carries the call out.
        """
        from coding_tool import JsonSession, query_json_in_session

        session_id = _record_session_id(record)
        handle = (
            JsonSession(session_id=session_id, stage="resumed")
            if session_id
            else JsonSession()
        )

        query_json_in_session(
            self.coding_tool,
            handle,
            prompt=self._agent_prompt(plan_id, record, note),
            system_instruction=self.AGENT_SYSTEM_PROMPT,
            timeout=self.AGENT_TIMEOUT_SECONDS,
            allowed_tools=[],
        )


# ---------------------------------------------------------------------------
# The factory
# ---------------------------------------------------------------------------


#: Module-level aliases for the port's two agent constants, so a caller
#: that only wants the instruction or the timeout does not have to reach
#: into the class for them.
AGENT_SYSTEM_PROMPT = DefaultTerminalDecisionPort.AGENT_SYSTEM_PROMPT
AGENT_TIMEOUT_SECONDS = DefaultTerminalDecisionPort.AGENT_TIMEOUT_SECONDS


def default_port() -> DefaultTerminalDecisionPort:
    """The port to use when nobody supplied one.

    A factory rather than a module-level instance so the port is built
    where it is used, and so a caller that wants a different plan
    directory resolver can still build its own.
    """
    return DefaultTerminalDecisionPort()