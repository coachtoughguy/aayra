# Deploying Aayra (Vercel + Supabase)

This deploys **two Vercel projects from the same GitHub repo**:

| Vercel project | Root directory | What it is |
|---|---|---|
| `aayra-api` | `backend` | FastAPI API (Python). It also runs background jobs on demand. |
| `aayra-web` | `web` | React web app (static site). |

Supabase provides the database, logins and file storage. It is the project `aayra` you already have.

Plan for about 30–45 minutes the first time.

---

## 1. Supabase settings (dashboard)

1. **Database password.** Go to Project Settings → Database. If you don't know the `postgres` password, reset it here and save it somewhere safe.
2. **Connection string for the API.**
   - Click **Connect** at the top of the dashboard, then choose **Transaction pooler** (port **6543**).
   - Copy the URI. It looks like `postgresql://postgres.eufapsbufiibvfrlumre:[YOUR-PASSWORD]@aws-…pooler.supabase.com:6543/postgres`.
   - Change the start `postgresql://` to `postgresql+asyncpg://` and put in your password. This becomes `AAYRA_DATABASE_URL`.
3. **API keys.** Go to Project Settings → API keys. Copy:
   - the **Project URL**: `https://eufapsbufiibvfrlumre.supabase.co`
   - the **publishable / anon** key. This one is for the web app and is safe in browsers.
   - the **service_role / secret** key. This one is for the API only. **Never put it in the web app.**
4. **JWT.** Under Project Settings → JWT Keys:
   - If the project uses **JWT signing keys** (the default for new projects), the API checks tokens against Supabase's public keys automatically. You only need `AAYRA_SUPABASE_URL`.
   - If it uses the **legacy JWT secret**, copy that secret into `AAYRA_JWT_SECRET`.
5. **Auth URLs.** Under Authentication → URL Configuration:
   - **Site URL:** your web app URL, for example `https://aayra-web.vercel.app`. You'll have it after step 3; come back and set it then.
   - **Redirect URLs:** add `https://aayra-web.vercel.app/**`.
6. **Email sign-ups.** Under Authentication → Providers → Email, keep **Email** enabled. "Confirm email" is recommended.
   - Supabase's built-in email service is rate-limited, which is fine for testing.
   - For real use, set up your own SMTP under Authentication → Emails.
7. **Storage bucket and tutor tables.** These are migrations 024 and 025 in this repo, applied to Supabase by Claude. To check, go to Storage. There should be a **private** bucket called `lesson-content`.

## 2. Deploy the API (`aayra-api`)

1. In Vercel, go to **Add New → Project**, import `coachtoughguy/aayra`, and set **Root Directory = `backend`**. The framework is detected as FastAPI or Python. Vercel reads `pyproject.toml`, `uv.lock`, `.python-version` (3.12) and `vercel.json`.
2. Add these **Environment Variables** (Production):

   | Name | Value |
   |---|---|
   | `AAYRA_ENVIRONMENT` | `production` |
   | `AAYRA_DATABASE_URL` | the `postgresql+asyncpg://…:6543/postgres` URL from step 1.2 |
   | `AAYRA_SUPABASE_URL` | `https://eufapsbufiibvfrlumre.supabase.co` |
   | `AAYRA_SUPABASE_SERVICE_ROLE_KEY` | the service_role / secret key |
   | `AAYRA_JWT_SECRET` | the legacy JWT secret if your project has one; otherwise leave it **empty** |
   | `AAYRA_IDENTITY_PROVIDER` | `supabase` |
   | `AAYRA_STORAGE_PROVIDER` | `supabase` |
   | `AAYRA_CORS_ORIGINS` | `https://aayra-web.vercel.app` (your web URL; comma-separate several) |
   | `AAYRA_WEB_APP_URL` | `https://aayra-web.vercel.app` |
   | `AAYRA_WORKER_SECRET` | any long random string (for the job runner in step 4) |
   | `AAYRA_BOOTSTRAP_KEY` | any long random string. Only needed to create a school through the API; the seed script in step 5 doesn't need it. |
   | `AAYRA_AI_PROVIDER` | `stub` to start. Set it to `anthropic` once you add the next two. |
   | `AAYRA_ANTHROPIC_API_KEY` | your Anthropic API key (when using real AI) |
   | `AAYRA_AI_MODEL` | the exact Claude model id your account can use (when using real AI) |
   | `AAYRA_TRANSCRIPTION_PROVIDER` | `stub`, or `openai` together with the next two |
   | `AAYRA_OPENAI_API_KEY` / `AAYRA_TRANSCRIPTION_MODEL` | for video/audio transcription, for example `whisper-1` |
   | `AAYRA_YOUTUBE_API_KEY` | optional |

3. Deploy. Open `https://<api-url>/healthz`. It should say `{"data":{"status":"ok","roles":5}}`.

