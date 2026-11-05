# voice-rec: plan

## Goal

Talk into the phone; later find the note transcribed, titled, filed in Obsidian and linked in the
knowledge graph — without thinking about networks, files or servers.

## Done (first cut)

- TaskDeck **Voice notes** job: record (pause/resume/discard), history oldest-first with recorded and
  transcribed times, a queue that moves on by itself (Wi-Fi connecting, app start, polling while the
  workstation works), Wi-Fi-only per stage with a per-recording override, crash recovery of half-saved audio.
- Workstation server: Postgres with audio as `bytea`, faster-whisper (GPU or CPU) or an external Whisper
  server, llama.cpp categorizing, Obsidian notes, Neo4j or webhook graph sink, CLI, a browse/play page.
- `setup.sh` to paste into Termius; tests for both sides; CI.

## To find out on the real devices

1. **Recording with the screen off.** Android 14+ only lets an app keep the microphone in the background
   with a *microphone* foreground service. TaskDeck's service is a *data sync* one. Test: record, lock the
   phone for 2 minutes, unlock, stop. If the audio is cut short, add the microphone type to
   `native/background` (`foregroundServiceType="dataSync|microphone"` + `FOREGROUND_SERVICE_MICROPHONE`).
2. **Ports**: localhost 8094, HTTPS 8445 through Tailscale Serve (8093 and 8444 are photo-ocr).
3. **Whisper speed** on the workstation's GPU (or CPU) for a 5-minute note, and whether `large-v3-turbo`
   fits next to the llama.cpp model in VRAM. If not: `WHISPER_DEVICE=cpu` with `small`, or `distil-large-v3`.
4. **The LLM's categories**: after ~20 notes, look at what landed in Inbox and adjust `categories.txt`.

## Decisions needed from you

- **Knowledge graph**: which one is it (Neo4j, Memgraph, Graphiti, LightRAG, something else)? Its URL and
  login. Neo4j and a generic webhook work today.
- **Obsidian vault**: where it lives on the workstation, and how it reaches your other devices (Obsidian
  Sync, Syncthing, git). The server only writes files into the folder.
- **Existing Postgres?** setup.sh starts its own on port 5433. If you already run one you'd rather use, set
  `DATABASE_URL` and drop the `db` service.
- **Archive automatically?** Today the phone asks for it (so the archive stage obeys Wi-Fi only). With
  `AUTO_ARCHIVE=1` the workstation files notes as soon as they're transcribed.
- **Keep audio on the phone after archiving?** On by default (a TaskDeck setting).

## Next (one at a time)

1. Play a note on the phone (needs the `flet-audio` plugin in the APK).
2. Edit a transcript or title on the phone before archiving, sent with the archive request.
3. Search notes from TaskDeck (the server already has full-text search: `GET /api/recordings?q=`).
4. Nightly `pg_dump`, keeping the last 7.
5. Speaker diarization / timestamps in the Obsidian note (segments are already stored).
