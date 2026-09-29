"""验证点 ``test_command`` 的有界性护栏。

背景（2026-09-15）
--------------------------

VP 没有必要去跑整个 nightly CI —— 太久了；把门禁的 CI 跑过就可以。

a production plan 的 VP-023「Nightly CI 全过 (docker compose up + pytest)」每轮
跑 18-23 分钟（实测 1101s / 1382s），而同一个仓库的门禁 ``pr-gate.yml``
是 5 个并行 job、**≤5 分钟**。VP-023 从计划生成那天起就是无界的，而且
它在整个失败史里挂了 5 次——其中两次（``PYTEST_EXIT=124`` 在 540s 被砍、
``VerificationSubAgent has no attribute 'logger'``）还是 后端自己的 bug。

``prompts.VERIFICATION_PLAN_SYSTEM_PROMPT`` 里**本来就有**一条"单条
test_command 不超过 15 分钟"的软约束，VP-023 就是 18 分钟——所以问题不是
"提示词没写"，而是**提示词说了但没人兜底**。这个模块就是那道兜底：

* :data:`GENERATION_RULES` —— 同一份规则的可读文本，由 ``prompts.py``
  插进计划生成提示词里。规则文本和检测代码**共用这一个常量**，杜绝
  "提示词改了、检测没跟上"的漂移。
* :func:`find_violations` —— 生成后对每条 ``test_command`` 做静态检查，
  命中无界模式就返回违规项。
* :func:`annotate_plan` —— 把违规写进 ``plan_data`` 的确定性后处理，
  与 ``_flag_manual_check_vps`` / ``_auto_downgrade_manual_check_vps``
  同一层。

判据只有「有界 / 无界」一条。早期版本还要求"每条测试命令都要有显式时间
上限"，实测把 30 个 VP 里的 **23 个合法单测命令**全判违规——宁可错杀的
检查比不做更糟（LLM 会为了过检查空转）。有界命令本来就不会长，执行期还有
per-VP 预算兜底。

检测的是**形态**不是**结果**：只看这条命令会不会去跑一个无界的东西。
命令到底有没有真的跑到目标测试，是执行期的问题。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, NamedTuple

#: 2026-09-17: 「退出码被管道吞掉」的检测只有一份实现，放在
#: :mod:`test_command_quality`（task / repair 命令的护栏），这里复用。
from test_command_quality import find_pipeline_exit_issue


#: 违规项。``code`` 是稳定的机器可读标识（测试与日志都按它断言），
#: ``detail`` 是给人看的一句话。
class Violation(NamedTuple):
    code: str
    detail: str


#: 插进计划生成提示词的硬规则。与 :func:`find_violations` 的检测一一对应
#: —— 改一边就必须改另一边，所以它们放在同一个文件里。
#:
#: 措辞在 2026-09-15 被修正过一次，值得记下来：初版写成"禁止 nightly /
#: 禁止跑整个测试树"，把**手段**当成了**目的**。跑全量本身并不是错——
#: 理论上所有测试用例都应该被跑；错的是**把一个 VP 定义成"把所有的测试用例跑一遍"**，
#: 因为直接跑 Nightly CI 本质上就是这件事。验收命令应当按任务内容点名到
#: 对应的测试用例。
#:
#: 所以规则的重点不是"别跑全量"，而是"**一个 VP 必须界定到能证明它那条
#: 断言的那几个用例**"。覆盖面由多个有界 VP 共同构成，不由单个 VP 扛。
GENERATION_RULES = """**验收命令必须界定到「能证明这条断言的那几个用例」（硬规则，2026-09-15）**：
- 一个 VP 要证明的是一条具体断言，所以它的命令必须**点名到对应的测试用例**：
  `<文件>::<用例>`、某个测试文件、某个模块/子目录，或 `-k` / `-m` 表达式
  收窄到相关的那一组。
- 跑全量测试本身没有错——错的是**把一个 VP 定义成「把所有的测试用例跑一遍」**。
  那样的 VP 既不精确（失败时说不清是哪条断言挂了），又很贵（十几分钟起）。
  覆盖面应当由**多个各自有界的 VP 共同构成**。
