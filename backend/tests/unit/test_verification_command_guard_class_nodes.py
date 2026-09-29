"""护栏的存在性检查必须认得 **pytest 的类节点**（``file.py::TestFoo``）。

为什么单独一个文件：``test_verification_command_guard.py`` 锁的是"有界性"
这条规则（命令范围与断言是否相称），这里锁的是 V3 存在性检查里一个独立的
名字收集缺陷，两者改动的动因不同。

触发形状 —— 一条点名 pytest 类节点的 VP：

    pytest tests/example/test_example.py::TestExampleBoundary

``_TEST_IDENTIFIER_RE`` 明确把 ``Test\\w*`` 当成合法标识符——pytest 的类节点
就是这么点的。但收集"项目里定义了哪些测试"的 ``_NAME_DEF_RE`` 只匹配
``def`` / ``fn``，**不收 ``class``**，于是那个名字永远找不到：类确实存在于
测试文件里、junit 报告也确实跑过它，仍被判"匹配不到任何测试"。

代价不是"多打一条日志"：违规会进重生成的反馈循环，白烧 LLM 轮次，最后还在
计划上留一条假违规（``command_guard.violations``）——而这条护栏的立身之本
恰恰是"宁可漏报也不能误报"，因为喊狼来了会让 LLM 为了过检查空转而空转。
"""

from __future__ import annotations

from pathlib import Path

from verification_command_guard import find_violations


_CONTRACT = '''\
class TestAPositiveSchema:
    def test_a_envelope(self):
        assert True


class TestCTypedErrorBoundary:
    def test_c_unsupported_period_returns_400(self):
        assert True
'''


def _project(tmp_path: Path) -> Path:
    target = tmp_path / "tests" / "api_contracts"
    target.mkdir(parents=True)
    (target / "test_api_contract.py").write_text(
        _CONTRACT, encoding="utf-8",
    )
    return tmp_path


def test_pytest_class_node_is_not_reported_as_unknown(tmp_path: Path) -> None:
    """回归：点名一个**真实存在**的测试类，不得判 unknown_test_identifier。"""
    project = _project(tmp_path)
    cmd = (
        "cd <proj> && pytest "
        "tests/api_contracts/test_api_contract.py"
        "::TestCTypedErrorBoundary -v --no-cov"
    ).replace("<proj>", str(project))

    codes = [v.code for v in find_violations(cmd, project_dir=project)]
    assert "unknown_test_identifier" not in codes, codes


def test_pytest_method_node_inside_a_class_is_not_reported(tmp_path: Path) -> None:
    """``::TestFoo::test_bar`` 的尾段是方法名，同样要能匹配到。"""
    project = _project(tmp_path)
    cmd = (
        "cd <proj> && pytest "
        "tests/api_contracts/test_api_contract.py"
        "::TestCTypedErrorBoundary::test_c_unsupported_period_returns_400"
    ).replace("<proj>", str(project))

    codes = [v.code for v in find_violations(cmd, project_dir=project)]
    assert "unknown_test_identifier" not in codes, codes


def test_genuinely_missing_class_is_still_flagged(tmp_path: Path) -> None:
    """放宽不能放到失真：不存在的类节点仍必须被拦下。"""
    project = _project(tmp_path)
    cmd = (
        "cd <proj> && pytest "
        "tests/api_contracts/test_api_contract.py"
        "::TestNoSuchBoundary -v"
    ).replace("<proj>", str(project))

    codes = [v.code for v in find_violations(cmd, project_dir=project)]
    assert "unknown_test_identifier" in codes, codes
