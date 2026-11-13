import asyncio
import logging
import re
import uuid
from pyrogram import Client, filters
from pyrogram.enums import ButtonStyle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery
from config.config import Config
from database.db import db

logger = logging.getLogger("ANIZONEFLIX_BOT_MANAGER")

# Global caches with active size bounds to prevent memory leaks
search_cache = {}
MAX_CACHE_SIZE = 1000

async def report_addbot_issue(bot_identifier: str, reason: str):
    """Sends an automated report to the bot owner/admins whenever an added bot fails or encounters an error."""
    try:
        from config.config import Config
        if not Config.ADMIN_IDS:
            logger.warning(f"No ADMIN_IDS configured for reporting addbot error: {bot_identifier} -> {reason}")
            return

        text = (
            "🚨 **ADDBOT ALERT: CRITICAL FAILURE REPORT**\n\n"
            f"🤖 **Bot Target:** `{bot_identifier}`\n"
            f"⚠️ **Error Details:** `{reason}`\n\n"
            " Please inspect bot token validity and chat administrator permissions."
        )

        from bot import bot as main_bot
        for admin_id in Config.ADMIN_IDS:
            try:
                if main_bot and getattr(main_bot, "is_connected", False):
                    await main_bot.send_message(chat_id=admin_id, text=text)
            except Exception as e:
                logger.error(f"Failed to send addbot report to admin {admin_id}: {e}")
    except Exception as exc:
        logger.error(f"Failed to execute report_addbot_issue: {exc}")

def extract_candidate_queries(text: str) -> list:
    """
    Extracts high-probability anime title search candidates from a text.
    Handles lines, split segments, and a sliding window of phrases.
    """
    if not text:
        return []

    import re
    candidates = []
    seen = set()

    # Common stop words to ignore as standalone queries
    stop_words = {
        "the", "and", "but", "for", "with", "this", "that", "from", "your", "what",
        "some", "here", "there", "their", "them", "then", "also", "very", "good",
        "well", "will", "would", "could", "should", "want", "like", "love", "much",
        "many", "more", "most", "about", "above", "after", "again", "against", "all",
        "any", "are", "aren", "because", "been", "before", "being", "below", "between",
        "both", "cannot", "did", "didn", "does", "doesn", "doing", "don", "down", "during"
    }

    # 1. Parse line-by-line
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    for line in lines:
        if len(line) < 100:
            val = line.strip()
            if val and val.lower() not in stop_words and len(val) >= 3:
                if val.lower() not in seen:
                    candidates.append(val)
                    seen.add(val.lower())

        # 2. Split line by delimiters (comma, semicolon, colon)
        segments = re.split(r'[,;:]', line)
        for seg in segments:
            seg_val = seg.strip()
            if seg_val and len(seg_val) < 60 and seg_val.lower() not in stop_words and len(seg_val) >= 3:
                if seg_val.lower() not in seen:
                    candidates.append(seg_val)
                    seen.add(seg_val.lower())

    # 3. Sliding window over words (up to 4 words)
    # This is extremely effective for extracting names embedded inside paragraphs
    words = [w.strip() for w in re.split(r'\s+', text) if w.strip()]
    num_words = len(words)

    # We restrict sliding window on extremely large texts to capitalized phrases to avoid combinatorial explosion
    use_capitalized_only = num_words > 150

    for window_size in range(1, 5): # 1 to 4 words
        for i in range(num_words - window_size + 1):
            phrase_words = words[i:i + window_size]

            # Skip if we are focusing on capitalized phrases and none of the words start with a capital letter
            if use_capitalized_only:
                if not any(w[0].isupper() for w in phrase_words if w and w[0].isalpha()):
                    continue

            # Join and clean punctuation
            phrase = " ".join(phrase_words)
            phrase_clean = re.sub(r'[^\w\s]', '', phrase).strip()

            if len(phrase_clean) >= 3 and phrase_clean.lower() not in stop_words:
                # Do not allow purely numeric candidates
                if not phrase_clean.isdigit():
                    if phrase_clean.lower() not in seen:
                        candidates.append(phrase_clean)
                        seen.add(phrase_clean.lower())
                        # Hard cap candidates to prevent database overload
                        if len(candidates) >= 100:
                            return candidates

    return candidates

