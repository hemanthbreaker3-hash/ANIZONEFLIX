import asyncio
import logging
import traceback
import os
import json
import copy
import zipfile
import tempfile
import re
import uuid
import time
from urllib.parse import unquote
from io import BytesIO
from bson import ObjectId
from pyrogram import Client, filters, enums, ContinuePropagation, StopPropagation
from pyrogram.enums import ButtonStyle
from pyrogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, CallbackQuery, BotCommand
from pyrogram.handlers import MessageHandler, CallbackQueryHandler
from pyrogram.errors import MessageNotModified, FloodWait
from config.config import Config
from api.anime_api import anime_api
from database.db import db
from utils.utils import slugify
import unicodedata

# Setup Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ANIZONEFLIX_BOT")

bot = Client(
    "anizoneflix_bot",
    api_id=Config.API_ID,
    api_hash=Config.API_HASH,
    bot_token=Config.BOT_TOKEN,
    in_memory=True
)

# Temporary storage
search_results = {}
user_state = {}

async def is_authorized(user_id):
    """Canonical admin check shared by every command and inline callback."""
    if user_id is None:
        return False
    try:
        normalized_id = int(str(user_id).strip())
        if normalized_id <= 0:
            return False

        # Config normally exposes a list, but tolerate string/list values from
        # older deployments and runtime overrides as well.
        raw_ids = getattr(Config, "ADMIN_IDS", []) or []
        if isinstance(raw_ids, (str, int)):
            raw_ids = str(raw_ids).split(",")
        configured_ids = set()
        for value in raw_ids:
            try:
                candidate = int(str(value).strip())
                if candidate > 0:
                    configured_ids.add(candidate)
            except (TypeError, ValueError):
                continue
        if normalized_id in configured_ids:
            return True

        # Check legacy aliases too, in case Config was loaded before env updates.
        for key in ("ADMIN_ID", "OWNER_ID", "OWNER_IDS"):
            for value in str(os.getenv(key, "")).split(","):
                try:
                    if int(value.strip()) == normalized_id:
                        return True
                except (TypeError, ValueError):
                    pass

        return bool(await db.is_admin(normalized_id))
    except Exception:
        logger.exception("Authorization check failed for Telegram user ID %s", user_id)
        return False

async def set_commands(client):
    commands = [
        BotCommand("start", "Start the bot"),
        BotCommand("post", "Publish Anime Post to Channel"),
        BotCommand("search", "Search anime (including native titles)"),
        BotCommand("logo", "Set website homepage logo"),
        BotCommand("add_post", "Rapid One-Shot Post"),
        BotCommand("add_page", "Manual Content Creation"),
        BotCommand("manual", "Custom Detailed Creation"),
        BotCommand("edit_m", "Manage Custom Buttons"),
        BotCommand("edit", "Manage Content Groups"),
        BotCommand("change_poster", "Update Series Artwork"),
        BotCommand("categories", "Manage Genres/Tags"),
        BotCommand("create", "Create and sync a category from selected categories"),
        BotCommand("schedule", "Manage Airing Schedule"),
        BotCommand("del", "Permanent Archive Erasure"),
        BotCommand("category_page", "Migrate Page Category"),
        BotCommand("export_boxes", "Move matching boxes to a category"),
        BotCommand("delete_boxes", "Delete matching boxes (or all)"),
        BotCommand("duplicate", "Remove duplicate boxes and buttons"),
        BotCommand("songs", "Manage Background Songs"),
        BotCommand("uptime", "24/7 Bot Uptime Monitor"),
        BotCommand("setbot", "Configure Link Generator Bot"),
        BotCommand("ss", "Configure Pyrogram Session String"),
        BotCommand("save", "Backup & Restore Data"),
        BotCommand("cancel", "Abort Active Process"),
        BotCommand("ping", "System Latency Check")
    ]
    await client.set_bot_commands(commands)
    logger.info("Bot commands synchronized.")

    # Startup verification, empty-page cleanup, and persistent category mirrors.
    asyncio.create_task(verify_post_channel(client))
    asyncio.create_task(cleanup_stale_empty_pages())
    asyncio.create_task(sync_created_categories())

def title_with_category(title, category):
    """Append the selected category to the visible title exactly once."""
    base = " ".join(str(title or "Untitled").split()).strip()
    category_name = " ".join(str(category or "").split()).strip()
    if not category_name:
        return base
    suffix = f" - {category_name}"
    if base.casefold().endswith(suffix.casefold()):
        return base
    return f"{base}{suffix}"

def _contains_usable_url(value):
    """Return True when a nested page content value contains a usable link."""
    if isinstance(value, str):
        candidate = value.strip()
        return bool(re.match(r"^(?:(?:https?|tg)://|t\.me/|www\.)", candidate, re.IGNORECASE))
    if isinstance(value, dict):
        for key, item in value.items():
            key_name = str(key).casefold()
            if key_name in {"url", "link", "href", "download", "stream", "480p", "720p", "1080p", "4k"}:
                if isinstance(item, str) and item.strip() and item.strip().casefold() not in {"none", "null", "n/a", "-"}:
                    return True
            if _contains_usable_url(item):
                return True
    if isinstance(value, (list, tuple)):
        return any(_contains_usable_url(item) for item in value)
    return False

def _page_has_publishable_content(doc):
    """A page is non-empty when it has any usable button, stream, or download link."""
    content_fields = (
        "seasons_links", "custom_buttons", "custom_boxes", "buttons",
        "groups", "links", "download_links", "stream_links", "episodes",
    )
    return any(_contains_usable_url(doc.get(field)) for field in content_fields)

async def cleanup_stale_empty_pages():
    """Delete the entire page (and its episode rows) after 30 minutes empty.

    Empty means no usable URL/button in any supported page-content field and no
    separately stored episode rows. The timer is persisted on the page document,
    so restarts do not reset it. A fresh read and conditional delete protect pages
    that receive content while the cleanup worker is running.
    """
    while True:
        try:
            now = time.time()
            cursor = db.anime.find({})
            docs = await cursor.to_list(length=20000)
            for doc in docs:
                page_id = doc.get("_id")
                mal_id = doc.get("mal_id")
                # /create mirrors are intentionally preserved even when their source
                # currently has no links; they must never be removed by stale cleanup.
                if doc.get("_preserve_empty_page") or doc.get("_created_by_create"):
                    continue
                has_content = _page_has_publishable_content(doc)
                if not has_content and mal_id and db.episodes is not None:
                    try:
                        has_content = await db.episodes.find_one({"mal_id": mal_id}, {"_id": 1}) is not None
                    except Exception:
                        # Fail closed: do not delete a page if episode status cannot be checked.
                        logger.exception("Could not verify episode rows for page %s", page_id)
                        continue

                if has_content:
                    if doc.get("_empty_page_since") is not None:
                        await db.anime.update_one(
                            {"_id": page_id, "_empty_page_since": doc.get("_empty_page_since")},
                            {"$unset": {"_empty_page_since": ""}},
                        )
                    continue

                empty_since = doc.get("_empty_page_since")
                try:
                    empty_since = float(empty_since) if empty_since is not None else None
                except (TypeError, ValueError):
                    empty_since = None
                if empty_since is None:
                    await db.anime.update_one(
                        {"_id": page_id, "_empty_page_since": {"$exists": False}},
                        {"$set": {"_empty_page_since": now}},
                    )
                    continue
                if now - empty_since < 1800:
                    continue

                # Re-read immediately before deletion. If a button/link was added,
                # clear the timer instead of deleting the page.
                fresh = await db.anime.find_one({"_id": page_id})
                if not fresh or fresh.get("_empty_page_since") != doc.get("_empty_page_since"):
                    continue
                if _page_has_publishable_content(fresh):
                    await db.anime.update_one({"_id": page_id}, {"$unset": {"_empty_page_since": ""}})
                    continue
                if mal_id:
                    try:
                        if await db.episodes.find_one({"mal_id": mal_id}, {"_id": 1}):
                            await db.anime.update_one({"_id": page_id}, {"$unset": {"_empty_page_since": ""}})
                            continue
                    except Exception:
                        logger.exception("Episode cleanup check failed; preserving page %s", page_id)
                        continue
                result = await db.anime.delete_one({"_id": page_id, "_empty_page_since": doc.get("_empty_page_since")})
                if getattr(result, "deleted_count", 0):
                    if mal_id:
                        try:
                            await db.episodes.delete_many({"mal_id": mal_id})
                        except Exception:
                            logger.exception("Could not remove orphan episode rows for deleted page %s", page_id)
                    logger.info("Deleted entire empty page %s after 30 minutes", page_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale empty-page cleanup cycle failed")
        await asyncio.sleep(60)


async def sync_created_categories():
    """Keep /create destination categories mirrored from their selected sources."""
    while True:
        try:
            configs = await db.get_setting("created_category_sync", []) or []
            for config in configs:
                destination = str(config.get("destination") or "").strip()
                sources = [str(x).strip() for x in config.get("sources", []) if str(x).strip()]
                config_id = str(config.get("id") or "")
                if not destination or not sources or not config_id:
                    continue
                for source_category in sources:
                    cursor = db.anime.find({"category": source_category}).sort("_id", 1)
                    source_docs = await cursor.to_list(length=20000)
                    for source in source_docs:
                        # Empty placeholders and records with no actual downloadable
                        # content are not exported. Episode-backed pages count as content.
                        has_content = _page_has_publishable_content(source)
                        mal_id = source.get("mal_id")
                        if not has_content and mal_id:
                            try:
                                has_content = await db.episodes.find_one({"mal_id": mal_id}, {"_id": 1}) is not None
                            except Exception:
                                logger.exception("/create sync could not verify episodes for %s", source.get("_id"))
                                continue
                        if not has_content:
                            continue
                        source_id = str(source.get("_id") or source.get("slug") or "")
                        if not source_id:
                            continue
                        base_slug = str(source.get("slug") or slugify(source.get("title") or "page"))
                        suffix = slugify(destination)
                        mirror_slug = f"{base_slug}-{suffix}" if suffix else base_slug
                        # /create exports the page record only: keep title, artwork,
                        # metadata and descriptive fields, but deliberately omit every
                        # group/box/button/link collection. These structures are not
                        # copied into the destination category.
                        mirror = copy.deepcopy(source)
                        mirror.pop("_id", None)
                        excluded_export_fields = (
                            "custom_boxes", "groups", "boxes",
                            "custom_buttons", "buttons", "seasons_links",
                            "links", "download_links", "stream_links",
                            "_empty_page_since",
                        )
                        for field in excluded_export_fields:
                            mirror.pop(field, None)
                        mirror.update({
                            "category": destination,
                            "slug": mirror_slug,
                            "sync_source_id": source_id,
                            "sync_config_id": config_id,
                            "_created_by_create": True,
                            "_preserve_empty_page": True,
                        })
                        # Unset excluded fields on existing mirrors too; $set alone
                        # would leave previously copied buttons/boxes behind.
                        unset_fields = {field: "" for field in excluded_export_fields}
                        await db.anime.update_one(
                            {"sync_config_id": config_id, "sync_source_id": source_id},
                            {"$set": mirror, "$unset": unset_fields}, upsert=True,
                        )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("/create category synchronization cycle failed")
        await asyncio.sleep(60)


async def safe_send_channel_message(client, channel_id, text, disable_web_page_preview=False, reply_markup=None):
    target_chat = channel_id
    if isinstance(channel_id, str):
        channel_id_clean = channel_id.strip()
        if channel_id_clean.startswith("-100") or channel_id_clean.replace("-", "").isdigit():
            try:
                target_chat = int(channel_id_clean)
            except ValueError:
                target_chat = channel_id_clean
        else:
            target_chat = channel_id_clean

    try:
        chat_obj = await client.get_chat(target_chat)
        target_chat = chat_obj.id
    except Exception as resolve_err:
        logger.warning(f"get_chat peer resolve warning for {target_chat}: {resolve_err}")

    try:
        return await client.send_message(
            chat_id=target_chat,
            text=text,
            disable_web_page_preview=disable_web_page_preview,
            reply_markup=reply_markup
        )
    except Exception as err:
        if isinstance(target_chat, int):
            try:
                return await client.send_message(
                    chat_id=str(target_chat),
                    text=text,
                    disable_web_page_preview=disable_web_page_preview,
                    reply_markup=reply_markup
                )
            except Exception:
                pass
        raise err

async def verify_post_channel(client):
    try:
        channel_id = await db.get_post_channel()
        if channel_id:
            logger.info(f"Verifying post channel permissions for {channel_id}...")
            test_msg = await safe_send_channel_message(client, channel_id, "⚡ **AniZoneFlix Channel Verification Ping...**")
            await client.delete_messages(channel_id, test_msg.id)
            logger.info(f"Post channel {channel_id} verified successfully.")
    except Exception as e:
        logger.warning(f"Post channel verification warning for {channel_id}: {e}")

async def publish_post_to_channel(client, post_channel, heading, anime, page_link):
    title = anime.get("title", "Untitled")

    # Genres
    genres_raw = anime.get("genres", [])
    if isinstance(genres_raw, list):
        genres_str = " • ".join(genres_raw) if genres_raw else "Anime"
    else:
        genres_str = str(genres_raw)

    # Audio
    audio_str = anime.get("languages") or "Telugu • Tamil • Hindi • English • Japanese"

    # Quality
    quality_labels = []
    seen_q = set()

    seasons_links = anime.get("seasons_links", {})
    if isinstance(seasons_links, dict):
        for gname, gdata in seasons_links.items():
            if isinstance(gdata, dict):
                for label in gdata.keys():
                    lbl_clean = str(label).strip()
                    if lbl_clean and lbl_clean.lower() not in seen_q:
                        seen_q.add(lbl_clean.lower())
                        quality_labels.append(lbl_clean)

    custom_boxes = anime.get("custom_boxes", [])
    if isinstance(custom_boxes, list):
        for box in custom_boxes:
            groups = box.get("groups", {})
            if isinstance(groups, dict):
                for gname, gdata in groups.items():
                    if isinstance(gdata, dict):
                        for label in gdata.keys():
                            lbl_clean = str(label).strip()
                            if lbl_clean and lbl_clean.lower() not in seen_q:
                                seen_q.add(lbl_clean.lower())
                                quality_labels.append(lbl_clean)

    if quality_labels:
        quality_str = " | ".join(quality_labels)
    else:
        quality_str = "480p | 720p | 1080p"

    post_text = (
        f"{heading}\n\n"
        f"🎬 Title: {title}\n"
        f"🗣 Genres: {genres_str}\n"
        f"🔊 Audio: {audio_str}\n"
        f"📺 Quality: {quality_str}\n\n"
        f"📥 Watch / Download:\n"
        f"{page_link}"
    )

    post_keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 {title}", url=page_link, style=ButtonStyle.PRIMARY)]
    ])

    sent_msg = await safe_send_channel_message(
        client,
        post_channel,
        post_text,
        disable_web_page_preview=False,
        reply_markup=post_keyboard
    )
    return sent_msg

def extract_slug(text):
    """Bulletproof slug extraction from any URL or raw text"""
    if not text: return None
    text = unquote(text.strip())
    if "/anime/" in text:
        try:
            parts = text.split("/anime/")[-1].split("/")
            slug_part = parts[0] if parts[0] else parts[1]
            return slug_part.split("?")[0].split("\n")[0].split(" ")[0].rstrip("/").strip()
        except:
            return None
    return text

def parse_buttons_string(text, expected_count):
    if not text:
        return None
    text = text.strip()
    # Split only on separator colons (not protocol colons)
    pattern = r'(?<!\bhttps)(?<!\bhttp)(?<!\btg):'
    parts = re.split(pattern, text)
    if len(parts) != expected_count + 1:
        return None  # Invalid colon count

    buttons = {}
    curr_name = parts[0].strip()
    if not curr_name:
        return None

    for i in range(1, len(parts)):
        part = parts[i].strip()
        if i < len(parts) - 1:
            subparts = part.rsplit(None, 1)
            if len(subparts) < 2:
                return None
            link, next_name = subparts
            link = link.strip()
            next_name = next_name.strip()

            if not link.startswith("http") and not link.startswith("tg"):
                return None

            buttons[curr_name] = link
            curr_name = next_name
        else:
            link = part
            if not link.startswith("http") and not link.startswith("tg"):
                return None
            buttons[curr_name] = link

    return buttons


def parse_range_link(text):
    if not text:
        return None
    text = text.strip()

    # Match private channel links e.g. https://t.me/c/1234567890/100
    private_matches = re.findall(r'https?://(?:t\.me|telegram\.me|telegram\.dog)/c/(\d+)/(\d+)', text)
    if len(private_matches) >= 2:
        c1, id1 = private_matches[0]
        c2, id2 = private_matches[1]
        if c1 == c2:
            return f"c/{c1}", int(id1), int(id2)
    elif len(private_matches) == 1:
        c1, id1 = private_matches[0]
        short_m = re.search(r'https?://(?:t\.me|telegram\.me|telegram\.dog)/c/' + re.escape(c1) + r'/' + re.escape(id1) + r'(?:[^\d]*?)-[^\d]*?(\d+)', text)
        if short_m:
            return f"c/{c1}", int(id1), int(short_m.group(1))

    # Match public channel links e.g. https://t.me/channel_name/100
    public_matches = re.findall(r'https?://(?:t\.me|telegram\.me|telegram\.dog)/([a-zA-Z0-9_]+)/(\d+)', text)
    public_matches = [(chan, mid) for chan, mid in public_matches if chan.lower() != 'c']

    if len(public_matches) >= 2:
        chan1, id1 = public_matches[0]
        chan2, id2 = public_matches[1]
        if chan1.lower() == chan2.lower():
            return chan1, int(id1), int(id2)
    elif len(public_matches) == 1:
        chan1, id1 = public_matches[0]
        short_m = re.search(r'https?://(?:t\.me|telegram\.me|telegram\.dog)/' + re.escape(chan1) + r'/' + re.escape(id1) + r'(?:[^\d]*?)-[^\d]*?(\d+)', text)
        if short_m:
            return chan1, int(id1), int(short_m.group(1))

    return None

def parse_genlink_bot_response(text, filter_name=None):
    """
    Parses output returned by genlink bot for a single block.
    Extracts video quality (e.g. 480p, 720p, 1080p, 4k), episode number if present,
    and generated link (https://telegram.me/... or https://t.me/...).
    Returns dict or None.
    """
    if not text:
        return None

    if filter_name and filter_name.strip() != "." and filter_name.strip().lower() not in text.lower():
        return None

    # Extract link
    link_match = re.search(r'https?://(?:telegram\.me|t\.me|telegram\.dog)/[^\s"<>]+', text)
    if not link_match:
        return None
    link = link_match.group(0).strip('"\').,()<>')

    # Extract quality
    quality = "720P" # default quality if unspecified
    q_match = re.search(r'(480p|720p|1080p|2160p|4k)', text, re.IGNORECASE)
    if q_match:
        quality = q_match.group(1).upper()
        if quality == "4K":
            quality = "2160P"

    # Extract episode number if present
    episode = None
    ep_match = re.search(r'(?:S\d+)?E(\d+)|Episode\s*(\d+)|EP\s*(\d+)', text, re.IGNORECASE)
    if ep_match:
        ep_str = ep_match.group(1) or ep_match.group(2) or ep_match.group(3)
        if ep_str:
            episode = int(ep_str)

    return {
        "link": link,
        "quality": quality,
        "episode": episode
    }

def parse_all_genlink_blocks(text, filter_name=None):
    """
    Parses output returned by genlink / serielbatch bot, supporting bulk messages
    that contain multiple generated link blocks.
    Returns list of dicts: [{"link": ..., "quality": ..., "episode": ...}, ...]
    """
    if not text:
        return []

    # Split by block header pattern if multiple blocks exist in a single message
    chunks = re.split(r'(?=(?:<b>)?First (?:Filename|Caption):)', text, flags=re.IGNORECASE)
    results = []
    for chunk in chunks:
        if not chunk.strip():
            continue
        parsed = parse_genlink_bot_response(chunk, filter_name=filter_name)
        if parsed:
            results.append(parsed)

    # Fallback if split didn't yield blocks but text contains a valid link
    if not results:
        parsed = parse_genlink_bot_response(text, filter_name=filter_name)
        if parsed:
            results.append(parsed)

    return results

