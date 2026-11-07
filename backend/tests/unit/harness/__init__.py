# The harness unit suite is a package on purpose.
#
# Two things follow from this file, and both of them are about names.
#
# **The test modules get package-qualified names.** Without it pytest
# computes the module name of ``tests/unit/harness/test_foo.py`` from its
# basename alone (``test_foo``), and two test files with the same basename
# in different directories are then the same module — the second import
# silently returns the first one's module. With this file the name is
# ``backend.tests.unit.harness.test_foo``, which nothing else can claim.
#
# **The directory stays off ``sys.path``.** Pytest's default import mode
# puts the directory of a collected file on ``sys.path`` when that
# directory is not part of a package. A directory called ``harness`` on
# ``sys.path`` shadows the production ``harness`` package of the same
# name, and ``from harness.contract import ...`` then fails with
# ``ModuleNotFoundError: No module named 'harness.contract'``. As a
# package, this directory is imported as ``backend.tests.unit.harness``
# and never enters ``sys.path`` on its own.
#
# (The production package is a regular package too — ``backend/harness/
# __init__.py`` — so that it wins against a namespace portion of the
# same name wherever one turns up.)
#
# The suite's own fixtures do **not** live here: they are in
# ``tests/unit/conftest.py``, because pytest does not keep a directory
# conftest's fixtures for a directory that is collected more than once.
# That file's docstring explains the whole of it.
