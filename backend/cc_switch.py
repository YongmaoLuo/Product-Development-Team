"""Everything this project knows about CC Switch.

CC Switch is a separate desktop application that owns the provider
credentials and the routing decision. This module is the **only** place
that knows where it keeps its files and how to read them; every other
module asks here rather than joining ``~/.cc-switch`` or opening a
connection itself. Before that was true, five call sites resolved the
database path their own way and only one honoured an override — so a
relocated install had the probe report one file while the reader opened
another — and a provider row's JSON was parsed by three functions that
disagreed about which key held the API key.

Three things live on disk, and this module owns all of them:

``cc-switch.db``
    The SQLite store. ``providers.name`` **is** a provider's identity:
    the string ``provider_routing.yaml`` matches its regexes against and
    ``provider-order.json`` carries. Each row's ``settings_config`` is
    JSON whose ``env`` block holds ``ANTHROPIC_BASE_URL`` and the token.

``settings.json``
    CC Switch's own current-selection pointer (``currentProviderClaude``),
    read by :func:`current_provider`.

``proxy_request_logs``
    The ledger of every LLM call, in the same database. :mod:`plan_usage`
    reads it through :func:`snapshot` rather than opening the file.

Every read is read-only, through a ``mode=ro`` URI. This project must
never write to another application's database.

Two on-disk table layouts answer the same questions — the modern
``providers`` table and the legacy ``provider_configs`` table — and
they describe a provider's endpoint and model in different places. That
difference stops at this module's edge: every reader here returns a
:class:`ProviderConfig`, which projects both layouts onto one set of
names. A consumer reads ``.base_url`` without knowing which table
answered, which is what keeps the endpoint and credential rules in one
place instead of one copy per caller.

History: this module was named ``cc_switch_db`` (too narrow once it also
owned ``settings.json`` and the ledger snapshot). It carried a second,
id-keyed provider reader named ``provider_config_consumer``, keyed on a
locally-invented kebab-case id; that reader was merged in and deleted
2026-09-24, and a dead id-keyed reader inside this module went the same
way — it had no production caller and read a shape the live database
does not contain. The dict-shaped return values those readers exposed
were replaced by :class:`ProviderConfig` on 2026-09-25.
"""

import json
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Mapping, Optional, Union


class CCSwitchError(Exception):
    """Raised when CC Switch cannot be read.

    Covers "there is no database here" and "there is one but it cannot be
    opened or parsed" alike, because callers that degrade gracefully want
    to treat both the same way. Callers that must tell them apart catch
    :class:`CCSwitchDBNotFoundError`, which is the first case.
    """


class CCSwitchDBNotFoundError(CCSwitchError):
    """Raised when the CC Switch database file is missing or unopenable."""


def _default_db_path() -> Path:
    """The documented CC Switch database location."""
    return Path.home() / ".cc-switch" / "cc-switch.db"


#: Environment variables that name a database path, highest priority
#: first. ``PDT_CC_SWITCH_DB`` is the documented one and follows the
#: ``PDT_`` convention every other resolver in this project uses.
#:
#: ``CC_SWITCH_DB`` is a compatibility alias, not a second mechanism.
#: :mod:`plan_usage` read that name before this module became the single
#: resolver, so an operator who already exports it must keep getting the
#: database they named — silently reading a different file than the one
#: an override names is the failure this whole module exists to prevent.
_DB_PATH_ENV_VARS = ("PDT_CC_SWITCH_DB", "CC_SWITCH_DB")


def _env_db_override() -> Optional[str]:
    """The database path an environment variable names, if any."""
    for name in _DB_PATH_ENV_VARS:
        value = os.environ.get(name)
        if value:
            return value
    return None


