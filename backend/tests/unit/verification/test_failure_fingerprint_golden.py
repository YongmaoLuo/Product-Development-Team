"""TDD spec — 失败指纹的金标准快照。

Background
----------
失败指纹是**纯函数**,所以它可以被钉死:给定一组固定输入,指纹必须是一个固定
的 16 位 hex。金标准文件的作用是「回归护栏」—— 归一化规则一旦被人改动
(多掩一层、少掩一层、换一个分隔符),指纹就会变,而这个测试会立刻变红。

金标准必须**跨机器可复算**
--------------------------
指纹的输入里如果混进了机器相关的东西(用户目录、临时根的具体拼写),那么
macOS 上算出来的金标准在 Linux CI 上就必然对不上,金标准文件本身就成了
一次性的假阳性。所以金标准里的输入只用**与机器无关**的相对路径。

golden 文件结构
--------------
``[{input, fingerprint}]`` —— 一个数组,每项是 ``{"input": {...},
"fingerprint": "<16 位小写 hex>"}``。``input`` 就是
``failure_fingerprint`` 的五个关键字参数。

TDD contract
------------
1. ``test_golden_snapshot`` —— 文件里每一项,重算的指纹都必须等于钉住的值。
2. ``test_golden_file_structure`` —— 文件结构必须是 ``[{input, fingerprint}]``,
   指纹必须是 16 位小写 hex。
3. ``test_golden_covers_the_documented_cases`` —— 金标准至少覆盖:一次正常失败、
   一次全字段缺失、一次带十六进制地址的失败。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from verification.failure_fingerprint import failure_fingerprint  # noqa: E402

#: The golden file. Its location is fixed by the task brief:
#: ``backend/tests/fixtures/failure_history/fingerprint_golden.json``.
GOLDEN_PATH = (
    BACKEND_DIR / "tests" / "fixtures" / "failure_history" / "fingerprint_golden.json"
)

#: The five keyword arguments of ``failure_fingerprint`` — the golden file's
#: ``input`` object must have exactly these keys.
INPUT_KEYS = frozenset(
    {
        "vp_id",
        "failure_class",
        "failed_assertions",
        "command",
        "first_error_line",
    }
)

_HEX_DIGITS = frozenset("0123456789abcdef")


def _load_golden() -> list:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 1. the snapshot
# ---------------------------------------------------------------------------
def test_golden_snapshot():
    """文件里每一项,重算的指纹都必须等于钉住的值。"""
    entries = _load_golden()
    assert entries, "the golden file must not be empty"
    for entry in entries:
        expected = entry["fingerprint"]
        actual = failure_fingerprint(**entry["input"])
        assert actual == expected, (
            f"the fingerprint of {entry['input']!r} changed: "
            f"{actual!r} != {expected!r}. Either the normalisation rule was "
            f"edited (and the golden must be regenerated on purpose) or the "
            f"fingerprint stopped being deterministic."
        )


# ---------------------------------------------------------------------------
# 2. the file's shape
# ---------------------------------------------------------------------------
def test_golden_file_structure():
    """结构必须是 ``[{input, fingerprint}]``,指纹必须是 16 位小写 hex。"""
    entries = _load_golden()
    assert isinstance(entries, list), type(entries)
    for entry in entries:
        assert isinstance(entry, dict), entry
        assert set(entry) == {"input", "fingerprint"}, entry
        assert isinstance(entry["input"], dict), entry
        assert set(entry["input"]) == INPUT_KEYS, entry
        fingerprint = entry["fingerprint"]
        assert isinstance(fingerprint, str), entry
        assert len(fingerprint) == 16, entry
        assert set(fingerprint) <= _HEX_DIGITS, entry


# ---------------------------------------------------------------------------
# 3. coverage
# ---------------------------------------------------------------------------
def test_golden_covers_the_documented_cases():
    """金标准至少覆盖:正常失败 / 全字段缺失 / 带十六进制地址的失败。"""
    entries = _load_golden()
    assert len(entries) >= 3, entries

    all_missing = [
        entry
        for entry in entries
        if all(value is None for value in entry["input"].values())
    ]
    assert all_missing, "the golden must pin the all-fields-missing case"

    with_hex = [
        entry
        for entry in entries
        if "0x" in json.dumps(entry["input"])
    ]
    assert with_hex, "the golden must pin a failure carrying a hex address"

    complete = [
        entry
        for entry in entries
        if entry["input"]["vp_id"] and entry["input"]["failure_class"]
    ]
    assert complete, "the golden must pin a fully-populated failure"


# ---------------------------------------------------------------------------
# maintenance: regenerate on purpose
# ---------------------------------------------------------------------------
def regenerate_golden(path: Path = GOLDEN_PATH) -> list:
    """Rewrite the golden file from the current implementation.

    Only for an intentional change of the normalisation rule: run it, then
    read the diff — the whole point of the golden file is that a changed
    fingerprint has to be *seen*, not silently re-blessed.
    """
    from verification.failure_fingerprint import canonical_fingerprint_input

    inputs = [
        {
            "vp_id": "VP-3",
            "failure_class": "deliverable_defect",
            "failed_assertions": ["assert json_path", "assert status"],
            "command": "pytest tests/x.py",
            "first_error_line": "AssertionError at /tmp/a.py:12",
        },
        {
            "vp_id": None,
            "failure_class": None,
            "failed_assertions": None,
            "command": None,
            "first_error_line": None,
        },
        {
            "vp_id": "VP-9",
            "failure_class": "criterion_defect",
            "failed_assertions": ["assert status"],
            "command": "pytest tests/unit/test_y.py",
            "first_error_line": "ValueError at 0x7f3c9a21 in backend/app.py",
        },
    ]
    entries = [
        {
            "input": payload,
            "fingerprint": failure_fingerprint(**payload),
        }
        for payload in inputs
    ]
    # The canonical input is computed here too, so a regeneration that
    # cannot even build the hashed input fails loudly instead of writing
    # a golden file full of wrong fingerprints.
    for payload in inputs:
        canonical_fingerprint_input(**payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(entries, indent=2, ensure_ascii=False, sort_keys=False) + "\n",
        encoding="utf-8",
    )
    return entries


if __name__ == "__main__":
    regenerate_golden()
    print(f"regenerated {GOLDEN_PATH}")
