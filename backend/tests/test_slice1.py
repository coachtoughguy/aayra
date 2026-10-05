"""Vertical Slice 1 (contract §6): teacher creates a lesson -> AI processing -> Review & Publish ->
fan-out -> Student Home; plus the §14 failure injections that belong to this slice."""

import uuid
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import worker
from app.ai.content_ai import StubContentAI, set_content_ai
from app.config import get_settings
from app.db import execute, fetch_all, fetch_one
from app.devtools.seed_demo import seed_demo
from app.identity import StubIdentityProvider, set_identity_provider
from app.services import distribution
from app.storage import StubStorage, set_storage
from tests.factories import auth, token_for

KEY = "slice1-bootstrap-key"


@pytest.fixture(autouse=True)
def _stubs():
    settings = get_settings()
    previous = settings.bootstrap_key
    settings.bootstrap_key = KEY
    storage = StubStorage()
    set_identity_provider(StubIdentityProvider())
    set_storage(storage)
    set_content_ai(StubContentAI())
    distribution.after_batch_hook = None
    yield storage
    settings.bootstrap_key = previous
    set_identity_provider(None)
    set_storage(None)
    set_content_ai(None)
    distribution.after_batch_hook = None


@pytest.fixture
def storage(_stubs) -> StubStorage:
    return _stubs


@pytest.fixture
async def school(client, db) -> dict:
    return await seed_demo(client, bootstrap_key=KEY, token_for=token_for)


def uid(value) -> uuid.UUID:
    return uuid.UUID(str(value))


async def call(client, method: str, path: str, user, body: dict | None = None, *, key: str | None = None):
    headers = auth(uid(user))
    if method in ("POST", "PATCH", "PUT"):
        headers["Idempotency-Key"] = key or uuid.uuid4().hex
    return await client.request(method, path, json=body, headers=headers)


def ok(r, status: int = 200) -> dict:
    assert r.status_code == status, r.text
    return r.json()["data"]


def err(r, status: int, code: str) -> dict:
    assert r.status_code == status, r.text
    assert r.json()["error"]["code"] == code, r.text
    return r.json()["error"]


def sarah(school) -> str:
    return school["teachers"]["sarah"]["user_id"]


async def upload_pdf(client, storage, user, title: str = "Photosynthesis notes", *, put: bool = True) -> dict:
    item = ok(await call(client, "POST", "/v1/content-items", user, {"content_type": "PDF", "title": title}), 201)
    if put:
        storage.put(item["upload"]["object_key"], 120_000)
        ok(
            await call(
                client,
                "POST",
                f"/v1/content-items/{item['content_item_id']}/complete-upload",
                user,
                {
                    "object_key": item["upload"]["object_key"],
                    "mime_type": "application/pdf",
                    "file_size_bytes": 120_000,
                },
            )
        )
    return item


async def ready_lesson(client, db, storage, school, title: str = "Photosynthesis", user=None) -> dict:
    user = user or sarah(school)
    item = await upload_pdf(client, storage, user, f"{title} notes")
    lesson = ok(
        await call(
            client,
            "POST",
            "/v1/lessons",
            user,
            {"subject_id": school["subjects"]["BIO"], "academic_year_id": school["academic_year_id"], "title": title},
        ),
        201,
    )
    ok(
        await call(
            client,
            "POST",
            f"/v1/lessons/{lesson['lesson_id']}/content",
            user,
            {"content_item_id": item["content_item_id"], "display_order": 1},
        ),
        201,
    )
    await worker.drain(db)
    return ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))


def publish_body(view: dict, school: dict, *, days: float = 3, **over) -> dict:
    return {
        "version": view["version"],
        "reviewed_generation_version": view["generation"]["generation_version"],
        "target": {"type": "SECTION", "section_id": school["section_id"]},
        "due_at": (datetime.now(UTC) + timedelta(days=days)).isoformat(),
        "title": view["title"],
        **over,
    }


async def publish(client, view, school, user=None, **over) -> dict:
    return ok(
        await call(client, "POST", f"/v1/lessons/{view['lesson_id']}/publish", user or sarah(school),
                   publish_body(view, school, **over))
    )  # fmt: skip


def arjun(school) -> str:
    return school["students"][0]["user_id"]


