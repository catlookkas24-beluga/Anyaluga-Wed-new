"""
db.py
เลเยอร์คุยกับ MongoDB ทั้งหมดอยู่ที่นี่ที่เดียว — ทุก cog เรียกผ่านฟังก์ชันในไฟล์นี้
เก็บ config แยกตาม guild_id เป็นเอกสารเดียวต่อเซิร์ฟเวอร์ ทำให้บอทตัวเดียวดูแลได้หลายเซิร์ฟ

🆕 อัปเดต: เพิ่มระบบ "logging" (เบาเวอร์ชันแรก — เก็บแค่ channel_id ที่จะโพสต์ log)
🆕 อัปเดต: รวม schema กับ "db 2.py" ที่แยกกิ่งไปก่อนหน้านี้ — เพิ่ม avatar_enabled และ
text_color กลับเข้ามาใน welcome defaults (ยืนยันแล้วว่า welcome.py / welcome_wizard.py
ใช้ทั้งสอง field นี้จริง) ไฟล์นี้คือ schema ที่ถูกต้องตัวเดียวที่ทั้งบอทและแดชบอร์ดควร import
"""

import os
import sys
from datetime import datetime, timedelta, timezone
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorGridFSBucket
from bson import ObjectId

# 🩺 SCHEMA VERSION MARKER — เปลี่ยนทุกครั้งที่แก้ DEFAULT_CONFIG หรือ diagnostic นี้
# ใช้เทียบว่า process ที่รันอยู่จริงบน Render โหลดไฟล์เวอร์ชันไหน (grep หา marker นี้ใน log)
DB_SCHEMA_VERSION = "2026-09-12-avatar-textcolor-reconciled"

MONGO_URI = os.getenv("MONGO_URI")
MONGO_DB_NAME = os.getenv("MONGO_DB_NAME", "beluga_control")


# 🛡️ ENV VALIDATION — ป้องกัน production bug ที่เจอวันนี้ (2026-09-12): MONGO_DB_NAME มี
# trailing whitespace บน service หนึ่ง ทำให้ Dashboard กับ Bot ต่อคนละ MongoDB database กันจริง ๆ
# (MongoDB มองว่า "beluga_control" กับ "beluga_control " เป็นคนละฐานข้อมูล) ทั้งที่ print ค่าออกมา
# ดูเหมือนกันเป๊ะด้วยตาเปล่า — HTTP 200 ทุกครั้ง, MongoDB write สำเร็จทุกครั้ง, แต่ฝั่งอ่านไม่เห็นค่า
#
# กฎ: ถ้า MONGO_URI หรือ MONGO_DB_NAME มี leading/trailing whitespace, newline, หรือ tab ปนอยู่
# ให้ "crash ทันทีตอน startup" พร้อมข้อความชัดเจน — ห้าม trim แล้วปล่อยผ่านเงียบ ๆ (เพราะจะกลาย
# เป็นการซ่อนบั๊กไว้อีกชั้น ถ้าคนตั้งค่าไม่รู้ว่ามี whitespace แฝงอยู่) และห้าม fallback ไป database อื่น
def _assert_no_stray_whitespace(name: str, value: str) -> None:
    if value is None:
        return
    if value != value.strip() or any(c in value for c in ("\n", "\t", "\r")):
        raise RuntimeError(
            f"[db.py] ENV VALIDATION FAILED: {name} มีช่องว่าง/newline/tab แฝงอยู่ที่ต้นหรือท้ายค่า "
            f"(raw={value!r}) — นี่คือสาเหตุ production bug ที่ Dashboard กับ Bot ต่อคนละ MongoDB "
            f"database กัน แก้ไขค่า {name} ใน environment variables ให้ตรงตัวเป๊ะ ๆ แล้ว deploy ใหม่ "
            f"(ระบบจะไม่ trim ให้อัตโนมัติ เพราะจะซ่อนปัญหานี้ไว้เงียบ ๆ อีกครั้ง)"
        )


_assert_no_stray_whitespace("MONGO_URI", MONGO_URI)
_assert_no_stray_whitespace("MONGO_DB_NAME", MONGO_DB_NAME)

_client = AsyncIOMotorClient(MONGO_URI)
_db = _client[MONGO_DB_NAME]
guilds = _db["guild_configs"]


