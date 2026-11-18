"""R5 —— 既有语义没被这次改动顺手改掉。

背景（2026-11-17，plan 20261117-repair-coverage-hitl-2）
------------------------------------------------------
任务 1–5 动了 orchestrator：新增缺失 VP 的 HITL、覆盖校验与单次补生成、
infra 终端 VP 也开 HITL。这几条都是**加**东西，但它们站在四块既有语义
的肩上 —— 那四块一旦被顺手改了，没人会立刻发现，因为每一块都还有别的
用法：

* **四类 disposition**（``verification.failure_triage._DISPOSITIONS``）——
  决定一次失败算不算数。``retry_counted`` 与 ``escalate_counted`` 都会
  让 VP 走到同一条 HITL 路径，被改错了只会表现成「重复问了一次」；
* **infra 重试上限**（``MAX_INFRA_CROSS_ROUND_RETRIES``）—— 决定环境问题
  能拖几轮。被改大了是「永远重试」，被改小了是「明明能跑却提前停」；
* **out_of_scope 选项与默认值**（``verification.orchestrator``）——
  决定「这个检查没人要求过」时问什么、没人的时候默认按哪一条走。默认值
  换了方向，``auto`` 模式就会替人做另一个决定；
* **repair id 分配**（``repair_generator.next_repair_seq_base``）——
  决定下一条 repair 叫什么。id 复用会让 upsert 覆盖旧任务的运行历史
  （2026-09-20、2026-11-02 两次事故）。

本文件是**钉子**，不是测试：它断言的是**运行时常量**，不是源码文本。
读源码文本的断言会被注释、格式化、别名绕过去；钉运行时值，改代码的人
绕不过去 —— 除非他连常量一起改，那正是这个文件要让他看见的事。

钉住的四件事
------------
1. 四类 disposition 的映射不变；
2. infra 重试上限是 2（即最多 3 次尝试：首试 + 2 次跨轮重试）；
3. out_of_scope 的选项与默认值不变，且默认值确实在选项里；
4. repair id 分配是**计划全局**的：``next_repair_seq_base({"repair-29"}, 3)``
   是 30 —— 轮次（这里是 3）不参与分配。

这个文件**只读**：不写文件、不发请求、不看时钟、不新增依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
_REPO_ROOT = _BACKEND_DIR.parent
for _path in (str(_REPO_ROOT), str(_BACKEND_DIR)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from repair_generator import next_repair_seq_base  # noqa: E402
from verification.failure_triage import (  # noqa: E402
    MAX_INFRA_CROSS_ROUND_RETRIES,
    _DISPOSITIONS,
)
from verification.orchestrator import (  # noqa: E402
    OUT_OF_SCOPE_DEFAULT_OPTION,
    OUT_OF_SCOPE_OPTIONS,
)


def test_disposition_table_frozen():
    """四类失败 → disposition 的映射没变。

    表里有四类，一类都不能少（少一类 = 那一类的失败没人处置），也不能多
    （多一类 = 有人加了一张没走过的分支）。每一类对应哪个 disposition 也
    不能换：``deliverable_defect`` 与 ``criterion_defect`` 都进 HITL，但
    前者 ``retry_counted``（产物坏了，重试有意义）、后者
    ``escalate_counted``（判据本身错了，重试没有意义）。
    """
    assert _DISPOSITIONS == {
        "infra": "retry_not_counted",
        "deliverable_defect": "retry_counted",
        "criterion_defect": "escalate_counted",
        "out_of_scope": "skip_not_counted",
    }


def test_infra_retry_budget_frozen():
    """infra 的跨轮重试上限是 2。

    ``retry_not_counted`` 说的是「环境坏了不算产物的账」，**不是**「无限
    重试」。上限是 2，也就是这个 VP 最多尝试 3 次（首试 + 2 次跨轮重试）。
    调大 → 永远重试；调小 → 明明能跑却提前停。两种都是悄悄的。
    """
    assert MAX_INFRA_CROSS_ROUND_RETRIES == 2


def test_out_of_scope_options_frozen():
    """out_of_scope 问的两个选项与默认值没变。

    两个选项就是人面对「这个检查没人要求过」能做的两件事：跳过这个检查，
    或者同意把它纳入范围。没有第三个 —— 范围就是范围。默认值是
    ``skip_the_check``：没有人回复时（``auto`` 模式），保守地**不动**
    计划，而不是替人同意多做事。默认值换了方向，``auto`` 模式就会替人
    做另一个决定。
    """
    assert OUT_OF_SCOPE_OPTIONS == ("skip_the_check", "bring_it_into_scope")
    assert OUT_OF_SCOPE_DEFAULT_OPTION == "skip_the_check"
    # 默认值必须是选项之一，否则「没人回复时按默认走」根本走不通。
    assert OUT_OF_SCOPE_DEFAULT_OPTION in OUT_OF_SCOPE_OPTIONS


def test_repair_id_allocation_frozen():
    """repair id 是**计划全局**分配的：轮次不参与。

    传进来的是 ``{"repair-29"}`` 与轮次 3，答案是 30：

    * 最高已用序号 29 → 下一个是 30；
    * 轮次 3 **被忽略** —— id 是计划全局的序号，不是「本轮的第几个」。
      曾经它是轮次内的序号，于是每轮都从 1 重新开始，第二批 repair 与
      第一批同名，upsert 直接覆盖了第一批的运行历史（2026-09-20、
      2026-11-02 两次事故）。

    顺带钉住「不引用旧号」：把 29 换成别的最高号，答案跟着走 —— 说明
    分配器读的是已有的 id，不是某个写死的起点。
    """
    assert next_repair_seq_base({"repair-29"}, 3) == 30
    # 轮次被忽略：换成别的轮次，答案不变。
    assert next_repair_seq_base({"repair-29"}, 1) == 30
    # 读的是已有的最高序号，不是写死的起点。
    assert next_repair_seq_base({"repair-07", "repair-29"}, 3) == 30
    # 空集合 → 从 1 开始。
    assert next_repair_seq_base(set(), 3) == 1