- 不接受的形态（共同点都是「没有界定范围」）：
  - `pytest tests/`、裸 `pytest`、`pytest tests/unit tests/integration` 这类
    不点名到用例的调用；
  - `cargo test --workspace`、裸 `cargo test` 这类跑整个 workspace 的调用；
  - 任何带 `nightly` 的东西（`-m nightly`、直接调 nightly workflow 命令）。
    nightly 的定位就是"把所有测试跑一遍"，那是 CI 的活，不是某一条 VP 的证明；
  - `docker compose up -d && pytest tests/` 这种「起全家桶再跑全量」的组合。
- 仓库的**门禁**（gate / pr-gate 那一个，通常在 5 分钟内）是**有界**的，可以
  作为「整仓门禁」类 VP 的范围使用。
- 自定义脚本（不是 pytest/cargo 这类标准 runner）同样要能说清它验的是哪几条
  断言；说不清就拆成多个有界的 VP。
- 为什么：无界命令动辄 18-23 分钟，会撞 watchdog 的静默阈值、拖长每一轮，
  而且一旦失败很难定位到底是哪一块挂了。

**退出码必须可判（硬规则，2026-09-17）**：
- VP 的命令行信号就是它的**进程退出码**，判分不解析 stdout 里的
  `EXIT_CODE=` 文本。管道 `|` 的退出码取**最后一个命令**，所以
  `... | tee log`、`... | tail -30` 恒为 0，`... | grep X` 只反映"是否匹配"
  ——被测命令失败会被吞掉，VP 假通过且无人追查（an earlier plan repair-r3-08 的
  全量 CI 就是这样被 pre-flight 当成"已完成"跳过的）。
