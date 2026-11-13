# Vercel website deployment (ANIZONEFLIX)

This folder contains the Vercel Python ASGI entrypoint. The website runs in `WEB_ONLY` mode so it does not start the Telegram bot. Deploy the repository root on Vercel and keep `vercel.json` at the repository root; do not set `deployment/` as the project root because the app, templates, and static assets live at the repository root.

## Required Vercel environment variables

- `WEB_ONLY=true`
- `MONGO_URI` — MongoDB connection string (never commit this value)
- `DB_NAME=anizoneflix` (or your existing database name)
- `SECRET_KEY` — long, random secret (at least 32 random bytes)
- `BASE_URL` — your Vercel website URL
- `TMDB_API_KEY` — optional

In MongoDB Atlas, allow network access from your deployment as required by your plan, create a least-privilege database user, and verify the database name and URI. Do not expose credentials in Git. Rotate any database password or signing key that was previously committed to source control.

## Deployment split

- **Vercel:** website only (`WEB_ONLY=true`); it does not run the Telegram bot.
- **Render:** bot/service deployment using the existing `render.yaml`, `WEB_ONLY=false`, and your bot credentials. Use a persistent external MongoDB database shared with the website.

Vercel serverless functions are not intended for long-running Telegram polling/background tasks. Verify `/ping`, `/`, `/search`, and `/api/search` after deployment. MongoDB-backed pages require a valid reachable MongoDB instance; the website can serve its shell during an outage but cannot show database content until the connection recovers.
