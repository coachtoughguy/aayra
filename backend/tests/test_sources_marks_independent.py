"""Video/audio transcription, YouTube transcripts, Word documents, marks on submissions, editing
family assignments before they are assigned, and independent adult self-learners."""

import io
import shutil
import subprocess
import time
import uuid
import zipfile
from datetime import UTC, date, datetime, timedelta

import httpx
import jwt
import pytest

from app import worker
from app.ai import transcription
from app.ai.content_ai import ContentExtractionError
from app.ai.sources import docx_to_chunks, parse_transcript, youtube_metadata, youtube_video_id
from app.ai.transcription import OpenAITranscriber, TranscriptionUnavailable, prepare_audio
from app.config import get_settings
from app.db import fetch_all, fetch_one
from tests.factories import auth
from tests.test_extensions import completed_school_lesson, family_upload, link_parent
from tests.test_slice1 import (  # noqa: F401  (fixtures)
    _stubs,
    arjun,
    call,
    err,
    ok,
    sarah,
    school,
    storage,
    uid,
)
from tests.test_slice2 import _grading, learn, run_check  # noqa: F401

HAS_FFMPEG = shutil.which("ffmpeg") is not None


@pytest.fixture(autouse=True)
def _transcriber():
    transcription.set_transcriber(None)
    yield
    transcription.set_transcriber(None)


# ------------------------------------------------------------------------------------------------
# helpers
# ------------------------------------------------------------------------------------------------
async def upload(client, storage, user, ctype, title, mime, data: bytes, purpose: str = "SCHOOL") -> dict:
    item = ok(await call(client, "POST", "/v1/content-items", user,
                         {"content_type": ctype, "title": title, "purpose": purpose}), 201)  # fmt: skip
    storage.put(item["upload"]["object_key"], len(data), data)
    ok(await call(client, "POST", f"/v1/content-items/{item['content_item_id']}/complete-upload", user,
                  {"object_key": item["upload"]["object_key"], "mime_type": mime, "file_size_bytes": len(data)}))  # fmt: skip
    return item


async def lesson_with(client, db, school, item_id, title="Media lesson", drain=True) -> dict:
    user = sarah(school)
    lesson = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": title}), 201)  # fmt: skip
    ok(await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/content", user,
                  {"content_item_id": item_id, "display_order": 1}), 201)  # fmt: skip
    if drain:
        await worker.drain(db)
    return ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))


async def chunks_of(db, item_id) -> list[dict]:
    async with db.connect() as conn:
        return await fetch_all(
            conn,
            """SELECT chunk_type, content_text, page_number, start_seconds, end_seconds, extraction_method
                 FROM content_evidence_chunks WHERE content_item_id = :c ORDER BY created_at, chunk_id""",
            c=uid(item_id),
        )


async def job_error(db, item_id) -> str | None:
    async with db.connect() as conn:
        row = await fetch_one(conn, """SELECT error_code FROM content_processing_jobs WHERE content_item_id = :c
                                        ORDER BY created_at DESC LIMIT 1""", c=uid(item_id))  # fmt: skip
    return row["error_code"]


