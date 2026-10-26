"""Teacher review & edit of AI-generated content before publishing (docs 11/12 LOCKED)."""

from app import worker
from app.db import fetch_all
from tests.factories import auth
from tests.test_slice1 import (  # noqa: F401  (fixtures)
    _stubs,
    arjun,
    call,
    err,
    ok,
    publish,
    publish_body,
    ready_lesson,
    sarah,
    school,
    storage,
    uid,
)


def _q(view, qtype="MCQ", n=0):
    return [q for q in view["generation"]["questions"] if q["question_type"] == qtype][n]


async def _view(client, school, lesson_id):
    return ok(await call(client, "GET", f"/v1/lessons/{lesson_id}", sarah(school)))


async def test_teacher_edits_questions_and_topics_before_publishing(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    lid, gv = view["lesson_id"], view["generation"]["generation_version"]
    base = f"/v1/lessons/{lid}"
    mcq = _q(view)
    assert view["generation"]["edited_by_teacher"] is False and mcq["source"] == "AI"

    # bad edits are rejected without changing anything
    err(await call(client, "PATCH", f"{base}/questions/{mcq['question_id']}", sarah(school),
                   {"generation_version": gv, "options": ["A", "B"], "correct_index": 3}), 422, "VALIDATION_ERROR")  # fmt: skip
    err(await call(client, "PATCH", f"{base}/questions/{mcq['question_id']}", sarah(school),
                   {"generation_version": gv, "options": ["Same", "same"], "correct_index": 0}), 422, "VALIDATION_ERROR")  # fmt: skip
    other = school["teachers"]["ravi"]["user_id"]
    r = await call(
        client,
        "PATCH",
        f"{base}/questions/{mcq['question_id']}",
        other,
        {"generation_version": gv, "prompt_text": "Hacked?"},
    )
    assert r.status_code in (403, 404)

    edited = ok(await call(client, "PATCH", f"{base}/questions/{mcq['question_id']}", sarah(school), {
        "generation_version": gv, "prompt_text": "Which pigment captures light energy in leaves?",
        "options": ["Chlorophyll", "Glucose", "Oxygen", "Starch"], "correct_index": 0, "hint": "It makes leaves green."}))  # fmt: skip
    assert edited["generation_version"] == gv + 1
    err(await call(client, "PATCH", f"{base}/questions/{mcq['question_id']}", sarah(school),
                   {"generation_version": gv, "prompt_text": "Stale edit here"}), 409, "GENERATION_VERSION_STALE")  # fmt: skip

    view = await _view(client, school, lid)
    gv = view["generation"]["generation_version"]
    changed = next(q for q in view["generation"]["questions"] if q["question_id"] == str(edited["question_id"]))
    assert changed["prompt_text"].startswith("Which pigment") and changed["source"] == "EDITED"
    assert changed["answer_spec"]["hint"] == "It makes leaves green." and view["generation"]["edited_by_teacher"]
    assert len(view["generation"]["questions"]) == 5

    # add a teacher-written question, then remove one; every topic must keep a question
    app_concept = view["generation"]["concepts"][2]  # SUPPORTING concept: one MCQ only
    added = ok(await call(client, "POST", f"{base}/questions", sarah(school), {
        "generation_version": gv, "concept_id": app_concept["concept_id"], "question_type": "FREE_TEXT",
        "cognitive_type": "APPLICATION", "prompt_text": "Explain why a plant in a dark cupboard stops growing.",
        "rubric": ["Links light to photosynthesis", "Mentions energy/food"]}), 201)  # fmt: skip
    gv = added["generation_version"]
    view = await _view(client, school, lid)
    assert (
        next(q for q in view["generation"]["questions"] if q["question_id"] == str(added["question_id"]))["source"]
        == "TEACHER"
    )
    only_mcq = next(q for q in view["generation"]["questions"]
                    if q["concept_id"] == app_concept["concept_id"] and q["question_type"] == "MCQ")  # fmt: skip
    ok(await client.delete(f"{base}/questions/{only_mcq['question_id']}?generation_version={gv}",
                           headers=auth(uid(sarah(school)), **{"Idempotency-Key": "rm-q1"})))  # fmt: skip
    gv += 1
    view = await _view(client, school, lid)
    last = next(q for q in view["generation"]["questions"] if q["concept_id"] == app_concept["concept_id"])
    err(await client.delete(f"{base}/questions/{last['question_id']}?generation_version={gv}",
                            headers=auth(uid(sarah(school)), **{"Idempotency-Key": "rm-q2"})), 409, "CONCEPT_NEEDS_QUESTION")  # fmt: skip

    # rename / re-weight a topic, and drop one
    core = view["generation"]["concepts"][0]
    renamed = ok(await call(client, "PATCH", f"{base}/concepts/{core['concept_id']}", sarah(school),
                            {"generation_version": gv, "name": "Light capture by chlorophyll", "importance": "SUPPORTING"}))  # fmt: skip
    gv = renamed["generation_version"]
    view = await _view(client, school, lid)
    first = view["generation"]["concepts"][0]
    assert (first["name"], first["importance"]) == ("Light capture by chlorophyll", "SUPPORTING")
    assert {q["concept_id"] for q in view["generation"]["questions"]} >= {first["concept_id"]}
    drop = view["generation"]["concepts"][1]
    ok(await client.delete(f"{base}/concepts/{drop['concept_id']}?generation_version={gv}",
                           headers=auth(uid(sarah(school)), **{"Idempotency-Key": "rm-c"})))  # fmt: skip
    view = await _view(client, school, lid)
    assert [c["name"] for c in view["generation"]["concepts"]] == [
        "Light capture by chlorophyll",
        "Photosynthesis: application",
    ]
    assert drop["concept_id"] not in {q["concept_id"] for q in view["generation"]["questions"]}

    # publishing needs the latest reviewed version; then content is locked
    stale = publish_body(view, school)
    stale["reviewed_generation_version"] = gv
    err(await call(client, "POST", f"{base}/publish", sarah(school), stale), 409, "GENERATION_VERSION_STALE")
    await publish(client, view, school)
    gv = view["generation"]["generation_version"]
    err(await call(client, "PATCH", f"{base}/questions/{view['generation']['questions'][0]['question_id']}", sarah(school),
                   {"generation_version": gv, "prompt_text": "After publishing?"}), 409, "LESSON_NOT_EDITABLE")  # fmt: skip

    # students get exactly the reviewed content
    await worker.drain(db)
    home = ok(await call(client, "GET", "/v1/students/me/home", arjun(school)))
    sa = home["whats_next"][0]["student_assignment_id"]
    from tests.test_slice2 import learn

    await learn(client, school, lid, sa)
    start = ok(
        await call(
            client, "POST", "/v1/assessment-attempts", arjun(school), {"lesson_id": lid, "student_assignment_id": sa}
        ),
        201,
    )
    served_concepts = {start["next"]["question"]["concept_id"]} if "concept_id" in start["next"]["question"] else set()
    assert drop["concept_id"] not in served_concepts
    async with db.connect() as conn:
        history = await fetch_all(conn, """SELECT b.version, b.status, r.run_type FROM assessment_blueprints b
                                            JOIN ai_generation_runs r USING (generation_run_id)
                                           WHERE b.lesson_id = :l ORDER BY b.version""", l=uid(lid))  # fmt: skip
        audits = await fetch_all(conn, "SELECT action FROM audit_events WHERE action = 'LESSON_GENERATION_EDITED'")
    assert history[0]["run_type"] == "ASSESSMENT_PREPARATION" and history[-1]["status"] == "ACTIVE"
    assert {h["run_type"] for h in history[1:]} == {"TEACHER_EDIT"} and sum(
        h["status"] == "ACTIVE" for h in history
    ) == 1
    assert len(audits) == len(history) - 1


async def test_regenerate_asks_the_ai_again(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school, "Cells")
    lid = view["lesson_id"]
    ok(await call(client, "PATCH", f"/v1/lessons/{lid}/questions/{_q(view)['question_id']}", sarah(school),
                  {"generation_version": view["generation"]["generation_version"], "prompt_text": "Edited question?"}))  # fmt: skip
    view = await _view(client, school, lid)
    err(
        await call(client, "POST", f"/v1/lessons/{lid}/regenerate", sarah(school), {"version": 1}),
        409,
        "VERSION_CONFLICT",
    )
    regen = ok(await call(client, "POST", f"/v1/lessons/{lid}/regenerate", sarah(school), {"version": view["version"]}))
    assert regen["status"] == "PROCESSING"
    await worker.drain(db)
    fresh = await _view(client, school, lid)
    assert (
        fresh["status"] == "READY"
        and fresh["generation"]["generation_version"] == view["generation"]["generation_version"] + 1
    )
    assert {q["source"] for q in fresh["generation"]["questions"]} == {"AI"} and not fresh["generation"][
        "edited_by_teacher"
    ]
