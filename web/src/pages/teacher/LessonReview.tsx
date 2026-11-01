import { useState } from "react";
import { Link, useParams } from "react-router-dom";
import { Badge, Button, Card, ErrorNote, Field, Note, PageHeader, Spinner, useAction } from "../../components/ui";
import { del, patch, post } from "../../lib/api";
import { label } from "../../lib/format";
import { useApi, usePolling } from "../../lib/hooks";
import { useTeacherHome } from "./Teacher";

const COG = ["RECALL", "UNDERSTANDING", "REASONING", "APPLICATION", "TRANSFER"];

function QuestionEditor({ q, gv, lessonId, onDone, conceptId }: { q?: any; gv: number; lessonId: string; onDone: () => void; conceptId?: string }) {
  const isNew = !q;
  const [type, setType] = useState<string>(q?.question_type || "MCQ");
  const [prompt, setPrompt] = useState(q?.prompt_text || "");
  const [options, setOptions] = useState<string[]>(q?.answer_spec?.options || ["", "", "", ""]);
  const [correct, setCorrect] = useState<number>(q?.answer_spec?.correct_index ?? 0);
  const [rubric, setRubric] = useState<string>((q?.answer_spec?.rubric || []).join("\n"));
  const [hint, setHint] = useState(q?.answer_spec?.hint || "");
  const [explanation, setExplanation] = useState(q?.answer_spec?.explanation || "");
  const [difficulty, setDifficulty] = useState(q?.difficulty || "MEDIUM");
  const [cog, setCog] = useState(q?.cognitive_type || "UNDERSTANDING");
  const a = useAction();
  async function save() {
    const answer = type === "MCQ"
      ? { options: options.map((o) => o.trim()).filter(Boolean), correct_index: correct }
      : { rubric: rubric.split("\n").map((r) => r.trim()).filter(Boolean) };
    const body: any = { generation_version: gv, prompt_text: prompt, difficulty, cognitive_type: cog, hint: hint || null, explanation: explanation || null, ...answer };
    const ok = await a.run(() =>
      isNew ? post(`/v1/lessons/${lessonId}/questions`, { ...body, concept_id: conceptId, question_type: type }) : patch(`/v1/lessons/${lessonId}/questions/${q.question_id}`, body),
    );
    if (ok) onDone();
  }
  return (
    <div className="editor stack">
      {isNew ? (
        <div className="seg">
          <button type="button" className={type === "MCQ" ? "on" : ""} onClick={() => setType("MCQ")}>Multiple choice</button>
          <button type="button" className={type === "FREE_TEXT" ? "on" : ""} onClick={() => setType("FREE_TEXT")}>Written answer</button>
        </div>
      ) : null}
      <Field label="Question"><textarea rows={2} value={prompt} onChange={(e) => setPrompt(e.target.value)} /></Field>
      {type === "MCQ" ? (
        <fieldset className="radio-group">
          <legend className="field-label">Options (select the correct one)</legend>
          {options.map((o, i) => (
            <div key={i} className="row gap-sm">
              <input type="radio" checked={correct === i} onChange={() => setCorrect(i)} aria-label={`Option ${i + 1} is correct`} />
              <input value={o} onChange={(e) => setOptions(options.map((x, j) => (j === i ? e.target.value : x)))} placeholder={`Option ${i + 1}`} />
            </div>
          ))}
        </fieldset>
      ) : (
        <Field label="Rubric: what a good answer includes" hint="One point per line."><textarea rows={3} value={rubric} onChange={(e) => setRubric(e.target.value)} /></Field>
      )}
      <div className="grid2">
        <Field label="Hint (after a first wrong answer)"><input value={hint} onChange={(e) => setHint(e.target.value)} /></Field>
        <Field label="Explanation (after a second wrong answer)"><input value={explanation} onChange={(e) => setExplanation(e.target.value)} /></Field>
        <Field label="Difficulty">
          <select value={difficulty} onChange={(e) => setDifficulty(e.target.value)}>{["EASY", "MEDIUM", "HARD"].map((d) => <option key={d}>{d}</option>)}</select>
        </Field>
        <Field label="Skill">
          <select value={cog} onChange={(e) => setCog(e.target.value)}>{COG.map((c) => <option key={c} value={c}>{label(c)}</option>)}</select>
        </Field>
      </div>
      <ErrorNote error={a.error} />
      <div className="row gap">
        <Button onClick={save} busy={a.busy}>Save</Button>
        <Button variant="ghost" onClick={onDone}>Cancel</Button>
      </div>
    </div>
  );
}

