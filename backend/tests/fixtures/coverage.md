# Handcrafted Fixtures Coverage Tracker

**Function under test:** `SplitDecision.should_split`
**File:** `backend/verification_split.py`
**Verification point:** VP-016
**Test file:** `tests/fixtures/handcrafted/test_handcrafted_fixtures.py`
**Test command:** `source venv1/bin/activate && pytest tests/fixtures/handcrafted/ -v`

This file tracks coverage of the 5 early-return branches in
`should_split` plus the happy path. Each row is a single handcrafted
test case; the "Branch" column is the line in `verification_split.py`
that the case targets.

| # | Test function | Branch | Line | Early-return condition | Status |
|---|---------------|--------|------|------------------------|--------|
| 1 | `test_branch1_non_mapping_inputs_return_none` | B1 | 62 | `not isinstance(vp, Mapping) or not isinstance(result, Mapping)` | ✅ |
| 2 | `test_branch2_non_timeout_status_returns_none` | B2 | 65 | `result.get("status") != "timeout"` | ✅ |
| 3 | `test_branch3_expected_result_without_separator_returns_none` | B3 | 69 | `not isinstance(expected_result, str) or ";" not in expected_result` | ✅ |
| 4 | `test_branch4_empty_clauses_return_none` | B4 | 74 | `len(clauses) < 2` (after strip+filter) | ✅ |
| 5 | `test_branch5_missing_parent_id_returns_none` | B5 | 78 | `not parent_id` (empty / missing `id` field) | ✅ |
| 6 | `test_happy_path_produces_sub_vps` | Happy | 94+ | All rules hold → list of sub-VPs | ✅ |

## Summary

- **Total branches covered:** 5 / 5 early-return branches (100%)
- **Happy path covered:** 1 / 1 (100%)
- **Total fixture cases:** 6
- **Overall coverage:** 6 / 6 = **100%** ✅

## Run

```bash
source venv1/bin/activate && pytest tests/fixtures/handcrafted/ -v
```

Expected: `6 passed`.
