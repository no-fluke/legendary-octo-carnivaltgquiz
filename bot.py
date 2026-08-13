import asyncio
import os
import random
import re
from pathlib import Path
from typing import Optional, Tuple

import aiohttp
from aiohttp import web

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode

from telethon import TelegramClient, functions
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaPoll, MessageMediaPhoto, MessageMediaDocument
)
from telethon.errors import SessionPasswordNeededError, rpcerrorlist

from db import (
    get_user, set_user_field, set_user_fields, close_db,
    add_channel, remove_channel, get_channels, get_channel,
    save_job, get_job, clear_job,
)

# ======================== ENV ============================
BOT_TOKEN = os.getenv("BOT_TOKEN")
API_ID    = os.getenv("TELEGRAM_API_ID")
API_HASH  = os.getenv("TELEGRAM_API_HASH")

if not all([BOT_TOKEN, API_ID, API_HASH]):
    raise RuntimeError("Missing env vars: BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH")

API_ID = int(API_ID)

# ======================== PATHS ==========================
IMAGE_DIR = "quiz_images"
os.makedirs(IMAGE_DIR, exist_ok=True)

# ======================== DELAYS =========================
SEND_DELAY    = 3.0
VOTE_DELAY    = 2.5
SEGMENT_SIZE  = 40       # quizzes per /resume segment — safe under Render's 30 min timeout
BATCH_PAUSE   = 8.0      # short pause between every 10 quizzes within a segment

# Self-ping to keep Render free tier alive
RENDER_URL    = os.getenv("RENDER_EXTERNAL_URL", "")   # set this in Render env vars
PING_INTERVAL = 840                                     # every 14 min (Render sleeps at 15)
PING_PORT     = int(os.getenv("PORT", "10000"))

# ================== TELETHON SESSION HELPER ==============

async def get_client(user_id: str) -> Optional[TelegramClient]:
    """
    Build a TelegramClient from the session string stored in MongoDB.
    Returns None if no session exists yet.
    """
    user_doc = await get_user(user_id)
    session_str = user_doc.get("session_string", "")
    client = TelegramClient(StringSession(session_str), API_ID, API_HASH)
    await client.connect()
    return client


async def save_session(user_id: str, client: TelegramClient):
    """Save the current session string back to MongoDB."""
    session_str = client.session.save()
    await set_user_field(user_id, "session_string", session_str)


# ==================== CONVERSATION STATES =================
LOGIN_PHONE, LOGIN_OTP, LOGIN_2FA = range(3)
SCRAPE_START_LINK, SCRAPE_END_LINK, SCRAPE_DEST = range(3, 6)
ADD_CHANNEL_INPUT = 6
REMOVE_CHANNEL_INPUT = 7

# ======================== HELPERS ========================

