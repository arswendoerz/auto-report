"""
config.py - Konfigurasi terpusat untuk semua script.

Nilai sensitif (kredensial akun, app password Gmail) diambil dari file
.env di folder ini agar tidak lagi tertulis langsung di source code.
Salin .env.example menjadi .env lalu isi nilainya sebelum menjalankan
script apa pun di folder ini.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()


def _require(name: str) -> str:
    value = os.getenv(name)
    if not value:
        print(f"[CONFIG] ERROR: Variabel '{name}' belum diisi di file .env")
        print("[CONFIG] Salin .env.example menjadi .env lalu isi nilainya.")
        sys.exit(1)
    return value


# ===== AKUN TIKTOK =====
TIKTOK_EMAIL = _require("TIKTOK_EMAIL")
TIKTOK_PASSWORD = _require("TIKTOK_PASSWORD")

# ===== EMAIL (untuk ambil OTP via IMAP) =====
EMAIL_IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER", "imap.gmail.com")
EMAIL_IMAP_PORT = int(os.getenv("EMAIL_IMAP_PORT", "993"))
EMAIL_ACCOUNT = _require("EMAIL_ACCOUNT")
EMAIL_PASSWORD = _require("EMAIL_APP_PASSWORD")

# ===== TARGET REPORT =====
TARGET_VIDEO_URL = os.getenv(
    "TARGET_VIDEO_URL",
    "https://www.tiktok.com/@agung.brilian60/video/7674344770562575634",
)

# ===== SESI BROWSER (cookies login TikTok) =====
# Disimpan setelah login berhasil supaya run berikutnya tidak perlu
# login dari nol lagi (mengurangi frekuensi verifikasi/OTP/captcha yang
# biasanya dipicu oleh login baru). File ini berisi cookies sesi asli —
# jangan pernah dibagikan atau di-commit (sudah masuk .gitignore).
STORAGE_STATE_PATH = os.getenv("STORAGE_STATE_PATH", "tiktok_storage_state.json")
