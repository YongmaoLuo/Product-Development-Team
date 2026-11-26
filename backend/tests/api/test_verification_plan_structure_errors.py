"""结构非法的计划书要被读出来，不是被拒绝掉。

契约
----
架构决策点 6 / test 决策点 11：读一份不满足十章节契约的计划书，接口必须

* 返回 **200** —— 结构非法是文档的问题，不是接口的问题；
* **正文照原样给出** —— 不是空正文，也不是被改写过的正文；
* **structure_errors 逐条列出可见违规** —— 第一条是未知章节，并点明
  「十个固定章节」；缺哪一节就点哪一节的名字，一条违规一个章节。

VP-014 的 api_test 逐条评过这几件事，五条全部通过，执行器记的是 PASSED。
判定层曾把它改写成 FAILED —— 那一半在
``backend/tests/unit/test_api_test_verdict_authority.py``。PRD 验收④要这些
行为有自动化回归保护，所以这里把它们钉在 API 层上：一条断言一个用例。

模块名带着验收命令过滤的那个词，所以
``pytest backend/tests/api -k "structure_error or unexpected_section or
unknown_section"`` 选中的正好是下面这些用例。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from tests.unit.test_verification_plan_review import CANONICAL_MD  # noqa: E402
from verification_plan_doc import (  # noqa: E402
    REQUIRED_SECTIONS as VALIDATOR_SECTIONS,
)

client = TestClient(server.app)

#: 验证分支的产物：计划书（相对路径）。
PLAN_DOC = "verification-plan.md"

#: VP-014 读的那份文档：它根本不是一份计划书。
NOT_A_PLAN_MD = "## 不是十章节\n\n这段文字不是一份验证计划书。\n"

#: 十章节契约的十个固定章节 —— 这里写的是契约本身，不是从实现里抄来的，
#: 所以实现改了这份列表，用例会先红。
REQUIRED_SECTIONS = (
    "被验证对象",
    "验证范围",
    "排除范围",
    "验证方法",
    "验证点清单",
    "预期结果",
    "证据要求",
    "通过判据",
    "环境与依赖",
    "风险面",
)


# ---------------------------------------------------------------------------
# 计划目录
# ---------------------------------------------------------------------------


@pytest.fixture
def plan_dir() -> Path:
    """一个空计划目录（还没有计划书）。

    ``server.PLANS_DIR`` 由 conftest 的 autouse fixture 重定向到 tmp_path，
    所以这里建的目录不会落到仓库的计划目录树上。
    """
    path = server.PLANS_DIR / "vp-api-structure-errors"
    path.mkdir(parents=True)
    return path


def _write_doc(plan_dir: Path, text: str) -> None:
    """按字节落盘计划书 —— 读回来的必须是同一份字节。"""
    (plan_dir / PLAN_DOC).write_bytes(text.encode("utf-8"))


# ---------------------------------------------------------------------------
# 1. 结构非法不是接口错误：200，正文照原样，违规逐条给出
# ---------------------------------------------------------------------------


def test_the_document_endpoint_answers_200_for_a_malformed_document(
    plan_dir: Path,
) -> None:
    """第一件事：读一份结构非法的计划书，接口答 200，不是 500。"""
    _write_doc(plan_dir, NOT_A_PLAN_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    assert resp.status_code == 200, resp.text


def test_the_document_endpoint_returns_the_content_verbatim(
    plan_dir: Path,
) -> None:
    """第二件事：正文照原样给出 —— 不是空正文，也不是被改写过的正文。"""
    _write_doc(plan_dir, NOT_A_PLAN_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    body = resp.json()
    assert body["content"] == NOT_A_PLAN_MD, "返回的必须是磁盘上的原文"
    assert "不是十章节" in body["content"], body["content"]


def test_the_document_endpoint_lists_every_structure_error(
    plan_dir: Path,
) -> None:
    """第三件事：structure_errors 逐条列出可见违规。"""
    _write_doc(plan_dir, NOT_A_PLAN_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    errors = resp.json()["structure_errors"]
    assert errors, "结构违规必须逐条给出"
    for error in errors:
        assert set(error) == {"section", "line", "code", "message"}, error


def test_the_first_structure_error_is_the_unexpected_section(
    plan_dir: Path,
) -> None:
    """第四件事：第一条违规是未知章节，并点明「十个固定章节」。"""
    _write_doc(plan_dir, NOT_A_PLAN_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    errors = resp.json()["structure_errors"]
    first = errors[0]
    assert first["code"] == "unexpected_section", first
    assert "不是十章节" in first["message"], first
    assert "十个固定章节" in first["message"], first


def test_every_missing_section_is_named_separately(plan_dir: Path) -> None:
    """第五件事：缺哪一节就点哪一节的名字 —— 一条违规一个章节。"""
    _write_doc(plan_dir, NOT_A_PLAN_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    errors = resp.json()["structure_errors"]
    missing = {
        error["section"]
        for error in errors
        if error["code"] == "missing_section"
    }
    assert missing == set(REQUIRED_SECTIONS), missing


# ---------------------------------------------------------------------------
# 2. 负控：合法文档没有违规，上面那些断言才有判别力
# ---------------------------------------------------------------------------


def test_a_canonical_document_has_no_structure_errors(plan_dir: Path) -> None:
    """负控：canonical 文档 → 200、无违规、正文照原样。"""
    _write_doc(plan_dir, CANONICAL_MD)

    resp = client.get(f"/api/verification-plan/{plan_dir.name}")

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["structure_errors"] == [], body["structure_errors"]
    assert body["content"] == CANONICAL_MD, "返回的必须是磁盘上的原文"


def test_the_validator_knows_exactly_the_ten_fixed_sections() -> None:
    """十章节契约本身：校验器认的章节，就是契约写下的那十个。

    少一个、多一个、换个名字，都是契约被改过 —— 而 ``structure_errors``
    里的「十个固定章节」这句话就是照它说的。
    """
    assert tuple(VALIDATOR_SECTIONS) == REQUIRED_SECTIONS, VALIDATOR_SECTIONS
