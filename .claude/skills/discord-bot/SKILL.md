---
name: discord-bot
description: Set up a Discord bot end to end - create the application, store its token safely, enable privileged intents, build the invite link, and confirm which servers it can see. Use when the user wants a new Discord bot, a bot token, to add a bot to a server, or when a Discord bot gets empty message content or 401/403 errors.
---

# Discord bot setup

Discord has no API for creating a bot: a person logged into discord.com must create the
application and reveal its token, and a server admin must click the invite link. Everything
else is done by the helper `discord_bot.py`, next to this file. Do the automated parts yourself
and give the user exact clicks for the rest.

`H` below means `python .claude/skills/discord-bot/discord_bot.py`. All commands take
`--token-file PATH`. The default is `~/.config/discord-capture/token`. Give each bot its own
file, for example `~/.config/discord-bots/<name>.token`.

## Token handling, always

* Never print, echo, cat or log a token, and never pass it as a command-line argument.
* Never write it inside the repo. Store it with `H save-token`, which writes a file only the
  user can read and confirms by printing the bot's name, not the token.
* If the user pastes a token into the chat, it's in the transcript. Save it via stdin
  (`H save-token --stdin` with the token on stdin), then tell them to reset it after setup
  if that matters to them.

## Steps

1. **Create the application** (user, about a minute). Have them open
   https://discord.com/developers/applications, then **New Application**, give it a name, and
   accept the terms. Then **Bot** in the sidebar, **Reset Token** (Discord may ask for their
   2FA code), and **Copy**. If the claude-in-chrome skill is available and the user agrees, you
   can drive those pages in their logged-in browser. Stop for the CAPTCHA, the terms checkbox and
   2FA. Click Copy rather than reading the token off the page, so it never enters the transcript.
   On that Bot page, suggest turning **Public Bot** off, so only they can invite it.
2. **Save the token:** with the token on the machine's clipboard, run `H save-token`. It
   checks the token against Discord and refuses anything that doesn't look like one.
3. **Intents:** `H intents message-content` (add `guild-members` / `presence` only if needed).
   Without Message Content, the bot gets empty `content` for messages that don't mention it,
   over REST too. This works by itself for bots in fewer than 100 servers. If it prints
   `STILL OFF`, have the user flip it in the portal (Bot, then Privileged Gateway Intents).
4. **Invite:** `H invite-url --preset read` (read-only: View Channel and Read Message History),
   `--preset read-private-threads`, or `--preset chat` for a bot that posts. `--add PERM` adds
   one permission, and `--guild ID` preselects a server. Ask for the least the bot needs. Someone
   with Manage Server on the target server has to open the link and authorize it.
5. **Confirm:** `H check` shows the bot, its intents and its servers. For Discord Capture, then
   run `python -m discord_capture channels` from `discord-capture/src` to see what it can read.
   A channel missing from that list means the bot's role lacks View Channel or Read Message
   History there.

## Troubleshooting

* **401**: the token is wrong or was reset. Repeat steps 1 and 2.
* **403 Missing Access** on a channel: a permission overwrite hides it from the bot's role. Fix
  it in the server settings, not in code.
* **Empty `content`**: Message Content intent is off. See step 3.
* Don't automate a normal user account ("self-bot"). It's against Discord's Terms of Service,
  so refuse that route and use a bot.
