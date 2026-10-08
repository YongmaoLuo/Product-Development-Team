# Moving notification credentials into the keychain

The two real notification secrets — the Feishu app secret and the Telegram
bot token — can be read from a dedicated macOS keychain instead of from
`.env`, and handed to the processes that need them over a file
descriptor rather than through the environment.

**This is opt-in and ships disabled.** A deployment that changes nothing
behaves exactly as before. Read this page before assuming the protection
is active: the mechanism being available and the mechanism being in use
are two different facts, and only the second one changes what an
attacker on the same machine can read out of a process listing.

## What changes, and what does not

The index keys stay in `.env` — `FEISHU_APP_ID` and
`TELEGRAM_CHAT_ID` are configuration, not credentials, and moving them
would put a non-secret under credential management for no gain. Only the
two secrets move.

| | before | after |
|---|---|---|
| index keys | `.env` | `.env` (unchanged) |
| secrets | `.env` → `os.environ` | keychain → pipe → fd number in the environment |
| `ps eww <pid>` shows the secret | yes | no |
| Linux / Windows | unchanged | unchanged |

The switch is `PDT_DISABLE_KEYCHAIN_SECRETS`, and the name reads
backwards on purpose: it **disables** the keychain, so the keychain is on
when the variable is *absent or* set to `0` or `false`. Every other
spelling — `true`, `1`, `yes`, a typo, a stray space — leaves it off.
Reading it the other way would mean shelling out per lookup against a
store the deployment has not finished populating, which fails in a way
only an operator can debug. The cost of guessing wrong the other way is a
secret that stays in the environment a little longer, which is the state
the installation is already in.

## Before you start

You need macOS. On any other platform the keychain is not consulted at
all and there is nothing to migrate — the switch is not even read.

Confirm the deployment currently has working credentials, because every
step below is verified by "notifications still arrive", and that check is
meaningless if they were already broken:

```bash
backend/.venv/bin/python3 -m backend.cli secrets verify
```

Every line must read `source=os.environ` and no line may read
`source=missing`.

## 1. Create the dedicated keychain

The provider reads a keychain of its own rather than your login keychain,
so the items it touches are not mixed into the several hundred a login
keychain accumulates.

```bash
security create-keychain -p "runtime-secrets" ~/Library/Keychains/runtime-secrets.keychain-db
security unlock-keychain -p "runtime-secrets" ~/Library/Keychains/runtime-secrets.keychain-db
security set-keychain-settings -lut 21600 ~/Library/Keychains/runtime-secrets.keychain-db
```

The third line sets a lock timeout; without it the keychain locks on its
own default schedule and every notification send starts by blocking on a
prompt nobody is there to answer.

## 2. Put the two secrets in

The account is the **index key's value**, not the secret and not a service
name. The provider locates entries by account alone and never passes a
`-s`, so the account has to be the thing that is already in your `.env`.

```bash
# read the index values out of .env without echoing them
FEISHU_APP_ID=$(grep -m1 '^FEISHU_APP_ID=' .env | cut -d= -f2-)
TELEGRAM_CHAT_ID=$(grep -m1 '^TELEGRAM_CHAT_ID=' .env | cut -d= -f2-)

security add-generic-password -U -a "$FEISHU_APP_ID"   -w "$(grep -m1 '^FEISHU_APP_SECRET=' .env | cut -d= -f2-)" ~/Library/Keychains/runtime-secrets.keychain-db
security add-generic-password -U -a "$TELEGRAM_CHAT_ID" -w "$(grep -m1 '^TELEGRAM_BOT_TOKEN=' .env | cut -d= -f2-)" ~/Library/Keychains/runtime-secrets.keychain-db
```

`-U` updates in place, so re-running after a credential rotation is safe.

Verify before going further. Both lines must read `source=keychain`:

```bash
PDT_DISABLE_KEYCHAIN_SECRETS=0 backend/.venv/bin/python3 -m backend.cli secrets verify
```

!!! warning "An interactive authorization prompt means a non-interactive reader cannot read it"
    `security find-generic-password -w` returns exit status 128 with no
    output when the item's access control asks for authorization the
    calling process cannot supply. A server process has no terminal to
    answer a prompt on, so an item created in a way that requires
    per-read approval will make every notification silently fail to
    resolve.

    `secrets verify` reports this as `source=missing` rather than as an
    error, which is the symptom to recognise. Check the item's access
    control — `security dump-keychain <path>` lists it — and widen it to
    allow the reading application, or recreate the item with `-T` naming
    the binary that needs it.

## 3. Turn it on

```bash
echo 'PDT_DISABLE_KEYCHAIN_SECRETS=0' >> .env
```

Then remove the two secrets from `.env`. Leaving them there is not a
mistake the system will catch for you: with the keychain enabled they are
simply ignored, and the day the switch is turned back off they are
already stale.

```bash
# keep a copy outside the repo until notifications have been confirmed working
cp .env "$HOME/.pdt-env-backup"
```

## 4. Confirm

Restart the server and check that a notification actually goes out. The
provider resolves a secret once per process and caches both hits and
misses, so a running process keeps whatever it decided at first use —
turning the switch on underneath a live process changes nothing until it
restarts.

```bash
backend/.venv/bin/python3 -m backend.cli secrets verify   # both lines: source=keychain
```

Then send something. The status endpoint reports the source per channel
without printing a credential:

```bash
curl -s -H "X-PDT-Request: 1" http://127.0.0.1:8000/api/notifications/status
```

## Rolling back

The keychain path is additive, so rollback is removing one line:

```bash
sed -i '' '/^PDT_DISABLE_KEYCHAIN_SECRETS=/d' .env
cp "$HOME/.pdt-env-backup" .env
```

The keychain and its items are left in place. Deleting the keychain
(`security delete-keychain <path>`) is separate and irreversible, so it is
not part of the rollback — do it only once you are sure you will not
switch back on.

## Rotating a credential

Write the new value to the keychain, confirm it, then remove the old one
from `.env` if a copy is still there. `.env` is not read while the
keychain is enabled, so a rotation that only updates the keychain is
complete; a rotation that updates both is not, because the `.env` copy
becomes the one that gets used the moment the switch is turned off.

## Adding a third secret

`backend/credentials.py` holds the registry — one `SecretSpec` row per
secret, naming the environment variable the value falls back to and the
one carrying the keychain account. Add the row and the static gate that
forbids reading a secret out of the environment picks it up on the next
run, because it derives its list from the registry rather than from a
copy. The `secrets` CLI subcommands iterate the same registry, so they
report the new secret without further change.

## What this does not protect against

Reading the keychain still requires running as you. A local process
under your account can read anything you can read, and the keychain
raises the cost of the *accidental* case — a process listing, a crash
dump, a log line — rather than the cost of a determined one. See
[Security](../security.md) for the full trust model.
