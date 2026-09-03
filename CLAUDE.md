# CLAUDE.md, Oversite Roleplay bot

Single-file discord.py bot (`main.py`). This is the `roleplay` branch of
22hype22/oversite-customs, which IS the Oversite Roleplay codebase. It started
as a copy of the Network bot (branch `main`) and is allowed to diverge: fixes
are not required to be mirrored between the two. The GitHub repo
22hype22/oversite-roleplay mirrors this branch (its sync workflow pulls it every
ten minutes) and Railway deploys The Six Roleplay from that repo.

`BOT_BASE` defaults to "roleplay" here: brand name, the slash commands kept
before sync, and the dashboard blocks loaded.

## Message wording rules (owner's standing request)

- No emoji and no symbol glyphs. Ratings are written out, "4 out of 5".
- No em dashes, no parentheses, no mid-dot separators.
- No AI voice: no cheerleading, no filler. Say what happened.
- Existing red/green channel-name markers for tickets are the one exception.

## Persistence

Anything that must survive a redeploy goes through `_durable_config_get` and
`_bot_config_upsert` with a `*_loaded` flag, so a failed load never overwrites
stored data with an empty snapshot.

## Git

Commit trailers are set by the session. Never put model names in commits,
comments, or code.
