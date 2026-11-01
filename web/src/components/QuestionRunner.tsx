import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { errorMessage, get, kickWorker } from "../lib/api";
import { label } from "../lib/format";
import { Badge, Button, Card, ErrorNote, Note, Spinner } from "./ui";

export interface Next {
  type: "QUESTION" | "RETRY" | "AWAITING_EVALUATION" | "COMPLETED";
  question?: { question_id: string; question_type: string; prompt_text: string; options?: string[] };
  result?: Result;
}
interface Result {
  questions_answered: number;
  correct: number;
  concepts: { concept_id: string; name: string; status: string | null }[];
}

/** Runs a mastery check or a review: one question at a time, with hint -> retry -> explanation repair. */
export default function QuestionRunner({
  attemptId,
  initial,
  submit,
  doneLink,
}: {
  attemptId: string;
  initial: Next;
  submit: (questionId: string, answer: any) => Promise<any>;
  doneLink: { to: string; text: string };
}) {
  const [next, setNext] = useState<Next>(initial);
  const [choice, setChoice] = useState<number | null>(null);
  const [text, setText] = useState("");
  const [feedback, setFeedback] = useState<{ kind: string; text: string; evaluation?: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [count, setCount] = useState(0);

  useEffect(() => {
    if (next.type !== "AWAITING_EVALUATION") return;
    const t = window.setInterval(async () => {
      kickWorker();
      try {
        const view = await get<any>(`/v1/assessment-attempts/${attemptId}`);
        if (view.status === "COMPLETED") setNext({ type: "COMPLETED", result: view.result });
        else if (view.current_question)
          setNext({ type: view.current_question.mode === "RETRY" ? "RETRY" : "QUESTION", question: view.current_question });
      } catch {
        /* keep waiting */
      }
    }, 2500);
    return () => window.clearInterval(t);
  }, [next.type, attemptId]);

  async function answer() {
    if (!next.question) return;
    const q = next.question;
    const payload = q.question_type === "MCQ" ? { selected_index: choice } : { text };
    setBusy(true);
    setError(null);
    try {
      const r = await submit(q.question_id, payload);
      setCount((c) => c + 1);
      if (r.evaluation_status === "PENDING") setFeedback({ kind: "PENDING", text: "Got it. Your written answer is being checked; keep going." });
      else if (r.feedback) setFeedback({ ...r.feedback, evaluation: r.evaluation });
      else setFeedback(r.evaluation ? { kind: r.evaluation, text: r.evaluation === "CORRECT" ? "Correct, nice work!" : "", evaluation: r.evaluation } : null);
      setNext(r.next);
      setChoice(null);
      setText("");
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  if (next.type === "COMPLETED") {
    const res = next.result;
    return (
      <Card title="All done!">
        {res ? (
          <div className="stack">
            <p>You answered {res.questions_answered} question{res.questions_answered === 1 ? "" : "s"}. Here's where you are:</p>
            <ul className="list">
              {res.concepts.map((c) => (
                <li key={c.concept_id} className="list-item row between">
                  <span>{c.name}</span>
                  {c.status ? <Badge value={c.status} /> : <span className="muted small">Not assessed</span>}
                </li>
              ))}
            </ul>
            <Note tone="good">Anything marked "Developing" or "Needs review" comes back in your spaced reviews, so it sticks.</Note>
          </div>
        ) : null}
        <Link className="btn btn-primary top-gap" to={doneLink.to}>{doneLink.text}</Link>
      </Card>
    );
  }
  if (next.type === "AWAITING_EVALUATION") return <Card><Spinner text="Checking your written answers..." /></Card>;
  const q = next.question!;
  return (
    <Card title={next.type === "RETRY" ? "Try again" : `Question ${count + 1}`}>
      {feedback && feedback.text ? (
        <Note tone={feedback.kind === "HINT" || feedback.kind === "PENDING" ? "info" : feedback.evaluation === "CORRECT" ? "good" : "warn"}>
          <strong>{feedback.kind === "HINT" ? "Hint" : feedback.kind === "EXPLANATION" ? "Explanation" : label(feedback.kind)}:</strong> {feedback.text}
        </Note>
      ) : null}
      <p className="question">{q.prompt_text}</p>
      {q.question_type === "MCQ" ? (
        <div className="options" role="radiogroup">
          {q.options?.map((o, i) => (
            <label key={i} className={`option ${choice === i ? "on" : ""}`}>
              <input type="radio" name="opt" checked={choice === i} onChange={() => setChoice(i)} />
              <span>{o}</span>
            </label>
          ))}
        </div>
      ) : (
        <textarea rows={5} value={text} onChange={(e) => setText(e.target.value)} placeholder="Answer in your own words..." aria-label="Your answer" />
      )}
      <ErrorNote error={error} />
      <Button onClick={answer} busy={busy} disabled={q.question_type === "MCQ" ? choice === null : !text.trim()}>
        Submit answer
      </Button>
    </Card>
  );
}
