"""Alur login TikTok (email + password + verifikasi OTP), logout, dan cek sesi."""

import asyncio
import json
import os
import re
import time

from playwright.async_api import Page

import config
# Kredensial dibaca lewat `config.NAMA` supaya override dari gui.py
# (config.apply_credentials) ikut terbaca. Sisanya nilai statis.
from config import FAST_TIMEOUT, SLOW_TIMEOUT, STORAGE_STATE_PATH
from otp_utils import wait_for_new_otp, latest_tiktok_uid
from perf import POLL_INTERVAL, click_first, first_match
from ui_selectors import APP_POPUP_SELECTORS, CAPTCHA_SELECTOR, LOGIN_BUTTON_SELECTORS

LOGIN_SCREENSHOT_PATH = "tiktok_login_success.png"

# File sesi Playwright tidak menyimpan email pemiliknya, dan is_logged_in()
# hanya bisa menjawab "ada yang login" - bukan "akun yang mana". Tanpa catatan
# ini, sesi akun sebelumnya yang tertinggal akan dipakai ulang dan laporan
# terkirim atas nama akun yang salah tanpa peringatan apa pun.
SESSION_OWNER_PATH = STORAGE_STATE_PATH + ".owner"

# CATATAN BAHASA: UI TikTok bisa muncul dalam Bahasa Indonesia atau Inggris,
# tidak konsisten walau locale di-set "id-ID". Karena itu selector selalu
# diprioritaskan memakai `data-e2e` (stabil, tidak tergantung bahasa) dan baru
# fallback ke pencocokan teks dwibahasa.

# Pencocokan teks dibuat eksak lewat regex: engine `text=` mencocokkan
# SUBSTRING, jadi `text=Buat` juga cocok dengan "Buat efek" atau "Dibuat oleh".
LOGGED_IN_SELECTORS = [
    '[data-e2e="profile-icon"]',
    '[data-e2e="nav-upload"]',
    "a[href*='/upload']",
    "a[href*='/creator/']",
    "text=/^Buat$/i",
    "text=/^Upload$/i",
]

VERIFY_SELECTORS = [
    "text=/Verifikasi bahwa ini memang Anda/i",
    "text=/[Vv]erify.*(it'?s|that it'?s) you/i",
]

CAPTCHA_SELECTORS = [CAPTCHA_SELECTOR]

LOGIN_ERROR_SELECTORS = [
    "text=/Kata sandi.*salah/i",
    "text=/[Ii]ncorrect password/i",
    "text=/Akun tidak ditemukan/i",
    "text=/[Cc]ouldn'?t find (your )?account/i",
    "text=/Terlalu banyak percobaan/i",
    "text=/[Tt]oo many attempts/i",
]

OTP_ERROR_SELECTORS = [
    "text=/Kode verifikasi kedaluwarsa atau salah/i",
    "text=/[Kk]ode.*(salah|kedaluwarsa|tidak valid)/i",
    "text=/[Cc]ode is (incorrect|expired|invalid)/i",
    "text=/[Ii]ncorrect (verification )?code/i",
]

# HANYA pemicu dropdown avatar. `[data-e2e="nav-profile"]` sengaja tidak
# dipakai: itu link "Profil" di sidebar yang tetap dirender saat sesi anonim,
# dan mengkliknya memicu navigasi - bukan membuka menu keluar.
PROFILE_MENU_SELECTORS = [
    '[data-e2e="profile-icon"]',
    'header [data-e2e="profile-icon"]',
]

LOGOUT_ITEM_SELECTORS = [
    '[data-e2e="nav-logout"]',
    "text=/^Keluar$/i",
    "text=/^Log out$/i",
    'li:has-text("Keluar")',
    'li:has-text("Log out")',
]

LOGOUT_CONFIRM_SELECTORS = [
    'button:has-text("Keluar")',
    'button:has-text("Log out")',
    'button:has-text("Ya")',
]

# Sama dengan tembok login yang dideteksi report.py - lihat ui_selectors.py.
LOGGED_OUT_SELECTORS = LOGIN_BUTTON_SELECTORS


async def _dismiss_app_popup(page: Page):
    clicked = await click_first(
        page,
        APP_POPUP_SELECTORS,
        timeout=1500,
    )
    if clicked:
        print("[LOGIN] Pop-up ditutup.")
    else:
        print("[LOGIN] Tidak ada pop-up 'buka app' yang perlu ditutup.")


