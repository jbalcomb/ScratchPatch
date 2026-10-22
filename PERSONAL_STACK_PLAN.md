# Personal Productivity Stack: Plan

## Who and why

James is based in Cleveland, does Instacart gig work, and is looking for a real job. He has five computers at home, a laptop, and a phone, plus a local llama.cpp setup. Coding is easy for him. Starting, scoping, and finishing are the hard parts, so keep every step small, finished, and usable.

## Goals (in priority order)

1. **Pay bills now:** budget visibility and income stability
2. **Get a real job:** networking is the main channel; applications are under one per week
3. **Retire properly:** longer term

If a piece of work doesn't clearly serve one of these, it goes in the "later" list at the bottom.

## Architecture

One store, many inputs, one viewer.

- **Store:** SQLite (single file) on the home machine. Everything lands here: budget transactions, voice inbox items, events, contacts, and later tab captures. Add tables only when a feature needs them. Start with `transactions`.
- **Inputs (build in this order):**
  1. **Budget drop folder:** James exports a CSV from his bank's site on desktop and drops it in a watched folder. A script parses it and writes rows to the store.
  2. **Voice inbox:** a voice note from his phone is transcribed and becomes an inbox item. One agent type handles it at first: "research this person or event and add it to the networking list."
  3. **Cleveland events agent:** already mostly built (weekly Cleveland tech events and news). Point its output at the store, into the networking tables, instead of wherever it writes now.
- **Viewer:** a minimal tabbed PWA (usable on phone) over the store. One tab per data type, plain lists first. The job and networking tab exists from day one. A graph view comes last, once there is real data to graph.
- **Later input:** Chrome tab capture (all windows and tabs, page summary, whether anything was copied from the page) feeding a knowledge graph. It goes into the same store as just another input.

## First task: budget drop folder

Build only this, and finish it before anything else.

- Watch a folder (path configurable) for new bank export files.
- Ask James for a sample export first. Don't guess the format.
- Parse into `transactions`. Re-dropping an overlapping export must not create duplicates.
- Categorize with a plain rules file (merchant pattern to category) that James can edit by hand.
- Only transactions that no rule matches go to an LLM for a suggested category. Use the local llama.cpp endpoint or the Claude API, whichever is simpler to wire up, and write the result back as a new rule suggestion rather than silently.
- Done when James can drop a real export and see categorized rows in the store, with a short README for running it.

## Next, in order

1. Voice inbox: transcription plus the single "research and add to networking list" agent.
2. Repoint the Cleveland events agent at the store.
3. Minimal tabbed PWA: Budget, Inbox, Networking tabs.
4. Graph view.

## Scope guard

- Build the minimum that solves the stated task. No extra agent types, plugin systems, config frameworks, or generalized abstractions until a real need shows up.
- Finish and demonstrate one piece before starting the next.
- Prefer small, boring tools (SQLite, plain scripts, a simple watcher) over services that need ongoing care.
- If a task seems to be growing, stop and say so rather than expanding it.
- Keep the job and networking side supported at every stage, since it moves income more than anything else here.

## Later (not now)

- Chrome tab capture and the knowledge graph
- One consistent Claude/Gemini/Pi/llama.cpp setup across all devices
- Additional voice agent types
