"""Turning each content type into evidence chunks (contract §6.2, ContentIntelligenceService).

    VIDEO / AUDIO   -> speech-to-text (app.ai.transcription)   -> TRANSCRIPT chunks with timestamps
    YOUTUBE         -> the transcript the uploader provided     -> TRANSCRIPT chunks (+ optional metadata)
    DOCUMENT (Word) -> .docx parsed on the server               -> DOCUMENT_TEXT / TABLE chunks
    everything else -> the content model (ContentAI.extract)    -> PDF, images, web pages

YouTube (Oct 20 decision): we never download YouTube media (that breaks YouTube's terms) and the
official API only gives captions to a video's owner, so the teacher/parent/student pastes or uploads
the transcript. With AAYRA_YOUTUBE_API_KEY set, the official Data API also confirms the video exists
and supplies its title and duration.
"""

import io
import re
import time
import zipfile
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qs, urlparse
from xml.etree import ElementTree

import httpx

from app.ai.content_ai import Chunk, ContentExtractionError, Usage, get_content_ai
from app.ai.transcription import Segment, get_transcriber
from app.config import get_settings
from app.storage import get_storage

CHUNK_WORDS = 220
MAX_TRANSCRIPT_CHARS = 500_000


@dataclass
class Extraction:
    chunks: list[Chunk]
    method: str  # stored on content_evidence_chunks.extraction_method
    usage: Usage | None = None
    # set when a model was called (logged to ai_invocations); None for pure parsing
    model_name: str | None = None
    prompt_version: str | None = None
    latency_ms: int = 0
    item_updates: dict[str, Any] | None = None  # e.g. duration_seconds / language_code / title


# ------------------------------------------------------------------------------------------------
# Chunking helpers
# ------------------------------------------------------------------------------------------------
def chunk_segments(segments: list[Segment], words: int = CHUNK_WORDS) -> list[Chunk]:
    chunks: list[Chunk] = []
    buf: list[Segment] = []
    count = 0
    for seg in segments:
        buf.append(seg)
        count += len(seg.text.split())
        if count >= words:
            chunks.append(Chunk("TRANSCRIPT", " ".join(s.text for s in buf), None, buf[0].start, buf[-1].end))
            buf, count = [], 0
    if buf:
        chunks.append(Chunk("TRANSCRIPT", " ".join(s.text for s in buf), None, buf[0].start, buf[-1].end))
    return chunks


_TS = r"(?:(\d+):)?(\d{1,2}):(\d{2})[.,](\d{1,3})"
_CUE = re.compile(rf"{_TS}\s*-->\s*{_TS}")


