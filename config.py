"""Konfigurasi terpusat. Nilai sensitif diambil dari .env, bukan dari source code."""

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


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


# ===== AKUN TIKTOK =====
TIKTOK_EMAIL = _require("TIKTOK_EMAIL")
TIKTOK_PASSWORD = _require("TIKTOK_PASSWORD")

# ===== EMAIL (untuk ambil OTP via IMAP) =====
EMAIL_IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER", "imap.gmail.com")
EMAIL_IMAP_PORT = int(os.getenv("EMAIL_IMAP_PORT", "993"))
EMAIL_ACCOUNT = _require("EMAIL_ACCOUNT")
EMAIL_PASSWORD = _require("EMAIL_APP_PASSWORD")

# ===== TARGET REPORT =====
# Sengaja TIDAK ada di sini. Link video ditanyakan langsung saat script
# dijalankan (lihat ask_target_video_url() di main.py), supaya target selalu
# ditentukan sadar tiap run - tidak pernah terwarisi diam-diam dari file.

# ===== SESI BROWSER =====
# Cookies login yang disimpan setelah login berhasil. Selama file ini valid,
# run berikutnya melewati login sepenuhnya - itu yang membuat OTP dan captcha
# jarang muncul. Jangan pernah dibagikan atau di-commit.
STORAGE_STATE_PATH = os.getenv("STORAGE_STATE_PATH", "tiktok_storage_state.json")

# ===== PERFORMA =====
HEADLESS = _flag("HEADLESS", False)
BLOCK_HEAVY_RESOURCES = _flag("BLOCK_HEAVY_RESOURCES", True)

# Lebih cepat, tapi tampilan captcha ikut hilang sehingga tidak bisa diselesaikan.
BLOCK_IMAGES = _flag("BLOCK_IMAGES", False)

# FAST: transisi UI lokal. SLOW: elemen yang menunggu response jaringan.
FAST_TIMEOUT = int(os.getenv("FAST_TIMEOUT", "3000"))
SLOW_TIMEOUT = int(os.getenv("SLOW_TIMEOUT", "10000"))

PAUSE_AT_END = _flag("PAUSE_AT_END", False)

# Logout membuang sesi tersimpan, sehingga setiap run wajib login penuh lagi.
# Login baru berulang adalah pemicu utama OTP dan captcha - nyalakan hanya
# kalau cookies memang tidak boleh tertinggal (mis. mesin dipakai bersama).
LOGOUT_AFTER_REPORT = _flag("LOGOUT_AFTER_REPORT", False)

# Hapus file sesi otomatis HANYA saat terdeteksi rusak (halaman video
# menampilkan "Log masuk"). Bukan hapus-sesi-tiap-run.
AUTO_CLEAR_STALE_SESSION = _flag("AUTO_CLEAR_STALE_SESSION", True)