def make_docx(body_xml: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr(
            "word/document.xml",
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            f"<w:body>{body_xml}</w:body></w:document>",
        )
    return buf.getvalue()


def para(text: str, style: str | None = None, page_break: bool = False) -> str:
    ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
    br = '<w:r><w:br w:type="page"/></w:r>' if page_break else ""
    return f"<w:p>{ppr}{br}<w:r><w:t>{text}</w:t></w:r></w:p>"


DOCX = make_docx(
    para("Photosynthesis", "Title")
    + para("Plants turn light into chemical energy. Chlorophyll absorbs light in the leaves.")
    + para("The light reactions", "Heading1", page_break=True)
    + para("Water is split and oxygen is released.")
    + "<w:tbl><w:tr><w:tc>" + para("Input") + "</w:tc><w:tc>" + para("Output") + "</w:tc></w:tr>"
    + "<w:tr><w:tc>" + para("Carbon dioxide") + "</w:tc><w:tc>" + para("Glucose") + "</w:tc></w:tr></w:tbl>"
)  # fmt: skip


# ================================================================================================
# Video / audio
# ================================================================================================
async def test_video_is_transcribed_into_timestamped_evidence(client, db, storage, school):
    item = await upload(client, storage, sarah(school), "VIDEO", "Leaf walk", "video/mp4", b"\x00fake-mp4")
    view = await lesson_with(client, db, school, item["content_item_id"])
    assert view["status"] == "READY"
    chunks = await chunks_of(db, item["content_item_id"])
    assert {c["chunk_type"] for c in chunks} == {"TRANSCRIPT"}
    assert chunks[0]["start_seconds"] == 0 and chunks[-1]["end_seconds"] == 150
    assert chunks[0]["extraction_method"] == "stub-transcriber"
    async with db.connect() as conn:
        meta = await fetch_one(conn, "SELECT duration_seconds, language_code FROM content_items WHERE content_item_id = :c",
                               c=uid(item["content_item_id"]))  # fmt: skip
        logged = await fetch_one(conn, """SELECT model_name, status FROM ai_invocations
                                           WHERE entity_id = :c AND purpose = 'CONTENT_EXTRACTION'""",
                                 c=uid(item["content_item_id"]))  # fmt: skip
    assert (meta["duration_seconds"], meta["language_code"]) == (150, "en")
    assert (logged["model_name"], logged["status"]) == ("stub-transcriber", "SUCCEEDED")


async def test_media_without_speech_fails_the_file_visibly(client, db, storage, school):
    item = await upload(client, storage, sarah(school), "AUDIO", "[fail] silence", "audio/mpeg", b"ID3")
    view = await lesson_with(client, db, school, item["content_item_id"])
    assert view["status"] == "PROCESSING_FAILED"
    assert await job_error(db, item["content_item_id"]) == "NO_SPEECH_FOUND"


async def test_transcription_outage_retries_then_fails_lesson_not_stuck(client, db, storage, school):
    item = await upload(client, storage, sarah(school), "AUDIO", "[outage] podcast", "audio/mpeg", b"ID3")
    view = await lesson_with(client, db, school, item["content_item_id"])
    assert view["status"] == "PROCESSING_FAILED"  # before: stayed PROCESSING forever
    async with db.connect() as conn:
        job = await fetch_one(conn, "SELECT status, attempt_count, error_code FROM content_processing_jobs WHERE content_item_id = :c",
                              c=uid(item["content_item_id"]))  # fmt: skip
    assert (job["status"], job["attempt_count"], job["error_code"]) == ("FAILED", 3, "PROVIDER_UNAVAILABLE")


@pytest.fixture
def openai_settings():
    s = get_settings()
    prev = (s.openai_api_key, s.transcription_model)
    s.openai_api_key, s.transcription_model = "sk-test", "whisper-1"
    yield s
    s.openai_api_key, s.transcription_model = prev


def tone(seconds: float = 2.0) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-nostdin", "-loglevel", "error", "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}",
         "-f", "mp3", "-"], capture_output=True, check=True,
    ).stdout  # fmt: skip


async def test_openai_transcriber_requires_configuration():
    with pytest.raises(RuntimeError, match="AAYRA_TRANSCRIPTION_MODEL"):
        OpenAITranscriber()


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
async def test_openai_whisper_request_and_segments(openai_settings):
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"text": "Plants make food.", "language": "english", "duration": 2.0,
                                         "segments": [{"start": 0.0, "end": 1.2, "text": " Plants make food. "},
                                                      {"start": 1.2, "end": 2.0, "text": "  "}]})  # fmt: skip

    t = OpenAITranscriber(transport=httpx.MockTransport(handler))
    result = await t.transcribe(tone(), mime_type="audio/mpeg", label="x")
    assert [(s.start, s.end, s.text) for s in result.segments] == [(0.0, 1.2, "Plants make food.")]
    assert result.language == "english" and 1.5 < result.duration_seconds < 2.5
    req = seen[0]
    assert req.url.path == "/v1/audio/transcriptions" and req.headers["authorization"] == "Bearer sk-test"
    body = req.content.decode(errors="replace")
    assert 'name="model"\r\n\r\nwhisper-1' in body and "verbose_json" in body and 'filename="piece0000.mp3"' in body


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
async def test_long_media_is_split_into_offset_pieces(monkeypatch):
    monkeypatch.setattr(transcription, "PIECE_SECONDS", 1)
    pieces, duration = await prepare_audio(tone(3.0), "audio/mpeg", max_seconds=60)
    assert len(pieces) >= 3 and [p.offset_seconds for p in pieces[:3]] == [0.0, 1.0, 2.0]
    with pytest.raises(ContentExtractionError) as exc:
        await prepare_audio(tone(3.0), "audio/mpeg", max_seconds=2)
    assert exc.value.code == "MEDIA_TOO_LONG"
    with pytest.raises(ContentExtractionError) as exc:
        await prepare_audio(b"not media at all", "video/mp4", max_seconds=60)
    assert exc.value.code == "NO_AUDIO_TRACK"


