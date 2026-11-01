import { useState } from "react";
import { Button, Card, ErrorNote, useAction } from "../../components/ui";
import { post } from "../../lib/api";
import { useApi } from "../../lib/hooks";

export default function IndependentSubjects() {
  const { data, reload } = useApi<any>("/v1/students/me/subjects");
  const [name, setName] = useState("");
  const a = useAction();
  return (
    <Card title="My subjects">
      <div className="chips">
        {data?.subjects?.map((s: any) => <span key={s.subject_id} className="chip static">{s.name}</span>)}
      </div>
      <form
        className="row gap top-gap"
        onSubmit={async (e) => {
          e.preventDefault();
          if (await a.run(() => post("/v1/independent-learners/me/subjects", { name }))) {
            setName("");
            reload();
          }
        }}
      >
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="Add a subject" aria-label="New subject" />
        <Button type="submit" variant="secondary" busy={a.busy} disabled={!name.trim()}>Add</Button>
      </form>
      <ErrorNote error={a.error} />
    </Card>
  );
}
