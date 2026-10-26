from fastapi import APIRouter, Depends, Request

from app.auth import Actor, current_actor
from app.db import fetch_all, fetch_one, get_db

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict:
    async with get_db().connect() as conn:
        row = await fetch_one(conn, "SELECT count(*) AS roles FROM roles")
    return {"data": {"status": "ok", "roles": row["roles"] if row else 0}}


@router.get("/v1/me")
async def me(request: Request, actor: Actor = Depends(current_actor)) -> dict:
    """Who am I, which roles do I hold where, and which home screens apply (the web app routes on
    this). Also the simplest auth smoke test."""
    async with get_db().connect() as conn:
        user = await fetch_one(conn, "SELECT email, age_gate_status FROM users WHERE user_id = :u", u=actor.user_id)
        schools = (
            await fetch_all(
                conn,
                "SELECT school_id, name, school_kind, timezone FROM schools WHERE school_id = ANY(:ids) ORDER BY name",
                ids=list(actor.roles),
            )
            if actor.roles
            else []
        )
        student = await fetch_one(
            conn,
            """SELECT sp.student_id, se.school_id, s.school_kind, s.name AS school_name, g.name AS grade_name,
                      sec.name AS section_name, se.academic_year_id
                 FROM student_profiles sp
                 JOIN student_enrollments se ON se.student_id = sp.student_id AND se.status = 'ACTIVE'
                 JOIN academic_years ay ON ay.academic_year_id = se.academic_year_id AND ay.status = 'ACTIVE'
                 JOIN schools s ON s.school_id = se.school_id
                 JOIN grades g ON g.grade_id = se.grade_id JOIN sections sec ON sec.section_id = se.section_id
                WHERE sp.user_id = :u AND sp.status = 'ACTIVE' ORDER BY ay.start_date DESC LIMIT 1""",
            u=actor.user_id,
        )
        children = await fetch_one(
            conn,
            """SELECT count(*) AS n FROM guardian_profiles gp
                 JOIN student_guardians sg ON sg.guardian_id = gp.guardian_id AND sg.status = 'ACTIVE'
                WHERE gp.user_id = :u""",
            u=actor.user_id,
        )
    for sc in schools:
        sc["roles"] = sorted(actor.roles.get(sc["school_id"], ()))
    homes = []
    if student:
        homes.append("INDEPENDENT" if student["school_kind"] == "INDEPENDENT" else "STUDENT")
    if actor.schools_with_role("TEACHER"):
        homes.append("TEACHER")
    if children and children["n"]:
        homes.append("PARENT")
    if actor.schools_with_role("SCHOOL_ADMIN"):
        homes.append("ADMIN")
    return {
        "data": {
            "user_id": str(actor.user_id),
            "display_name": actor.display_name,
            "email": user["email"] if user else None,
            "roles": {str(s): sorted(r) for s, r in actor.roles.items()},
            "schools": schools,
            "student": student,
            "homes": homes,
        },
        "meta": {"request_id": request.state.request_id},
    }
