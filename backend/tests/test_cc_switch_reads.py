"""Re-export shim for the CC Switch reader tests.

The verification test_command for VP-004 invokes:

    pytest backend/tests/test_cc_switch_reads.py \
        --cov=backend.cc_switch --cov-report=term-missing

The canonical test suite lives at
``backend/tests/unit/test_cc_switch_reads.py``. This shim makes the
verification command's path resolve to that suite, and additionally
imports the source module under the dotted path ``backend.cc_switch``
so that ``pytest-cov`` measures coverage on the same module object the
``--cov`` flag names.

Both names moved on 2026-09-24: ``provider_config_consumer`` was merged
into ``cc_switch_db``, and the merged module was renamed ``cc_switch``.
The shim follows them so the historical ``test_command`` still resolves.
"""

import importlib.util
import sys
from pathlib import Path

# Import the source under the dotted path that --cov=backend.cc_switch
# expects, so coverage tracking binds to the same module instance the
# tests exercise.
import backend.cc_switch  # noqa: F401

_backend_test_path = (
    Path(__file__).resolve().parent / "unit" / "test_cc_switch_reads.py"
)

_spec = importlib.util.spec_from_file_location(
    "_unit_cc_switch_reads_tests", str(_backend_test_path)
)
_module = importlib.util.module_from_spec(_spec)
sys.modules["_unit_cc_switch_reads_tests"] = _module
_spec.loader.exec_module(_module)

# Re-export every test_* symbol so pytest discovers them under this
# file's node-id.
for _name in dir(_module):
    if _name.startswith("test_"):
        globals()[_name] = getattr(_module, _name)
