import { useState } from "react";
import { Badge, Button, Card, Empty, ErrorNote, Field, Note, PageHeader, Secret, Spinner, useAction } from "../../components/ui";
import { post } from "../../lib/api";
import { fmtDate } from "../../lib/format";
import { useApi } from "../../lib/hooks";
import { allSections, LoginsTable, useSchoolId, useStructure } from "./Admin";

export function Students() {
  const st = useStructure();
  const sections = allSections(st.data);
  const [sectionId, setSectionId] = useState("");
  const current = sectionId || sections[0]?.section_id || "";
  const roster = useApi<any>(current ? `/v1/sections/${current}/students` : null, [current]);
  const [form, setForm] = useState({ display_name: "", admission_id: "", date_of_birth: "", email: "" });
  const [created, setCreated] = useState<any[]>([]);
  const [action, setAction] = useState<{ id: string; kind: "transfer" | "withdraw" | "guardian" } | null>(null);
  const [extra, setExtra] = useState<Record<string, string>>({});
  const [reset, setReset] = useState<Record<string, string>>({});
  const a = useAction();
  if (st.loading && !st.data) return <Spinner />;
  if (!sections.length) return <Card><Empty>Create a class first (Classes &amp; subjects).</Empty></Card>;
  async function add() {
    const r = await a.run(() => post<any>("/v1/students", { section_id: current, ...form, email: form.email || null }));
    if (r) {
      setCreated([...created, { display_name: r.display_name, username: r.username, email: form.email || null, temporary_password: r.temporary_password }]);
      setForm({ display_name: "", admission_id: "", date_of_birth: "", email: "" });
      roster.reload();
    }
  }
  const doAction = (fn: () => Promise<any>) => a.run(async () => { await fn(); setAction(null); setExtra({}); await roster.reload(); });
  return (
    <div className="stack">
      <PageHeader
        title="Students"
        actions={
          <select value={current} onChange={(e) => setSectionId(e.target.value)} aria-label="Class">
            {sections.map((s) => <option key={s.section_id} value={s.section_id}>{s.grade_name} · {s.name}</option>)}
          </select>
        }
      />
      <ErrorNote error={a.error} />
      <Card title="Add a student" actions={<span className="muted small">Mid-year joiners get the class's current work automatically.</span>}>
        <div className="grid2">
          <Field label="Name"><input value={form.display_name} onChange={(e) => setForm({ ...form, display_name: e.target.value })} /></Field>
          <Field label="Admission id"><input value={form.admission_id} onChange={(e) => setForm({ ...form, admission_id: e.target.value })} /></Field>
          <Field label="Date of birth"><input type="date" value={form.date_of_birth} onChange={(e) => setForm({ ...form, date_of_birth: e.target.value })} /></Field>
          <Field label="Email (optional)" hint="Without email, the student gets a username and a temporary password."><input type="email" value={form.email} onChange={(e) => setForm({ ...form, email: e.target.value })} /></Field>
        </div>
        <Button className="top-gap" busy={a.busy} disabled={!form.display_name || !form.admission_id || !form.date_of_birth} onClick={add}>Add student</Button>
        {created.length ? <div className="top-gap"><LoginsTable logins={created} /></div> : null}
      </Card>
      <Card title={`Class list (${roster.data?.students?.length ?? 0})`}>
        {roster.loading && !roster.data ? <Spinner /> : null}
        {!roster.data?.students?.length && !roster.loading ? <Empty>No students in this class yet.</Empty> : null}
        <ul className="list">
          {roster.data?.students?.map((s: any) => (
            <li key={s.student_id} className="list-item">
              <div className="row between wrap gap">
                <span>
                  <strong>{s.display_name}</strong> <span className="muted small">{s.admission_id} · {s.email || "username login"}</span>{" "}
                  {s.guardian_status !== "LINKED" ? <Badge value="PROCESSING" text="Guardian pending" /> : null}
                  {s.account_status !== "ACTIVE" ? <Badge value="PROCESSING" text="Not signed in yet" /> : null}
                </span>
                <span className="row gap-sm wrap">
                  <button className="link small" onClick={() => setAction({ id: s.student_id, kind: "guardian" })}>Link parent</button>
                  <button className="link small" onClick={() => setAction({ id: s.student_id, kind: "transfer" })}>Transfer</button>
                  {!s.email ? <button className="link small" onClick={() => a.run(async () => { const r = await post<any>(`/v1/students/${s.student_id}/reset-login`); setReset({ ...reset, [s.student_id]: r.temporary_password }); })}>Reset login</button> : null}
                  <button className="link small danger" onClick={() => setAction({ id: s.student_id, kind: "withdraw" })}>Withdraw</button>
                </span>
              </div>
              {reset[s.student_id] ? <Note tone="warn">New temporary password: <Secret value={reset[s.student_id]} /></Note> : null}
              {action?.id === s.student_id && action?.kind === "transfer" ? (
                <div className="editor row gap wrap">
                  <select value={extra.section || ""} onChange={(e) => setExtra({ section: e.target.value })} aria-label="Move to class">
                    <option value="">Move to...</option>
                    {sections.filter((x) => x.section_id !== current && x.academic_year_id === sections.find((y) => y.section_id === current)?.academic_year_id).map((x) => <option key={x.section_id} value={x.section_id}>{x.grade_name} · {x.name}</option>)}
                  </select>
                  <Button busy={a.busy} disabled={!extra.section} onClick={() => doAction(() => post(`/v1/students/${s.student_id}/transfer`, { section_id: extra.section }))}>Transfer</Button>
                  <Button variant="ghost" onClick={() => setAction(null)}>Cancel</Button>
                  <span className="muted small">History is kept; in-progress work continues.</span>
                </div>
              ) : null}
              {action?.id === s.student_id && action?.kind === "withdraw" ? (
                <div className="editor row gap wrap">
                  <input placeholder="Reason (optional)" value={extra.reason || ""} onChange={(e) => setExtra({ reason: e.target.value })} aria-label="Reason" />
                  <Button variant="danger" busy={a.busy} onClick={() => doAction(() => post(`/v1/students/${s.student_id}/withdraw`, { reason: extra.reason || null }))}>Withdraw student</Button>
                  <Button variant="ghost" onClick={() => setAction(null)}>Cancel</Button>
                  <span className="muted small">Access stops; records are kept.</span>
                </div>
              ) : null}
              {action?.id === s.student_id && action?.kind === "guardian" ? (
                <div className="editor stack">
                  <div className="grid2">
                    <Field label="Parent's name"><input value={extra.name || ""} onChange={(e) => setExtra({ ...extra, name: e.target.value })} /></Field>
                    <Field label="Email"><input type="email" value={extra.email || ""} onChange={(e) => setExtra({ ...extra, email: e.target.value })} /></Field>
                    <Field label="Relationship">
                      <select value={extra.rel || "MOTHER"} onChange={(e) => setExtra({ ...extra, rel: e.target.value })}>
                        {["MOTHER", "FATHER", "LEGAL_GUARDIAN", "OTHER"].map((r) => <option key={r} value={r}>{r.replace("_", " ").toLowerCase()}</option>)}
                      </select>
                    </Field>
                    <Field label="Contact">
                      <select value={extra.prio || "PRIMARY"} onChange={(e) => setExtra({ ...extra, prio: e.target.value })}>
                        {["PRIMARY", "SECONDARY", "TERTIARY"].map((r) => <option key={r} value={r}>{r.toLowerCase()}</option>)}
                      </select>
                    </Field>
                  </div>
                  <div className="row gap">
                    <Button busy={a.busy} disabled={!extra.name || !extra.email} onClick={() => doAction(() => post(`/v1/students/${s.student_id}/guardians`, { email: extra.email, display_name: extra.name, relationship_type: extra.rel || "MOTHER", guardian_priority: extra.prio || "PRIMARY" }))}>Link and invite</Button>
                    <Button variant="ghost" onClick={() => setAction(null)}>Cancel</Button>
                  </div>
                </div>
              ) : null}
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

const SAMPLE = "admission_id,display_name,date_of_birth,section,email\n10A-101,Lina Park,2011-04-02,10A,\n10A-102,Omar Said,2011-05-03,10A,omar@example.com\n";

export function Import() {
  const sid = useSchoolId();
  const [text, setText] = useState("");
  const [fileName, setFileName] = useState("students.csv");
  const [result, setResult] = useState<any>(null);
  const [committed, setCommitted] = useState<any>(null);
  const a = useAction();
  return (
    <div className="stack narrow">
      <PageHeader title="Import students" subtitle="Check the file first; valid rows are added even if some rows have problems." />
      <Card title="1. Choose a CSV file">
        <p className="muted small">Columns: admission_id, display_name, date_of_birth (YYYY-MM-DD), section (e.g. 10A), email (optional). Up to 500 rows.</p>
        <input type="file" accept=".csv,text/csv" onChange={async (e) => { const f = e.target.files?.[0]; if (f) { setFileName(f.name); setText(await f.text()); setResult(null); setCommitted(null); } }} aria-label="CSV file" />
        <a className="small" href={`data:text/csv;charset=utf-8,${encodeURIComponent(SAMPLE)}`} download="aayra-students-template.csv">Download a template</a>
        <Field label="Or paste the CSV"><textarea rows={6} value={text} onChange={(e) => { setText(e.target.value); setResult(null); }} /></Field>
        <ErrorNote error={a.error} />
        <Button busy={a.busy} disabled={!text.trim()} onClick={async () => setResult(await a.run(() => post(`/v1/schools/${sid}/student-imports`, { file_name: fileName, csv_text: text })))}>Check file</Button>
      </Card>
      {result ? (
        <Card title="2. Check the results">
          <p><strong>{result.valid_rows}</strong> of {result.total_rows} rows are ready. {result.error_rows ? `${result.error_rows} have problems:` : ""}</p>
          {result.errors.length ? (
            <table className="table">
              <thead><tr><th>Row</th><th>Admission id</th><th>Problem</th></tr></thead>
              <tbody>{result.errors.map((e: any) => <tr key={e.row_number}><td>{e.row_number}</td><td>{e.admission_id}</td><td>{e.message}</td></tr>)}</tbody>
            </table>
          ) : null}
          {!committed ? (
            <Button className="top-gap" busy={a.busy} disabled={!result.valid_rows} onClick={async () => setCommitted(await a.run(() => post(`/v1/student-imports/${result.batch_id}/commit`)))}>
              Add {result.valid_rows} student{result.valid_rows === 1 ? "" : "s"}
            </Button>
          ) : null}
        </Card>
      ) : null}
      {committed ? (
        <Card title={`3. Added ${committed.created} students`}>
          <LoginsTable logins={committed.logins} />
        </Card>
      ) : null}
    </div>
  );
}

export function Rollover() {
  const st = useStructure();
  const years = st.data?.academic_years || [];
  const active = years.find((y: any) => y.status === "ACTIVE");
  const planned = years.filter((y: any) => y.status === "PLANNED");
  const [to, setTo] = useState("");
  const [plan, setPlan] = useState<any>(null);
  const [overrides, setOverrides] = useState<Record<string, string>>({});
  const [done, setDone] = useState<any>(null);
  const a = useAction();
  const target = to || planned[0]?.academic_year_id || "";
  const newSections = allSections({ academic_years: planned.filter((y: any) => y.academic_year_id === target) }, false);
  const body = (dry: boolean) => ({
    to_academic_year_id: target,
    dry_run: dry,
    mappings: Object.entries(overrides).map(([from, toSec]) => ({ from_section_id: from, to_section_id: toSec === "COMPLETE" ? null : toSec })),
  });
  if (st.loading && !st.data) return <Spinner />;
  return (
    <div className="stack narrow">
      <PageHeader title="Start the new academic year" subtitle="Students move up to the next grade's matching class; last year becomes read-only history." />
      {!active ? <Note tone="warn">There's no active year to roll over.</Note> : null}
      {active && !planned.length ? <Note>First create next year (status "Planned") with its grades, classes and subjects under Classes &amp; subjects.</Note> : null}
      {active && planned.length ? (
        <Card title={`From ${active.name} to...`}>
          <select value={target} onChange={(e) => { setTo(e.target.value); setPlan(null); }} aria-label="New year">
            {planned.map((y: any) => <option key={y.academic_year_id} value={y.academic_year_id}>{y.name} (starts {fmtDate(y.start_date)})</option>)}
          </select>
          <ErrorNote error={a.error} />
          <Button className="top-gap" variant="secondary" busy={a.busy} onClick={async () => setPlan(await a.run(() => post(`/v1/academic-years/${active.academic_year_id}/rollover`, body(true))))}>Preview</Button>
        </Card>
      ) : null}
      {plan && !done ? (
        <Card title="Preview">
          <table className="table">
            <thead><tr><th>Class now</th><th>Students</th><th>Goes to</th></tr></thead>
            <tbody>
              {plan.moves.map((m: any) => (
                <tr key={m.from_section_id}>
                  <td>{m.from_section}</td>
                  <td>{m.students}</td>
                  <td>
                    <select value={overrides[m.from_section_id] || m.to_section_id || "COMPLETE"} onChange={(e) => { setOverrides({ ...overrides, [m.from_section_id]: e.target.value }); }} aria-label={`Destination for ${m.from_section}`}>
                      <option value="COMPLETE">Finish (leave the school)</option>
                      {newSections.map((s) => <option key={s.section_id} value={s.section_id}>{s.grade_name} · {s.name}</option>)}
                    </select>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
          {plan.problems.map((p: any, i: number) => <Note key={i} tone="warn">{p.section}: {p.problem}</Note>)}
          <p className="small muted">{plan.open_assignments_to_close} open assignment(s) will close with the year. Teacher class assignments end; set next year's in Teachers.</p>
          <div className="row gap">
            <Button variant="secondary" busy={a.busy} onClick={async () => setPlan(await a.run(() => post(`/v1/academic-years/${active.academic_year_id}/rollover`, body(true))))}>Update preview</Button>
            <Button busy={a.busy} disabled={!plan.can_apply && !Object.keys(overrides).length} onClick={async () => setDone(await a.run(() => post(`/v1/academic-years/${active.academic_year_id}/rollover`, body(false))))}>Start the new year</Button>
          </div>
        </Card>
      ) : null}
      {done && !done.dry_run ? <Note tone="good">Done: {done.promoted} students moved up, {done.completed} finished, {done.assignments_closed} assignments closed.</Note> : null}
      {done && done.dry_run ? <Note tone="warn">Some classes still need a destination. Choose them above, then try again.</Note> : null}
    </div>
  );
}
