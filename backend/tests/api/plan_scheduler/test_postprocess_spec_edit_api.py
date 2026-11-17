"""The requirement book's editor, and the deadline the panel adjusts.

What is pinned here
-------------------
The finishing stage judges a plan against a **requirement book** —
``plans/{id}/postprocess.json`` — and the operator panel needs two ways
into it:

* **read and write the book** — ``GET`` shows it, ``PUT`` writes it back;
* **adjust a wait's deadline** — ``PATCH`` puts the deadline where the
  operator says it should be.

What each of them answers is pinned here:

* **200** — the change was carried out;
* **400** — the plan id is not a name: a traversal, an absolute path, a
  null byte.  It is the shared guard's own refusal, and it happens
  before anything is written;
* **403** — the request never reached a handler.  The guard is
  middleware over every ``/api/`` path, so a request without the header
  is refused before the endpoint runs;
* **404** — there is no requirement book to show;
* **409** — the wait is not pending any more;
* **422** — the book is not a legal requirement book, or the number of
  seconds asked for is not a number of seconds.

The requests are real HTTP requests
-----------------------------------
The cases drive the **mounted app**, not the route functions: the answer
an operator gets is the answer the HTTP layer gives, status code and
body together.  A case that called the function directly would prove the
body and not the status, and the status is half of what this contract
is.

The write is limited to one file
--------------------------------
``PUT`` writes **one** file: ``plans/{id}/postprocess.json``.  Every
other file of the plan — the progress file, the free-form companion, the
plan's own tasks — is left exactly as it was, and a refused write leaves
the book on disk byte-for-byte as it was.  A write that reached past
that one file would change the criteria of a round that is already
running.

An edit during a round belongs to the next round
-------------------------------------------------
The finishing stage freezes the book when it starts (architecture
decision point 3): this round's criteria are the book as it stood at
that moment, and an edit made while the round runs belongs to the
*next* round.  So an edit does not touch the frozen fingerprint the
progress file carries, and the next freeze reads the new content.

The books are the shared samples
--------------------------------
``tests/fixtures/postprocess/builders.py`` owns the shape of a
requirement item, so these cases spell only what they are about.  The
one shape the fixture does not carry — ``command`` and ``target``, which
belong to the outward-facing entries — is layered on top of it here.
"""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi.testclient import TestClient

_BACKEND_DIR = Path(__file__).resolve().parents[3]
if str(_BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(_BACKEND_DIR))

import server  # noqa: E402
from postprocess.progress import load_progress, mark  # noqa: E402
from postprocess.spec import SPEC_FILENAME, freeze, load_spec  # noqa: E402
from postprocess.waits import WaitRegistry  # noqa: E402
from tests.fixtures.postprocess.builders import make_item, make_spec  # noqa: E402


client = TestClient(server.app)

#: The waiting item the deadline cases act on.
ITEM_ID = "approve"


# ---------------------------------------------------------------------------
# The fixtures the cases act through
# ---------------------------------------------------------------------------


def plan_dir(plan_id: str) -> Path:
    """The plan's own directory, as the server sees it."""
    return Path(server.PLANS_DIR) / plan_id


def spec_path(plan_id: str) -> Path:
    """The requirement book's own path."""
    return plan_dir(plan_id) / SPEC_FILENAME


def digest(path: Path) -> str:
    """The sha256 of a file's bytes."""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def plan_digests(plan_id: str) -> Dict[str, str]:
    """Every file of the plan's own directory, by name and by digest."""
    directory = plan_dir(plan_id)
    if not directory.is_dir():
        return {}
    return {
        path.name: digest(path)
        for path in sorted(directory.iterdir())
        if path.is_file()
    }


def a_book(action: str = "整理发布说明") -> Dict[str, Any]:
    """A legal requirement book."""
    return make_spec([make_item(id="release-notes", action=action)])


def an_outward_book_without_a_command() -> Dict[str, Any]:
    """An illegal requirement book: an outward entry without an argv.

    The command runner's whitelist is built from the book's argv, so an
    outward-facing entry without one cannot be whitelisted at all — the
    loader refuses it, and the editor refuses it too.
    """
    item = make_item(id="open-pr", action="开 PR", external=True)
    item["target"] = "origin/main"
    return make_spec([item])


