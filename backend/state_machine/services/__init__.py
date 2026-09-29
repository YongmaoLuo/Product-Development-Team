"""Marker package for state_machine service classes (lightweight wrappers
that sit on top of the repository layer).  Kept intentionally empty so
``state_machine.services`` is a namespace package the test suite can
import even before the first concrete service lands.
"""