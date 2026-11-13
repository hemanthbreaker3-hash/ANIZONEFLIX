from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
from database.db import db, clean_doc
from config.config import Config
from api.anime_api import anime_api
import os
import logging
import traceback
import asyncio
import sys
from bot import bot, set_commands, register_handlers
from utils.auth import get_current_admin, verify_token
from utils.utils import slugify
from utils.protect import Protect, VERIFY_PAGE_HTML
from fastapi.responses import RedirectResponse, JSONResponse, Response, HTMLResponse
from contextlib import asynccontextmanager

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ANIZONEFLIX_APP")

# --- GLOBAL ERROR HANDLING ---

def handle_exception(exc_type, exc_value, exc_traceback):
    if issubclass(exc_type, KeyboardInterrupt):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return
    logger.critical("Uncaught exception", exc_info=(exc_type, exc_value, exc_traceback))

sys.excepthook = handle_exception

@asynccontextmanager
async def lifespan(app: FastAPI):
    # STARTUP
    logger.info("AniZoneFlix Production Engine starting...")
    try:
        try:
            await db.connect()
        except Exception as db_error:
            # Keep public pages available during a temporary database outage.
            # Database-backed routes return empty results until MongoDB recovers.
            logger.error("MongoDB unavailable at startup: %s", db_error)
        if Config.WEB_ONLY:
            logger.info("WEB_ONLY enabled: website mode; Telegram bot startup is disabled.")
            yield
            return
        loop = asyncio.get_running_loop()
        bot.loop = loop
        if hasattr(bot, "dispatcher"):
            bot.dispatcher.loop = loop

        def loop_exception_handler(loop, context):
            msg = context.get("exception", context["message"])
            logger.error(f"Async Task Error: {msg}")
        loop.set_exception_handler(loop_exception_handler)

        register_handlers(bot)
        await bot.start()
        await set_commands(bot)

        # Dynamic load added bots
        from bot.bot_manager import added_bot_manager
        asyncio.create_task(added_bot_manager.start_all())

        # Start continuous 1-second Uptime Monitoring Worker
        from utils.uptime import start_uptime_monitor
        start_uptime_monitor()

        me = await bot.get_me()
        logger.info(f"Production Suite LIVE -> @{me.username}")
    except Exception as e:
        logger.critical(f"STARTUP FAILURE: {e}")
        logger.error(traceback.format_exc())
        # In bot mode, fail application startup instead of leaving a healthy-looking
        # web process running with a disconnected Telegram bot. systemd can then retry.
        if not Config.WEB_ONLY:
            raise

    yield

    # SHUTDOWN
    logger.info("Production Engine shutting down...")
    try:
        if not Config.WEB_ONLY:
            from bot.bot_manager import added_bot_manager
            await added_bot_manager.stop_all()
            if bot.is_connected:
                await bot.stop()
        await anime_api.close()
        if getattr(db, "client", None) is not None:
            db.client.close()
            db.client = None
    except Exception as e:
        logger.error(f"Shutdown error: {e}")

# --- APP INITIALIZATION ---

app = FastAPI(title="ANIZONEFLIX", lifespan=lifespan)

_cors_origins = [origin.strip() for origin in os.getenv("CORS_ORIGINS", Config.BASE_URL).split(",") if origin.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=False,
    allow_methods=["GET", "HEAD", "POST", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "X-Requested-With"],
)
app.add_middleware(GZipMiddleware, minimum_size=1000)

def button_link_rewrite(url: str) -> str:
    """
    Safely rewrites any website page link (/anime/{slug}) in buttons
    to point to the current Base URL.
    Excludes Telegram (t.me) and other external links.
    """
    if not url:
        return ""
    import re
    if "/anime/" in url and not any(ext in url for ext in ["t.me", "telegram.me", "telegram.dog"]):
        slug_match = re.search(r'/anime/([a-zA-Z0-9-]+)', url)
        if slug_match:
            slug = slug_match.group(1)
            return f"{Config.BASE_URL}/anime/{slug}"
    return url

