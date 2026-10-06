# 10-Man Discord Bot — Claude Context

## What this project does
discord.py bot for organizing CS 10-mans: `/tenman start` posts a sign-up with In / Maybe / Out / +1 buttons, keeps a first-come roster plus waitlist, posts a daily "need N more" countdown alert until the game is full, and shares the lobby link. Infrastructure is copied from `../steam_tracker_bot` and being ported piece by piece.

Running working doc: `docs/working-doc.html` (update it as work progresses; add a progress-log row per milestone).

## Stack
- Python 3.14, **discord.py 2.7**, **SQLite** (WAL) behind a single-writer queue, `requests` for the webhook
- Local venv: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt`

## Key files
| File | Purpose |
|---|---|
| `bot.py` | Entry point: `/tenman start|schedule|list|lobby|cancel`, persistent buttons (DynamicItem `tenman:<action>:<session_id>`), `tick()` = series → scheduled posts → close started → daily backup → alerts (60 s), heartbeat (30 s) |
| `devtool.py` | Webhook-only testing: `ping`, `demo --when "fri 9pm" --players 6`, `alerts`, `list` (uses `data/dev.db`) |
| `utils/db_queue.py` | `DBQueue`: one writer thread, one transaction per job, `run()` (async) / `call()` (sync) / `read()` (read-only conn) |
| `utils/database.py` | Schema, migrations, all queries. Write functions take `conn` first and are submitted to the queue |
| `utils/roster.py` | Pure roster math: confirmed / waitlist / maybe from join order |
| `utils/alerts.py` | Daily / game-day / full alert schedule, rendering, claim-before-send pass |
| `utils/webhook.py` | Webhook POST with the steam tracker's no-duplicate retry contract |
| `utils/when.py` | Start-time parser |
| `utils/embeds.py` | Sign-up embed, button definitions, numbered list / series lines |
| `utils/style.py` | Palette from the avatar, progress bar, brand |
| `utils/sanitize.py` | Input validation (`clean_*`) and output escaping (`md`) |
| `healthcheck.py` | Container healthcheck on the heartbeat |
| `tests/sim/` | Simulated end-to-end: `harness.py` drives real callbacks against fake Discord, `fakes.py` fake webhook server |
| `scripts/ci.sh` | Local run of the CI checks (`--image` to build and test the container) |
| `utils/config.py` | Typed settings from `.env` (`PASTE_` placeholders = unset) |

## Conventions
- Every DB write goes through `DBQueue` — never open a write connection elsewhere. Read-modify-write logic belongs inside ONE queue job
- Roster position is computed from `(joined_at, id)`, never stored
- Alerts: claim → send → finish. `sending` left by a crash becomes `ambiguous` at startup and is never resent
- User-controlled text never pings: always pass explicit `allowed_mentions`
- Update `CHANGELOG.md` under `[Unreleased]` before code changes; use `utils/exit_codes.py` constants
- Retired steam files live in the gitignored `deadcode/`; nothing in the bot imports them
- Full documentation: `docs/README.md` (aspect docs 01-21, components in 22); update the relevant doc with any behaviour change

- Session lifecycle: `status` open→cancelled|done; `post_state` scheduled→posting→posted. Only open+posted sessions take clicks/alerts
- Schema changes: append to `MIGRATIONS` in `utils/database.py` as an idempotent callable; never rebuild `sessions` (ON DELETE CASCADE wipes signups)
- Interaction paths use `INTERACTION_DB_TIMEOUT` (2 s) and must always answer (use `answer()`)
- No emoji in message text; emoji only on the sign-up buttons
- Never run a second bot instance with the same token (local + VPS) — both would answer clicks and post alerts
- The bot runs as `tenman` (uid 10001), never root: keep `USER 10001:10001` in the Containerfile and `user: "10001:10001"` in both compose files; `bot.py` refuses real root
- Secrets are files: the token/webhook come from `secrets/` via `DISCORD_TOKEN_FILE` / `DISCORD_WEBHOOK_URL_FILE` (`config.read_secret`); never put them back into env in compose. `secrets/` must stay in `.gitignore` and `.containerignore`
- Compose mounts use the short `:Z` syntax (long `bind.selinux` and compose `secrets:` aren't relabelled under SELinux); keep `cap_drop: ALL`, `no-new-privileges`, the noexec `/tmp`, limits and `pull_policy: never` in both files
- Any container change: run `scripts/docker-smoke.sh` (proves the hardening in a live container) and `scripts/ci.sh --image`

## Tests and CI
`scripts/ci.sh` (lint, unit, sim, audit; `--image` adds container build/test). GitHub: `.github/workflows/ci.yml` on push/PR, `deploy.yml` on `v*` tags.