- ✅ `timeout 900 pytest -v tests/x.py::test_y > /tmp/<vp>.log 2>&1; rc=$?; tail -n 20 /tmp/<vp>.log; exit $rc`
- ✅ `set -o pipefail; timeout 900 pytest -v tests/x.py 2>&1 | tee /tmp/<vp>.log`
- ❌ `... 2>&1 | tee /tmp/<vp>_progress.log`（退出码是 tee 的）
- ❌ `... 2>&1 | tail -30` / `... | grep PASSED`
"""


#: `pytest` 的收窄开关。命中任意一个就认为这条 pytest 调用是**有界**的。
_NARROWING_FLAGS = ("-k", "-m", "--ignore", "--deselect")

#: 这些 pytest 位置目标等于「整棵测试树」。子目录（``tests/serialization/``）
#: 刻意不在其中——它们是有界的，判违规只会制造噪声。
_WIDE_PYTEST_ROOTS = frozenset({
    "tests", "tests/", "./tests", "./tests/",
    "test", "test/", "./test", "./test/",
    ".", "./",
})

#: 需要吃掉下一个 token 作为取值的开关。注意 ``--lib`` / ``--bins`` /
#: ``--workspace`` 这类 cargo 开关**不在此列**——它们是纯开关，过滤名是
#: 位置参数（``cargo test --lib <过滤器>``）。
_VALUE_FLAGS = frozenset({
    "-k", "-m", "-p", "-n", "--timeout", "--ignore", "--deselect",
    "--test", "--package", "--reporter", "--bin",
})


def _segments(cmd: str) -> List[str]:
    """按 shell 分隔符把复合命令切成独立段。

    **必须分段**：早期版本直接在整条命令上一次切词，``&&`` / ``;`` 被当成
    空白吃掉后，``pytest tests/serialization/ && npx playwright test ...``
    里 playwright 的 ``test`` 会被算成 pytest 的位置参数，于是误判无界。
    每段单独判定才对得上 shell 的真实语义。
    """
    return [s for s in re.split(r"&&|\|\||;|\|", cmd) if s.strip()]


def _shell_tokens(segment: str) -> List[str]:
    """把一段命令粗切成 token（引号不做特殊处理，够用即可）。"""
    return [t for t in re.split(r"\s+", segment.strip()) if t]


def _positional_args(tokens: List[str], tool: str) -> List[str]:
    """``tokens`` 里 ``tool`` 之后不被任何开关吃掉的「位置参数」。"""
    try:
        start = next(
            i for i, t in enumerate(tokens)
            if t == tool or t.endswith("/" + tool)
        )
    except StopIteration:
        return []

    args: List[str] = []
    skip_next = False
    for t in tokens[start + 1:]:
        if skip_next:
            skip_next = False
            continue
        if t.startswith("-"):
            if t in _VALUE_FLAGS:
                skip_next = True
            continue
        args.append(t)
    return args


def _pytest_violation(tokens: List[str], cmd: str) -> Violation | None:
    targets = _positional_args(tokens, "pytest")
    # 「宽」= 没有任何位置目标（走 pytest.ini 的 testpaths），或者目标就是
    # 整棵测试树的根，或者一次点了一堆目录（`pytest tests/unit tests/integration`
    # 实质就是整棵树）。**单个子目录不算宽**——`pytest tests/serialization/`
    # 是有界的，判违规只会制造噪声。
    dir_targets = [t for t in targets if not re.search(r"\.py\b", t)]
    wide = (
        (not targets)
        or any(t in _WIDE_PYTEST_ROOTS for t in targets)
        or len(dir_targets) >= 2
    )
    # 「收窄」= 表达式 / marker / 忽略 / 显式节点 ID / 点名到文件。
    narrowed = (
        any(flag in cmd for flag in _NARROWING_FLAGS)
        or "::" in cmd
        or any(re.search(r"\.py\b", t) for t in targets)
    )
    if wide and not narrowed:
        return Violation(
            "unbounded_pytest",
            "pytest 没有界定到具体用例（点名文件/节点/表达式都没给），"
            f"等于把测试跑一遍：{' '.join(targets) or '(无位置参数)'}",
        )
    return None


def _cargo_violation(tokens: List[str]) -> Violation | None:
    # ``cargo test`` 的收窄形态有两种：
    #   * 位置过滤名          —— ``cargo test --lib <过滤器>`` / ``cargo test <过滤器>``
    #   * ``--test <文件>``   —— 指名某个集成测试文件
    # 两者都没有（裸 ``cargo test`` / ``cargo test --workspace``）就是全量。
    try:
        idx = next(
            i for i, t in enumerate(tokens)
            if t == "cargo" or t.endswith("/cargo")
        )
    except StopIteration:
        return None
    if idx + 1 >= len(tokens) or tokens[idx + 1] != "test":
        return None

    rest = tokens[idx + 2:]
    has_test_selector = any(
        t.startswith("--test=") or (t == "--test" and i + 1 < len(rest))
        for i, t in enumerate(rest)
    )
    has_name_filter = bool(_positional_args(tokens[idx + 1:], "test"))
    if has_test_selector or has_name_filter:
        return None
    return Violation(
        "unbounded_cargo",
        "cargo test 没有界定到具体用例（既无过滤名也无 --test <文件>），"
        "等于把整个 workspace 跑一遍。",
    )


# ---------------------------------------------------------------------------
# V2/V3（2026-09-15）：命令点名的测试目标必须真实存在
# ---------------------------------------------------------------------------
#
# 任务侧早有这道检查（``tasks_generator.TaskOutputValidator`` 的 Step 4
# ``test_command_function_name_check``：AST 解析目标文件、确认函数真的定义在
# 里面）。VP 侧一直缺 —— 于是 VP-013 的 ``cargo test --lib <集成测试名>``
# 编译成功、匹配 0 个测试、退出 0，却"验证"通过；VP-017 扫描一个不存在的
# 目录，``grep`` 退 2 被 ``|| echo CLEAN`` 吞掉。**这两类都是"命令看起来
# 跑了，其实什么都没验"。**

#: 认得出来的测试 runner。命令里**一个都没有**时，静态层无从判断它的范围
#: —— 那是 :func:`needs_semantic_review` 的触发条件（交给 LLM 判语义）。
_RECOGNISED_RUNNERS = frozenset({
    "pytest", "py.test", "cargo", "jest", "vitest", "mocha", "tox", "nox",
    "npm", "pnpm", "yarn", "npx", "go", "mvn", "gradle",
})

#: 像测试标识符的 token（``test_foo`` / ``TestFoo``）。
#: 只对这类做存在性搜索 —— 正则、marker 表达式不该被当成名字去找。
_TEST_IDENTIFIER_RE = re.compile(r"^(?:test_\w+|Test\w*)$")

#: 搜索标识符时要跳过的重目录（大、且与"测试定义在哪"无关）。
_SEARCH_PRUNE = frozenset({
    ".git", "node_modules", "target", "__pycache__", ".venv", "venv",
    "venv1", ".mypy_cache", ".pytest_cache", "dist", "build",
})

#: 单次标识符搜索最多读多少个文件（防止在一个巨大仓库上把计划生成拖住）。
_IDENTIFIER_SEARCH_FILE_CAP = 4000


def _strip_quotes(token: str) -> str:
    return token.strip("\"'")


def _segments_with_root(cmd: str, project_dir):
    """按 shell 分隔符切段，并跟踪 ``cd`` 造成的相对根变化。

    ``project_dir`` 为 ``None`` 时 root 恒为 ``None`` —— 调用方据此跳过全部
    文件系统检查，护栏在不带项目目录时仍然可用（保持它还是个纯函数模块）。
    """
    root = Path(project_dir) if project_dir else None
    for segment in _segments(cmd):
        tokens = [_strip_quotes(t) for t in _shell_tokens(segment)]
        if not tokens:
            continue
        if tokens[0] == "cd" and len(tokens) > 1 and not tokens[1].startswith("-"):
            if root is not None:
                root = root / tokens[1]
        yield tokens, root


def _exists_anywhere(rel: str, root, project_dir) -> bool:
    """``rel`` 在 ``root`` 或 ``project_dir`` 下存在吗？

    两个根都查一遍是**刻意的**：``cd`` 跟踪会因为引号、嵌套 ``bash -c`` 之类
    失准，而**误报**（把一条好命令判违规、逼 LLM 空转）的代价远高于漏报。
    只在这两处都找不到时，才认定"它点名了一个不存在的东西"。
    """
    for base in (root, Path(project_dir) if project_dir else None):
        if base is None:
            continue
        try:
            if (base / rel).exists():
                return True
        except OSError:
            continue
    return False


def _iter_source_files(root):
    """遍历 ``root`` 下的源码文件，剪掉重目录，并设总量上限。"""
    if root is None or not root.exists():
        return
    count = 0
    for path in root.rglob("*"):
        if count >= _IDENTIFIER_SEARCH_FILE_CAP:
            return
        if any(part in _SEARCH_PRUNE for part in path.parts):
            continue
        if not path.is_file():
            continue
        count += 1
        yield path


def _cargo_test_filters(tokens: List[str]) -> List[str]:
    """``cargo test [--lib] <过滤名>`` 里的位置过滤名。

    锚定在 ``cargo test`` 之后 —— 直接全局找 token ``"test"`` 会被
    ``pytest tests/test_x.py && cargo test`` 这类命令骗到。
    """
    try:
        idx = next(
            i for i, t in enumerate(tokens)
            if t == "cargo" or t.endswith("/cargo")
        )
    except StopIteration:
        return []
    if idx + 1 >= len(tokens) or tokens[idx + 1] != "test":
        return []
    return _positional_args(tokens[idx + 1:], "test")


#: 从源码里抽测试名字用的正则。
#:
#: **必须包含 ``class``。** ``_TEST_IDENTIFIER_RE`` 明确把 ``Test\\w*``
#: 当成合法标识符（pytest 的类节点 ``file.py::TestFoo`` 就是这么点的），
#: 但只收 ``def`` / ``fn`` 的话就永远找不到那个名字，于是每一条点名类节点的
#: VP 都会被误判成 ``unknown_test_identifier``：类真实存在、junit 里也确实
#: 跑过，仍被判"匹配不到任何测试"，白白触发重生成，最后还在计划上留一条假
#: 违规。失败是静默的 —— 判据只是"这个名字没找到"，看不出找法本身太窄。
#:
#: 方向是安全的：这里只是**多收一些名字**，让存在性检查更宽松 = 误报更少。
#: 这类检查一旦开始喊狼来了，LLM 就会为了过检查而空转，比不做更糟。
_NAME_DEF_RE = re.compile(r"\b(?:def|fn|class)\s+([A-Za-z_]\w*)")

#: ``_defined_names`` 的按-roots 缓存（见该函数的 docstring）。
_NAMES_CACHE: Dict[tuple, set] = {}


def _defined_names(roots) -> set:
    """收集 ``roots`` 下所有 ``def`` / ``fn`` / ``class`` 的名字。

    含 ``class`` 是必须的——pytest 的类节点（``file.py::TestFoo``）在
    ``_TEST_IDENTIFIER_RE`` 眼里是合法标识符，漏收就会把每一条点名类节点的
    命令误判成"匹配不到任何测试"（见 :data:`_NAME_DEF_RE` 的注释）。

    ``roots`` 的元素可以是目录，也可以是单个文件。

    带一层按 roots 缓存：同一个 VP 里可能有多个候选名（``-k "a or b"``），
    不缓存就会把同一棵树重扫多遍。计划生成期间仓库不会变，缓存是安全的；
    条目数封顶，避免长驻进程里无限增长。
    """
    key = tuple(str(r) for r in roots if r is not None)
    cached = _NAMES_CACHE.get(key)
    if cached is not None:
        return cached

    names: set = set()
    seen: set = set()
    for root in roots:
        if root is None:
            continue
        try:
            exists = root.exists()
        except OSError:
            continue
        if not exists:
            continue
        paths = [root] if root.is_file() else _iter_source_files(root)
        for path in paths:
            if path in seen:
                continue
            seen.add(path)
            if path.suffix not in (".py", ".rs"):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            names.update(_NAME_DEF_RE.findall(text))

    if len(_NAMES_CACHE) >= 32:
        _NAMES_CACHE.clear()
    _NAMES_CACHE[key] = names
    return names


def _identifier_matches(name: str, roots) -> bool:
    """``name`` 能不能匹配到 ``roots`` 下某个已定义的测试？

    **子串匹配，不是精确匹配。** cargo 的 test filter 与 pytest 的 ``-k``
    都是子串语义：``cargo test --test f test_a`` 会跑
    ``fn test_a_signal_fields()``。按精确名找会把这些命令全判成"点名了
    不存在的测试"—— 实测 VP-025 / VP-026 就是这样被误报的
    （``test_20260903_14_09_consolidation`` vs 真实名
    ``..._signal_fields``）。

    子串语义更宽松 = 误报更少，方向是对的：这类检查一旦开始喊狼来了，
    LLM 就会为了过检查而空转，比不做更糟。
    """
    defined = _defined_names(roots)
    return any(name in candidate for candidate in defined)


def _cargo_search_roots(tokens, root, project_dir) -> List[Path]:
    """``cargo test`` 这次会搜索哪些路径 —— 按 ``--lib`` / ``--test`` 限定。

    不建模作用域就抓不到 VP-013 那种形态：``cargo test --lib
    test_no_cross_branch_fallback`` 里，那个测试其实住在
    ``tests/signal_classification.rs``，``--lib`` 只搜 ``src/``，
    于是它编译成功、匹配 0 个测试、退出 0 —— 什么都没验。
    """
    bases = [b for b in (root, Path(project_dir) if project_dir else None) if b]
    for i, token in enumerate(tokens):
        if token == "--lib":
            return [b / "src" for b in bases]
        if token == "--test" and i + 1 < len(tokens):
            return [b / "tests" / f"{tokens[i + 1]}.rs" for b in bases]
        if token.startswith("--test="):
            name = token.split("=", 1)[1]
            return [b / "tests" / f"{name}.rs" for b in bases]
    return [p for b in bases for p in (b / "src", b / "tests")]


def _selector_violations(tokens, root, project_dir) -> List[Violation]:
    """命令点名的测试目标存在吗（V2），`-k` / 过滤名找得到吗（V3）。"""
    if project_dir is None:
        return []
    out: List[Violation] = []
    roots = [root, Path(project_dir)]

    # --- V2a: pytest 的位置目标必须存在 --------------------------------
    for target in _positional_args(tokens, "pytest"):
        path_part = target.split("::")[0]
        if not path_part or path_part.startswith("-"):
            continue
        looks_like_path = (
            re.search(r"\.py\b", path_part) or "/" in path_part or "::" in target
        )
        if looks_like_path and not _exists_anywhere(path_part, root, project_dir):
            out.append(Violation(
                "missing_test_target",
                f"pytest 点名的 `{path_part}` 在项目里不存在 —— "
                "这条命令验不到任何东西，请改指向真实存在的测试文件。",
            ))

    # --- V2b: cargo `--test <文件>` 必须存在 -----------------------------
    for i, token in enumerate(tokens):
        if token == "--test" and i + 1 < len(tokens):
            candidate = tokens[i + 1]
        elif token.startswith("--test="):
            candidate = token.split("=", 1)[1]
        else:
            continue
        rel = f"tests/{candidate}.rs"
        if not _exists_anywhere(rel, root, project_dir):
            out.append(Violation(
                "missing_test_target",
                f"cargo --test {candidate} 对应的 {rel} 不存在。",
            ))

    # --- V3: 点名的测试标识符必须真的能匹配到某个测试 --------------------
    #   覆盖面：`-k "a or b"`、`cargo test [--lib|--test f] <过滤名>`、
    #   `pytest <文件>::<用例>`。只搜字面像标识符的 token，正则/marker 不动。
    #
    #   **匹配语义按各 runner 的真实行为来**：
    #     * pytest 的 ``<文件>::<用例>`` → 精确节点 ID
    #     * pytest 的 ``-k`` 与 cargo 的过滤名 → **子串**（见 _identifier_matches）
    #   两者都按子串处理（子串是超集，误报更少）。
    for i, token in enumerate(tokens):
        if token not in ("-k", "--kw") or i + 1 >= len(tokens):
            continue
        for name in re.split(r"[\s()]+", tokens[i + 1]):
            if _TEST_IDENTIFIER_RE.match(name) and not _identifier_matches(name, roots):
                out.append(Violation(
                    "unknown_test_identifier",
                    f"`-k {name}` 在项目里匹配不到任何测试 —— "
                    "过滤器匹配不到测试时命令仍会退出 0，等于什么都没验。",
                ))

    for arg in _positional_args(tokens, "pytest"):
        if "::" not in arg:
            continue
        tail = arg.split("::")[-1]
        if _TEST_IDENTIFIER_RE.match(tail) and not _identifier_matches(tail, roots):
            out.append(Violation(
                "unknown_test_identifier",
                f"pytest 节点 `{tail}` 在项目里匹配不到任何测试。",
            ))

    # cargo 的过滤名要**按 --lib / --test 的作用域**去搜：不建模作用域就
    # 抓不到 VP-013 那种形态（--lib 配一个住在 tests/ 里的测试名）。
    for name in _cargo_test_filters(tokens):
        if not _TEST_IDENTIFIER_RE.match(name):
            continue
        scope = _cargo_search_roots(tokens, root, project_dir)
        if not _identifier_matches(name, scope):
            out.append(Violation(
                "unknown_test_identifier",
                f"`cargo test` 的过滤名 `{name}` 在 "
                f"{'、'.join(str(p) for p in scope)} 里匹配不到任何测试 —— "
                "过滤器匹配不到测试时命令仍会退出 0，等于什么都没验。",
            ))

    return out


#: 这些程序**有可能在做验证**，但不是标准测试 runner。命令里出现它们时，
#: 静态层才真的"判不了范围"，需要 LLM 读语义。
#:
#: 刻意**不含** ``echo``：``echo ok`` 这种命令静态层完全判得了 —— 它什么都
#: 没验，那不是"判不了"，那是"一眼就能看出来"。把它也送进 LLM 有两个坏处：
#: 白花一次 high 档调用；以及大量以 ``echo`` 占位的测试夹具会凭空多打一轮。
#: The LLM is the fallback only when static analysis genuinely cannot
#: decide — not merely when deciding is inconvenient.
_OPAQUE_VERIFIERS = frozenset({
    "bash", "sh", "zsh", "python", "python3", "node", "npx", "curl",
    "wget", "git", "maturin", "make", "docker", "docker-compose", "go",
})


def needs_semantic_review(cmd: Any) -> bool:
    """静态层**无从判断**这条命令的范围吗？

    触发条件（两个都要满足）：

    1. 命令里**没有**任何认得出来的测试 runner；
    2. 命令里**有**某个"可能在做验证但说不准"的程序（脚本 / 解释器 / 网络 /
       版本控制 / 构建工具）—— 即 :data:`_OPAQUE_VERIFIERS`，或一个
       ``.sh`` / ``.py`` / ``.js`` / ``.ts`` 脚本路径。

    典型触发者是自定义脚本（``bash -c ./verify.sh``）；典型**不**触发的是
    ``echo ok`` —— 它什么都没验，那是静态层一眼能判的，不是"判不了"。

    已经静态判出违规的命令也**不**走这条路：结论已经拿到了，没必要再花钱。
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return False
    if find_violations(cmd):
        return False
    lowered = cmd.lower()
    if any(
        re.search(rf"(^|[\s;&|/]){re.escape(runner)}\b", lowered)
        for runner in _RECOGNISED_RUNNERS
    ):
        return False
    if any(
        re.search(rf"(^|[\s;&|/]){re.escape(prog)}\b", lowered)
        for prog in _OPAQUE_VERIFIERS
    ):
        return True
    return re.search(r"[\w./-]+\.(?:sh|py|js|ts|mjs)\b", lowered) is not None


