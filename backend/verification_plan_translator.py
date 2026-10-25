"""把审核过的验证计划书翻译成机器格式的验证计划。

三阶段管道
----------
1. **解析**（确定性，见 :mod:`verification_plan_doc`）—— 文档变成
   :class:`~verification_plan_doc.ParsedPlan`，验证点集合在这一步定死；
2. **翻译**（本模块）—— 模型只补**执行细节**，集合一个都不许动；
3. **校验**（确定性）—— 模型给的验证点集合必须与文档逐条相等。

为什么集合由文档定死
--------------------
计划书是人工审核的唯一对象，机器格式是执行判据的唯一依据。这两者一旦不是同一
批验证点，"审核过的"与"实际被验的"就分家了 —— 而且**这件事不会自己暴露**：

* 多出来的验证点看起来很正常，像是审核者本来就想加的；
* 少掉的验证点看起来像"本来就该少"，像是这条已经不需要验了。

两种漂移都会安静地改变"验过什么"这个事实。所以翻译阶段对集合是零容忍：多一条、
少一条、改个 id、改个标题，一律抛 :class:`TranslationError`；调用方在
``translate`` 返回之后才落盘，所以抛错就意味着**JSON 没落盘**。

模型能碰什么、不能碰什么
------------------------
模型只被允许补八个**执行字段**：

``uses_services`` / ``request`` / ``assertions`` / ``target_url`` /
``test_command`` / ``ci_entry`` / ``ci_timeout_seconds`` / ``phase_order``

其余字段一律以文档为准：

* ``id`` 与 ``title`` 是**身份** —— 模型给出不一样的值就是漂移，抛错；
* ``verification_method`` / ``related_prd_criteria`` / ``expected_result``
  这类内容字段，模型写什么都不采信，丢掉即可 —— 文档里那些是**人工审核过**的
  事实，模型的版本不是。

服务声明不来自模型：``services`` 是仓库级事实（端口只在这里出现一次），由
``repo_facts`` 带进来。

等价性门禁
----------
:func:`validate_equivalence` 是**独立于翻译**的一道门禁：它拿任意来源的机器格式
（不一定是 :func:`translate` 刚产出的那一份）与计划书逐条比对，差在哪儿就报在
哪儿 —— 四项**实质内容**：验证点集合（``points``）、通过判据（``criteria``）、
执行范围（``scope``）与证据编号（``evidence``）。

它**返回**差异而不是抛错：差异是一份报告，不是异常。调用方拿到非空列表就
**不许落盘** —— 落盘与否是调用方的决定，本模块只负责把差异说清楚。

本模块**不写任何文件**。落盘是调用方的事 —— 正因如此，"抛错"与"JSON 不落盘"
是同一件事的两面。
"""

from __future__ import annotations

import inspect
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from verification_plan_doc import ParsedPlan, PlanItem

logger = logging.getLogger(__name__)


#: 模型**只许**填的执行字段。其余字段一律以文档为准。
ALLOWED_LLM_FIELDS: Tuple[str, ...] = (
    "uses_services",
    "request",
    "assertions",
    "target_url",
    "test_command",
    "ci_entry",
    "ci_timeout_seconds",
    "phase_order",
)

#: 身份字段：文档说了算。模型给出不一样的值 = 漂移 = 抛错。
PROTECTED_FIELDS: Tuple[str, ...] = ("id", "title")

#: 调模型的超时（秒）。与 :mod:`verification_split_llm` 的同名字段同量级。
LLM_TIMEOUT_SECONDS: int = 300


class TranslationError(Exception):
    """翻译失败。

    两种来源：模型给的验证点集合与文档对不上（漂移），或者模型的回应根本不是
    一份计划（不是 JSON / 没有验证点 / 调用失败）。原异常挂在 ``__cause__`` 上。
    """


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

