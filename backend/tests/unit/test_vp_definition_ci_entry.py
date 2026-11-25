"""full_ci 的 ``ci_entry`` 与 ``expected_result`` 的**形式一致性**校验。

2026-11-09（plan ``20261109-verification-evidenc`` 的第 3 项任务）：

VP-025 的 ``ci_entry`` 写的是**绝对路径**的 ``scripts/ci_gha.sh``（GHA 派发器，
路径写死在那台机器上），``expected_result`` 点名的却是 ``grep_guard.sh`` 与
``run_tests.sh`` —— 判据入口跑的脚本和验收文本说的根本不是一回事。
``vp_definition_gaps()`` 以前只校验 ``ci_entry`` 是不是非空字符串，于是这种
**形式冲突**静默通过：定义被判"完整"，跑起来才发现入口对不上。

这里钉的是两条**机械可判**的规则（都不猜语义、不替操作者发明判据）：

* ``ci_entry`` 里出现的脚本必须是 **repo-relative**（不能是绝对路径）；
* ``expected_result`` 点名的脚本（``*.sh`` / ``*.py``）必须出现在 ``ci_entry``
  的脚本集合里。

``expected_result`` 没点名任何脚本时，只查 repo-relative —— 没有点名就没有
"漏掉"可言，硬凑一个缺口就是假话。
"""

from __future__ import annotations

import sys
from pathlib import Path


_BACKEND_DIR = Path(__file__).resolve().parent.parent.parent
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))


#: VP-022 冻结入口的**形状**（逐字取自
#: ``plans/<plan-id>/verification_plan.json``，见
#: ``tests/unit/test_ci_entry_plan_gate.py``）。
#:
#: 绝对路径换成了 ``/opt/shared/venv``：入口里那条路径**本机专属**，留在测试里
#: 反而会把一台机器的目录结构写进仓里。
#:
#: 它是本模块的**反向锚点**：这条入口里的绝对路径是 venv 目录，不是脚本 ——
#: 绝对路径检查只看 ``*.sh`` / ``*.py``，所以它必须继续通过。
VP022_FROZEN_ENTRY = (
    "(test -e backend/.venv || ln -sfn /opt/shared/venv backend/.venv) && "
    "bash scripts/run_tests.sh"
)


def _vp(**over):
    vp = {
        "id": "VP-025",
        "title": "整仓门禁",
        "verification_method": "full_ci",
        "ci_entry": "bash scripts/run_ci.sh",
        "expected_result": "run_ci.sh 全绿",
    }
    vp.update(over)
    return vp


def test_consistent_entry_has_no_gap() -> None:
    """入口跑的脚本就是验收文本点名的那个 —— 完整定义，没有 gap。

    这是这条判据的**正向锚点**：新加的两条规则不许把合法的判据也拒掉。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash scripts/run_ci.sh",
        expected_result="run_ci.sh 全绿",
    ))

    assert gaps == [], (
        f"an entry that runs the named script is a complete definition; got "
        f"{gaps!r}"
    )


def test_absolute_script_path_is_a_gap() -> None:
    """脚本写成绝对路径 —— gap。

    绝对路径是**那台机器**上的路径：换一个 checkout、换一个容器它就指向别的
    东西（或者什么都不指向）。整仓门禁的判据必须能在任何机器上跑同一件事。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash /abs/run_ci.sh",
        expected_result="run_ci.sh 全绿",
    ))

    assert any("repo-relative" in gap for gap in gaps), (
        f"an absolute script path must be reported as such; got {gaps!r}"
    )
    assert any("/abs/run_ci.sh" in gap for gap in gaps), (
        f"the gap must name the offending token; got {gaps!r}"
    )