def candidate_db_paths() -> List[Path]:
    """Every location this project will look for a CC Switch database.

    Most-likely first. CC Switch is a separate desktop application and
    where it keeps its data depends on the build and how it was
    installed — a user-directory install and an Applications-mode
    install need not agree.

    A machine can carry several of these at once — a zero-byte file left
    by an older install sitting next to the live database. Only the one
    with a readable schema is the answer, and the others are the reason
    this is a *list* rather than a single path: hard-coding one would
    report "CC Switch is not installed" on a machine that plainly has
    it, and send the user down the inherit path for no reason. The probe
    therefore validates rather than merely stat-ing, and the empty
    candidates below are covered because they are conventions a build
    may legitimately use — not because this machine happens to have them.
    """
    home = Path.home()
    candidates = [
        # Documented location.
        home / ".cc-switch" / "cc-switch.db",
        # macOS: the Tauri/Electron app-support convention. A build that
        # keeps its database under the bundle id is plausible rather than
        # hypothetical, so it is probed rather than assumed absent.
        home / "Library" / "Application Support" / "com.ccswitch.desktop"
        / "cc-switch.db",
        # Linux/XDG convention used by most desktop builds.
        home / ".config" / "cc-switch" / "cc-switch.db",
    ]
    appdata = os.environ.get("APPDATA")
    if appdata:
        # Windows: %APPDATA%\cc-switch\cc-switch.db
        candidates.append(Path(appdata) / "cc-switch" / "cc-switch.db")
    return candidates


def resolve_db_path(db_path: Optional[Union[str, Path]] = None) -> Path:
    """Return the CC Switch database this process should read.

    Precedence (first hit wins):

    1. an explicit ``db_path`` argument — callers that already know;
    This is the **only** implementation in the project. Every module that
    reads CC Switch calls it rather than joining ``~/.cc-switch`` itself:
    before this was centralised, four other call sites each built the
    path their own way and only this one honoured an override, so a
    relocated install made the probe report one file while the reader
    opened another.

    Precedence (first hit wins):

    1. an explicit ``db_path`` argument — callers that already know;
    2. ``PDT_CC_SWITCH_DB`` (then the ``CC_SWITCH_DB`` alias) — operator
       escape hatch, for a non-standard install location or a test;
    3. the first entry of :func:`candidate_db_paths` that exists;
    4. the documented default, when none exists.

    Note that an environment override is **authoritative**: if it is set
    and points somewhere unusable, this returns that path rather than
    quietly falling through to another database. An override that
    silently reads a different file than the one named is worse than an
    error — :func:`probe` reports it.

    Resolution is re-run on every call (never cached), so a test that
    sets the variable after import still gets the redirected path.
    """
    if db_path is not None:
        return Path(db_path).expanduser()
    override = _env_db_override()
    if override:
        return Path(override).expanduser()
    for candidate in candidate_db_paths():
        if candidate.exists():
            return candidate
    return _default_db_path()


def resolve_settings_path() -> Path:
    """Return CC Switch's ``settings.json``.

    This is a *different file from the database*, holding CC Switch's
    own current-selection pointer (``currentProviderClaude``) rather than
    the providers themselves. It sits beside the database in the
    documented install layout, and its location is deliberately **not**
    redirected by ``PDT_CC_SWITCH_DB``: that variable names a database,
    and treating it as naming a directory too would make an override
    aimed at one file silently relocate another.
    """
    return _default_db_path().parent / "settings.json"


def _connect_db(db_path: Optional[Union[str, Path]] = None) -> sqlite3.Connection:
    """Open a read-only connection to the CC Switch database.

    The only opener in the project. Beyond constructing the URI it
    *validates* the file — SQLite will happily open a text file or a
    zero-byte one as an empty database, and several zero-byte
    ``cc-switch.db`` files exist on a real machine (see
    :func:`candidate_db_paths`). Without the ``SELECT 1`` probe those
    would surface later as ``no such table: providers`` from the middle
    of a dispatch instead of as "this database is not usable".

    Raises:
        CCSwitchDBNotFoundError: the file does not exist, is empty, or
            cannot be opened.
    """
    path = resolve_db_path(db_path)
    if not path.exists():
        raise CCSwitchDBNotFoundError(f"CC Switch database not found: {path}")
    if path.stat().st_size == 0:
        raise CCSwitchDBNotFoundError(
            f"CC Switch database is empty (zero-byte file): {path}"
        )
    try:
        uri = f"file:{path}?mode=ro"
        conn = sqlite3.connect(uri, uri=True, timeout=5.0)
        conn.execute("SELECT 1")
        return conn
    except sqlite3.Error as exc:
        raise CCSwitchDBNotFoundError(
            f"cannot open CC Switch database at {path}: {exc}"
        ) from exc


