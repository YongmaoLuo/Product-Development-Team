"""验证计划书这条链路的输入边界。

决策点 11（安全与输入边界）与架构决策点 6（结构非法要可见，而不是变成接口错误）。

计划书是**不可信输入**：模型写、人审、落盘，然后又被当成真源读回来。读它
的那条路径上，四个边界必须同时成立：

* **路径边界** —— 请求带来的 plan id 不得让任何文件落到计划根之外，包括
  「id 本身合法、但它指向的目录是一个逃出根的符号链接」这一种；
* **内容边界** —— 畸形文档要变成 4xx（结构非法是**看得见的**结论），而不是
  500（接口错误把「文档坏了」与「服务坏了」混成一件事）；
* **载荷边界** —— 错误响应体与落盘的失败原因里不得出现本机绝对路径；
* **请求边界** —— 这条链路必须**经过真实的 request guard**：裸请求（不带
  ``X-PDT-Request``）被守卫挡下，带了头的才走到端点自己的门前。

四个边界的失败方式不同，所以看的东西也不同：路径边界看**盘上多了什么**，
内容边界看**状态码**，载荷边界看**响应体里有没有路径**，请求边界看**守卫
自己给的拒绝理由**。

计划目录一律建在 ``tmp_path`` 之下（仓库里真实的计划树是运行时的活状态，
写进去就等于替别人声明了一条 plan）。
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException
from starlette.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import request_guard  # noqa: E402
import server  # noqa: E402
from plan_artifacts import (  # noqa: E402
    BRANCH_CONFLICT,
    MACHINE_PLAN_FILENAME,
    PLAN_DOC_FILENAME,
    TASKS_FILENAME,
    ArtifactSet,
)
from routes import phases as phases_routes  # noqa: E402
from routes import verification as verification_routes  # noqa: E402
from routes.plans import (  # noqa: E402
    ARTIFACT_CONFLICT_ERROR,
    artifact_conflict_payload,
)
from tests.fixtures.verification_mode import (  # noqa: E402
    CANONICAL_PLAN_DOC,
    build_machine_plan,
)

#: 本模块用到的计划 id。形态合法（``<日期>-<slug>``），但只是夹具名。
#: 日期落在归档分界线之后 —— 早于分界的 plan 会被归档扫描判成 archived，
#: 那条路走不到产物判定这一层。
PLAN_ID = "20260901-boundary"

#: 载荷里不得出现的本机路径前缀。三个都写出来：写一个就等于赌这一台机器
#: 的临时目录长什么样。
FORBIDDEN_PATH_PREFIXES = ("/Users/", "/home/", "/tmp/")


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------


class _FakeTool:
    """翻译阶段的模型替身：回一份与计划书等价的机器计划。

    替身**不联网**：翻译这一步在生产里是要问模型的，而用例要断言的是
    「问了几次、问了什么」，不是模型答得对不对。
    """

    def __init__(self, payload: Dict[str, Any]) -> None:
        self.payload = payload
        self.calls: List[Dict[str, Any]] = []

    def query_json(self, **kwargs: Any) -> Dict[str, Any]:
        self.calls.append(kwargs)
        return self.payload


def _install_fake_tool(monkeypatch: pytest.MonkeyPatch) -> _FakeTool:
    """把「造模型工具」这一步换成替身，返回替身本身（用来数调用次数）。"""
    tool = _FakeTool(build_machine_plan(CANONICAL_PLAN_DOC))
    monkeypatch.setattr(server, "create_coding_tool", lambda *a, **k: tool)
    return tool


def _tree(root: Path) -> List[str]:
    """``root`` 之下所有条目的相对路径（不跟随符号链接）。

    用来回答「这一趟请求在盘上多了什么」。``os.walk`` 默认不跟随符号链接，
    所以逃出根的那个目录只以**链接**的形式出现，它指向的那个目录的内容则
    按它本来的位置被列出来 —— 两种形态都看得见。
    """
    entries: List[str] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        for name in list(dirnames) + list(filenames):
            entries.append(str((base / name).relative_to(root)))
    return sorted(entries)


def _temp_residue(plan_dir: Path) -> List[str]:
    """plan 目录里残留的临时文件（``.tmp`` / ``.bak`` / ``__pycache__``）。"""
    residue: List[str] = []
    for path in plan_dir.rglob("*"):
        if path.name == "__pycache__" or path.name.endswith((".tmp", ".bak")):
            residue.append(str(path.relative_to(plan_dir)))
    return sorted(residue)


# ---------------------------------------------------------------------------
# 文档变形：每一种都是「读它的那条路径」必须挡住的输入
# ---------------------------------------------------------------------------


def _doc_without_section(title: str) -> bytes:
    """把一个必需章节整段拿掉。"""
    kept: List[str] = []
    skipping = False
    for line in CANONICAL_PLAN_DOC.splitlines(keepends=True):
        if line.startswith("## "):
            skipping = line.strip() == f"## {title}"
        if not skipping:
            kept.append(line)
    return "".join(kept).encode("utf-8")


def _doc_with_long_line() -> bytes:
    """一行超长（字符数远超任何一行该有的长度）。"""
    return (CANONICAL_PLAN_DOC + "x" * 50_000 + "\n").encode("utf-8")


def _doc_oversized() -> bytes:
    """整个文档超过 1 MiB —— 每一行都不长，加起来太长。"""
    padding = "".join("y" * 99 + "\n" for _ in range(11_000))
    return (CANONICAL_PLAN_DOC + padding).encode("utf-8")


def _doc_with_unbalanced_fence() -> bytes:
    """代码围栏不成对：正文里嵌了半个代码块，围栏的个数成了单数。

    单数的围栏意味着「从这里到文末都算代码」——读这份文档的人和读它的模型
    看到的章节边界是乱的。
    """
    nested = "```bash\necho fence\n"
    return CANONICAL_PLAN_DOC.replace(
        "## 风险面", nested + "\n## 风险面"
    ).encode("utf-8")


#: 畸形文档的六种形态。名字会进断言消息，所以要能一眼看出是哪种。
MALFORMED_DOCS = (
    ("empty", b""),
    ("missing_section", _doc_without_section("风险面")),
    ("overlong_line", _doc_with_long_line()),
    ("invalid_utf8", b"\xff\xfe not utf-8 at all"),
    ("oversized", _doc_oversized()),
    ("unbalanced_code_fence", _doc_with_unbalanced_fence()),
)


# ---------------------------------------------------------------------------
# 1. 路径边界
# ---------------------------------------------------------------------------


def test_plan_id_path_traversal_rejected(monkeypatch, tmp_path):
    """四种穿越形态都被挡在文件系统之外。

    三种是**字符串**形态（``../x``、``/etc/passwd``、``a/../../b``），一种
    是**符号链接**形态：id 本身完全合法，可它指向的目录在计划根之外。

    前三种由共享校验器挡下（id 不合法）；第四种不是 —— 字符串合法就意味着
    校验器放行，于是「目录在根外」这件事只能由**解析之后**的包含性检查来
    挡。少了那一道，一个合法的 id 就足以把写入引到根外。
    """
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "escape-link"
    link.symlink_to(outside, target_is_directory=True)
    # 计划根指到夹具：会话里的那个计划根是**共享**的，往里面丢一个符号链接
    # 会漏给别的用例（比如数 plan 条数的那个）。
    monkeypatch.setattr(server, "PLANS_DIR", root)

    before = _tree(tmp_path)

    for bad_id in ("../x", "/etc/passwd", "a/../../b"):
        with pytest.raises(HTTPException) as excinfo:
            phases_routes.generate_verification_plan(plan_id=bad_id)
        assert excinfo.value.status_code in (400, 404), (
            f"{bad_id!r} must be refused, got {excinfo.value.status_code}"
        )

    # 符号链接形态：id 合法，目录在根外 —— 必须被**拒绝**，而不是「目录在，
    # 就当它是这条 plan」。
    with pytest.raises(HTTPException) as excinfo:
        phases_routes.generate_verification_plan(plan_id=link.name)
    assert excinfo.value.status_code in (400, 404), (
        "a plan directory that resolves outside the plans root must be "
        f"refused, got {excinfo.value.status_code}"
    )

    # HTTP 这一层同样：穿越形态不得被当成一条 plan。
    #
    # 编码过的斜杠（``%2F``）在路由层就被拆开，落到应用末尾那条 catch-all
    # 上 —— 那种拒绝由路由给出，不经处理函数，所以这里只断言「没成功」。
    # 真正由处理函数回答的是**符号链接**形态：id 合法、路由匹配得上，于是
    # 拒绝必须来自入口的包含性检查，状态码是 400。
    client = TestClient(server.app)
    for raw in ("..%2Fx", "%2Fetc%2Fpasswd", "a%2F..%2F..%2Fb"):
        response = client.post(f"/api/verification-plan/{raw}/generate")
        assert response.status_code >= 400, (
            f"{raw!r} must be refused, got {response.status_code}"
        )

    response = client.post(f"/api/verification-plan/{link.name}/generate")
    assert response.status_code == 400, (
        "a plan directory that resolves outside the plans root must be "
        f"refused with 400, got {response.status_code}"
    )

    # 盘上多出来的东西：一条也没有。
    assert _tree(tmp_path) == before, (
        "the refused requests wrote outside the plans root"
    )


# ---------------------------------------------------------------------------
# 2. 内容边界
# ---------------------------------------------------------------------------


def test_malformed_plan_doc_returns_422_not_500(monkeypatch, tmp_path):
    """六种畸形输入都变成 4xx，而不是 500。

    「结构非法」是一个**看得见的结论**（这份文档不合格，请改），不是接口
    错误（服务坏了）。两者混在一起时，运维看到的是 500，去查服务；实际上
    坏的是一份文档。
    """
    _install_fake_tool(monkeypatch)

    for name, payload in MALFORMED_DOCS:
        plan_dir = tmp_path / name
        plan_dir.mkdir()
        (plan_dir / PLAN_DOC_FILENAME).write_bytes(payload)

        response = verification_routes._prepare_machine_plan(PLAN_ID, plan_dir)

        assert response is not None, (
            f"{name}: a malformed plan document must be refused, not accepted"
        )
        assert response.status_code == 422, (
            f"{name}: expected 422, got {response.status_code}"
        )
        body = json.loads(response.body)
        assert body["error"] == "verification_plan_invalid", name
        assert "Traceback" not in response.body.decode("utf-8"), name
        assert not (plan_dir / MACHINE_PLAN_FILENAME).exists(), (
            f"{name}: a refused document must not leave a machine plan behind"
        )


# ---------------------------------------------------------------------------
# 3. 原子写
# ---------------------------------------------------------------------------


def test_machine_plan_write_is_atomic(monkeypatch, tmp_path):
    """发布机器计划是一次原子替换：崩在半路也看不到半个文件。

    机器计划是验证轮真正消费的那份产物。一份写了一半的 JSON 比没有更糟 ——
    它读得出来、看着像一份计划，然后让验证轮去验一份不存在的计划。

    三种形态一起钉住：**发布崩了**（改名失败）、**翻译中途断了**（模型调用
    抛异常）、**盘上已经有**（这一轮不发布，旧的那一份逐字节不变）。
    """
    plan_dir = tmp_path / "atomic"
    plan_dir.mkdir()
    (plan_dir / PLAN_DOC_FILENAME).write_text(
        CANONICAL_PLAN_DOC, encoding="utf-8"
    )
    machine_plan_path = plan_dir / MACHINE_PLAN_FILENAME
    _install_fake_tool(monkeypatch)

    def _boom(*args: Any, **kwargs: Any) -> None:
        raise OSError("simulated crash while publishing the machine plan")

    # 发布的最后一步（改名）崩了：目标文件要么是旧的，要么根本不存在。
    monkeypatch.setattr(os, "replace", _boom)
    monkeypatch.setattr(os, "rename", _boom)

    response = verification_routes._prepare_machine_plan(PLAN_ID, plan_dir)

    assert response is not None, (
        "a failed publish must be reported, not swallowed"
    )
    assert response.status_code in (422, 500), response.status_code
    body = json.loads(response.body)
    assert body.get("error"), body
    assert not machine_plan_path.exists(), (
        "a half-written machine plan was published"
    )
    assert not _temp_residue(plan_dir), (
        f"temporary files left behind: {_temp_residue(plan_dir)}"
    )

    # 翻译**中途**注入异常：那次调用还没拿到答案，所以目标 .json 不该存在
    # （盘上原本也没有），盘上同样不该有临时文件。
    interrupted = tmp_path / "mid-translation"
    interrupted.mkdir()
    (interrupted / PLAN_DOC_FILENAME).write_text(
        CANONICAL_PLAN_DOC, encoding="utf-8"
    )

    class _InterruptedTool:
        def query_json(self, **kwargs: Any) -> Dict[str, Any]:
            raise RuntimeError("the model call was interrupted")

    monkeypatch.setattr(
        server, "create_coding_tool", lambda *a, **k: _InterruptedTool()
    )
    response = verification_routes._prepare_machine_plan(PLAN_ID, interrupted)
    assert response is not None, (
        "an interrupted translation must be reported, not swallowed"
    )
    assert response.status_code in (422, 500), response.status_code
    body = json.loads(response.body)
    assert body.get("error"), body
    assert not (interrupted / MACHINE_PLAN_FILENAME).exists(), (
        "an interrupted translation left a machine plan behind"
    )
    assert not _temp_residue(interrupted), (
        f"temporary files left behind: {_temp_residue(interrupted)}"
    )

    # 第三种形态：盘上**已经有**机器计划。那一份是上一轮管线写下的产物，
    # 这一轮**不重译、不改写** —— 「没创建」是这一轮的结论，「保持旧内容」
    # 是它的另一半：目标 .json 要么不存在，要么 sha256 一字不变。
    #
    # 写进去的那一份用**紧凑 JSON**（而不是发布器写的那种缩进格式）：内容
    # 一样，字节不一样 —— 于是「被重新发布过一次」这件事在 sha256 上看得见，
    # 而不是被一份内容相同的新文件盖过去。
    preserved = tmp_path / "preserved"
    preserved.mkdir()
    (preserved / PLAN_DOC_FILENAME).write_text(
        CANONICAL_PLAN_DOC, encoding="utf-8"
    )
    preserved_plan = preserved / MACHINE_PLAN_FILENAME
    preserved_bytes = json.dumps(
        build_machine_plan(CANONICAL_PLAN_DOC),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    preserved_plan.write_bytes(preserved_bytes)
    sha_before = hashlib.sha256(preserved_bytes).hexdigest()
    inode_before = preserved_plan.stat().st_ino

    response = verification_routes._prepare_machine_plan(PLAN_ID, preserved)
    assert response is None, (
        "an existing machine plan must be reused as-is, not re-translated: "
        f"{response.body!r}"
    )
    assert hashlib.sha256(preserved_plan.read_bytes()).hexdigest() == sha_before, (
        "an existing machine plan was rewritten: its sha256 changed"
    )
    assert preserved_plan.stat().st_ino == inode_before, (
        "an existing machine plan was republished (the file was replaced)"
    )
    assert not _temp_residue(preserved), (
        f"temporary files left behind: {_temp_residue(preserved)}"
    )


# ---------------------------------------------------------------------------
# 4. 载荷脱敏
# ---------------------------------------------------------------------------


def test_error_payloads_have_no_absolute_paths(monkeypatch, tmp_path):
    """错误响应体与落盘的失败原因里不得出现本机绝对路径。

    绝对路径在错误体里等于把「这台机器的目录结构」发给任何一个能看见这个
    响应的人；异常原文尤其危险 —— 它连文件名带盘符一起抄了出去。
    """
    _install_fake_tool(monkeypatch)

    payloads: List[str] = []

    # (a) 计划书读不出来（它是个目录）。
    unreadable_doc = tmp_path / "unreadable-doc"
    unreadable_doc.mkdir()
    (unreadable_doc / PLAN_DOC_FILENAME).mkdir()
    response = verification_routes._prepare_machine_plan(
        PLAN_ID, unreadable_doc
    )
    assert response is not None
    assert response.status_code == 422, response.status_code
    payloads.append(response.body.decode("utf-8"))

    # (b) 盘上的机器计划读不出来（它也是个目录）。
    unreadable_plan = tmp_path / "unreadable-plan"
    unreadable_plan.mkdir()
    (unreadable_plan / PLAN_DOC_FILENAME).write_text(
        CANONICAL_PLAN_DOC, encoding="utf-8"
    )
    (unreadable_plan / MACHINE_PLAN_FILENAME).mkdir()
    response = verification_routes._prepare_machine_plan(
        PLAN_ID, unreadable_plan
    )
    assert response is not None
    assert response.status_code == 422, response.status_code
    payloads.append(response.body.decode("utf-8"))

    # (c) 落盘的失败原因（auto-loop 崩溃时写进终态的那句话）。
    captured: Dict[str, Any] = {}
    monkeypatch.setattr(
        server,
        "_persist_verification_terminal",
        lambda plan_id, status, reason: captured.update(
            {"status": status, "reason": reason}
        ),
    )
    leaky_path = tmp_path / "plan-dir" / MACHINE_PLAN_FILENAME
    server._verification_state[PLAN_ID] = {"verification_status": "running"}
    try:
        verification_routes._persist_auto_loop_crash(
            PLAN_ID, RuntimeError(f"boom while reading {leaky_path}")
        )
    finally:
        server._verification_state.pop(PLAN_ID, None)
    assert captured.get("status") == "failed", captured
    payloads.append(str(captured.get("reason") or ""))

    for text in payloads:
        for prefix in FORBIDDEN_PATH_PREFIXES:
            assert prefix not in text, (
                f"the payload leaks a local path ({prefix}): {text}"
            )
        assert str(tmp_path) not in text, (
            f"the payload leaks the plan directory path: {text}"
        )


# ---------------------------------------------------------------------------
# 5. 产物冲突的契约
# ---------------------------------------------------------------------------


def test_artifact_conflict_code_unchanged(monkeypatch, tmp_path):
    """产物冲突的 API 契约不变：409 + ``artifact_conflict``。

    共存合法之后，这个错误码不再由产物判定产出，但它**仍然是调用方用来识别
    「产物状态不符」这一类回答的字符串** —— 所以契约要留着：码不变、状态码
    不变、载荷里只有产物的相对名。

    契约挂在**任务生成**入口上（决策点 11：共存目录调任务生成 → 409 且
    ``error_code == "artifact_conflict"``，文案变、码不变）：共存是**单向**
    的 —— 目录里已有计划书时不再写任务清单（不把一条纯验证分支的计划改造成
    开发分支）。计划书生成那一侧是放行的，所以这个错误码在共存语义下只剩
    任务生成这一个入口会给出。
    """
    assert ARTIFACT_CONFLICT_ERROR == "artifact_conflict"

    artifacts = ArtifactSet(
        tasks_path=Path("tasks.json"),
        plan_doc_path=Path("verification-plan.md"),
        branch=BRANCH_CONFLICT,
    )
    payload = artifact_conflict_payload(PLAN_ID, artifacts)
    assert payload["error"] == "artifact_conflict", payload
    assert payload["plan_id"] == PLAN_ID, payload
    for name in list(payload["artifacts"]) + list(payload["existing_artifacts"]):
        assert not Path(name).is_absolute(), (
            f"the conflict payload must name artefacts relatively: {name}"
        )

    # 路由层：共存目录调任务生成，答案仍然是 409 + 同一个错误码。
    #
    # 判定走**生产路径**（``plan_artifacts.detect`` 读真实目录），不替换任何
    # 模块级的 ``detect`` —— 换掉判定等于让这条用例测的是替身而不是生产
    # 逻辑，而「判定漂移」正是这条契约要防的东西。
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", root)
    plan_dir = root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)
    # 共存目录：任务清单与计划书同时存在（主分支仍是开发分支 —— 任务优先）。
    (plan_dir / TASKS_FILENAME).write_text("{}", encoding="utf-8")
    (plan_dir / PLAN_DOC_FILENAME).write_text("# plan\n", encoding="utf-8")

    client = TestClient(server.app)
    response = client.post(f"/api/tasks/{PLAN_ID}/generate")
    assert response.status_code == 409, response.text
    body = response.json()
    assert body["error"] == "artifact_conflict", response.text
    assert body["plan_id"] == PLAN_ID, response.text
    # 共存目录的主分支是开发分支：判定结果不是「冲突」。
    assert body["branch"] == "dev", response.text
    # 载荷里只有两份产物的**相对名** —— 不带本机绝对路径。
    assert sorted(body["artifacts"]) == [TASKS_FILENAME, PLAN_DOC_FILENAME], (
        response.text
    )
    assert sorted(body["existing_artifacts"]) == [
        TASKS_FILENAME,
        PLAN_DOC_FILENAME,
    ], response.text
    for name in list(body["artifacts"]) + list(body["existing_artifacts"]):
        assert not Path(name).is_absolute(), (
            f"the conflict payload must name artefacts relatively: {name}"
        )
    assert str(tmp_path) not in response.text, (
        f"the conflict payload leaks the plan directory path: {response.text}"
    )


# ---------------------------------------------------------------------------
# 6. 请求守卫
# ---------------------------------------------------------------------------


def test_request_guard_required_on_new_flow(monkeypatch, tmp_path):
    """裸请求 → 403；带头部 → 过守卫，答案由端点自己的门决定。

    决策点 11 要求这条新流程**经过真实的 request guard**：一个不带
    ``X-PDT-Request`` 的请求必须被守卫挡下（403），带了的才走到端点自己的
    门前。

    「过守卫」不等于「成功」—— 端点还有自己的门（基线门、PRD 审核面门），
    它们给出的 4xx 是**端点**的答案，不是守卫的。所以这里钉住的是「不是
    403」：403 只会由守卫给出，别的门不会给。
    """
    root = tmp_path / "root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(server, "PLANS_DIR", root)
    plan_dir = root / PLAN_ID
    plan_dir.mkdir(parents=True, exist_ok=True)

    # 裸请求：客户端构造时默认带头部（conftest 注入），把它摘掉。
    bare = TestClient(server.app)
    bare.headers.pop(request_guard.REQUEST_HEADER, None)
    refused = bare.post(f"/api/verification-plan/{PLAN_ID}/generate", json={})
    assert refused.status_code == 403, refused.text
    body = refused.json()
    assert body.get("error") == "forbidden", refused.text
    # 403 是**守卫**给的：拒绝理由里得指名那个缺失的头部。别的层也会给
    # 403（只是这一层没有），所以光看状态码不足以证明「经过真实守卫」。
    assert request_guard.REQUEST_HEADER in str(body.get("detail")), refused.text

    # 带头部：过守卫，答案由端点自己的门决定（这个目录里没有记录基线 →
    # 422 ``baseline_missing``）。
    guarded = TestClient(server.app)
    passed = guarded.post(f"/api/verification-plan/{PLAN_ID}/generate", json={})
    assert passed.status_code != 403, passed.text
    assert passed.status_code == 422, passed.text
    assert passed.json()["error"] == "baseline_missing", passed.text