#: 系统提示词。**不做任何字符串格式化** —— 里面写着 JSON 示例与服务占位符
#: （``{{svc.api.url}}``），格式化会把它们的花括号吃掉。
_SYSTEM_PROMPT = """你是一位验证执行工程师。你会拿到一份**已经人工审核过**的验证计划书里的验证点骨架，以及一份仓库事实。

你只有一个任务：为每个验证点补上**执行细节**。验证点的集合、id、标题都已经由人定死 —— 你既不能增删验证点，也不能改它们的 id 或标题。

只允许你输出下面这八个字段（其余字段一律不要输出；输出了也会被丢掉）：

- uses_services：该验证点依赖的服务名数组（服务名取自「仓库事实」里的 services）
- request：api_test 的请求规格，形如 {"method": "GET", "url": "{{svc.api.url}}/api/items", "headers": {}, "body": null, "timeout_seconds": 30}
- assertions：api_test 的断言数组，形如 [{"name": "C1: 状态码", "status": 200}, {"name": "C2: 资源 id 存在", "json_path": "$.id", "exists": true}]（断言名必须以它验证的判据编号 "C<n>: " 开头，每条 C 编号判据至少一条断言）
- target_url：ui_validation / e2e 的目标地址，必须用服务占位符，形如 http://127.0.0.1:{{svc.api.port}}/dashboard
- test_command：一条可执行命令（在仓库根目录下可直接跑）
- ci_entry：full_ci 的整仓 CI 总入口（不得收窄到单个测试文件或选择器）
- ci_timeout_seconds：ci_entry 的超时秒数（默认 3600）
- phase_order：Phase 2 的执行顺序（1 = 整仓 Nightly CI 先跑，2 = 全量 E2E 后跑）

**纯 JSON 输出，不要 markdown 代码块围栏。** 输出形状：

{"verification_points": [{"id": "VP-001", "request": {}, "assertions": []}]}
"""

#: 用户提示词模板。只有两个占位符，模板自身不含花括号。
_USER_PROMPT = """仓库事实（JSON）：
{facts}

待补全的验证点骨架（JSON 数组，顺序即执行顺序）：
{points}

请为上面**每一条**验证点补上执行细节。输出的 verification_points 必须与上面的验证点一一对应：条数相同、id 相同、不多不少。

断言名必须以它验证的判据编号开头（形如 "C1: 状态码"），并且每条 C 编号判据至少有一条断言 —— 审核门禁会逐条核对「每个判据是否都被断言覆盖」。
"""


def _build_prompt(skeletons: Sequence[Dict[str, Any]], repo_facts: Mapping[str, Any]) -> str:
    """把骨架与仓库事实拼成给模型的提示词。"""
    facts = json.dumps(repo_facts, ensure_ascii=False, indent=2, sort_keys=True, default=str)
    points = json.dumps(list(skeletons), ensure_ascii=False, indent=2)
    return _USER_PROMPT.format(facts=facts, points=points)


# ---------------------------------------------------------------------------
# 文档骨架
# ---------------------------------------------------------------------------


def _criteria_text(item: PlanItem) -> str:
    """把文档里的编号判据拍平成一行（``C1: …；C2: …``）。"""
    return "；".join(f"{cid}: {text}" for cid, text in item.criteria.items())


def _skeleton(item: PlanItem) -> Dict[str, Any]:
    """文档给出的验证点骨架：身份 + 判据，模型不许改。"""
    return {
        "id": item.id,
        "title": item.title,
        "verification_method": item.method,
        "related_prd_criteria": _criteria_text(item),
        "expected_result": item.expected_result,
    }


# ---------------------------------------------------------------------------
# 调模型
# ---------------------------------------------------------------------------


def _accepts_keywords(func: Any, kwargs: Dict[str, Any]) -> bool:
    """这个函数能不能按给定的关键字参数调用。

    先试绑定再调用，避免"第一次调用因为签名不匹配抛 ``TypeError``、于是把同一个
    模型调用再发一遍"—— 那会让一次签名不匹配变成两次 LLM 调用。
    """
    try:
        inspect.signature(func).bind(**kwargs)
    except (TypeError, ValueError):
        return False
    return True


def _call_tool(tool: Any, prompt: str, system_instruction: str) -> Any:
    """问模型要一份计划。

    模型工具的接口形态有两种：带 ``query_json`` 的（仓库里的
    ``coding_tool.query_json``），以及只吃一个提示词的可调用对象。两种都支持。
    """
    try:
        query_json = getattr(tool, "query_json", None)
        if callable(query_json):
            kwargs = {
                "prompt": prompt,
                "system_instruction": system_instruction,
                "timeout": LLM_TIMEOUT_SECONDS,
            }
            if _accepts_keywords(query_json, kwargs):
                return query_json(**kwargs)
            # 签名只吃位置参数的替身 / 旧接口
            return query_json(prompt)
        if callable(tool):
            return tool(prompt)
        raise TranslationError(f"模型工具不可用：{type(tool).__name__}")
    except TranslationError:
        raise
    except Exception as exc:  # noqa: BLE001 — 模型的任何失败都归到翻译失败
        raise TranslationError(f"调用模型失败：{exc}") from exc


