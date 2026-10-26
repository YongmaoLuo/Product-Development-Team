"""Static gate: both plan-creation doors document the delivery they bind.

Background
----------
Two doors create a plan and bind its human-in-the-loop origin:
``POST /api/plan/import`` (``routes/fast_lane.py``) and
``POST /api/interview/start`` (``routes/phases.py``).  Both call the same
helper — ``bind_hook_for_plan`` — so both record the same origin, in the
same file, in the same shape.

The *protocol* of that door did not change: the body still carries
``hook_destination`` and ``hook_session_id``, both optional, both
strings.  What changed is what those two values are **for**.  The
stored hook is no longer a ``cc_connect`` transport description — it is a
``cmd`` hook: a command that runs ``backend.hooks.session_send`` with the
plan's destination and session as its arguments, and that module is what
turns them into a delivered message.  ``hook_destination`` is the
cc-connect project the question goes to; ``hook_session_id`` is the
cc-connect session key inside it.

Why a gate
----------
The two doors are the only places a caller learns what those two field
names *mean*.  A docstring that stops at "where a question is
delivered" describes the intent but not the mechanism, and the
mechanism is exactly the part that changed: a reader who still expects
a ``cc_connect`` hook reads a ``cmd`` hook as a different contract, and
the reader who has to debug a decision that never arrived has no way to
tell "the delivery is a command now" from "the delivery was never
configured".  Documentation that is merely incomplete is harmless;
documentation that describes the old mechanism is actively misleading,
and nothing fails — it is read once, believed, and then the reader
reasons about the wrong contract from then on.

So the two doors have to say, in their own text, that the delivery is a
``cmd`` hook running ``backend.hooks.session_send``, that the
destination is the cc-connect project and the session is the cc-connect
session key.  This gate is the executable form of that requirement.

What it pins
------------
1. **The delivery is documented** — both route files name
   ``session_send``, the hook kind ``cmd``, the cc-connect project and
   the cc-connect session.
2. **The protocol is unchanged** — both route files still declare
   ``hook_destination`` and ``hook_session_id`` with the same name, type
   and default.  This task changed prose, not the wire format.

The gate reads the files as text.  That is deliberate: the fields are
declared as ``#:`` comments and the explanation lives partly in those
comments, so an AST that only saw docstrings would miss half of what
the two doors say about themselves.
"""

from __future__ import annotations

from pathlib import Path

#: ``backend/`` — this file lives in ``backend/tests/static_gates/``.
BACKEND_DIR = Path(__file__).resolve().parents[2]

#: The two plan-creation doors.  Both bind the same origin through the
#: same helper, so both have to describe the same delivery.
ROUTE_FILES: tuple[str, ...] = (
    "routes/fast_lane.py",
    "routes/phases.py",
)

#: What the delivery has to be named in each door's own text: the
#: delivery module, the hook kind it is stored as, and the two things
#: the two fields actually are.
REQUIRED_TOKENS: tuple[str, ...] = (
    "session_send",
    "cmd",
    "cc-connect project",
    "cc-connect session",
)

#: The protocol fields, unchanged: name, type and default.  This task
#: changed prose; the wire format is the same.
PROTOCOL_DECLARATIONS: tuple[str, ...] = (
    "hook_destination: Optional[str] = None",
    "hook_session_id: Optional[str] = None",
)


def _source(rel: str) -> str:
    """The text of one route module, read relative to ``backend/``."""
    path = BACKEND_DIR / rel
    assert path.exists(), f"{rel} is missing: the gate is scanning a tree that moved."
    text = path.read_text(encoding="utf-8")
    assert text.strip(), f"{rel} is empty: a gate that reads nothing proves nothing."
    return text


def test_routes_document_cmd_delivery() -> None:
    """Both plan-creation doors name the delivery they bind.

    The delivery is a ``cmd`` hook running ``backend.hooks.session_send``;
    ``hook_destination`` is the cc-connect project and ``hook_session_id``
    is the cc-connect session key.  A door that does not say so describes
    a mechanism that is no longer the one in use.
    """
    for rel in ROUTE_FILES:
        source = _source(rel)
        missing = [token for token in REQUIRED_TOKENS if token not in source]
        assert not missing, (
            f"{rel} does not document the delivery it binds: {missing!r} "
            f"does not appear in it.\n"
            f"The stored hook is a 'cmd' hook whose command runs "
            f"backend.hooks.session_send with the plan's destination and "
            f"session as its arguments; hook_destination is the "
            f"cc-connect project and hook_session_id is the cc-connect "
            f"session key. Say so in this file — a door that describes "
            f"the old transport describes a contract the code no longer "
            f"implements."
        )


def test_protocol_fields_unchanged() -> None:
    """Both doors still declare the same two protocol fields.

    The protocol did not change: the names, the type and the default are
    what they were, and both doors carry them.  A field renamed, made
    required, or given a different default is a breaking change to every
    caller — and one that would pass a gate that only checked that the
    names are still mentioned somewhere.
    """
    for rel in ROUTE_FILES:
        source = _source(rel)
        missing = [
            declaration
            for declaration in PROTOCOL_DECLARATIONS
            if declaration not in source
        ]
        assert not missing, (
            f"{rel} no longer declares {missing!r}. The creation door's "
            f"protocol is unchanged by this work: hook_destination and "
            f"hook_session_id stay optional strings with a None default."
        )
