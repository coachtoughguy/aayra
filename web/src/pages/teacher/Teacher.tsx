import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import ContentUploader, { type AddedItem } from "../../components/ContentUploader";
import { Badge, Button, Card, Empty, ErrorNote, Field, PageHeader, Spinner, Stat, useAction } from "../../components/ui";
import { post } from "../../lib/api";
import { relativeDue } from "../../lib/format";
import { useApi } from "../../lib/hooks";

export function useTeacherHome() {
  return useApi<any>("/v1/teacher/home");
}

export default function TeacherHome() {
  const { data, error, loading, reload } = useTeacherHome();
  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote error={error} onRetry={reload} />;
  const toReview = data.lessons.filter((l: any) => l.status === "READY");
  return (
    <div className="stack">
      <PageHeader title="Teacher home" subtitle={`${data.school_name}${data.active_year ? ` · ${data.active_year.name}` : ""}`} actions={<Link className="btn btn-primary" to="/teacher/lessons/new">New lesson</Link>} />
      <div className="stats">
        <Link to="/teacher/review" className="stat link-card"><div className={`stat-value ${data.needs_attention ? "warn-text" : ""}`}>{data.needs_attention}</div><div className="stat-label">Need your attention</div></Link>
        <Stat value={toReview.length} label="Lessons ready to review" />
        <Stat value={data.teaching.filter((t: any) => t.subject_id).length} label="Classes you teach" />
      </div>
      <Card title="Your classes">
        <ul className="list">
          {data.teaching.map((t: any, i: number) => (
            <li key={i} className="list-item row between">
              <span><strong>{t.section_name}</strong> {t.subject_name || "Class teacher"}</span>
              <span className="muted small">{t.students} students · {t.assignment_role.replace(/_/g, " ").toLowerCase()}</span>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Lessons">
        {!data.lessons.length ? <Empty>No lessons yet. Upload your material to create the first one.</Empty> : null}
        <ul className="list">
          {data.lessons.map((l: any) => (
            <li key={l.lesson_id} className="list-item">
              <Link to={`/teacher/lessons/${l.lesson_id}`} className="list-button row between">
                <span>{l.title} <span className="muted small">{l.subject_name}</span></span>
                <span className="row gap-sm"><Badge value={l.status} />{l.assignments ? <span className="muted small">{l.assignments} assigned</span> : null}</span>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

export function NewLesson() {
  const nav = useNavigate();
  const { data } = useTeacherHome();
  const [items, setItems] = useState<AddedItem[]>([]);
  const [title, setTitle] = useState("");
  const [subject, setSubject] = useState("");
  const [instructions, setInstructions] = useState("");
  const a = useAction();
  const subjects = Array.from(new Map((data?.teaching || []).filter((t: any) => t.subject_id).map((t: any) => [t.subject_id, t])).values()) as any[];
  async function create() {
    const s = subjects.find((x) => x.subject_id === subject);
    const lesson = await a.run(async () => {
      const l = await post<any>("/v1/lessons", { subject_id: subject, academic_year_id: s.academic_year_id, title, instructions: instructions || null });
      let order = 1;
      for (const it of items) await post(`/v1/lessons/${l.lesson_id}/content`, { content_item_id: it.content_item_id, display_order: order++ });
      return l;
    });
    if (lesson) nav(`/teacher/lessons/${lesson.lesson_id}`);
  }
  return (
    <div className="stack narrow">
      <PageHeader title="New lesson" subtitle="Upload your normal material. Aayra reads it and prepares topics and questions for you to review." />
      <Card title="1. Material">
        <ContentUploader purpose="SCHOOL" onAdded={(i) => setItems((x) => [...x, i])} />
        {items.length ? <ul className="list top-gap">{items.map((i) => <li key={i.content_item_id} className="list-item">{i.title} <span className="muted small">{i.content_type.toLowerCase()}</span></li>)}</ul> : null}
      </Card>
      <Card title="2. Lesson">
        <div className="stack">
          <Field label="Subject">
            <select value={subject} onChange={(e) => setSubject(e.target.value)}>
              <option value="">Choose...</option>
              {subjects.map((s) => <option key={s.subject_id} value={s.subject_id}>{s.subject_name}</option>)}
            </select>
          </Field>
          <Field label="Title"><input value={title} onChange={(e) => setTitle(e.target.value)} placeholder="e.g. Photosynthesis" /></Field>
          <Field label="Instructions for students (optional)"><textarea rows={3} value={instructions} onChange={(e) => setInstructions(e.target.value)} /></Field>
          <ErrorNote error={a.error} />
          <Button onClick={create} busy={a.busy} disabled={!items.length || !title.trim() || !subject}>Create lesson</Button>
        </div>
      </Card>
    </div>
  );
}

export function TeacherAssignments() {
  const { data, error, loading, reload } = useApi<any>("/v1/teacher/assignments");
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title="Assignments" />
      <ErrorNote error={error} onRetry={reload} />
      <Card>
        {!data?.assignments?.length ? <Empty>Nothing published yet.</Empty> : null}
        <ul className="list">
          {data?.assignments?.map((a: any) => (
            <li key={a.assignment_id} className="list-item">
              <Link className="list-button row between wrap gap" to={`/teacher/assignments/${a.assignment_id}?lesson=${a.lesson_id}`}>
                <span>
                  <strong>{a.title}</strong> <span className="muted small">{a.section_name} · {a.subject_name}</span>
                  <div className="muted small">{relativeDue(a.due_at)}{a.max_marks ? ` · out of ${a.max_marks}` : ""}</div>
                </span>
                <span className="row gap-sm"><span className="muted small">{a.done}/{a.students} done</span><Badge value={a.status} /></span>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}
