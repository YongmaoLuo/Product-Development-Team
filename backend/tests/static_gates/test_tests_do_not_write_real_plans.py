"""新增的测试文件不得指向真实 plans 树 —— 目录即 fixture。

背景
----
仓库里的 plans 树是**运行时的活状态**：服务端把每个 plan 的产物写在那里，
启动时的清扫会读它来回答"哪些 plan 还在跑"。所以落在那里的目录不是垃圾 ——
它是一个声明，对每一个读这棵树的人说"有一条 plan 存在，可能正在跑"。

2026-09-24 就有八个这样的声明出现在某个开发者的树里：``resume-test-`` 开头的
八个目录，每个都躺着一份 ``current_phase: "executing"`` 的状态文件。写它们的
测试辅助把自己的 plans 根从 ``__file__`` 推出来，而不是走
``config_paths.resolve_plans_dir()``，并且从不清理。两个 pytest 跑次产出八个
目录（四个用例各两个），那份清单随后把"有几条 plan 在跑"答成了八。

本仓库已经有防线（``PDT_PLANS_DIR`` 重定向整棵树，``resolve_plans_dir()`` 是
唯一的解析口），缺的是**一个盯着这棵树的检查**：helper 换一种方式解析根目录
就能写进去，而没有一条用例会转红。

这个门禁盯的是另一头：**新增的测试文件**。手写 plan 目录的用例最容易图省事
直接写真实的 plans 树 —— 写进去的那一刻，它就在污染别人的运行环境。

规则
----
两条规则，都是**语法**规则（不是文本规则）：

* **规则一（plans-tree path expression）**：一条拼路径的表达式点到了这棵树
  —— 路径的一段是 ``plans``，或者路径的根就叫 ``plans``。
* **规则二（plans-directory assignment）**：一个**名字叫这棵树**的变量
  （``plans_dir`` / ``PLANS_DIR`` / ``plans_root`` …）被赋了一个路径。

两条规则问的都是同一个问题：**这条路径的根是谁？** 根是已知的、指向临时
目录或者由调用方注入的，路径就是安全的；根是说不清来路的，路径就可能落在
运维人员那棵真树上。于是规则带一份**证明清单**：

* **临时根** —— 名字里带 ``tmp`` / ``temp``，或者字面量在 ``/tmp`` 底下；
* **注入根** —— 根是所在函数的参数，谁调用谁决定它落在哪里；
* **fixture 根** —— 路径落在仓库的 fixture 目录下；
* **生产根** —— 根是 ``PLANS_DIR`` / ``resolve_plans_dir()``：那是服务端自己
  的那棵树，测试套件把它重定向走（``PDT_PLANS_DIR`` / monkeypatch），运行时
  那一半门禁盯着它有没有被改。

证明不了的根就是可疑的根。**断言不算构造**：``assert default_plans_root() ==
module_path.parents[1] / "plans"`` 是在核对生产布局，不是在往里写。

基线
----
规则写下的时候，仓库里已经有 142 个测试文件带着这些东西（绝大多数是文档
字符串里的一句话，不是路径）。它们被**逐个写死**在 :data:`PRE_EXISTING_OFFENDERS`
里 —— 基线之外的任何文件带这些东西，门禁转红。

这个门禁看不到什么
------------------
它看的是**源码**，不是运行时行为：一个用 ``resolve_plans_dir()`` 之外的路径、
且靠变量拼出来的目录，语法上未必带这两个字面量。运行时那一半由
``tests/static_gates/test_plan_fixtures_stay_out_of_the_operator_plans_tree.py``
守着（它比对整棵树在会话前后有没有变）。两个门禁看的是两件事，合起来才是
"绝不触碰真实 plans 树"。

新用例该怎么做
--------------
要一个 plan 目录，用 ``tests/fixtures/plan_artifacts_fixture.py`` 的
:func:`plan_dir` —— 它只往 ``tmp_path`` 里写，目录名由 ``plan_id_for`` 派生
（与 worker 无关），产物字节取 canonical 计划书。数据库行由既有的
``plan_sqlite_seeder`` 注入，构造器自己不碰数据库。
"""

from __future__ import annotations

import ast
import re
import warnings
from pathlib import Path

import source_scan
from test_no_plan_ids_in_non_test_source import is_test_source

#: Repository root, taken from the walker rather than recomputed — the two have
#: to agree, and this file sits beside ``source_scan.py``.
REPO_ROOT: Path = source_scan.REPO_ROOT

#: The path segment that names the operator's plans tree.
PLANS_SEGMENT: str = "plans"

#: The plan-state fixture this change adds — the one supported way to build a
#: plan directory in a test.
PLAN_STATE_FIXTURE: Path = Path("backend/tests/fixtures/plan_artifacts_fixture.py")