#: Every column this project expects the CC Switch ``providers`` table to
#: carry.
#:
#: ``name`` and ``is_current`` are not needed by :func:`probe` itself,
#: but :func:`get_provider` / :func:`list_provider_names` key every lookup
#: on ``name`` and :func:`current_provider` reads ``is_current`` as its
#: fallback. They are listed anyway so the probe fails a database that
#: would break those later, inside a dispatch, which is a much worse
#: place to find out.
#:
#: All five are stock upstream CC Switch schema (checked against
#: farion1231/cc-switch ``main`` on 2026-09-24, and against this repo's
#: own "real providers table schema" test fixture). Nothing here is
#: fork-specific — which is the point: this project reads CC Switch's
#: *data contract*, so it works against upstream and a compatible fork
#: alike, and says so out loud when the contract is not met.
REQUIRED_PROVIDERS_COLUMNS = ("id", "app_type", "settings_config", "name", "is_current")


@dataclass(frozen=True)
class CCSwitchStatus:
    """Outcome of probing for a readable CC Switch database.

    ``available`` is the only thing callers should branch on.
    ``detail`` and ``remedy`` exist so a human sees *what* was wrong and
    *what to do* — the alternative is an ``sqlite3.OperationalError``
    raised from deep inside a dispatch, which tells an operator nothing.
    """

    available: bool
    path: Path
    detail: str
    provider_count: int = 0
    remedy: str = ""


@dataclass(frozen=True)
class ProviderConfig:
    """One CC Switch provider row, normalized across both layouts.

    Two on-disk layouts describe the same provider differently: the
    modern ``providers`` table keeps the endpoint and the model inside
    the ``settings_config.env`` block as ``ANTHROPIC_BASE_URL`` /
    ``ANTHROPIC_MODEL``, while the legacy ``provider_configs`` table
    keeps them in ``url`` / ``model`` columns and puts only the rest in
    ``extra_params``. Normalizing here is what lets a consumer read
    ``.base_url`` without knowing which table answered — before this
    existed, every consumer re-derived the endpoint and the credential
    from raw dicts, which is how three parsers that disagreed about
    where the API key lives came to exist.

    ``env`` is the row's env block **verbatim**: for a modern row, the
    ``settings_config.env`` mapping; for a legacy row, the parsed
    ``extra_params``. Consumers that stamp a subprocess environment
    want exactly this, unmodified.

    ``base_url`` and ``model`` are the *normalized* values, empty /
    ``None`` when the row declares neither. They are separate from
    ``env`` rather than folded into it: folding a legacy row's columns
    into its env block would fabricate keys the row never carried, and
    ``env`` is documented as verbatim.

    Read-only by construction — every field is frozen and the reader
    never writes to the database.
    """

    name: str
    env: Mapping[str, Any] = field(default_factory=dict)
    base_url: str = ""
    model: Optional[str] = None

    @property
    def api_key(self) -> str:
        """The credential, under whichever of the two names the row used.

        CC Switch writes ``ANTHROPIC_AUTH_TOKEN``; some rows carry
        ``ANTHROPIC_API_KEY`` instead. Which one a row uses is not
        something a caller should have to know, so both are accepted
        here and the empty string is the answer when neither is present.
        """
        return (
            self.env.get("ANTHROPIC_AUTH_TOKEN")
            or self.env.get("ANTHROPIC_API_KEY")
            or ""
        )

    @property
    def models(self) -> Dict[str, str]:
        """The per-tier model names this row declares.

        ``{"default", "opus", "sonnet", "haiku"}`` — the projection the
        dispatch stamps onto ``ANTHROPIC_MODEL`` and
        ``ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL``. Delegates to
        :func:`_tier_models`, the single implementation of that mapping.
        """
        return _tier_models(dict(self.env))

    def is_dispatchable(self) -> bool:
        """Whether this row can drive a sub-agent call.

        A row needs *both* an endpoint and a credential. "Has a base URL
        but no token" is a real shape — it is how a provider that was
        added to CC Switch but never signed in appears — and dispatching
        to it produces a 401 from a provider the operator believes is
        configured.
        """
        return bool(self.base_url and self.api_key)