@app.middleware("http")
async def add_security_headers_and_rewrite_links(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' cdn.tailwindcss.com cdn.jsdelivr.net cdnjs.cloudflare.com; "
        "style-src 'self' 'unsafe-inline' fonts.googleapis.com cdnjs.cloudflare.com cdn.jsdelivr.net; "
        "font-src 'self' fonts.gstatic.com cdnjs.cloudflare.com; "
        "img-src * data:; "
        "connect-src 'self' *; "
        "frame-src 'self' www.youtube.com;"
    )
    # Optimization: Cache static assets
    if request.url.path.startswith("/static"):
        response.headers["Cache-Control"] = "public, max-age=31536000, immutable"

    return response

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")
templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # Browsers probe this path directly regardless of the <link rel="icon"> tag.
    # Redirect to the configured logo when set, otherwise return no content so
    # the request stops showing up as a 404 in the logs.
    if Config.LOGO_URL:
        return RedirectResponse(url=Config.LOGO_URL)
    return Response(status_code=204)

def t_me_to_telegram_me(url: str) -> str:
    """
    Safely replaces t.me with telegram.me in any given URL.
    """
    if not url:
        return ""
    import re
    return re.sub(r'\bt\.me\b', 'telegram.me', url)

# Override templates.TemplateResponse to automatically rewrite all t.me links to telegram.me in HTML output
_original_template_response = templates.TemplateResponse

def custom_template_response(*args, **kwargs):
    response = _original_template_response(*args, **kwargs)
    if hasattr(response, "body"):
        import re
        html = response.body.decode("utf-8", errors="ignore")
        # Replace t.me and urlencoded t%2Eme with telegram.me and telegram%2Eme globally
        html = re.sub(r'\bt\.me\b', 'telegram.me', html)
        html = re.sub(r't%2[eE]me\b', 'telegram%2Eme', html)
        encoded = html.encode("utf-8")
        response._body = encoded
        response.headers["content-length"] = str(len(encoded))
    return response

templates.TemplateResponse = custom_template_response

templates.env.filters["slugify"] = slugify
templates.env.filters["button_link_rewrite"] = button_link_rewrite
templates.env.filters["t_me_to_telegram_me"] = t_me_to_telegram_me

# --- CUSTOM RESPONSES ---

def safe_api_response(success=True, data=None, message=""):
    return JSONResponse(content={
        "success": success,
        "data": data or [],
        "message": message
    })

# --- ERROR HANDLERS ---

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    logger.error(f"ROUTE ERROR: {request.url.path} -> {exc}")
    if "api" in request.url.path:
        return safe_api_response(False, None, "Internal Server Error")
    # CRITICAL: Always use keyword arguments for TemplateResponse
    return templates.TemplateResponse(request=request, name="404.html", context={"error": "Internal Server Error"}, status_code=500)

# --- WEB ROUTES ---

async def get_site_logo_url():
    """Use the admin-configured logo, falling back to the deployment default."""
    try:
        return await db.get_setting("site_logo_url", Config.LOGO_URL) or Config.LOGO_URL
    except Exception:
        return Config.LOGO_URL

@app.api_route("/", methods=["GET", "HEAD"])
async def index(request: Request):
    if request.method == "HEAD":
        is_healthy = await db.ping() and (Config.WEB_ONLY or bot.is_connected)
        return Response(status_code=200 if is_healthy else 503)

    try:
        trending = await db.get_all_anime(limit=15)
        recent = await db.get_all_anime(limit=20)
        categories = await db.get_all_categories()
        logo_url = await get_site_logo_url()

        return templates.TemplateResponse(request=request, name="index.html", context={
            "trending": trending or [],
            "recent": recent or [],
            "categories": categories or [],
            "logo_url": logo_url,
            "site_name": "ANIZONEFLIX"
        })
    except Exception as e:
        logger.error(f"Index error: {e}")
        return templates.TemplateResponse(request=request, name="index.html", context={
            "trending": [], "recent": [], "categories": [],
            "logo_url": await get_site_logo_url(), "site_name": "ANIZONEFLIX"
        })

@app.get("/ping")
async def health_ping():
    return safe_api_response(True, {"status": "ok", "db": await db.ping(), "bot": ("disabled" if Config.WEB_ONLY else bot.is_connected)})

