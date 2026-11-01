import { useNavigate } from "react-router-dom";
import { Button, Card, Empty, ErrorNote, PageHeader, Spinner } from "../components/ui";
import { post } from "../lib/api";
import { fmtDate } from "../lib/format";
import { useApi } from "../lib/hooks";

// Deep links look like aayra://student/assignments/<id>/feedback; map the ones the web app has screens for.
function webPath(link: string | null): string | null {
  if (!link) return null;
  const m = link.replace(/^aayra:\/\//, "/");
  const sa = m.match(/^\/student\/assignments\/([^/]+)/);
  if (sa) return `/student/lesson/${sa[1]}`;
  return null;
}

export default function Notifications() {
  const { data, error, loading, reload } = useApi<any>("/v1/me/notifications?limit=50");
  const nav = useNavigate();
  if (loading && !data) return <Spinner />;
  return (
    <div className="stack">
      <PageHeader
        title="Notifications"
        actions={data?.unread_count ? <Button variant="secondary" onClick={() => post("/v1/me/notifications/read-all").then(reload)}>Mark all read</Button> : null}
      />
      <ErrorNote error={error} onRetry={reload} />
      <Card>
        {!data?.notifications?.length ? <Empty>You're all caught up.</Empty> : null}
        <ul className="list">
          {data?.notifications?.map((n: any) => {
            const to = webPath(n.deep_link);
            return (
              <li key={n.notification_id} className={`list-item ${n.status === "UNREAD" ? "unread" : ""}`}>
                <button
                  className="list-button"
                  onClick={async () => {
                    if (n.status === "UNREAD") await post(`/v1/me/notifications/${n.notification_id}/read`).catch(() => {});
                    if (to) nav(to);
                    else reload();
                  }}
                >
                  <div className="list-title">{n.title}</div>
                  {n.body ? <div className="muted small">{n.body}</div> : null}
                  <div className="muted small">{fmtDate(n.created_at, true)}</div>
                </button>
              </li>
            );
          })}
        </ul>
      </Card>
    </div>
  );
}