#: The names that hold the **tree** (its root), as opposed to one plan's own
#: directory (``plan_dir`` — that one is a plan, not the tree).
#:
#: The rule looks at the name because a helper that keeps the tree in a
#: variable named after it is pointing at it, whatever the value happens to be.
TREE_NAME_PATTERN: re.Pattern[str] = re.compile(
    r"^(?:real_|operator_)?plans(?:_(?:dir|dirs|root|path|tree))?$", re.IGNORECASE
)

#: The production symbols that name the plans root.  They point at the
#: server's own tree, which the test suite redirects (``PDT_PLANS_DIR``,
#: ``monkeypatch.setattr("server.PLANS_DIR", ...)``); the runtime gate checks
#: that tree did not change.
PRODUCTION_ROOT_NAMES: frozenset[str] = frozenset({"PLANS_DIR", "PLANS_ROOT"})

#: The production resolver — the one supported way to find the tree.
PRODUCTION_RESOLVERS: frozenset[str] = frozenset({"resolve_plans_dir"})

#: The environment variables that carry the **redirected** plans root.  The
#: suite points them at a session temporary directory, so a root read out of
#: one of them is the redirected tree rather than the operator's.
PRODUCTION_REDIRECT_ENV_VARS: frozenset[str] = frozenset({"PDT_PLANS_DIR"})

#: The callables that build a path out of pieces.  A "path expression" is one
#: of these calls, or a division by such a path.
PATH_BUILDERS: frozenset[str] = frozenset(
    {
        "Path",
        "PurePath",
        "PurePosixPath",
        "PureWindowsPath",
        "pathlib.Path",
        "pathlib.PurePath",
        "pathlib.PurePosixPath",
        "pathlib.PureWindowsPath",
        "os.path.join",
        "posixpath.join",
        "ntpath.join",
    }
)

#: The name fragments that mark a throwaway root.
TEMPORARY_MARKERS: tuple[str, ...] = ("tmp", "temp")

#: The directory name that marks a fixture root.
FIXTURE_SEGMENT: str = "fixtures"

#: The methods that **write**.  A path that is only read (or only compared)
#: does not put anything into the tree.
WRITE_METHODS: frozenset[str] = frozenset(
    {
        "mkdir",
        "makedirs",
        "write_text",
        "write_bytes",
        "unlink",
        "rmtree",
        "touch",
        "rename",
        "replace",
        "symlink_to",
        "hardlink_to",
    }
)


def _dotted_name(node: ast.AST) -> str:
    """The dotted name of a call target: ``os.path.join`` -> ``os.path.join``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    return ""


def _path_segments(text: str) -> list[str]:
    """The path segments of a string: ``plans/p1/x.json`` -> ``[plans, p1, x.json]``."""
    return [part for part in text.replace("\\", "/").split("/") if part]


def _subscript_key(node: ast.Subscript) -> str | None:
    """The constant key of a subscript, or ``None`` when it has none."""
    key = node.slice
    if isinstance(key, ast.Constant) and isinstance(key.value, str):
        return key.value
    return None


def _parameter_names(args: ast.arguments) -> set[str]:
    """The names a function's parameters bind."""
    names = {
        arg.arg
        for arg in (
            list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs)
        )
    }
    if args.vararg:
        names.add(args.vararg.arg)
    if args.kwarg:
        names.add(args.kwarg.arg)
    return names


def _assigned_name(target: ast.AST) -> str | None:
    """The name an assignment target binds, or ``None`` when it is not a name."""
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _string_is_a_throwaway(text: str) -> bool:
    """Is this literal path under a temporary directory or a fixture root?"""
    value = text.replace("\\", "/")
    if value == "/tmp" or value.startswith("/tmp/") or "/tmp/" in value:
        return True
    return FIXTURE_SEGMENT in _path_segments(value)


class _Scope:
    """One lexical scope: its parameters and the names bound inside it."""

    def __init__(self, parameters: frozenset[str]) -> None:
        self.parameters = parameters
        self.bindings: dict[str, list[ast.AST | None]] = {}

    def bind(self, name: str, value: ast.AST | None) -> None:
        self.bindings.setdefault(name, []).append(value)


def _bind_target(target: ast.AST, value: ast.AST | None, scope: _Scope) -> None:
    """Bind the names an assignment target introduces."""
    if isinstance(target, ast.Name):
        scope.bind(target.id, value)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for element in target.elts:
            _bind_target(element, value, scope)
    elif isinstance(target, ast.Attribute):
        scope.bind(target.attr, value)