@app.get("/schedule")
async def schedule_page(request: Request):
    try:
        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        schedules = {}
        for day in days:
            schedules[day] = await db.get_schedule(day)

        categories = await db.get_all_categories()
        return templates.TemplateResponse(request=request, name="schedule.html", context={
            "schedules": schedules,
            "categories": categories or [],
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })
    except Exception as e:
        logger.error(f"Schedule page error: {e}")
        return templates.TemplateResponse(request=request, name="schedule.html", context={
            "schedules": {}, "categories": [],
            "logo_url": await get_site_logo_url(), "site_name": "ANIZONEFLIX"
        })

@app.get("/anime/{slug}")
async def anime_detail(request: Request, slug: str):
    try:
        anime = await db.get_anime_by_slug(slug)
        if not anime:
            return templates.TemplateResponse(request=request, name="404.html", context={"error": "Title not found."}, status_code=404)

        categories = await db.get_all_categories()
        episodes = await db.get_episodes(anime.get("mal_id", "0"))

        return templates.TemplateResponse(request=request, name="details.html", context={
            "anime": anime,
            "episodes": episodes or [],
            "categories": categories or [],
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })
    except Exception as e:
        logger.error(f"Detail error for {slug}: {e}")
        return templates.TemplateResponse(request=request, name="404.html", context={"error": "Database error."}, status_code=500)

@app.get("/api/songs")
async def get_songs_api():
    try:
        data = await db.get_all_songs()
        return safe_api_response(True, data)
    except Exception as e:
        return safe_api_response(False, None, str(e))

@app.get("/api/songs/stream/{song_id}")
async def stream_song_api(song_id: str):
    try:
        song = await db.get_song(song_id)
        if not song or not song.get("file_path"):
            raise HTTPException(status_code=404, detail="Song not found")
        file_path = song.get("file_path", "").lstrip("/")
        full_path = os.path.join(".", file_path)
        if not os.path.exists(full_path):
            raise HTTPException(status_code=404, detail="Song file missing")
        from fastapi.responses import FileResponse
        return FileResponse(full_path)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/anime")
async def get_anime_api(skip: int = 0, limit: int = 18):
    try:
        limit = max(1, min(int(limit), 48))
        skip = max(0, int(skip))
        data = await db.get_all_anime(limit=limit, skip=skip)
        return safe_api_response(True, data)
    except Exception as e:
        return safe_api_response(False, None, str(e))

@app.get("/api/search")
async def get_search_api(q: str = "", skip: int = 0, limit: int = 24):
    # Keep each request bounded; clients can request later pages without loading
    # the entire collection into application memory.
    limit = max(1, min(limit, 48))
    skip = max(0, skip)
    q = q.strip()[:120]
    try:
        if not q:
            return safe_api_response(True, await db.get_all_anime(limit=limit, skip=skip))

        results = []
        if await db.ping():
            # Search and paginate at the database layer, not by loading a
            # truncated in-memory list. Use intelligent matching on page one
            # to keep relevance ranking; use direct MongoDB pagination later.
            if skip == 0:
                results = await db.search_anime_intelligent(q, limit=max(limit + skip, 150))
                if results:
                    return safe_api_response(True, results[skip:skip + limit])
            else:
                results = await db.search_anime_page(q, skip=skip, limit=limit)
                if results:
                    return safe_api_response(True, results)

        # If the catalogue has no match or MongoDB is temporarily unavailable,
        # use public anime metadata providers so titles such as Naruto still show.
        # External results link to their canonical provider page, not a broken
        # local detail route. Keep this fallback limited to the first page.
        if skip == 0:
            external = await anime_api.search_all(q)
            mapped = []
            provider_urls = {
                "jikan": lambda item: f"https://myanimelist.net/anime/{item['id']}",
                "anilist": lambda item: f"https://anilist.co/anime/{item['id']}",
                "kitsu": lambda item: f"https://kitsu.io/anime/{item['id']}",
                "shikimori": lambda item: f"https://shikimori.one/animes/{item['id']}",
                "tmdb": lambda item: f"https://www.themoviedb.org/tv/{item['id']}",
            }
            for item in external:
                source = item.get("source", "")
                item_id = item.get("id")
                title = item.get("title")
                if not title or source not in provider_urls or item_id is None:
                    continue
                mapped.append({
                    "title": title,
                    "slug": f"external-{source}-{item_id}",
                    "image": item.get("image"),
                    "year": item.get("year"),
                    "score": None,
                    "category": "Anime",
                    "status": "External result",
                    "external_url": provider_urls[source](item),
                    "source": source,
                })
            return safe_api_response(True, mapped[:limit])
        return safe_api_response(True, [])
    except Exception as e:
        logger.exception("Search API failed")
        return safe_api_response(False, None, "Search is temporarily unavailable. Please try again.")