function PublishForm({ lesson, onDone }: { lesson: any; onDone: () => void }) {
  const th = useTeacherHome();
  const sections = (th.data?.teaching || []).filter((t: any) => t.subject_id === lesson.subject_id);
  const taken = new Set((lesson.assignments || []).map((a: any) => a.section_id));
  const [f, setF] = useState({ section_id: "", due_at: "", available_from: "", completion_mode: "LEARNING_COMPLETION", allow_late: true, marks: "", visibility: "TEACHER_ONLY", title: lesson.title, instructions: "" });
  const a = useAction();
  const set = (k: string, v: any) => setF({ ...f, [k]: v });
  async function publish() {
    const ok = await a.run(() =>
      post(`/v1/lessons/${lesson.lesson_id}/publish`, {
        version: lesson.version,
        reviewed_generation_version: lesson.generation.generation_version,
        target: { type: "SECTION", section_id: f.section_id },
        due_at: new Date(f.due_at).toISOString(),
        available_from: f.available_from ? new Date(f.available_from).toISOString() : null,
        allow_late_submission: f.allow_late,
        completion_mode: f.completion_mode,
        title: f.title,
        instructions: f.instructions || null,
        ...(f.marks && f.completion_mode !== "LEARNING_COMPLETION" ? { max_marks: Number(f.marks), marks_visibility: f.visibility } : {}),
      }),
    );
    if (ok) onDone();
  }
  return (
    <Card title={lesson.status === "READY" ? "Publish" : "Publish to another class"}>
      <div className="stack">
        <div className="grid2">
          <Field label="Class">
            <select value={f.section_id} onChange={(e) => set("section_id", e.target.value)}>
              <option value="">Choose...</option>
              {sections.map((s: any) => <option key={s.section_id} value={s.section_id} disabled={taken.has(s.section_id)}>{s.section_name}{taken.has(s.section_id) ? " (already assigned)" : ""}</option>)}
            </select>
          </Field>
          <Field label="Title for students"><input value={f.title} onChange={(e) => set("title", e.target.value)} /></Field>
          <Field label="Due"><input type="datetime-local" value={f.due_at} onChange={(e) => set("due_at", e.target.value)} /></Field>
          <Field label="Release later (optional)"><input type="datetime-local" value={f.available_from} onChange={(e) => set("available_from", e.target.value)} /></Field>
          <Field label="Students complete it by">
            <select value={f.completion_mode} onChange={(e) => set("completion_mode", e.target.value)}>
              <option value="LEARNING_COMPLETION">Learning + check</option>
              <option value="SUBMISSION_REQUIRED">Submitting work</option>
              <option value="BOTH">Both</option>
            </select>
          </Field>
          {f.completion_mode !== "LEARNING_COMPLETION" ? (
            <Field label="Marks (optional)" hint="Leave empty for no marks.">
              <div className="row gap-sm">
                <input type="number" min={1} value={f.marks} onChange={(e) => set("marks", e.target.value)} placeholder="Out of" />
                <select value={f.visibility} onChange={(e) => set("visibility", e.target.value)} aria-label="Who sees marks">
                  <option value="TEACHER_ONLY">Teacher only</option>
                  <option value="STUDENT_VISIBLE">Students see them</option>
                </select>
              </div>
            </Field>
          ) : null}
        </div>
        <label className="check"><input type="checkbox" checked={f.allow_late} onChange={(e) => set("allow_late", e.target.checked)} /> Accept late work (labelled "Submitted late")</label>
        <ErrorNote error={a.error} />
        <Button onClick={publish} busy={a.busy} disabled={!f.section_id || !f.due_at || !f.title}>Publish</Button>
      </div>
    </Card>
  );
}

