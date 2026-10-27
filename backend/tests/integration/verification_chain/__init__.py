"""Integration tests for the verification subsystem.

The deterministic fixtures used by these tests live under
``backend/tests/fixtures/``:

``verification/vp-fixture-001``
    An all-deterministic plan: ``api_test`` x2, ``full_ci`` x1, ``e2e`` x1.
``verification/vp-fixture-002``
    A mixed plan: one ``code_review`` VP, one off-topic VP, one orphan VP.
``effect_report/baseline-sample.json``
    A baseline sample with the real numeric distribution and synthetic ids.
``failure_history/legacy_payload.json``
    A legacy round payload that carries no ``failure_fingerprints``.
"""