def _inspect_db(path: Path):
    """Inspect one candidate.

    Returns ``(usable, detail, provider_count)``. Never raises — a
    candidate that cannot be read is a *reason*, not an exception,
    because the caller is in the middle of deciding between several.
    """
    if not path.exists():
        return False, "not found", 0
    if path.stat().st_size == 0:
        # Named explicitly rather than left to the connect attempt: a
        # real machine carries several zero-byte leftovers (see
        # candidate_db_paths), and "the file is there but empty" is a
        # different thing to tell an operator than "unreadable".
        return False, "zero-byte file", 0

    try:
        conn = _connect_db(path)
    except Exception as exc:  # noqa: BLE001 - any failure is just a reason
        return False, f"cannot open: {exc}", 0

    try:
        cur = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='providers'"
        )
        if cur.fetchone() is None:
            # Also the verdict for a zero-byte file, which SQLite happily
            # opens as an empty database. Several such files exist on a
            # real machine (see candidate_db_paths) and they must not be
            # mistaken for a working install.
            return False, "no 'providers' table", 0

        columns = {row[1] for row in conn.execute("PRAGMA table_info(providers)")}
        missing = [c for c in REQUIRED_PROVIDERS_COLUMNS if c not in columns]
        if missing:
            return False, f"missing column(s) {missing}", 0

        count = conn.execute("SELECT COUNT(*) FROM providers").fetchone()[0]
        return True, f"{count} provider row(s)", count
    except sqlite3.Error as exc:
        return False, f"schema probe failed: {exc}", 0
    finally:
        conn.close()


def probe(db_path: Optional[Union[str, Path]] = None) -> CCSwitchStatus:
    """Report whether CC Switch is present and readable. Never raises.

    A probe is a question, and a question that can throw is one the
    caller has to wrap in ``try`` — at which point "not installed" and
    "installed but broken" collapse into a single ``except`` branch and
    stop being distinguishable. Callers need those told apart: absent
    means "fall back to inheriting Claude Code's own configuration",
    broken means "something is wrong, say so".

    When a path is **named** — an explicit argument or ``PDT_CC_SWITCH_DB``
    — only that path is probed. An override that quietly read a different
    database than the one it named would be worse than an error.
    Otherwise every entry of :func:`candidate_db_paths` is tried in order
    and the first *usable* one wins, so a machine whose CC Switch keeps
    its data outside ``~/.cc-switch`` is detected rather than declared
    absent.
    """
    named = db_path is not None or _env_db_override() is not None
    candidates = [resolve_db_path(db_path)] if named else candidate_db_paths()

    tried = []
    for path in candidates:
        usable, detail, count = _inspect_db(path)
        if usable:
            return CCSwitchStatus(
                available=True,
                path=path,
                detail=f"{detail} in {path}",
                provider_count=count,
            )
        tried.append((path, detail))

    if named:
        path, why = tried[0]
        # Name the path in ``detail`` as well as carrying it on the
        # status: a log line reading ``available=False detail="not
        # found"`` tells an operator nothing about *what* was not found.
        detail = f"{path}: {why}"
        remedy = (
            "Check the file is readable by this user, is a CC Switch "
            "database, and is not locked by an upgrade — or unset "
            "PDT_CC_SWITCH_DB to let the usual locations be searched."
        )
    else:
        path = tried[0][0]
        detail = "no usable CC Switch database; tried " + "; ".join(
            f"{p} ({d})" for p, d in tried
        )
        remedy = (
            "Install CC Switch, or point PDT_CC_SWITCH_DB at an existing "
            "database. Without one, providers are inherited from Claude "
            "Code's own configuration."
        )

    return CCSwitchStatus(available=False, path=path, detail=detail, remedy=remedy)


# ---------------------------------------------------------------------------
# Provider rows
# ---------------------------------------------------------------------------
#
# A CC Switch row is ``(id, name, settings_config)``. ``name`` is the
# provider's identity; ``settings_config`` is JSON whose ``env`` block
# carries the endpoint and the token. Exactly two shapes are read here:
#
#   * the production ``providers`` table, above; and
#   * the legacy ``provider_configs`` table (``id, url, model,
#     extra_params``), which older installs still carry.


