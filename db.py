import os
from motor.motor_asyncio import AsyncIOMotorClient

MONGO_URI   = os.getenv("MONGO_URI", "mongodb://localhost:27017")
DB_NAME     = os.getenv("MONGO_DB_NAME", "telegram_quiz_bot")

_client = None
_db     = None


def get_db():
    global _client, _db
    if _db is None:
        _client = AsyncIOMotorClient(MONGO_URI)
        _db     = _client[DB_NAME]
    return _db


# ── User helpers ──────────────────────────────────────────────────

async def get_user(user_id: str) -> dict:
    doc = await get_db().users.find_one({"_id": user_id})
    return doc or {}


async def set_user_field(user_id: str, field: str, value):
    await get_db().users.update_one(
        {"_id": user_id}, {"$set": {field: value}}, upsert=True
    )


async def set_user_fields(user_id: str, fields: dict):
    await get_db().users.update_one(
        {"_id": user_id}, {"$set": fields}, upsert=True
    )


# ── Channel helpers ───────────────────────────────────────────────

async def add_channel(user_id: str, channel_id: int, title: str, link: str = ""):
    """Add a channel to the user's saved channels list (no duplicates)."""
    db = get_db()
    await db.channels.update_one(
        {"user_id": user_id, "channel_id": channel_id},
        {"$set": {"user_id": user_id, "channel_id": channel_id,
                  "title": title, "link": link}},
        upsert=True,
    )


async def remove_channel(user_id: str, channel_id: int):
    """Remove a channel from the user's saved channels list."""
    await get_db().channels.delete_one(
        {"user_id": user_id, "channel_id": channel_id}
    )


async def get_channels(user_id: str) -> list:
    """Return all saved channels for a user."""
    cursor = get_db().channels.find({"user_id": user_id})
    return await cursor.to_list(length=100)


async def get_channel(user_id: str, channel_id: int) -> dict | None:
    """Return a single saved channel or None."""
    return await get_db().channels.find_one(
        {"user_id": user_id, "channel_id": channel_id}
    )


async def save_job(user_id: str, job: dict):
    """Save or update a scrape job for a user."""
    await get_db().jobs.update_one(
        {"user_id": user_id},
        {"$set": {**job, "user_id": user_id}},
        upsert=True,
    )


async def get_job(user_id: str) -> dict | None:
    """Get the active scrape job for a user."""
    return await get_db().jobs.find_one({"user_id": user_id})


async def clear_job(user_id: str):
    """Delete the scrape job once fully complete."""
    await get_db().jobs.delete_one({"user_id": user_id})


# ── Cleanup ───────────────────────────────────────────────────────

async def close_db():
    global _client, _db
    if _client:
        _client.close()
        _client = None
        _db     = None
