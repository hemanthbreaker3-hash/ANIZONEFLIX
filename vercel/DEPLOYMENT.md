# 🚀 ANIZONEFLIX — Vercel Website + Render Telegram Bot

This directory contains the Vercel-only configuration and serverless entry point.
The Telegram bot must run on Render (or another persistent worker), not Vercel.

## 1. Deploy the website on Vercel

1. Push the **same repository** to GitHub.
2. In Vercel, import the repository and keep **Root Directory = `.` (repository root)**.
3. Keep `vercel.json` at the repository root. It routes requests to `vercel/api/index.py`.
4. In **Project → Settings → Environment Variables**, add the values below.
5. Deploy or redeploy after changing environment variables.

### Required Vercel environment variables

| Name | Value |
| --- | --- |
| `WEB_ONLY` | `true` |
| `MONGO_URI` | Your MongoDB Atlas connection string |
| `DB_NAME` | `anizoneflix` |
| `SECRET_KEY` | A unique, long random secret (32+ random bytes) |
| `CORS_ORIGINS` | `https://your-production-domain.vercel.app` |

Optional: `TMDB_API_KEY`, `LOGO_URL`, and other values used by the app.

**BASE_URL automatic detection:** leave `BASE_URL` unset to use Vercel's
`VERCEL_PROJECT_PRODUCTION_URL` or `VERCEL_URL`. These are platform-provided
hostnames; the entry point adds `https://`. For stable links, you can explicitly
set `BASE_URL=https://your-production-domain.vercel.app`. A custom domain is also
supported when configured in Vercel.

## 2. Optional `config.py` or `.env` configuration

- `vercel/config.py` has a `CONFIG` dictionary. Fill values there only if you
  prefer file-based fallback configuration.
- Vercel Project Environment Variables take precedence over `config.py`.
- `vercel/.env` is for local testing only and is gitignored. Copy the keys from
  `.env.example`; never commit credentials or real database passwords.
- For production, use Vercel's Environment Variables instead of storing secrets
  in source files.

## 3. Deploy the bot on Render using the same repository

Create a separate Render service from the same GitHub repository. Use a persistent
Python web service/worker configuration appropriate to your app, and set:

| Name | Value |
| --- | --- |
| `WEB_ONLY` | `false` |
| `BASE_URL` | The stable Vercel production URL, e.g. `https://your-production-domain.vercel.app` |
| `MONGO_URI` | The same MongoDB connection string |
| `DB_NAME` | `anizoneflix` |
| `SECRET_KEY` | The same secret if shared signed tokens/sessions need to validate across services |
| `API_ID` | Telegram API ID |
| `API_HASH` | Telegram API hash |
| `BOT_TOKEN` | Telegram bot token |
| `ADMIN_IDS` | Comma-separated Telegram user IDs |

Use the repository's `render.yaml` or the start command documented in the root
`DEPLOYMENT.md`, after verifying it matches your chosen Render service type.

### Important: automatic URL behavior

Vercel knows its own deployment URL; Render does **not** automatically receive that
URL just because both services deploy the same repository. Set Render's `BASE_URL`
to the Vercel **production** URL once. This keeps links stable instead of pointing
to temporary preview deployments. The Vercel website itself auto-detects its URL
when `BASE_URL` is omitted.

## 4. Verify

- Open `/` on the Vercel domain.
- Open `/ping` if enabled by the app.
- Check Vercel Function Logs for Python import/runtime errors.
- Confirm MongoDB Atlas permits connections from the deployment environment.
- Send `/start` to the Telegram bot hosted on Render.

## 5. Deployment reliability notes

No configuration can guarantee 100% successful deployment in every account. Success
depends on valid secrets, MongoDB network access, supported dependencies, Vercel's
runtime limits, and third-party API availability. Do not run the Telegram polling
bot on Vercel; serverless functions can be suspended and are not a persistent worker.
