"""TDD spec — 根因报告的地址：写的地方 == 读的地方。

背景
----
一份根因报告的地址只有一个含义：**它落在哪儿**。谁按这个地址找它，
谁就得找到同一个文件 —— 名字对不上时，读的人看到的是「没交付」，
而报告其实躺在磁盘上。

2026-11-07（合并 origin/main 之后）这个契约换了住处
--------------------------------------------------
链的**状态机**没有了：base 的 PR #113
（refactor/remove-root-cause-chain，阶段 1–3）把
``backend/root_cause_task.py`` 和
``VerificationOrchestrator._root_cause_chain_tasks`` /
``._root_cause_report_file`` 一起删了 —— 编排器不再下发根因任务，
执行器不再接一份根因报告。所以本文件原来钉的那三方
（编排器下发的 / 链读回的 / 执行器解析的）已经不存在，
钉一个不存在的东西只会得到一份红的测试。

但**地址**还在，而且还在被读：链的**台账**
（``<plan_dir>/verification_root_cause_chain.json``）里每个 VP 记着
一条 ``report_path``，重置路径（``backend/routes/verification.py``）
会把它解成计划内的相对路径、再按**计划自己的目录**去删：

  * ``_plan_relative_report_path(plan_id, report_path)``
    —— 校验台账点名的地址，返回计划内的相对路径，越界就拒绝；
  * ``_clear_root_cause_chain(plan_id, plan_dir)``
    —— 删台账，以及台账点名的那些报告。

于是本文件钉的仍然是同一件事，只是两端换了：
**台账写下来的地址 == 重置读到的地址 == 计划自己 reports/ 下的那个文件。**

2026-11-03 起锚就换过一次
--------------------------
换锚之前，工作区那一侧会长出一棵 ``plans/<plan_id>/reports/``（报告落在
那儿），换锚之后不该再长，但**已经长出来的那棵还在**。它落在**被服务的
plans 树**下面，于是 ``GET /api/plans`` 的守卫
（``_refuse_when_data_source_does_not_describe_the_tree``）看见一个状态库
不认识的计划目录，回 503 而不是 200 + []。所以「地址不在工作区那一侧」
这件事，必须在**工作区已经长了那棵树**的前提下成立，否则这条契约挡不住
下一次复发。

TDD contract — 8 cases
-----------------------
1. ``test_the_ledger_and_the_reset_agree_on_the_report_address``
   —— 台账点名的地址解出来落在 ``plan_dir/reports/`` 下面，且重置动的
   就是那一个文件。
2. ``test_the_report_address_is_not_anchored_on_the_workspace``
   —— 地址**不**在 ``project_dir/plans/`` 下面（工作区那一侧）。
3. ``test_the_report_address_survives_a_workspace_that_grew_its_own_plans_tree``
   —— 工作区里**已经长出**一棵 ``plans/<plan_id>/reports/``（连同名文件
   都长出来了）时，地址仍然逐字落在 ``plan_dir/reports/`` 下面，且被删的
   是**计划目录那一份**，工作区那份一个字都没动。
4. ``test_an_address_that_escapes_the_plan_tree_is_refused``
   —— 绝对路径、``..``、别的 plan 的路径：一律拒绝，一个文件都不删。
5. ``test_an_address_naming_a_directory_is_not_deleted``
   —— 地址指着目录时不算报告 —— 契约说的是「删点名的文件」。
6. ``test_an_address_whose_tail_escapes_the_plan_tree_is_refused``
   —— **前缀对了，余下的那一截仍然越界**（``plans/<id>//<绝对路径>``）：
   拒绝，一个文件都不删。
7. ``test_an_address_whose_tail_is_a_windows_drive_is_refused``
   —— 余下的一截是 Windows 盘符形态（``plans/<id>/C:/...``）：同样拒绝。
8. ``test_the_delete_layer_refuses_a_target_outside_the_plan_dir``
   —— 删除那一层自己再确认一次：解析那一层被绕过时，越界目标仍然
   不删、不计入 cleared。

边界条件
--------
* 地址必须落在**计划自己的** ``reports/`` 下面，且由**计划自己的目录**
  解析 —— 不是工作区。
* 台账是数据：数据没有资格指名计划树以外的任何文件。
* 一个地址指着目录时不是报告，不删。
* **前缀过了不代表安全**：余下的那一截也得在计划树里。
* 删除动作不依赖一次字符串前缀校验 —— 它自己再确认一次。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
REPO_ROOT = BACKEND_DIR.parent
for _entry in (BACKEND_DIR, REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import pytest  # noqa: E402

# ``server`` 要排在 ``routes.verification`` 前面：这两个模块互相 import，
# 先把 server 放进 sys.modules，环才解得开（``backend/server.py`` 结尾从
# ``routes.verification`` 取名字，反过来也一样）。
import server  # noqa: E402,F401
import routes.verification as _verification_module  # noqa: E402
from routes.verification import (  # noqa: E402
    _ROOT_CAUSE_CHAIN_FILENAME,
    _clear_root_cause_chain,
    _plan_relative_report_path,
)

pytestmark = pytest.mark.unit


PLAN_ID = "p1"
VP_ID = "VP-3"
REPORT_NAME = f"root-cause-{VP_ID}.md"


def _tree(tmp_path: Path) -> tuple[Path, Path]:
    """A served plans tree and a workspace tree, side by side and distinct.

    ``plan_dir`` is one plan inside the tree the api service serves;
    ``project_dir`` is the workspace the verification runs against. The
    two are distinct on purpose — the whole contract is about which one
    an address is anchored on.
    """
    plans_dir = tmp_path / "plans"
    plans_dir.mkdir()
    plan_dir = plans_dir / PLAN_ID
    (plan_dir / "reports").mkdir(parents=True)
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    return plan_dir, project_dir


def _named_address() -> str:
    """The address the ledger names for one verification point.

    Repo-relative on purpose: the ledger is data that travels with the
    plan, so it names the file the way the plan's own tree does.
    """
    return f"plans/{PLAN_ID}/reports/{REPORT_NAME}"


def _write_ledger(plan_dir: Path, report_path: str) -> None:
    """The chain's ledger, in the shape the orchestrator writes it."""
    payload = {
        "version": 1,
        "stage": "BLOCKED_ON_USER",
        "vps": {VP_ID: {"report_path": report_path}},
        "updated_round": 3,
    }
    (plan_dir / _ROOT_CAUSE_CHAIN_FILENAME).write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# 1. 台账点名的地址 == 重置读到的地址