def clean_search_query(q: str) -> str:
    """Standardized clean search query preserving Unicode, lowercasing, stripping, removing punctuation."""
    q = q.lower().strip()
    q = re.sub(r'[^\w\s]', '', q, flags=re.UNICODE)
    q = re.sub(r'\s+', ' ', q).strip()
    return q

async def auto_delete_after_delay(message, delay: int = 20):
    """Background task to automatically delete a sent message after a delay."""
    await asyncio.sleep(delay)
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"Auto-delete failed (already deleted or permissions missing): {e}")

async def send_search_results_page(client: Client, message, session_id: str, page: int = 0):
    cache = search_cache.get(session_id)
    if not cache:
        return

    results = cache["results"]
    user_id = cache["user_id"]

    per_page = 8
    start_idx = page * per_page
    end_idx = start_idx + per_page
    page_items = results[start_idx:end_idx]

    if not page_items:
        return

    buttons = []
    row = []
    for anime in page_items:
        # Compact callback data: s:user_id:hex_id (guaranteed under 64 bytes)
        aid = str(anime["_id"])
        callback_data = f"s:{user_id}:{aid}"
        button = InlineKeyboardButton(anime["title"][:25], callback_data=callback_data, style=ButtonStyle.PRIMARY)
        row.append(button)
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)

    # Navigation buttons
    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"nav:{user_id}:{page-1}:{session_id}", style=ButtonStyle.PRIMARY))
    if end_idx < len(results):
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"nav:{user_id}:{page+1}:{session_id}", style=ButtonStyle.PRIMARY))

    if nav_row:
        buttons.append(nav_row)

    text = f"🎯 **Choose a matching title for '{cache['query']}'**:"

    try:
        if isinstance(message, CallbackQuery):
            res_msg = await message.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        else:
            res_msg = await message.reply(text, reply_markup=InlineKeyboardMarkup(buttons))
            # Auto delete list option after 20 seconds
            asyncio.create_task(auto_delete_after_delay(res_msg, 20))
    except Exception as e:
        logger.error(f"Error sending search results page: {e}")

