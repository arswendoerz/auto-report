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

# ===== PERFORMA =====
# Semua nilai di bawah punya default yang mempertahankan perilaku lama,
# kecuali PAUSE_AT_END (lihat catatannya).

# Headless jauh lebih cepat, tapi TikTok lebih sering memunculkan
# verifikasi kalau browser tidak terlihat. Default tetap False seperti
# versi lama; ubah lewat .env kalau mau coba.
HEADLESS = _flag("HEADLESS", False)

# Blokir stream video/font + host telemetri. Ini pemangkas waktu muat
# halaman video yang paling besar dan tidak mempengaruhi DOM yang dipakai
# otomasi. Lihat perf.block_heavy_resources().
BLOCK_HEAVY_RESOURCES = _flag("BLOCK_HEAVY_RESOURCES", True)

# Blokir gambar juga. Lebih cepat lagi, TAPI tampilan captcha ikut hilang
# sehingga alur captcha tidak bisa jalan. Default False.
BLOCK_IMAGES = _flag("BLOCK_IMAGES", False)

# Timeout untuk elemen yang mestinya sudah ada begitu aksi sebelumnya
# selesai (transisi UI lokal, tanpa request jaringan).
FAST_TIMEOUT = int(os.getenv("FAST_TIMEOUT", "3000"))

# Timeout untuk elemen yang menunggu response jaringan (halaman baru,
# dialog report yang isinya di-fetch, halaman verifikasi login).
SLOW_TIMEOUT = int(os.getenv("SLOW_TIMEOUT", "10000"))

# PERUBAHAN PERILAKU: versi lama selalu berhenti di `input("Tekan Enter
# untuk menutup browser...")` di akhir. Itu membuat script tidak pernah
# bisa selesai sendiri. Sekarang default-nya langsung selesai, dan status
# akhirnya dicetak eksplisit. Set PAUSE_AT_END=true di .env untuk
# mengembalikan jeda manual seperti sebelumnya.
PAUSE_AT_END = _flag("PAUSE_AT_END", False)

# Keluar dari akun setelah report selesai, lalu hapus file sesi.
#
# Default MATI, dan pertimbangkan baik-baik sebelum menyalakannya: sesi
# tersimpan itu justru yang membuat run berikutnya bisa melewati login,
# OTP, dan captcha sepenuhnya. Kalau logout dinyalakan, setiap run wajib
# login penuh dari nol - dan login baru berulang-ulang adalah pemicu utama
# verifikasi OTP, captcha, serta penandaan anti-bot oleh TikTok.
#
# Nyalakan kalau memang butuh, misalnya mesin dipakai bersama orang lain
# dan cookies sesi tidak boleh tertinggal.
LOGOUT_AFTER_REPORT = _flag("LOGOUT_AFTER_REPORT", False)

# Hapus file sesi secara OTOMATIS, tapi HANYA kalau sesinya terdeteksi
# rusak - yaitu saat halaman video menampilkan tombol "Log masuk" padahal
# cek sesi di homepage sempat lolos. Kondisi itu berarti cookies-nya sudah
# tidak dipercaya TikTok dan tidak akan pernah bisa membuka menu report,
# berapa kali pun diulang.
#
# CATATAN PENTING: ini BUKAN "hapus sesi tiap run". Menghapus sesi setiap
# kali justru memaksa login baru terus-menerus, dan login baru berulang
# adalah pemicu utama OTP dan captcha. Sesi yang sehat harus dipertahankan.
AUTO_CLEAR_STALE_SESSION = _flag("AUTO_CLEAR_STALE_SESSION", True)