def _as_payload(raw: Any) -> Any:
    """把模型的回应变成 Python 对象。"""
    if isinstance(raw, (dict, list)):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise TranslationError("模型没有给出任何内容")
        if text.startswith("```"):
            # 去掉 markdown 代码块围栏（```json … ```）
            text = re.sub(r"^```[^\n]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text)
            text = text.strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise TranslationError(f"模型的回应不是 JSON：{exc}") from exc
    raise TranslationError(f"模型的回应不是 JSON 对象：{type(raw).__name__}")


def _extract_points(payload: Any) -> List[Dict[str, Any]]:
    """从模型回应里取出验证点数组。"""
    raw: Any = None
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, dict):
        raw = payload.get("verification_points")
        if raw is None:
            raw = payload.get("vps")
    if not isinstance(raw, list):
        raise TranslationError("模型的回应里没有验证点数组（verification_points）")
    points: List[Dict[str, Any]] = []
    for entry in raw:
        if not isinstance(entry, dict):
            raise TranslationError("模型给的验证点不是对象")
        points.append(entry)
    return points


# ---------------------------------------------------------------------------
# 零漂移校验
# ---------------------------------------------------------------------------


def _check_vp_set(doc_ids: Sequence[str], points: Sequence[Dict[str, Any]]) -> None:
    """模型给的验证点集合必须与文档**逐条相等**。"""
    llm_ids: List[str] = []
    for entry in points:
        raw_id = entry.get("id")
        if not isinstance(raw_id, str) or not raw_id.strip():
            raise TranslationError("模型给的验证点没有 id")
        llm_ids.append(raw_id.strip())

    duplicates = sorted(vid for vid, count in Counter(llm_ids).items() if count > 1)
    if duplicates:
        raise TranslationError("模型把同一条验证点给了多遍：" + "、".join(duplicates))

    added = sorted(set(llm_ids) - set(doc_ids))
    missing = sorted(set(doc_ids) - set(llm_ids))
    if added or missing:
        parts: List[str] = []
        if added:
            parts.append("模型新增了未审核的验证点：" + "、".join(added))
        if missing:
            parts.append("模型漏掉了已审核的验证点：" + "、".join(missing))
        raise TranslationError("；".join(parts))