# ================================================================================================
# Golden path
# ================================================================================================
async def test_golden_path_teacher_to_student_home(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    assert view["status"] == "READY"
    assert [c["processing_status"] for c in view["content"]] == ["DONE"]
    gen = view["generation"]
    assert gen["generation_version"] == 1
    assert [c["importance"] for c in gen["concepts"]] == ["CORE", "CORE", "SUPPORTING"]
    assert sorted(q["question_type"] for q in gen["questions"]) == ["FREE_TEXT", "FREE_TEXT", "MCQ", "MCQ", "MCQ"]
    assert all(q["answer_spec"] for q in gen["questions"])  # the teacher reviews answers too

    published = await publish(client, view, school)
    assert published["distribution_status"] == "PENDING"
    assert published["lesson_status"] == "PUBLISHED" and published["assignment_status"] == "PUBLISHED"

    await worker.drain(db)
    a = ok(await call(client, "GET", f"/v1/assignments/{published['assignment_id']}", sarah(school)))
    assert (a["distribution_status"], a["delivered_count"], a["expected_count"]) == ("COMPLETE", 28, 28)

    home = ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))
    assert home["continue_learning"] is None
    [item] = home["whats_next"]
    assert item["lesson_id"] == view["lesson_id"] and item["tier"] == "LATER" and item["source"] == "SCHOOL"
    assert item["subject"]["name"] == "Biology"

    learning = ok(await call(client, "GET", "/v1/students/me/learning", arjun(school)))
    by_code = {s["code"]: s for s in learning["subjects"]}
    assert len(by_code["BIO"]["pending"]) == 1 and by_code["MATH"]["pending"] == []

    progress = ok(await call(client, "GET", f"/v1/lessons/{view['lesson_id']}/progress", sarah(school)))
    [p] = progress["assignments"]
    assert p["counts"] == {"completed": 0, "in_progress": 0, "not_started": 28}
    names = [s["display_name"] for s in p["cohorts"]["not_started"]]
    assert names == sorted(names)  # alphabetical, never a ranking

    async with db.connect() as conn:
        notes = await fetch_one(
            conn, "SELECT count(*) AS n FROM notifications WHERE notification_type = 'ASSIGNMENT_PUBLISHED'"
        )
        events = [
            e["event_type"]
            for e in await fetch_all(conn, "SELECT event_type FROM domain_event_outbox ORDER BY created_at")
        ]
    assert notes["n"] == 28
    for expected in ("ContentUploadCompleted", "LessonContentAttached", "LessonReadyForReview",
                     "AssignmentPublished", "AssignmentDistributed"):  # fmt: skip
        assert expected in events


async def test_every_model_call_is_logged(client, db, storage, school):
    await ready_lesson(client, db, storage, school)
    async with db.connect() as conn:
        calls = await fetch_all(
            conn,
            "SELECT purpose, model_name, prompt_version, input_token_count, status FROM ai_invocations ORDER BY created_at",
        )
        runs = await fetch_all(conn, "SELECT status, output_summary FROM ai_generation_runs")
    assert [c["purpose"] for c in calls] == ["CONTENT_EXTRACTION", "CONCEPT_EXTRACTION", "ASSESSMENT_PREPARATION"]
    assert all(c["model_name"] and c["prompt_version"] and c["input_token_count"] for c in calls)
    assert [r["status"] for r in runs] == ["SUCCEEDED"]
    assert runs[0]["output_summary"]["blueprint_version"] == 1


# ================================================================================================
# §14 failure injections
# ================================================================================================
async def test_failed_upload_leaves_other_files_usable(client, db, storage, school):
    user = sarah(school)
    f1 = await upload_pdf(client, storage, user, "Part 1")
    f2 = await upload_pdf(client, storage, user, "Part 2")
    f3 = await upload_pdf(client, storage, user, "Part 3", put=False)  # upload never finished
    # complete-upload is refused until the object really exists, and only at the issued key
    r = await call(client, "POST", f"/v1/content-items/{f3['content_item_id']}/complete-upload", user,
                   {"object_key": f3["upload"]["object_key"], "mime_type": "application/pdf", "file_size_bytes": 10})  # fmt: skip
    err(r, 409, "UPLOAD_NOT_FOUND")
    r = await call(client, "POST", f"/v1/content-items/{f3['content_item_id']}/complete-upload", user,
                   {"object_key": "schools/x/elsewhere", "mime_type": "application/pdf", "file_size_bytes": 10})  # fmt: skip
    err(r, 422, "VALIDATION_ERROR")

    lesson = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": "Cells"}), 201)  # fmt: skip
    path = f"/v1/lessons/{lesson['lesson_id']}/content"
    ok(await call(client, "POST", path, user, {"content_item_id": f1["content_item_id"], "display_order": 1}), 201)
    ok(await call(client, "POST", path, user, {"content_item_id": f2["content_item_id"], "display_order": 2}), 201)
    err(await call(client, "POST", path, user, {"content_item_id": f3["content_item_id"], "display_order": 3}),
        409, "CONTENT_NOT_UPLOADED")  # fmt: skip
    await worker.drain(db)
    view = ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))
    assert view["status"] == "READY" and len(view["content"]) == 2


