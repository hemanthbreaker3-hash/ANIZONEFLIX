import asyncio
import time
import logging
import aiohttp
from database.db import db

logger = logging.getLogger("ANIZONEFLIX_UPTIME")

_uptime_task = None

async def ping_bot(bot, session):
    bot_id = bot.get("bot_id")
    url = bot.get("url")
    if not url or not bot_id:
        return

    start_time = time.time()
    try:
        # Use short timeout (5 seconds max per check)
        timeout = aiohttp.ClientTimeout(total=5)
        async with session.get(url, timeout=timeout, headers={"User-Agent": "AniZoneFlix-Uptime-Monitor/1.0"}, allow_redirects=True) as resp:
            latency = int((time.time() - start_time) * 1000)
            status_code = resp.status
            status = "online" if resp.status < 500 else "degraded"
            await db.update_monitored_bot_status(bot_id, status=status, status_code=status_code, response_time_ms=latency)
    except Exception as e:
        latency = int((time.time() - start_time) * 1000)
        await db.update_monitored_bot_status(bot_id, status="offline", status_code=None, response_time_ms=latency)

async def uptime_monitor_loop():
    logger.info("Starting 24/7 continuous 1-second Uptime Monitoring Worker...")
    headers = {"User-Agent": "AniZoneFlix-Uptime-Monitor/1.0"}

    # Reuse ClientSession for efficiency across loops
    async with aiohttp.ClientSession(headers=headers) as session:
        while True:
            loop_start = time.time()
            try:
                bots = await db.get_all_monitored_bots()
                if bots:
                    tasks = [ping_bot(bot, session) for bot in bots if bot.get("url")]
                    if tasks:
                        await asyncio.gather(*tasks, return_exceptions=True)
            except Exception as e:
                logger.error(f"Error in Uptime Monitor Loop: {e}")

            # Calculate sleep duration to maintain 1-second check intervals
            elapsed = time.time() - loop_start
            sleep_time = max(0.1, 1.0 - elapsed)
            await asyncio.sleep(sleep_time)

def start_uptime_monitor():
    global _uptime_task
    if _uptime_task is None or _uptime_task.done():
        try:
            loop = asyncio.get_running_loop()
            _uptime_task = loop.create_task(uptime_monitor_loop())
            logger.info("Uptime Monitoring Task successfully scheduled.")
        except RuntimeError:
            logger.warning("No running event loop to attach Uptime Monitor task.")
    return _uptime_task
