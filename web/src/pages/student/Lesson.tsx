import { useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import TutorPanel from "../../components/TutorPanel";
import { Badge, Button, Card, Empty, ErrorNote, Field, Note, PageHeader, Spinner, useAction } from "../../components/ui";
import { ApiError, fileUrl, get, post, uploadToSignedUrl } from "../../lib/api";
import { fmtDate, relativeDue, SOURCE_LABEL, toDate } from "../../lib/format";
import { useApi } from "../../lib/hooks";

function Timer({ since }: { since: string }) {
  const [now, setNow] = useState(Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const start = toDate(since)?.getTime() || now;
  const s = Math.max(0, Math.floor((now - start) / 1000));
  const mm = String(Math.floor(s / 60)).padStart(2, "0");
  const ss = String(s % 60).padStart(2, "0");
  return <span className="timer" aria-live="off">{mm}:{ss}</span>;
}

function Material({ m }: { m: any }) {
  const [open, setOpen] = useState(false);
  const url = fileUrl(m.download_url);
  return (
    <div className="material">
      <div className="row between">
        <div>
          <strong>{m.title || m.content_type}</strong> <span className="muted small">{m.content_type.toLowerCase().replace("_", " ")}</span>
        </div>
        {url ? <a href={url} target="_blank" rel="noreferrer" className="small">Open file</a> : null}
        {m.content_type === "WEB_LINK" && m.source_url ? <a href={m.source_url} target="_blank" rel="noreferrer" className="small">Open page</a> : null}
      </div>
      {m.youtube_id ? (
        <div className="video">
          <iframe
            src={`https://www.youtube-nocookie.com/embed/${m.youtube_id}`}
            title={m.title || "YouTube video"}
            allow="accelerometer; encrypted-media; gyroscope; picture-in-picture"
            allowFullScreen
          />
        </div>
      ) : null}
      {m.content_type === "VIDEO" && url ? <video className="media" controls src={url} /> : null}
      {m.content_type === "AUDIO" && url ? <audio className="media" controls src={url} /> : null}
      {(m.content_type === "IMAGE" || m.content_type === "PHOTO") && url ? <img className="media" src={url} alt={m.title || "Lesson image"} /> : null}
      {m.sections?.length ? (
        <>
          <button className="link small" onClick={() => setOpen(!open)}>{open ? "Hide text" : "Read the text"}</button>
          {open ? (
            <div className="reading">
              {m.sections.map((s: any, i: number) => (
                <p key={i} className={s.chunk_type === "TABLE" ? "pre" : ""}>
                  {s.start_seconds != null ? <span className="muted small">[{Math.floor(s.start_seconds / 60)}:{String(Math.floor(s.start_seconds % 60)).padStart(2, "0")}] </span> : null}
                  {s.page_number ? <span className="muted small">p.{s.page_number} </span> : null}
                  {s.text}
                </p>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </div>
  );
}

function Submission({ sa, lesson }: { sa: string; lesson: any }) {
  const { data, reload } = useApi<any>(`/v1/student-assignments/${sa}/submissions`);
  const [note, setNote] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const a = useAction();
  async function submit() {
    await a.run(async () => {
      const attachments = [];
      for (const f of files) {
        const up = await post<any>(`/v1/student-assignments/${sa}/submission-uploads`, { file_name: f.name, mime_type: f.type || "application/octet-stream" });
        await uploadToSignedUrl(up.upload_url, f);
        attachments.push({ object_key: up.object_key, file_name: f.name, mime_type: f.type || null });
      }
      await post(`/v1/student-assignments/${sa}/submissions`, { student_note: note || null, attachments });
      setNote("");
      setFiles([]);
      reload();
    });
  }
  return (
    <Card title="Your submission" actions={lesson.marks && lesson.marks.visibility === "STUDENT_VISIBLE" ? <span className="muted small">Marked out of {lesson.marks.out_of}</span> : null}>
      {data?.submissions?.length ? (
        <ul className="list">
          {data.submissions.map((s: any) => (
            <li key={s.submission_id} className="list-item">
              <div className="row between">
                <span>Attempt {s.attempt_number} · {fmtDate(s.submitted_at, true)}</span>
                <span className="row gap-sm">
                  {s.status === "SUBMITTED_LATE" ? <Badge value="SUBMITTED_LATE" /> : <Badge value="SUBMITTED" />}
                  {s.marks ? <Badge value="COMPLETED" text={`${s.marks.awarded} / ${s.marks.out_of}`} /> : null}
                </span>
              </div>
              {s.student_note ? <p className="small">{s.student_note}</p> : null}
              {s.files.map((f: any) => <a key={f.file_name} href={fileUrl(f.download_url) || "#"} target="_blank" rel="noreferrer" className="small">{f.file_name} </a>)}
            </li>
          ))}
        </ul>
      ) : null}
      <div className="stack">
        <Field label={data?.submissions?.length ? "Submit again (your earlier work is kept)" : "Add a note"}>
          <textarea rows={3} value={note} onChange={(e) => setNote(e.target.value)} />
        </Field>
        <input type="file" multiple onChange={(e) => setFiles(Array.from(e.target.files || []))} aria-label="Attach files" />
        <ErrorNote error={a.error} />
        <Button onClick={submit} busy={a.busy} disabled={!note.trim() && !files.length}>Submit</Button>
      </div>
    </Card>
  );
}

function Feedback({ sa }: { sa: string }) {
  const { data } = useApi<any>(`/v1/students/me/assignments/${sa}/feedback`);
  if (!data?.feedback?.length) return null;
  return (
    <Card title="Feedback from your teacher">
      {data.feedback.map((f: any) => (
        <div key={f.feedback_id} className="feedback">
          <p>{f.feedback_text}</p>
          <span className="muted small">{f.teacher_name} · {fmtDate(f.published_at)}</span>
        </div>
      ))}
    </Card>
  );
}

export default function StudentLesson() {
  const { sa = "" } = useParams();
  const nav = useNavigate();
  const { data: l, error, loading, reload } = useApi<any>(`/v1/student-assignments/${sa}/lesson`);
  const a = useAction();
  const [showTutor, setShowTutor] = useState(true);
  if (loading && !l) return <Spinner />;
  if (!l) return <ErrorNote error={error} onRetry={reload} />;

  const learning = l.learning.status;
  const finished = ["LEARNING_COMPLETE", "ASSESSING", "COMPLETED"].includes(learning);
  const cycle = l.learning.active_focus_cycle;

  async function start() {
    await a.run(async () => {
      if (l.status === "NOT_STARTED") await post(`/v1/student-assignments/${sa}/start`);
      try {
        await post(`/v1/lessons/${l.lesson_id}/focus-cycles`, { student_assignment_id: sa });
      } catch (e) {
        if (!(e instanceof ApiError && e.code === "FOCUS_CYCLE_ALREADY_ACTIVE")) throw e;
      }
      await reload();
    });
  }
  async function pause(reason: "BREAK" | "CONTINUE_LATER") {
    await a.run(async () => {
      await post(`/v1/focus-cycles/${cycle.focus_cycle_id}/end`, { end_reason: reason });
      await reload();
    });
  }
  async function finish() {
    await a.run(async () => {
      await post(`/v1/lessons/${l.lesson_id}/finish`, { student_assignment_id: sa });
      await reload();
    });
  }

  return (
    <div className={`lesson-layout ${showTutor ? "with-tutor" : ""}`}>
      <div className="stack">
        <PageHeader
          title={l.title}
          subtitle={<>{l.subject_name} · {relativeDue(l.due_at)} · <Badge value={l.source} text={SOURCE_LABEL[l.source]} /></>}
          actions={<Button variant="ghost" onClick={() => setShowTutor(!showTutor)}>{showTutor ? "Hide tutor" : "Ask the tutor"}</Button>}
        />
        {l.instructions ? <Note>{l.instructions}</Note> : null}
        <ErrorNote error={a.error} />
        <Card title="Learn">
          {!finished ? (
            cycle ? (
              <div className="focus">
                <div>
                  <div className="muted small">Focus time</div>
                  <Timer since={cycle.started_at} />
                </div>
                <div className="row gap wrap">
                  <Button variant="secondary" onClick={() => pause("BREAK")} busy={a.busy}>Take a break</Button>
                  <Button variant="secondary" onClick={() => pause("CONTINUE_LATER")} busy={a.busy}>Continue later</Button>
                  <Button onClick={finish} busy={a.busy}>I've finished the material</Button>
                </div>
              </div>
            ) : (
              <div className="row between wrap gap">
                <span className="muted">{l.learning.minutes ? `${l.learning.minutes} min so far.` : "Start when you're ready. Take breaks whenever you need."}</span>
                <Button onClick={start} busy={a.busy}>{l.learning.minutes || learning === "IN_PROGRESS" ? "Resume" : "Start learning"}</Button>
              </div>
            )
          ) : (
            <Note tone="good">You finished the material{l.learning.minutes ? ` (${l.learning.minutes} min)` : ""}.</Note>
          )}
          <div className="stack top-gap">
            {l.materials.length ? l.materials.map((m: any) => <Material key={m.content_item_id} m={m} />) : <Empty>No material attached.</Empty>}
          </div>
        </Card>
        {l.completion_mode !== "SUBMISSION_REQUIRED" ? (
          <Card title="Check your understanding">
            {l.check.completed_attempt_id ? (
              <div className="row between wrap gap">
                <span>You've done the check for this lesson.</span>
                <Link to={`/student/check/${sa}`} className="btn btn-secondary">See result</Link>
              </div>
            ) : finished ? (
              <div className="row between wrap gap">
                <span>A short check: a few questions, with hints if you get stuck.</span>
                <Button onClick={() => nav(`/student/check/${sa}`)}>{l.check.active_attempt_id ? "Continue the check" : "Start the check"}</Button>
              </div>
            ) : (
              <p className="muted">Finish the material first, then take a short check.</p>
            )}
          </Card>
        ) : null}
        {l.completion_mode !== "LEARNING_COMPLETION" ? <Submission sa={sa} lesson={l} /> : null}
        {l.feedback_count ? <Feedback sa={sa} /> : null}
      </div>
      {showTutor ? <TutorPanel lessonId={l.lesson_id} studentAssignmentId={sa} /> : null}
    </div>
  );
}

const starting = new Map<string, Promise<{ attemptId: string; next: any }>>();

/** Start the check, or resume the one already open. Concurrent calls share one request. */
export function startOrResumeCheck(sa: string): Promise<{ attemptId: string; next: any }> {
  if (!starting.has(sa)) {
    const p = startOnce(sa).finally(() => window.setTimeout(() => starting.delete(sa), 1000));
    starting.set(sa, p);
  }
  return starting.get(sa)!;
}

async function startOnce(sa: string, retried = false): Promise<{ attemptId: string; next: any }> {
  const l = await get<any>(`/v1/student-assignments/${sa}/lesson`);
  const existing = l.check.active_attempt_id || l.check.completed_attempt_id;
  if (existing) {
    const v = await get<any>(`/v1/assessment-attempts/${existing}`);
    if (v.status === "COMPLETED") return { attemptId: existing, next: { type: "COMPLETED", result: v.result } };
    if (v.current_question) return { attemptId: existing, next: { type: v.current_question.mode === "RETRY" ? "RETRY" : "QUESTION", question: v.current_question } };
    return { attemptId: existing, next: { type: "AWAITING_EVALUATION" } };
  }
  try {
    const a = await post<any>("/v1/assessment-attempts", { lesson_id: l.lesson_id, student_assignment_id: sa });
    return { attemptId: a.attempt_id, next: a.next };
  } catch (e) {
    if (!retried && e instanceof ApiError && e.status === 409 && e.code.startsWith("ASSESSMENT_ALREADY")) return startOnce(sa, true);
    throw e;
  }
}
