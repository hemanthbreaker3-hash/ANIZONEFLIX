"""Optional Vercel-only settings.

Vercel Project Environment Variables take precedence over values in this file.
Do not commit real credentials. Fill only values you intentionally want to use
as local fallback configuration.
"""
import os

CONFIG = {
    "WEB_ONLY": "true",
    "BASE_URL": "https://anizoneflix-vercel.vercel.app/",  # Empty means detect VERCEL_URL automatically
    "MONGO_URI": "mongodb+srv://hemanthbreaker3_db_user:9550399779htr@cluster0.mysm1dt.mongodb.net/",
    "DB_NAME": "Cluster0",
    "SECRET_KEY": "anizoneflix",
    "CORS_ORIGINS": "https://anizoneflix-vercel.vercel.app/",
    "TMDB_API_KEY": "b1048f453055b9944a23e8fd411bb469",
}