async def test_without_ffmpeg_only_api_ready_files_are_sent(monkeypatch):
    monkeypatch.setattr(transcription.shutil, "which", lambda _: None)
    pieces, _ = await prepare_audio(b"ID3small", "audio/mpeg", max_seconds=60)
    assert [p.file_name for p in pieces] == ["media.mp3"]
    for data, mime, code in ((b"x" * (26 * 1024 * 1024), "audio/mpeg", "MEDIA_TOO_LARGE"),
                             (b"moov", "video/quicktime", "UNSUPPORTED_MEDIA_FORMAT")):  # fmt: skip
        with pytest.raises(ContentExtractionError) as exc:
            await prepare_audio(data, mime, max_seconds=60)
        assert exc.value.code == code


async def test_openai_text_models_and_errors(openai_settings, monkeypatch):
    monkeypatch.setattr(transcription.shutil, "which", lambda _: None)
    openai_settings.transcription_model = "gpt-4o-transcribe"
    t = OpenAITranscriber(transport=httpx.MockTransport(lambda r: httpx.Response(
        200, json={"text": "Light reactions split water.", "usage": {"input_tokens": 50, "output_tokens": 9}})))  # fmt: skip
    result = await t.transcribe(b"ID3", mime_type="audio/mpeg", label="x")
    assert [s.text for s in result.segments] == ["Light reactions split water."] and result.usage.input_tokens == 50
    for status, exc_type in (
        (429, TranscriptionUnavailable),
        (503, TranscriptionUnavailable),
        (400, ContentExtractionError),
    ):
        t = OpenAITranscriber(transport=httpx.MockTransport(lambda r, s=status: httpx.Response(s, text="nope")))
        with pytest.raises(exc_type):
            await t.transcribe(b"ID3", mime_type="audio/mpeg", label="x")
    t = OpenAITranscriber(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"text": "  "})))
    with pytest.raises(ContentExtractionError, match="No speech"):
        await t.transcribe(b"ID3", mime_type="audio/mpeg", label="x")


# ================================================================================================
# YouTube (transcript provided by the uploader)
# ================================================================================================
VTT = """WEBVTT

00:00:00.000 --> 00:00:04.000
Plants make their own food.

00:00:04.000 --> 00:00:08.500
Plants make their own food.

00:00:08.500 --> 00:00:12.000
<c>Chlorophyll</c> absorbs sunlight in the leaves.
"""


def test_transcript_formats():
    assert [(s.start, s.end, s.text) for s in parse_transcript(VTT)] == [
        (0.0, 4.0, "Plants make their own food."), (8.5, 12.0, "Chlorophyll absorbs sunlight in the leaves.")]  # fmt: skip
    srt = "1\n00:00:01,000 --> 00:00:03,250\nHello class\n\n2\n00:01:00,000 --> 00:01:02,000\nNext idea"
    assert [(s.start, s.end) for s in parse_transcript(srt)] == [(1.0, 3.25), (60.0, 62.0)]
    pasted = "0:00\nIntro to cells\n1:05\nThe nucleus holds DNA\n2:10 Mitochondria make energy"
    assert [(s.start, s.end, s.text) for s in parse_transcript(pasted)] == [
        (0.0, 65.0, "Intro to cells"), (65.0, 130.0, "The nucleus holds DNA"), (130.0, 130.0, "Mitochondria make energy")]  # fmt: skip
    assert [s.text for s in parse_transcript("Just plain words.\nMore words.")] == ["Just plain words.", "More words."]
    assert parse_transcript("   ") == []


