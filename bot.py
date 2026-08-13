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
from telethon.errors import (
    SessionPasswordNeededError,
    PhoneCodeInvalidError,
    PhoneCodeExpiredError,
    PhoneNumberInvalidError,
    PasswordHashInvalidError,
    rpcerrorlist,
)

from db import (
    add_channel, remove_channel, get_channels,
    get_user, set_user_field, set_user_fields, close_db,
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
QUIZ_BATCH    = SEGMENT_SIZE  # alias used in run_scrape batching
BATCH_PAUSE   = 8.0      # short pause between every 10 quizzes within a segment
AUTO_VOTE     = True          # cast dummy vote to reveal correct answer (same as polls3108)
OUTPUT_JSON   = "quiz_output.json"
OUTPUT_TXT    = "quiz_output.txt"

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
    user = update.effective_user
    first_name = user.first_name if user and user.first_name else "there"
    await update.message.reply_text(
        f"👋 *Welcome, {first_name}!*\n\n"
        "I'm your *Quiz Scraper Bot* — I scrape quizzes and polls from private Telegram channels "
        "and deliver them to any chat of your choice.\n\n"
        "━━━━━━━━━━━━━━━━━━\n"
        "🔐 *Account*\n"
        "• /login — connect your Telegram account\n"
        "• /logout — revoke your session\n"
        "• /status — check login status\n\n"
        "🚀 *Scraping*\n"
        "• /scrape — scrape quizzes from a message range\n"
        "• /set\\_destination — set where results are sent\n\n"
        "⚙️ *Other*\n"
        "• /cancel — cancel any ongoing operation\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "👉 New here? Start with /login to connect your account.",
        parse_mode=ParseMode.MARKDOWN
    )


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    context.user_data.clear()
    await _cleanup_login_state(user_id)
    await update.message.reply_text("❌ Cancelled.")


async def logout(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    if not user_doc.get("session_string"):
        await update.message.reply_text("❌ You are not logged in.")
        return

    # Revoke the Telethon session on Telegram's side
    try:
        client = await get_client(user_id)
        if await client.is_user_authorized():
            await client.log_out()
        else:
            await client.disconnect()
    except Exception:
        pass

    # Wipe session from DB
    await set_user_field(user_id, "session_string", "")
    await _cleanup_login_state(user_id)
    await update.message.reply_text(
        "✅ Logged out successfully.\n"
        "Your session has been revoked from Telegram.\n\n"
        "Use /login to log in again."
    )


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


# ---------------- LOGIN STATE (in-memory, like reference code) ----------------
# Structure: {user_id: {"step": "WAITING_PHONE"|"WAITING_CODE"|"WAITING_PASSWORD", "data": {...}}}
LOGIN_STATE = {}


async def _cleanup_login_state(user_id: str):
    """Disconnect any live client and remove state."""
    state = LOGIN_STATE.pop(user_id, None)
    if state:
        client = state.get("data", {}).get("client")
        if client:
            try:
                await client.disconnect()
            except Exception:
                pass


# ---------------- LOGIN CONVERSATION --------------------

async def login_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)

    # Already logged in?
    user_doc = await get_user(user_id)
    if user_doc.get("session_string"):
        try:
            client = await get_client(user_id)
            if await client.is_user_authorized():
                await client.disconnect()
                await update.message.reply_text(
                    "✅ You are already logged in.\n\nUse /status to check or /cancel to abort."
                )
                return ConversationHandler.END
            await client.disconnect()
        except Exception:
            pass

    # Clean up any previous half-finished login
    await _cleanup_login_state(user_id)
    LOGIN_STATE[user_id] = {"step": "WAITING_PHONE", "data": {}}

    await update.message.reply_text(
        "📱 *Login — Step 1 of 3*\n\n"
        "Send your phone number with country code.\n\n"
        "📎 Example: `+919876543210`\n\n"
        "Or /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN
    )
    return LOGIN_PHONE


async def login_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    phone = update.message.text.strip().replace(" ", "")

    if not re.match(r"^\+\d{7,15}$", phone):
        await update.message.reply_text("❌ Invalid format. Use +1234567890.")
        return LOGIN_PHONE

    await update.message.reply_text("📩 Sending OTP...")

    # Build a fresh in-memory client (never reuse an old one)
    client = TelegramClient(StringSession(), API_ID, API_HASH)
    await client.connect()

    try:
        sent = await client.send_code_request(phone)
        # Store everything in LOGIN_STATE — same pattern as reference code
        LOGIN_STATE[user_id] = {
            "step": "WAITING_CODE",
            "data": {
                "client": client,
                "phone":  phone,
                "hash":   sent.phone_code_hash,
            }
        }
        await set_user_field(user_id, "phone_number", phone)
        await update.message.reply_text(
            "📲 *Login — Step 2 of 3*\n\n"
            "A verification code has been sent to your Telegram app.\n\n"
            "Send it here — spaces are fine:\n`1 2 3 4 5` or `12345`\n\n"
            "Or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return LOGIN_OTP

    except PhoneNumberInvalidError:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text("❌ Phone number is invalid. Try again.")
        return LOGIN_PHONE
    except Exception as e:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text(f"❌ Failed to send OTP: {e}\n\nTry /login again.")
        return ConversationHandler.END


async def login_otp(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    otp = update.message.text.strip().replace(" ", "")

    if not otp.isdigit():
        await update.message.reply_text("❌ Send only the numeric code.")
        return LOGIN_OTP

    state = LOGIN_STATE.get(user_id)
    if not state or state["step"] != "WAITING_CODE":
        await update.message.reply_text("❌ Session lost. Please /login again.")
        return ConversationHandler.END

    client       = state["data"]["client"]
    phone        = state["data"]["phone"]
    phone_hash   = state["data"]["hash"]

    try:
        await client.sign_in(phone, otp, phone_code_hash=phone_hash)
        await _finalize_login(update, client, user_id)
        return ConversationHandler.END

    except PhoneCodeInvalidError:
        await update.message.reply_text("❌ OTP is incorrect. Try again.")
        return LOGIN_OTP  # Let them retry — don't clear state

    except PhoneCodeExpiredError:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text("❌ OTP expired. Please /login again.")
        return ConversationHandler.END

    except SessionPasswordNeededError:
        LOGIN_STATE[user_id]["step"] = "WAITING_PASSWORD"
        await update.message.reply_text(
            "🔐 *Login — Step 3 of 3*\n\n"
            "Two-step verification is enabled on your account.\n\n"
            "Please send your 2FA password.\n\n"
            "Or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return LOGIN_2FA

    except Exception as e:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text(f"❌ Error: {e}\n\nPlease /login again.")
        return ConversationHandler.END


async def login_2fa(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id  = str(update.effective_user.id)
    password = update.message.text.strip()

    state = LOGIN_STATE.get(user_id)
    if not state or state["step"] != "WAITING_PASSWORD":
        await update.message.reply_text("❌ Session lost. Please /login again.")
        return ConversationHandler.END

    client = state["data"]["client"]

    try:
        await client.sign_in(password=password)
        await _finalize_login(update, client, user_id)
        return ConversationHandler.END

    except PasswordHashInvalidError:
        await update.message.reply_text("❌ Wrong password. Try again.")
        return LOGIN_2FA

    except Exception as e:
        await client.disconnect()
        del LOGIN_STATE[user_id]
        await update.message.reply_text(f"❌ 2FA error: {e}\n\nPlease /login again.")
        return ConversationHandler.END


async def _finalize_login(update: Update, client: TelegramClient, user_id: str):
    """Save session and clean up — mirrors reference code's finalize_login."""
    try:
        me = await client.get_me()
        await save_session(user_id, client)
        await client.disconnect()
        LOGIN_STATE.pop(user_id, None)
        await update.message.reply_text(
            f"✅ *Logged in successfully!*\n\n"
            f"👤 Name: {me.first_name}\n"
            f"🔖 Username: @{me.username}\n\n"
            "Your session has been saved. You're ready to /scrape!\n\n"
            "_If you ever get an auth error, use /logout then /login again._",
            parse_mode=ParseMode.MARKDOWN
        )
    except Exception as e:
        LOGIN_STATE.pop(user_id, None)
        await update.message.reply_text(f"❌ Error finalizing login: {e}")





# ================================================================
# DESTINATION MANAGEMENT
#
# Destinations stored in MongoDB as:
#   "destinations": [{"label": "My Channel", "chat_id": "-100123"}, ...]
#
# /set_destination  → shows saved destinations as buttons.
#                     Tap a destination → detail view with Remove button.
#                     Tap ➕ → bot fetches your admin channels via Telethon
#                               and shows them as buttons to add.
# /scrape (dest)    → shows saved destinations as buttons to pick from.
# ================================================================

ADD_DEST_PICK  = 10   # conversation state: waiting for button presses
ADD_DEST_TYPED = 11   # conversation state: waiting for user to type a chat ID

_DEST_PREFIX  = "dest:"    # scrape picker: dest:<chat_id>
_DVIEW_PREFIX = "dview:"   # manage view: tap channel name → detail
_RDEST_PREFIX = "rdest:"   # remove: rdest:<chat_id>
_DADD_PREFIX  = "dadd:"    # add: dadd:<chat_id>:<label>  (label url-encoded)
_DADD_FETCH   = "dadd_fetch"  # button: type a chat ID manually


# ---------- DB helpers (backed by MongoDB channels collection) ----------

async def _get_destinations(user_id: str) -> list[dict]:
    """Return saved destinations as list of {label, chat_id} dicts."""
    channels = await get_channels(user_id)
    return [{"label": ch["title"], "chat_id": str(ch["channel_id"])} for ch in channels]


async def _add_destination(user_id: str, label: str, chat_id) -> list[dict]:
    """Save a destination to MongoDB channels collection."""
    await add_channel(user_id, int(chat_id), label)
    return await _get_destinations(user_id)


async def _remove_destination(user_id: str, chat_id) -> list[dict]:
    """Remove a destination from MongoDB channels collection."""
    await remove_channel(user_id, int(chat_id))
    return await _get_destinations(user_id)


# ---------- Keyboard builders ----------

def _manage_keyboard(dests: list[dict]) -> InlineKeyboardMarkup:
    """Main manage view: each destination as a tappable button + ➕ Add."""
    buttons = []
    for d in dests:
        buttons.append([
            InlineKeyboardButton(
                f"📢 {d['label']}",
                callback_data=f"{_DVIEW_PREFIX}{d['chat_id']}",
            )
        ])
    buttons.append([InlineKeyboardButton("➕ Add by Chat ID", callback_data=_DADD_FETCH)])
    return InlineKeyboardMarkup(buttons)


def _detail_keyboard(chat_id: str) -> InlineKeyboardMarkup:
    """Detail view for one destination: Remove + Back."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🗑 Remove", callback_data=f"{_RDEST_PREFIX}{chat_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="dback")],
    ])


def _pick_keyboard(dests: list[dict]) -> InlineKeyboardMarkup:
    """Scrape destination picker: one button per saved destination."""
    buttons = []
    for d in dests:
        buttons.append([
            InlineKeyboardButton(
                f"📢 {d['label']}",
                callback_data=f"{_DEST_PREFIX}{d['chat_id']}",
            )
        ])
    return InlineKeyboardMarkup(buttons)


# ---------- /set_destination — manage saved destinations ----------

async def set_destination(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    dests   = await _get_destinations(user_id)
    text    = (
        "📬 *Destinations*\n\nTap a destination to view or remove it."
        if dests else
        "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
    )
    await update.message.reply_text(
        text,
        reply_markup=_manage_keyboard(dests),
        parse_mode=ParseMode.MARKDOWN,
    )
    return ADD_DEST_PICK


async def manage_dest_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles all button presses inside the /set_destination conversation."""
    query   = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    data    = query.data

    # ── Tap a saved destination → show detail with Remove + Back ──
    if data.startswith(_DVIEW_PREFIX):
        chat_id = data[len(_DVIEW_PREFIX):]
        dests   = await _get_destinations(user_id)
        entry   = next((d for d in dests if str(d["chat_id"]) == chat_id), None)
        label   = entry["label"] if entry else chat_id
        await query.edit_message_text(
            f"📢 *{escape_md(label)}*\n`{chat_id}`",
            reply_markup=_detail_keyboard(chat_id),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ADD_DEST_PICK

    # ── Remove button on detail view ──
    if data.startswith(_RDEST_PREFIX):
        chat_id = data[len(_RDEST_PREFIX):]
        dests   = await _remove_destination(user_id, chat_id)
        text    = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await query.edit_message_text(
            text,
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_PICK

    # ── Back button on detail view ──
    if data == "dback":
        dests = await _get_destinations(user_id)
        text  = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await query.edit_message_text(
            text,
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_PICK

    # ── ➕ Add: ask user to type a chat ID or @username ──
    if data == _DADD_FETCH:
        await query.edit_message_text(
            "➕ *Add destination*\n\n"
            "You can add a destination in *two ways*:\n\n"
            "1️⃣ *Forward any message* from the channel/group to this chat — the bot will detect it automatically.\n\n"
            "2️⃣ *Type the ID or @username* manually:\n"
            "• `-1001234567890`\n"
            "• `@mychannelname`\n\n"
            "Or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_TYPED

    # ── Tap a channel in the add-list → save it ──
    if data.startswith(_DADD_PREFIX):
        payload   = data[len(_DADD_PREFIX):]
        # chat_id is everything before the first ':', label is the rest
        colon     = payload.index(":")
        chat_id   = payload[:colon]
        label     = payload[colon + 1:].replace("｜", ":")
        dests     = await _add_destination(user_id, label, chat_id)
        text      = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await query.edit_message_text(
            f"✅ *{escape_md(label)}* added\\!\n\n" + escape_md(text.split("\n\n", 1)[1]),
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ADD_DEST_PICK


async def add_dest_typed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User typed a chat ID/@username OR forwarded a message — resolve and save."""
    user_id = str(update.effective_user.id)
    msg     = update.message

    # ── Check for a forwarded message first ──
    fwd = msg.forward_origin if hasattr(msg, "forward_origin") else None
    # PTB v20+: forward_origin; also support legacy forward_from_chat
    fwd_chat = None
    if fwd is not None:
        # MessageOriginChannel / MessageOriginChat have .chat attribute
        fwd_chat = getattr(fwd, "chat", None)
    if fwd_chat is None:
        # Legacy fallback
        fwd_chat = getattr(msg, "forward_from_chat", None)

    if fwd_chat is not None:
        # We have a clean entity straight from the forwarded message.
        # PTB already gives the full signed ID (e.g. -1004460564772) so
        # we must NOT prepend -100 again — just use it as-is.
        raw_id    = fwd_chat.id
        chat_type = fwd_chat.type  # 'channel', 'supergroup', 'group', 'private'
        if chat_type in ("channel", "supergroup"):
            abs_str = str(abs(raw_id))
            # Ensure it carries the -100 prefix exactly once
            chat_id_str = "-" + (abs_str if abs_str.startswith("100") else "100" + abs_str)
        else:
            chat_id_str = str(raw_id)
        title = (getattr(fwd_chat, "title", None) or getattr(fwd_chat, "username", None) or chat_id_str)[:40]

        dests = await _add_destination(user_id, title, chat_id_str)
        text = (
            "📬 *Destinations*\n\nTap a destination to view or remove it."
            if dests else
            "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
        )
        await msg.reply_text(
            f"✅ *{escape_md(title)}* added\\!\n`{chat_id_str}`\n\n" + escape_md(text.split("\n\n", 1)[1]),
            reply_markup=_manage_keyboard(dests),
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return ConversationHandler.END

    # ── No forward — fall back to typed ID / @username ──
    raw_text = (msg.text or "").strip()
    if not raw_text:
        await msg.reply_text(
            "❌ Please forward a message from the channel, or type its ID / @username.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ADD_DEST_TYPED

    await msg.reply_text("🔍 Looking up the chat…")

    client = await get_client(user_id)
    if not await client.is_user_authorized():
        await client.disconnect()
        await update.message.reply_text(
            "❌ You're not logged in. Use /login first, then try /set\\_destination again.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    entity = None
    chat_id_str = None
    title = None

    # ── Strategy 1: try get_entity (works for @usernames and cached IDs) ──
    try:
        entity = await client.get_entity(raw_text)
    except Exception:
        pass

    # ── Strategy 2: numeric ID — try InputPeerChannel / InputPeerChat ──
    if entity is None:
        numeric = raw_text.lstrip("-")
        # Strip -100 prefix to get bare channel id
        if numeric.startswith("100") and len(numeric) > 10:
            bare_id = int(numeric[3:])
        else:
            bare_id = int(numeric) if numeric.isdigit() else None

        if bare_id is not None:
            from telethon.tl.types import InputPeerChannel, InputPeerChat
            # Try as a channel/supergroup first (most common for -100... IDs)
            try:
                from telethon.tl.functions.channels import GetFullChannelRequest
                peer = InputPeerChannel(channel_id=bare_id, access_hash=0)
                entity = await client.get_entity(peer)
            except Exception:
                pass

            # Try iterating dialogs to find the entity (slower but reliable)
            if entity is None:
                try:
                    target_id = int(raw_text)  # the full -100xxx number
                    async for dialog in client.iter_dialogs():
                        if dialog.id == target_id:
                            entity = dialog.entity
                            break
                except Exception:
                    pass

    await client.disconnect()

    # ── If still not found, save it as-is using the raw ID the user typed ──
    if entity is None:
        # Accept the ID directly if it looks like a valid Telegram chat ID
        stripped = raw_text.lstrip("-")
        if stripped.isdigit():
            chat_id_str = raw_text  # trust what the user typed
            title = raw_text[:40]
            await update.message.reply_text(
                f"⚠️ Could not auto-resolve the chat name (your account may not have joined it yet), "
                f"but the ID `{raw_text}` looks valid and has been saved.\n\n"
                "If sending fails later, make sure your account is a member of that chat.",
                parse_mode=ParseMode.MARKDOWN,
            )
        else:
            await update.message.reply_text(
                f"❌ Could not find that chat: `{raw_text}`\n\n"
                "Make sure the ID is correct and your account has access to that chat.\n\n"
                "Send another ID or /cancel to abort.",
                parse_mode=ParseMode.MARKDOWN,
            )
            return ADD_DEST_TYPED
    else:
        # Build the proper -100xxxxxxxxxx chat_id from the resolved entity
        raw_id     = entity.id
        is_channel = getattr(entity, "broadcast", False)
        is_mega    = getattr(entity, "megagroup",  False)
        is_giga    = getattr(entity, "gigagroup",  False)
        if is_channel or is_mega or is_giga:
            chat_id_str = str(int("-100" + str(raw_id)))
        else:
            chat_id_str = str(raw_id)
        title = (getattr(entity, "title", None) or getattr(entity, "username", None) or raw_text)[:40]

    dests = await _add_destination(user_id, title, chat_id_str)

    text = (
        "📬 *Destinations*\n\nTap a destination to view or remove it."
        if dests else
        "📬 *Destinations*\n\nNo destinations saved yet. Tap ➕ to add one."
    )
    await update.message.reply_text(
        f"✅ *{escape_md(title)}* added\\!\n\n" + escape_md(text.split("\n\n", 1)[1]),
        reply_markup=_manage_keyboard(dests),
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return ConversationHandler.END


# ---------- Scrape destination picker ----------

async def _show_scrape_dest_picker(update: Update, context: ContextTypes.DEFAULT_TYPE,
                                    user_id: str) -> bool:
    """Show numbered list of destinations. Returns False if none saved."""
    dests  = await _get_destinations(user_id)
    target = update.message

    # Always prepend "This chat" (bot private chat with the user) as option 1
    bot_chat = {"label": "🤖 This chat (bot)", "chat_id": str(update.effective_user.id)}
    full_list = [bot_chat] + list(dests)

    # Store full list in user_data so scrape_dest_number can read it
    context.user_data["dest_list"] = full_list

    lines = ["📬 *Where should the results be sent?*", ""]
    for i, d in enumerate(full_list, 1):
        lines.append(f"*{i}.* {d['label']}  (`{d['chat_id']}`)")
    lines += ["", "Reply with the *number* of your choice, or /cancel to abort."]

    await target.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.MARKDOWN,
    )
    return True


async def scrape_dest_number(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User replied with a number to pick a destination."""
    raw   = update.message.text.strip()
    dests = context.user_data.get("dest_list", [])

    start_id   = context.user_data.get("start_id")
    end_id     = context.user_data.get("end_id")
    channel_id = context.user_data.get("channel_id")

    if start_id is None or end_id is None or channel_id is None:
        await update.message.reply_text(
            "❌ *Session lost* — the bot may have restarted.\n\nPlease run /scrape again to start over.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return ConversationHandler.END

    if not raw.isdigit() or not (1 <= int(raw) <= len(dests)):
        await update.message.reply_text(
            f"❌ Please reply with a number between *1* and *{len(dests)}*.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return SCRAPE_DEST

    chosen = dests[int(raw) - 1]
    label  = chosen["label"]
    dest   = int(chosen["chat_id"])
    total  = end_id - start_id + 1

    await update.message.reply_text(
        f"✅ *Sending to:* {label}\n\n"
        "⏳ *Scrape started!*\n\n"
        f"📡 Channel: `{channel_id}`\n"
        f"📨 Range: `{start_id}` → `{end_id}` ({total} messages)\n\n"
        "I\'ll notify you here when it\'s done.",
        parse_mode=ParseMode.MARKDOWN,
    )
    task = asyncio.create_task(run_scrape(update, context, dest))
    # Track it so post_shutdown can cancel it cleanly
    scrape_tasks = context.application.bot_data.setdefault("scrape_tasks", set())
    scrape_tasks.add(task)
    task.add_done_callback(lambda t: scrape_tasks.discard(t))
    return ConversationHandler.END

# ---------------- SCRAPE CONVERSATION --------------------

async def scrape_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = str(update.effective_user.id)
    user_doc = await get_user(user_id)
    if not user_doc.get("session_string"):
        await update.message.reply_text(
            "❌ *Not logged in.*\n\nUse /login to connect your Telegram account first.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    try:
        client = await get_client(user_id)
        if not await client.is_user_authorized():
            await client.disconnect()
            await update.message.reply_text(
                "❌ *Session expired.*\n\nPlease /login again to refresh your session.",
                parse_mode=ParseMode.MARKDOWN
            )
            return ConversationHandler.END
        await client.disconnect()
    except Exception:
        await update.message.reply_text(
            "❌ *Session error.*\n\nPlease /login again.",
            parse_mode=ParseMode.MARKDOWN
        )
        return ConversationHandler.END

    context.user_data["scrape_user_id"] = user_id
    await update.message.reply_text(
        "🚀 *Scrape*\n\n"
        "Paste the *start message link* (first quiz in the range).\n\n"
        "📎 Example:\n`https://t.me/c/1234567890/42`\n\n"
        "Or /cancel to abort.",
        parse_mode=ParseMode.MARKDOWN
    )
    return SCRAPE_START_LINK


async def scrape_start_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    link = update.message.text.strip()
    parsed = parse_private_link(link)
    if not parsed:
        await update.message.reply_text(
            "❌ *Couldn't read that link.*\n\n"
            "Make sure it looks like:\n`https://t.me/c/1234567890/42`\n\n"
            "Try again or /cancel to abort.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_START_LINK
    context.user_data["channel_id"] = parsed[0]
    context.user_data["start_id"]   = parsed[1]
    await update.message.reply_text(
        f"✅ Start set \\(ID: `{parsed[1]}`\\)\n\n"
        "Now paste the *end message link* \\(last quiz in the range\\)\\.\n\n"
        "📎 Example:\n`https://t.me/c/1234567890/99`\n\n"
        "Or /cancel to abort\\.",
        parse_mode=ParseMode.MARKDOWN_V2
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
        await update.message.reply_text(
            "❌ *Couldn't read that link.*\n\n"
            "Make sure it looks like:\n`https://t.me/c/1234567890/99`\n\nTry again or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_END_LINK

    channel_id = context.user_data["channel_id"]
    if parsed[0] != channel_id:
        await update.message.reply_text(
            "❌ *Wrong channel.*\n\nThe end link must be from the same channel as the start link. Try again or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_END_LINK

    end_id   = parsed[1]
    start_id = context.user_data["start_id"]
    if end_id < start_id:
        await update.message.reply_text(
            "❌ *End must come after start.*\n\nThe end message ID must be greater than or equal to the start ID. Try again or /cancel.",
            parse_mode=ParseMode.MARKDOWN
        )
        return SCRAPE_END_LINK

    context.user_data["end_id"] = end_id
    user_id = context.user_data["scrape_user_id"]
    await _show_scrape_dest_picker(update, context, user_id)
    return SCRAPE_DEST





# ==================== FORMAT HELPERS (from polls3108) ===================

def format_quiz_text(quiz: dict, number: int) -> str:
    lines = [
        "="*60,
        f"Quiz #{number}  |  ID: {quiz['message_id']}  |  {quiz['date']}"
        + ("  [auto-voted]" if quiz.get("auto_voted") else ""),
        "="*60,
        f"Q: {quiz['question']}\n",
    ]
    for ans in quiz["answers"]:
        marker = ""
        if quiz["correct_answer_index"] is not None:
            marker = " ✅" if ans["index"] == quiz["correct_answer_index"] else " ❌"
        voters = f"  [{ans.get('voters','?')} votes]" if ans.get("voters") is not None else ""
        lines.append(f"  {ans['index']+1}. {ans['text']}{marker}{voters}")
    if quiz.get("explanation"):
        lines.append(f"\n💡 {quiz['explanation']}")
    if quiz.get("image_path"):
        lines.append(f"\n🖼️  Image: {quiz['image_path']}")
    if quiz.get("caption"):
        lines.append(f"\n📝 Caption: {quiz['caption']}")
    if quiz.get("image_caption"):
        lines.append(f"\n🖼️ Image caption: {quiz['image_caption']}")
    lines.append(
        f"\nType: {'Quiz' if quiz['is_quiz'] else 'Poll'} | "
        f"Total voters: {quiz['total_voters'] or 'N/A'}"
    )
    return "\n".join(lines)


async def send_text_messages(bot, text_msgs: list, chat_id, title: str):
    """
    Send collected plain-text messages to the destination chat.
    Each entry: {"message_id": int, "date": str, "text": str}
    """
    if not text_msgs:
        print("  ℹ️  No text messages found in this range.")
        return

    print(f"\n  📝  Forwarding {len(text_msgs)} text message(s)...\n")

    try:
        await bot.send_message(
            chat_id = chat_id,
            text    = f"📝 Text Messages — {title} — {len(text_msgs)} message(s)",
        )
    except Exception as e:
        print(f"  ⚠️  Header send failed: {e}")
    await asyncio.sleep(SEND_DELAY)

    for i, msg in enumerate(text_msgs, 1):
        text = clean_text(msg["text"].strip())
        if not text:
            continue
        chunks = [text[j:j+4000] for j in range(0, len(text), 4000)]
        for chunk in chunks:
            try:
                await bot.send_message(chat_id=chat_id, text=chunk)
            except Exception as e:
                print(f"  ❌  Text msg #{i} failed: {e}")
            await asyncio.sleep(SEND_DELAY)
        print(f"  ✉️  Sent text msg #{i}: \"{text[:60]}\"")

    try:
        await bot.send_message(
            chat_id    = chat_id,
            text       = f"✅ *Done\\! {len(text_msgs)} text message\\(s\\) forwarded\\.*",
            parse_mode = ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        print(f"  ⚠️  Footer send failed: {e}")


async def send_via_bot(bot, items: list, title: str, chat_id, recreate_polls: bool = True):
    """
    Send all items (quizzes + text messages) to the destination chat
    in the exact original channel order — same as polls3108 send_via_bot.
    """
    quizzes   = [x for x in items if x["type"] == "quiz"]
    text_msgs = [x for x in items if x["type"] == "text"]
    documents = [x for x in items if x["type"] == "document"]
    print(f"\n  🤖  Bot sending {len(items)} item(s) in original order "
          f"({len(quizzes)} quiz, {len(text_msgs)} text, {len(documents)} docs)...\n")

    try:
        await bot.send_message(
            chat_id    = chat_id,
            text       = (
                f"📚 *Quiz Export — {escape_md(title)}*\n"
                f"Quizzes: {len(quizzes)} \\| Text msgs: {len(text_msgs)}"
            ),
            parse_mode = ParseMode.MARKDOWN_V2,
        )
    except Exception as e:
        print(f"  ⚠️  Summary header failed: {e}")
    await asyncio.sleep(SEND_DELAY)

    quiz_counter = 0

    for item in items:
        if item["type"] == "text":
            text = clean_text(item["text"].strip())
            if not text:
                continue
            chunks = [text[j:j+4000] for j in range(0, len(text), 4000)]
            for chunk in chunks:
                try:
                    await bot.send_message(chat_id=chat_id, text=chunk)
                except Exception as e:
                    print(f"  ❌  Text msg #{item['message_id']} failed: {e}")
                await asyncio.sleep(SEND_DELAY)

        elif item["type"] == "quiz":
            quiz_counter += 1
            i = quiz_counter
            try:
                if recreate_polls:
                    await recreate_quiz_poll(bot, item, chat_id, i)
                else:
                    caption = build_bot_caption(item, i)
                    img     = item.get("image_path")
                    if img and os.path.exists(img):
                        with open(img, "rb") as f:
                            await bot.send_photo(
                                chat_id    = chat_id,
                                photo      = f,
                                caption    = caption,
                                parse_mode = ParseMode.MARKDOWN_V2,
                            )
                    else:
                        await bot.send_message(
                            chat_id    = chat_id,
                            text       = caption,
                            parse_mode = ParseMode.MARKDOWN_V2,
                        )
                    print(f"  ✉️  Sent quiz #{i}: \"{item['question'][:50]}\"")
            except Exception as e:
                print(f"  ⚠️  Failed quiz #{i}: {e} — trying plain text fallback")
                try:
                    plain = f"Quiz #{i}\nQ: {item['question']}\n"
                    for ans in item["answers"]:
                        mark = " ✅" if ans["index"] == item["correct_answer_index"] else " ❌"
                        plain += f"  {ans['index']+1}. {ans['text']}{mark}\n"
                    if item.get("explanation"):
                        plain += f"\n💡 {item['explanation']}"
                    await bot.send_message(chat_id=chat_id, text=plain)
                except Exception as e2:
                    print(f"  ❌  Fallback failed: {e2}")
            await asyncio.sleep(SEND_DELAY)

        elif item["type"] == "document":
            # Forward PDF / non-image document with a note
            caption = item.get("text", "").strip()
            mime    = item.get("mime", "")
            label   = "📄 PDF" if "pdf" in mime else "📎 Document"
            note    = f"{label} (msg #{item['message_id']})"
            if caption:
                note += f"\n{caption}"
            try:
                await bot.send_message(chat_id=chat_id, text=note)
                print(f"  📄  Sent document notice for msg #{item['message_id']}")
            except Exception as e:
                print(f"  ❌  Document notice failed: {e}")
            await asyncio.sleep(SEND_DELAY)

    print("\n  ✅  All done.")


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

        try:
            entity = await client.get_entity(channel_id)
        except Exception as e:
            await context.bot.send_message(chat_id=dest_chat_id, text=f"❌ Could not access channel: {e}\nMake sure you are a member.")
            await client.disconnect()
            return

        title   = getattr(entity, "title", str(channel_id))
        msg_ids = list(range(start_id, end_id + 1))

        print(f"\n{'─'*58}")
        print(f"  Channel   : {title}")
        print(f"  Msg range : {start_id} → {end_id} ({len(msg_ids)} messages)")
        print(f"  Auto-vote : {'ON ⚡' if AUTO_VOTE else 'OFF'}")
        print(f"  Dest chat : {dest_chat_id}")
        print(f"{'─'*58}\n")

        # Fetch & process messages — same logic as polls3108 main()
        items                 = []
        total_fetched         = 0
        auto_voted_n          = 0
        already_done_n        = 0
        pending_image_path    = None
        pending_image_caption = ""

        BATCH = 100
        total_batches = (len(msg_ids) + BATCH - 1) // BATCH

        for batch_num, batch_start in enumerate(range(0, len(msg_ids), BATCH), 1):
            batch    = msg_ids[batch_start:batch_start + BATCH]
            print(f"  📦  Batch {batch_num}/{total_batches} — fetching {len(batch)} messages...")
            messages = await client.get_messages(entity, ids=batch)
            messages = sorted(
                [m for m in messages if m is not None],
                key=lambda m: m.id
            )

            for message in messages:
                total_fetched += 1

                # Plain text-only messages
                if (
                    not message.media
                    and message.text
                    and message.text.strip()
                ):
                    items.append({
                        "type":       "text",
                        "message_id": message.id,
                        "date":       message.date.isoformat(),
                        "text":       message.text,
                    })
                    print(f"  📝  Text msg #{message.id}: \"{message.text[:60]}\"")
                    continue

                # Poll / quiz messages
                if isinstance(message.media, MessageMediaPoll):
                    poll_caption = message.text or ""
                    poll_data    = parse_poll(message, caption=poll_caption)
                    if poll_data is None:
                        continue

                    if pending_image_path:
                        poll_data["image_path"]    = pending_image_path
                        poll_data["image_caption"] = pending_image_caption
                        pending_image_path    = None
                        pending_image_caption = ""
                    else:
                        poll_data["image_caption"] = ""

                    if is_closed(message.media):
                        kind = "quiz" if poll_data["is_quiz"] else "poll"
                        print(f"  🔒  Closed {kind} — reading Final Results: \"{poll_data['question'][:50]}\"")
                        poll_data = read_closed_results(message, poll_data)
                        already_done_n += 1
                    elif is_unattempted(message.media):
                        if AUTO_VOTE:
                            poll_data = await auto_vote_and_reveal(
                                client, entity, message, poll_data
                            )
                            if poll_data["auto_voted"]:
                                auto_voted_n += 1
                        else:
                            kind = "quiz" if poll_data["is_quiz"] else "poll"
                            print(f"  ➖  Skipped unattempted {kind} (auto-vote OFF)")
                    else:
                        already_done_n += 1
                        print(f"  ✔  Already answered: \"{poll_data['question'][:52]}\"")

                    poll_data["type"] = "quiz"
                    items.append(poll_data)

                # Image-only messages (photos or image documents — skip PDFs/other docs)
                elif isinstance(message.media, (MessageMediaPhoto, MessageMediaDocument)):
                    doc  = getattr(message.media, "document", None)
                    mime = getattr(doc, "mime_type", "") if doc else ""
                    is_image = (
                        isinstance(message.media, MessageMediaPhoto)
                        or (mime and mime.startswith("image/"))
                    )
                    if not is_image:
                        # PDF or other non-image document — queue it for forwarding
                        print(f"  📄  Non-image doc at msg {message.id} (mime={mime or 'unknown'}) — queuing as document")
                        items.append({
                            "type":       "document",
                            "message_id": message.id,
                            "date":       message.date.isoformat(),
                            "text":       message.text or "",
                            "mime":       mime,
                        })
                        continue
                    image_path = await download_image(client, message, message.id)
                    if image_path:
                        pending_image_path    = image_path
                        pending_image_caption = message.text or ""
                        print(f'      🖼️  Stored image from msg {message.id} with caption: "{pending_image_caption[:50]}"')

        # Summary
        quizzes   = [x for x in items if x["type"] == "quiz"]
        text_msgs = [x for x in items if x["type"] == "text"]

        print(f"\n{'═'*58}")
        print(f"  📨  Messages fetched : {total_fetched}")
        print(f"  🧩  Quizzes found    : {len(quizzes)}")
        print(f"  📝  Text messages    : {len(text_msgs)}")
        print(f"  🗳️  Auto-voted       : {auto_voted_n}")
        print(f"  ✅  Already answered : {already_done_n}")
        print(f"{'═'*58}\n")

        if not items:
            await context.bot.send_message(chat_id=dest_chat_id, text="⚠️ Nothing found in this message range.")
            await client.disconnect()
            return

        # Strip raw bytes before JSON save
        for q in quizzes:
            for ans in q["answers"]:
                ans.pop("option", None)

        # Save JSON
        try:
            import json as _json
            with open(OUTPUT_JSON, "w", encoding="utf-8") as f:
                _json.dump({"quizzes": quizzes, "text_msgs": text_msgs}, f, ensure_ascii=False, indent=2)
            print(f"  💾  JSON → {OUTPUT_JSON}")
        except Exception as e:
            print(f"  ⚠️  JSON save failed: {e}")

        # Save TXT
        try:
            with open(OUTPUT_TXT, "w", encoding="utf-8") as f:
                f.write(f"Telegram Quiz Export — {title}\n")
                f.write(f"Range: msg {start_id} → {end_id} | Quizzes: {len(quizzes)} | Text msgs: {len(text_msgs)}\n\n")
                if quizzes:
                    f.write("═"*60 + "\nQUIZZES\n" + "═"*60 + "\n\n")
                    for i, quiz in enumerate(quizzes, 1):
                        f.write(format_quiz_text(quiz, i) + "\n\n")
                if text_msgs:
                    f.write("═"*60 + "\nTEXT MESSAGES\n" + "═"*60 + "\n\n")
                    for i, msg in enumerate(text_msgs, 1):
                        f.write(f"[{i}] ID:{msg['message_id']} | {msg['date']}\n")
                        f.write(msg["text"] + "\n\n")
            print(f"  📄  TXT  → {OUTPUT_TXT}")
        except Exception as e:
            print(f"  ⚠️  TXT save failed: {e}")

        # Send via bot using send_via_bot (same as polls3108)
        await send_via_bot(
            context.bot,
            items,
            title,
            chat_id        = dest_chat_id,
            recreate_polls = True,
        )

        await client.disconnect()

        # ── Completion notification ──────────────────────────────────────────
        done_text = (
            "✅ *Scrape complete\\!*\n\n"
            f"📡 Channel: `{escape_md(title)}`\n"
            f"📨 Messages fetched: `{total_fetched}`\n"
            f"🧩 Quizzes scraped: `{len(quizzes)}`\n"
            f"📝 Text messages: `{len(text_msgs)}`\n"
            f"🗳️ Auto\\-voted: `{auto_voted_n}`\n\n"
            f"📬 Results sent to: `{escape_md(str(dest_chat_id))}`"
        )
        # Always notify the user in their bot DM (even if dest is a different chat)
        await context.bot.send_message(
            chat_id    = user_id,
            text       = done_text,
            parse_mode = ParseMode.MARKDOWN_V2,
        )
        # If the destination is a different chat, also send a brief done notice there
        if str(dest_chat_id) != str(user_id):
            await context.bot.send_message(
                chat_id    = dest_chat_id,
                text       = f"✅ *Done\\! {len(quizzes)} quiz\\(es\\) and {len(text_msgs)} text message\\(s\\) delivered\\.*",
                parse_mode = ParseMode.MARKDOWN_V2,
            )

    except asyncio.CancelledError:
        print("  ⚠️  run_scrape cancelled (bot shutting down)")
        if "client" in locals():
            try:
                await client.disconnect()
            except Exception:
                pass
        raise  # let asyncio know it was properly handled

    except Exception as e:
        print(f"  ❌  run_scrape error: {e}")
        try:
            await context.bot.send_message(chat_id=dest_chat_id, text=f"❌ Error: {e}")
        except Exception:
            pass
        try:
            await context.bot.send_message(chat_id=user_id, text=f"❌ Scrape failed: {e}")
        except Exception:
            pass
        if "client" in locals():
            try:
                await client.disconnect()
            except Exception:
                pass

    finally:
        if "client" in locals():
            try:
                await client.disconnect()
            except Exception:
                pass


# ================== POLL HELPERS =================



def is_unattempted(media: MessageMediaPoll) -> bool:
    results = media.results
    if results is None:
        return True
    if results.results:
        return not any(getattr(r, "chosen", False) for r in results.results)
    return True


def is_closed(media: MessageMediaPoll) -> bool:
    """Returns True if the poll/quiz is closed (expired or manually closed)."""
    return getattr(media.poll, "closed", False)


def read_closed_results(message, poll_data: dict) -> dict:
    """
    For closed polls/quizzes, the correct answer is already revealed in Final Results.
    Read it directly without needing to vote.
    """
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
    if is_quiz:
        correct = get_correct_index(poll, results)
        label   = f"option {correct + 1}" if correct is not None else "unknown"
        print(f"      ✅  Closed quiz — correct answer from Final Results: {label}")
    else:
        correct = get_max_votes_index(poll, results)
        if correct is not None:
            winning_votes = answers[correct].get("voters", "?")
            label = f"option {correct + 1} ({winning_votes} votes)"
        else:
            label = "no votes"
        print(f"      📊  Closed poll — top answer from Final Results: {label}")

    poll_data["answers"]              = answers
    poll_data["correct_answer_index"] = correct
    poll_data["total_voters"]         = results.total_voters if results else None
    poll_data["explanation"]          = (
        results.solution if results and getattr(results, "solution", None) else None
    )
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
    """For regular polls — returns the index of the option with the most votes."""
    if not results or not results.results:
        return None
    best_i      = None
    best_voters = -1
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
    """
    Parse a poll message.
    caption is the text that accompanies the message (may be from image caption).
    """
    media = message.media
    if not isinstance(media, MessageMediaPoll):
        return None

    poll    = media.poll
    results = media.results

    question_text = (
        poll.question.text if hasattr(poll.question, "text") else str(poll.question)
    )

    answers = []
    for i, answer in enumerate(poll.answers):
        answer_text = (
            answer.text.text if hasattr(answer.text, "text") else str(answer.text)
        )
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
        "explanation": (
            results.solution
            if results and getattr(results, "solution", None) else None
        ),
        "image_path":    None,
        "auto_voted":    False,
        "caption":       caption,   # message text that accompanies the poll
        "image_caption": "",        # separate field for the image caption (if any)
    }


async def auto_vote_and_reveal(client, entity, message, poll_data: dict) -> dict:
    """
    Cast a dummy vote then re-fetch to reveal results.
    - Quiz polls: correct answer is flagged by Telegram (get_correct_index).
    - Regular polls: winning answer = option with the most votes (get_max_votes_index).
    """
    dummy     = [random.choice(message.media.poll.answers).option]
    q_preview = poll_data['question'][:50]
    is_quiz   = poll_data.get("is_quiz", False)
    kind_lbl  = "quiz" if is_quiz else "poll"
    print(f"      🗳️  Voting ({kind_lbl}): \"{q_preview}\"")

    try:
        await client(functions.messages.SendVoteRequest(
            peer=entity, msg_id=message.id, options=dummy
        ))
    except rpcerrorlist.MessagePollClosedError:
        print("      ⚠️  Poll closed — cannot vote.")
        return poll_data
    except Exception as e:
        print(f"      ⚠️  Vote error: {e}")
        return poll_data

    await asyncio.sleep(random.uniform(2.0, 5.0))

    try:
        refreshed = await client.get_messages(entity, ids=message.id)
    except Exception as e:
        print(f"      ⚠️  Re-fetch failed: {e}")
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

    if is_quiz:
        correct = get_correct_index(up, ures)
        label   = f"option {correct + 1}" if correct is not None else "still hidden"
        print(f"      ✅  Correct answer (quiz flag): {label}")
    else:
        correct = get_max_votes_index(up, ures)
        if correct is not None:
            winning_votes = updated_answers[correct].get("voters", "?")
            label = f"option {correct + 1} ({winning_votes} votes)"
        else:
            label = "no votes yet"
        print(f"      📊  Top answer (max votes): {label}")

    poll_data["answers"]              = updated_answers
    poll_data["correct_answer_index"] = correct
    poll_data["total_voters"]         = ures.total_voters if ures else None
    poll_data["auto_voted"]           = True
    poll_data["explanation"]          = (
        ures.solution if ures and getattr(ures, "solution", None) else None
    )
    return poll_data


async def download_image(client, message, msg_id: int) -> Optional[str]:
    """Download a photo or image document; return local path."""
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
    except Exception as e:
        print(f"      ⚠️  Image download failed: {e}")
        return None


async def recreate_quiz_poll(bot, quiz: dict, chat_id, number: int):
    """
    Send the quiz as a native Telegram quiz poll (sendPoll, type=quiz).
    If an image is attached, send it first with its original caption,
    then reply with the poll.
    """
    correct       = quiz.get("correct_answer_index")
    answers       = quiz.get("answers", [])
    image_caption = quiz.get("image_caption", "")

    # Native quiz poll requires a known correct answer
    if correct is None or not answers:
        print(f"  ⚠️  Quiz #{number} — correct answer unknown, sending as text")
        caption = build_bot_caption(quiz, number)
        img = quiz.get("image_path")
        try:
            if img and os.path.exists(img):
                with open(img, "rb") as f:
                    if image_caption:
                        await bot.send_photo(chat_id=chat_id, photo=f,
                                             caption=image_caption[:1024])
                    else:
                        await bot.send_photo(chat_id=chat_id, photo=f,
                                             caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await bot.send_message(chat_id=chat_id, text=caption,
                                       parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e:
            print(f"  ❌  Text fallback failed for #{number}: {e}")
        return

    # Truncate to Telegram API limits
    question     = quiz["question"][:300]
    option_texts = [ans["text"][:100] for ans in answers]
    explanation  = (quiz.get("explanation") or "")[:200]
    is_quiz_type = quiz.get("is_quiz", True)

    # Send image first (if exists) with its original caption
    reply_to_id = None
    img = quiz.get("image_path")
    try:
        if img and os.path.exists(img):
            img_caption = image_caption[:1024] if image_caption else None
            with open(img, "rb") as f:
                sent_photo = await bot.send_photo(
                    chat_id=chat_id,
                    photo=f,
                    caption=img_caption,
                    parse_mode=None
                )
                reply_to_id = sent_photo.message_id
            await asyncio.sleep(SEND_DELAY)
    except Exception as e:
        print(f"  ⚠️  Failed to send image for quiz #{number}: {e}")

    # Send the poll (as reply to the image if available)
    try:
        if is_quiz_type:
            await bot.send_poll(
                chat_id             = chat_id,
                question            = question,
                options             = option_texts,
                type                = "quiz",
                correct_option_ids  = [correct],
                explanation         = explanation or None,
                is_anonymous        = True,
                open_period         = None,
                reply_to_message_id = reply_to_id,
            )
            print(f"  🗳️  Recreated quiz #{number}: \"{question[:50]}\"")
        else:
            # Regular poll: no explanation field supported by Bot API
            await bot.send_poll(
                chat_id             = chat_id,
                question            = question,
                options             = option_texts,
                type                = "regular",
                is_anonymous        = True,
                open_period         = None,
                reply_to_message_id = reply_to_id,
            )
            print(f"  📊  Recreated poll #{number}: \"{question[:50]}\"")
            # Send explanation as a follow-up message (if any)
            if explanation:
                await asyncio.sleep(SEND_DELAY)
                winning  = option_texts[correct] if correct is not None else "N/A"
                exp_text = f"🎯 Top answer: {winning}\n\n💡 {explanation}"
                await bot.send_message(chat_id=chat_id, text=exp_text)
                print(f"      💡  Sent poll explanation for #{number}")
    except Exception as e:
        print(f"  ⚠️  Poll API error for #{number}: {e} — falling back to text")
        try:
            caption = build_bot_caption(quiz, number)
            if img and os.path.exists(img):
                with open(img, "rb") as f:
                    await bot.send_photo(chat_id=chat_id, photo=f,
                                         caption=caption, parse_mode=ParseMode.MARKDOWN_V2)
            else:
                await bot.send_message(chat_id=chat_id, text=caption,
                                       parse_mode=ParseMode.MARKDOWN_V2)
        except Exception as e2:
            print(f"  ❌  Text fallback also failed: {e2}")


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
    import httpx

    # Drop any existing webhook/session so we don't conflict with a lingering instance
    try:
        httpx.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/deleteWebhook",
            params={"drop_pending_updates": True},
            timeout=10,
        )
        print("✅ Webhook cleared.")
    except Exception as e:
        print(f"⚠️  Could not clear webhook: {e}")

    async def post_init(application):
        await start_ping_server()
        application.bot_data["ping_task"] = asyncio.create_task(self_ping_loop())

    async def post_shutdown(application):
        # Cancel the self-ping task
        ping_task = application.bot_data.get("ping_task")
        if ping_task and not ping_task.done():
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass

        # Cancel all in-flight scrape tasks cleanly
        scrape_tasks = application.bot_data.get("scrape_tasks", set())
        if scrape_tasks:
            print(f"⏳ Cancelling {len(scrape_tasks)} in-flight scrape task(s)…")
            for t in list(scrape_tasks):
                t.cancel()
            await asyncio.gather(*scrape_tasks, return_exceptions=True)
            print("✅ Scrape tasks cancelled.")

        # Close the DB while the event loop is still running
        try:
            await close_db()
            print("✅ DB closed cleanly.")
        except Exception as e:
            print(f"⚠️  DB close error: {e}")

    # Register lifecycle hooks via the builder pattern so PTB actually calls them
    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .post_shutdown(post_shutdown)
        .build()
    )

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

    # Manage saved destinations
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("set_destination", set_destination)],
        states={
            ADD_DEST_PICK: [
                CallbackQueryHandler(manage_dest_callback),
            ],
            ADD_DEST_TYPED: [
                # Accept forwarded messages OR typed text (ID / @username)
                MessageHandler(
                    (filters.TEXT & ~filters.COMMAND) | filters.FORWARDED,
                    add_dest_typed,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    # Scrape
    app.add_handler(ConversationHandler(
        entry_points=[CommandHandler("scrape", scrape_start)],
        states={
            SCRAPE_START_LINK: [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_start_link)],
            SCRAPE_END_LINK:   [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_end_link)],
            SCRAPE_DEST:       [MessageHandler(filters.TEXT & ~filters.COMMAND, scrape_dest_number)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    ))

    app.add_handler(CommandHandler("start",  start))
    app.add_handler(CommandHandler("logout", logout))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("cancel", cancel))

    print("Bot is running...")
    app.run_polling(
        drop_pending_updates=True,
        allowed_updates=Update.ALL_TYPES,
    )


if __name__ == "__main__":
    main()