def test_named_script_missing_from_entry_is_a_gap() -> None:
    """验收文本点名的脚本入口里没有 —— gap。

    VP-025 就是这么写歪的：入口跑的是 GHA 派发器，验收文本说的是
    ``grep_guard.sh`` / ``run_tests.sh``。两者对不上时，判据"跑绿了"证明的
    根本不是验收文本说的那件事。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash scripts/ci_gha.sh",
        expected_result="run_tests.sh 全绿",
    ))

    assert any("run_tests.sh" in gap for gap in gaps), (
        f"the gap must name the script the entry does not run; got {gaps!r}"
    )


def test_unnamed_scripts_only_need_repo_relative() -> None:
    """验收文本没点名任何脚本 —— 只查 repo-relative，没有别的缺口。

    没有点名就没有"漏掉"可言：硬凑一个缺口就是假话，而假话比没有话更贵
    （操作者会照着它改，改到天荒地老也过不去）。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash scripts/run_ci.sh",
        expected_result="退出码为 0",
    ))

    assert gaps == [], (
        f"an unnamed criterion only has to be repo-relative; got {gaps!r}"
    )


def test_every_uncovered_script_is_reported() -> None:
    """点名的脚本一个都没被入口覆盖 —— 每个漏掉的都要被点名。

    只报一个会让操作者以为改一个就够了，改完再被挡一次 —— 一轮只往前挪
    一步的循环就是这么来的。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps({
        "id": "VP-025",
        "title": "整仓门禁",
        "verification_method": "full_ci",
        "ci_entry": "bash /abs/scripts/ci_gha.sh",
        "expected_result": "grep_guard.sh + run_tests.sh 全绿",
    })

    assert any("repo-relative" in gap for gap in gaps), (
        f"the absolute script path must be reported; got {gaps!r}"
    )
    for name in ("grep_guard.sh", "run_tests.sh"):
        assert any(name in gap for gap in gaps), (
            f"the uncovered script {name} must be named; got {gaps!r}"
        )


def test_absolute_paths_that_are_not_scripts_are_not_flagged() -> None:
    """绝对路径**不是脚本**时不算缺口（VP-022 冻结入口必须继续通过）。

    入口里那条绝对路径是 venv 目录（``ln -sfn /opt/shared/venv
    backend/.venv``）—— 它是这台机器的环境，不是判据本身。只查 ``*.sh`` /
    ``*.py`` 才不会把这种环境声明误判成"入口写死了路径"。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry=VP022_FROZEN_ENTRY,
        expected_result="run_tests.sh 全绿",
    ))

    assert not any("repo-relative" in gap for gap in gaps), (
        f"a non-script absolute path is not an entry defect; got {gaps!r}"
    )
    assert not any("未包含" in gap for gap in gaps), (
        f"the frozen entry runs the named script; got {gaps!r}"
    )


def test_an_unusable_entry_keeps_only_the_original_gap() -> None:
    """入口缺失 / 空白 / 非字符串 —— 只保留原来那条缺口，不追加新的。

    "没给入口"和"入口写错了"是两件事：前者已经有一条缺口在说话，后者才轮到
    新规则。同一条判据缺两遍话，说的还是两件不同的事。
    """
    from verification_plan_delta import vp_definition_gaps

    for entry in (None, "", "   ", 123):
        gaps = vp_definition_gaps(_vp(
            ci_entry=entry,
            expected_result="run_tests.sh 全绿",
        ))

        assert any("ci_entry" in gap for gap in gaps), (
            f"ci_entry={entry!r} must keep the original gap; got {gaps!r}"
        )
        assert not any("repo-relative" in gap for gap in gaps), (
            f"ci_entry={entry!r} has no entry to judge; got {gaps!r}"
        )
        assert not any("未包含" in gap for gap in gaps), (
            f"ci_entry={entry!r} has no script set to compare against; got "
            f"{gaps!r}"
        )


def test_a_non_string_expected_result_is_treated_as_empty() -> None:
    """``expected_result`` 不是字符串 —— 按空串处理，只查 repo-relative。"""
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash scripts/run_ci.sh",
        expected_result=123,
    ))

    assert gaps == [], (
        f"a non-string criterion names no script; got {gaps!r}"
    )