async def _open_email_login_form(page: Page):
    # `data-e2e="channel-item"` dipakai beberapa pilihan (Google, Apple, dst),
    # jadi disaring lewat teksnya untuk mengambil yang benar.
    clicked = await click_first(
        page,
        ['[data-e2e="channel-item"]'],
        timeout=SLOW_TIMEOUT,
        has_text=re.compile(r"phone|email|telepon|nomor", re.I),
    )
    if not clicked:
        clicked = await click_first(
            page, ["text=/.*nomor telepon.*email.*/i", "text=/Telepon/i"], timeout=FAST_TIMEOUT
        )
    if clicked:
        print("[LOGIN] Opsi login (telepon/email) dipilih.")
    else:
        print("[WARNING] Tidak menemukan opsi 'telepon/email/username'. Mungkin sudah di form yang benar.")

    await first_match(
        page,
        ['[data-e2e="email-tab"]', 'input[type="password"]', 'input[name="username"]'],
        timeout=SLOW_TIMEOUT,
    )

    clicked = await click_first(
        page,
        ['[data-e2e="email-tab"]', "text=/Alamat email/i", "text=/Email.*Username/i"],
        timeout=1500,
    )
    if clicked:
        print("[LOGIN] Tab 'Email/Username' diklik.")
    else:
        print("[INFO] Tab 'Email/Username' tidak ditemukan (mungkin sudah aktif).")


async def _fill_credentials(page: Page) -> bool:
    try:
        email_input = page.locator('input[name="username"], input[type="text"], input[type="email"]').first
        await email_input.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        await email_input.fill(config.TIKTOK_EMAIL)
        print(f"[LOGIN] Email terisi: {config.TIKTOK_EMAIL}")
    except Exception as e:
        print(f"[ERROR] Gagal isi email: {e}")
        return False

    try:
        pass_input = page.locator('input[type="password"]').first
        await pass_input.wait_for(state="visible", timeout=SLOW_TIMEOUT)
        await pass_input.fill(config.TIKTOK_PASSWORD)
        print("[LOGIN] Password terisi.")
    except Exception as e:
        print(f"[ERROR] Gagal isi password: {e}")
        return False

    clicked = await click_first(
        page,
        [
            '[data-e2e="login-button"]:not([disabled])',
            'button:has-text("Log masuk"):not([disabled])',
            'button:has-text("Log in"):not([disabled])',
            'button[type="submit"]:not([disabled])',
        ],
        timeout=FAST_TIMEOUT,
    )
    if clicked:
        print("[LOGIN] Tombol login diklik.")
    else:
        print("[ERROR] Tombol login tidak ditemukan/tidak bisa diklik.")
        return False

    return True


async def _find_otp_input(page: Page):
    selectors = [
        "#code-input",
        ".components-code-input .input",
        'input[type="text"][maxlength="6"]',
    ]
    index = await first_match(page, selectors, timeout=SLOW_TIMEOUT)
    if index is None:
        return None
    print(f"[LOGIN] Field OTP ditemukan ({selectors[index]}).")
    return page.locator(selectors[index]).first


async def _wait_for_logged_in(page: Page, timeout: int = SLOW_TIMEOUT) -> bool:
    """Konfirmasi login berhasil sebelum sesi dipakai atau disimpan.

    Tombol "Log masuk" SENGAJA tidak dianggap kesimpulan selama waktu tunggu
    masih ada. Tepat setelah OTP dikirim, TikTok sempat merender header versi
    anonim beberapa detik sebelum avatar muncul. Kalau tombol itu ikut
    mengakhiri polling (perilaku lama: satu `first_match` atas gabungan semua
    daftar, siapa pun yang terlihat duluan menang), login yang sebenarnya
    berhasil dilaporkan gagal dan alur berhenti minta verifikasi manual.
    Hanya pesan galat OTP/login yang menghentikan tunggu lebih awal.
    """
    fatal = OTP_ERROR_SELECTORS + LOGIN_ERROR_SELECTORS
    deadline = time.monotonic() + timeout / 1000
    while True:
        # timeout=0: satu sapuan tanpa menunggu, ritme tunggu dipegang loop ini.
        if await first_match(page, LOGGED_IN_SELECTORS, timeout=0) is not None:
            return True
        outcome = await first_match(page, fatal, timeout=0)
        if outcome is not None:
            if outcome < len(OTP_ERROR_SELECTORS):
                print("[ERROR] TikTok menolak kode verifikasi.")
            else:
                print("[ERROR] Login ditolak oleh TikTok.")
            return False
        if time.monotonic() >= deadline:
            break
        await asyncio.sleep(POLL_INTERVAL)

    if await first_match(page, LOGIN_BUTTON_SELECTORS, timeout=0) is not None:
        print("[ERROR] Halaman masih menampilkan tombol login.")
    else:
        print("[ERROR] Status login tidak dapat dikonfirmasi.")
    return False


