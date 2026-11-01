import { useState, type FormEvent } from "react";
import { Button, Card, ErrorNote, Field, Note, useAction } from "../../components/ui";
import { post } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { Brand } from "./Login";

const COUNTRIES = [["US", "United States"], ["IN", "India"], ["GB", "United Kingdom"], ["CA", "Canada"], ["AU", "Australia"], ["AE", "United Arab Emirates"], ["SG", "Singapore"]];

/** Signed in, but no Aayra account yet: independent (18+) signup, or wait for the school's invite. */
export default function Welcome() {
  const { email, refresh, signOut } = useAuth();
  const tz = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  const [form, setForm] = useState({ display_name: "", date_of_birth: "", country_code: "US", timezone: tz, subjects: "", confirm: false });
  const [blocked, setBlocked] = useState(false);
  const a = useAction();
  async function submit(e: FormEvent) {
    e.preventDefault();
    const r = await a.run(async () => {
      try {
        return await post("/v1/independent-learners", {
          display_name: form.display_name,
          date_of_birth: form.date_of_birth,
          confirms_18_or_older: form.confirm,
          country_code: form.country_code,
          timezone: form.timezone,
          subjects: form.subjects.split(",").map((s) => s.trim()).filter(Boolean),
        });
      } catch (err: any) {
        if (err?.code === "AGE_RESTRICTED") setBlocked(true);
        throw err;
      }
    });
    if (r) await refresh();
  }
  const set = (k: string, v: any) => setForm({ ...form, [k]: v });
  return (
    <div className="auth-wrap wide">
      <Brand />
      <Card title="Set up your learning space">
        <p className="muted">Signed in as {email}. Aayra helps you learn from your own material: upload notes, a PDF or a video, and it builds lessons, checks and spaced reviews.</p>
        {blocked ? (
          <Note tone="warn">Independent accounts are for people aged 18 and over. If you're in school, ask your school to set up your Aayra login so a parent or guardian can be linked.</Note>
        ) : (
          <form className="stack" onSubmit={submit}>
            <Field label="Your name"><input value={form.display_name} onChange={(e) => set("display_name", e.target.value)} required /></Field>
            <div className="grid2">
              <Field label="Date of birth"><input type="date" value={form.date_of_birth} onChange={(e) => set("date_of_birth", e.target.value)} required /></Field>
              <Field label="Country">
                <select value={form.country_code} onChange={(e) => set("country_code", e.target.value)}>
                  {COUNTRIES.map(([c, n]) => <option key={c} value={c}>{n}</option>)}
                </select>
              </Field>
            </div>
            <Field label="Subjects you want to learn" hint="Comma separated, e.g. Statistics, Spanish. You can add more later.">
              <input value={form.subjects} onChange={(e) => set("subjects", e.target.value)} />
            </Field>
            <label className="check"><input type="checkbox" checked={form.confirm} onChange={(e) => set("confirm", e.target.checked)} /> I confirm I am 18 or older.</label>
            <ErrorNote error={a.error} />
            <Button type="submit" busy={a.busy} disabled={!form.confirm}>Create my learning space</Button>
          </form>
        )}
        <hr />
        <p className="small muted">Teacher, parent or school student? Use the invite or login your school sent you. <button className="link" onClick={() => signOut()}>Sign out</button></p>
      </Card>
    </div>
  );
}
