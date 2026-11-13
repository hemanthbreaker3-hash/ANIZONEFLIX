"""Vercel serverless entry point for the website (never starts Telegram polling)."""
from __future__ import annotations

import os
import sys
from pathlib import Path

VERCEL_DIR = Path(__file__).resolve().parents[1]
ROOT = VERCEL_DIR.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Load optional local Vercel .env, then use config.py only as a fallback.
try:
    from dotenv import load_dotenv
    load_dotenv(VERCEL_DIR / ".env", override=False)
except ImportError:
    pass

from vercel.config import CONFIG

for key, value in CONFIG.items():
    if value not in (None, "") and not os.getenv(key):
        os.environ[key] = str(value)

# Vercel provides the hostname without a scheme. Respect an explicit BASE_URL,
# otherwise derive it from the platform-provided deployment URL.
if not os.getenv("BASE_URL", "").strip():
    hostname = os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL")
    if hostname:
        os.environ["BASE_URL"] = "https://" + hostname.removeprefix("https://").rstrip("/")
os.environ.setdefault("WEB_ONLY", "true")

from app import app  # noqa: E402