def _collect_bindings(statement: ast.AST, scope: _Scope) -> None:
    """Record the names a statement binds, without descending into new scopes."""
    if isinstance(
        statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
    ):
        return
    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            _bind_target(target, statement.value, scope)
    elif isinstance(statement, ast.AnnAssign):
        _bind_target(statement.target, statement.value, scope)
    elif isinstance(statement, ast.AugAssign):
        _bind_target(statement.target, statement.value, scope)
    elif isinstance(statement, ast.NamedExpr):
        _bind_target(statement.target, statement.value, scope)
    elif isinstance(statement, (ast.For, ast.AsyncFor)):
        _bind_target(statement.target, statement.iter, scope)
        for inner in statement.body + statement.orelse:
            _collect_bindings(inner, scope)
    elif isinstance(statement, (ast.While, ast.If)):
        for inner in statement.body + statement.orelse:
            _collect_bindings(inner, scope)
    elif isinstance(statement, (ast.With, ast.AsyncWith)):
        for item in statement.items:
            if item.optional_vars is not None:
                _bind_target(item.optional_vars, item.context_expr, scope)
        for inner in statement.body:
            _collect_bindings(inner, scope)
    elif isinstance(statement, ast.Try):
        for inner in statement.body + statement.orelse + statement.finalbody:
            _collect_bindings(inner, scope)
        for handler in statement.handlers:
            if handler.name:
                scope.bind(handler.name, None)
            for inner in handler.body:
                _collect_bindings(inner, scope)
    elif isinstance(statement, ast.Match):
        for case in statement.cases:
            for inner in case.body:
                _collect_bindings(inner, scope)


def _collect_scopes(tree: ast.Module) -> tuple[_Scope, dict[int, _Scope]]:
    """Build the scope tree: the module scope plus one per nested scope."""
    module = _Scope(frozenset())
    nested: dict[int, _Scope] = {}

    def walk(body: list[ast.stmt], scope: _Scope) -> None:
        for statement in body:
            _collect_bindings(statement, scope)
            _walk_statement(statement, scope)

    def _walk_statement(statement: ast.AST, scope: _Scope) -> None:
        """Give a nested scope its own parameters and bindings."""
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            child_scope = _Scope(frozenset(_parameter_names(statement.args)))
            nested[id(statement)] = child_scope
            walk(statement.body, child_scope)
            return
        if isinstance(statement, ast.ClassDef):
            class_scope = _Scope(frozenset())
            nested[id(statement)] = class_scope
            walk(statement.body, class_scope)
            return
        if isinstance(statement, ast.Lambda):
            nested[id(statement)] = _Scope(frozenset(_parameter_names(statement.args)))
            return
        for child in ast.iter_child_nodes(statement):
            if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)
            ):
                _walk_statement(child, scope)

    walk(tree.body, module)
    return module, nested


