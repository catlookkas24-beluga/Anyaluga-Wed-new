"""
dashboard/app.py — 🌐 Beluga Web Dashboard
เว็บแดชบอร์ดคู่กับบอท Anyaluga — login ด้วย Discord OAuth2 แล้วตั้งค่าทุกระบบผ่านหน้าเว็บ
แทนการพิมพ์ slash command ทีละอัน ใช้ MongoDB collection เดียวกับบอท (db.py ตัวเดียวกัน)
เปลี่ยนอะไรที่นี่ บอทเห็นผลทันที ไม่ต้อง sync อะไรเพิ่ม

รันด้วย: uvicorn app:app --host 0.0.0.0 --port 8000
ต้องมีไฟล์ db.py (ตัวเดียวกับที่บอทใช้) อยู่ในโฟลเดอร์เดียวกันกับไฟล์นี้

ENV ที่ต้องตั้ง (ดู .env.example):
  MONGO_URI, MONGO_DB_NAME        — เหมือนที่บอทใช้ (ต่อ MongoDB ตัวเดียวกัน)
  DISCORD_CLIENT_ID               — จาก Discord Developer Portal > OAuth2
  DISCORD_CLIENT_SECRET           — จากหน้าเดียวกัน
  DISCORD_BOT_TOKEN                — โทเคนบอทตัวเดียวกับที่ bot.py ใช้ (ไว้ดึงรายชื่อห้อง/ยศ)
  DASHBOARD_BASE_URL               — เช่น https://beluga-dashboard.onrender.com (ไม่มี / ปิดท้าย)
  SESSION_SECRET                   — string สุ่มยาว ๆ ไว้เซ็น session cookie
"""

import asyncio
import os
import time
from urllib.parse import urlencode

import httpx
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

import db

CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
BOT_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
BASE_URL = os.getenv("DASHBOARD_BASE_URL", "http://localhost:8000")
REDIRECT_URI = f"{BASE_URL}/callback"
SESSION_SECRET = os.getenv("SESSION_SECRET", "dev-secret-change-me")

DISCORD_API = "https://discord.com/api/v10"
MANAGE_GUILD = 0x20  # bitwise permission flag ของ Discord สำหรับ "Manage Server"

app = FastAPI(title="Beluga Dashboard")
app.add_middleware(SessionMiddleware, secret_key=SESSION_SECRET, same_site="lax")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

# แคชรายชื่อห้อง/ยศสั้น ๆ (60 วิ) กันยิง Discord API ถี่เกินไปตอนโหลดหน้าเดิมซ้ำ ๆ
_channel_cache: dict = {}
_role_cache: dict = {}
CACHE_TTL = 60


# ---------------- Discord API helpers ----------------

async def fetch_user_guilds(access_token: str) -> list:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API}/users/@me/guilds",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def fetch_user_info(access_token: str) -> dict:
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API}/users/@me",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        resp.raise_for_status()
        return resp.json()


async def bot_is_in_guild(guild_id: int) -> bool:
    """🆕 เช็คตรงกับ Discord API ว่าบอทอยู่ในเซิร์ฟนี้จริงไหม — เดิมเช็คจาก MongoDB (db.guilds)
    ซึ่งผิด เพราะ get_guild_config() สร้าง record ก็ต่อเมื่อมีการเรียกใช้ระบบใดระบบหนึ่งในเซิร์ฟนั้น
    แล้วเท่านั้น เซิร์ฟที่บอทอยู่จริงแต่ยังไม่เคยรันคำสั่ง/ยังไม่เคย on_ready ครบ จะไม่มี record เลย
    ทำให้ขึ้น "เชิญบอทเข้าเซิร์ฟนี้" ทั้งที่บอทอยู่แล้ว"""
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API}/guilds/{guild_id}",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
        )
        return resp.status_code == 200


async def fetch_guild_channels(guild_id: int) -> list:
    now = time.time()
    cached = _channel_cache.get(guild_id)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API}/guilds/{guild_id}/channels",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
        )
        if resp.status_code != 200:
            return []
        channels = resp.json()
    _channel_cache[guild_id] = (now, channels)
    return channels


async def fetch_guild_roles(guild_id: int) -> list:
    now = time.time()
    cached = _role_cache.get(guild_id)
    if cached and now - cached[0] < CACHE_TTL:
        return cached[1]
    async with httpx.AsyncClient() as client:
        resp = await client.get(
            f"{DISCORD_API}/guilds/{guild_id}/roles",
            headers={"Authorization": f"Bot {BOT_TOKEN}"},
        )
        if resp.status_code != 200:
            return []
        roles = resp.json()
    _role_cache[guild_id] = (now, roles)
    return roles