def test_youtube_ids():
    for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&t=10", "https://youtu.be/dQw4w9WgXcQ",
                "https://youtube.com/shorts/dQw4w9WgXcQ", "https://m.youtube.com/embed/dQw4w9WgXcQ"):  # fmt: skip
        assert youtube_video_id(url) == "dQw4w9WgXcQ"
    for url in ("https://vimeo.com/123", "https://youtube.com/watch?v=short", "https://evil.com/watch?v=dQw4w9WgXcQ"):
        assert youtube_video_id(url) is None


async def test_youtube_flow(client, db, storage, school):
    user = sarah(school)
    base = {"content_type": "YOUTUBE", "title": "Photosynthesis explained"}
    err(
        await call(client, "POST", "/v1/content-items", user, {**base, "source_url": "https://vimeo.com/1"}),
        422,
        "VALIDATION_ERROR",
    )
    item = ok(
        await call(client, "POST", "/v1/content-items", user, {**base, "source_url": "https://youtu.be/dQw4w9WgXcQ"}),
        201,
    )
    assert item["upload_status"] == "AWAITING_TRANSCRIPT" and item["upload"] is None
    lesson = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": "Video lesson"}), 201)  # fmt: skip
    err(await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/content", user,
                   {"content_item_id": item["content_item_id"], "display_order": 1}), 409, "CONTENT_NOT_UPLOADED")  # fmt: skip
    path = f"/v1/content-items/{item['content_item_id']}/transcript"
    err(await call(client, "POST", path, arjun(school), {"text": VTT}), 403, "INSUFFICIENT_ROLE_SCOPE")
    done = ok(await call(client, "POST", path, user, {"text": VTT}))
    assert done["upload_status"] == "COMPLETED"
    err(await call(client, "POST", path, user, {"text": VTT}), 409, "TRANSCRIPT_ALREADY_PROVIDED")
    ok(await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/content", user,
                  {"content_item_id": item["content_item_id"], "display_order": 1}), 201)  # fmt: skip
    await worker.drain(db)
    assert ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))["status"] == "READY"
    chunks = await chunks_of(db, item["content_item_id"])
    assert chunks[0]["chunk_type"] == "TRANSCRIPT" and chunks[0]["extraction_method"] == "provided-transcript"
    assert "Chlorophyll absorbs sunlight" in chunks[0]["content_text"] and chunks[0]["end_seconds"] == 12
    # transcript can also come with the link, and only YouTube takes one
    inline = ok(await call(client, "POST", "/v1/content-items", user, {**base, "source_url": "https://youtu.be/dQw4w9WgXcQ",
                                                                      "transcript_text": VTT}), 201)  # fmt: skip
    assert inline["upload_status"] == "COMPLETED"
    err(
        await call(client, "POST", "/v1/content-items", user, {"content_type": "PDF", "transcript_text": "x"}),
        422,
        "VALIDATION_ERROR",
    )


async def test_youtube_metadata_from_official_api():
    s = get_settings()
    assert await youtube_metadata("dQw4w9WgXcQ") is None  # no key: skipped, never blocks
    s.youtube_api_key = "yt-key"
    try:
        seen = {}

        def handler(request):
            seen["params"] = dict(request.url.params)
            return httpx.Response(200, json={"items": [{"snippet": {"title": "Photosynthesis"},
                                                        "contentDetails": {"duration": "PT1H2M5S"},
                                                        "status": {"privacyStatus": "public"}}]})  # fmt: skip

        meta = await youtube_metadata("dQw4w9WgXcQ", transport=httpx.MockTransport(handler))
        assert meta == {"title": "Photosynthesis", "duration_seconds": 3725}
        assert seen["params"]["id"] == "dQw4w9WgXcQ" and seen["params"]["key"] == "yt-key"
        with pytest.raises(ContentExtractionError, match="doesn't exist"):
            await youtube_metadata(
                "x" * 11, transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"items": []}))
            )
        assert await youtube_metadata("x" * 11, transport=httpx.MockTransport(lambda r: httpx.Response(403))) is None
    finally:
        s.youtube_api_key = None