async def _handle_otp_verification(page: Page) -> bool:
    detected = await first_match(page, VERIFY_SELECTORS, timeout=FAST_TIMEOUT) is not None
    if detected:
        print("[LOGIN] Halaman verifikasi terdeteksi.")
    else:
        print("[WARNING] Tidak ada halaman verifikasi terdeteksi otomatis.")
        input("Periksa browser. Jika sudah login, tekan Enter untuk lanjut...")
        return await _wait_for_logged_in(page)

    # GARIS BATAS - harus dicatat SEBELUM kode diminta. Tanpa ini, polling
    # mengambil email TikTok terbaru yang ADA SAAT ITU, yang biasanya adalah
    # kode dari permintaan sebelumnya (email kode baru butuh beberapa detik
    # untuk sampai). Kode lama itu hangus begitu TikTok menerbitkan yang baru,
    # dan hasilnya "Kode verifikasi kedaluwarsa atau salah".
    baseline_uid = await asyncio.to_thread(latest_tiktok_uid)
    if baseline_uid:
        print(f"[OTP] Garis batas inbox dicatat: UID {baseline_uid}")
    else:
        print("[OTP] Garis batas inbox tidak terbaca (inbox kosong / IMAP gagal).")

    clicked = await click_first(
        page,
        ["text=/Alamat email/i", "text=/Email/i", "text=/.*@gmail\\.com/i"],
        timeout=FAST_TIMEOUT,
    )
    if clicked:
        print("[LOGIN] Opsi kirim OTP ke email diklik.")
    else:
        print("[WARNING] Gagal klik otomatis. Klik manual.")
        input("Klik 'Alamat email'/'Email' manual, lalu Enter...")

    # Indeks 0-1 = halaman OTP muncul, >=2 = ternyata sudah langsung login.
    outcome = await first_match(
        page,
        ["text=/Kirim ulang kode/i", "text=/[Rr]esend code/i"] + LOGGED_IN_SELECTORS,
        timeout=SLOW_TIMEOUT + 5000,
    )
    if outcome is None:
        print("[WARNING] Halaman OTP tidak muncul dan belum terlihat login.")
    elif outcome >= 2:
        print("[LOGIN] Sudah login, lewati OTP.")
        return True
    else:
        print("[LOGIN] Halaman OTP muncul.")

    otp_input = await _find_otp_input(page)
    if otp_input is None:
        print("[ERROR] Field OTP tidak ditemukan. Isi manual.")
        input("Isi OTP manual di browser, lalu tekan Enter...")
        return await _wait_for_logged_in(page)

    print("\n[OTP] Menunggu OTP terbaru...")
    # wait_for_new_otp() sinkron dan memakai time.sleep(); dijalankan di thread
    # terpisah supaya event loop (dan browser) tidak ikut membeku.
    otp_code = await asyncio.to_thread(wait_for_new_otp, after_uid=baseline_uid)

    if not otp_code:
        otp_code = input("Gagal ambil OTP otomatis. Ketik OTP 6 digit manual: ").strip()
        if not otp_code or len(otp_code) != 6 or not otp_code.isdigit():
            print("[ERROR] OTP tidak valid. Selesaikan login manual di browser.")
            input("Tekan Enter setelah login manual selesai...")
            return await _wait_for_logged_in(page)

    try:
        await otp_input.fill(otp_code)
        print(f"[LOGIN] OTP {otp_code} diisi.")
    except Exception as e:
        print(f"[ERROR] Gagal isi OTP: {e}")
        input("Isi manual di browser, lalu Enter...")

    clicked = await click_first(
        page,
        [
            'button:has-text("Verifikasi")',
            'button:has-text("Lanjutkan")',
            'button:has-text("Verify")',
            'button:has-text("Continue")',
            'button:has-text("Submit")',
        ],
        timeout=FAST_TIMEOUT,
    )
    if clicked:
        print("[LOGIN] Tombol verifikasi diklik.")
    else:
        print("[INFO] Tombol verifikasi tidak ditemukan (mungkin OTP auto-submit).")

    # Redirect pasca-OTP ke beranda kerap lebih lama dari SLOW_TIMEOUT (10s),
    # jadi jangan buru-buru melempar pengguna ke verifikasi manual.
    if await _wait_for_logged_in(page, timeout=SLOW_TIMEOUT * 3):
        return True

    print("[WARNING] Selesaikan verifikasi manual di browser, lalu tekan Enter.")
    input()
    return await _wait_for_logged_in(page)