@app.get("/search")
async def search_web(request: Request, q: str = "", skip: int = 0, limit: int = 24):
    limit = max(1, min(limit, 48))
    skip = max(0, skip)
    try:
        results = []
        if q:
            if await db.ping():
                intelligent_results = await db.search_anime_intelligent(q, limit=1000)
                results = intelligent_results[skip:skip + limit]
            else:
                results = []
        else:
            # Unlimited list logic
            results = await db.get_all_anime(limit=24, skip=0)

        categories = await db.get_all_categories()
        return templates.TemplateResponse(request=request, name="search.html", context={
            "results": results or [],
            "query": q,
            "categories": categories or [],
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })
    except Exception as e:
        logger.error(f"Search error: {e}")
        return templates.TemplateResponse(request=request, name="search.html", context={"results": [], "query": q, "categories": []})

@app.get("/categories")
async def categories_page(request: Request):
    """Browse all available categories."""
    try:
        categories = await db.get_all_categories()
        # Enrich with counts for a richer UI
        enriched = []
        for cat in categories or []:
            name = cat.get("name") if isinstance(cat, dict) else str(cat)
            if not name:
                continue
            count = await db.count_anime_by_category(name)
            enriched.append({"name": name, "count": count, **(cat if isinstance(cat, dict) else {})})
        return templates.TemplateResponse(request=request, name="categories.html", context={
            "categories": enriched,
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })
    except Exception as e:
        logger.error(f"Categories page error: {e}")
        return templates.TemplateResponse(request=request, name="categories.html", context={
            "categories": [],
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })

@app.get("/category/{category_name}")
async def category_detail(request: Request, category_name: str, skip: int = 0, limit: int = 36):
    """View all pages inside a single category."""
    limit = max(1, min(limit, 48))
    skip = max(0, skip)
    try:
        from urllib.parse import unquote
        name = unquote(category_name).strip()
        results = await db.get_anime_by_category(name, limit=limit, skip=skip)
        total = await db.count_anime_by_category(name)
        categories = await db.get_all_categories()
        return templates.TemplateResponse(request=request, name="category.html", context={
            "category_name": name,
            "results": results or [],
            "total": total,
            "skip": skip,
            "limit": limit,
            "categories": categories or [],
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })
    except Exception as e:
        logger.error(f"Category detail error ({category_name}): {e}")
        return templates.TemplateResponse(request=request, name="category.html", context={
            "category_name": category_name,
            "results": [],
            "total": 0,
            "skip": 0,
            "limit": limit,
            "categories": [],
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })

@app.get("/verify")
async def verify_page(request: Request):
    ip = request.client.host
    token = Protect.generate_verify_token(ip)
    # Using simple template replacement for now since Jinja2 is already configured for templates directory
    html = VERIFY_PAGE_HTML.replace("{{ logo_url }}", Config.LOGO_URL).replace("{{ token }}", token)
    return HTMLResponse(content=html)

@app.get("/verify/check")
async def verify_check(request: Request, token: str):
    ip = request.client.host
    if Protect.validate_verify_token(token, ip):
        response = RedirectResponse(url="/")
        cookie_val = Protect.create_verified_cookie()
        is_secure = "onrender.com" in str(request.base_url) or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie("verify_token", cookie_val, max_age=Config.VERIFY_EXPIRE, httponly=True, secure=is_secure, samesite="lax")
        return response
    return RedirectResponse(url="/verify")

