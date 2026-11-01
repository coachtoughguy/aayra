import { useState, type FormEvent } from "react";
import { Button, Card, ErrorNote, Field, useAction } from "../../components/ui";
import { useAuth } from "../../lib/auth";
import { Brand } from "./Login";

export default function SetPassword() {
  const { setPassword, signOut } = useAuth();
  const [pw, setPw] = useState("");
  const [pw2, setPw2] = useState("");
  const a = useAction();
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pw.length < 8) return a.setError("Use at least 8 characters.");
    if (pw !== pw2) return a.setError("The two passwords don't match.");
    await a.run(() => setPassword(pw));
  }
  return (
    <div className="auth-wrap">
      <Brand />
      <Card title="Choose your password">
        <p className="muted">Welcome! Set a password you'll use to sign in from now on.</p>
        <form className="stack" onSubmit={submit}>
          <Field label="New password" hint="At least 8 characters.">
            <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" required />
          </Field>
          <Field label="Repeat it">
            <input type="password" value={pw2} onChange={(e) => setPw2(e.target.value)} autoComplete="new-password" required />
          </Field>
          <ErrorNote error={a.error} />
          <Button type="submit" busy={a.busy}>Save password</Button>
          <button type="button" className="link" onClick={() => signOut()}>Sign out</button>
        </form>
      </Card>
    </div>
  );
}