# ================================================================================================
# Word documents
# ================================================================================================
def test_docx_parsing():
    chunks = docx_to_chunks(DOCX)
    assert [(c.chunk_type, c.page_number) for c in chunks] == [("DOCUMENT_TEXT", 1), ("DOCUMENT_TEXT", 2), ("TABLE", 2)]
    assert chunks[0].text.startswith("Photosynthesis\nPlants turn light") and chunks[1].text.startswith(
        "The light reactions"
    )
    assert chunks[2].text == "Input | Output\nCarbon dioxide | Glucose"
    for data, code in ((b"\xd0\xcf\x11\xe0legacy", "UNSUPPORTED_DOCUMENT_FORMAT"), (b"PK-not-zip", "UNSUPPORTED_DOCUMENT_FORMAT"),
                       (make_docx(para("")), "NOT_ENOUGH_TEXT")):  # fmt: skip
        with pytest.raises(ContentExtractionError) as exc:
            docx_to_chunks(data)
        assert exc.value.code == code


async def test_word_document_becomes_a_lesson(client, db, storage, school):
    mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    item = await upload(client, storage, sarah(school), "DOCUMENT", "Worksheet", mime, DOCX)
    assert (await lesson_with(client, db, school, item["content_item_id"]))["status"] == "READY"
    assert {c["extraction_method"] for c in await chunks_of(db, item["content_item_id"])} == {"docx-parser"}
    old = await upload(
        client, storage, sarah(school), "DOCUMENT", "Old file", "application/msword", b"\xd0\xcf\x11\xe0x"
    )
    assert (await lesson_with(client, db, school, old["content_item_id"], "Old"))["status"] == "PROCESSING_FAILED"
    assert await job_error(db, old["content_item_id"]) == "UNSUPPORTED_DOCUMENT_FORMAT"


# ================================================================================================
# Marks
# ================================================================================================
async def _submit(client, school, sa):
    return ok(
        await call(
            client, "POST", f"/v1/student-assignments/{sa}/submissions", arjun(school), {"student_note": "Report"}
        ),
        201,
    )


