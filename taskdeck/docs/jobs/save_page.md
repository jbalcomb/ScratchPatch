# Save web page

Saves a web page, **as it looks**, onto your storage machine, from the phone. The machine's own browser
does the saving, so pages built by JavaScript and sites you're logged into (in that browser) come out
right. Formats: **MHTML** (one file; Chrome or Edge open it exactly as the page was) and/or **PDF**.

The job queues the save on the machine and follows it; the machine finishes it even if the phone goes
offline. Saving the same page again makes a new copy (`… (2)`), it never overwrites one.

## How it works

TaskDeck → the **chromers daemon** on the storage machine
([github.com/jbalcomb/chromers-odyssey](https://github.com/jbalcomb/chromers-odyssey)), over Tailscale,
with a token → its Chromium opens the page in a background tab → the files go into a folder inside one
of the machine's **save roots** (allowed folders; nothing outside them can be written).

## Setup (once, on the storage machine)

1. Run chromers there ([its README](https://github.com/jbalcomb/chromers-odyssey#quick-start); start at
   login with `scripts/install-services.sh`).
2. Give it the save commands. Until they're in chromers-odyssey's main branch, apply the patch from this
   folder in your chromers-odyssey checkout and restart the daemon:
   ```bash
   cd ~/chromers-odyssey
   git am /path/to/ScratchPatch/taskdeck/docs/jobs/save_page-chromers.patch
   systemctl --user restart chromers-daemon      # or: cd daemon && npm start
   ```
3. Choose where pages may go (optional; default: `web` = `~/Archive/web`), e.g. in the daemon's service
   file or shell: `CHROMERS_SAVE_ROOTS="web=~/Archive/web,docs=/srv/docs"`.
4. Make TaskDeck its own token: `scripts/token.sh new taskdeck` (prints it; revoke later with
   `scripts/token.sh revoke taskdeck`).

## In TaskDeck

**Save web page** → the tune icon (settings):

| Setting | |
|---|---|
| Storage machine | `http://<its Tailscale address>:7700` |
| Token | the one from step 4 (kept with your other credentials) |
| Save root | a root name from step 3 (default `web`) |
| Wi-Fi only | optional |

Opening the job checks the machine answers, the token works and the root exists (and, if the machine
isn't reachable, whether the phone is on Tailscale).

To save: the page address, a folder inside the root (e.g. `recipes/soups`, made if needed), MHTML and/or
PDF, and optionally a file name (default: the page title and date).

## Messages

| You see | Meaning |
|---|---|
| "…doesn't know how to save pages yet: update it" | the machine's chromers doesn't have the save commands: step 2 |
| "refused the token" | wrong or revoked token: step 4 |
| "has no save root “x”" | the Save root setting isn't one of the machine's `CHROMERS_SAVE_ROOTS` |
| "folder must stay inside the root" | the folder had `..`, started with `/`, or a link inside the root leads out of it |
| "Chromium is not reachable" | the daemon runs but its browser doesn't: `scripts/launch-chromium.sh` or the browser service |
| "Queued on the storage machine" | it took longer than 2 minutes; it's still saving there |
