# PRD: Accessibility — blind, low-vision, deaf, limited-mobility and colorblind players

## Goal
Make ReMoM playable by people the 1994 original shuts out, starting with a community request for
**screen reader support**. Everything is **additive and off by default**: with accessibility off, the
game behaves exactly like the reconstruction, and nothing that is faithful to the disassembly is
deleted or rewritten.

Phased approach: ship the smallest proof of concept (POC) a blind player can try, learn from that
player, then build out.

## Who this is for

| Need | What it means for Master of Magic | Size |
|---|---|---|
| **Blind** (screen reader) | Menus and buttons are tractable. The hard part is the **overland map, city screen and combat grid**: they have to be explorable by keyboard and described in words. | Large |
| **Low vision** | Zoom/scale (exists, 4x), a larger cursor (hardware cursor exists), high contrast. The game is a fixed 320x200 image, so fonts can be zoomed but not re-flowed. | Small–medium |
| **Deaf / hard of hearing** | No voice acting; little is conveyed by sound alone. Add visual cues for sounds that carry meaning, and separate music and effects volumes. | Small |
| **Limited mobility** | The original is mouse-first. Full keyboard play, remappable keys, controller support, no drag-only actions. Turn-based play already avoids timing pressure. | Medium |
| **Colorblind** | The **wizard banner colors** (red, green, blue, yellow, purple, brown) are the main problem. Palette filter, alternative banner colors, and/or a symbol on banners. | Small–medium |
| **Cognitive** | Right-click help already exists everywhere. Add a turn summary, confirm before End Turn, and undo for misclicks. | Small |
| **Photosensitivity** | Option to reduce flashing spell and screen effects. | Small |

