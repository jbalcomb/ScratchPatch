"""Speech to text. Two ways, chosen by the environment:

* Built in (default): faster-whisper on this machine. ``WHISPER_MODEL`` (default ``small``; on a GPU try
  ``large-v3-turbo``), ``WHISPER_DEVICE`` (``auto``, ``cuda`` or ``cpu``), ``WHISPER_COMPUTE`` (``default``,
  ``int8``, ``float16``…), ``WHISPER_LANGUAGE`` (empty = detect). The model downloads on first use into
  ``HF_HOME``.
* ``TRANSCRIBE_URL``: any OpenAI-compatible ``/v1/audio/transcriptions`` server you already run
  (whisper.cpp's server, speaches, LocalAI…), with ``TRANSCRIBE_API_KEY`` / ``TRANSCRIBE_MODEL``.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import urllib.request
import uuid
from dataclasses import dataclass, field

EXTENSIONS = {"audio/mp4": ".m4a", "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/wav": ".wav",
              "audio/x-wav": ".wav", "audio/ogg": ".ogg", "audio/webm": ".webm", "audio/mpeg": ".mp3",
              "audio/flac": ".flac"}


def extension(mime: str) -> str:
    return EXTENSIONS.get((mime or "").split(";")[0].strip().lower(), ".m4a")


@dataclass
class Transcript:
    text: str
    language: str | None = None
    segments: list[dict] = field(default_factory=list)  # [{"start": s, "end": s, "text": ...}]
    model: str = ""


class WhisperLocal:
    def __init__(self, model: str = "small", device: str = "auto", compute: str = "default", language: str = ""):
        self.model_name, self.device, self.compute, self.language = model, device, compute, language or None
        self._model = None
        self._lock = threading.Lock()

    @property
    def label(self) -> str:
        return f"faster-whisper {self.model_name} ({self.device})"

    def _load(self):
        with self._lock:
            if self._model is None:
                from faster_whisper import WhisperModel

                self._model = WhisperModel(self.model_name, device=self.device, compute_type=self.compute)
            return self._model

    def __call__(self, audio: bytes, mime: str) -> Transcript:
        model = self._load()
        with tempfile.NamedTemporaryFile(suffix=extension(mime)) as fh:
            fh.write(audio)
            fh.flush()
            segments, info = model.transcribe(fh.name, language=self.language, vad_filter=True, beam_size=5)
            segs = [{"start": round(s.start, 2), "end": round(s.end, 2), "text": s.text.strip()} for s in segments]
        text = " ".join(s["text"] for s in segs).strip()
        return Transcript(text, getattr(info, "language", None), segs, self.label)


class WhisperRemote:
    """POST multipart/form-data to an OpenAI-compatible transcription endpoint."""

    def __init__(self, url: str, api_key: str = "", model: str = "whisper-1", language: str = "", timeout: float = 900):
        url = url.rstrip("/")
        if not url.endswith("/audio/transcriptions"):
            url += "/v1/audio/transcriptions" if not url.endswith("/v1") else "/audio/transcriptions"
        self.url, self.api_key, self.model, self.language, self.timeout = url, api_key, model, language, timeout

    @property
    def label(self) -> str:
        return f"{self.model} at {self.url}"

    def __call__(self, audio: bytes, mime: str) -> Transcript:
        boundary = uuid.uuid4().hex
        fields = {"model": self.model, "response_format": "verbose_json"}
        if self.language:
            fields["language"] = self.language
        parts = []
        for name, value in fields.items():
            parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="{name}"\r\n\r\n{value}\r\n'.encode())
        parts.append(f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="note{extension(mime)}"\r\n'
                     f"Content-Type: {mime}\r\n\r\n".encode() + audio + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=b"".join(parts), headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return Transcript(raw.strip(), model=self.label)
        segs = [{"start": s.get("start"), "end": s.get("end"), "text": (s.get("text") or "").strip()}
                for s in data.get("segments") or []]
        return Transcript((data.get("text") or "").strip(), data.get("language"), segs, self.label)


def from_env(env=os.environ):
    if env.get("TRANSCRIBE_URL"):
        return WhisperRemote(env["TRANSCRIBE_URL"], env.get("TRANSCRIBE_API_KEY", ""),
                             env.get("TRANSCRIBE_MODEL", "whisper-1"), env.get("WHISPER_LANGUAGE", ""))
    return WhisperLocal(env.get("WHISPER_MODEL", "small"), env.get("WHISPER_DEVICE", "auto"),
                        env.get("WHISPER_COMPUTE", "default"), env.get("WHISPER_LANGUAGE", ""))
