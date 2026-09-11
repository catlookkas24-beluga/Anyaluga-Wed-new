"""
dashboard_db.py
เลเยอร์คุย MongoDB ฝั่ง dashboard — ตั้งใจให้ตรงกับ db.py ของบอท (beluga-bot) ทุก field/collection
เพื่อให้สองระบบอ่าน/เขียนเอกสารเดียวกันได้จริง (ไม่ใช่แค่ต่อ DB เดียวกันเฉย ๆ)

ต้องตั้ง ENV ให้ตรงกับฝั่งบอท:
- MONGO_URI          (เชื่อม cluster เดียวกัน)
- MONGO_DB_NAME       (ต้องเป็น "beluga_control" เหมือนบอท — ค่า default ตรงกันแล้ว)
"""

import os
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from bson import ObjectId

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "beluga_control")

_client = AsyncIOMotorClient(MONGO_URI)
_db = _client[MONGO_DB_NAME]
guilds = _db["guild_configs"]
welcome_presets = _db["welcome_presets"]

# ⚠️ bucket_name ต้องเป็น "assets" เหมือนบอทเป๊ะ ๆ — GridFS แยก collection ตาม bucket_name
# ถ้าตั้งชื่ออื่น ไฟล์ที่อัปโหลดจากเว็บจะไม่โผล่ให้บอทเห็น (และกลับกัน)
_assets_bucket = None


def _get_assets_bucket() -> AsyncIOMotorGridFSBucket:
    global _assets_bucket
    if _assets_bucket is None:
        _assets_bucket = AsyncIOMotorGridFSBucket(_db, bucket_name="assets")
    return _assets_bucket


# ต้องตรงกับ DEFAULT_CONFIG["welcome"] ใน beluga-bot/db.py เป๊ะ ๆ ทุกฟิลด์
# (คัดลอกมาโดยตรง — ถ้าฝั่งบอทเพิ่มฟิลด์ใหม่ ต้องมาอัปเดตตรงนี้ด้วยเสมอ)
WELCOME_DEFAULTS = {
    "channel_id": None,
    "title": "🎉 ยินดีต้อนรับ {user} สู่ {server_name}!",
    "description": "ตอนนี้เซิร์ฟเวอร์มี {server_membercount} สมาชิกแล้ว!",
    "color": "#a0d2eb",
    "image_url": None,
    "image_urls": [],
    "font_key": None,
    "author_name": None,
    "author_icon_url": None,
    "footer_text": None,
    "footer_icon_url": None,
    "fields": [],
    "extra_embed_title": None,
    "extra_embed_description": None,
    "avatar_position": "center",
    "avatar_size": 128,
    "avatar_enabled": True,
    "text_position": "bottom",
    "text_color": "#ffffff",
    "border_color": None,
    "border_width": 0,
    "delay_seconds": 0,
    "dm_enabled": False,
    "send_count": 0,
}


async def get_welcome_config(guild_id: int) -> dict:
    doc = await guilds.find_one({"_id": guild_id}, {"welcome": 1})
    stored = (doc or {}).get("welcome") or {}
    return {**WELCOME_DEFAULTS, **stored}


async def update_welcome_config(guild_id: int, values: dict) -> None:
    """อัปเดตเฉพาะ field ที่ส่งมา (partial update) แบบเดียวกับ update_guild_section ฝั่งบอท"""
    await guilds.update_one(
        {"_id": guild_id},
        {"$set": {f"welcome.{k}": v for k, v in values.items()}},
        upsert=True,
    )


async def save_welcome_image(guild_id: int, filename: str, data: bytes, content_type: str) -> str:
    file_id = await _get_assets_bucket().upload_from_stream(
        filename, data, metadata={"guild_id": guild_id, "asset_type": "image", "content_type": content_type}
    )
    return str(file_id)


async def read_asset_bytes(file_id: str) -> tuple[bytes, str]:
    """คืนค่า (bytes, content_type) — โยน gridfs.NoFile ถ้าไม่พบ (จับใน router)"""
    bucket = _get_assets_bucket()
    stream = await bucket.open_download_stream(ObjectId(file_id))
    data = await stream.read()
    content_type = (stream.metadata or {}).get("content_type", "image/png")
    return data, content_type


async def save_welcome_preset(guild_id: int, name: str, config_snapshot: dict) -> None:
    await welcome_presets.update_one(
        {"_id": f"{guild_id}:{name}"},
        {"$set": {"guild_id": guild_id, "name": name, "config": config_snapshot}},
        upsert=True,
    )


async def load_welcome_preset(guild_id: int, name: str) -> dict | None:
    doc = await welcome_presets.find_one({"_id": f"{guild_id}:{name}"})
    return doc["config"] if doc else None


async def list_welcome_presets(guild_id: int) -> list:
    cursor = welcome_presets.find({"guild_id": guild_id})
    return [doc["name"] async for doc in cursor]


# ---------------- Systems Enabled (สำหรับหน้า Dashboard หลัก) ----------------
# ต้องตรงกับ DEFAULT_CONFIG["systems_enabled"] ใน beluga-bot/db.py เป๊ะ ๆ

SYSTEMS_ENABLED_DEFAULTS = {
    "welcome": True, "verify": True, "rules": True, "antiraid": True,
    "autorole": True, "activity": True, "goodbye": True, "ticket": True,
}

SYSTEM_META = {
    "welcome": {"label": "Welcome", "icon": "🎨", "desc": "ข้อความต้อนรับสมาชิกใหม่"},
    "goodbye": {"label": "Goodbye", "icon": "👋", "desc": "ข้อความอำลาสมาชิกที่ออก"},
    "verify": {"label": "Verify", "icon": "🔐", "desc": "ระบบยืนยันตัวตน"},
    "rules": {"label": "Rules", "icon": "📜", "desc": "กฎเซิร์ฟเวอร์และเงื่อนไขเลื่อนยศ"},
    "antiraid": {"label": "Anti-Raid", "icon": "🛡️", "desc": "ป้องกันการโจมตีแบบ raid"},
    "autorole": {"label": "Auto Role", "icon": "🏅", "desc": "แจกยศอัตโนมัติตามอายุสมาชิก"},
    "ticket": {"label": "Ticket", "icon": "🎫", "desc": "ระบบตั๋วขอความช่วยเหลือ"},
    "activity": {"label": "สถิติ", "icon": "📊", "desc": "สถิติกิจกรรมสมาชิก"},
}


async def get_systems_enabled(guild_id: int) -> dict:
    doc = await guilds.find_one({"_id": guild_id}, {"systems_enabled": 1})
    stored = (doc or {}).get("systems_enabled") or {}
    return {**SYSTEMS_ENABLED_DEFAULTS, **stored}


async def set_system_enabled(guild_id: int, system_name: str, enabled: bool) -> None:
    await guilds.update_one(
        {"_id": guild_id}, {"$set": {f"systems_enabled.{system_name}": enabled}}, upsert=True
    )