class _PlansTreeDetector(ast.NodeVisitor):
    """Find the ways a module can point at the operator's plans tree.

    The detector answers one question twice: **where does this path start?**
    A path that starts at a temporary directory, at an injected parameter, at
    a fixture directory or at the server's own (redirected) plans root is
    harmless.  A path that starts somewhere nobody can account for is the
    accident this gate exists to catch.
    """

    def __init__(
        self,
        parents: dict[ast.AST, ast.AST],
        nested: dict[int, _Scope],
        module_scope: _Scope,
    ) -> None:
        self.hits: list[tuple[int, str]] = []
        self._parents = parents
        self._nested = nested
        self._scopes: list[_Scope] = [module_scope]

    # -- scopes ---------------------------------------------------------
    def _all_parameters(self) -> set[str]:
        """The parameters of every enclosing scope."""
        merged: set[str] = set()
        for scope in self._scopes:
            merged |= scope.parameters
        return merged

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_in_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_in_scope(node)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self._visit_in_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._visit_in_scope(node)

    def _visit_in_scope(self, node: ast.AST) -> None:
        """Walk a nested scope with its own parameters and bindings."""
        scope = self._nested.get(id(node))
        if scope is None:
            self.generic_visit(node)
            return
        self._scopes.append(scope)
        self.generic_visit(node)
        self._scopes.pop()

    # -- rules ----------------------------------------------------------
    def visit_BinOp(self, node: ast.BinOp) -> None:
        if isinstance(node.op, ast.Div):
            parent = self._parents.get(node)
            if not (isinstance(parent, ast.BinOp) and parent.left is node):
                self._check_path_expression(node)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _dotted_name(node.func) in PATH_BUILDERS:
            self._check_path_expression(node)
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        for target in node.targets:
            self._check_assignment(target, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if node.value is not None:
            self._check_assignment(node.target, node.value)
        self.generic_visit(node)

    def _check_path_expression(self, node: ast.AST) -> None:
        """Rule one: a path expression that names the tree and proves nothing."""
        if not self._names_the_tree(node):
            return
        if self._is_only_compared(node):
            return
        if self._root_is_provably_safe(node):
            return
        self.hits.append((node.lineno, "plans-tree path expression"))

    def _check_assignment(self, target: ast.AST, value: ast.AST) -> None:
        """Rule two: a tree-named variable holding a path nobody can place."""
        name = _assigned_name(target)
        if name is None or not TREE_NAME_PATTERN.match(name):
            return
        if not _is_path_expression(value):
            return
        if self._root_is_provably_safe(value):
            return
        self.hits.append((value.lineno, "plans-directory assignment"))

    # -- the tree -------------------------------------------------------
    def _names_the_tree(self, node: ast.AST) -> bool:
        """Does this expression name the operator's plans tree?

        The tree is named by a **segment**: a string that has ``plans`` as one
        step of its path, or a name that is called ``plans``.
        """
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if PLANS_SEGMENT in _path_segments(sub.value):
                    return True
            elif isinstance(sub, ast.Name) and sub.id.lower() == PLANS_SEGMENT:
                return True
            elif isinstance(sub, ast.Attribute) and sub.attr.lower() == PLANS_SEGMENT:
                return True
        return False

    # -- the proofs -----------------------------------------------------
    def _root_is_provably_safe(self, node: ast.AST) -> bool:
        """Is the root of this path accounted for?"""
        if self._passes_through_the_fixtures(node):
            return True
        return self._anchor_is_safe(_anchor(node), frozenset())

    def _passes_through_the_fixtures(self, node: ast.AST) -> bool:
        """Does this path run through the repository's fixture directory?

        A path that steps into ``fixtures`` lands in a directory the
        repository ships for tests to copy from.  That is a fixture root
        whatever the path started at.
        """
        for sub in ast.walk(node):
            if isinstance(sub, ast.Constant) and isinstance(sub.value, str):
                if FIXTURE_SEGMENT in _path_segments(sub.value):
                    return True
        return False

    def _anchor_is_safe(self, anchor: ast.AST, seen: frozenset[str]) -> bool:
        """Is this anchor provably not the operator's tree?"""
        if isinstance(anchor, ast.Name):
            return self._name_is_safe(anchor.id, seen)
        if isinstance(anchor, ast.Attribute):
            if anchor.attr in PRODUCTION_ROOT_NAMES:
                return True
            if anchor.attr == "__file__":
                # A module's own file: the checkout, never a throwaway tree.
                return False
            return self._anchor_is_safe(_anchor(anchor.value), seen)
        if isinstance(anchor, ast.Call):
            dotted = _dotted_name(anchor.func)
            if dotted.split(".")[-1] in PRODUCTION_RESOLVERS:
                return True
            if any(marker in dotted.lower() for marker in TEMPORARY_MARKERS):
                return True
            if dotted in PATH_BUILDERS:
                if anchor.args:
                    return self._anchor_is_safe(_anchor(anchor.args[0]), seen)
                return False
            if isinstance(anchor.func, ast.Attribute):
                # A method call: the receiver is what the path starts at.
                return self._anchor_is_safe(_anchor(anchor.func.value), seen)
            return False
        if isinstance(anchor, ast.Subscript):
            if _subscript_key(anchor) in PRODUCTION_REDIRECT_ENV_VARS:
                # A root read out of the redirection variable is the
                # redirected tree: the suite points that variable at a
                # session temporary directory.
                return True
            return self._anchor_is_safe(_anchor(anchor.value), seen)
        if isinstance(anchor, ast.BinOp):
            return self._anchor_is_safe(_anchor(anchor.left), seen)
        if isinstance(anchor, ast.Constant) and isinstance(anchor.value, str):
            return _string_is_a_throwaway(anchor.value)
        return False

    def _name_is_safe(self, name: str, seen: frozenset[str]) -> bool:
        """Is this name bound to something that is provably not the tree?"""
        if name in self._all_parameters():
            # Injected: the caller decides where the tree is.
            return True
        lowered = name.lower()
        if any(marker in lowered for marker in TEMPORARY_MARKERS):
            # A temporary root, by name.
            return True
        if "hermetic" in lowered:
            # The hermetic environment's own directories.
            return True
        if name in PRODUCTION_ROOT_NAMES:
            return True
        if name in seen:
            return False
        for scope in reversed(self._scopes):
            if name in scope.bindings:
                values = scope.bindings[name]
                return any(
                    value is not None
                    and self._value_is_safe(value, seen | frozenset({name}))
                    for value in values
                )
        # Unbound: nobody can say where it points.
        return False

    def _value_is_safe(self, value: ast.AST, seen: frozenset[str]) -> bool:
        """Is this value provably not the operator's tree?"""
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            return _string_is_a_throwaway(value.value)
        if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.Tuple)):
            # Data, not a path.
            return True
        if isinstance(value, ast.IfExp):
            return self._value_is_safe(value.body, seen) or self._value_is_safe(
                value.orelse, seen
            )
        return self._anchor_is_safe(_anchor(value), seen)

    # -- context --------------------------------------------------------
    def _is_only_compared(self, node: ast.AST) -> bool:
        """Is this expression merely compared, rather than used to build a path?

        An assertion about where the tree lives is a check on the production
        layout.  Writing into it is not.
        """
        current: ast.AST | None = node
        while current is not None:
            parent = self._parents.get(current)
            if parent is None:
                return False
            if isinstance(parent, ast.Assert):
                return not self._is_write_context(node)
            if isinstance(parent, ast.Attribute) and parent.value is current:
                grand = self._parents.get(parent)
                if (
                    isinstance(grand, ast.Call)
                    and grand.func is parent
                    and parent.attr in WRITE_METHODS
                ):
                    return False
            current = parent
        return False

    def _is_write_context(self, node: ast.AST) -> bool:
        """Is this expression the target of a write?"""
        current: ast.AST | None = node
        while current is not None:
            parent = self._parents.get(current)
            if parent is None:
                return False
            if isinstance(parent, ast.Attribute) and parent.value is current:
                grand = self._parents.get(parent)
                if (
                    isinstance(grand, ast.Call)
                    and grand.func is parent
                    and parent.attr in WRITE_METHODS
                ):
                    return True
            current = parent
        return False


