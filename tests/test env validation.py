"""
tests/test_env_validation.py — ทดสอบ ENV validation ตอน startup ของ dashboard (app.py)

รันจาก root ของ dashboard repo (โฟลเดอร์เดียวกับ app.py, db.py, static/, templates/, routers/):
    pytest tests/test_env_validation.py -v

ทำไมใช้ subprocess แทนการ import app ตรง ๆ ในเทส:
ENV validation ทั้งหมดทำงานตอน module-level (import time) ไม่ใช่ตอน request จริง ถ้า import app
ตรง ๆ ในเทส process เดียวกัน พอเทสแรก fail (RuntimeError ตอน import) โมดูล app จะค้างอยู่ในสถานะ
import ไม่สำเร็จใน sys.modules ทำให้เทสถัดไป import ซ้ำไม่ได้ถูกต้อง (Python cache การ import)
spawn process ใหม่ทุกครั้งด้วย subprocess เลยตรงกับพฤติกรรมจริงที่สุด (เหมือนตอน Render สั่ง
`uvicorn app:app` ใหม่ทุกครั้งที่ deploy) และแยกแต่ละเทสออกจากกันสมบูรณ์
"""
import os
import subprocess
import sys

import pytest

# ค่า ENV ที่ valid ครบทุกตัว ใช้เป็น baseline แล้วแต่ละเทสค่อยเบี่ยงเบนจากตรงนี้
# MONGO_URI ใช้ localhost เฉย ๆ พอ เพราะ motor ต่อแบบ lazy — ไม่ query จริงตอน import
BASE_VALID_ENV = {
    "MONGO_URI": "mongodb://localhost:27017",
    "MONGO_DB_NAME": "beluga_control_test",
    "DISCORD_CLIENT_ID": "123456789012345678",
    "DISCORD_CLIENT_SECRET": "fake_client_secret_for_test",
    "DISCORD_BOT_TOKEN": "fake_bot_token_for_test",
    "SESSION_SECRET": "fake_session_secret_at_least_this_long_for_test",
    "DASHBOARD_BASE_URL": "http://localhost:8000",
}

REQUIRED_ENV_NAMES = [
    "MONGO_URI",
    "DISCORD_BOT_TOKEN",
    "DISCORD_CLIENT_ID",
    "DISCORD_CLIENT_SECRET",
    "SESSION_SECRET",
]


def _run_import_app(env_overrides: dict) -> subprocess.CompletedProcess:
    """spawn python process ใหม่ พยายาม `import app` ด้วย env ที่กำหนด
    ค่าใน env_overrides ที่เป็น None แปลว่า 'ลบ ENV ตัวนี้ออกไปเลย' (ไม่ใช่ตั้งเป็นค่าว่าง)"""
    env = {**os.environ, **BASE_VALID_ENV}
    for key, val in env_overrides.items():
        if val is None:
            env.pop(key, None)
        else:
            env[key] = val
    return subprocess.run(
        [sys.executable, "-c", "import app"],
        env=env,
        cwd=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        capture_output=True,
        text=True,
        timeout=30,
    )


class TestEnvValidationHappyPath:
    def test_all_env_present_startup_succeeds(self):
        """ENV ครบทุกตัว ค่าถูกต้อง → import ผ่าน ไม่ crash"""
        result = _run_import_app({})
        assert result.returncode == 0, f"ควร import สำเร็จแต่ fail:\n{result.stderr}"


class TestEnvValidationMissing:
    @pytest.mark.parametrize("env_name", REQUIRED_ENV_NAMES)
    def test_missing_required_env_fails_startup(self, env_name):
        """ENV ที่ required ตัวใดตัวหนึ่งหายไปเลย (ไม่ได้ตั้งค่าอะไรเลย) → ต้อง fail-fast"""
        result = _run_import_app({env_name: None})
        assert result.returncode != 0, f"{env_name} หายไปแต่ import ผ่าน — ควร fail-fast"
        assert env_name in result.stderr, f"error message ควรบอกชื่อ {env_name} ชัดเจน"
        assert "ENV VALIDATION FAILED" in result.stderr


class TestEnvValidationWhitespace:
    @pytest.mark.parametrize("env_name", REQUIRED_ENV_NAMES)
    @pytest.mark.parametrize("bad_value", [" ", "   ", "\t", "\n", "\t\n "])
    def test_whitespace_only_env_fails_startup(self, env_name, bad_value):
        """ENV มีค่าเป็น whitespace ล้วน ๆ (ไม่ใช่หายไปเลย) → ต้อง fail-fast เหมือนกัน"""
        result = _run_import_app({env_name: bad_value})
        assert result.returncode != 0, f"{env_name}={bad_value!r} แต่ import ผ่าน — ควร fail-fast"
        assert env_name in result.stderr