def _check_identity(doc_vp: Dict[str, Any], llm_vp: Dict[str, Any]) -> None:
    """身份字段以文档为准：模型给了不一样的值就是漂移。"""
    for field in PROTECTED_FIELDS:
        if field not in llm_vp:
            continue
        given = llm_vp[field]
        given_text = given.strip() if isinstance(given, str) else str(given)
        want_text = str(doc_vp.get(field, "")).strip()
        if given_text != want_text:
            raise TranslationError(
                f"验证点 {doc_vp['id']} 的 {field} 被模型改了"
                f"（文档：{want_text!r}；模型：{given_text!r}）"
            )


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def translate(
    parsed: ParsedPlan,
    repo_facts: Mapping[str, Any],
    tool: Any,
) -> Dict[str, Any]:
    """把审核过的计划书翻译成机器格式的验证计划。

    Args:
        parsed: 任务 2 的解析产物 —— 验证点集合在这里定死。
        repo_facts: 仓库级事实。目前只用到 ``services``（服务声明，端口只在这里
            出现一次）与 ``repo_root``（给模型写相对路径用）。
        tool: 模型工具。带 ``query_json(prompt=..., system_instruction=...)`` 的
            句柄，或只吃一个提示词的可调用对象。

    Returns:
        ``{"services": [...], "verification_points": [...]}`` —— 每条验证点以文档
        的身份字段为准，加上模型补的执行字段。

    Raises:
        TranslationError: 验证点集合漂移、身份被改、模型的回应不是一份计划，或
            模型调用失败。**不落盘**：本函数不写任何文件。
    """
    if not isinstance(parsed, ParsedPlan):
        raise TranslationError(f"parsed 不是 ParsedPlan：{type(parsed).__name__}")

    facts: Dict[str, Any] = dict(repo_facts or {})
    services = facts.get("services")
    if services is None:
        services = []
    if not isinstance(services, list):
        raise TranslationError(
            f"repo_facts['services'] 不是数组：{type(services).__name__}"
        )

    skeletons = [_skeleton(item) for item in parsed.items]
    doc_ids = [sk["id"] for sk in skeletons]

    raw = _call_tool(tool, _build_prompt(skeletons, facts), _SYSTEM_PROMPT)
    points = _extract_points(_as_payload(raw))

    _check_vp_set(doc_ids, points)

    by_id = {str(entry["id"]).strip(): entry for entry in points}
    translated: List[Dict[str, Any]] = []
    for sk in skeletons:
        llm_vp = by_id[sk["id"]]
        _check_identity(sk, llm_vp)
        vp = dict(sk)
        for field in ALLOWED_LLM_FIELDS:
            if field in llm_vp:
                vp[field] = llm_vp[field]
        ignored = sorted(
            set(llm_vp) - set(ALLOWED_LLM_FIELDS) - set(PROTECTED_FIELDS)
        )
        if ignored:
            logger.warning(
                "翻译丢弃了模型多写的字段：vp=%s fields=%s",
                sk["id"],
                "、".join(ignored),
            )
        translated.append(vp)

    return {"services": list(services), "verification_points": translated}


# ---------------------------------------------------------------------------
# 等价性校验
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Divergence:
    """一处差异：机器格式与审核过的计划书对不上。

    ``dimension`` 是差异的维度，取值固定为 ``points`` / ``scope`` / ``criteria``
    / ``evidence``（审核的四项实质内容）；``item_id`` 是差异落在哪个验证点上
    （文档级问题用空串）；``detail`` 是一句人话。

    差异是**报告**，不是异常 —— 所以它可比较、可序列化、可以直接贴给人看。
    """

    dimension: str
    item_id: str
    detail: str

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.dimension}:{self.item_id}: {self.detail}"


#: 机器格式里 ``expected_result`` 的**规范化引用串**前缀：证据编号用逗号分隔，
#: 紧跟一个 ``" | "``，之后才是预期结果正文（``"E1,E2 | 两条判据全部满足"``）。
#:
#: 编号的顺序不算内容（集合相等即可），但前缀必须在 —— 前缀不在，这条验证点就
#: 等于"一条证据都没引用"。
_EVIDENCE_REF_RE = re.compile(r"^(?P<refs>E\d+(?:\s*,\s*E\d+)*)\s*\|\s*")


def _evidence_refs(expected_result: Any) -> Set[str]:
    """从机器格式的 ``expected_result`` 里取证据编号集合。

    没有规范前缀（或字段不是字符串）就返回空集 —— "少引"与"没引"落在同一条
    差异上，省掉一个说不清的特例。
    """
    if not isinstance(expected_result, str):
        return set()
    match = _EVIDENCE_REF_RE.match(expected_result)
    if not match:
        return set()
    return {ref.strip() for ref in match.group("refs").split(",") if ref.strip()}


# ---------------------------------------------------------------------------
# criteria 维度：判据编号
# ---------------------------------------------------------------------------

#: 自由文本里的判据编号（``C1``、``C2``……）。前后不能再接字母数字 ——
#: ``AC12``、``C1a`` 这种串不是判据编号。
_CRITERION_ID_RE = re.compile(r"(?<![0-9A-Za-z_])C\d+(?![0-9A-Za-z_])")

#: 断言名里的判据前缀（``C1: 状态码``）。前缀必须在**开头** —— 断言名是自由
#: 文本，出现在中间的多半是正文里顺带提到的编号，不是"这条断言验的是它"。
_ASSERTION_CRITERION_RE = re.compile(r"^\s*(?P<id>C\d+)\s*[:：]")