def _anchor(node: ast.AST) -> ast.AST:
    """The node a path expression starts at.

    ``base / "plans" / plan_id`` starts at ``base``; ``Path(base) / "plans"``
    starts at ``base`` as well.  Everything after the start is decoration.

    Three shapes are **not** peeled away, because they are where the proof
    lives: a member access to the production root (``server.PLANS_DIR``), a
    call to the production resolver (``server.resolve_plans_dir()``) and a
    subscript with a constant key (``child_env["PDT_PLANS_DIR"]``).  Peeling
    them reduced them to the module they hang off — a bare name that nobody
    can place — and the exemptions :meth:`_PlansTreeDetector._anchor_is_safe`
    writes down for exactly those shapes could then never fire.  The rule
    said it understood the production root; it did not, because the proof was
    thrown away one step before the proof was read.
    """
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
        return _anchor(node.left)
    if isinstance(node, ast.Attribute):
        if node.attr in PRODUCTION_ROOT_NAMES:
            return node
        return _anchor(node.value)
    if isinstance(node, ast.Subscript):
        if _subscript_key(node) is not None:
            # The key names the container; keep it visible.
            return node
        return _anchor(node.value)
    if isinstance(node, ast.Starred):
        return _anchor(node.value)
    if isinstance(node, ast.Call):
        if _dotted_name(node.func) in PATH_BUILDERS and node.args:
            return _anchor(node.args[0])
        if isinstance(node.func, ast.Attribute):
            if node.func.attr in PRODUCTION_RESOLVERS:
                return node
            return _anchor(node.func.value)
        return node
    return node


def _is_path_expression(value: ast.AST) -> bool:
    """Is this value a path somebody built, rather than data?

    Only a division or a path constructor counts: those are the shapes a
    helper *builds* a directory out of.  A name, an attribute or a subscript
    is as often data as it is a path (``report["plans"]`` is a list of
    results), and rule two is about directories that were put together.
    """
    if isinstance(value, ast.BinOp) and isinstance(value.op, ast.Div):
        return True
    if isinstance(value, ast.Call):
        return _dotted_name(value.func) in PATH_BUILDERS
    return False


