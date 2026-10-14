# Contributing

Thanks for helping out. Bug reports, fixes and small features are all welcome. For anything large, open an issue first so we can agree on the approach before you write it.

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
scripts/ci.sh
```

`scripts/ci.sh` runs exactly what CI runs: ruff, the unit tests, the simulated end-to-end runs and a dependency audit. Add `--image` if you touched the `Containerfile` or compose files. Please make sure it passes before opening a pull request.

To see what the bot's messages look like without running the bot, point `DISCORD_WEBHOOK_URL` at a test channel and run `.venv/bin/python devtool.py preview`.

## Project rules

These keep the bot correct under concurrent clicks and restarts. A pull request that breaks one will be asked to change.

- **All database writes go through `DBQueue`** (`utils/db_queue.py`). Never open a write connection anywhere else, and keep any read-modify-write logic inside one queue job.
- **Roster position is computed** from `(joined_at, id)`, never stored.
- **Alerts are claim → send → finish.** A row left in `sending` by a crash becomes `ambiguous` at startup and is never resent. An alert may be lost, but never posted twice.
- **User-controlled text never pings.** Always pass explicit `allowed_mentions`. Validate input with the `clean_*` helpers in `utils/sanitize.py` and escape output with `md()`.
- **Interaction handlers must always answer** (use `answer()`) within `INTERACTION_DB_TIMEOUT`.
- **Schema changes** are appended to `MIGRATIONS` in `utils/database.py` as idempotent callables. Never rebuild the `sessions` table: `ON DELETE CASCADE` would wipe the sign-ups.
- **No emoji in message text.** Emoji appear only on the sign-up buttons.
- **The container runs as uid 10001, never root.** Secrets are files under `secrets/`, not environment variables. Keep `cap_drop: ALL`, `no-new-privileges` and the read-only mounts in both compose files.
- **Use the exit codes** in `utils/exit_codes.py`, never a bare `sys.exit(1)`.

## Pull requests

- Add an entry under `[Unreleased]` in `CHANGELOG.md`.
- Add or update tests. Behaviour changes usually deserve a case in `tests/sim/` as well as a unit test.
- Keep dependencies minimal. If the standard library can do it, use the standard library.
- Never commit a real token, webhook URL or server ID. The tests use obviously fake IDs like `123456789012345678`.

By contributing, you agree that your contributions are licensed under the [Apache License 2.0](LICENSE).
