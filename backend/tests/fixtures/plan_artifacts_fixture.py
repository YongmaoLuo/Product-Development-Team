"""plan 目录状态构造器 —— 目录即 fixture，绝不触碰真实的 plans 树。

背景
----
一条 plan 的"状态"不是数据库里的几行，而是**一棵目录**：``tasks.json`` /
``verification-plan.md`` / ``verification_plan.json`` / ``prd.json`` /
``review.json`` 五件套的任意子集。每个用例自己手写这棵树，会同时坏三件事：

* **字节漂移** —— 同一个"合法的验证计划书"在两个用例里是两份字节，等价性
  门禁在两处就有两处含义；
* **越界写入** —— 图省事把目录建在真实的 plans 根下，就在那里留了一份
  ``current_phase: executing``，运行中的服务读那份清单时会把这条"plan"算成
  正在跑，而仓库里没有任何一个用例会因此变红；
* **撞名** —— ``-n 8`` 下每个 worker 有自己的 tmp 根，plan id 又是从目录
  派生的，不做处理就会撞。

这个模块把三件事各收进一处：目录状态的构造（:func:`plan_dir`）、字节级的写入
与快照（:func:`write_doc_bytes` / :func:`snapshot`）、与 worker 无关的 plan id
（:func:`plan_id_for`）。

边界
----
* **只写文件系统**：构造器不碰数据库。数据库行由既有的
  ``plan_sqlite_seeder`` 注入 —— 两套写库逻辑会漂成两套真源。
* **只写给定的目录**：目录之外一个字节都不动。
* **计划书取 canonical 那一份**：字节恒定；机器计划由它推导（走 translator，
  并过等价性门禁），不是另写一份常量。

用法
----
::

    directory = plan_dir(tmp_path, tasks=True, doc=True)
    # 目录里有 tasks.json 与 verification-plan.md，没有别的
    before = snapshot(directory)
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

from framework.ids import derive_plan_id, slugify_plan_id  # noqa: E402
from plan_artifacts import (  # noqa: E402
    MACHINE_PLAN_FILENAME,
    PLAN_DOC_FILENAME,
    TASKS_FILENAME,
)
from tests.fixtures.verification_mode import (  # noqa: E402
    CANONICAL_PLAN_DOC,
    build_machine_plan,
)
from verification_plan_review import (  # noqa: E402
    PRD_FILENAME,
    PRD_REVIEW_FILENAME,
)

#: The PRD's review file.
#:
#: ``verification_plan_review`` owns two review files and names them apart:
#: the plan document's own review and the PRD's.  A plan directory holds the
#: PRD one, so the PRD constant is the one re-exported here — a second
#: spelling of the same filename is a second answer to the same question.
REVIEW_FILENAME: str = PRD_REVIEW_FILENAME

#: The canonical task list: one task, in the shape the executor reads.
#:
#: Fixed rather than derived: two directories built by this module hold the
#: same bytes, so a test comparing two of them compares the thing it meant to
#: compare.
CANONICAL_TASKS: Dict[str, Any] = {
    "tasks": [
        {
            "id": "1",
            "title": "目录即 fixture",
            "description": (
                "plan 目录的状态由构造器造出来：产物字节恒定，"
                "目录只落在 tmp_path 里，数据库行由 plan_sqlite_seeder 注入。"
            ),
            "test_command": (
                "pytest tests/unit/test_plan_artifacts_fixture_contract.py -v"
            ),
            "files_to_modify": [
                "backend/tests/fixtures/plan_artifacts_fixture.py",
            ],
            "depends_on": [],
        }
    ]
}

#: The canonical PRD: the smallest document that still carries a decision
#: point, in the schema the generators read.
CANONICAL_PRD: Dict[str, Any] = {
    "title": "fixture PRD",
    "background": {
        "why": "测试数据不落在真实的 plans 树上：目录即 fixture。",
    },
    "goals": {
        "what": "产物字节恒定，目录只落在 tmp_path 里。",
    },
    "scope": {
        "in": ["plan 目录状态构造器"],
        "out": ["数据库行"],
    },
    "decisions": [
        {
            "index": 0,
            "title": "数据库行由 plan_sqlite_seeder 注入",
            "context": "构造器同时写文件与数据库，就是两套真源。",
            "problem": "两套写库逻辑会漂。",
            "evidence": "构造器的调用点只需要目录状态。",
            "action": "构造器只写文件；数据库行交给既有的 seeder。",
        }
    ],
    "acceptance": [
        "AC-1: 目录里只有被打开的产物文件",
        "AC-2: 计划书的字节与 canonical 那一份逐字节相同",
    ],
}

#: The canonical PRD review: one accepted decision point.
CANONICAL_REVIEW: Dict[str, Any] = {
    "items": [
        {
            "index": 0,
            "title": "数据库行由 plan_sqlite_seeder 注入",
            "status": "accepted",
            "note": "",
        }
    ]
}

#: The date stamp a fixture plan id carries.
#:
#: Fixed rather than "today": the id has to be a function of the path and of
#: nothing else, or "同一个路径永远同一个 id" tomorrow stops being true.
_FIXTURE_DATE: dt.datetime = dt.datetime(2026, 1, 1)

#: The xdist worker directory names.
#:
#: pytest gives every worker its own temporary root (``popen-gw0`` and
#: friends), so two workers running the same case would derive two different
#: ids from the same logical directory.  The worker component is therefore
#: not part of a plan's identity.
_WORKER_DIR_RE = re.compile(r"^(?:popen-)?gw\d+$")


def _worker_neutral_parts(directory: Path) -> List[str]:
    """The path components of *directory* with the worker directories removed."""
    return [part for part in directory.parts if not _WORKER_DIR_RE.match(part)]


def write_doc_bytes(path: Path, text: str) -> Path:
    """Write ``text`` to ``path`` as exact UTF-8 bytes.

    Bytes, not text: the plan document's hash is computed over the bytes on
    disk, and a newline translation (CRLF on a platform that translates) would
    change the hash without changing a single character of the document.

    Args:
        path: Where to write. Missing parent directories are created.
        text: The document.

    Returns:
        The path written to.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode("utf-8"))
    return path