def clean_text(text: str) -> str:
    text = re.sub(r'```[\s\S]*?```', lambda m: m.group(0).replace('`', ''), text)
    text = re.sub(r'\*\*(.+?)\*\*', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'__(.+?)__',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\*(.+?)\*',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'_(.+?)_',       r'\1', text, flags=re.DOTALL)
    text = re.sub(r'~~(.+?)~~',     r'\1', text, flags=re.DOTALL)
    text = re.sub(r'\|\|(.+?)\|\|', r'\1', text, flags=re.DOTALL)
    text = re.sub(r'`(.+?)`',       r'\1', text, flags=re.DOTALL)
    return text


def parse_private_link(link: str) -> Optional[Tuple[int, int]]:
    link = link.strip()
    m = re.search(r"t\.me/c/(\d+)/\d+/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"t\.me/c/(\d+)/(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    m = re.search(r"channel=(\d+)&post=(\d+)", link)
    if m:
        return int("-100" + m.group(1)), int(m.group(2))
    return None


def escape_md(text: str) -> str:
    special = r"\_*[]()~`>#+-=|{}.!"
    return re.sub(f"([{re.escape(special)}])", r"\\\1", str(text))


OPTION_LETTERS = ["A", "B", "C", "D", "E", "F"]


def build_bot_caption(quiz: dict, number: int) -> str:
    lines = [f"📋 *Quiz \\#{number}*", ""]
    lines.append(f"*Q: {escape_md(quiz['question'])}*")
    lines.append("")
    correct = quiz["correct_answer_index"]
    for ans in quiz["answers"]:
        letter = OPTION_LETTERS[ans["index"]] if ans["index"] < len(OPTION_LETTERS) else str(ans["index"] + 1)
        text = escape_md(ans["text"])
        if correct is not None and ans["index"] == correct:
            lines.append(f"✅ *{letter}\\. {text}*")
        else:
            lines.append(f"❌ {letter}\\. {text}")
    if quiz.get("explanation"):
        lines += ["", f"💡 _{escape_md(quiz['explanation'])}_"]
    if quiz.get("auto_voted"):
        lines += ["", "_\\(answer revealed via auto\\-vote\\)_"]
    return "\n".join(lines)


# =================== BOT COMMANDS ========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *Quiz Scraper Bot*\n\n"
        "Commands:\n"
        "• /login — log in with your Telegram account\n"
        "• /scrape — scrape quizzes from a channel range\n"
        "• /addchannel — save a private channel\n"
        "• /channels — list your saved channels\n"
        "• /removechannel — remove a saved channel\n"
        "• /set\\_destination — where to send results\n"
        "• /status — show login status\n"
        "• /cancel — cancel current operation",
        parse_mode=ParseMode.MARKDOWN
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.clear()
    await update.message.reply_text("❌ Cancelled.")


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    if user_doc.get("session_string"):
        try:
            client = await get_client(user_id)
            if await client.is_user_authorized():
                me = await client.get_me()
                await client.disconnect()
                await update.message.reply_text(
                    f"✅ Logged in as {me.first_name} (@{me.username})"
                )
                return
            await client.disconnect()
        except Exception:
            pass
    await update.message.reply_text("❌ Not logged in. Use /login to authenticate.")


# ---------------- LOGIN CONVERSATION --------------------

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    phone = user_doc.get("phone_number")
    if phone:
        await update.message.reply_text(
            f"Saved phone: `{phone}`\n"
            "Send a new number or type `yes` to reuse it.\n/cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "Send your phone in international format, e.g. `+919876543210`.\n/cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
    return LOGIN_PHONE


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text.strip()
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    saved_phone = user_doc.get("phone_number")

    if text.lower() == "yes" and saved_phone:
        phone = saved_phone
    elif re.match(r"^\+\d+$", text):
        phone = text
    else:
        await update.message.reply_text("❌ Invalid. Use +1234567890 or 'yes'.")
        return LOGIN_PHONE

    client = await get_client(user_id)

    if await client.is_user_authorized():
        await update.message.reply_text("✅ Already logged in.")
        await client.disconnect()
        return ConversationHandler.END

    try:
        await client.send_code_request(phone)
        context.user_data["login_client"] = client
        context.user_data["login_phone"]  = phone
        await set_user_field(user_id, "phone_number", phone)
        await update.message.reply_text("📲 OTP sent. Please send the numeric code.")
        return LOGIN_OTP
    except Exception as e:
        await client.disconnect()
        await update.message.reply_text(f"❌ Failed to send OTP: {e}")
        return ConversationHandler.END


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    otp = update.message.text.strip()
    if not otp.isdigit():
        await update.message.reply_text("❌ Send a numeric OTP.")
        return LOGIN_OTP

    client = context.user_data.get("login_client")
    if not client:
        await update.message.reply_text("❌ Session expired. /login again.")
        return ConversationHandler.END

    try:
        await client.sign_in(context.user_data["login_phone"], otp)
        me = await client.get_me()
        await save_session(user_id, client)
        await client.disconnect()
        context.user_data.pop("login_client", None)
        await update.message.reply_text(f"✅ Logged in as {me.first_name} (@{me.username})\nSession saved to MongoDB.")
        return ConversationHandler.END
    except SessionPasswordNeededError:
        await update.message.reply_text("🔐 2FA enabled. Send your 2FA password.")
        return LOGIN_2FA
    except Exception as e:
        await update.message.reply_text(f"❌ OTP error: {e}. Try again or /cancel.")
        return LOGIN_OTP


async def login_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    password = update.message.text.strip()
    client = context.user_data.get("login_client")
    if not client:
        await update.message.reply_text("❌ Session expired. /login again.")
        return ConversationHandler.END

    try:
        await client.sign_in(password=password)
        me = await client.get_me()
        await save_session(user_id, client)
        await client.disconnect()
        context.user_data.pop("login_client", None)
        await update.message.reply_text(f"✅ Logged in as {me.first_name} (@{me.username})\nSession saved to MongoDB.")
        return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ 2FA error: {e}. Try again or /cancel.")
        return LOGIN_2FA


# ---------------- CHANNEL MANAGEMENT --------------------

async def add_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📡 *Add a Channel*\n\n"
        "Paste any message link from the private channel you want to save.\n"
        "Format: `https://t.me/c/1234567890/42`\n\n"
        "I'll resolve the channel name automatically.\n/cancel to abort.",
        parse_mode=ParseMode.MARKDOWN
    )
    return ADD_CHANNEL_INPUT


async def add_channel_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()

    parsed = parse_private_link(text)
    if not parsed:
        await update.message.reply_text(
            "❌ Could not parse that link. Paste a valid `t.me/c/...` link or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ADD_CHANNEL_INPUT

    channel_id = parsed[0]

    # Check if already saved
    existing = await get_channel(user_id, channel_id)
    if existing:
        await update.message.reply_text(
            f"ℹ️ Channel already saved as *{existing['title']}*.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    # Try to resolve the channel name via Telethon
    user_doc = await get_user(user_id)
    if not user_doc.get("session_string"):
        await update.message.reply_text(
            "❌ You need to /login first so I can verify channel access."
        )
        return ConversationHandler.END

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            await client.disconnect()
            await update.message.reply_text("❌ Session expired. Please /login again.")
            return ConversationHandler.END
        entity = await client.get_entity(channel_id)
        title  = getattr(entity, "title", str(channel_id))
        await client.disconnect()
    except Exception as e:
        await update.message.reply_text(
            f"❌ Could not access channel: {e}\nMake sure you're a member."
        )
        return ConversationHandler.END

    await add_channel(user_id, channel_id, title, link=text)
    await update.message.reply_text(
        f"✅ Channel *{title}* saved!\n`channel_id: {channel_id}`",
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END


async def list_channels(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    channels = await get_channels(user_id)

    if not channels:
        await update.message.reply_text(
            "📭 No saved channels yet. Use /addchannel to add one."
        )
        return

    lines = ["📡 *Your Saved Channels*\n"]
    for i, ch in enumerate(channels, 1):
        lines.append(f"{i}\\. *{escape_md(ch['title'])}*")
        lines.append(f"   ID: `{ch['channel_id']}`")
    lines.append("\nUse /removechannel to remove one.")
    await update.message.reply_text(
        "\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2
    )


async def remove_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    channels = await get_channels(user_id)

    if not channels:
        await update.message.reply_text("📭 No saved channels to remove.")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(ch["title"], callback_data=f"rmch_{ch['channel_id']}")]
        for ch in channels
    ]
    keyboard.append([InlineKeyboardButton("❌ Cancel", callback_data="rmch_cancel")])
    await update.message.reply_text(
        "Select a channel to remove:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return REMOVE_CHANNEL_INPUT


async def remove_channel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = str(query.from_user.id)
    data = query.data

    if data == "rmch_cancel":
        await query.edit_message_text("❌ Cancelled.")
        return ConversationHandler.END

    channel_id = int(data.replace("rmch_", ""))
    ch = await get_channel(user_id, channel_id)
    title = ch["title"] if ch else str(channel_id)
    await remove_channel(user_id, channel_id)
    await query.edit_message_text(f"🗑️ Removed channel *{escape_md(title)}*.", parse_mode=ParseMode.MARKDOWN_V2)
    return ConversationHandler.END


# ---------------- SET DESTINATION --------------------

async def set_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    current = user_doc.get("destination")
    if current:
        await update.message.reply_text(
            f"Current destination: `{current}`\nSend a new chat ID/@username, or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "Send the destination chat ID or @username.\nUse `me` for your private chat.",
            parse_mode=ParseMode.MARKDOWN
        )
    return SCRAPE_DEST


async def dest_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    text = update.message.text.strip()
    if text.lower() == "me":
        dest = update.effective_user.id
    elif re.match(r"^-?\d+$", text):
        dest = int(text)
    else:
        dest = text if text.startswith("@") else f"@{text}"
    await set_user_field(user_id, "destination", dest)
    await update.message.reply_text(f"✅ Destination set to `{dest}`.", parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END


# ---------------- SCRAPE CONVERSATION --------------------

async def scrape_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    if not user_doc.get("session_string"):
        await update.message.reply_text("❌ Not logged in. Use /login first.")
        return ConversationHandler.END

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            await client.disconnect()
            await update.message.reply_text("❌ Session expired. /login again.")
            return ConversationHandler.END
        await client.disconnect()
    except Exception:
        await update.message.reply_text("❌ Session error. /login again.")
        return ConversationHandler.END

    # Show saved channels as quick-pick buttons
    channels = await get_channels(user_id)
    context.user_data["scrape_user_id"] = user_id

    if channels:
        keyboard = [
            [InlineKeyboardButton(ch["title"], callback_data=f"chan_{ch['channel_id']}")]
            for ch in channels
        ]
        keyboard.append([InlineKeyboardButton("🔗 Paste a link instead", callback_data="chan_manual")])
        await update.message.reply_text(
            "📡 *Pick a saved channel or paste a link:*",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode=ParseMode.MARKDOWN
        )
    else:
        await update.message.reply_text(
            "📎 Paste the *start message link* (first quiz).\n"
            "Format: `https://t.me/c/1234567890/42`\n/cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
    return SCRAPE_START_LINK


async def scrape_channel_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Inline button handler when user picks a saved channel during scrape."""
    query = update.callback_query
    await query.answer()
    data = query.data

    if data == "chan_manual":
        await query.edit_message_text(
            "📎 Paste the *start message link* (first quiz).\n"
            "Format: `https://t.me/c/1234567890/42`\n/cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_START_LINK

    channel_id = int(data.replace("chan_", ""))
    context.user_data["channel_id"] = channel_id
    await query.edit_message_text(
        f"✅ Channel selected (`{channel_id}`)\n\n"
        "Now paste the *start message link*.",
        parse_mode=ParseMode.MARKDOWN
    )
    return SCRAPE_START_LINK


async def scrape_start_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    parsed = parse_private_link(link)
    if not parsed:
        await update.message.reply_text("❌ Could not parse link. Try a valid `t.me/c/...` link.")
        return SCRAPE_START_LINK
    context.user_data["channel_id"] = parsed[0]
    context.user_data["start_id"]   = parsed[1]
    await update.message.reply_text(
        f"✅ Start msg: `{parsed[1]}`\nNow paste the *end message link*.",
        parse_mode=ParseMode.MARKDOWN
    )
    return SCRAPE_END_LINK


async def scrape_end_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # If channel was already set (from saved channel pick), allow start link to also set start_id
    if "start_id" not in context.user_data:
        # First message after channel pick is the start link
        return await scrape_start_link(update, context)

    link = update.message.text.strip()
    parsed = parse_private_link(link)
    if not parsed:
        await update.message.reply_text("❌ Could not parse end link.")
        return SCRAPE_END_LINK

    channel_id = context.user_data["channel_id"]
    if parsed[0] != channel_id:
        await update.message.reply_text("❌ End link is from a different channel.")
        return SCRAPE_END_LINK

    end_id   = parsed[1]
    start_id = context.user_data["start_id"]
    if end_id < start_id:
        await update.message.reply_text("❌ End ID must be >= start ID.")
        return SCRAPE_END_LINK

    context.user_data["end_id"] = end_id
    await update.message.reply_text(
        "Where to send results?\nSend `me`, a chat ID, @username, or `skip` to use saved destination.\n/cancel to abort."
    )
    return SCRAPE_DEST


async def scrape_dest(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = context.user_data["scrape_user_id"]
    user_doc = await get_user(user_id)
    text     = update.message.text.strip()

    if text.lower() == "skip":
        dest = user_doc.get("destination", update.effective_user.id)
    elif text.lower() == "me":
        dest = update.effective_user.id
    elif re.match(r"^-?\d+$", text):
        dest = int(text)
    else:
        dest = text if text.startswith("@") else f"@{text}"

    await set_user_field(user_id, "destination", dest)
    await update.message.reply_text(
        f"🔄 Starting scrape…\n"
        f"Channel: `{context.user_data['channel_id']}`\n"
        f"Range: {context.user_data['start_id']} → {context.user_data['end_id']}\n"
        f"Destination: `{dest}`\n\nThis may take a while.",
        parse_mode=ParseMode.MARKDOWN
    )
    asyncio.create_task(run_scrape(update, context, dest))
    return ConversationHandler.END


# ==================== BACKGROUND SCRAPING =================

async def run_scrape(update: Update, context: ContextTypes.DEFAULT_TYPE, dest_chat_id):
    user_id    = context.user_data["scrape_user_id"]
    channel_id = context.user_data["channel_id"]
    start_id   = context.user_data["start_id"]
    end_id     = context.user_data["end_id"]

    await context.bot.send_message(chat_id=dest_chat_id, text="⏳ Scraping started…")

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            await context.bot.send_message(chat_id=dest_chat_id, text="❌ Session expired. /login again.")
            await client.disconnect()
            return

        entity = await client.get_entity(channel_id)
        title  = getattr(entity, "title", str(channel_id))

        msg_ids = list(range(start_id, end_id + 1))
        items   = []
        pending_image_path    = None
        pending_image_caption = ""

        BATCH = 100
        total_batches = (len(msg_ids) + BATCH - 1) // BATCH

        for batch_num, batch_start in enumerate(range(0, len(msg_ids), BATCH), 1):
            batch    = msg_ids[batch_start:batch_start + BATCH]
            messages = await client.get_messages(entity, ids=batch)
            messages = sorted([m for m in messages if m is not None], key=lambda m: m.id)

            for message in messages:
                # Plain text
                if not message.media and message.text and message.text.strip():
                    items.append({
                        "type":       "text",
                        "message_id": message.id,
                        "date":       message.date.isoformat(),
                        "text":       message.text,
                    })
                    continue

                # Poll / quiz
                if isinstance(message.media, MessageMediaPoll):
                    poll_data = parse_poll(message, caption=message.text or "")
                    if poll_data is None:
                        continue

                    if pending_image_path:
                        poll_data["image_path"]    = pending_image_path
                        poll_data["image_caption"] = pending_image_caption
                        pending_image_path    = None
                        pending_image_caption = ""

                    if is_closed(message.media):
                        poll_data = read_closed_results(message, poll_data)
                    elif is_unattempted(message.media):
                        poll_data = await auto_vote_and_reveal(client, entity, message, poll_data)

                    poll_data["type"] = "quiz"
                    items.append(poll_data)

                # Image
                elif isinstance(message.media, (MessageMediaPhoto, MessageMediaDocument)):
                    image_path = await download_image(client, message, message.id)
                    if image_path:
                        pending_image_path    = image_path
                        pending_image_caption = message.text or ""

        quizzes   = [x for x in items if x["type"] == "quiz"]
        text_msgs = [x for x in items if x["type"] == "text"]

        summary = (
            f"📊 *Scrape complete*\n"
            f"Channel: {escape_md(title)}\n"
            f"Range: {start_id} → {end_id}\n"
            f"Quizzes: {len(quizzes)}\n"
            f"Text messages: {len(text_msgs)}"
        )
        await context.bot.send_message(chat_id=dest_chat_id, text=summary, parse_mode=ParseMode.MARKDOWN_V2)
        await asyncio.sleep(SEND_DELAY)

        # Send in original channel order, pausing every QUIZ_BATCH quizzes
        quiz_counter      = 0
        quiz_in_batch     = 0
        total_quiz_count  = len([x for x in items if x["type"] == "quiz"])
        total_batches_out = (total_quiz_count + QUIZ_BATCH - 1) // QUIZ_BATCH

        for item in items:
            if item["type"] == "text":
                text = clean_text(item["text"].strip())
                if not text:
                    continue
                for chunk in [text[i:i+4000] for i in range(0, len(text), 4000)]:
                    try:
                        await context.bot.send_message(chat_id=dest_chat_id, text=chunk)
                    except Exception:
                        pass
                    await asyncio.sleep(SEND_DELAY)

            elif item["type"] == "quiz":
                quiz_counter  += 1
                quiz_in_batch += 1
                await recreate_quiz_poll(context.bot, item, dest_chat_id, quiz_counter)
                await asyncio.sleep(SEND_DELAY)

                # After every QUIZ_BATCH quizzes, pause and notify
                if quiz_in_batch >= QUIZ_BATCH and quiz_counter < total_quiz_count:
                    batch_num = quiz_counter // QUIZ_BATCH
                    await context.bot.send_message(
                        chat_id=dest_chat_id,
                        text=(
                            f"⏸️ *Batch {batch_num}/{total_batches_out} done*\n"
                            f"{quiz_counter}/{total_quiz_count} quizzes sent.\n"
                            f"Pausing {int(BATCH_PAUSE)}s to avoid flood limits…"
                        ),
                        parse_mode=ParseMode.MARKDOWN,
                    )
                    await asyncio.sleep(BATCH_PAUSE)
                    quiz_in_batch = 0

        await context.bot.send_message(chat_id=dest_chat_id, text="✅ All done!")
        await client.disconnect()

    except Exception as e:
        await context.bot.send_message(chat_id=dest_chat_id, text=f"❌ Error: {e}")
        if "client" in locals():
            await client.disconnect()


# ================== POLL HELPERS =================

def is_unattempted(media: MessageMediaPoll) -> bool:
    results = media.results
    if results is None:
        return True
    if results.results:
        return not any(getattr(r, "chosen", False) for r in results.results)
    return True


def is_closed(media: MessageMediaPoll) -> bool:
    return getattr(media.poll, "closed", False)


def read_closed_results(message, poll_data: dict) -> dict:
    media   = message.media
    poll    = media.poll
    results = media.results
    answers = []
    for i, answer in enumerate(poll.answers):
        text  = answer.text.text if hasattr(answer.text, "text") else str(answer.text)
        entry = {"index": i, "text": text, "option": answer.option}
        if results and results.results:
            for res in results.results:
                if res.option == answer.option:
                    entry["voters"] = res.voters
                    entry["chosen"] = getattr(res, "chosen", False)
                    break
        answers.append(entry)
    is_quiz = poll_data.get("is_quiz", False)
    correct = get_correct_index(poll, results) if is_quiz else get_max_votes_index(poll, results)
    poll_data["answers"]              = answers
    poll_data["correct_answer_index"] = correct
    poll_data["total_voters"]         = results.total_voters if results else None
    poll_data["explanation"]          = results.solution if results and getattr(results, "solution", None) else None
    poll_data["auto_voted"] = False
    poll_data["was_closed"] = True
    return poll_data


def get_correct_index(poll, results) -> Optional[int]:
    if results and results.results:
        for res in results.results:
            if getattr(res, "correct", False):
                for i, ans in enumerate(poll.answers):
                    if ans.option == res.option:
                        return i
    return None


def get_max_votes_index(poll, results) -> Optional[int]:
    if not results or not results.results:
        return None
    best_i, best_voters = None, -1
    for res in results.results:
        v = res.voters or 0
        if v > best_voters:
            best_voters = v
            for i, ans in enumerate(poll.answers):
                if ans.option == res.option:
                    best_i = i
                    break
    return best_i


def parse_poll(message, caption: str = "") -> Optional[dict]:
    media = message.media
    if not isinstance(media, MessageMediaPoll):
        return None
    poll    = media.poll
    results = media.results
    question_text = poll.question.text if hasattr(poll.question, "text") else str(poll.question)
    answers = []
    for i, answer in enumerate(poll.answers):
        answer_text = answer.text.text if hasattr(answer.text, "text") else str(answer.text)
        entry = {"index": i, "text": answer_text, "option": answer.option}
        if results and results.results:
            for res in results.results:
                if res.option == answer.option:
                    entry["voters"] = res.voters
                    entry["chosen"] = getattr(res, "chosen", False)
                    break
        answers.append(entry)
    return {
        "message_id":           message.id,
        "date":                 message.date.isoformat(),
        "question":             question_text,
        "is_quiz":              poll.quiz,
        "anonymous":            not getattr(poll, "public_voters", False),
        "multiple_choice":      getattr(poll, "multiple_choice", False),
        "total_voters":         results.total_voters if results else None,
        "answers":              answers,
        "correct_answer_index": get_correct_index(poll, results),
        "explanation":          results.solution if results and getattr(results, "solution", None) else None,
        "image_path":           None,
        "auto_voted":           False,
        "caption":              caption,
        "image_caption":        "",
    }


async def auto_vote_and_reveal(client, entity, message, poll_data: dict) -> dict:
    dummy   = [random.choice(message.media.poll.answers).option]
    is_quiz = poll_data.get("is_quiz", False)
    try:
        await client(functions.messages.SendVoteRequest(
            peer=entity, msg_id=message.id, options=dummy
        ))
    except rpcerrorlist.MessagePollClosedError:
        return poll_data
    except Exception:
        return poll_data

    await asyncio.sleep(random.uniform(2.0, 5.0))

    try:
        refreshed = await client.get_messages(entity, ids=message.id)
    except Exception:
        return poll_data

    if not refreshed or not isinstance(refreshed.media, MessageMediaPoll):
        return poll_data

    up   = refreshed.media.poll
    ures = refreshed.media.results
    updated_answers = []
    for i, answer in enumerate(up.answers):
        text  = answer.text.text if hasattr(answer.text, "text") else str(answer.text)
        entry = {"index": i, "text": text, "option": answer.option}
        if ures and ures.results:
            for res in ures.results:
                if res.option == answer.option:
                    entry["voters"] = res.voters
                    entry["chosen"] = getattr(res, "chosen", False)
                    break
        updated_answers.append(entry)

    correct = get_correct_index(up, ures) if is_quiz else get_max_votes_index(up, ures)
    poll_data["answers"]              = updated_answers
    poll_data["correct_answer_index"] = correct
    poll_data["total_voters"]         = ures.total_voters if ures else None
    poll_data["auto_voted"]           = True
    poll_data["explanation"]          = ures.solution if ures and getattr(ures, "solution", None) else None
    return poll_data


async def download_image(client, message, msg_id: int) -> Optional[str]:
    Path(IMAGE_DIR).mkdir(exist_ok=True)
    media = message.media
    if isinstance(media, MessageMediaPhoto):
        path = os.path.join(IMAGE_DIR, f"quiz_{msg_id}.jpg")
    elif isinstance(media, MessageMediaDocument):
        doc  = media.document
        ext  = ".jpg"
        mime = getattr(doc, "mime_type", "")
        for attr in doc.attributes:
            if hasattr(attr, "file_name") and attr.file_name:
                ext = Path(attr.file_name).suffix or ext
                break
        if "png"  in mime: ext = ".png"
        if "gif"  in mime: ext = ".gif"
        if "webp" in mime: ext = ".webp"
        path = os.path.join(IMAGE_DIR, f"quiz_{msg_id}{ext}")
    else:
        return None
    try:
        await client.download_media(message, file=path)
        return path
    except Exception:
        return None


async def recreate_quiz_poll(bot, quiz: dict, chat_id, number: int):
    """Improved version from polls3108: properly handles quiz vs regular poll."""
    correct       = quiz.get("correct_answer_index")
    answers       = quiz.get("answers", [])
    image_caption = quiz.get("image_caption", "")

    if correct is None or not answers:
        caption = build_bot_caption(quiz, number)
        img = quiz.get("image_path")
        try:
            if img and os.path.exists(img):
                with open(img, "rb") as f:
                    await bot.send_photo(
                        chat_id=chat_id, photo=f,
                        caption=image_caption[:1024] if image_caption else caption,
                        parse_mode=None if image_caption else ParseMode.MARKDOWN_V2
                    )
            else:
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pass
        return

    question     = quiz["question"][:300]
    option_texts = [ans["text"][:100] for ans in answers]
    explanation  = (quiz.get("explanation") or "")[:200]
    is_quiz_type = quiz.get("is_quiz", True)

    # Send image first if exists, poll replies to it
    reply_to_id = None
    img = quiz.get("image_path")
    if img and os.path.exists(img):
        try:
            with open(img, "rb") as f:
                sent = await bot.send_photo(
                    chat_id=chat_id, photo=f,
                    caption=image_caption[:1024] if image_caption else None,
                    parse_mode=None
                )
                reply_to_id = sent.message_id
            await asyncio.sleep(SEND_DELAY)
        except Exception:
            pass

    try:
        if is_quiz_type:
            await bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=option_texts,
                type="quiz",
                correct_option_ids=[correct],
                explanation=explanation or None,
                is_anonymous=True,
                reply_to_message_id=reply_to_id,
            )
        else:
            # Regular poll — no explanation field in Bot API, send as follow-up
            await bot.send_poll(
                chat_id=chat_id,
                question=question,
                options=option_texts,
                type="regular",
                is_anonymous=True,
                reply_to_message_id=reply_to_id,
            )
            if explanation:
                await asyncio.sleep(SEND_DELAY)
                winning = option_texts[correct] if correct is not None else "N/A"
                await bot.send_message(
                    chat_id=chat_id,
                    text=f"🎯 Top answer: {winning}\n\n💡 {explanation}"
                )
    except Exception:
        caption = build_bot_caption(quiz, number)
        try:
            if img and os.path.exists(img):
                with open(img, "rb") as f:
                    await bot.send_photo(chat_id=chat_id, photo=f,
                                         caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await bot.send_message(chat_id=chat_id, text=caption, parse_mode=ParseMode.MARKDOWN_V2)
        except Exception:
            pass


# ===================== SELF-PING =========================

async def health_handler(request):
    """Simple health endpoint so Render marks the service as live."""
    return web.Response(text="OK")


async def start_ping_server():
    """Start a minimal HTTP server on PING_PORT for Render health checks."""
    app_web = web.Application()
    app_web.router.add_get("/", health_handler)
    app_web.router.add_get("/health", health_handler)
    runner = web.AppRunner(app_web)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PING_PORT)
    await site.start()
    print(f"🌐 Health server running on port {PING_PORT}")


async def self_ping_loop():
    """Ping our own URL every PING_INTERVAL seconds to prevent Render sleep."""
    if not RENDER_URL:
        print("⚠️  RENDER_EXTERNAL_URL not set — self-ping disabled.")
        return
    url = RENDER_URL.rstrip("/") + "/health"
    print(f"🔁 Self-ping enabled → {url} every {PING_INTERVAL}s")
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(PING_INTERVAL)
            try:
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    print(f"  🏓 Ping → {resp.status}")
            except Exception as e:
                print(f"  ⚠️  Ping failed: {e}")


# ======================== MAIN ============================

def main():
    app = Application.builder().token(BOT_TOKEN).build()

    # Login
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("login", login_start)],
        states={
            LOGIN_PHONE: [MessageHandler(filters.TEXT & ~filters.COMMAND, login_phone)],
            LOGIN_OTP:   [MessageHandler(filters.TEXT & ~filters.COMMAND, login_otp)],
            LOGIN_2FA:   [MessageHandler(filters.TEXT & ~filters.COMMAND, login_2fa)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Add channel
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("addchannel", add_channel_start)],
        states={
            ADD_CHANNEL_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_channel_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Remove channel
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("removechannel", remove_channel_start)],
        states={
            REMOVE_CHANNEL_INPUT: [CallbackQueryHandler(remove_channel_callback, pattern=r"^rmch_")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Set destination
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("set_destination", set_destination)],
        states={
            SCRAPE_DEST: [MessageHandler(filters.TEXT & ~filters.COMMAND, dest_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Scrape
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("scrape", scrape_start)],
        states={
            SCRAPE_START_LINK: [
                CallbackQueryHandler(scrape_channel_pick, pattern=r"^chan_"),
                MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_start_link),
            ],
            SCRAPE_END_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_end_link)],
            SCRAPE_DEST:     [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_dest)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    app.add_handler(CommandHandler("start",   start))
    app.add_handler(CommandHandler("status",  status))
    app.add_handler(CommandHandler("channels", list_channels))
    app.add_handler(CommandHandler("cancel",  cancel))

    async def post_init(application):
        await start_ping_server()
        asyncio.create_task(self_ping_loop())

    app.post_init = post_init

    print("Bot is running...")
    try:
        app.run_polling()
    finally:
        import asyncio as _asyncio
        _asyncio.get_event_loop().run_until_complete(close_db())


if __name__ == "__main__":
    main()