# ---------------------------------------------------------------------------
def test_the_ledger_and_the_reset_agree_on_the_report_address(tmp_path):
    """台账写下来的那个地址，和重置读到的那个，是同一个文件。"""
    plan_dir, _project_dir = _tree(tmp_path)
    report = plan_dir / "reports" / REPORT_NAME
    report.write_text("the report", encoding="utf-8")
    _write_ledger(plan_dir, _named_address())

    relative = _plan_relative_report_path(PLAN_ID, _named_address())
    assert relative == f"reports/{REPORT_NAME}", (
        "the address the ledger names does not resolve into the plan's own "
        f"reports dir: {relative!r}"
    )
    assert plan_dir / relative == report, (
        "the address the ledger names and the file on disk are different "
        f"files: {plan_dir / relative} vs {report}"
    )

    cleared = _clear_root_cause_chain(PLAN_ID, plan_dir)

    assert f"reports/{REPORT_NAME}" in cleared, (
        f"the reset did not act on the report the ledger names: {cleared}"
    )
    assert not report.exists(), "the report the ledger names is still there"
    assert not (plan_dir / _ROOT_CAUSE_CHAIN_FILENAME).exists(), (
        "the ledger itself is still there"
    )


# ---------------------------------------------------------------------------
# 2. 地址不锚在工作区那一侧
# ---------------------------------------------------------------------------
def test_the_report_address_is_not_anchored_on_the_workspace(tmp_path):
    """工作区那一侧（``<project_dir>/plans/…``）不是这个地址。"""
    plan_dir, project_dir = _tree(tmp_path)
    _write_ledger(plan_dir, _named_address())

    relative = _plan_relative_report_path(PLAN_ID, _named_address())
    assert relative is not None, "the address the ledger names was refused"
    target = plan_dir / relative

    assert project_dir not in target.parents, (
        "the report address is still anchored on the workspace: "
        f"{target}"
    )
    assert target != (
        project_dir / "plans" / PLAN_ID / "reports" / REPORT_NAME
    )


