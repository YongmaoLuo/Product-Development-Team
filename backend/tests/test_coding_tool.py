"""Re-export shim for the coding_tool unit tests.

The verification test_command for VP-018 invokes:

    pytest backend/tests/test_coding_tool.py -v

The canonical test suite lives at
``backend/tests/unit/test_coding_tool.py``. This shim makes the
verification command's path resolve to that suite by re-exporting
both module-level ``test_*`` functions and ``Test*`` classes from
the canonical file, so pytest discovers them under this file's
node-id. Mirrors the pattern in ``test_cc_switch.py``.
"""

import importlib.util
import sys
from pathlib import Path

_backend_test_path = (
    Path(__file__).resolve().parent / "unit" / "test_coding_tool.py"
)

_spec = importlib.util.spec_from_file_location(
    "_unit_coding_tool_tests", str(_backend_test_path)
)
_module = importlib.util.module_from_spec(_spec)
sys.modules["_unit_coding_tool_tests"] = _module
_spec.loader.exec_module(_module)

for _name in dir(_module):
    if _name.startswith(("test_", "Test")):
        globals()[_name] = getattr(_module, _name)
