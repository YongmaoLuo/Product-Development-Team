# AGENTS.md

Entry point for any coding agent working in this repository — Claude Code, a
scripted executor, or a person following the same habits. It is deliberately
short: the rules themselves live in
[docs/development/project-rules.md](docs/development/project-rules.md)
(Chinese mirror: [project-rules.zh.md](docs/development/project-rules.zh.md)).

Read them before you change a test, a CI lane, or open a pull request. Each one
names the failure it prevents, so the edge cases can be judged rather than
guessed. The load-bearing ones:

- A product contract change updates every test that asserts the old contract.
- Tests of asynchronous operations wait for the terminal condition.
- Tests do not depend on module identity.
- One pull request per piece of work; fixes go to the existing branch with
  `git push --force-with-lease`; `main` is protected and never force-pushed.
- Every commit on the branch must be CI-green.
- A merge is verified: `git diff --stat <old-main>..main` contains exactly the
  PR's files.