class TestSessionSecretNoFallback:
    def test_session_secret_has_no_hardcoded_fallback_in_source(self):
        """ยืนยันว่าไม่มี fallback value เดิม ('dev-secret-change-me') หลงเหลืออยู่ในซอร์สโค้ด"""
        app_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "app.py")
        with open(app_path, encoding="utf-8") as f:
            content = f.read()
        assert "dev-secret-change-me" not in content, (
            "เจอ fallback value เดิมยังหลงเหลืออยู่ใน app.py"
        )

    def test_session_secret_missing_fails_startup(self):
        """ซ้ำกับ TestEnvValidationMissing แต่เจาะจง SESSION_SECRET เพราะเป็นจุดที่สำคัญสุด (security)"""
        result = _run_import_app({"SESSION_SECRET": None})
        assert result.returncode != 0


class TestNoDeadCodeReferences:
    def test_dashboard_db_not_referenced_anywhere(self):
        """ยืนยันว่าไม่มีไฟล์ .py ไหนใน repo reference dashboard_db อีกต่อไป"""
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        result = subprocess.run(
            ["grep", "-rl", "dashboard_db", "--include=*.py", "."],
            cwd=repo_root,
            capture_output=True,
            text=True,
        )
        assert result.stdout.strip() == "", f"ยังเจอไฟล์ที่ reference dashboard_db อยู่:\n{result.stdout}"

    def test_dashboard_db_file_removed(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assert not os.path.exists(os.path.join(repo_root, "dashboard_db.py"))

    def test_pages_file_removed(self):
        repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        assert not os.path.exists(os.path.join(repo_root, "pages.py"))


class TestMongoDbNameWhitespaceGuard:
    """ทดสอบ guard ที่มีอยู่แล้วใน db.py (ไม่ได้แก้ตอนนี้ — แค่เพิ่มเทสยืนยันว่ายังทำงานถูกต้อง
    ผ่านการ import app.py ซึ่ง import db.py อยู่แล้ว) ตรงตาม test case 1-4 ในสเปค"""

    def test_mongo_db_name_normal_value_passes(self):
        """MONGO_DB_NAME=beluga_control → PASS"""
        result = _run_import_app({"MONGO_DB_NAME": "beluga_control"})
        assert result.returncode == 0, f"ควรผ่านแต่ fail:\n{result.stderr}"

    @pytest.mark.parametrize(
        "bad_value,label",
        [
            (" beluga_control", "leading space"),
            ("beluga_control ", "trailing space"),
            ("beluga_control\n", "trailing newline"),
            ("beluga_control\t", "trailing tab"),
        ],
    )
    def test_mongo_db_name_whitespace_fails(self, bad_value, label):
        """MONGO_DB_NAME ที่มี whitespace แฝงหัว/ท้าย → ต้อง FAIL ทุกแบบ"""
        result = _run_import_app({"MONGO_DB_NAME": bad_value})
        assert result.returncode != 0, f"MONGO_DB_NAME={bad_value!r} ({label}) แต่ import ผ่าน — ควร fail-fast"


class TestFullStartupSucceedsWithAllValidEnv:
    def test_website_import_startup_configuration_passes(self):
        """test case 10 ในสเปค: ENV ครบถ้วนถูกต้องทั้งหมด → import/startup configuration ผ่านสมบูรณ์"""
        result = _run_import_app({})
        assert result.returncode == 0, f"Startup ควรผ่านแต่ fail:\n{result.stderr}"
        assert result.stderr.strip() == "" or "ENV VALIDATION FAILED" not in result.stderr



    def test_secret_value_never_appears_in_startup_output(self):
        """ตั้งค่า secret เป็น canary value ที่จำได้ง่าย แล้วยืนยันว่าค่านั้นไม่โผล่ใน stdout/stderr
        เลยตอน startup (ทั้งกรณี validation ผ่านและกรณีอื่น ๆ ที่เกี่ยวข้อง)"""
        canary = "CANARY_SECRET_VALUE_MUST_NEVER_APPEAR_XYZ123"
        result = _run_import_app({"SESSION_SECRET": canary})
        assert result.returncode == 0  # canary เป็นค่าจริง (ไม่ว่าง) ควรผ่าน validation
        assert canary not in result.stdout
        assert canary not in result.stderr