def find_violations(cmd: Any, project_dir: Any = None) -> List[Violation]:
    """检查单条 ``test_command``，返回全部违规项（空列表 = 通过）。

    只处理字符串命令；``None`` / 空串 / 非字符串一律放行——没有命令的 VP
    走的是 ``manual_check`` / ``code_review`` 那条路，不是本模块的职责。

    Args:
        cmd: 待检查的命令。
        project_dir: 项目根目录。给了才会做**存在性**检查（V2/V3：命令点名的
            测试目标是否真的存在）。不给则退化成纯语法检查。
    """
    if not isinstance(cmd, str) or not cmd.strip():
        return []

    violations: List[Violation] = []
    # nightly 只需要看整条命令：任何位置出现都算。
    if "nightly" in cmd.lower():
        violations.append(Violation(
            "nightly_marker",
            "命令引用了 nightly —— 它的定位就是「把所有测试跑一遍」，那是 CI "
            "的活，不是某一条 VP 的证明。请把这条 VP 收窄到能证明它断言的"
            "那几条用例。",
        ))

    # 2026-09-17: 退出码被管道吞掉。检测复用 ``test_command_quality``
    # 的唯一实现（task / repair 命令走的是同一条规则），避免两处各写一份
    # 判定而慢慢漂移。
    _pipeline_issue = find_pipeline_exit_issue(cmd)
    if _pipeline_issue is not None:
        violations.append(Violation(_pipeline_issue.code, _pipeline_issue.detail))

    for tokens, root in _segments_with_root(cmd, project_dir):
        segment = " ".join(tokens)
        if any(t == "pytest" or t.endswith("/pytest") for t in tokens):
            v = _pytest_violation(tokens, segment)
            if v:
                violations.append(v)
        cargo_v = _cargo_violation(tokens)
        if cargo_v:
            violations.append(cargo_v)
        violations.extend(_selector_violations(tokens, root, project_dir))

    # 完全相同的违规只说一次；**不同实例要各自留着** —— 反馈里得把每个
    # 点名不存在目标的命令都列出来，只报一个会让 LLM 改漏。
    seen: set = set()
    deduped: List[Violation] = []
    for v in violations:
        key = (v.code, v.detail)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(v)
    return deduped


