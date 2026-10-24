# ReMoM AI Assist

Planning space for an AI accessibility agent in [ReMoM](https://github.com/jbalcomb/ReMoM), the C rebuild of
*Master of Magic* (1994). Nothing here is code yet. These docs set out what to build, how it fits ReMoM's
architecture, and the decisions to make before anyone writes code.

| Doc | What it covers |
|---|---|
| [PRD-AI-Assist.md](PRD-AI-Assist.md) | What the feature is, the settings page, provider sign-in, and live translation as the first capability, then object identification. |
| [OPEN-QUESTIONS.md](OPEN-QUESTIONS.md) | What we still need to know or decide, ordered by what blocks what. |

## Where the code will live

The real implementation belongs in the ReMoM repo, as new non-fidelity modules marked `/* CLAUDE */`, per
ReMoM's `AGENTS.md`. This folder may later hold a standalone proof of concept, such as a small harness that
feeds captured game strings and screenshots to a provider, so the prompts and caching can be tried without
building the game.