Notes:
- **Database connections:** the API detects Vercel and uses no connection pool of its own, which is safe with Supabase's transaction pooler.
- **Background jobs** (reading files, preparing lessons, delivering work, grading) run when the web app asks after each change (`POST /v1/worker/kick`). They also run on the schedule in step 4. Vercel's own daily cron (`vercel.json`) is a last-resort backup.
- **Large uploads** go straight from the browser to Supabase Storage, so Vercel's 4.5 MB request limit doesn't apply to files.

## 3. Deploy the web app (`aayra-web`)

1. In Vercel, go to **Add New → Project**, import the same repo, and set **Root Directory = `web`**. The framework is detected as Vite.
2. Add these Environment Variables:

   | Name | Value |
   |---|---|
   | `VITE_API_URL` | your API URL, for example `https://aayra-api.vercel.app` (no trailing slash) |
   | `VITE_SUPABASE_URL` | `https://eufapsbufiibvfrlumre.supabase.co` |
   | `VITE_SUPABASE_ANON_KEY` | the publishable / anon key |
   | `VITE_STUDENT_LOGIN_DOMAIN` | `students.aayra.invalid` (it must match the API's `AAYRA_STUDENT_LOGIN_DOMAIN`, which defaults to the same) |

3. Deploy. Then finish the remaining settings:
   - Put the web URL into Supabase (step 1.5).
   - Put it into the API's `AAYRA_CORS_ORIGINS` and `AAYRA_WEB_APP_URL`, then redeploy the API.

## 4. Run background jobs every 5 minutes (GitHub Actions)

Vercel's free plan has no always-on worker, and its cron runs at most once a day. The workflow `.github/workflows/worker-tick.yml` calls the API every 5 minutes so that scheduled releases and grading happen even when nobody is online.

In GitHub, go to repo → Settings → Secrets and variables → Actions, and add:
- `AAYRA_API_URL`: your API URL
- `AAYRA_WORKER_SECRET`: the same value as the API's `AAYRA_WORKER_SECRET`

Until these are set, the workflow skips itself without doing anything.

## 5. Create demo logins to test every role

From your computer, in the repo's `backend` folder (needs [uv](https://docs.astral.sh/uv/)), run in PowerShell:

```powershell
cd backend
$env:AAYRA_DATABASE_URL = "postgresql+asyncpg://postgres.eufapsbufiibvfrlumre:YOUR_PASSWORD@aws-...pooler.supabase.com:6543/postgres"
$env:AAYRA_SUPABASE_URL = "https://eufapsbufiibvfrlumre.supabase.co"
$env:AAYRA_SUPABASE_SERVICE_ROLE_KEY = "YOUR_SERVICE_ROLE_KEY"
uv run python -m app.devtools.seed_live --email-domain yourdomain.com
```

This creates:
- the demo school (2026-27, Grade 10, class 10A, Biology + Mathematics)
- an admin and 3 teachers (Sarah, Ravi, Priya)
- 8 students with username logins (for example `demohs.10a001`)
- a parent linked to the first student

Every login and its **temporary password** is written to `demo_logins.csv`. Each person picks a new password at first sign-in. Keep the file private and delete it afterwards. Use a domain you control for `--email-domain`; no emails are sent to these addresses.

To test as an **independent adult learner**, click **"New here? Create an account"** on the sign-in page.

## 6. Running locally (optional)

```bash
./scripts/local_db.sh                                    # local Postgres from supabase/migrations
cd backend && AAYRA_BOOTSTRAP_KEY=local-key uv run python -m app.devtools.seed_demo
AAYRA_CORS_ORIGINS=http://localhost:5173 uv run uvicorn app.main:app --port 8000
cd ../web && cp .env.example .env.local   # set VITE_DEV_LOGIN=true
npm install && npm run dev                 # http://localhost:5173: pick any demo person to sign in
```

`VITE_DEV_LOGIN=true` and the API's `/dev/*` routes exist only locally (`AAYRA_ENVIRONMENT=local`, not on Vercel). The deployed site always uses real Supabase logins.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Web app: "Can't reach Aayra right now" | Check `VITE_API_URL`, and that `AAYRA_CORS_ORIGINS` on the API contains the exact web URL (no trailing slash). |
| Signed in, but asked to set up a learning space | This login has no Aayra account. Staff, parents and students must be created by the school (or via the seed script). Adults can create an independent space. |
| A lesson stays "Preparing" | Background jobs aren't running. Reload the page (it triggers them), or check step 4. |
| 401 "Invalid access token" | Set `AAYRA_SUPABASE_URL` on the API (signing keys), or `AAYRA_JWT_SECRET` (legacy secret). |
| Student username login fails | `VITE_STUDENT_LOGIN_DOMAIN` must equal the API's `AAYRA_STUDENT_LOGIN_DOMAIN`. If Supabase rejects `.invalid` addresses, set both to a subdomain you own, for example `students.yourdomain.com`. |
