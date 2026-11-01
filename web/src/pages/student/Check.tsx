import { useEffect, useState } from "react";
import { Link, useParams } from "react-router-dom";
import QuestionRunner, { type Next } from "../../components/QuestionRunner";
import { ErrorNote, PageHeader, Spinner } from "../../components/ui";
import { errorMessage, post } from "../../lib/api";
import { startOrResumeCheck } from "./Lesson";

export default function Check() {
  const { sa = "" } = useParams();
  const [state, setState] = useState<{ attemptId: string; next: Next } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    startOrResumeCheck(sa).then(setState).catch((e) => setError(errorMessage(e)));
  }, [sa]);
  return (
    <div className="stack narrow">
      <PageHeader title="Mastery check" subtitle={<Link to={`/student/lesson/${sa}`}>Back to the lesson</Link>} />
      <ErrorNote error={error} />
      {!state && !error ? <Spinner /> : null}
      {state ? (
        <QuestionRunner
          attemptId={state.attemptId}
          initial={state.next}
          submit={(qid, answer) => post(`/v1/assessment-attempts/${state.attemptId}/responses`, { question_id: qid, answer })}
          doneLink={{ to: "/student", text: "Back to home" }}
        />
      ) : null}
    </div>
  );
}
