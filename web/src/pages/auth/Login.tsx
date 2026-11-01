import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { Button, Card, ErrorNote, Field, Note, useAction } from "../../components/ui";
import { api } from "../../lib/api";
import { useAuth } from "../../lib/auth";
import { config, supabaseConfigured } from "../../lib/config";

function DevLogin() {
  const { devSignIn } = useAuth();
  const [users, setUsers] = useState<any[]>([]);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    api<any>("GET", "/dev/users", undefined, { auth: false })
      .then((d) => setUsers(d.users))
      .catch(() => setError("The API isn't running in local mode (start it with AAYRA_ENVIRONMENT=local)."));
  }, []);
  return (
    <Card title="Developer sign-in">
      <Note tone="warn">Local development only: pick any seeded person.</Note>
      <ErrorNote error={error} />
      <ul className="list">
        {users.map((u) => (
          <li key={u.user_id} className="list-item">
            <button
              className="list-button row between"
              onClick={async () => {
                const t = await api<any>("POST", "/dev/token", { user_id: u.user_id }, { auth: false });
                await devSignIn(t.access_token);
              }}
            >
              <span>{u.display_name}</span>
              <span className="muted small">{u.roles.join(", ").toLowerCase().replace(/_/g, " ")}</span>
            </button>
          </li>
        ))}
      </ul>
    </Card>
  );
}

export default function Login() {
  const { signIn } = useAuth();
  const [identifier, setIdentifier] = useState("");
  const [password, setPassword] = useState("");
  const a = useAction();
  if (config.devLogin) return <div className="auth-wrap"><Brand /><DevLogin /></div>;
  async function submit(e: FormEvent) {
    e.preventDefault();
    await a.run(() => signIn(identifier, password));
  }
  return (
    <div className="auth-wrap">
      <Brand />
      <Card title="Sign in">
        {!supabaseConfigured ? <Note tone="warn">Sign-in isn't configured yet (missing Supabase settings).</Note> : null}
        <form className="stack" onSubmit={submit}>
          <Field label="Email or username" hint="Students without an email use the username from their school.">
            <input value={identifier} onChange={(e) => setIdentifier(e.target.value)} autoComplete="username" required />
          </Field>
          <Field label="Password">
            <input type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
          </Field>
          <ErrorNote error={a.error} />
          <Button type="submit" busy={a.busy}>Sign in</Button>
        </form>
        <div className="row between small top-gap">
          <Link to="/forgot-password">Forgot password?</Link>
          <Link to="/signup">New here? Create an account (18+)</Link>
        </div>
      </Card>
      <p className="muted small center">School students: your school sends your login. Ask your teacher if you don't have one.</p>
    </div>
  );
}

export function Brand() {
  return (
    <div className="auth-brand">
      <span className="brand-mark lg">A</span>
      <div>
        <div className="auth-title">Aayra</div>
        <div className="muted">Learn it. Remember it.</div>
      </div>
    </div>
  );
}
