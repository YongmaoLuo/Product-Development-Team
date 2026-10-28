"""The quota-tier memory survives the process that wrote it.

What this pins
--------------
CC Switch reports the quota windows a provider *currently* carries.  A
provider on a plan with a weekly limit reports a ``weekly_limit`` tier
next to its ``five_hour`` burst window; a provider on a metered API
product reports no weekly tier at all.

The failover architecture (decision point 1) settles the ambiguity: a
missing weekly tier is treated as an exhausted weekly tier — but only
for a provider that has *ever* been seen to have one.  "This provider
has no weekly window" and "this provider's weekly window went missing"
are different facts, and only history tells them apart.

``backend/quota_tier_memory.py`` is the file layer that carries that
history across a restart, and these tests pin the *file* contract only —
no quota reader, no dispatcher, no routes:

* **round trip** -- what was recorded is what a fresh read gives back;
* **persistence** -- the fact survives the loss of the in-process cache,
  which is what a restart looks like from here;
* **degradation** -- a missing, truncated or non-map file reads as an
  empty memory rather than an exception, because "no history" is the
  answer every caller can act on;
* **isolation** -- one provider's history is nobody else's.

The three extra tests pin the boundary conditions the module promises:
an empty tier list writes nothing at all, the file location honours the
environment override before the config directory, and the memory is
**not** folded into ``provider-order.json`` — that file is the
optimizer's product and is rewritten wholesale by the optimizer, so a
memory living inside it would die with the next optimizer run.

Note that every test redirects ``PDT_QUOTA_TIER_MEMORY_FILE`` at
``tmp_path``: the real ``.config`` directory belongs to the operator's
machine, and a test that wrote there would be a test that changes the
answer of the next run.
"""

import json

import pytest

import quota_tier_memory
from quota_tier_memory import (
    has_seen_tier,
    memory_file_path,
    record_seen_tiers,
    reset_cache,
    seen_tiers,
)

#: A provider name spelled the way CC Switch spells it.
PROVIDER = "MiniMax Token Plan Huawei"

#: Another provider, for the isolation test.
OTHER_PROVIDER = "Vendor B API"


@pytest.fixture(autouse=True)
def memory_file(tmp_path, monkeypatch):
    """Point the memory at a throwaway file inside ``tmp_path``.

    The redirect is what keeps this suite away from the operator's real
    ``.config`` directory, so it is applied to every test rather than
    to the ones that happen to write.
    """
    path = tmp_path / "quota-tier-memory.json"
    monkeypatch.setenv("PDT_QUOTA_TIER_MEMORY_FILE", str(path))
    reset_cache()
    yield path
    reset_cache()


def test_record_then_read(memory_file):
    """A recorded tier is visible to a reader straight away."""
    record_seen_tiers(PROVIDER, ["five_hour", "weekly_limit"])

    assert has_seen_tier(PROVIDER, "weekly_limit") is True
    assert has_seen_tier(PROVIDER, "five_hour") is True
    assert seen_tiers(PROVIDER) == ("five_hour", "weekly_limit")


def test_persists_to_disk(memory_file):
    """The fact is on disk, not only in this process's memory."""
    record_seen_tiers(PROVIDER, ["five_hour", "weekly_limit"])

    # The in-process cache is gone -- this is what a restart looks like
    # from inside the module.
    reset_cache()

    assert has_seen_tier(PROVIDER, "weekly_limit") is True
    assert seen_tiers(PROVIDER) == ("five_hour", "weekly_limit")

    # And it really is on disk, so a *different* process would find it.
    on_disk = json.loads(memory_file.read_text(encoding="utf-8"))
    assert on_disk[PROVIDER] == ["five_hour", "weekly_limit"]


def test_corrupt_file_is_empty_memory(memory_file):
    """A truncated file reads as an empty memory, never as an exception."""
    memory_file.write_text("{", encoding="utf-8")
    reset_cache()

    assert has_seen_tier(PROVIDER, "weekly_limit") is False
    assert seen_tiers(PROVIDER) == ()


def test_providers_are_isolated(memory_file):
    """One provider's history is nobody else's."""
    record_seen_tiers(PROVIDER, ["weekly_limit"])

    assert has_seen_tier(OTHER_PROVIDER, "weekly_limit") is False
    assert seen_tiers(OTHER_PROVIDER) == ()
    assert has_seen_tier(PROVIDER, "weekly_limit") is True


def test_missing_file_is_empty_memory(memory_file):
    """A provider nobody recorded reads as empty, not as an error."""
    assert not memory_file.exists()

    assert has_seen_tier(PROVIDER, "weekly_limit") is False
    assert seen_tiers(PROVIDER) == ()


def test_non_map_document_is_empty_memory(memory_file):
    """A file whose top level is not a provider map reads as empty."""
    memory_file.write_text(json.dumps(["five_hour"]), encoding="utf-8")
    reset_cache()

    assert has_seen_tier(PROVIDER, "five_hour") is False
    assert seen_tiers(PROVIDER) == ()


def test_empty_tiers_are_not_written(memory_file):
    """Recording nothing writes nothing -- not even an empty file."""
    record_seen_tiers(PROVIDER, [])

    assert not memory_file.exists()
    assert seen_tiers(PROVIDER) == ()


def test_memory_file_path_prefers_the_env_override(memory_file):
    """``PDT_QUOTA_TIER_MEMORY_FILE`` wins over the config directory."""
    assert memory_file_path() == memory_file


def test_memory_file_path_falls_back_to_the_config_dir(tmp_path, monkeypatch):
    """Without the override the memory lives beside the other config."""
    monkeypatch.delenv("PDT_QUOTA_TIER_MEMORY_FILE", raising=False)
    monkeypatch.setenv("PDT_CONFIG_DIR", str(tmp_path))

    assert memory_file_path() == tmp_path / "quota-tier-memory.json"


def test_record_does_not_write_provider_order_json(memory_file, tmp_path):
    """The memory is not folded into the optimizer's own payload.

    ``provider-order.json`` is rewritten wholesale by the optimizer, so
    a memory kept inside it would not survive the next optimizer run —
    which is the whole reason this module exists.
    """
    order_file = tmp_path / "provider-order.json"
    order_file.write_text(json.dumps({"providers": {}}), encoding="utf-8")

    record_seen_tiers(PROVIDER, ["weekly_limit"])

    assert json.loads(order_file.read_text(encoding="utf-8")) == {"providers": {}}
    assert quota_tier_memory.memory_file_path() != order_file