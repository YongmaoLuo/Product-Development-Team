# Claude Code `stream-json` recordings

Golden event baselines for the harness' event-identity resolution.

These files are **recordings of the real `claude` CLI**, not hand-written
objects. A hand-written fixture would silently turn *our assumption* of the
event shape into a contract; a recording keeps the contract anchored to what
the CLI actually emits.

Every line of every fixture is one standalone JSON document (JSONL),
compact-encoded exactly as the CLI writes it (`separators=(",", ":")`,
`ensure_ascii=False`).

| File | Lines | Terminal event | Purpose |
|---|---|---|---|
| `single_turn.jsonl` | 4 | `result` / `success` | One prompt → one text answer, no tools |
| `multi_turn_tools.jsonl` | 95 | `result` / `success` | `Bash ls` tool call + tool result, then a final answer |
| `error_and_partial_usage.jsonl` | 5 | `result` / `error_max_turns` | Error result whose `usage` lost its `cache_creation*` keys |
| `session_edge_cases.jsonl` | 4 | `result` / `success` | The `single_turn` recording with a degenerate session id: empty on three events, absent from one |

The "every line is a JSON document" rule holds for the four files above. It
does **not** hold for `malformed/` — see below.

## The negative samples: `malformed/`

`malformed/` holds the streams that are **meant not to parse**.

| File | Lines | Terminal event | Purpose |
|---|---|---|---|
| `malformed/truncated_tail.jsonl` | 4 | `result` / `success` | The `single_turn` recording with its third line cut off mid-write |

A write that is interrupted leaves a half-written line behind. That line is
not a JSON document, and the parser has to *report* it rather than swallow
it: a dropped line is a failure that leaves no trace. Pinning that needs a
sample that really does not parse, so the rule above cannot be applied to
this directory.

**The exemption is explicit and narrow:**

- Files under `malformed/` are **not** part of the parseable fixture set
  (`ALL_FIXTURES` in `backend/tests/unit/harness/test_stream_fixture_shape.py`
  and `backend/tests/unit/harness/test_claude_parse_identity.py`), so the
  "every line is a JSON document" check never looks at them.
- The files under `malformed/` **are** checked — by
  `test_the_malformed_sample_is_deliberately_unparseable`, which requires
  that they still contain an unparseable line. A negative sample that was
  "fixed" by accident would otherwise turn the exemption into a hole in the
  gate.
- The directory is still covered by the sanitisation gate
  (`backend/tests/static_gates/test_harness_fixtures_are_sanitized.py`): a
  sample that names the machine it was recorded on is a finding whether or
  not it parses.

## Provenance of the edited recordings

Two of the files are **derived from `single_turn.jsonl`** rather than
recorded separately. Their shape is the CLI's — every line is the
corresponding line of that recording, byte for byte — and only the one thing
each file exists to pin was changed.

| File | Derived from | What was changed |
|---|---|---|
| `session_edge_cases.jsonl` | `single_turn.jsonl` | The `session_id` of the init, the first assistant event and the result was set to the empty string `""`; the key was removed from the second assistant event. Nothing else was touched. |
| `malformed/truncated_tail.jsonl` | `single_turn.jsonl` | The third line (an assistant event) was cut off mid-write: the file ends in the middle of the key name `"cache_cre`. The other three lines are the recording's own. |

Both were written with the same encoder as the recordings
(`separators=(",", ":")`, `ensure_ascii=False`), so the edited lines stay
byte-identical to their source apart from the edit.

## Recording environment

| | |
|---|---|
| Recording date | **2026-10-23** (event timestamps `2026-10-23T13:53Z`) |
| CLI | Claude Code **2.1.302** (`claude_code_version` in every `init` event) |
| Provider model | `MiniMax-M3.1-Flash` (`init.model`, `result.modelUsage` key) |
| Upstream model | `deepseek-flash` (`assistant.message.model`) |
| Platform | macOS (darwin 25.6.0), zsh |
| Auth | `ANTHROPIC_API_KEY` present in the recording environment |

## Recording commands

The recordings were taken in throw-away directories under `/tmp` so that no
repository content and no home-directory path entered the stream. The raw
recordings were then sanitised into the committed files.