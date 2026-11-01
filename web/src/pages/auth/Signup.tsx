import { useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Button, Card, ErrorNote, Field, Note, useAction } from "../../components/ui";
import { useAuth } from "../../lib/auth";
import { Brand } from "./Login";

export default function Signup() {
  const { signUp } = useAuth();
  const [email, setEmail] = useState("");
  const [pw, setPw] = useState("");
  const [sent, setSent] = useState(false);
  const a = useAction();
  async function submit(e: FormEvent) {
    e.preventDefault();
    if (pw.length < 8) return a.setError("Use at least 8 characters.");
    const r = await a.run(() => signUp(email, pw));
    if (r?.needsConfirmation) setSent(true);
  }
  return (
    <div className="auth-wrap">
      <Brand />
      <Card title="Create your account">
        <Note>For independent learners aged 18 and over. Students under 18 join through their school.</Note>
        {sent ? (
          <Note tone="good">Check your inbox: we sent a link to confirm {email}. Open it to continue.</Note>
        ) : (
          <form className="stack" onSubmit={submit}>
            <Field label="Email">
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="email" required />
            </Field>
            <Field label="Password" hint="At least 8 characters.">
              <input type="password" value={pw} onChange={(e) => setPw(e.target.value)} autoComplete="new-password" required />
            </Field>
            <ErrorNote error={a.error} />
            <Button type="submit" busy={a.busy}>Create account</Button>
          </form>
        )}
        <p className="small top-gap"><Link to="/">Back to sign in</Link></p>
      </Card>
    </div>
  );
}

export function ForgotPassword() {
  const { sendReset } = useAuth();
  const [email, setEmail] = useState("");
  const [sent, setSent] = useState(false);
  const a = useAction();
  return (
    <div className="auth-wrap">
      <Brand />
      <Card title="Reset your password">
        {sent ? (
          <Note tone="good">If {email} has an account, a reset link is on its way.</Note>
        ) : (
          <form
            className="stack"
            onSubmit={async (e) => {
              e.preventDefault();
              if (await a.run(() => sendReset(email)) !== undefined) setSent(true);
            }}
          >
            <Field label="Email" hint="Students with a username: ask your school admin to reset your login.">
              <input type="email" value={email} onChange={(e) => setEmail(e.target.value)} required />
            </Field>
            <ErrorNote error={a.error} />
            <Button type="submit" busy={a.busy}>Send reset link</Button>
          </form>
        )}
        <p className="small top-gap"><Link to="/">Back to sign in</Link></p>
      </Card>
    </div>
  );
}
