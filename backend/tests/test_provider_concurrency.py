"""Re-export shim for the provider-concurrency tests.

The verification test_command for VP-013 invokes:

    pytest backend/tests/test_provider_concurrency.py -v

The canonical test suite lives at
``backend/tests/unit/test_provider_concurrency.py``. This shim makes the
verification command's path resolve to that suite.
"""

import importlib.util
import sys
from pathlib import Path

_backend_test_path = (
    Path(__file__).resolve().parent / "unit" / "test_provider_concurrency.py"
)

_spec = importlib.util.spec_from_file_location(
    "_unit_provider_concurrency_tests", str(_backend_test_path)
)
_module = importlib.util.module_from_spec(_spec)
sys.modules["_unit_provider_concurrency_tests"] = _module
_spec.loader.exec_module(_module)

for _name in dir(_module):
    if _name.startswith("test_"):
        globals()[_name] = getattr(_module, _name)
