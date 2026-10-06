# CS2 10-Man Bot

A Discord bot that gets ten players into a Counter-Strike 2 game without anyone having to chase people in the channel.

It posts one sign-up message, keeps a first-come roster and waitlist, and pings the group daily with how many players are still needed (plus once more 2 hours before a game that's still short). When the game starts, it hands the confirmed ten the lobby link.

Full documentation lives in `docs/` (every aspect, 01-21, plus a component-level reference, 22). It is kept locally and not checked into the repository.

## Commands

| Command | What it does |
|---|---|
| `/tenman start when: sat 9pm [lobby: https://...]` | Post a sign-up now. The organizer is added as In. |
| `/tenman schedule when: fri 9pm` | Save a game; its sign-up posts automatically 3 days before (`post_days:` to change). |
| `/tenman schedule when: fri 9pm repeat: weekly [weeks: 4]` | A weekly series; each week's sign-up posts by itself. |
| `/tenman list` | Everything coming up in the channel, numbered, plus weekly series. |
| `/tenman lobby url: https://... [number: N] [whole_series: true]` | Set a game's lobby link (number from `/tenman list`); pings its confirmed players. Works on scheduled games too. |
| `/tenman cancel [number: N]` | Cancel a game or stop a weekly series (asks to confirm). |
| `/tenman edit [number: N] [when:] [title:] [slots:] [post_days:] [lobby:]` | Change one game, posted or scheduled; only the options given change. A posted game keeps its roster, its sign-up is redrawn and a short note is posted (players moved onto or off the waitlist by a slot change are pinged, the role never is). Organizer or moderator. |
| `/tenman update [series: S<id>] [when:] [title:] [slots:] [post_days:] [lobby:] [apply_to_next: true]` | Change a weekly series for the games it creates from now on (`S<id>` as shown in `/tenman list`); `apply_to_next` also changes the upcoming game. Organizer or moderator. |

The `when:` box previews how the bot read the time before you send it ("Sat, Oct 10 at 9:00 PM EDT"). Most phrasings work: `sat 9pm`, `saturday at 9 PM`, `10/10 9pm`, `oct 10 9:30pm`, `fri 9`. Times are in the bot's `TIMEZONE` (Eastern) unless you add one: `sat 9pm ct`, `pt`, `mt`. Discord shows everyone the time in their own zone.

**Buttons on the sign-up:** In, Maybe, Out, +1 Guest, -1 Guest. The first 10 In play; the rest wait in order and move up automatically, with a ping, when someone drops. From the start time clicks are refused; the buttons are removed 30 minutes later.

**Alerts, in the channel:**
- **Daily** at `ALERT_TIME` (noon by default) while the game is short. It pings @10 Manner with how many are still needed.
- **Game day,** 2 hours before start, if still short.
- **Full,** once, when the roster reaches 10.
- **Starting now:** pings the confirmed players with the lobby link.

## Running it

- **Settings:** copy `.env.sample` to `.env` and fill in the values marked `PASTE_`. Every value is checked at startup.
- **Locally:** `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`, then `.venv/bin/python bot.py`.
- **On a VPS:** with rootless Podman. See [guides/guide-deploy.txt](guides/guide-deploy.txt).
- **One copy only:** never run two copies with the same token.

## Development

| | |
|---|---|
| `scripts/ci.sh` | Lint, unit tests, simulated end-to-end runs, dependency audit (`--image` adds the container build) |
| `devtool.py preview` | Post every message style through the webhook, no pings |
| `python -m utils.backup [--verify F] [--restore F DEST]` | Back up, verify or restore the database |
| `docs/working-doc.html` | Running project doc: decisions, status, log (local only) |
| `CHANGELOG.md` | Update under `[Unreleased]` before every change |

All database writes go through one queue (`utils/db_queue.py`), so simultaneous clicks can't race. All user input is validated on the way in and escaped on the way out (`utils/sanitize.py`).

Infrastructure is adapted from `steam_tracker_bot`. Retired steam files are in the gitignored `deadcode/`.