async def login_tiktok(page: Page) -> bool:
    """Login dengan email + password, menangani verifikasi OTP.

    Return False hanya kalau form kredensial gagal total atau login ditolak.
    """
    print("\n[LOGIN] Membuka halaman login...")
    # Cookies "setengah valid" membuat TikTok menampilkan layar "lanjutkan
    # sebagai..." alih-alih chooser login normal, sehingga selector di bawah
    # gagal semua. Karena itu sisa sesi lama dibuang dulu.
    try:
        await page.context.clear_cookies()
    except Exception:
        pass

    await page.goto("https://www.tiktok.com/login", wait_until="domcontentloaded", timeout=30000)

    await _dismiss_app_popup(page)
    await _open_email_login_form(page)

    if not await _fill_credentials(page):
        return False

    # Semua kemungkinan hasil di-race sekaligus, lalu dicabangkan lewat indeks.
    n_ok = len(LOGGED_IN_SELECTORS)
    n_verify = len(VERIFY_SELECTORS)
    outcome = await first_match(
        page,
        LOGGED_IN_SELECTORS + VERIFY_SELECTORS + CAPTCHA_SELECTORS + LOGIN_ERROR_SELECTORS,
        timeout=SLOW_TIMEOUT,
    )

    if outcome is not None and outcome < n_ok:
        print("[LOGIN] Login berhasil (tanpa verifikasi).")
        success = True
    elif outcome is not None and outcome >= n_ok + n_verify + len(CAPTCHA_SELECTORS):
        print("[ERROR] Login ditolak (kredensial salah / terlalu banyak percobaan).")
        print("[ERROR] Periksa TIKTOK_EMAIL & TIKTOK_PASSWORD di .env.")
        return False
    else:
        if outcome == n_ok + n_verify:
            print("[LOGIN] Captcha terdeteksi.")
            if not await check_and_solve_captcha(page):
                print("[ERROR] Captcha belum terselesaikan.")
                return False
        print("[LOGIN] Memastikan verifikasi login selesai...")
        success = await _handle_otp_verification(page)

    if not success:
        print("[ERROR] Login belum terkonfirmasi; sesi tidak akan disimpan.")
        return False

    try:
        await page.screenshot(path=LOGIN_SCREENSHOT_PATH)
        print(f"[LOGIN] Screenshot disimpan: {LOGIN_SCREENSHOT_PATH}")
    except Exception:
        pass

    print("[LOGIN] Proses login selesai.")
    return True


async def is_logged_in(page: Page) -> bool:
    """Cek apakah sesi yang dimuat masih valid, tanpa mencoba login."""
    try:
        # "networkidle" dihindari: TikTok terus kirim request background
        # (ads/analytics) yang jarang benar-benar hening, sehingga sering
        # timeout 30 detik walau halaman sudah siap.
        await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        return await first_match(page, LOGGED_IN_SELECTORS, timeout=FAST_TIMEOUT + 2000) is not None
    except Exception:
        return False


def session_owner() -> str:
    """Email akun pemilik file sesi tersimpan; "" kalau tidak tercatat."""
    try:
        with open(SESSION_OWNER_PATH, encoding="utf-8") as handle:
            return str(json.load(handle).get("email", "")).strip()
    except Exception:
        return ""


def save_session_owner(email: str) -> None:
    """Catat akun pemilik sesi, dipanggil tiap kali storage_state disimpan."""
    try:
        with open(SESSION_OWNER_PATH, "w", encoding="utf-8") as handle:
            json.dump(
                {"email": (email or "").strip(), "saved_at": time.time()},
                handle,
            )
    except Exception as e:
        print(f"[SESI] Gagal mencatat pemilik sesi: {e}")


