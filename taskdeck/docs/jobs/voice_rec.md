# Voice notes

Record on the phone; the AI Workstation transcribes, sorts and archives each note (Postgres, Obsidian,
knowledge graph). The workstation side and its setup are in [voice-rec/README.md](../../../voice-rec/README.md).

## On the phone

* **Record**: the microphone button; pause/resume, discard, stop to save. Audio is AAC (`.m4a`), 16 kHz mono,
  about 0.35 MB a minute, in TaskDeck's private storage (`voice_rec/audio/`). A recording carries on if you
  leave the screen. Half-saved audio from a crash is picked up again on the next start.
* **History**: every note, oldest first, with when it was **recorded** and when it was **transcribed**, its
  status and category. Tap one for the transcript, tags, summary, copy, delete and its network switches.
* **Queue**: notes move on by themselves: when one is recorded, when the app starts, when Wi-Fi connects, and
  every few seconds (backing off to 2 min) while the workstation is busy with one. *Sync now* forces a pass.

## Stages and the network

| Stage | What the phone does | Default |
|---|---|---|
| Record | nothing on the network | — |
| Transcribe | sends the audio; fetches the transcript back | Wi-Fi only |
| Archive | asks the workstation to file it; fetches title/category back | Wi-Fi only |

Defaults are in the job's settings. Each recording can override them: *Use mobile data for this note* while
recording (both stages), or per stage in the note's details. When the phone doesn't know what network it is
on (desktop), everything counts as Wi-Fi.

## Settings

| Setting | |
|---|---|
| Workstation address | printed by `voice-rec/setup.sh`, e.g. `https://ai-workstation.tail1234.ts.net:8093` |
| Access token | `VOICE_REC_TOKEN`, printed by setup.sh. Kept in plain settings, not the fingerprint vault, so notes can be sent while the vault is locked; the server is only reachable over Tailscale anyway. |
| Transcribe / Archive: Wi-Fi only | the defaults above |
| Archive automatically | off: each note waits for its *Archive now* button |
| Keep the audio on the phone | off: deleted from the phone once archived (it stays in Postgres and the vault) |

## Needs the new APK

The microphone plugin (`flet-audio-recorder`) and the `RECORD_AUDIO` permission are part of the APK, so a
live update alone isn't enough: until the new APK is installed the job says so instead of recording. The rest
(history, sending queued notes) works on any version.

## Code

`src/taskdeck/jobs/voice_rec/`: `library.py` (files + index), `sync.py` (one pass of the queue, pure),
`client.py` (HTTP to the workstation), `screen.py` (UI, recorder, background sync). Tests:
`tests/test_voice_rec.py`.
