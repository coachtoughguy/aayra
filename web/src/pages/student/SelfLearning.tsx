import { Link, useParams } from "react-router-dom";
import { FamilyCreate, FamilyDetail, FamilyList } from "../../components/FamilyFlow";
import { Card, ErrorNote, PageHeader, Spinner } from "../../components/ui";
import { useApi } from "../../lib/hooks";

export default function SelfLearning() {
  const { data, error, loading, reload } = useApi<any>("/v1/self-assignments");
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title="Learn something" subtitle="Your own learning, at your own pace." actions={<Link className="btn btn-primary" to="/student/self/new">New</Link>} />
      <ErrorNote error={error} onRetry={reload} />
      <Card title="My own learning">
        <FamilyList items={data?.assignments || []} to={(id) => `/student/self/${id}`} />
      </Card>
    </div>
  );
}

export function SelfNew() {
  return <FamilyCreate source="SELF" subjectsPath="/v1/students/me/subjects" backTo="/student/self" detailTo={(id) => `/student/self/${id}`} />;
}

export function SelfDetail() {
  const { id = "" } = useParams();
  return <FamilyDetail source="SELF" id={id} backTo="/student/self" />;
}