def test_a_named_python_file_counts_as_a_named_script() -> None:
    """点名的 ``*.py`` 同样算数（哪怕它是一条测试文件路径）。

    这是**刻意的形式校验**：入口跑的脚本集合与验收文本点名的脚本集合必须
    对得上。至于那条 ``.py`` 是不是"应该"由门禁跑 —— 那是语义，不归这条
    判据说。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash scripts/run_ci.sh",
        expected_result="tests/unit/test_foo.py 全绿",
    ))

    assert any("test_foo.py" in gap for gap in gaps), (
        f"a named .py file must be reported when the entry does not run it; "
        f"got {gaps!r}"
    )


def test_a_var_assignment_token_is_read_as_its_script() -> None:
    """``VAR=脚本`` 这种写法 —— 脚本名在 ``=`` 后面，也要认得出来。

    只看 token 自身会把它整个漏掉（这样的 token 以 ``VAR=`` 开头，不是
    脚本）：入口明明写了某个脚本，检查却当它没写。
    """
    from verification_plan_delta import _absolute_script_tokens, _script_names

    assert _script_names("SCRIPT=scripts/run_ci.sh") == ["run_ci.sh"], (
        "the value of a VAR= token names the script; got "
        f"{_script_names('SCRIPT=scripts/run_ci.sh')!r}"
    )
    assert _absolute_script_tokens("SCRIPT=/abs/run_ci.sh") == ["/abs/run_ci.sh"], (
        "an absolute path behind VAR= is still an absolute path; got "
        f"{_absolute_script_tokens('SCRIPT=/abs/run_ci.sh')!r}"
    )


def test_an_unbalanced_quote_does_not_crash_the_check() -> None:
    """引号不配对 —— 退回空白切分，不抛。

    一条写歪的 ``ci_entry`` 不该把整条判据的检查带崩：检查崩了等于
    静默放过，而静默放过正是这条判据要杀的东西。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry='bash /abs/run_ci.sh "unclosed',
        expected_result="run_ci.sh 全绿",
    ))

    assert any("repo-relative" in gap for gap in gaps), (
        f"an unparsable entry must still be judged, not crash the check; got "
        f"{gaps!r}"
    )


# ---------------------------------------------------------------------------
# 中文标点与机器可读标识（2026-11-24，VP-005）
# ---------------------------------------------------------------------------
#
# 两条都是"机器可读"的问题：
#
# * 认脚本时拿**整个 token** 取 basename —— 中文标点粘在 token 上时脚本名变成
#   「run_ci.sh，全绿」，与验收文本里的「run_ci.sh」不相等：一条完全合法的
#   判据被误报成"入口没跑那个脚本"，在 ``apply_delta`` 里连号都拿不到。
# * 反过来，token 前面带中文标点时前缀匹配整个落空 —— 点名的脚本被静默漏认，
#   真缺口永远不报。漏报比误报更难发现：误报会被人看见，漏报只会让一条
#   "入口跑的不是验收说的那件事"的判据安静地通过。
#
# gap 只有中文散文时，消费方要靠匹配关键字区分 gap 种类 —— 文案一改，判据
# 就跟着失效。每条 gap 带自己的 code 之后，散文可以改，code 不动。