@app.get("/dl")
async def download_redirect(request: Request, url: str):
    if not url: return RedirectResponse(url="/")

    import re
    # Automatically adjust details page links from old base domains to the current running BASE_URL
    if "/anime/" in url and not any(ext in url for ext in ["t.me", "telegram.me", "telegram.dog"]):
        slug_match = re.search(r'/anime/([a-zA-Z0-9-]+)', url)
        if slug_match:
            url = f"{Config.BASE_URL}/anime/{slug_match.group(1)}"

    # Automatically transform t.me to telegram.me in the target redirect URL
    url = re.sub(r'\bt\.me\b', 'telegram.me', url)

    # 1. Referer Check
    if not Protect.check_referer(request):
        return RedirectResponse(url="/")

    # 2. Verification Check
    if not Protect.is_verified(request):
        return RedirectResponse(url="/verify")

    # 3. Shortlink Generation
    final_url = await Protect.get_shortlink(url)
    # Ensure final URL also replaces t.me with telegram.me in case shortlink generators preserve or modify it
    final_url = re.sub(r'\bt\.me\b', 'telegram.me', final_url)
    return RedirectResponse(url=final_url)

@app.get("/az-index")
async def az_index(request: Request):
    try:
        if await db.ping():
            # Fetch all to build index (for production consider aggregation or separate collection if size is massive)
            all_anime = await db.anime.find({}, {"title": 1, "slug": 1}).sort("title", 1).to_list(length=5000)
        else:
            all_anime = []

        indexed_data = {}
        chars = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        for char in chars: indexed_data[char] = []
        indexed_data["#"] = []

        for item in all_anime:
            title = item.get("title", "").strip().upper()
            if not title: continue
            first_char = title[0]
            if first_char in chars:
                indexed_data[first_char].append(item)
            elif first_char.isdigit() or not first_char.isalpha():
                indexed_data["#"].append(item)

        categories = await db.get_all_categories()
        return templates.TemplateResponse(request=request, name="az_index.html", context={
            "indexed_data": indexed_data,
            "chars": chars + "#",
            "categories": categories or [],
            "logo_url": await get_site_logo_url(),
            "site_name": "ANIZONEFLIX"
        })
    except Exception as e:
        logger.error(f"AZ Index error: {e}")
        return templates.TemplateResponse(request=request, name="404.html", context={"error": "Index Generation Failed"}, status_code=500)

# --- ADMIN ROUTES ---

@app.get("/admin/login")
async def admin_login(request: Request, token: str = None):
    if not token: return RedirectResponse(url="/")
    try:
        payload = verify_token(token)
        if payload and payload.get("is_admin"):
            response = RedirectResponse(url="/admin/dashboard")
            is_secure = "onrender.com" in str(request.base_url) or request.headers.get("x-forwarded-proto") == "https"
            response.set_cookie("admin_token", token, httponly=True, secure=is_secure, samesite="lax", max_age=86400)
            return response
    except: pass
    return RedirectResponse(url="/")

@app.get("/admin/dashboard")
async def admin_dashboard(request: Request, admin=Depends(get_current_admin)):
    posts = await db.get_all_anime(limit=100)
    return templates.TemplateResponse(request=request, name="admin_dashboard.html", context={
        "posts": posts or [], "logo_url": await get_site_logo_url(), "site_name": "ANIZONEFLIX"
    })

@app.get("/admin/edit/{mal_id}")
async def edit_post_page(request: Request, mal_id: str, admin=Depends(get_current_admin)):
    try: query_id = int(mal_id)
    except: query_id = mal_id
    anime = await db.get_anime_by_mal_id(query_id)
    if not anime: return RedirectResponse(url="/admin/dashboard")
    return templates.TemplateResponse(request=request, name="edit.html", context={
        "anime": anime, "logo_url": await get_site_logo_url(), "site_name": "ANIZONEFLIX"
    })

@app.post("/api/admin/save/{mal_id}")
async def save_post(request: Request, mal_id: str, admin=Depends(get_current_admin)):
    try:
        data = await request.json()
        try: query_id = int(mal_id)
        except: query_id = mal_id
        if await db.ping():
            await db.anime.update_one({"mal_id": query_id}, {"$set": data})
            return safe_api_response(True)
        return safe_api_response(False, message="Database Offline")
    except Exception as e: return safe_api_response(False, message=str(e))

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run("app:app", host="0.0.0.0", port=port, log_level="info")
