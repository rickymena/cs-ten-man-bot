# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Infrastructure was copied from steam_tracker_bot on 2026-10-04; its history lives in that repo.

---

## [Unreleased]

Newest sections first.

### Added (edit and update)
- `/tenman edit [number] [when] [title] [slots] [post_days] [lobby]`: change one game, posted or scheduled, numbered and autocompleted like cancel/lobby. Organizer or moderator only; at least one option; every input through the existing validators
- Scheduled game: changed quietly (ephemeral reply); `post_at` recomputed with the `/tenman schedule` rule (N days before at the game's wall-clock time, never before now). Without `post_days`, a weekly game uses the series' value and a one-off game reads it back from its `post_at` (default `SCHEDULE_POST_DAYS_BEFORE` if it can't). Weekly games are capped at 6
- Posted game: roster kept, sign-up redrawn, a public note says what changed (`allowed_mentions` users only, never the role). A smaller roster pings the players pushed onto the waitlist; a bigger one pings those promoted. Can't be moved into the past or more than 30 days ahead; `post_days` is ignored with a note
- Time change on a posted game: the gameday/starting alert rows are cleared only when their new moment is still ahead, so an alert already sent is never sent twice; daily rows (one per calendar day) are kept; rows mid-send are never touched. A slot change that leaves the roster not full re-arms the "full" alert
- A default title ("Friday 10-man") follows a new weekday; a custom title is kept
- `/tenman update [series] [when] [title] [slots] [post_days] [lobby] [apply_to_next]`: change a weekly series for future weeks (`S<id>` from `/tenman list`, autocompleted; `post_days` capped at 6). `apply_to_next:true` also edits the upcoming game (moved to the new weekday on or after its current day); if that game can't be edited the series change still applies and the reply says why
- Atomic: each edit/update is one `DBQueue` job with `INTERACTION_DB_TIMEOUT` that re-checks the game is still open (not cancelled, started or mid-post) and computes the before/after rosters, so it can't interleave with a click
- Code: `utils/database.py` `edit_session`, `update_series`, `next_series_session`, `EditRefused`, `default_title`; `bot.py` `edit` / `update` commands, `change_phrases`, `edit_note`, `user_mentions`, `series_token`; `/tenman list` footer mentions edit/update. No schema change (still version 3)
- Tests: `tests/test_edit.py` (database layer, alert re-arming, v3 database) and `tests/sim/test_edit_e2e.py` (permissions, note without role ping, redraw, hostile input, race with clicks, series update)
- Fixed before release: moving a weekly game past the next week's slot made the series skip that week. Each series game now records its original weekly slot (`series_slots`, filled by `materialize_series` and on the first move of a game) and the series counts weeks by slot. `series_slots` is additive and created without a schema version bump, so the database stays at version 3 and v0.1.0 can still open it
- Fixed before release: a game moved after its game-day alert could get a second ping that day from the daily alert. Start-tied alert rows are now kept as history (`once@<old start>`) instead of deleted, and the daily alert is skipped on a day a game-day ping already went out (327 tests; the 5 new tests fail on the pre-fix code)

### Security (container hardening)
- The bot has its own unprivileged user, `tenman` (uid/gid 10001, no shell, no home), created in the image and set as its default `USER`; both compose files run as it (`HOST_UID`/`HOST_GID` removed). Host folders are owned by 10001 (`sudo chown` / `podman unshare chown`); a missing or unwritable folder fails closed with exit 7
- The bot refuses to start as real root (`running_as_real_root()`, exit 7; rootless Podman's mapped root is allowed; `TENMAN_ALLOW_ROOT=1` overrides)
- Token and webhook URL moved out of the environment into read-only files (`secrets/` mounted at `/run/secrets`, read via `DISCORD_TOKEN_FILE` / `DISCORD_WEBHOOK_URL_FILE`): no longer visible in `docker inspect` or `/proc/<pid>/environ`. A plain `:ro,Z` bind instead of compose `secrets:`, which compose doesn't relabel for SELinux
- Both compose files: `cap_drop: [ALL]`, `no-new-privileges`, `noexec,nosuid,nodev` size-capped `/tmp`, `ipc: private`, 512 MB / 1 CPU / 128 process limits, `pull_policy: never`, rotated logs; short `:Z` mounts (the long syntax's `bind.selinux` wasn't applied, which blocked writes)
- `scripts/docker-smoke.sh` proves each setting in a running container; `secrets/` added to `.gitignore` and `.containerignore`
- Code: `utils/config.py` `read_secret(name)` (`NAME_FILE` wins over `NAME`; an unreadable file is a `ConfigError` that never echoes the value), used for `DISCORD_TOKEN` and `DISCORD_WEBHOOK_URL` and by `utils/webhook.get_webhook_url`; `bot.py` `running_as_real_root()` (reads `/proc/self/uid_map`) checked first in `main()`
- Image: `Containerfile` creates `tenman` (`groupadd`/`useradd --uid 10001 --no-create-home --no-log-init --shell /usr/sbin/nologin`) and sets `USER 10001:10001`; the `dev` (test) stage switches back to root
- Error messages tell you the real fix: the root refusal says to keep `user: "10001:10001"`, the unwritable-folder message gives the exact `chown` / `podman unshare chown` command (they still mentioned the removed `HOST_UID`)
- Smoke test reworked: deploy folder prepared exactly as the guide says (secrets files, chown to 10001 via a root helper container with a shared `:z` label, so no `sudo` needed), then 21 checks: default user, identity, capabilities, NoNewPrivs, real-root refusal (real Docker only), read-only code, writable volumes, noexec `/tmp`, no `.env` in the image, missing folder fails closed, token via file and absent from the environment and `compose config`, pids/memory limits, database/backup/restore as `tenman`, healthcheck
- Tests: secret files (file wins, unreadable file, empty webhook), real-root detection for normal user / rootless Podman / real root, refusal exits 7, webhook URL from a secret file (292 tests)
- Guide (`guides/guide-deploy.txt`): A1 creates `secrets/` (optional named `tenman` host account for Docker), A3 rewritten (secret files, `.env` without the token, chown to 10001), new error hints in A5, database move and restore run inside the container as `tenman`; `.env.sample` explains keeping the token out of `.env` in containers
- Local deployment migrated in place (token moved into `secrets/`, removed from `.env`, folders chowned, data kept); release rebuilt and `dist/release-v0.1.0/` refreshed with `SHA256SUMS` (image sha256 `ebdaf241...`)
- Docs updated for all of the above (09, 11-22); new change note `docs/23-container-hardening.md`

### Added (documentation)
- `docs/`: 21 aspect documents (overview to troubleshooting) and a component-level document (`docs/22-components.md`: 18 components with job, interface, dependencies, state, failure handling, tests; interaction sequences; dependency matrix), indexed in `docs/README.md`

### Fixed (found while documenting)
- GitHub workflows built with plain `docker build` (no `-f Containerfile`, no `--load`) and deployed an image tagged `tenman-bot`, which neither compose file runs; both workflows now use `build.sh`, and CI runs `scripts/docker-smoke.sh`
- `devtool.py alerts` could ping the real role; it now needs `--ping` like `demo`
- `devtool.py ping` message had an emoji; stale `build.sh` header and `CLAUDE.md` notes updated
- Weekly series: an occurrence created after its post time had passed was stored as posted with no message (nobody could join); it is now scheduled as due now and posted on the next pass. Weekly `post_days` capped at 6 (one game ahead)
- Posting a scheduled sign-up: 500/502/503 are now retried (definite failures); only 504 and timeouts count as unknown
- An image older than the database exits 7 with a message instead of a traceback; a failed daily backup waits 30 min before retrying instead of retrying every minute
- Relative `DB_PATH`/`BACKUP_DIR` resolve against the project folder; `python -m utils.backup` loads `.env`; `.env.sample` lists every setting
- "9:30" without am/pm now means PM like a bare "9"; `/tenman list` cuts long output on a line boundary; series lines show the zone at the next game; button errors keep "In" capitalised
- `scripts/docker-smoke.sh` builds `:smoke` with the new `TAG_LATEST=0` and never moves `:latest` (it had silently swapped a running local deployment's image)
- `scripts/ci.sh --image` no longer retags `localhost/tenman-bot:latest` (it would have replaced a running local deployment's image); healthcheck default path matches the bot's; unused `SCHEDULER_LOG` removed

### Added
- `utils/db_queue.py`: single-writer queue in front of SQLite. One thread owns the write connection; every job is one `BEGIN IMMEDIATE` transaction; bounded queue + per-call timeout raise `QueueBusy` instead of hanging; jobs whose caller timed out are cancelled before they run; reads use separate read-only WAL connections
- `utils/database.py` (ported from `database_entry_comparision.py`): SQLite schema for sessions, signups and alerts; `schema_migrations` + ordered `MIGRATIONS` + `SchemaVersionError` kept from the steam tracker
- `utils/roster.py`: confirmed / waitlist / maybe computed from join order, never stored, so a dropout promotes the waitlist head with no extra write
- `utils/alerts.py`: daily countdown alert ("6/10 signed up, need 4 more") at `ALERT_TIME`, game-day alert `GAMEDAY_ALERT_HOURS_BEFORE` hours out, one "full" post; claim-before-send per (session, kind, slot) so restarts never double-post; missed slots sent late within `ALERT_CATCHUP_HOURS`
- `utils/when.py`: start-time parser ("fri 9pm", "tomorrow 21:00", "2026-10-09 21:00")
- `utils/config.py`, `.env.sample`: settings with `PASTE_` placeholders treated as unset
- `utils/embeds.py`, `bot.py`: discord.py bot with `/tenman start|lobby|cancel|list`, persistent In / Maybe / Out / +1 / -1 buttons (DynamicItem), waitlist promotion pings, 60 s alert + heartbeat loop
- `devtool.py`: `ping`, `demo`, `alerts`, `list` against the webhook using `data/dev.db`
- Tests: queue race (20 simultaneous joins at 9/10 fill exactly one spot), database, alerts, time parsing, rendering

### Added (scheduling)
- `/tenman schedule when:... [repeat:weekly] [weeks:N] [post_days:N]`: one-off games saved now whose sign-up posts automatically N days before (default 3, `SCHEDULE_POST_DAYS_BEFORE`), and weekly series that keep exactly one game ahead, at the same local time across DST (`bot.py`, `utils/database.py`)
- `/tenman list` shows posted and scheduled games plus weekly series; `/tenman cancel` covers scheduled games and offers "Stop the whole series"
- Schema migration 2 (columns added in place: a table rebuild would cascade-delete signups): `post_state`, `post_at`, `post_attempts`, `series_id`, `series` table, indexes. Scheduled posting is claim-before-send like alerts; a crash mid-post is never reposted
- Games close at start + 30 min: buttons removed, clicks after kickoff refused with a reason

### Added (CI/CD)
- `.github/workflows/ci.yml`: ruff, unit tests, simulated end-to-end runs, pip-audit, dev/runtime image build with tests inside the image and a no-secrets smoke test; `deploy.yml`: on a `v*` tag, reuses CI then ships the image to the VPS over SSH (host key pinned, secrets only in GitHub) and checks health; `scripts/ci.sh` runs the same checks locally
- `ruff.toml` (pyflakes, bugbear, bandit, async, pylint subsets); found and fixed literal bidi/zero-width characters in source ("Trojan Source"), now written as `\u` escapes
- Simulated end-to-end suite `tests/sim/` (harness drives the real command and button callbacks against a fake Discord; `fakes.py` ported from the steam tracker): a full simulated week, 60 simultaneous clicks, three chaos weeks with 500/429/dropped/late replies and daily restarts, scheduling, permissions, abuse
- Ported `healthcheck.py` (stalled loop, long gateway outage, stuck DB job), `Containerfile` (explicit allowlist, 173 MB), `compose.yaml` (read-only root filesystem), `compose.docker.yaml`, `build.sh`

### Added (Docker support)
- Docker Engine is a first-class alternative to Podman: `compose.docker.yaml` runs the bot as your own uid (`HOST_UID`/`HOST_GID`, default 1000) so bind mounts aren't root-owned, with :Z SELinux labels; `build.sh` passes `-f Containerfile` (Docker only looks for Dockerfile) and `--load` (buildx docker-container builders otherwise keep the image in cache); `deploy.yml` takes repository variable `VPS_ENGINE=podman|docker`
- One image name on both engines, `localhost/tenman-bot`: a Podman-built archive loaded into Docker would otherwise not match compose.docker.yaml. Release archives carry `:vX.Y.Z` and `:latest`, so no manual tag step after `load`
- `scripts/docker-smoke.sh`: builds with Docker, exports, loads, and runs through compose.docker.yaml without a token (exit 7, uid, read-only root, writable volumes, no .env in image, DB/backup/restore, healthcheck)
- Unwritable logs/ no longer crashes the bot (falls back to stdout with the uid to fix); an unwritable data/ exits 7 with the fix instead of a traceback
- Guide: Podman reboot persistence needs `podman-restart.service` as well as lingering; Docker equivalents for every step

### Changed (live testing, round 2)
- Clicking Maybe now also removes your guests (a guest only holds a spot while the sponsor is In); harness invariant tightened to match
- Time zones: `when:` accepts et/ct/mt/pt (est, cst, central, pacific, utc...) and converts to the bot's TIMEZONE; the preview shows both ("Sat, Oct 10 at 9:00 PM CDT = 10:00 PM EDT"); schedule confirmations and series lines spell out the zone, with Discord's local-time rendering alongside
- Lobby links per game: `lobby:` option on /tenman start and /tenman schedule (weekly: a series default copied onto every week, schema migration 3); /tenman lobby now works on scheduled games too (saved quietly, no pings) and takes `whole_series:true`; scheduled/weekly lobbies are organizer/moderator only

### Added (release prep)
- `utils/backup.py` ported to SQLite: online backup API (safe while the bot writes), gzip, read back + `PRAGMA integrity_check` before the final name, `.sha256` sidecar, atomic rename; steam retention kept (30 days, newest 7 always). Run daily by the bot at `BACKUP_TIME` (04:00), named in the bot's `TIMEZONE`; CLI `python -m utils.backup [--verify F] [--restore F DEST]` (restore never overwrites)
- `guides/guide-deploy.txt` rewritten for this bot (VPS setup, settings, image transfer, start, moving the DB, backup/restore, CI/CD secrets, live checklist); `README.md`
- Retired steam leftovers to the gitignored `deadcode/` (scheduler, script, preflight, failover/restore JSON, anomaly, run summary and their tests): each is superseded by bot.py's loop, config validation or the SQLite backup
- Deploy rehearsal in `deploy-test/` (gitignored) following the guide: `build.sh` -> tar -> `podman load` -> backup/restore of the live DB inside the container -> `podman compose up -d` -> healthcheck, read-only root, writable volumes, in-container backup -> rebuild/reload/update with data kept

### Fixed (release prep)
- Simulation tests wrote 31 fake backups into the project's real `backups/` (their Config left `backup_dir` at a relative default), which made the live bot think today's backup was done. `Config.backup_dir` now defaults to None (no backups) unless load_config sets it; test configs use tmp dirs; `tests/test_zz_no_repo_writes.py` guards it
- Backups taken inside the UTC container were stamped in UTC while the bot's own daily backup used local time; all names now use `TIMEZONE`

### Fixed (live testing)
- `when:"sat 9pm"` was rejected: Discord passes quotes literally (and the docs' own examples used them). The parser now strips straight and smart quotes and accepts many phrasings: "Saturday at 9 PM", "9pm sat", "sat 9p", "sat 2100", "10/10 9pm", "oct 10th 9pm", a bare "sat 9" (read as PM), noon/midnight; errors say what wasn't understood (`utils/when.py`)
- `when` autocomplete on /tenman start and /tenman schedule shows how the bot read the time ("Sat, Oct 10 at 9:00 PM EDT") before sending, or offers the next few evenings when empty (`bot.py`)

### Fixed (code review)
- `/tenman lobby` now limited to the organizer, moderators and confirmed players (anyone could ping the roster)
- A click is one database job (ownership check + change + rosters) with a 2 s budget, fitting Discord's 3 s window; every click/command error is answered instead of "interaction failed"
- `/tenman start` creates the session and the organizer's signup in one job and cancels it if the post fails (no orphan sessions)
- Waitlist promotion is computed from the same transaction, so concurrent clicks can't double-ping
- Sign-up embed is kept under Discord's 6000-character limit (waitlist/maybe lists truncate with "+N more")
- Alerts: instants compared as timestamps (DST-correct game-day and starting alerts), no daily + game-day double ping, finished alerts skip the write path, a per-channel failure no longer stops other channels' alerts
- Heartbeat on its own 30 s loop; `ALERT_CATCHUP_HOURS` minimum 0.25; write queue rolls back a failed COMMIT and `stop()` cannot hang; N+1 queries removed
- `devtool.py preview` uses a throwaway database; `demo` no longer pings the real role (`--ping` to opt in)
- Emoji removed from message text (kept on the buttons): bold labels **Starts**, **Lobby**, **Maybe** instead

### Added (styling)
- Bot avatar (`assets/avatar.png`, cropped square from the CS2 chicken artwork) and `devtool.py set-avatar`
- `utils/style.py`: palette sampled from the avatar (CS2 orange = filling, camo green = full, comb red = starting soon and short, vest grey = cancelled/over), `▰▱` progress bar, brand name
- Sign-up post: author line + avatar thumbnail, progress bar, numbered slot columns of 5 with open slots shown, `W1..` waitlist, masked "Join lobby" link, emoji buttons
- Alerts are embeds with the avatar thumbnail and a title that jumps to the sign-up post; the message text (what push notifications show) carries the count and the ping
- New "starting now" alert at start time: pings only the confirmed players, includes the lobby link, sent once within 30 min of start
- `devtool.py preview`: posts every message style through the webhook with no pings

### Security
- All user-controlled input is validated on the way in and escaped on the way out (`utils/sanitize.py`, replacing the copied scraper; `clean_text` kept from it and extended to invisible format characters):
  - titles: 1-80 chars, control/bidi/zero-width characters stripped, links, invites and mentions rejected with a reason
  - start time: bounded length before parsing; lobby links: https only, real domain (no IP, no localhost), no credentials, no custom port, no spaces or hidden characters, at most 300 chars, optional `LOBBY_ALLOWED_HOSTS` allowlist
  - display names: cleaned and capped at 32; autocomplete text bounded
  - every stored string is passed through `md()` when shown: markdown, masked links and URLs neutralised, `@` broken with a zero-width space on top of explicit `allowed_mentions`
- Button custom_ids are only honoured on the message the bot posted for that session and in the home guild; commands are guild-only and refuse other servers
- Spam limits: 5 open sign-ups per channel, 2 per organiser (checked inside the insert transaction), 1 s per-user click cooldown
- Every `.env` value is validated at startup (`ConfigError` lists all problems; token never echoed); unhandled command errors reply "Something went wrong" instead of failing silently
- SQL: all queries already used bound parameters; verified no string-built SQL

### Changed
- `/tenman cancel` and `/tenman lobby` no longer silently pick the latest-starting sign-up: with several open in a channel they reply with a ufw-style numbered list (soonest first) and take `number:N`, with autocomplete; cancel asks for confirmation (Cancel it / Keep it) and is race-safe (`cancel_if_open`); `/tenman list` shows the same numbering (`bot.py`, `utils/embeds.py`, `utils/database.py`)
- Startup checks channel access and whether the configured role can actually be pinged; alerts fall back to the webhook when the bot cannot post (`bot.py`)
- `utils/webhook.py`: kept the steam tracker's retry contract (retry only when Discord provably never got the request; ambiguous never retried; URL never logged); replaced game batching with `send_message(content, role_id)` that only lets the given role be pinged
- `utils/tracking.py`: logger `tenman`, job log `tenman.log`, timestamps now really UTC (the `Z` suffix previously labelled local time)
- `requirements*.txt`: discord.py instead of psycopg2; pins updated

