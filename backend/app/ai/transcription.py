"""Speech-to-text for VIDEO and AUDIO content (Oct 20 decision: OpenAI's transcription API).

Claude can't listen to audio, so media goes through a `Transcriber` first; the resulting transcript
becomes TRANSCRIPT evidence chunks with timestamps, and concept extraction / question generation then
run on that text exactly as for any other material.

- `StubTranscriber` (local/tests): deterministic. A title containing "[fail]" simulates media with no
  usable speech; "[outage]" simulates the provider being down (retryable).
- `OpenAITranscriber`: POST /v1/audio/transcriptions. Media is first normalised with ffmpeg into
  mono 16 kHz MP3 pieces of 10 minutes, which keeps every request far below the API's 25 MB upload
  limit and gives each piece a known time offset. Without ffmpeg, only files already under the limit
  and in a format the API accepts are sent as-is.

Enable with AAYRA_TRANSCRIPTION_PROVIDER=openai, AAYRA_OPENAI_API_KEY and AAYRA_TRANSCRIPTION_MODEL.
"""

import asyncio
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

import httpx

from app.ai.content_ai import ContentExtractionError, Usage
from app.config import get_settings

API_LIMIT_BYTES = 25 * 1024 * 1024
PIECE_SECONDS = 600
ACCEPTED_EXTENSIONS = {".mp3", ".mp4", ".mpeg", ".mpga", ".m4a", ".wav", ".webm"}
MIME_EXTENSIONS = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a", "audio/m4a": ".m4a",
    "audio/wav": ".wav", "audio/x-wav": ".wav", "audio/webm": ".webm", "video/mp4": ".mp4", "video/webm": ".webm",
    "video/mpeg": ".mpeg", "video/quicktime": ".mov", "audio/ogg": ".ogg", "audio/flac": ".flac",
}  # fmt: skip


class TranscriptionUnavailable(Exception):
    """Provider down / rate limited / timed out: the job is retried."""


@dataclass(frozen=True)
class Segment:
    start: float
    end: float
    text: str


@dataclass
class Transcript:
    segments: list[Segment]
    duration_seconds: float | None = None
    language: str | None = None
    usage: Usage | None = None
    notes: list[str] = field(default_factory=list)


class Transcriber(Protocol):
    model_name: str
    prompt_version: str

    async def transcribe(self, data: bytes, *, mime_type: str | None, label: str) -> Transcript: ...


class StubTranscriber:
    model_name = "stub-transcriber"
    prompt_version = "stub-v1"

    async def transcribe(self, data: bytes, *, mime_type: str | None, label: str) -> Transcript:
        if "[fail]" in label:
            raise ContentExtractionError("NO_SPEECH_FOUND", f"No speech could be recognised in '{label}'.")
        if "[outage]" in label:
            raise TranscriptionUnavailable("stub transcription outage")
        segs = [
            Segment(0.0, 42.0, f"Welcome. Today we look at {label}: what it is and why it matters."),
            Segment(42.0, 95.5, f"Here is the step-by-step process behind {label}, with the key terms."),
            Segment(95.5, 150.0, f"Finally, a real-world example of {label} in action."),
        ]
        return Transcript(segs, duration_seconds=150.0, language="en", usage=Usage(150, 60))


# ------------------------------------------------------------------------------------------------
# Media preparation (ffmpeg)
# ------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class AudioPiece:
    data: bytes
    file_name: str
    offset_seconds: float


def _extension(mime_type: str | None) -> str:
    return MIME_EXTENSIONS.get((mime_type or "").lower().split(";")[0].strip(), ".bin")