def _redact_mongo_uri(uri: str) -> str:
    if not uri:
        return "(ไม่ได้ตั้ง MONGO_URI)"
    if "@" in uri:
        scheme_and_creds, rest = uri.rsplit("@", 1)
        scheme = scheme_and_creds.split("//")[0]
        return f"{scheme}//***:***@{rest}"
    return uri


# 🩺 DIAGNOSTIC — ใช้ print() ล้วน ๆ ไม่ผ่าน logging module เลย เพราะ logging ต้องพึ่ง handler/config
# ที่อาจยังไม่ถูกตั้งตอนไฟล์นี้ถูก import (เช่น import db ใน app.py มาก่อน logging.basicConfig())
# print() ไป stdout ตรง ๆ แบบนี้ Render (หรือ log collector ไหนก็ตาม) จับได้แน่นอน 100% ไม่มีทาง
# ถูกกรองทิ้งเงียบ ๆ เหมือน logging เคยเป็นมาก่อน — ถ้ายังไม่เห็นบรรทัดนี้ใน log แปลว่าไฟล์นี้
# (เวอร์ชันนี้) ไม่ได้ถูก import ขึ้นมาจริง ๆ (deploy เก่าค้าง / cache / import ไฟล์ผิดตัว)
print(
    f"[BELUGA-DB-BOOT] db.py version={DB_SCHEMA_VERSION!r} loaded from file={__file__} "
    f"| MongoDB host={_redact_mongo_uri(MONGO_URI)} db={MONGO_DB_NAME!r} collection=guild_configs",
    file=sys.stderr,
    flush=True,
)

# 📊 Activity/Stats Dashboard — เก็บสถิติข้อความ/เวลาเข้าเสียงแยกกัน 2 collection:
# - activity_totals: ยอดรวมตลอดกาลต่อคน (rank เร็ว ไม่ต้อง aggregate ทุกครั้ง)
# - activity_daily: ยอดรายวัน ใช้ทำกราฟและสรุป 7/30 วัน
activity_totals = _db["activity_totals"]
activity_daily = _db["activity_daily"]
tickets = _db["tickets"]  # 🎫 บันทึกตั๋วที่เปิดอยู่/ปิดแล้ว กันเปิดซ้ำและไว้ตรวจสอบย้อนหลัง
welcome_presets = _db["welcome_presets"]  # 🎨 บันทึกดีไซน์ welcome ไว้หลายชุด สลับใช้ได้

# GridFS bucket สำหรับเก็บไฟล์ที่ผู้ใช้อัปโหลด (รูป/ฟอนต์/config) แบบถาวร
# ไม่หายตอน redeploy บอท (ต่างจากดิสก์ของ Render ที่ล้างทุกครั้งที่ deploy ใหม่)
# สร้างแบบ lazy (ตอนถูกเรียกใช้ครั้งแรก) เพราะ motor เวอร์ชันใหม่ต้องการ event loop
# ที่ทำงานอยู่แล้วตอนสร้าง — ถ้าสร้างตอน import module จะ crash ทันที (ยังไม่มี event loop)
_assets_bucket = None


def _get_assets_bucket() -> AsyncIOMotorGridFSBucket:
    global _assets_bucket
    if _assets_bucket is None:
        _assets_bucket = AsyncIOMotorGridFSBucket(_db, bucket_name="assets")
    return _assets_bucket


async def warm_up():
    """เรียกตอนบอทเริ่มทำงาน เพื่อเปิดการเชื่อมต่อ MongoDB ล่วงหน้า
    ป้องกัน interaction แรกที่ user สั่งค้าง/timeout เพราะรอ TLS handshake"""
    await _client.admin.command("ping")