def annotate_plan(
    plan_data: Dict[str, Any], project_dir: Any = None,
) -> List[Dict[str, Any]]:
    """把违规写进 ``plan_data``（就地），返回违规清单以便调用方记录/重试。

    ``project_dir`` 给了才会做存在性检查（V2/V3）。

    每个命中违规的 VP 拿到一个 ``command_guard`` 字段::

        "command_guard": {"violations": [{"code": ..., "detail": ...}]}

    **不**擅自改写命令——护栏的职责是"说出问题"，替 LLM 猜一条正确的命令
    是另一种越权（VP-013 的 ``--lib`` 就是被"善意改写"改写坏的）。
    调用方拿到返回值后自行决定是重试生成还是记录。
    """
    findings: List[Dict[str, Any]] = []
    points = plan_data.get("verification_points")
    if not isinstance(points, list):
        return findings

    # 2026-09-16: Phase 2（全量关卡）豁免有界命令要求。
    #
    # 这条护栏源自 2026-09-15 用户"VP 没必要跑整个 nightly CI，太久"的决定；
    # 2026-09-16 用户把它细化为"nightly/E2E 要做，但放到所有子任务验证通过
    # 之后的最后一段"。因此全量范围在 Phase 2 是**职责所在**，不是越界：
    # 在这里继续报违规会让生成循环与提示词互相打架（提示词要求 Phase 2 写
    # 全量命令，护栏又判它无界），白白耗掉重试额度。
    from verification_phases import is_final_gate

    for vp in points:
        if not isinstance(vp, dict):
            continue
        if is_final_gate(vp):
            vp.pop("command_guard", None)
            continue
        violations = find_violations(vp.get("test_command"), project_dir)
        if not violations:
            vp.pop("command_guard", None)
            continue
        vp["command_guard"] = {
            "violations": [
                {"code": v.code, "detail": v.detail} for v in violations
            ],
        }
        findings.append({
            "id": vp.get("id"),
            "title": vp.get("title"),
            "test_command": vp.get("test_command"),
            "violations": [v.code for v in violations],
        })

    return findings
