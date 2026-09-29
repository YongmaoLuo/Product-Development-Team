"""Unit tests for backend.utils.check_path.is_safe_dir_name."""

from backend.utils.check_path import is_safe_dir_name


# --- AC-1: a plain, safe directory name should pass ---
def test_my_folder_is_safe() -> None:
    assert is_safe_dir_name("my-folder") is True


# --- AC-2: an empty string is unsafe ---
def test_empty_string_is_unsafe() -> None:
    assert is_safe_dir_name("") is False


# --- AC-3: whitespace-only (incl. tab/newline) is unsafe ---
def test_whitespace_only_is_unsafe() -> None:
    assert is_safe_dir_name("   ") is False
    assert is_safe_dir_name("\t") is False
    assert is_safe_dir_name("\n") is False
    assert is_safe_dir_name(" \t\n ") is False


# --- AC-4: parent-directory traversal is unsafe ---
def test_parent_dir_traversal_is_unsafe() -> None:
    assert is_safe_dir_name("../etc") is False
    assert is_safe_dir_name("..") is False
    assert is_safe_dir_name("a/..") is False


# --- AC-5: a forward slash anywhere is unsafe ---
def test_forward_slash_is_unsafe() -> None:
    assert is_safe_dir_name("a/b") is False
    assert is_safe_dir_name("/") is False
    assert is_safe_dir_name("/leading") is False
    assert is_safe_dir_name("trailing/") is False


# --- AC-6: a backslash anywhere is unsafe ---
def test_backslash_is_unsafe() -> None:
    assert is_safe_dir_name("a\\b") is False
    assert is_safe_dir_name("\\") is False
    assert is_safe_dir_name("\\leading") is False
    assert is_safe_dir_name("trailing\\") is False


# --- AC-7: a null byte anywhere is unsafe ---
def test_null_byte_is_unsafe() -> None:
    assert is_safe_dir_name("a\x00b") is False
    assert is_safe_dir_name("\x00") is False


# --- DP index 2: non-string inputs must return False (no TypeError) ---
def test_non_string_returns_false() -> None:
    assert is_safe_dir_name(None) is False  # type: ignore[arg-type]
    assert is_safe_dir_name(123) is False  # type: ignore[arg-type]
    assert is_safe_dir_name(b"my-folder") is False  # type: ignore[arg-type]
