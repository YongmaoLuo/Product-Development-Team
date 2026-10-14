"""The Telegram mirror must work from the repo, not from a module path.

Why this suite exists
---------------------
The mirror used to resolve its transport at call time with
``importlib.import_module("tools.<helper>.transport")``.
``tools/`` is operator-owned and gitignored, so on a clean checkout the
import raised, the notifier logged an exception, and the channel was
silently dead — the failure was invisible because "channel disabled" is
also how a not-provisioned operator looks.

Two things are pinned here:

  * the body the mirror hands to the transport is **valid MarkdownV2**
    (content escaped, this module's own emphasis markers preserved) —
    the transport sends it verbatim, so a single unescaped character
    means Telegram rejects the whole message;
  * the mirror reaches the transport through the package, and the
    message id it persists is the one that is *live* on the channel, not
    the one it asked to edit.
"""

from __future__ import annotations

import pytest

from notifications import feishu_notifier as fn
from notifications.feishu_notifier import FeishuNotifier, PlanCardState
from notifications.telegram_client import MARKDOWN_V2_SPECIAL_CHARS


def _card(
    title: str = "验收完成",
    body: str = "run 1.0 done!",
    tag: str | None = "plan-a",
) -> dict:
    """A card shaped like the ones ``notifications/cards.py`` builds.

    ``elements``, **not** ``sections``. Until 2026-10-11 this fixture
    built ``sections`` — a key no card builder in this repo writes —
    and the renderer read exactly that key, so the two agreed with each
    other and neither agreed with production. Every real render came
    out as the title line alone; because the title only changes when
    the phase does, the mirror sent Telegram an identical body on every
    push and Telegram answered ``400 message is not modified`` from the
    first phase change onward. A fixture that spells out the producer's
    shape is the only kind that can catch that.

    ``tag=None`` drops the ``plan_id_tag`` line, which is itself
    emphasis markup — useful when a test is auditing the body alone.
    """
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"title": {"tag": "plain_text", "content": title}, "template": "red"},
        "elements": [
            {"tag": "div", "text": {"tag": "lark_md", "content": body}},
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [
                    {"tag": "plain_text", "content": "🕐 刷新于 10-11 09:03:40"}
                ],
            },
        ],
    }
    if tag is not None:
        card["plan_id_tag"] = tag
    return card


@pytest.fixture
def telegram_env(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "-1001234567890")
    return {"token": "123456:test-token", "chat": "-1001234567890"}


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_escapes_card_content_and_keeps_its_own_emphasis():
    """Reserved characters in the card are content, not markup."""
    rendered = fn._render_card_as_markdown(_card(), "plan-a")

    # The title is emphasised by *this* function, so its asterisks stay.
    assert rendered.startswith("*验收完成*")
    # The tag's underscores belong to this function too.
    assert "_plan\\-a_" in rendered
    # ... while "1.0 done!" came from the card and must be neutralised.
    assert "1\\.0 done\\!" in rendered


def test_render_falls_back_to_the_plan_id_for_an_empty_card():
    rendered = fn._render_card_as_markdown({}, "plan-b")
    assert "plan\\-b" in rendered


def test_render_truncates_past_the_telegram_limit():
    card = _card(body="x" * (fn.MAX_MESSAGE_CHARS + 500))
    rendered = fn._render_card_as_markdown(card, "plan-a")
    assert len(rendered) == fn.MAX_MESSAGE_CHARS
    assert rendered.endswith("…")