async def test_marks_are_optional_and_released_by_the_teacher(client, db, storage, school):
    from tests.test_slice1 import publish, ready_lesson

    view = await ready_lesson(client, db, storage, school, "Marked")
    r = await call(client, "POST", f"/v1/lessons/{view['lesson_id']}/publish", sarah(school), {
        "version": view["version"], "reviewed_generation_version": view["generation"]["generation_version"],
        "target": {"type": "SECTION", "section_id": school["section_id"]}, "title": "x", "max_marks": 20,
        "due_at": (datetime.now(UTC) + timedelta(days=2)).isoformat()})  # fmt: skip
    err(r, 422, "VALIDATION_ERROR")  # LEARNING_COMPLETION can't be marked
    await publish(
        client, view, school, completion_mode="SUBMISSION_REQUIRED", max_marks=20, marks_visibility="STUDENT_VISIBLE"
    )
    await worker.drain(db)
    home = ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))
    sa = next(i["student_assignment_id"] for i in home["whats_next"] if i.get("lesson_id") == view["lesson_id"])
    sub = await _submit(client, school, sa)
    sid = sub["submission_id"]
    mine = f"/v1/student-assignments/{sa}/submissions"
    assert ok(await call(client, "GET", mine, arjun(school)))["submissions"][0]["marks"] is None

    err(
        await call(client, "PUT", f"/v1/submissions/{sid}/marks", sarah(school), {"marks_awarded": 25}),
        422,
        "VALIDATION_ERROR",
    )
    err(await call(client, "POST", f"/v1/submissions/{sid}/marks/release", sarah(school)), 409, "NOT_MARKED")
    other = school["teachers"]["ravi"]["user_id"]
    r = await call(client, "PUT", f"/v1/submissions/{sid}/marks", other, {"marks_awarded": 1})
    assert r.status_code in (403, 404)
    marked = ok(await call(client, "PUT", f"/v1/submissions/{sid}/marks", sarah(school), {"marks_awarded": 17.5}))
    assert (marked["marks_awarded"], marked["out_of"], marked["released_at"]) == (17.5, 20.0, None)
    queue = ok(await call(client, "GET", "/v1/teacher/review-items", sarah(school)))
    assert queue["count"] == 0  # marking = reviewed
    assert ok(await call(client, "GET", mine, arjun(school)))["submissions"][0]["marks"] is None  # not released yet

    a_id = await _assignment_of(db, sa)
    teacher_list = ok(await call(client, "GET", f"/v1/assignments/{a_id}/submissions", sarah(school)))["submissions"]
    assert teacher_list[0]["marks"]["awarded"] == 17.5 and teacher_list[0]["marks"]["visibility"] == "STUDENT_VISIBLE"
    released = ok(await call(client, "POST", f"/v1/assignments/{a_id}/marks/release", sarah(school)))
    assert (released["released"], released["not_marked_yet"]) == (1, 0)
    assert ok(await call(client, "GET", mine, arjun(school)))["submissions"][0]["marks"] == {
        "awarded": 17.5,
        "out_of": 20.0,
    }
    err(
        await call(client, "POST", f"/v1/submissions/{sid}/marks/release", sarah(school)), 409, "MARKS_ALREADY_RELEASED"
    )
    ok(await call(client, "PUT", f"/v1/submissions/{sid}/marks", sarah(school), {"marks_awarded": 18}))  # correction
    assert ok(await call(client, "GET", mine, arjun(school)))["submissions"][0]["marks"]["awarded"] == 18.0
    async with db.connect() as conn:
        notes = await fetch_all(
            conn, "SELECT title FROM notifications WHERE notification_type = 'MARKS_RELEASED' ORDER BY created_at"
        )
        audits = await fetch_one(conn, "SELECT count(*) AS n FROM audit_events WHERE action = 'SUBMISSION_MARKED'")
        mastery = await fetch_one(conn, "SELECT count(*) AS n FROM student_concept_mastery")
    assert [n["title"] for n in notes] == ["Marks: Marked", "Updated mark: Marked"]
    assert audits["n"] == 2 and mastery["n"] == 0  # marks never create mastery


async def _assignment_of(db, sa) -> str:
    async with db.connect() as conn:
        row = await fetch_one(
            conn, "SELECT assignment_id FROM student_assignments WHERE student_assignment_id = :s", s=uid(sa)
        )
    return str(row["assignment_id"])


async def test_teacher_only_marks_never_reach_the_student(client, db, storage, school):
    _, sa = await completed_school_lesson(client, db, storage, school, completion_mode="BOTH", max_marks=10)
    sid = (await _submit(client, school, sa))["submission_id"]
    ok(await call(client, "PUT", f"/v1/submissions/{sid}/marks", sarah(school), {"marks_awarded": 7}))
    err(await call(client, "POST", f"/v1/submissions/{sid}/marks/release", sarah(school)), 409, "MARKS_TEACHER_ONLY")
    assert (
        ok(await call(client, "GET", f"/v1/student-assignments/{sa}/submissions", arjun(school)))["submissions"][0][
            "marks"
        ]
        is None
    )
    _, sa2 = await completed_school_lesson(client, db, storage, school, completion_mode="SUBMISSION_REQUIRED")
    sid2 = (await _submit(client, school, sa2))["submission_id"]
    err(
        await call(client, "PUT", f"/v1/submissions/{sid2}/marks", sarah(school), {"marks_awarded": 1}),
        409,
        "MARKS_NOT_ENABLED",
    )