# ค่าเริ่มต้นของแต่ละระบบ — ใช้ตอนเซิร์ฟเวอร์ยังไม่มี config ใน DB เลย
DEFAULT_CONFIG = {
    "systems_enabled": {
        "welcome": True,
        "verify": True,
        "rules": True,
        "antiraid": True,
        "autorole": True,
        "activity": True,
        "goodbye": True,
        "ticket": True,
        "logging": True,  # 🆕 เบาเวอร์ชันแรก — บันทึกลบ/แก้ข้อความ + ยศเปลี่ยน
    },
    # 🔑 Custom Command Permissions — { "command-name": [role_id, role_id, ...] }
    # ว่างเปล่า = ยังไม่ตั้งค่าอะไร (แปลว่าต้องมี Manage Server เท่านั้นถึงใช้ได้ ตามค่าเดิม)
    "command_permissions": {},
    "welcome": {
        "channel_id": None,
        "title": "🎉 ยินดีต้อนรับ {user} สู่ {server_name}!",
        "description": "ตอนนี้เซิร์ฟเวอร์มี {server_membercount} สมาชิกแล้ว!",
        "color": "#a0d2eb",
        "image_url": None,
        "image_urls": [],
        "font_key": None,
        # 👤 Author / Footer field
        "author_name": None,
        "author_icon_url": None,
        "footer_text": None,
        "footer_icon_url": None,
        # 📋 Embed fields (สูงสุด 3 ช่อง) — [{"name":.., "value":.., "inline": bool}]
        "fields": [],
        # 📨 Multi-Embed — embed ที่สองต่อท้าย (เว้นว่าง title = ไม่ส่ง)
        "extra_embed_title": None,
        "extra_embed_description": None,
        # 🖼️ ตำแหน่ง/ขนาด avatar และข้อความบน composite image + กรอบ
        "avatar_enabled": True,
        "avatar_position": "center",
        "avatar_size": 128,
        "text_color": "#ffffff",
        "text_position": "bottom",
        "border_color": None,
        "border_width": 0,
        # 🎲 พฤติกรรมการส่ง
        "delay_seconds": 0,
        "dm_enabled": False,
        "send_count": 0,
    },
    "goodbye": {
        "channel_id": None,
        "title": "👋 ลาก่อน {user_name}",
        "description": "ขอให้โชคดีนะครับ หวังว่าจะได้เจอกันอีก",
        "color": "#6b7280",
        "image_url": None,
        "image_urls": [],
        "font_key": None,
    },
    "verify": {
        "channel_id": None,
        "role_id": None,
        "explain_text": "ยืนยันตัวตนเพื่อป้องกันบอทและผู้ใช้ปลอม ช่วยให้เซิร์ฟเวอร์ปลอดภัยขึ้นครับ",
        "banner_asset_id": None,
        "color": "#2ecc71",
        "confirm_emoji": "✅",
        "explain_emoji": "❓",
    },
    "rules": {
        "channel_id": None,
        "message_id": None,
        "title": "📜 กฎของเซิร์ฟเวอร์",
        "rules_text": "1. เคารพกันและกัน\n2. ห้ามสแปม\n3. ห้ามโฆษณาที่ไม่ได้รับอนุญาต",
        "rank_text": "7 วัน = ส่งรูปได้ | 30 วัน = เข้าเสียงได้",
        "color": "#e67e22",
    },
    "ticket": {
        "category_id": None,
        "support_role_ids": [],
        "panel_channel_id": None,
        "title": "🎫 เปิดตั๋วขอความช่วยเหลือ",
        "description": "กดปุ่มด้านล่างเพื่อเปิดห้องส่วนตัวคุยกับทีมงาน",
        "color": "#5865f2",
        "welcome_text": "สวัสดีครับ {user} ทีมงานจะเข้ามาช่วยเหลือเร็ว ๆ นี้ กรุณาอธิบายปัญหาของคุณ",
    },
    "antiraid": {
        "join_threshold": 5,
        "window_seconds": 10,
        "min_account_age_days": 7,
        "alert_channel_id": None,
    },
    "autorole": {
        "auto_grant": True,
        "claim_channel_id": None,
        "timeline": [
            {"days": 7, "role_id": None, "label": "Member"},
            {"days": 30, "role_id": None, "label": "Active"},
            {"days": 60, "role_id": None, "label": "Veteran"},
            {"days": 90, "role_id": None, "label": "Legend"},
        ],
    },
    "logging": {
        # 🆕 เบาเวอร์ชันแรก: มีแค่ห้อง log — ยังไม่มี mod-case/ตัวกรองแยกประเภท event
        "channel_id": None,
    },
}


def _merge_defaults(doc: dict) -> dict:
    """เติม key ที่ขาดไปด้วยค่า default แบบ recursive (กันเอกสารเก่าที่ schema ยังไม่ครบ)"""
    merged = {}
    for key, default_val in DEFAULT_CONFIG.items():
        stored_val = doc.get(key)
        if isinstance(default_val, dict) and isinstance(stored_val, dict):
            merged[key] = {**default_val, **stored_val}
        elif stored_val is not None:
            merged[key] = stored_val
        else:
            merged[key] = default_val
    return merged