async def test_unreadable_file_fails_the_lesson_not_the_system(client, db, storage, school):
    user = sarah(school)
    bad = await upload_pdf(client, storage, user, "[fail] blurry scan")
    lesson = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": "Cells"}), 201)  # fmt: skip
    ok(await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/content", user,
                  {"content_item_id": bad["content_item_id"], "display_order": 1}), 201)  # fmt: skip
    await worker.drain(db)
    view = ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))
    assert view["status"] == "PROCESSING_FAILED"
    assert view["content"][0]["processing_status"] == "FAILED"
    assert view["generation"] is None
    r = await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/publish", user,
                   {**publish_body({**view, "generation": {"generation_version": 1}}, school)})  # fmt: skip
    err(r, 409, "LESSON_NOT_READY")
    async with db.connect() as conn:
        failed = await fetch_one(
            conn, "SELECT payload FROM domain_event_outbox WHERE event_type = 'ContentProcessingFailed'"
        )
    assert failed["payload"]["data"]["error_code"] == "UNREADABLE_SOURCE"


async def test_double_publish(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    body = publish_body(view, school)
    path = f"/v1/lessons/{view['lesson_id']}/publish"
    first = await call(client, "POST", path, sarah(school), body, key="tap-1")
    replay = await call(client, "POST", path, sarah(school), body, key="tap-1")  # same key: replayed
    second = await call(client, "POST", path, sarah(school), body, key="tap-2")  # new key: stale version
    assert first.status_code == 200 and replay.json()["data"] == first.json()["data"]
    assert replay.headers["Idempotent-Replayed"] == "true"
    err(second, 409, "VERSION_CONFLICT")
    async with db.connect() as conn:
        n = await fetch_one(conn, "SELECT count(*) AS n FROM assignments")
    assert n["n"] == 1


async def test_stale_generation_cannot_be_published(client, db, storage, school):
    user = sarah(school)
    v1 = await ready_lesson(client, db, storage, school)
    extra = await upload_pdf(client, storage, user, "Extra worksheet")
    ok(await call(client, "POST", f"/v1/lessons/{v1['lesson_id']}/content", user,
                  {"content_item_id": extra["content_item_id"], "display_order": 2}), 201)  # fmt: skip
    mid = ok(await call(client, "GET", f"/v1/lessons/{v1['lesson_id']}", user))
    assert mid["status"] == "PROCESSING" and mid["generation"] is None
    err(await call(client, "POST", f"/v1/lessons/{v1['lesson_id']}/publish", user,
                   publish_body(v1, school, version=mid["version"])), 409, "LESSON_NOT_READY")  # fmt: skip
    await worker.drain(db)
    v2 = ok(await call(client, "GET", f"/v1/lessons/{v1['lesson_id']}", user))
    assert v2["generation"]["generation_version"] == 2
    # Teacher's screen still shows generation 1 but has a fresh lesson version: blocked.
    r = await call(client, "POST", f"/v1/lessons/{v1['lesson_id']}/publish", user,
                   publish_body(v2, school, reviewed_generation_version=1))  # fmt: skip
    e = err(r, 409, "GENERATION_VERSION_STALE")
    assert e["details"] == {"reviewed_generation_version": 1, "current_generation_version": 2}
    ok(await call(client, "POST", f"/v1/lessons/{v1['lesson_id']}/publish", user, publish_body(v2, school)))
    # GET shows only the current generation's questions
    assert {q["question_id"] for q in v2["generation"]["questions"]}.isdisjoint(
        {q["question_id"] for q in v1["generation"]["questions"]}
    )


async def test_fanout_crash_reports_partial_then_recovers_without_duplicates(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    published = await publish(client, view, school)
    crashed = {"done": False}

    def crash_once(batch_index: int) -> None:
        if not crashed["done"]:
            crashed["done"] = True
            raise RuntimeError("worker died after batch 0")

    distribution.after_batch_hook = crash_once
    assert await worker.run_once(db)  # fan-out runs and crashes after the first committed batch
    a = ok(await call(client, "GET", f"/v1/assignments/{published['assignment_id']}", sarah(school)))
    assert a["distribution_status"] == "PARTIAL_FAILURE"
    assert (a["delivered_count"], a["expected_count"]) == (distribution.BATCH_SIZE, 28)
    assert "worker died" in a["last_error"]

    await worker.drain(db)  # retry picks up exactly the missing students
    a = ok(await call(client, "GET", f"/v1/assignments/{published['assignment_id']}", sarah(school)))
    assert (a["distribution_status"], a["delivered_count"]) == ("COMPLETE", 28)
    async with db.connect() as conn:
        dup = await fetch_one(
            conn,
            """SELECT count(*) AS n FROM (SELECT student_id FROM student_assignments
                                       GROUP BY student_id HAVING count(*) > 1) d""",
        )
        notes = await fetch_one(conn, "SELECT count(*) AS n FROM notifications")
        progress = await fetch_one(conn, "SELECT count(*) AS n FROM student_lesson_progress")
    assert dup["n"] == 0 and notes["n"] == 28 and progress["n"] == 28


# ================================================================================================
# Authorization (§3: checked per call, Primary == Co-teacher)
# ================================================================================================
async def test_teacher_authority(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    ravi = school["teachers"]["ravi"]["user_id"]
    priya = school["teachers"]["priya"]["user_id"]
    lesson_path = f"/v1/lessons/{view['lesson_id']}"
    # Ravi teaches Maths, not Biology
    err(await call(client, "POST", "/v1/lessons", ravi, {"subject_id": school["subjects"]["BIO"],
        "academic_year_id": school["academic_year_id"], "title": "x"}), 403, "INSUFFICIENT_ROLE_SCOPE")  # fmt: skip
    err(await call(client, "GET", lesson_path, ravi), 403, "INSUFFICIENT_ROLE_SCOPE")
    # Priya is a Biology co-teacher: same permissions as the primary
    ok(await call(client, "GET", lesson_path, priya))
    # Students can't use teacher endpoints; teachers have no student home
    err(await call(client, "GET", lesson_path, arjun(school)), 403, "INSUFFICIENT_ROLE_SCOPE")
    err(await call(client, "GET", "/v1/students/me/home", sarah(school)), 403, "ENROLLMENT_INACTIVE")


async def test_publish_requires_teaching_the_target_section(client, db, storage, school):
    admin = school["admin_user_id"]
    other = ok(await call(client, "POST", f"/v1/grades/{school['grade_id']}/sections", admin, {"name": "10B"}), 201)
    view = await ready_lesson(client, db, storage, school)
    r = await call(client, "POST", f"/v1/lessons/{view['lesson_id']}/publish", sarah(school),
                   publish_body(view, school, target={"type": "SECTION", "section_id": other["section_id"]}))  # fmt: skip
    err(r, 403, "INSUFFICIENT_ROLE_SCOPE")


async def test_teacher_reassigned_mid_flight_loses_access_but_keeps_attribution(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    published = await publish(client, view, school)
    async with db.begin() as conn:
        await execute(conn, """UPDATE teacher_section_subject_assignments SET status = 'INACTIVE'
                               WHERE teacher_id = :t AND assignment_role = 'PRIMARY_SUBJECT_TEACHER'""",
                      t=uid(school["teachers"]["sarah"]["teacher_id"]))  # fmt: skip
    err(await call(client, "GET", f"/v1/lessons/{view['lesson_id']}/progress", sarah(school)),
        403, "INSUFFICIENT_ROLE_SCOPE")  # fmt: skip
    async with db.connect() as conn:
        row = await fetch_one(conn, "SELECT created_by, approved_by FROM lessons WHERE lesson_id = :l",
                              l=uid(view["lesson_id"]))  # fmt: skip
        a = await fetch_one(conn, "SELECT created_by FROM assignments WHERE assignment_id = :a",
                            a=uid(published["assignment_id"]))  # fmt: skip
    assert str(row["created_by"]) == str(row["approved_by"]) == str(a["created_by"]) == sarah(school)


# ================================================================================================
# Scheduling, reuse, dependencies, Home ordering
# ================================================================================================
async def test_scheduled_assignment_is_invisible_until_release(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    available = datetime.now(UTC) + timedelta(hours=2)
    published = await publish(client, view, school, available_from=available.isoformat())
    assert published["assignment_status"] == "SCHEDULED"
    await worker.drain(db)
    a = ok(await call(client, "GET", f"/v1/assignments/{published['assignment_id']}", sarah(school)))
    assert a["distribution_status"] == "COMPLETE" and a["status"] == "SCHEDULED"
    assert ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))["whats_next"] == []
    async with db.begin() as conn:  # time passes
        assert (await fetch_one(conn, "SELECT count(*) AS n FROM notifications"))["n"] == 0
        await execute(conn, "UPDATE assignments SET available_from = now() - interval '1 second'")
        await execute(
            conn,
            "UPDATE background_jobs SET scheduled_at = now() - interval '1 second' WHERE job_type = 'ASSIGNMENT_RELEASE'",
        )
    await worker.drain(db)
    assert len(ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))["whats_next"]) == 1
    async with db.connect() as conn:
        assert (await fetch_one(conn, "SELECT count(*) AS n FROM notifications"))["n"] == 28
        assert (await fetch_one(conn, "SELECT status FROM assignments"))["status"] == "PUBLISHED"


async def test_reused_content_is_not_reprocessed(client, db, storage, school):
    user = sarah(school)
    first = await ready_lesson(client, db, storage, school, "Photosynthesis")
    item_id = first["content"][0]["content_item_id"]
    second = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": "Plant energy"}), 201)  # fmt: skip
    attached = ok(await call(client, "POST", f"/v1/lessons/{second['lesson_id']}/content", user,
                             {"content_item_id": item_id, "display_order": 1}), 201)  # fmt: skip
    assert attached["status"] == "PROCESSING"
    await worker.drain(db)
    view = ok(await call(client, "GET", f"/v1/lessons/{second['lesson_id']}", user))
    assert view["status"] == "READY" and view["generation"]["generation_version"] == 1
    async with db.connect() as conn:
        jobs = await fetch_one(
            conn, "SELECT count(*) AS n FROM content_processing_jobs WHERE content_item_id = :c", c=uid(item_id)
        )
    assert jobs["n"] == 1


async def test_dependency_cycles_are_rejected(client, db, storage, school):
    user = sarah(school)
    mk = lambda t: call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],  # noqa: E731
                        "academic_year_id": school["academic_year_id"], "title": t})  # fmt: skip
    a, b, c = [ok(await mk(t), 201)["lesson_id"] for t in "ABC"]
    ok(await call(client, "POST", f"/v1/lessons/{b}/dependencies", user, {"depends_on_lesson_id": a}), 201)
    ok(await call(client, "POST", f"/v1/lessons/{c}/dependencies", user, {"depends_on_lesson_id": b}), 201)
    err(
        await call(client, "POST", f"/v1/lessons/{a}/dependencies", user, {"depends_on_lesson_id": c}),
        409,
        "DEPENDENCY_CYCLE",
    )
    err(
        await call(client, "POST", f"/v1/lessons/{a}/dependencies", user, {"depends_on_lesson_id": a}),
        422,
        "VALIDATION_ERROR",
    )


