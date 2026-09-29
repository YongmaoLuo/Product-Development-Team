"""Handcrafted fixtures for verification_split.SplitDecision.should_split.

This package contains 6 handcrafted test cases that exhaustively cover
the 5 early-return branches of ``SplitDecision.should_split`` plus one
positive case (the happy path). The fixtures are intentionally
self-contained (no shared state, no I/O) so the policy under test can
fail in isolation.

Branches covered (see verification_split.py:should_split):

  Branch 1 (line 62): ``not isinstance(vp, Mapping) or not isinstance(result, Mapping)``
  Branch 2 (line 65): ``result.get("status") != "timeout"``
  Branch 3 (line 69): ``not isinstance(expected_result, str) or ";" not in expected_result``
  Branch 4 (line 74): ``len(clauses) < 2`` after strip+filter
  Branch 5 (line 78): ``not parent_id`` (empty / missing id)
  Happy    (line 94+): valid input → list of sub-VPs

Each fixture file below targets exactly one of these branches.
"""