async def process_range_link_task(bot_client, status_msg, aid, chat_slug, start_id, end_id, group_names, target_bot=None, target_box_idx=None, filter_name=None, mode="genlink"):
    """
    Background processing task for range link processing.
    Supports two modes:
    - mode="genlink": Sends /genlink https://t.me/<chat_slug>/<msg_id> sequentially for each message ID in range.
    - mode="serial": Triggers /serielbatch in configured bot, responds to prompts for Range Link and Filter Name, and monitors output stream.
    Collects output link & quality, organizes by serial-numbered groups, and updates database.
    """
    try:
        configured_bot = target_bot or (await db.get_configured_bot())
        configured_ss = await db.get_configured_session()

        if not configured_bot:
            return await status_msg.edit_text("❌ **Automation Aborted:** Bot username not set. Please set it using `/setbot`.")
        if not configured_ss:
            return await status_msg.edit_text("❌ **Automation Aborted:** Pyrogram session string not set. Please set it using `/ss`.")

        await status_msg.edit_text(
            f"🔄 **Starting Link Generation Process ({mode.upper()})...**\n\n"
            f"🤖 **Bot:** `@{configured_bot}`\n"
            f"🔢 **IDs:** `{start_id}` to `{end_id}`\n\n"
            "Connecting user session client..."
        )

        user_client = Client(
            "genlink_user_session",
            api_id=Config.API_ID,
            api_hash=Config.API_HASH,
            session_string=configured_ss,
            in_memory=True
        )

        await user_client.start()

        collected_outputs = []
        failed_ids = []
        total_msgs = end_id - start_id + 1

        if mode == "serial":
            # --- SERIAL BATCH FLOW ---
            await status_msg.edit_text(
                f"🔄 **Sending `/serielbatch` to @{configured_bot}...**"
            )

            # Send /serielbatch
            batch_cmd = await user_client.send_message(configured_bot, "/serielbatch")

            # Wait for bot prompt: "📌 Send the Range Link:"
            prompt_range_msg = None
            for _ in range(15):
                await asyncio.sleep(1)
                async for history_msg in user_client.get_chat_history(configured_bot, limit=5):
                    if history_msg.id > batch_cmd.id and not history_msg.outgoing:
                        prompt_range_msg = history_msg
                        break
                if prompt_range_msg:
                    break

            # Send Range Link
            range_link_str = f"https://t.me/{chat_slug}/{start_id}-https://t.me/{chat_slug}/{end_id}"
            await status_msg.edit_text(
                f"📌 **Sending Range Link to @{configured_bot}...**\n`{range_link_str}`"
            )
            sent_range_msg = await user_client.send_message(configured_bot, range_link_str)

            # Wait for bot prompt: "🔎 Send the Filter Name:"
            prompt_filter_msg = None
            for _ in range(15):
                await asyncio.sleep(1)
                async for history_msg in user_client.get_chat_history(configured_bot, limit=5):
                    if history_msg.id > sent_range_msg.id and not history_msg.outgoing:
                        prompt_filter_msg = history_msg
                        break
                if prompt_filter_msg:
                    break

            # Send Filter Name
            filter_str = filter_name.strip() if (filter_name and filter_name.strip()) else "."
            await status_msg.edit_text(
                f"🔎 **Sending Filter Name to @{configured_bot}...**\n`{filter_str}`"
            )
            sent_filter_msg = await user_client.send_message(configured_bot, filter_str)

            # Monitor incoming output stream from configured_bot
            await status_msg.edit_text(
                f"⏳ **Monitoring Output Stream from @{configured_bot}...**\n\n"
                f"Filter: `{filter_str}`\n"
                f"Collected: `0`"
            )

            last_msg_id = sent_filter_msg.id
            processed_msg_ids = set()
            start_time = asyncio.get_event_loop().time()
            last_activity_time = start_time
            is_completed = False

            while True:
                await asyncio.sleep(1)
                now = asyncio.get_event_loop().time()
                new_found = False

                async for history_msg in user_client.get_chat_history(configured_bot, limit=20):
                    if history_msg.id > last_msg_id and not history_msg.outgoing and history_msg.id not in processed_msg_ids:
                        processed_msg_ids.add(history_msg.id)
                        reply_text = history_msg.text or history_msg.caption or ""

                        if "Serial Batch Link Generation Complete!" in reply_text:
                            is_completed = True
                            break

                        parsed_items = parse_all_genlink_blocks(reply_text, filter_name=filter_name)
                        if parsed_items:
                            for p_item in parsed_items:
                                collected_outputs.append({
                                    "msg_id": len(collected_outputs) + start_id,
                                    "link": p_item["link"],
                                    "quality": p_item["quality"],
                                    "episode": p_item["episode"]
                                })
                            new_found = True
                            last_activity_time = now
                            try:
                                await status_msg.edit_text(
                                    f"⏳ **Serial Batch Monitoring ({len(collected_outputs)} links collected)...**\n\n"
                                    f"Latest: `{parsed_items[-1]['quality']}` - `{parsed_items[-1]['link']}`"
                                )
                            except Exception:
                                pass

                if is_completed:
                    break

                if new_found and len(collected_outputs) >= total_msgs:
                    break

                idle_time = now - last_activity_time
                if collected_outputs and idle_time > 15:
                    break
                if not collected_outputs and idle_time > 30:
                    break

        else:
            # --- GENLINK SEQUENTIAL FLOW ---
            for current_idx, msg_id in enumerate(range(start_id, end_id + 1), 1):
                if current_idx % 5 == 0 or current_idx == total_msgs:
                    try:
                        await status_msg.edit_text(
                            f"⏳ **Processing Messages ({current_idx}/{total_msgs})...**\n\n"
                            f"Current Message ID: `{msg_id}`\n"
                            f"Collected: `{len(collected_outputs)}`"
                        )
                    except Exception:
                        pass

                target_link = f"https://t.me/{chat_slug}/{msg_id}"
                cmd_text = f"/genlink {target_link}"

                try:
                    sent_req = await user_client.send_message(configured_bot, cmd_text)

                    bot_reply = None
                    for _ in range(15):
                        await asyncio.sleep(1)
                        async for history_msg in user_client.get_chat_history(configured_bot, limit=5):
                            if history_msg.id > sent_req.id and not history_msg.outgoing:
                                bot_reply = history_msg
                                break
                        if bot_reply:
                            break

                    if bot_reply and (bot_reply.text or bot_reply.caption):
                        reply_text = bot_reply.text or bot_reply.caption
                        parsed = parse_genlink_bot_response(reply_text, filter_name=filter_name)
                        if parsed:
                            collected_outputs.append({
                                "msg_id": msg_id,
                                "link": parsed["link"],
                                "quality": parsed["quality"],
                                "episode": parsed["episode"]
                            })
                        else:
                            failed_ids.append(msg_id)
                    else:
                        failed_ids.append(msg_id)

                except Exception as msg_err:
                    logger.error(f"Error processing message {msg_id}: {msg_err}")
                    failed_ids.append(msg_id)

                await asyncio.sleep(1)

        try:
            await user_client.stop()
        except Exception:
            pass

        if not collected_outputs:
            return await status_msg.edit_text(
                "❌ **Process Failed:** No links could be generated from the target bot.\n"
                "Please check if the session string and bot username are correct."
            )

        # Organize outputs into groups based on serial numbers provided by admin
        # Serial 1 -> Group 1, Serial 2 -> Group 2 ...
        # If there are outputs for episodes/qualities, group them logically
        # Group format: Serial 1 = group_names[0], Serial 2 = group_names[1], ...
        # Group contents:
        # If outputs have distinct qualities (e.g. 480P, 720P, 1080P), button name = quality.
        # If multiple episodes/qualities, map sequentially or by episode to serial groups.

        anime = await db.get_anime(aid)
        if not anime:
            return await status_msg.edit_text("❌ **Error:** Target anime page no longer exists.")

        # Structure calculated groups
        # Every serial group (1. Group Name) gets its respective outputs
        # Group outputs count / distribution:
        num_groups = len(group_names)

        # If num_groups == 1, all collected outputs belong to Group 1
        # If outputs can be grouped by episode or chunks across serial groups:
        # e.g., Group 1 for Serial 1, Group 2 for Serial 2...
        # Each serial group will contain buttons labeled by video quality ("480P", "720P", "1080P", etc.)

        # Build group_dict: { group_name: { "480P": link, "720P": link, ... } }
        outputs_by_ep = {}
        has_parsed_episodes = any(out.get("episode") is not None for out in collected_outputs)

        if has_parsed_episodes:
            for out in collected_outputs:
                ep_num = out.get("episode")
                if ep_num is not None:
                    if ep_num not in outputs_by_ep:
                        outputs_by_ep[ep_num] = []
                    outputs_by_ep[ep_num].append(out)
                else:
                    if 1 not in outputs_by_ep:
                        outputs_by_ep[1] = []
                    outputs_by_ep[1].append(out)
        else:
            for idx, out in enumerate(collected_outputs, 1):
                ep_num = idx if len(collected_outputs) == len(group_names) else ((idx - 1) // max(1, len(collected_outputs) // len(group_names)) + 1)
                if ep_num not in outputs_by_ep:
                    outputs_by_ep[ep_num] = []
                outputs_by_ep[ep_num].append(out)

        if target_box_idx is not None:
            boxes = anime.get("custom_boxes", [])
            if target_box_idx < len(boxes):
                target_box = boxes[target_box_idx]
                if "groups" not in target_box or not isinstance(target_box["groups"], dict):
                    target_box["groups"] = {}

                for g_idx, g_name in enumerate(group_names, 1):
                    g_outputs = outputs_by_ep.get(g_idx, [])
                    if g_name not in target_box["groups"] or not isinstance(target_box["groups"][g_name], dict):
                        target_box["groups"][g_name] = {}

                    for out in g_outputs:
                        q_label = out["quality"]
                        target_box["groups"][g_name][q_label] = out["link"]

                update_fields = {
                    "custom_boxes": boxes,
                    "newly_added_groups": group_names
                }
            else:
                target_box_idx = None

        if target_box_idx is None:
            structured_seasons_links = anime.get("seasons_links", {})
            if not isinstance(structured_seasons_links, dict):
                structured_seasons_links = {}

            for g_idx, g_name in enumerate(group_names, 1):
                g_outputs = outputs_by_ep.get(g_idx, [])

                if g_name not in structured_seasons_links or not isinstance(structured_seasons_links[g_name], dict):
                    structured_seasons_links[g_name] = {}

                for out in g_outputs:
                    q_label = out["quality"]
                    structured_seasons_links[g_name][q_label] = out["link"]

            update_fields = {
                "seasons_links": structured_seasons_links,
                "newly_added_groups": group_names
            }

        # Save to database
        if db._anime is not None:
            await db._anime.update_one(
                {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                {
                    "$set": update_fields,
                    "$currentDate": {"updated_at": True}
                }
            )

        summary = (
            "🎉 **Range Link Processing Completed Successfully!**\n\n"
            f"🎬 **Anime:** `{anime['title']}`\n"
            f"📊 **Total Processed:** `{total_msgs}` messages\n"
            f"✅ **Links Generated:** `{len(collected_outputs)}` link(s)\n"
            f"⚠️ **Failed:** `{len(failed_ids)}`\n"
            f"👥 **Groups Added/Updated:** `{len(group_names)}`\n\n"
            f"🔗 **Web Link:** {Config.BASE_URL}/anime/{anime['slug']}"
        )

        await status_msg.edit_text(summary)

    except Exception as err:
        logger.error(f"Range Link Task Error: {err}\n{traceback.format_exc()}")
        await status_msg.edit_text(f"❌ **Range Link Processing Error:** `{str(err)}`")

LANGUAGE_MAPPINGS = {
    "tel": "Telugu",
    "tam": "Tamil",
    "hin": "Hindi",
    "eng": "English",
    "jap": "Japanese",
    "jpn": "Japanese",
    "jp": "Japanese",
    "kan": "Kannada",
    "mal": "Malayalam",
    "ben": "Bengali",
    "mar": "Marathi",
    "kor": "Korean",
    "chi": "Chinese",
    "zho": "Chinese"
}

def parse_language_input(text):
    if not text:
        return "Telugu • Tamil • Hindi • English • Japanese"

    parts = [p.strip() for p in text.split(",") if p.strip()]
    parsed_names = []
    for p in parts:
        low = p.lower()
        if low in LANGUAGE_MAPPINGS:
            parsed_names.append(LANGUAGE_MAPPINGS[low])
        else:
            parsed_names.append(p.title() if p.islower() else p)

    if not parsed_names:
        return "Telugu • Tamil • Hindi • English • Japanese"

    return " • ".join(parsed_names)

def parse_group_names_list(text):
    if not text or not text.strip():
        return None
    lines = [line.strip() for line in text.strip().split("\n") if line.strip()]
    groups = []
    for line in lines:
        match = re.match(r'^\s*(\d+)\s*\.\s*(.*)$', line)
        if match:
            gname = match.group(2).strip()
            if gname:
                groups.append(gname)
            else:
                return None
        else:
            return None
    return groups if groups else None

def parse_advanced_group_message(text):
    """
    Parses a text block into structured custom box groups and buttons.
    Returns:
        tuple: (groups_dict, error_message)
        where groups_dict is { "Group Name": { "Button Label": "Link", ... }, ... }
        or (None, "Error description pointing to the specific group/button")
    """
    if not text:
        return None, "Empty message received."

    lines = text.strip().split("\n")
    groups = {}
    current_group_name = None
    current_buttons = {}
    group_num = 0

    i = 0
    n = len(lines)

    while i < n:
        line = lines[i].strip()
        if not line:
            i += 1
            continue

        # Check if line starts a new group, e.g. "1. Season 1" or "1."
        match = re.match(r"^\s*(\d+)\s*\.\s*(.*)$", line)
        if match:
            # Save previous group if active
            if current_group_name:
                if not current_buttons:
                    return None, f"Group '{current_group_name}' (Entry #{group_num}) has no buttons defined."
                groups[current_group_name] = current_buttons
                current_buttons = {}

            group_num = int(match.group(1))
            gname_part = match.group(2).strip()

            if gname_part:
                current_group_name = gname_part
            else:
                # Group name must be on next non-empty line
                i += 1
                next_gname = None
                while i < n:
                    next_line = lines[i].strip()
                    if next_line:
                        next_gname = next_line
                        break
                    i += 1
                if not next_gname:
                    return None, f"Group #{group_num} has a missing or empty Group Name."
                current_group_name = next_gname

            current_buttons = {}
            i += 1
            continue

        if not current_group_name:
            return None, f"Found button definition '{line}' before any group was defined (e.g. '1. Group Name')."

        if ":" not in line:
            return None, f"Invalid format in Group '{current_group_name}' (Entry #{group_num}): '{line}'. Missing ':' separator."

        parts = line.split(":", 1)
        btn_name = parts[0].strip()
        btn_link = parts[1].strip()

        if not btn_name:
            return None, f"Invalid button label in Group '{current_group_name}' (Entry #{group_num}): '{line}'."

        if not (btn_link.startswith("http://") or btn_link.startswith("https://")):
            return None, f"Invalid URL in Group '{current_group_name}' (Entry #{group_num}) for button '{btn_name}': '{btn_link}'."

        current_buttons[btn_name] = btn_link
        i += 1

    if current_group_name:
        if not current_buttons:
            return None, f"Group '{current_group_name}' (Entry #{group_num}) has no buttons defined."
        groups[current_group_name] = current_buttons

    if not groups:
        return None, "No valid groups or buttons found. Please follow the required format (e.g. starting with '1. Season 1')."

    return groups, None

ADMIN_GUIDE_PAGES = [
    (
        "👑 **ANIZONEFLIX ULTRA: Executive Admin Guide (A to Z)**\n\n"
        "Welcome to the comprehensive, step-by-step master guide for AniZoneFlix Ultra Administration.\n\n"
        "**📚 Guide Navigation Index:**\n"
        "• Page 1: Overview & Core Features\n"
        "• Page 2: `/search` & `/add_post` (Intelligence Feeds & Rapid Publication)\n"
        "• Page 3: `/post` (Channel Publication & Channel Configuration)\n"
        "• Page 4: `/add_page` & `/manual` (Manual Page Creation)\n"
        "• Page 5: `/edit` & `/edit_m` (Content Groups, Custom Boxes & Buttons)\n"
        "• Page 6: `/change_poster` & `/categories` (Artwork & Category Management)\n"
        "• Page 7: `/schedule` (Airing Schedule Management)\n"
        "• Page 8: `/songs` (Background Music & Web Audio)\n"
        "• Page 9: `/uptime` (24/7 Bot Uptime Monitor)\n"
        "• Page 10: `/setbot` & `/ss` (Genlink Bot & Session String Configuration)\n"
        "• Page 11: **Range Links (Genlink & Serial Automation)**\n"
        "• Page 12: `/save` & `/category_page` (Backup/Restore & Page Migration)\n"
        "• Page 13: `/del`, `/ping`, `/cancel` & Process Security\n\n"
        "💡 *Use the navigation buttons below to flip through pages.*"
    ),
    (
        "📖 **Page 1: Overview & Core Architecture**\n\n"
        "AniZoneFlix is an industrial-grade anime portal and automated management suite.\n\n"
        "**🌟 Key Capabilities:**\n"
        "• **Multi-API Aggregation:** Fetches metadata from Jikan (MAL), AniList, Kitsu, TMDB, Shikimori & Simkl.\n"
        "• **Color-Coded Interactive UI:** Native Pyrogram/Kurigram `ButtonStyle` inline buttons (`PRIMARY` / BLUE, `SUCCESS` / GREEN, `DANGER` / RED).\n"
        "• **Batch Link Automation:** Process message range links via Genlink and Serial batch modes.\n"
        "• **24/7 Uptime Monitoring:** Continuous 1-second ping monitoring for external bots/services.\n"
        "• **Web Audio Engine:** Embedded Web Audio API background music player with 300% gain.\n"
        "• **Backup & Restore System:** Instant ZIP export and restoration for zero-data-loss security."
    ),
    (
        "📖 **Page 2: `/search` & `/add_post` Commands**\n\n"
        "**🔍 Command: `/search <anime_title>`**\n"
        "• Scans external intelligence feeds across all providers simultaneously.\n"
        "• Returns top matching titles as interactive buttons.\n"
        "• Selecting a title launches step-by-step page wizard (Title, Synopsis, Score, Artwork, Seasons).\n\n"
        "**⚡ Command: `/add_post <anime_title>`**\n"
        "• One-shot rapid deployment tool.\n"
        "• Automatically selects the top match, auto-enriches missing metadata, and prompts for target category.\n"
        "• Instantly publishes the page to the web portal in seconds."
    ),
    (
        "📖 **Page 3: `/post` Command (Channel Posting)**\n\n"
        "**📢 Command: `/post`**\n"
        "Publishes formatted anime announcements directly to your Telegram channel with attached styled page link buttons.\n\n"
        "**💡 Usage Syntax:**\n"
        "• `/post <CHANNEL_ID>` — Configure target channel (e.g. `/post -1001234567890` or `/post @mychannel`)\n"
        "• `/post <PAGE_LINK>` — Interactive prompt asking for custom post heading\n"
        "• `/post <PAGE_LINK> | <HEADING>` — One-shot instant publication to channel\n\n"
        "**✨ Attached Inline Button:**\n"
        "Channel posts automatically attach a styled inline button (`📥 <Anime Title>`) linking directly to the web portal page."
    ),
    (
        "📖 **Page 4: `/add_page` & `/manual` Commands**\n\n"
        "**📝 Command: `/add_page`**\n"
        "• Manual page wizard for adding custom anime entries.\n"
        "• Prompts for title, synopsis, rating, poster URL, and custom group links.\n\n"
        "**🛠 Command: `/manual`**\n"
        "• Advanced step-by-step manual creator.\n"
        "• Allows specifying custom title, synopsis, genre, direct poster asset URL, rating, button counts, and custom button links."
    ),
    (
        "📖 **Page 5: `/edit` & `/edit_m` Commands**\n\n"
        "**🏛 Command: `/edit <url/slug>`**\n"
        "The Executive Suite for managing existing pages:\n"
        "• **Content Groups (Seasons):** Add, rename, reorder, or delete season links (480p, 720p, 1080p).\n"
        "• **Custom Boxes:** Create and manage custom box containers.\n"
        "• **External Redirects:** Add external buttons.\n"
        "• **Advanced & Ultra Advanced Group:** Import structured text blocks.\n"
        "• **Change Category, Title, Poster or Purge Archive.**\n\n"
        "**🖇 Command: `/edit_m <url/slug>`**\n"
        "Dedicated Custom Button Management suite for top-level external links, custom boxes, and audio language settings."
    ),
    (
        "📖 **Page 6: `/change_poster` & `/categories` Commands**\n\n"
        "**🖼 Command: `/change_poster <url/slug>`**\n"
        "• Swaps the poster image/artwork for any series page instantly.\n"
        "• Accepts direct HTTP/HTTPS image URLs.\n\n"
        "**📂 Command: `/categories`**\n"
        "• Category and Genre Management Console.\n"
        "• View all existing categories, add new categories, or delete unused categories.\n"
        "• All changes synchronize live with website filter badges."
    ),
    (
        "📖 **Page 7: `/schedule` Command**\n\n"
        "**📅 Command: `/schedule`**\n"
        "Airing Schedule Management Console:\n"
        "• View and update daily airing anime schedules (Monday through Sunday).\n"
        "• **Usage:** `/schedule {TIME} {NAME} {OPTIONAL_IMAGE_URL}`\n"
        "• Day selection buttons let you assign entries to specific days.\n"
        "• Supports manual override text input."
    ),
    (
        "📖 **Page 8: `/songs` Command (Web Audio Engine)**\n\n"
        "**🎵 Command: `/songs`**\n"
        "Background Music Playlist Console:\n"
        "• Upload MP3, M4A, WAV, or MP4 files to serve as background music on the website.\n"
        "• Configure dedicated Song Channel for Telegram storage.\n"
        "• Replace or delete existing tracks.\n"
        "• The website renders tracks using Web Audio API with +300% gain booster."
    ),
    (
        "📖 **Page 9: `/uptime` Command (24/7 Monitor)**\n\n"
        "**⚡ Command: `/uptime`**\n"
        "Continuous 24/7 Uptime Monitoring Suite:\n"
        "• Monitor external bots, web services, or Render/Koyeb deployments.\n"
        "• Background monitor pings target HTTP/HTTPS URLs every 1 second.\n"
        "• Displays live HTTP status code, latency in milliseconds, and online/offline indicators."
    ),
    (
        "📖 **Page 10: `/setbot` & `/ss` Commands**\n\n"
        "**🤖 Command: `/setbot`**\n"
        "• Configures target link generator bot usernames used for range link batching.\n"
        "• Supports managing multiple configured bots.\n\n"
        "**🔑 Command: `/ss`**\n"
        "• Configures the Pyrogram User Session String.\n"
        "• Required for automated range link generation and `/serielbatch` monitoring."
    ),
    (
        "📖 **Page 11: Range Links (Genlink & Serial Automation)**\n\n"
        "**🔗 Feature: Message Range Link Automation**\n"
        "Send any Telegram message range link (e.g. `https://t.me/c/1234567890/100-200` or `https://t.me/mychan/10-50`):\n\n"
        "**Processing Flow:**\n"
        "1. Select Bot (if multiple bots configured via `/setbot`).\n"
        "2. Send group names list (e.g. `1. Season 1`, `2. Season 2`).\n"
        "3. Send filter name (or `.` for all).\n"
        "4. Send target anime page link/slug.\n"
        "5. Select destination box (or Main Section).\n"
        "6. Select Mode:\n"
        "   • **Genlink**: Sequential `/genlink` requests per message.\n"
        "   • **Serial**: Automated `/serielbatch` stream execution and bulk block parsing."
    ),
    (
        "📖 **Page 12: `/save` & `/category_page` Commands**\n\n"
        "**💾 Command: `/save`**\n"
        "Database Backup & Restore System:\n"
        "• **📥 BACKUP:** Generates and sends a complete `backup.zip` file containing JSON exports of all MongoDB collections.\n"
        "• **📤 RESTORE:** Upload a `backup.zip` file to restore and overwrite database records safely.\n\n"
        "**📂 Command: `/category_page <url/slug>`**\n"
        "Migrates an existing anime page from its current category to any destination category with one click."
    ),
    (
        "📖 **Page 13: `/del`, `/ping`, `/cancel` & Process Security**\n\n"
        "**🗑 Command: `/del <url/slug>`**\n"
        "Permanently erases an anime entry and its associated content from the MongoDB database.\n\n"
        "**🏓 Command: `/ping`**\n"
        "Checks bot latency and database connection status.\n\n"
        "**✨ Command: `/cancel`**\n"
        "Aborts any active interaction process, input state, or wizard flow."
    )
]

async def send_admin_guide_page(client, message_or_cb, page_idx=0, user_id=0):
    if not await is_authorized(user_id):
        if isinstance(message_or_cb, CallbackQuery):
            return await message_or_cb.answer("🚫 Access Denied. Authorized admins only.", show_alert=True)
        else:
            return await message_or_cb.reply("🚫 **Access Denied.** Authorized admins only.")

    total_pages = len(ADMIN_GUIDE_PAGES)
    page_idx = max(0, min(page_idx, total_pages - 1))
    text = ADMIN_GUIDE_PAGES[page_idx]

    nav_buttons = []
    if page_idx > 0:
        nav_buttons.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"guide_nav_{page_idx-1}", style=ButtonStyle.PRIMARY))

    nav_buttons.append(InlineKeyboardButton(f"📖 Page {page_idx+1}/{total_pages}", callback_data="guide_noop", style=ButtonStyle.PRIMARY))

    if page_idx < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton("Next ➡️", callback_data=f"guide_nav_{page_idx+1}", style=ButtonStyle.PRIMARY))

    buttons = [
        nav_buttons,
        [InlineKeyboardButton("❌ Close", callback_data="cancel_op", style=ButtonStyle.DANGER)]
    ]

    markup = InlineKeyboardMarkup(buttons)

    if isinstance(message_or_cb, CallbackQuery):
        try:
            await message_or_cb.message.edit_text(text, reply_markup=markup)
            await message_or_cb.answer()
        except MessageNotModified:
            await message_or_cb.answer()
    else:
        await message_or_cb.reply(text, reply_markup=markup)