def _get_table_columns(conn: sqlite3.Connection, table_name: str) -> set:
    """Return the set of column names present in *table_name*."""
    cursor = conn.execute(f"PRAGMA table_info({table_name})")
    return {row[1] for row in cursor.fetchall()}


def _parse_settings_config(raw: Any) -> Dict[str, str]:
    """Decode a ``settings_config`` cell into its ``env`` block.

    The **only** implementation of that decode. It used to be written
    three times — here, in ``coding_tool``'s by-name reader and in its
    current-provider reader — and the three disagreed: two of them
    treated unparseable JSON as "no config", one raised, and only some
    of them also accepted ``ANTHROPIC_API_KEY`` alongside
    ``ANTHROPIC_AUTH_TOKEN``.

    Returns the ``env`` mapping, or ``{}`` when the cell carries no
    usable env block. Raises :class:`CCSwitchError` when the cell is a
    string that is not JSON: a corrupt row is a fact about the database
    that the caller should decide what to do about, not something to
    silently read as "this provider has no credentials".
    """
    if isinstance(raw, dict):
        settings = raw
    elif isinstance(raw, str):
        try:
            settings = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CCSwitchError(f"invalid settings_config JSON: {exc}") from exc
    else:
        return {}
    if not isinstance(settings, dict):
        return {}
    env = settings.get("env") or {}
    return dict(env) if isinstance(env, dict) else {}


def _parse_extra_params(value: Any) -> Dict[str, Any]:
    """Decode a legacy ``provider_configs.extra_params`` cell.

    Unlike :func:`_parse_settings_config` this is the env block itself,
    not JSON wrapping one.
    """
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise CCSwitchError(f"invalid extra_params JSON: {exc}") from exc
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _tier_models(env: Dict[str, Any]) -> Dict[str, str]:
    """The per-tier model names a provider's env block declares.

    The dispatch assigns these to ``ANTHROPIC_DEFAULT_{OPUS,SONNET,HAIKU}_MODEL``
    and ``ANTHROPIC_MODEL``; ``coding_tool`` reads the result as
    ``{"default", "opus", "sonnet", "haiku"}``.
    """
    return {
        "default": env.get("ANTHROPIC_MODEL", ""),
        "opus": env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", ""),
        "sonnet": env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", ""),
        "haiku": env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", ""),
    }


def _provider_row_to_config(
    provider_name: str, settings_config: Any
) -> ProviderConfig:
    """Convert a production ``providers`` row to a :class:`ProviderConfig`.

    The modern layout declares the endpoint inside the env block, so
    ``base_url`` and ``model`` are projected out of it here — once, at
    the boundary, rather than in each of the consumers.
    """
    env = _parse_settings_config(settings_config)
    return ProviderConfig(
        name=provider_name,
        env=env,
        base_url=env.get("ANTHROPIC_BASE_URL") or "",
        model=(
            env.get("ANTHROPIC_MODEL")
            or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
            or None
        ),
    )


