# voice-rec

Voice notes from the phone, transcribed and filed on the AI Workstation.

```
 Phone (TaskDeck → Voice notes)                 AI Workstation (this folder, Docker)
 ──────────────────────────────                 ─────────────────────────────────────────────
 1 Record   → .m4a in app storage
 2 Transcribe (Wi-Fi only*) ── PUT audio ──▶    Postgres (audio as bytea, status = queued)
                                                Whisper (faster-whisper, GPU if there is one)
            ◀── GET transcript ──────────        status = transcribed
 3 Archive  (Wi-Fi only*) ── POST archive ─▶    llama.cpp: title, category, tags, summary,
                                                           actions, people/projects/places
                                                → Obsidian vault: Voice Notes/<Category>/<date> <title>.md
                                                → knowledge graph (Neo4j or a webhook)
            ◀── GET archived ────────────        status = archived
 * per recording, each stage can be allowed to use mobile data
```

The phone keeps the history (oldest first) with the time each note was **recorded** and **transcribed**.
Nothing leaves your machines: Tailscale is the only way in, and the server also wants a token.

## Set it up (from Termius on the phone)

Connect to the workstation (Tailscale SSH, `100.83.88.59`, as for the budget app), then paste:

```bash
cd ~/develai/ScratchPatch && git checkout main && git pull && bash voice-rec/setup.sh
```

It asks one question (where your Obsidian vault is on the workstation; Enter takes `~/Obsidian/Notes`), then:

* adds `VOICE_REC_TOKEN` to `~/.config/secrets.env` (the file the budget app uses; the existing `LLM_URL`,
  `LLM_API_KEY`, `LLM_MODEL` are reused for categorizing),
* writes `voice-rec/.env` (database password, vault path, GPU or CPU) — not in git,
* runs Whisper (`small`) on the CPU: llama.cpp's model fills the GPU. `USE_GPU=1` puts it on the GPU (`large-v3-turbo`),
* starts Postgres (`127.0.0.1:5433`) and the server (`127.0.0.1:8094`; 8093 is photo-ocr), waits until it answers,
* sets up Tailscale Serve at `https://<workstation>.<tailnet>.ts.net:8445` (443 is budget, 8444 photo-ocr),
* prints the **address** and **token** to enter in TaskDeck: *Voice notes → tune icon*, then *Test connection*.

Run the same line again after any update. The first note is slow: the Whisper model downloads once
(~1.6 GB for large-v3-turbo) into `~/voice-rec/models`.

Copying from Termius: long-press the output to select. The token is also in `~/.config/secrets.env`.

## Everyday

| | |
|---|---|
| Watch it work | `cd ~/develai/ScratchPatch/voice-rec && docker compose logs -f voice-rec` |
| Counts per status | `docker compose exec voice-rec python voicerec.py status` |
| List / search | `docker compose exec voice-rec python voicerec.py list -q dentist` |
| Retry one | `docker compose exec voice-rec python voicerec.py retry <id>` |
| Re-file all (after editing `categories.txt`) | `docker compose exec voice-rec python voicerec.py rearchive --all` |
| Browse and play notes | `https://<workstation>:8445/?token=<token>` in the phone's browser |
| Back up the database | `docker compose exec db pg_dump -U voicerec voicerec \| gzip > ~/voice-rec/backup-$(date +%F).sql.gz` |

## Settings (`~/.config/secrets.env`, then `docker compose up -d`)

| Variable | What |
|---|---|
| `VOICE_REC_TOKEN` | Required by the API (made by setup.sh). Same value in TaskDeck. |
| `LLM_URL`, `LLM_API_KEY`, `LLM_MODEL` | llama.cpp's server, for categorizing (`VOICE_REC_LLM_*` override them for this app). Without it, notes go to Inbox. |
| `AUTO_ARCHIVE=1` | Archive right after transcription, without waiting for the phone. |
| `WHISPER_MODEL` | In `voice-rec/.env`. `small` (CPU), `large-v3-turbo` (GPU), `medium`, `distil-large-v3`… |
| `WHISPER_LANGUAGE` | e.g. `en` to skip language detection. |
| `TRANSCRIBE_URL`, `TRANSCRIBE_API_KEY`, `TRANSCRIBE_MODEL` | Use a Whisper server you already run (OpenAI-style `/v1/audio/transcriptions`) instead of the built-in one. |
| `OBSIDIAN_FOLDER` | Folder inside the vault (default `Voice Notes`). `OBSIDIAN_AUDIO=0` leaves the audio out of the vault. |
| `GRAPH_KIND`, `GRAPH_URL`, `GRAPH_USER`, `GRAPH_PASSWORD`, `GRAPH_DATABASE` | Knowledge graph, below. |

## Where things end up

**Postgres** (`recordings` table): the audio (`bytea`), timestamps (`recorded_at`, `received_at`,
`transcribed_at`, `archived_at`), the transcript with per-segment times, title, category, tags, summary,
actions, entities, and a full-text index. One table, no files to keep in step with it. A minute of audio is
about 0.35 MB, so even years of notes stay small; `bytea` is the right fit up to tens of MB per row.

**Obsidian**: one Markdown note per recording in `Voice Notes/<Category>/`, with front matter
(`recorded`, `transcribed`, `category`, `tags`, `people`, `projects`) for Dataview, the audio embedded
(`Voice Notes/_audio/`), action items as `- [ ]` tasks, and people/projects/places as `[[wikilinks]]`, so
they appear in the graph view. Write your own thoughts under **Your notes** at the bottom: re-filing keeps them.

**Knowledge graph** (`GRAPH_KIND`):

* `neo4j` — `GRAPH_URL=http://localhost:7474`, `GRAPH_USER`, `GRAPH_PASSWORD`. Creates
  `(:VoiceNote)-[:IN_CATEGORY]->(:Category)`, `-[:TAGGED]->(:Tag)`, `-[:RECORDED_ON]->(:Day)`,
  `-[:MENTIONS]->(:Entity {type})`, `-[:HAS_ACTION]->(:Action)`. Re-sending a note replaces its edges.
* `webhook` — POSTs each note as JSON to `GRAPH_URL` (`GRAPH_PASSWORD` as a Bearer token): for Graphiti,
  LightRAG, an n8n flow or your own ingester.

## If something is off

* **TaskDeck says the workstation isn't reachable** — Tailscale on the phone, then `curl -s
  -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8094/health` on the workstation.
* **`tailscale serve` fails on port 8445** — use another HTTPS port:
  `sudo tailscale serve --bg --https=8446 http://127.0.0.1:8094`, and that port in TaskDeck.
* **Whisper on the GPU** — only if the GPU has room (llama.cpp's coder model uses most of it): `USE_GPU=1 bash
  voice-rec/setup.sh`. Docker needs the NVIDIA runtime first: install the NVIDIA Container Toolkit
  (`sudo apt install nvidia-container-toolkit && sudo nvidia-ctk runtime configure --runtime=docker &&
  sudo systemctl restart docker`), then run setup.sh again.
* **A note says "transcribe: …" and stays failed** — after 3 tries it stops; fix the cause, then `retry <id>`.

## Tests

```bash
VOICE_REC_TEST_DSN=postgresql://postgres@127.0.0.1:5432/postgres python -m pytest voice-rec/tests
```

They use a fake transcriber, a stub LLM, stub Neo4j and webhook servers, and a fresh schema in a real Postgres.

## Open questions (decide as we go)

See [PLAN.md](PLAN.md).
