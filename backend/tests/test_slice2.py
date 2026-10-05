"""Vertical Slice 2 (contract §7): learn -> mastery check -> evidence/mastery -> teacher review ->
fixed-cadence spaced review, plus the Oct 5 source-scoping decision for parent/self work."""

import uuid
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app import worker
from app.ai.grading_ai import StubGradingAI, set_grading_ai
from app.db import execute, fetch_all, fetch_one
from tests.factories import auth
from tests.test_slice1 import (  # noqa: F401  (fixtures re-used from Slice 1)
    _stubs,
    arjun,
    call,
    err,
    ok,
    publish,
    ready_lesson,
    sarah,
    school,
    storage,
    uid,
)

GOOD_TEXT = "Plants turn light energy into chemical energy, for example in leaf cells during the day."


@pytest.fixture(autouse=True)
def _grading():
    set_grading_ai(StubGradingAI())
    yield
    set_grading_ai(None)


def today_phx() -> date:
    return datetime.now(UTC).astimezone(ZoneInfo("America/Phoenix")).date()


async def assigned(client, db, storage, school, title="Photosynthesis") -> tuple[dict, str]:
    view = await ready_lesson(client, db, storage, school, title)
    await publish(client, view, school)
    await worker.drain(db)
    home = ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))
    sa = next(i["student_assignment_id"] for i in home["whats_next"] if i.get("lesson_id") == view["lesson_id"])
    return view, sa


async def learn(client, school, lesson_id, sa, user=None) -> dict:
    user = user or arjun(school)
    ok(await call(client, "POST", f"/v1/student-assignments/{sa}/start", user))
    c1 = ok(
        await call(client, "POST", f"/v1/lessons/{lesson_id}/focus-cycles", user, {"student_assignment_id": sa}), 201
    )
    ok(await call(client, "POST", f"/v1/focus-cycles/{c1['focus_cycle_id']}/end", user,
                  {"end_reason": "BREAK", "client_reported_seconds": 7200}))  # fmt: skip
    ok(await call(client, "POST", f"/v1/lessons/{lesson_id}/focus-cycles", user, {"student_assignment_id": sa}), 201)
    return ok(await call(client, "POST", f"/v1/lessons/{lesson_id}/finish", user, {"student_assignment_id": sa}))


async def answer_key(db, question_id) -> int:
    async with db.connect() as conn:
        q = await fetch_one(conn, "SELECT answer_spec FROM questions WHERE question_id = :q", q=uid(question_id))
    return q["answer_spec"]["correct_index"]


async def answer(client, db, path, user, question, *, correct=True, text=None, key=None):
    if question["question_type"] == "MCQ":
        idx = await answer_key(db, question["question_id"])
        body = {
            "question_id": str(question["question_id"]),
            "answer": {"selected_index": idx if correct else (idx + 1) % 4},
        }
    else:
        body = {
            "question_id": str(question["question_id"]),
            "answer": {"text": text or (GOOD_TEXT if correct else "no idea")},
        }
    return await call(client, "POST", path, user, body, key=key)


async def run_check(client, db, school, lesson_id, sa, *, correct=True, text=None, user=None) -> tuple[str, dict]:
    """Start the mastery check and answer every question; returns (attempt_id, last next-state)."""
    user = user or arjun(school)
    start = ok(
        await call(
            client, "POST", "/v1/assessment-attempts", user, {"lesson_id": lesson_id, "student_assignment_id": sa}
        ),
        201,
    )
    attempt_id, nxt = start["attempt_id"], start["next"]
    path = f"/v1/assessment-attempts/{attempt_id}/responses"
    while nxt["type"] in ("QUESTION", "RETRY"):
        r = await answer(client, db, path, user, nxt["question"], correct=correct, text=text)
        assert r.status_code in (200, 202), r.text
        nxt = r.json()["data"]["next"]
    return attempt_id, nxt


