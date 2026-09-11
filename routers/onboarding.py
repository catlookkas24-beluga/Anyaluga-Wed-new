"""
routers/onboarding.py — 🧙 Welcome Designer Wizard (เว็บ)
คู่กับ /welcome-wizard ฝั่งบอท — อ่าน/เขียน field เดียวกันใน MongoDB (welcome.*)
ผ่าน dashboard_db.py ที่ schema ตรงกับ beluga-bot/db.py ทุกฟิลด์

Mount เข้า FastAPI app หลักด้วย:
    from routers.onboarding import router as onboarding_router
    app.include_router(onboarding_router)

ต้องมี middleware auth เดิมของ dashboard (Discord OAuth2) คลุม path /onboard/*
และ /api/onboard/* อยู่แล้ว — ไฟล์นี้ไม่ได้จัดการ login เอง
"""

from fastapi import APIRouter, Request, UploadFile, File, Form, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import io

import dashboard_db as db

router = APIRouter()
templates = Jinja2Templates(directory="templates")

ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/webp", "image/gif"}
MAX_IMAGE_BYTES = 5 * 1024 * 1024


@router.get("/onboard/{guild_id}", response_class=HTMLResponse)
async def onboarding_page(request: Request, guild_id: int):
    welcome_cfg = await db.get_welcome_config(guild_id)
    presets = await db.list_welcome_presets(guild_id)
    return templates.TemplateResponse(
        "onboarding_wizard.html",
        {
            "request": request,
            "guild_id": guild_id,
            "welcome": welcome_cfg,
            "presets": presets,
        },
    )


@router.post("/api/onboard/{guild_id}/welcome")
async def save_welcome(guild_id: int, payload: dict):
    """บันทึกค่า welcome ทั้งหมดทีเดียวตอนกด "บันทึกการตั้งค่า" ในขั้นตอนสุดท้าย
    payload ต้องเป็น dict ของฟิลด์ใน WELCOME_DEFAULTS เท่านั้น (ฟิลด์แปลกปลอมจะถูกกรองทิ้ง)"""
    allowed_keys = set(db.WELCOME_DEFAULTS.keys())
    clean = {k: v for k, v in payload.items() if k in allowed_keys}
    if not clean:
        raise HTTPException(400, "ไม่มีฟิลด์ที่ถูกต้องให้บันทึก")
    await db.update_welcome_config(guild_id, clean)
    return JSONResponse({"ok": True, "saved_fields": list(clean.keys())})


@router.post("/api/onboard/{guild_id}/upload-image")
async def upload_image(request: Request, guild_id: int, file: UploadFile = File(...)):
    if file.content_type not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(400, "รองรับเฉพาะ .png .jpg .jpeg .webp .gif เท่านั้น")
    data = await file.read()
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(400, "ไฟล์ใหญ่เกิน 5MB")

    file_id = await db.save_welcome_image(guild_id, file.filename, data, file.content_type)
    # สร้าง URL แบบ absolute เพราะ embed ของ Discord ต้อง fetch รูปจาก URL จริงได้
    image_url = str(request.base_url).rstrip("/") + f"/assets/{file_id}"
    return JSONResponse({"ok": True, "file_id": file_id, "image_url": image_url})


@router.get("/assets/{file_id}")
async def serve_asset(file_id: str):
    """สตรีมไฟล์จาก GridFS ออกเป็น URL สาธารณะ ให้ Discord embed fetch ได้จริง
    (bucket เดียวกับที่ /asset-upload ฝั่งบอทใช้ — ไฟล์ที่อัปทั้งสองทางเห็นกันหมด)"""
    try:
        data, content_type = await db.read_asset_bytes(file_id)
    except Exception:
        raise HTTPException(404, "ไม่พบไฟล์นี้")
    return StreamingResponse(io.BytesIO(data), media_type=content_type)


@router.post("/api/onboard/{guild_id}/preset/save")
async def save_preset(guild_id: int, payload: dict):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "ต้องใส่ชื่อ preset")
    config_snapshot = {k: v for k, v in payload.items() if k in db.WELCOME_DEFAULTS and k != "name"}
    await db.save_welcome_preset(guild_id, name, {**db.WELCOME_DEFAULTS, **config_snapshot})
    return JSONResponse({"ok": True})


@router.get("/api/onboard/{guild_id}/preset/{name}")
async def load_preset(guild_id: int, name: str):
    config = await db.load_welcome_preset(guild_id, name)
    if config is None:
        raise HTTPException(404, f"ไม่พบ preset '{name}'")
    return JSONResponse({"ok": True, "config": config})