def get_provider(
    provider_name: str,
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[ProviderConfig]:
    """Return one provider's configuration, keyed by its CC Switch name.

    ``provider_name`` is the ``providers.name`` column verbatim (e.g.
    ``"Vendor B Pro API"``) — the same string ``provider-order.json``
    carries and ``provider_routing.yaml`` matches against. There is no
    intermediate mapping table; the CC Switch database is the single
    source of truth.

    Returns a :class:`ProviderConfig`, or ``None`` when no row matches
    ``provider_name`` or the row carries no endpoint. A row with no
    ``ANTHROPIC_BASE_URL`` cannot drive a sub-agent, so reporting it as
    a miss is the honest answer. A matched legacy row is returned even
    when its ``url`` column happens to be empty — only the modern
    layout's rows are filtered on the endpoint, which is the behaviour
    that layout's readers have always had.

    Callers that need the credential too should use
    :meth:`ProviderConfig.is_dispatchable` rather than re-reading the
    env block: the "endpoint but no token" shape is a real one.

    Raises:
        CCSwitchDBNotFoundError: the database is missing or unusable.
        CCSwitchError: the database exists but a row cannot be parsed.
    """
    if not isinstance(provider_name, str) or not provider_name:
        raise CCSwitchError(
            f"provider_name must be non-empty string: {provider_name!r}"
        )
    conn = _connect_db(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name IN ('providers', 'provider_configs')"
        )
        table_row = cursor.fetchone()
        if table_row is None:
            return None

        if table_row[0] == "provider_configs":
            # Two things vary across on-disk layouts, so both are
            # discovered rather than assumed:
            #
            #   * which column identifies the row — current layouts have
            #     ``name``; older ones only have ``id``;
            #   * which value columns exist at all — a database that
            #     dropped ``url`` or ``extra_params`` must still read,
            #     with the absent values coming back as empty.
            #
            # Selecting the intersection is what makes both work. Asking
            # for a fixed column list raises ``no such column`` instead,
            # which is how a dropped ``url`` used to take the whole read
            # down.
            available = _get_table_columns(conn, "provider_configs")
            if "name" in available:
                key_col = "name"
            elif "id" in available:
                key_col = "id"
            else:
                return None

            cols = [key_col] + [
                c for c in ("id", "url", "model", "extra_params")
                if c in available and c != key_col
            ]
            cursor.execute(
                f"SELECT {', '.join(cols)} FROM provider_configs WHERE {key_col} = ?",
                (provider_name,),
            )
            row = cursor.fetchone()
            if row is None:
                return None
            data = dict(zip(cols, row))
            env = _parse_extra_params(data.get("extra_params"))
            return ProviderConfig(
                name=provider_name,
                env=env,
                # The legacy layout may declare the endpoint in the
                # column *or* (in rows written after that column was
                # added) inside extra_params; either answer is the same
                # endpoint, so both are accepted, column first.
                base_url=data.get("url") or env.get("ANTHROPIC_BASE_URL") or "",
                model=(
                    data.get("model")
                    or env.get("ANTHROPIC_MODEL")
                    or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
                    or None
                ),
            )

        cursor.execute(
            "SELECT settings_config FROM providers WHERE name = ?",
            (provider_name,),
        )
        row = cursor.fetchone()
        if row is None:
            return None
        cfg = _provider_row_to_config(provider_name, row[0])
        return cfg if cfg.base_url else None
    finally:
        conn.close()


def list_provider_names(db_path: Optional[Union[str, Path]] = None) -> List[str]:
    """Return every CC Switch provider name usable by the backend.

    The names are the ``providers.name`` column — the exact string the
    CC Switch UI shows, ``provider_routing.yaml`` matches its regexes
    against, and :func:`get_provider` queries by. Rows without an
    ``ANTHROPIC_BASE_URL`` are skipped: they cannot drive a Claude
    sub-agent, so offering them would make the roster promise something
    the lookup then fails to deliver.

    This is the enumeration entry point — "which providers exist?" — and
    a name that comes back from here is dispatchable.

    Returns names in DB row order (stable, not sorted). A legacy
    ``provider_configs`` layout has no name column and returns ``[]``.

    Raises:
        CCSwitchDBNotFoundError: the database is missing or unusable.
    """
    conn = _connect_db(db_path)
    try:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' "
            "AND name = 'providers'"
        )
        if cursor.fetchone() is None:
            return []

        cursor.execute("SELECT name, settings_config FROM providers")
        rows = cursor.fetchall()

        names: List[str] = []
        for name, settings_config in rows:
            if not isinstance(name, str) or not name:
                continue
            try:
                env = _parse_settings_config(settings_config)
            except CCSwitchError:
                # One unparseable row cannot declare an endpoint either,
                # so skip it rather than failing the whole enumeration.
                continue
            if env.get("ANTHROPIC_BASE_URL"):
                names.append(name)
        return names
    finally:
        conn.close()