async def test_home_priority_tiers_and_continue_learning(client, db, storage, school):
    tz = ZoneInfo("America/Phoenix")
    now = datetime.now(UTC)
    end_of_today = (now.astimezone(tz) + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if end_of_today - now < timedelta(minutes=5):
        pytest.skip("too close to local midnight for a stable 'due today'")
    plan = {
        "Later": now + timedelta(days=5),
        "Overdue": None,  # set after publish
        "Today": now + (end_of_today - now) / 2,
        "Tomorrow": end_of_today + timedelta(hours=12),
        "Started": now + timedelta(days=6),
    }
    ids = {}
    for title, due in plan.items():
        view = await ready_lesson(client, db, storage, school, title)
        due = due or now + timedelta(days=1)
        ids[title] = (await publish(client, view, school, due_at=due.isoformat()))["assignment_id"]
    await worker.drain(db)
    student = uid(school["students"][0]["student_id"])
    async with db.begin() as conn:
        await execute(
            conn,
            "UPDATE assignments SET due_at = now() - interval '2 hours' WHERE assignment_id = :a",
            a=uid(ids["Overdue"]),
        )
        await execute(
            conn,
            "UPDATE student_assignments SET effective_due_at = now() - interval '2 hours' WHERE assignment_id = :a",
            a=uid(ids["Overdue"]),
        )
        await execute(
            conn,
            """UPDATE student_assignments SET status = 'IN_PROGRESS', started_at = now()
                               WHERE assignment_id = :a AND student_id = :s""",
            a=uid(ids["Started"]),
            s=student,
        )
        await execute(conn, """UPDATE student_lesson_progress SET status = 'IN_PROGRESS', last_activity_at = now()
                               WHERE student_id = :s AND student_assignment_id IN
                                 (SELECT student_assignment_id FROM student_assignments WHERE assignment_id = :a)""",
                      s=student, a=uid(ids["Started"]))  # fmt: skip
    home = ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))
    assert home["continue_learning"]["title"] == "Started"
    assert [(i["title"], i["tier"]) for i in home["whats_next"]] == [
        ("Overdue", "OVERDUE"),
        ("Today", "DUE_TODAY"),
        ("Tomorrow", "DUE_TOMORROW"),
        ("Later", "LATER"),
    ]
    assert home["my_learning"]["pending_count"] == 5