References: [Game Accessibility Guidelines](https://gameaccessibilityguidelines.com/), Microsoft
[Xbox Accessibility Guidelines](https://learn.microsoft.com/gaming/accessibility/guidelines).

## Screen readers
On Windows the two big screen readers are **NVDA** (free, the most common among blind gamers) and
**JAWS** (commercial). macOS has **VoiceOver**; Linux has **Orca** (which speaks through Speech
Dispatcher). Games don't target one: they use a small library that talks to whichever is running.
ReMoM uses **Tolk** on Windows, which supports NVDA, JAWS and others, and falls back to the Windows
built-in voice (SAPI) when no screen reader is running.

## Design principles
1. **Two layers.** The platform layer decides **how** text is spoken (Tolk / Speech Dispatcher / `say` /
   log-only). The engine layer decides **what** is said (help titles, focused control, map tile). The
   game code is never aware of which screen reader is running.
2. **Additive.** New code goes in new files (`MoX/src/Access.c`, `platform/speech/`). Original
   functions get, at most, a one-line hook marked `/* CLAUDE */`. Faithful code is never deleted.
3. **Off by default.** Enabled with `--access` or `REMOM_ACCESS=1`; later also an in-game option
   stored in a **new** settings file (not `CONFIG.MOM`, whose format is the original's).
4. **Testable headless.** `Platform_Headless` builds speech as log-only and remembers the last
   utterance, so tests and `.hms` scenarios can assert "after X, the game said Y".
5. **Read state, don't change it.** Accessibility code reads engine globals; it does not call
   original loaders with side effects (e.g. it reads `HELP.LBX` itself, rather than through
   `LBX_Load_Data_Static()`, which shares a cached handle and is fatal on error).

## What already exists in the engine (verified)
- **Help lists.** Every screen calls `Set_Help_List()` with rectangles `[help_idx, x1, y1, x2, y2]`
  ([MoX/src/Input.c](https://github.com/jbalcomb/ReMoM/blob/main/MoX/src/Input.c), `Check_Help_List()`), each pointing at a
  `HELP.LBX` entry 2 record (`struct s_HELP_DATA`: 30-char title, 1000-char text;
  [MoX/src/Input.h](https://github.com/jbalcomb/ReMoM/blob/main/MoX/src/Input.h)). This is a ready-made "name and description of
  everything on screen".
- **Fields.** `struct s_Field` ([MoX/src/Fields.h](https://github.com/jbalcomb/ReMoM/blob/main/MoX/src/Fields.h)) is the list of every
  clickable control on the current screen: rectangle, type, hotkey, help index. This is the basis for
  keyboard focus navigation.
- **Synthetic input.** `Platform_Warp_Mouse()` and `Platform_Set_Synthetic_Mouse_Button()`
  ([platform/include/Platform.h](https://github.com/jbalcomb/ReMoM/blob/main/platform/include/Platform.h)) already let replay drive the
  game; keyboard "activate" can reuse them, so original click handling is untouched.
- **Per-frame hook.** `Platform_Register_Frame_Callback()` (single slot; `--scenario` uses it).
- **Palette choke point.** `Platform_Video_Update()` converts the 8-bit frame with
  `platform_palette_buffer` on every backend: the place for a colorblind filter.

## Architecture

```
MoM (game)  ──  MoX (engine) ──────────────────────────  platform/
                 Access.c   "what to say"                 include/Platform_Speech.h   contract
                   reads: help lists, fields,             speech/PFL_Speech.c         "how to say it"
                          pointer, (later) game state       Windows: Tolk.dll (runtime-loaded)
                   calls: Platform_Speak()                  Linux:   spd-say
                                                            macOS:   say
                                                            headless: log only
```

---

## Phase 0 — Proof of concept: "hover to hear" (this change)

**What it does.** With `--access`, the game says "Master of Magic. Speech on." at startup. After
that, whenever the mouse moves onto a different right-click-help area, the area's help **title** is
spoken through the screen reader (e.g. "GOLD RESERVE"), interrupting the previous one.

**Why this first.** It needs zero edits to original game functions, works on every screen that has
right-click help, and proves the whole pipeline end to end (engine → platform → Tolk → NVDA). It
gives the blind tester something real to try within days.

**Files.**
- `platform/include/Platform_Speech.h`: `Platform_Speak`, `Platform_Speech_Stop`, startup/shutdown,
  `Platform_Speech_Last`/`Count` for tests.
- `platform/speech/PFL_Speech.c`: shared implementation, linked into every backend; log-only in
  `Platform_Headless`.
- `MoX/src/Access.c/.h`: help-area hit test (same as `Check_Help_List()`), `HELP.LBX` title
  reader, change detection, `Access_Frame()`.
- `src/ReMoM.c`: `--access` / `REMOM_ACCESS`, registers `Access_Frame` as the frame callback.
- `MoX/tests/test_Access.cpp`: 7 tests (synthetic `HELP.LBX`, hit test, speak-on-change).

**Trying it (Windows).**
1. Get Tolk's release ([dkager/tolk](https://github.com/dkager/tolk)), x64: copy `Tolk.dll` and
   `nvdaControllerClient64.dll` (plus `SAAPI64.dll` for System Access, optional) next to `ReMoM.exe`.
2. Start NVDA, then run `./ReMoM.exe --access`.
3. Move the mouse over the main-screen buttons and status areas.
4. Without `Tolk.dll`, the game runs normally and logs that speech is disabled.

**Trying it (Linux).** Install `speech-dispatcher` (`spd-say` on `PATH`), run `./ReMoM --access`.
`REMOM_SPEECH_LOG=1` prints each utterance to stderr on any platform.

**Known limits (by design, for the POC).**
- Mouse-driven only: a blind player still can't *aim* the mouse. This is a pipeline proof, not
  playability. Keyboard navigation is Phase 2.
- Titles are spoken as stored (upper case). Only the title, not the help body.
- Screens or areas without a help list are silent.
- `--scenario` owns the single frame-callback slot, so hover speech is off in scenario runs.
- Windows Tolk path is compiled (MinGW cross-compile) but **not yet run on Windows with NVDA**;
  that is the first thing to verify.

**Acceptance.** On Windows with NVDA running, hovering main-screen controls speaks their help titles,
once per change, without lag, and the game is unchanged without `--access`.

**Questions for the tester after Phase 0.** Is Tolk/NVDA speech working and responsive? Should titles
be read in normal case? Which screens matter most? What key layout do they expect (other accessible
games they play)?

---

## Phase 1 — Speech foundation
- Key to read the **full help text** of the hovered/focused item (the same text as the right-click
  scroll), and a "repeat last" key.
- Speak startup errors (missing game data) and the main menu, so a blind player can tell the game
  launched.
- Turn the single frame-callback slot into a small list, so `--access` and `--scenario` coexist
  (and `.hms` scenarios can test speech).
- Accessibility settings in a new options file; an options screen that is itself spoken.
- Normal-case titles, speech rate/verbosity left to the screen reader.
- **Acceptance:** a blind player can start the game, reach New Game, and hear help for any control.

## Phase 2 — Keyboard navigation (also serves limited-mobility players)
- A **focus model over the current fields list**: Tab / Shift-Tab (and arrows) move focus, the focused
  field is spoken (help title, else a label derived from field type/string), Enter/Space activates
  via `Platform_Warp_Mouse` + synthetic click. Escape = right-click/back where the original uses it.
- Optional visible focus highlight for sighted keyboard users.
- Remappable keys; controller support through SDL GameController mapped onto the same actions.
- **Acceptance:** New Game through wizard creation is completable without a mouse.

## Phase 3 — Screen text
- Capture text drawn by `Print()` / `Print_Paragraph()` per frame into a "screen text" buffer
  (one-line hooks), so dialogs, messages and reports can be read ("read screen" key, auto-read for
  pop-up messages).
- **Acceptance:** event messages, diplomacy text and reports are spoken.

## Phase 4 — Vision: colorblind, low vision, photosensitivity
- Palette filter in `Platform_Video_Update()` (all backends): deuteranopia/protanopia/tritanopia
  simulations-in-reverse or alternate banner palette entries; optional banner symbols.
- Larger cursor option, high-contrast option for text, "reduce flashing".
- **Acceptance:** all six wizard banners distinguishable under each colorblind filter (checked with a
  simulator and a colorblind tester).

## Phase 5 — Game-state descriptions (the big one)
- **Overland map:** keyboard tile cursor; "what's here" (terrain, special, city, stacks, owner),
  "what's around me", jump to next unit with moves / next city / next event.
- **City screen:** population, production, buildings, queue, all speakable and keyboard-editable.
- **Combat:** grid cursor, unit list with stats, targets in range, spoken results.
- **Turn summary** at start of turn.
- Lives in `MoM/` (it needs game knowledge), reading state only.
- **Acceptance:** a blind tester plays a full small game.

## Phase 6 — Deaf / hard of hearing
- Visual cue for meaningful sounds (spell cast, combat hit, event fanfare), separate music/effects
  volume, optional on-screen captions for sound events.

---

## Testing
- Unit tests in `MOX_tests` (headless speech log) for each announcement rule.
- `.hms` scenarios asserting speech after scripted input (needs Phase 1's callback list).
- Real users: the requesting community member plus at least one more blind player; a colorblind
  tester for Phase 4. Blind 4X players can be found on audiogames.net.

## Open questions
1. Windows only for speech at first, or all three platforms? (POC supports all three; Windows is the
   one that matters most.)
2. Ship `Tolk.dll` in the Windows zip/installer, or document "drop it in"? (Licence: LGPLv3; dynamic
   loading is compatible.)
3. Key layout: copy conventions from an accessible strategy game the testers already know.
4. How much of Phase 5 is needed before blind players can actually play — overland first, or combat
   first (auto-combat exists)?

## Risks
- Phase 5 is large and game-specific; scope it with testers, screen by screen.
- Fidelity rule: every hook into original code must be a one-line `/* CLAUDE */` call; review each.
- Frame-callback ordering vs replay determinism: accessibility must stay read-only so recordings and
  replays are unaffected.
