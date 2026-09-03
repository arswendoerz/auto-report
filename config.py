"""Konfigurasi terpusat.

Nilai sensitif diambil dari .env, bukan dari source code. Kredensial TIDAK lagi
memaksa keluar saat import: gui.py mengisinya dari form lewat
apply_credentials(), jadi validasi dipindah ke pemanggil (require_credentials()).
"""

import io
import os
import re
import sys

from dotenv import load_dotenv

load_dotenv()

ACCOUNT_KEYS = (
    "TIKTOK_EMAIL",
    "TIKTOK_PASSWORD",
    "EMAIL_ACCOUNT",
    "EMAIL_APP_PASSWORD",
)
ACCOUNT_HEADER_PATTERN = re.compile(r"^\s*#\s*((?:akun|account)\b.*)$", re.I)
ACCOUNT_VALUE_PATTERN = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*(.*)$")


def load_saved_accounts(path):
    """Baca beberapa blok akun dari file teks (mis. akun.txt)."""
    if not path:
        return None, [], None
    source = path
    if not os.path.isfile(source):
        return source, [], "File tidak ditemukan."

    try:
        with io.open(source, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
    except OSError as exc:
        return source, [], str(exc)

    accounts = []
    values = {}
    label = ""

    def add_account():
        nonlocal values, label
        if not values:
            return
        email = values.get("TIKTOK_EMAIL", "").strip()
        display = label.strip() or email or f"Akun {len(accounts) + 1}"
        if email and email.lower() not in display.lower():
            display = f"{display} — {email}"
        accounts.append({"label": display, "values": values})
        values = {}
        label = ""

    for raw_line in lines:
        header = ACCOUNT_HEADER_PATTERN.match(raw_line)
        if header:
            add_account()
            label = header.group(1).strip()
            continue

        match = ACCOUNT_VALUE_PATTERN.match(raw_line)
        if not match:
            continue
        key, value = match.groups()
        if key not in ACCOUNT_KEYS:
            continue
        if key == "TIKTOK_EMAIL" and "TIKTOK_EMAIL" in values:
            add_account()
        values[key] = value.strip()

    add_account()
    return source, accounts, None


def _flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "y", "on")


# ===== AKUN TIKTOK =====
TIKTOK_EMAIL = os.getenv("TIKTOK_EMAIL", "").strip()
TIKTOK_PASSWORD = os.getenv("TIKTOK_PASSWORD", "")

# ===== EMAIL (untuk ambil OTP via IMAP) =====
EMAIL_IMAP_SERVER = os.getenv("EMAIL_IMAP_SERVER", "imap.gmail.com")
EMAIL_IMAP_PORT = int(os.getenv("EMAIL_IMAP_PORT", "993"))
EMAIL_ACCOUNT = os.getenv("EMAIL_ACCOUNT", "").strip()
EMAIL_PASSWORD = os.getenv("EMAIL_APP_PASSWORD", "")

# Label untuk pesan error. Kunci = nama variabel di modul ini, nilai = nama
# variabel .env yang setara (dipakai juga saat gui.py menyimpan ke .env).
CREDENTIAL_ENV_NAMES = {
    "TIKTOK_EMAIL": "TIKTOK_EMAIL",
    "TIKTOK_PASSWORD": "TIKTOK_PASSWORD",
    "EMAIL_ACCOUNT": "EMAIL_ACCOUNT",
    "EMAIL_PASSWORD": "EMAIL_APP_PASSWORD",
}


def apply_credentials(
    tiktok_email: str = None,
    tiktok_password: str = None,
    email_account: str = None,
    email_app_password: str = None,
):
    """Override kredensial saat runtime (dipakai gui.py).

    Modul lain WAJIB membacanya lewat `config.NAMA` - bukan `from config import
    NAMA` - supaya nilai baru di sini ikut terbaca. `from ... import` mengikat
    nilainya sekali saat import dan tidak pernah ikut berubah.
    """
    global TIKTOK_EMAIL, TIKTOK_PASSWORD, EMAIL_ACCOUNT, EMAIL_PASSWORD

    if tiktok_email is not None:
        TIKTOK_EMAIL = tiktok_email.strip()
    if tiktok_password is not None:
        TIKTOK_PASSWORD = tiktok_password
    if email_account is not None:
        EMAIL_ACCOUNT = email_account.strip()
    if email_app_password is not None:
        EMAIL_PASSWORD = email_app_password


def missing_credentials() -> list:
    """Nama variabel .env yang masih kosong."""
    values = {
        "TIKTOK_EMAIL": TIKTOK_EMAIL,
        "TIKTOK_PASSWORD": TIKTOK_PASSWORD,
        "EMAIL_ACCOUNT": EMAIL_ACCOUNT,
        "EMAIL_PASSWORD": EMAIL_PASSWORD,
    }
    return [CREDENTIAL_ENV_NAMES[key] for key, value in values.items() if not value]


def require_credentials():
    """Keluar dengan pesan jelas kalau kredensial belum lengkap (dipakai CLI)."""
    missing = missing_credentials()
    if not missing:
        return
    for name in missing:
        print(f"[CONFIG] ERROR: Variabel '{name}' belum diisi di file .env")
    print("[CONFIG] Salin .env.example menjadi .env lalu isi nilainya,")
    print("[CONFIG] atau jalankan 'python gui.py' dan isi lewat form.")
    sys.exit(1)


# ===== TARGET REPORT =====
# Sengaja TIDAK ada di sini. Link video ditanyakan langsung saat script
# dijalankan (lihat ask_target_video_url() di main.py) atau diisi di form
# gui.py, supaya target selalu ditentukan sadar tiap run - tidak pernah
# terwarisi diam-diam dari file.

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
