"""Tier B 的固定语料：约 20 组手写输入，不带任何真实标识。

这个文件是什么
--------------
§5.2 的做法是「一次性手写一小撮固定输入，存在测试夹具里」，然后由
``scripts/judgment_snapshots.py`` 把这些输入跑出来的输出冻结成 JSON。
金标不声称行为是**对的**，只声称行为**没变**——这正是它能覆盖长尾的
原因：长尾上没人需要为每个分支写一句语义期望，只需要把输入固定住。

所以这个文件只做一件事：**声明输入**。它不含任何判定函数，也不调用
任何判定函数；谁要用这些输入，由谁在自己的模块里把输入接上自己的
判定函数。导入它不会跑任何东西，也不会写任何东西。

组的形状
--------
键是**快照组名**（``scripts/judgment_snapshots.py`` 的 ``REGISTRY``
用的就是这个名字），值是这个组的用例列表。每个用例是一个字典，带
三个键：

``id``
    这一条钉住的是什么。用 kebab-case 的多个词写成，不许写成
    ``case1`` 这种计数器——那等于没说。id 在整份语料里唯一。
``vp``
    输入侧：这道验证点是什么（``id`` / ``verification_method`` /
    ``expected_result`` / ``entry_command`` / ``ci_entry`` /
    ``e2e_kind``）。有些判定面不吃「验证点」而吃别的形状（一次失败
    的输出、一条重试状态），这时这一侧装的就是那份输入。
``verdict``
    结论侧：裁决本身（``status`` / ``reasons`` / ``evidence``）。判定
    面要核对的载荷（引用、检查点……）挂在 ``evidence`` 下，键名与
    产物文件里的载荷同名，这样语料既能被判定函数直接读，也能被产物
    核对读。

每组对应哪一段判定面
--------------------
组名沿用判定面所在模块的名字（``verification_evidence`` 简称
``evidence``），这样注册快照组时可以直接拿组名去找它对应的函数：

===================  ===============================================
组                   钉住的那段判定面
===================  ===============================================
``evidence``         命令证据：这条裁决有没有真跑过它声明的命令
``verdict_subject``  主语闸：这条裁决答的是不是这道题
``verdict_downgrade`` 主语闸够到裁决上时，只降级、不放过
``citations``        引用核对：结论落到哪一行、那行是不是那么写
``checkpoints``      检查点核对：界面结论必须留下可观察的记录
``declared_entry``   声明入口：这道题问的是哪条命令
``projected_entry``  投影形态：裁决被投影之后记录还剩多少
``evidence_command`` 证据命令：框架替验证点跑的那条命令
``artifact_stage``   产物归档：验证点的产物落到哪儿
``tree_fingerprint`` 树指纹：跑完之后仓里的东西变了没有
``agent``            越界路径与声明文件是否存在
``executor``         关卡阻塞理由与延后层重算
``buckets``          验证点归桶
``failure_classification`` 失败分类：这次失败属于哪一类
``retry_manager``    重试：这次失败要不要再跑一遍
``ci_runner``        运行器看到的失败形状与阻塞判定
``loop``             轮次与截止
``subagent``         子代理的证据契约与超时
``verification_agent`` 编排层的证据核对与结果聚合
``framework_fix``     框架缺陷判定：这次失败阻不阻塞，计划显示成什么状态
===================  ===============================================

为什么这里不放真实数据
----------------------
§12.9 要求「Tier B 的固定输入不得包含真实 plan id、私有路径或人名」。
理由不是洁癖：语料随仓库入库、进 git 历史、会被任何人读到，而**公开
过的东西收不回来**（强制推送只删 ref，不删对象）。所以这里的一切都是
合成的：路径一律是仓内相对路径（``backend/x.py``），验证点编号一律是
``VP-1xx``，人一律不出现。

这道约束由 ``backend/tests/unit/test_judgment_corpus_anonymity.py`` 守着：
它按文本形状（plan id、绝对路径、邮箱、人名）扫这份语料，并要求每条
id 唯一且有描述性。

只读、无副作用
--------------
这个模块只有字面量数据：没有 import、没有函数调用、没有环境变量读写、
没有文件读写。它是**输入**，不是状态——一份会自己动的输入，钉住的就
不是输入，而是当时的运行环境。
"""