def _criterion_ids(text: Any) -> Set[str]:
    """从自由文本里取出判据编号集合。

    ``related_prd_criteria`` 是自由文本（``C1: …；C2: …``），编号就散在正文里，
    所以按"文本里出现了哪些 C 编号"来读。顺序不算内容。
    """
    if not isinstance(text, str):
        return set()
    return set(_CRITERION_ID_RE.findall(text))


def _assertion_criteria(assertions: Any) -> Tuple[Set[str], List[str]]:
    """断言对判据的覆盖情况。

    Returns:
        ``(被断言指到的判据编号集合, 缺 ``Cx:`` 前缀的断言名)``。两条都空
        表示"这条验证点没有断言" —— 那不是差异，是事实。
    """
    ids: Set[str] = set()
    missing_prefix: List[str] = []
    if not isinstance(assertions, list):
        return ids, missing_prefix
    for entry in assertions:
        raw_name = entry.get("name") if isinstance(entry, Mapping) else None
        name = raw_name.strip() if isinstance(raw_name, str) else ""
        match = _ASSERTION_CRITERION_RE.match(name)
        if match:
            ids.add(match.group("id"))
        else:
            missing_prefix.append(name or "<无名断言>")
    return ids, missing_prefix


# ---------------------------------------------------------------------------
# scope 维度：执行位置 vs 声明范围
# ---------------------------------------------------------------------------

#: 计划书里两个计划级章节的标题：范围内 / 排除。
SCOPE_SECTION = "验证范围"
EXCLUSION_SECTION = "排除范围"

#: 文本里的 URL。
_URL_RE = re.compile(r"https?://[^\s，。；、）)】」』“”\"'<>]+", re.IGNORECASE)

#: 文本里的仓库相对路径（至少含一个 ``/``）。
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./\-])[A-Za-z0-9_.\-]+(?:/[A-Za-z0-9_.\-]+)+/?"
)

#: URL 拆成 ``origin`` 与 ``path``。
_URL_SPLIT_RE = re.compile(
    r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://(?P<netloc>[^/?#]*)(?P<path>[/?#].*)?$"
)


def _url_parts(url: str) -> Tuple[str, str]:
    """URL 归一化成 ``(origin, path)``；解析不出来就返回两个空串。

    ``origin`` 是 ``scheme://host`` —— **端口不比**：计划书写 ``:9000``，机器
    格式写 ``{{svc.api.port}}``，比端口等于自己找麻烦。路径去掉末尾的 ``/``，
    根路径归一成空串（空串覆盖该 origin 下的所有路径）。
    """
    text = url.strip().rstrip(".,;:!?")
    match = _URL_SPLIT_RE.match(text)
    if not match:
        return "", ""
    netloc = match.group("netloc").rsplit("@", 1)[-1]
    host = netloc
    if ":" in host and not host.endswith("]"):
        host = host.split(":", 1)[0]
    origin = f"{match.group('scheme').lower()}://{host.lower()}"
    path = (match.group("path") or "/").split("#", 1)[0].split("?", 1)[0]
    return origin, path.rstrip("/")


def _normalize_path(raw: str) -> str:
    """仓库相对路径归一化：去掉 ``./`` 前缀、末尾的 ``/`` 与首尾标点。

    末尾的点要当心：``backend/x.py`` 里的点是扩展名的一部分，剥掉就变成了
    ``backend/x`` —— 比不出来。所以只有"点后面没有内容"（散文里句末的点）才剥。
    """
    text = raw.strip()
    while text and text[-1] in ".,;:!?'\"":
        if text[-1] == ".":
            _head, _sep, tail = text.rpartition(".")
            if tail and "/" not in tail:
                break
        text = text[:-1]
    while text.startswith("./"):
        text = text[2:]
    if text.endswith("/"):
        text = text[:-1]
    return text


def _scope_targets(text: Any) -> Tuple[List[str], List[str]]:
    """从一段自由文本里取出 ``(URL 前缀, 仓库相对路径)``。

    「范围」「排除范围」都是人写的自由文本（``backend/verification_loop.py 的
    配额等待路径``），所以按"这段话里出现了哪些位置"来读。URL 先摘走，剩下的
    才当路径看 —— 否则 URL 里的 ``/`` 会被当成路径分隔符。
    """
    if not isinstance(text, str) or not text.strip():
        return [], []
    urls: List[str] = []
    rest = text
    for raw in _URL_RE.findall(text):
        urls.append(raw.strip().rstrip(".,;:!?"))
        rest = rest.replace(raw, " ")
    paths: List[str] = []
    for raw in _PATH_RE.findall(rest):
        path = _normalize_path(raw)
        if path:
            paths.append(path)
    return urls, paths


