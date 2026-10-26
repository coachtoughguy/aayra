"""Admin operations (bulk import, transfer, withdraw, rollover, admin home), late-enrollment back-fill,
and the role-home views the web app uses."""

from app import worker
from app.db import fetch_all, fetch_one
from tests.test_extensions import family_upload, link_parent
from tests.test_slice1 import (  # noqa: F401
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


def admin(school) -> str:
    return school["admin_user_id"]


async def second_section(client, school, name="10B") -> str:
    grade = school["grade_id"]
    return ok(await call(client, "POST", f"/v1/grades/{grade}/sections", admin(school), {"name": name}), 201)[
        "section_id"
    ]


CSV = """admission_id,display_name,date_of_birth,section,email
N-001,Lina Park,2011-04-02,10A,
N-002,Omar Said,2011-05-03,10b,omar@family.example.com
10A-001,Duplicate Arjun,2011-01-01,10A,
N-003,Too Young,2020-01-01,10A,
N-004,Nowhere,2011-01-01,12Z,
N-001,Lina Again,2011-04-02,10A,
N-005,Bad Date,02/03/2011,10A,
"""


async def test_bulk_import_validates_per_row_and_commits_valid_rows(client, db, school):
    await second_section(client, school)
    r = ok(await call(client, "POST", f"/v1/schools/{school['school_id']}/student-imports", admin(school),
                      {"file_name": "roster.csv", "csv_text": CSV}), 201)  # fmt: skip
    assert (r["total_rows"], r["valid_rows"], r["error_rows"]) == (7, 2, 5)
    assert [(e["row_number"], e["error_reason"]) for e in r["errors"]] == [
        (4, "duplicate_admission_id"), (5, "below_minimum_age"), (6, "invalid_section"), (7, "duplicate_in_file"),
        (8, "invalid_date_of_birth")]  # fmt: skip
    err(await call(client, "POST", f"/v1/schools/{school['school_id']}/student-imports", admin(school),
                   {"file_name": "x.csv", "csv_text": "name,age\nA,1\n"}), 422, "VALIDATION_ERROR")  # fmt: skip
    err(await call(client, "POST", f"/v1/schools/{school['school_id']}/student-imports", sarah(school),
                   {"file_name": "x.csv", "csv_text": CSV}), 403, "INSUFFICIENT_ROLE_SCOPE")  # fmt: skip
    done = ok(await call(client, "POST", f"/v1/student-imports/{r['batch_id']}/commit", admin(school)))
    assert done["created"] == 2 and {lg["username"] for lg in done["logins"]} == {"demohs.n001", None}
    err(await call(client, "POST", f"/v1/student-imports/{r['batch_id']}/commit", admin(school), key="again"), 409,
        "IMPORT_ALREADY_COMMITTED")  # fmt: skip
    batch = ok(await call(client, "GET", f"/v1/student-imports/{r['batch_id']}", admin(school)))
    assert batch["status"] == "COMMITTED" and sum(1 for x in batch["rows"] if x["resulting_user_id"]) == 2


async def test_late_joiner_backfill_transfer_and_withdraw(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    await publish(client, view, school)
    await worker.drain(db)
    # a past-due assignment is NOT auto-assigned; the teacher is told
    old = await ready_lesson(client, db, storage, school, "Cells")
    await publish(client, old, school)
    await worker.drain(db)
    async with db.begin() as conn:
        from app.db import execute

        await execute(
            conn,
            "UPDATE assignments SET due_at = now() - interval '1 day' WHERE lesson_id = :l",
            l=uid(old["lesson_id"]),
        )
    new_kid = ok(await call(client, "POST", "/v1/students", admin(school), {"section_id": school["section_id"],
                 "display_name": "Mid Year", "admission_id": "MY-1", "date_of_birth": "2011-02-02"}), 201)  # fmt: skip
    me = await fetch_user(db, new_kid["student_id"])
    home = ok(await call(client, "GET", "/v1/students/me/home", me))
    assert [i["title"] for i in home["whats_next"] if i["kind"] == "LEARNING"] == ["Photosynthesis"]
    async with db.connect() as conn:
        note = await fetch_one(
            conn, "SELECT title, body FROM notifications WHERE notification_type = 'LATE_JOINER_CATCH_UP'"
        )
    assert note["title"].startswith("Mid Year joined your class: 1") and note["body"] == "Cells"

    # transfer to 10B keeps history and back-fills 10B's current work
    b = await second_section(client, school)
    t = ok(
        await call(client, "POST", f"/v1/students/{new_kid['student_id']}/transfer", admin(school), {"section_id": b})
    )
    assert t["section_name"] == "10B"
    err(await call(client, "POST", f"/v1/students/{new_kid['student_id']}/transfer", admin(school), {"section_id": b}, key="t2"),
        409, "ALREADY_IN_SECTION")  # fmt: skip
    async with db.connect() as conn:
        hist = await fetch_all(conn, "SELECT status FROM student_enrollments WHERE student_id = :s ORDER BY created_at",
                               s=uid(new_kid["student_id"]))  # fmt: skip
    assert [h["status"] for h in hist] == ["TRANSFERRED", "ACTIVE"]
    assert ok(await call(client, "GET", "/v1/students/me/home", me))["whats_next"]  # in-progress work continues
    roster = ok(await call(client, "GET", f"/v1/sections/{b}/students", admin(school)))["students"]
    assert [s["display_name"] for s in roster] == ["Mid Year"] and roster[0][
        "guardian_status"
    ] == "PRIMARY_GUARDIAN_PENDING"

    # withdrawal revokes access but keeps the records
    ok(
        await call(
            client, "POST", f"/v1/students/{new_kid['student_id']}/withdraw", admin(school), {"reason": "Moved away"}
        )
    )
    err(await call(client, "GET", "/v1/students/me/home", me), 403, "ENROLLMENT_INACTIVE")
    async with db.connect() as conn:
        assert (await fetch_one(conn, "SELECT count(*) AS n FROM student_assignments WHERE student_id = :s",
                                s=uid(new_kid["student_id"])))["n"] >= 1  # fmt: skip


async def fetch_user(db, student_id) -> str:
    async with db.connect() as conn:
        return str(
            (await fetch_one(conn, "SELECT user_id FROM student_profiles WHERE student_id = :s", s=uid(student_id)))[
                "user_id"
            ]
        )


async def test_rollover_dry_run_then_apply(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    await publish(client, view, school)
    await worker.drain(db)
    sid = school["school_id"]
    ny = ok(await call(client, "POST", f"/v1/schools/{sid}/academic-years", admin(school),
                       {"name": "2027-28", "start_date": "2027-06-01", "end_date": "2028-05-31", "status": "PLANNED"}), 201)  # fmt: skip
    g11 = ok(await call(client, "POST", f"/v1/academic-years/{ny['academic_year_id']}/grades", admin(school),
                        {"name": "Grade 11", "grade_level": 11}), 201)  # fmt: skip
    path = f"/v1/academic-years/{school['academic_year_id']}/rollover"
    plan = ok(await call(client, "POST", path, admin(school), {"to_academic_year_id": ny["academic_year_id"]}))
    # 10A is the top grade of the old year, so with no 11A it would "complete"; add 11A and plan again
    assert plan["dry_run"] and plan["moves"][0]["outcome"] == "COMPLETED"
    s11 = ok(await call(client, "POST", f"/v1/grades/{g11['grade_id']}/sections", admin(school), {"name": "11A"}), 201)
    plan = ok(
        await call(client, "POST", path, admin(school), {"to_academic_year_id": ny["academic_year_id"]}, key="p2")
    )
    assert plan["moves"][0]["to_section"] == "11A" and plan["moves"][0]["students"] == 28 and plan["can_apply"]
    assert plan["open_assignments_to_close"] == 1
    done = ok(
        await call(
            client, "POST", path, admin(school), {"to_academic_year_id": ny["academic_year_id"], "dry_run": False}
        )
    )
    assert (done["promoted"], done["completed"], done["assignments_closed"]) == (28, 0, 1)
    me = ok(await call(client, "GET", "/v1/me", arjun(school)))
    assert me["student"]["section_name"] == "11A"
    async with db.connect() as conn:
        years = {
            r["name"]: r["status"]
            for r in await fetch_all(conn, "SELECT name, status FROM academic_years WHERE school_id = :s", s=uid(sid))
        }
        teach = await fetch_one(conn, """SELECT count(*) AS n FROM teacher_section_subject_assignments
                                          WHERE academic_year_id = :y AND status = 'ACTIVE'""", y=uid(school["academic_year_id"]))  # fmt: skip
    assert years == {"2026-27": "CLOSED", "2027-28": "ACTIVE"} and teach["n"] == 0
    err(await call(client, "POST", path, admin(school), {"to_academic_year_id": ny["academic_year_id"], "dry_run": False}, key="x"),
        409, "ROLLOVER_NOT_ALLOWED")  # fmt: skip
    assert s11


async def test_admin_home_structure_and_teacher_list(client, db, school):
    home = ok(await call(client, "GET", f"/v1/schools/{school['school_id']}/admin/home", admin(school)))
    assert home["people"] == {"active_students": 28, "teachers": 3}
    assert {n["key"]: n["count"] for n in home["needs_attention"]}["students_without_guardian"] == 28
    await link_parent(client, school)
    home = ok(await call(client, "GET", f"/v1/schools/{school['school_id']}/admin/home", admin(school)))
    assert {n["key"]: n["count"] for n in home["needs_attention"]}["students_without_guardian"] == 27
    st = ok(await call(client, "GET", f"/v1/schools/{school['school_id']}/structure", admin(school)))
    sec = st["academic_years"][0]["grades"][0]["sections"][0]
    assert (sec["name"], sec["active_students"], sec["class_teacher"]) == ("10A", 28, "Sarah Johnson")
    teachers = ok(await call(client, "GET", f"/v1/schools/{school['school_id']}/teachers", admin(school)))["teachers"]
    assert {t["display_name"]: len(t["assignments"]) for t in teachers} == {
        "Priya Nair": 1,
        "Ravi Kumar": 1,
        "Sarah Johnson": 2,
    }
    err(
        await call(client, "GET", f"/v1/schools/{school['school_id']}/admin/home", sarah(school)),
        403,
        "INSUFFICIENT_ROLE_SCOPE",
    )


async def test_role_home_views(client, db, storage, school):
    view = await ready_lesson(client, db, storage, school)
    await publish(client, view, school, completion_mode="SUBMISSION_REQUIRED")
    await worker.drain(db)
    th = ok(await call(client, "GET", "/v1/teacher/home", sarah(school)))
    assert th["school_name"] == "Aayra Demo High School" and [lsn["title"] for lsn in th["lessons"]] == [
        "Photosynthesis"
    ]
    assert {(t["section_name"], t["assignment_role"]) for t in th["teaching"]} == {
        ("10A", "CLASS_TEACHER"),
        ("10A", "PRIMARY_SUBJECT_TEACHER"),
    }
    ta = ok(await call(client, "GET", "/v1/teacher/assignments", sarah(school)))["assignments"]
    assert ta[0]["students"] == 28 and ta[0]["done"] == 0
    # parent: school work + their own assignments, never the child's self-learning
    parent = await link_parent(client, school)
    arjun_id = school["students"][0]["student_id"]
    item = await family_upload(client, storage, parent, "PARENT", arjun_id)
    ok(await call(client, "POST", "/v1/parent-assignments", parent, {"student_id": arjun_id, "subject_id": school["subjects"]["BIO"],
                  "title": "Home practice", "content_item_ids": [item], "review_mode": "ONE_TIME"}), 201)  # fmt: skip
    own = await family_upload(client, storage, arjun(school), "SELF", title="Secret hobby")
    ok(await call(client, "POST", "/v1/self-assignments", arjun(school), {"subject_id": school["subjects"]["BIO"],
                  "title": "My own thing", "content_item_ids": [own], "review_mode": "SPACED"}), 201)  # fmt: skip
    await worker.drain(db)
    ov = ok(await call(client, "GET", f"/v1/guardians/me/children/{arjun_id}/overview", parent))
    assert [w["title"] for w in ov["work"]] == ["Photosynthesis"] and ov["work"][0]["label"] == "NOT_STARTED"
    assert [m["title"] for m in ov["my_assignments"]] == ["Home practice"] and ov["my_assignments"][0][
        "state"
    ] == "READY_TO_ASSIGN"
    assert "SELF" not in ov["progress"]["work"] and "My own thing" not in str(ov)
    subjects = ok(await call(client, "GET", f"/v1/guardians/me/children/{arjun_id}/subjects", parent))["subjects"]
    assert [s["name"] for s in subjects] == ["Biology", "Mathematics"]
    mine = ok(await call(client, "GET", "/v1/self-assignments", arjun(school)))["assignments"]
    assert [m["title"] for m in mine] == ["My own thing"] and mine[0]["state"] == "READY_TO_ASSIGN"
