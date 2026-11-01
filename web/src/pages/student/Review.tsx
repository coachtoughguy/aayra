import { useEffect, useState } from "react";
import { useParams } from "react-router-dom";
import QuestionRunner, { type Next } from "../../components/QuestionRunner";
import { ErrorNote, Note, PageHeader, Spinner } from "../../components/ui";
import { errorMessage, post } from "../../lib/api";

export default function Review() {
  const { itemId = "" } = useParams();
  const [state, setState] = useState<{ sessionId: string; attemptId: string; next: Next; day: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    post<any>("/v1/review-sessions", { review_schedule_item_id: itemId })
      .then((r) => setState({ sessionId: r.review_session_id, attemptId: r.attempt_id, next: r.next, day: r.review_day }))
      .catch((e) => setError(errorMessage(e)));
  }, [itemId]);
  return (
    <div className="stack narrow">
      <PageHeader title="Quick review" subtitle={state ? `Day ${state.day} review: a couple of questions so it sticks.` : undefined} />
      <ErrorNote error={error} />
      {!state && !error ? <Spinner /> : null}
      {state ? (
        <>
          <Note>Reviews come back on a fixed schedule (day 2, 4, 8, 16...). Getting one wrong is normal; it's how memory gets stronger.</Note>
          <QuestionRunner
            attemptId={state.attemptId}
            initial={state.next}
            submit={(qid, answer) => post(`/v1/review-sessions/${state.sessionId}/responses`, { question_id: qid, answer })}
            doneLink={{ to: "/student", text: "Back to home" }}
          />
        </>
      ) : null}
    </div>
  );
}