from __future__ import annotations

__all__ = ["CASES"]


#: The fixed corpus: one entry per snapshot group, one list of cases per
#: group.  Every case is ``{"id": ..., "vp": ..., "verdict": ...}``.
CASES: dict[str, list[dict]] = {

    # ------------------------------------------------------------------
    # 命令证据：一条裁决有没有真的跑过它声明的那条命令。
    # ------------------------------------------------------------------
    "evidence": [
        {
            "id": "command-entry-exit-zero",
            "vp": {
                "id": "VP-101",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["声明的命令跑完了，退出码 0"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "command-entry-exit-nonzero",
            "vp": {
                "id": "VP-102",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["声明的命令跑完了，退出码 1"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 1,
                },
            },
        },
        {
            "id": "command-evidence-records-another-command",
            "vp": {
                "id": "VP-103",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["跑的是另一条命令"],
                "evidence": {
                    "entry_command": "pytest backend/tests/contract -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "command-evidence-without-exit-code",
            "vp": {
                "id": "VP-104",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["命令跑过了"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                },
            },
        },
        {
            "id": "command-evidence-is-not-a-dict",
            "vp": {
                "id": "VP-105",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["命令跑过了"],
                "evidence": "pytest backend/tests/unit -q 全部通过",
            },
        },
        {
            "id": "ci-entry-exit-zero",
            "vp": {
                "id": "VP-106",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["CI 入口跑完了，退出码 0"],
                "evidence": {
                    "ci_entry": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "projected-command-record-carries-exit-code",
            "vp": {
                "id": "VP-107",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["投影形态里留着运行器写下的那条记录"],
                "evidence": {
                    "details": "命令已执行",
                    "raw": [
                        '{"entry_command": "pytest backend/tests/unit -q", '
                        '"exit_code": 0}'
                    ],
                },
            },
        },
        {
            "id": "projected-raw-line-is-prose-only",
            "vp": {
                "id": "VP-108",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["投影形态里只有一段话"],
                "evidence": {
                    "details": "命令已执行",
                    "raw": ["pytest backend/tests/unit -q 全部通过"],
                },
            },
        },
        {
            "id": "point-without-declared-entry",
            "vp": {
                "id": "VP-109",
                "verification_method": "e2e",
                "e2e_kind": "command",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["跑了一条命令，但没有声明过入口"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 主语闸：这条裁决答的是不是这道题。
    # ------------------------------------------------------------------
    "verdict_subject": [
        {
            "id": "code-review-about-another-file",
            "vp": {
                "id": "VP-110",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["我看了 backend/y.py"],
                "evidence": {"details": "看了 backend/y.py"},
            },
        },
        {
            "id": "code-review-about-the-same-file",
            "vp": {
                "id": "VP-111",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["我看了 backend/x.py"],
                "evidence": {"details": "看了 backend/x.py"},
            },
        },
        {
            "id": "code-review-about-the-bare-file-name",
            "vp": {
                "id": "VP-112",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["我看了 x.py"],
                "evidence": {"details": "看了 x.py"},
            },
        },
        {
            "id": "framework-graded-point-is-not-judged",
            "vp": {
                "id": "VP-113",
                "verification_method": "full_ci",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["我看了 backend/y.py"],
                "evidence": {"details": "看了 backend/y.py"},
            },
        },
        {
            "id": "missing-subject-file-is-not-judged",
            "vp": {
                "id": "VP-114",
                "verification_method": "code_review",
                "expected_result": "检查 backend/never_written.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["我看了 backend/y.py"],
                "evidence": {"details": "看了 backend/y.py"},
            },
        },
        {
            "id": "command-record-exempt-from-subject-check",
            "vp": {
                "id": "VP-115",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["命令跑完了"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 降级：主语闸够不着「把失败说成通过」的那条路。
    # ------------------------------------------------------------------
    "verdict_downgrade": [
        {
            "id": "passed-off-subject-is-downgraded",
            "vp": {
                "id": "VP-116",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["我看了 backend/y.py"],
                "evidence": {"details": "看了 backend/y.py"},
            },
        },
        {
            "id": "passed-on-subject-is-untouched",
            "vp": {
                "id": "VP-117",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["我看了 backend/x.py"],
                "evidence": {"details": "看了 backend/x.py"},
            },
        },
        {
            "id": "failed-verdict-is-never-rewritten",
            "vp": {
                "id": "VP-118",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["backend/x.py 的门禁不满足"],
                "evidence": {"details": "门禁没过"},
            },
        },
        {
            "id": "non-dict-verdict-is-handed-back",
            "vp": {
                "id": "VP-119",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": "这不是裁决",
        },
    ],

    # ------------------------------------------------------------------
    # 引用核对：结论落到哪一行、那一行是不是真的那么写。
    # ------------------------------------------------------------------
    "citations": [
        {
            "id": "citation-names-file-line-and-snippet",
            "vp": {
                "id": "VP-120",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["结论有出处"],
                "evidence": {
                    "citations": [
                        {
                            "file": "backend/x.py",
                            "line": 12,
                            "snippet": "def f():",
                        }
                    ]
                },
            },
        },
        {
            "id": "citation-without-a-line",
            "vp": {
                "id": "VP-121",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["结论有出处"],
                "evidence": {
                    "citations": [
                        {
                            "file": "backend/x.py",
                            "snippet": "def f():",
                        }
                    ]
                },
            },
        },
        {
            "id": "citation-without-a-snippet",
            "vp": {
                "id": "VP-122",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["结论有出处"],
                "evidence": {
                    "citations": [
                        {
                            "file": "backend/x.py",
                            "line": 12,
                        }
                    ]
                },
            },
        },
        {
            "id": "citation-pointing-outside-the-project",
            "vp": {
                "id": "VP-123",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["结论有出处"],
                "evidence": {
                    "citations": [
                        {
                            "file": "backend/../../elsewhere/y.py",
                            "line": 3,
                            "snippet": "def g():",
                        }
                    ]
                },
            },
        },
        {
            "id": "citations-not-a-list",
            "vp": {
                "id": "VP-124",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["结论有出处"],
                "evidence": {"citations": {}},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 检查点核对：界面结论必须留下可观察的记录。
    # ------------------------------------------------------------------
    "checkpoints": [
        {
            "id": "checkpoint-records-selector-and-observation",
            "vp": {
                "id": "VP-125",
                "verification_method": "ui_validation",
                "expected_result": "界面显示一行结果",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["每个检查点都有观察记录"],
                "evidence": {
                    "checkpoints": [
                        {
                            "selector": "#result-row",
                            "expected": "两行结果",
                            "actual": "两行结果",
                            "passed": True,
                        }
                    ]
                },
            },
        },
        {
            "id": "checkpoint-without-a-selector",
            "vp": {
                "id": "VP-126",
                "verification_method": "ui_validation",
                "expected_result": "界面显示一行结果",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["看起来没问题"],
                "evidence": {
                    "checkpoints": [
                        {
                            "expected": "两行结果",
                            "actual": "两行结果",
                            "passed": True,
                        }
                    ]
                },
            },
        },
        {
            "id": "checkpoint-without-an-observation",
            "vp": {
                "id": "VP-127",
                "verification_method": "ui_validation",
                "expected_result": "界面显示一行结果",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["看起来没问题"],
                "evidence": {
                    "checkpoints": [
                        {
                            "selector": "#result-row",
                            "expected": "两行结果",
                            "passed": True,
                        }
                    ]
                },
            },
        },
        {
            "id": "checkpoints-not-a-list",
            "vp": {
                "id": "VP-128",
                "verification_method": "ui_validation",
                "expected_result": "界面显示一行结果",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["看起来没问题"],
                "evidence": {"checkpoints": []},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 声明入口：这道题问的是哪条命令。
    # ------------------------------------------------------------------
    "declared_entry": [
        {
            "id": "declared-entry-reads-the-command-field",
            "vp": {
                "id": "VP-129",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["命令一致"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "declared-entry-reads-the-ci-field",
            "vp": {
                "id": "VP-130",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["命令一致"],
                "evidence": {
                    "ci_entry": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "declared-entry-prefers-the-command-field",
            "vp": {
                "id": "VP-131",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
                "ci_entry": "pytest backend/tests/contract -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["跑的是声明的那条命令"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "point-without-any-declared-entry",
            "vp": {
                "id": "VP-132",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["没有声明入口"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "command-differences-in-whitespace-are-noise",
            "vp": {
                "id": "VP-133",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["同一条命令"],
                "evidence": {
                    "entry_command": "pytest   backend/tests/unit   -q",
                    "exit_code": 0,
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 投影形态：裁决被投影之后，运行器的记录还剩多少。
    # ------------------------------------------------------------------
    "projected_entry": [
        {
            "id": "projected-record-carries-command-and-exit-code",
            "vp": {
                "id": "VP-134",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["投影里留着记录"],
                "evidence": {
                    "details": "命令已执行",
                    "raw": [
                        '{"entry_command": "pytest backend/tests/unit -q", '
                        '"exit_code": 0}'
                    ],
                },
            },
        },
        {
            "id": "projected-record-without-an-exit-code",
            "vp": {
                "id": "VP-135",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["投影里留着记录，但没写退出码"],
                "evidence": {
                    "details": "命令已执行",
                    "raw": [
                        '{"entry_command": "pytest backend/tests/unit -q"}'
                    ],
                },
            },
        },
        {
            "id": "projected-record-of-another-command",
            "vp": {
                "id": "VP-136",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["投影里留着另一条命令的记录"],
                "evidence": {
                    "details": "命令已执行",
                    "raw": [
                        '{"entry_command": "pytest backend/tests/contract -q", '
                        '"exit_code": 0}'
                    ],
                },
            },
        },
        {
            "id": "browser-point-does-not-earn-the-exemption",
            "vp": {
                "id": "VP-137",
                "verification_method": "e2e",
                "e2e_kind": "browser",
                "expected_result": "界面显示一行结果",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["浏览器点的证据是叙述，不是运行器的记录"],
                "evidence": {
                    "details": "命令已执行",
                    "raw": [
                        '{"entry_command": "pytest backend/tests/unit -q", '
                        '"exit_code": 0}'
                    ],
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 证据命令：框架替验证点跑的那条命令。
    # ------------------------------------------------------------------
    "evidence_command": [
        {
            "id": "command-runs-and-records-its-exit-code",
            "vp": {
                "id": "VP-138",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["命令跑完了"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": 0,
                },
            },
        },
        {
            "id": "command-not-found-is-recorded-as-such",
            "vp": {
                "id": "VP-139",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["命令不存在"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": None,
                    "error": "command not found",
                },
            },
        },
        {
            "id": "command-timeout-is-recorded-as-such",
            "vp": {
                "id": "VP-140",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["命令超时"],
                "evidence": {
                    "entry_command": "pytest backend/tests/unit -q",
                    "exit_code": None,
                    "error": "timed out",
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 产物归档：验证点的产物落到哪儿。
    # ------------------------------------------------------------------
    "artifact_stage": [
        {
            "id": "artifact-is-staged-under-the-point-directory",
            "vp": {
                "id": "VP-141",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["产物归档了"],
                "evidence": {"details": "vp_artifacts/VP-141/citations.json"},
            },
        },
        {
            "id": "artifact-source-is-missing",
            "vp": {
                "id": "VP-142",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["产物不在"],
                "evidence": {"details": "vp_artifacts/VP-142/citations.json"},
            },
        },
        {
            "id": "artifact-target-leaves-the-staging-root",
            "vp": {
                "id": "VP-143",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["归档路径跑出了暂存根"],
                "evidence": {"details": "vp_artifacts/VP-143/../elsewhere.json"},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 树指纹：跑完之后仓里的东西变了没有。
    # ------------------------------------------------------------------
    "tree_fingerprint": [
        {
            "id": "tree-is-unchanged-after-the-run",
            "vp": {
                "id": "VP-144",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["仓没动"],
                "evidence": {"details": "fingerprint: same"},
            },
        },
        {
            "id": "tree-changed-after-the-run",
            "vp": {
                "id": "VP-145",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["仓被改了"],
                "evidence": {"details": "fingerprint: changed"},
            },
        },
        {
            "id": "no-baseline-is-not-a-change",
            "vp": {
                "id": "VP-146",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["没有基线可比"],
                "evidence": {"details": "fingerprint: none"},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 越界路径与声明文件：任务写到的路径是不是还在这个仓里。
    # ------------------------------------------------------------------
    "agent": [
        {
            "id": "declared-path-inside-the-project",
            "vp": {
                "id": "VP-147",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["路径都在仓内"],
                "evidence": {"details": "只写了 backend/x.py"},
            },
        },
        {
            "id": "declared-path-leaving-the-project",
            "vp": {
                "id": "VP-148",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["路径跑出了仓"],
                "evidence": {"details": "写了 backend/../elsewhere/y.py"},
            },
        },
        {
            "id": "no-declared-path-at-all",
            "vp": {
                "id": "VP-149",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["没有写路径"],
                "evidence": {"details": "改了一处逻辑"},
            },
        },
        {
            "id": "declared-file-exists-in-the-tree",
            "vp": {
                "id": "VP-150",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["文件在"],
                "evidence": {"details": "改的是 backend/x.py"},
            },
        },
        {
            "id": "declared-file-missing-from-the-tree",
            "vp": {
                "id": "VP-151",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["文件不在"],
                "evidence": {"details": "改的是 backend/never_written.py"},
            },
        },
        {
            "id": "no-declared-files-at-all",
            "vp": {
                "id": "VP-152",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["没有声明文件"],
                "evidence": {"details": "只写了说明"},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 关卡阻塞理由与延后层重算。
    # ------------------------------------------------------------------
    "executor": [
        {
            "id": "gate-blocked-by-a-missing-artifact",
            "vp": {
                "id": "VP-153",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["结论没有出处"],
                "evidence": {"details": "缺 citations.json"},
            },
        },
        {
            "id": "gate-blocked-by-a-failed-verification-point",
            "vp": {
                "id": "VP-154",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["验证点没过"],
                "evidence": {"details": "验证点 VP-154 是 FAILED"},
            },
        },
        {
            "id": "gate-not-blocked",
            "vp": {
                "id": "VP-155",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["出处齐全，验证点也过了"],
                "evidence": {"details": "citations 与 VP-155 都对得上"},
            },
        },
        {
            "id": "deferred-layer-reruns-after-the-gate-opens",
            "vp": {
                "id": "VP-156",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["关卡放行，延后层重算"],
                "evidence": {"details": "延后层：索引层"},
            },
        },
        {
            "id": "deferred-layer-stays-closed-while-the-gate-holds",
            "vp": {
                "id": "VP-157",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["关卡没放行，延后层不动"],
                "evidence": {"details": "延后层：索引层"},
            },
        },
        {
            "id": "deferred-layer-order-is-stable",
            "vp": {
                "id": "VP-158",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["层的顺序不变"],
                "evidence": {"details": "顺序：基础层、延后层"},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 验证点归桶。
    # ------------------------------------------------------------------
    "buckets": [
        {
            "id": "code-review-point-lands-in-the-review-bucket",
            "vp": {
                "id": "VP-159",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["归到评审桶"],
                "evidence": {"details": "bucket: review"},
            },
        },
        {
            "id": "command-point-lands-in-the-command-bucket",
            "vp": {
                "id": "VP-160",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["归到命令桶"],
                "evidence": {"details": "bucket: command"},
            },
        },
        {
            "id": "browser-point-lands-in-the-ui-bucket",
            "vp": {
                "id": "VP-161",
                "verification_method": "ui_validation",
                "expected_result": "界面显示一行结果",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["归到界面桶"],
                "evidence": {"details": "bucket: ui"},
            },
        },
        {
            "id": "unknown-method-lands-in-the-default-bucket",
            "vp": {
                "id": "VP-162",
                "verification_method": "api_test",
                "expected_result": "接口返回约定的结果",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["归到默认桶"],
                "evidence": {"details": "bucket: default"},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 失败分类：一次执行没通过，它属于哪一类失败。
    # ------------------------------------------------------------------
    "failure_classification": [
        {
            "id": "timeout-output-classifies-as-a-timeout",
            "vp": {
                "id": "VP-163",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["命令超时"],
                "evidence": {"exit_code": None, "timed_out": True},
            },
        },
        {
            "id": "nonzero-exit-classifies-as-a-test-failure",
            "vp": {
                "id": "VP-164",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["命令挂了"],
                "evidence": {"exit_code": 1, "timed_out": False},
            },
        },
        {
            "id": "missing-file-classifies-as-an-environment-problem",
            "vp": {
                "id": "VP-165",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["文件不在"],
                "evidence": {"exit_code": None, "missing_file": True},
            },
        },
        {
            "id": "unreadable-output-classifies-as-unknown",
            "vp": {
                "id": "VP-166",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["输出读不出来"],
                "evidence": {"exit_code": None, "output": ""},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 重试：这次失败要不要再跑一遍。
    # ------------------------------------------------------------------
    "retry_manager": [
        {
            "id": "retryable-failure-is-retried",
            "vp": {
                "id": "VP-167",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["环境抖了"],
                "evidence": {"exit_code": None, "retryable": True},
            },
        },
        {
            "id": "terminal-failure-is-not-retried",
            "vp": {
                "id": "VP-168",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["这条命令本身是错的"],
                "evidence": {"exit_code": 2, "retryable": False},
            },
        },
        {
            "id": "retry-budget-is-spent",
            "vp": {
                "id": "VP-169",
                "verification_method": "e2e",
                "e2e_kind": "command",
                "entry_command": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["重试用完了"],
                "evidence": {"exit_code": None, "retryable": True, "attempts": 3},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 运行器看到的失败形状与阻塞判定。
    # ------------------------------------------------------------------
    "ci_runner": [
        {
            "id": "failing-test-counts-as-a-blocking-failure",
            "vp": {
                "id": "VP-170",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["有一个用例挂了"],
                "evidence": {
                    "details": "1 failed",
                    "junit": {"tests": 3, "failures": 1, "errors": 0},
                },
            },
        },
        {
            "id": "skipped-test-is-not-a-blocking-failure",
            "vp": {
                "id": "VP-171",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["只有一个用例被跳过"],
                "evidence": {
                    "details": "1 skipped",
                    "junit": {"tests": 3, "failures": 0, "errors": 0},
                },
            },
        },
        {
            "id": "empty-report-is-inconclusive",
            "vp": {
                "id": "VP-172",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["报告是空的，说不清有没有失败"],
                "evidence": {
                    "details": "",
                    "junit": {"tests": 0, "failures": 0, "errors": 0},
                },
            },
        },
        {
            "id": "failure-shape-keeps-the-test-name",
            "vp": {
                "id": "VP-173",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["用例名要留在失败形状里"],
                "evidence": {
                    "details": "test_corpus_has_no_plan_id_or_absolute_path",
                    "junit": {"tests": 1, "failures": 1, "errors": 0},
                },
            },
        },
        {
            "id": "python-test-belongs-to-the-unit-lane",
            "vp": {
                "id": "VP-174",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["这条用例属于 unit 分片"],
                "evidence": {
                    "details": "backend/tests/unit/test_judgment_corpus.py",
                    "lane": "unit",
                },
            },
        },
        {
            "id": "path-like-token-is-recognized",
            "vp": {
                "id": "VP-175",
                "verification_method": "full_ci",
                "ci_entry": "pytest backend/tests/unit -q",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["这一段看着像路径"],
                "evidence": {
                    "details": "backend/tests/unit/test_judgment_corpus.py",
                    "looks_like_path": True,
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 轮次与截止：这一轮还该不该继续。
    # ------------------------------------------------------------------
    "loop": [
        {
            "id": "deadline-not-reached-keeps-the-round-running",
            "vp": {
                "id": "VP-176",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["还没到截止时间"],
                "evidence": {"details": "deadline_passed: False"},
            },
        },
        {
            "id": "deadline-reached-ends-the-round",
            "vp": {
                "id": "VP-177",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["到截止时间了"],
                "evidence": {"details": "deadline_passed: True"},
            },
        },
        {
            "id": "all-unfinished-blocked-by-a-failed-upstream",
            "vp": {
                "id": "VP-178",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["上游失败，剩下的一动不了"],
                "evidence": {"details": "blocked_by_failed_upstream: True"},
            },
        },
        {
            "id": "divergence-gates-pass",
            "vp": {
                "id": "VP-179",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["漂移的关卡都过了"],
                "evidence": {"details": "divergence_gates_pass: True"},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 子代理的证据契约与超时。
    # ------------------------------------------------------------------
    "subagent": [
        {
            "id": "declared-timeout-comes-from-the-point",
            "vp": {
                "id": "VP-180",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
                "timeout_seconds": 600,
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["按验证点声明的超时跑"],
                "evidence": {"details": "timeout_seconds: 600"},
            },
        },
        {
            "id": "evidence-contract-missing-for-a-method-that-owes-evidence",
            "vp": {
                "id": "VP-181",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["该交的产物没交"],
                "evidence": {"details": "evidence_contract_issues: 1"},
            },
        },
        {
            "id": "evidence-retry-block-after-a-failed-attempt",
            "vp": {
                "id": "VP-182",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["上一次尝试没产出证据，先挡一会儿"],
                "evidence": {"details": "evidence_retry_block: True"},
            },
        },
        {
            "id": "checkpoint-summary-counts-the-failed-ones",
            "vp": {
                "id": "VP-183",
                "verification_method": "ui_validation",
                "expected_result": "界面显示一行结果",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["两个检查点里有一个没过"],
                "evidence": {
                    "details": "checkpoints: 2, failed: 1",
                    "checkpoints": [
                        {
                            "selector": "#result-row",
                            "expected": "两行结果",
                            "actual": "两行结果",
                            "passed": True,
                        },
                        {
                            "selector": "#result-empty",
                            "expected": "没有空行",
                            "actual": "有一行空的",
                            "passed": False,
                        },
                    ],
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 编排层的证据核对与结果聚合。
    # ------------------------------------------------------------------
    "verification_agent": [
        {
            "id": "evidence-check-leaves-a-failed-verdict-alone",
            "vp": {
                "id": "VP-184",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["这个结论本来就是失败的"],
                "evidence": {"details": "缺 citations.json"},
            },
        },
        {
            "id": "attempt-count-comes-from-the-ledger",
            "vp": {
                "id": "VP-185",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["这是第二次尝试"],
                "evidence": {"details": "attempts: 2"},
            },
        },
        {
            "id": "split-verdicts-are-aggregated-into-one",
            "vp": {
                "id": "VP-186",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["拆出来的几份合回一份"],
                "evidence": {
                    "details": "split: 2",
                    "results": [
                        {"id": "split-1", "status": "PASSED"},
                        {"id": "split-2", "status": "PASSED"},
                    ],
                },
            },
        },
    ],

    # ------------------------------------------------------------------
    # 裁决理由：理由列表本身的形状。
    # ------------------------------------------------------------------
    "verdict_reasons": [
        {
            "id": "reasons-are-a-list-of-strings",
            "vp": {
                "id": "VP-187",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["出处齐全", "门禁也过了"],
                "evidence": {"details": "两条理由"},
            },
        },
        {
            "id": "reasons-missing-entirely",
            "vp": {
                "id": "VP-188",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "evidence": {"details": "没写理由"},
            },
        },
        {
            "id": "reasons-are-not-a-list",
            "vp": {
                "id": "VP-189",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": "出处齐全",
                "evidence": {"details": "理由不是列表"},
            },
        },
        {
            "id": "reasons-carry-an-empty-string",
            "vp": {
                "id": "VP-190",
                "verification_method": "code_review",
                "expected_result": "检查 backend/x.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["", "出处齐全"],
                "evidence": {"details": "有一条空理由"},
            },
        },
    ],

    # ------------------------------------------------------------------
    # 框架缺陷判定：这次失败阻不阻塞，计划对外显示成什么状态。
    #
    # 这一组的 ``evidence`` 装的是**判定输入**，不是结论：命中事实、复现
    # 结论、计划状态与注册表行。结论由金标从判定函数本身读出来——语料
    # 不写「应该是什么」，它只固定「拿什么去判」。这正是金标的约定：
    # 冻结行为，不断言正确性。
    #
    # ``hit`` 里只有两个布尔事实，因为判定函数只读这两个；其余字段对判
    # 定没有影响，不写进语料，免得读的人以为它们也在参与判定。
    # ------------------------------------------------------------------
    "framework_fix": [
        {
            "id": "non-acceptance-critical-never-blocks",
            "vp": {
                "id": "VP-201",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["这次失败不落在验收关键上"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": False,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "framework-deterministic-reproduction-blocks",
            "vp": {
                "id": "VP-202",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["复现成框架缺陷"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": "framework_deterministic",
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "framework-timeout-reproduction-blocks",
            "vp": {
                "id": "VP-203",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["复现成框架超时"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": "framework_timeout",
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "flake-outranks-out-of-scope-change",
            "vp": {
                "id": "VP-204",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["这次是抖动，改动也越界了"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": True,
                    },
                    "reproduced": "flake",
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "not-reproducible-outranks-out-of-scope-change",
            "vp": {
                "id": "VP-205",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["本地复现不出来，改动也越界了"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": True,
                    },
                    "reproduced": "not_reproducible",
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "out-of-scope-change-blocks-without-a-reproduction",
            "vp": {
                "id": "VP-206",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["改动越出了声明的文件集"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": True,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "consecutive-unclassified-rounds-exhaust-the-budget",
            "vp": {
                "id": "VP-207",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["连续几轮都归不了类"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": "unclassified",
                    "unclassified_rounds": 2,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "unclassified-reproduction-registers-its-evidence",
            "vp": {
                "id": "VP-208",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["跑过了，但归不了类"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": "unclassified",
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "not-reproduced-yet-needs-a-reproduction",
            "vp": {
                "id": "VP-209",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/classifier.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["还没复现过，本轮判不了"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "held-plan-shows-the-blocked-state",
            "vp": {
                "id": "VP-210",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/read_model.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["计划的工作被框架缺陷挂起了"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {"framework_fix": {"hold": True}},
                    "registry_row": {},
                },
            },
        },
        {
            "id": "exhausted-outranks-waiting-on-user-merge",
            "vp": {
                "id": "VP-211",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/read_model.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["轮次耗尽了，修复也等着合并"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {
                        "framework_fix": {"status": "waiting_on_user_merge"},
                    },
                    "registry_row": {"status": "exhausted"},
                },
            },
        },
        {
            "id": "deadlock-outranks-reopened",
            "vp": {
                "id": "VP-212",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/read_model.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["修复计划建了却没有执行路径"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {
                        "framework_fix": {
                            "status": "scheduler_deadlock_suspected",
                        },
                    },
                    "registry_row": {"status": "reopened"},
                },
            },
        },
        {
            "id": "reopened-outranks-waiting-on-user-merge",
            "vp": {
                "id": "VP-213",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/read_model.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["合入后重跑又命中了"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {
                        "framework_fix": {"status": "waiting_on_user_merge"},
                    },
                    "registry_row": {"status": "reopened"},
                },
            },
        },
        {
            "id": "a-ready-fix-shows-waiting-on-user-merge",
            "vp": {
                "id": "VP-214",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/read_model.py",
            },
            "verdict": {
                "status": "FAILED",
                "reasons": ["修复做好了，等着批准合并"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {"status": "fixed"},
                },
            },
        },
        {
            "id": "a-plan-without-framework-fix-state-shows-nothing",
            "vp": {
                "id": "VP-215",
                "verification_method": "code_review",
                "expected_result": "检查 backend/framework_fix/read_model.py",
            },
            "verdict": {
                "status": "PASSED",
                "reasons": ["这个计划没有框架修复的事"],
                "evidence": {
                    "hit": {
                        "acceptance_critical": True,
                        "out_of_scope_change": False,
                    },
                    "reproduced": None,
                    "unclassified_rounds": 0,
                    "plan_state": {},
                    "registry_row": {},
                },
            },
        },
    ],
}
