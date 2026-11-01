import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { del, post } from "../lib/api";
import { label } from "../lib/format";
import { useApi, usePolling } from "../lib/hooks";
import ContentUploader, { type AddedItem } from "./ContentUploader";
import { Badge, Button, Card, Empty, ErrorNote, Field, Note, PageHeader, Spinner, useAction } from "./ui";

type Source = "PARENT" | "SELF";
const base = (s: Source) => (s === "PARENT" ? "/v1/parent-assignments" : "/v1/self-assignments");

/** Create parent-assigned or self-chosen learning: material -> subject -> spaced or one-time -> prepare. */
export function FamilyCreate({ source, studentId, subjectsPath, backTo, detailTo }: {
  source: Source;
  studentId?: string;
  subjectsPath: string;
  backTo: string;
  detailTo: (id: string) => string;
}) {
  const nav = useNavigate();
  const subjects = useApi<any>(subjectsPath);
  const [items, setItems] = useState<AddedItem[]>([]);
  const [form, setForm] = useState({ title: "", subject_id: "", instructions: "", due_at: "", review_mode: "SPACED" });
  const a = useAction();
  const set = (k: string, v: string) => setForm({ ...form, [k]: v });
  async function create() {
    const r = await a.run(() =>
      post<any>(base(source), {
        ...(source === "PARENT" ? { student_id: studentId } : {}),
        subject_id: form.subject_id,
        title: form.title,
        instructions: form.instructions || null,
        content_item_ids: items.map((i) => i.content_item_id),
        due_at: form.due_at ? new Date(form.due_at).toISOString() : null,
        review_mode: form.review_mode,
      }),
    );
    if (r) nav(detailTo(r.assignment_id));
  }
  return (
    <div className="stack narrow">
      <PageHeader title={source === "PARENT" ? "Assign learning" : "Learn something new"} subtitle="Aayra turns your material into a lesson with a short check." />
      <Card title="1. Material">
        <ContentUploader purpose={source} studentId={studentId} onAdded={(i) => setItems((x) => [...x, i])} />
        {items.length ? (
          <ul className="list top-gap">
            {items.map((i) => (
              <li key={i.content_item_id} className="list-item row between">
                <span>{i.title}</span>
                <button className="link small" onClick={() => setItems(items.filter((x) => x !== i))}>Remove</button>
              </li>
            ))}
          </ul>
        ) : null}
      </Card>
      <Card title="2. Details">
        <div className="stack">
          <Field label="Title"><input value={form.title} onChange={(e) => set("title", e.target.value)} placeholder="e.g. Photosynthesis practice" /></Field>
          <Field label="Subject">
            <select value={form.subject_id} onChange={(e) => set("subject_id", e.target.value)}>
              <option value="">Choose...</option>
              {subjects.data?.subjects?.map((s: any) => <option key={s.subject_id} value={s.subject_id}>{s.name}</option>)}
            </select>
          </Field>
          {source === "PARENT" ? (
            <Field label="Note for your child (optional)"><textarea rows={2} value={form.instructions} onChange={(e) => set("instructions", e.target.value)} /></Field>
          ) : null}
          <Field label="Due date (optional)"><input type="datetime-local" value={form.due_at} onChange={(e) => set("due_at", e.target.value)} /></Field>
          <fieldset className="radio-group">
            <legend className="field-label">After the lesson</legend>
            <label className="check"><input type="radio" checked={form.review_mode === "SPACED"} onChange={() => set("review_mode", "SPACED")} /> Spaced reviews (day 2, 4, 8, 16...) so it sticks</label>
            <label className="check"><input type="radio" checked={form.review_mode === "ONE_TIME"} onChange={() => set("review_mode", "ONE_TIME")} /> One-time learning only</label>
          </fieldset>
          <Note>Progress here is kept separate from school marks and school mastery.</Note>
          <ErrorNote error={a.error} />
          <div className="row gap">
            <Button onClick={create} busy={a.busy} disabled={!items.length || !form.title.trim() || !form.subject_id}>Prepare lesson</Button>
            <Button variant="ghost" onClick={() => nav(backTo)}>Cancel</Button>
          </div>
        </div>
      </Card>
    </div>
  );
}

export function FamilyDetail({ source, id, studentId, backTo }: { source: Source; id: string; studentId?: string; backTo: string }) {
  const path = `${base(source)}/${id}`;
  const { data, error, loading, reload } = useApi<any>(path);
  const a = useAction();
  const [adding, setAdding] = useState(false);
  usePolling(data?.status === "PREPARING", reload);
  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote error={error} onRetry={reload} />;
  const draft = data.status !== "ASSIGNED";
  return (
    <div className="stack narrow">
      <PageHeader title={data.title} subtitle={<Badge value={data.status} />} />
      <ErrorNote error={a.error || error} />
      {data.status === "PREPARING" ? <Card><Spinner text="Preparing the lesson: reading the material and writing questions..." /></Card> : null}
      {data.status === "FAILED" ? <Note tone="bad">A file couldn't be read. Remove it or add another, and Aayra will try again.</Note> : null}
      {data.preview ? (
        <Card title="What Aayra understood">
          <p className="muted small">{data.preview.check_questions} check questions on these topics:</p>
          <div className="chips">{data.preview.topics.map((t: string) => <span key={t} className="chip static">{t}</span>)}</div>
        </Card>
      ) : null}
      <Card title="Material">
        <ul className="list">
          {data.content.map((c: any) => (
            <li key={c.content_item_id} className="list-item row between">
              <span>{c.title || c.content_type} <span className="muted small">{label(c.processing_status)}</span></span>
              {draft && data.content.length > 1 ? (
                <button
                  className="link small"
                  onClick={() => a.run(async () => { await del(`${path}/content/${c.content_item_id}?version=${data.version}`); await reload(); })}
                >
                  Remove
                </button>
              ) : null}
            </li>
          ))}
        </ul>
        {draft ? (
          adding ? (
            <ContentUploader
              purpose={source}
              studentId={studentId}
              onAdded={(i) => a.run(async () => { await post(`${path}/content`, { content_item_id: i.content_item_id, version: data.version }); setAdding(false); await reload(); })}
            />
          ) : (
            <button className="link small" onClick={() => setAdding(true)}>+ Add material</button>
          )
        ) : (
          <p className="muted small">Assigned work is locked. Create a new one to change the material.</p>
        )}
      </Card>
      {data.status === "READY_TO_ASSIGN" ? (
        <Button busy={a.busy} onClick={() => a.run(async () => { await post(`${path}/assign`, { version: data.version }); await reload(); })}>
          {source === "PARENT" ? "Assign to my child" : "Start learning it"}
        </Button>
      ) : null}
      {data.status === "ASSIGNED" ? <Note tone="good">{source === "PARENT" ? "Assigned. It's on your child's home screen." : "Added to your home screen."}</Note> : null}
      <a className="small" href={backTo}>Back</a>
    </div>
  );
}

export function FamilyList({ items, to }: { items: any[]; to: (id: string) => string }) {
  const nav = useNavigate();
  if (!items.length) return <Empty>Nothing yet.</Empty>;
  return (
    <ul className="list">
      {items.map((m) => (
        <li key={m.assignment_id} className="list-item">
          <button className="list-button row between" onClick={() => nav(to(m.assignment_id))}>
            <span>{m.title}</span>
            <Badge value={m.state} />
          </button>
        </li>
      ))}
    </ul>
  );
}
