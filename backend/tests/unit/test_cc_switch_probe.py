"""The CC Switch probe must answer, never raise, and never guess.

Why this exists
---------------
This project reads CC Switch's **data contract** — a SQLite table named
``providers`` with a known set of columns — rather than importing any of
its code. That is what makes it work against upstream CC Switch *and* a
compatible fork; it is also what makes the contract worth checking.

Every column the probe requires is stock upstream schema (verified
2026-09-24 against farion1231/cc-switch ``main``), so a normal install
satisfies it. The probe earns its keep on the two cases a plain read
cannot tell apart:

* **CC Switch is not installed.** This is a supported state, not an
  error — it means providers are inherited from Claude Code's own
  configuration. Code that cannot distinguish it from breakage will
  either crash or, worse, silently pick the wrong branch.
* **CC Switch is installed but its schema is not what we read.** A
  fork that moved a column, a half-finished upgrade, a file that is not
  a CC Switch database at all. Without a probe, this surfaces as
  ``sqlite3.OperationalError: no such column`` raised from inside a
  dispatch, which tells an operator nothing actionable.

Hence the two properties under test: the probe **never raises** (a
question that can throw forces callers to collapse the cases above into
one ``except``), and it always says *what to do* about a negative.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[2]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import cc_switch  # noqa: E402

#: The stock upstream ``providers`` schema, trimmed to the columns this
#: project reads plus one irrelevant column so a "missing column" test
#: cannot pass merely by the table being small.
_REAL_COLUMNS = """
    id TEXT NOT NULL,
    app_type TEXT NOT NULL,
    name TEXT NOT NULL,
    settings_config TEXT NOT NULL,
    is_current BOOLEAN NOT NULL DEFAULT 0,
    notes TEXT,
    PRIMARY KEY (id, app_type)