def session_belongs_to(email: str) -> bool:
    """True hanya kalau sesi tersimpan memang milik `email`.

    Pemilik yang tidak tercatat dianggap BUKAN milik akun ini: lebih baik
    login ulang daripada melaporkan video atas nama akun yang salah.
    """
    email = (email or "").strip().lower()
    owner = session_owner().strip().lower()
    return bool(email and owner and owner == email)


def clear_session_file(reason: str = "") -> bool:
    """Hapus file sesi tersimpan supaya run berikutnya login dari nol.

    Dipakai setelah logout disengaja, saat sesi terdeteksi rusak, dan saat
    sesi ternyata milik akun lain. Sesi yang sehat justru yang membuat OTP dan
    captcha jarang muncul, jadi jangan dihapus tanpa alasan.
    """
    # Catatan pemilik selalu ikut dibuang, termasuk kalau file sesinya sendiri
    # sudah tidak ada - catatan yang tertinggal bisa membuat sesi akun lain
    # dianggap milik akun ini di run berikutnya.
    owner_removed = False
    if os.path.exists(SESSION_OWNER_PATH):
        try:
            os.remove(SESSION_OWNER_PATH)
            owner_removed = True
        except Exception as e:
            print(f"[SESI] Gagal menghapus {SESSION_OWNER_PATH}: {e}")

    if not os.path.exists(STORAGE_STATE_PATH):
        return owner_removed
    try:
        os.remove(STORAGE_STATE_PATH)
        suffix = f" ({reason})" if reason else ""
        print(f"[SESI] File {STORAGE_STATE_PATH} dihapus{suffix}.")
        return True
    except Exception as e:
        print(f"[SESI] Gagal menghapus {STORAGE_STATE_PATH}: {e}")
        return False


async def logout_tiktok(page: Page) -> bool:
    """Keluar dari akun lewat menu profil. Butuh page dengan layout desktop."""
    print("\n[LOGOUT] Keluar dari akun...")

    # Pemeriksaan "sudah tidak login" harus di depan: kalau ditaruh sebagai
    # fallback, script terlanjur mencoba membuka menu profil dan bisa mengklik
    # elemen mirip lainnya, lalu melaporkan gagal padahal tidak ada yang perlu
    # dikerjakan.
    if await first_match(page, LOGGED_OUT_SELECTORS, timeout=1200) is not None:
        print("[LOGOUT] Sudah tidak dalam keadaan login, tidak ada yang perlu dilakukan.")
        return True

    opened = await click_first(page, PROFILE_MENU_SELECTORS, timeout=FAST_TIMEOUT)
    if not opened:
        print("[LOGOUT] Ikon profil tidak terlihat, kembali ke homepage dulu...")
        try:
            await page.goto("https://www.tiktok.com/", wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"[LOGOUT] Gagal membuka homepage: {e}")
            return False

        if await first_match(page, LOGGED_OUT_SELECTORS, timeout=FAST_TIMEOUT) is not None:
            print("[LOGOUT] Sudah tidak dalam keadaan login.")
            return True
        opened = await click_first(page, PROFILE_MENU_SELECTORS, timeout=SLOW_TIMEOUT)

    if not opened:
        print("[LOGOUT] Ikon profil tidak ditemukan - status login tidak jelas.")
        return False

    if not await click_first(page, LOGOUT_ITEM_SELECTORS, timeout=FAST_TIMEOUT):
        print("[LOGOUT] Item 'Keluar' tidak ditemukan di menu profil.")
        return False

    # Dialog konfirmasi tidak selalu ada.
    await click_first(page, LOGOUT_CONFIRM_SELECTORS, timeout=1500)

    confirmed = await first_match(page, LOGGED_OUT_SELECTORS, timeout=SLOW_TIMEOUT) is not None
    if confirmed:
        print("[LOGOUT] Berhasil keluar dari akun.")
    else:
        print("[LOGOUT] Status keluar tidak terkonfirmasi - periksa jendela browser.")
    return confirmed


async def ensure_logged_in(page: Page) -> bool:
    """Pakai sesi tersimpan kalau masih valid, kalau tidak login penuh."""
    if await is_logged_in(page):
        print("[LOGIN] Sesi tersimpan masih valid, login dilewati.")
        return True

    print("[LOGIN] Sesi tidak ada/sudah tidak valid, melakukan login penuh...")
    return await login_tiktok(page)