# ---------------------------------------------------------------------------
# 3. 工作区已经长出了自己的 plans 树，也不改地址
# ---------------------------------------------------------------------------
def test_the_report_address_survives_a_workspace_that_grew_its_own_plans_tree(
    tmp_path,
):
    """工作区里已经长出一棵 ``plans/<plan_id>/reports/`` 时，地址不变。

    这是复发钉子：2026-11-03 换锚之前，工作区会长出这棵树（连同名
    文件都长出来）。换锚之后它不该再长，但**已经长出来的那棵还在** ——
    而它落在被服务的 plans 树下面，能把 ``GET /api/plans`` 顶成 503。

    所以这里把工作区那一侧**摆好**（同名文件、内容不同），再问地址：
    它必须逐字落在 ``plan_dir/reports/`` 下面；而且被删的必须是**计划
    目录那一份** —— 工作区那份一个字都不能动。
    """
    plan_dir, project_dir = _tree(tmp_path)

    # 工作区已经长出自己的一棵 plans 树，同名文件也在。
    workspace_reports = project_dir / "plans" / PLAN_ID / "reports"
    workspace_reports.mkdir(parents=True)
    workspace_copy = workspace_reports / REPORT_NAME
    workspace_copy.write_text("workspace copy", encoding="utf-8")

    # 计划目录那一份也在，内容不同 —— 删错了哪一份，一读就知道。
    plan_copy = plan_dir / "reports" / REPORT_NAME
    plan_copy.write_text("plan dir copy", encoding="utf-8")

    _write_ledger(plan_dir, _named_address())

    relative = _plan_relative_report_path(PLAN_ID, _named_address())
    assert relative is not None, "the address the ledger names was refused"
    target = plan_dir / relative

    # 落在计划自己的 reports 下面，且不在工作区那一侧。
    assert target == plan_copy, (
        f"the address the ledger names is not the plan's own report: {target}"
    )
    assert target.parent == plan_dir / "reports", (
        f"the address is not under the plan's own reports dir: {target}"
    )
    workspace_plans = project_dir / "plans"
    assert workspace_plans not in target.parents, (
        f"the address is under the workspace's plans tree: {target}"
    )
    assert target != workspace_copy, (
        f"the address points at the workspace's copy: {target}"
    )

    _clear_root_cause_chain(PLAN_ID, plan_dir)

    # 删掉的是计划目录那一份；工作区那份一个字都没动。
    assert not plan_copy.exists(), "the plan's own report is still there"
    assert workspace_copy.exists(), (
        "the reset deleted the workspace's copy — the address was anchored "
        "on the workspace, not on the plan"
    )
    assert workspace_copy.read_text(encoding="utf-8") == "workspace copy"


# ---------------------------------------------------------------------------
# 4. 越界的地址一律拒绝
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "kind", ["absolute", "dot-dot", "other-plan"],
)
def test_an_address_that_escapes_the_plan_tree_is_refused(tmp_path, kind):
    """台账是数据：数据没有资格指名计划树以外的文件。"""
    plan_dir, _project_dir = _tree(tmp_path)
    plans_dir = plan_dir.parent

    if kind == "absolute":
        outside_dir = tmp_path / "abs"
        outside_dir.mkdir()
        address = str(outside_dir / REPORT_NAME)
    elif kind == "dot-dot":
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        address = f"../outside/{REPORT_NAME}"
    else:
        outside_dir = plans_dir / "other-plan" / "reports"
        outside_dir.mkdir(parents=True)
        address = f"plans/other-plan/reports/{REPORT_NAME}"

    named = outside_dir / REPORT_NAME
    named.write_text("must survive", encoding="utf-8")
    _write_ledger(plan_dir, address)

    assert _plan_relative_report_path(PLAN_ID, address) is None, (
        f"the address {address!r} names a file outside the plan tree and "
        "must be refused"
    )

    cleared = _clear_root_cause_chain(PLAN_ID, plan_dir)

    assert named.exists(), (
        f"the reset deleted a file the plan tree does not own: {named}"
    )
    assert cleared == [_ROOT_CAUSE_CHAIN_FILENAME], (
        f"the reset acted on a file outside the plan tree: {cleared}"
    )