#: The files that already carried one of the two forms when this gate was
#: written.
#:
#: Written down one path per line rather than derived from git: the gate runs
#: in a CI job whose checkout is shallow, so "what was there before" is not
#: answerable there, and a gate that cannot answer it would have to skip —
#: and a gate that skips is not a gate.  Writing the list down makes the
#: baseline reviewable in a diff, which is the point of a baseline.
#:
#: The list only ever grows, and only deliberately: a new test file that
#: mentions the plans tree in prose has to be added here, which means somebody
#: reads it and says "yes, this one is a mention, not a path".  That is the
#: review the gate is for.
#:
#: The three entries added with the VP-033 repair (2026-11-13) are the same
#: kind of entry as the rest: they are **main-existing** offenders — the
#: files named the plans tree before this branch, and none of them is
#: touched by it — so the branch only writes down what was already there.
#: The baseline grows; the rule itself does not get weaker.
#:
#: ``backend/tests/integration/test_service_spawn_repro.py`` is the one
#: entry that is a **true positive** rather than a rule defect: it derives a
#: deployment-shaped plans root from ``__file__`` (``parents[3] / "plans"``),
#: which is exactly the shape the 2026-09-24 accident had.  Its one use
#: hands that constant to a reaper whose calls are mocked out — the value is
#: never read and never written — but the constant itself is the dangerous
#: shape, so the file is written down rather than rewritten.  It is a
#: main-existing file and this branch does not touch it.
PRE_EXISTING_OFFENDERS: frozenset[str] = frozenset(
    """
backend/state_machine/tests/integration/test_execution_routes.py
backend/state_machine/tests/integration/test_storage_e2e.py
backend/state_machine/tests/integration/test_subagent_verdict.py
backend/state_machine/tests/integration/test_verification_routes.py
backend/state_machine/tests/unit/test_artifact_repository.py
backend/test_command_rules.py
backend/tests/api/plan_scheduler/test_import_priority.py
backend/tests/api/test_execution_state_persistence.py
backend/tests/api/test_hitl_security.py
backend/tests/api/test_plan_import_api.py
backend/tests/api/test_plan_import_failed_phase.py
backend/tests/api/test_plan_import_queued.py
backend/tests/api/test_plan_lifecycle_endpoints.py
backend/tests/api/test_plan_revise_api.py
backend/tests/api/test_start_endpoint.py
backend/tests/api/test_verification_plan_refine.py
backend/tests/api/test_verification_plan_review_actions.py
backend/tests/api/test_verification_plan_review_routes.py
backend/tests/conftest.py
backend/tests/conftest_verification.py
backend/tests/e2e/plan_scheduler/test_failed_plan_actions_api_e2e.py
backend/tests/e2e/test_capacity_wait_plan_state_end_to_end.py
backend/tests/e2e/test_dashboard_plan_and_verification_e2e.py
backend/tests/e2e/test_execution_status_restart_e2e.py
backend/tests/e2e/test_fault_injection.py
backend/tests/e2e/test_instance_identity_baseline_e2e.py
backend/tests/e2e/test_instance_project_dir_ui_e2e.py
backend/tests/e2e/test_pause_resume_end_to_end.py
backend/tests/e2e/test_real_20260805_plan_migration.py
backend/tests/e2e/test_security_fixes_do_not_break_plan_lifecycle.py
backend/tests/e2e/test_service_relocation_browser.py
backend/tests/e2e/test_shutdown_checkpoint_e2e.py
backend/tests/e2e/test_stop_resume_ui_e2e.py
backend/tests/e2e/test_token_convergence_ui_acceptance.py
backend/tests/e2e/test_verification_death_e2e.py
backend/tests/e2e/test_verification_e2e_dryrun.py
backend/tests/e2e/verification/test_deterministic_plan_flow.py
backend/tests/e2e/verification/test_effect_report_contract.py
backend/tests/e2e/verification/test_real_model_attribution.py
backend/tests/e2e/verification/test_scan_and_effect_report_cli.py
backend/tests/integration/test_agent_lock_hooks.py
backend/tests/integration/test_capacity_wait_negative_control.py
backend/tests/integration/test_crash_execution_still_failed_http.py
backend/tests/integration/test_edit_write_lock_hook.py
backend/tests/integration/test_execution_api.py
backend/tests/integration/test_executor_dag.py
backend/tests/integration/test_file_lock_manager.py
backend/tests/integration/test_fixture_replay.py
backend/tests/integration/test_migration_audit_schema.py
backend/tests/integration/test_missing_plan_404_http.py
backend/tests/integration/test_plans_list_waiting_visible_http.py
backend/tests/integration/test_quota_normal_plan_unaffected_http.py
backend/tests/integration/test_quota_parked_plan_status_http.py
backend/tests/integration/test_quota_parked_progress_http.py
backend/tests/integration/test_quota_status_survives_restart_http.py
backend/tests/integration/test_quota_wait_topup_log_http.py
backend/tests/integration/test_quota_wait_window_log_http.py
backend/tests/integration/test_service_spawn_repro.py
backend/tests/integration/test_verification_baseline_freeze.py
backend/tests/integration/verification_chain/test_attribution_isolation.py
backend/tests/integration/verification_chain/test_fixture_plans_loadable.py
backend/tests/integration/verification_chain/test_runner_outcomes.py
backend/tests/integration/verification_chain/test_scan_report_artifact.py
backend/tests/integration/verification_chain/test_zero_llm_on_deterministic_path.py
backend/tests/perf/test_perf_verification.py
backend/tests/security/test_plan_id_boundary_matrix.py
backend/tests/security/test_plan_id_containment.py
backend/tests/security/test_request_guard_not_bypassed_in_suite.py
backend/tests/security/test_static_route_containment.py
backend/tests/static_gates/source_scan.py
backend/tests/static_gates/test_brief_references_context_pack.py
backend/tests/static_gates/test_e2e_lifecycle_produces_no_state_json.py
backend/tests/static_gates/test_fixtures_carry_no_local_measurements.py
backend/tests/static_gates/test_no_cross_consumer_aggregation_facade_exists.py
backend/tests/static_gates/test_no_json_state_filename_in_backend_source.py
backend/tests/static_gates/test_no_plan_artifacts_in_source_tree.py
backend/tests/static_gates/test_plan_fixtures_stay_out_of_the_operator_plans_tree.py
backend/tests/static_gates/test_state_db_path_has_one_resolver.py
backend/tests/test_config_paths.py
backend/tests/test_edit_write_containment_guard.py
backend/tests/test_execution_start_api.py
backend/tests/test_extract_failed_vps.py
backend/tests/test_plan_id_derivation.py
backend/tests/test_repair_task_assembler.py
backend/tests/test_server_json_cleanup.py
backend/tests/test_sub_agent_watchdog.py
backend/tests/test_usage_registry.py
backend/tests/test_verification_api.py
backend/tests/test_verification_integration.py
backend/tests/test_verification_state_machine.py
backend/tests/unit/db/test_plan_execution_single_phase_column.py
backend/tests/unit/notifications/test_card_snapshot.py
backend/tests/unit/notifications/test_exec_progress_watch.py
backend/tests/unit/test_agent_persist.py
backend/tests/unit/test_allowed_write_files_hook.py
backend/tests/unit/test_arch_design_principles.py
backend/tests/unit/test_arch_generator_hard_gate.py
backend/tests/unit/test_cards_header_and_recent_activity_v10.py
backend/tests/unit/test_ci_bugfix_gates.py
backend/tests/unit/test_command_quality.py
backend/tests/unit/test_dispatcher_persist_path.py
backend/tests/unit/test_execution_log_tail.py
backend/tests/unit/test_execution_status_log_backfill.py
backend/tests/unit/test_file_lock_broker.py
backend/tests/unit/test_hitl_test_isolation.py
backend/tests/unit/test_ids.py
backend/tests/unit/test_m6_cleanup.py
backend/tests/unit/test_orphan_reaper_daemon_stop.py
backend/tests/unit/test_plan_id_scrubber.py
backend/tests/unit/test_plan_status.py
backend/tests/unit/test_preflight_endpoints.py
backend/tests/unit/test_preflight_review.py
backend/tests/unit/test_progress_prefers_current_task_list.py
backend/tests/unit/test_recovery_closes_its_connections.py
backend/tests/unit/test_refiner_loop.py
backend/tests/unit/test_refiner_structure.py
backend/tests/unit/test_repair_failing_case_ids.py
backend/tests/unit/test_report_deliverable.py
backend/tests/unit/test_self_review_in_generator.py
backend/tests/unit/test_service_restart_preserves_fixtures.py
backend/tests/unit/test_smoke_v9_robustness.py
backend/tests/unit/test_stranded_card_sweep.py
backend/tests/unit/test_subagent_ledger.py
backend/tests/unit/test_task_commit_message_has_no_plan_id.py
backend/tests/unit/test_task_commit_message_is_checked_before_commit.py
backend/tests/unit/test_terminal_analysis_brief_render.py
backend/tests/unit/test_terminal_analysis_judge.py
backend/tests/unit/test_terminal_analysis_judge_contract.py
backend/tests/unit/test_terminal_analysis_session.py
backend/tests/unit/test_test_command_read_only_whitelist.py
backend/tests/unit/test_test_command_rules_declared_paths.py
backend/tests/unit/test_undeclared_write_gate_no_false_positive.py
backend/tests/unit/test_undeclared_write_gate_paths.py
backend/tests/unit/test_usage_registry_attribution.py
backend/tests/unit/test_verification_agent.py
backend/tests/unit/test_verification_evidence_staging.py
backend/tests/unit/test_verification_loop_outcome.py
backend/tests/unit/test_verification_repair_tasks_card.py
backend/tests/unit/test_verification_round_lease.py
backend/tests/unit/test_verification_service_freshness.py
backend/tests/unit/test_verification_subagent_verdict.py
backend/tests/unit/test_watchdog_actions.py
backend/tests/unit/test_watchdog_detect.py
backend/tests/unit/test_watchdog_signal.py
backend/tests/unit/verification/test_root_cause_report_address.py
""".split()
)


