# Runtime scratch

`tmp/` is where the application writes while it runs: the verification
servers' logs, the SQLite state databases, the backups taken before a
migration, and the per-run plan directories a plan's own
`services[].start_cmd` creates.

None of it is source. The plan directories under `tmp/pdt-verify/plans/`
hold generated plan data, and they are excluded from the privacy scan for
the same reason the plan directories elsewhere in the tree are.

The directory is tracked, and it is one of the scan roots in
`backend/tests/static_gates/source_scan.py`. That is deliberate: the
privacy gates read every tracked top-level directory, and a directory that
no gate reads is a directory whose contents can be published without being
checked. The README is what the gates read here.