def current_provider(
    db_path: Optional[Union[str, Path]] = None,
) -> Optional[ProviderConfig]:
    """The provider CC Switch is *currently routing* Claude traffic to.

    CC Switch owns the routing decision, so its current selection is the
    honest answer for which provider a scene-less dispatch should use.

    Resolution: ``settings.json::currentProviderClaude`` (a provider id)
    → the matching ``providers`` row for ``app_type='claude'`` → its
    ``settings_config.env``. Falls back to the row flagged
    ``is_current``, then to ``None``.

    Returns a dispatchable :class:`ProviderConfig`, or ``None`` when CC
    Switch is not reachable or has no current provider. **Never raises** —
    this sits on the dispatch path, where "ask someone else" is a valid
    answer and an exception is not.
    """
    settings_path = resolve_settings_path()
    db_path = resolve_db_path(db_path)
    if not db_path.exists():
        return None

    current_id = None
    try:
        if settings_path.exists():
            with open(settings_path, encoding="utf-8") as fh:
                current_id = json.load(fh).get("currentProviderClaude")
    except (OSError, json.JSONDecodeError, AttributeError):
        current_id = None

    def _row_to_provider(row) -> Optional[ProviderConfig]:
        if not row:
            return None
        try:
            env = _parse_settings_config(row[0])
        except CCSwitchError:
            return None
        cfg = ProviderConfig(
            name=row[1],
            env=env,
            base_url=env.get("ANTHROPIC_BASE_URL", "") or "",
            model=(
                env.get("ANTHROPIC_MODEL")
                or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL")
                or None
            ),
        )
        # The two-file lookup above can land on a row that was added but
        # never signed in; ask the config itself rather than repeating
        # the endpoint-and-credential test here.
        return cfg if cfg.is_dispatchable() else None

    try:
        conn = _connect_db(db_path)
        try:
            cursor = conn.cursor()
            if current_id:
                cursor.execute(
                    "SELECT settings_config, name FROM providers"
                    " WHERE id = ? AND app_type = 'claude'",
                    (current_id,),
                )
                cfg = _row_to_provider(cursor.fetchone())
                if cfg is not None:
                    return cfg
            cursor.execute(
                "SELECT settings_config, name FROM providers"
                " WHERE app_type = 'claude' AND is_current = 1 LIMIT 1"
            )
            return _row_to_provider(cursor.fetchone())
        finally:
            conn.close()
    except CCSwitchError:
        return None


# ---------------------------------------------------------------------------
# Consistent snapshots, for readers that need to page through the ledger
# ---------------------------------------------------------------------------


@contextmanager
def snapshot(
    db_path: Optional[Union[str, Path]] = None, *, attempts: int = 3
) -> Iterator[Path]:
    """Yield a consistent temp copy of the CC Switch database.

    CC Switch writes to this database while we read it, so a long paged
    query (``plan_usage`` walks ``proxy_request_logs``) can see a torn
    view. SQLite's online backup API produces a coherent copy without
    blocking the writer.

    The backup is retried: it can still surface ``database is locked``
    when CC Switch holds a write transaction for longer than the busy
    timeout.

    Raises:
        FileNotFoundError: there is no database at the resolved path.
        RuntimeError: every attempt hit a lock.
    """
    source_path = resolve_db_path(db_path)
    if not source_path.exists():
        raise FileNotFoundError(f"CC Switch DB not found: {source_path}")
    with tempfile.TemporaryDirectory(prefix="cc-switch-snapshot-") as tmpdir:
        copy = Path(tmpdir) / "cc-switch-snapshot.db"
        last_exc: Optional[Exception] = None
        for _ in range(max(1, attempts)):
            src_conn = None
            dst_conn = None
            try:
                src_conn = sqlite3.connect(
                    f"file:{source_path}?mode=ro", uri=True, timeout=10
                )
                dst_conn = sqlite3.connect(str(copy))
                src_conn.backup(dst_conn)
                break
            except sqlite3.Error as exc:  # locked / busy
                last_exc = exc
                copy.unlink(missing_ok=True)
                time.sleep(0.5)
            finally:
                if dst_conn is not None:
                    dst_conn.close()
                if src_conn is not None:
                    src_conn.close()
        else:
            raise RuntimeError(
                f"could not snapshot CC Switch DB at {source_path}: {last_exc}"
            )
        yield copy