def _url_contains(outer: str, inner: str) -> bool:
    """``inner`` 这个 URL 是否落在 ``outer`` 这个 URL 前缀之下。"""
    outer_origin, outer_path = _url_parts(outer)
    inner_origin, inner_path = _url_parts(inner)
    if not outer_origin or not inner_origin or outer_origin != inner_origin:
        return False
    if not outer_path:
        return True
    return inner_path == outer_path or inner_path.startswith(outer_path + "/")


def _path_contains(outer: str, inner: str) -> bool:
    """``inner`` 这个仓库相对路径是否落在 ``outer`` 之下（按路径段比）。"""
    if not outer or not inner:
        return False
    return inner == outer or inner.startswith(outer + "/")


def _vp_locations(entry: Mapping[str, Any]) -> List[Tuple[str, str, str]]:
    """验证点声明的执行位置：``[(字段名, 形式, 值)]``。

    形式只有两种：``"url"`` 与 ``"path"``。两种形式**互不可比** —— 应用里的
    URL 路径与仓库里的相对路径不在同一个坐标系里，拿它们互相比只会比出噪声。

    ``ci_entry`` 不在其中：整仓 CI 入口按契约就比任何单条验证点的范围宽，拿它
    比范围必然误报。
    """
    locations: List[Tuple[str, str, str]] = []

    target_url = entry.get("target_url")
    if isinstance(target_url, str) and target_url.strip():
        locations.append(("target_url", "url", target_url.strip()))

    request = entry.get("request")
    if isinstance(request, Mapping):
        request_url = request.get("url")
        if isinstance(request_url, str) and request_url.strip():
            locations.append(("request.url", "url", request_url.strip()))

    command = entry.get("test_command")
    if isinstance(command, str) and command.strip():
        urls, paths = _scope_targets(command)
        for url in urls:
            locations.append(("test_command", "url", url))
        for path in paths:
            locations.append(("test_command", "path", path))

    return locations


def _section_body(parsed: ParsedPlan, title: str) -> str:
    """计划书里某个章节的正文（章节不存在就当空）。"""
    section = parsed.sections.get(title)
    return section.body if section is not None else ""


def _hits_exclusion(
    form: str,
    value: str,
    excluded_urls: Sequence[str],
    excluded_paths: Sequence[str],
) -> bool:
    """执行位置是否命中排除范围。

    命中包含两个方向：执行位置落在排除项**之内**，以及执行位置比排除项**更
    宽**（跑了一整片目录，而排除项在目录里）—— 后者照样验到了被排除的东西。
    """
    if form == "url":
        return any(
            _url_contains(excluded, value) or _url_contains(value, excluded)
            for excluded in excluded_urls
        )
    return any(
        _path_contains(excluded, value) or _path_contains(value, excluded)
        for excluded in excluded_paths
    )


def _in_range(
    form: str,
    value: str,
    allowed_urls: Sequence[str],
    allowed_paths: Sequence[str],
) -> bool:
    """执行位置是否落在声明的范围内。

    只有**同一种形式**能比：URL 跟 URL 比前缀，路径跟路径比包含。形式不同的
    两边不可比 —— 不可比就意味着"证明不了落在范围内"，判为越界。
    """
    if form == "url":
        return any(_url_contains(allowed, value) for allowed in allowed_urls)
    return any(_path_contains(allowed, value) for allowed in allowed_paths)


