# ANIZONEFLIX Vercel deployment notes

## Entry point
`vercel.json` builds `vercel/api/index.py` with `@vercel/python`, and every
request routes there. That file:
- adds the project root to `sys.path`,
- loads `vercel/.env` locally (if present) via `python-dotenv`,
- falls back to `vercel/config.py`'s `CONFIG` dict for any environment
  variable not already set (the dict ships empty by default — see below),
- derives `BASE_URL` from Vercel's `VERCEL_URL` / `VERCEL_PROJECT_PRODUCTION_URL`
  when `BASE_URL` isn't set explicitly,
- forces `WEB_ONLY=true` so the Telegram bot never starts polling on Vercel,
- then imports the FastAPI `app` from the project root `app.py`.

`vercel.json` also sets `includeFiles` for `templates/**` and `static/**` so
the Jinja2 templates and static assets are bundled with the function.

## Credentials
`vercel/config.py` is a **local fallback only** and must never contain real
secrets — its values ship empty. Set the real values in Vercel → Project →
Settings → Environment Variables instead:
- `MONGO_URI`: your MongoDB connection string (keep it private)
- `DB_NAME`: your actual database name
- `BASE_URL`: e.g. `https://your-project.vercel.app`
- `SECRET_KEY`: a long random secret
- `CORS_ORIGINS`: your site origin
- `TMDB_API_KEY`: your TMDB API key
- `WEB_ONLY`: `true`

If you ever hardcode real credentials here for local testing, rotate them
before committing — anything in this repo should be treated as public.

For MongoDB Atlas, make sure the network/IP allowlist permits Vercel's
serverless outbound traffic.

## Deploy
1. Push the contents of this project to the GitHub repository connected to
   Vercel, with `vercel.json`, `vercel/`, `app.py`, `config/`, `database/`,
   `templates/`, and `static/` at the repository root.
2. Set the environment variables above in the Vercel dashboard.
3. Redeploy (use **Clear build cache** if an old deployment keeps appearing).
4. Test `/ping`; it should return JSON. `db:false` means the app deployed
   but MongoDB credentials/network/database still need attention.