# ================================================================================================
# Family assignments: change files until assigned
# ================================================================================================
async def test_parent_can_change_files_until_assigned(client, db, storage, school):
    parent = await link_parent(client, school)
    arjun_id = school["students"][0]["student_id"]
    a = await family_upload(client, storage, parent, "PARENT", arjun_id, title="Leaf notes")
    b = await family_upload(client, storage, parent, "PARENT", arjun_id, title="Root notes")
    created = ok(await call(client, "POST", "/v1/parent-assignments", parent, {
        "student_id": arjun_id, "subject_id": school["subjects"]["BIO"], "title": "Plants",
        "content_item_ids": [a], "review_mode": "ONE_TIME"}), 201)  # fmt: skip
    path = f"/v1/parent-assignments/{created['assignment_id']}"
    err(
        await call(client, "POST", f"{path}/content", parent, {"content_item_id": b, "version": 99}),
        409,
        "VERSION_CONFLICT",
    )
    added = ok(
        await call(client, "POST", f"{path}/content", parent, {"content_item_id": b, "version": created["version"]}),
        201,
    )
    assert [c["title"] for c in added["content"]] == ["Leaf notes", "Root notes"] and added["status"] == "PREPARING"
    err(await call(client, "POST", f"{path}/content", parent, {"content_item_id": b, "version": added["version"]}),
        409, "CONTENT_ALREADY_ATTACHED")  # fmt: skip
    teacher_item = (await upload(client, storage, sarah(school), "PDF", "Teacher", "application/pdf", b"%PDF"))[
        "content_item_id"
    ]
    err(await call(client, "POST", f"{path}/content", parent, {"content_item_id": teacher_item, "version": added["version"]}),
        404, "NOT_FOUND")  # fmt: skip

    def delete(item, version, key):
        return client.delete(
            f"{path}/content/{item}?version={version}", headers=auth(uid(parent), **{"Idempotency-Key": key})
        )

    removed = ok(await delete(a, added["version"], "rm-a"))
    assert [c["title"] for c in removed["content"]] == ["Root notes"]
    err(await delete(b, removed["version"], "rm-b"), 409, "LAST_FILE")
    await worker.drain(db)
    ready = ok(await call(client, "GET", path, parent))
    assert ready["status"] == "READY_TO_ASSIGN"
    ok(await call(client, "POST", f"{path}/assign", parent, {"version": ready["version"]}))
    after = ok(await call(client, "GET", path, parent))
    err(
        await call(client, "POST", f"{path}/content", parent, {"content_item_id": a, "version": after["version"]}),
        409,
        "CONTENT_LOCKED",
    )


# ================================================================================================
# Independent adult self-learners
# ================================================================================================
def adult_dob(years: int = 25) -> str:
    t = date.today()
    return date(t.year - years, 1, 1).isoformat()


def signup_body(**over) -> dict:
    return {"display_name": "Maya Rao", "date_of_birth": adult_dob(), "confirms_18_or_older": True, "country_code": "US",
            "timezone": "America/Phoenix", "subjects": ["Spanish", "Spanish ", "Statistics"], **over}  # fmt: skip


async def test_independent_signup_rules(client, db, school):
    newbie = uuid.uuid4()
    t = date.today()
    seventeen = date(t.year - 17, t.month, min(t.day, 28)).isoformat()
    e = err(
        await call(client, "POST", "/v1/independent-learners", newbie, signup_body(date_of_birth=seventeen)),
        403,
        "AGE_RESTRICTED",
    )
    assert e["details"]["route"] == "SCHOOL_AFFILIATION"
    err(
        await call(client, "POST", "/v1/independent-learners", newbie, signup_body(confirms_18_or_older=False)),
        422,
        "VALIDATION_ERROR",
    )
    err(
        await call(client, "POST", "/v1/independent-learners", newbie, signup_body(timezone="Mars/Base")),
        422,
        "VALIDATION_ERROR",
    )
    created = ok(await call(client, "POST", "/v1/independent-learners", newbie, signup_body()), 201)
    assert sorted(s["name"] for s in created["subjects"]) == ["Spanish", "Statistics"]
    err(await call(client, "POST", "/v1/independent-learners", newbie, signup_body()), 409, "ALREADY_A_STUDENT")
    # a school student is routed to self-learning inside their school
    err(await call(client, "POST", "/v1/independent-learners", arjun(school), signup_body()), 409, "ALREADY_A_STUDENT")
    err(await call(client, "GET", "/v1/independent-learners/me", arjun(school)), 409, "NOT_INDEPENDENT")
    # one person, one account: a teacher can also be an adult self-learner
    ok(await call(client, "POST", "/v1/independent-learners", sarah(school), signup_body(subjects=[])), 201)
    # an email already used by someone else is refused
    async with db.connect() as conn:
        sarah_email = (await fetch_one(conn, "SELECT email FROM users WHERE user_id = :u", u=uid(sarah(school))))[
            "email"
        ]
    now = int(time.time())
    token = jwt.encode({"sub": str(uuid.uuid4()), "aud": "authenticated", "iat": now, "exp": now + 600,
                        "email": sarah_email}, get_settings().jwt_secret, algorithm="HS256")  # fmt: skip
    r = await client.post("/v1/independent-learners", json=signup_body(),
                          headers={"Authorization": f"Bearer {token}", "Idempotency-Key": "e1"})  # fmt: skip
    err(r, 409, "EMAIL_IN_USE")
    async with db.connect() as conn:
        user = await fetch_one(conn, "SELECT age_gate_status, account_status FROM users WHERE user_id = :u", u=newbie)
        space = await fetch_one(
            conn, "SELECT school_kind, owner_user_id, market_region FROM schools WHERE owner_user_id = :u", u=newbie
        )
        audit_row = await fetch_one(
            conn,
            "SELECT after_data FROM audit_events WHERE action = 'INDEPENDENT_LEARNER_SIGNED_UP' AND actor_user_id = :u",
            u=newbie,
        )
    assert (user["age_gate_status"], user["account_status"]) == ("self_declared_18_plus", "ACTIVE")
    assert (space["school_kind"], space["market_region"]) == ("INDEPENDENT", "US")
    assert audit_row["after_data"]["declared_age"] == 25