# ================================================================================================
# v1.3 additions (doc 21, Oct 5): publish to more sections; retry / remove a failed file
# ================================================================================================
async def _second_section(client, db, school) -> dict:
    """10B with Sarah as Primary Biology teacher and 3 students."""
    admin = school["admin_user_id"]
    sec = ok(await call(client, "POST", f"/v1/grades/{school['grade_id']}/sections", admin, {"name": "10B"}), 201)
    ok(await call(client, "POST", "/v1/teacher-assignments", admin, {
        "teacher_id": school["teachers"]["sarah"]["teacher_id"], "section_id": sec["section_id"],
        "subject_id": school["subjects"]["BIO"], "assignment_role": "PRIMARY_SUBJECT_TEACHER"}), 201)  # fmt: skip
    for i in range(3):
        ok(await call(client, "POST", "/v1/students", admin, {
            "section_id": sec["section_id"], "display_name": f"B Student {i}", "admission_id": f"10B-{i:03d}",
            "date_of_birth": "2011-01-01", "enrollment_date": "2026-06-01"}), 201)  # fmt: skip
    return sec


async def test_published_lesson_can_be_published_to_another_section(client, db, storage, school):
    sec_b = await _second_section(client, db, school)
    view = await ready_lesson(client, db, storage, school)
    first = await publish(client, view, school)
    after = ok(await call(client, "GET", f"/v1/lessons/{view['lesson_id']}", sarah(school)))
    assert after["status"] == "PUBLISHED"
    # same approved generation, fresh lesson version, new section
    second = await publish(client, after, school, target={"type": "SECTION", "section_id": sec_b["section_id"]})
    assert second["assignment_id"] != first["assignment_id"] and second["lesson_status"] == "PUBLISHED"
    # publishing to 10A again is refused (it already has this lesson)
    latest = ok(await call(client, "GET", f"/v1/lessons/{view['lesson_id']}", sarah(school)))
    r = await call(
        client, "POST", f"/v1/lessons/{view['lesson_id']}/publish", sarah(school), publish_body(latest, school)
    )
    assert err(r, 409, "ALREADY_PUBLISHED_TO_TARGET")["details"]["assignment_id"] == first["assignment_id"]
    await worker.drain(db)
    a = ok(await call(client, "GET", f"/v1/assignments/{second['assignment_id']}", sarah(school)))
    assert (a["distribution_status"], a["delivered_count"]) == ("COMPLETE", 3)
    progress = ok(await call(client, "GET", f"/v1/lessons/{view['lesson_id']}/progress", sarah(school)))
    assert [p["counts"]["not_started"] for p in progress["assignments"]] == [28, 3]
    async with db.connect() as conn:
        lesson = await fetch_one(
            conn, "SELECT approved_at, published_at FROM lessons WHERE lesson_id = :l", l=uid(view["lesson_id"])
        )
    assert lesson["approved_at"] == lesson["published_at"]  # the original approval is kept