def write_book(plan_id: str, book: Dict[str, Any]) -> Path:
    """Put a requirement book on disk, as the plan's own file."""
    path = spec_path(plan_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(book, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return path


def make_wait(plan_id: str, item_id: str = ITEM_ID) -> None:
    """Give one plan one waiting item, through the production registry."""
    WaitRegistry(plan_id).create_wait(
        {"id": item_id, "wait": {"type": "human"}},
        question="批准合入？",
    )


def wait_record(plan_id: str, item_id: str = ITEM_ID) -> Dict[str, Any]:
    """One wait's durable record, as it sits in the progress file."""
    return load_progress(plan_id)["items"][item_id]["wait"]


def spec_url(plan_id: str) -> str:
    """The requirement book endpoint of one plan."""
    return f"/api/scheduler/{plan_id}/postprocess-spec"


def deadline_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The deadline endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/deadline"


def extend_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The extend endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/extend"


def terminate_url(plan_id: str, item_id: str = ITEM_ID) -> str:
    """The terminate endpoint of one waiting item."""
    return f"/api/scheduler/{plan_id}/waits/{item_id}/terminate"


# ---------------------------------------------------------------------------
# 1 — the book: read it
# ---------------------------------------------------------------------------


def test_get_returns_spec_json() -> None:
    """The book is shown as it is on disk.

    The editor is where an illegal book gets fixed, so the answer is the
    file's own content — not a prettified copy of it, and not a summary
    of the criteria the loader happens to understand.
    """
    plan_id = "spec-edit-get"
    book = a_book()
    write_book(plan_id, book)

    response = client.get(spec_url(plan_id))

    assert response.status_code == 200, response.text
    assert response.json() == book, (
        "the answer is the book's own content, read back from the file"
    )


def test_get_without_a_book_returns_404() -> None:
    """A plan with no requirement book has nothing to show."""
    plan_id = "spec-edit-get-missing"
    plan_dir(plan_id).mkdir(parents=True, exist_ok=True)

    response = client.get(spec_url(plan_id))

    assert response.status_code == 404, (
        f"there is no requirement book to show, got {response.status_code}: "
        f"{response.text}"
    )


# ---------------------------------------------------------------------------
# 2 — the book: write it back
# ---------------------------------------------------------------------------


def test_put_writes_only_postprocess_json() -> None:
    """A write touches one file, and only that one.

    The plan's own directory holds more than the book: the progress file
    of a round that is running, the free-form companion, the plan's own
    tasks.  A write that reached past the book would change the
    criteria of a round that is already running — so every other file
    has to come out of it byte-for-byte as it went in, and no file may
    appear that was not there before.
    """
    plan_id = "spec-edit-put-only"
    write_book(plan_id, a_book("整理发布说明"))

    # The plan's other files, as the finishing stage and the plan leave
    # them: a progress file written by the production progress module, a
    # companion file, and the plan's own task list.
    mark(plan_id, "release-notes", "running")
    (plan_dir(plan_id) / "postprocess.md").write_text(
        "# 补充说明\n", encoding="utf-8"
    )
    (plan_dir(plan_id) / "tasks.json").write_text(
        '[{"id": "t1"}]\n', encoding="utf-8"
    )

    before = plan_digests(plan_id)
    assert SPEC_FILENAME in before, "the book is one of the plan's files"

    response = client.put(spec_url(plan_id), json=a_book("整理发布说明并通知"))

    assert response.status_code == 200, response.text

    after = plan_digests(plan_id)
    assert sorted(after) == sorted(before), (
        f"the write added or removed a file: {sorted(before)} -> "
        f"{sorted(after)}"
    )
    for name in before:
        if name == SPEC_FILENAME:
            continue
        assert after[name] == before[name], (
            f"the write changed {name}, and only the book may be written"
        )

    written = json.loads(spec_path(plan_id).read_text(encoding="utf-8"))
    assert written["items"][0]["action"] == "整理发布说明并通知", (
        "the book on disk is the book that was sent"
    )


def test_put_rejects_traversal_plan_id(isolated_plans_dir: Path) -> None:
    """A plan id that is a traversal is refused, and nothing is written.

    The refusal is the shared guard's own 400 — a bad request, not a
    missing plan — and it happens before the write, so the plan's own
    book is untouched and no directory was created next to the plans
    root.
    """
    plan_id = "spec-edit-put-traversal"
    write_book(plan_id, a_book())
    before = digest(spec_path(plan_id))

    response = client.put(
        "/api/scheduler/%2e%2e%2fescape/postprocess-spec", json=a_book("越界")
    )

    assert response.status_code == 400, (
        f"a traversal plan id is a bad request, got {response.status_code}: "
        f"{response.text}"
    )
    assert digest(spec_path(plan_id)) == before, (
        "the refused write left the plan's own book as it was"
    )
    assert not (isolated_plans_dir.parent / "escape").exists(), (
        "the refused write created a directory outside the plan's own"
    )


def test_put_rejects_an_absolute_or_empty_plan_id() -> None:
    """The other malformed plan ids are refused the same way.

    An absolute path, a null byte and a bare traversal are three shapes
    of the same mistake: a plan id is a **name**, not a path.
    """
    plan_id = "spec-edit-put-absolute"
    write_book(plan_id, a_book())
    before = digest(spec_path(plan_id))

    for bad in ("%2fetc%2fpasswd", "%00", "%2e%2e"):
        response = client.put(
            f"/api/scheduler/{bad}/postprocess-spec", json=a_book("越界")
        )
        assert response.status_code == 400, (
            f"{bad!r} is not a plan id and was answered {response.status_code}: "
            f"{response.text}"
        )

    assert digest(spec_path(plan_id)) == before, (
        "the refused writes left the plan's own book as it was"
    )


def test_put_without_the_guard_header_returns_403() -> None:
    """A request without the guard header never reaches the endpoint.

    The guard is middleware over every ``/api/`` path, so the refusal
    happens before the endpoint is reached — and the book is not
    written.
    """
    plan_id = "spec-edit-put-unguarded"
    write_book(plan_id, a_book())
    before = digest(spec_path(plan_id))

    response = client.put(
        spec_url(plan_id),
        json=a_book("越界"),
        headers={"X-PDT-Request": ""},
    )

    assert response.status_code == 403, (
        f"a request without the guard header is refused, got "
        f"{response.status_code}: {response.text}"
    )
    assert digest(spec_path(plan_id)) == before, (
        "the refused request wrote nothing"
    )


def test_edit_rejects_invalid_spec() -> None:
    """An illegal book is refused, and the file on disk does not move.

    The book is judged by the loader the finishing stage itself uses, so
    an edit the stage would refuse is refused here too — and the refusal
    names the entry that is wrong, because a book has many entries and
    the author needs to know which one to open.
    """
    plan_id = "spec-edit-put-invalid"
    write_book(plan_id, a_book())
    before = digest(spec_path(plan_id))

    response = client.put(
        spec_url(plan_id), json=an_outward_book_without_a_command()
    )

    assert response.status_code == 422, (
        f"an outward entry without an argv is not a requirement book, got "
        f"{response.status_code}: {response.text}"
    )
    assert "open-pr" in response.text, (
        f"the refusal names the entry that is wrong: {response.text}"
    )
    assert digest(spec_path(plan_id)) == before, (
        "the refused write left the book on disk byte-for-byte as it was"
    )


def test_put_rejects_a_book_that_is_not_an_object() -> None:
    """A body that is not a requirement book at all is refused."""
    plan_id = "spec-edit-put-not-a-book"
    write_book(plan_id, a_book())
    before = digest(spec_path(plan_id))

    for body in ([], "not a book", {"items": []}, {"items": "not a list"}):
        response = client.put(spec_url(plan_id), json=body)
        assert response.status_code == 422, (
            f"{body!r} is not a requirement book and was answered "
            f"{response.status_code}: {response.text}"
        )

    assert digest(spec_path(plan_id)) == before, (
        "the refused writes left the book on disk as it was"
    )


# ---------------------------------------------------------------------------
# 3 — the deadline
# ---------------------------------------------------------------------------


def test_patch_updates_same_field_as_extend() -> None:
    """The panel's two deadline verbs move the very same field.

    ``extend`` pushes the deadline out; ``PATCH`` puts it where the
    operator says it should be.  They are two ways of writing one
    field, so the second one starts from what the first one wrote: an
    extension that is set afterwards is gone, and an extension that
    follows a setting starts from the setting.
    """
    plan_id = "spec-edit-patch-same-field"
    make_wait(plan_id)

    created = datetime.fromisoformat(wait_record(plan_id)["deadline"])

    extended = client.post(extend_url(plan_id), json={"seconds": 3600})
    assert extended.status_code == 200, extended.text
    after_extend = datetime.fromisoformat(wait_record(plan_id)["deadline"])
    assert after_extend - created == timedelta(seconds=3600), (
        "the extension moved the deadline by exactly the seconds asked for"
    )

    patched = client.patch(
        deadline_url(plan_id), json={"remaining_seconds": 172800}
    )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    after_patch = datetime.fromisoformat(wait_record(plan_id)["deadline"])

    assert body["item_id"] == ITEM_ID
    assert body["deadline"] == after_patch.isoformat(), (
        "the answer carries the deadline that was written down"
    )
    assert body["remaining_seconds"] == 172800, (
        "the answer says how much time is left"
    )
    assert after_patch != after_extend, (
        "the setting replaced the deadline, and the extension is gone"
    )
    assert abs(
        (after_patch - datetime.now(timezone.utc)).total_seconds() - 172800
    ) <= 2, "the deadline is exactly the number of seconds that were asked for"

    again = client.post(extend_url(plan_id), json={"seconds": 60})
    assert again.status_code == 200, again.text
    after_again = datetime.fromisoformat(wait_record(plan_id)["deadline"])
    assert after_again - after_patch == timedelta(seconds=60), (
        "the extension started from the deadline the setting wrote"
    )


def test_patch_rejects_a_wait_that_is_over() -> None:
    """A wait that is over cannot have its deadline moved.

    The wait is given up on first — through the endpoint, so the state
    the adjustment meets is the state the production code left behind.
    """
    plan_id = "spec-edit-patch-conflict"
    make_wait(plan_id)

    terminated = client.post(terminate_url(plan_id))
    assert terminated.status_code == 200, terminated.text

    response = client.patch(
        deadline_url(plan_id), json={"remaining_seconds": 3600}
    )

    assert response.status_code == 409, (
        f"moving the deadline of a wait that is over is a conflict, got "
        f"{response.status_code}: {response.text}"
    )


def test_patch_rejects_values_that_are_not_seconds() -> None:
    """A number of seconds that is not one is refused, not guessed.

    Six ways of asking for something that is not a number of seconds —
    a negative number, zero, a fraction, a string, a boolean and a
    number so large it is not a wait any more — and a body that says
    nothing at all.
    """
    plan_id = "spec-edit-patch-invalid"
    make_wait(plan_id)
    before = wait_record(plan_id)["deadline"]

    for body in (
        {"remaining_seconds": -1},
        {"remaining_seconds": 0},
        {"remaining_seconds": 1.5},
        {"remaining_seconds": "3600"},
        {"remaining_seconds": True},
        {"remaining_seconds": 10 ** 12},
        {},
    ):
        response = client.patch(deadline_url(plan_id), json=body)
        assert response.status_code == 422, (
            f"{body!r} is not a number of seconds and was answered "
            f"{response.status_code}: {response.text}"
        )

    assert wait_record(plan_id)["deadline"] == before, (
        "a refused adjustment leaves the deadline where it was"
    )


def test_patch_rejects_an_unknown_waiting_item() -> None:
    """An item that is not waiting is not found."""
    plan_id = "spec-edit-patch-unknown"
    make_wait(plan_id)

    response = client.patch(
        deadline_url(plan_id, "never-created"), json={"remaining_seconds": 60}
    )

    assert response.status_code == 404, (
        f"there is no such waiting item, got {response.status_code}: "
        f"{response.text}"
    )


def test_patch_answers_404_for_a_wait_the_lifecycle_cannot_speak_about() -> None:
    """A wait record the lifecycle does not know is not a waiting item.

    The rebuild leaves out a record whose kind of wait is outside the
    vocabulary the lifecycle knows: it cannot be said what that wait
    is, so there is no waiting item to move a deadline on.  The answer
    is the same 404 an item that was never created gets, and the record
    on disk is left exactly as it was.
    """
    plan_id = "spec-edit-patch-alien-wait"
    make_wait(plan_id)
    mark(
        plan_id,
        "alien",
        "waiting",
        wait={
            "item_id": "alien",
            "wait_type": "alien",
            "question": "？",
            "deadline": (
                datetime.now(timezone.utc) + timedelta(days=1)
            ).isoformat(),
            "extensions": [],
            "status": "pending",
        },
    )
    before = wait_record(plan_id, "alien")

    response = client.patch(
        deadline_url(plan_id, "alien"), json={"remaining_seconds": 60}
    )

    assert response.status_code == 404, (
        f"a wait the lifecycle cannot speak about is not a waiting item, "
        f"got {response.status_code}: {response.text}"
    )
    assert wait_record(plan_id, "alien") == before, (
        "the refused adjustment left the record as it was"
    )


def test_patch_rejects_an_item_id_that_is_not_a_name() -> None:
    """An item id that carries a separator, or is a traversal, is refused.

    An item id names a row of the plan's own progress file.  One that
    carries a path separator is not a name, and it is refused rather
    than answered with whatever it points at.
    """
    plan_id = "spec-edit-patch-item-id"
    make_wait(plan_id)
    before = wait_record(plan_id)["deadline"]

    for bad in ("a%2Fb", "%2e%2e"):
        response = client.patch(
            deadline_url(plan_id, bad), json={"remaining_seconds": 60}
        )
        assert response.status_code == 400, (
            f"{bad!r} is not an item id and was answered {response.status_code}: "
            f"{response.text}"
        )

    assert wait_record(plan_id)["deadline"] == before, (
        "the refused adjustments left the deadline where it was"
    )


def test_patch_from_a_foreign_origin_returns_403() -> None:
    """A page from another origin cannot drive the adjustment.

    The guard is middleware over every ``/api/`` path, so the refusal
    happens before the endpoint is reached — and the deadline does not
    move.
    """
    plan_id = "spec-edit-patch-foreign-origin"
    make_wait(plan_id)
    before = wait_record(plan_id)["deadline"]

    response = client.patch(
        deadline_url(plan_id),
        json={"remaining_seconds": 60},
        headers={"Origin": "http://evil.example.com"},
    )

    assert response.status_code == 403, (
        f"a request from a foreign origin is refused, got "
        f"{response.status_code}: {response.text}"
    )
    assert wait_record(plan_id)["deadline"] == before, (
        "the refused request changed nothing"
    )


# ---------------------------------------------------------------------------
# 4 — an edit during a running round
# ---------------------------------------------------------------------------


def test_edit_during_postprocessing_keeps_frozen_hash() -> None:
    """An edit made while a round runs belongs to the *next* round.

    The finishing stage freezes the book when it starts, and the
    progress file carries that round's fingerprint.  An edit made while
    the round runs must not move that fingerprint — the round's
    criteria are the book as it stood when the round began — while the
    next freeze reads the new content.
    """
    plan_id = "spec-edit-during-round"
    write_book(plan_id, a_book("本轮的判据"))

    # The round that is running: its criteria are frozen, and the
    # progress file carries that fingerprint.
    frozen = freeze(load_spec(plan_dir(plan_id)))
    mark(plan_id, "release-notes", "running", spec_sha256=frozen.spec_sha256)

    response = client.put(spec_url(plan_id), json=a_book("下一轮的判据"))
    assert response.status_code == 200, response.text

    assert load_progress(plan_id)["spec_sha256"] == frozen.spec_sha256, (
        "this round's fingerprint did not move"
    )
    assert frozen.spec.items[0].action == "本轮的判据", (
        "the frozen criteria are the ones the round started with"
    )

    next_round = freeze(load_spec(plan_dir(plan_id)))
    assert next_round.spec_sha256 != frozen.spec_sha256, (
        "the next round freezes the new content"
    )
    assert next_round.spec.items[0].action == "下一轮的判据", (
        "the next round reads the book that was written"
    )
