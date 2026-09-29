"""One-off maintenance / migration scripts for the backend.

Each module here is named ``migrate_<YYYYMMDD>_<slug>`` and exposes a
single pure-ish entry point that takes a plan directory and returns a
structured result. Scripts are import-safe (no side effects at import
time) so they can be unit-tested without touching a real plan.
"""
