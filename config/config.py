import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    API_ID = int(os.getenv("API_ID", 0))
    API_HASH = os.getenv("API_HASH", "")
    BOT_TOKEN = os.getenv("BOT_TOKEN", "")

    # Database Configuration
    MONGO_URI = os.getenv("MONGO_URI", "").strip()
    DB_NAME = os.getenv("DB_NAME", "anizoneflix")

    # Core Identity
    _vercel_host = os.getenv("VERCEL_PROJECT_PRODUCTION_URL") or os.getenv("VERCEL_URL", "")
    _auto_base_url = ("https://" + _vercel_host.removeprefix("https://").rstrip("/")) if _vercel_host else ""
    BASE_URL = os.getenv("BASE_URL", _auto_base_url).strip().rstrip("/")
    PORT = int(os.getenv("PORT", 10000))
    LOGO_URL = os.getenv("LOGO_URL", "https://telegra.ph/file/0c1737e466395b3531b78.jpg")

    # Security & Intelligence
    SECRET_KEY = os.getenv("SECRET_KEY", "").strip()
    ADMIN_API_KEY = os.getenv("ADMIN_API_KEY", "").strip()
    # Accept one or multiple admin IDs, including common deployment aliases.
    # Normalize whitespace and ignore malformed/empty values safely.
    _admin_ids_raw = ",".join(filter(None, [
        os.getenv("ADMIN_IDS", ""),
        os.getenv("ADMIN_ID", ""),
        os.getenv("OWNER_ID", ""),
        os.getenv("OWNER_IDS", ""),
    ]))
    ADMIN_IDS = []
    for _admin_id in _admin_ids_raw.split(","):
        try:
            _parsed_admin_id = int(_admin_id.strip())
            if _parsed_admin_id > 0 and _parsed_admin_id not in ADMIN_IDS:
                ADMIN_IDS.append(_parsed_admin_id)
        except (TypeError, ValueError):
            pass
    del _admin_ids_raw, _admin_id
    TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
    SIMKL_ID = os.getenv("SIMKL_ID", "")
    JIKAN_API = os.getenv("JIKAN_API", "https://api.jikan.moe/v4").rstrip("/")

    DEBUG = os.getenv("DEBUG", "False").lower() == "true"
    WEB_ONLY = os.getenv("WEB_ONLY", "false").lower() == "true"

    # Protection & Verification
    USE_SHORTLINK = os.getenv("USE_SHORTLINK", "False").lower() == "true"
    SHORTLINK_URL = os.getenv("SHORTLINK_URL", "") # e.g. api.shareus.io
    SHORTLINK_API = os.getenv("SHORTLINK_API", "")

    USE_VERIFY = os.getenv("USE_VERIFY", "False").lower() == "true"
    VERIFY_EXPIRE = int(os.getenv("VERIFY_EXPIRE", 86400)) # 24 hours

    USE_REFERER = os.getenv("USE_REFERER", "False").lower() == "true"
    REFERER_URL = os.getenv("REFERER_URL", "") # Expected referer domain

    @classmethod
    def validate(cls):
        """Validate critical production variables"""
        is_prod = "onrender.com" in cls.BASE_URL or "vercel.app" in cls.BASE_URL
        if is_prod and not cls.MONGO_URI:
            return False, "MONGO_URI is missing in production environment!"
        if not cls.SECRET_KEY:
            return False, "SECRET_KEY is missing! Set a strong random secret in your deployment environment."
        if not cls.MONGO_URI:
            return False, "MONGO_URI is missing! Set your MongoDB connection string."
        if not cls.WEB_ONLY and not cls.BOT_TOKEN:
            return False, "BOT_TOKEN is missing!"
        return True, "Configuration Validated."
