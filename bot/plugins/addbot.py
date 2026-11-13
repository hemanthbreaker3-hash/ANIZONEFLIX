import asyncio
import logging
import re
from pyrogram import Client, filters
from pyrogram.types import Message
from config.config import Config
from database.db import db
from bot.bot_manager import added_bot_manager

logger = logging.getLogger("ANIZONEFLIX_BOT_PLUGINS_ADMIN_ADDBOT")

def validate_bot_token(token: str) -> bool:
    """Validate format of a Bot Token (flexible secret length)"""
    pattern = r"^\d+:[A-Za-z0-9_-]+$"
    return bool(re.match(pattern, token))

@Client.on_message(filters.command("addbot") & filters.private)
async def addbot_command_handler(client: Client, message: Message):
    if not message.from_user:
        return

    # Step 1: Use the same normalized admin check as every other command.
    from bot import is_authorized
    if not await is_authorized(message.from_user.id):
        await message.reply("🚫 **Access Denied.** This command is reserved for Bot Administrators.")
        return

    args = message.text.split(None, 1)
    token = ""
    if len(args) > 1:
        token = args[1].strip()
    elif message.reply_to_message:
        token = (message.reply_to_message.text or "").strip()

    if not token:
        await message.reply("💡 **Usage:** `/addbot <BOT_TOKEN>` or reply to a BOT_TOKEN with `/addbot`")
        return

    # Validate token format
    if not validate_bot_token(token):
        await message.reply("❌ **Invalid Token Format.** Please verify your Telegram Bot Token.")
        return

    # Verify token using Telegram Bot API (or by trying to start a test pyrogram client/getMe)
    logger.info(f"Validating bot token with Telegram...")
    status_msg = await message.reply("⏳ **Validating token with Telegram...**")

    bot_info = None
    try:
        import httpx
        async with httpx.AsyncClient(timeout=15.0) as http_client:
            res = await http_client.get(f"https://api.telegram.org/bot{token}/getMe")
            if res.status_code != 200:
                err_desc = res.json().get("description", "Invalid Bot Token")
                await status_msg.edit(f"❌ **Token Verification Failed:** {err_desc}")
                return
            res_data = res.json().get("result", {})
            bot_info = type("BotInfo", (), {
                "id": res_data.get("id"),
                "first_name": res_data.get("first_name"),
                "username": res_data.get("username")
            })()
    except Exception as e:
        logger.error(f"Token validation failed: {e}")
        await status_msg.edit(f"❌ **Token Verification Failed:** {e}")
        try:
            from bot.bot_manager import report_addbot_issue
            await report_addbot_issue(token[:15] + "...", f"Validation error during /addbot setup: {str(e)}")
        except: pass
        return

    # Step 2: Bot asks "Send the Auto Group ID."
    await status_msg.delete()

    # Prompt the admin to enter the Auto Group ID.
    prompt = await message.reply(
        f"🤖 **Bot Recognized:** @{bot_info.username}\n\n"
        "👉 **Step 2:** Send the **Auto Group ID** (numeric chat ID, e.g. `-1001234567890`) or **Group Username** (e.g. `@mygroup`):"
    )

    try:
        # Await next message from user using interactive flow
        # In this project, user_state is typically used for simple state machine since pyromod handles callbacks,
        # but let's register the input flow using user_state in plugins/admin.py / interaction_handler.
        # Let's save intermediate state in user_state.
        from bot import user_state
        user_state[message.from_user.id] = {
            "action": "addbot_await_group",
            "token": token,
            "bot_info": {
                "id": bot_info.id,
                "first_name": bot_info.first_name,
                "username": bot_info.username
            }
        }
    except Exception as e:
        logger.error(f"Error starting addbot state flow: {e}")
        await message.reply("❌ Failed to initiate state flow.")
        try:
            from bot.bot_manager import report_addbot_issue
            await report_addbot_issue(f"User ID {message.from_user.id}", f"State flow initiation failure: {str(e)}")
        except: pass
