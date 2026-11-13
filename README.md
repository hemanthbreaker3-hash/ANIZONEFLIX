# AniZoneFlix

AniZoneFlix is an anime catalogue website with a Telegram management bot. The website and bot can share one MongoDB database while running as separate services:

- **Vercel:** website only. The Telegram bot is not started here.
- **Render:** Telegram bot/service. Set `BASE_URL` to the public Vercel website URL so the bot sends users to the correct website.
- **MongoDB Atlas:** persistent database shared by both services.

> Deployments depend on valid credentials, compatible dependencies, and correct provider settings. Always test the deployed website and bot after setup; this README does not guarantee that a third-party deployment provider will accept every configuration.

## Contents

- [Features](#features)
- [Architecture](#deployment-architecture)
- [Requirements](#requirements)
- [1. Prepare MongoDB Atlas](#1-prepare-mongodb-atlas)
- [2. Deploy the website to Vercel](#2-deploy-the-website-to-vercel)
- [3. Set the website Base URL](#3-set-the-website-base-url)
- [4. Deploy the Telegram bot to Render](#4-deploy-the-telegram-bot-to-render)
- [5. Configure environment variables](#environment-variables)
- [6. Verify the deployment](#6-verify-the-deployment)
- [Local setup](#local-development)
- [Termux (Android) bot deploy](#termux-android-bot-deploy)
- [VPS / Linux bot deploy](#vps--linux-bot-deploy)
- [Troubleshooting](#troubleshooting)
- [Security checklist](#security-checklist)

## Features

- Responsive anime catalogue and detail pages.
- Search and anime metadata integration with supported external providers.
- Telegram bot commands for managing pages, categories, posters, buttons, and posts.
- MongoDB-backed content storage and backup/restore functionality.
- Optional uptime monitoring and background music features.

Available bot commands depend on the configured services and permissions. Send `/help` to the bot after deployment for its in-app command guide.

## Deployment architecture

```text
Visitor's browser
       |
       v
Vercel website  ------------------+
                                   |
                                   v
                             MongoDB Atlas
                                   ^
                                   |
Telegram users ---> Telegram bot on Render
                                   |
                      BASE_URL points to Vercel
```

Use the **Vercel website URL** as `BASE_URL` on both services. Do not use the Render bot-service URL as `BASE_URL` unless you intentionally want links to point to Render.

## Requirements

- A GitHub repository containing this project.
- A Vercel account for the website.
- A Render account for the bot.
- A MongoDB Atlas cluster and database user.
- A Telegram bot token from [@BotFather](https://t.me/BotFather).
- Telegram API ID and API hash from [my.telegram.org](https://my.telegram.org).
- Your Telegram numeric user ID for `ADMIN_IDS`.

Optional integrations may require their own API keys.

## 1. Prepare MongoDB Atlas

1. Create a project and cluster at [MongoDB Atlas](https://www.mongodb.com/atlas/database).
2. Open **Database Access** and create a dedicated database user with a strong password. Do not use your Atlas account password.
3. Open **Network Access** and allow connections from your hosting services. For a quick test, Atlas may offer `0.0.0.0/0`, but that exposes the database endpoint to all IP addresses and should only be used if you understand the risk. Prefer provider-supported static egress IPs or a restricted network configuration where available.
4. Choose **Connect → Drivers** and copy the MongoDB connection string. Replace the username, password, and database name placeholders. URL-encode special characters in the password.
5. Use a database name such as `anizoneflix`.

Example format (replace every placeholder; this is not a real credential):

```text
mongodb+srv://DB_USER:URL_ENCODED_PASSWORD@YOUR_CLUSTER_HOST/anizoneflix?retryWrites=true&w=majority
```

Never commit the real URI to GitHub, paste it into public issues, or include it in screenshots. If a URI or password has already been exposed, rotate it in Atlas immediately.

## 2. Deploy the website to Vercel

The repository includes `vercel.json` and `deployment/api/index.py` for the website-only entry point. **Set the Vercel project root to the repository root**, not the `deployment/` folder; the app, templates, and static assets are located at the repository root.

1. Push the project to a GitHub repository.
2. In Vercel, select **Add New → Project** and import that repository.
3. Keep the **Root Directory** set to `./` (the repository root).
4. Use the Python runtime/configuration defined by the repository's `vercel.json`. If Vercel displays build settings, do not override the configured entry point without a reason.
5. Open **Settings → Environment Variables** and add the website variables listed below. Set `WEB_ONLY` to `true`.
6. Add the variables to the environments you use (Production, and Preview only if needed), then redeploy after changing them.
7. Deploy and copy the public HTTPS domain Vercel assigns, for example `https://your-project.vercel.app`.

### Required Vercel environment variables

| Variable | Value |
|---|---|
| `WEB_ONLY` | `true` |
| `BASE_URL` | Your public Vercel website URL, without a trailing slash |
| `MONGO_URI` | Your private MongoDB Atlas connection string |
| `DB_NAME` | `anizoneflix` (or your chosen database name) |
| `SECRET_KEY` | A long, random secret used for signing/security functions |
| `ADMIN_IDS` | Comma-separated Telegram numeric admin IDs if the website uses them; otherwise leave empty if allowed |
| `LOGO_URL` | Optional public URL for the fallback logo/poster |

The Telegram API credentials and bot token are **not needed for the website-only Vercel service**. Do not add `BOT_TOKEN` to Vercel unless you have a separate, explicit reason to do so.

Generate a secret locally with one of these commands:

```bash
# Linux/macOS
python -c "import secrets; print(secrets.token_urlsafe(48))"

# OpenSSL alternative
openssl rand -hex 32
```

Use the generated value as `SECRET_KEY`; do not use the example text as a real secret. Keep the same secret across services if they need to validate the same signed tokens. If they do not share signed sessions/tokens, use distinct secrets where appropriate.

## 3. Set the website Base URL

`BASE_URL` is the canonical public address of the **website**. The bot uses it when generating links such as `/anime/<slug>` and when showing the website/access-portal button.

Example:

```text
BASE_URL=https://your-project.vercel.app
```

Rules:

- Include `https://`.
- Do not add a trailing slash.
- Do not add `/api`, `/ping`, or an individual page path.
- Use the final custom domain instead if you connect one to Vercel, for example `https://anime.example.com`.
- If the Vercel domain changes, update `BASE_URL` on both Vercel and Render and redeploy/restart the affected services.

**Important:** `BASE_URL` is a setting, not a domain provider. Deploy the website first, obtain the actual Vercel domain, and then enter that exact URL in the environment variables.

## 4. Deploy the Telegram bot to Render

The repository contains a `render.yaml` blueprint. The bot must run separately from the Vercel website because Vercel's serverless runtime is not intended for a persistent Telegram polling process.

1. Open Render and create a **Blueprint** from the GitHub repository, or create a Python Web Service manually.
2. If deploying manually, use:
   - **Build command:** `pip install -r requirements.txt`
   - **Start command:** `python main.py`
3. Configure the environment variables in Render. Set `WEB_ONLY=false` and provide `API_ID`, `API_HASH`, `BOT_TOKEN`, `MONGO_URI`, `DB_NAME`, `SECRET_KEY`, `ADMIN_IDS`, and `BASE_URL`.
4. Set `BASE_URL` to the Vercel website domain from Step 2, not the Render service domain.
5. Use the same `MONGO_URI` and `DB_NAME` as the website if the bot and website must display and manage the same content.
6. Deploy and review Render logs for startup errors. Open your bot in Telegram and send `/start`.

If you use a Render **Web Service**, it may need to bind to the port provided by the platform. This project reads `PORT` and defaults to `10000`; check the logs if the service fails to bind. Keep the bot on a continuously running service if it relies on long polling or background tasks.

### Required Render environment variables

| Variable | Value |
|---|---|
| `WEB_ONLY` | `false` |
| `BASE_URL` | The public Vercel website URL |
| `MONGO_URI` | The same private Atlas URI used by the website |
| `DB_NAME` | `anizoneflix` |
| `SECRET_KEY` | A strong secret; use the same value if signed tokens must be shared |
| `API_ID` | Telegram API ID from `my.telegram.org` |
| `API_HASH` | Telegram API hash from `my.telegram.org` |
| `BOT_TOKEN` | Token from `@BotFather` |
| `ADMIN_IDS` | Telegram numeric admin IDs, separated by commas |
| `LOGO_URL` | Optional public logo URL |
| `TMDB_API_KEY` | Optional; only if using TMDB integration |
| `SIMKL_ID` | Optional; only if using Simkl integration |

In Render's dashboard, set secret values as private environment variables. Do not put real tokens in `render.yaml` or source code.

## Environment variables

The `.env.example` file is a template only. Copy the relevant settings into your hosting provider's environment-variable dashboard. Do not commit a populated `.env` file.

### Website-only `.env` example

```dotenv
WEB_ONLY=true
BASE_URL=https://your-project.vercel.app
DB_NAME=anizoneflix
MONGO_URI=mongodb+srv://DB_USER:URL_ENCODED_PASSWORD@YOUR_CLUSTER_HOST/anizoneflix?retryWrites=true&w=majority
SECRET_KEY=REPLACE_WITH_A_LONG_RANDOM_SECRET
ADMIN_IDS=123456789
LOGO_URL=https://example.com/logo.png
```

### Bot service environment example

```dotenv
WEB_ONLY=false
BASE_URL=https://your-project.vercel.app
DB_NAME=anizoneflix
MONGO_URI=mongodb+srv://DB_USER:URL_ENCODED_PASSWORD@YOUR_CLUSTER_HOST/anizoneflix?retryWrites=true&w=majority
SECRET_KEY=REPLACE_WITH_A_LONG_RANDOM_SECRET
API_ID=123456
API_HASH=REPLACE_WITH_TELEGRAM_API_HASH
BOT_TOKEN=REPLACE_WITH_BOTFATHER_TOKEN
ADMIN_IDS=123456789
LOGO_URL=https://example.com/logo.png
TMDB_API_KEY=
SIMKL_ID=
```

These are examples with placeholders. Replace them with your own values; never copy placeholder values as working credentials.

## 6. Verify the deployment

After both services deploy:

1. Open the Vercel website URL in a private/incognito browser window.
2. Check the home page and test the search field on desktop and mobile.
3. Open a known anime detail page and confirm its images, buttons, and links load.
4. Test the project's `/ping` route if enabled. Review the response and logs; a successful HTTP response alone does not prove every database query works.
5. In Render, confirm the service stays running and the logs do not show MongoDB authentication, DNS, or Telegram authorization errors.
6. Send `/start` and `/help` to the Telegram bot. Use a management command only with an authorized admin account.
7. Create or update a test entry, then verify that the change appears on the website. This confirms that the bot and website are using the intended database.
8. Test after a redeploy and check that existing content remains in MongoDB.

## Local development

Use a supported Python version compatible with the pinned/project dependencies.

```bash
git clone YOUR_GITHUB_REPOSITORY_URL
cd YOUR_REPOSITORY_DIRECTORY
python -m venv .venv
```

Activate the environment:

```bash
# Linux/macOS
source .venv/bin/activate

# Windows PowerShell
.venv\Scripts\Activate.ps1
```

Install dependencies and create a local `.env` file based on `.env.example`:

```bash
pip install -r requirements.txt
python main.py
```

For local website-only testing, set `WEB_ONLY=true`, configure `MONGO_URI`, `DB_NAME`, and `SECRET_KEY`, then run `python main.py`. For local bot testing, set `WEB_ONLY=false` and supply valid Telegram credentials. Do not use production secrets in an untrusted development environment.

## Termux (Android) bot deploy

Run the **Telegram bot only** on your phone with Termux. The website stays on Vercel (or any host); the bot on Termux talks to the same MongoDB and uses `BASE_URL` to link users to the site.

### One-paste install (after you push this repo to GitHub)

1. Install [Termux](https://f-droid.org/en/packages/com.termux/) from F-Droid (recommended) or the official source.
2. Open Termux and paste **this single command** (replace the GitHub URL with yours):

```bash
pkg update -y && pkg install -y git && git clone https://github.com/YOUR_USERNAME/ANIZONEFLIX.git && cd ANIZONEFLIX && bash deploy.termux
```

3. When the script opens `nano`, fill in:
   - `API_ID` / `API_HASH` → from [my.telegram.org](https://my.telegram.org)
   - `BOT_TOKEN` → from [@BotFather](https://t.me/BotFather)
   - `ADMIN_IDS` → your numeric Telegram user ID
   - `MONGO_URI` → same Atlas URI used by the website
   - `SECRET_KEY` → any long random string
   - `BASE_URL` → your live website URL (e.g. `https://your-project.vercel.app`)
4. Save (`Ctrl+X` → `Y` → Enter). The bot starts automatically.
5. Keep Termux open (or run `termux-wake-lock` / use a wake-lock app). Send `/start` to your bot in Telegram.

### Re-run later

```bash
cd ANIZONEFLIX
bash deploy.termux
```

The script skips package install when possible, keeps your existing `.env`, forces `WEB_ONLY=false`, and starts the bot with `python main.py`.

> **Note:** Termux is for the **bot process only**. Do not set `WEB_ONLY=true` here. Public website traffic should stay on Vercel/Render.

## VPS / Linux bot deploy

Run the **Telegram bot only** on any Ubuntu/Debian VPS. Default mode uses **Docker** (`Dockerfile` + `docker-compose.yml`). The website stays on Vercel; the bot shares MongoDB and uses `BASE_URL`.

### Recommended: export credentials then one command

```bash
# 1. Clone
sudo apt update -y && sudo apt install -y git
git clone https://github.com/YOUR_USERNAME/ANIZONEFLIX.git
cd ANIZONEFLIX

# 2. Export your secrets (they are written into .env automatically)
export API_ID=12345678
export API_HASH=your_api_hash_here
export BOT_TOKEN=123456:AAHxxxxxx
export ADMIN_IDS=987654321
export MONGO_URI="mongodb+srv://user:pass@cluster.mongodb.net/anizoneflix"
export BASE_URL=https://your-project.vercel.app
export SECRET_KEY=$(openssl rand -hex 32)   # optional — auto-generated if empty

# 3. Deploy (Docker by default)
bash deploy.vps
```

Lowercase aliases also work (`export bot_token=...`, `export api_id=...`, etc.).

### What the script does

1. Installs Docker (if missing) or system Python packages
2. Creates/updates `.env` from your `export`s and forces `WEB_ONLY=false`
3. Builds the image from `Dockerfile` and starts the container via `docker compose`
4. Restarts automatically (`restart: unless-stopped`)

### Useful Docker commands after deploy

```bash
docker compose logs -f          # live logs
docker compose ps               # status
docker compose restart          # restart bot
docker compose down             # stop
docker logs -f anizoneflix-bot
```

### Native mode (no Docker)

```bash
DEPLOY_MODE=native bash deploy.vps
```

Uses a Python venv + optional `screen` session named `anizone`.

### Re-run / update credentials

```bash
export BOT_TOKEN=new_token_here
# ... other exports if needed
bash deploy.vps
```

> **Note:** Bot-only. Do not set `WEB_ONLY=true` on the VPS. Keep public website traffic on Vercel.

## Troubleshooting

### Website loads, but content is empty or database errors appear

- Check `MONGO_URI` and `DB_NAME` on Vercel and Render.
- Verify the Atlas database username/password and URL-encode special characters in the password.
- Check Atlas Network Access and database-user permissions.
- Review the provider's runtime logs for DNS, TLS, authentication, or timeout errors.
- Ensure the bot and website point to the same database if they are meant to share content.

### The bot sends incorrect or old website links

- Set `BASE_URL` to the exact public Vercel URL, including `https://` and without a trailing slash.
- Update the value on Render and redeploy/restart the bot service.
- If using a custom domain, ensure it is connected and working in Vercel before setting it as `BASE_URL`.

### Vercel deployment fails

- Ensure the Vercel Root Directory is the repository root.
- Ensure `vercel.json`, `app.py`, `templates/`, `static/`, and `deployment/api/index.py` are present in the repository.
- Check build/runtime logs and confirm all dependencies in `requirements.txt` install successfully.
- Verify all required environment variables are set in the Vercel environment that you deployed.
- Remember that serverless runtimes have execution and resource limits; persistent polling/background work belongs on Render or another long-running host.

### Render service exits or restarts repeatedly

- Check the start command is `python main.py` and that dependencies installed successfully.
- Check `WEB_ONLY=false` and the Telegram credentials are valid.
- Review logs for invalid tokens, missing environment variables, MongoDB connectivity, or port-binding errors.
- Do not run the same polling bot in multiple instances unless the bot implementation is designed for that setup.

### Telegram bot does not respond

- Confirm the bot token is current and belongs to the expected bot.
- Confirm `API_ID` and `API_HASH` are correct.
- Check Render logs and ensure only the intended bot process is running.
- Confirm the user has started the bot in Telegram before expecting private messages.

## Security checklist

- [ ] Rotate any MongoDB password or token that has previously been exposed.
- [ ] Use a dedicated MongoDB database user with minimum required permissions.
- [ ] Restrict MongoDB network access as much as your hosting architecture allows.
- [ ] Keep `.env`, API keys, bot tokens, and secret keys out of Git.
- [ ] Use a long, random `SECRET_KEY`; never use the sample value.
- [ ] Keep Python packages updated and review deployment logs regularly.
- [ ] Add server-side rate limiting, input validation, and monitoring for public endpoints. Browser-side scripts cannot reliably block all scrapers, automated clients, or malicious traffic.
- [ ] Configure provider/CDN firewall and bot protections where available, while avoiding blanket browser blocking that prevents legitimate users from accessing the site.
- [ ] Back up MongoDB before major updates and test restore procedures.

## License and disclaimer

Use this project only where you have the rights and permissions to host and distribute the content. You are responsible for your deployment, secrets, database, third-party API usage, and compliance with applicable laws and service terms.
