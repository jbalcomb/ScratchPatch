# PRD: ReMoM AI Assist (accessibility agent)

**Status:** Draft, planning only
**Date:** 2026-10-24
**Target repo:** [jbalcomb/ReMoM](https://github.com/jbalcomb/ReMoM)
**Related ReMoM docs:** `doc/@AI_Plans/PRD-In-App-Update-Check.md` (first outbound HTTP; we share its
platform HTTP seam), `doc/@AI_Plans/PRD-Installer-Game-Data-Setup.md` (`ReMoM.ini`),
`doc/@Devel/Dependency-Vetting.md`

---

## Summary

An optional, player-configured AI assistant in ReMoM whose job is accessibility.

1. **First: live translation** of everything the player reads, including in-game text, prompts and
   dialogs, help entries, and text baked into images.
2. **Next: object detection and identification.** "What is on this tile?", "describe this unit", and spoken
   or enlarged descriptions of the screen.

The player brings their own provider. A new **AI settings page** holds the API base URL and API token, or a
**Sign in with Google** option for Gemini.

## How ReMoM is built (the facts this plan rests on)

- **Text is drawn from strings we own.** All on-screen text goes through the `Print*` family in
  `MoX/src/Fonts.c` (`Print`, `Print_Centered`, `Print_Right`, `Print_Full`, `Print_To_Bitmap`, and their
  `Clipped_` variants). Most of it comes from LBX archives (for example help text in `HLPENTRY.LBX`, see
  `MoX/src/Help.c`) or from string literals in the reconstructed code. **So text translation does not need
  OCR.** We can intercept the string itself.
- **The original fonts hold only 96 glyphs**, ASCII 32 to 127 (`struct s_FONT_HEADER`,
  `current_font_widths[96]` in `MoX/src/Fonts.h`). Translated text with accents, Cyrillic, CJK or RTL scripts
  **cannot render in the game's own fonts**, so it needs a second render path.
- **The screen is a 320x200, 8-bit, palette-indexed framebuffer** (`video_page_buffer[]`,
  `Platform_Video_Update()` in `platform/include/Platform.h`), upscaled by the SDL2, SDL3 or Win32 backend.
  Screenshots for a vision model are cheap: convert one page to RGB with the current palette.
- **The engine is single-threaded C**, with a per-frame hook already available
  (`Platform_Register_Frame_Callback`). Network calls must run on a worker thread that posts results back.
- **There is no HTTP code today.** The update-check PRD already proposes `Platform_Http_Get` (WinHTTP on
  Windows, libcurl on Linux). We need POST, custom headers and longer timeouts, so this feature extends that
  seam rather than adding a second one.
- **Config already exists.** `STU_GRAF_Read_Ini_Value("ReMoM.ini", ...)` reads the per-user config dir
  (`%APPDATA%\ReMoM` on Windows).
- **There is a fidelity rule.** Reconstructed game code must stay faithful to the disassembly
  (`AGENTS.md`). Everything here is additive, kept in new modules marked `/* CLAUDE */`, with only thin hook
  calls in original code, all behind a runtime off switch. **With the AI feature off, the game must behave
  exactly as it does today.**

## Goals

- A player can switch on translation into their language and play the full game with every string
  translated.
- Works with any OpenAI-compatible endpoint (URL plus token), the Anthropic API, the Gemini API, and local
  models (Ollama, LM Studio, llama.cpp server). Bring your own key. We ship no keys.
- Translation stays usable offline and cheap after the first run, through a persistent translation cache.
- The game is untouched when the feature is off: no network calls, no behavior change, HeMoM and replay
  tests still byte-identical.

## Non-goals (for now)

- An AI opponent or changes to the game's computer players. This is about accessibility, not gameplay AI.
- Telemetry. Nothing is sent anywhere unless the player sets up a provider and switches a feature on.
- Shipping or bundling a model.

## Features

### 1. AI settings page (new screen)

The original `Settings_Screen()` (`MoM/src/Settings.c`) is a reconstructed screen with a fixed layout, so we
**do not modify it**. We add a new screen reached from it or from the main menu (see Open Questions).

| Field | Notes |
|---|---|
| Provider | `OpenAI-compatible` / `Anthropic` / `Gemini (API key)` / `Gemini (Sign in with Google)` / `Local` |
| API base URL | Pre-filled per provider and editable, for example `http://localhost:11434/v1` for Ollama |
| API token | Masked on entry. Stored in the OS credential store, never in `ReMoM.ini` (see Open Questions) |
| Model | Free text, with a per-provider default |
| Target language | For example `es`, `de`, `ja` |
| Features | Toggles: translate text, translate images, describe screen |
| Test connection | One small request that reports OK or the error |

Non-secret settings go in `ReMoM.ini`:

```ini
[AI]
enabled=0
provider=openai_compatible
base_url=
model=
target_language=
translate_text=1
translate_images=0
describe_screen=0
```

**Text entry is the hard UI part.** The game's input fields (`Fields.c`) were built for short ASCII names. A
long token is impractical to type, so the settings screen should also accept **paste from the clipboard**
(SDL has `SDL_GetClipboardText`) and should treat **editing `ReMoM.ini` or a first-run desktop dialog** as
fully supported ways to configure it.

### 2. Sign in with Google (Gemini)

**Key finding: a consumer Gemini subscription (Google AI Pro or Ultra) cannot pay for API calls from a
third-party app.** "Sign in with Google" OAuth confirms the user's identity. It does not hand their
subscription quota to our app. The Gemini CLI's OAuth path is reserved for Google's own tools, and Google's
terms forbid reusing it from other software. What a Google login *can* do:

- **OAuth into the Gemini API (Generative Language API) under the player's own Google Cloud project.** This
  works, but the player needs a GCP project and billing or the free tier. That is more setup than pasting an
  AI Studio key, not less.
- **Google AI Pro's monthly Google Cloud credit**, if Google still includes it, can pay for Gemini API usage
  with Cloud Billing on. That still needs an API key or project, not just a login.

**Recommendation:** for the MVP, offer "Gemini (API key)" with a one-click link to AI Studio to get a key.
Build "Sign in with Google" only if Google publishes a sanctioned subscription-for-third-party-apps program,
and re-check this before building. Desktop OAuth also adds a loopback-redirect browser flow, token refresh,
and a Google OAuth app verification review for sensitive scopes.

### 3. Live translation (priority 1)

**3a. Engine text (no OCR).**

- **Hook point:** one new function, `AI_Translate_String(const char * src)`, called at the top of the
  `Print*` entry points when translation is on. It returns the translated string, or the original on a cache
  miss.
- **Two text kinds:**
  - **Static text**, from LBX archives and literals: menu labels, help, spell and unit descriptions, event
    messages. This is finite. **Pre-translate it in bulk once per language** by extracting every string with
    the existing LBX tooling, batch-translating, and saving to a cache file such as
    `translations/<lang>.tsv`. These cache files can be shared, reviewed by people, and committed, so a player
    needs no API key at all for languages already done.
  - **Dynamic text**, built with format strings: `"%s has cast %s"`, numbers, names. Translate the
    **template** once, keeping placeholders, then fill it in at runtime. Unknown strings at runtime go to the
    worker thread. The original text shows until the translation arrives (no stall), and then the result is
    cached.
- **Rendering:** translated text needs a Unicode font path. Use a bundled TTF through `stb_truetype` or
  SDL_ttf, rasterized into the 8-bit framebuffer with palette-matched colors and shadow styles, or drawn as a
  high-resolution overlay layer after upscaling (crisper, and avoids the 96-glyph limit). Translations often
  run 30 to 40% longer than English, so a fit-to-box policy is needed: smaller font, wrapping, or an
  on-demand tooltip or panel.
- **Alternative presentation:** leave the game drawn as-is and show translations in a **side panel or
  overlay** listing the text currently on screen. Simpler, needs no layout work, and suits screen readers
  too. A good MVP.

**3b. Text inside images.** Some text is drawn into the art itself (title and screen headers, some button
labels, cartographer map labels). For these, send a region of the framebuffer to a vision model once, then
cache the result by **LBX entry plus frame hash**, because the art never changes. This too can be done in
bulk offline and shipped as a table.

### 4. Object detection and identification (priority 2)

- **Prefer game state to pixels.** ReMoM knows exactly what is under the cursor: the unit, city, terrain,
  node or lair (`_UNITS[]`, `_CITIES[]`, the world map arrays). A "describe what's here" action can build a
  structured description from that state with no AI at all, which is perfectly accurate and works offline. AI
  then only *phrases* it, or answers follow-up questions.
- **Vision models** are for the cases state cannot cover, such as "describe this screen" for a blind or
  low-vision player. Send the RGB-converted framebuffer with the screen name as context.
- **Output:** text in the overlay, plus text-to-speech (OS TTS: SAPI on Windows, speech-dispatcher on Linux)
  so screen-reader users benefit.

## Architecture sketch

```
Print*() ──hook──► AI_Translate_String ──► cache (memory + translations/<lang>.tsv)
                                     └─miss─► AI request queue ──► worker thread ──► Platform_Http_Post
Frame callback ◄── results posted back ◄──────────────────────────────────────────┘
Screen describe ──► game-state describer ──(optional)──► vision request
```

New modules (names follow ReMoM's conventions): `src/AI_Assist.c` (queue, cache, providers),
`src/AI_Settings_Screen.c`, `platform/http/` (shared with update check), `platform/secrets/`
(Windows Credential Manager, libsecret on Linux).

## Acceptance criteria (MVP: translation into one language)

- [ ] With `[AI] enabled=0`, zero network connections and the test suite unchanged.
- [ ] With a valid provider and a target language, every string on the main menu, settings, city screen and
      help shows translated, using the cache on the second launch with no network.
- [ ] A cache miss never freezes input. The original string shows until the translation arrives.
- [ ] The API token never appears in `ReMoM.ini`, the logs or crash dumps.
- [ ] "Test connection" reports success, a bad URL, a bad token or a timeout distinctly.
