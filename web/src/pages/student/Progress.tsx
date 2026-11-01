import { Card, Empty, ErrorNote, PageHeader, Spinner, Stat } from "../../components/ui";
import { fmtDate } from "../../lib/format";
import { useApi } from "../../lib/hooks";

const DAYS = ["M", "T", "W", "T", "F", "S", "S"];

export function ProgressView({ p, ownLabel = "My own learning" }: { p: any; ownLabel?: string }) {
  const school = p.work.SCHOOL;
  const tw = p.weekly.this_week;
  const lw = p.weekly.last_week;
  const better = tw.focus_minutes > lw.focus_minutes || tw.lessons_finished > lw.lessons_finished;
  return (
    <div className="stack">
      <div className="stats">
        <Stat value={(school?.pending ?? 0) + (p.work.PARENT?.pending ?? 0) + (p.work.SELF?.pending ?? 0)} label="To do" />
        <Stat value={(school?.completed ?? 0) + (p.work.PARENT?.completed ?? 0) + (p.work.SELF?.completed ?? 0)} label="Completed" />
        <Stat value={p.reviews.due_today} label="Reviews today" />
        <Stat value={`${p.streak.current_days} day${p.streak.current_days === 1 ? "" : "s"}`} label="Learning streak" />
      </div>
      <Card title="This week">
        <div className="week">
          {p.streak.this_week.map((on: boolean, i: number) => (
            <span key={i} className={`day ${on ? "on" : ""}`} title={on ? "Learned" : "No learning"}>{DAYS[i]}</span>
          ))}
        </div>
        <div className="grid2 top-gap">
          <div><strong>{tw.focus_minutes}</strong> min focused <span className="muted small">(last week {lw.focus_minutes})</span></div>
          <div><strong>{tw.lessons_finished}</strong> lessons finished <span className="muted small">(last week {lw.lessons_finished})</span></div>
          <div><strong>{tw.checks_completed}</strong> checks done</div>
          <div><strong>{tw.reviews_completed}</strong> reviews done</div>
        </div>
        {better ? <p className="good-text top-gap">You're doing more than last week. Keep it up!</p> : null}
      </Card>
      <Card title="Understanding by subject">
        {!p.mastery.length ? <Empty>Finish a lesson check to see this.</Empty> : null}
        <div className="stack-sm">
          {p.mastery.map((m: any) => {
            const total = m.mastered + m.developing + m.needs_review || 1;
            return (
              <div key={m.subject_id + m.scope}>
                <div className="row between small">
                  <strong>{m.subject_name}{m.scope === "OWN" ? ` · ${ownLabel}` : ""}</strong>
                  <span className="muted">{m.mastered} mastered · {m.developing} developing · {m.needs_review} to review</span>
                </div>
                <div className="bar" role="img" aria-label={`${m.mastered} of ${total} topics mastered`}>
                  <span className="bar-good" style={{ width: `${(m.mastered / total) * 100}%` }} />
                  <span className="bar-info" style={{ width: `${(m.developing / total) * 100}%` }} />
                  <span className="bar-warn" style={{ width: `${(m.needs_review / total) * 100}%` }} />
                </div>
              </div>
            );
          })}
        </div>
      </Card>
      <Card title="Coming up: reviews">
        {!p.reviews.next_7_days.length ? <Empty>No reviews in the next 7 days.</Empty> : null}
        <ul className="list">
          {p.reviews.next_7_days.map((r: any, i: number) => (
            <li key={i} className="list-item row between">
              <span>{r.lesson_title}</span>
              <span className="muted small">{fmtDate(r.scheduled_for)} · {r.concepts} topic{r.concepts === 1 ? "" : "s"}</span>
            </li>
          ))}
        </ul>
      </Card>
    </div>
  );
}

export default function Progress() {
  const { data, error, loading, reload } = useApi<any>("/v1/students/me/progress");
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader title="My progress" subtitle="Compared only with yourself." />
      <ErrorNote error={error} onRetry={reload} />
      {data ? <ProgressView p={data} /> : null}
    </div>
  );
}