export default function LessonReview() {
  const { id = "" } = useParams();
  const { data: l, error, loading, reload } = useApi<any>(`/v1/lessons/${id}`);
  const [editing, setEditing] = useState<string | null>(null);
  const [topicEdit, setTopicEdit] = useState<any>(null);
  const a = useAction();
  usePolling(l?.status === "PROCESSING", reload);
  if (loading && !l) return <Spinner />;
  if (!l) return <ErrorNote error={error} onRetry={reload} />;
  const g = l.generation;
  const gv = g?.generation_version;
  const editable = l.status === "READY";
  const run = (fn: () => Promise<any>) => a.run(async () => { await fn(); await reload(); });
  return (
    <div className="stack">
      <PageHeader title={l.title} subtitle={<Badge value={l.status} />} actions={<Link to="/teacher" className="btn btn-ghost">Back</Link>} />
      <ErrorNote error={a.error} />
      <Card title="Material">
        <ul className="list">
          {l.content.map((c: any) => (
            <li key={c.content_item_id} className="list-item row between wrap gap">
              <span>{c.title || c.content_type} <span className="muted small">{c.content_type.toLowerCase()}</span></span>
              <span className="row gap-sm">
                <Badge value={c.processing_status === "FAILED" ? "FAILED" : c.processing_status === "DONE" ? "COMPLETED" : "PROCESSING"} text={label(c.processing_status)} />
                {c.processing_status === "FAILED" ? (
                  <>
                    <button className="link small" onClick={() => run(() => post(`/v1/content-items/${c.content_item_id}/retry-processing`))}>Retry</button>
                    <button className="link small" onClick={() => run(() => del(`/v1/lessons/${id}/content/${c.content_item_id}`))}>Remove</button>
                  </>
                ) : null}
              </span>
            </li>
          ))}
        </ul>
      </Card>
      {l.status === "PROCESSING" ? <Card><Spinner text="Reading your material and preparing topics and questions..." /></Card> : null}
      {g && l.status !== "PROCESSING" ? (
        <Card
          title="Review what Aayra prepared"
          actions={editable ? <Button variant="ghost" busy={a.busy} onClick={() => run(() => post(`/v1/lessons/${id}/regenerate`, { version: l.version }))}>Regenerate</Button> : null}
        >
          {editable ? <Note>Check every question before publishing. Edit, add or remove anything; your changes are kept as a new version.</Note> : <Note tone="info">Published content is locked.</Note>}
          {g.concepts.map((c: any) => {
            const qs = g.questions.filter((q: any) => q.concept_id === c.concept_id);
            return (
              <div key={c.concept_id} className="topic">
                {topicEdit?.concept_id === c.concept_id ? (
                  <div className="editor row gap wrap">
                    <input value={topicEdit.name} onChange={(e) => setTopicEdit({ ...topicEdit, name: e.target.value })} aria-label="Topic name" />
                    <select value={topicEdit.importance} onChange={(e) => setTopicEdit({ ...topicEdit, importance: e.target.value })} aria-label="Importance">
                      {["CORE", "SUPPORTING", "STRETCH", "PREREQUISITE"].map((i) => <option key={i} value={i}>{label(i)}</option>)}
                    </select>
                    <Button onClick={() => run(async () => { await patch(`/v1/lessons/${id}/concepts/${c.concept_id}`, { generation_version: gv, name: topicEdit.name, importance: topicEdit.importance }); setTopicEdit(null); })}>Save</Button>
                    <Button variant="ghost" onClick={() => setTopicEdit(null)}>Cancel</Button>
                  </div>
                ) : (
                  <div className="row between wrap gap">
                    <h3>{c.name} <span className="muted small">{label(c.importance)}</span></h3>
                    {editable ? (
                      <span className="row gap-sm">
                        <button className="link small" onClick={() => setTopicEdit({ concept_id: c.concept_id, name: c.name, importance: c.importance })}>Edit topic</button>
                        {g.concepts.length > 1 ? <button className="link small danger" onClick={() => run(() => del(`/v1/lessons/${id}/concepts/${c.concept_id}?generation_version=${gv}`))}>Remove topic</button> : null}
                      </span>
                    ) : null}
                  </div>
                )}
                {qs.map((q: any) =>
                  editing === q.question_id ? (
                    <QuestionEditor key={q.question_id} q={q} gv={gv} lessonId={id} onDone={() => { setEditing(null); reload(); }} />
                  ) : (
                    <div key={q.question_id} className="question-card">
                      <div className="row between gap">
                        <div>
                          <p className="question small-q">{q.prompt_text}</p>
                          {q.question_type === "MCQ" ? (
                            <ol className="opts">{q.answer_spec.options.map((o: string, i: number) => <li key={i} className={i === q.answer_spec.correct_index ? "correct" : ""}>{o}</li>)}</ol>
                          ) : (
                            <ul className="opts">{(q.answer_spec.rubric || []).map((r: string, i: number) => <li key={i}>{r}</li>)}</ul>
                          )}
                          {q.answer_spec.hint ? <p className="muted small">Hint: {q.answer_spec.hint}</p> : null}
                        </div>
                        <div className="stack-sm right">
                          <Badge value={q.source === "AI" ? "PROCESSING" : "IN_PROGRESS"} text={q.source === "AI" ? "AI" : q.source === "TEACHER" ? "Yours" : "Edited"} />
                          {editable ? <button className="link small" onClick={() => setEditing(q.question_id)}>Edit</button> : null}
                          {editable && qs.length > 1 ? <button className="link small danger" onClick={() => run(() => del(`/v1/lessons/${id}/questions/${q.question_id}?generation_version=${gv}`))}>Remove</button> : null}
                        </div>
                      </div>
                    </div>
                  ),
                )}
                {editable ? (
                  editing === `new-${c.concept_id}` ? (
                    <QuestionEditor gv={gv} lessonId={id} conceptId={c.concept_id} onDone={() => { setEditing(null); reload(); }} />
                  ) : (
                    <button className="link small" onClick={() => setEditing(`new-${c.concept_id}`)}>+ Add a question</button>
                  )
                ) : null}
              </div>
            );
          })}
        </Card>
      ) : null}
      {["READY", "PUBLISHED", "ACTIVE"].includes(l.status) && g ? <PublishForm lesson={l} onDone={reload} /> : null}
      {l.assignments?.length ? (
        <Card title="Assigned to">
          <ul className="list">
            {l.assignments.map((x: any) => (
              <li key={x.assignment_id} className="list-item">
                <Link className="list-button row between" to={`/teacher/assignments/${x.assignment_id}?lesson=${id}`}>
                  <span>{x.target_type === "SECTION" ? "Class" : "Student"} · due {new Date(x.due_at).toLocaleDateString()}</span>
                  <Badge value={x.status} />
                </Link>
              </li>
            ))}
          </ul>
        </Card>
      ) : null}
    </div>
  );
}