# ---------------------------------------------------------------------------
# 5. 地址指着目录时不是报告
# ---------------------------------------------------------------------------
def test_an_address_naming_a_directory_is_not_deleted(tmp_path):
    """契约说的是「删点名的**文件**」，不是「删地址指着的东西」。"""
    plan_dir, _project_dir = _tree(tmp_path)
    reports = plan_dir / "reports"
    _write_ledger(plan_dir, f"plans/{PLAN_ID}/reports")

    cleared = _clear_root_cause_chain(PLAN_ID, plan_dir)

    assert reports.is_dir(), "the reset deleted a directory"
    assert "reports" not in cleared, (
        f"the reset counted a directory as a report: {cleared}"
    )


# ---------------------------------------------------------------------------
# 6. 前缀对了，余下的那一截仍然可能越界
# ---------------------------------------------------------------------------
def test_an_address_whose_tail_escapes_the_plan_tree_is_refused(tmp_path):
    """前缀过了不代表安全 —— 余下的那一截也得在计划树里。

    ``plans/<plan_id>//<绝对路径>`` 逐字以计划前缀开头，但余下的那一截
    是**绝对路径**：拼回计划目录时 pathlib 会丢掉左侧的基路径，于是删除
    落在计划树之外。台账是数据 —— 它没有资格指名计划树以外的文件。
    """
    plan_dir, _project_dir = _tree(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / REPORT_NAME
    victim.write_text("must survive", encoding="utf-8")

    # 夹具自己先确认一遍：这条地址逐字以计划前缀开头（双斜杠在中间）。
    address = f"plans/{PLAN_ID}/{victim}"
    assert address.startswith(f"plans/{PLAN_ID}/"), (
        f"the fixture address lost its plan prefix: {address!r}"
    )
    assert address[len(f"plans/{PLAN_ID}/"):].startswith("/"), (
        f"the fixture address does not carry an absolute tail: {address!r}"
    )

    assert _plan_relative_report_path(PLAN_ID, address) is None, (
        f"the address {address!r} resolves outside the plan tree and "
        "must be refused"
    )

    _write_ledger(plan_dir, address)
    cleared = _clear_root_cause_chain(PLAN_ID, plan_dir)

    assert victim.exists(), (
        f"the reset deleted a file the plan tree does not own: {victim}"
    )
    assert cleared == [_ROOT_CAUSE_CHAIN_FILENAME], (
        f"the reset acted on a file outside the plan tree: {cleared}"
    )


# ---------------------------------------------------------------------------
# 7. 余下的那一截是 Windows 盘符形态
# ---------------------------------------------------------------------------
def test_an_address_whose_tail_is_a_windows_drive_is_refused(tmp_path):
    """``plans/<plan_id>/C:/...`` 的余串在 Windows 上就是绝对路径。"""
    _plan_dir, _project_dir = _tree(tmp_path)
    address = f"plans/{PLAN_ID}/C:/Windows/System32/drivers/etc/hosts"

    assert _plan_relative_report_path(PLAN_ID, address) is None, (
        f"the address {address!r} escapes the plan tree on a Windows host "
        "and must be refused"
    )


# ---------------------------------------------------------------------------
# 8. 删除那一层自己再确认一次
# ---------------------------------------------------------------------------
def test_the_delete_layer_refuses_a_target_outside_the_plan_dir(
    tmp_path, monkeypatch,
):
    """删除动作不依赖一次字符串前缀校验 —— 它自己再确认一次。

    这条钉的是**删除那一层**的防御：把解析那一层绕过去（假装它放行），
    只有删除那一层自己的「目标还在计划目录里吗」能拦住越界的删除。
    """
    plan_dir, _project_dir = _tree(tmp_path)
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    victim = outside_dir / REPORT_NAME
    victim.write_text("must survive", encoding="utf-8")

    _write_ledger(plan_dir, f"plans/{PLAN_ID}/{victim}")

    # 假装解析那一层放行了 —— 只有删除那一层的自保能拦住。
    monkeypatch.setattr(
        _verification_module,
        "_plan_relative_report_path",
        lambda plan_id, report_path: str(victim),
    )

    cleared = _clear_root_cause_chain(PLAN_ID, plan_dir)

    assert victim.exists(), (
        f"the delete layer removed a file outside the plan tree: {victim}"
    )
    assert victim.name not in cleared, (
        f"the delete layer counted an out-of-tree file as cleared: {cleared}"
    )
