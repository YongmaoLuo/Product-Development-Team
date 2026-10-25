# Browser-automation captures

This directory is where the browser-automation tooling (the Playwright MCP
server) writes the page snapshots and console logs it captures while a run
is inspected by hand.

The captures themselves are **not** part of the repository. A snapshot is a
picture of whatever happened to be on screen during one run; it carries the
identifiers of the plan that was open at the time, it is never read by
anything afterwards, and a repository that keeps them keeps a running log of
private names. The `.gitignore` beside this file keeps them out.

The directory itself is tracked, and it is one of the scan roots in
`backend/tests/static_gates/source_scan.py`. That is deliberate: the
privacy gates read every tracked top-level directory, and a directory that
no gate reads is a directory whose contents can be published without being
checked. The README is what the gates read here.
