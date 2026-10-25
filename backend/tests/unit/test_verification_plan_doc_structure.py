"""``verification-plan.md`` 的十章节结构校验与 ``baseline:`` 行解析。

背景
----
验证计划书既是人工审核的唯一对象，也是机器格式翻译的唯一来源。要让「审核过
的文档」与「实际被验证的代码」是同一条记录，计划书必须有一个**确定性**解析
器：不问模型、不碰 git、不看路径是否存在，只按固定语法判定「这份文档合法吗 /
基线是哪一行」。

判据
----
本文件钉住四件事：

* 一份 canonical 合法文档 → 零错误；
* ``baseline:`` 行的合法引用有 5 个等价类（分支 / tag / 完整 SHA / 短 SHA /
  ``refs/pull/<n>/head``），全部解析成功；
* ``baseline:`` 行有 10 类非法写法，每类必须被拒绝，且**错误码与行号**与
  预期一致（只知道「拒绝了」不知道「拒绝在哪」的校验器没有用）；
* 十个必需章节缺任何一个 → 校验失败。

外加两组边界：结构校验不执行任何子进程（不调 git），也不检查 ``repo`` 目录
是否存在 —— 这两件事属于「基线冻结」阶段，不属于「文档结构」阶段；``baseline:``
行还有归属与形态上的额外要求（必须写在「被验证对象」章节里、只出现一次、键值
对形式、``ref`` 不许像选项、``pr`` 必须是数字或 ``null``），验证点条目也有额外
要求（判据与证据缺一不可、清单里必须有验证点）。

测试数据
--------
canonical 文档与全部单点变异语料都放在本文件里：每条变异只改一处，其余字节
与 canonical 完全一致。文档一律先落盘再按字节读回，解析器拿到的是真实文件
字节，不是测试进程里拼出来的近似字符串。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import verification_plan_doc  # noqa: E402
from verification_plan_doc import (  # noqa: E402
    REQUIRED_SECTIONS,
    BaselineDoc,
    StructureError,
    parse_baseline,
    parse_plan_doc,
    validate_structure,
)

# ---------------------------------------------------------------------------
# 语料
# ---------------------------------------------------------------------------

#: 一份合法的计划书。十个章节按模板顺序排列；「被验证对象」章节含对象描述与
#: 唯一一行 ``baseline:``；「验证点清单」章节含两个 VP 块。
CANONICAL_MD = """\
# 验证计划书

本计划书是对被验证对象的人工审核对象，也是机器格式翻译的唯一来源。

## 被验证对象

被验证对象为 product-development-team 仓库 PR #7 的 head，含配额等待的重构。
baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7

## 验证范围

- 配额等待按真实重开时刻睡
- 双档配额耗尽时取最晚重开的窗口

## 排除范围

- 前端交互（本次未改动前端）
- 与配额无关的执行器行为

## 验证方法

- 以单元测试为主，必要时做一次手工核验

## 验证点清单

### VP-001 配额按真实重开时刻睡眠

- 范围：backend/verification_loop.py 的配额等待路径
- 验证方法：单元测试
- 通过判据：
  - C1: 配额耗尽时睡到真实重开时刻
  - C2: 双档耗尽时取最晚重开的窗口
- 证据要求：
  - E1: tests/unit/test_quota_wait.py 的 pytest 输出
- 预期结果：两条判据全部满足，无超时

### VP-002 报告把 skip 条目标注为未验证

- 范围：backend/verification_report.py
- 验证方法：单元测试
- 通过判据：
  - C1: skip 条目显示为「未验证」
- 证据要求：
  - E1: tests/unit/test_report_render.py 的 pytest 输出
- 预期结果：渲染结果与期望一致

## 预期结果

- 全部验证点通过，未发现需要修复的缺陷

## 证据要求

- 每个验证点给出可复跑的命令与原始输出

## 通过判据

- 所有验证点的 C 编号判据全部满足

## 环境与依赖

- Python 3.11 与 pytest 可用

## 风险面

