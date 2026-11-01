import { useState } from "react";
import { Link, useParams, useSearchParams } from "react-router-dom";
import { Badge, Button, Card, Empty, ErrorNote, Note, PageHeader, Spinner, useAction } from "../../components/ui";
import { ApiError, fileUrl, patch, post, put } from "../../lib/api";
import { fmtDate, label, relativeDue } from "../../lib/format";
import { useApi } from "../../lib/hooks";

function FeedbackBox({ sa, onClose }: { sa: string; onClose: () => void }) {
  const list = useApi<any>(`/v1/teacher-feedback?student_assignment_id=${sa}`);
  const draft = list.data?.feedback?.find((f: any) => f.status === "DRAFT" && f.mine);
  const published = list.data?.feedback?.filter((f: any) => f.status === "PUBLISHED") || [];
  const [text, setText] = useState<string | null>(null);
  const a = useAction();
  const value = text ?? draft?.feedback_text ?? "";
  async function create(ai: boolean) {
    await a.run(async () => {
      try {
        await post("/v1/teacher-feedback", { student_assignment_id: sa, ai_draft: ai, feedback_text: ai ? null : value || null });
      } catch (e) {
        if (!(e instanceof ApiError && e.code === "FEEDBACK_DRAFT_EXISTS")) throw e;
      }
      setText(null);
      await list.reload();
    });
  }
  return (
    <div className="editor stack">
      {published.map((f: any) => <Note key={f.feedback_id} tone="good">Sent {fmtDate(f.published_at)}: {f.feedback_text}</Note>)}
      {draft ? (
        <>
          {draft.ai_draft_text ? <p className="muted small">AI draft below: edit it so it sounds like you. Students only see what you send.</p> : null}
          <textarea rows={4} value={value} onChange={(e) => setText(e.target.value)} aria-label="Feedback" />
          <div className="row gap wrap">
            <Button variant="secondary" busy={a.busy} onClick={() => a.run(async () => { await patch(`/v1/teacher-feedback/${draft.feedback_id}`, { feedback_text: value }); await list.reload(); setText(null); })}>Save draft</Button>
            <Button busy={a.busy} disabled={!value.trim()} onClick={() => a.run(async () => {
              if (text !== null) await patch(`/v1/teacher-feedback/${draft.feedback_id}`, { feedback_text: value });
              await post(`/v1/teacher-feedback/${draft.feedback_id}/publish`);
              await list.reload();
              setText(null);
            })}>Send to student</Button>
          </div>
        </>
      ) : (
        <div className="row gap wrap">
          <Button variant="secondary" busy={a.busy} onClick={() => create(true)}>Draft with AI</Button>
          <Button variant="ghost" busy={a.busy} onClick={() => create(false)}>Write my own</Button>
        </div>
      )}
      <ErrorNote error={a.error} />
      <button className="link small" onClick={onClose}>Close</button>
    </div>
  );
}

function MarkCell({ sub, out_of, onSaved }: { sub: any; out_of: number; onSaved: () => void }) {
  const [v, setV] = useState<string>(sub.marks?.awarded != null ? String(sub.marks.awarded) : "");
  const a = useAction();
  return (
    <span className="row gap-sm">
      <input className="mark" type="number" min={0} max={out_of} step="0.5" value={v} onChange={(e) => setV(e.target.value)} aria-label="Marks" />
      <span className="muted small">/ {out_of}</span>
      <Button variant="secondary" busy={a.busy} disabled={v === ""} onClick={() => a.run(async () => { await put(`/v1/submissions/${sub.submission_id}/marks`, { marks_awarded: Number(v) }); onSaved(); })}>Save</Button>
      {sub.marks?.released_at ? <span className="muted small">Released</span> : null}
      {a.error ? <span className="bad-text small">{a.error}</span> : null}
    </span>
  );
}

