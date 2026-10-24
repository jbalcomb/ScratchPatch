# ReMoM Accessibility

Accessibility plan and proof of concept for [ReMoM](https://github.com/jbalcomb/ReMoM), the
reassembled Master of Magic: screen reader support for blind players first, then keyboard-only play,
colorblind, low-vision, deaf/hard-of-hearing and limited-mobility options.

| File | What it is |
|---|---|
| [PRD-Accessibility.md](PRD-Accessibility.md) | The plan: who it's for, architecture, Phase 0 (POC) through Phase 6. |
| [patches/](patches/) | Phase 0 as a patch against ReMoM `main` @ `a9cc082`. The same PRD is included in it, at `doc/@AI_Plans/`. |

## Phase 0: "hover to hear"

Run `ReMoM --access` (or set `REMOM_ACCESS=1`). The game says "Master of Magic. Speech on.", then
speaks the right-click-help title of whatever the mouse moves onto, through the running screen reader
(NVDA, JAWS, ...) via Tolk on Windows, `spd-say` on Linux, or `say` on macOS. Off by default, and no
original game function is edited.

### Apply to a ReMoM checkout

```bash
cd /c/STU/devel/ReMoM
git checkout -b accessibility-poc
git am --keep-cr /path/to/ScratchPatch/remom-accessibility/patches/0001-*.patch
```

`--keep-cr` matters: `MoX/tests/CMakeLists.txt` has Windows line endings, which plain `git am`
strips. `git apply <patch>` also works if you'd rather commit yourself.

### Try it on Windows with NVDA

1. Build as usual: `cmake --workflow --preset=MSVC-debug`.
2. From the x64 [Tolk](https://github.com/dkager/tolk) release, copy `Tolk.dll` and
   `nvdaControllerClient64.dll` next to `ReMoM.exe`.
3. Start NVDA, then `./ReMoM.exe --access`, and move the mouse over the main-screen buttons.

Without `Tolk.dll` the game runs normally and logs that speech is off. `REMOM_SPEECH_LOG=1` prints
every utterance to stderr on any platform.

### What was verified, and what wasn't

- Builds with the `clang-nosound-debug` preset (Linux, SDL2) with no warnings in the new files.
- 7 new `MOX_tests` tests pass: synthetic `HELP.LBX` parsing, help-area hit testing, speak-once-on-change.
- Full `ctest`: same 17 failures with and without the patch. All of them need the original `.LBX` game
  files, which this environment doesn't have.
- Linux speech path checked with a stand-in `spd-say` that records its arguments: the cancel goes
  before each new utterance, and text is passed as a single argument with no shell involved.
- Windows Tolk code compiles (MinGW cross-compile) but has **not been run on Windows or with NVDA**,
  and nothing has been run with real game data. Those are the first things to check.