async def get_guild_config(guild_id: int) -> dict:
    doc = await guilds.find_one({"_id": guild_id})
    if doc is None:
        fresh = {"_id": guild_id, **DEFAULT_CONFIG}
        await guilds.insert_one(fresh)
        return fresh
    return {"_id": guild_id, **_merge_defaults(doc)}


async def update_guild_section(guild_id: int, section: str, values: dict) -> None:
    """อัปเดตเฉพาะ sub-document ของ section หนึ่ง (เช่น 'welcome', 'verify') แบบ upsert"""
    await guilds.update_one(
        {"_id": guild_id},
        {"$set": {f"{section}.{k}": v for k, v in values.items()}},
        upsert=True,
    )


async def set_system_enabled(guild_id: int, system_name: str, enabled: bool) -> None:
    await guilds.update_one(
        {"_id": guild_id},
        {"$set": {f"systems_enabled.{system_name}": enabled}},
        upsert=True,
    )


async def is_system_enabled(guild_id: int, system_name: str) -> bool:
    cfg = await get_guild_config(guild_id)
    return cfg["systems_enabled"].get(system_name, True)


async def reset_guild_config(guild_id: int) -> None:
    """⚡ Force Reset Config — เขียนทับ config ของเซิร์ฟนี้กลับเป็นค่าโรงงานทั้งหมด
    (ไม่แตะไฟล์ asset ที่อัปโหลดไว้ใน GridFS — อันนั้นต้องลบแยกถ้าต้องการ)"""
    await guilds.replace_one(
        {"_id": guild_id}, {"_id": guild_id, **DEFAULT_CONFIG}, upsert=True
    )


# ---------------- Asset Storage (GridFS) ----------------
# เก็บไฟล์ที่แอดมินอัปโหลดเอง (รูปพื้นหลัง, ฟอนต์, ไฟล์ config) แยกตาม guild_id
# ไม่ประมวลผล/เรนเดอร์อะไรกับไฟล์เหล่านี้ — เป็นแค่คลังเก็บไฟล์

ALLOWED_ASSET_TYPES = {
    "image": {".png", ".jpg", ".jpeg", ".webp"},
    "font": {".ttf", ".otf"},
    "config": {".json", ".txt"},
}
MAX_ASSET_SIZE_BYTES = 5 * 1024 * 1024  # 5MB


def detect_asset_type(filename: str) -> str | None:
    ext = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    for asset_type, extensions in ALLOWED_ASSET_TYPES.items():
        if ext in extensions:
            return asset_type
    return None


async def save_asset(guild_id: int, filename: str, data: bytes, asset_type: str, label: str) -> str:
    """อัปโหลดไฟล์เข้า GridFS คืนค่า file_id เป็น string"""
    file_id = await _get_assets_bucket().upload_from_stream(
        filename,
        data,
        metadata={
            "guild_id": guild_id,
            "asset_type": asset_type,
            "label": label,
        },
    )
    return str(file_id)


async def list_assets(guild_id: int, asset_type: str | None = None) -> list[dict]:
    query = {"metadata.guild_id": guild_id}
    if asset_type:
        query["metadata.asset_type"] = asset_type
    cursor = _get_assets_bucket().find(query)
    results = []
    async for doc in cursor:
        results.append(
            {
                "file_id": str(doc._id),
                "filename": doc.filename,
                "label": doc.metadata.get("label", doc.filename),
                "asset_type": doc.metadata.get("asset_type"),
                "length": doc.length,
            }
        )
    return results


async def get_asset_bytes(file_id: str) -> bytes:
    stream = await _get_assets_bucket().open_download_stream(ObjectId(file_id))
    return await stream.read()


async def delete_asset(guild_id: int, file_id: str) -> bool:
    """ลบไฟล์ ตรวจสอบก่อนว่าไฟล์นี้เป็นของ guild นี้จริงก่อนลบ (กันลบข้ามเซิร์ฟ)"""
    grid_out = await _get_assets_bucket().open_download_stream(ObjectId(file_id))
    if grid_out.metadata.get("guild_id") != guild_id:
        return False
    await _get_assets_bucket().delete(ObjectId(file_id))
    return True


# ---------------- Activity / Stats Dashboard ----------------


def _totals_id(guild_id: int, user_id: int) -> str:
    return f"{guild_id}:{user_id}"


