"""
routers/onboarding.py — 🧙 Welcome Designer Wizard (เว็บ)
เสริมจาก generic section editor เดิม (GET/POST /dashboard/{guild_id}/welcome ใน app.py)
เพราะฟอร์มเดิมใน SECTION_SCHEMA มีแค่ 8 ฟิลด์พื้นฐาน ไม่รองรับ author/footer/fields/
composite config/preset เหมือนฝั่งบอท — ใช้เส้นทางแยก /welcome/wizard ไม่ชนกับของเดิม

ใช้ db.py ตัวเดียวกับที่ app.py import อยู่แล้ว (ไม่มี DB layer แยก) และ endpoint
อัปโหลดรูปก็ใช้ตัวเดิมที่ app.py มีอยู่แล้ว (/dashboard/{guild_id}/upload-image)
ไม่สร้างซ้ำ — ฝั่ง template แค่เรียก endpoint นั้นตรงๆ

Mount เข้า app.py หลักด้วย (วางไว้หลังบรรทัด app = FastAPI(...) และ templates = ... ก็ได้):
    from routers.onboarding import router as onboarding_router
    app.include_router(onboarding_router)
"""

import sys

from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates

import db

router = APIRouter()
templates = Jinja2Templates(directory="templates")


async def _guard(request: Request, guild_id: int):
    """ใช้ auth guard ตัวเดียวกับ app.py (require_guild_access) — import แบบ deferred
    (ข้างในฟังก์ชัน ไม่ใช่หัวไฟล์) เพื่อกัน circular import เพราะ app.py เป็นฝ่าย
    include_router(onboarding_router) อยู่แล้ว ถ้า import ตอนหัวไฟล์จะวนกลับไม่จบ"""
    from app import require_guild_access
    return await require_guild_access(request, guild_id)


@router.get("/dashboard/{guild_id}/welcome/wizard", response_class=HTMLResponse)
async def welcome_wizard_page(request: Request, guild_id: int):
    await _guard(request, guild_id)
    cfg = await db.get_guild_config(guild_id)
    presets = await db.list_welcome_presets(guild_id)
    return templates.TemplateResponse(
        "onboarding_wizard.html",
        {"request": request, "guild_id": guild_id, "welcome": cfg["welcome"], "presets": presets},
    )


@router.post("/dashboard/{guild_id}/welcome/wizard/save")
async def welcome_wizard_save(request: Request, guild_id: int, payload: dict):
    await _guard(request, guild_id)
    allowed_keys = set(db.DEFAULT_CONFIG["welcome"].keys())
    clean = {k: v for k, v in payload.items() if k in allowed_keys}
    if not clean:
        raise HTTPException(400, "ไม่มีฟิลด์ที่ถูกต้องให้บันทึก")
    await db.update_guild_section(guild_id, "welcome", clean)
    return JSONResponse({"ok": True, "saved_fields": list(clean.keys())})


@router.post("/dashboard/{guild_id}/welcome/wizard/preset/save")
async def welcome_wizard_save_preset(request: Request, guild_id: int, payload: dict):
    await _guard(request, guild_id)
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "ต้องใส่ชื่อ preset")
    allowed_keys = set(db.DEFAULT_CONFIG["welcome"].keys())
    snapshot = {k: v for k, v in payload.items() if k in allowed_keys}
    await db.save_welcome_preset(guild_id, name, snapshot)
    return JSONResponse({"ok": True})


@router.get("/dashboard/{guild_id}/welcome/wizard/preset/{name}")
async def welcome_wizard_load_preset(request: Request, guild_id: int, name: str):
    await _guard(request, guild_id)
    config = await db.load_welcome_preset(guild_id, name)
    if config is None:
        raise HTTPException(404, f"ไม่พบ preset '{name}'")
    return JSONResponse({"ok": True, "config": config})