class AddedBotManager:
    def __init__(self):
        self.clients = {}  # token -> Client

    async def start_all(self):
        logger.info("Initializing and starting all added bots...")
        if not await db.ping():
            logger.error("DB Offline, cannot load added bots.")
            return

        try:
            cursor = db.added_bots.find()
            bots = await cursor.to_list(length=1000)
            for bot_doc in bots:
                token = bot_doc.get("token")
                if not token:
                    continue
                group_id = bot_doc.get("group_id")
                bot_info = bot_doc.get("bot_info", {})
                await self.start_bot(token, group_id, bot_info)
        except Exception as e:
            logger.error(f"Error starting added bots: {e}")

    async def start_bot(self, token: str, group_id: int, bot_info: dict) -> bool:
        if token in self.clients:
            logger.info(f"Bot {bot_info.get('username')} already running.")
            return True

        logger.info(f"Starting added bot: @{bot_info.get('username') or 'Unknown'}")
        try:
            client = Client(
                name=f"added_bot_{bot_info.get('id', uuid.uuid4().hex[:8])}",
                api_id=Config.API_ID,
                api_hash=Config.API_HASH,
                bot_token=token,
                in_memory=True
            )

            # Register handlers
            self._register_handlers(client, group_id)

            await client.start()
            self.clients[token] = client
            logger.info(f"Added bot @{bot_info.get('username')} is now running.")
            return True
        except Exception as e:
            logger.error(f"Failed to start bot {bot_info.get('username')}: {e}")
            await report_addbot_issue(bot_info.get("username") or token[:10], f"Failed to start bot client: {str(e)}")
            return False

    async def stop_bot(self, token: str):
        client = self.clients.pop(token, None)
        if client:
            try:
                await client.stop()
                logger.info("Bot client stopped.")
            except Exception as e:
                logger.error(f"Error stopping bot client: {e}")

    async def stop_all(self):
        logger.info("Stopping all added bots...")
        for token in list(self.clients.keys()):
            await self.stop_bot(token)

    def _register_handlers(self, client: Client, configured_group_id):

        # Support filtering on both integer chat ID and string username/representation
        chat_filter = filters.chat(configured_group_id)
        try:
            if isinstance(configured_group_id, str) and (configured_group_id.startswith("-") or configured_group_id.isdigit()):
                chat_filter = filters.chat([configured_group_id, int(configured_group_id)])
            elif isinstance(configured_group_id, int):
                chat_filter = filters.chat([configured_group_id, str(configured_group_id)])
        except Exception:
            pass

        @client.on_message(filters.text & chat_filter)
        async def handle_group_text(c: Client, message: Message):
            # Ignore messages from bots or empty sender to prevent self-reply spam
            if not message.from_user or message.from_user.is_bot:
                return

            if message.text.startswith("/"):
                return

            query = message.text.strip()
            if not query:
                return

            try:
                is_long_text = len(query.split()) > 4
                results = []
                seen_results = set()

                if is_long_text:
                    # Multi-way candidate matching for extremely large texts / paragraphs
                    candidates = extract_candidate_queries(query)
                    for candidate in candidates:
                        matches = await db.search_anime_intelligent(candidate, limit=2)
                        for m in matches:
                            mid = str(m["_id"])
                            if mid not in seen_results:
                                results.append(m)
                                seen_results.add(mid)
                                if len(results) >= 15:
                                    break
                        if len(results) >= 15:
                            break
                else:
                    results = await db.search_anime_intelligent(query, limit=50)

                if not results:
                    return

                if len(results) == 1:
                    anime = results[0]
                    sent_msg = await message.reply(f"{Config.BASE_URL}/anime/{anime['slug']}")
                    # Setup background task to delete sent link message after 20 seconds
                    asyncio.create_task(auto_delete_after_delay(sent_msg, 20))
                    return

                # Multiple matches
                session_id = uuid.uuid4().hex[:8]

                # Active Cache Pruning to keep memory usage strictly bounded
                if len(search_cache) >= MAX_CACHE_SIZE:
                    # Remove first key (FIFO)
                    old_key = next(iter(search_cache))
                    search_cache.pop(old_key, None)

                search_cache[session_id] = {
                    "results": results[:15],
                    "query": query[:40] + "..." if len(query) > 40 else query,
                    "user_id": message.from_user.id if message.from_user else 0
                }

                await send_search_results_page(c, message, session_id, page=0)
            except Exception as e:
                logger.error(f"Error handling group text message: {e}")

        @client.on_callback_query()
        async def handle_bot_callback(c: Client, callback_query: CallbackQuery):
            data = callback_query.data
            if not (data.startswith("s:") or data.startswith("nav:")):
                return

            parts = data.split(":")
            if len(parts) < 3:
                await callback_query.answer("⚠️ Invalid callback data.", show_alert=True)
                return

            action = parts[0]
            expected_user_id = int(parts[1])
            clicker_id = callback_query.from_user.id if callback_query.from_user else 0

            # Button security
            if expected_user_id != clicker_id:
                await callback_query.answer("This menu belongs to another user.", show_alert=True)
                return

            if action == "s":
                aid = parts[2]
                await callback_query.answer()

                try:
                    # Direct lookup using _id / ID
                    anime = await db.get_anime(aid)
                    if anime:
                        res_msg = await callback_query.message.edit_text(
                            f"{Config.BASE_URL}/anime/{anime['slug']}",
                            reply_markup=None
                        )
                        # Auto delete selected link message after 20 seconds
                        asyncio.create_task(auto_delete_after_delay(res_msg, 20))
                    else:
                        await callback_query.answer("❌ Title not found in database.", show_alert=True)
                except Exception as e:
                    logger.error(f"Error serving selection callback: {e}")
                    await callback_query.answer("❌ Database Error.")

            elif action == "nav":
                if len(parts) < 4:
                    return
                page = int(parts[2])
                session_id = parts[3]
                await callback_query.answer()
                await send_search_results_page(c, callback_query, session_id, page=page)

added_bot_manager = AddedBotManager()