def _secs(h: str | None, m: str, s: str, ms: str) -> float:
    return int(h or 0) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def parse_transcript(text: str) -> list[Segment]:
    """Accepts WebVTT, SRT, or plain text (pasted from YouTube's "Show transcript", with or
    without leading m:ss stamps)."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not text:
        return []
    if _CUE.search(text):
        segments: list[Segment] = []
        for block in re.split(r"\n\s*\n", text):
            lines = [ln.strip() for ln in block.split("\n") if ln.strip()]
            for i, ln in enumerate(lines):
                m = _CUE.search(ln)
                if m:
                    body = " ".join(re.sub(r"<[^>]+>", "", t) for t in lines[i + 1 :]).strip()
                    if body and (not segments or segments[-1].text != body):  # auto-captions repeat lines
                        segments.append(Segment(_secs(*m.groups()[:4]), _secs(*m.groups()[4:]), body))
                    break
        return segments
    stamped = re.compile(r"^(?:(\d+):)?(\d{1,2}):(\d{2})\s*(.*)$")
    segments = []
    pending: float | None = None
    for ln in (ln.strip() for ln in text.split("\n")):
        if not ln:
            continue
        m = stamped.match(ln)
        if m:
            start = int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
            if m.group(4):
                segments.append(Segment(float(start), float(start), m.group(4)))
            else:
                pending = float(start)
            continue
        start = pending if pending is not None else (segments[-1].end if segments else 0.0)
        segments.append(Segment(start, start, ln))
        pending = None
    if any(s.start for s in segments):  # close each stamped segment at the next one's start
        segments = [Segment(s.start, segments[i + 1].start if i + 1 < len(segments) else s.start, s.text)
                    for i, s in enumerate(segments)]  # fmt: skip
        return segments
    return [Segment(0.0, 0.0, s.text) for s in segments] if segments else []


# ------------------------------------------------------------------------------------------------
# Word documents
# ------------------------------------------------------------------------------------------------
W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _para_text(p: ElementTree.Element) -> tuple[str, bool]:
    parts: list[str] = []
    page_break = False
    for el in p.iter():
        if el.tag == f"{W}t" and el.text:
            parts.append(el.text)
        elif el.tag == f"{W}tab":
            parts.append("\t")
        elif el.tag == f"{W}br" and el.get(f"{W}type") == "page":
            page_break = True
        elif el.tag == f"{W}lastRenderedPageBreak":
            page_break = True
    return "".join(parts).strip(), page_break


def docx_to_chunks(data: bytes, words: int = CHUNK_WORDS) -> list[Chunk]:
    if data[:4] == b"\xd0\xcf\x11\xe0":
        raise ContentExtractionError(
            "UNSUPPORTED_DOCUMENT_FORMAT", "This is an old .doc file. Save it as .docx or PDF and upload it again."
        )
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except (zipfile.BadZipFile, KeyError) as exc:
        raise ContentExtractionError(
            "UNSUPPORTED_DOCUMENT_FORMAT", "Only Word .docx files are supported here. Save it as .docx or PDF."
        ) from exc
    try:
        body = ElementTree.fromstring(xml).find(f"{W}body")
    except ElementTree.ParseError as exc:
        raise ContentExtractionError("UNREADABLE_SOURCE", "The document is damaged and can't be read.") from exc

    chunks: list[Chunk] = []
    page = 1
    buf: list[str] = []
    buf_page = 1

    def flush() -> None:
        nonlocal buf
        if buf:
            chunks.append(Chunk("DOCUMENT_TEXT", "\n".join(buf), buf_page))
            buf = []

    for el in list(body) if body is not None else []:
        if el.tag == f"{W}p":
            text, page_break = _para_text(el)
            style = el.find(f"{W}pPr/{W}pStyle")
            heading = style is not None and re.match(r"(?i)(heading|title)", style.get(f"{W}val") or "")
            if page_break:
                page += 1
            if not text:
                continue
            if heading:
                flush()
            if not buf:
                buf_page = page
            buf.append(text)
            if sum(len(t.split()) for t in buf) >= words:
                flush()
        elif el.tag == f"{W}tbl":
            flush()
            rows = []
            for tr in el.iter(f"{W}tr"):
                cells = [" ".join(_para_text(p)[0] for p in tc.iter(f"{W}p")).strip() for tc in tr.iter(f"{W}tc")]
                if any(cells):
                    rows.append(" | ".join(cells))
            if rows:
                chunks.append(Chunk("TABLE", "\n".join(rows), page))
    flush()
    if not chunks:
        raise ContentExtractionError("NOT_ENOUGH_TEXT", "The document has no readable text.")
    return chunks


# ------------------------------------------------------------------------------------------------
# YouTube
# ------------------------------------------------------------------------------------------------
_YT_ID = re.compile(r"^[A-Za-z0-9_-]{11}$")


def youtube_video_id(url: str) -> str | None:
    u = urlparse(url)
    host = (u.hostname or "").lower().removeprefix("www.").removeprefix("m.")
    vid = None
    if host == "youtu.be":
        vid = u.path.strip("/").split("/")[0]
    elif host in ("youtube.com", "youtube-nocookie.com", "music.youtube.com"):
        if u.path == "/watch":
            vid = (parse_qs(u.query).get("v") or [None])[0]
        else:
            m = re.match(r"^/(?:embed|shorts|live|v)/([^/?#]+)", u.path)
            vid = m.group(1) if m else None
    return vid if vid and _YT_ID.match(vid) else None


def _iso_duration(value: str) -> int | None:
    m = re.match(r"^P(?:(\d+)D)?T?(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?$", value or "")
    if not m:
        return None
    d, h, mi, s = (int(x or 0) for x in m.groups())
    return d * 86400 + h * 3600 + mi * 60 + s


async def youtube_metadata(video_id: str, transport: httpx.AsyncBaseTransport | None = None) -> dict | None:
    key = get_settings().youtube_api_key
    if not key:
        return None
    try:
        async with httpx.AsyncClient(timeout=15, transport=transport) as c:
            r = await c.get(
                "https://www.googleapis.com/youtube/v3/videos",
                params={"part": "snippet,contentDetails,status", "id": video_id, "key": key},
            )
    except httpx.HTTPError:
        return None  # metadata is optional; never fail the lesson over it
    if r.status_code >= 400:
        return None
    items = r.json().get("items") or []
    if not items:
        raise ContentExtractionError("VIDEO_NOT_FOUND", "This YouTube video doesn't exist or isn't available.")
    v = items[0]
    if (v.get("status") or {}).get("privacyStatus") == "private":
        raise ContentExtractionError("VIDEO_PRIVATE", "This YouTube video is private.")
    return {
        "title": (v.get("snippet") or {}).get("title"),
        "duration_seconds": _iso_duration((v.get("contentDetails") or {}).get("duration", "")),
    }


# ------------------------------------------------------------------------------------------------
# Dispatcher
# ------------------------------------------------------------------------------------------------
async def extract_item(item: dict[str, Any]) -> Extraction:
    ctype = item["content_type"]
    label = item.get("title") or ctype.lower()
    if ctype in ("VIDEO", "AUDIO"):
        t = get_transcriber()
        data = await get_storage().download(item["object_key"])
        started = time.perf_counter()
        transcript = await t.transcribe(data, mime_type=item.get("mime_type"), label=label)
        chunks = chunk_segments(transcript.segments)
        updates = {}
        if transcript.duration_seconds and not item.get("duration_seconds"):
            updates["duration_seconds"] = int(round(transcript.duration_seconds))
        if transcript.language and not item.get("language_code"):
            updates["language_code"] = transcript.language[:20]
        return Extraction(chunks, t.model_name, transcript.usage, t.model_name, t.prompt_version,
                          int((time.perf_counter() - started) * 1000), updates)  # fmt: skip
    if ctype == "YOUTUBE":
        if not item.get("object_key"):
            raise ContentExtractionError("TRANSCRIPT_REQUIRED", "Add the video's transcript before it can be used.")
        raw = (await get_storage().download(item["object_key"])).decode("utf-8", errors="replace")
        chunks = chunk_segments(parse_transcript(raw[:MAX_TRANSCRIPT_CHARS]))
        if not chunks:
            raise ContentExtractionError("NOT_ENOUGH_TEXT", "The transcript is empty.")
        updates: dict[str, Any] = {}
        vid = youtube_video_id(item.get("source_url") or "")
        meta = await youtube_metadata(vid) if vid else None
        if meta:
            if meta.get("title") and not item.get("title"):
                updates["title"] = meta["title"][:250]
            if meta.get("duration_seconds") and not item.get("duration_seconds"):
                updates["duration_seconds"] = meta["duration_seconds"]
        return Extraction(chunks, "provided-transcript", item_updates=updates)
    if ctype == "DOCUMENT":
        data = await get_storage().download(item["object_key"])
        return Extraction(docx_to_chunks(data), "docx-parser")
    ai = get_content_ai()
    started = time.perf_counter()
    chunks, usage = await ai.extract(item)
    return Extraction(chunks, ai.model_name, usage, ai.model_name, ai.prompt_version,
                      int((time.perf_counter() - started) * 1000))  # fmt: skip


def model_for(content_type: str) -> tuple[str, str] | None:
    """Which model (name, prompt_version) handles a content type, for logging failures."""
    if content_type in ("VIDEO", "AUDIO"):
        t = get_transcriber()
        return t.model_name, t.prompt_version
    if content_type in ("YOUTUBE", "DOCUMENT"):
        return None
    ai = get_content_ai()
    return ai.model_name, ai.prompt_version
