import { useState } from "react";
import { Link } from "react-router-dom";
import { Badge, Button, Card, Empty, ErrorNote, Field, Note, PageHeader, Secret, Spinner, Stat, useAction } from "../../components/ui";
import { post } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { fmtDate, label } from "../../lib/format";
import { useApi } from "../../lib/hooks";

export function useSchoolId(): string {
  const { me } = useAuth();
  return me?.schools.find((s) => s.roles.includes("SCHOOL_ADMIN"))?.school_id || "";
}

const NEEDS: Record<string, [string, string]> = {
  students_without_guardian: ["students without a linked parent/guardian", "/admin/students"],
  sections_without_class_teacher: ["classes without a class teacher", "/admin/teachers"],
  section_subjects_without_primary_teacher: ["class subjects without a primary teacher", "/admin/teachers"],
  invites_pending: ["people who haven't activated their login yet", "/admin/teachers"],
  imports_waiting: ["imports waiting to be committed", "/admin/import"],
};

export default function AdminHome() {
  const sid = useSchoolId();
  const { data, error, loading, reload } = useApi<any>(sid ? `/v1/schools/${sid}/admin/home` : null);
  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote error={error} onRetry={reload} />;
  return (
    <div className="stack">
      <PageHeader title={data.school.name} subtitle={data.active_year ? `Academic year ${data.active_year.name}` : "No active academic year yet"} />
      <div className="stats">
        <Stat value={data.people.active_students} label="Active students" />
        <Stat value={data.people.teachers} label="Teachers" />
      </div>
      <Card title="Needs attention">
        {!data.needs_attention.length ? <Empty>Everything is set up.</Empty> : null}
        <ul className="list">
          {data.needs_attention.map((n: any) => (
            <li key={n.key} className="list-item">
              <Link to={NEEDS[n.key]?.[1] || "/admin"} className="list-button row between">
                <span><strong>{n.count}</strong> {NEEDS[n.key]?.[0] || n.key}</span>
                <span aria-hidden>›</span>
              </Link>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Quick actions">
        <div className="row gap wrap">
          <Link className="btn btn-secondary" to="/admin/students">Add a student</Link>
          <Link className="btn btn-secondary" to="/admin/import">Import students</Link>
          <Link className="btn btn-secondary" to="/admin/teachers">Invite a teacher</Link>
          <Link className="btn btn-secondary" to="/admin/structure">Classes & subjects</Link>
        </div>
      </Card>
    </div>
  );
}

export function useStructure() {
  const sid = useSchoolId();
  return useApi<any>(sid ? `/v1/schools/${sid}/structure` : null, [sid]);
}

export function allSections(structure: any, activeOnly = true): any[] {
  const out: any[] = [];
  for (const y of structure?.academic_years || []) {
    if (activeOnly && y.status !== "ACTIVE") continue;
    for (const g of y.grades) for (const s of g.sections) out.push({ ...s, grade_name: g.name, year: y.name, academic_year_id: y.academic_year_id });
  }
  return out;
}

export function Structure() {
  const sid = useSchoolId();
  const { data, error, loading, reload } = useStructure();
  const a = useAction();
  const [year, setYear] = useState({ name: "", start_date: "", end_date: "", status: "PLANNED" });
  const [inputs, setInputs] = useState<Record<string, string>>({});
  const val = (k: string) => inputs[k] || "";
  const setVal = (k: string, v: string) => setInputs({ ...inputs, [k]: v });
  const run = (fn: () => Promise<any>, clear?: string[]) =>
    a.run(async () => {
      await fn();
      if (clear) setInputs((i) => ({ ...i, ...Object.fromEntries(clear.map((c) => [c, ""])) }));
      await reload();
    });
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title="Classes & subjects" />
      <ErrorNote error={error || a.error} onRetry={reload} />
      {data?.academic_years?.map((y: any) => (
        <Card key={y.academic_year_id} title={`${y.name}`} actions={<Badge value={y.status === "ACTIVE" ? "ACTIVE" : y.status} />}>
          <p className="muted small">{fmtDate(y.start_date)} – {fmtDate(y.end_date)}</p>
          {y.grades.map((g: any) => (
            <div key={g.grade_id} className="topic">
              <h3>{g.name}</h3>
              <div className="chips">
                {g.sections.map((s: any) => (
                  <span key={s.section_id} className="chip static">{s.name} · {s.active_students} students{s.class_teacher ? ` · ${s.class_teacher}` : ""}</span>
                ))}
              </div>
              <div className="row gap-sm top-gap">
                <input placeholder="New section, e.g. 10B" value={val(`sec-${g.grade_id}`)} onChange={(e) => setVal(`sec-${g.grade_id}`, e.target.value)} aria-label="New section name" />
                <Button variant="secondary" busy={a.busy} disabled={!val(`sec-${g.grade_id}`)} onClick={() => run(() => post(`/v1/grades/${g.grade_id}/sections`, { name: val(`sec-${g.grade_id}`) }), [`sec-${g.grade_id}`])}>Add section</Button>
              </div>
            </div>
          ))}
          <div className="row gap-sm wrap top-gap">
            <input placeholder="Grade name, e.g. Grade 11" value={val(`gr-${y.academic_year_id}`)} onChange={(e) => setVal(`gr-${y.academic_year_id}`, e.target.value)} aria-label="New grade name" />
            <input type="number" placeholder="Level" min={1} max={12} className="short" value={val(`lv-${y.academic_year_id}`)} onChange={(e) => setVal(`lv-${y.academic_year_id}`, e.target.value)} aria-label="Grade level" />
            <Button variant="secondary" busy={a.busy} disabled={!val(`gr-${y.academic_year_id}`) || !val(`lv-${y.academic_year_id}`)} onClick={() => run(() => post(`/v1/academic-years/${y.academic_year_id}/grades`, { name: val(`gr-${y.academic_year_id}`), grade_level: Number(val(`lv-${y.academic_year_id}`)) }), [`gr-${y.academic_year_id}`, `lv-${y.academic_year_id}`])}>Add grade</Button>
          </div>
          <h3 className="top-gap">Subjects</h3>
          <div className="chips">{y.subjects.map((s: any) => <span key={s.subject_id} className="chip static">{s.name} ({s.code})</span>)}</div>
          <div className="row gap-sm wrap top-gap">
            <input placeholder="Subject name" value={val(`sn-${y.academic_year_id}`)} onChange={(e) => setVal(`sn-${y.academic_year_id}`, e.target.value)} aria-label="Subject name" />
            <input placeholder="Code" className="short" value={val(`sc-${y.academic_year_id}`)} onChange={(e) => setVal(`sc-${y.academic_year_id}`, e.target.value.toUpperCase())} aria-label="Subject code" />
            <Button
              variant="secondary"
              busy={a.busy}
              disabled={!val(`sn-${y.academic_year_id}`) || !val(`sc-${y.academic_year_id}`) || !y.grades.length}
              onClick={() => run(() => post(`/v1/academic-years/${y.academic_year_id}/subjects`, { name: val(`sn-${y.academic_year_id}`), code: val(`sc-${y.academic_year_id}`), grade_ids: y.grades.map((g: any) => g.grade_id) }), [`sn-${y.academic_year_id}`, `sc-${y.academic_year_id}`])}
            >
              Add subject (all grades)
            </Button>
          </div>
        </Card>
      ))}
      <Card title="New academic year">
        <div className="grid2">
          <Field label="Name"><input value={year.name} onChange={(e) => setYear({ ...year, name: e.target.value })} placeholder="2027-28" /></Field>
          <Field label="Status">
            <select value={year.status} onChange={(e) => setYear({ ...year, status: e.target.value })}>
              <option value="PLANNED">Planned (next year)</option>
              <option value="ACTIVE">Active now</option>
            </select>
          </Field>
          <Field label="Starts"><input type="date" value={year.start_date} onChange={(e) => setYear({ ...year, start_date: e.target.value })} /></Field>
          <Field label="Ends"><input type="date" value={year.end_date} onChange={(e) => setYear({ ...year, end_date: e.target.value })} /></Field>
        </div>
        <Button className="top-gap" busy={a.busy} disabled={!year.name || !year.start_date || !year.end_date} onClick={() => run(() => post(`/v1/schools/${sid}/academic-years`, year))}>Create year</Button>
      </Card>
    </div>
  );
}

export function Teachers() {
  const sid = useSchoolId();
  const { data, error, loading, reload } = useApi<any>(sid ? `/v1/schools/${sid}/teachers` : null, [sid]);
  const st = useStructure();
  const sections = allSections(st.data);
  const subjects = (st.data?.academic_years || []).find((y: any) => y.status === "ACTIVE")?.subjects || [];
  const [inv, setInv] = useState({ email: "", display_name: "" });
  const [asg, setAsg] = useState<Record<string, any>>({});
  const a = useAction();
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title="Teachers" />
      <ErrorNote error={error || a.error} onRetry={reload} />
      <Card title="Invite a teacher">
        <div className="grid2">
          <Field label="Name"><input value={inv.display_name} onChange={(e) => setInv({ ...inv, display_name: e.target.value })} /></Field>
          <Field label="Email"><input type="email" value={inv.email} onChange={(e) => setInv({ ...inv, email: e.target.value })} /></Field>
        </div>
        <Button className="top-gap" busy={a.busy} disabled={!inv.email || !inv.display_name} onClick={() => a.run(async () => { await post("/v1/teachers", { school_id: sid, ...inv }); setInv({ email: "", display_name: "" }); await reload(); })}>Send invite</Button>
      </Card>
      {data?.teachers?.map((t: any) => {
        const f = asg[t.teacher_id] || { section_id: "", subject_id: "", assignment_role: "PRIMARY_SUBJECT_TEACHER" };
        const setF = (k: string, v: string) => setAsg({ ...asg, [t.teacher_id]: { ...f, [k]: v } });
        return (
          <Card key={t.teacher_id} title={t.display_name} actions={<Badge value={t.account_status === "ACTIVE" ? "COMPLETED" : "PROCESSING"} text={t.account_status === "ACTIVE" ? "Active" : "Invited"} />}>
            <p className="muted small">{t.email}</p>
            <ul className="list">
              {t.assignments.map((x: any) => (
                <li key={x.teacher_assignment_id} className="list-item">{x.section_name} · {x.subject_name || "Class teacher"} <span className="muted small">{label(x.assignment_role)}</span></li>
              ))}
            </ul>
            <div className="row gap-sm wrap top-gap">
              <select value={f.section_id} onChange={(e) => setF("section_id", e.target.value)} aria-label="Class">
                <option value="">Class...</option>
                {sections.map((s) => <option key={s.section_id} value={s.section_id}>{s.name}</option>)}
              </select>
              <select value={f.assignment_role} onChange={(e) => setF("assignment_role", e.target.value)} aria-label="Role">
                <option value="PRIMARY_SUBJECT_TEACHER">Primary subject teacher</option>
                <option value="CO_TEACHER">Co-teacher</option>
                <option value="CLASS_TEACHER">Class teacher</option>
              </select>
              {f.assignment_role !== "CLASS_TEACHER" ? (
                <select value={f.subject_id} onChange={(e) => setF("subject_id", e.target.value)} aria-label="Subject">
                  <option value="">Subject...</option>
                  {subjects.map((s: any) => <option key={s.subject_id} value={s.subject_id}>{s.name}</option>)}
                </select>
              ) : null}
              <Button
                variant="secondary"
                busy={a.busy}
                disabled={!f.section_id || (f.assignment_role !== "CLASS_TEACHER" && !f.subject_id)}
                onClick={() => a.run(async () => {
                  await post("/v1/teacher-assignments", { teacher_id: t.teacher_id, section_id: f.section_id, assignment_role: f.assignment_role, ...(f.assignment_role !== "CLASS_TEACHER" ? { subject_id: f.subject_id } : {}) });
                  await reload();
                })}
              >
                Assign
              </Button>
            </div>
          </Card>
        );
      })}
    </div>
  );
}

export function LoginsTable({ logins }: { logins: any[] }) {
  if (!logins.length) return null;
  const csv = ["name,login,temporary_password", ...logins.map((l) => `${l.display_name},${l.username || l.email},${l.temporary_password || ""}`)].join("\n");
  return (
    <div className="stack">
      <Note tone="warn">Hand these over privately. Temporary passwords are shown only now; students choose their own at first sign-in.</Note>
      <table className="table">
        <thead><tr><th>Student</th><th>Sign in with</th><th>Temporary password</th></tr></thead>
        <tbody>
          {logins.map((l, i) => (
            <tr key={i}><td>{l.display_name}</td><td><code>{l.username || l.email}</code></td><td>{l.temporary_password ? <Secret value={l.temporary_password} /> : <span className="muted small">invite email sent</span>}</td></tr>
          ))}
        </tbody>
      </table>
      <a className="btn btn-secondary" href={`data:text/csv;charset=utf-8,${encodeURIComponent(csv)}`} download="aayra-logins.csv">Download as CSV</a>
    </div>
  );
}