async def test_failed_file_can_be_retried(client, db, storage, school):
    user = sarah(school)
    flaky = await upload_pdf(client, storage, user, "[fail-once] slides")
    lesson = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": "Respiration"}), 201)  # fmt: skip
    ok(await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/content", user,
                  {"content_item_id": flaky["content_item_id"], "display_order": 1}), 201)  # fmt: skip
    await worker.drain(db)
    view = ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))
    assert view["status"] == "PROCESSING_FAILED" and view["content"][0]["processing_status"] == "FAILED"

    retry_path = f"/v1/content-items/{flaky['content_item_id']}/retry-processing"
    r = ok(await call(client, "POST", retry_path, user))
    assert r["processing_status"] == "QUEUED" and r["lessons_reprocessing"] == [lesson["lesson_id"]]
    err(await call(client, "POST", retry_path, user), 409, "RETRY_NOT_ALLOWED")  # already queued
    await worker.drain(db)
    view = ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))
    assert view["status"] == "READY" and view["generation"]["generation_version"] == 1
    err(await call(client, "POST", retry_path, user), 409, "RETRY_NOT_ALLOWED")  # nothing to retry
    # only teachers of the same school may retry
    err(await call(client, "POST", retry_path, arjun(school)), 403, "INSUFFICIENT_ROLE_SCOPE")


