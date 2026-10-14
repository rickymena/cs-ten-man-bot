# Security policy

## Reporting a vulnerability

Please **don't open a public issue** for security problems. Report them privately through GitHub's [private vulnerability reporting](https://github.com/rickymena/cs-ten-man-bot/security/advisories/new) instead.

Include what you found, how to reproduce it and what an attacker could do with it. You should get a reply within a week.

## Scope

In scope: anything that lets a Discord user make the bot ping roles or `@everyone`, post content they shouldn't, crash or stall the bot, corrupt the roster or database, or read the bot's token or webhook URL. Weaknesses in the container setup also count.

Out of scope: problems that need control of the server the bot runs on, or of the bot's Discord application.

## Supported versions

Only the latest release on `main` gets fixes.

## If your token leaks

Reset it straight away at [discord.com/developers/applications](https://discord.com/developers/applications) → your app → **Bot** → **Reset Token**. Then update `secrets/discord_token` (or `.env`) and restart the bot. A leaked webhook URL can be deleted and recreated under the channel's **Integrations** settings.