export default function AssignmentDetail() {
  const { id = "" } = useParams();
  const [params] = useSearchParams();
  const lessonId = params.get("lesson");
  const asg = useApi<any>(`/v1/assignments/${id}`);
  const prog = useApi<any>(lessonId ? `/v1/lessons/${lessonId}/progress` : null);
  const subs = useApi<any>(`/v1/assignments/${id}/submissions`);
  const [open, setOpen] = useState<string | null>(null);
  const a = useAction();
  if (asg.loading && !asg.data) return <Spinner />;
  if (!asg.data) return <ErrorNote error={asg.error} onRetry={asg.reload} />;
  const A = asg.data;
  const p = prog.data?.assignments?.find((x: any) => x.assignment_id === id);
  const takesSubmission = A.completion_mode !== "LEARNING_COMPLETION";
  const subsByStudent: Record<string, any[]> = {};
  for (const s of subs.data?.submissions || []) (subsByStudent[s.student_id] ||= []).push(s);
  const cohorts = p ? [...p.cohorts.completed, ...p.cohorts.in_progress, ...p.cohorts.not_started] : [];
  return (
    <div className="stack">
      <PageHeader
        title={A.title}
        subtitle={<>{relativeDue(A.due_at)} · {label(A.completion_mode)}{A.max_marks ? ` · out of ${A.max_marks} (${A.marks_visibility === "STUDENT_VISIBLE" ? "students see marks" : "teacher only"})` : ""}</>}
        actions={
          <span className="row gap">
            {A.max_marks && A.marks_visibility === "STUDENT_VISIBLE" ? (
              <Button variant="secondary" busy={a.busy} onClick={() => a.run(async () => { await post(`/v1/assignments/${id}/marks/release`); await subs.reload(); })}>Release marks</Button>
            ) : null}
            {lessonId ? <Link className="btn btn-ghost" to={`/teacher/lessons/${lessonId}`}>Lesson</Link> : null}
          </span>
        }
      />
      <ErrorNote error={a.error} />
      {p ? (
        <div className="stats">
          <div className="stat"><div className="stat-value">{p.counts.completed}</div><div className="stat-label">Completed</div></div>
          <div className="stat"><div className="stat-value">{p.counts.in_progress}</div><div className="stat-label">In progress</div></div>
          <div className="stat"><div className="stat-value">{p.counts.not_started}</div><div className="stat-label">Not started</div></div>
        </div>
      ) : null}
      <Card title="Students" actions={<span className="muted small">Alphabetical, never ranked</span>}>
        {!cohorts.length ? <Empty>No students yet.</Empty> : null}
        <ul className="list">
          {cohorts
            .sort((x: any, y: any) => x.display_name.localeCompare(y.display_name))
            .map((s: any) => {
              const ss = subsByStudent[s.student_id] || [];
              const latest = ss[ss.length - 1];
              return (
                <li key={s.student_assignment_id} className="list-item">
                  <div className="row between wrap gap">
                    <span>
                      <strong>{s.display_name}</strong>{" "}
                      <Badge value={s.is_late && s.status !== "NOT_STARTED" ? "SUBMITTED_LATE" : s.is_overdue ? "OVERDUE" : s.status} />
                    </span>
                    <span className="row gap wrap">
                      {takesSubmission && latest ? (
                        <span className="small">
                          Submitted {fmtDate(latest.submitted_at)}{" "}
                          {latest.files.map((f: any) => <a key={f.file_name} href={fileUrl(f.download_url) || "#"} target="_blank" rel="noreferrer">{f.file_name} </a>)}
                        </span>
                      ) : null}
                      {A.max_marks && latest ? <MarkCell sub={latest} out_of={A.max_marks} onSaved={subs.reload} /> : null}
                      <button className="link small" onClick={() => setOpen(open === s.student_assignment_id ? null : s.student_assignment_id)}>Feedback</button>
                    </span>
                  </div>
                  {latest?.student_note ? <p className="small muted">"{latest.student_note}"</p> : null}
                  {open === s.student_assignment_id ? <FeedbackBox sa={s.student_assignment_id} onClose={() => setOpen(null)} /> : null}
                </li>
              );
            })}
        </ul>
      </Card>
    </div>
  );
}

export function ReviewQueue() {
  const { data, error, loading, reload } = useApi<any>("/v1/teacher/review-items");
  const a = useAction();
  if (loading && !data) return <Spinner />;
  const act = (id: string, path: string, body?: any) => a.run(async () => { await post(`/v1/teacher-review-items/${id}/${path}`, body); await reload(); });
  return (
    <div className="stack">
      <PageHeader title="Needs your attention" subtitle="Answers the AI wasn't sure about, and submitted work." />
      <ErrorNote error={error || a.error} onRetry={reload} />
      {!data?.items?.length ? <Card><Empty>Nothing waiting.</Empty></Card> : null}
      {data?.items?.map((i: any) => {
        const snap = i.ai_evaluation_snapshot || {};
        return (
          <Card key={i.review_item_id} title={`${i.student_name} · ${i.assignment_title || ""}`} actions={<Badge value="NEEDS_REVIEW" text={label(i.reason_type)} />}>
            {i.submission_id ? (
              <p>Submitted work{snap.late ? " (late)" : ""} · {snap.files ?? 0} file(s). Open the assignment to view files, mark and give feedback.</p>
            ) : (
              <div className="stack-sm">
                <p className="question small-q">{i.prompt_text}</p>
                {snap.answer ? <blockquote>{snap.answer}</blockquote> : null}
                {snap.evaluation ? <p className="small muted">AI suggested: {label(snap.evaluation)}{snap.confidence != null ? ` (confidence ${Math.round(snap.confidence * 100)}%)` : ""}</p> : null}
              </div>
            )}
            <div className="row gap wrap top-gap">
              {!i.submission_id ? (
                <>
                  {["CORRECT", "PARTIAL", "INCORRECT"].map((e) => (
                    <Button key={e} variant="secondary" busy={a.busy} onClick={() => act(i.review_item_id, "adjust", { evaluation: e })}>{label(e)}</Button>
                  ))}
                  {snap.evaluation ? <Button variant="ghost" busy={a.busy} onClick={() => act(i.review_item_id, "confirm")}>Agree with AI</Button> : null}
                </>
              ) : (
                <Button variant="secondary" busy={a.busy} onClick={() => act(i.review_item_id, "confirm")}>Mark as reviewed</Button>
              )}
            </div>
          </Card>
        );
      })}
    </div>
  );
}
