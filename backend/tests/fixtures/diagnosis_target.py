"""Target test module used by test_diagnosis_output.py.

This fixture contains 10 test functions whose collection line numbers are
baked into backend/tests/fixtures/diagnosis.json.  The tests themselves are
no-ops (they pass), but the diagnosis JSON treats them as failures so the
validation tests can verify structure, buckets, error types, and line-number
accuracy against real pytest --collect-only -q output.
"""


def test_bucket_assertion_alpha():
    """Placeholder for assertion-category failure."""
    pass


def test_bucket_assertion_beta():
    """Placeholder for assertion-category failure."""
    pass


def test_bucket_import_gamma():
    """Placeholder for import-category failure."""
    pass


def test_bucket_import_delta():
    """Placeholder for import-category failure."""
    pass


def test_bucket_timeout_epsilon():
    """Placeholder for timeout-category failure."""
    pass


def test_bucket_timeout_zeta():
    """Placeholder for timeout-category failure."""
    pass


def test_bucket_syntax_eta():
    """Placeholder for syntax-category failure."""
    pass


def test_bucket_missing_fixture_theta():
    """Placeholder for missing-fixture-category failure."""
    pass


def test_bucket_runtime_iota():
    """Placeholder for runtime-category failure."""
    pass


def test_bucket_runtime_kappa():
    """Placeholder for runtime-category failure."""
    pass