async def _run(*args: str) -> tuple[int, bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(*args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    out, err = await proc.communicate()
    return proc.returncode or 0, out, err


async def probe_duration(path: str) -> float | None:
    if not shutil.which("ffprobe"):
        return None
    code, out, _ = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path
    )
    try:
        return float(out.strip()) if code == 0 else None
    except ValueError:
        return None


async def prepare_audio(
    data: bytes, mime_type: str | None, *, max_seconds: int
) -> tuple[list[AudioPiece], float | None]:
    """Turn uploaded media into API-sized audio pieces with known start offsets."""
    ext = _extension(mime_type)
    if not shutil.which("ffmpeg"):
        if len(data) <= API_LIMIT_BYTES and ext in ACCEPTED_EXTENSIONS:
            return [AudioPiece(data, f"media{ext}", 0.0)], None
        raise ContentExtractionError(
            "MEDIA_TOO_LARGE" if len(data) > API_LIMIT_BYTES else "UNSUPPORTED_MEDIA_FORMAT",
            "This file needs converting before it can be transcribed (ffmpeg is not installed on the server).",
        )
    with tempfile.TemporaryDirectory(prefix="aayra-media-") as tmp:
        src = os.path.join(tmp, f"source{ext}")
        await asyncio.to_thread(Path(src).write_bytes, data)
        duration = await probe_duration(src)
        if duration is not None and duration > max_seconds:
            raise ContentExtractionError(
                "MEDIA_TOO_LONG", f"Media is {duration / 3600:.1f} h long; the limit is {max_seconds / 3600:.1f} h."
            )
        pattern = os.path.join(tmp, "piece%04d.mp3")
        code, _, err = await _run(
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", "32k", "-f", "segment", "-segment_time", str(PIECE_SECONDS), "-reset_timestamps", "1", pattern,
        )  # fmt: skip
        pieces = sorted(p for p in os.listdir(tmp) if p.startswith("piece"))
        if code != 0 or not pieces:
            raise ContentExtractionError(
                "NO_AUDIO_TRACK", "No audio could be read from this file: " + err.decode(errors="replace")[-200:]
            )
        out = []
        for i, p in enumerate(pieces):
            out.append(AudioPiece(await asyncio.to_thread(Path(tmp, p).read_bytes), p, float(i * PIECE_SECONDS)))
        return out, duration


# ------------------------------------------------------------------------------------------------
# OpenAI
# ------------------------------------------------------------------------------------------------
class OpenAITranscriber:
    prompt_version = "openai-stt-v1"

    def __init__(self, transport: httpx.AsyncBaseTransport | None = None):
        s = get_settings()
        if not s.openai_api_key or not s.transcription_model:
            raise RuntimeError(
                "AAYRA_OPENAI_API_KEY and AAYRA_TRANSCRIPTION_MODEL are required when "
                "AAYRA_TRANSCRIPTION_PROVIDER=openai"
            )
        self.model_name = s.transcription_model
        self.max_seconds = s.max_media_seconds
        self.client = httpx.AsyncClient(
            base_url=s.openai_base_url,
            timeout=s.transcription_timeout_seconds,
            transport=transport,
            headers={"Authorization": f"Bearer {s.openai_api_key}"},
        )

    @property
    def _timestamps(self) -> bool:
        # whisper-1 returns per-segment timestamps (verbose_json); the gpt-4o transcribe models return text.
        return self.model_name.startswith("whisper")

    async def _transcribe_piece(self, piece: AudioPiece) -> tuple[list[Segment], dict]:
        form: dict[str, str | list[str]] = {"model": self.model_name}
        if self._timestamps:
            form["response_format"] = "verbose_json"
            form["timestamp_granularities[]"] = "segment"
        else:
            form["response_format"] = "json"
        try:
            r = await self.client.post(
                "/v1/audio/transcriptions", data=form, files={"file": (piece.file_name, piece.data)}
            )
        except httpx.TimeoutException as exc:
            raise TranscriptionUnavailable(f"timeout: {exc}") from exc
        except httpx.TransportError as exc:
            raise TranscriptionUnavailable(f"network error: {exc}") from exc
        if r.status_code in (408, 409, 429) or r.status_code >= 500:
            raise TranscriptionUnavailable(f"transcription unavailable: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise ContentExtractionError(
                "UNREADABLE_SOURCE", f"Transcription rejected: HTTP {r.status_code} {r.text[:200]}"
            )
        body = r.json()
        off = piece.offset_seconds
        if body.get("segments"):
            segs = [Segment(off + s["start"], off + s["end"], s["text"].strip()) for s in body["segments"]]
        else:
            text = (body.get("text") or "").strip()
            segs = [Segment(off, off + float(body.get("duration") or PIECE_SECONDS), text)] if text else []
        return [s for s in segs if s.text], body

    async def transcribe(self, data: bytes, *, mime_type: str | None, label: str) -> Transcript:
        pieces, duration = await prepare_audio(data, mime_type, max_seconds=self.max_seconds)
        segments: list[Segment] = []
        tokens_in = tokens_out = 0
        language = None
        for piece in pieces:
            segs, body = await self._transcribe_piece(piece)
            segments.extend(segs)
            usage = body.get("usage") or {}
            tokens_in += int(usage.get("input_tokens") or 0)
            tokens_out += int(usage.get("output_tokens") or 0)
            language = language or body.get("language")
        if not segments:
            raise ContentExtractionError("NO_SPEECH_FOUND", "No speech could be recognised in this media.")
        return Transcript(segments, duration, language, Usage(tokens_in, tokens_out) if tokens_in else None)


_transcriber: Transcriber | None = None


def get_transcriber() -> Transcriber:
    global _transcriber
    if _transcriber is None:
        _transcriber = OpenAITranscriber() if get_settings().transcription_provider == "openai" else StubTranscriber()
    return _transcriber


def set_transcriber(t: Transcriber | None) -> None:
    global _transcriber
    _transcriber = t