def find_plans_tree_references(text: str) -> list[tuple[int, str]]:
    """Return ``(lineno, rule)`` for every way *text* points at the plans tree.

    The rules are **syntactic**, not textual — see the module docstring for
    why that distinction is the whole difference between a gate people read
    and a gate people switch off.

    Args:
        text: The module's source.

    Returns:
        One ``(lineno, rule)`` pair per offending construct, sorted.
    """
    try:
        with warnings.catch_warnings():
            # Parsing somebody else's file is not this gate's business:
            # a stray escape sequence in a module that is not a test is
            # that module's problem, and the gate's output has to stay
            # about the plans tree.
            warnings.simplefilter("ignore", DeprecationWarning)
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(text)
    except SyntaxError as exc:
        # A module that does not parse cannot be scanned; failing closed
        # keeps an unparseable file from walking past the gate.
        return [(exc.lineno or 1, "the module does not parse")]

    parents: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node

    module_scope, nested = _collect_scopes(tree)
    detector = _PlansTreeDetector(parents, nested, module_scope)
    detector.visit(tree)
    return sorted(set(detector.hits))


def governed_test_modules() -> list[Path]:
    """The test modules the rule applies to, as repository-relative paths.

    "Test module" is the repository's own definition: a first-party ``.py``
    file under a ``tests`` directory, or a first-party ``test_*.py`` file
    anywhere (``test_no_plan_ids_in_non_test_source.is_test_source``).  The
    definition is reused rather than restated: a second definition of "which
    files are tests" is a second answer to the same question.
    """
    modules: list[Path] = []
    for path in source_scan.iter_first_party_sources():
        relative = source_scan.repo_relative(path)
        if path.suffix != ".py" or not is_test_source(relative):
            continue
        modules.append(relative)
    return sorted(modules)