def test_a_chinese_punctuation_suffix_does_not_fake_a_missing_script() -> None:
    """脚本名后面粘着中文标点 —— 不是"入口没跑那个脚本"。

    真计划里的写法就是这种：入口与验收文本里都把脚本名和一句中文连在一起。
    以前拿**整个 token** 取 basename，得到的脚本名是「run_ci.sh，全绿」——
    与验收文本里的「run_ci.sh」不相等，一条完全合法的判据被报成"入口没跑
    那个脚本"，在 ``apply_delta`` 里连号都拿不到。

    两个方向都钉：标点在**入口**那一侧（脚本名被撑大），以及标点在**验收
    文本**那一侧（点名被撑大）—— 撑大的那一边以前都对不上。
    """
    from verification_plan_delta import vp_definition_gaps

    punctuated_entry = vp_definition_gaps(_vp(
        ci_entry="bash scripts/run_ci.sh，全绿",
        expected_result="run_ci.sh 全绿",
    ))
    punctuated_expected = vp_definition_gaps(_vp(
        ci_entry="bash scripts/run_ci.sh",
        expected_result="run_ci.sh，全绿",
    ))

    for gaps in (punctuated_entry, punctuated_expected):
        assert not any("未包含" in gap for gap in gaps), (
            f"the entry does run the named script; got {gaps!r}"
        )
        assert gaps == [], (
            f"a consistent criterion must not produce a gap; got {gaps!r}"
        )


