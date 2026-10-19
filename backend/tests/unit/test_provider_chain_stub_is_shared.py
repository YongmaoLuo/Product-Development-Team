"""The provider-chain stub is one installer both trees call.

The bug this pins: the stub used to be the session fixture
``_provider_chain_from_cc_switch``, declared in ``backend/tests/conftest.py``.
A fixture belongs to the conftest that declares it, so an invocation that
collected only ``state_machine/tests`` got no provider chain at all -- its
route tests really dialled CC Switch's ``http://127.0.0.1:4000`` and five of
them failed with ``ProviderOrderError``. Five failures that appear only when
the command line happens to name the other tree first is not a suite.

Three properties are checked here, and none of them opens a socket:

  1. **Idempotence.** Both conftests call the installer and pytest may import
     either first, so the second call has to be a no-op.  Concretely it must
     not overwrite the saved real reader with the stub it just installed --
     otherwise ``real_provider_chain_from_api`` hands back the stub and every
     test about the real transport silently exercises the stub instead.
  2. **Both roots call it.** A regression guard on the two conftest texts, so
     deleting either call fails here rather than in CI.
  3. **Neither root declares its own copy**, or the two can drift apart again.
"""
from __future__ import annotations

from pathlib import Path

from _pytest_hermetic_env import (
    install_provider_chain_stub,
    real_provider_chain_from_api,
)

_BACKEND = Path(__file__).resolve().parents[2]
_CONFTESTS = ("tests/conftest.py", "state_machine/tests/conftest.py")


def test_installing_twice_still_reports_the_stub_chain():
    """The second install must not disturb the first one's answer."""
    import provider_order

    install_provider_chain_stub()
    install_provider_chain_stub()

    assert provider_order._chain_from_api() == ["parent"]


def test_the_saved_real_reader_is_not_the_stub():
    """Idempotence, observed from the side that would break if it were wrong.

    This is the half a chain assertion cannot see: the second install would
    happily re-stub ``_chain_from_api`` and the assertion above would still
    pass, while ``real_provider_chain_from_api`` -- what ``real_cc_switch``
    puts back -- had become the stub. Sixty-odd tests across fifteen files
    ask for that fixture, so the failure would be green everywhere it
    matters and wrong everywhere it is read.

    The comparison is against the reader's *defining module*, not its
    identity. Every install mints a fresh lambda, so after two unguarded
    installs the saved reader and the module attribute are two different
    stub objects and ``real is not provider_order._chain_from_api`` passes
    on the broken build — measured, by removing the idempotency guard and
    watching this file stay green. Only where the function was defined
    tells them apart: the real reader is ``provider_order``'s own, the stub
    belongs to this module. No call, so no socket.
    """
    install_provider_chain_stub()

    real = real_provider_chain_from_api()

    assert real is not None, "no real reader was ever saved"
    assert real.__module__ == "provider_order", (
        f"install_provider_chain_stub() saved a stub as the real reader "
        f"(it is defined in {real.__module__!r}, not provider_order); "
        f"real_cc_switch would put the stub back"
    )


def test_both_conftests_install_the_shared_stub():
    """Neither root may lose the call without this failing."""
    for rel in _CONFTESTS:
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "install_provider_chain_stub()" in text, (
            f"{rel} no longer installs the shared provider-chain stub; "
            f"running that tree alone would dial CC Switch for real"
        )


def test_neither_conftest_declares_its_own_stub_anymore():
    """The tree-local copy must be gone, or the two can drift apart again.

    Matched on the fixture *declaration*, not the bare name: ``tests/
    conftest.py`` names the old symbol in a comment explaining what replaced
    it, and deleting that history to satisfy a substring match would trade a
    real explanation for a grep. A ``def`` of that name is the thing whose
    return would re-introduce the split.
    """
    for rel in _CONFTESTS:
        text = (_BACKEND / rel).read_text(encoding="utf-8")
        assert "def _provider_chain_from_cc_switch" not in text, (
            f"{rel} still declares the session fixture the shared "
            f"installer replaced"
        )