def _daily_id(guild_id: int, user_id: int, date_str: str) -> str:
    return f"{guild_id}:{user_id}:{date_str}"


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def track_message(guild_id: int, user_id: int, channel_id: int) -> None:
    """เรียกทุกครั้งที่มีข้อความใหม่ (ไม่นับบอท) — อัปเดตทั้งยอดรวมและยอดรายวัน"""
    channel_key = str(channel_id)
    await activity_totals.update_one(
        {"_id": _totals_id(guild_id, user_id)},
        {
            "$set": {"guild_id": guild_id, "user_id": user_id},
            "$inc": {"total_messages": 1, f"channel_counts.{channel_key}": 1},
        },
        upsert=True,
    )
    date_str = _today_str()
    await activity_daily.update_one(
        {"_id": _daily_id(guild_id, user_id, date_str)},
        {
            "$set": {"guild_id": guild_id, "user_id": user_id, "date": date_str},
            "$inc": {"messages": 1},
        },
        upsert=True,
    )


async def track_voice_time(guild_id: int, user_id: int, seconds: int) -> None:
    """เรียกตอนสมาชิกออกจากห้องเสียง — บวกเวลาที่อยู่ในห้องเสียง (วินาที)"""
    if seconds <= 0:
        return
    await activity_totals.update_one(
        {"_id": _totals_id(guild_id, user_id)},
        {
            "$set": {"guild_id": guild_id, "user_id": user_id},
            "$inc": {"total_voice_seconds": seconds},
        },
        upsert=True,
    )
    date_str = _today_str()
    await activity_daily.update_one(
        {"_id": _daily_id(guild_id, user_id, date_str)},
        {
            "$set": {"guild_id": guild_id, "user_id": user_id, "date": date_str},
            "$inc": {"voice_seconds": seconds},
        },
        upsert=True,
    )


async def get_user_totals(guild_id: int, user_id: int) -> dict:
    doc = await activity_totals.find_one({"_id": _totals_id(guild_id, user_id)})
    if doc is None:
        return {"total_messages": 0, "total_voice_seconds": 0, "channel_counts": {}}
    return {
        "total_messages": doc.get("total_messages", 0),
        "total_voice_seconds": doc.get("total_voice_seconds", 0),
        "channel_counts": doc.get("channel_counts", {}),
    }


async def get_window_sum(guild_id: int, user_id: int, days: int) -> tuple:
    """รวมยอดข้อความ/เวลาเสียง ย้อนหลัง N วัน (รวมวันนี้) คืนค่า (messages, voice_seconds)"""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days - 1)).strftime("%Y-%m-%d")
    cursor = activity_daily.find(
        {"guild_id": guild_id, "user_id": user_id, "date": {"$gte": cutoff}}
    )
    messages, voice_seconds = 0, 0
    async for doc in cursor:
        messages += doc.get("messages", 0)
        voice_seconds += doc.get("voice_seconds", 0)
    return messages, voice_seconds


async def get_rank(guild_id: int, user_id: int, metric: str) -> tuple:
    """metric = 'total_messages' หรือ 'total_voice_seconds' — คืนค่า (rank, จำนวนคนที่มีสถิติทั้งหมด)"""
    my_doc = await activity_totals.find_one({"_id": _totals_id(guild_id, user_id)})
    my_value = my_doc.get(metric, 0) if my_doc else 0
    higher_count = await activity_totals.count_documents(
        {"guild_id": guild_id, metric: {"$gt": my_value}}
    )
    total_count = await activity_totals.count_documents({"guild_id": guild_id})
    return higher_count + 1, total_count


async def get_daily_series(guild_id: int, user_id: int, days: int = 14) -> list:
    """คืนค่า list ของ (date_str, messages, voice_seconds) เรียงจากเก่าไปใหม่ ครบทุกวันแม้ไม่มีข้อมูล"""
    today = datetime.now(timezone.utc).date()
    date_list = [(today - timedelta(days=i)) for i in range(days - 1, -1, -1)]
    date_strs = [d.strftime("%Y-%m-%d") for d in date_list]
    cursor = activity_daily.find(
        {"guild_id": guild_id, "user_id": user_id, "date": {"$in": date_strs}}
    )
    by_date = {}
    async for doc in cursor:
        by_date[doc["date"]] = (doc.get("messages", 0), doc.get("voice_seconds", 0))
    return [(d, *by_date.get(d, (0, 0))) for d in date_strs]


