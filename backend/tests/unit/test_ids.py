"""TDD spec for :mod:`framework.ids` (architecture decision point 8).

Covers the contract points for :func:`validate_plan_id` and the
:class:`InvalidPlanIdError` exception it raises:

1. Valid plan ids (the byte set ``[A-Za-z0-9._-]``) round-trip unchanged.
2. Empty string is rejected.
3. Non-string inputs (None, int, bytes, list, dict, etc.) are rejected.
4. Absolute paths (POSIX ``/`` and Windows ``C:\\``) are rejected.
5. Path separators (both ``/`` and ``\\``) are rejected.
6. Path traversal (``..`` and bare ``.``) is rejected.
7. NUL and other control characters are rejected.
8. Whitespace / shell-metacharacter characters are rejected.
9. Length cap (``MAX_PLAN_ID_LEN = 64``) is exact: ids at the boundary
   round-trip, ids past the boundary are rejected.
10. :class:`InvalidPlanIdError` is a subclass of :class:`ValueError` so
    legacy ``except ValueError`` callers continue to work.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from framework.ids import (
    InvalidPlanIdError,
    MAX_PLAN_ID_LEN,
    derive_plan_id,
    slugify_plan_id,
    validate_plan_id,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Happy path: valid plan ids round-trip unchanged.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "plan_id",
    [
        "20260424-task-app",
        "20260618-test",
        "abc",
        "A",
        "0",
        "1",
        "2024.05.01-feature_branch",
        "a-b-c",
        "a_b_c",
        "a.b.c",
        "X" * 8,
        "X" * MAX_PLAN_ID_LEN,
        "20260424" "-task_app.v2",
        "0-0",
        "1-1-1-1",
    ],
)
def test_valid_plan_id_round_trips(plan_id):
    """Every byte in the safe character set is accepted and returned as-is."""
    assert validate_plan_id(plan_id) == plan_id
    assert isinstance(validate_plan_id(plan_id), str)


def test_first_and_last_char_can_be_alnum():
    """Leading and trailing characters are not subject to extra restrictions."""
    assert validate_plan_id("a") == "a"
    assert validate_plan_id("3") == "3"
    assert validate_plan_id("20260424-task-app") == "20260424-task-app"
    assert validate_plan_id("a.") == "a."
    assert validate_plan_id("a_") == "a_"
    assert validate_plan_id("a-") == "a-"


def test_can_have_no_first_or_last_separator():
    """A single-component id like ``a.b.c`` is allowed; only multi-component
    or absolute ids are rejected."""
    assert validate_plan_id("a.b.c") == "a.b.c"
    assert validate_plan_id("a_b") == "a_b"
    assert validate_plan_id("a-b") == "a-b"


# ---------------------------------------------------------------------------
# Empty / non-string.
# ---------------------------------------------------------------------------


def test_empty_string_is_rejected():
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id("")


@pytest.mark.parametrize(
    "bad_input",
    [None, 0, 1, 42, True, False, 3.14, b"abc", bytearray(b"abc")],
)
def test_non_string_inputs_are_rejected(bad_input):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(bad_input)


@pytest.mark.parametrize(
    "bad_input",
    [[], ["a"], {}, {"a": "b"}, ("a",), set()],
)
def test_collection_inputs_are_rejected(bad_input):
    """Lists, tuples, dicts and sets are rejected even if they contain
    safe characters."""
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(bad_input)


# ---------------------------------------------------------------------------
# Absolute paths.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "absolute_path",
    [
        "/etc/passwd",
        "/",
        "/a",
        "/plans/secret",
        "//double",
    ],
)
def test_absolute_posix_paths_are_rejected(absolute_path):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(absolute_path)


@pytest.mark.parametrize(
    "windows_absolute",
    [
        "C:\\Windows\\System32",
        "C:/Windows",
        "z:\\foo",
        "D:test",
    ],
)
def test_absolute_windows_paths_are_rejected(windows_absolute):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(windows_absolute)


# ---------------------------------------------------------------------------
# Path separators.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "with_separator",
    [
        "a/b",
        "a/b/c",
        "2024/05/01-plan",
        "a\\b",
        "a\\b\\c",
        "2024\\05\\01-plan",
        "/",
        "\\",
    ],
)
def test_path_separators_are_rejected(with_separator):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(with_separator)


# ---------------------------------------------------------------------------
# Path traversal.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "traversal",
    [
        "..",
        "../etc",
        "..\\windows",
        "a/../b",
        "a/../../b",
        "a\\..\\b",
        ".",
    ],
)
def test_traversal_components_are_rejected(traversal):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(traversal)


# ---------------------------------------------------------------------------
# NUL / control characters.
# ---------------------------------------------------------------------------


def test_nul_character_is_rejected():
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id("abc\x00def")


def test_nul_character_at_end_is_rejected():
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id("plan\0")


def test_nul_character_alone_is_rejected():
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id("\x00")


@pytest.mark.parametrize(
    "control_char",
    [
        "\x01",  # SOH
        "\x02",  # STX
        "\x07",  # BEL
        "\x08",  # BS
        "\x0b",  # VT
        "\x0c",  # FF
        "\x0e",  # SO
        "\x1f",  # unit separator
        "\x7f",  # DEL
        "\r",    # carriage return
        "\n",    # newline
        "\t",    # tab
    ],
)
def test_other_control_characters_are_rejected(control_char):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(f"plan{control_char}id")


# ---------------------------------------------------------------------------
# Whitespace / shell metacharacters.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "whitespace",
    [" ", "  ", "a b", "a\tb", "a\nb", "a\rb"],
)
def test_whitespace_is_rejected(whitespace):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(whitespace)


@pytest.mark.parametrize(
    "shell_meta",
    [
        "a;b",
        "a&b",
        "a|b",
        "a$b",
        "a`b",
        "a>b",
        "a<b",
        "a*b",
        "a?b",
        "a(b",
        "a)b",
        "a[b",
        "a]b",
        "a{b",
        "a}b",
        "a'b",
        'a"b',
        "a!b",
        "a~b",
        "a#b",
        "a%b",
        "a=b",
        "a+b",
        "a,b",
        "a:b",
        "a@b",
        "a^b",
    ],
)
def test_shell_metacharacters_are_rejected(shell_meta):
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(shell_meta)


# ---------------------------------------------------------------------------
# Exception class contract.
# ---------------------------------------------------------------------------


def test_invalid_plan_id_error_is_value_error():
    """InvalidPlanIdError must be a subclass of ValueError so legacy
    ``except ValueError`` callers continue to work."""
    assert issubclass(InvalidPlanIdError, ValueError)


def test_invalid_plan_id_error_can_be_caught_as_value_error():
    """Calls that already catch ValueError for general input validation
    must continue to work."""
    with pytest.raises(ValueError):
        validate_plan_id("")


def test_invalid_plan_id_error_carries_message():
    """The raised exception must carry a human-readable message that
    references the offending input."""
    with pytest.raises(InvalidPlanIdError) as excinfo:
        validate_plan_id("../etc")
    message = str(excinfo.value)
    assert message  # non-empty
    assert "../etc" in message or ".." in message


# ---------------------------------------------------------------------------
# Length / boundary.
# ---------------------------------------------------------------------------


def test_extreme_length_is_rejected():
    """Ids past :data:`MAX_PLAN_ID_LEN` are rejected even when every byte
    is in the safe set.

    Pins the length cap from the *rejection* side so a future bump of
    the constant only needs the matching acceptance-side test
    (``test_plan_id_boundary_matrix.py::test_max_len_boundary_is_exact``)
    to be updated; the unit layer here keeps the constant under test
    from creeping back toward "any length is accepted".
    """
    for n in (MAX_PLAN_ID_LEN + 1, MAX_PLAN_ID_LEN + 100):
        with pytest.raises(InvalidPlanIdError):
            validate_plan_id("a" * n)


def test_single_character_alnum_is_accepted():
    assert validate_plan_id("a") == "a"
    assert validate_plan_id("Z") == "Z"
    assert validate_plan_id("0") == "0"
    assert validate_plan_id("9") == "9"


def test_leading_dot_is_accepted():
    """``.foo`` is a hidden-file-style name but is still a safe single
    component. We only reject bare ``.`` and ``..`` as traversal."""
    assert validate_plan_id(".foo") == ".foo"


def test_trailing_dot_is_accepted():
    assert validate_plan_id("foo.") == "foo."


def test_only_dots_combination_except_bare():
    """Only bare ``.`` and ``..`` are rejected — anything else with
    dots in the middle is allowed."""
    assert validate_plan_id("a..b") == "a..b"


# ---------------------------------------------------------------------------
# Unicode / non-ASCII.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "unicode_input",
    [
        "中文-plan",
        "café",
        "naïve",
        "🚀-plan",
        "α-β",
        "test_ü",
    ],
)
def test_non_ascii_unicode_is_rejected(unicode_input):
    """Non-ASCII characters are unsafe for filesystem portability and
    must be rejected."""
    with pytest.raises(InvalidPlanIdError):
        validate_plan_id(unicode_input)


# ---------------------------------------------------------------------------
# slugify_plan_id (2026-09-14)
#
# Plan ids are meant to be "date prefix + slug", and every code path
# that touches ``plans/{plan_id}`` funnels through ``validate_plan_id``
# — but the creator in ``server.py`` only replaced SPACES. A CJK
# requirement therefore produced ``2026-09-04 plan``,
# a directory name the project's own validator rejects. Consequence
# observed: the dispatcher's watchdog signal write raised, the old
# ``_write_dispatcher_signal`` swallowed it, and a stranded plan
# produced no signal file and no alert.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "payment-gateway 支付网关重构 + 架构 rev",
        "café au lait",
        "🚀 launch the rocket",
        "a//b",
        "   ",
        "..",
        ".",
    ],
)
def test_slugify_output_always_satisfies_validate_plan_id(raw):
    """Whatever the input, the slug must be usable as a plan-id fragment."""
    slug = slugify_plan_id(raw)
    assert slug, f"slugify must never return an empty slug for {raw!r}"
    validate_plan_id(f"20260101-{slug}")  # must not raise


def test_slugify_collapses_unsafe_runs_and_strips_edges():
    assert slugify_plan_id("payment-gateway 支付网关重构 + 架构 rev") == "payment-gateway-rev"


def test_slugify_keeps_safe_bytes_verbatim():
    assert slugify_plan_id("task-app_v2.1") == "task-app_v2.1"


def test_slugify_truncates_to_max_len():
    assert slugify_plan_id("abcdefghij klmnopqrst uvwxyz", max_len=12) == "abcdefghij-k"


def test_slugify_truncation_does_not_leave_a_trailing_dash():
    """The cut lands exactly on the separator — it must be stripped.

    Otherwise the id would end in ``-`` and, worse, a longer run of
    unsafe bytes could be truncated back to a bare ``-``.
    """
    slug = slugify_plan_id("abcdefghijk lmno", max_len=12)
    assert slug == "abcdefghijk"
    assert not slug.endswith("-")


def test_slugify_falls_back_to_a_digest_when_nothing_is_usable():
    """An all-CJK requirement has no usable bytes.

    Returning "" would make every such plan on the same day collapse
    onto one directory, so a short digest of the input is used
    instead — and it must be stable across calls.
    """
    slug = slugify_plan_id("仓工程化基线对齐")
    assert slug
    assert slug == slugify_plan_id("仓工程化基线对齐")
    assert slug != slugify_plan_id("完全不同的需求")
    validate_plan_id(f"20260101-{slug}")


def test_slugify_digest_is_reachable_only_for_unusable_text():
    """Readable text never degrades to the digest form."""
    assert len(slugify_plan_id("normal requirement")) > 0
    assert slugify_plan_id("normal requirement") == "normal-requirement"


# ---------------------------------------------------------------------------
# derive_plan_id — the auto-generated id used by ``POST /api/plans``
# ---------------------------------------------------------------------------


def test_derive_plan_id_is_date_plus_safe_slug():
    when = datetime(2026, 1, 1, 12, 0, 0)
    assert derive_plan_id("payment-gateway 支付网关重构 + 架构 rev", when=when) == (
        "20260101-payment-gateway-rev"
    )


@pytest.mark.parametrize(
    "requirement",
    [
        "payment-gateway 支付网关重构 + 架构 rev",
        "café au lait",
        "🚀 launch",
        "a/b/c",
        "   padded   ",
        "normal task app",
    ],
)
def test_derive_plan_id_is_always_a_valid_plan_id(requirement):
    """The one property that matters: the derived id must be usable.

    Regression guard for the 2026-09-14 stall — the previous inline
    derivation only replaced spaces, so a CJK requirement produced an
    id that ``validate_plan_id`` rejects, and every guarded code path
    (notably the dispatcher's watchdog signal) silently gave up.
    """
    plan_id = derive_plan_id(requirement)
    assert validate_plan_id(plan_id) == plan_id