# ================================================================================================
# Golden path
# ================================================================================================
async def test_golden_path_learn_check_mastery_reviews(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    finished = await learn(client, school, view["lesson_id"], sa)
    assert finished["status"] == "LEARNING_COMPLETE"
    assert finished["total_active_seconds"] < 60  # server-derived; the client-reported 7200 is ignored

    attempt_id, last = await run_check(client, db, school, view["lesson_id"], sa)
    assert last["type"] == "AWAITING_EVALUATION"  # two written answers still being graded
    attempt = ok(await call(client, "GET", f"/v1/assessment-attempts/{attempt_id}", arjun(school)))
    assert attempt["status"] == "AWAITING_EVALUATION"
    assert sum(r["evaluation_status"] == "PENDING" for r in attempt["responses"]) == 2

    await worker.drain(db)  # TX2 grading -> evidence -> mastery -> completion
    attempt = ok(await call(client, "GET", f"/v1/assessment-attempts/{attempt_id}", arjun(school)))
    assert attempt["status"] == "COMPLETED"
    result = attempt["result"]
    assert result["stop_reason"] == "SUFFICIENT_EVIDENCE" and result["questions_answered"] == 5
    assert {c["status"] for c in result["concepts"]} == {"MASTERED"}
    assert "internal_confidence" not in str(result)  # students never see internal scores

    async with db.connect() as conn:
        sa_row = await fetch_one(
            conn, "SELECT status, is_late FROM student_assignments WHERE student_assignment_id = :s", s=uid(sa)
        )
        evidence = await fetch_all(
            conn, "SELECT evidence_context, source_scope, mastery_eligible, review_eligible FROM evidence_events"
        )
        items = await fetch_all(conn, "SELECT review_day, scheduled_for, status FROM review_schedule_items")
        graded = await fetch_one(conn, "SELECT count(*) AS n FROM ai_invocations WHERE purpose = 'RESPONSE_EVALUATION'")
        exposures = await fetch_one(
            conn, "SELECT count(*) AS n FROM student_question_exposures WHERE evaluation IS NOT NULL"
        )
    assert (sa_row["status"], sa_row["is_late"]) == ("COMPLETED", False)
    assert len(evidence) == 5 and all(
        e["source_scope"] == "SCHOOL" and e["mastery_eligible"] and e["review_eligible"] for e in evidence
    )
    assert [(i["review_day"], i["scheduled_for"], i["status"]) for i in items] == [
        (2, today_phx() + timedelta(days=2), "UPCOMING")
    ] * 3
    assert graded["n"] == 2 and exposures["n"] == 5

    progress = ok(await call(client, "GET", f"/v1/lessons/{view['lesson_id']}/progress", sarah(school)))
    assert progress["assignments"][0]["counts"] == {"completed": 1, "in_progress": 0, "not_started": 27}
    reviews = ok(await call(client, "GET", "/v1/students/me/reviews", arjun(school)))
    assert reviews["due"] == [] and len(reviews["upcoming"]) == 3


# ================================================================================================
# Answering rules
# ================================================================================================
async def test_wrong_answer_repair_path_hint_retry_explanation_verification(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    await learn(client, school, view["lesson_id"], sa)
    start = ok(await call(client, "POST", "/v1/assessment-attempts", arjun(school),
                          {"lesson_id": view["lesson_id"], "student_assignment_id": sa}), 201)  # fmt: skip
    path = f"/v1/assessment-attempts/{start['attempt_id']}/responses"
    first = start["next"]["question"]
    assert first["question_type"] == "MCQ" and "correct_index" not in str(first)

    r1 = ok(await answer(client, db, path, arjun(school), first, correct=False))
    assert r1["evaluation"] == "INCORRECT" and r1["feedback"]["kind"] == "HINT"
    assert r1["next"]["type"] == "RETRY" and r1["next"]["question"]["question_id"] == first["question_id"]

    r2 = ok(await answer(client, db, path, arjun(school), first, correct=False))
    assert r2["feedback"]["kind"] == "EXPLANATION" and "The answer is" in r2["feedback"]["text"]
    verification = r2["next"]["question"]
    assert r2["next"]["type"] == "QUESTION" and verification["question_id"] != first["question_id"]
    async with db.connect() as conn:
        concepts = await fetch_all(conn, "SELECT question_id, concept_id FROM questions WHERE question_id = ANY(:q)",
                                   q=[uid(first["question_id"]), uid(verification["question_id"])])  # fmt: skip
        helps = await fetch_all(conn, "SELECT help_level FROM evidence_events ORDER BY created_at")
    assert len({c["concept_id"] for c in concepts}) == 1  # related verification on the same concept
    assert [h["help_level"] for h in helps] == ["NONE", "HINT"]

    # answering anything but the current question is rejected
    err(await answer(client, db, path, arjun(school), first), 409, "QUESTION_ALREADY_ANSWERED")
    async with db.connect() as conn:
        other = await fetch_one(conn, "SELECT question_id, question_type FROM questions WHERE lesson_id = :l AND question_id <> ALL(:q) LIMIT 1",
                                l=uid(view["lesson_id"]), q=[uid(first["question_id"]), uid(verification["question_id"])])  # fmt: skip
    err(await answer(client, db, path, arjun(school), other), 409, "QUESTION_NOT_CURRENT")


async def test_duplicate_submit_replays_without_double_evidence(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    await learn(client, school, view["lesson_id"], sa)
    start = ok(await call(client, "POST", "/v1/assessment-attempts", arjun(school),
                          {"lesson_id": view["lesson_id"], "student_assignment_id": sa}), 201)  # fmt: skip
    path = f"/v1/assessment-attempts/{start['attempt_id']}/responses"
    q = start["next"]["question"]
    a = await answer(client, db, path, arjun(school), q, key="double-tap")
    b = await answer(client, db, path, arjun(school), q, key="double-tap")
    assert a.json()["data"] == b.json()["data"] and b.headers["Idempotent-Replayed"] == "true"
    async with db.connect() as conn:
        assert (await fetch_one(conn, "SELECT count(*) AS n FROM evidence_events"))["n"] == 1
        assert (await fetch_one(conn, "SELECT count(*) AS n FROM assessment_responses"))["n"] == 1


async def test_check_requires_finished_lesson_and_one_active_attempt(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    body = {"lesson_id": view["lesson_id"], "student_assignment_id": sa}
    err(await call(client, "POST", "/v1/assessment-attempts", arjun(school), body), 409, "LESSON_NOT_FINISHED")
    await learn(client, school, view["lesson_id"], sa)
    ok(await call(client, "POST", "/v1/assessment-attempts", arjun(school), body), 201)
    err(await call(client, "POST", "/v1/assessment-attempts", arjun(school), body), 409, "ASSESSMENT_ALREADY_ACTIVE")
    # another student can't use Arjun's assignment
    other = school["students"][1]["user_id"]
    err(await call(client, "POST", "/v1/assessment-attempts", other, body), 404, "ASSIGNMENT_NOT_AVAILABLE")


async def test_focus_cycle_rules(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    user, lesson = arjun(school), view["lesson_id"]
    err(
        await call(client, "POST", f"/v1/lessons/{lesson}/focus-cycles", user, {"student_assignment_id": sa}),
        409,
        "ASSIGNMENT_NOT_STARTED",
    )
    ok(await call(client, "POST", f"/v1/student-assignments/{sa}/start", user))
    c = ok(await call(client, "POST", f"/v1/lessons/{lesson}/focus-cycles", user, {"student_assignment_id": sa}), 201)
    err(
        await call(client, "POST", f"/v1/lessons/{lesson}/focus-cycles", user, {"student_assignment_id": sa}),
        409,
        "FOCUS_CYCLE_ALREADY_ACTIVE",
    )
    async with db.begin() as conn:  # forgotten-open session
        await execute(conn, "UPDATE focus_cycles SET started_at = now() - interval '6 hours'")
    ended = ok(
        await call(
            client, "POST", f"/v1/focus-cycles/{c['focus_cycle_id']}/end", user, {"end_reason": "CONTINUE_LATER"}
        )
    )
    assert ended["actual_seconds"] == 4 * 3600  # clamped
    err(
        await call(client, "POST", f"/v1/focus-cycles/{c['focus_cycle_id']}/end", user, {"end_reason": "BREAK"}),
        409,
        "FOCUS_CYCLE_ALREADY_ENDED",
    )


async def test_prerequisite_locks_the_next_lesson(client, db, storage, school):
    a_view, a_sa = await assigned(client, db, storage, school, "Cells")
    b_view = await ready_lesson(client, db, storage, school, "Tissues")
    ok(await call(client, "POST", f"/v1/lessons/{b_view['lesson_id']}/dependencies", sarah(school),
                  {"depends_on_lesson_id": a_view["lesson_id"]}), 201)  # fmt: skip
    b_view = ok(await call(client, "GET", f"/v1/lessons/{b_view['lesson_id']}", sarah(school)))
    await publish(client, b_view, school)
    await worker.drain(db)
    home = ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))
    b_sa = next(i["student_assignment_id"] for i in home["whats_next"] if i.get("lesson_id") == b_view["lesson_id"])
    e = err(
        await call(client, "POST", f"/v1/student-assignments/{b_sa}/start", arjun(school)),
        409,
        "PREREQUISITE_NOT_COMPLETED",
    )
    assert e["details"]["prerequisites"][0]["title"] == "Cells"
    await learn(client, school, a_view["lesson_id"], a_sa)
    await run_check(client, db, school, a_view["lesson_id"], a_sa)
    await worker.drain(db)
    ok(await call(client, "POST", f"/v1/student-assignments/{b_sa}/start", arjun(school)))


# ================================================================================================
# Async grading, teacher review queue (§7.5, §14)
# ================================================================================================
async def test_grading_outage_falls_back_to_teacher(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    await learn(client, school, view["lesson_id"], sa)
    attempt_id, last = await run_check(
        client, db, school, view["lesson_id"], sa, text="[timeout] my answer about light energy"
    )
    await worker.drain(db)  # 3 tries, then MANUAL_REVIEW
    attempt = ok(await call(client, "GET", f"/v1/assessment-attempts/{attempt_id}", arjun(school)))
    assert attempt["status"] == "AWAITING_EVALUATION"
    queue = ok(await call(client, "GET", "/v1/teacher/review-items", sarah(school)))
    assert queue["count"] == 2 and {i["reason_type"] for i in queue["items"]} == {"MANUAL_REVIEW"}
    assert all(i["student_name"] == "Arjun Mehta" for i in queue["items"])
    first, second = queue["items"]
    err(
        await call(client, "POST", f"/v1/teacher-review-items/{first['review_item_id']}/confirm", sarah(school)),
        409,
        "NOTHING_TO_CONFIRM",
    )
    ok(
        await call(
            client,
            "POST",
            f"/v1/teacher-review-items/{first['review_item_id']}/adjust",
            sarah(school),
            {"evaluation": "CORRECT"},
        )
    )
    assert (
        ok(await call(client, "GET", f"/v1/assessment-attempts/{attempt_id}", arjun(school)))["status"]
        == "AWAITING_EVALUATION"
    )
    ok(
        await call(
            client,
            "POST",
            f"/v1/teacher-review-items/{second['review_item_id']}/adjust",
            sarah(school),
            {"evaluation": "PARTIAL"},
        )
    )
    assert (
        ok(await call(client, "GET", f"/v1/assessment-attempts/{attempt_id}", arjun(school)))["status"] == "COMPLETED"
    )
    async with db.connect() as conn:
        jobs = await fetch_all(
            conn, "SELECT status, attempt_count FROM background_jobs WHERE job_type = 'RESPONSE_GRADING'"
        )
    assert all(j["status"] == "FAILED" and j["attempt_count"] == 3 for j in jobs)


async def test_low_confidence_goes_to_teacher_and_adjust_recomputes_mastery(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    await learn(client, school, view["lesson_id"], sa)
    attempt_id, _ = await run_check(
        client, db, school, view["lesson_id"], sa, text="[unsure] light makes food somehow in plants"
    )
    await worker.drain(db)
    assert (
        ok(await call(client, "GET", f"/v1/assessment-attempts/{attempt_id}", arjun(school)))["status"] == "COMPLETED"
    )
    queue = ok(await call(client, "GET", "/v1/teacher/review-items", sarah(school)))
    assert {i["reason_type"] for i in queue["items"]} == {"LOW_CONFIDENCE"}
    item = queue["items"][0]
    assert item["ai_evaluation_snapshot"]["evaluation"] == "PARTIAL"

    async def core_status():
        async with db.connect() as conn:
            return await fetch_all(
                conn,
                """SELECT c.name, m.mastery_status FROM student_concept_mastery m
                                            JOIN concepts c USING (concept_id) ORDER BY c.name""",
            )

    before = await core_status()
    adjusted = ok(
        await call(
            client,
            "POST",
            f"/v1/teacher-review-items/{item['review_item_id']}/adjust",
            sarah(school),
            {"evaluation": "CORRECT"},
        )
    )
    assert adjusted["status"] == "ADJUSTED"
    after = await core_status()
    assert before != after  # the PARTIAL no longer counts; the teacher's CORRECT does
    async with db.connect() as conn:
        evals = await fetch_all(conn, """SELECT re.evaluated_by, re.evaluation, re.is_current FROM response_evaluations re
                                         WHERE re.response_id = (SELECT response_id FROM response_evaluations
                                                                  WHERE response_evaluation_id = (SELECT response_evaluation_id FROM teacher_review_items WHERE review_item_id = :i))
                                         ORDER BY re.evaluated_at""", i=uid(item["review_item_id"]))  # fmt: skip
        ctxs = await fetch_all(
            conn, "SELECT evidence_context FROM evidence_events WHERE evidence_context = 'TEACHER_EVALUATION'"
        )
    assert [(e["evaluated_by"], e["evaluation"], e["is_current"]) for e in evals] == [
        ("AI", "PARTIAL", False),
        ("TEACHER", "CORRECT", True),
    ]
    assert len(ctxs) == 1
    err(
        await call(client, "POST", f"/v1/teacher-review-items/{item['review_item_id']}/confirm", sarah(school)),
        409,
        "REVIEW_ITEM_CLOSED",
    )
    # a teacher moved off the subject can no longer act on the queue
    other = queue["items"][1]
    async with db.begin() as conn:
        await execute(conn, "UPDATE teacher_section_subject_assignments SET status = 'INACTIVE' WHERE teacher_id = :t",
                      t=uid(school["teachers"]["sarah"]["teacher_id"]))  # fmt: skip
    err(
        await call(client, "POST", f"/v1/teacher-review-items/{other['review_item_id']}/confirm", sarah(school)),
        403,
        "INSUFFICIENT_ROLE_SCOPE",
    )


# ================================================================================================
# Spaced review (§7.6): fixed cadence, shared pipeline
# ================================================================================================
async def test_review_cadence_is_fixed_regardless_of_outcome(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    await learn(client, school, view["lesson_id"], sa)
    await run_check(client, db, school, view["lesson_id"], sa)
    await worker.drain(db)
    reviews = ok(await call(client, "GET", "/v1/students/me/reviews", arjun(school)))
    item = reviews["upcoming"][0]
    err(await call(client, "POST", "/v1/review-sessions", arjun(school), {"review_schedule_item_id": item["review_schedule_item_id"]}),
        409, "REVIEW_NOT_DUE")  # fmt: skip

    async with db.begin() as conn:  # two days pass
        await execute(conn, "UPDATE review_schedules SET anchor_date = anchor_date - 2")
        await execute(conn, "UPDATE review_schedule_items SET scheduled_for = scheduled_for - 2")
    home = ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))
    assert home["my_reviews"]["due_today_count"] == 3
    assert [i["tier"] for i in home["whats_next"]] == ["REVIEW_DUE_TODAY"] * 3

    session = ok(
        await call(
            client,
            "POST",
            "/v1/review-sessions",
            arjun(school),
            {"review_schedule_item_id": item["review_schedule_item_id"]},
        ),
        201,
    )
    assert session["review_day"] == 2
    path = f"/v1/review-sessions/{session['review_session_id']}/responses"
    nxt = session["next"]
    while nxt["type"] in ("QUESTION", "RETRY"):  # get everything wrong
        r = await answer(client, db, path, arjun(school), nxt["question"], correct=False)
        nxt = r.json()["data"]["next"]
    await worker.drain(db)

    async with db.connect() as conn:
        rows = await fetch_all(
            conn,
            """SELECT i.review_day, i.scheduled_for, i.status, rs.anchor_date FROM review_schedule_items i
                 JOIN review_schedules rs USING (review_schedule_id)
                WHERE rs.concept_id = (SELECT concept_id FROM review_schedules rs2 JOIN review_schedule_items i2
                                         USING (review_schedule_id) WHERE i2.review_schedule_item_id = :i)
                ORDER BY i.review_day""",
            i=uid(item["review_schedule_item_id"]),
        )
        ev = await fetch_all(
            conn,
            "SELECT evidence_type, review_day, evaluation FROM evidence_events WHERE evidence_context = 'SPACED_REVIEW'",
        )
        reuse = await fetch_all(conn, "SELECT is_anchor_reuse FROM review_session_questions")
        session_row = await fetch_one(conn, "SELECT status FROM review_sessions")
    # Wrong answers do NOT pull the next review earlier: Day 2 -> Day 4, always.
    assert [(r["review_day"], r["status"]) for r in rows] == [(2, "COMPLETED"), (4, "UPCOMING")]
    assert rows[1]["scheduled_for"] == rows[1]["anchor_date"] + timedelta(days=4)
    assert ev and all(e["evidence_type"] == "RETENTION" and e["review_day"] == 2 for e in ev)
    assert any(r["is_anchor_reuse"] for r in reuse)  # reviews may reuse questions seen before
    assert session_row["status"] == "COMPLETED"
    assert len(ok(await call(client, "GET", "/v1/students/me/reviews", arjun(school)))["due"]) == 2


# ================================================================================================
# Oct 5 decision: parent/self learning is scoped and reviews are opt-in
# ================================================================================================
async def _parent_assignment(db, school, lesson_id, review_mode) -> uuid.UUID:
    student = uid(school["students"][0]["student_id"])
    async with db.begin() as conn:
        a = await fetch_one(conn, """INSERT INTO assignments (school_id, lesson_id, created_by, source_type, title, due_at,
                                         status, review_mode, published_at)
                                     VALUES (:s, :l, :u, 'PARENT', 'From home', now() + interval '3 days', 'PUBLISHED', :rm, now())
                                     RETURNING assignment_id""",
                            s=uid(school["school_id"]), l=uid(lesson_id), u=uid(school["admin_user_id"]), rm=review_mode)  # fmt: skip
        await execute(conn, "INSERT INTO assignment_targets (assignment_id, target_type, student_id) VALUES (:a, 'STUDENT', :s)",
                      a=a["assignment_id"], s=student)  # fmt: skip
        sa = await fetch_one(conn, """INSERT INTO student_assignments (school_id, assignment_id, student_id)
                                      VALUES (:sc, :a, :s) RETURNING student_assignment_id""",
                             sc=uid(school["school_id"]), a=a["assignment_id"], s=student)  # fmt: skip
        await execute(conn, "INSERT INTO student_lesson_progress (student_id, lesson_id, student_assignment_id) VALUES (:s, :l, :sa)",
                      s=student, l=uid(lesson_id), sa=sa["student_assignment_id"])  # fmt: skip
    return sa["student_assignment_id"]


@pytest.mark.parametrize("review_mode,expected_parent_reviews", [("ONE_TIME", 0), ("SPACED", 3)])
async def test_parent_work_is_scoped_and_reviews_are_opt_in(
    client, db, storage, school, review_mode, expected_parent_reviews
):
    # School lesson: mastered.
    school_view, school_sa = await assigned(client, db, storage, school, "Photosynthesis")
    await learn(client, school, school_view["lesson_id"], school_sa)
    await run_check(client, db, school, school_view["lesson_id"], school_sa)
    await worker.drain(db)
    # Parent-assigned lesson on the SAME concepts: everything wrong.
    parent_view = await ready_lesson(client, db, storage, school, "Photosynthesis")
    parent_sa = await _parent_assignment(db, school, parent_view["lesson_id"], review_mode)
    await learn(client, school, parent_view["lesson_id"], str(parent_sa))
    await run_check(client, db, school, parent_view["lesson_id"], str(parent_sa), correct=False, text="[wrong] no")
    await worker.drain(db)

    async with db.connect() as conn:
        mastery = await fetch_all(
            conn,
            """SELECT mastery_scope, mastery_status, concept_id FROM student_concept_mastery
                                           ORDER BY mastery_scope, concept_id""",
        )
        parent_evidence = await fetch_all(
            conn, "SELECT mastery_eligible, review_eligible FROM evidence_events WHERE source_scope = 'PARENT'"
        )
        parent_schedules = await fetch_one(
            conn, "SELECT count(*) AS n FROM review_schedules WHERE lesson_id = :l", l=uid(parent_view["lesson_id"])
        )
    school_rows = {m["concept_id"]: m["mastery_status"] for m in mastery if m["mastery_scope"] == "SCHOOL"}
    parent_rows = {m["concept_id"]: m["mastery_status"] for m in mastery if m["mastery_scope"] == "PARENT"}
    assert set(school_rows) == set(parent_rows)  # same concepts...
    assert set(school_rows.values()) == {"MASTERED"}  # ...school mastery untouched
    assert set(parent_rows.values()) == {"NEEDS_REVIEW"}  # parent mastery tracked separately
    assert parent_evidence and all(e["mastery_eligible"] for e in parent_evidence)
    assert all(e["review_eligible"] == (review_mode == "SPACED") for e in parent_evidence)
    assert parent_schedules["n"] == expected_parent_reviews


async def test_school_assignments_cannot_opt_out_of_spaced_review(client, db, storage, school):
    await assigned(client, db, storage, school)
    with pytest.raises(Exception, match="assignments_school_always_spaced"):
        async with db.begin() as conn:
            await execute(conn, "UPDATE assignments SET review_mode = 'ONE_TIME' WHERE source_type = 'SCHOOL'")


async def test_students_cannot_see_answer_keys(client, db, storage, school):
    view, sa = await assigned(client, db, storage, school)
    await learn(client, school, view["lesson_id"], sa)
    start = ok(await call(client, "POST", "/v1/assessment-attempts", arjun(school),
                          {"lesson_id": view["lesson_id"], "student_assignment_id": sa}), 201)  # fmt: skip
    attempt = await client.get(f"/v1/assessment-attempts/{start['attempt_id']}", headers=auth(uid(arjun(school))))
    text = attempt.text + str(start)
    assert "correct_index" not in text and "rubric" not in text and "answer_spec" not in text