async def test_independent_learner_learns_end_to_end(client, db, storage, school):
    maya = uuid.uuid4()
    ok(await call(client, "POST", "/v1/independent-learners", maya, signup_body(subjects=["Biology"])), 201)
    added = ok(await call(client, "POST", "/v1/independent-learners/me/subjects", maya, {"name": "Chemistry"}), 201)
    err(
        await call(client, "POST", "/v1/independent-learners/me/subjects", maya, {"name": "chemistry"}),
        409,
        "SUBJECT_EXISTS",
    )
    subjects = ok(await call(client, "GET", "/v1/students/me/subjects", maya))["subjects"]
    assert [s["name"] for s in subjects] == ["Biology", "Chemistry"]
    bio = next(s["subject_id"] for s in subjects if s["name"] == "Biology")

    item = await upload(client, storage, maya, "DOCUMENT", "My notes",
                        "application/vnd.openxmlformats-officedocument.wordprocessingml.document", DOCX, purpose="SELF")  # fmt: skip
    created = ok(await call(client, "POST", "/v1/self-assignments", maya, {"subject_id": str(bio), "title": "Photosynthesis",
                 "content_item_ids": [item["content_item_id"]], "review_mode": "SPACED"}), 201)  # fmt: skip
    await worker.drain(db)
    path = f"/v1/self-assignments/{created['assignment_id']}"
    ok(
        await call(
            client, "POST", f"{path}/assign", maya, {"version": ok(await call(client, "GET", path, maya))["version"]}
        )
    )
    await worker.drain(db)
    home = ok(await call(client, "GET", "/v1/students/me/home", maya))
    [entry] = [i for i in home["whats_next"] if i["kind"] == "LEARNING"]
    assert entry["source"] == "SELF"
    await learn(client, school, created["lesson_id"], entry["student_assignment_id"], user=maya)
    await run_check(client, db, school, created["lesson_id"], entry["student_assignment_id"], user=maya)
    await worker.drain(db)
    async with db.connect() as conn:
        scopes = await fetch_all(conn, "SELECT DISTINCT mastery_scope FROM student_concept_mastery")
        reviews = await fetch_one(
            conn, "SELECT count(*) AS n FROM review_schedules WHERE lesson_id = :l", l=uid(created["lesson_id"])
        )
    assert [s["mastery_scope"] for s in scopes] == ["SELF"] and reviews["n"] > 0
    assert added["code"] == "CHEMISTRY"
    # nobody from the school can see into the personal space
    err(await call(client, "GET", f"/v1/lessons/{created['lesson_id']}", sarah(school)), 404, "NOT_FOUND")
