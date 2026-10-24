# Open questions: what we still need to know or decide

Ordered by what blocks what. ★ = decide before writing code.

## Product decisions

1. ★ **Overlay or in-place translation for the MVP?** An overlay or side panel listing on-screen text is
   fast to build and suits screen readers. Replacing text in place looks native but needs a Unicode font
   renderer and per-screen layout fitting. Recommendation: overlay first.
2. ★ **Which languages first?** Latin-script languages (es, de, fr, pt) are much easier than CJK, Cyrillic
   or right-to-left scripts. Pick 1 or 2 pilot languages.
3. ★ **Is Sign in with Google worth it?** A consumer Gemini subscription can't pay for API calls from
   third-party apps (see the PRD, §2). Do we ship "Gemini API key" only and revisit later?
4. **Who is the accessibility audience?** Non-English speakers, low-vision players, blind players and
   dyslexic players need different things (translation, enlargement, TTS, simpler wording). Pick the primary
   one for v1.
5. **Bring your own key only, or also community-shared translation packs?** Pre-translated
   `translations/<lang>.tsv` files mean most players need no key at all. Are they reviewed by people, and
   where do they live?
6. **Where is the AI settings page reached from?** A new button on the reconstructed Settings screen breaks
   fidelity. Options: a hotkey on Settings, a main-menu corner link, or a first-run dialog.

## Technical unknowns

7. ★ **How many distinct strings are there, and where do they come from?** Write a pass over the LBX
   archives and source literals that counts static strings, format templates and text drawn into images.
   That sizes the bulk-translation cost and the cache.
8. ★ **Does every piece of text go through `Print*`?** Verify there is no direct glyph blitting or
   pre-rendered text that bypasses the hook (grep for `Print_Character*` callers and bitmap text).
9. **How are dynamic strings assembled?** If screens build messages by concatenation rather than one
   `sprintf`, translating templates is harder (word order differs between languages). We need a survey of the
   message-building code.
10. **Text entry in the engine.** Can `Fields.c` input handle a long string and clipboard paste without
    touching reconstructed code, or does the settings screen need its own small text widget?
11. **HTTP seam scope.** Extend the update-check `Platform_Http_Get` to POST, custom headers and streaming,
    and settle libcurl on Linux (vetted per `Dependency-Vetting.md`) versus shelling out. **Coordinate with the
    update-check PRD so we build one seam, not two.**
12. **Threading.** C89 with no thread library today. Use SDL threads (already a dependency) or Win32 threads
    for the native backend, and confirm the worker never touches engine state.
13. **Secret storage.** Windows Credential Manager and libsecret on Linux. Is a "store in ini (insecure)"
    fallback acceptable for portable/zip installs?
14. **Determinism.** HeMoM and replay tests must stay byte-identical. The AI path must be fully off in
    headless builds, or must never alter game state or RNG.

## Provider and API facts to confirm

15. **Common request format.** Most providers speak the OpenAI Chat Completions format (Ollama, LM Studio,
    OpenRouter, and Gemini's OpenAI-compatible endpoint). Anthropic and native Gemini differ. Decide whether
    "OpenAI-compatible" is the one built-in format plus small adapters.
16. **Vision support** varies by model and provider. "Translate images" and "describe screen" must detect or
    declare it.
17. **Cost and rate limits.** Estimate tokens for a full bulk translation of one language, and the per-hour
    cost of on-demand screen descriptions.
18. **Privacy notice.** Screenshots and text go to a third party. Say so on the settings page.
19. **Legal: translated game text.** The LBX text belongs to the original copyright holder. Translation
    caches generated locally from the player's own copy are fine. *Distributing* translated packs may count as
    a derivative work. Check before publishing packs, in the same spirit as ReMoM's "ship no game data" rule.

## Suggested first spikes

- **Spike A:** a string census tool (answers 7 to 9).
- **Spike B:** a logging-only hook in `Print*` that writes every drawn string with the screen name (no
  network). Play for an hour and you have a real corpus.
- **Spike C:** a standalone harness in this folder: corpus file → provider → `translations/<lang>.tsv`, to
  try prompts, glossaries (spell names, unit names) and cost.