async def get_leaderboard(guild_id: int, metric: str, limit: int = 10) -> list:
    """metric = 'total_messages' หรือ 'total_voice_seconds' — คืนค่า list ของ (user_id, value)"""
    cursor = activity_totals.find({"guild_id": guild_id}).sort(metric, -1).limit(limit)
    results = []
    async for doc in cursor:
        results.append((doc["user_id"], doc.get(metric, 0)))
    return results


async def get_top_channels(guild_id: int, user_id: int, limit: int = 3) -> list:
    """คืนค่า list ของ (channel_id, count) เรียงมากไปน้อย"""
    doc = await activity_totals.find_one({"_id": _totals_id(guild_id, user_id)})
    if not doc or "channel_counts" not in doc:
        return []
    counts = doc["channel_counts"]
    sorted_channels = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:limit]
    return [(int(ch_id), count) for ch_id, count in sorted_channels]


# ---------------- Custom Command Permissions ----------------
# ให้แอดมินกำหนดได้ว่ายศไหนใช้คำสั่งไหนได้บ้าง แยกจากสิทธิ์ Manage Server ของ Discord เอง
# คนที่มี Manage Server ใช้ได้ทุกคำสั่งเสมอ (bypass) — อันนี้ใช้ปลดล็อกให้ยศอื่นเพิ่มเติม

async def get_allowed_roles(guild_id: int, command_name: str) -> list:
    cfg = await get_guild_config(guild_id)
    return cfg.get("command_permissions", {}).get(command_name, [])


async def add_allowed_role(guild_id: int, command_name: str, role_id: int) -> None:
    await guilds.update_one(
        {"_id": guild_id},
        {"$addToSet": {f"command_permissions.{command_name}": role_id}},
        upsert=True,
    )


async def remove_allowed_role(guild_id: int, command_name: str, role_id: int) -> None:
    await guilds.update_one(
        {"_id": guild_id},
        {"$pull": {f"command_permissions.{command_name}": role_id}},
    )


async def get_all_command_permissions(guild_id: int) -> dict:
    cfg = await get_guild_config(guild_id)
    return cfg.get("command_permissions", {})


# ---------------- Ticket System ----------------
# บันทึกตั๋วที่เปิดอยู่ กันคนเปิดซ้ำหลายตั๋วพร้อมกัน และไว้ตรวจสอบย้อนหลังได้

async def create_ticket_record(guild_id: int, channel_id: int, opener_id: int) -> None:
    await tickets.insert_one(
        {
            "_id": channel_id,
            "guild_id": guild_id,
            "opener_id": opener_id,
            "status": "open",
        }
    )


async def get_open_ticket_channel_id(guild_id: int, opener_id: int) -> int | None:
    doc = await tickets.find_one(
        {"guild_id": guild_id, "opener_id": opener_id, "status": "open"}
    )
    return doc["_id"] if doc else None


async def close_ticket_record(channel_id: int) -> None:
    await tickets.update_one({"_id": channel_id}, {"$set": {"status": "closed"}})


# ---------------- Welcome Preset / Theme System ----------------
# บันทึกดีไซน์ welcome ไว้หลายชุด (ชื่อ + snapshot ของ config ทั้งหมด) สลับใช้ได้

def _preset_id(guild_id: int, name: str) -> str:
    return f"{guild_id}:{name}"


async def save_welcome_preset(guild_id: int, name: str, config_snapshot: dict) -> None:
    await welcome_presets.update_one(
        {"_id": _preset_id(guild_id, name)},
        {"$set": {"guild_id": guild_id, "name": name, "config": config_snapshot}},
        upsert=True,
    )


async def load_welcome_preset(guild_id: int, name: str) -> dict | None:
    doc = await welcome_presets.find_one({"_id": _preset_id(guild_id, name)})
    return doc["config"] if doc else None


async def list_welcome_presets(guild_id: int) -> list:
    cursor = welcome_presets.find({"guild_id": guild_id})
    return [doc["name"] async for doc in cursor]


async def delete_welcome_preset(guild_id: int, name: str) -> bool:
    result = await welcome_presets.delete_one({"_id": _preset_id(guild_id, name)})
    return result.deleted_count > 0


async def increment_welcome_send_count(guild_id: int) -> None:
    await guilds.update_one({"_id": guild_id}, {"$inc": {"welcome.send_count": 1}}, upsert=True)