def validate_equivalence(
    parsed: ParsedPlan,
    plan_json: Mapping[str, Any],
) -> List[Divergence]:
    """把机器格式与审核过的计划书逐条比对，返回全部差异。

    四个维度 —— 审核的四项实质内容：

    * ``points`` —— 验证点**id 集合**。机器格式少一条、多一条、改了名（id
      变了）、或者同一条给了两遍，都是漂移；
    * ``criteria`` —— **判据编号**。机器格式引用的 C 编号必须与计划书声明的
      是同一批：多一个就是在验没被审核过的事，少一个就是有判据没人验；
    * ``scope`` —— **执行范围**。执行位置（URL / 命令里的路径）必须落在计划书
      声明的范围内，且不得命中排除范围；
    * ``evidence`` —— 证据编号。机器格式必须引用计划书声明过的**每一个** E
      编号；多引用一个计划书里没有的 E，同样是漂移。

    三条边界：

    * **顺序不算差异** —— 验证点换顺序、E 编号换顺序、C 编号换顺序，都不产生
      差异（集合相等即可）；"顺序变了"看起来像漂移，但漂移的是**验过什么**，
      不是**按什么顺序验**；
    * **重复 id 是失败** —— 同一批验证点里出现两个同 id 的条目，比对本身没有
      意义（不知道该拿哪一个去比），所以它是差异而不是"取第一条就算了"；这类
      验证点的其余维度也不比 —— 比了也是拿随便一条去比；
    * **判据落在断言名上** —— 断言名用 ``C<n>: <正文>`` 的形式指明它验的是哪
      条判据，所以"每个判据至少一条断言"是可以机械核对的；缺前缀的断言照样
      报出来（它证明不了自己验的是哪条判据）。

    比对**只按 id 集合、C 编号集合、E 编号集合与执行位置**做，标题、判据正文
    这类内容不在这里判：标题与判据是别的维度的地盘，混进来会让"坏了一个维度"
    报出另一个维度的差异。

    Args:
        parsed: 计划书的解析产物 —— 比对的基准（人工审核过的唯一对象）。
        plan_json: 机器格式的验证计划（``{"verification_points": [...]}``）。

    Returns:
        差异列表。**空列表 = 逐条相等**；非空列表意味着"执行的就不是审过的"，
        调用方据此**不许落盘**。

    Raises:
        TypeError: ``parsed`` 不是 :class:`ParsedPlan`，或 ``plan_json`` 不是
            映射。这两个是调用方的 bug，不是"两份产物对不上" —— 对不上是上面
            那份列表里的事。
    """
    if not isinstance(parsed, ParsedPlan):
        raise TypeError(f"parsed 不是 ParsedPlan：{type(parsed).__name__}")
    if not isinstance(plan_json, Mapping):
        raise TypeError(f"plan_json 不是映射：{type(plan_json).__name__}")

    divergences: List[Divergence] = []

    doc_items = list(parsed.items)
    doc_ids = [item.id for item in doc_items]

    raw_points = plan_json.get("verification_points")
    if not isinstance(raw_points, list):
        return [
            Divergence(
                dimension="points",
                item_id="",
                detail="no verification_points array",
            )
        ]

    plan_ids: List[str] = []
    plan_by_id: Dict[str, Mapping[str, Any]] = {}
    for entry in raw_points:
        if not isinstance(entry, Mapping):
            divergences.append(
                Divergence(
                    dimension="points",
                    item_id="",
                    detail="verification point is not an object",
                )
            )
            continue
        raw_id = entry.get("id")
        vp_id = raw_id.strip() if isinstance(raw_id, str) else ""
        if not vp_id:
            divergences.append(
                Divergence(
                    dimension="points",
                    item_id="",
                    detail="verification point without id",
                )
            )
            continue
        plan_ids.append(vp_id)
        # 重复 id 也要留第一条 —— 重复本身是差异，不能让比对悄悄换个对象。
        plan_by_id.setdefault(vp_id, entry)

    doc_id_set = set(doc_ids)
    plan_id_set = set(plan_ids)

    # 重复 id：同一批验证点给了两遍 → 不知道该拿哪一条去比。
    plan_counts = Counter(plan_ids)
    doc_counts = Counter(doc_ids)
    ambiguous = {
        vp_id for vp_id, count in plan_counts.items() if count > 1
    } | {vp_id for vp_id, count in doc_counts.items() if count > 1}
    for vp_id in sorted(ambiguous):
        detail = (
            "duplicate VP id"
            if plan_counts.get(vp_id, 0) > 1
            else "duplicate VP id in document"
        )
        divergences.append(
            Divergence(dimension="points", item_id=vp_id, detail=detail)
        )

    # 少了一条 / 多了一条。
    for vp_id in doc_ids:
        if vp_id not in plan_id_set:
            divergences.append(
                Divergence(dimension="points", item_id=vp_id, detail="missing VP")
            )
    for vp_id in plan_ids:
        if vp_id not in doc_id_set:
            divergences.append(
                Divergence(dimension="points", item_id=vp_id, detail="added VP")
            )

    # 证据编号：只在两边都有、且 id 不重复的验证点上比。
    for item in doc_items:
        entry = plan_by_id.get(item.id)
        if entry is None or item.id in ambiguous:
            continue
        want = set(item.evidence.keys())
        given = _evidence_refs(entry.get("expected_result"))
        for evidence_id in sorted(want - given):
            divergences.append(
                Divergence(
                    dimension="evidence",
                    item_id=item.id,
                    detail=f"{evidence_id} not referenced",
                )
            )
        for evidence_id in sorted(given - want):
            divergences.append(
                Divergence(
                    dimension="evidence",
                    item_id=item.id,
                    detail=f"{evidence_id} not declared in document",
                )
            )

    # 判据编号：机器格式引用的 C 编号必须与计划书声明的**同一批**。
    #
    # 引用集合来自两处：``related_prd_criteria``（机器格式自己声称验的判据）
    # 与断言名上的 ``C<n>: `` 前缀（这条断言验的是哪条判据）。两处合起来才是
    # "这条验证点到底声称验了哪些判据"。
    #
    # 机器格式既没提判据也没给断言时**不比** —— 那是"没东西可比"，不是"没问题"。
    for item in doc_items:
        entry = plan_by_id.get(item.id)
        if entry is None or item.id in ambiguous:
            continue
        declared = set(item.criteria.keys())
        claim = _criterion_ids(entry.get("related_prd_criteria"))
        raw_assertions = entry.get("assertions")
        assertions = raw_assertions if isinstance(raw_assertions, list) else []
        assertion_ids, missing_prefix = _assertion_criteria(assertions)
        if not claim and not assertions:
            continue
        referenced = claim | assertion_ids
        for criterion_id in sorted(declared - referenced):
            divergences.append(
                Divergence(
                    dimension="criteria",
                    item_id=item.id,
                    detail=f"{criterion_id} not covered",
                )
            )
        for criterion_id in sorted(referenced - declared):
            divergences.append(
                Divergence(
                    dimension="criteria",
                    item_id=item.id,
                    detail=f"{criterion_id} not in plan",
                )
            )
        for name in missing_prefix:
            divergences.append(
                Divergence(
                    dimension="criteria",
                    item_id=item.id,
                    detail=f"assertion without Cx prefix: {name}",
                )
            )

    # 执行范围：执行位置必须落在声明的范围内，且不得命中排除范围。
    #
    # 范围内 = 计划级「验证范围」+ 这条验证点自己的「范围」；排除范围 =
    # 计划级「排除范围」。两者都是人写的自由文本，所以先把里面的位置（URL
    # 前缀 / 仓库相对路径）摘出来再比。
    scope_urls, scope_paths = _scope_targets(_section_body(parsed, SCOPE_SECTION))
    excluded_urls, excluded_paths = _scope_targets(
        _section_body(parsed, EXCLUSION_SECTION)
    )
    for item in doc_items:
        entry = plan_by_id.get(item.id)
        if entry is None or item.id in ambiguous:
            continue
        item_urls, item_paths = _scope_targets(item.scope)
        allowed_urls = scope_urls + item_urls
        allowed_paths = scope_paths + item_paths
        if not allowed_urls and not allowed_paths:
            # 计划书没有声明任何范围 —— 无从判断执行位置是否越界。
            continue
        for field, form, value in _vp_locations(entry):
            if _hits_exclusion(form, value, excluded_urls, excluded_paths):
                divergences.append(
                    Divergence(
                        dimension="scope",
                        item_id=item.id,
                        detail=f"{field} in excluded scope",
                    )
                )
                continue
            if not _in_range(form, value, allowed_urls, allowed_paths):
                divergences.append(
                    Divergence(
                        dimension="scope",
                        item_id=item.id,
                        detail=f"{field} outside range",
                    )
                )

    return divergences


__all__ = [
    "ALLOWED_LLM_FIELDS",
    "PROTECTED_FIELDS",
    "Divergence",
    "TranslationError",
    "translate",
    "validate_equivalence",
]