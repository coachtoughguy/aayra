import { Card, Empty, ErrorNote, PageHeader, Spinner } from "../../components/ui";
import { useApi } from "../../lib/hooks";
import { WorkItem } from "./Home";

export default function MyLearning() {
  const { data, error, loading, reload } = useApi<any>("/v1/students/me/learning?include_completed=true");
  const reviews = useApi<any>("/v1/students/me/reviews");
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title="My learning" />
      <ErrorNote error={error} onRetry={reload} />
      {reviews.data?.due?.length ? (
        <Card title="Reviews due">
          <div className="stack-sm">
            {reviews.data.due.map((r: any) => (
              <WorkItem key={r.review_schedule_item_id} item={{ kind: "REVIEW", ...r, title: `Review: ${r.concept_name}`, subject: { name: r.subject_name } }} />
            ))}
          </div>
        </Card>
      ) : null}
      {data?.subjects?.map((s: any) => (
        <Card key={s.subject_id} title={s.name} actions={<span className="muted small">{s.completed_count} done</span>}>
          {s.pending.length ? <div className="stack-sm">{s.pending.map((w: any) => <WorkItem key={w.student_assignment_id} item={w} />)}</div> : <Empty>Nothing to do here right now.</Empty>}
          {s.completed?.length ? (
            <details className="top-gap">
              <summary className="small">Completed ({s.completed.length})</summary>
              <div className="stack-sm top-gap">{s.completed.map((w: any) => <WorkItem key={w.student_assignment_id} item={w} />)}</div>
            </details>
          ) : null}
        </Card>
      ))}
    </div>
  );
}
