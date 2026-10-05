"""Evidence + MasteryEngine (contract §7.4 TX2, §7.5, §8).

`finalize_evaluation` is the single place an evaluation becomes evidence. It runs inside the
caller's transaction, freezes the source-policy result onto the evidence row, and recomputes
mastery synchronously so a student finishing a check sees an immediate, consistent result.

Source scoping (doc 21, Oct 5 decision): parent-assigned and self-chosen learning produce mastery
that is scoped to that work (`mastery_scope` PARENT / SELF). It never adds to or changes SCHOOL
mastery. Parent/self work schedules spaced reviews only if the assignment chose review_mode SPACED.
"""

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncConnection

from app.context import RequestContext
from app.db import execute, fetch_all, fetch_one
from app.outbox import emit

EVIDENCE_TYPE = {
    "RECALL": "UNDERSTANDING",
    "UNDERSTANDING": "UNDERSTANDING",
    "REASONING": "REASONING",
    "APPLICATION": "APPLICATION",
    "TRANSFER": "TRANSFER",
}
VALUE = {"CORRECT": 1.0, "PARTIAL": 0.5, "INCORRECT": 0.0}
HELP_FACTOR = {None: 1.0, "NONE": 1.0, "HINT": 0.75, "EXPLANATION": 0.5}
MASTERED_CONFIDENCE = 0.75
DEVELOPING_CONFIDENCE = 0.4


@dataclass(frozen=True)
class Eligibility:
    assignment_id: UUID | None
    scope: str  # SCHOOL | PARENT | SELF
    mastery_eligible: bool
    review_eligible: bool


async def eligibility_for(conn: AsyncConnection, student_assignment_id: UUID | None) -> Eligibility:
    """Evaluated once, at evidence-write time, and frozen onto the evidence row (contract §8)."""
    row = await fetch_one(
        conn,
        """SELECT a.assignment_id, a.source_type, a.review_mode,
                  p.can_create_mastery_evidence, p.can_schedule_retention_reviews
             FROM student_assignments sa
             JOIN assignments a ON a.assignment_id = sa.assignment_id
             JOIN assignment_source_policies p ON p.source_type = a.source_type
            WHERE sa.student_assignment_id = :sa""",
        sa=student_assignment_id,
    )
    if row is None:  # not linked to any assignment: learn-only, nothing recorded as mastery
        return Eligibility(None, "SELF", False, False)
    return Eligibility(
        assignment_id=row["assignment_id"],
        scope=row["source_type"],
        mastery_eligible=row["can_create_mastery_evidence"],
        review_eligible=row["can_schedule_retention_reviews"] and row["review_mode"] == "SPACED",
    )


async def finalize_evaluation(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    attempt: dict[str, Any],
    response: dict[str, Any],
    question: dict[str, Any],
    evaluation: str,
    confidence: float | None,
    help_level: str,
    evaluated_by: str,
    evidence_context: str,
    review_day: int | None = None,
) -> dict[str, Any]:
    await execute(
        conn,
        "UPDATE response_evaluations SET is_current = false WHERE response_id = :r AND is_current",
        r=response["response_id"],
    )
    ev = await fetch_one(
        conn,
        """INSERT INTO response_evaluations (response_id, evaluated_by, evaluation, evaluator_confidence, help_level,
                                             is_current, evaluated_at)
           VALUES (:r, :by, :e, :c, :h, true, now()) RETURNING response_evaluation_id""",
        r=response["response_id"],
        by=evaluated_by,
        e=evaluation,
        c=confidence,
        h=help_level,
    )
    await execute(
        conn,
        """UPDATE student_question_exposures SET evaluation = :e
            WHERE assessment_attempt_id = :a AND question_id = :q""",
        e=evaluation,
        a=attempt["attempt_id"],
        q=question["question_id"],
    )
    elig = await eligibility_for(conn, attempt["student_assignment_id"])
    lesson = await fetch_one(conn, "SELECT school_id FROM lessons WHERE lesson_id = :l", l=attempt["lesson_id"])
    evidence_type = "RETENTION" if evidence_context == "SPACED_REVIEW" else EVIDENCE_TYPE[question["cognitive_type"]]
    evidence = await fetch_one(
        conn,
        """INSERT INTO evidence_events (school_id, student_id, lesson_id, assignment_id, concept_id, question_id,
                                        response_evaluation_id, evidence_context, evidence_type, evaluation, help_level,
                                        evidence_strength, source_provenance, review_day, occurred_at,
                                        mastery_eligible, review_eligible, source_scope)
           VALUES (:s, :st, :l, :a, :c, :q, :re, :ctxt, :et, :e, :h, :strength,
                   jsonb_build_object('attempt_id', CAST(:att AS text), 'response_id', CAST(:resp AS text),
                                      'evaluated_by', CAST(:by AS text)),
                   :rd, now(), :me, :rev, :scope)
           RETURNING evidence_event_id""",
        s=lesson["school_id"],
        st=attempt["student_id"],
        l=attempt["lesson_id"],
        a=elig.assignment_id,
        c=question["concept_id"],
        q=question["question_id"],
        re=ev["response_evaluation_id"],
        ctxt=evidence_context,
        et=evidence_type,
        e=evaluation,
        h=help_level,
        strength=VALUE[evaluation] * HELP_FACTOR[help_level],
        att=str(attempt["attempt_id"]),
        resp=str(response["response_id"]),
        by=evaluated_by,
        rd=review_day,
        me=elig.mastery_eligible,
        rev=elig.review_eligible,
        scope=elig.scope,
    )
    common = {
        "student_id": attempt["student_id"],
        "lesson_id": attempt["lesson_id"],
        "concept_id": question["concept_id"],
    }
    await emit(
        conn,
        ctx,
        event_type="ResponseEvaluated",
        aggregate_type="assessment_response",
        aggregate_id=response["response_id"],
        school_id=lesson["school_id"],
        data={**common, "evaluation": evaluation, "evaluated_by": evaluated_by, "help_level": help_level},
    )
    await emit(
        conn,
        ctx,
        event_type="EvidenceRecorded",
        aggregate_type="evidence_event",
        aggregate_id=evidence["evidence_event_id"],
        school_id=lesson["school_id"],
        data={**common, "context": evidence_context, "scope": elig.scope, "mastery_eligible": elig.mastery_eligible},
    )
    change = None
    if elig.mastery_eligible:
        change = await recompute_mastery(
            conn,
            ctx,
            student_id=attempt["student_id"],
            concept_id=question["concept_id"],
            lesson_id=attempt["lesson_id"],
            scope=elig.scope,
            school_id=lesson["school_id"],
            trigger_evidence_id=evidence["evidence_event_id"],
        )
    return {
        "response_evaluation_id": ev["response_evaluation_id"],
        "evidence_event_id": evidence["evidence_event_id"],
        "eligibility": elig,
        "mastery_change": change,
    }


