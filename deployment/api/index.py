"""Vercel ASGI entrypoint for the website-only deployment."""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.environ.setdefault("WEB_ONLY", "true")
from app import app  # noqa: E402
