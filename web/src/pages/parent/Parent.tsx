import { Link, useParams } from "react-router-dom";
import { FamilyCreate, FamilyDetail, FamilyList } from "../../components/FamilyFlow";
import { Badge, Card, Empty, ErrorNote, PageHeader, Spinner } from "../../components/ui";
import { relativeDue, SOURCE_LABEL } from "../../lib/format";
import { useApi } from "../../lib/hooks";
import { ProgressView } from "../student/Progress";

export default function ParentHome() {
  const { data, error, loading, reload } = useApi<any>("/v1/guardians/me/children");
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title="My children" />
      <ErrorNote error={error} onRetry={reload} />
      {!data?.children?.length ? <Empty>No children are linked to your account yet. Ask the school to link you.</Empty> : null}
      <div className="grid2">
        {data?.children?.map((c: any) => (
          <Link key={c.student_id} to={`/parent/child/${c.student_id}`} className="card link-card">
            <h2>{c.display_name}</h2>
            <p className="muted">{c.school_name}{c.section_name ? ` · ${c.section_name}` : ""}</p>
          </Link>
        ))}
      </div>
    </div>
  );
}

export function ParentChild() {
  const { studentId = "" } = useParams();
  const { data, error, loading, reload } = useApi<any>(`/v1/guardians/me/children/${studentId}/overview`);
  if (loading && !data) return <Spinner />;
  if (!data) return <ErrorNote error={error} onRetry={reload} />;
  return (
    <div className="stack">
      <PageHeader title={data.child.display_name} subtitle="Progress is compared only with their own past work." actions={<Link className="btn btn-primary" to={`/parent/child/${studentId}/assign`}>Assign learning</Link>} />
      <ProgressView p={data.progress} ownLabel="work you assigned" />
      <Card title="Their work">
        {!data.work.length ? <Empty>Nothing assigned yet.</Empty> : null}
        <ul className="list">
          {data.work.map((w: any) => (
            <li key={w.student_assignment_id} className="list-item row between wrap gap">
              <div>
                <div>{w.title}</div>
                <div className="muted small">{w.subject_name} · {relativeDue(w.due_at)}</div>
              </div>
              <span className="row gap-sm">
                <Badge value={w.source_type} text={SOURCE_LABEL[w.source_type]} />
                <Badge value={w.label} />
              </span>
            </li>
          ))}
        </ul>
      </Card>
      <Card title="Learning you assigned">
        <FamilyList items={data.my_assignments} to={(id) => `/parent/child/${studentId}/assignment/${id}`} />
      </Card>
    </div>
  );
}

export function ParentAssign() {
  const { studentId = "" } = useParams();
  return (
    <FamilyCreate
      source="PARENT"
      studentId={studentId}
      subjectsPath={`/v1/guardians/me/children/${studentId}/subjects`}
      backTo={`/parent/child/${studentId}`}
      detailTo={(id) => `/parent/child/${studentId}/assignment/${id}`}
    />
  );
}

export function ParentAssignment() {
  const { studentId = "", id = "" } = useParams();
  return <FamilyDetail source="PARENT" id={id} studentId={studentId} backTo={`/parent/child/${studentId}`} />;
}
