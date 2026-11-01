"""Voice notes: record on the phone, transcribe and archive on the AI Workstation.

The app shows ``screen.VoiceRecScreen`` (record button, history, per-recording network override) and
keeps a :class:`screen.VoiceSync` running from start-up, so queued notes move on when Wi-Fi connects.
The pipeline below only lists the history (no network), for tests and the generic runner.
See docs/jobs/voice_rec.md and voice-rec/README.md (the workstation side).
"""

from __future__ import annotations

from pathlib import Path

from taskdeck.core import Field, JobPlugin, Pipeline, Problem, Result, SettingsAction, Stage, default_data_dir, step

from .client import base_url, test_connection
from .library import ARCHIVED, Library, clock, local_time

SETTINGS = (
    Field.text("server_url", "Workstation address",
               help="The voice-rec server, e.g. https://ai-workstation.tail1234.ts.net:8093 (Tailscale Serve) "
                    "or http://100.83.88.59:8093. The setup script prints it."),
    # Not in the fingerprint vault on purpose: notes are sent in the background, often while the vault is
    # locked. The server is only reachable over Tailscale; the token is a second lock on top of that.
    Field.text("token", "Access token",
               help="VOICE_REC_TOKEN from ~/.config/secrets.env on the workstation (the setup script prints it)."),
    Field.toggle("transcribe_wifi_only", "Transcribe: Wi-Fi only", default=True,
                 help="Send new recordings only on Wi-Fi. Each recording can override this."),
    Field.toggle("archive_wifi_only", "Archive: Wi-Fi only", default=True,
                 help="Ask the workstation to archive only on Wi-Fi. Each recording can override this."),
    Field.toggle("auto_archive", "Archive automatically after transcription", default=True,
                 help="Off: each note waits for its own Archive button (to read it over first)."),
    Field.toggle("keep_audio", "Keep the audio on the phone after archiving", default=True,
                 help="Off: the phone's copy is deleted once the workstation has archived it."),
)


def library() -> Library:
    return Library(Path(default_data_dir()) / "voice_rec")


@step(Stage.PRESENT, name="list recordings")
def list_recordings(ctx):
    recs = library().all()  # oldest first
    rows = [(r.display_title, f"recorded {local_time(r.recorded_at)} · {clock(r.duration)} · "
                              + (f"transcribed {local_time(r.transcribed_at)}" if r.transcribed_at else r.status))
            for r in recs]
    done = sum(1 for r in recs if r.status == ARCHIVED)
    ctx.result = Result(text=f"{len(recs)} recordings, {done} archived.", rows=rows, title="Voice notes")


class VoiceRec(JobPlugin):
    id = "voice_rec"
    title = "Voice notes"
    description = "Record voice notes on the phone; the AI Workstation transcribes, sorts and archives them."
    category = "Capture"
    icon = "MIC"
    tags = ("voice", "record", "recorder", "memo", "dictate", "transcribe", "whisper", "audio", "note", "obsidian")
    settings = SETTINGS
    settings_actions = (SettingsAction("Test connection", lambda s: test_connection(s.get("server_url", ""),
                                                                                     s.get("token", "")),
                                       icon="NETWORK_CHECK"),)
    run_label = "List recordings"

    def check_ready(self, settings):
        problems = []
        url = base_url(settings.get("server_url") or "")
        if not url:
            problems.append(Problem("Recording works now. To transcribe, set the workstation address in this "
                                    "job's settings.", blocking=False))
        elif "localhost" in url or "127.0.0.1" in url:
            problems.append(Problem("The workstation address points at the phone itself (localhost). Use its "
                                    "Tailscale name or address.", blocking=False))
        return problems

    def build_pipeline(self):
        return Pipeline([list_recordings])

    def make_screen(self, app, preset=None):
        from .screen import VoiceRecScreen  # needs flet; the logic above doesn't

        return VoiceRecScreen(app, self)

    def on_app_start(self, app) -> None:
        """Start the background sync, so queued notes move on without opening this job."""
        from .screen import VoiceSync

        VoiceSync.for_app(app, self).kick()


PLUGIN = VoiceRec()
