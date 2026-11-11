"""VP 的 ``covers_paths``：**作者声明优先**，任务派生兜底（2026-11-05）。

背景
----
一条验证点能不能在下一轮复用，取决于它声明的范围（``covers_paths``）——
范围与本轮变更集不相交、且上一轮 PASSED，才允许复用（见
:mod:`verification_round_scope`）。

VP 是对一个**功能点**的黑盒综合验证，任务可以拆得很细 —— 一条 VP 常常
横跨好几条任务，一条任务也可能涉及好几条 VP。VP 与任务**不是**绑定关系，
所以范围不能只由任务派生。

范围的口径是"宁宽勿窄"：范围偏宽只是多跑几条；范围偏窄会让**本不该复用的
结论被复用** —— 那才是错的方向。因此：

* 验证点自己声明了非空 ``covers_paths`` → **保留它**（只归一化 + 去重），
  不被任务派生结果覆盖；
* 没声明 / 声明为空 → 从它对应任务声明的 ``files_to_modify`` 派生（文件所在
  的目录前缀）：去重保序，不跨目录合并；
* 两者都得不到 → 空（该 VP 每轮重跑，安全的一侧）。

这份文件钉住派生口径与四条边界：

* 任务 id 不存在 / ``source_task_ids`` 缺失 → 派生不出范围（该 VP 每轮重跑）；
* ``files_to_modify`` 只含哨兵（"这个任务不改文件"）→ 派生不出范围；
* 没有 ``tasks.json`` / JSON 解析失败 → 读不到任务行 → 派生不出范围；
* 作者自带 ``covers_paths`` → 保留（归一化去重），派生结果不参与。

TDD contract — 4 个必测用例
----------------------------
1. ``test_attach_covers_paths_from_tasks`` —— 按任务声明派生范围。
2. ``test_unknown_task_id_yields_empty`` —— 任务 id 未知 / 字段缺失 → 空范围。
3. ``test_sentinel_files_ignored`` —— 哨兵不是路径 → 空范围。
4. ``test_authored_paths_are_preserved`` —— 作者声明的范围被保留。

另附 ``test_load_task_rows_*``：读任务行的那一步自身的边界（缺文件 /
坏 JSON / 两种载荷形状）；以及 ``test_generate_verification_plan_*`` /
``test_planning_prompt_*``：Phase-1 生成路径上的接线（范围真的被派生、
真的落盘、提示词里真的带着任务清单）。

测试命令::

    pytest backend/tests/unit/verification/test_covers_paths_derivation.py -v
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# ``backend/`` 与仓库根都进 sys.path：``import verification_agent`` 需要前者
# （同级模块），``prompts`` 里的 ``backend.framework.prompts`` 需要后者。
_BACKEND_DIR = Path(__file__).resolve().parents[3]
_REPO_ROOT = _BACKEND_DIR.parent
for _entry in (_BACKEND_DIR, _REPO_ROOT):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

from verification_agent import (  # noqa: E402
    _attach_covers_paths,
    _load_task_rows,
)
from verification_round_scope import covers_paths_for_files  # noqa: E402

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# 1. 按任务声明派生
# ---------------------------------------------------------------------------


def test_attach_covers_paths_from_tasks():
    """VP 声明了对应任务 → 范围 = 那些任务 ``files_to_modify`` 的目录前缀。"""
    plan = [{"id": "VP-001", "source_task_ids": ["3"]}]
    tasks = [
        {
            "id": "3",
            "files_to_modify": [
                "backend/verification/orchestrator.py",
                "backend/tests/unit/x.py",
            ],
        }
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == [
        "backend/verification/",
        "backend/tests/unit/",
    ]


def test_attach_covers_paths_uses_every_source_task():
    """一条 VP 可以对应多个任务：范围是它们目录前缀的并集（去重保序）。"""
    plan = [{"id": "VP-001", "source_task_ids": ["3", "4"]}]
    tasks = [
        {"id": "3", "files_to_modify": ["backend/verification/orchestrator.py"]},
        {"id": "4", "files_to_modify": ["backend/verification/sub/other.py"]},
    ]

    result = _attach_covers_paths(plan, tasks)

    # 同一目录（"backend/verification" 与它的子目录不是同一个目录）→ 两条
    assert result[0]["covers_paths"] == [
        "backend/verification/",
        "backend/verification/sub/",
    ]


def test_attach_covers_paths_dedupes_same_directory():
    """同一目录下的多个文件 / 多个任务 → 只产生一条范围。"""
    plan = [{"id": "VP-001", "source_task_ids": ["3", "4"]}]
    tasks = [
        {"id": "3", "files_to_modify": ["backend/verification/a.py"]},
        {"id": "4", "files_to_modify": ["backend/verification/b.py"]},
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == ["backend/verification/"]


def test_attach_covers_paths_does_not_merge_across_directories():
    """不跨目录合并：``backend/verification`` 与 ``backend/tests`` 各自一条。"""
    plan = [{"id": "VP-001", "source_task_ids": ["3"]}]
    tasks = [
        {
            "id": "3",
            "files_to_modify": [
                "backend/verification/orchestrator.py",
                "backend/tests/unit/x.py",
            ],
        }
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == [
        "backend/verification/",
        "backend/tests/unit/",
    ]
    assert "backend/" not in result[0]["covers_paths"]


def test_derived_paths_match_the_scope_helper():
    """派生结果与 :func:`covers_paths_for_files` 的口径一致（只是写成目录前缀）。"""
    files = ["backend/verification/orchestrator.py", "backend/tests/unit/x.py"]
    plan = [{"id": "VP-001", "source_task_ids": ["3"]}]
    tasks = [{"id": "3", "files_to_modify": files}]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == [
        f"{prefix}/" for prefix in covers_paths_for_files(files)
    ]


# ---------------------------------------------------------------------------
# 2. 任务 id 未知 / 字段缺失 → 空范围
# ---------------------------------------------------------------------------


def test_unknown_task_id_yields_empty():
    """任务 id 在任务清单里不存在 → 范围为空（这条 VP 每轮重跑）。"""
    plan = [{"id": "VP-001", "source_task_ids": ["999"]}]
    tasks = [
        {
            "id": "3",
            "files_to_modify": ["backend/verification/orchestrator.py"],
        }
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == []


def test_missing_source_task_ids_yields_empty():
    """VP 没声明 ``source_task_ids`` → 范围为空。"""
    plan = [{"id": "VP-001"}]
    tasks = [
        {
            "id": "3",
            "files_to_modify": ["backend/verification/orchestrator.py"],
        }
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == []


def test_no_task_rows_yields_empty():
    """没有任务行（读不到 tasks.json）→ 范围为空。"""
    plan = [{"id": "VP-001", "source_task_ids": ["3"]}]

    result = _attach_covers_paths(plan, [])

    assert result[0]["covers_paths"] == []


# ---------------------------------------------------------------------------
# 3. 哨兵不是路径
# ---------------------------------------------------------------------------


def test_sentinel_files_ignored():
    """``files_to_modify`` 只含哨兵 → 范围为空（哨兵不是路径）。"""
    plan = [{"id": "VP-001", "source_task_ids": ["3"]}]
    tasks = [
        {"id": "3", "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"]},
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == []

    plan = [{"id": "VP-002", "source_task_ids": ["4"]}]
    tasks = [{"id": "4", "files_to_modify": ["__NO_FILE_CHANGES__"]}]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == []


def test_sentinel_mixed_with_real_files_keeps_real_ones():
    """哨兵与真实文件混在一起 → 只取真实文件所在目录。"""
    plan = [{"id": "VP-001", "source_task_ids": ["3"]}]
    tasks = [
        {
            "id": "3",
            "files_to_modify": [
                "__UNKNOWN_MODIFICATIONS__",
                "backend/verification/orchestrator.py",
            ],
        }
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == ["backend/verification/"]


# ---------------------------------------------------------------------------
# 4. 作者自己声明的范围：保留（不被派生覆盖）
# ---------------------------------------------------------------------------


def test_authored_paths_are_preserved():
    """验证点自己声明的 ``covers_paths`` 被保留，不被任务派生结果覆盖。

    VP 管的是一个功能点，任务可以拆得很细 —— 二者不是绑定关系，所以范围
    以作者声明为准：偏宽只是多跑几条，偏窄会让不该复用的结论被复用。
    """
    plan = [
        {
            "id": "VP-001",
            "source_task_ids": ["3"],
            "covers_paths": ["frontend/src/"],
        }
    ]
    tasks = [
        {
            "id": "3",
            "files_to_modify": ["backend/verification/orchestrator.py"],
        }
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == ["frontend/src"]
    assert "backend/verification" not in result[0]["covers_paths"]


def test_authored_paths_survive_when_nothing_derivable():
    """没有任务映射时，声明的范围仍然保留（没有范围才是每轮重跑的那一侧）。"""
    plan = [
        {
            "id": "VP-001",
            "source_task_ids": ["999"],
            "covers_paths": ["frontend/src/"],
        }
    ]

    result = _attach_covers_paths(plan, task_rows=None)

    assert result[0]["covers_paths"] == ["frontend/src"]


def test_authored_paths_win_over_derived():
    """同时有声明的 ``covers_paths`` 与 ``source_task_ids`` → 声明胜出。

    派生结果不参与：声明的范围是作者（验证点）自己负责的那一片。
    """
    plan = [
        {
            "id": "VP-001",
            "source_task_ids": ["3", "4"],
            "covers_paths": ["backend/verification/"],
        }
    ]
    tasks = [
        {"id": "3", "files_to_modify": ["backend/verification/orchestrator.py"]},
        {"id": "4", "files_to_modify": ["frontend/src/app.tsx"]},
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == ["backend/verification"]


def test_authored_paths_are_normalised_and_deduped():
    """声明列表里的重复项 / 尾斜杠写法被归一化去重。"""
    plan = [
        {
            "id": "VP-001",
            "source_task_ids": ["3"],
            "covers_paths": [
                "backend/verification/",
                "backend/verification",
                "backend/verification//",
                "  backend/tests/unit/  ",
                "backend/tests/unit",
            ],
        }
    ]
    tasks = [
        {"id": "3", "files_to_modify": ["backend/verification/orchestrator.py"]},
    ]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["covers_paths"] == [
        "backend/verification",
        "backend/tests/unit",
    ]


def test_attach_covers_paths_keeps_other_vp_fields():
    """派生只写 ``covers_paths``，不碰 VP 上的其它字段。"""
    plan = [
        {
            "id": "VP-001",
            "title": "标题",
            "verification_method": "api_test",
            "source_task_ids": ["3"],
        }
    ]
    tasks = [{"id": "3", "files_to_modify": ["backend/verification/orchestrator.py"]}]

    result = _attach_covers_paths(plan, tasks)

    assert result[0]["title"] == "标题"
    assert result[0]["verification_method"] == "api_test"
    assert result[0]["source_task_ids"] == ["3"]


# ---------------------------------------------------------------------------
# 5. 读任务行那一步
# ---------------------------------------------------------------------------


def test_load_task_rows_reads_tasks_json(tmp_path):
    """``tasks.json`` 的任务行被读出来（``{"tasks": [...]}`` 载荷）。"""
    (tmp_path / "tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "3", "files_to_modify": ["backend/verification/x.py"]},
                    {"id": "4", "files_to_modify": ["backend/tests/unit/y.py"]},
                ]
            }
        ),
        encoding="utf-8",
    )

    rows = _load_task_rows(tmp_path)

    assert [row["id"] for row in rows] == ["3", "4"]


def test_load_task_rows_accepts_list_payload(tmp_path):
    """``tasks.json`` 直接是数组也认。"""
    (tmp_path / "tasks.json").write_text(
        json.dumps([{"id": "3", "files_to_modify": []}]),
        encoding="utf-8",
    )

    rows = _load_task_rows(tmp_path)

    assert [row["id"] for row in rows] == ["3"]


def test_load_task_rows_missing_file_returns_empty(tmp_path):
    """没有 ``tasks.json`` → 空表（不是错误）。"""
    assert _load_task_rows(tmp_path) == []


def test_load_task_rows_broken_json_returns_empty(tmp_path):
    """``tasks.json`` 解析失败 → 空表（不是错误）。"""
    (tmp_path / "tasks.json").write_text("{ not json", encoding="utf-8")

    assert _load_task_rows(tmp_path) == []


# ---------------------------------------------------------------------------
# 6. 接线：Phase-1 生成路径上范围真的被派生
# ---------------------------------------------------------------------------


def _make_agent(tmp_path, tasks_payload):
    """造一个只够跑 Phase-1 规划的 :class:`VerificationAgent`。"""
    from unittest.mock import Mock

    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    (plan_dir / "tasks.json").write_text(
        json.dumps(tasks_payload), encoding="utf-8",
    )
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)

    tool = Mock()
    tool.query_json.return_value = {
        "verification_points": [
            {
                "id": "VP-001",
                "title": "验证点",
                "related_prd_criteria": "验收标准",
                # 模型声明它验的是任务 3，但**没有**声明范围 —— 范围由框架从
                # 任务声明派生（作者声明的范围见下面那条用例）。
                "source_task_ids": ["3"],
                "verification_method": "code_review",
                "verification_phase": 1,
                "priority": "high",
                "expected_result": "验证通过",
            }
        ]
    }
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=tool,
    )
    return agent, tool


def test_generate_verification_plan_derives_covers_paths(tmp_path):
    """Phase-1 生成路径上，作者没声明范围 → 由任务声明派生。"""
    agent, _tool = _make_agent(
        tmp_path,
        {
            "tasks": [
                {
                    "id": "3",
                    "title": "派生范围",
                    "files_to_modify": [
                        "backend/verification/orchestrator.py",
                        "backend/tests/unit/x.py",
                    ],
                }
            ]
        },
    )

    plan = agent.generate_verification_plan(retry_llm=1)

    point = plan["verification_points"][0]
    assert point["covers_paths"] == [
        "backend/verification/",
        "backend/tests/unit/",
    ]


def test_generate_verification_plan_keeps_authored_covers_paths(tmp_path):
    """Phase-1 生成路径上，作者声明的范围被保留（不被任务派生覆盖）。"""
    agent, tool = _make_agent(
        tmp_path,
        {
            "tasks": [
                {
                    "id": "3",
                    "title": "派生范围",
                    "files_to_modify": ["backend/verification/orchestrator.py"],
                }
            ]
        },
    )
    tool.query_json.return_value["verification_points"][0]["covers_paths"] = [
        "frontend/src/",
    ]

    plan = agent.generate_verification_plan(retry_llm=1)

    point = plan["verification_points"][0]
    assert point["covers_paths"] == ["frontend/src"]
    assert "backend/verification" not in point["covers_paths"]


def test_generate_verification_plan_persists_covers_paths(tmp_path):
    """落盘的计划里也带着派生的范围（下一轮读到的就是它）。"""
    agent, _tool = _make_agent(
        tmp_path,
        {
            "tasks": [
                {
                    "id": "3",
                    "title": "派生范围",
                    "files_to_modify": ["backend/verification/orchestrator.py"],
                }
            ]
        },
    )

    agent.generate_verification_plan(retry_llm=1)

    saved = json.loads(
        (tmp_path / "plan" / "verification_plan.json").read_text(encoding="utf-8")
    )
    assert saved["verification_points"][0]["covers_paths"] == [
        "backend/verification/",
    ]


def test_planning_prompt_carries_the_task_catalog(tmp_path):
    """提示词里带任务清单（生成器据此填 ``source_task_ids``）。"""
    agent, tool = _make_agent(
        tmp_path,
        {
            "tasks": [
                {
                    "id": "3",
                    "title": "派生范围",
                    "files_to_modify": ["backend/verification/orchestrator.py"],
                }
            ]
        },
    )

    agent.generate_verification_plan(retry_llm=1)

    prompt = tool.query_json.call_args.kwargs["prompt"]
    assert "## 本次任务清单" in prompt
    assert "任务 3" in prompt
    assert "backend/verification/orchestrator.py" in prompt


def test_planning_prompt_hides_file_sentinels(tmp_path):
    """哨兵不是路径 → 提示词里不显示它（别让生成器以为那是真路径）。"""
    agent, tool = _make_agent(
        tmp_path,
        {
            "tasks": [
                {
                    "id": "3",
                    "title": "不改文件",
                    "files_to_modify": ["__UNKNOWN_MODIFICATIONS__"],
                }
            ]
        },
    )

    agent.generate_verification_plan(retry_llm=1)

    prompt = tool.query_json.call_args.kwargs["prompt"]
    assert "## 本次任务清单" in prompt
    assert "__UNKNOWN_MODIFICATIONS__" not in prompt
    assert "（未声明）" in prompt


def test_system_prompt_requires_authored_covers_paths():
    """规划提示词要求每条验证点声明 ``covers_paths``（目录级，宁宽勿窄）。

    提示词是这条链路的**生产入口**：范围的第一来源是验证点自己声明的
    文件夹，提示词必须把它说清楚 —— 包括"不再是被覆盖的字段"。
    """
    from prompts import VERIFICATION_PLAN_SYSTEM_PROMPT

    assert "必须声明 `covers_paths`" in VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "宁宽勿窄" in VERIFICATION_PLAN_SYSTEM_PROMPT
    # 旧口径（"不是你写的字段 / 你写了也会被覆盖"）必须不在提示词里。
    assert "`covers_paths` 不是你写的字段" not in VERIFICATION_PLAN_SYSTEM_PROMPT
    assert "你写了也会" not in VERIFICATION_PLAN_SYSTEM_PROMPT
    # 输出格式模板里也要有那个字段 —— 模型才知道要写它。
    assert '"covers_paths"' in VERIFICATION_PLAN_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# 7. 复用路径（磁盘上已经躺着计划）
# ---------------------------------------------------------------------------


def _make_reuse_agent(tmp_path, disk_points, tasks_payload=None):
    """造一个 plan 目录里已经躺着验证计划的 agent（走复用路径）。"""
    from verification_agent import VerificationAgent

    plan_dir = tmp_path / "plan"
    plan_dir.mkdir(exist_ok=True)
    (plan_dir / "verification_plan.json").write_text(
        json.dumps({"verification_points": disk_points}), encoding="utf-8",
    )
    if tasks_payload is not None:
        (plan_dir / "tasks.json").write_text(
            json.dumps(tasks_payload), encoding="utf-8",
        )
    project_dir = tmp_path / "project"
    project_dir.mkdir(exist_ok=True)
    agent = VerificationAgent(
        plan_dir=plan_dir, project_dir=project_dir, coding_tool=None,
    )
    return agent


def test_reuse_path_does_not_add_a_scope_field(tmp_path):
    """复用磁盘计划时，不给没有范围的验证点凭空造出 ``covers_paths`` 键。

    复用路径的既有契约是"只标注、不重问"（见
    ``tests/unit/test_plan_completeness.py::test_reused_plan_gets_the_gap_annotated``）：
    磁盘上那份计划的**结构**原样复用。派生不出范围时就不写这个键 ——
    读侧把"没有范围"当成"每轮重跑"（安全的一侧），不需要那个空键。
    """
    agent = _make_reuse_agent(
        tmp_path,
        [{"id": "VP-001", "title": "验证点", "verification_method": "code_review"}],
    )

    plan = agent.generate_verification_plan(retry_llm=1)

    assert "covers_paths" not in plan["verification_points"][0]


def test_reuse_path_keeps_an_authored_scope(tmp_path):
    """复用路径上，验证点自己声明的范围被保留（只归一化），不被任务派生覆盖。"""
    agent = _make_reuse_agent(
        tmp_path,
        [
            {
                "id": "VP-001",
                "title": "验证点",
                "verification_method": "code_review",
                "source_task_ids": ["3"],
                "covers_paths": ["frontend/src/"],
            }
        ],
        tasks_payload={
            "tasks": [
                {
                    "id": "3",
                    "files_to_modify": ["backend/verification/orchestrator.py"],
                }
            ]
        },
    )

    plan = agent.generate_verification_plan(retry_llm=1)

    assert plan["verification_points"][0]["covers_paths"] == ["frontend/src"]


def test_reuse_path_derives_a_scope_when_none_is_authored(tmp_path):
    """复用路径上，没有声明范围的验证点按任务声明派生（兜底路径仍然有效）。"""
    agent = _make_reuse_agent(
        tmp_path,
        [
            {
                "id": "VP-001",
                "title": "验证点",
                "verification_method": "code_review",
                "source_task_ids": ["3"],
            }
        ],
        tasks_payload={
            "tasks": [
                {
                    "id": "3",
                    "files_to_modify": ["backend/verification/orchestrator.py"],
                }
            ]
        },
    )

    plan = agent.generate_verification_plan(retry_llm=1)

    assert plan["verification_points"][0]["covers_paths"] == ["backend/verification/"]