- 上游 PR head 可能移动，导致基线漂移
"""

#: canonical 文档里的基线行原文（单点变异的锚点）。
BASELINE_LINE = "baseline: repo=/tmp/repo ref=refs/pull/7/head pr=7"

#: canonical 文档里「被验证对象」章节的对象描述行（去掉它，章节只剩基线行）。
OBJECT_DESCRIPTION_LINE = "被验证对象为 product-development-team 仓库 PR #7 的 head，含配额等待的重构。"

#: canonical 文档里基线行与章节标题的行号（1 起）。这两行被下面的
#: ``test_canonical_line_numbers_are_pinned`` 钉住，防止语料与断言漂移。
BASELINE_LINE_NO = 8
OBJECT_HEADING_NO = 5

#: 「以 ``~`` 开头的路径」是负例语料（基线行里的 ``repo`` 必须是绝对路径）。
#: 波浪号前缀在这里拼出来而不是写成字面量：这条语料不指向任何真实目录，
#: 仓库里也不该留下用户主目录形态的路径字面量。
TILDE_REPO_PATH = "~" + "/repo"


def _write_and_read(tmp_path: Path, md: str) -> str:
    """把文档落盘，再按字节读回来。

    解析器消费的是真实文件字节；测试进程里拼出来的字符串不是文档。
    """
    path = tmp_path / "verification-plan.md"
    path.write_bytes(md.encode("utf-8"))
    return path.read_bytes().decode("utf-8")


def _mutate(md: str, old: str, new: str) -> str:
    """单点变异：只改一处，其余字节原样保留。"""
    assert md.count(old) == 1, f"变异锚点必须唯一：{old!r}"
    return md.replace(old, new, 1)


def _remove_section(md: str, title: str) -> str:
    """删掉一个二级章节（标题 + 正文），其余内容原样保留。"""
    lines = md.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"## {title}")
    end = len(lines)
    for i in range(start + 1, len(lines)):
        if lines[i].startswith("## "):
            end = i
            break
    return "\n".join(lines[:start] + lines[end:])


# ---------------------------------------------------------------------------
# 1. canonical 文档 → 零错误
# ---------------------------------------------------------------------------


def test_canonical_doc_passes(tmp_path: Path) -> None:
    md = _write_and_read(tmp_path, CANONICAL_MD)

    assert validate_structure(md) == []

    parsed = parse_plan_doc(md)
    assert parsed.baseline == BaselineDoc(
        repo_dir=Path("/tmp/repo"),
        ref="refs/pull/7/head",
        pr_number=7,
    )
    assert [item.id for item in parsed.items] == ["VP-001", "VP-002"]
    assert list(parsed.sections) == list(REQUIRED_SECTIONS)


def test_canonical_line_numbers_are_pinned() -> None:
    """语料的行号是断言的一部分，漂了就先修这里。"""
    lines = CANONICAL_MD.splitlines()
    assert lines[BASELINE_LINE_NO - 1] == BASELINE_LINE
    assert lines[OBJECT_HEADING_NO - 1] == "## 被验证对象"


def test_sections_expose_heading_line_numbers(tmp_path: Path) -> None:
    md = _write_and_read(tmp_path, CANONICAL_MD)
    sections = parse_plan_doc(md).sections

    assert sections["被验证对象"].line == OBJECT_HEADING_NO
    assert sections["验证点清单"].line == 24
    assert "配额等待" in sections["验证范围"].body


def test_items_carry_scope_criteria_and_evidence(tmp_path: Path) -> None:
    md = _write_and_read(tmp_path, CANONICAL_MD)
    items = parse_plan_doc(md).items

    first = items[0]
    assert first.id == "VP-001"
    assert first.title == "配额按真实重开时刻睡眠"
    assert first.scope == "backend/verification_loop.py 的配额等待路径"
    assert first.method == "单元测试"
    assert first.criteria == {
        "C1": "配额耗尽时睡到真实重开时刻",
        "C2": "双档耗尽时取最晚重开的窗口",
    }
    assert first.evidence == {"E1": "tests/unit/test_quota_wait.py 的 pytest 输出"}
    assert first.expected_result == "两条判据全部满足，无超时"


# ---------------------------------------------------------------------------
# 2. baseline 行的合法等价类
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ref",
    [
        pytest.param("main", id="branch"),
        pytest.param("v1.2.3", id="tag"),
        pytest.param("0123456789abcdef0123456789abcdef01234567", id="full-sha"),
        pytest.param("0123456", id="short-sha"),
        pytest.param("refs/pull/7/head", id="pull-ref"),
    ],
)
def test_baseline_equivalence_classes(tmp_path: Path, ref: str) -> None:
    md = _mutate(CANONICAL_MD, "ref=refs/pull/7/head", f"ref={ref}")
    doc = _write_and_read(tmp_path, md)

    assert validate_structure(doc) == []

    parsed = parse_plan_doc(doc)
    assert parsed.baseline.ref == ref
    assert parsed.baseline.repo_dir == Path("/tmp/repo")
    assert parsed.baseline.pr_number == 7


def test_baseline_null_pr_means_no_pr(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, "pr=7", "pr=null")
    doc = _write_and_read(tmp_path, md)

    parsed = parse_plan_doc(doc)
    assert parsed.baseline.pr_number is None


# ---------------------------------------------------------------------------
# 3. baseline 行的 10 类非法写法
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "old,new,expected_code,expected_line",
    [
        pytest.param(
            "repo=/tmp/repo",
            "repo=./repo",
            "baseline_relative_path",
            BASELINE_LINE_NO,
            id="relative-path",
        ),
        pytest.param(
            "repo=/tmp/repo",
            f"repo={TILDE_REPO_PATH}",
            "baseline_home_path",
            BASELINE_LINE_NO,
            id="home-path",
        ),
        pytest.param(
            "repo=/tmp/repo",
            "repo=/tmp/my repo",
            "baseline_whitespace_in_value",
            BASELINE_LINE_NO,
            id="whitespace-in-value",
        ),
        pytest.param(
            "ref=refs/pull/7/head pr=7",
            "ref=refs/pull/7/head",
            "baseline_missing_key",
            BASELINE_LINE_NO,
            id="missing-key",
        ),
        pytest.param(
            "repo=/tmp/repo ref=",
            "repo=/tmp/repo repo=/tmp/repo ref=",
            "baseline_duplicate_key",
            BASELINE_LINE_NO,
            id="duplicate-key",
        ),
        pytest.param(
            " pr=7",
            " pr=7 foo=bar",
            "baseline_unknown_key",
            BASELINE_LINE_NO,
            id="unknown-key",
        ),
        pytest.param(
            "repo=/tmp/repo ref=refs/pull/7/head",
            "ref=refs/pull/7/head repo=/tmp/repo",
            "baseline_key_order",
            BASELINE_LINE_NO,
            id="key-order",
        ),
        pytest.param(
            "repo=/tmp/repo",
            "repo=",
            "baseline_empty_value",
            BASELINE_LINE_NO,
            id="empty-value",
        ),
        pytest.param(
            BASELINE_LINE,
            f"{BASELINE_LINE}\n{BASELINE_LINE}",
            "baseline_multiple_lines",
            BASELINE_LINE_NO + 1,
            id="multiple-lines",
        ),
        pytest.param(
            f"{OBJECT_DESCRIPTION_LINE}\n",
            "",
            "empty_section",
            OBJECT_HEADING_NO,
            id="only-baseline-line",
        ),
    ],
)
def test_baseline_rejects_invalid_forms(
    tmp_path: Path,
    old: str,
    new: str,
    expected_code: str,
    expected_line: int,
) -> None:
    md = _mutate(CANONICAL_MD, old, new)
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert errors, "非法写法必须被拒绝"
    assert errors[0].code == expected_code
    assert errors[0].section == "被验证对象"
    assert errors[0].line == expected_line

    with pytest.raises(StructureError) as excinfo:
        parse_plan_doc(doc)
    assert excinfo.value.code == expected_code


# ---------------------------------------------------------------------------
# 4. 缺章节 → 校验失败
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", list(REQUIRED_SECTIONS))
def test_missing_section_rejected(tmp_path: Path, title: str) -> None:
    md = _remove_section(CANONICAL_MD, title)
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert errors, f"缺少章节 {title!r} 必须被拒绝"
    assert any(
        error.code == "missing_section" and error.section == title
        for error in errors
    ), [error.code for error in errors]


def test_unexpected_section_rejected(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, "## 风险面", "## 附录\n\n补充说明\n\n## 风险面")
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["unexpected_section"]
    assert errors[0].section == "附录"


def test_duplicate_section_rejected(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, "## 风险面", "## 风险面\n\n- 重复的章节\n\n## 风险面")
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["duplicate_section"]
    assert errors[0].section == "风险面"


def test_sections_out_of_order_rejected(tmp_path: Path) -> None:
    swapped = CANONICAL_MD.replace(
        "## 验证范围\n\n- 配额等待按真实重开时刻睡\n- 双档配额耗尽时取最晚重开的窗口\n\n"
        "## 排除范围\n\n- 前端交互（本次未改动前端）\n- 与配额无关的执行器行为",
        "## 排除范围\n\n- 前端交互（本次未改动前端）\n- 与配额无关的执行器行为\n\n"
        "## 验证范围\n\n- 配额等待按真实重开时刻睡\n- 双档配额耗尽时取最晚重开的窗口",
    )
    assert swapped != CANONICAL_MD, "换序变异必须真的换掉了顺序"
    doc = _write_and_read(tmp_path, swapped)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["section_out_of_order"]
    assert errors[0].section == "排除范围"


# ---------------------------------------------------------------------------
# 5. 解析 / 动作分层：结构校验不碰 git，也不看路径是否存在
# ---------------------------------------------------------------------------


def test_structure_check_does_not_shell_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(*args, **kwargs):  # pragma: no cover - 只在被调用时触发
        raise AssertionError("结构校验不得执行任何子进程")

    monkeypatch.setattr(subprocess, "run", _boom)
    monkeypatch.setattr(subprocess, "check_output", _boom)
    monkeypatch.setattr(subprocess, "Popen", _boom)

    md = _mutate(CANONICAL_MD, "repo=/tmp/repo", "repo=/nonexistent/repo/does-not-exist")
    doc = _write_and_read(tmp_path, md)

    assert validate_structure(doc) == []
    parsed = parse_plan_doc(doc)
    assert parsed.baseline.repo_dir == Path("/nonexistent/repo/does-not-exist")


def test_module_does_not_import_subprocess() -> None:
    source = Path(verification_plan_doc.__file__).read_text(encoding="utf-8")
    assert "import subprocess" not in source


# ---------------------------------------------------------------------------
# 6. 条目级失败（重复 id、编号缺失、空判据）
# ---------------------------------------------------------------------------


def test_duplicate_vp_id_rejected(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, "### VP-002", "### VP-001")
    doc = _write_and_read(tmp_path, md)

    with pytest.raises(StructureError) as excinfo:
        parse_plan_doc(doc)
    assert excinfo.value.code == "duplicate_vp_id"


def test_criteria_numbering_gap_rejected(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, "  - C2: 双档耗尽时取最晚重开的窗口", "  - C3: 双档耗尽时取最晚重开的窗口")
    doc = _write_and_read(tmp_path, md)

    with pytest.raises(StructureError) as excinfo:
        parse_plan_doc(doc)
    assert excinfo.value.code == "criteria_numbering"


def test_empty_criteria_rejected(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, "  - C1: 配额耗尽时睡到真实重开时刻", "  - C1:")
    doc = _write_and_read(tmp_path, md)

    with pytest.raises(StructureError) as excinfo:
        parse_plan_doc(doc)
    assert excinfo.value.code == "empty_criteria"


def test_parse_baseline_accepts_a_single_line() -> None:
    baseline = parse_baseline(BASELINE_LINE, line_number=BASELINE_LINE_NO)

    assert baseline == BaselineDoc(
        repo_dir=Path("/tmp/repo"),
        ref="refs/pull/7/head",
        pr_number=7,
    )


def test_parse_baseline_rejects_a_bad_line() -> None:
    with pytest.raises(StructureError) as excinfo:
        parse_baseline("baseline: repo=./repo ref=main pr=1", line_number=3)
    assert excinfo.value.code == "baseline_relative_path"
    assert excinfo.value.line == 3


# ---------------------------------------------------------------------------
# 7. baseline 行的其余语法违规
# ---------------------------------------------------------------------------


def test_baseline_rejects_option_like_ref(tmp_path: Path) -> None:
    """``ref`` 以 ``-`` 开头会被 git 当成选项 —— 结构阶段就得拒绝。"""
    md = _mutate(CANONICAL_MD, "ref=refs/pull/7/head", "ref=-main")
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["baseline_invalid_ref"]
    assert errors[0].section == "被验证对象"
    assert errors[0].line == BASELINE_LINE_NO


def test_baseline_rejects_non_numeric_pr(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, "pr=7", "pr=abc")
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["baseline_invalid_pr"]
    assert errors[0].line == BASELINE_LINE_NO


def test_baseline_rejects_a_line_that_is_not_key_value(tmp_path: Path) -> None:
    """第一个词不是 ``键=值`` 时，这一行根本不是基线行。"""
    md = _mutate(
        CANONICAL_MD,
        BASELINE_LINE,
        "baseline: 不是键值对 repo=/tmp/repo ref=main pr=1",
    )
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["baseline_malformed"]
    assert errors[0].line == BASELINE_LINE_NO


def test_parse_baseline_without_a_baseline_line_is_rejected() -> None:
    with pytest.raises(StructureError) as excinfo:
        parse_baseline("这一段里没有基线行", line_number=5)
    assert excinfo.value.code == "baseline_missing_line"
    assert excinfo.value.line == 5


def test_parse_baseline_rejects_two_baseline_lines() -> None:
    with pytest.raises(StructureError) as excinfo:
        parse_baseline(
            f"{BASELINE_LINE}\n{BASELINE_LINE}",
            line_number=BASELINE_LINE_NO,
        )
    assert excinfo.value.code == "baseline_multiple_lines"
    assert excinfo.value.line == BASELINE_LINE_NO + 1


# ---------------------------------------------------------------------------
# 8. baseline 行的归属：必须写在「被验证对象」章节里
# ---------------------------------------------------------------------------


def test_baseline_in_another_section_is_rejected(tmp_path: Path) -> None:
    md = _mutate(CANONICAL_MD, f"{BASELINE_LINE}\n", "")
    md = _mutate(md, "## 验证范围\n", f"## 验证范围\n\n{BASELINE_LINE}\n")
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["baseline_wrong_section"]
    assert errors[0].section == "验证范围"
    assert errors[0].line == 11


def test_baseline_in_the_preamble_is_rejected(tmp_path: Path) -> None:
    """章节之前的内容是抬头，不属于任何章节 —— 基线行写在那里同样不行。"""
    md = _mutate(CANONICAL_MD, f"{BASELINE_LINE}\n", "")
    md = _mutate(md, "# 验证计划书\n", f"# 验证计划书\n\n{BASELINE_LINE}\n")
    doc = _write_and_read(tmp_path, md)

    errors = validate_structure(doc)
    assert [error.code for error in errors] == ["baseline_wrong_section"]
    assert errors[0].section == "被验证对象"
    assert errors[0].line == 3


# ---------------------------------------------------------------------------
# 9. 编号条目写在标签同一行的写法
# ---------------------------------------------------------------------------


def test_inline_criterion_and_evidence_entries_are_parsed(tmp_path: Path) -> None:
    md = _mutate(
        CANONICAL_MD,
        "- 通过判据：\n  - C1: 配额耗尽时睡到真实重开时刻\n  - C2: 双档耗尽时取最晚重开的窗口",
        "- 通过判据：C1: 配额耗尽时睡到真实重开时刻",
    )
    md = _mutate(
        md,
        "- 证据要求：\n  - E1: tests/unit/test_quota_wait.py 的 pytest 输出",
        "- 证据要求：E1: tests/unit/test_quota_wait.py 的 pytest 输出",
    )
    doc = _write_and_read(tmp_path, md)

    assert validate_structure(doc) == []

    parsed = parse_plan_doc(doc)
    assert parsed.items[0].criteria == {"C1": "配额耗尽时睡到真实重开时刻"}
    assert parsed.items[0].evidence == {"E1": "tests/unit/test_quota_wait.py 的 pytest 输出"}


# ---------------------------------------------------------------------------
# 10. 条目层面的其余违规
# ---------------------------------------------------------------------------


def test_free_text_line_inside_a_verification_point_is_ignored(tmp_path: Path) -> None:
    """VP 块里的自由文本行既不产生条目，也不算违规。"""
    md = _mutate(
        CANONICAL_MD,
        "- 验证方法：单元测试\n- 通过判据：\n  - C1: 配额耗尽时睡到真实重开时刻",
        "- 验证方法：单元测试\n- 备注：这一行是自由文本\n"
        "- 通过判据：\n  - C1: 配额耗尽时睡到真实重开时刻",
    )
    doc = _write_and_read(tmp_path, md)

    parsed = parse_plan_doc(doc)
    assert parsed.items[0].method == "单元测试"
    assert parsed.items[0].criteria == {
        "C1": "配额耗尽时睡到真实重开时刻",
        "C2": "双档耗尽时取最晚重开的窗口",
    }


def test_verification_point_without_criteria_is_rejected(tmp_path: Path) -> None:
    md = _mutate(
        CANONICAL_MD,
        "- 通过判据：\n  - C1: 配额耗尽时睡到真实重开时刻\n  - C2: 双档耗尽时取最晚重开的窗口",
        "- 通过判据：暂无",
    )
    doc = _write_and_read(tmp_path, md)

    with pytest.raises(StructureError) as excinfo:
        parse_plan_doc(doc)
    assert excinfo.value.code == "missing_criteria"


def test_verification_point_without_evidence_is_rejected(tmp_path: Path) -> None:
    md = _mutate(
        CANONICAL_MD,
        "- 证据要求：\n  - E1: tests/unit/test_quota_wait.py 的 pytest 输出",
        "- 证据要求：暂无",
    )
    doc = _write_and_read(tmp_path, md)

    with pytest.raises(StructureError) as excinfo:
        parse_plan_doc(doc)
    assert excinfo.value.code == "missing_evidence"


def test_list_section_without_verification_points_is_rejected(tmp_path: Path) -> None:
    """章节非空但没有 ``VP-`` 块 —— 清单里没有验证点。"""
    md = _mutate(CANONICAL_MD, "### VP-001", "#### 说明")
    md = _mutate(md, "### VP-002", "#### 补充")
    doc = _write_and_read(tmp_path, md)

    assert validate_structure(doc) == []

    with pytest.raises(StructureError) as excinfo:
        parse_plan_doc(doc)
    assert excinfo.value.code == "no_verification_points"
    assert excinfo.value.section == "验证点清单"
    assert excinfo.value.line == 24


# ---------------------------------------------------------------------------
# 11. 错误对象自身的可读形式
# ---------------------------------------------------------------------------


def test_structure_error_renders_code_section_and_line() -> None:
    error = StructureError("被验证对象", 8, "baseline_relative_path", "repo 必须是绝对路径")

    assert str(error) == "[baseline_relative_path] 被验证对象:8: repo 必须是绝对路径"


def test_document_level_structure_error_names_the_document() -> None:
    error = StructureError("", 0, "missing_section", "缺少必需章节")

    assert str(error) == "[missing_section] <document>:0: 缺少必需章节"