async def _min_correct(conn: AsyncConnection, lesson_id: UUID, concept_id: UUID) -> int:
    row = await fetch_one(
        conn,
        """SELECT bc.required_evidence FROM assessment_blueprint_concepts bc
             JOIN assessment_blueprints b ON b.blueprint_id = bc.blueprint_id
            WHERE b.lesson_id = :l AND b.status = 'ACTIVE' AND bc.concept_id = :c""",
        l=lesson_id,
        c=concept_id,
    )
    return int((row["required_evidence"] or {}).get("min_correct", 1)) if row else 1


async def recompute_mastery(
    conn: AsyncConnection,
    ctx: RequestContext,
    *,
    student_id: UUID,
    concept_id: UUID,
    lesson_id: UUID,
    scope: str,
    school_id: UUID,
    trigger_evidence_id: UUID,
) -> dict[str, Any] | None:
    """Mastery for one (student, concept, lesson, scope) from all CURRENT, eligible evidence.

    confidence = mean(value x help factor); MASTERED needs >= min_correct unaided correct answers
    and confidence >= 0.75; DEVELOPING >= 0.4; otherwise NEEDS_REVIEW. Adjusted (superseded)
    evaluations no longer count. Scopes never mix: school mastery reads only SCHOOL evidence.
    """
    evidence = await fetch_all(
        conn,
        """SELECT e.evaluation, e.help_level FROM evidence_events e
             LEFT JOIN response_evaluations re ON re.response_evaluation_id = e.response_evaluation_id
            WHERE e.student_id = :st AND e.concept_id = :c AND e.lesson_id = :l AND e.source_scope = :scope
              AND e.mastery_eligible AND (re.response_evaluation_id IS NULL OR re.is_current)""",
        st=student_id,
        c=concept_id,
        l=lesson_id,
        scope=scope,
    )
    if not evidence:
        return None
    confidence = sum(VALUE[e["evaluation"]] * HELP_FACTOR[e["help_level"]] for e in evidence) / len(evidence)
    unaided = sum(1 for e in evidence if e["evaluation"] == "CORRECT" and e["help_level"] in (None, "NONE"))
    needed = await _min_correct(conn, lesson_id, concept_id)
    if unaided >= needed and confidence >= MASTERED_CONFIDENCE:
        status = "MASTERED"
    elif confidence >= DEVELOPING_CONFIDENCE:
        status = "DEVELOPING"
    else:
        status = "NEEDS_REVIEW"
    confidence = round(confidence, 3)

    current = await fetch_one(
        conn,
        """SELECT mastery_id, mastery_status FROM student_concept_mastery
            WHERE student_id = :st AND concept_id = :c AND lesson_id = :l AND mastery_scope = :scope
            FOR UPDATE""",
        st=student_id,
        c=concept_id,
        l=lesson_id,
        scope=scope,
    )
    if current is None:
        await execute(
            conn,
            """INSERT INTO student_concept_mastery (student_id, concept_id, lesson_id, mastery_status,
                                                    internal_confidence, last_evidence_at, mastery_scope)
               VALUES (:st, :c, :l, :s, :conf, now(), :scope)""",
            st=student_id,
            c=concept_id,
            l=lesson_id,
            s=status,
            conf=confidence,
            scope=scope,
        )
        previous = None
    else:
        await execute(
            conn,
            """UPDATE student_concept_mastery SET mastery_status = :s, internal_confidence = :conf,
                      last_evidence_at = now(), version = version + 1
                WHERE mastery_id = :m""",
            s=status,
            conf=confidence,
            m=current["mastery_id"],
        )
        previous = current["mastery_status"]
    if previous == status:
        return None
    await execute(
        conn,
        """INSERT INTO mastery_state_history (student_id, concept_id, lesson_id, from_status, to_status,
                                              internal_confidence, trigger_evidence_event_id, reason_code, changed_at)
           VALUES (:st, :c, :l, :f, :t, :conf, :ev, 'EVIDENCE', now())""",
        st=student_id,
        c=concept_id,
        l=lesson_id,
        f=previous,
        t=status,
        conf=confidence,
        ev=trigger_evidence_id,
    )
    await emit(
        conn,
        ctx,
        event_type="MasteryStatusChanged",
        aggregate_type="student_concept_mastery",
        aggregate_id=concept_id,
        school_id=school_id,
        data={
            "student_id": student_id,
            "concept_id": concept_id,
            "lesson_id": lesson_id,
            "scope": scope,
            "from": previous,
            "to": status,
        },  # fmt: skip
    )
    return {"concept_id": concept_id, "from": previous, "to": status}