def test_truncation_never_leaves_a_dangling_escape():
    """A body that fails to parse fails on *every* retry, forever.

    The cut can land between a backslash and the character it escapes;
    the trailing escape then has nothing to escape, which Telegram
    rejects — and because the card content is unchanged, each retry
    produces the same rejected body.
    """
    card = _card(body="\\" * (fn.MAX_MESSAGE_CHARS // 2))
    rendered = fn._render_card_as_markdown(card, "plan-a")
    body = rendered[:-1]  # drop the ellipsis
    trailing_backslashes = len(body) - len(body.rstrip("\\"))
    assert trailing_backslashes % 2 == 0


def test_render_reads_the_elements_a_card_actually_has():
    """The regression: a card's body lives in ``elements``.

    2026-10-11. The renderer read ``card["sections"]``, which nothing
    writes. The loop never ran, every render was the title line, and
    the mirror froze at the last phase transition — the title line only
    changes when the phase does, so Telegram kept being handed the same
    body and kept answering ``message is not modified``.
    """
    rendered = fn._render_card_as_markdown(_card(), "plan-a")

    assert "1\\.0 done\\!" in rendered, (
        "the div's text must reach the mirror; producing only the title "
        "line is the frozen-card bug"
    )
    assert "刷新于 10\\-11 09:03:40" in rendered, (
        "the note's text must reach the mirror — it is what tells an "
        "operator the card is alive"
    )


def test_render_drops_the_rule_but_keeps_the_order():
    """``hr`` carries no text; the sections it separates are headings."""
    rendered = fn._render_card_as_markdown(_card(body="A"), "plan-a")

    assert rendered.index("A") < rendered.index("刷新于")


def test_render_skips_element_tags_it_does_not_know():
    """A card that grows a button loses the button, not the message."""
    card = _card()
    card["elements"].insert(
        0, {"tag": "action", "actions": [{"tag": "button", "text": {"content": "x"}}]}
    )

    rendered = fn._render_card_as_markdown(card, "plan-a")

    assert "1\\.0 done\\!" in rendered
    assert "button" not in rendered


def test_lark_bold_becomes_markdown_v2_bold():
    """``**x**`` is lark's spelling; MarkdownV2's is ``*x*``.

    Escaping it instead is safe but shows the operator the asterisks and
    flattens the headings that make the card skimmable.
    """
    rendered = fn._render_card_as_markdown(_card(body="**📍 当前状态：**go"), "plan-a")

    assert "*📍 当前状态：*go" in rendered
    assert "\\*" not in rendered


def test_an_unpaired_bold_delimiter_is_left_literal():
    """Opening a bold run that never closes is a parse error.

    Telegram rejects the whole message, and because the card content is
    unchanged the failure repeats identically on every retry — a frozen
    card, which is the state this renderer just came out of.
    """
    rendered = fn._render_card_as_markdown(_card(body="a ** b"), "plan-a")

    assert "a \\*\\* b" in rendered


def test_inline_code_spans_survive_as_code_spans():
    """Cards put task ids in backticks, and MarkdownV2 keeps them.

    The delimiters must stay **raw** — an escaped backtick is a literal
    backtick, so a flat ``escape_markdown_v2`` over the whole run would
    turn every code span into visible punctuation.
    """
    rendered = fn._render_card_as_markdown(_card(body="running `[13]` now"), "plan-a")

    assert "`[13]`" in rendered


def test_an_unpaired_backtick_opens_nothing():
    rendered = fn._render_card_as_markdown(_card(body="a ` b"), "plan-a")

    assert rendered.count("`") % 2 == 0


def _markup_errors(body: str) -> list:
    """``[(index, char)]`` for reserved characters that are *not* safe.

    Walks the body the way Telegram's parser does: a backslash escapes
    the next character, a pair of backticks opens and closes a code
    span, and everything else reserved has to be escaped. The one
    exception is ``*``, which this module emits itself as emphasis.
    """
    reserved = set(MARKDOWN_V2_SPECIAL_CHARS) - {"*"}
    errors, inside_code, i = [], False, 0
    while i < len(body):
        ch = body[i]
        if ch == "\\":
            i += 2
            continue
        if ch == "`":
            inside_code = not inside_code
            i += 1
            continue
        if not inside_code and ch in reserved:
            errors.append((i, ch))
        i += 1
    if inside_code:
        errors.append((len(body), "unclosed code span"))
    return errors


def test_the_body_is_parseable_for_arbitrary_card_content():
    """The property the whole renderer exists to hold.

    Every one of these would make Telegram answer HTTP 400 ``can't
    parse entities`` for the entire message, not just the offending
    line — and a rejected body is re-sent unchanged on the next push,
    so it never recovers.
    """
    for content in [
        "plain",
        "1.0 done! (a/b) [13] - x_y",
        r"back\slash and \*already escaped\*",
        "`code with [brackets] and (parens)`",
        "**bold** and `code` and **`both`**",
        "unbalanced ** here",
        "unbalanced ` here",
        "`",
        "**",
        "~strike~ >quote# + - = | { } . !",
        "multi\nline\ncontent",
    ]:
        rendered = fn._render_card_as_markdown(_card(body=content, tag=None), "plan-a")
        assert _markup_errors(rendered) == [], (
            f"body {content!r} rendered to unparseable markup: {rendered!r}"
        )


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def _spy(monkeypatch, return_value):
    calls: list[tuple] = []

    def fake(text, config, last_message_id=None):
        calls.append((text, config, last_message_id))
        return return_value

    monkeypatch.setattr(fn, "send_or_edit_telegram", fake)
    return calls


def test_push_sends_escaped_markdown_through_the_package_transport(monkeypatch, telegram_env):
    calls = _spy(monkeypatch, return_value=1402)
    notifier = FeishuNotifier()
    state = PlanCardState(plan_id="plan-a")

    assert notifier._push_to_telegram(state, _card()) is True

    text, config, last_message_id = calls[0]
    assert "1\\.0 done\\!" in text
    assert config["enabled"] is True
    assert config["chat_id"] == telegram_env["chat"]
    assert last_message_id is None
    # The id that is live on the channel is what gets persisted, as a
    # string (the on-disk card-state JSON shape).
    assert state.telegram_message_id == "1402"
    assert notifier._telegram_pushes_ok == 1


def test_push_passes_the_persisted_string_id_back_to_the_transport(monkeypatch, telegram_env):
    calls = _spy(monkeypatch, return_value=1402)
    notifier = FeishuNotifier()
    state = PlanCardState(plan_id="plan-a", telegram_message_id="1402")

    assert notifier._push_to_telegram(state, _card()) is True
    assert calls[0][2] == "1402"


def test_push_keeps_the_new_id_when_the_edit_fell_back_to_a_send(monkeypatch, telegram_env):
    """The old card is gone; the live one is the freshly sent message."""
    calls = _spy(monkeypatch, return_value=7777)
    notifier = FeishuNotifier()
    state = PlanCardState(plan_id="plan-a", telegram_message_id="1402")

    assert notifier._push_to_telegram(state, _card()) is True
    assert calls[0][2] == "1402"
    assert state.telegram_message_id == "7777"


def test_push_counts_a_failure_when_nothing_was_delivered(monkeypatch, telegram_env):
    _spy(monkeypatch, return_value=None)
    notifier = FeishuNotifier()
    state = PlanCardState(plan_id="plan-a")

    assert notifier._push_to_telegram(state, _card()) is False
    assert notifier._telegram_pushes_failed == 1
    assert state.telegram_message_id is None


def test_push_runs_with_only_a_per_plan_chat_id(monkeypatch):
    """``TELEGRAM_CHAT_ID_<plan>`` alone is a supported configuration.

    The guard used to require the shared ``TELEGRAM_CHAT_ID`` as well, so
    an operator who configured only per-plan ids got a silent channel
    even though every id they needed was present.
    """
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_plan-a", "-100999")
    calls = _spy(monkeypatch, return_value=1)

    notifier = FeishuNotifier()
    assert notifier._push_to_telegram(PlanCardState(plan_id="plan-a"), _card()) is True
    assert calls[0][1]["chat_id"] == "-100999"


def test_push_does_not_call_the_transport_when_unprovisioned(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    calls = _spy(monkeypatch, return_value=1)

    notifier = FeishuNotifier()
    assert notifier._push_to_telegram(PlanCardState(plan_id="plan-a"), _card()) is False
    assert calls == []
    assert notifier._telegram_pushes_failed == 0  # not a failure — just disabled


def test_status_reports_the_channel_for_per_plan_only_setups(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:test-token")
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)
    monkeypatch.setenv("TELEGRAM_CHAT_ID_plan-a", "-100999")
    assert FeishuNotifier().stats()["telegram_enabled"] is True
