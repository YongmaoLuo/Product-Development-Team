"""The decision event carries the question it is about.

What this pins
--------------
Upstream requires the delivered message to *contain the question*, so
the event JSON has to carry it.  ``hitl.service._event_payload`` builds
that body, and before this change it carried the decision's identity,
the task it is about, the answers on the table and the deadline -- but
not the question itself.  A ``session_send`` on the other end had
nothing to put in the message.

So this file pins three things:

* **the question travels** -- the payload's ``question`` is the question
  as asked, character for character;
* **the key set is fixed** -- adding ``question`` added exactly one key
  and did not disturb the other seven (their names and their values);
* **an empty question is carried as-is** -- it is not dropped and not
  an error: the receiving side shows a placeholder, so the payload
  must still have the key with an empty string in it.

Nothing here touches the state machine: ``_ask_the_human`` and
``_dispatch_in_background`` are untouched, and the settlement semantics
of a failed delivery are the same as they were.
"""

from hitl.models import Decision
from hitl.service import _event_payload


def _decision(**overrides) -> Decision:
    """A decision as the service would have built it."""
    kwargs = {
        "plan_id": "p1",
        "decision_id": "d1",
        "source": "execution",
        "question": "是否合入？",
        "options": ["accept", "revise"],
        "default_option": "accept",
    }
    kwargs.update(overrides)
    return Decision(**kwargs)


def test_payload_carries_question():
    """The payload carries the question exactly as it was asked."""
    payload = _event_payload(_decision())

    assert payload["question"] == "是否合入？"


def test_payload_keeps_existing_keys():
    """Adding ``question`` added one key and disturbed none of the rest."""
    payload = _event_payload(_decision())

    assert set(payload) == {
        "plan_id",
        "decision_id",
        "task_id",
        "kind",
        "question",
        "options",
        "default_option",
        "deadline_at",
    }
    assert payload["plan_id"] == "p1"
    assert payload["decision_id"] == "d1"
    assert payload["task_id"] is None
    assert payload["kind"] == "execution"
    assert payload["options"] == ["accept", "revise"]
    assert payload["default_option"] == "accept"


def test_empty_question_is_carried():
    """An empty question is carried as an empty string, not dropped."""
    payload = _event_payload(_decision(question=""))

    assert "question" in payload
    assert payload["question"] == ""