async def test_permanently_bad_file_can_be_removed(client, db, storage, school):
    user = sarah(school)
    good = await upload_pdf(client, storage, user, "Good notes")
    bad = await upload_pdf(client, storage, user, "[fail] corrupt scan")
    lesson = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                "academic_year_id": school["academic_year_id"], "title": "Genetics"}), 201)  # fmt: skip
    path = f"/v1/lessons/{lesson['lesson_id']}/content"
    ok(await call(client, "POST", path, user, {"content_item_id": good["content_item_id"], "display_order": 1}), 201)
    ok(await call(client, "POST", path, user, {"content_item_id": bad["content_item_id"], "display_order": 2}), 201)
    await worker.drain(db)
    assert ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))["status"] == "PROCESSING_FAILED"
    # retrying a truly unreadable file fails again
    ok(await call(client, "POST", f"/v1/content-items/{bad['content_item_id']}/retry-processing", user))
    await worker.drain(db)
    assert ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))["status"] == "PROCESSING_FAILED"
    # removing it lets the lesson finish with the good file
    r = ok(
        await client.delete(f"{path}/{bad['content_item_id']}", headers=auth(uid(user), **{"Idempotency-Key": "rm-1"}))
    )
    assert r["status"] == "PROCESSING"
    await worker.drain(db)
    view = ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))
    assert view["status"] == "READY" and [c["title"] for c in view["content"]] == ["Good notes"]
    # removing the last file returns the lesson to DRAFT; published content can't be removed
    published = await publish(client, view, school)
    assert published["lesson_status"] == "PUBLISHED"
    r = await client.delete(f"{path}/{good['content_item_id']}", headers=auth(uid(user), **{"Idempotency-Key": "rm-2"}))
    err(r, 409, "LESSON_CONTENT_LOCKED")


async def test_attaching_a_previously_failed_file_marks_lesson_failed(client, db, storage, school):
    user = sarah(school)
    bad = await upload_pdf(client, storage, user, "[fail] scan")
    for title in ("First", "Second"):
        lesson = ok(await call(client, "POST", "/v1/lessons", user, {"subject_id": school["subjects"]["BIO"],
                    "academic_year_id": school["academic_year_id"], "title": title}), 201)  # fmt: skip
        ok(await call(client, "POST", f"/v1/lessons/{lesson['lesson_id']}/content", user,
                      {"content_item_id": bad["content_item_id"], "display_order": 1}), 201)  # fmt: skip
        await worker.drain(db)
        assert (
            ok(await call(client, "GET", f"/v1/lessons/{lesson['lesson_id']}", user))["status"] == "PROCESSING_FAILED"
        )