def test_new_tests_do_not_reference_real_plans() -> None:
    """No test module outside the written-down baseline may name the plans tree."""
    modules = governed_test_modules()
    assert modules, (
        "no test module found — the rule would pass without reading a byte. "
        "The walker or the test-source definition has moved."
    )

    offenders: list[tuple[str, list[tuple[int, str]]]] = []
    for relative in modules:
        if str(relative) in PRE_EXISTING_OFFENDERS:
            continue
        text = (REPO_ROOT / relative).read_text(encoding="utf-8")
        hits = find_plans_tree_references(text)
        if hits:
            offenders.append((str(relative), hits))

    assert not offenders, (
        "these test files point at the operator's real plans tree, which is "
        "live state the server reads: "
        + "; ".join(
            f"{name}:{hits[0][0]} ({hits[0][1]})" for name, hits in offenders
        )
        + ". Build the plan directory with "
        "`tests/fixtures/plan_artifacts_fixture.py`'s `plan_dir` instead — it "
        "writes under `tmp_path` only — and keep the database rows to "
        "`plan_sqlite_seeder`."
    )


def test_the_plan_state_fixture_is_governed_and_clean() -> None:
    """The fixture this change adds is inside the rule's scope, and it is clean.

    Two things at once.  The scope: a fixture that the rule cannot see is a
    fixture the rule cannot hold.  The cleanliness: the supported way to build
    a plan directory must itself not carry a path to the real one.
    """
    modules = governed_test_modules()
    assert PLAN_STATE_FIXTURE in modules, (
        f"{PLAN_STATE_FIXTURE} is not among the files the rule scans — the "
        "rule would not be able to hold the one module whose whole job is to "
        "build plan directories."
    )

    text = (REPO_ROOT / PLAN_STATE_FIXTURE).read_text(encoding="utf-8")
    hits = find_plans_tree_references(text)
    assert not hits, (
        f"{PLAN_STATE_FIXTURE} names the plans tree at line {hits[0][0]} — "
        "the fixture is the one module that must not."
    )


def test_the_rule_sees_the_references_it_looks_for() -> None:
    """Negative control: the rule finds what it is written to find.

    A rule that cannot fire is indistinguishable from a rule that passes.
    The five writings below are the ways a helper ends up pointing at the
    operator's real tree, and the rule has to see **every one** of them —
    they are five different spellings of the same accident, and catching
    four of them is catching none of them in practice.

    The rule also has to stay quiet on the shapes that are *not* accidents:
    a temporary root, an injected root, a fixture root, a mention in prose,
    a path handed over as a keyword argument.  A gate that cries wolf is a
    gate people switch off.
    """
    writings = {
        "a plans directory built by joining": 'root = base / "plans"\n',
        "a plans directory with a plan id under it": (
            'root = base / "plans" / plan_id\n'
        ),
        "a plans directory derived from the file's own location": (
            'root = Path(__file__).resolve().parents[3] / "plans"\n'
        ),
        "a plans directory held in a variable": 'plans_dir = base / "plans"\n',
        "a plans directory held in a constant": 'PLANS_DIR = base / "plans"\n',
    }
    for name, text in writings.items():
        assert find_plans_tree_references(text), (
            f"the rule does not see {name}"
        )

    safe = {
        "a temporary root": 'root = tmp_path / "plans"\n',
        "an injected root": (
            "def test_it(base):\n    root = base / \"plans\"\n"
        ),
        "a fixture root": 'root = Path("fixtures") / "plans"\n',
        "a mention in prose": (
            "# the report lives in "
            + PLANS_SEGMENT
            + "/p1/logs/executor_crash.json\n"
        ),
        "a path passed in as a keyword argument": (
            'f(plan_dir="/tmp/plans/p1")\n'
        ),
        # The four spellings of the production root the rule has to
        # recognise as **proven** rather than as suspicious: the suite
        # redirects all four, so they are the proof the module docstring
        # claims the rule understands.
        "the production root reached through its module": (
            "plans_dir = Path(server.PLANS_DIR)\n"
        ),
        "the production root reached through a private alias": (
            "plans_dir = Path(_server.PLANS_DIR)\n"
        ),
        "the redirected root read out of an environment mapping": (
            'plans_dir = Path(child_env["PDT_PLANS_DIR"])\n'
        ),
        "the production root reached through its resolver": (
            "plans_dir = Path(server.resolve_plans_dir())\n"
        ),
    }
    for name, text in safe.items():
        assert not find_plans_tree_references(text), (
            f"the rule fires on {name}, which is not a way to reach the "
            "operator's tree"
        )
