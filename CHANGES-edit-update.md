# /tenman edit and /tenman update

Branch `feature/edit-update`, from `v0.1.0`.

## Before deploying

- **No schema change, no migration.** The database stays at version 3; a v0.1.0 database opens unchanged (tested with data in place). Rolling back to v0.1.0 is safe: nothing new is stored.
- **Two new slash commands.** They register on the next start (`tree.sync` on the guild, as today). Existing commands and button custom_ids (`tenman:<action>:<session_id>`) are unchanged.
- No new settings, permissions or intents. One bot instance only, as always.

## /tenman edit

`/tenman edit [number] [when] [title] [slots] [post_days] [lobby]`

- Picks the game like cancel/lobby (number from `/tenman list`, same autocomplete; with one game the number can be left out).
- Organizer of the game or a moderator (Manage Messages), like cancel.
- Only the options given change; at least one is required. Inputs go through `clean_when` + `parse_when`, `clean_title`, `clean_lobby_url`; slots 2-40, post_days 0-14.
- **Scheduled game** (not posted yet): changed quietly, ephemeral reply. `post_at` is recomputed like `/tenman schedule` (post_days before, at the game's wall-clock time, never earlier than now). Without `post_days`: a weekly game uses its series' value; a one-off game reads it back from its current `post_at` (falls back to `SCHEDULE_POST_DAYS_BEFORE` when `post_at` had been pulled forward to "now"). Weekly games are capped at 6 days. The new time can be up to 120 days ahead (`SCHEDULE_MAX_DAYS`).
- **Posted game:** the roster is kept, the sign-up is redrawn and a public note says what changed ("moved to ...", "now 12 slots (was 10)", "renamed from ...", "lobby link updated"). The note never pings the role (`allowed_mentions` = only users the edit moved). Fewer slots: players pushed onto the waitlist are pinged ("you're now on the waitlist"); more slots: waitlisted players who got in are pinged like a promotion. A posted game can't be moved into the past, nor more than 30 days ahead (the `/tenman start` horizon). `post_days` is ignored with a note. Changing the lobby here doesn't ping the players; `/tenman lobby` still does that.
- A default title ("Friday 10-man") follows the new weekday; a custom title is kept.
- A game in a weekly series: only that game changes.
- Refused: cancelled/done games, games that have started, a scheduled game in the middle of being posted ("try again in a minute").

### Alerts after a time change (posted games)

- `gameday` and `starting` rows are cleared only when their new moment is still ahead, so they fire again for the new time. If the new moment has already passed (e.g. a game moved 30 minutes after its game-day alert went out) the row is kept: that alert is never sent twice.
- `daily` rows are never cleared: they are keyed by calendar date, so a day that already had its daily alert won't get a second one.
- Rows in `sending` (in flight) are never touched.
- `full`: cleared when a slot change leaves the roster not full, so "is full" fires again once the bigger roster fills; kept if the roster is still full.
- Moving a game after its game-day alert went out keeps that alert as history (`once@<old start>`), so the alert for the new time can still fire, and no daily alert goes out on a day a game-day ping already did (no double ping).

## /tenman update

`/tenman update [series] [when] [title] [slots] [post_days] [lobby] [apply_to_next]`

- `series` is the `S<id>` shown in `/tenman list` (autocompleted; `S3`, `[S3]` or `3` accepted; may be left out when the channel has one series).
- Series organizer or a moderator.
- Changes the series' defaults for the games it creates from now on. `when` sets the new weekday and time (give a day and time, e.g. `sat 9pm`). `post_days` is capped at 6.
- `apply_to_next:true` also edits the upcoming game with the same rules as `/tenman edit` (a new weekday moves it to that weekday on or after its current day). If the upcoming game can't be edited (started, mid-post, new time already passed) the series change still applies and the reply says why. Without it, the reply says the upcoming game keeps its settings.
- Ephemeral summary; if the upcoming game was posted and changed, the public note is posted as with edit.

## Race safety

Every edit/update is one `DBQueue` job (`INTERACTION_DB_TIMEOUT`, 2 s) that re-reads the game, re-checks it is still open, writes, and computes the before/after roster in the same transaction. Clicks are queued before or after it, never in between; all checks run before the first write, so a refusal changes nothing.

## Tests

`tests/test_edit.py` (21) and `tests/sim/test_edit_e2e.py` (9): 292 -> 322 tests, ruff clean.

## Not included

- No way to clear a lobby link or reset a custom title back to the default.
- Moving a weekly game never skips a week: each series game remembers its original weekly slot in the `series_slots` table, and the series counts weeks by slot. Move Friday the 9th to Sunday the 18th and Friday the 16th is still created.
- `series_slots` is a new table created on start WITHOUT a schema version bump (the database stays at version 3), so rolling back to v0.1.0 still works: v0.1.0 ignores the table.
- `docs/` (gitignored, not in this worktree) still needs the matching updates: commands, alerts and components docs, and a progress-log row in `docs/working-doc.html`.
