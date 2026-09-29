# Configuration

## `backend/config.yaml` is intentionally thin

Three things are worth knowing:

- **Nothing is required to run.** It ships with no provider order file and
  no subsidiary processes. The server boots and the workflow runs; features
  that need an external component simply have nothing to read.
- **`provider_order_file`** points at a JSON contract describing a fallback
  provider chain. There is no built-in chain — if you want provider
  failover, point this at whatever produces that file. The
  `PROVIDER_ORDER_FILE` environment variable overrides the config value.
- **`subsidiary_processes`** is an empty list. It is a generic mechanism for
  bringing up long-running helper processes during the FastAPI lifespan,
  with two load-bearing properties: entries start **in order**, and a failed
  start **aborts startup** unless the entry is marked `optional: true`.

## `example/` vs `.config/`

Operator-specific configuration — provider concurrency caps and provider
routing — ships as **committed templates** under `example/`, and you copy
them into the gitignored `.config/` at the repository root to customise
them:

```bash
mkdir -p .config
cp example/provider_capacity.yaml.example .config/provider_capacity.yaml
cp example/provider_routing.yaml.example  .config/provider_routing.yaml
# edit each copy — replace the placeholder patterns with your own
# provider names, then save.
```

`.config/` is gitignored **on purpose**: a table of concrete names in source
would compile one deployment's providers into every install. There is
deliberately **no** built-in example-with-real-names — every installation
starts from the placeholder bundle and adds its own rows.

Both files are also overridable from the environment, so a deployment that
keeps its config elsewhere does not need to drop a file into the repo root:

| Environment variable | Overrides |
|---|---|
| `PDT_PROVIDER_CAPACITY_FILE` | path to the capacity YAML |
| `PDT_PROVIDER_ROUTING_FILE` | path to the routing YAML |

!!! warning "Do not name a provider on a template"
    If a contribution adds a new external provider, do **not** edit the
    example files to name it. A row of concrete names belongs in
    `PDT_PROVIDER_*` config on the deploying machine — putting it into
    `example/` would compile one install's provider list into every
    install's onboarding. Read the header of each example file for the
    placeholders the schema accepts.

## Verification bounds

`backend/configs/verification.yaml` holds the verification loop's limits:

| Key | What it bounds |
|---|---|
| round budget | how many verify → repair → re-verify rounds may run |
| per-VP timeouts | how long one verification point may take |
| `parallelism_cap` | one method-group's own fan-out |

`parallelism_cap` is **not** the fleet ceiling. Groups run concurrently, so
per-group caps multiply; the real limit is a `plan_semaphore` created once
per round and shared by every group. See
[Verification](../architecture/verification.md).

## Credentials

Credentials come from `backend/.env`, which is gitignored.
`backend/.env.ci` holds committed **placeholders** — it is a test fixture,
and a real key must never go into it.

## Runtime state

Everything a checkout *produces* rather than *ships* lives under a single
gitignored `<repo>/.pdt/` directory: the state database, its WAL siblings,
the server boot counter, and the rotating shutdown backups of the database.

Keeping them together is deliberate. They are one unit — the boot counter
and the backup directory both derive their location from the database's
parent — so relocating the database relocates all four, and nothing that a
checkout produces sits next to the files it ships.

!!! note "Moving the state database"
    SQLite in WAL mode keeps committed transactions in `-wal` until a
    checkpoint. Stop the server **gracefully** (so SQLite checkpoints on
    close), confirm `-wal` is empty, and only then move the files — a
    database copied without its WAL loses everything still in it. The
    path is declared once, in `backend/config_paths.py`; do not reconstruct
    it from `__file__` anywhere, which is a mistake this project has made
    twice and gated against
    (`backend/tests/static_gates/test_state_db_path_has_one_resolver.py`).