def register_handlers(bot: Client):
    logger.info("Initializing Hardened Intelligence Suite Handlers...")

    # Central authorization guard: protects management commands even if an
    # individual handler forgets its own check. Public commands stay public.
    admin_commands = [
        "post", "add_post", "add_page", "manual", "edit", "edit_m",
        "change_poster", "categories", "create", "schedule", "del", "category_page",
        "export_boxes", "delete_boxes", "duplicate", "logo", "addbot", "songs",
        "setbot", "ss", "save", "uptime",
    ]

    @bot.on_message(filters.command(admin_commands), group=-1)
    async def centralized_admin_command_guard(client, message):
        uid = message.from_user.id if message.from_user else None
        if not await is_authorized(uid):
            await message.reply("🚫 **Access Denied.** This command is restricted to configured administrators.")
            raise StopPropagation
        raise ContinuePropagation

    # Do not apply a blanket authorization handler to *every* callback query.
    # It intercepted category-delete callbacks before their dedicated handlers
    # and could produce a false Access Denied for valid admin sessions. Each
    # management callback below performs its own authorization check; public
    # callbacks remain usable by ordinary users.

    # --- ADDBOT PLUGIN HANDLER ---
    from bot.plugins.addbot import addbot_command_handler
    bot.add_handler(MessageHandler(addbot_command_handler, filters.command("addbot") & filters.private))

    # --- DEBUG LOGGER (GROUP -3) ---
    @bot.on_message(filters.all, group=-3)
    async def debug_logger(client, message):
        # logger.debug(f"UPDATE: {message.chat.id} -> {message.text or 'MEDIA'}")
        raise ContinuePropagation

    # --- AUTO-LINK HANDLER (GROUP -2) ---
    @bot.on_message(filters.all, group=-2)
    async def auto_file_grouping(client, message):
        if (message.document or message.video) and message.from_user and not message.from_user.is_bot:
            try:
                from utils.parser import parse_filename
                fname = message.document.file_name if message.document else "video.mp4"
                parsed = parse_filename(fname)
                if await db.ping():
                    anime = await db.anime.find_one({"title": {"$regex": parsed["title"], "$options": "i"}})
                    if anime:
                        await db.add_episode({
                            "mal_id": anime["mal_id"], "season": parsed["season"], "episode": parsed["episode"],
                            "quality": parsed["quality"], "audio": parsed["audio"], "codec": parsed["codec"],
                            "file_id": message.document.file_id if message.document else message.video.file_id,
                            "file_name": fname, "file_size": "N/A", "views": 0, "downloads": 0
                        })
                        logger.info(f"Auto-Link Success: {fname} -> {anime['title']}")
            except Exception as e:
                logger.error(f"Auto-Link Error: {e}")

        raise ContinuePropagation

    # --- COMMAND HANDLERS (GROUP 0) ---

    @bot.on_message(filters.command("ping"))
    async def ping_handler(client, message):
        db_status = "Connected" if await db.ping() else "Disconnected"
        await message.reply(f"⚡ **System Status:** Operational\n🗄 **Database:** {db_status}\n🏓 **Latency Check:** Minimal/Responsive.")

    @bot.on_message(filters.command(["post", "POST"]))
    async def post_command_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Unauthorized user.")

        query = " ".join(message.command[1:]).strip()
        if not query and message.reply_to_message:
            query = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()

        if not query:
            post_ch = await db.get_post_channel()
            ch_status = f"`{post_ch}`" if post_ch else "*Not Configured*"
            return await message.reply(
                "📢 **Post Channel Management**\n\n"
                f"Currently Configured Channel: {ch_status}\n\n"
                "💡 **Usage:**\n"
                "• `/post <CHANNEL_ID>` — Configure target channel (e.g. `/post -1001234567890` or `/post @mychannel`)\n"
                "• `/post <PAGE_LINK>` — Prepare and publish post to channel\n"
                "• `/post <PAGE_LINK> | <HEADING>` — Publish post instantly"
            )

        # Check if query is channel configuration e.g. -100... or @channel
        if (query.startswith("-100") or query.startswith("@") or query.replace("-", "").isdigit()) and "http" not in query:
            channel_id = query
            try:
                test_msg = await safe_send_channel_message(client, channel_id, "⚡ **AniZoneFlix Channel Verification Ping...**")
                await client.delete_messages(channel_id, test_msg.id)
                await db.set_post_channel(channel_id)
                return await message.reply(f"✅ **Post Channel Successfully Configured:** `{channel_id}`")
            except Exception as err:
                return await message.reply(
                    f"❌ **Failed to verify post channel `{channel_id}`:** {err}\n\n"
                    "Please make sure the bot is an admin with posting permissions in the channel."
                )

        # Check for one-shot heading e.g. PAGE_LINK | HEADING or PAGE_LINK\nHEADING
        one_shot_heading = None
        if "|" in query:
            parts = query.split("|", 1)
            link_part = parts[0].strip()
            one_shot_heading = parts[1].strip()
        elif "\n" in query:
            parts = query.split("\n", 1)
            link_part = parts[0].strip()
            one_shot_heading = parts[1].strip()
        else:
            link_part = query

        post_channel = await db.get_post_channel()
        if not post_channel:
            return await message.reply(
                "⚠️ **Post Channel Not Configured!**\n\n"
                "Please configure the target channel first using:\n"
                "`/post <CHANNEL_ID_OR_USERNAME>`\n\n"
                "Example: `/post -1001234567890` or `/post @mychannel`"
            )

        slug = extract_slug(link_part)
        anime = await db.get_anime(slug)
        if not anime:
            results = await db.search_anime_db(link_part)
            if results:
                anime = results[0]

        if not anime:
            return await message.reply(f"❌ **Page Not Found in Database:** `{slug}`")

        # Automatically enrich missing metadata if needed
        from api.anime_api import auto_fill_missing_metadata
        anime = await auto_fill_missing_metadata(anime)

        aid = str(anime["_id"])
        full_page_link = link_part if link_part.startswith("http") else f"{Config.BASE_URL}/anime/{anime['slug']}"

        if one_shot_heading:
            # Publish immediately
            try:
                sent_msg = await publish_post_to_channel(client, post_channel, one_shot_heading, anime, full_page_link)
                return await message.reply(
                    f"🎉 **Post Successfully Published to Channel!**\n\n"
                    f"📢 **Channel:** `{post_channel}`\n"
                    f"💬 **Message ID:** `{sent_msg.id}`"
                )
            except Exception as post_err:
                logger.error(f"Error publishing post to channel {post_channel}: {post_err}")
                return await message.reply(f"❌ **Failed to publish post to channel `{post_channel}`:** `{str(post_err)}`")

        # Otherwise prompt for heading
        user_state[message.from_user.id] = {
            "action": "ask_post_heading",
            "aid": aid,
            "slug": anime["slug"],
            "page_link": full_page_link
        }

        await message.reply(
            f"📌 **Send the Heading for this Post:**\n\n"
            f"🎬 **Title:** `{anime['title']}`\n"
            f"🔗 **Link:** `{full_page_link}`\n\n"
            "Example:\n`🏁 Tokyo Ghoul:re(season - 3) — Added! 🎉`\n\n"
            "Send /cancel to abort."
        )

    @bot.on_message(filters.command("start"))
    async def start_handler(client, message):
        await message.reply_photo(
            photo=Config.LOGO_URL,
            caption=(
                "👑 **ANIZONEFLIX PREMIUM v2.0**\n\n"
                "Welcome to the premier Anime Management Suite. Experience seamless automation and high-speed metadata intelligence.\n\n"
                "⚡ **Quick Start:**\n"
                "• `/search <name>` — Automated series setup\n"
                "• `/add_post <name>` — Rapid one-shot publication\n"
                "• `/add_page` — Manual content creation\n"
                "• `/edit <url>` — Manage content groups\n"
                "• `/help` — View full documentation"
            ),
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("🌐 Access Portal", url=Config.BASE_URL, style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("📚 Admin Guide", callback_data="help_guide", style=ButtonStyle.PRIMARY)]
            ])
        )

    @bot.on_message(filters.command("help"))
    async def help_handler(client, message):
        uid = message.from_user.id if message.from_user else 0
        await send_admin_guide_page(client, message, page_idx=0, user_id=uid)

    @bot.on_message(filters.command("add_page"))
    async def add_page_handler(client, message):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Unauthorized.**")

        title = " ".join(message.command[1:])
        user_state[message.from_user.id] = {
            "action": "edit_title",
            "anime_data": {
                "title": title or "Untitled Content",
                "synopsis": "N/A",
                "score": 8.5,
                "image": Config.LOGO_URL,
                "genres": [],
                "status": "Airing",
                "year": "2024",
                "trailer": None,
                "studios": []
            }
        }

        await message.reply(
            "📝 **Manual Page Creation**\n\n"
            f"Step 1: Calibration for `{user_state[message.from_user.id]['anime_data']['title']}`\n\n"
            "📥 Please send the **New Title** or `/skip` to maintain current:"
        )

    @bot.on_message(filters.command("search"))
    async def search_handler(client, message, is_retry=False):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.**")

        query = " ".join(message.command[1:]) if not is_retry else message.text
        if not query:
            user_state[message.from_user.id] = {"action": "ask_search_query"}
            return await message.reply("🛰 **Intelligence Aggregator**\n\nPlease send the **Title** of the series:")

        msg = await message.reply("📡 **Scanning Intelligence Feeds...**")
        results = []
        try:
            results = await asyncio.wait_for(anime_api.search_all(query), timeout=15)
        except Exception as e:
            logger.error(f"External Search Error/Timeout: {e}")

        # Fall back to database search if external search returned empty
        if not results:
            try:
                db_matches = await db.search_anime_intelligent(query)
                if db_matches:
                    results = [
                        {
                            "source": "database",
                            "id": str(m["_id"]),
                            "title": m["title"],
                            "image": m.get("image"),
                            "year": m.get("year", "N/A")
                        }
                        for m in db_matches
                    ]
            except Exception as dbe:
                logger.error(f"DB Fallback Search Error: {dbe}")

        if not results:
            user_state[message.from_user.id] = {"action": "ask_search_query"}
            return await msg.edit("😔 **Search Exhausted.** No matches found. Try again:")

        search_results[message.from_user.id] = results
        text = "🎯 **Select Match from Feed:**\n\n"
        for i, res in enumerate(results[:10], 1):
            text += f"**{i}.** {res['title']} ({res.get('year') or 'N/A'}) `[{res['source'].upper()}]`\n"

        await msg.edit(text)
        user_state[message.from_user.id] = {"action": "select_anime"}

    @bot.on_message(filters.command("add_post"))
    async def auto_post_handler(client, message):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Unauthorized.**")

        query = " ".join(message.command[1:])
        if not query: return await message.reply("💡 **Usage:** `/add_post <name>`")

        msg = await message.reply(f"⚡ **Rapid Publication: {query}...**")
        try:
            results = await anime_api.search_all(query)
            if not results: return await msg.edit("❌ **Publication Failed:** No match found.")

            best_match = results[0]
            details = await anime_api.get_details(best_match["source"], best_match["id"])
            if not details:
                details = {"title": best_match["title"], "synopsis": "N/A", "score": 0, "image": best_match["image"], "genres": [], "status": "N/A", "year": best_match["year"], "episodes": 0, "trailer": None, "studios": []}

            user_state[message.from_user.id] = {"action": "ask_category", "anime_data": details, "season": "1", "image": details["image"]}
            cats = await db.get_all_categories()
            buttons = [[InlineKeyboardButton(c['name'], callback_data=f"setcat_{c['name']}", style=ButtonStyle.PRIMARY)] for c in (cats if cats else [{"name": "Anime"}])]
            await message.reply_photo(photo=details["image"] if details["image"] else Config.LOGO_URL, caption=f"🎬 **Archive Ready:** `{details['title']}`\n\nTarget **Category**:", reply_markup=InlineKeyboardMarkup(buttons))
            await msg.delete()
        except Exception as e:
            logger.error(f"Add Post Error: {e}")
            await msg.edit("❌ **Rapid Deployment Failure.**")

    @bot.on_message(filters.command(["edit", "edit_m"]))
    async def unified_edit_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Unauthorized.**")

        query = " ".join(message.command[1:]).strip()
        if not query and message.reply_to_message:
            query = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()

        slug = extract_slug(query)
        if not slug:
            return await message.reply("💡 **Usage:** `/edit <url>` (or reply to a link)")

        try:
            anime = await db.get_anime(slug)
            if not anime:
                results = await db.search_anime_db(query)
                if results: anime = results[0]

            if not anime: return await message.reply(f"❌ **Not Found:** `{slug}`")
            aid = str(anime["_id"])

            if message.command[0] == "edit_m":
                buttons = [
                    [InlineKeyboardButton("🔊 Languages", callback_data=f"manage_langs_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("📦 Add Custom Group", callback_data=f"add_cgrp_start_{aid}", style=ButtonStyle.SUCCESS)],
                    [InlineKeyboardButton("➕ Add Custom Button", callback_data=f"add_btn_start_{aid}", style=ButtonStyle.SUCCESS)],
                    [InlineKeyboardButton("🗃 Add Custom Box", callback_data=f"add_box_start_{aid}", style=ButtonStyle.SUCCESS)],
                    [InlineKeyboardButton("🚀 Advanced Group", callback_data=f"adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🔥 Ultra Advanced Group", callback_data=f"ultra_adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("📋 Manage Boxes", callback_data=f"manage_boxes_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("📝 Manage Buttons", callback_data=f"manage_btns_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"edit_m_back_{aid}", style=ButtonStyle.SUCCESS)],
                    [InlineKeyboardButton("🛡 Back to Archive", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.PRIMARY), InlineKeyboardButton("❌ Abort", callback_data="cancel_op", style=ButtonStyle.DANGER)]
                ]
                await message.reply(
                    f"🖇 **Custom Management: {anime['title']}**\n\nSelect a sector to manage:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                buttons = [
                    [InlineKeyboardButton("👑 Admin Choice (Manage Content Groups)", callback_data=f"manage_groups_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🔊 Languages", callback_data=f"manage_langs_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("📦 Content Groups (Seasons)", callback_data=f"manage_groups_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🗃 Custom Boxes", callback_data=f"manage_boxes_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🔗 External Redirects (Buttons)", callback_data=f"manage_btns_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🚀 Advanced Group", callback_data=f"adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🔥 Ultra Advanced Group", callback_data=f"ultra_adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("📂 Change Category", callback_data=f"manage_category_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🏷 Change Title", callback_data=f"edit_title_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🖼 Change Poster", callback_data=f"trigger_poster_{aid}", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🗑 Purge Archive", callback_data=f"confirm_purge_{aid}", style=ButtonStyle.DANGER)],
                    [InlineKeyboardButton("🔄 Refresh", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.SUCCESS), InlineKeyboardButton("❌ Abort", callback_data="cancel_op", style=ButtonStyle.DANGER)]
                ]
                await message.reply(
                    f"🏛 **Executive Suite: {anime['title']}**\n"
                    f"ID: `{aid}`\n\n"
                    "Select a sector to manage:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
        except Exception as e:
            logger.error(f"Edit Cmd Error: {e}")
            await message.reply("❌ **Intelligence Feed Offline.**")

    @bot.on_message(filters.command("change_poster"))
    async def change_poster_handler(client, message):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.**")

        query = " ".join(message.command[1:])
        if not query and message.reply_to_message:
            query = message.reply_to_message.text or message.reply_to_message.caption or ""

        slug = extract_slug(query)
        if not slug: return await message.reply("💡 **Usage:** `/change_poster <url>`")

        try:
            anime = await db.get_anime_by_slug(slug)
            if not anime: return await message.reply(f"🔍 **Not Found:** `{slug}`")
            user_state[message.from_user.id] = {"action": "ask_new_poster", "slug": slug}
            await message.reply(f"🖼 **Artwork Update:** `{anime['title']}`\n\nSend **New Asset URL**:")
        except Exception as e:
            logger.error(f"Change Poster Error: {e}")
            await message.reply("❌ **Database Failure.**")

    @bot.on_message(filters.command("create") & filters.private)
    async def create_category_handler(client, message):
        uid = message.from_user.id if message.from_user else None
        if not await is_authorized(uid):
            return await message.reply("🚫 **Access Denied.** Admins only.")
        user_state[uid] = {"action": "create_category_await_name"}
        await message.reply("🆕 **Create a synchronized category**\n\nSend the new category name. Use /cancel to stop.")

    async def show_create_category_sources(message, uid, category_name):
        categories = await db.get_all_categories()
        categories = [str(c.get("name", "")).strip() for c in categories if str(c.get("name", "")).strip() and str(c.get("name", "")).strip().casefold() != category_name.casefold()]
        if not categories:
            user_state.pop(uid, None)
            return await message.reply("❌ No existing source categories were found.")
        user_state[uid] = {"action": "create_category_choose_sources", "new_category": category_name, "categories": categories, "selected": []}
        await render_create_category_sources(message, uid)

    async def render_create_category_sources(message_or_callback, uid):
        state = user_state.get(uid, {})
        categories, selected = state.get("categories", []), set(state.get("selected", []))
        buttons = []
        for index, name in enumerate(categories):
            mark = "✅" if name in selected else "▫️"
            buttons.append([InlineKeyboardButton(f"{mark} {name[:48]}", callback_data=f"create_toggle:{uid}:{index}", style=ButtonStyle.SUCCESS if name in selected else ButtonStyle.PRIMARY)])
        buttons.append([InlineKeyboardButton(f"✅ Done ({len(selected)})", callback_data=f"create_done:{uid}", style=ButtonStyle.SUCCESS)])
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)])
        text_value = f"📂 **New category:** {state.get('new_category')}\n\nSelect one or more source categories. All current and future content pages will be synchronized automatically."
        markup = InlineKeyboardMarkup(buttons)
        if hasattr(message_or_callback, "message") and hasattr(message_or_callback, "data"):
            try:
                await message_or_callback.message.edit_text(text_value, reply_markup=markup)
            except MessageNotModified:
                pass
            await message_or_callback.answer()
        else:
            await message_or_callback.reply(text_value, reply_markup=markup)

    @bot.on_callback_query(filters.regex(r"^create_toggle:"))
    async def create_category_toggle_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid, {})
        if not await is_authorized(uid) or state.get("action") != "create_category_choose_sources":
            return await callback_query.answer("This selection has expired.", show_alert=True)
        try:
            _, owner, raw_index = callback_query.data.split(":")
            index = int(raw_index)
            if int(owner) != uid or index < 0 or index >= len(state["categories"]):
                raise ValueError("invalid selection")
            name = state["categories"][index]
            selected = set(state.get("selected", []))
            selected.remove(name) if name in selected else selected.add(name)
            state["selected"] = sorted(selected, key=str.casefold)
            await render_create_category_sources(callback_query, uid)
        except Exception as exc:
            logger.exception("/create source selection failed")
            await callback_query.answer(f"Selection error: {str(exc)[:100]}", show_alert=True)

    @bot.on_callback_query(filters.regex(r"^create_done:"))
    async def create_category_done_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid, {})
        if not await is_authorized(uid) or state.get("action") != "create_category_choose_sources":
            return await callback_query.answer("This selection has expired.", show_alert=True)
        try:
            _, owner = callback_query.data.split(":")
            if int(owner) != uid:
                raise ValueError("owner")
            destination = state.get("new_category", "").strip()
            sources = list(state.get("selected", []))
            if not destination or not sources:
                return await callback_query.answer("Select at least one source category.", show_alert=True)
            existing = await db.get_all_categories()
            if any(str(c.get("name", "")).casefold() == destination.casefold() for c in existing):
                return await callback_query.answer("That category already exists. Choose another name.", show_alert=True)
            result = await db.add_category(destination)
            if result is None:
                return await callback_query.answer("Could not save the category.", show_alert=True)
            import hashlib
            config_id = hashlib.sha256((destination.casefold() + "|" + "|".join(sorted(x.casefold() for x in sources))).encode()).hexdigest()[:20]
            configs = await db.get_setting("created_category_sync", []) or []
            configs = [c for c in configs if str(c.get("id")) != config_id and str(c.get("destination", "")).casefold() != destination.casefold()]
            configs.append({"id": config_id, "destination": destination, "sources": sources, "created_at": time.time()})
            saved = await db.set_setting("created_category_sync", configs)
            if saved is None:
                return await callback_query.answer("Category created, but sync settings could not be saved. Please retry.", show_alert=True)
            user_state.pop(uid, None)
            # Start an immediate pass so existing pages are exported without waiting
            # for the next periodic synchronization interval.
            asyncio.create_task(sync_created_categories())
            await callback_query.answer("Category created; synchronization enabled.")
            await callback_query.message.edit_text(
                f"✅ **Category created: {destination}**\n\n"
                f"📂 Sources: {', '.join(sources)}\n"
                "🔄 Existing pages are being exported now. New and updated pages will sync automatically.\n"
                "🛡 Pages created by /create are excluded from automatic empty-page deletion.\n"
                f"🔗 Page URLs use the suffix `-{slugify(destination)}`."
            )
        except Exception as exc:
            logger.exception("/create completion failed")
            await callback_query.answer(f"Create failed: {str(exc)[:120]}", show_alert=True)

    @bot.on_message(filters.command("categories"))
    async def categories_handler(client, message):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.**")

        try:
            cats = await db.get_all_categories()
            buttons = [[InlineKeyboardButton(f"🏷 {c['name']}", callback_data=f"vcat_{c['name']}", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🗑", callback_data=f"del_cat_{c['name']}", style=ButtonStyle.DANGER)] for c in cats]
            buttons.append([InlineKeyboardButton("➕ Add Category", callback_data="add_cat_prompt", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="categories_refresh", style=ButtonStyle.SUCCESS)])
            await message.reply("📂 **Category Management**", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logger.error(f"Categories Error: {e}")
            await message.reply("❌ **Database Failure.**")

    @bot.on_message(filters.command("schedule"))
    async def schedule_handler(client, message):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.**")

        args = message.text.split(None, 2)
        if len(args) > 1:
            time = args[1]
            image = None
            remaining = args[2] if len(args) > 2 else ""

            # Split remaining into name and potential image URL
            parts = remaining.rsplit(None, 1)
            if len(parts) > 1 and parts[1].startswith("http"):
                name = parts[0]
                image = parts[1]
            else:
                name = remaining

            if not name:
                return await message.reply("💡 **Usage:** `/schedule {TIME} {NAME} {OPTIONAL_IMAGE_URL}`")

            user_state[message.from_user.id] = {
                "action": "add_sched_entry",
                "entry": {"time": time, "name": name, "image": image}
            }

            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            buttons = [[InlineKeyboardButton(days[i], callback_data=f"addsched_{days[i]}", style=ButtonStyle.PRIMARY), InlineKeyboardButton(days[i+1], callback_data=f"addsched_{days[i+1]}", style=ButtonStyle.PRIMARY)] for i in range(0, 6, 2)]
            buttons.append([InlineKeyboardButton("Sunday", callback_data="addsched_Sunday", style=ButtonStyle.PRIMARY)])
            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)])

            return await message.reply(
                f"📅 **Adding Entry:**\n\n"
                f"🕒 **Time:** `{time}`\n"
                f"🎬 **Name:** `{name}`\n"
                f"🖼 **Image:** `{'Yes' if image else 'No'}`\n\n"
                "Select the day to add this entry:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
        buttons = [[InlineKeyboardButton(days[i], callback_data=f"edit_sched_{days[i]}", style=ButtonStyle.PRIMARY), InlineKeyboardButton(days[i+1], callback_data=f"edit_sched_{days[i+1]}", style=ButtonStyle.PRIMARY)] for i in range(0, 6, 2)]
        buttons.append([InlineKeyboardButton("Sunday", callback_data="edit_sched_Sunday", style=ButtonStyle.PRIMARY)])
        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="schedule_refresh", style=ButtonStyle.SUCCESS)])
        await message.reply("📅 **Airing Schedule Management**\nSelect a day to manually update (overwrites with text):", reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex("^schedule_refresh$"))
    async def schedule_refresh_cb(client, callback_query):
        try:
            days = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
            buttons = [[InlineKeyboardButton(days[i], callback_data=f"edit_sched_{days[i]}", style=ButtonStyle.PRIMARY), InlineKeyboardButton(days[i+1], callback_data=f"edit_sched_{days[i+1]}", style=ButtonStyle.PRIMARY)] for i in range(0, 6, 2)]
            buttons.append([InlineKeyboardButton("Sunday", callback_data="edit_sched_Sunday", style=ButtonStyle.PRIMARY)])
            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="schedule_refresh", style=ButtonStyle.SUCCESS)])
            await callback_query.message.edit_text("📅 **Airing Schedule**", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer("Schedule Synchronized")
        except MessageNotModified:
            await callback_query.answer("Already Synchronized")
        except Exception as e:
            logger.error(f"Schedule Refresh Error: {e}")
            await callback_query.answer("Sync Failed", show_alert=True)

    @bot.on_message(filters.command("del"))
    async def delete_handler(client, message):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.**")

        query = " ".join(message.command[1:])
        if not query and message.reply_to_message:
            query = message.reply_to_message.text or message.reply_to_message.caption or ""

        slug = extract_slug(query)
        if not slug: return await message.reply("💡 **Usage:** `/del <url/slug>`")

        try:
            res = await db.delete_anime_by_slug(slug)
            if res and res.deleted_count > 0:
                return await message.reply(f"🗑 **Erased:** `{slug}` removed.")
            anime = await db.anime.find_one({"title": {"$regex": query, "$options": "i"}})
            if anime:
                await db.delete_anime_by_slug(anime["slug"])
                return await message.reply(f"🗑 **Erased:** `{anime['title']}` removed.")
            await message.reply(f"❓ **Failed:** `{slug}` not found.")
        except Exception as e:
            logger.error(f"Delete Error: {e}")
            await message.reply("❌ **Erasure Failure.**")

    @bot.on_message(filters.command(["uptime", "UPTIME"]))
    async def uptime_command_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Unauthorized user.")

        await send_uptime_menu(client, message)

    async def send_uptime_menu(client, message_or_cb):
        bots = await db.get_all_monitored_bots()

        text = "⚡ **24/7 Bot Uptime Monitoring Suite**\n\n"
        text += f"📊 **Monitored Bots:** `{len(bots)}` (Continuous 1s check)\n\n"

        if bots:
            text += "**Live Monitor Feed:**\n"
            for idx, b in enumerate(bots, 1):
                status = b.get("status", "pending")
                icon = "🟢" if status == "online" else ("🔴" if status == "offline" else "🟡")
                code_str = f" [HTTP {b.get('status_code')}]" if b.get('status_code') else ""
                ms_str = f" ({b.get('response_time_ms')}ms)" if b.get('response_time_ms') is not None else ""
                url_str = b.get("url", "")
                text += f"**{idx}.** {icon} **{b.get('name', 'Bot')}**{code_str}{ms_str}\n🔗 `{url_str}`\n\n"
        else:
            text += "ℹ️ *No bots currently monitored. Click 'Add Bot' below to register a bot URL for continuous 24/7 uptime monitoring.*"

        buttons = [
            [InlineKeyboardButton("➕ Add Bot", callback_data="add_uptime_bot_prompt", style=ButtonStyle.SUCCESS)]
        ]

        if bots:
            for idx, b in enumerate(bots, 1):
                bid = b.get("bot_id") or str(b.get("_id"))
                bname = (b.get("name", f"Bot {idx}")[:14] + "..") if len(b.get("name", "")) > 16 else b.get("name", f"Bot {idx}")
                buttons.append([
                    InlineKeyboardButton(f"🔄 Replace {bname}", callback_data=f"replace_uptime_{bid}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton(f"🗑", callback_data=f"del_uptime_{bid}", style=ButtonStyle.DANGER)
                ])

        buttons.append([InlineKeyboardButton("🔄 Refresh Status", callback_data="uptime_refresh", style=ButtonStyle.SUCCESS), InlineKeyboardButton("❌ Close", callback_data="cancel_op", style=ButtonStyle.DANGER)])

        if isinstance(message_or_cb, CallbackQuery):
            try:
                await message_or_cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                await message_or_cb.answer()
            except MessageNotModified:
                await message_or_cb.answer("Already Up-to-date")
        else:
            await message_or_cb.reply(text, reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex("^uptime_refresh$"))
    async def uptime_refresh_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)
        await send_uptime_menu(client, callback_query)

    @bot.on_callback_query(filters.regex("^add_uptime_bot_prompt$"))
    async def add_uptime_bot_prompt_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        user_state[callback_query.from_user.id] = {"action": "ask_uptime_bot_url"}
        await callback_query.message.edit_text(
            "➕ **Add Bot for Uptime Monitoring**\n\n"
            "Please send the **HTTP / HTTPS URL** of the bot/service to monitor 24/7:\n\n"
            "Example: `https://my-telegram-bot.onrender.com`\n\n"
            "Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^replace_uptime_"))
    async def replace_uptime_bot_prompt_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        bot_id = callback_query.data.split("replace_uptime_")[-1]
        b = await db.get_monitored_bot(bot_id)
        if not b:
            return await callback_query.answer("❌ Monitored bot not found", show_alert=True)

        user_state[callback_query.from_user.id] = {"action": "ask_replace_uptime_url", "bot_id": bot_id}
        await callback_query.message.edit_text(
            f"🔄 **Replace Monitored Bot: {b.get('name', 'Bot')}**\n\n"
            f"Current URL: `{b.get('url')}`\n\n"
            "Please send the new **HTTP / HTTPS URL**:\n\n"
            "Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^del_uptime_"))
    async def del_uptime_bot_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        bot_id = callback_query.data.split("del_uptime_")[-1]
        b = await db.get_monitored_bot(bot_id)
        if b:
            await db.delete_monitored_bot(bot_id)
            await callback_query.answer(f"🗑 Stopped monitoring '{b.get('name', 'Bot')}'", show_alert=True)
        else:
            await callback_query.answer("❌ Bot record already deleted", show_alert=True)

        await send_uptime_menu(client, callback_query)

    @bot.on_message(filters.command("setbot"))
    async def setbot_command_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Unauthorized user.")

        args = message.text.split(None, 1)
        if len(args) > 1:
            bot_username = args[1].strip().lstrip("@")
            await db.add_configured_bot(bot_username)
            return await message.reply(f"✅ **Configured Bot Username Saved:** `@{bot_username}`")

        await send_setbot_menu(client, message)

    async def send_setbot_menu(client, message_or_cb):
        bots = await db.get_configured_bots()

        text = "🤖 **Configured Genlink Bots Management**\n\n"
        text += f"📊 **Total Configured Bots:** `{len(bots)}`\n\n"

        if bots:
            text += "**Active Bots List:**\n"
            for idx, b in enumerate(bots, 1):
                text += f"**{idx}.** `@{b}`\n"
        else:
            text += "ℹ️ *No bots configured yet. Click 'Add Bot' below to register a bot username.*"

        buttons = [
            [InlineKeyboardButton("➕ Add Bot", callback_data="add_setbot_prompt", style=ButtonStyle.SUCCESS)]
        ]

        if bots:
            for idx, b in enumerate(bots, 1):
                buttons.append([
                    InlineKeyboardButton(f"🗑 Delete @{b}", callback_data=f"del_setbot_{b}", style=ButtonStyle.DANGER)
                ])

        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="setbot_refresh", style=ButtonStyle.SUCCESS), InlineKeyboardButton("❌ Close", callback_data="cancel_op", style=ButtonStyle.DANGER)])

        if isinstance(message_or_cb, CallbackQuery):
            try:
                await message_or_cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                await message_or_cb.answer()
            except MessageNotModified:
                await message_or_cb.answer("Already Up-to-date")
        else:
            await message_or_cb.reply(text, reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex("^sel_range_bot_"))
    async def sel_range_bot_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        uid = callback_query.from_user.id
        selected_bot = callback_query.data.split("sel_range_bot_")[-1]

        state = user_state.get(uid)
        if not state:
            return await callback_query.answer("❌ Session Expired", show_alert=True)

        user_state[uid].update({
            "action": "ask_range_groups",
            "selected_bot": selected_bot
        })

        await callback_query.message.edit_text(
            f"🔗 **Range Link Received:** `{state['chat_slug']}` (Message IDs: `{state['start_id']}` to `{state['end_id']}`)\n"
            f"🤖 **Bot Selected:** `@{selected_bot}`\n\n"
            "Please send the **Group Names** in this format:\n\n"
            "1. Group name\n"
            "2. Group name\n"
            "3. Group name\n\n"
            "Send /cancel to abort."
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^sel_range_box_"))
    async def sel_range_box_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state:
            return await callback_query.answer("❌ Session Expired", show_alert=True)

        data = callback_query.data.split("sel_range_box_")[-1]

        target_box_idx = None
        if data.startswith("main_"):
            aid = data.split("main_")[-1]
        else:
            parts = data.split("_")
            target_box_idx = int(parts[0])
            aid = parts[1]

        user_state[uid]["target_box_idx"] = target_box_idx
        user_state[uid]["action"] = "ask_range_mode"

        anime_title = state.get("anime_title", aid)
        box_suffix = f"main_{aid}" if target_box_idx is None else f"{target_box_idx}_{aid}"

        buttons = [
            [
                InlineKeyboardButton("⚡ Genlink", callback_data=f"sel_range_mode_genlink_{box_suffix}", style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("📺 Serial", callback_data=f"sel_range_mode_serial_{box_suffix}", style=ButtonStyle.PRIMARY)
            ],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]
        ]

        await callback_query.message.edit_text(
            f"🎯 **Select Link Generation Mode:**\n\n"
            f"🎬 **Target Page:** `{anime_title}`\n\n"
            "• **Genlink**: Sends `/genlink <link>` sequentially for each message.\n"
            "• **Serial**: Uses `/serielbatch` automation stream in configured bot.",
            reply_markup=InlineKeyboardMarkup(buttons)
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^sel_range_mode_"))
    async def sel_range_mode_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state:
            return await callback_query.answer("❌ Session Expired", show_alert=True)

        # callback data format: sel_range_mode_<genlink|serial>_<main|box_idx>_<aid>
        raw = callback_query.data.split("sel_range_mode_")[-1]
        parts = raw.split("_")
        mode = parts[0] # genlink or serial

        target_box_idx = None
        if parts[1] == "main":
            aid = parts[2]
            box_dest_str = "Main Section (Default)"
        else:
            target_box_idx = int(parts[1])
            aid = parts[2]
            anime_doc = await db.get_anime(aid)
            boxes = anime_doc.get("custom_boxes", []) if anime_doc else []
            box_name = boxes[target_box_idx]["name"] if target_box_idx < len(boxes) else "Custom Box"
            box_dest_str = f"Custom Box: {box_name}"

        selected_bot = state.get("selected_bot") or (await db.get_configured_bot())
        chat_slug = state["chat_slug"]
        start_id = state["start_id"]
        end_id = state["end_id"]
        group_names = state["group_names"]
        filter_name = state.get("filter_name")
        anime_title = state.get("anime_title", aid)

        user_state.pop(uid, None)

        status_msg = await callback_query.message.edit_text(
            f"🚀 **Range Link Automation Initiated ({mode.upper()})!**\n\n"
            f"🎬 **Target Page:** `{anime_title}` (`{aid}`)\n"
            f"🤖 **Selected Bot:** `@{selected_bot}`\n"
            f"🔍 **Filter Name:** `{filter_name or 'N/A'}`\n"
            f"💬 **Channel/Chat:** `{chat_slug}`\n"
            f"🔢 **Message Range:** `{start_id}` to `{end_id}` ({end_id - start_id + 1} messages)\n"
            f"👥 **Groups Configured:** {len(group_names)}\n"
            f"📂 **Destination:** {box_dest_str}\n\n"
            f"⏳ Mode: `{mode}`..."
        )
        await callback_query.answer()

        asyncio.create_task(process_range_link_task(
            client, status_msg, aid, chat_slug, start_id, end_id, group_names,
            target_bot=selected_bot, target_box_idx=target_box_idx, filter_name=filter_name, mode=mode
        ))

    @bot.on_callback_query(filters.regex("^setbot_refresh$"))
    async def setbot_refresh_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)
        await send_setbot_menu(client, callback_query)

    @bot.on_callback_query(filters.regex("^add_setbot_prompt$"))
    async def add_setbot_prompt_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        user_state[callback_query.from_user.id] = {"action": "ask_setbot_username"}
        await callback_query.message.edit_text(
            "🤖 **Add Genlink Bot**\n\n"
            "Please send the **Bot Username** (e.g., `@AniZoneFlix_bot`):\n\n"
            "Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^del_setbot_"))
    async def del_setbot_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        bot_username = callback_query.data.split("del_setbot_")[-1]
        await db.delete_configured_bot(bot_username)
        await callback_query.answer(f"🗑 Deleted @{bot_username}", show_alert=True)
        await send_setbot_menu(client, callback_query)

    @bot.on_message(filters.command("ss"))
    async def ss_command_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Unauthorized user.")

        args = message.text.split(None, 1)
        if len(args) > 1:
            session_str = args[1].strip()
            await db.set_configured_session(session_str)
            return await message.reply("✅ **Pyrogram Session String Saved Successfully!**")

        user_state[message.from_user.id] = {"action": "ask_ss_string"}
        await message.reply("🔑 **Configure Pyrogram Session String**\n\nPlease send the **Session String**:")

    @bot.on_message(filters.command(["songs", "SONGS"]))
    async def songs_command_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Unauthorized user.")

        await send_songs_menu(client, message)

    async def send_songs_menu(client, message_or_cb):
        songs = await db.get_all_songs()
        channel_id = await db.get_song_channel()

        text = "🎵 **Background Songs Management**\n\n"
        text += f"📢 **Dedicated Song Channel:** `{channel_id or 'Not Configured'}`\n"
        text += f"🎼 **Total Songs:** `{len(songs)}`\n\n"

        if songs:
            text += "**Current Playlist:**\n"
            for idx, song in enumerate(songs, 1):
                text += f"**{idx}.** {song.get('title', 'Untitled Song')}\n"
        else:
            text += "ℹ️ *No background songs found. Upload a song to enable web audio playback.*"

        buttons = [
            [InlineKeyboardButton("➕ Add New Song", callback_data="add_song_prompt", style=ButtonStyle.SUCCESS)],
            [InlineKeyboardButton("📢 Set Song Channel", callback_data="set_song_channel_prompt", style=ButtonStyle.PRIMARY)]
        ]

        if songs:
            for idx, song in enumerate(songs, 1):
                sid = song.get("song_id") or str(song.get("_id"))
                stitle = (song.get("title", f"Song {idx}")[:15] + "..") if len(song.get("title", "")) > 18 else song.get("title", f"Song {idx}")
                buttons.append([
                    InlineKeyboardButton(f"🔄 Replace {stitle}", callback_data=f"replace_song_{sid}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton(f"🗑", callback_data=f"del_song_{sid}", style=ButtonStyle.DANGER)
                ])

        buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="songs_refresh", style=ButtonStyle.SUCCESS), InlineKeyboardButton("❌ Close", callback_data="cancel_op", style=ButtonStyle.DANGER)])

        if isinstance(message_or_cb, CallbackQuery):
            try:
                await message_or_cb.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                await message_or_cb.answer()
            except MessageNotModified:
                await message_or_cb.answer("Already Up-to-date")
        else:
            await message_or_cb.reply(text, reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex("^songs_refresh$"))
    async def songs_refresh_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)
        await send_songs_menu(client, callback_query)

    @bot.on_callback_query(filters.regex("^add_song_prompt$"))
    async def add_song_prompt_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        user_state[callback_query.from_user.id] = {"action": "ask_song_file"}
        await callback_query.message.edit_text(
            "🎵 **Add Background Song**\n\n"
            "Please send or upload the **Audio** or **Video** document (MP3, M4A, WAV, MP4, etc.) for the background music:\n\n"
            "Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^replace_song_"))
    async def replace_song_prompt_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        song_id = callback_query.data.split("replace_song_")[-1]
        song = await db.get_song(song_id)
        if not song:
            return await callback_query.answer("❌ Song not found", show_alert=True)

        user_state[callback_query.from_user.id] = {"action": "ask_replace_song_file", "song_id": song_id}
        await callback_query.message.edit_text(
            f"🔄 **Replace Song: {song.get('title', 'Untitled')}**\n\n"
            "Please send or upload the new **Audio** or **Video** document (MP3, M4A, WAV, MP4, etc.) to replace this song:\n\n"
            "Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^del_song_"))
    async def del_song_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        song_id = callback_query.data.split("del_song_")[-1]
        song = await db.get_song(song_id)
        if song:
            file_path = song.get("file_path", "")
            if file_path and file_path.startswith("/static/"):
                full_path = os.path.join(".", file_path.lstrip("/"))
                if os.path.exists(full_path):
                    try: os.remove(full_path)
                    except: pass
            await db.delete_song(song_id)
            await callback_query.answer(f"🗑 Deleted '{song.get('title', 'Song')}'", show_alert=True)
        else:
            await callback_query.answer("❌ Song already deleted", show_alert=True)

        await send_songs_menu(client, callback_query)

    @bot.on_callback_query(filters.regex("^set_song_channel_prompt$"))
    async def set_song_channel_prompt_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        user_state[callback_query.from_user.id] = {"action": "ask_song_channel"}
        await callback_query.message.edit_text(
            "📢 **Configure Dedicated Song Channel**\n\n"
            "Please send the **Channel Username** or **Numeric Channel ID** (e.g., `@anizone_songs` or `-1001234567890`):\n\n"
            "Make sure the bot is an admin in the song channel!\n"
            "Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )
        await callback_query.answer()

    @bot.on_message(filters.command("manual"))
    async def manual_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id): return
        user_state[message.from_user.id] = {"action": "ask_manual_title"}
        await message.reply("📝 **Step 1: Title**\nSend Title for the custom page:")


    @bot.on_message(filters.command("save"))
    async def save_command_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Unauthorized.**")

        buttons = [
            [
                InlineKeyboardButton("📥 BACKUP", callback_data="backup_data", style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("📤 RESTORE", callback_data="restore_data", style=ButtonStyle.PRIMARY)
            ]
        ]
        await message.reply(
            "💾 **Database Management System**\n\n"
            "Choose an action to manage your data safely:",
            reply_markup=InlineKeyboardMarkup(buttons)
        )

    @bot.on_message(filters.command("logo") & filters.private)
    async def logo_command_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Admins only.")
        user_state[message.from_user.id] = {"action": "await_site_logo_url"}
        await message.reply(
            "🖼 **Website Logo Setup**\nSend a direct image URL (HTTPS recommended).\n"
            "Send `/cancel` to keep the current logo."
        )

    @bot.on_message(filters.command("export_boxes") & filters.private)
    async def export_boxes_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Admins only.")
        term = " ".join(message.command[1:]).strip()
        if not term:
            user_state[message.from_user.id] = {"action": "export_boxes_await_name"}
            return await message.reply("📦 Send the box name or text to find (example: `Telugu`).")
        await show_export_box_categories(message, term)

    async def show_export_box_categories(message, term):
        categories = await db.get_all_categories()
        if not categories:
            return await message.reply("❌ No categories found. Add categories first with /categories.")
        uid = message.from_user.id
        user_state[uid] = {"action": "export_boxes_choose_category", "term": term}
        buttons = [[InlineKeyboardButton(str(c['name'])[:50], callback_data=f"exportboxcat:{uid}:{i}", style=ButtonStyle.PRIMARY)] for i, c in enumerate(categories)]
        user_state[uid]["categories"] = [str(c['name']) for c in categories]
        buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)])
        await message.reply(f"🔎 Search all pages for boxes containing **{term}**.\n\nChoose the destination category:", reply_markup=InlineKeyboardMarkup(buttons))

    @bot.on_callback_query(filters.regex(r"^exportboxcat:"))
    async def export_boxes_category_callback(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid, {})
        if not await is_authorized(uid) or state.get("action") != "export_boxes_choose_category":
            return await callback_query.answer("This migration menu expired.", show_alert=True)
        try:
            _, owner, idx = callback_query.data.split(":")
            if int(owner) != uid: raise ValueError("owner")
            category = state["categories"][int(idx)]
            term = state["term"]
            await callback_query.answer("Migration started")
            status = await callback_query.message.edit_text("⏳ Scanning pages and preparing a verified migration...")
            cursor = db.anime.find({"custom_boxes": {"$exists": True, "$ne": []}})
            docs = await cursor.to_list(length=10000)
            moved_pages = moved_boxes = 0
            needle = unicodedata.normalize("NFC", term).casefold()
            for source in docs:
                boxes = source.get("custom_boxes") or []
                matched, remaining = [], []
                for box in boxes:
                    try:
                        haystack = unicodedata.normalize("NFC", json.dumps(box, ensure_ascii=False, default=str)).casefold()
                    except Exception:
                        haystack = unicodedata.normalize("NFC", str(box)).casefold()
                    (matched if needle in haystack else remaining).append(box)
                if not matched: continue
                # Preserve the complete page record and every matched box field; only
                # the destination category and unique slug are changed on the new page.
                clone = dict(source)
                clone.pop("_id", None)
                clone["category"] = category
                base_slug = str(source.get("slug") or slugify(source.get("title", "page")))
                clone["slug"] = f"{base_slug}-export-{slugify(category)}-{uuid.uuid4().hex[:6]}"
                clone["custom_boxes"] = matched
                clone["exported_from"] = str(source.get("_id"))
                clone["exported_box_query"] = term
                try:
                    result = await db.anime.insert_one(clone)
                    verify = await db.anime.find_one({"_id": result.inserted_id})
                    if not verify or verify.get("custom_boxes") != matched:
                        if result.inserted_id: await db.anime.delete_one({"_id": result.inserted_id})
                        continue
                    # Delete from source only after the destination is confirmed.
                    update = await db.anime.update_one({"_id": source["_id"], "custom_boxes": boxes}, {"$set": {"custom_boxes": remaining}, "$currentDate": {"updated_at": True}})
                    if update.modified_count == 1:
                        moved_pages += 1
                        moved_boxes += len(matched)
                    else:
                        # Concurrent source edit: roll back destination to avoid duplicates.
                        await db.anime.delete_one({"_id": result.inserted_id})
                except Exception:
                    logger.exception("export_boxes migration failed for source %s", source.get("_id"))
            user_state.pop(uid, None)
            await status.edit_text(f"✅ **Box migration finished**\n\n🔎 Search: `{term}`\n📂 Destination: **{category}**\n📄 Pages migrated: **{moved_pages}**\n📦 Boxes moved: **{moved_boxes}**\n\nEach destination page was verified before its source boxes were removed.")
        except Exception as exc:
            logger.exception("export_boxes callback error")
            await callback_query.answer(f"Migration failed: {str(exc)[:150]}", show_alert=True)

    @bot.on_message(filters.command("delete_boxes") & filters.private)
    async def delete_boxes_handler(client, message):
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Admins only.")
        term = " ".join(message.command[1:]).strip()
        if not term:
            user_state[message.from_user.id] = {"action": "delete_boxes_await_name"}
            return await message.reply(
                "🗑 Send the box name or text to find and delete.\n\n"
                "Send `all` to delete **only custom text boxes** from every page. Categories, anime pages, posters, episodes, and other records will remain untouched."
            )
        await show_delete_boxes_confirm(message, term)

    async def show_delete_boxes_confirm(message, term):
        uid = message.from_user.id
        term = term.strip()
        wipe_all = term.casefold() == "all"
        status = await message.reply("⏳ Scanning pages for matching boxes...")
        try:
            cursor = db.anime.find({"custom_boxes": {"$exists": True, "$ne": []}})
            docs = await cursor.to_list(length=10000)
        except Exception as exc:
            user_state.pop(uid, None)
            return await status.edit_text(f"❌ Database error while scanning: {str(exc)[:150]}")

        needle = unicodedata.normalize("NFC", term).casefold()
        affected_pages = affected_boxes = 0
        for doc in docs:
            boxes = doc.get("custom_boxes") or []
            if wipe_all:
                matched = boxes
            else:
                matched = []
                for box in boxes:
                    try:
                        haystack = unicodedata.normalize("NFC", json.dumps(box, ensure_ascii=False, default=str)).casefold()
                    except Exception:
                        haystack = unicodedata.normalize("NFC", str(box)).casefold()
                    if needle in haystack:
                        matched.append(box)
            if matched:
                affected_pages += 1
                affected_boxes += len(matched)

        if affected_boxes == 0:
            user_state.pop(uid, None)
            msg = "❌ No custom boxes exist anywhere." if wipe_all else f"❌ No boxes found matching `{term}`."
            return await status.edit_text(msg)

        user_state[uid] = {"action": "delete_boxes_confirm", "term": term, "wipe_all": wipe_all}
        warn = "⚠️ **This will delete ONLY custom text boxes on EVERY page. Categories and all other page/database records will remain untouched.**" if wipe_all else f"🔎 Search: `{term}`"
        buttons = [
            [InlineKeyboardButton("🧨 CONFIRM DELETE", callback_data="execute_delboxes", style=ButtonStyle.DANGER)],
            [InlineKeyboardButton("🛡 Abort", callback_data="cancel_op", style=ButtonStyle.DANGER)],
        ]
        await status.edit_text(
            f"{warn}\n\n📄 Pages affected: **{affected_pages}**\n📦 Boxes to delete: **{affected_boxes}**\n\n"
            "This cannot be undone.",
            reply_markup=InlineKeyboardMarkup(buttons),
        )

    @bot.on_callback_query(filters.regex("^execute_delboxes$"))
    async def execute_delboxes_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state or state.get("action") != "delete_boxes_confirm" or not await is_authorized(uid):
            return await callback_query.answer("This confirmation expired.", show_alert=True)
        term = state["term"]
        wipe_all = state.get("wipe_all", False)
        await callback_query.answer("Deleting...")
        status_msg = callback_query.message
        await status_msg.edit_text("⏳ Deleting matched boxes...", reply_markup=None)
        try:
            cursor = db.anime.find({"custom_boxes": {"$exists": True, "$ne": []}})
            docs = await cursor.to_list(length=10000)
        except Exception as exc:
            user_state.pop(uid, None)
            return await status_msg.edit_text(f"❌ Database error: {str(exc)[:150]}")

        needle = term.casefold()
        deleted_pages = deleted_boxes = 0
        for doc in docs:
            boxes = doc.get("custom_boxes") or []
            if wipe_all:
                matched, remaining = boxes, []
            else:
                matched, remaining = [], []
                for box in boxes:
                    try:
                        haystack = json.dumps(box, ensure_ascii=False, default=str).casefold()
                    except Exception:
                        haystack = unicodedata.normalize("NFC", str(box)).casefold()
                    (matched if needle in haystack else remaining).append(box)
            if not matched:
                continue
            try:
                update = await db.anime.update_one(
                    {"_id": doc["_id"], "custom_boxes": boxes},
                    {"$set": {"custom_boxes": remaining}, "$currentDate": {"updated_at": True}},
                )
                if update.modified_count == 1:
                    deleted_pages += 1
                    deleted_boxes += len(matched)
            except Exception:
                logger.exception("delete_boxes execution failed for %s", doc.get("_id"))

        user_state.pop(uid, None)
        label = "all custom text boxes (only the custom_boxes fields)" if wipe_all else f"boxes matching `{term}`"
        await status_msg.edit_text(
            f"✅ **Boxes deleted**\n\n🗑 Removed {label}\n📄 Pages affected: **{deleted_pages}**\n📦 Boxes deleted: **{deleted_boxes}**"
        )

    @bot.on_message(filters.command("duplicate") & filters.private)
    async def duplicate_cleanup_handler(client, message):
        """Remove duplicate anime pages and duplicate custom boxes/groups/buttons site-wide."""
        if not message.from_user or not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** Admins only.")

        status = await message.reply("🔎 Scanning every category and website page for duplicate anime pages, boxes, groups and button URLs...")
        try:
            cursor = db.anime.find({"_created_by_create": {"$ne": True}}).sort("_id", 1)
            docs = await cursor.to_list(length=None)
        except Exception as exc:
            logger.exception("/duplicate scan failed")
            return await status.edit_text(f"❌ Database scan failed: {str(exc)[:180]}")

        def norm(value):
            value = unicodedata.normalize("NFKC", str(value or "")).strip().casefold()
            value = re.sub(r"[\W_]+", " ", value, flags=re.UNICODE)
            return re.sub(r"\s+", " ", value).strip()

        def page_key(doc):
            # Exact normalized display-title matching deliberately ignores category,
            # language tags and slug suffixes (e.g. '-export-telugu-997c11').
            title = norm(doc.get("title") or doc.get("name"))
            if title:
                return ("title", title)
            mal_id = str(doc.get("mal_id") or "").strip()
            return ("mal_id", mal_id) if mal_id else None

        def canonical_score(doc):
            slug = norm(doc.get("slug"))
            # Prefer the plain/canonical page over language/export copies; tie-break
            # deterministically so rerunning /duplicate never changes the survivor.
            penalty = 0
            for marker in (" export ", " telugu ", " tamil ", " hindi ", " multi audio "):
                if marker in f" {slug} ":
                    penalty += 1
            return (penalty, len(str(doc.get("slug") or "")), str(doc.get("slug") or "").casefold(), str(doc.get("_id")))

        # Identify duplicate catalogue pages globally, regardless of category.
        page_groups = {}
        for doc in docs:
            key = page_key(doc)
            if key:
                page_groups.setdefault(key, []).append(doc)

        duplicate_page_ids = set()
        duplicate_page_slugs = []
        for _key, group in page_groups.items():
            if len(group) < 2:
                continue
            survivor = min(group, key=canonical_score)
            for duplicate in group:
                if duplicate.get("_id") == survivor.get("_id"):
                    continue
                duplicate_page_ids.add(duplicate.get("_id"))
                duplicate_page_slugs.append(str(duplicate.get("slug") or duplicate.get("_id")))

        pages_deleted = 0
        # Delete duplicate anime records directly. Do not cascade-delete episodes:
        # duplicates can share mal_id with the surviving canonical page.
        for duplicate_id in duplicate_page_ids:
            try:
                result = await db.anime.delete_one({"_id": duplicate_id})
                if result.deleted_count:
                    pages_deleted += 1
            except Exception:
                logger.exception("/duplicate page deletion failed for %s", duplicate_id)

        # Re-scan survivors after page deletion. All page-level box/group/button
        # uniqueness sets are shared across categories and pages.
        try:
            cursor = db.anime.find({"custom_boxes": {"$exists": True, "$ne": []}}).sort("_id", 1)
            docs = await cursor.to_list(length=None)
        except Exception as exc:
            logger.exception("/duplicate custom-box scan failed")
            return await status.edit_text(f"⚠️ Deleted {pages_deleted} duplicate pages, but custom-box scan failed: {str(exc)[:150]}")

        seen_box_names, seen_group_names, seen_button_urls = set(), set(), set()
        pages_changed = boxes_removed = groups_removed = buttons_removed = 0

        for doc in docs:
            original = doc.get("custom_boxes") or []
            remaining_boxes = []
            changed = False
            for box in original:
                if not isinstance(box, dict):
                    remaining_boxes.append(box)
                    continue

                box = copy.deepcopy(box)
                box_key = norm(box.get("name"))
                if box_key and box_key in seen_box_names:
                    boxes_removed += 1
                    changed = True
                    continue
                if box_key:
                    seen_box_names.add(box_key)

                groups = box.get("groups")
                if isinstance(groups, dict):
                    remaining_groups = {}
                    for group_name, group_buttons in groups.items():
                        group_key = norm(group_name)
                        if group_key and group_key in seen_group_names:
                            groups_removed += 1
                            changed = True
                            continue
                        if group_key:
                            seen_group_names.add(group_key)

                        if not isinstance(group_buttons, dict):
                            remaining_groups[group_name] = group_buttons
                            continue

                        remaining_buttons = {}
                        for label, url in group_buttons.items():
                            url_key = norm(url)
                            if url_key and url_key in seen_button_urls:
                                buttons_removed += 1
                                changed = True
                                continue
                            if url_key:
                                seen_button_urls.add(url_key)
                            remaining_buttons[label] = url
                        remaining_groups[group_name] = remaining_buttons
                    box["groups"] = remaining_groups

                remaining_boxes.append(box)

            if changed:
                try:
                    result = await db.anime.update_one(
                        {"_id": doc["_id"], "custom_boxes": original},
                        {"$set": {"custom_boxes": remaining_boxes}, "$currentDate": {"updated_at": True}},
                    )
                    if result.modified_count:
                        pages_changed += 1
                except Exception:
                    logger.exception("/duplicate update failed for page %s", doc.get("_id"))

        await status.edit_text(
            "✅ **Website-wide duplicate cleanup completed**\n\n"
            f"📄 Duplicate anime pages deleted (all categories): **{pages_deleted}**\n"
            f"📦 Duplicate boxes removed: **{boxes_removed}**\n"
            f"🗂 Duplicate groups removed: **{groups_removed}**\n"
            f"🔘 Duplicate button URLs removed: **{buttons_removed}**\n"
            f"🛠 Pages with box changes: **{pages_changed}**\n\n"
            "Duplicate pages were matched by normalized title across every category. "
            "The canonical page was kept; duplicate pages were deleted. The first occurrence "
            "of each remaining box, group and button URL was kept globally."
        )

    @bot.on_message(filters.command("category_page"))
    async def category_page_handler(client, message):
        if not message.from_user: return
        if not await is_authorized(message.from_user.id):
            return await message.reply("🚫 **Access Denied.** This zone is for authorized administrators only.")

        query = " ".join(message.command[1:]).strip()
        if not query and message.reply_to_message:
            query = (message.reply_to_message.text or message.reply_to_message.caption or "").strip()

        slug = extract_slug(query)
        if not slug:
            return await message.reply("💡 **Usage:** `/category_page <url>` (or reply to a link)")

        try:
            anime = await db.get_anime(slug)
            if not anime:
                results = await db.search_anime_db(query)
                if results: anime = results[0]

            if not anime: return await message.reply(f"❌ **Not Found:** `{slug}`")
            aid = str(anime["_id"])
            current_cat = anime.get("category", "N/A")

            cats = await db.get_all_categories()
            buttons = []
            for c in cats:
                name = c['name']
                label = f"✅ {name}" if name == current_cat else name
                buttons.append([InlineKeyboardButton(label, callback_data=f"move_cat_{aid}:::{name}", style=ButtonStyle.PRIMARY)])

            buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)])
            await message.reply(
                f"📂 **Move Category: {anime['title']}**\n\n"
                f"Current Category: `{current_cat}`\n\n"
                "Select the destination category to move this page:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except Exception as e:
            logger.error(f"Category Page Cmd Error: {e}")
            await message.reply("❌ **Intelligence Feed Offline.**")

    @bot.on_message(filters.command("cancel"))
    async def cancel_handler(client, message):
        if message.from_user:
            user_state.pop(message.from_user.id, None)
        await message.reply("✨ **Action Cancelled.** standby.")

    # --- CALLBACK HANDLERS ---

    @bot.on_callback_query(filters.regex("^move_cat_"))
    async def move_cat_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        data = callback_query.data.split("move_cat_")[-1]
        aid, new_cat = data.split(":::")

        try:
            if await db.ping():
                anime = await db.get_anime(aid)
                if not anime: return await callback_query.answer("❌ Not Found", show_alert=True)
                res = await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"category": new_cat}, "$currentDate": {"updated_at": True}})
                if res.modified_count:
                    await callback_query.answer(f"🚀 Moved to {new_cat}", show_alert=True)
                    await callback_query.message.edit_text(
                        f"✅ **Category Updated Successfully!**\n\n"
                        f"🎬 **Anime:** `{anime['title']}`\n"
                        f"📂 **Moved To:** `{new_cat}`",
                        reply_markup=None
                    )
                else:
                    await callback_query.answer("⚠️ Category remains unchanged.", show_alert=True)
                    await callback_query.message.edit_text(
                        f"⚠️ **Category Unchanged**\n\n"
                        f"🎬 **Anime:** `{anime['title']}`\n"
                        f"📂 **Category:** `{new_cat}`",
                        reply_markup=None
                    )
            else:
                await callback_query.answer("❌ Database Offline", show_alert=True)
        except Exception as e:
            logger.error(f"Move Cat Callback Error: {e}")
            await callback_query.answer(f"❌ Error: {e}", show_alert=True)

    @bot.on_callback_query(filters.regex("^backup_data$"))
    async def backup_data_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        await callback_query.message.edit_text("⏳ **Generating system backup...**")
        await callback_query.answer()
        try:
            data = await db.export_data()
            if data is None:
                return await callback_query.message.edit_text("❌ **Export Failed:** Database connection offline.")

            if not data or not any(data.values()):
                return await callback_query.message.edit_text("❌ **Export Failed:** No records found in the database.")

            # Create a ZIP in memory
            zip_buffer = BytesIO()
            with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
                for coll_name, docs in data.items():
                    if docs:
                        json_data = json.dumps(docs, indent=4, default=str)
                        zf.writestr(f"{coll_name}.json", json_data)

            zip_buffer.seek(0)

            # Using a temporary file to ensure Pyrogram handles it correctly
            with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
                tmp.write(zip_buffer.getvalue())
                tmp_path = tmp.name

            try:
                await callback_query.message.delete()
                await client.send_document(
                    chat_id=callback_query.message.chat.id,
                    document=tmp_path,
                    file_name=f"backup_{Config.DB_NAME}.zip",
                    caption=(
                        f"✅ **Backup Generated Successfully**\n\n"
                        f"🌐 **Website:** {Config.BASE_URL}\n"
                        f"📦 **Collections:** {', '.join(data.keys())}\n\n"
                        f"Keep this file secure. You can restore this data anytime using the /save command."
                    )
                )
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

        except Exception as e:
            logger.error(f"Backup Error: {e}")
            await callback_query.message.edit_text(f"❌ **Backup Failed:** `{str(e)}`")

    @bot.on_callback_query(filters.regex("^restore_data$"))
    async def restore_data_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        user_state[callback_query.from_user.id] = {"action": "awaiting_restore_zip"}
        await callback_query.message.edit_text(
            "📤 **Ready to Restore Data**\n\n"
            "Please upload the `backup.zip` file you created earlier.\n\n"
            "⚠️ **Important:** This operation will overwrite all existing records with the data from the ZIP file.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )

    @bot.on_callback_query(filters.regex("^help_guide$"))
    async def help_cb(client, callback_query):
        uid = callback_query.from_user.id if callback_query.from_user else 0
        await send_admin_guide_page(client, callback_query, page_idx=0, user_id=uid)

    @bot.on_callback_query(filters.regex("^guide_nav_"))
    async def guide_nav_cb(client, callback_query):
        uid = callback_query.from_user.id if callback_query.from_user else 0
        try:
            page_idx = int(callback_query.data.split("guide_nav_")[-1])
        except Exception:
            page_idx = 0
        await send_admin_guide_page(client, callback_query, page_idx=page_idx, user_id=uid)

    @bot.on_callback_query(filters.regex("^guide_noop$"))
    async def guide_noop_cb(client, callback_query):
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^categories_refresh$"))
    async def categories_refresh_cb(client, callback_query):
        uid = getattr(getattr(callback_query, "from_user", None), "id", None)
        if not await is_authorized(uid):
            logger.warning("Blocked categories_refresh callback from Telegram user_id=%r", uid)
            return await callback_query.answer("🚫 Access Denied. Admins only.", show_alert=True)
        try:
            cats = await db.get_all_categories()
            buttons = [[InlineKeyboardButton(f"🏷 {c['name']}", callback_data=f"vcat_{c['name']}", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🗑", callback_data=f"del_cat_{c['name']}", style=ButtonStyle.DANGER)] for c in cats]
            buttons.append([InlineKeyboardButton("➕ Add Category", callback_data="add_cat_prompt", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="categories_refresh", style=ButtonStyle.SUCCESS)])
            await callback_query.message.edit_text("📂 **Category Management**", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer("Categories Synchronized")
        except MessageNotModified:
            await callback_query.answer("Already Up-to-date")
        except Exception as e:
            logger.error(f"Categories Refresh Error: {e}")
            await callback_query.answer("Sync Failed")

    @bot.on_callback_query(filters.regex("^add_cat_prompt$"))
    async def add_cat_prompt_cb(client, callback_query):
        uid = getattr(getattr(callback_query, "from_user", None), "id", None)
        if not await is_authorized(uid):
            logger.warning("Blocked add_cat_prompt callback from Telegram user_id=%r", uid)
            return await callback_query.answer("🚫 Access Denied. Admins only.", show_alert=True)
        user_state[callback_query.from_user.id] = {"action": "ask_cat_name"}
        await callback_query.message.edit_text("🏷 **Enter Category Name:**", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^del_cat_"))
    async def del_cat_cb(client, callback_query):
        uid = getattr(getattr(callback_query, "from_user", None), "id", None)
        if not await is_authorized(uid):
            logger.warning("Blocked del_cat callback from Telegram user_id=%r data=%r", uid, callback_query.data)
            return await callback_query.answer("🚫 Access Denied. Admins only.", show_alert=True)
        try:
            name = callback_query.data.split("del_cat_")[-1]
            await db.delete_category(name)
            cats = await db.get_all_categories()
            buttons = [
                [
                    InlineKeyboardButton(f"🏷 {c['name']}", callback_data=f"vcat_{c['name']}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton("🗑", callback_data=f"del_cat_{c['name']}", style=ButtonStyle.DANGER),
                ]
                for c in cats
            ]
            buttons.append([InlineKeyboardButton("➕ Add Category", callback_data="add_cat_prompt", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="categories_refresh", style=ButtonStyle.SUCCESS)])
            try:
                await callback_query.message.edit_text("📂 **Category Management**", reply_markup=InlineKeyboardMarkup(buttons))
            except MessageNotModified:
                # Telegram raises 400 when text and keyboard are already identical.
                # The category deletion itself has succeeded; this is not an error.
                pass
            await callback_query.answer(f"🗑 Removed: {name}", show_alert=True)
        except Exception as e:
            logger.exception("Del Cat CB Error")
            await callback_query.answer("❌ Could not delete category. Check database connection.", show_alert=True)

    @bot.on_callback_query(filters.regex("^add_group_yes_"))
    async def add_group_yes_cb(client, callback_query):
        aid = callback_query.data.split("add_group_yes_")[-1]
        user_state[callback_query.from_user.id] = {"action": "ask_edit_group_name", "slug": aid}
        await callback_query.message.edit_text("📝 **Group Identity:**\n*(e.g. Season 2, OVA, Movie)*", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^trigger_poster_"))
    async def trigger_poster_cb(client, callback_query):
        aid = callback_query.data.split("trigger_poster_")[-1]
        user_state[callback_query.from_user.id] = {"action": "ask_new_poster", "slug": aid}
        await callback_query.message.edit_text("🖼 **New Asset URL:**", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^confirm_purge_"))
    async def confirm_purge_cb(client, callback_query):
        aid = callback_query.data.split("confirm_purge_")[-1]
        buttons = [[InlineKeyboardButton("🧨 PURGE EVERYTHING", callback_data=f"execute_purge_{aid}", style=ButtonStyle.DANGER)],[InlineKeyboardButton("🛡 Abort", callback_data="cancel_op", style=ButtonStyle.DANGER)]]
        await callback_query.message.edit_text(f"⚠️ **CRITICAL:** Purge `{aid}`?", reply_markup=InlineKeyboardMarkup(buttons))
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^execute_purge_"))
    async def execute_purge_cb(client, callback_query):
        aid = callback_query.data.split("execute_purge_")[-1]
        try:
            anime = await db.get_anime(aid)
            if anime:
                await db.delete_anime_by_slug(anime["slug"])
                await callback_query.message.edit_text(f"🔥 **Sanitized:** `{anime['slug']}` purged.", reply_markup=None)
            else:
                await callback_query.answer("❌ Already Deleted", show_alert=True)
        except Exception as e: await callback_query.answer(f"Purge Failed: {e}", show_alert=True)

    @bot.on_callback_query(filters.regex("^addsched_"))
    async def addsched_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state or state.get("action") != "add_sched_entry":
            return await callback_query.answer("❌ Session Expired", show_alert=True)

        day = callback_query.data.split("addsched_")[-1]
        entry = state["entry"]

        try:
            current = await db.get_schedule(day)
            if not isinstance(current, list):
                current = []

            current.append(entry)
            await db.update_schedule(day, current)

            await callback_query.message.edit_text(
                f"✅ **Entry Added to {day}!**\n\n"
                f"🕒 {entry['time']} - {entry['name']}"
            )
            del user_state[uid]
        except Exception as e:
            logger.error(f"Add Sched CB Error: {e}")
            await callback_query.answer("Failed to update schedule.")

    @bot.on_callback_query(filters.regex("^edit_sched_"))
    async def edit_sched_cb(client, callback_query):
        day = callback_query.data.split("edit_sched_")[-1]
        user_state[callback_query.from_user.id] = {"action": "ask_sched_content", "day": day}
        current = await db.get_schedule(day)

        display_text = ""
        if isinstance(current, list):
            for item in current:
                display_text += f"• {item.get('name')} ({item.get('time')})\n"
        else:
            display_text = str(current)

        await callback_query.message.edit_text(
            f"📅 **Manual Override: {day}**\n\n"
            f"Current:\n`{display_text or 'Empty'}`\n\n"
            "⚠️ **Note:** Sending text here will convert the schedule to a simple list and remove images.\n"
            "Send NEW schedule text or `/skip` to cancel.",
            reply_markup=None
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^manage_groups_"))
    async def manage_groups_cb(client, callback_query):
        try:
            aid = callback_query.data.split("manage_groups_")[-1]
            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Not Found", show_alert=True)

            groups = anime.get("seasons_links", {})
            group_list = list(groups.keys())
            user_state[callback_query.from_user.id] = {"slug": aid, "group_names": group_list}

            buttons = [[InlineKeyboardButton("➕ Add New Group", callback_data=f"add_cgrp_start_{aid}", style=ButtonStyle.SUCCESS)]]
            for idx, gname in enumerate(group_list):
                display_name = (gname[:12] + '..') if len(gname) > 14 else gname
                row = [
                    InlineKeyboardButton(f"⚙️ {display_name}", callback_data=f"selgidx_{idx}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton(f"🗑", callback_data=f"remgidx_{idx}", style=ButtonStyle.DANGER)
                ]
                if idx > 0: row.append(InlineKeyboardButton("⬆️", callback_data=f"mvupg_{idx}", style=ButtonStyle.PRIMARY))
                if idx < len(group_list) - 1: row.append(InlineKeyboardButton("⬇️", callback_data=f"mvdng_{idx}", style=ButtonStyle.PRIMARY))
                buttons.append(row)

            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"manage_groups_{aid}", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.PRIMARY)])
            await callback_query.message.edit_text(f"📝 **Groups: {anime['title']}**\nSelect a group to manage buttons or reorder:", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Manage Groups Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^selgidx_|^select_group_refresh_"))
    async def select_group_cb(client, callback_query):
        try:
            idx = int(callback_query.data.split("_")[-1])
            state = user_state.get(callback_query.from_user.id)
            if not state: return await callback_query.answer("❌ Session Expired")

            gname = state["group_names"][idx]
            aid = state["slug"]
            anime = await db.get_anime(aid)
            group_data = anime.get("seasons_links", {}).get(gname, {})

            user_state[callback_query.from_user.id].update({"group_name": gname, "btn_labels": list(group_data.keys())})

            buttons = [
                [InlineKeyboardButton("➕ Add Button", callback_data=f"add_gbtn_start_{idx}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("🏷 Rename Group", callback_data=f"rengidx_{idx}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🗑 Delete Group", callback_data=f"remgidx_{idx}", style=ButtonStyle.DANGER)]
            ]

            for b_idx, label in enumerate(group_data.keys()):
                row = [
                    InlineKeyboardButton(f"✏️ {label}", callback_data=f"egbtn_{idx}_{b_idx}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton(f"🗑", callback_data=f"rgbtn_{idx}_{b_idx}", style=ButtonStyle.DANGER)
                ]
                if b_idx > 0: row.append(InlineKeyboardButton("⬆️", callback_data=f"mvupgb_{idx}_{b_idx}", style=ButtonStyle.PRIMARY))
                if b_idx < len(group_data) - 1: row.append(InlineKeyboardButton("⬇️", callback_data=f"mvdngb_{idx}_{b_idx}", style=ButtonStyle.PRIMARY))
                buttons.append(row)

            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"select_group_refresh_{idx}", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"manage_groups_{aid}", style=ButtonStyle.PRIMARY)])
            await callback_query.message.edit_text(f"📦 **Group: {gname}**\nManage internal buttons:", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Select Group Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^manage_btns_"))
    async def manage_btns_cb(client, callback_query):
        try:
            aid = callback_query.data.split("manage_btns_")[-1]
            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Not Found", show_alert=True)

            btns = anime.get("custom_buttons", [])
            user_state[callback_query.from_user.id] = {"slug": aid, "btns": btns}

            buttons = [[InlineKeyboardButton("➕ Add New Button", callback_data=f"add_btn_start_{aid}", style=ButtonStyle.SUCCESS)]]
            for idx, btn in enumerate(btns):
                display_name = (btn['name'][:12] + '..') if len(btn['name']) > 14 else btn['name']
                row = [
                    InlineKeyboardButton(f"✏️ {display_name}", callback_data=f"rebidx_{idx}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton(f"🗑", callback_data=f"rembidx_{idx}", style=ButtonStyle.DANGER)
                ]
                if idx > 0: row.append(InlineKeyboardButton("⬆️", callback_data=f"mvupb_{idx}", style=ButtonStyle.PRIMARY))
                if idx < len(btns) - 1: row.append(InlineKeyboardButton("⬇️", callback_data=f"mvdnb_{idx}", style=ButtonStyle.PRIMARY))
                buttons.append(row)

            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"manage_btns_{aid}", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.PRIMARY)])
            await callback_query.message.edit_text(f"🖇 **External Redirects: {anime['title']}**\nManage top-level buttons:", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Manage Btns Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^edit_m_back_"))
    async def edit_m_back_cb(client, callback_query):
        try:
            aid = callback_query.data.split("edit_m_back_")[-1]
            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Not Found")

            buttons = [
                [InlineKeyboardButton("🔊 Languages", callback_data=f"manage_langs_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("📦 Add Custom Group", callback_data=f"add_cgrp_start_{aid}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("➕ Add Custom Button", callback_data=f"add_btn_start_{aid}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("🗃 Add Custom Box", callback_data=f"add_box_start_{aid}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("🚀 Advanced Group", callback_data=f"adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🔥 Ultra Advanced Group", callback_data=f"ultra_adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("📋 Manage Boxes", callback_data=f"manage_boxes_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("📝 Manage Buttons", callback_data=f"manage_btns_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"edit_m_back_{aid}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("🛡 Back to Archive", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.PRIMARY), InlineKeyboardButton("❌ Abort", callback_data="cancel_op", style=ButtonStyle.DANGER)]
            ]
            await callback_query.message.edit_text(f"🖇 **Custom Management: {anime['title']}**", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Edit M Back Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^manage_langs_"))
    async def manage_langs_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        aid = callback_query.data.split("manage_langs_")[-1]
        anime = await db.get_anime(aid)
        if not anime:
            return await callback_query.answer("❌ Anime Not Found", show_alert=True)

        curr_langs = anime.get("languages", "Telugu • Tamil • Hindi • English • Japanese")
        user_state[callback_query.from_user.id] = {
            "action": "ask_anime_languages",
            "slug": aid
        }

        await callback_query.message.edit_text(
            f"🔊 **Audio Languages Management: {anime['title']}**\n\n"
            f"Current Configured Languages:\n`{curr_langs}`\n\n"
            "Please send the language names or codes separated by commas:\n\n"
            "Example:\n`tel,tam,hin,eng` or `Telugu, Tamil, Hindi, English, Japanese`\n\n"
            "Send /cancel to abort.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]])
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^add_cgrp_start_"))
    async def add_cgrp_start_cb(client, callback_query):
        aid = callback_query.data.split("add_cgrp_start_")[-1]
        user_state[callback_query.from_user.id] = {"action": "ask_cgrp_btn_count", "slug": aid}
        await callback_query.message.edit_text("How many buttons do you want to add?", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^add_box_start_"))
    async def add_box_start_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        aid = callback_query.data.split("add_box_start_")[-1]

        # Detect origin (edit_m vs edit)
        origin = "edit"
        curr_text = callback_query.message.text or ""
        if "Custom Management" in curr_text:
            origin = "edit_m"

        user_state[callback_query.from_user.id] = {
            "action": "ask_box_name",
            "slug": aid,
            "origin": origin
        }

        cancel_callback = f"cancel_bulkbox_{origin}:::{aid}"

        instruction_text = (
            "🗃 **Bulk Box Creation**\n\n"
            "You can add multiple boxes at once!\n"
            "Please send your box names in the following format:\n\n"
            "1. Naruto\n"
            "2. One Piece\n"
            "3. Bleach\n"
            "4. Jujutsu Kaisen\n"
            "5. Demon Slayer\n\n"
            "Send your list of box names now or click Cancel to abort:"
        )

        await callback_query.message.edit_text(
            instruction_text,
            reply_markup=InlineKeyboardMarkup([[
                InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback, style=ButtonStyle.DANGER)
            ]])
        )
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^cancel_bulkbox_(edit|edit_m):::"))
    async def cancel_bulkbox_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        data = callback_query.data.split("cancel_bulkbox_")[-1]
        origin, aid = data.split(":::", 1)

        uid = callback_query.from_user.id
        user_state.pop(uid, None)

        await callback_query.answer("Operation Cancelled")

        if origin == "edit_m":
            callback_query.data = f"edit_m_back_{aid}"
            return await edit_m_back_cb(client, callback_query)
        else:
            callback_query.data = f"back_to_edit_{aid}"
            return await back_to_edit_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^ultra_adv_grp_start_"))
    async def ultra_adv_grp_start_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        try:
            aid = callback_query.data.split("ultra_adv_grp_start_")[-1]
            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Anime Not Found", show_alert=True)

            uid = callback_query.from_user.id

            # Record the origin callback (whether it came from edit or edit_m)
            # We can detect this based on the current message text/caption or state.
            # Usually `/edit_m` has "Custom Management" in its title, and `/edit` has "Executive Suite" or similar.
            origin = "edit"
            curr_text = callback_query.message.text or ""
            if "Custom Management" in curr_text:
                origin = "edit_m"

            user_state[uid] = {
                "action": "ask_ultra_adv_grp_message",
                "slug": aid,
                "origin": origin
            }

            cancel_callback = f"cancel_uag_{origin}:::{aid}"

            instruction_text = (
                f"🔥 **Ultra Advanced Group: {anime['title']}**\n\n"
                "Please send the custom box groups and buttons in this format:\n\n"
                "`I. BOX NAME: Naruto` (Roman-numbered existing box)\n\n"
                "`1. Quality` (Serial number + Group Name)\n"
                "`480P : https://example.com/480` (Button Name : Link)\n"
                "`720P : https://example.com/720`\n\n"
                "`2. Languages`\n"
                "`Telugu : https://example.com/telugu`\n\n"
                "`II. BOX NAME: One Piece`\n\n"
                "`1. Quality`\n"
                "`480P : https://example.com/480`\n\n"
                "⚠️ **Rules & Guidelines:**\n"
                "• **Ultra Advanced Group must NEVER create a new box.** It only updates existing boxes.\n"
                "• Any boxes not found in this series will be marked as failed/not found, but other valid boxes will continue processing.\n"
                "• All groups and buttons will be appended to the existing box without overwriting other unrelated groups.\n"
                "• The parser splits only on the **first colon (:)** for button links to avoid corrupting URLs containing colons.\n\n"
                "Send your formatted input now or click Cancel to abort:"
            )

            await callback_query.message.edit_text(
                instruction_text,
                reply_markup=InlineKeyboardMarkup([[
                    InlineKeyboardButton("❌ Cancel", callback_data=cancel_callback, style=ButtonStyle.DANGER)
                ]])
            )
            await callback_query.answer()
        except Exception as e:
            logger.error(f"Ultra Advanced Group Start Error: {e}")
            await callback_query.answer("Error initiating Ultra Advanced Group flow", show_alert=True)

    @bot.on_callback_query(filters.regex("^cancel_uag_(edit|edit_m):::"))
    async def cancel_uag_cb(client, callback_query):
        if not await is_authorized(callback_query.from_user.id):
            return await callback_query.answer("🚫 Unauthorized", show_alert=True)

        data = callback_query.data.split("cancel_uag_")[-1]
        origin, aid = data.split(":::", 1)

        uid = callback_query.from_user.id
        user_state.pop(uid, None)

        await callback_query.answer("Operation Cancelled")

        if origin == "edit_m":
            # Simulate back to edit_m callback
            callback_query.data = f"edit_m_back_{aid}"
            return await edit_m_back_cb(client, callback_query)
        else:
            # Simulate back to edit callback
            callback_query.data = f"back_to_edit_{aid}"
            return await back_to_edit_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^adv_grp_start_"))
    async def adv_grp_start_cb(client, callback_query):
        try:
            aid = callback_query.data.split("adv_grp_start_")[-1]
            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Anime Not Found", show_alert=True)

            boxes = anime.get("custom_boxes", [])
            if not boxes:
                buttons = [
                    [InlineKeyboardButton("🗃 Create Custom Box", callback_data=f"add_box_start_{aid}", style=ButtonStyle.SUCCESS)],
                    [InlineKeyboardButton("🛡 Back", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.PRIMARY)]
                ]
                await callback_query.message.edit_text(
                    "⚠️ **No Custom Boxes Found!**\n\n"
                    "You need to create at least one Custom Box on this page before using the Advanced Group feature.",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
                return await callback_query.answer()

            buttons = []
            for idx, box in enumerate(boxes):
                buttons.append([InlineKeyboardButton(f"🗃 {box['name']}", callback_data=f"adv_box_sel_{idx}_{aid}", style=ButtonStyle.PRIMARY)])

            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.PRIMARY)])

            await callback_query.message.edit_text(
                f"🚀 **Advanced Group: {anime['title']}**\n\n"
                "Select the target Custom Box to add groups and buttons into:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            await callback_query.answer()
        except Exception as e:
            logger.error(f"Advanced Group Start Error: {e}")
            await callback_query.answer("Error initiating Advanced Group flow", show_alert=True)

    @bot.on_callback_query(filters.regex("^adv_box_sel_"))
    async def adv_box_sel_cb(client, callback_query):
        try:
            parts = callback_query.data.split("_")
            b_idx = int(parts[3])
            aid = parts[4]

            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Anime Not Found", show_alert=True)

            boxes = anime.get("custom_boxes", [])
            if b_idx >= len(boxes): return await callback_query.answer("❌ Box Not Found", show_alert=True)
            box_name = boxes[b_idx]["name"]

            uid = callback_query.from_user.id
            user_state[uid] = {
                "action": "ask_adv_grp_message",
                "slug": aid,
                "box_idx": b_idx
            }

            instruction_text = (
                f"🚀 **Advanced Group setup for: {anime['title']}**\n"
                f"Target Box: **{box_name}**\n\n"
                "Please send one or more groups in a single message following this format exactly:\n\n"
                "`1. Season 1` (A number, a dot, space, followed by the Group Name)\n\n"
                "`Button Name : Link` (Any number of buttons, one per line)\n"
                "`Button Name : Link`\n\n"
                "Example:\n"
                "```\n"
                "1. Season 1\n\n"
                "480P : https://example.com/480p\n"
                "720P : https://example.com/720p\n"
                "1080P : https://example.com/1080p\n\n"
                "2. Season 2\n\n"
                "480P : https://example.com/480p\n"
                "720P : https://example.com/720p\n"
                "1080P : https://example.com/1080p\n"
                "```\n\n"
                "⚠️ **Formatting Rules:**\n"
                "• Each numbered entry represents a new Group.\n"
                "• The first line after the number is the Group Name.\n"
                "• Every following line must use the format: `Button Name : Link`\n"
                "• Ignore empty lines automatically.\n"
                "• All buttons and URLs will be verified before saving.\n\n"
                "Send your message now or send /cancel to abort:"
            )

            await callback_query.message.edit_text(instruction_text, reply_markup=None)
            await callback_query.answer()
        except Exception as e:
            logger.error(f"Advanced Box Select Error: {e}")
            await callback_query.answer("Error setting up Advanced Box target", show_alert=True)

    @bot.on_callback_query(filters.regex("^manage_boxes_"))
    async def manage_boxes_cb(client, callback_query):
        try:
            aid = callback_query.data.split("manage_boxes_")[-1]
            if not aid or aid.startswith("remboxidx_") or aid.startswith("mvupbox_") or aid.startswith("mvdnbox_"):
                # If called internally after an action, retrieve from state
                state = user_state.get(callback_query.from_user.id)
                if state: aid = state["slug"]
                else: return await callback_query.answer("❌ Session Expired")

            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Not Found", show_alert=True)

            boxes = anime.get("custom_boxes", [])
            user_state[callback_query.from_user.id] = {"slug": aid, "boxes": boxes}

            buttons = [[InlineKeyboardButton("➕ Add New Box", callback_data=f"add_box_start_{aid}", style=ButtonStyle.SUCCESS)]]
            for idx, box in enumerate(boxes):
                display_name = (box['name'][:12] + '..') if len(box['name']) > 14 else box['name']
                row = [
                    InlineKeyboardButton(f"⚙️ {display_name}", callback_data=f"selboxidx_{idx}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton(f"🗑", callback_data=f"remboxidx_{idx}", style=ButtonStyle.DANGER)
                ]
                if idx > 0: row.append(InlineKeyboardButton("⬆️", callback_data=f"mvupbox_{idx}", style=ButtonStyle.PRIMARY))
                if idx < len(boxes) - 1: row.append(InlineKeyboardButton("⬇️", callback_data=f"mvdnbox_{idx}", style=ButtonStyle.PRIMARY))
                buttons.append(row)

            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"manage_boxes_{aid}", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"edit_m_back_{aid}", style=ButtonStyle.PRIMARY)])
            await callback_query.message.edit_text(f"📋 **Custom Boxes: {anime['title']}**", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Manage Boxes Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^selboxidx_|^select_box_refresh_"))
    async def select_box_cb(client, callback_query):
        try:
            data = callback_query.data.split("_")
            if len(data) > 1:
                idx = int(data[-1])
            else:
                # Fallback if called internally
                state = user_state.get(callback_query.from_user.id)
                if state and "box_idx" in state: idx = state["box_idx"]
                else: return await callback_query.answer("❌ Error")

            state = user_state.get(callback_query.from_user.id)
            if not state: return await callback_query.answer("❌ Session Expired")

            box = state["boxes"][idx]
            aid = state["slug"]

            user_state[callback_query.from_user.id].update({"box_idx": idx})

            buttons = [
                [InlineKeyboardButton("🏷 Rename Box", callback_data=f"renbox_{idx}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🔗 Edit Page Link", callback_data=f"edboxlink_{idx}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("📦 Add Group to Box", callback_data=f"box_add_grp_{idx}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("🗑 Delete Box", callback_data=f"remboxidx_{idx}", style=ButtonStyle.DANGER)]
            ]

            groups = box.get("groups", {})
            for g_idx, g_name in enumerate(groups.keys()):
                buttons.append([
                    InlineKeyboardButton(f"⚙️ {g_name}", callback_data=f"selboxg_{idx}_{g_idx}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton("🗑", callback_data=f"remboxg_{idx}_{g_idx}", style=ButtonStyle.DANGER)
                ])

            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"select_box_refresh_{idx}", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"manage_boxes_{aid}", style=ButtonStyle.PRIMARY)])
            await callback_query.message.edit_text(f"🗃 **Box: {box['name']}**\nManage box settings and groups:", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Select Box Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^box_add_grp_"))
    async def box_add_grp_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        aid = user_state[callback_query.from_user.id]["slug"]
        user_state[callback_query.from_user.id].update({"action": "ask_box_cgrp_name", "box_idx": idx})
        await callback_query.message.edit_text("📦 **Group Name for Box:**\n*(e.g. 1080p [Dual], Zip File)*", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^box_grp_yes$"))
    async def box_grp_yes_cb(client, callback_query):
        # Part of the box creation flow
        state = user_state.get(callback_query.from_user.id)
        if not state: return await callback_query.answer("❌ Session Expired")
        user_state[callback_query.from_user.id].update({"action": "ask_box_initial_grp_name"})
        await callback_query.message.edit_text("📦 **First Group Name:**", reply_markup=None)

    @bot.on_callback_query(filters.regex("^box_grp_no$"))
    async def box_grp_no_cb(client, callback_query):
        # Part of the box creation flow
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state: return await callback_query.answer("❌ Session Expired")

        aid = state["slug"]
        new_box = {
            "name": state["box_name"],
            "link": state["box_link"],
            "groups": {}
        }

        anime = await db.get_anime(aid)
        boxes = anime.get("custom_boxes", [])
        boxes.append(new_box)

        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
        await callback_query.message.edit_text(f"✅ **Box '{state['box_name']}' created successfully (Empty).**")
        del user_state[uid]

    @bot.on_callback_query(filters.regex("^remboxidx_"))
    async def remove_box_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state: return await callback_query.answer("❌ Session Expired")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        boxes = anime.get("custom_boxes", [])
        if idx < len(boxes):
            name = boxes[idx]['name']
            boxes.pop(idx)
            await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
            await callback_query.answer(f"🗑 Box '{name}' Removed", show_alert=True)
            return await manage_boxes_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^mvupbox_"))
    async def move_box_up_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or idx == 0: return await callback_query.answer("❌ Error")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        boxes = anime.get("custom_boxes", [])
        boxes[idx], boxes[idx-1] = boxes[idx-1], boxes[idx]
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
        await manage_boxes_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^mvdnbox_"))
    async def move_box_dn_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state: return await callback_query.answer("❌ Error")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        boxes = anime.get("custom_boxes", [])
        if idx >= len(boxes) - 1: return await callback_query.answer("❌ Error")

        boxes[idx], boxes[idx+1] = boxes[idx+1], boxes[idx]
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
        await manage_boxes_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^remboxg_"))
    async def remove_box_grp_cb(client, callback_query):
        parts = callback_query.data.split("_")
        if len(parts) < 3: return await callback_query.answer("❌ Error")
        b_idx, g_idx = int(parts[1]), int(parts[2])
        state = user_state.get(callback_query.from_user.id)
        if not state: return await callback_query.answer("❌ Session Expired")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        boxes = anime.get("custom_boxes", [])
        if b_idx < len(boxes):
            groups = boxes[b_idx]["groups"]
            if g_idx < len(groups):
                g_name = list(groups.keys())[g_idx]
                del groups[g_name]
                await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                await callback_query.answer(f"🗑 Group '{g_name}' Removed", show_alert=True)
                return await select_box_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^selboxg_|^select_box_grp_refresh_"))
    async def select_box_grp_cb(client, callback_query):
        try:
            if callback_query.data.startswith("select_box_grp_refresh_"):
                parts = callback_query.data.split("_refresh_", 1)[1].split("_")
                b_idx, g_idx = int(parts[0]), int(parts[1])
            else:
                parts = callback_query.data.split("_")
                if len(parts) >= 3:
                    b_idx, g_idx = int(parts[1]), int(parts[2])
                else:
                    state = user_state.get(callback_query.from_user.id)
                    if state and "box_idx" in state and "box_g_idx" in state:
                        b_idx, g_idx = state["box_idx"], state["box_g_idx"]
                    else: return await callback_query.answer("❌ Error")

            state = user_state.get(callback_query.from_user.id)
            if not state: return await callback_query.answer("❌ Session Expired")

            aid = state["slug"]
            anime = await db.get_anime(aid)
            box = anime.get("custom_boxes", [])[b_idx]
            g_name = list(box["groups"].keys())[g_idx]
            group_data = box["groups"][g_name]

            user_state[callback_query.from_user.id].update({"box_idx": b_idx, "box_g_idx": g_idx, "box_g_name": g_name})

            buttons = [
                [InlineKeyboardButton("➕ Add Button to Group", callback_data=f"box_g_add_btn_{b_idx}_{g_idx}", style=ButtonStyle.SUCCESS)],
                [InlineKeyboardButton("🏷 Rename Group", callback_data=f"renboxg_{b_idx}_{g_idx}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🗑 Delete Group", callback_data=f"remboxg_{b_idx}_{g_idx}", style=ButtonStyle.DANGER)]
            ]

            for btn_label in group_data.keys():
                buttons.append([
                InlineKeyboardButton(f"✏️ {btn_label}", callback_data=f"eboxgbtn_{b_idx}_{g_idx}_{btn_label}", style=ButtonStyle.PRIMARY),
                InlineKeyboardButton("🗑", callback_data=f"rboxgbtn_{b_idx}_{g_idx}_{btn_label}", style=ButtonStyle.DANGER)
                ])

            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"select_box_grp_refresh_{b_idx}_{g_idx}", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"selboxidx_{b_idx}", style=ButtonStyle.PRIMARY)])
            await callback_query.message.edit_text(f"📦 **Group: {g_name}** (Box: {box['name']})", reply_markup=InlineKeyboardMarkup(buttons))
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Select Box Group Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^box_g_add_btn_"))
    async def box_g_add_btn_cb(client, callback_query):
        parts = callback_query.data.split("_")
        b_idx, g_idx = int(parts[4]), int(parts[5])
        state = user_state.get(callback_query.from_user.id)
        anime = await db.get_anime(state["slug"])
        box = anime["custom_boxes"][b_idx]
        g_name = list(box["groups"].keys())[g_idx]
        user_state[callback_query.from_user.id].update({"action": "ask_box_g_btn_label", "box_idx": b_idx, "box_g_idx": g_idx, "box_g_name": g_name})
        await callback_query.message.edit_text("🏷 **Button Label:**", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^renbox_"))
    async def rename_box_cb_prompt(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        user_state[callback_query.from_user.id].update({"action": "ask_renbox_name", "box_idx": idx})
        await callback_query.message.edit_text("✏️ **New Box Name:**", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^edboxlink_"))
    async def edit_box_link_cb_prompt(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        user_state[callback_query.from_user.id].update({"action": "ask_edbox_link", "box_idx": idx})
        await callback_query.message.edit_text("🔗 **New Page Link for Box:**\n*(Send URL or /skip to remove)*", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^renboxg_"))
    async def rename_box_grp_cb_prompt(client, callback_query):
        parts = callback_query.data.split("_")
        b_idx, g_idx = int(parts[1]), int(parts[2])
        state = user_state.get(callback_query.from_user.id)
        anime = await db.get_anime(state["slug"])
        box = anime["custom_boxes"][b_idx]
        g_name = list(box["groups"].keys())[g_idx]
        user_state[callback_query.from_user.id].update({"action": "ask_renbox_g_name", "box_idx": b_idx, "box_g_idx": g_idx, "old_g_name": g_name})
        await callback_query.message.edit_text(f"✏️ **New Name for Group '{g_name}':**", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^eboxgbtn_"))
    async def edit_box_gbtn_cb_prompt(client, callback_query):
        parts = callback_query.data.split("_", 3)
        b_idx, g_idx, label = int(parts[1]), int(parts[2]), parts[3]
        state = user_state.get(callback_query.from_user.id)
        anime = await db.get_anime(state["slug"])
        box = anime["custom_boxes"][b_idx]
        g_name = list(box["groups"].keys())[g_idx]
        user_state[callback_query.from_user.id].update({"action": "ask_ebox_gbtn_label", "box_idx": b_idx, "box_g_idx": g_idx, "box_g_name": g_name, "old_label": label})
        await callback_query.message.edit_text(f"✏️ **New Label for '{label}':**\n*(or /skip)*", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^rboxgbtn_"))
    async def remove_box_gbtn_cb(client, callback_query):
        parts = callback_query.data.split("_", 3)
        b_idx, g_idx, label = int(parts[1]), int(parts[2]), parts[3]
        state = user_state.get(callback_query.from_user.id)
        if not state: return await callback_query.answer("❌ Session Expired")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        boxes = anime.get("custom_boxes", [])
        if b_idx < len(boxes):
            g_name = list(boxes[b_idx]["groups"].keys())[g_idx]
            if g_name in boxes[b_idx]["groups"] and label in boxes[b_idx]["groups"][g_name]:
                del boxes[b_idx]["groups"][g_name][label]
                await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                await callback_query.answer(f"🗑 Button '{label}' Removed")
                return await select_box_grp_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^add_btn_start_"))
    async def add_btn_start_cb(client, callback_query):
        aid = callback_query.data.split("add_btn_start_")[-1]
        user_state[callback_query.from_user.id] = {"action": "ask_new_btn_name", "slug": aid}
        await callback_query.message.edit_text("🖇 **New Button Name:**\n*(e.g. Watch Online, Join Channel)*", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^manage_category_"))
    async def manage_category_cb(client, callback_query):
        try:
            aid = callback_query.data.split("manage_category_")[-1]
            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Not Found", show_alert=True)

            cats = await db.get_all_categories()
            current_cat = anime.get("category", "N/A")

            buttons = []
            for c in cats:
                name = c['name']
                label = f"✅ {name}" if name == current_cat else name
                buttons.append([InlineKeyboardButton(label, callback_data=f"setncat_{aid}:::{name}", style=ButtonStyle.PRIMARY)])

            buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data=f"manage_category_{aid}", style=ButtonStyle.SUCCESS)])
            buttons.append([InlineKeyboardButton("🛡 Back", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.PRIMARY)])
            await callback_query.message.edit_text(
                f"📂 **Migrate Category: {anime['title']}**\n\n"
                f"Current: `{current_cat}`\n\n"
                "Select new destination category:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Manage Category Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^setncat_"))
    async def set_new_category_cb(client, callback_query):
        data = callback_query.data.split("setncat_")[-1]
        aid, new_cat = data.split(":::")

        try:
            if await db.ping():
                anime = await db.get_anime(aid)
                if not anime: return await callback_query.answer("❌ Not Found")
                res = await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"category": new_cat}, "$currentDate": {"updated_at": True}})
                if res.modified_count:
                    await callback_query.answer(f"🚀 Migrated to {new_cat}", show_alert=True)
                    callback_query.data = f"back_to_edit_{aid}"
                    return await back_to_edit_cb(client, callback_query)
                await callback_query.answer("⚠️ Category remains unchanged.")
            else:
                await callback_query.answer("❌ Database Offline", show_alert=True)
        except Exception as e:
            await callback_query.answer(f"❌ Error: {e}", show_alert=True)

    @bot.on_callback_query(filters.regex("^back_to_edit_"))
    async def back_to_edit_cb(client, callback_query):
        try:
            aid = callback_query.data.split("back_to_edit_")[-1]
            anime = await db.get_anime(aid)
            if not anime: return await callback_query.answer("❌ Not Found")

            buttons = [
                [InlineKeyboardButton("🔊 Languages", callback_data=f"manage_langs_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("📦 Content Groups (Seasons)", callback_data=f"manage_groups_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🗃 Custom Boxes", callback_data=f"manage_boxes_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🔗 External Redirects (Buttons)", callback_data=f"manage_btns_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🚀 Advanced Group", callback_data=f"adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🔥 Ultra Advanced Group", callback_data=f"ultra_adv_grp_start_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("📂 Change Category", callback_data=f"manage_category_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🏷 Change Title", callback_data=f"edit_title_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🖼 Change Poster", callback_data=f"trigger_poster_{aid}", style=ButtonStyle.PRIMARY)],
                [InlineKeyboardButton("🗑 Purge Archive", callback_data=f"confirm_purge_{aid}", style=ButtonStyle.DANGER)],
                [InlineKeyboardButton("🔄 Refresh", callback_data=f"back_to_edit_{aid}", style=ButtonStyle.SUCCESS), InlineKeyboardButton("❌ Abort", callback_data="cancel_op", style=ButtonStyle.DANGER)]
            ]
            await callback_query.message.edit_text(
                f"🏛 **Executive Suite: {anime['title']}**\n"
                f"ID: `{aid}`\n\n"
                "Select a sector to manage:",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            await callback_query.answer()
        except MessageNotModified:
            await callback_query.answer("Refresh Complete")
        except Exception as e:
            logger.error(f"Back to Edit Error: {e}")
            await callback_query.answer("Sync Error")

    @bot.on_callback_query(filters.regex("^setposb_"))
    async def set_pos_btn_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state: return await callback_query.answer("❌ Session Expired")

        data = callback_query.data.split("_")[-1]
        anime = await db.get_anime(state["slug"])
        btns = anime.get("custom_buttons", [])

        if data == "select":
            buttons = []
            for i in range(len(btns) + 1):
                buttons.append(InlineKeyboardButton(str(i+1), callback_data=f"setposb_{i}", style=ButtonStyle.PRIMARY))

            # Group buttons in rows of 5
            rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
            return await callback_query.message.edit_text("🎯 **Select insertion point:**", reply_markup=InlineKeyboardMarkup(rows))

        pos = int(data)
        new_btn = {"name": state["btn_name"], "link": state["btn_link"]}
        if pos == -1: btns.append(new_btn)
        else: btns.insert(pos, new_btn)

        aid = state["slug"]
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_buttons": btns}, "$currentDate": {"updated_at": True}})
        await callback_query.message.edit_text(f"✅ **Button '{state['btn_name']}' synchronized at position {pos if pos != -1 else len(btns)}.**")
        del user_state[uid]

    @bot.on_callback_query(filters.regex("^setposcg_"))
    async def set_pos_custom_group_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state: return await callback_query.answer("❌ Session Expired")

        data = callback_query.data.split("_")[-1]
        anime = await db.get_anime(state["slug"])
        groups = list(anime.get("seasons_links", {}).items())

        if data == "select":
            buttons = []
            for i in range(len(groups) + 1):
                buttons.append(InlineKeyboardButton(str(i+1), callback_data=f"setposcg_{i}", style=ButtonStyle.PRIMARY))
            rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
            return await callback_query.message.edit_text("🎯 **Select insertion point:**", reply_markup=InlineKeyboardMarkup(rows))

        pos = int(data)
        new_group = (state["cgrp_name"], state["cgrp_data"])
        if pos == -1: groups.append(new_group)
        else: groups.insert(pos, new_group)

        aid = state["slug"]
        await db.anime.update_one(
            {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
            {
                "$set": {
                    "seasons_links": dict(groups),
                    "newly_added_groups": [state["cgrp_name"]]
                },
                "$currentDate": {"updated_at": True}
            }
        )
        await callback_query.message.edit_text(f"✅ **Group '{state['cgrp_name']}' synchronized at position {pos if pos != -1 else len(groups)}.**")
        del user_state[uid]

    @bot.on_callback_query(filters.regex("^setposgb_"))
    async def set_pos_group_btn_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state: return await callback_query.answer("❌ Session Expired")

        data = callback_query.data.split("_")[-1]
        g_idx = state["g_idx"]
        anime = await db.get_anime(state["slug"])
        groups = list(anime.get("seasons_links", {}).items())
        gname, gdata = groups[g_idx]
        items = list(gdata.items())

        if data == "select":
            buttons = []
            for i in range(len(items) + 1):
                buttons.append(InlineKeyboardButton(str(i+1), callback_data=f"setposgb_{i}", style=ButtonStyle.PRIMARY))
            rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
            return await callback_query.message.edit_text("🎯 **Select insertion point:**", reply_markup=InlineKeyboardMarkup(rows))

        pos = int(data)
        new_btn = (state["new_label"], state["new_link"])
        if pos == -1: items.append(new_btn)
        else: items.insert(pos, new_btn)

        groups[g_idx] = (gname, dict(items))
        aid = state["slug"]
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": dict(groups)}, "$currentDate": {"updated_at": True}})
        await callback_query.message.edit_text(f"✅ **Button '{state['new_label']}' synchronized in group '{gname}'.**")
        del user_state[uid]

    @bot.on_callback_query(filters.regex("^setposg_"))
    async def set_pos_group_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state: return await callback_query.answer("❌ Session Expired")

        data = callback_query.data.split("_")[-1]
        anime = await db.get_anime(state["slug"])
        groups = list(anime.get("seasons_links", {}).items())

        if data == "select":
            buttons = []
            for i in range(len(groups) + 1):
                buttons.append(InlineKeyboardButton(str(i+1), callback_data=f"setposg_{i}", style=ButtonStyle.PRIMARY))
            rows = [buttons[i:i + 5] for i in range(0, len(buttons), 5)]
            return await callback_query.message.edit_text("🎯 **Select insertion point:**", reply_markup=InlineKeyboardMarkup(rows))

        user_state[uid].update({"action": "ask_edit_480p", "insert_pos": int(data)})
        await callback_query.message.edit_text(f"📦 **Configuring: {state['group_name']}**\n\n🛰 **480p Link** (or /skip):")

    @bot.on_callback_query(filters.regex("^rgbtn_"))
    async def remove_group_btn_cb(client, callback_query):
        parts = callback_query.data.split("_")
        g_idx, b_idx = int(parts[1]), int(parts[2])
        state = user_state.get(callback_query.from_user.id)
        if not state: return await callback_query.answer("❌ Session Expired")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        groups = list(anime.get("seasons_links", {}).items())

        gname, gdata = groups[g_idx]
        btn_labels = list(gdata.keys())
        label = btn_labels[b_idx]

        del gdata[label]
        groups[g_idx] = (gname, gdata)

        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": dict(groups)}, "$currentDate": {"updated_at": True}})
        await callback_query.answer(f"🗑 Removed {label}")
        await select_group_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^egbtn_"))
    async def edit_group_btn_cb(client, callback_query):
        parts = callback_query.data.split("_")
        user_state[callback_query.from_user.id].update({
            "action": "ask_edit_gbtn_label",
            "g_idx": int(parts[1]),
            "b_idx": int(parts[2])
        })
        await callback_query.message.edit_text("✏️ **New Label for this button:**\n*(or `/skip` to keep current)*", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^mvupgb_"))
    async def move_gbtn_up_cb(client, callback_query):
        parts = callback_query.data.split("_")
        g_idx, b_idx = int(parts[1]), int(parts[2])
        if b_idx == 0: return await callback_query.answer("🔝 Already at top")

        state = user_state.get(callback_query.from_user.id)
        aid = state["slug"]
        anime = await db.get_anime(aid)
        groups = list(anime.get("seasons_links", {}).items())
        gname, gdata = groups[g_idx]

        items = list(gdata.items())
        items[b_idx], items[b_idx-1] = items[b_idx-1], items[b_idx]
        groups[g_idx] = (gname, dict(items))

        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": dict(groups)}, "$currentDate": {"updated_at": True}})
        await select_group_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^mvdngb_"))
    async def move_gbtn_dn_cb(client, callback_query):
        parts = callback_query.data.split("_")
        g_idx, b_idx = int(parts[1]), int(parts[2])

        state = user_state.get(callback_query.from_user.id)
        aid = state["slug"]
        anime = await db.get_anime(aid)
        groups = list(anime.get("seasons_links", {}).items())
        gname, gdata = groups[g_idx]

        items = list(gdata.items())
        if b_idx >= len(items) - 1: return await callback_query.answer("🔚 Already at bottom")

        items[b_idx], items[b_idx+1] = items[b_idx+1], items[b_idx]
        groups[g_idx] = (gname, dict(items))

        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": dict(groups)}, "$currentDate": {"updated_at": True}})
        await select_group_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^add_gbtn_start_"))
    async def add_gbtn_start_cb(client, callback_query):
        g_idx = int(callback_query.data.split("_")[-1])
        user_state[callback_query.from_user.id].update({"action": "ask_new_gbtn_label", "g_idx": g_idx})
        await callback_query.message.edit_text("➕ **New Button Label:**\n*(e.g. 1080p, Mirror 1)*", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^rengidx_"))
    async def rename_group_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "group_names" not in state: return await callback_query.answer("❌ Session Expired", show_alert=True)

        gname = state["group_names"][idx]
        user_state[callback_query.from_user.id].update({"action": "ask_rename_group_new_name", "old_name": gname})
        await callback_query.message.edit_text(f"✏️ **Rename Group: {gname}**\n\nSend the **New Name** for this group:", reply_markup=None)

    @bot.on_callback_query(filters.regex("^editlidx_"))
    async def edit_links_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "group_names" not in state: return await callback_query.answer("❌ Session Expired", show_alert=True)

        gname = state["group_names"][idx]
        user_state[callback_query.from_user.id].update({"action": "ask_edit_group_name", "group_name": gname})
        await callback_query.message.edit_text(f"🔗 **Updating Links: {gname}**\n\nExisting links for this quality will be replaced.\n🛰 **480p Link** (or `/skip`):", reply_markup=None)

    @bot.on_callback_query(filters.regex("^remgidx_"))
    async def remove_group_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "group_names" not in state: return await callback_query.answer("❌ Session Expired", show_alert=True)

        gname, aid = state["group_names"][idx], state["slug"]
        anime = await db.get_anime(aid)
        groups = anime.get("seasons_links", {})
        if gname in groups:
            del groups[gname]
            await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": groups}, "$currentDate": {"updated_at": True}})
            await callback_query.answer(f"🗑 Removed {gname}", show_alert=True)
            return await manage_groups_cb(client, callback_query)
        await callback_query.answer("❌ Group already missing")

    @bot.on_callback_query(filters.regex("^mvupg_"))
    async def move_group_up_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "group_names" not in state or idx == 0: return await callback_query.answer("❌ Error")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        groups = anime.get("seasons_links", {})
        items = list(groups.items())
        items[idx], items[idx-1] = items[idx-1], items[idx]
        new_groups = dict(items)
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": new_groups}, "$currentDate": {"updated_at": True}})
        await manage_groups_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^mvdng_"))
    async def move_group_dn_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "group_names" not in state: return await callback_query.answer("❌ Error")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        groups = anime.get("seasons_links", {})
        items = list(groups.items())
        if idx >= len(items) - 1: return await callback_query.answer("❌ Error")

        items[idx], items[idx+1] = items[idx+1], items[idx]
        new_groups = dict(items)
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": new_groups}, "$currentDate": {"updated_at": True}})
        await manage_groups_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^mvupb_"))
    async def move_btn_up_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "btns" not in state or idx == 0: return await callback_query.answer("❌ Error")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        btns = anime.get("custom_buttons", [])
        btns[idx], btns[idx-1] = btns[idx-1], btns[idx]
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_buttons": btns}, "$currentDate": {"updated_at": True}})
        await manage_btns_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^mvdnb_"))
    async def move_btn_dn_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "btns" not in state: return await callback_query.answer("❌ Error")

        aid = state["slug"]
        anime = await db.get_anime(aid)
        btns = anime.get("custom_buttons", [])
        if idx >= len(btns) - 1: return await callback_query.answer("❌ Error")

        btns[idx], btns[idx+1] = btns[idx+1], btns[idx]
        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_buttons": btns}, "$currentDate": {"updated_at": True}})
        await manage_btns_cb(client, callback_query)

    @bot.on_callback_query(filters.regex("^rembidx_"))
    async def remove_btn_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "btns" not in state: return await callback_query.answer("❌ Session Expired", show_alert=True)

        aid = state["slug"]
        anime = await db.get_anime(aid)
        btns = anime.get("custom_buttons", [])
        if idx < len(btns):
            btn_name = btns[idx]['name']
            btns.pop(idx)
            await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_buttons": btns}, "$currentDate": {"updated_at": True}})
            await callback_query.answer(f"🗑 Removed {btn_name}", show_alert=True)
            return await manage_btns_cb(client, callback_query)
        await callback_query.answer("❌ Button missing")

    @bot.on_callback_query(filters.regex("^rebidx_"))
    async def edit_btn_cb(client, callback_query):
        idx = int(callback_query.data.split("_")[-1])
        state = user_state.get(callback_query.from_user.id)
        if not state or "btns" not in state: return await callback_query.answer("❌ Session Expired", show_alert=True)

        btn = state["btns"][idx]
        user_state[callback_query.from_user.id].update({"action": "ask_edit_btn_name", "btn_idx": idx})
        await callback_query.message.edit_text(f"✏️ **Edit Button: {btn['name']}**\n\nSend the **New Name** for this button:", reply_markup=None)

    @bot.on_callback_query(filters.regex("^edit_title_"))
    async def edit_title_cb(client, callback_query):
        aid = callback_query.data.split("edit_title_")[-1]
        user_state[callback_query.from_user.id] = {"action": "ask_change_series_title", "slug": aid}
        await callback_query.message.edit_text("🏷 **Change Series Title:**\n\nSend the **New Title** for this series:", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^cancel_op$"))
    async def cancel_op_cb(client, callback_query):
        user_state.pop(callback_query.from_user.id, None)
        await callback_query.message.edit_text("✨ **Operation Cancelled.**", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^img_"))
    async def image_choice_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state or state["action"] != "ask_image_choice": return
        choice = callback_query.data.split("_")[1]
        user_state[uid]["image"] = state["anime_data"]["image"] if choice == "api" else None
        if choice == "api":
            # Image selection completes the search setup. Groups/boxes are added later
            # by admins through /edit or /edit_m.
            user_state[uid]["seasons_data"] = {}
            user_state[uid]["action"] = "ask_category_final"
            cats = await db.get_all_categories()
            buttons = [[InlineKeyboardButton(c['name'], callback_data=f"finalcat_{c['name']}", style=ButtonStyle.PRIMARY)] for c in (cats if cats else [{"name": "Anime"}])]
            await callback_query.message.edit_caption(caption="✅ **Poster Selected.**\n\n📂 Choose a category to publish.\n\n📦 Admins can add groups or boxes later using `/edit` or `/edit_m`.", reply_markup=InlineKeyboardMarkup(buttons))
        else:
            user_state[uid]["action"] = "ask_manual_img"
            await callback_query.message.edit_caption(caption="🛰 **Send the direct image URL** (or /skip to use no poster):", reply_markup=None)
        await callback_query.answer()

    @bot.on_callback_query(filters.regex("^finalcat_"))
    async def final_publish_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state or state["action"] != "ask_category_final": return
        cat, data = callback_query.data[len("finalcat_"):], state["anime_data"]
        page_title = title_with_category(data.get("title"), cat)
        slug = slugify(page_title)
        entry = {"mal_id": f"series_{slug}", "title": page_title, "slug": slug, "synopsis": data.get("synopsis", "N/A"), "score": data.get("score", 0), "image": state.get("image"), "genres": data.get("genres", []), "category": cat, "status": data.get("status", "N/A"), "year": data.get("year", "N/A"), "trailer": data.get("trailer"), "studios": data.get("studios", []), "seasons_links": state.get("seasons_data", {}), "custom_buttons": [], "_empty_page_since": time.time()}
        try:
            if await db.ping():
                await db.anime.update_one({"slug": slug}, {"$set": entry, "$currentDate": {"updated_at": True}}, upsert=True)
                await callback_query.message.edit_text(text=f"💎 **LIVE:** `{page_title}`\nPortal: {Config.BASE_URL}/anime/{slug}", reply_markup=None)
                del user_state[uid]
            else:
                await callback_query.answer("❌ Database Offline", show_alert=True)
        except Exception as e: await callback_query.answer(f"DB Error: {e}", show_alert=True)

    @bot.on_callback_query(filters.regex("^setcat_"))
    async def auto_post_set_cat_cb(client, callback_query):
        uid = callback_query.from_user.id
        state = user_state.get(uid)
        if not state or state["action"] != "ask_category": return
        cat, data = callback_query.data[len("setcat_"):], state["anime_data"]
        page_title = title_with_category(data.get("title"), cat)
        slug = slugify(page_title)
        entry = {"mal_id": f"auto_{slug}", "title": page_title, "slug": slug, "synopsis": data["synopsis"], "score": data["score"], "image": state["image"], "genres": data["genres"], "category": cat, "status": data["status"], "year": data["year"], "trailer": data["trailer"], "studios": data.get("studios", []), "seasons_links": {"1": {"480p": None, "720p": None, "1080p": None}}, "custom_buttons": [], "_empty_page_since": time.time()}
        try:
            if await db.ping():
                await db.anime.update_one({"slug": slug}, {"$set": entry, "$currentDate": {"updated_at": True}}, upsert=True)
                await callback_query.message.edit_caption(caption=f"⚡ **Deployment Success!**\n`{page_title}`\nPortal: {Config.BASE_URL}/anime/{slug}", reply_markup=None)
                del user_state[uid]
            else:
                await callback_query.answer("❌ Database Offline", show_alert=True)
        except Exception as e: await callback_query.answer(f"DB Error: {e}", show_alert=True)

    # --- INTERACTION HANDLER (GROUP 1) ---

    @bot.on_message(filters.private & (filters.text | filters.document | filters.audio | filters.video) & ~filters.command(["start", "help", "search", "post", "POST", "add_post", "add_page", "edit", "categories", "create", "del", "cancel", "change_poster", "ping", "schedule", "manual", "edit_m", "save", "category_page", "export_boxes", "delete_boxes", "duplicate", "logo", "addbot", "songs", "SONGS", "uptime", "UPTIME", "setbot", "ss"]), group=1)
    async def interaction_handler(client, message):
        if not message.from_user: return
        uid = message.from_user.id
        if not await is_authorized(uid):
            return await message.reply("🚫 **Access Denied.** Unauthorized user.")
        state = user_state.get(uid)
        msg_input = message.text or message.caption or ""
        parsed_range = parse_range_link(msg_input)

        if state and state.get("action") == "create_category_await_name":
            category_name = " ".join(msg_input.split()).strip()
            if not category_name or category_name.startswith("/") or len(category_name) > 60:
                return await message.reply("❌ Send a valid category name (1–60 characters), or /cancel.")
            return await show_create_category_sources(message, uid, category_name)

        if state and state.get("action") == "await_site_logo_url":
            url = msg_input.strip()
            if not re.match(r"^https?://[^\s]+$", url, flags=re.IGNORECASE):
                return await message.reply("❌ Send a valid http:// or https:// image URL, or /cancel.")
            try:
                from urllib.parse import urlparse
                parsed = urlparse(url)
                if not parsed.netloc or not parsed.path:
                    return await message.reply("❌ URL must point to an image file or image endpoint.")
                result = await db.set_setting("site_logo_url", url)
                if result is None:
                    return await message.reply("❌ Could not save the logo. Check the database connection and try again.")
                user_state.pop(uid, None)
                await message.reply("✅ Website logo URL saved. The homepage will use the new logo on its next load.")
            except Exception as exc:
                logger.exception("Website logo update failed")
                await message.reply(f"❌ Could not update logo: {str(exc)[:120]}")
            return

        if state and state.get("action") == "export_boxes_await_name":
            term = msg_input.strip()
            if not term or term.startswith("/"):
                return await message.reply("Send a valid box name, or /cancel to stop.")
            return await show_export_box_categories(message, term[:100])

        if state and state.get("action") == "delete_boxes_await_name":
            term = msg_input.strip()
            if not term or term.startswith("/"):
                return await message.reply("Send a valid box name, `all`, or /cancel to stop.")
            return await show_delete_boxes_confirm(message, term[:100])

        if parsed_range and (not state or state.get("action") not in ["ask_range_groups", "ask_range_filter_name", "ask_range_page_link", "ask_range_box_selection", "ask_range_mode"]):
            chat_slug, start_id, end_id = parsed_range
            if start_id > end_id:
                start_id, end_id = end_id, start_id

            configured_bots = await db.get_configured_bots()
            if not configured_bots:
                return await message.reply("❌ **No Bots Configured!** Please configure at least one bot using `/setbot` first.")

            if len(configured_bots) > 1:
                # Multiple bots configured: Ask admin to choose bot
                buttons = [
                    [InlineKeyboardButton(f"🤖 @{b}", callback_data=f"sel_range_bot_{b}", style=ButtonStyle.PRIMARY)] for b in configured_bots
                ]
                buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)])

                user_state[uid] = {
                    "chat_slug": chat_slug,
                    "start_id": start_id,
                    "end_id": end_id
                }

                return await message.reply(
                    f"🔗 **Range Link Received:** `{chat_slug}` (Message IDs: `{start_id}` to `{end_id}`)\n\n"
                    "Please select the **Bot** to use for generating links:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            else:
                selected_bot = configured_bots[0]
                user_state[uid] = {
                    "action": "ask_range_groups",
                    "selected_bot": selected_bot,
                    "chat_slug": chat_slug,
                    "start_id": start_id,
                    "end_id": end_id
                }
                return await message.reply(
                    f"🔗 **Range Link Received:** `{chat_slug}` (Message IDs: `{start_id}` to `{end_id}`)\n"
                    f"🤖 **Bot Selected:** `@{selected_bot}`\n\n"
                    "Please send the **Group Names** in this format:\n\n"
                    "1. Group name\n"
                    "2. Group name\n"
                    "3. Group name\n\n"
                    "Send /cancel to abort."
                )

        if not state:
            return

        action = state.get("action", "")

        if not message.text and action not in ["awaiting_restore_zip", "ask_song_file", "ask_replace_song_file"]:
            return await message.reply("❌ **Invalid Input.** Please send text.")

        if action == "ask_range_groups":
            parsed_groups = parse_group_names_list(message.text)
            if not parsed_groups:
                return await message.reply(
                    "❌ **Invalid Group Names Format.** Please send in serial number format:\n\n"
                    "1. Group name\n"
                    "2. Group name\n"
                    "3. Group name\n\n"
                    "Send /cancel to abort."
                )

            user_state[uid].update({
                "action": "ask_range_filter_name",
                "group_names": parsed_groups
            })
            return await message.reply(
                f"✅ **{len(parsed_groups)} Groups Registered.**\n\n"
                "Please send the **Filter Name** (e.g. `Sword Art Online` or `Naruto`):\n"
                "*(Only bot outputs containing this name, episode number, quality, and generated link will be used)*"
            )

        elif action == "ask_range_filter_name":
            filter_name = message.text.strip()
            if not filter_name:
                return await message.reply("❌ **Invalid Filter Name.** Please send a valid filter name (or /cancel to abort):")

            user_state[uid].update({
                "action": "ask_range_page_link",
                "filter_name": filter_name
            })
            return await message.reply(
                f"✅ **Filter Name Registered:** `{filter_name}`\n\n"
                "Please send the **Page Link** (e.g. `https://anizoneflix-six.vercel.app/anime/naruto` or `/anime/naruto` or `naruto`):"
            )

        elif action == "ask_range_page_link":
            page_input = message.text.strip()
            slug = extract_slug(page_input)
            if not slug:
                return await message.reply("❌ **Invalid Page Link or Title.** Please send a valid link or slug:")

            anime = await db.get_anime(slug)
            if not anime:
                results = await db.search_anime_db(page_input)
                if results:
                    anime = results[0]

            if not anime:
                return await message.reply(f"❌ **Page Not Found in Database:** `{slug}`. Please send a valid page link or slug:")

            aid = str(anime["_id"])
            user_state[uid]["aid"] = aid
            user_state[uid]["anime_title"] = anime["title"]
            user_state[uid]["anime_slug"] = anime["slug"]

            boxes = anime.get("custom_boxes", [])
            if boxes:
                # Custom boxes available: Prompt admin to choose box OR Main Section (Default)
                buttons = [
                    [InlineKeyboardButton("🌐 Main Section (Default)", callback_data=f"sel_range_box_main_{aid}", style=ButtonStyle.PRIMARY)]
                ]
                for b_idx, box in enumerate(boxes):
                    buttons.append([InlineKeyboardButton(f"🗃 Box: {box['name']}", callback_data=f"sel_range_box_{b_idx}_{aid}", style=ButtonStyle.PRIMARY)])

                buttons.append([InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)])

                user_state[uid]["action"] = "ask_range_box_selection"
                return await message.reply(
                    f"📦 **Custom Boxes Detected for '{anime['title']}':**\n\n"
                    "Select which box to add the groups to, or choose **Main Section (Default)**:",
                    reply_markup=InlineKeyboardMarkup(buttons)
                )

            # Ask mode selection (Genlink vs Serial)
            buttons = [
                [
                    InlineKeyboardButton("⚡ Genlink", callback_data=f"sel_range_mode_genlink_main_{aid}", style=ButtonStyle.PRIMARY),
                    InlineKeyboardButton("📺 Serial", callback_data=f"sel_range_mode_serial_main_{aid}", style=ButtonStyle.PRIMARY)
                ],
                [InlineKeyboardButton("❌ Cancel", callback_data="cancel_op", style=ButtonStyle.DANGER)]
            ]
            user_state[uid]["target_box_idx"] = None
            user_state[uid]["action"] = "ask_range_mode"
            return await message.reply(
                f"🎯 **Select Link Generation Mode:**\n\n"
                f"🎬 **Target Page:** `{anime['title']}`\n\n"
                "• **Genlink**: Sends `/genlink <link>` sequentially for each message.\n"
                "• **Serial**: Uses `/serielbatch` automation stream in configured bot.",
                reply_markup=InlineKeyboardMarkup(buttons)
            )

        if action == "ask_post_heading":
            heading = message.text.strip()

            # Handle cancellation
            if heading.lower() in ["/cancel", "cancel"]:
                del user_state[uid]
                return await message.reply("✨ **Action Cancelled.** standby.")

            # Validate that heading is not a command starting with /
            if heading.startswith("/"):
                return await message.reply("⚠️ **Heading cannot be a bot command.** Please send the text heading (or send /cancel to abort):")

            aid = state["aid"]
            anime = await db.get_anime(aid)
            if not anime:
                await message.reply("❌ **Error:** Anime page not found in database.")
                del user_state[uid]
                return

            post_channel = await db.get_post_channel()
            if not post_channel:
                await message.reply("⚠️ **Post Channel Not Configured.** Please use `/post <CHANNEL_ID>` first.")
                del user_state[uid]
                return

            page_link = state.get("page_link") or f"{Config.BASE_URL}/anime/{anime['slug']}"

            try:
                sent_msg = await publish_post_to_channel(client, post_channel, heading, anime, page_link)
                await message.reply(
                    f"🎉 **Post Successfully Published to Channel!**\n\n"
                    f"📢 **Channel:** `{post_channel}`\n"
                    f"💬 **Message ID:** `{sent_msg.id}`"
                )
            except Exception as post_err:
                logger.error(f"Error publishing post to channel {post_channel}: {post_err}")
                await message.reply(f"❌ **Failed to publish post to channel `{post_channel}`:** `{str(post_err)}`")

            del user_state[uid]
            return

        if action == "ask_anime_languages":
            aid = state["slug"]
            formatted_langs = parse_language_input(message.text)
            if await db.ping():
                res = await db.anime.update_one(
                    {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                    {"$set": {"languages": formatted_langs}, "$currentDate": {"updated_at": True}}
                )
                await message.reply(f"✅ **Audio Languages Updated!**\n\n🔊 **Languages:** `{formatted_langs}`")
            else:
                await message.reply("❌ Database Offline")
            del user_state[uid]
            return

        if action == "ask_setbot_username":
            bot_username = message.text.strip().lstrip("@")
            if not bot_username:
                return await message.reply("❌ **Invalid Username.** Please send a valid Telegram bot username:")
            await db.set_configured_bot(bot_username)
            del user_state[uid]
            return await message.reply(f"✅ **Configured Bot Username Saved:** `@{bot_username}`")

        elif action == "ask_ss_string":
            session_str = message.text.strip()
            if not session_str:
                return await message.reply("❌ **Invalid Session String.** Please send a valid session string:")
            await db.set_configured_session(session_str)
            del user_state[uid]
            return await message.reply("✅ **Pyrogram Session String Saved Successfully!**")

        if action == "ask_uptime_bot_url":
            url = message.text.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                return await message.reply("❌ **Invalid URL.** Must begin with `http://` or `https://`. Please send again:")

            bot_id = await db.add_monitored_bot(url)
            del user_state[uid]
            await message.reply(f"✅ **Bot Registered for 24/7 Monitoring!**\n🔗 **URL:** `{url}`\n\nThe continuous 1-second ping monitor is active.")
            return

        elif action == "ask_replace_uptime_url":
            url = message.text.strip()
            if not (url.startswith("http://") or url.startswith("https://")):
                return await message.reply("❌ **Invalid URL.** Must begin with `http://` or `https://`. Please send again:")

            bot_id = state.get("bot_id")
            await db.replace_monitored_bot(bot_id, url)
            del user_state[uid]
            await message.reply(f"✅ **Monitored Bot URL Successfully Replaced!**\n🔗 **New URL:** `{url}`")
            return

        if action == "ask_song_channel":
            channel_input = message.text.strip()
            await db.set_song_channel(channel_input)
            del user_state[uid]
            await message.reply(f"✅ **Dedicated Song Channel set to:** `{channel_input}`")
            return

        elif action in ["ask_song_file", "ask_replace_song_file"]:
            media = message.audio or message.video or message.document
            if not media and not (message.text and message.text.startswith("http")):
                return await message.reply("❌ **Invalid Media.** Please send/upload an audio or video file (MP3, M4A, WAV, MP4, etc.) or a direct media URL.")

            msg = await message.reply("⏳ **Processing & storing background song...**")
            file_name = "background_song"
            file_id = None

            if media:
                file_id = getattr(media, "file_id", None)
                file_name = getattr(media, "file_name", None) or getattr(media, "title", None) or "song.mp3"
            elif message.text:
                file_name = message.text.split("/")[-1].split("?")[0] or "song.mp3"

            # Create media directory
            songs_dir = os.path.join("static", "uploads", "songs")
            os.makedirs(songs_dir, exist_ok=True)

            song_id = state.get("song_id") or str(ObjectId())
            safe_filename = f"{song_id}_{slugify(file_name)}".strip("_")
            if not safe_filename.endswith((".mp3", ".m4a", ".wav", ".mp4", ".ogg", ".aac")):
                safe_filename += ".mp3"

            dest_path = os.path.join(songs_dir, safe_filename)
            file_path = f"/static/uploads/songs/{safe_filename}"

            try:
                if media:
                    await message.download(file_name=dest_path)
                elif message.text:
                    import httpx
                    async with httpx.AsyncClient(timeout=30.0) as http_client:
                        resp = await http_client.get(message.text.strip())
                        if resp.status_code == 200:
                            with open(dest_path, "wb") as f:
                                f.write(resp.content)

                # Post file to dedicated channel if configured
                song_channel = await db.get_song_channel()
                channel_msg_id = None
                if song_channel:
                    try:
                        sent_msg = await client.send_document(
                            chat_id=song_channel,
                            document=dest_path,
                            caption=f"🎵 **Background Song:** {file_name}\nID: `{song_id}`"
                        )
                        channel_msg_id = sent_msg.id
                    except Exception as ch_err:
                        logger.error(f"Failed to send song to channel: {ch_err}")

                song_data = {
                    "song_id": song_id,
                    "title": file_name,
                    "file_id": file_id,
                    "file_path": file_path,
                    "channel_msg_id": channel_msg_id,
                    "created_at": message.date.timestamp() if message.date else 0
                }

                if action == "ask_replace_song_file":
                    await db.replace_song(song_id, song_data)
                    await msg.edit(f"✅ **Song Successfully Replaced!**\n🎵 **Title:** `{file_name}`")
                else:
                    await db.add_song(song_data)
                    await msg.edit(f"✅ **Song Successfully Added!**\n🎵 **Title:** `{file_name}`")

                del user_state[uid]
                return
            except Exception as e:
                logger.error(f"Error saving song file: {e}")
                await msg.edit(f"❌ **Failed to process song:** `{str(e)}`")
                del user_state[uid]
                return
        try:
            if action == "addbot_await_group":
                # Safely resolve group/chat username or numeric ID or invite links
                group_input = message.text.strip()

                # Safely parse and resolve Telegram invite link to its username/ID representation
                if "t.me/+" in group_input or "joinchat" in group_input:
                    await message.reply("⚠️ **Direct Invite Links cannot be resolved via getChat directly.** Please send the exact **Group Username** (e.g. `@mygroup`) or **Numeric Group ID** (e.g. `-100...`):")
                    return

                if "t.me/" in group_input:
                    group_input = "@" + group_input.split("t.me/")[-1].split("/")[0]

                # Automatically normalize positive numbers / missing -100 prefix for supergroups
                if group_input.replace("-", "").isdigit():
                    raw_num = group_input.replace("-", "")
                    if len(raw_num) >= 10 and not raw_num.startswith("100"):
                        raw_num = "100" + raw_num
                    group_input = f"-{raw_num}"

                await message.reply("⏳ **Resolving and verifying group properties...**")

                # Verify that group/username is valid
                from bot.bot_manager import added_bot_manager
                import httpx

                # Check permissions and bot existence inside the group
                # We can perform a dynamic check using Telegram Bot API directly or via a transient Client
                tg_bot_api_url = f"https://api.telegram.org/bot{state['token']}"
                try:
                    async with httpx.AsyncClient(timeout=15.0) as http_client:
                        # 1. Resolve Chat to get exact Chat ID
                        chat_res = await http_client.get(f"{tg_bot_api_url}/getChat", params={"chat_id": group_input})
                        if chat_res.status_code != 200:
                            await message.reply(f"❌ **Chat Not Found:** Telegram cannot find or access `{group_input}`. Make sure the bot is already added to the chat.")
                            return

                        chat_data = chat_res.json().get("result", {})
                        resolved_group_id = chat_data.get("id")
                        resolved_title = chat_data.get("title") or chat_data.get("username")

                        # 2. Check if Bot is an Administrator in the chat
                        member_res = await http_client.get(f"{tg_bot_api_url}/getChatMember", params={"chat_id": resolved_group_id, "user_id": state["bot_info"]["id"]})
                        if member_res.status_code != 200:
                            await message.reply("❌ **Bot is not in the group.** Please add the bot as an administrator first.")
                            return

                        member_data = member_res.json().get("result", {})
                        status = member_data.get("status")
                        if status not in ["administrator", "creator"]:
                            await message.reply("❌ **Bot is in the group but is NOT an Administrator.** Please grant admin privileges.")
                            return

                        # 3. Check Required Admin Permissions
                        # Required: Read Messages (can_be_edited / implicit), Send Messages, Delete Messages
                        # Pyrogram / Bot API specific permissions:
                        can_post = member_data.get("can_post_messages", True)
                        can_delete = member_data.get("can_delete_messages", False)

                        missing_perms = []
                        if not can_delete:
                            missing_perms.append("Delete Messages")

                        if missing_perms:
                            await message.reply(
                                f"⚠️ **Warning: Missing Admin Permissions:**\n\n"
                                f"• {', '.join(missing_perms)}\n\n"
                                f"Please grant these permissions to @{state['bot_info']['username']} in the group for optimal performance."
                            )

                        # Save Bot configuration into added_bots collection
                        await db.added_bots.update_one(
                            {"token": state["token"]},
                            {"$set": {
                                "token": state["token"],
                                "group_id": resolved_group_id,
                                "group_title": resolved_title,
                                "bot_info": state["bot_info"]
                            }},
                            upsert=True
                        )

                        # Dynamic Boot/Start the newly added bot
                        started = await added_bot_manager.start_bot(state["token"], resolved_group_id, state["bot_info"])
                        if started:
                            await message.reply(
                                f"🎉 **Success! Bot Configuration Activated.**\n\n"
                                f"🤖 **Bot:** @{state['bot_info']['username']}\n"
                                f"👥 **Configured Chat:** `{resolved_title}` ({resolved_group_id})\n"
                                f"⚡ **Status:** Active & Scanning group query inputs 24/7."
                            )
                        else:
                            await message.reply("⚠️ **Configuration saved, but the bot client failed to boot.** Ensure API keys are active.")

                        del user_state[uid]
                except Exception as e:
                    logger.error(f"Error during addbot validation flow: {e}")
                    await message.reply(f"❌ **An unexpected error occurred during validation:** {e}")
                    try:
                        from bot.bot_manager import report_addbot_issue
                        await report_addbot_issue(state.get("token", "")[:15] + "...", f"Addbot group validation failure: {str(e)}")
                    except: pass
                return
            elif action == "ask_search_query":
                return await search_handler(client, message, is_retry=True)
            elif action == "ask_manual_title":
                user_state[uid].update({"title": message.text.strip(), "action": "ask_manual_synopsis"})
                await message.reply("📝 **Step 2: Synopsis**\nSend Synopsis:")
            elif action == "ask_manual_synopsis":
                user_state[uid].update({"synopsis": message.text.strip(), "action": "ask_manual_genre"})
                await message.reply("📝 **Step 3: Genre**\nSend Genre (e.g. Action, Comedy):")
            elif action == "ask_manual_genre":
                user_state[uid].update({"genre": message.text.strip(), "action": "ask_manual_image"})
                await message.reply("📝 **Step 4: Image URL**\nSend direct link to image:")
            elif action == "ask_manual_image":
                user_state[uid].update({"image": message.text.strip(), "action": "ask_manual_rating"})
                await message.reply("📝 **Step 5: Rating**\nSend Rating (e.g. 8.5):")
            elif action == "ask_manual_rating":
                user_state[uid].update({"rating": message.text.strip(), "action": "ask_manual_btn_count"})
                await message.reply("📝 **Step 6: How many buttons do you want to add?**\nSend a number:")
            elif action == "ask_manual_btn_count":
                try:
                    count = int(message.text.strip())
                    if count < 0: raise ValueError
                    user_state[uid].update({"btn_count": count, "current_btn": 1, "buttons": [], "action": "ask_manual_btn_name" if count > 0 else "manual_publish"})
                    if count > 0:
                        await message.reply(f"🔗 **Button 1 Name:**")
                    else:
                        return await interaction_handler(client, message) # Trigger publish
                except: await message.reply("❌ **Invalid number.** Send a valid integer:")
            elif action == "ask_manual_btn_name":
                user_state[uid]["temp_btn_name"] = message.text.strip()
                user_state[uid]["action"] = "ask_manual_btn_link"
                await message.reply(f"🔗 **Button {state['current_btn']} Link:**")
            elif action == "ask_manual_btn_link":
                if not message.text.strip().startswith("http"):
                    return await message.reply("❌ **Invalid Link.** Must start with http/https. Send again:")
                user_state[uid]["buttons"].append({"name": state["temp_btn_name"], "link": message.text.strip()})
                if state["current_btn"] < state["btn_count"]:
                    user_state[uid]["current_btn"] += 1
                    user_state[uid]["action"] = "ask_manual_btn_name"
                    await message.reply(f"🔗 **Button {user_state[uid]['current_btn']} Name:**")
                else:
                    user_state[uid]["action"] = "manual_publish"
                    return await interaction_handler(client, message)

            # --- PUBLISH MANUAL ---
            elif action == "manual_publish":
                slug = slugify(state["title"])
                entry = {
                    "mal_id": f"manual_{slug}",
                    "title": state["title"],
                    "slug": slug,
                    "synopsis": state["synopsis"],
                    "score": state["rating"],
                    "image": state["image"],
                    "genres": [g.strip() for g in state["genre"].split(",")],
                    "category": state["genre"].split(",")[0].strip(),
                    "status": "Manual",
                    "year": "N/A",
                    "custom_buttons": state.get("buttons", []),
                    "seasons_links": {}
                }
                if await db.ping():
                    await db.anime.update_one({"slug": slug}, {"$set": entry, "$currentDate": {"updated_at": True}}, upsert=True)
                    await message.reply(f"🚀 **Custom Page Published!**\nPortal: {Config.BASE_URL}/anime/{slug}")
                else:
                    await message.reply("❌ Database Offline")
                del user_state[uid]
            elif action == "ask_rename_group_new_name":
                new_name = message.text.strip()
                aid, old_name = state["slug"], state["old_name"]
                anime = await db.get_anime(aid)
                if anime:
                    groups = anime.get("seasons_links", {})
                    if old_name in groups:
                        groups[new_name] = groups.pop(old_name)
                        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": groups}, "$currentDate": {"updated_at": True}})
                        await message.reply(f"✅ **Group Renamed:** `{old_name}` → `{new_name}`")
                    else: await message.reply("❌ Group mismatch.")
                else: await message.reply("❌ Series not found.")
                del user_state[uid]
            elif action == "ask_change_series_title":
                new_title = message.text.strip()
                aid = state["slug"]
                new_slug = slugify(new_title)
                if await db.ping():
                    res = await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"title": new_title, "slug": new_slug}, "$currentDate": {"updated_at": True}})
                    if res.modified_count:
                        await message.reply(f"✅ **Identity Synchronized!**\n🏷 New Title: `{new_title}`\n🔗 New URL: {Config.BASE_URL}/anime/{new_slug}")
                    else:
                        await message.reply("❌ **Update Failed.** Identity already matches or series missing.")
                else:
                    await message.reply("❌ Database Offline")
                del user_state[uid]

            # --- EDIT_M FLOW ---
            elif action == "ask_edit_m_count":
                try:
                    count = int(message.text.strip())
                    if count <= 0: return await message.reply("❌ **Number must be greater than 0.**")
                    user_state[uid].update({"btn_count": count, "current_btn": 1, "new_buttons": [], "action": "ask_edit_m_btn_name"})
                    await message.reply(f"🔗 **Button 1 Name:**")
                except: await message.reply("❌ **Invalid number.** Send a valid integer:")
            elif action == "ask_edit_m_btn_name":
                user_state[uid]["temp_btn_name"] = message.text.strip()
                user_state[uid]["action"] = "ask_edit_m_btn_link"
                await message.reply(f"🔗 **Button {state['current_btn']} Link:**")
            elif action == "ask_edit_m_btn_link":
                if not message.text.strip().startswith("http"):
                    return await message.reply("❌ **Invalid Link.** Must start with http/https. Send again:")
                user_state[uid]["new_buttons"].append({"name": state["temp_btn_name"], "link": message.text.strip()})
                if state["current_btn"] < state["btn_count"]:
                    user_state[uid]["current_btn"] += 1
                    user_state[uid]["action"] = "ask_edit_m_btn_name"
                    await message.reply(f"🔗 **Button {user_state[uid]['current_btn']} Name:**")
                else:
                    # Update database
                    existing_buttons = state["anime"].get("custom_buttons", [])
                    existing_buttons.extend(state["new_buttons"])
                    if await db.ping():
                        await db.anime.update_one({"slug": state["slug"]}, {"$set": {"custom_buttons": existing_buttons}, "$currentDate": {"updated_at": True}})
                        await message.reply(f"✅ **Buttons Synchronized!**\nPage: {Config.BASE_URL}/anime/{state['slug']}")
                    else:
                        await message.reply("❌ Database Offline")
                    del user_state[uid]

            elif action == "ask_cat_name":
                await db.add_category(message.text.strip())
                await message.reply(f"✅ **Category Created.** Use /categories to view.")
                del user_state[uid]
            elif action == "ask_new_btn_name":
                user_state[uid].update({"action": "ask_new_btn_link", "btn_name": message.text.strip()})
                await message.reply(f"🔗 **URL for '{message.text.strip()}':**")
            elif action == "ask_new_btn_link":
                if not message.text.startswith("http"): return await message.reply("❌ Invalid URL.")
                user_state[uid].update({"action": "ask_btn_pos", "btn_link": message.text.strip()})
                buttons = [
                    [InlineKeyboardButton("🔝 Beginning", callback_data="setposb_0", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🔚 End", callback_data="setposb_-1", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🎯 Select Position", callback_data="setposb_select", style=ButtonStyle.PRIMARY)]
                ]
                await message.reply("📍 **Position for the new button:**", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_cgrp_btn_count":
                try:
                    count = int(message.text.strip())
                    if count <= 0:
                        return await message.reply("❌ **Number must be greater than 0. Please try again:**")
                    user_state[uid].update({
                        "action": "ask_cgrp_links_format",
                        "btn_count": count
                    })
                    await message.reply(
                        "Please send the button names and links in the following format:\n\n"
                        "GROUP NAME\n"
                        "Button Name : Link Button Name : Link\n\n"
                        "Example:\n"
                        "EP15\n\n"
                        "480p : https://example.com/480p 720p : https://example.com/720p 1080p : https://example.com/1080p"
                    )
                except:
                    await message.reply("❌ **Invalid number.** Send a valid integer:")
            elif action == "ask_cgrp_links_format":
                btn_count = state["btn_count"]
                # Split message into non-empty lines
                lines = [line.strip() for line in message.text.split("\n") if line.strip()]
                if len(lines) < 2:
                    return await message.reply(
                        f"❌ **Invalid Format.** Please send the Group Name on the first line, followed by exactly {btn_count} buttons on the next line:\n\n"
                        "GROUP NAME\n"
                        "Button Name : Link Button Name : Link\n\n"
                        "Example:\n"
                        "EP15\n\n"
                        "480p : https://example.com/480p 720p : https://example.com/720p 1080p : https://example.com/1080p"
                    )

                gname = lines[0]
                buttons_text = "\n".join(lines[1:])
                parsed_buttons = parse_buttons_string(buttons_text, btn_count)
                if parsed_buttons is None:
                    return await message.reply(
                        f"❌ **Invalid Format or Link.** Please send exactly {btn_count} buttons in the correct format:\n\n"
                        "GROUP NAME\n"
                        "Button Name : Link Button Name : Link\n\n"
                        "Example:\n"
                        "EP15\n\n"
                        "480p : https://example.com/480p 720p : https://example.com/720p 1080p : https://example.com/1080p"
                    )

                user_state[uid].update({
                    "cgrp_name": gname,
                    "cgrp_data": parsed_buttons,
                    "action": "ask_cgrp_pos"
                })
                buttons = [
                    [InlineKeyboardButton("🔝 Beginning", callback_data="setposcg_0", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🔚 End", callback_data="setposcg_-1", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🎯 Select Position", callback_data="setposcg_select", style=ButtonStyle.PRIMARY)]
                ]
                await message.reply(f"📍 **Position for group '{gname}':**", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_cgrp_name_after_links":
                # Kept as fallback in case any existing state is active
                gname = message.text.strip()
                user_state[uid].update({
                    "cgrp_name": gname,
                    "action": "ask_cgrp_pos"
                })
                buttons = [
                    [InlineKeyboardButton("🔝 Beginning", callback_data="setposcg_0", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🔚 End", callback_data="setposcg_-1", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🎯 Select Position", callback_data="setposcg_select", style=ButtonStyle.PRIMARY)]
                ]
                await message.reply(f"📍 **Position for group '{gname}':**", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_ultra_adv_grp_message":
                from utils.parser import parse_ultra_advanced_group
                parsed_boxes, parsing_errors = parse_ultra_advanced_group(message.text)
                if parsing_errors:
                    await message.reply(
                        f"❌ **Formatting Error in Ultra Advanced Group:**\n\n"
                        f"{chr(10).join(parsing_errors[:10])}\n"
                        f"{'...and more' if len(parsing_errors) > 10 else ''}\n\n"
                        "Please correct the formatting and resend the complete message (or send /cancel to abort):"
                    )
                    return

                aid = state["slug"]
                anime = await db.get_anime(aid)
                if not anime:
                    await message.reply("❌ **Error:** Anime page not found in database anymore.")
                    user_state.pop(uid, None)
                    return

                boxes = anime.get("custom_boxes", [])

                # We need to process sequentially
                boxes_processed = len(parsed_boxes)
                successful_boxes = 0
                failed_boxes_list = []
                groups_added = 0
                buttons_added = 0

                # Convert existing box names to lowercase for robust case-insensitive matching
                box_name_map = {}
                for idx, box in enumerate(boxes):
                    box_name_map[box["name"].strip().lower()] = idx

                for item in parsed_boxes:
                    box_name_ref = item["box_name"].strip()
                    lower_ref = box_name_ref.lower()

                    if lower_ref not in box_name_map:
                        failed_boxes_list.append(box_name_ref)
                        continue

                    # Found the existing box!
                    b_idx = box_name_map[lower_ref]
                    target_box = boxes[b_idx]

                    if "groups" not in target_box or not isinstance(target_box["groups"], dict):
                        target_box["groups"] = {}

                    # Add groups
                    for grp in item["groups"]:
                        grp_name = grp["group_name"]
                        buttons_dict = grp["buttons"]

                        if grp_name not in target_box["groups"]:
                            target_box["groups"][grp_name] = {}

                        # Append newly supplied buttons to existing/new groups
                        target_box["groups"][grp_name].update(buttons_dict)
                        groups_added += 1
                        buttons_added += len(buttons_dict)

                    successful_boxes += 1

                if successful_boxes > 0:
                    new_grp_names = []
                    for item in parsed_boxes:
                        for grp in item["groups"]:
                            new_grp_names.append(grp["group_name"])

                    await db.anime.update_one(
                        {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                        {
                            "$set": {
                                "custom_boxes": boxes,
                                "newly_added_groups": new_grp_names
                            },
                            "$currentDate": {"updated_at": True}
                        }
                    )

                failed_boxes_count = len(failed_boxes_list)

                summary = (
                    "✅ **Ultra Advanced Group Completed**\n\n"
                    f"📦 **Boxes Processed:** {boxes_processed}\n"
                    f"✅ **Successful Boxes:** {successful_boxes}\n"
                    f"❌ **Boxes Not Found:** {failed_boxes_count}\n"
                    f"➕ **Groups Added:** {groups_added}\n"
                    f"🔘 **Buttons Added:** {buttons_added}\n"
                )

                if failed_boxes_list:
                    summary += "\n**Not Found:**\n" + "\n".join(f"• {name}" for name in failed_boxes_list)

                user_state.pop(uid, None)
                await message.reply(summary)

            elif action == "ask_adv_grp_message":
                parsed_groups, error_msg = parse_advanced_group_message(message.text)
                if error_msg:
                    await message.reply(
                        f"❌ **Formatting Error:**\n{error_msg}\n\n"
                        "Please correct the formatting and resend the complete message (or send /cancel to abort):"
                    )
                    return

                aid, b_idx = state["slug"], state["box_idx"]
                anime = await db.get_anime(aid)
                if not anime:
                    await message.reply("❌ **Error:** Anime page not found in database anymore.")
                    del user_state[uid]
                    return

                boxes = anime.get("custom_boxes", [])
                if b_idx >= len(boxes):
                    await message.reply("❌ **Error:** Target box not found in database anymore.")
                    del user_state[uid]
                    return

                if "groups" not in boxes[b_idx] or not isinstance(boxes[b_idx]["groups"], dict):
                    boxes[b_idx]["groups"] = {}

                boxes[b_idx]["groups"].update(parsed_groups)

                await db.anime.update_one(
                    {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                    {
                        "$set": {
                            "custom_boxes": boxes,
                            "newly_added_groups": list(parsed_groups.keys())
                        },
                        "$currentDate": {"updated_at": True}
                    }
                )

                g_count = len(parsed_groups)
                btn_count = sum(len(btn_map) for btn_map in parsed_groups.values())

                success_text = (
                    "✅ **Advanced Group Import Successful!**\n\n"
                    f"📦 **Custom Box:** {boxes[b_idx]['name']}\n"
                    f"📂 **Groups added/updated:** {g_count}\n"
                    f"🔗 **Buttons added/updated:** {btn_count}\n\n"
                    "All changes have been safely saved and instantly synchronized with the website."
                )

                del user_state[uid]
                await message.reply(success_text)
            elif action == "ask_box_name":
                from utils.parser import parse_bulk_box_names
                box_names, parsing_errors = parse_bulk_box_names(message.text)
                if parsing_errors:
                    await message.reply(
                        f"❌ **Formatting Error in Bulk Box format:**\n\n"
                        f"{chr(10).join(parsing_errors[:10])}\n"
                        f"{'...and more' if len(parsing_errors) > 10 else ''}\n\n"
                        "Please correct the formatting and send again (or send /cancel to abort):"
                    )
                    return

                aid = state["slug"]
                anime = await db.get_anime(aid)
                if not anime:
                    await message.reply("❌ **Error:** Anime page not found in database.")
                    user_state.pop(uid, None)
                    return

                existing_boxes = anime.get("custom_boxes", [])
                existing_box_names_lower = {b["name"].strip().lower() for b in existing_boxes}

                total_submitted = len(box_names)
                added_boxes = []
                already_exists = []
                failed = 0

                for bname in box_names:
                    bname_clean = bname.strip()
                    if bname_clean.lower() in existing_box_names_lower:
                        already_exists.append(bname_clean)
                    else:
                        new_box = {
                            "name": bname_clean,
                            "link": None,
                            "groups": {}
                        }
                        existing_boxes.append(new_box)
                        added_boxes.append(bname_clean)

                if added_boxes:
                    await db.anime.update_one(
                        {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                        {"$set": {"custom_boxes": existing_boxes}, "$currentDate": {"updated_at": True}}
                    )

                summary = (
                    "✅ **Bulk Box Creation Completed**\n\n"
                    f"📦 **Total Submitted:** {total_submitted}\n"
                    f"✅ **Added:** {len(added_boxes)}\n"
                    f"⚠️ **Already Exists:** {len(already_exists)}\n"
                    f"❌ **Failed:** {failed}\n"
                )

                if already_exists:
                    summary += "\n**Already Exists:**\n" + "\n".join(f"• {name}" for name in already_exists)

                user_state.pop(uid, None)
                await message.reply(summary)
            elif action == "ask_box_link":
                link = message.text.strip() if message.text != "/skip" else None
                user_state[uid].update({"box_link": link, "action": "ask_box_group_check"})
                buttons = [
                    [InlineKeyboardButton("✅ Yes", callback_data="box_grp_yes", style=ButtonStyle.SUCCESS), InlineKeyboardButton("❌ No", callback_data="box_grp_no", style=ButtonStyle.DANGER)]
                ]
                await message.reply("📦 **Do you want to add a group to this box?**\n*(Reply Y/N or use buttons)*", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_box_group_check":
                text = message.text.strip().upper()
                if text in ["Y", "YES"]:
                    user_state[uid].update({"action": "ask_box_initial_grp_name"})
                    await message.reply("📦 **First Group Name:**")
                elif text in ["N", "NO"]:
                    aid = state["slug"]
                    new_box = {"name": state["box_name"], "link": state["box_link"], "groups": {}}
                    anime = await db.get_anime(aid)
                    boxes = anime.get("custom_boxes", [])
                    boxes.append(new_box)
                    await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                    await message.reply(f"✅ **Box '{state['box_name']}' created successfully (Empty).**")
                    del user_state[uid]
                else:
                    await message.reply("❌ **Invalid Input.** Please send Y or N:")
            elif action == "ask_box_initial_grp_name":
                user_state[uid].update({"action": "ask_box_initial_grp_btn_count", "temp_grp_name": message.text.strip()})
                await message.reply(f"🖇 **How many buttons in '{message.text.strip()}'?**")
            elif action == "ask_box_initial_grp_btn_count":
                try:
                    count = int(message.text.strip())
                    user_state[uid].update({"action": "ask_box_initial_grp_btn_label", "btn_count": count, "current_idx": 1, "temp_grp_data": {}})
                    await message.reply(f"🏷 **Button 1 Label:**")
                except: return await message.reply("❌ **Invalid number.** Send a valid integer:")
            elif action == "ask_box_initial_grp_btn_label":
                user_state[uid].update({"action": "ask_box_initial_grp_btn_link", "temp_btn_label": message.text.strip()})
                await message.reply(f"🔗 **URL for '{message.text.strip()}':**")
            elif action == "ask_box_initial_grp_btn_link":
                if not message.text.startswith("http"): return await message.reply("❌ Invalid URL.")
                state["temp_grp_data"][state["temp_btn_label"]] = message.text.strip()
                if state["current_idx"] < state["btn_count"]:
                    user_state[uid]["current_idx"] += 1
                    user_state[uid]["action"] = "ask_box_initial_grp_btn_label"
                    await message.reply(f"🏷 **Button {user_state[uid]['current_idx']} Label:**")
                else:
                    # Finalize BOX with initial group
                    aid = state["slug"]
                    new_box = {
                        "name": state["box_name"],
                        "link": state["box_link"],
                        "groups": {state["temp_grp_name"]: state["temp_grp_data"]}
                    }
                    anime = await db.get_anime(aid)
                    boxes = anime.get("custom_boxes", [])
                    boxes.append(new_box)
                    await db.anime.update_one(
                        {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                        {
                            "$set": {
                                "custom_boxes": boxes,
                                "newly_added_groups": [state["temp_grp_name"]]
                            },
                            "$currentDate": {"updated_at": True}
                        }
                    )
                    await message.reply(f"✅ **Box '{state['box_name']}' created with group '{state['temp_grp_name']}'.**")
                    del user_state[uid]
            elif action == "ask_box_cgrp_name":
                user_state[uid].update({"action": "ask_box_cgrp_btn_count", "temp_grp_name": message.text.strip()})
                await message.reply(f"🖇 **How many buttons in '{message.text.strip()}'?**")
            elif action == "ask_box_cgrp_btn_count":
                try:
                    count = int(message.text.strip())
                    user_state[uid].update({"action": "ask_box_cgrp_btn_label", "btn_count": count, "current_idx": 1, "temp_grp_data": {}})
                    await message.reply(f"🏷 **Button 1 Label:**")
                except: return await message.reply("❌ **Invalid number.** Send a valid integer:")
            elif action == "ask_box_cgrp_btn_label":
                user_state[uid].update({"action": "ask_box_cgrp_btn_link", "temp_btn_label": message.text.strip()})
                await message.reply(f"🔗 **URL for '{message.text.strip()}':**")
            elif action == "ask_box_cgrp_btn_link":
                if not message.text.startswith("http"): return await message.reply("❌ Invalid URL.")
                state["temp_grp_data"][state["temp_btn_label"]] = message.text.strip()
                if state["current_idx"] < state["btn_count"]:
                    user_state[uid]["current_idx"] += 1
                    user_state[uid]["action"] = "ask_box_cgrp_btn_label"
                    await message.reply(f"🏷 **Button {user_state[uid]['current_idx']} Label:**")
                else:
                    aid, b_idx = state["slug"], state["box_idx"]
                    anime = await db.get_anime(aid)
                    boxes = anime.get("custom_boxes", [])
                    boxes[b_idx]["groups"][state["temp_grp_name"]] = state["temp_grp_data"]
                    await db.anime.update_one(
                        {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                        {
                            "$set": {
                                "custom_boxes": boxes,
                                "newly_added_groups": [state["temp_grp_name"]]
                            },
                            "$currentDate": {"updated_at": True}
                        }
                    )
                    await message.reply(f"✅ **Group '{state['temp_grp_name']}' added to box '{boxes[b_idx]['name']}'.**")
                    del user_state[uid]
            elif action == "ask_renbox_name":
                idx, aid = state["box_idx"], state["slug"]
                anime = await db.get_anime(aid)
                boxes = anime.get("custom_boxes", [])
                boxes[idx]["name"] = message.text.strip()
                await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                await message.reply("✅ Box Renamed.")
                del user_state[uid]
            elif action == "ask_edbox_link":
                idx, aid = state["box_idx"], state["slug"]
                anime = await db.get_anime(aid)
                boxes = anime.get("custom_boxes", [])
                boxes[idx]["link"] = message.text.strip() if message.text != "/skip" else None
                await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                await message.reply("✅ Box Link Updated.")
                del user_state[uid]
            elif action == "ask_box_g_btn_label":
                user_state[uid].update({"action": "ask_box_g_btn_link", "temp_btn_label": message.text.strip()})
                await message.reply(f"🔗 **URL for '{message.text.strip()}':**")
            elif action == "ask_box_g_btn_link":
                if not message.text.startswith("http"): return await message.reply("❌ Invalid URL.")
                aid, b_idx, g_name = state["slug"], state["box_idx"], state["box_g_name"]
                anime = await db.get_anime(aid)
                boxes = anime.get("custom_boxes", [])
                boxes[b_idx]["groups"][g_name][state["temp_btn_label"]] = message.text.strip()
                await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                await message.reply(f"✅ Button added to group '{g_name}'.")
                del user_state[uid]
            elif action == "ask_renbox_g_name":
                idx, aid, old_name = state["box_idx"], state["slug"], state["old_name"] if "old_name" in state else state["old_g_name"]
                new_name = message.text.strip()
                anime = await db.get_anime(aid)
                boxes = anime.get("custom_boxes", [])
                if old_name in boxes[idx]["groups"]:
                    # Order-preserving rename
                    groups = boxes[idx]["groups"]
                    new_groups = {}
                    for k, v in groups.items():
                        if k == old_name: new_groups[new_name] = v
                        else: new_groups[k] = v
                    boxes[idx]["groups"] = new_groups
                    await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                    await message.reply(f"✅ Group Renamed: {old_name} -> {new_name}")
                del user_state[uid]
            elif action == "ask_ebox_gbtn_label":
                user_state[uid].update({"action": "ask_ebox_gbtn_link", "new_label": message.text.strip()})
                await message.reply("🔗 **New URL:**\n*(or /skip)*")
            elif action == "ask_ebox_gbtn_link":
                idx, aid, g_name, old_label = state["box_idx"], state["slug"], state["box_g_name"], state["old_label"]
                anime = await db.get_anime(aid)
                boxes = anime.get("custom_boxes", [])
                group = boxes[idx]["groups"][g_name]

                new_label = state["new_label"] if state["new_label"] != "/skip" else old_label
                new_link = message.text.strip() if message.text != "/skip" else group[old_label]

                if new_label != old_label:
                    del group[old_label]
                group[new_label] = new_link

                await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_boxes": boxes}, "$currentDate": {"updated_at": True}})
                await message.reply("✅ Button Updated.")
                del user_state[uid]
            elif action == "ask_new_gbtn_label":
                user_state[uid].update({"action": "ask_new_gbtn_link", "new_label": message.text.strip()})
                await message.reply(f"🔗 **URL for '{message.text.strip()}':**")
            elif action == "ask_new_gbtn_link":
                if not message.text.startswith("http"): return await message.reply("❌ Invalid URL.")
                user_state[uid].update({"action": "ask_gbtn_pos", "new_link": message.text.strip()})
                buttons = [
                    [InlineKeyboardButton("🔝 Beginning", callback_data="setposgb_0", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🔚 End", callback_data="setposgb_-1", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🎯 Select Position", callback_data="setposgb_select", style=ButtonStyle.PRIMARY)]
                ]
                await message.reply("📍 **Position for the new button:**", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_edit_gbtn_label":
                user_state[uid].update({"action": "ask_edit_gbtn_link", "new_label": message.text.strip()})
                await message.reply("🔗 **New URL:**\n*(or `/skip` to keep current)*")
            elif action == "ask_edit_gbtn_link":
                g_idx, b_idx = state["g_idx"], state["b_idx"]
                aid = state["slug"]
                anime = await db.get_anime(aid)
                groups = list(anime.get("seasons_links", {}).items())
                gname, gdata = groups[g_idx]
                items = list(gdata.items())
                old_label, old_link = items[b_idx]
                new_label = state["new_label"] if state["new_label"] != "/skip" else old_label
                new_link = message.text.strip() if message.text != "/skip" else old_link
                items[b_idx] = (new_label, new_link)
                groups[g_idx] = (gname, dict(items))
                await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"seasons_links": dict(groups)}, "$currentDate": {"updated_at": True}})
                await message.reply("✅ Button updated.")
                del user_state[uid]
            elif action == "ask_sched_content":
                if message.text != "/skip": await db.update_schedule(state["day"], message.text.strip())
                await message.reply(f"✅ **Schedule Updated.**")
                del user_state[uid]
            elif action == "awaiting_restore_zip":
                if not message.document or not message.document.file_name.endswith(".zip"):
                    return await message.reply("❌ **Invalid File.** Please upload a valid .zip backup file.")

                msg = await message.reply("⏳ **Restoring database records...**")
                path = None
                try:
                    path = await message.download()
                    with tempfile.TemporaryDirectory() as tmp_dir:
                        with zipfile.ZipFile(path, 'r') as zip_ref:
                            zip_ref.extractall(tmp_dir)

                        restored_data = {}
                        for filename in os.listdir(tmp_dir):
                            if filename.endswith(".json"):
                                coll_name = filename[:-5]
                                with open(os.path.join(tmp_dir, filename), 'r') as f:
                                    restored_data[coll_name] = json.load(f)

                        if not restored_data:
                            return await msg.edit("❌ **Restoration Failed:** No compatible JSON data found within the ZIP.")

                        success = await db.import_data(restored_data)
                        if success:
                            await msg.edit("✅ **Restoration Successful!** The database has been fully synchronized.")
                            del user_state[uid]
                        else:
                            await msg.edit("❌ **Restoration Failed:** An error occurred during database synchronization.")
                except Exception as e:
                    logger.error(f"Restore Error: {e}")
                    await msg.edit(f"❌ **Restoration Failed:** `{str(e)}`")
                finally:
                    if path and os.path.exists(path):
                        os.remove(path)
            elif action == "ask_new_poster":
                aid = state["slug"]
                if await db.ping():
                    res = await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"image": message.text.strip()}, "$currentDate": {"updated_at": True}})
                    await message.reply("✅ **Visual Synchronized.**" if res.modified_count else "❌ **Update Failed.**")
                else:
                    await message.reply("❌ Database Offline")
                del user_state[uid]
            elif action == "ask_edit_group_name":
                gname = state.get("group_name") or message.text.strip()
                user_state[uid].update({"action": "ask_group_pos", "group_name": gname})
                buttons = [
                    [InlineKeyboardButton("🔝 Beginning", callback_data="setposg_0", style=ButtonStyle.PRIMARY), InlineKeyboardButton("🔚 End", callback_data="setposg_-1", style=ButtonStyle.PRIMARY)],
                    [InlineKeyboardButton("🎯 Select Position", callback_data="setposg_select", style=ButtonStyle.PRIMARY)]
                ]
                await message.reply(f"📍 **Position for group '{gname}':**", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_edit_480p":
                user_state[uid]["480p"] = message.text if message.text != "/skip" else None
                user_state[uid]["action"] = "ask_edit_720p"
                await message.reply(f"🛰 **720p Link** (or /skip):")
            elif action == "ask_edit_720p":
                user_state[uid]["720p"] = message.text if message.text != "/skip" else None
                user_state[uid]["action"] = "ask_edit_1080p"
                await message.reply(f"🛰 **1080p Link** (or /skip):")
            elif action == "ask_edit_1080p":
                aid = state["slug"]
                anime = await db.get_anime(aid)
                current_groups = list(anime.get("seasons_links", {}).items()) if anime else []
                new_group_data = (state["group_name"], {"480p": state.get("480p"), "720p": state.get("720p"), "1080p": message.text if message.text != "/skip" else None})

                pos = state.get("insert_pos", -1)
                if pos == -1: current_groups.append(new_group_data)
                else: current_groups.insert(pos, new_group_data)

                new_links = dict(current_groups)
                if await db.ping():
                    await db.anime.update_one(
                        {"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid},
                        {
                            "$set": {
                                "seasons_links": new_links,
                                "newly_added_groups": [state["group_name"]]
                            },
                            "$currentDate": {"updated_at": True}
                        }
                    )
                    await message.reply(f"💎 **Success!** Group synchronized.")
                else:
                    await message.reply("❌ Database Offline")
                del user_state[uid]
            elif action == "ask_edit_btn_name":
                user_state[uid].update({"action": "ask_edit_btn_link", "new_name": message.text.strip()})
                await message.reply(f"🔗 **New URL for '{message.text.strip()}':**\n*(or `/skip` to keep current)*")
            elif action == "ask_edit_btn_link":
                idx, aid = state["btn_idx"], state["slug"]
                anime = await db.get_anime(aid)
                if anime:
                    btns = anime.get("custom_buttons", [])
                    if idx < len(btns):
                        btns[idx]['name'] = state["new_name"]
                        if message.text != "/skip": btns[idx]['link'] = message.text.strip()
                        await db.anime.update_one({"_id": ObjectId(aid)} if ObjectId.is_valid(aid) else {"slug": aid}, {"$set": {"custom_buttons": btns}, "$currentDate": {"updated_at": True}})
                        await message.reply(f"✅ **Button Updated.**")
                    else: await message.reply("❌ Error.")
                del user_state[uid]
            elif action == "select_anime":
                try:
                    selected = search_results[uid][int(message.text) - 1]
                    details = await anime_api.get_details(selected["source"], selected["id"])
                    if not details: details = {"title": selected["title"], "image": selected["image"], "synopsis": "N/A", "score": 0, "genres": [], "year": selected["year"], "status": "N/A", "episodes": 0, "trailer": None}
                    user_state[uid].update({"action": "edit_title", "anime_data": details})
                    await message.reply(f"✨ **Intelligence Step 1: Title**\n🏷 Current: `{details['title']}`\n\nSend **New Title** or `/skip`:")
                except: await message.reply("❌ **Invalid selection.**")
            elif action == "edit_title":
                if message.text != "/skip": user_state[uid]["anime_data"]["title"] = message.text
                user_state[uid]["action"] = "edit_synopsis"
                await message.reply(f"✨ **Step 2: Synopsis**\n📥 Send **New Synopsis** or `/skip`:")
            elif action == "edit_synopsis":
                if message.text != "/skip": user_state[uid]["anime_data"]["synopsis"] = message.text
                user_state[uid]["action"] = "edit_score"
                await message.reply(f"✨ **Step 3: Rating**\n📥 Send **New Rating** or `/skip`:")
            elif action == "edit_score":
                if message.text != "/skip":
                    try: user_state[uid]["anime_data"]["score"] = float(message.text)
                    except: pass
                user_state[uid]["action"] = "ask_image_choice"
                buttons = [[InlineKeyboardButton("🖼 intelligence Poster", callback_data="img_api", style=ButtonStyle.PRIMARY)], [InlineKeyboardButton("🔗 Manual Asset", callback_data="img_manual", style=ButtonStyle.PRIMARY)]]
                await message.reply_photo(photo=user_state[uid]["anime_data"]["image"] or Config.LOGO_URL, caption="✨ **Step 4: Visual Selection**", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_manual_img":
                user_state[uid]["image"] = None if message.text.strip() == "/skip" else message.text.strip()
                user_state[uid]["seasons_data"] = {}
                user_state[uid]["action"] = "ask_category_final"
                cats = await db.get_all_categories()
                buttons = [[InlineKeyboardButton(c['name'], callback_data=f"finalcat_{c['name']}", style=ButtonStyle.PRIMARY)] for c in (cats if cats else [{"name": "Anime"}])]
                await message.reply("✅ **Poster Registered.**\n\n📂 Choose a category to publish.\n\n📦 Groups and boxes can be added later by admins using `/edit` or `/edit_m`.", reply_markup=InlineKeyboardMarkup(buttons))
            elif action == "ask_seasons":
                groups = [s.strip() for s in message.text.split(",")]
                user_state[uid].update({"seasons_list": groups, "current_season_idx": 0, "seasons_data": {}, "action": f"ask_480p_{groups[0]}"})
                await message.reply(f"📦 **Architecting: {groups[0]}**\n\n🛰 **480p Link** or `/skip`:")
            elif "ask_480p_" in action:
                g = action.split("ask_480p_")[-1]
                user_state[uid]["seasons_data"][g] = {"480p": message.text if message.text != "/skip" else None}
                user_state[uid]["action"] = f"ask_720p_{g}"
                await message.reply(f"🛰 **720p Link** (or /skip):")
            elif "ask_720p_" in action:
                g = action.split("ask_720p_")[-1]
                user_state[uid]["seasons_data"][g]["720p"] = message.text if message.text != "/skip" else None
                user_state[uid]["action"] = f"ask_1080p_{g}"
                await message.reply(f"🛰 **1080p Link** (or /skip):")
            elif "ask_1080p_" in action:
                g = action.split("ask_1080p_")[-1]
                user_state[uid]["seasons_data"][g]["1080p"] = message.text if message.text != "/skip" else None
                user_state[uid]["current_season_idx"] += 1
                if user_state[uid]["current_season_idx"] < len(user_state[uid]["seasons_list"]):
                    next_s = user_state[uid]["seasons_list"][user_state[uid]["current_season_idx"]]
                    user_state[uid]["action"] = f"ask_480p_{next_s}"
                    await message.reply(f"📦 **Architecting: {next_s}**\n\n🛰 **480p Link** or `/skip`:")
                else:
                    user_state[uid]["action"] = "ask_category_final"
                    cats = await db.get_all_categories()
                    buttons = [[InlineKeyboardButton(c['name'], callback_data=f"finalcat_{c['name']}", style=ButtonStyle.PRIMARY)] for c in (cats if cats else [{"name": "Anime"}])]
                    await message.reply("🛰 **Aggregation Complete.**\nTarget **Category**:", reply_markup=InlineKeyboardMarkup(buttons))
        except Exception as e:
            logger.error(f"Interaction Error: {e}\n{traceback.format_exc()}")
            await message.reply(f"❌ **System Error:** `{str(e)}`")
            user_state.pop(uid, None)