# ---------------- Auth guard ----------------

def require_login(request: Request):
    user = request.session.get("user")
    if not user:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return user


async def require_guild_access(request: Request, guild_id: int):
    """เช็คว่า user คนนี้มีสิทธิ์ Manage Server ในเซิร์ฟนี้จริง (ตาม session ที่ cache ไว้ตอน login)
    กันคนเดา guild_id ใน URL แล้วเข้าไปแก้ config เซิร์ฟที่ตัวเองไม่มีสิทธิ์"""
    user = require_login(request)
    manageable_ids = {g["id"] for g in request.session.get("manageable_guilds", [])}
    if str(guild_id) not in manageable_ids:
        raise HTTPException(status_code=403, detail="คุณไม่มีสิทธิ์ Manage Server ในเซิร์ฟนี้")
    return user


# ---------------- Auth routes ----------------

@app.get("/")
async def root(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/guilds")
    return RedirectResponse("/login")


@app.get("/login")
async def login(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/guilds")
    params = {
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "response_type": "code",
        "scope": "identify guilds",
    }
    return templates.TemplateResponse(
        "login.html", {"request": request, "oauth_url": f"{DISCORD_API}/oauth2/authorize?{urlencode(params)}"}
    )


@app.get("/callback")
async def callback(request: Request, code: str = None, error: str = None):
    if error or not code:
        return RedirectResponse("/login")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            f"{DISCORD_API}/oauth2/token",
            data={
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": REDIRECT_URI,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if token_resp.status_code != 200:
        raise HTTPException(status_code=400, detail="ล็อกอินกับ Discord ไม่สำเร็จ ลองใหม่อีกครั้ง")
    access_token = token_resp.json()["access_token"]

    user_info = await fetch_user_info(access_token)
    all_guilds = await fetch_user_guilds(access_token)

    # เก็บเฉพาะเซิร์ฟที่ user มีสิทธิ์ Manage Server เท่านั้น (เก็บ minimal fields กัน session cookie บวม)
    manage_guild_guilds = [g for g in all_guilds if int(g.get("permissions", 0)) & MANAGE_GUILD]

    # 🆕 เช็คสถานะบอทกับ Discord API ตรง ๆ พร้อมกันทุกเซิร์ฟ (เร็วกว่าเช็คทีละอัน) แทนการอ่านจาก
    # MongoDB ซึ่งไม่รับประกันว่าจะมี record ครบทุกเซิร์ฟที่บอทอยู่จริง
    presence_results = await asyncio.gather(
        *[bot_is_in_guild(int(g["id"])) for g in manage_guild_guilds]
    )

    manageable = [
        {
            "id": g["id"],
            "name": g["name"],
            "icon": g.get("icon"),
            "bot_present": is_present,
        }
        for g, is_present in zip(manage_guild_guilds, presence_results)
    ]

    request.session["user"] = {
        "id": user_info["id"],
        "username": user_info["username"],
        "avatar": user_info.get("avatar"),
    }
    request.session["manageable_guilds"] = manageable
    return RedirectResponse("/guilds")


@app.get("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login")


# ---------------- Guild picker ----------------

@app.get("/guilds")
async def guilds_page(request: Request):
    user = require_login(request)
    manageable = request.session.get("manageable_guilds", [])
    return templates.TemplateResponse(
        "guilds.html",
        {
            "request": request,
            "user": user,
            "guilds": manageable,
            "invite_base": f"https://discord.com/api/oauth2/authorize?client_id={CLIENT_ID}&permissions=8&scope=bot%20applications.commands&guild_id=",
        },
    )


# ---------------- Guild home (system toggle grid) ----------------

SYSTEM_LABELS = {
    "welcome": ("🎉", "ต้อนรับ"),
    "goodbye": ("👋", "อำลา"),
    "verify": ("🔐", "ยืนยันตัวตน"),
    "rules": ("📜", "กฎ"),
    "antiraid": ("🛡️", "Anti-Raid"),
    "autorole": ("🏅", "Auto Role"),
    "activity": ("📊", "Activity"),
    "ticket": ("🎫", "Ticket"),
    "logging": ("🧾", "Logging"),
}


@app.get("/dashboard/{guild_id}")
async def dashboard_home(request: Request, guild_id: int):
    user = await require_guild_access(request, guild_id)
    cfg = await db.get_guild_config(guild_id)
    guild_meta = next(
        (g for g in request.session.get("manageable_guilds", []) if g["id"] == str(guild_id)), None
    )
    return templates.TemplateResponse(
        "dashboard_home.html",
        {
            "request": request,
            "user": user,
            "guild_id": guild_id,
            "guild_name": guild_meta["name"] if guild_meta else "เซิร์ฟเวอร์",
            "systems": SYSTEM_LABELS,
            "systems_enabled": cfg["systems_enabled"],
        },
    )


@app.post("/dashboard/{guild_id}/toggle/{system_key}")
async def toggle_system(request: Request, guild_id: int, system_key: str):
    await require_guild_access(request, guild_id)
    cfg = await db.get_guild_config(guild_id)
    current = cfg["systems_enabled"].get(system_key, True)
    await db.set_system_enabled(guild_id, system_key, not current)
    return RedirectResponse(f"/dashboard/{guild_id}", status_code=303)


# ---------------- Generic section editor ----------------
# แต่ละระบบมี schema ของตัวเอง (field ไหนแก้ได้, เป็น type อะไร) — render ผ่าน template
# เดียวกันหมด (section_form.html) กันโค้ดซ้ำซ้อน ฟีเจอร์ขั้นสูง (preset, composite config,
# multi-role ticket, autorole timeline) ยังจัดการผ่าน slash command ในดิสคอร์ดเหมือนเดิมไปก่อน

SECTION_SCHEMA = {
    "welcome": {
        "title": "🎉 Welcome",
        "fields": [
            {"key": "channel_id", "label": "ห้องโพสต์ข้อความต้อนรับ", "type": "channel"},
            {"key": "title", "label": "หัวข้อ", "type": "text"},
            {"key": "description", "label": "คำอธิบาย", "type": "textarea"},
            {"key": "color", "label": "สี", "type": "color"},
            {"key": "image_url", "label": "รูปหลัก (URL)", "type": "text"},
            {"key": "font_key", "label": "Font key (ดูจาก /font-list)", "type": "text"},
            {"key": "delay_seconds", "label": "หน่วงเวลาก่อนส่ง (วินาที)", "type": "number"},
            {"key": "dm_enabled", "label": "ส่ง DM ต้อนรับแยกด้วย", "type": "checkbox"},
        ],
        "note": "ฟีเจอร์ขั้นสูง (preset, multi-embed, composite config) จัดการผ่าน /welcome-editor ในดิสคอร์ดครับ",
    },
    "goodbye": {
        "title": "👋 Goodbye",
        "fields": [
            {"key": "channel_id", "label": "ห้องโพสต์ข้อความอำลา", "type": "channel"},
            {"key": "title", "label": "หัวข้อ", "type": "text"},
            {"key": "description", "label": "คำอธิบาย", "type": "textarea"},
            {"key": "color", "label": "สี", "type": "color"},
            {"key": "image_url", "label": "รูปหลัก (URL)", "type": "text"},
            {"key": "font_key", "label": "Font key", "type": "text"},
        ],
    },
    "verify": {
        "title": "🔐 Verify",
        "fields": [
            {"key": "role_id", "label": "ยศที่จะมอบให้หลังยืนยันตัวตน", "type": "role"},
            {"key": "explain_text", "label": "ข้อความ 'ทำไมต้องยืนยัน'", "type": "textarea"},
            {"key": "color", "label": "สี", "type": "color"},
            {"key": "confirm_emoji", "label": "อีโมจิปุ่มยืนยัน", "type": "text"},
            {"key": "explain_emoji", "label": "อีโมจิปุ่มคำถาม", "type": "text"},
        ],
        "note": "ตั้ง Verify Gate (สร้างห้อง+ล็อกทั้งเซิร์ฟ) ยังต้องใช้ /verify-setup-gate ในดิสคอร์ดครับ",
    },
    "rules": {
        "title": "📜 Rules",
        "fields": [
            {"key": "title", "label": "หัวข้อ", "type": "text"},
            {"key": "rules_text", "label": "ข้อความกฎ", "type": "textarea"},
            {"key": "rank_text", "label": "เงื่อนไขเลื่อนยศ", "type": "textarea"},
            {"key": "color", "label": "สี", "type": "color"},
        ],
        "note": "บันทึกที่นี่จะอัปเดตข้อความกฎที่โพสต์ไว้แล้วให้อัตโนมัติ (ถ้าเคย /rules-post ไปแล้ว)",
    },
    "ticket": {
        "title": "🎫 Ticket",
        "fields": [
            {"key": "category_id", "label": "หมวดหมู่สร้างห้องตั๋ว", "type": "category"},
            {"key": "title", "label": "หัวข้อแผง", "type": "text"},
            {"key": "description", "label": "คำอธิบายแผง", "type": "textarea"},
            {"key": "color", "label": "สี", "type": "color"},
            {"key": "welcome_text", "label": "ข้อความต้อนรับในห้องตั๋ว", "type": "textarea"},
        ],
        "note": "เพิ่ม/ลบยศทีมงาน ยังต้องใช้ /ticket-setup กับ /ticket-remove-support-role ในดิสคอร์ดครับ",
    },
    "antiraid": {
        "title": "🛡️ Anti-Raid",
        "fields": [
            {"key": "join_threshold", "label": "จำนวนคนเข้าที่ถือว่าผิดปกติ", "type": "number"},
            {"key": "window_seconds", "label": "ภายในกี่วินาที", "type": "number"},
            {"key": "min_account_age_days", "label": "อายุบัญชีขั้นต่ำ (วัน)", "type": "number"},
            {"key": "alert_channel_id", "label": "ห้องแจ้งเตือน", "type": "channel"},
        ],
    },
    "autorole": {
        "title": "🏅 Auto Role",
        "fields": [
            {"key": "auto_grant", "label": "แจกยศอัตโนมัติ (ปิด = ให้กดรับเอง)", "type": "checkbox"},
        ],
        "note": "ตั้งวัน/ยศ/ชื่อระดับในตารางเวลา ยังต้องใช้ /autorole-set ในดิสคอร์ดครับ",
    },
    "logging": {
        "title": "🧾 Logging",
        "fields": [
            {"key": "channel_id", "label": "ห้องบันทึก log", "type": "channel"},
        ],
    },
}


@app.get("/dashboard/{guild_id}/{section}")
async def section_form(request: Request, guild_id: int, section: str):
    await require_guild_access(request, guild_id)
    if section not in SECTION_SCHEMA:
        raise HTTPException(status_code=404, detail="ไม่พบระบบนี้")

    cfg = await db.get_guild_config(guild_id)
    section_cfg = cfg.get(section, {})
    schema = SECTION_SCHEMA[section]

    channels, roles = [], []
    field_types = {f["type"] for f in schema["fields"]}
    if "channel" in field_types:
        raw = await fetch_guild_channels(guild_id)
        channels = [c for c in raw if c.get("type") == 0]  # 0 = text channel
    if "category" in field_types:
        raw = await fetch_guild_channels(guild_id)
        channels = channels or []
        categories = [c for c in raw if c.get("type") == 4]  # 4 = category
    else:
        categories = []
    if "role" in field_types:
        roles = [r for r in await fetch_guild_roles(guild_id) if r["name"] != "@everyone"]

    return templates.TemplateResponse(
        "section_form.html",
        {
            "request": request,
            "guild_id": guild_id,
            "section": section,
            "schema": schema,
            "values": section_cfg,
            "channels": channels,
            "categories": categories,
            "roles": roles,
        },
    )


@app.post("/dashboard/{guild_id}/{section}")
async def section_save(request: Request, guild_id: int, section: str):
    await require_guild_access(request, guild_id)
    if section not in SECTION_SCHEMA:
        raise HTTPException(status_code=404, detail="ไม่พบระบบนี้")

    form = await request.form()
    updates = {}
    for field in SECTION_SCHEMA[section]["fields"]:
        key, ftype = field["key"], field["type"]
        if ftype == "checkbox":
            updates[key] = key in form
        elif ftype == "number":
            raw = form.get(key, "")
            updates[key] = int(raw) if raw.strip().isdigit() else 0
        elif ftype in ("channel", "category", "role"):
            raw = form.get(key, "")
            updates[key] = int(raw) if raw.strip().isdigit() else None
        else:
            updates[key] = form.get(key, "")

    await db.update_guild_section(guild_id, section, updates)
    return RedirectResponse(f"/dashboard/{guild_id}/{section}?saved=1", status_code=303)


# ---------------- Theme (apply one color to many sections) ----------------

THEMED_SECTIONS = ["welcome", "goodbye", "verify", "rules", "ticket"]


@app.get("/dashboard/{guild_id}/tools/theme")
async def theme_form(request: Request, guild_id: int):
    await require_guild_access(request, guild_id)
    cfg = await db.get_guild_config(guild_id)
    colors = {s: cfg.get(s, {}).get("color", "#5865f2") for s in THEMED_SECTIONS}
    return templates.TemplateResponse(
        "theme.html",
        {"request": request, "guild_id": guild_id, "colors": colors, "sections": THEMED_SECTIONS},
    )


@app.post("/dashboard/{guild_id}/tools/theme")
async def theme_save(request: Request, guild_id: int, hex_color: str = Form(...)):
    await require_guild_access(request, guild_id)
    hex_color = hex_color.strip()
    if not hex_color.startswith("#"):
        hex_color = "#" + hex_color
    for section in THEMED_SECTIONS:
        await db.update_guild_section(guild_id, section, {"color": hex_color})
    return RedirectResponse(f"/dashboard/{guild_id}/tools/theme?saved=1", status_code=303)
