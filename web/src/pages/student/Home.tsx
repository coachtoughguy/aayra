import { Link } from "react-router-dom";
import { Badge, Card, Empty, ErrorNote, PageHeader, Spinner, Stat } from "../../components/ui";
import { useAuth } from "../../lib/auth";
import { relativeDue, SOURCE_LABEL } from "../../lib/format";
import { useApi } from "../../lib/hooks";
import IndependentSubjects from "./IndependentSubjects";

export function WorkItem({ item }: { item: any }) {
  if (item.kind === "REVIEW")
    return (
      <Link className="work" to={`/student/review/${item.review_schedule_item_id}`}>
        <div>
          <div className="work-title">{item.title}</div>
          <div className="muted small">{item.subject.name} · Day {item.review_day} review</div>
        </div>
        <Badge value="REVIEW" text="Review" />
      </Link>
    );
  return (
    <Link className="work" to={`/student/lesson/${item.student_assignment_id}`}>
      <div>
        <div className="work-title">{item.title}</div>
        <div className="muted small">
          {item.subject.name} · {relativeDue(item.due_at)}
        </div>
      </div>
      <div className="row gap-sm">
        <Badge value={item.source} text={SOURCE_LABEL[item.source]} />
        {item.tier === "OVERDUE" ? <Badge value="OVERDUE" /> : null}
      </div>
    </Link>
  );
}

export default function StudentHome() {
  const { me } = useAuth();
  const { data, error, loading, reload } = useApi<any>("/v1/students/me/home");
  const independent = me?.student?.school_kind === "INDEPENDENT";
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title={`Hi ${me?.display_name.split(" ")[0] || ""}`} subtitle={independent ? "Your personal learning space" : `${me?.student?.school_name} · ${me?.student?.section_name}`} />
      <ErrorNote error={error} onRetry={reload} />
      {data?.continue_learning ? (
        <Card title="Continue learning" className="accent">
          <WorkItem item={data.continue_learning} />
        </Card>
      ) : null}
      <Card title="What's next" actions={<Link to="/student/learning" className="small">See all</Link>}>
        {data?.whats_next?.length ? (
          <div className="stack-sm">{data.whats_next.map((w: any, i: number) => <WorkItem key={i} item={w} />)}</div>
        ) : (
          <Empty>{independent ? "Nothing yet. Add something you want to learn." : "You're all caught up. Nice!"}</Empty>
        )}
        {independent ? <Link className="btn btn-primary top-gap" to="/student/self/new">Learn something new</Link> : null}
      </Card>
      {data ? (
        <div className="stats">
          <Stat value={data.my_learning.pending_count} label="To do" />
          <Stat value={data.my_reviews.due_today_count} label="Reviews today" />
          <Stat value={data.progress_snapshot.completed_this_week} label="Done this week" />
        </div>
      ) : null}
      {independent ? <IndependentSubjects /> : null}
    </div>
  );
}
