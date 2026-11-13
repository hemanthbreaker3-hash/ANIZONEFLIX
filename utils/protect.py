import time
import hmac
import hashlib
import aiohttp
import logging
from fastapi import Request, HTTPException
from fastapi.responses import HTMLResponse
from config.config import Config

logger = logging.getLogger("ANIZONEFLIX_PROTECT")

# --- VERIFICATION PAGE HTML ---

VERIFY_PAGE_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Human Verification - ANIZONEFLIX</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.0.0/css/all.min.css">
    <style>
        :root { --tg-theme-bg: #050505; --tg-accent: #e50914; }
        body { background-color: var(--tg-theme-bg); color: white; font-family: 'Inter', sans-serif; }
        .glass { background: rgba(15, 15, 15, 0.7); backdrop-filter: blur(20px); border: 1px solid rgba(255,255,255,0.08); }
    </style>
</head>
<body class="flex items-center justify-center min-h-screen p-4">
    <div class="max-w-md w-full glass p-10 rounded-[2.5rem] text-center shadow-2xl animate-fade-in">
        <div class="mb-8">
            <div class="relative inline-block">
                <div class="absolute -inset-1 bg-red-600 rounded-full blur opacity-25 animate-pulse"></div>
                <img src="{{ logo_url }}" class="w-20 h-20 rounded-full relative border border-white/10" alt="Logo">
            </div>
        </div>

        <h1 class="text-3xl font-black mb-4 tracking-tight uppercase">Security <span class="text-red-600">Check</span></h1>
        <p class="text-gray-400 text-sm mb-10 leading-relaxed font-medium">To maintain archive stability and prevent automated leeching, please complete the short verification below.</p>

        <div id="verify-area">
            <button onclick="startVerification()" class="w-full py-5 bg-red-600 hover:bg-red-700 text-white font-black rounded-2xl transition-all shadow-2xl shadow-red-900/40 uppercase tracking-widest flex items-center justify-center">
                <i class="fa-solid fa-shield-halved mr-3 text-xl"></i> VERIFY HUMAN IDENTITY
            </button>
        </div>

        <p class="mt-8 text-[10px] font-black text-gray-600 uppercase tracking-widest">Archive Protocol Active • 2026</p>
    </div>

    <script>
        function startVerification() {
            const btn = document.querySelector('button');
            btn.disabled = true;
            btn.innerHTML = '<i class="fa-solid fa-spinner fa-spin mr-3"></i> Redirecting...';
            window.location.href = "/verify/check?token={{ token }}";
        }
    </script>
</body>
</html>
"""

# --- PROTECTION LOGIC ---

class Protect:
    @staticmethod
    async def get_shortlink(url: str):
        if not Config.USE_SHORTLINK or not Config.SHORTLINK_API or not Config.SHORTLINK_URL:
            return url

        try:
            api_url = f"https://{Config.SHORTLINK_URL}/api?api={Config.SHORTLINK_API}&url={url}"
            async with aiohttp.ClientSession() as session:
                async with session.get(api_url, timeout=5) as resp:
                    if resp.status == 200:
                        res = await resp.json()
                        if res.get("status") == "success":
                            return res.get("shortenedUrl")
        except Exception as e:
            logger.error(f"Shortlink Error: {e}")
        return url

    @staticmethod
    def generate_verify_token(user_ip: str):
        """Simple HMAC based token for verification"""
        timestamp = int(time.time())
        msg = f"{user_ip}|{timestamp}"
        signature = hmac.new(Config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return f"{msg}|{signature}"

    @staticmethod
    def validate_verify_token(token: str, user_ip: str):
        try:
            parts = token.split("|")
            if len(parts) != 3: return False
            ip, timestamp, signature = parts[0], parts[1], parts[2]

            # Check IP and Timeout
            if ip != user_ip: return False
            if int(time.time()) - int(timestamp) > 300: # 5 min limit to click
                return False

            # Validate Signature
            msg = f"{ip}|{timestamp}"
            expected = hmac.new(Config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except:
            return False

    @staticmethod
    def is_verified(request: Request):
        if not Config.USE_VERIFY: return True

        cookie = request.cookies.get("verify_token")
        if not cookie: return False

        try:
            parts = cookie.split(":")
            if len(parts) != 2: return False
            timestamp, signature = parts[0], parts[1]

            # Check Expiry
            if int(time.time()) - int(timestamp) > Config.VERIFY_EXPIRE:
                return False

            # Validate Signature (Include User-Agent or IP for better security)
            msg = f"{timestamp}"
            expected = hmac.new(Config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
            return hmac.compare_digest(signature, expected)
        except:
            return False

    @staticmethod
    def check_referer(request: Request):
        if not Config.USE_REFERER or not Config.REFERER_URL: return True

        referer = request.headers.get("referer", "")
        if Config.REFERER_URL in referer:
            return True
        return False

    @staticmethod
    def create_verified_cookie():
        timestamp = int(time.time())
        msg = f"{timestamp}"
        signature = hmac.new(Config.SECRET_KEY.encode(), msg.encode(), hashlib.sha256).hexdigest()
        return f"{timestamp}:{signature}"
