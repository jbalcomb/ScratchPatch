# Budget Manager: Plan

Scope: the budget manager only. Other projects (voice inbox, events agent, tab capture, and so on) have their
own plans and are out of scope here.

## Who and why

James is based in Cleveland, does Instacart gig work, and is looking for a real job. Coding is easy for him.
Starting, scoping, and finishing are the hard parts, so keep every step small, finished, and usable.

**Goal: pay the bills now.** See where the money goes across the three Bank of America accounts (daily
checking, bills checking, savings) and what's coming due, so the bills account doesn't overdraw.

## Architecture

- **Store:** one SQLite file on the AI Workstation (`~/budget/budget.db`): `transactions` and `imports`.
- **Budget list:** the SnapShot tab of the budget Google Sheet, read by the TaskDeck budget jobs (the phone
  writes the results back to the sheet; this service never touches Google).
- **Input:** Bank of America CSV exports, uploaded from the phone or any computer through the upload page,
  or saved into the drop folder.
- **Categories:** a hand-edited rules file. Unmatched merchants get suggested rules from the local llama.cpp,
  never applied automatically.
- **Hosting:** one Docker container on the AI Workstation, reached over Tailscale Serve (HTTPS, own devices only).

## Done

- Drop folder and upload page: parse exports into `transactions`; overlapping re-uploads add no duplicates.
- Rules file plus LLM rule suggestions.
- Protection against bad uploads: file-name warning, running-balance check, numbered upload log,
  undo / redo / rebuild from the archived files, backups before each rebuild.
- Docker Compose deployment behind Tailscale Serve.

- Budget sheet link-up: rule categories are named after the budget sheet's SnapShot tab, and a read-only
  feed (`GET /api/transactions`) serves the TaskDeck jobs **Reconcile budget month** and **Update budget
  timeline** (upcoming bills and balances per account; see `taskdeck/docs/jobs/budget.md`).

## Next (candidates, one at a time)

1. **Budget view on the phone:** monthly summary, the upload list with an undo button, and accepting
   suggested rules, instead of running commands over SSH.
2. **Nightly copy of the store**, keeping the last 7.
3. **Later, to consider (noted 2026-11-02 from photo-ocr):** move the store to Postgres, and read photo-ocr's
   receipt data to tag each transaction as having a receipt photo or not. Whether photo-ocr's store moves to
   the same Postgres is part of that decision (see photo-ocr/README.md, Later).

## Scope guard

- Build the minimum that solves the stated task. No plugin systems, config frameworks, or generalized
  abstractions until a real need shows up.
- Finish and demonstrate one piece before starting the next.
- Prefer small, boring tools (SQLite, plain scripts, one container) over services that need ongoing care.
- If a task seems to be growing, stop and say so rather than expanding it.
- Anything that isn't the budget manager goes to its own project and chat.