def machine_plan_for(doc: str) -> Dict[str, Any]:
    """The machine plan equivalent to the plan document ``doc``.

    Derived, not written out: the derivation goes through the translator and
    is checked against the translator's own equivalence gate, so the machine
    plan cannot drift away from its document.  A machine plan written as a
    constant would drift the moment the document's wording changed, and the
    drift would show up as an equivalence failure that reads like a
    production bug.

    Args:
        doc: The plan document.

    Returns:
        The machine plan.
    """
    return build_machine_plan(doc)


def plan_dir(
    tmp_path: Path,
    *,
    tasks: bool = False,
    doc: bool = False,
    machine_plan: bool = False,
    prd: bool = False,
    review: bool = False,
) -> Path:
    """Build a plan directory holding exactly the artefacts the flags ask for.

    The five flags are orthogonal: every combination of them produces a
    directory holding exactly the corresponding files.  Artefacts whose flag
    is off are removed, because a stale ``tasks.json`` left in a
    verification-only directory changes what the directory *means* — the
    branch is decided by which files are present.

    Only the filesystem is written.  The database rows are injected by the
    existing ``plan_sqlite_seeder``: a constructor that also wrote rows would
    be a second source of truth for the same plan.

    Args:
        tmp_path: The directory to build. Created if missing; nothing outside
            it is touched.
        tasks: Write ``tasks.json``.
        doc: Write the canonical plan document.
        machine_plan: Write the machine plan derived from that document.
        prd: Write ``prd.json``.
        review: Write the PRD's ``review.json``.

    Returns:
        The directory.
    """
    tmp_path = Path(tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)

    wanted = {
        "tasks": (TASKS_FILENAME, CANONICAL_TASKS),
        "doc": (PLAN_DOC_FILENAME, None),
        "machine_plan": (MACHINE_PLAN_FILENAME, None),
        "prd": (PRD_FILENAME, CANONICAL_PRD),
        "review": (REVIEW_FILENAME, CANONICAL_REVIEW),
    }
    enabled = {
        "tasks": tasks,
        "doc": doc,
        "machine_plan": machine_plan,
        "prd": prd,
        "review": review,
    }

    for flag, (filename, payload) in wanted.items():
        path = tmp_path / filename
        if not enabled[flag]:
            if path.is_file():
                path.unlink()
            continue
        if flag == "doc":
            write_doc_bytes(path, CANONICAL_PLAN_DOC)
        elif flag == "machine_plan":
            _write_json(path, machine_plan_for(CANONICAL_PLAN_DOC))
        else:
            _write_json(path, payload)

    return tmp_path


def _write_json(path: Path, payload: Dict[str, Any]) -> None:
    """Write ``payload`` as JSON, byte-stable across calls and machines."""
    text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)
    path.write_text(text + "\n", encoding="utf-8")


def snapshot(directory: Path) -> Dict[str, Dict[str, Any]]:
    """Snapshot the files of ``directory``: name → sha256 + mtime.

    The two fields answer two different questions.  The hash answers "did the
    content change?"; the mtime answers "did anything touch it?".  A test that
    only looked at the hash would miss a rewrite with identical bytes, and a
    test that only looked at the mtime would miss a change inside the same
    clock tick.

    Only the directory's own files are seen — a plan directory's subtrees
    (worktrees, baselines) are somebody else's state.

    Args:
        directory: The directory to snapshot.

    Returns:
        A mapping from file name to ``{"sha256": ..., "mtime": ...}``.  An
        empty mapping for a directory that does not exist.
    """
    directory = Path(directory)
    if not directory.is_dir():
        return {}

    result: Dict[str, Dict[str, Any]] = {}
    for entry in sorted(directory.iterdir()):
        if not entry.is_file():
            continue
        stat = entry.stat()
        result[entry.name] = {
            "sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
            "mtime": stat.st_mtime,
        }
    return result


def plan_id_for(path: Path) -> str:
    """A plan id derived from ``path``: unique, and independent of the worker.

    Two properties, both needed:

    * **unique** — two different directories must not derive the same id, or
      two plans collide on one directory name;
    * **worker-independent** — pytest gives every xdist worker its own
      temporary root, so an id that counted the worker directory would differ
      between workers for the same logical directory.

    The identity of a directory is therefore its path with the worker
    components removed, plus its own name.  The date stamp is fixed rather
    than "today", so the id stays a function of the path: the same path gives
    the same id tomorrow as well.

    Args:
        path: The directory (or any path) the id is derived from.

    Returns:
        A legal plan id.
    """
    path = Path(path)

    leaf = slugify_plan_id(path.name, max_len=24)
    if not leaf:
        leaf = "plan"
    if not leaf[0].isalpha():
        # The plan-id shape wants a letter right after the date stamp.
        leaf = "p" + leaf

    identity = "/".join([*_worker_neutral_parts(path.parent), path.name])
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:8]

    return derive_plan_id(f"{leaf}-{digest}", max_len=40, when=_FIXTURE_DATE)