def test_a_named_script_behind_a_chinese_prefix_is_still_recognised() -> None:
    """脚本名前面带中文标点 —— 缺口必须报出来。

    认脚本认的是 token **内部**的路径片段，不是整个 token：前缀匹配整个
    落空时，点名的脚本被静默漏认，缺口永远不报。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash scripts/ci_gha.sh",
        expected_result="跑一下 0：scripts/run_tests.sh",
    ))

    assert any("run_tests.sh" in gap for gap in gaps), (
        f"the named script must be recognised behind the punctuation; got "
        f"{gaps!r}"
    )


def test_every_gap_carries_a_stable_machine_readable_code() -> None:
    """每一条 gap 都以本模块定义的 code 开头。

    消费方靠 code 区分 gap 种类，不靠猜中文散文：散文一改，判据就跟着
    失效，而失效的方向通常是"悄悄不再报"。
    """
    from verification_plan_delta import (
        VP_DEFINITION_GAP_CODES,
        vp_definition_gaps,
    )

    cases = {
        "缺标题": {"title": "  ", "verification_method": "full_ci"},
        "方法不在词汇表": {
            "title": "整仓门禁", "verification_method": "retired_method",
        },
        "断言写法用不了": {
            "title": "整仓门禁", "verification_method": "api_test",
            "assertions": [{"json_path": "decision_id"}],
        },
        "缺入口": {"title": "整仓门禁", "verification_method": "full_ci"},
        "入口写死了绝对路径": {
            "title": "整仓门禁", "verification_method": "full_ci",
            "ci_entry": "bash /abs/run_ci.sh",
            "expected_result": "run_ci.sh 全绿",
        },
        "入口没跑点名的脚本": {
            "title": "整仓门禁", "verification_method": "full_ci",
            "ci_entry": "bash scripts/ci_gha.sh",
            "expected_result": "run_tests.sh 全绿",
        },
    }

    codes_by_case: dict = {}
    for label, case in cases.items():
        gaps = vp_definition_gaps(dict(case, id="VP-025"))
        assert gaps, f"the case {label!r} must produce a gap; got {gaps!r}"
        for gap in gaps:
            assert any(
                gap.startswith(f"{code}: ") for code in VP_DEFINITION_GAP_CODES
            ), (
                f"the gap must start with a known code followed by ': '; got "
                f"{gap!r}"
            )
        codes_by_case[label] = {gap.split(": ", 1)[0] for gap in gaps}

    absolute = codes_by_case["入口写死了绝对路径"]
    mismatch = codes_by_case["入口没跑点名的脚本"]
    assert absolute != mismatch, (
        "the two full_ci gaps must be told apart by their codes, not by "
        f"guessing from the prose; got {absolute!r} and {mismatch!r}"
    )


def test_an_absolute_script_path_behind_punctuation_is_named_by_its_fragment() -> None:
    """绝对路径后面粘着中文标点 —— 报出来的必须是**片段**，不是整 token。

    认脚本认的是 token **内部**的路径片段：``bash /abs/run_ci.sh，全绿`` 的
    片段是 ``/abs/run_ci.sh``，token 是整串。报整串的话，操作者照着它去改
    路径时找的是一个从来不存在的文件「/abs/run_ci.sh，全绿」—— 照着一条
    假路径改，改到天荒地老也过不去。

    干净 token（``bash /abs/run_ci.sh``）里片段与 token 恰好相同，所以"报
    片段还是报 token"在那种写法上分不出来 —— 只有粘了标点的写法分得出来。
    """
    from verification_plan_delta import vp_definition_gaps

    gaps = vp_definition_gaps(_vp(
        ci_entry="bash /abs/run_ci.sh，全绿",
        expected_result="run_ci.sh 全绿",
    ))

    absolute = [gap for gap in gaps if "repo-relative" in gap]
    assert absolute, (
        f"an absolute script path must be reported even behind punctuation; "
        f"got {gaps!r}"
    )
    assert any("/abs/run_ci.sh" in gap for gap in absolute), (
        f"the gap must name the offending fragment; got {absolute!r}"
    )
    assert not any("run_ci.sh，全绿" in gap for gap in absolute), (
        f"the gap must quote the fragment, not the whole token; got "
        f"{absolute!r}"
    )
    assert not any("未包含" in gap for gap in gaps), (
        f"the entry does run the named script; got {gaps!r}"
    )


def test_a_var_assignment_behind_punctuation_still_names_its_script() -> None:
    """``VAR=脚本`` 后面粘着中文标点 —— 脚本名照样认得出来。

    脚本名在 ``=`` 后面那一截里，而 ``=`` 与中文标点都不在脚本路径的正则
    字符类里：切出来的片段自然就是等号之后那截路径。认成整 token 的话，
    片段会连着标点一起被算进文件名 —— 判据又不成立了（见上一个用例）。

    绝对路径那一侧同样要成立：``VAR=/abs/x.sh`` 的片段是那条路径本身，
    不是那个赋值。
    """
    from verification_plan_delta import _absolute_script_tokens, _script_names

    assert _script_names("SCRIPT=scripts/run_ci.sh，全绿") == ["run_ci.sh"], (
        "the value of a VAR= token names the script; got "
        f"{_script_names('SCRIPT=scripts/run_ci.sh，全绿')!r}"
    )
    assert _absolute_script_tokens("SCRIPT=/abs/run_ci.sh，全绿") == [
        "/abs/run_ci.sh",
    ], (
        "an absolute path behind VAR= is still an absolute path; got "
        f"{_absolute_script_tokens('SCRIPT=/abs/run_ci.sh，全绿')!r}"
    )


def test_each_gap_kind_carries_exactly_its_own_code() -> None:
    """每一种 gap 带的是**它自己**那个 code，且 code 的字面值就是约定的那几个。

    六个 code 都存在，不等于它们用对了地方：认脚本那条若带的是绝对路径的
    code，消费方就会把"入口写死了路径"当成"入口没跑那个脚本" —— 两件不同
    的事被合成一件，排查时先跑错方向。

    字面值也一起钉住：消费方分辨 gap 种类靠的就是这几个字符串本身，code 改名
    等于消费方失聪（散文那一半可以改，code 那一半不能动）。

    一条判据可以同时缺几样东西（标题是空的、又没给入口），所以这里钉的是
    「这个 case 一定产出它自己那个 code」，不是「只产出这一个」。
    """
    from verification_plan_delta import (
        VP_DEFINITION_GAP_CODE_API_TEST_ASSERTION,
        VP_DEFINITION_GAP_CODE_CI_ENTRY_ABSOLUTE_PATH,
        VP_DEFINITION_GAP_CODE_CI_ENTRY_MISSING,
        VP_DEFINITION_GAP_CODE_CI_ENTRY_SCRIPT_MISMATCH,
        VP_DEFINITION_GAP_CODE_MISSING_TITLE,
        VP_DEFINITION_GAP_CODE_UNSUPPORTED_METHOD,
        vp_definition_gaps,
    )

    assert {
        VP_DEFINITION_GAP_CODE_MISSING_TITLE,
        VP_DEFINITION_GAP_CODE_UNSUPPORTED_METHOD,
        VP_DEFINITION_GAP_CODE_API_TEST_ASSERTION,
        VP_DEFINITION_GAP_CODE_CI_ENTRY_MISSING,
        VP_DEFINITION_GAP_CODE_CI_ENTRY_ABSOLUTE_PATH,
        VP_DEFINITION_GAP_CODE_CI_ENTRY_SCRIPT_MISMATCH,
    } == {
        "vp_definition_gap_code_missing_title",
        "vp_definition_gap_code_unsupported_method",
        "vp_definition_gap_code_api_test_assertion",
        "vp_definition_gap_code_ci_entry_missing",
        "vp_definition_gap_code_ci_entry_absolute_path",
        "vp_definition_gap_code_ci_entry_script_mismatch",
    }, (
        "the gap codes are the machine-readable contract; renaming one "
        "silences every consumer"
    )

    cases = {
        "缺标题": (
            {"title": "  ", "verification_method": "full_ci"},
            VP_DEFINITION_GAP_CODE_MISSING_TITLE,
        ),
        "方法不在词汇表": (
            {"title": "整仓门禁", "verification_method": "retired_method"},
            VP_DEFINITION_GAP_CODE_UNSUPPORTED_METHOD,
        ),
        "断言写法用不了": (
            {
                "title": "整仓门禁",
                "verification_method": "api_test",
                "assertions": [{"json_path": "decision_id"}],
            },
            VP_DEFINITION_GAP_CODE_API_TEST_ASSERTION,
        ),
        "缺入口": (
            {"title": "整仓门禁", "verification_method": "full_ci"},
            VP_DEFINITION_GAP_CODE_CI_ENTRY_MISSING,
        ),
        "入口写死了绝对路径": (
            {
                "title": "整仓门禁",
                "verification_method": "full_ci",
                "ci_entry": "bash /abs/run_ci.sh",
                "expected_result": "run_ci.sh 全绿",
            },
            VP_DEFINITION_GAP_CODE_CI_ENTRY_ABSOLUTE_PATH,
        ),
        "入口没跑点名的脚本": (
            {
                "title": "整仓门禁",
                "verification_method": "full_ci",
                "ci_entry": "bash scripts/ci_gha.sh",
                "expected_result": "run_tests.sh 全绿",
            },
            VP_DEFINITION_GAP_CODE_CI_ENTRY_SCRIPT_MISMATCH,
        ),
    }

    for label, (overrides, expected_code) in cases.items():
        gaps = vp_definition_gaps(dict(overrides, id="VP-025"))
        assert gaps, f"the case {label!r} must produce a gap; got {gaps!r}"
        codes = {gap.split(": ", 1)[0] for gap in gaps}
        assert expected_code in codes, (
            f"the case {label!r} must carry its own code {expected_code!r}; "
            f"got {codes!r}"
        )
        assert codes <= {
            VP_DEFINITION_GAP_CODE_MISSING_TITLE,
            VP_DEFINITION_GAP_CODE_UNSUPPORTED_METHOD,
            VP_DEFINITION_GAP_CODE_API_TEST_ASSERTION,
            VP_DEFINITION_GAP_CODE_CI_ENTRY_MISSING,
            VP_DEFINITION_GAP_CODE_CI_ENTRY_ABSOLUTE_PATH,
            VP_DEFINITION_GAP_CODE_CI_ENTRY_SCRIPT_MISMATCH,
        }, (
            f"the case {label!r} must not produce an unknown code; got "
            f"{codes!r}"
        )