"""


def _make_db(path: Path, columns: str = _REAL_COLUMNS, rows: int = 0) -> Path:
    conn = sqlite3.connect(str(path))
    try:
        conn.execute(f"CREATE TABLE providers ({columns})")
        for i in range(rows):
            conn.execute(
                "INSERT INTO providers (id, app_type, name, settings_config)"
                " VALUES (?, 'claude', ?, '{}')",
                (f"prov-{i}", f"Provider {i}"),
            )
        conn.commit()
    finally:
        conn.close()
    return path


class TestItSaysYesWhenItShould:
    def test_a_stock_database_is_available(self, tmp_path):
        db = _make_db(tmp_path / "cc-switch.db", rows=3)
        status = cc_switch.probe(db)
        assert status.available is True
        assert status.provider_count == 3
        assert status.path == db

    def test_an_empty_but_valid_database_is_still_available(self, tmp_path):
        """Zero rows is a real state — CC Switch installed, nothing
        configured yet. Conflating it with "not installed" would send the
        user down the inherit path with no way to tell why."""
        db = _make_db(tmp_path / "cc-switch.db", rows=0)
        status = cc_switch.probe(db)
        assert status.available is True
        assert status.provider_count == 0


class TestItSaysNoClearly:
    def test_a_missing_database_names_the_path_and_the_way_out(self, tmp_path):
        missing = tmp_path / "nope.db"
        status = cc_switch.probe(missing)
        assert status.available is False
        assert str(missing) in status.detail
        assert "PDT_CC_SWITCH_DB" in status.remedy

    def test_a_file_that_is_not_a_database_is_reported_not_raised(self, tmp_path):
        junk = tmp_path / "cc-switch.db"
        junk.write_text("this is not sqlite", encoding="utf-8")
        status = cc_switch.probe(junk)
        assert status.available is False
        assert status.remedy

    def test_a_database_without_the_providers_table_is_reported(self, tmp_path):
        db = tmp_path / "cc-switch.db"
        conn = sqlite3.connect(str(db))
        conn.execute("CREATE TABLE something_else (id TEXT)")
        conn.commit()
        conn.close()

        status = cc_switch.probe(db)
        assert status.available is False
        assert "providers" in status.detail

    def test_a_missing_column_is_named_individually(self, tmp_path):
        """The failure mode this probe exists for.

        Naming *which* column is gone is the difference between "upgrade
        CC Switch" and "your fork dropped ``is_current``" — and the
        second is the one an operator can act on.
        """
        db = _make_db(
            tmp_path / "cc-switch.db",
            columns="""
                id TEXT NOT NULL,
                app_type TEXT NOT NULL,
                settings_config TEXT NOT NULL
            """,
        )
        status = cc_switch.probe(db)
        assert status.available is False
        assert "['name', 'is_current']" in status.detail, (
            f"the detail must name exactly the columns that are gone, "
            f"not all of them and not none; got {status.detail!r}"
        )


class TestItNeverRaises:
    """A probe that throws is not a probe.

    If it can raise, every caller collapses "absent" and "broken" into a
    single ``except`` — and those two demand opposite responses.
    """

    @pytest.mark.parametrize(
        "make",
        [
            lambda p: p / "does-not-exist.db",
            lambda p: _write(p, ""),
            lambda p: _write(p, "not a database at all"),
            lambda p: _write(p, "\x00\x01\x02\x03"),
            lambda p: _empty_sqlite(p),
            lambda p: p,  # a directory where a file is expected
        ],
    )
    def test_every_pathological_input_returns_a_status(self, tmp_path, make):
        status = cc_switch.probe(make(tmp_path / "cc-switch.db"))
        assert status.available is False
        assert status.detail, "a negative must say what was wrong"
        assert status.remedy, "a negative must say what to do about it"


def _write(path: Path, text: str) -> Path:
    path.write_bytes(text.encode("utf-8", "replace"))
    return path


def _empty_sqlite(path: Path) -> Path:
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE t (x TEXT)")
    conn.commit()
    conn.close()
    return path


class TestPathResolution:
    def test_the_explicit_argument_wins(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PDT_CC_SWITCH_DB", str(tmp_path / "env.db"))
        explicit = tmp_path / "explicit.db"
        assert cc_switch.resolve_db_path(explicit) == explicit

    def test_the_env_override_is_honoured(self, tmp_path, monkeypatch):
        """CC Switch is a separate application. Assuming it always lands
        in one place per user is the kind of assumption that breaks
        quietly on somebody else's machine."""
        target = tmp_path / "elsewhere" / "cc-switch.db"
        monkeypatch.setenv("PDT_CC_SWITCH_DB", str(target))
        assert cc_switch.resolve_db_path() == target

    def test_the_default_follows_home_at_call_time(self, tmp_path, monkeypatch):
        """Not an import-time snapshot: redirecting HOME must take effect,
        which is what lets a test simulate "no CC Switch installed"."""
        monkeypatch.delenv("PDT_CC_SWITCH_DB", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cc_switch.resolve_db_path() == (
            tmp_path / ".cc-switch" / "cc-switch.db"
        )

    def test_a_tilde_in_the_override_is_expanded(self, monkeypatch):
        monkeypatch.setenv("PDT_CC_SWITCH_DB", "~/custom-cc-switch.db")
        assert "~" not in str(cc_switch.resolve_db_path())


class TestCandidateLocations:
    """CC Switch is a separate application; where it keeps its data
    depends on the build and how it was installed.

    The app has plainly used more than one location: a machine can carry
    several candidate files at once, most of them zero bytes. A single
    hard-coded path would report "CC Switch is not installed" on a
    machine that has it, and silently send
    the user down the inherit path.
    """

    def test_the_documented_location_comes_first(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert cc_switch.candidate_db_paths()[0] == (
            tmp_path / ".cc-switch" / "cc-switch.db"
        )

    def test_platform_conventions_are_covered(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        rendered = [str(p) for p in cc_switch.candidate_db_paths()]
        assert any("Application Support" in p for p in rendered), (
            "macOS app-support is where a Tauri/Electron build keeps its "
            "data; the bundle-id directory exists on a real machine"
        )
        assert any(".config" in p for p in rendered), (
            "the XDG location is the convention on Linux builds"
        )

    def test_a_zero_byte_leftover_is_not_a_working_install(
        self, tmp_path, monkeypatch
    ):
        """SQLite opens a zero-byte file happily as an empty database, so
        "the file exists" is not the question worth asking."""
        monkeypatch.setenv("HOME", str(tmp_path))
        primary = tmp_path / ".cc-switch" / "cc-switch.db"
        primary.parent.mkdir(parents=True)
        primary.write_bytes(b"")

        status = cc_switch.probe()
        assert status.available is False
        assert "zero-byte" in status.detail, (
            f"a zero-byte candidate must be reported as such; got "
            f"{status.detail!r}"
        )

    def test_the_probe_skips_an_unusable_candidate_and_keeps_looking(
        self, tmp_path, monkeypatch
    ):
        """The behaviour the candidate list exists for.

        This is the real-machine shape: the documented path is present
        but empty, and the usable database is somewhere else.
        """
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PDT_CC_SWITCH_DB", raising=False)

        leftover = tmp_path / ".cc-switch" / "cc-switch.db"
        leftover.parent.mkdir(parents=True)
        leftover.write_bytes(b"")

        usable = tmp_path / ".config" / "cc-switch" / "cc-switch.db"
        usable.parent.mkdir(parents=True)
        _make_db(usable, rows=2)

        status = cc_switch.probe()
        assert status.available is True
        assert status.path == usable
        assert status.provider_count == 2

    def test_a_named_override_is_authoritative(self, tmp_path, monkeypatch):
        """Setting ``PDT_CC_SWITCH_DB`` and silently reading a *different*
        database would be worse than an error, so a named path is the
        only one probed."""
        monkeypatch.setenv("HOME", str(tmp_path))
        _make_db(
            _mkparent(tmp_path / ".cc-switch" / "cc-switch.db"), rows=1
        )
        typo = tmp_path / "typo.db"
        monkeypatch.setenv("PDT_CC_SWITCH_DB", str(typo))

        status = cc_switch.probe()
        assert status.available is False
        assert status.path == typo
        assert "typo.db" in status.detail

    def test_a_failure_names_every_candidate_it_tried(self, tmp_path, monkeypatch):
        """When nothing works, the operator needs the list — otherwise
        "not installed" is indistinguishable from "installed somewhere I
        did not look"."""
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.delenv("PDT_CC_SWITCH_DB", raising=False)

        status = cc_switch.probe()
        assert status.available is False
        assert "no usable CC Switch database" in status.detail
        for path in cc_switch.candidate_db_paths():
            assert str(path) in status.detail


def _mkparent(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def test_no_url_map_reference():
    """``cc_switch`` must stay free of the legacy provider URL map.

    This is the last survivor of ``tests/unit/test_cc_switch_db.py``,
    which was deleted 2026-09-24 along with the id-keyed reader it
    covered.  The gate is about a *file*, not about that reader, so it
    moved here rather than going with it.
    """
    source = Path(__file__).parent.parent.parent / "cc_switch.py"
    text = source.read_text(encoding="utf-8")
    legacy_token = "provider" + "-" + "url" + "-map"
    assert legacy_token not in text
